"""Read out the W2a shadow-hash churn instrument — the artefact the storage gate is signed from.

02 §2.3.2 P1 makes the payload archive content-addressed on a NORMALISED body and
turns the measurement into a gate: *"Measure churn before P2 is enabled — this is a
gate, not a preference … fetch 200 listings × 3 fetches per portal, compute
raw-vs-normalised change rates, and only then set each contract's `volatile_paths` and
enable P2. One afternoon decides whether index archiving costs ~1 GB/month or ~1
TB/year."* The instrument (migration 402, `scraper.db.record_payload_churn`) has been
counting since the `location_payload_shadow_hash` flag was flipped; this module is the
only thing that reads it.

**The rate denominator is repeat fetches, never fetches.** A key's FIRST sighting cannot
be a change — there is nothing to compare it against, and `record_payload_churn` scores
it as one fetch and zero changes. So a surface's change rate is

    sum(raw_changes) / sum(fetches - 1)  ==  sum(raw_changes) / (sum(fetches) - keys)

and dividing the summed changes by the summed fetches instead would understate every
rate by exactly the share of the sample that was seen once. On a week-old instrument
whose keys average two fetches that is a factor of two — straight into the number the
operator signs. `Surface.repeat_fetches` is that corrected denominator and nothing else
in this module divides by `fetches`.

**Cohorts are (source, page_kind, normalizer_version), never just source.** Detail and
index bodies churn for entirely different reasons (an index page re-orders on every
walk); `normalizer_version` splits a rolling profile change into clean before/after
cohorts (migration 402) — it names the ENGINE and the CONTRACT VERSION that supplied
the volatile paths (`payload_norm@3+contract@2`), or `…+base` where the contract
declares none for that surface — and the confirmation probe writes into its own
`…+probe` cohort so its three-fetches-in-ten-minutes cadence can never blend into the
passive measurement's ~6-hourly one. Because a profile rollout is DESIGNED to leave two
cohorts on one `(source, page_kind)`, the fleet totals sum the NEWEST cohort per surface
and name the superseded ones — summing both would silently double the signed number.

**The projection's cadence is the OBSERVED refetch interval, never the declared one.**
`CYCLES_PER_DAY` below is each portal's INDEX-WALK schedule, and it is the only cadence
02 §2.3.2 states — but a detail body is refetched only when the index signals a change
(`listing_detail_queue`, rule 19), so on a detail surface the walk cadence is not the
refetch cadence and multiplying by it overstates the bill by whatever the ratio happens
to be. The instrument records `(last_seen_at - first_seen_at) / (fetches - 1)` per key,
which IS the refetch cadence, so that is what `gb_per_month` multiplies; the declared
constant is the fallback for a surface with no repeat yet, and a divergence beyond
`CADENCE_DIVERGENCE_FACTOR` is printed as a loud marker on the row rather than left for
the reader to spot across two columns.

**Index keys are week-stamped, so rows are not artefacts.** All three index archivers key
their churn rows `…/{offset}/{week}` (`db.index_archive_week`), so one index PAGE POSITION
opens a new row every ISO week the instrument stays on and `count(*)` grows with the
measurement window instead of with the surface. One cycle is one pass over the POSITIONS,
so the per-cycle base is the distinct week-stripped key count (`artefacts`), which on a
detail surface is exactly `keys`.

**Projections are suppressed, not estimated, when the sample is thin.** A surface where
no key was fetched twice has no rate at all; one with a handful of repeats has a rate
that is noise. Both print an explicit `INSUFFICIENT` marker where the GB number would
be — the operator must never read a projection built on one fetch.

Read-only: every statement here is a SELECT, and tests/location_data/
test_payload_churn_report.py pins that statically.

Usage:
  python -m scripts.location_payload_churn_report
  python -m scripts.location_payload_churn_report --json > churn.json
Required: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from location_data import loader_db
from location_data.payload_norm import (
    BASE_PROFILE_SUFFIX,
    CONTRACT_PROFILE_SUFFIX,
    NORMALIZER_VERSION,
    PROBE_NORMALIZER_SUFFIX,
)
from scraper import db

LOG = logging.getLogger("location_payload_churn_report")

# Decimal GB, the unit 02 §2.3.2's own arithmetic uses (445,191 pages x ~70 KB
# "is ~31 GB per full refetch cycle" is 10^3-based, and both R2 and Supabase
# price storage decimally). Printed in every header so it is never ambiguous.
BYTES_PER_GB = 1_000_000_000
DAYS_PER_MONTH = 30.0
SECONDS_PER_DAY = 86_400.0
SECONDS_PER_HOUR = 3_600.0

# THE DECLARED CADENCE — each portal's INDEX-WALK schedule, and a FALLBACK ONLY.
# 02 §2.3.2: "the 6 h portals run ~4 cycles/day" and "sreality's hourly index walk
# … is worse by an order of magnitude". That is the walk, so it is the right
# cadence for a page_kind='index' surface and the WRONG one for page_kind='detail'
# — a detail body is refetched only when the index enqueues it (rule 19), which on
# sreality is orders of magnitude rarer than 24x/day. Nothing is projected from
# this table when the instrument has an observed interval to project from instead;
# see Surface.projection_cycles_per_day.
CYCLES_PER_DAY: dict[str, float] = {
    "sreality": 24.0,
    "bazos": 4.0,
    "bezrealitky": 4.0,
    "ceskereality": 4.0,
    "idnes": 4.0,
    "maxima": 4.0,
    "mmreality": 4.0,
    "realitymix": 4.0,
    "remax": 4.0,
}

# A rate needs repeat observations, and a PORTAL's rate needs them spread over
# more than a handful of listings: 200 refetches of one listing measure that
# listing, not the portal. Both floors must clear before a GB number is printed.
MIN_REPEAT_FETCHES = 30
MIN_KEYS_REPEATED = 10

INSUFFICIENT_NO_REPEAT = "INSUFFICIENT: no key was fetched twice"
INSUFFICIENT_FEW_REPEATS = f"INSUFFICIENT: < {MIN_REPEAT_FETCHES} repeat fetches"
INSUFFICIENT_FEW_KEYS = f"INSUFFICIENT: < {MIN_KEYS_REPEATED} keys fetched twice"
INSUFFICIENT_NO_SIZE = "INSUFFICIENT: no body size recorded"
# A repeat fetch can contribute at most one change, so a rate above 1 is not a high
# rate — it is proof the counters and the fetch total disagree (a partial delete of the
# instrument's rows would do it). Naming it beats projecting from it.
ANOMALY_RATE_ABOVE_ONE = "ANOMALY: change rate > 100% — counters are inconsistent"
UNKNOWN_CADENCE = "no cadence constant for this source"

# CYCLES_PER_DAY is the INDEX-WALK schedule; the observed interval is how often this
# surface's artefacts were actually refetched. On a detail surface those are different
# questions (rule 19: a detail body is refetched when the index enqueues it), so the two
# diverging is expected — but a projection multiplied by the wrong one of them is a
# wrong tens-of-GB answer, so past this ratio the row says so in words.
CADENCE_DIVERGENCE_FACTOR = 2.0
CADENCE_DIVERGED = (
    "CADENCE {ratio:.0f}x: declared {declared:g}/day (index walk) vs observed "
    "{observed:.2f}/day — projected at OBSERVED"
)
CADENCE_DECLARED_ONLY = "no observed interval — projected at the DECLARED index-walk cadence"
NO_CADENCE = "NO CADENCE: no observed interval and no cadence constant"

DETAIL = "detail"

STATEMENT_TIMEOUT_ENV = "LOCATION_CHURN_REPORT_TIMEOUT_S"
DEFAULT_STATEMENT_TIMEOUT_S = 120

# One grouped read of the whole instrument. The per-key refetch interval has to be
# computed per ROW before it is averaged — (last_seen_at - first_seen_at) / (fetches - 1)
# is a per-key quantity, and averaging the numerator and denominator separately would
# weight a key fetched 40 times the same as one fetched twice — so the keys CTE carries
# it and the aggregate reads it. `nullif(fetches - 1, 0)` drops the once-seen keys from
# the interval statistics (they have no interval) without dropping them from `keys`,
# where they still count towards the sample and towards the projection's key base.
#
# `artefact_key` is the second per-ROW quantity the aggregate cannot reconstruct: index
# churn keys are WEEK-STAMPED upstream (…/{offset}/{week} — scraper.db.index_archive_week,
# and migration 402's own header says so), so a single index page position opens a new row
# every ISO week the instrument stays on. count(*) would therefore report pages x weeks
# where the per-cycle projection needs pages, growing linearly with the measurement
# window. Stripping the suffix and counting DISTINCT gives the artefact count; the CASE
# keeps a detail native id that happens to end in something week-shaped untouched.
#
# Every projected column carries an explicit alias, the first three redundantly: the
# projection ORDER is a contract with `surface_from_row`, and the alias list is what
# tests/location_data/test_payload_churn_report.py reads to pin it (a positional reader
# against a silently reordered SELECT inverts raw-vs-norm without failing anything).
_CHURN_SURFACE_SQL = """
    WITH per_key AS (
        SELECT source,
               page_kind::text AS page_kind,
               normalizer_version,
               CASE WHEN page_kind::text = 'index'
                    THEN regexp_replace(source_id_native, '/[0-9]{4}w[0-9]{2}$', '')
                    ELSE source_id_native END AS artefact_key,
               fetches,
               raw_changes,
               norm_changes,
               last_byte_size,
               last_norm_byte_size,
               first_seen_at,
               last_seen_at,
               (extract(epoch FROM (last_seen_at - first_seen_at))
                  / nullif(fetches - 1, 0))::double precision AS refetch_interval_s
          FROM portal_payload_churn
    )
    SELECT source                                     AS source,
           page_kind                                  AS page_kind,
           normalizer_version                         AS normalizer_version,
           count(*)                                   AS keys,
           count(DISTINCT artefact_key)               AS artefacts,
           count(*) FILTER (WHERE fetches > 1)        AS keys_repeated,
           coalesce(sum(fetches), 0)                  AS fetches,
           coalesce(sum(raw_changes), 0)              AS raw_changes,
           coalesce(sum(norm_changes), 0)             AS norm_changes,
           avg(last_byte_size::double precision)      AS mean_raw_bytes,
           percentile_cont(0.5) WITHIN GROUP (
               ORDER BY last_byte_size::double precision)      AS median_raw_bytes,
           avg(last_norm_byte_size::double precision) AS mean_norm_bytes,
           percentile_cont(0.5) WITHIN GROUP (
               ORDER BY last_norm_byte_size::double precision) AS median_norm_bytes,
           avg(refetch_interval_s)                    AS mean_interval_s,
           percentile_cont(0.5) WITHIN GROUP (
               ORDER BY refetch_interval_s)           AS median_interval_s,
           min(first_seen_at)                         AS window_start,
           max(last_seen_at)                          AS window_end
      FROM per_key
     GROUP BY 1, 2, 3
     ORDER BY 1, 2, 3
