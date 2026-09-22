"""Rule 16, enforced: the Watchdog and Browse share ONE per-m² definition.

Until W5 this was unenforced — `grep -c price_per_m2 tests/api/test_notifications.py`
was 0, and the matcher carried its own hand-typed `price_czk / NULLIF(area_m2, 0)`
for months while Browse filtered on a different number entirely.

WHAT "ONE DEFINITION" LOOKS LIKE IN SQL DEPENDS ON THE RELATION, and that is why
this file does not assert the two clauses are byte-identical — they cannot be:

  * the Watchdog matcher runs against `properties_public`, which PUBLISHES
    `price_per_m2` as `measure_price_per_m2(...)` (migration 425). Reading the
    published column IS reading the measure.
  * `toolkit.comparables._shared_filter_where` runs against the `listings`
    TABLE (its only three FROM clauses are comparables, velocity and the transit
    corridor). `listings` has NO price_per_m2 column, so that spelling does not
    fail review — it fails at PREPARE. The four-argument call is the only legal
    way to name the same measure there.

So the invariant is: NEITHER site derives the formula itself, and BOTH resolve
to `measure_price_per_m2`. That is what these tests pin.
"""

from __future__ import annotations

import re
from pathlib import Path

from api.notifications import WatchdogFilterSpec, _build_match_clauses
from toolkit.comparables import (
    ComparableFilters,
    TargetSpec,
    _shared_filter_where,
    build_query,
)
from tests.migration_defs import latest_definition
from toolkit.measures import per_m2_sql, plot_area_sql

_ROOT = Path(__file__).resolve().parents[2]

# A price-over-area division in SQL, in any of the spellings the repo has used.
_HAND_TYPED_DIVISION = re.compile(
    r"price_czk\s*(::numeric)?\s*/\s*(NULLIF\s*\()?\s*\w*\.?area_m2", re.I
)


def _watchdog_ppm2_clauses() -> list[str]:
    where, _ = _build_match_clauses(
        WatchdogFilterSpec(min_price_per_m2=50_000, max_price_per_m2=120_000)
    )
    return [c for c in where if "price_per_m2" in c]


def _comparables_ppm2_clauses() -> list[str]:
    where, _ = _shared_filter_where(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(min_price_per_m2=50_000, max_price_per_m2=120_000),
    )
    return [c for c in where if "price_per_m2" in c or "measure_price_per_m2" in c]


def test_watchdog_reads_the_published_measure_column():
    clauses = _watchdog_ppm2_clauses()
    assert clauses == [
        "l.price_per_m2 >= %(min_price_per_m2)s",
        "l.price_per_m2 <= %(max_price_per_m2)s",
    ]


def test_comparables_calls_the_named_measure_over_the_listings_table():
    clauses = _comparables_ppm2_clauses()
    assert clauses == [
        f"{per_m2_sql('l')} >= %(min_price_per_m2)s",
        f"{per_m2_sql('l')} <= %(max_price_per_m2)s",
    ]
    # `listings` has no price_per_m2 column: the published-column spelling would
    # PREPARE-fail here, which is precisely why the two sites differ textually.
    assert all("l.price_per_m2 " not in c for c in clauses)


def _plot_clauses(where: list[str]) -> list[str]:
    return [c for c in where if "estate_area" in c or "plot_area_m2" in c]


def test_the_plot_bound_is_one_measure_on_both_sites():
    """W21, rule 16 for the OTHER polymorphic area. `area_m2` is the parcel for
    `pozemek` (rule 23), so `estate_area >= x` drops every land row whose portal states
    the plot only as the headline — 32 626 of 101 021 active land rows on 2026-09-17.
    Both sites call `plot_area_m2`, and here the spelling IS identical: neither relation
    publishes a plot column, so both must name the function."""
    watchdog, _ = _build_match_clauses(
        WatchdogFilterSpec(min_estate_area=200, max_estate_area=5000))
    comparables, _ = _shared_filter_where(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(min_estate_area=200, max_estate_area=5000),
    )
    expected = [
        f"{plot_area_sql('l')} >= %(min_estate_area)s",
        f"{plot_area_sql('l')} <= %(max_estate_area)s",
    ]
    assert _plot_clauses(watchdog) == expected
    assert _plot_clauses(comparables) == expected


