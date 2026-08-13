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
cohorts (migration 402), and the confirmation probe writes into its own
`…+probe` cohort so its three-fetches-in-ten-minutes cadence can never blend into the
passive measurement's ~6-hourly one.

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
from location_data.payload_norm import NORMALIZER_VERSION, PROBE_NORMALIZER_SUFFIX
from scraper import db

LOG = logging.getLogger("location_payload_churn_report")

# Decimal GB, the unit 02 §2.3.2's own arithmetic uses (445,191 pages x ~70 KB
# "is ~31 GB per full refetch cycle" is 10^3-based, and both R2 and Supabase
# price storage decimally). Printed in every header so it is never ambiguous.
BYTES_PER_GB = 1_000_000_000
DAYS_PER_MONTH = 30.0
SECONDS_PER_DAY = 86_400.0
SECONDS_PER_HOUR = 3_600.0

# THE CADENCE ASSUMPTION, in one place. 02 §2.3.2: "the 6 h portals run ~4
# cycles/day" and "sreality's hourly index walk … is worse by an order of
# magnitude". A cycle is one pass over the keys of a surface, so this is a
# property of the portal's live schedule, NOT of the measurement — which is
# exactly why the observed per-key interval is reported beside it: where the
# two disagree, the observed one is the measurement and this one is the plan.
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
_CHURN_SURFACE_SQL = """
    WITH per_key AS (
        SELECT source,
               page_kind::text AS page_kind,
               normalizer_version,
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
    SELECT source,
           page_kind,
           normalizer_version,
           count(*)                                   AS keys,
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
        """What the instrument SAW, as a cadence — the check on CYCLES_PER_DAY."""
        if not self.row.mean_interval_s:
            return None
        return SECONDS_PER_DAY / self.row.mean_interval_s

    def gb_per_cycle(self, keys: int | None = None) -> float | None:
        """Bytes appended by one pass over `keys` artefacts, at the NORMALISED rate.

        The append-on-change store writes a row only when the normalised hash moves, and
        what it stores is the BODY — so the projection is (keys x rate x mean raw body),
        never the normalised projection's size, which exists only to be hashed.
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
        base = self.row.keys if keys is None else keys
        return base * rate * self.row.mean_raw_bytes / BYTES_PER_GB

    def gb_per_month(self, keys: int | None = None) -> float | None:
        return _per_month(self.gb_per_cycle(keys), self.cycles_per_day)

    def gb_per_month_raw(self, keys: int | None = None) -> float | None:
        return _per_month(self.gb_per_cycle_raw(keys), self.cycles_per_day)

    def gb_per_month_observed(self, keys: int | None = None) -> float | None:
        return _per_month(self.gb_per_cycle(keys), self.observed_cycles_per_day)


def _per_month(gb_per_cycle: float | None, cycles_per_day: float | None) -> float | None:
    if gb_per_cycle is None or cycles_per_day is None:
        return None
    return gb_per_cycle * cycles_per_day * DAYS_PER_MONTH


@dataclass(frozen=True)
class Measurement:
    surfaces: list[Surface]
    active_inventory: dict[str, int]