"""

# The measured keys are the artefacts the instrument HAPPENED to see while the flag was
# on; a full refetch cycle is over the portal's live inventory. Both bases are reported
# — the measured one is the measurement, the scaled one is the number the fleet will
# actually spend — and neither is presented as the other.
_ACTIVE_INVENTORY_SQL = """
    SELECT source, count(*) AS active_listings
      FROM listings
     WHERE is_active
     GROUP BY 1
"""


@dataclass(frozen=True)
class SurfaceRow:
    """One (source, page_kind, normalizer_version) group, as the statement returns it."""

    source: str
    page_kind: str
    normalizer_version: str
    keys: int
    artefacts: int
    keys_repeated: int
    fetches: int
    raw_changes: int
    norm_changes: int
    mean_raw_bytes: float | None
    median_raw_bytes: float | None
    mean_norm_bytes: float | None
    median_norm_bytes: float | None
    mean_interval_s: float | None
    median_interval_s: float | None
    window_start: datetime.datetime | None
    window_end: datetime.datetime | None


@dataclass(frozen=True)
class Surface:
    """One cohort's readout: the corrected rates and the projections they support."""

    row: SurfaceRow

    @property
    def repeat_fetches(self) -> int:
        """Fetches that COULD have been a change: sum(fetches - 1) over the keys.

        Every key contributes one first sighting that is not a change opportunity, so
        the count of keys is exactly what has to come off the fetch total.
        """
        return max(0, self.row.fetches - self.row.keys)

    @property
    def is_probe_cohort(self) -> bool:
        return self.row.normalizer_version.endswith(PROBE_NORMALIZER_SUFFIX)

    @property
    def artefacts(self) -> int:
        """Distinct artefacts behind the rows — the base ONE cycle passes over.

        Identical to `keys` on a detail surface. On an index surface the churn key is
        week-stamped upstream, so `keys` is positions x ISO weeks measured and only the
        distinct count is the thing a walk touches once per pass.
        """
        return self.row.artefacts or self.row.keys

    @property
    def raw_change_rate(self) -> float | None:
        if not self.repeat_fetches:
            return None
        return self.row.raw_changes / self.repeat_fetches

    @property
    def norm_change_rate(self) -> float | None:
        if not self.repeat_fetches:
            return None
        return self.row.norm_changes / self.repeat_fetches

    @property
    def insufficient(self) -> str | None:
        """Why this surface cannot carry a projection, or None when it can."""
        if not self.repeat_fetches:
            return INSUFFICIENT_NO_REPEAT
        if self.repeat_fetches < MIN_REPEAT_FETCHES:
            return INSUFFICIENT_FEW_REPEATS
        if self.row.keys_repeated < MIN_KEYS_REPEATED:
            return INSUFFICIENT_FEW_KEYS
        if not self.row.mean_raw_bytes:
            return INSUFFICIENT_NO_SIZE
        if max(self.raw_change_rate or 0.0, self.norm_change_rate or 0.0) > 1.0:
            return ANOMALY_RATE_ABOVE_ONE
        return None

    @property
    def cycles_per_day(self) -> float | None:
        return CYCLES_PER_DAY.get(self.row.source)

    @property
    def observed_cycles_per_day(self) -> float | None:
        """How often the instrument actually saw this surface's artefacts refetched."""
        if not self.row.mean_interval_s:
            return None
        return SECONDS_PER_DAY / self.row.mean_interval_s

    @property
    def projection_cycles_per_day(self) -> float | None:
        """The cadence every GB/month below is multiplied by.

        The OBSERVED interval whenever there is one: it is the measurement, and on a
        detail surface it is the only one of the two that answers "how often is this body
        refetched" (the declared constant answers "how often is the INDEX walked" — a
        different question since rule 19 split index-walk from detail-drain). The declared
        cadence is the fallback for a surface no key has been refetched on yet.
        """
        return self.observed_cycles_per_day or self.cycles_per_day

    @property
    def cadence_basis(self) -> str | None:
        if self.observed_cycles_per_day:
            return "observed"
        return "declared" if self.cycles_per_day else None

    @property
    def cadence_note(self) -> str:
        """What to print beside the projection about the cadence it used.

        Empty on a surface that carries no projection: there the marker column already
        says why there is no number, and the cadence it would have used is moot.
        """
        if self.insufficient is not None:
            return ""
        declared, observed = self.cycles_per_day, self.observed_cycles_per_day
        if not observed:
            return CADENCE_DECLARED_ONLY if declared else NO_CADENCE
        if not declared:
            return UNKNOWN_CADENCE
        ratio = max(declared / observed, observed / declared)
        if ratio > CADENCE_DIVERGENCE_FACTOR:
            return CADENCE_DIVERGED.format(
                ratio=ratio, declared=declared, observed=observed,
            )
        return ""

    def gb_per_cycle(self, keys: int | None = None) -> float | None:
        """Bytes appended by one pass over `keys` artefacts, at the NORMALISED rate.

        The append-on-change store writes a row only when the normalised hash moves, and
        what it stores is the BODY — so the projection is (artefacts x rate x mean raw
        body), never the normalised projection's size, which exists only to be hashed.
        """
        return self._gb_per_cycle(self.norm_change_rate, keys)

    def gb_per_cycle_raw(self, keys: int | None = None) -> float | None:
        """The same pass with NO normaliser — what content-addressing the raw bytes costs.

        The gap between this and `gb_per_cycle` is the entire value of `volatile_paths`,
        stated in GB rather than in adjectives.
        """
        return self._gb_per_cycle(self.raw_change_rate, keys)

    def _gb_per_cycle(self, rate: float | None, keys: int | None) -> float | None:
        if self.insufficient is not None or rate is None or not self.row.mean_raw_bytes:
            return None
        base = self.artefacts if keys is None else keys
        return base * rate * self.row.mean_raw_bytes / BYTES_PER_GB

    def gb_per_month(self, keys: int | None = None) -> float | None:
        return _per_month(self.gb_per_cycle(keys), self.projection_cycles_per_day)

    def gb_per_month_raw(self, keys: int | None = None) -> float | None:
        return _per_month(self.gb_per_cycle_raw(keys), self.projection_cycles_per_day)

    def gb_per_month_observed(self, keys: int | None = None) -> float | None:
        return _per_month(self.gb_per_cycle(keys), self.observed_cycles_per_day)

    def gb_per_month_declared(self, keys: int | None = None) -> float | None:
        """The same pass at the DECLARED index-walk cadence — reported, never totalled."""
        return _per_month(self.gb_per_cycle(keys), self.cycles_per_day)


