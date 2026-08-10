"""The blocking load-time controls, as pure functions over staging statistics.

Anchoring rule (04 §4.5.3): every assertion whose subject grows with the register is
anchored to the PREVIOUS SUCCESSFUL LOAD, never to a 2026 constant — the register grows
monotonically, so an equality test against 3,020,222 becomes a false failure within a year
and pins the mirror to a frozen vintage. The 2026-08 measurements survive only as (a) the
first-load seed and (b) an outer sanity bound that catches a truncated download regardless
of history. The golden point is the one deliberately absolute anchor: a fixed physical
location cannot drift with the register.
"""

from __future__ import annotations

from dataclasses import dataclass

from location_data import krovak

ROW_COUNT_MIN_RATIO = 0.998
ROW_COUNT_MAX_RATIO = 1.015
ROW_COUNT_SANITY = (2_500_000, 4_500_000)
ENVELOPE_SLACK_M = 500.0
MISSING_COORD_SLACK_FRACTION = 0.0002  # 0.02 percentage points
DISCREPANCY_TREND_FACTOR = 3


@dataclass(frozen=True, slots=True)
class StagingStats:
    row_count: int
    missing_psc: int
    missing_coords: int
    golden_distance_m: float | None
    krovak_y_min: float | None
    krovak_y_max: float | None
    krovak_x_min: float | None
    krovak_x_max: float | None
    lat_min: float | None
    lat_max: float | None
    lon_min: float | None
    lon_max: float | None
    only_in_adr: int = 0
    only_in_chain: int = 0


@dataclass(frozen=True, slots=True)
class PriorLoad:
    row_count: int
    missing_psc: int
    missing_coords: int
    krovak_y_min: float
    krovak_y_max: float
    krovak_x_min: float
    krovak_x_max: float
    discrepancies: int
    proj_pipeline: str


@dataclass(frozen=True, slots=True)
class Assertion:
    name: str
    ok: bool
    expected: str
    actual: str
    blocking: bool = True
    route: str = "page"


def _within(value: float | None, low: float, high: float) -> bool:
    return value is not None and low <= value <= high


