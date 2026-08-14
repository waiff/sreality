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

TWO FOOTPRINTS, NOT ONE, because bodies do not live in Postgres. `payloads` spills every
body whose compressed form is larger than a heap tuple can hold for free to R2 and keeps
the metadata row — identity, both hashes, sizes, version, pin state, the key — in
Postgres. So a cap costs:

  * **Postgres**, in ROWS: heap tuple plus five index entries, ~`postgres_row_bytes()`
    each, against `ARCHIVE_ALLOWANCE_GB` — what is LEFT of the subsystem's envelope, not
    the whole envelope. That is the number the gate asserts.
  * **R2**, in BYTES: the compressed bodies, at `R2_USD_PER_GB_MONTH`. Object storage is
    ~1/100th the price of database storage, so this is a cents-per-month line item and
    is reported rather than gated.

The split is not an optimisation, it is the reason the archive is affordable at all.
Postgres-resident bodies would also tax the whole instance's shared buffer cache, which
this platform has been burned by twice (the Browse statement-timeout saga; the 2026-08-10
multi-lane incident) — and nothing on a latency-critical path ever reads a body. The
readers are the W2 re-mine, one-off backfills and the round-trip verifier: all batch.

Every figure below is measured from production on MEASURED_AT, decimal GB (the unit
02 §2.3.2 and both storage vendors use), and re-derivable with
`scripts/location_payload_storage_ceiling.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

BYTES_PER_GB = 1_000_000_000

MEASURED_AT = "2026-08-14"

# 06's sizing envelope for the WHOLE location subsystem — RÚIAN mirror, claims,
# resolutions, projections and this archive together.
SUBSYSTEM_BUDGET_GB = 20.0

# ...AND WHAT IS ALREADY SPENT OF IT. Measured at the W1 gate: the 3.02 M-point RÚIAN
# mirror, the claim spine, the resolution and projection tables. ~7 GB of that is
# one-time re-scan bloat with a dedup guard queued, so this number should FALL — but a
# budget gate must assert against today's remainder, not against a hoped-for one.
#
# Naming it separately is the whole correction. The gate used to compare the archive's
# ceiling against SUBSYSTEM_BUDGET_GB, i.e. against a 20 GB envelope the archive does not
# have; the real allowance is the difference, and it is four times smaller.
SUBSYSTEM_SPENT_GB = 16.0

# What the archive may take in POSTGRES. Derived, never hand-set, so re-measuring the
# subsystem moves the gate rather than requiring two edits that can disagree.
ARCHIVE_ALLOWANCE_GB = SUBSYSTEM_BUDGET_GB - SUBSYSTEM_SPENT_GB

# Cloudflare R2 standard storage, list price. Egress is free and the archive's readers
# are batch jobs inside the same account, so storage is the whole bill that scales with
# the cap; Class A (PUT) operations are one per appended body and are priced per million.
R2_USD_PER_GB_MONTH = 0.015

# `payloads.DEFAULT_R2_THRESHOLD_BYTES`, restated here because the model needs it and
# `payload_budget` must not import the writer (the writer's chokepoint imports THIS, to
# refuse a surface nobody has costed). `test_payload_budget` asserts the two agree, so
# they cannot drift.
INLINE_THRESHOLD_BYTES = 2048


@dataclass(frozen=True, slots=True)
class PortalStorage:
    """One portal SURFACE's contribution to the archive, per body and per cohort.

    * `page_kind` is which surface these figures describe. Every row today is `detail`,
      the only surface `payload_dual_write` archives; the index surfaces are gated behind
      `payload_index_archive`, have never been profiled, and are NOT modelled here —
      which is why `is_measured` exists and why the write chokepoint consults it. A
      corpus that silently omits a surface is how a storage projection reads low and gets
      signed.
    * `stored_bytes_per_body` is what one body COSTS, wherever it lives: the raw body
      gzipped, which is what `payloads.encode_body` produces for everything above its
      4 KB threshold — i.e. every portal here. Not the raw size, and not the normalised
      size (the normalised projection is hashed, never stored). Bodies at or under
      `INLINE_THRESHOLD_BYTES` stay in Postgres; the rest are R2 bytes.
    * `groups_active` / `groups_ever` are how many (key, page_kind) GROUPS this surface
      contributes — for a detail surface, one per listing, which is why they are read off
      `listings`. An index surface's groups would be week-stamped index positions, not
      listings, which is exactly why the field is not called `listings`.
    """

    source: str
    page_kind: str
    stored_bytes_per_body: int
    groups_active: int
    groups_ever: int
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
    PortalStorage("idnes", "detail", 20_175, 110_023, 196_187, "live 79,287 B / fixture 3.93x"),
    PortalStorage("sreality", "detail", 8_399, 102_536, 201_641, "churn@3 58,539 B / fixture 6.97x"),
    PortalStorage("ceskereality", "detail", 21_164, 72_395, 75_144, "live 117,883 B / fixture 5.57x"),
    PortalStorage("realitymix", "detail", 15_996, 48_135, 67_704, "live 83,097 B / fixture 5.20x"),
    PortalStorage("bazos", "detail", 7_158, 30_054, 93_544, "0.65 x live TOAST 11,013 B"),
    PortalStorage("mmreality", "detail", 35_511, 10_785, 10_785, "live 244,672 B / fixture 6.89x"),
    PortalStorage("remax", "detail", 13_456, 8_086, 11_740, "live 64,858 B / fixture 4.82x"),
    PortalStorage("bezrealitky", "detail", 1_258, 5_655, 14_794, "churn@3 5,033 B / 4.0x assumed"),
    PortalStorage("maxima", "detail", 11_274, 266, 447, "0.65 x live TOAST 17,345 B"),
)

COHORTS = ("active", "ever")


@dataclass(frozen=True, slots=True)
class RelationBytes:
    """One heap or index component of what a metadata ROW costs in Postgres."""

    name: str
    bytes_per_row: int
    provenance: str


# The Postgres side of the ledger, per archived body, with the body itself in R2.
#
# MEASURED, like the page weights above, not derived: 200,000 representative rows —
# `body` NULL, a real 87-character content-addressed key, both 32-byte digests, an idnes
# `source_id_native` — loaded into the applied 382+403+405 shape on PostgreSQL 18.4,
# VACUUM ANALYZEd, then read off `pg_relation_size` per relation. The corresponding hand
# derivation (32 B tuple header + 272 B of data at column alignment, index entries at key
# width + 12 B over a 0.75 fill factor) predicts 720 B against the 713 B measured, 1 %
# apart, so either method would have answered the question; the measurement is what is
# frozen because it is the one that can be re-run.
#
# `scripts/location_payload_storage_ceiling.py` re-derives it from
# `pg_total_relation_size` once production holds rows, and warns on drift.
#
# THE BODY IS THE POINT. The same row with a 20 KB gzipped body inline costs ~20,700 B —
# 29x this — and puts that body in the shared buffer cache of an instance that serves
# Browse. Nothing on a latency-critical path ever reads a payload body (the readers are
# the W2 re-mine, one-off backfills, and the round-trip verifier — all batch), so the
# bytes buy nothing in Postgres that they do not buy more cheaply in R2.
#
# prp_listing is absent ON PURPOSE, not forgotten: it is partial on
# `listing_id IS NOT NULL`, and both writers (the live chokepoint and W2a-4's backfill)
# insert NULL, so it indexes nothing this archive writes — 0.04 B/row measured.
#
# prp_r2_key is the component the R2 default ADDS, and it is the largest index in the
# ledger. It was partial-and-empty at the 256 KB threshold nothing reached; at 2 KB it
# holds an entry for essentially every row. That cost buys the reclaim check
# (`payloads._ORPHANED_KEYS_SQL`) that stops an evicted row's DELETE from reporting a
# live row's shared, content-addressed object as reclaimable.
POSTGRES_ROW_LAYOUT: tuple[RelationBytes, ...] = (
    RelationBytes("heap tuple", 328, "pg_relation_size / rows, body NULL"),
    RelationBytes("prp_r2_key (body_r2_key)", 149,
                  "87-char content-addressed key; empty before the R2 default"),
    RelationBytes("identity unique (source, native, kind, payload_sha256)", 86,
                  "382's uniqueness constraint"),
    RelationBytes("prp_sha (payload_sha256)", 77, "382"),
    RelationBytes("prp_native (source, native, kind, first_observed_at)", 50,
                  "382; the index the time floor's window arm probes"),
    RelationBytes("pkey (id)", 23, "382"),
)


def postgres_row_bytes() -> int:
    """What ONE archived body costs Postgres when its body is in R2 — heap and indexes."""
    return sum(c.bytes_per_row for c in POSTGRES_ROW_LAYOUT)


def _surfaces(page_kind: str | None = None) -> tuple[PortalStorage, ...]:
    if page_kind is None:
        return PORTAL_STORAGE
    return tuple(p for p in PORTAL_STORAGE if p.page_kind == page_kind)


def measured_surfaces() -> frozenset[tuple[str, str]]:
    """Every (source, page_kind) whose page weight this table carries."""
    return frozenset((p.source, p.page_kind) for p in PORTAL_STORAGE)


def is_measured(source: str, page_kind: str) -> bool:
    """May the archive accept this surface at all?

    The frozen corpus is the authority rather than a footnote: a surface nobody has
    weighed cannot appear in the ceiling, so archiving it would make the number the
    operator signed silently wrong. The write chokepoint refuses an unmeasured surface
    for exactly that reason — which is also what forces whoever enables
    `payload_index_archive` to profile the index surface first, instead of discovering
    its cost on the storage bill.
    """
    return (source, page_kind) in measured_surfaces()


def _groups(cohort: str, page_kind: str | None) -> tuple[tuple[PortalStorage, int], ...]:
    if cohort not in COHORTS:
        raise ValueError(f"unknown cohort {cohort!r}; expected one of {COHORTS}")
    field = "groups_active" if cohort == "active" else "groups_ever"
    return tuple((p, int(getattr(p, field))) for p in _surfaces(page_kind))


def group_count(cohort: str = "ever", page_kind: str | None = "detail") -> int:
    """How many (key, page_kind) groups the cap multiplies."""
    return sum(n for _p, n in _groups(cohort, page_kind))


def one_body_bytes(cohort: str = "ever", page_kind: str | None = "detail") -> int:
    """Bytes to hold exactly ONE body for every group in the cohort, wherever it lives.

    This is the archive's irreducible unit: the cap multiplies it, and no cap divides it.
    A cap of 1 does not buy a smaller archive than this — it buys the pins, which are two
    bodies wherever a listing's page has ever changed. Since W2a-7 those bytes are R2
    bytes rather than database bytes, which is what took this number off the critical
    path; `postgres_ceiling_gb` is the one that has to fit a budget.
    """
    return sum(p.stored_bytes_per_body * n for p, n in _groups(cohort, page_kind))


def inline_body_bytes(cohort: str = "ever", page_kind: str | None = "detail") -> int:
    """Of `one_body_bytes`, the part that stays in Postgres — bodies under the threshold.

    Modelled on each surface's MEAN stored body, which is the honest resolution for a
    frozen table: only bezrealitky's JSON (1.3 KB gzipped) sits under the threshold at
    all, and it is 0.2 % of the corpus, so a per-body distribution would be false
    precision on a rounding error.
    """
    return sum(p.stored_bytes_per_body * n for p, n in _groups(cohort, page_kind)
               if p.stored_bytes_per_body <= INLINE_THRESHOLD_BYTES)


def bodies_per_group(cap: int) -> int:
    """Worst-case bodies retained for one (listing, page_kind): the cap plus the pinned first."""
    if cap < 1:
        raise ValueError(f"version cap {cap} is not a retention policy")
    return cap + 1


def postgres_ceiling_gb(
    cap: int, cohort: str = "ever", page_kind: str | None = "detail",
) -> float:
    """The archive's worst case IN POSTGRES at this cap — the number the gate asserts.

    Metadata rows plus the inline residue (bodies too small to be worth an object). It
    is dominated by row overhead rather than by body size, which is the entire point of
    spilling: it moves with the corpus and the cap, not with how heavy a portal's HTML is.
    """
    rows = bodies_per_group(cap) * group_count(cohort, page_kind)
    inline = bodies_per_group(cap) * inline_body_bytes(cohort, page_kind)
    return (rows * postgres_row_bytes() + inline) / BYTES_PER_GB


def r2_ceiling_gb(
    cap: int, cohort: str = "ever", page_kind: str | None = "detail",
) -> float:
    """The archive's worst case IN R2 at this cap — reported, not gated."""
    spilled = one_body_bytes(cohort, page_kind) - inline_body_bytes(cohort, page_kind)
    return bodies_per_group(cap) * spilled / BYTES_PER_GB


def r2_cost_usd_per_month(
    cap: int, cohort: str = "ever", page_kind: str | None = "detail",
) -> float:
    """What the R2 ceiling costs a month. Cents, which is why it is not the constraint."""
    return r2_ceiling_gb(cap, cohort, page_kind) * R2_USD_PER_GB_MONTH


def largest_affordable_cap(
    cohort: str = "ever", page_kind: str | None = "detail", limit: int = 100,
) -> int:
    """The deepest cap whose Postgres ceiling still fits `ARCHIVE_ALLOWANCE_GB`.

    The headroom, published rather than left implicit: it is what tells an operator
    whether a re-mine that wants deeper history can have it by editing one constant, and
    it is what makes the shipped default a CHOICE rather than the only option that fit.
    """
    cap = 0
    while cap < limit and postgres_ceiling_gb(cap + 1, cohort, page_kind) <= ARCHIVE_ALLOWANCE_GB:
        cap += 1
    return cap


def ceiling_table(
    caps: tuple[int, ...] = (1, 2, 3, 5, 10, 20), cohort: str = "ever",
    page_kind: str | None = "detail",
) -> list[dict[str, object]]:
    """Ceiling vs cap in BOTH footprints — the table the operator signs."""
    return [
        {
            "cap": cap,
            "bodies_per_group": bodies_per_group(cap),
            "rows": bodies_per_group(cap) * group_count(cohort, page_kind),
            "postgres_gb": round(postgres_ceiling_gb(cap, cohort, page_kind), 2),
            "r2_gb": round(r2_ceiling_gb(cap, cohort, page_kind), 1),
            "r2_usd_month": round(r2_cost_usd_per_month(cap, cohort, page_kind), 2),
            "fits_allowance": postgres_ceiling_gb(cap, cohort, page_kind) <= ARCHIVE_ALLOWANCE_GB,
        }
        for cap in caps
    ]