def _per_month(gb_per_cycle: float | None, cycles_per_day: float | None) -> float | None:
    if gb_per_cycle is None or cycles_per_day is None:
        return None
    return gb_per_cycle * cycles_per_day * DAYS_PER_MONTH


@dataclass(frozen=True)
class Measurement:
    surfaces: list[Surface]
    active_inventory: dict[str, int]


def surface_from_row(row: Sequence[Any]) -> Surface:
    (source, page_kind, normalizer_version, keys, artefacts, keys_repeated, fetches,
     raw_changes, norm_changes, mean_raw, median_raw, mean_norm, median_norm,
     mean_interval, median_interval, window_start, window_end) = row
    return Surface(SurfaceRow(
        source=str(source),
        page_kind=str(page_kind),
        normalizer_version=str(normalizer_version),
        keys=int(keys),
        artefacts=int(artefacts),
        keys_repeated=int(keys_repeated),
        fetches=int(fetches),
        raw_changes=int(raw_changes),
        norm_changes=int(norm_changes),
        mean_raw_bytes=_opt_float(mean_raw),
        median_raw_bytes=_opt_float(median_raw),
        mean_norm_bytes=_opt_float(mean_norm),
        median_norm_bytes=_opt_float(median_norm),
        mean_interval_s=_opt_float(mean_interval),
        median_interval_s=_opt_float(median_interval),
        window_start=window_start,
        window_end=window_end,
    ))


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.UTC)