def surface_from_row(row: Sequence[Any]) -> Surface:
    (source, page_kind, normalizer_version, keys, keys_repeated, fetches, raw_changes,
     norm_changes, mean_raw, median_raw, mean_norm, median_norm, mean_interval,
     median_interval, window_start, window_end) = row
    return Surface(SurfaceRow(
        source=str(source),
        page_kind=str(page_kind),
        normalizer_version=str(normalizer_version),
        keys=int(keys),
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
        f"  declared cadence (cycles per day): {cadence}",
        "  a cycle = one pass over the surface's keys; the OBSERVED interval column is",
        "  what the instrument actually saw and is the check on the line above",
        "  GB/cycle = keys x normalised change rate x mean RAW body size — the archive",
        "  stores the body, and the normalised projection exists only to be hashed",
        "  sizes are UNCOMPRESSED, so every GB figure is an UPPER BOUND: the archive",
        "  gzips bodies above its threshold and HTML compresses several-fold",
        "  change rate denominator = fetches - keys (a key's first sighting cannot be a",
        f"  change); a surface with < {MIN_REPEAT_FETCHES} repeat fetches or",
        f"  < {MIN_KEYS_REPEATED} repeated keys prints INSUFFICIENT instead of a projection",
        f"  normalizer = {NORMALIZER_VERSION}; cohorts ending {PROBE_NORMALIZER_SUFFIX!r} are"
        " the confirmation probe, reported apart",
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
             "  kept in its own cohort so its cadence never contaminates the passive rows",),
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
            f"{'source':<14}{'page_kind':<10}{'cohort':<22}{'keys':>9}{'repeat':>9}"
            f"{'raw':>8}{'norm':>8}{'raw KB':>10}{'norm KB':>10}{'interval h':>12}"
        )
        for surface in section:
            row = surface.row
            lines.append(
                f"{row.source:<14}{row.page_kind:<10}{row.normalizer_version:<22}"
                f"{_num(row.keys):>9}{_num(surface.repeat_fetches):>9}"
                f"{_pct(surface.raw_change_rate):>8}{_pct(surface.norm_change_rate):>8}"
                f"{_kb(row.mean_raw_bytes):>10}{_kb(row.mean_norm_bytes):>10}"
                f"{_hours(row.mean_interval_s):>12}"
            )
    return lines


def _render_medians(surfaces: Sequence[Surface]) -> list[str]:
    if not surfaces:
        return []
    lines = [
        "",
        "DISTRIBUTION — mean vs median (a mean pulled far off its median is one fat body)",
        f"{'source':<14}{'page_kind':<10}{'cohort':<22}{'raw mean':>10}{'raw med':>10}"
        f"{'norm mean':>11}{'norm med':>10}{'int mean h':>12}{'int med h':>11}",
    ]
    for surface in surfaces:
        row = surface.row
        lines.append(
            f"{row.source:<14}{row.page_kind:<10}{row.normalizer_version:<22}"
            f"{_kb(row.mean_raw_bytes):>10}{_kb(row.median_raw_bytes):>10}"
            f"{_kb(row.mean_norm_bytes):>11}{_kb(row.median_norm_bytes):>10}"
            f"{_hours(row.mean_interval_s):>12}{_hours(row.median_interval_s):>11}"
        )
    return lines


def _render_projection(surfaces: Sequence[Surface]) -> list[str]:
    """The signed number: GB per cycle and per month over the keys actually measured."""
    passive = [s for s in surfaces if not s.is_probe_cohort]
    if not passive:
        return []
    lines = [
        "",
        "PROJECTION over the KEYS MEASURED (not the portal's inventory — see the next table)",
        f"{'source':<14}{'page_kind':<10}{'cohort':<22}{'cyc/day':>9}{'obs/day':>9}"
        f"{'GB/cycle':>10}{'GB/month':>10}{'raw GB/mo':>11}  marker",
    ]
    for surface in passive:
        row = surface.row
        marker = surface.insufficient or ""
        if surface.cycles_per_day is None:
            marker = f"{marker} {UNKNOWN_CADENCE}".strip()
        lines.append(
            f"{row.source:<14}{row.page_kind:<10}{row.normalizer_version:<22}"
            f"{(f'{surface.cycles_per_day:g}' if surface.cycles_per_day else '—'):>9}"
            f"{(f'{surface.observed_cycles_per_day:.1f}' if surface.observed_cycles_per_day else '—'):>9}"
            f"{_gb(surface.gb_per_cycle()):>10}{_gb(surface.gb_per_month()):>10}"
            f"{_gb(surface.gb_per_month_raw()):>11}  {marker}"
        )
    lines.extend(_render_totals(passive))
    return lines


