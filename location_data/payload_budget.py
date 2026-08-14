"""What a retention cap actually costs — the arithmetic behind `payloads.DEFAULT_VERSION_CAP`.

02 §2.3.2 P4 gives the payload archive a version cap and leaves the number to the
operator; the number it shipped with (20) came from the design document and was never
checked against a corpus size. This module is that check, frozen as data so it can be
asserted in CI instead of re-derived in a comment.

**The cap is the CEILING; the churn rate only sets how fast the ceiling is reached.**
That inversion is the whole reason this file exists. A volatile profile that stops
working makes a listing race to its cap; it cannot make it exceed it. So the archive's
worst case is a function of two things only — the cap and the corpus — and neither one
depends on how good anyone's hand-written filters are.

**Worst case is `cap + 1` bodies per group, not `cap`.** `payloads._PRUNE_SQL` deletes
unpinned rows ranked beyond the cap, and the FIRST version is pinned, so once a group is
deeper than the cap the first version survives OUTSIDE it. Claim-referenced bodies are
pinned the same way — they are the archive's purpose rather than its overhead, and they
are bounded by distinct claim VALUES per listing (01 §4.2.1's time-free fingerprint), not
by fetches.

Every figure below is measured from production on MEASURED_AT, decimal GB (the unit
02 §2.3.2 and both storage vendors use), and re-derivable with
`scripts/location_payload_storage_ceiling.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

BYTES_PER_GB = 1_000_000_000

MEASURED_AT = "2026-08-14"

# 06's sizing envelope for the WHOLE location subsystem — RÚIAN mirror, claims,
# resolutions, projections and this archive together. Named here because the cap is
# chosen against it: at W1 gate time the subsystem already measured ~16 GB (of which
# ~7 GB is one-time re-scan bloat with a dedup guard queued), so the archive is
# competing for the remainder, not for a fresh 20 GB.
SUBSYSTEM_BUDGET_GB = 20.0


@dataclass(frozen=True, slots=True)
class PortalStorage:
    """One portal's contribution to the archive, per body and per cohort.

    * `stored_bytes_per_body` is what POSTGRES holds for one body: the raw body gzipped,
      which is what `payloads.encode_body` produces for everything above its 4 KB
      threshold — i.e. every portal here. Not the raw size, and not the normalised size
      (the normalised projection is hashed, never stored).
    * `active_listings` is today's live corpus — the cohort the dual-write archives from
      the moment it is enabled.
    * `listings_ever` is every listing row the portal has, which is the cohort the
      archive actually converges on: rule 3 delists, it never deletes, and a pinned
      first/latest body outlives the listing's activity.
    """

    source: str
    stored_bytes_per_body: int
    active_listings: int
    listings_ever: int
    provenance: str


# MEASURED, not assumed. Two live reads plus one local measurement, all reproducible:
#
#   * mean raw body size per source — `portal_raw_pages` (463,256 real bodies, the
#     legacy staging archive W2a-4 migrates from) for the seven portals that stage HTML;
#     `portal_payload_churn.last_byte_size` at payload_norm@3 for sreality and
#     bezrealitky, which stage no page and are only visible to the churn instrument.
#   * compression ratio per source — this repo's own full-size fixture bodies for that
#     portal, gzipped through `payloads.encode_body` itself. Where no full-size fixture
#     exists (bazos, maxima, bezrealitky), 0.65 x the live `pg_column_size` instead:
#     that factor is the gzip/TOAST ratio measured across the five portals that have
#     both, which sits in a tight 0.51-0.72 band.
#   * the whole model, checked against the ONE directly-measured gzip figure the project
#     has: W0 item 0o exported 447,510 pages to R2 as 7.69 GB gzipped = 17,184 B/page.
#     Applying the per-portal figures below to the same corpus predicts 17,227 B/page —
#     0.3 % apart. The estimates are not estimates in any way that matters at this scale.
PORTAL_STORAGE: tuple[PortalStorage, ...] = (
    PortalStorage("idnes", 20_175, 110_023, 196_187, "live 79,287 B / fixture 3.93x"),
    PortalStorage("sreality", 8_399, 102_536, 201_641, "churn@3 58,539 B / fixture 6.97x"),
    PortalStorage("ceskereality", 21_164, 72_395, 75_144, "live 117,883 B / fixture 5.57x"),
    PortalStorage("realitymix", 15_996, 48_135, 67_704, "live 83,097 B / fixture 5.20x"),
    PortalStorage("bazos", 7_158, 30_054, 93_544, "0.65 x live TOAST 11,013 B"),
    PortalStorage("mmreality", 35_511, 10_785, 10_785, "live 244,672 B / fixture 6.89x"),
    PortalStorage("remax", 13_456, 8_086, 11_740, "live 64,858 B / fixture 4.82x"),
    PortalStorage("bezrealitky", 1_258, 5_655, 14_794, "churn@3 5,033 B / 4.0x assumed"),
    PortalStorage("maxima", 11_274, 266, 447, "0.65 x live TOAST 17,345 B"),
)

COHORTS = ("active", "ever")


def one_body_bytes(cohort: str = "active") -> int:
    """Bytes to hold exactly ONE body for every listing in the cohort.

    This is the archive's irreducible unit: the cap multiplies it, and no cap divides it.
    A cap of 1 does not buy a smaller archive than this — it buys the pins, which are two
    bodies wherever a listing's page has ever changed.
    """
    if cohort not in COHORTS:
        raise ValueError(f"unknown cohort {cohort!r}; expected one of {COHORTS}")
    field = "active_listings" if cohort == "active" else "listings_ever"
    return sum(p.stored_bytes_per_body * getattr(p, field) for p in PORTAL_STORAGE)


def bodies_per_group(cap: int) -> int:
    """Worst-case bodies retained for one (listing, page_kind): the cap plus the pinned first."""
    if cap < 1:
        raise ValueError(f"version cap {cap} is not a retention policy")
    return cap + 1


def ceiling_gb(cap: int, cohort: str = "active") -> float:
    """The archive's worst case in decimal GB at this cap — the number the operator signs."""
    return bodies_per_group(cap) * one_body_bytes(cohort) / BYTES_PER_GB


def ceiling_table(caps: tuple[int, ...] = (1, 2, 3, 5, 10, 20)) -> list[dict[str, object]]:
    """Ceiling vs cap over both cohorts, ready to print or to diff against a live re-read."""
    return [
        {
            "cap": cap,
            "bodies_per_group": bodies_per_group(cap),
            "active_gb": round(ceiling_gb(cap, "active"), 1),
            "ever_gb": round(ceiling_gb(cap, "ever"), 1),
        }
        for cap in caps
    ]