def newest_cohorts(surfaces: Sequence[Surface]) -> tuple[list[Surface], list[Surface]]:
    """Split into one cohort per (source, page_kind) plus the cohorts it supersedes.

    A normaliser rollout is DESIGNED to leave two `normalizer_version` rows on one
    surface (migration 402 opens a clean cohort rather than relabelling), so summing every
    row into a fleet total doubles it for as long as the rollout takes — silently, because
    both rows are individually correct. The cohort still being written is the one with the
    later `window_end`; the version string breaks a tie.

    The probe cohort is grouped separately, so the confirmation probe can never supersede
    the passive cohort it exists to confirm.
    """
    best: dict[tuple[str, str, bool], Surface] = {}
    for surface in surfaces:
        key = (surface.row.source, surface.row.page_kind, surface.is_probe_cohort)
        current = best.get(key)
        if current is None or _cohort_rank(surface) > _cohort_rank(current):
            best[key] = surface
    newest = list(best.values())
    superseded = [s for s in surfaces if s not in newest]
    return newest, superseded


def _cohort_rank(surface: Surface) -> tuple[datetime.datetime, str]:
    return (surface.row.window_end or _EPOCH, surface.row.normalizer_version)


def measure(
    conn: psycopg.Connection, *, statement_timeout_s: int, with_inventory: bool = True,
) -> Measurement:
    """Both reads, each inside its own bounded transaction.

    `loader_db.bounded` rather than a session SET: `db.connect()` is autocommit against
    the transaction-mode pooler, where a session-level timeout can land on a different
    backend than the statement it was meant to guard.
    """
    with loader_db.bounded(conn, statement_timeout_s) as cur:
        cur.execute(_CHURN_SURFACE_SQL)
        rows = cur.fetchall()
    LOG.info("CHURN surfaces=%d", len(rows))

    inventory: dict[str, int] = {}
    if with_inventory:
        with loader_db.bounded(conn, statement_timeout_s) as cur:
            cur.execute(_ACTIVE_INVENTORY_SQL)
            inventory = {str(source): int(count) for source, count in cur.fetchall()}
        LOG.info("CHURN active-inventory sources=%d", len(inventory))

    return Measurement(
        surfaces=[surface_from_row(row) for row in rows], active_inventory=inventory,
    )