def _render_totals(surfaces: Sequence[Surface]) -> list[str]:
    """Fleet totals over the projectable surfaces only, with the omissions named."""
    projectable = [s for s in surfaces if s.insufficient is None and s.cycles_per_day]
    skipped = [s for s in surfaces if s.insufficient is not None or not s.cycles_per_day]
    total_month = sum(s.gb_per_month() or 0.0 for s in projectable)
    total_month_raw = sum(s.gb_per_month_raw() or 0.0 for s in projectable)
    lines = [
        f"{'TOTAL':<14}{'':<32}{'':>9}{'':>9}{'':>10}"
        f"{_gb(total_month):>10}{_gb(total_month_raw):>11}"
        f"  over {len(projectable)} projectable surface(s)",
    ]
    if skipped:
        names = ", ".join(f"{s.row.source}/{s.row.page_kind}" for s in skipped)
        lines.append(f"  NOT in the total (insufficient or no cadence): {names}")
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
    lines = [
        "",
        "PROJECTION scaled to the ACTIVE INVENTORY (detail surfaces only)",
        "  assumes every active listing churns like the measured sample; index surfaces",
        "  have no inventory analogue and are absent here, not zero",
        f"{'source':<14}{'cohort':<22}{'active':>10}{'measured':>10}{'GB/cycle':>10}"
        f"{'GB/month':>10}{'raw GB/mo':>11}  marker",
    ]
    total = 0.0
    for surface in section:
        active = inventory[surface.row.source]
        month = surface.gb_per_month(active)
        total += month or 0.0
        lines.append(
            f"{surface.row.source:<14}{surface.row.normalizer_version:<22}"
            f"{_num(active):>10}{_num(surface.row.keys):>10}"
            f"{_gb(surface.gb_per_cycle(active)):>10}{_gb(month):>10}"
            f"{_gb(surface.gb_per_month_raw(active)):>11}  {surface.insufficient or ''}"
        )
    lines.append(f"{'TOTAL':<14}{'':<52}{_gb(total):>10}")
    return lines


def _render_window(surfaces: Sequence[Surface]) -> list[str]:
    starts = [s.row.window_start for s in surfaces if s.row.window_start]
    ends = [s.row.window_end for s in surfaces if s.row.window_end]
    if not starts or not ends:
        return ["", "MEASUREMENT WINDOW — the instrument has recorded nothing yet"]
    span = (max(ends) - min(starts)).total_seconds() / SECONDS_PER_HOUR
    return [
        "",
        f"MEASUREMENT WINDOW {_stamp(min(starts))} → {_stamp(max(ends))} UTC "
        f"({span:.1f} h)",
    ]


def render(measurement: Measurement) -> list[str]:
    lines = list(_assumptions())
    lines.extend(_render_window(measurement.surfaces))
    lines.extend(_render_measurement(measurement.surfaces))
    lines.extend(_render_medians(measurement.surfaces))
    lines.extend(_render_projection(measurement.surfaces))
    lines.extend(_render_inventory_scaled(measurement.surfaces, measurement.active_inventory))
    return lines


def surface_json(surface: Surface, active_listings: int | None) -> dict[str, Any]:
    row = surface.row
    return {
        "source": row.source,
        "page_kind": row.page_kind,
        "normalizer_version": row.normalizer_version,
        "probe_cohort": surface.is_probe_cohort,
        "keys": row.keys,
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
        "cycles_per_day": surface.cycles_per_day,
        "observed_cycles_per_day": surface.observed_cycles_per_day,
        "gb_per_cycle": surface.gb_per_cycle(),
        "gb_per_month": surface.gb_per_month(),
        "gb_per_cycle_raw": surface.gb_per_cycle_raw(),
        "gb_per_month_raw": surface.gb_per_month_raw(),
        "gb_per_month_at_observed_cadence": surface.gb_per_month_observed(),
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
    return {
        "measured_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "normalizer_version": NORMALIZER_VERSION,
        "probe_cohort_suffix": PROBE_NORMALIZER_SUFFIX,
        "assumptions": {
            "bytes_per_gb": BYTES_PER_GB,
            "days_per_month": DAYS_PER_MONTH,
            "cycles_per_day": dict(sorted(CYCLES_PER_DAY.items())),
            "min_repeat_fetches": MIN_REPEAT_FETCHES,
            "min_keys_repeated": MIN_KEYS_REPEATED,
            "sizes_are_uncompressed_upper_bound": True,
        },
        "active_inventory": dict(sorted(measurement.active_inventory.items())),
        "surfaces": [
            surface_json(surface, measurement.active_inventory.get(surface.row.source))
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