def test_neither_site_reads_the_bare_plot_column():
    """The source-text guard, the sibling of the per-m² one below: a reader that goes
    back to `estate_area` silently re-drops a third of the land inventory, and nothing
    else in the system would say so."""
    bare = re.compile(r"\bl\.estate_area\s*[<>]=?", re.I)
    for rel in ("api/notifications.py", "toolkit/comparables.py"):
        code = "\n".join(
            line for line in (_ROOT / rel).read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        assert not bare.search(code), (
            f"{rel} bounds the bare estate_area column; call plot_area_m2 instead"
        )


_BROWSE_RPCS = ("browse_stats_properties", "browse_map_cells")


def test_the_browse_aggregate_rpcs_bound_the_plot_measure():
    """The third site, and the one that got away: `browse_stats_properties` and
    `browse_map_cells` bounded `l.estate_area` while the Browse LIST beside them
    narrowed `plot_area_m2` (the registry's declared `pg_column`), so a plot bound
    made the Stats panel and the map describe a different cohort than the rows —
    rule 16, silently. Both relations PUBLISH a `plot_area_m2` column (migration 534
    via `browse_projection`), so here reading the column IS reading the measure.
    RED by: pointing either predicate back at the bare column."""
    for func in _BROWSE_RPCS:
        # One file may carry both definitions, so this reads every estate bound in it.
        bounds = [
            line for line in latest_definition(func).read_text().splitlines()
            if ("estate_area_min_filter" in line or "estate_area_max_filter" in line)
            and (">=" in line or "<=" in line)
        ]
        assert bounds, f"{func}: no estate-area bound found"
        for line in bounds:
            assert "l.plot_area_m2" in line, f"{func} bounds the bare column: {line}"
            assert "l.estate_area" not in line


def test_plot_area_sql_names_the_one_measure():
    assert plot_area_sql("l").startswith("plot_area_m2(")
    for part in ("l.category_main", "l.area_m2::numeric", "l.estate_area::numeric"):
        assert part in plot_area_sql("l")


def test_per_m2_sql_names_the_one_measure():
    assert per_m2_sql("l").startswith("measure_price_per_m2(")
    assert "l.category_main" in per_m2_sql("l")
    assert "l.category_type" in per_m2_sql("l")


def test_neither_site_derives_the_formula_itself():
    """Both bounds resolve to the measure; neither re-spells price / area."""
    for clause in _watchdog_ppm2_clauses() + _comparables_ppm2_clauses():
        assert not _HAND_TYPED_DIVISION.search(clause), clause


def test_no_hand_typed_per_m2_division_survives_in_either_module():
    """The source-text guard: a fifth copy cannot be reintroduced quietly."""
    for rel in ("api/notifications.py", "toolkit/comparables.py",
                "toolkit/transit_axis.py", "toolkit/neighborhoods.py"):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        # Strip comment lines: the WHY of the collapse names the old spelling.
        code = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert not _HAND_TYPED_DIVISION.search(code), (
            f"{rel} still derives price-per-m² itself; call the measure instead"
        )


def test_the_sql_corpus_still_cannot_see_these_statements():
    """Pins the premise behind tests/test_measure_sql_prepare.py.

    The automatic SQL gate discovers module-level `*_SQL` constants and inline
    `.execute()` literals; all four per-m² builders assemble their statement in
    a local variable, so discovery reaches none of them. Written as an assertion
    rather than a comment: the day the corpus grows an in-function resolver,
    this fails loudly instead of leaving a duplicate gate running forever.
    """
    from tests.sql_corpus import discover
    from toolkit.neighborhoods import build_query as neighborhood_query
    from toolkit.transit_axis import build_corridor_query
    from toolkit.velocity import build_market_velocity_query

    def norm(sql: str) -> str:
        return " ".join(sql.split())

    target = TargetSpec(lat=50.08, lng=14.42)
    filters = ComparableFilters(min_price_per_m2=50_000)
    rendered = [
        norm(build_query(target, filters)[0]),
        norm(build_market_velocity_query(target, filters, "all")[0]),
        norm(build_corridor_query(target, filters, ["tram"], 800, 300)[0]),
        norm(neighborhood_query(50.08, 14.42, 1000, 30, "byt", "prodej")[0]),
    ]
    discovered = {norm(item.sql) for item in discover(include_inline=True)}
    assert not [sql for sql in rendered if sql in discovered], (
        "sql_corpus now discovers the per-m² statements — fold "
        "tests/test_measure_sql_prepare.py into tests/test_sql_schema_prepare.py"
    )