# ------------------------------------------------------------------ rendering


def _num(value: int) -> str:
    return f"{value:,}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{100.0 * value:.1f}%"


def _kb(value: float | None) -> str:
    return "—" if value is None else f"{value / 1000.0:,.1f}"


def _hours(seconds: float | None) -> str:
    return "—" if seconds is None else f"{seconds / SECONDS_PER_HOUR:.2f}"


def _gb(value: float | None) -> str:
    return "—" if value is None else f"{value:,.2f}"


def _stamp(value: datetime.datetime | None) -> str:
    return "—" if value is None else value.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M")


def _assumptions() -> list[str]:
    cadence = ", ".join(
        f"{source}={cycles:g}/day"
        for source, cycles in sorted(CYCLES_PER_DAY.items())
    )
    return [
        "ASSUMPTIONS (every projection below rests on these, and only these)",
        f"  GB = {BYTES_PER_GB:,} bytes (decimal, as in 02 §2.3.2's own ~31 GB/cycle figure)",
        f"  month = {DAYS_PER_MONTH:g} days",
        "  CADENCE: GB/month = GB/cycle x the OBSERVED refetch cadence (obs/day), which is",
        "  the per-key (last_seen_at - first_seen_at) / (fetches - 1) this instrument",
        "  recorded. The declared constants below are each portal's INDEX-WALK schedule",
        f"    {cadence}",
        "  — the right cadence for an index surface and the WRONG one for a detail surface",
        "  (a detail body is refetched only when the index enqueues it, rule 19). They are",
        "  used ONLY as the fallback for a surface with no repeat fetch yet, and a",
        f"  declared/observed divergence beyond {CADENCE_DIVERGENCE_FACTOR:g}x is marked on the row",
        "  a cycle = one pass over the surface's ARTEFACTS: distinct keys with the index",
        "  archivers' /{week} suffix stripped, so an index page position counts once no",
        "  matter how many ISO weeks the instrument has been running",
        "  GB/cycle = artefacts x normalised change rate x mean RAW body size — the archive",
        "  stores the body, and the normalised projection exists only to be hashed",
        "  the rate and the observed cadence come from the REPEATED keys; multiplying them",
        "  by every artefact assumes the once-seen ones churn the same way (the safe way",
        "  to be wrong: it over-, never under-states)",
        "  sizes are UNCOMPRESSED, so every GB figure is an UPPER BOUND: the archive",
        "  gzips bodies above its threshold and HTML compresses several-fold",
        "  change rate denominator = fetches - keys (a key's first sighting cannot be a",
        f"  change); a surface with < {MIN_REPEAT_FETCHES} repeat fetches or",
        f"  < {MIN_KEYS_REPEATED} repeated keys prints INSUFFICIENT instead of a projection",
        f"  normalizer = {NORMALIZER_VERSION}; cohorts ending {PROBE_NORMALIZER_SUFFIX!r} are"
        " the confirmation probe, reported apart",
        f"  cohorts ending {BASE_PROFILE_SUFFIX!r} were hashed with the GENERIC base"
        " profile — no volatile paths are DECLARED for that (source, page_kind),"
        " so their rate is an upper bound on a surface, not a verdict on a profile",
        f"  {CONTRACT_PROFILE_SUFFIX!r}<N> names the CONTRACT VERSION whose"
        " persistence.volatile_paths produced the projection; a contract bump opens a"
        " clean cohort rather than relabelling accumulated counters (migration 402)",
        "  totals sum ONE cohort per (source, page_kind) — the newest; a rollout's older",
        "  cohort is named below the total instead of added to it",
    ]