def evaluate(
    stats: StagingStats,
    prior: PriorLoad | None,
    *,
    proj_pipeline: str,
) -> list[Assertion]:
    """Every control for one baseline load. `blocking` failures abort before publish."""
    out: list[Assertion] = []

    out.append(Assertion(
        name="row_count_sanity",
        ok=ROW_COUNT_SANITY[0] <= stats.row_count <= ROW_COUNT_SANITY[1],
        expected=f"{ROW_COUNT_SANITY[0]}..{ROW_COUNT_SANITY[1]}",
        actual=str(stats.row_count),
    ))

    if prior is not None:
        low = int(prior.row_count * ROW_COUNT_MIN_RATIO)
        high = int(prior.row_count * ROW_COUNT_MAX_RATIO)
        out.append(Assertion(
            name="row_count_vs_prior",
            ok=low <= stats.row_count <= high,
            expected=f"{low}..{high} (prior {prior.row_count})",
            actual=str(stats.row_count),
        ))

    out.append(Assertion(
        name="missing_psc",
        ok=stats.missing_psc == 0,
        expected="0",
        actual=str(stats.missing_psc),
    ))

    if prior is not None:
        slack = int(stats.row_count * MISSING_COORD_SLACK_FRACTION)
        allowed = prior.missing_coords + slack
        out.append(Assertion(
            name="missing_coordinates_vs_prior",
            ok=stats.missing_coords <= allowed,
            expected=f"<= {allowed} (prior {prior.missing_coords} + {slack})",
            actual=str(stats.missing_coords),
        ))

    out.append(Assertion(
        name="golden_point",
        ok=stats.golden_distance_m is not None
        and stats.golden_distance_m <= krovak.GOLDEN_TOLERANCE_M,
        expected=f"kod_adm {krovak.GOLDEN_KOD_ADM} within "
                 f"{krovak.GOLDEN_TOLERANCE_M} m of {krovak.GOLDEN_WGS84}",
        actual="absent" if stats.golden_distance_m is None else f"{stats.golden_distance_m:.2f} m",
    ))

    out.append(Assertion(
        name="krovak_super_envelope",
        ok=(
            _within(stats.krovak_y_min, krovak.KROVAK_Y_MIN, krovak.KROVAK_Y_MAX)
            and _within(stats.krovak_y_max, krovak.KROVAK_Y_MIN, krovak.KROVAK_Y_MAX)
            and _within(stats.krovak_x_min, krovak.KROVAK_X_MIN, krovak.KROVAK_X_MAX)
            and _within(stats.krovak_x_max, krovak.KROVAK_X_MIN, krovak.KROVAK_X_MAX)
        ),
        expected=f"Y {krovak.KROVAK_Y_MIN}..{krovak.KROVAK_Y_MAX} "
                 f"X {krovak.KROVAK_X_MIN}..{krovak.KROVAK_X_MAX}",
        actual=f"Y {stats.krovak_y_min}..{stats.krovak_y_max} "
               f"X {stats.krovak_x_min}..{stats.krovak_x_max}",
    ))

    if prior is not None:
        pairs = (
            (stats.krovak_y_min, prior.krovak_y_min),
            (stats.krovak_y_max, prior.krovak_y_max),
            (stats.krovak_x_min, prior.krovak_x_min),
            (stats.krovak_x_max, prior.krovak_x_max),
        )
        ok = all(
            current is not None and abs(current - previous) <= ENVELOPE_SLACK_M
            for current, previous in pairs
        )
        out.append(Assertion(
            name="krovak_envelope_vs_prior",
            ok=ok,
            expected=f"within +/-{ENVELOPE_SLACK_M} m of "
                     f"Y {prior.krovak_y_min}..{prior.krovak_y_max} "
                     f"X {prior.krovak_x_min}..{prior.krovak_x_max}",
            actual=f"Y {stats.krovak_y_min}..{stats.krovak_y_max} "
                   f"X {stats.krovak_x_min}..{stats.krovak_x_max}",
        ))

    out.append(Assertion(
        name="wgs84_bbox",
        ok=(
            _within(stats.lat_min, krovak.CZ_LAT_MIN, krovak.CZ_LAT_MAX)
            and _within(stats.lat_max, krovak.CZ_LAT_MIN, krovak.CZ_LAT_MAX)
            and _within(stats.lon_min, krovak.CZ_LON_MIN, krovak.CZ_LON_MAX)
            and _within(stats.lon_max, krovak.CZ_LON_MIN, krovak.CZ_LON_MAX)
        ),
        expected=f"lat {krovak.CZ_LAT_MIN}..{krovak.CZ_LAT_MAX} "
                 f"lon {krovak.CZ_LON_MIN}..{krovak.CZ_LON_MAX}",
        actual=f"lat {stats.lat_min}..{stats.lat_max} lon {stats.lon_min}..{stats.lon_max}",
    ))

    if prior is not None:
        out.append(Assertion(
            name="proj_pipeline_unchanged",
            ok=proj_pipeline == prior.proj_pipeline,
            expected=prior.proj_pipeline,
            actual=proj_pipeline,
            blocking=False,
            route="warn",
        ))
        allowed = max(DISCREPANCY_TREND_FACTOR * prior.discrepancies, DISCREPANCY_TREND_FACTOR)
        observed = stats.only_in_adr + stats.only_in_chain
        out.append(Assertion(
            name="product_skew_discrepancies",
            ok=observed <= allowed,
            expected=f"<= {allowed} ({DISCREPANCY_TREND_FACTOR}x prior {prior.discrepancies})",
            actual=str(observed),
            blocking=False,
            route="digest",
        ))

    return out


def blocking_failures(assertions: list[Assertion]) -> list[Assertion]:
    return [a for a in assertions if a.blocking and not a.ok]