def _sections(surfaces: Sequence[Surface]) -> list[tuple[str, tuple[str, ...], list[Surface]]]:
    passive = [s for s in surfaces if not s.is_probe_cohort]
    probe = [s for s in surfaces if s.is_probe_cohort]
    return [
        (
            "PASSIVE MEASUREMENT — live scrape traffic, one row per artefact per cohort",
            (),
            passive,
        ),
        (
            "CONFIRMATION PROBE — 02 §2.3.2's 200 x 3 protocol (scripts/"
            "location_payload_refetch_probe.py)",
            ("  three fetches minutes apart, so a NORMALISED change here is per-request",
             "  volatility the profile failed to strip — not a listing that changed;",
             "  kept in its own cohort so its cadence never contaminates the passive rows",
             "  DETAIL BODIES ONLY (02 §2.3.2's protocol is '200 listings'): index-page",
             "  volatility is only ever measured passively, by the three portals that",
             "  archive index pages (sreality, ceskereality, remax)",),
            probe,
        ),
    ]


def _render_measurement(surfaces: Sequence[Surface]) -> list[str]:
    lines: list[str] = []
    for title, notes, section in _sections(surfaces):
        if not section:
            continue
        lines.append("")
        lines.append(title)
        lines.extend(notes)
        lines.append(
            f"{'source':<14}{'page_kind':<10}{'cohort':<33}{'rows':>9}{'artefacts':>11}"
            f"{'repeat':>9}{'raw':>8}{'norm':>8}{'raw KB':>10}{'norm KB':>10}"
            f"{'interval h':>12}"
        )
        for surface in section:
            row = surface.row
            lines.append(
                f"{row.source:<14}{row.page_kind:<10}{row.normalizer_version:<33}"
                f"{_num(row.keys):>9}{_num(surface.artefacts):>11}"
                f"{_num(surface.repeat_fetches):>9}"
                f"{_pct(surface.raw_change_rate):>8}{_pct(surface.norm_change_rate):>8}"
                f"{_kb(row.mean_raw_bytes):>10}{_kb(row.mean_norm_bytes):>10}"
                f"{_hours(row.mean_interval_s):>12}"
            )
    return lines


def _render_medians(surfaces: Sequence[Surface]) -> list[str]:
    """Mean vs median, passive and probe kept in the same two sections as everywhere else.

    One blended table would let the probe's minutes-apart interval sit unlabelled beside
    the passive rows the gate is signed from.
    """
    lines: list[str] = []
    header = (
        f"{'source':<14}{'page_kind':<10}{'cohort':<33}{'raw mean':>10}{'raw med':>10}"
        f"{'norm mean':>11}{'norm med':>10}{'int mean h':>12}{'int med h':>11}"
    )
    for title, _notes, section in _sections(surfaces):
        if not section:
            continue
        lines.append("")
        lines.append(
            "DISTRIBUTION — mean vs median (a mean far off its median is one fat body) — "
            f"{title.split(' —')[0].split(' (')[0]}"
        )
        lines.append(header)
        for surface in section:
            row = surface.row
            lines.append(
                f"{row.source:<14}{row.page_kind:<10}{row.normalizer_version:<33}"
                f"{_kb(row.mean_raw_bytes):>10}{_kb(row.median_raw_bytes):>10}"
                f"{_kb(row.mean_norm_bytes):>11}{_kb(row.median_norm_bytes):>10}"
                f"{_hours(row.mean_interval_s):>12}{_hours(row.median_interval_s):>11}"
            )
    return lines


def _render_projection(surfaces: Sequence[Surface]) -> list[str]:
    """The signed number: GB per cycle and per month over the artefacts measured."""
    passive = [s for s in surfaces if not s.is_probe_cohort]
    if not passive:
        return []
    lines = [
        "",
        "PROJECTION over the ARTEFACTS MEASURED (not the portal's inventory — next table)",
        "  GB/month is at the OBSERVED cadence wherever there is one; 'cyc/day' is the",
        "  declared index-walk schedule, shown only so a divergence is visible",
        f"{'source':<14}{'page_kind':<10}{'cohort':<33}{'base':>10}{'cyc/day':>9}"
        f"{'obs/day':>9}{'GB/cycle':>10}{'GB/month':>10}{'raw GB/mo':>11}  marker",
    ]
    for surface in passive:
        row = surface.row
        marker = " ".join(part for part in (surface.insufficient, surface.cadence_note) if part)
        lines.append(
            f"{row.source:<14}{row.page_kind:<10}{row.normalizer_version:<33}"
            f"{_num(surface.artefacts):>10}"
            f"{(f'{surface.cycles_per_day:g}' if surface.cycles_per_day else '—'):>9}"
            f"{(f'{surface.observed_cycles_per_day:.2f}' if surface.observed_cycles_per_day else '—'):>9}"
            f"{_gb(surface.gb_per_cycle()):>10}{_gb(surface.gb_per_month()):>10}"
            f"{_gb(surface.gb_per_month_raw()):>11}  {marker}"
        )
    lines.extend(_render_totals(passive))
    return lines


def _render_totals(surfaces: Sequence[Surface]) -> list[str]:
    """Fleet totals over the projectable surfaces only, with every omission named."""
    newest, superseded = newest_cohorts(surfaces)
    projectable = [
        s for s in newest if s.insufficient is None and s.projection_cycles_per_day
    ]
    skipped = [
        s for s in newest if s.insufficient is not None or not s.projection_cycles_per_day
    ]
    total_month = sum(s.gb_per_month() or 0.0 for s in projectable)
    total_month_raw = sum(s.gb_per_month_raw() or 0.0 for s in projectable)
    lines = [
        f"{'TOTAL':<14}{'':<42}{'':>9}{'':>9}{'':>10}"
        f"{_gb(total_month):>10}{_gb(total_month_raw):>11}"
        f"  over {len(projectable)} projectable surface(s)",
    ]
    if skipped:
        names = ", ".join(f"{s.row.source}/{s.row.page_kind}" for s in skipped)
        lines.append(f"  NOT in the total (insufficient or no cadence): {names}")
    if superseded:
        names = ", ".join(
            f"{s.row.source}/{s.row.page_kind}@{s.row.normalizer_version}"
            for s in superseded
        )
        lines.append(f"  NOT in the total (superseded cohort, a newer one is live): {names}")
    return lines


def _render_inventory_scaled(
    surfaces: Sequence[Surface], inventory: dict[str, int],
) -> list[str]:
    """The same rates over the portal's live inventory — the fleet-scale number."""
    section = [
        s for s in surfaces
        if not s.is_probe_cohort and s.row.page_kind == DETAIL and s.row.source in inventory
    ]
    if not section:
        return []
    newest, superseded = newest_cohorts(section)
    lines = [
        "",
        "PROJECTION scaled to the ACTIVE INVENTORY (detail surfaces only)",
        "  assumes every active listing churns like the measured sample; index surfaces",
        "  have no inventory analogue and are absent here, not zero",
        f"{'source':<14}{'cohort':<33}{'active':>10}{'measured':>10}{'GB/cycle':>10}"
        f"{'GB/month':>10}{'raw GB/mo':>11}  marker",
    ]
    total = 0.0
    for surface in section:
        active = inventory[surface.row.source]
        month = surface.gb_per_month(active)
        if surface in newest:
            total += month or 0.0
        marker = " ".join(
            part for part in (
                surface.insufficient,
                surface.cadence_note,
                "" if surface in newest else "superseded cohort — NOT in the total",
            ) if part
        )
        lines.append(
            f"{surface.row.source:<14}{surface.row.normalizer_version:<33}"
            f"{_num(active):>10}{_num(surface.artefacts):>10}"
            f"{_gb(surface.gb_per_cycle(active)):>10}{_gb(month):>10}"
            f"{_gb(surface.gb_per_month_raw(active)):>11}  {marker}"
        )
    lines.append(
        f"{'TOTAL':<14}{'':<52}{_gb(total):>10}"
        f"  over {len(newest)} surface(s)"
        + (f", {len(superseded)} superseded cohort(s) excluded" if superseded else "")
    )
    return lines


def _render_window(surfaces: Sequence[Surface]) -> list[str]:
    """The passive window and the probe's, never one span across both.

    A probe run is minutes long and lands wherever the operator dispatched it; folding it
    into one min/max would silently widen or shift the window the passive rates — and so
    the signed projection — were measured over.
    """
    lines: list[str] = []
    for title, _notes, section in _sections(surfaces):
        starts = [s.row.window_start for s in section if s.row.window_start]
        ends = [s.row.window_end for s in section if s.row.window_end]
        if not starts or not ends:
            continue
        span = (max(ends) - min(starts)).total_seconds() / SECONDS_PER_HOUR
        label = title.split(" —")[0].split(" (")[0]
        lines.append(
            f"MEASUREMENT WINDOW ({label}) {_stamp(min(starts))} → {_stamp(max(ends))} "
            f"UTC ({span:.1f} h)"
        )
    if not lines:
        return ["", "MEASUREMENT WINDOW — the instrument has recorded nothing yet"]
    return ["", *lines]


def render(measurement: Measurement) -> list[str]:
    lines = list(_assumptions())
    lines.extend(_render_window(measurement.surfaces))
    lines.extend(_render_measurement(measurement.surfaces))
    lines.extend(_render_medians(measurement.surfaces))
    lines.extend(_render_projection(measurement.surfaces))
    lines.extend(_render_inventory_scaled(measurement.surfaces, measurement.active_inventory))
    return lines


def surface_json(
    surface: Surface, active_listings: int | None, *, superseded: bool = False,
) -> dict[str, Any]:
    row = surface.row
    return {
        "source": row.source,
        "page_kind": row.page_kind,
        "normalizer_version": row.normalizer_version,
        "probe_cohort": surface.is_probe_cohort,
        "superseded_cohort": superseded,
        "keys": row.keys,
        "artefacts": surface.artefacts,
        "keys_repeated": row.keys_repeated,
        "fetches": row.fetches,
        "repeat_fetches": surface.repeat_fetches,
        "raw_changes": row.raw_changes,
        "norm_changes": row.norm_changes,
        "raw_change_rate": surface.raw_change_rate,
        "norm_change_rate": surface.norm_change_rate,
        "mean_raw_bytes": row.mean_raw_bytes,
        "median_raw_bytes": row.median_raw_bytes,
        "mean_norm_bytes": row.mean_norm_bytes,
        "median_norm_bytes": row.median_norm_bytes,
        "mean_refetch_interval_s": row.mean_interval_s,
        "median_refetch_interval_s": row.median_interval_s,
        "window_start": _iso(row.window_start),
        "window_end": _iso(row.window_end),
        "insufficient": surface.insufficient,
        "cadence_note": surface.cadence_note or None,
        "cadence_basis": surface.cadence_basis,
        "cycles_per_day": surface.cycles_per_day,
        "observed_cycles_per_day": surface.observed_cycles_per_day,
        "projection_cycles_per_day": surface.projection_cycles_per_day,
        "gb_per_cycle": surface.gb_per_cycle(),
        "gb_per_month": surface.gb_per_month(),
        "gb_per_cycle_raw": surface.gb_per_cycle_raw(),
        "gb_per_month_raw": surface.gb_per_month_raw(),
        "gb_per_month_at_observed_cadence": surface.gb_per_month_observed(),
        "gb_per_month_at_declared_cadence": surface.gb_per_month_declared(),
        "active_listings": active_listings,
        "gb_per_cycle_active_inventory": (
            None if active_listings is None or row.page_kind != DETAIL
            else surface.gb_per_cycle(active_listings)
        ),
        "gb_per_month_active_inventory": (
            None if active_listings is None or row.page_kind != DETAIL
            else surface.gb_per_month(active_listings)
        ),
    }


def _iso(value: datetime.datetime | None) -> str | None:
    return None if value is None else value.astimezone(datetime.UTC).isoformat()


def to_json(measurement: Measurement) -> dict[str, Any]:
    _newest, superseded = newest_cohorts(measurement.surfaces)
    return {
        "measured_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "normalizer_version": NORMALIZER_VERSION,
        "probe_cohort_suffix": PROBE_NORMALIZER_SUFFIX,
        "base_profile_cohort_suffix": BASE_PROFILE_SUFFIX,
        "assumptions": {
            "bytes_per_gb": BYTES_PER_GB,
            "days_per_month": DAYS_PER_MONTH,
            "declared_cycles_per_day_is_the_index_walk": dict(sorted(CYCLES_PER_DAY.items())),
            "projection_cadence": "observed refetch interval, declared as fallback",
            "cadence_divergence_factor": CADENCE_DIVERGENCE_FACTOR,
            "min_repeat_fetches": MIN_REPEAT_FETCHES,
            "min_keys_repeated": MIN_KEYS_REPEATED,
            "sizes_are_uncompressed_upper_bound": True,
            "index_keys_are_week_stamped": True,
        },
        "active_inventory": dict(sorted(measurement.active_inventory.items())),
        "surfaces": [
            surface_json(
                surface,
                measurement.active_inventory.get(surface.row.source),
                superseded=surface in superseded,
            )
            for surface in measurement.surfaces
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="read out portal_payload_churn")
    parser.add_argument("--json", action="store_true",
                        help="Emit the readout as JSON on stdout instead of a table.")
    parser.add_argument("--skip-inventory", action="store_true",
                        help="Skip the active-listings count (the inventory-scaled table).")
    parser.add_argument(
        "--statement-timeout", type=int,
        default=loader_db.env_timeout_s(STATEMENT_TIMEOUT_ENV, DEFAULT_STATEMENT_TIMEOUT_S),
        help=f"Per-statement timeout in seconds (${STATEMENT_TIMEOUT_ENV}).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    with db.connect() as conn:
        measurement = measure(
            conn,
            statement_timeout_s=args.statement_timeout,
            with_inventory=not args.skip_inventory,
        )

    if not measurement.surfaces:
        LOG.warning(
            "CHURN portal_payload_churn is empty — is %s on?", db.PAYLOAD_SHADOW_HASH_SETTING,
        )

    if args.json:
        print(json.dumps(to_json(measurement), indent=2, default=str))
    else:
        for line in render(measurement):
            print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
