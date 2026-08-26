"""browse_map_cells returns a BOUNDED, COMPLETE answer (migration 439), executed.

W6b replaces an unordered `.limit(50000)` — which hid 52% of the default cohort, every
row north of ~lat 50.025 — with a server-side grid. Swapping one truncation for another
would be worse than leaving it, so the two properties that matter are:

  * **BOUNDED.** The cell count must be capped BY CONSTRUCTION, not by a LIMIT. There is
    no LIMIT anywhere in the function; the bound comes from two things, and each is one
    edit away from being lost:
      - the `case when <inside the extent>` guard, without which a row at lng 125 (the
        live data reaches -118 and +125) mints its own cell, and
      - the `least(..., c_cols - 1)` clamp, without which a row exactly ON the north or
        east edge lands in a 21st column or a 14th row.
    Removing EITHER leaves every count correct and every test that checks counts green.
    NOTE that a rail phrased as "no cohort read applies .limit() without .order()" would
    have been useless here: `authenticator` carries pgrst.db_max_rows=50000 (migration
    394), so a read can still truncate server-side with no client `.limit()` at all. The
    assertion has to be on the SIZE OF THE RESULT.

  * **COMPLETE.** total must be the whole mappable cohort, and every row must be counted
    exactly once — inside a cell, or inside `off_grid`. Rows whose coordinates fall
    outside the extent are the interesting case: folding them onto an edge cell invents a
    location, and dropping them repeats the defect. They are counted and reported.

Nothing else in CI can see any of this. The function type-checks with or without the
clamp and with or without the guard, so the PREPARE sweep passes either way; the plan
rail (tests/test_browse_map_cells_plan.py) reads the projection, not the arithmetic; and
the fake connections cannot evaluate SQL at all.

THE OBSTACLE, AND THE TECHNIQUE — copied from tests/test_broker_leaderboard_live.py.
`properties_map_mv` is a MATERIALIZED VIEW: you cannot INSERT into it, and CI's replayed
copy is empty. So each test PARKS it (renames it) and creates a real table of the same
shape in its place. Verified safe: nothing in the catalog depends on properties_map_mv
(checked live 2026-08-26 — zero rewrite dependencies), and `browse_map_cells` is plpgsql,
whose body is a string that re-resolves names at plan time, so it picks up the substitute.
The conn fixture is FUNCTION-scoped and parks before the function is ever called in that
session, so no cached plan can be holding the matview's OID. Everything runs inside a
transaction that always rolls back.

FLOAT DISCIPLINE. Test A passes an explicit bbox of 0..20 x 0..13 so the cell size is
exactly 1.0 in both axes. With the CZ fallback extent the edge index is
(51.1 - 48.5) / ((51.1 - 48.5) / 13), which IEEE-754 may return as 12.999999999999998 —
the clamp assertion would then pass for the wrong reason.

Skip posture: the migrations lane sets DB_RAILS_REQUIRED=1, so a lane that loses its
TEST_DATABASE_URL goes RED instead of reporting a green skip.
"""

from __future__ import annotations

import os

import pytest

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_REQUIRED = os.environ.get("DB_RAILS_REQUIRED") == "1"

pytestmark = pytest.mark.skipif(
    not _DB_URL and not _REQUIRED,
    reason="TEST_DATABASE_URL not set — this rail runs in CI's migrations lane",
)

_PARK = """
alter materialized view public.properties_map_mv rename to pmm_parked_for_rail;
create table public.properties_map_mv (like public.pmm_parked_for_rail);
"""

# Migration 439's grid, restated so a change to either constant fails loudly here rather
# than silently widening what "bounded" means.
_COLS, _ROWS = 20, 13


@pytest.fixture()
def conn():
    if not _DB_URL:
        pytest.fail(
            "DB_RAILS_REQUIRED=1 but TEST_DATABASE_URL is not set — the migrations lane "
            "is misconfigured and this rail would otherwise have skipped green."
        )
    import psycopg

    with psycopg.connect(_DB_URL, autocommit=False) as c:
        yield c
        c.rollback()


def _park(cur) -> None:
    cur.execute(_PARK)


def _seed(cur, points) -> None:
    """One matview row per (lat, lng), with the three id spaces all set to the 1-based
    row ordinal so a filter on any of them selects the same row. Only the columns the
    function reads are set; every other predicate is guarded by a NULL parameter, so
    NULLs elsewhere are inert."""
    for i, (lat, lng) in enumerate(points):
        cur.execute(
            "insert into properties_map_mv "
            "(property_id, listing_id, obec_id, lat, lng, category_main, category_type, "
            " is_active) values (%s, %s, %s, %s, %s, 'byt', 'pronajem', true)",
            (i + 1, i + 1, i + 1, lat, lng),
        )


# Every argument is cast explicitly. Named-argument resolution is by TYPE as well as by
# name, and an int8 offered to an `integer` parameter is not implicitly castable — the call
# would fail with "function ... does not exist", which reads like a missing migration.
_CASTS = {
    "category_main_filter": "text[]",
    "category_type_filter": "text",
    "bbox_west": "double precision",
    "bbox_east": "double precision",
    "bbox_south": "double precision",
    "bbox_north": "double precision",
    "point_budget": "integer",
    "listing_ids_filter": "bigint[]",
    "obec_ids_filter": "bigint[]",
    "property_ids_filter": "bigint[]",
}


def _call(cur, **kw):
    args = {"category_main_filter": ["byt"], "category_type_filter": "pronajem", **kw}
    named = ", ".join(f"{k} => %({k})s::{_CASTS[k]}" for k in args)
    cur.execute(f"select public.browse_map_cells({named})", args)
    return cur.fetchone()[0]


def test_the_cell_count_is_bounded_by_the_grid(conn):
    """260 cells filled + 34 rows sitting exactly ON the north/east edge => 260 cells.

    RED by: deleting the `least(..., c_cols - 1)` / `least(..., c_rows - 1)` clamps in
    migration 439 — the 20 north-edge rows then land in a 14th row, the 13 east-edge rows
    in a 21st column and the corner row in both, so this returns 294 cells for the same
    294 properties, i.e. the grid stops being a grid. Every count stays correct, which is
    exactly why nothing else notices.
    """
    box = {"bbox_west": 0.0, "bbox_east": float(_COLS),
           "bbox_south": 0.0, "bbox_north": float(_ROWS)}
    centres = [(j + 0.5, i + 0.5) for i in range(_COLS) for j in range(_ROWS)]
    north_edge = [(float(_ROWS), i + 0.5) for i in range(_COLS)]
    east_edge = [(j + 0.5, float(_COLS)) for j in range(_ROWS)]
    corner = [(float(_ROWS), float(_COLS))]
    seeded = centres + north_edge + east_edge + corner

    with conn.cursor() as cur:
        _park(cur)
        _seed(cur, seeded)
        r = _call(cur, point_budget=0, **box)

    assert r["clustered"] is True
    assert r["total"] == len(seeded) == 294
    assert r["off_grid"] == 0, "every seeded row is inside the bbox, so none is off-grid"
    assert len(r["cells"]) == _COLS * _ROWS == 260, (
        f"the grid emitted {len(r['cells'])} cells for a {_COLS}x{_ROWS} grid — the "
        "north/east edge clamp is gone, so the cell count is no longer bounded by the "
        "grid and the payload can grow with the cohort."
    )
    assert sum(c["n"] for c in r["cells"]) == r["total"]


def test_rows_outside_the_extent_are_counted_and_not_relocated(conn):
    """Off-extent rows: in `total`, in `off_grid`, in NO cell, and NOT on an edge.

    With no bbox the extent is the CZ box, and the live data holds lng values from -118 to
    +125 (105 of the default cohort's 104,232 rows, measured 2026-08-26).

    RED by: dropping the `case when <inside the extent>` guard. Verified both ways it can
    be dropped, against the live grid arithmetic:
      - guard gone, `least(...)` kept  -> off_grid 5 -> 0 and all five rows RELOCATE onto
        edge cells (6 cells for 15 rows), i.e. pins invented on the Czech border;
      - guard gone, clamp gone too     -> each far-flung row mints its own cell, so the
        cell count stops being bounded by the grid at all.
    Each trips a different assertion below, and `total` stays 15 throughout — which is
    why no count-checking test can see either.
    """
    inside = [(50.08 + 0.001 * k, 14.42 + 0.001 * k) for k in range(10)]
    outside = [(-20.5, -118.2), (59.3, 125.7), (10.0, 100.0), (0.0, 0.0), (48.4, 11.9)]

    with conn.cursor() as cur:
        _park(cur)
        _seed(cur, inside + outside)
        r = _call(cur, point_budget=0)

    assert r["total"] == 15
    assert r["off_grid"] == len(outside) == 5
    assert sum(c["n"] for c in r["cells"]) == 10, (
        "cell counts plus off_grid must partition the cohort exactly — every mappable "
        "property is counted once, in a cell or in off_grid, and never in both or neither."
    )
    for c in r["cells"]:
        assert 48.5 <= c["lat"] <= 51.1 and 12.0 <= c["lng"] <= 18.9, (
            f"a cell was placed outside the grid extent at {c} — an off-extent row was "
            "relocated rather than reported."
        )


def test_the_point_budget_is_a_strict_threshold(conn):
    """At the budget the caller gets points; one row over it, cells.

    The boundary matters because the SPA branches on `clustered` and issues a SECOND read
    for the pins when it is false. RED by: writing `>=` instead of `>` — the map would
    then cluster a cohort it could have plotted, and (at budget 0, an empty cohort) would
    cluster nothing at all.
    """
    with conn.cursor() as cur:
        _park(cur)
        _seed(cur, [(50.0 + 0.01 * k, 14.0 + 0.01 * k) for k in range(5)])

        at_budget = _call(cur, point_budget=5)
        assert at_budget["total"] == 5
        assert at_budget["clustered"] is False
        assert at_budget["cells"] is None, (
            "below the budget the cells must be WITHHELD, not shipped and ignored — the "
            "caller distinguishes the two lanes on `clustered`, and a populated array "
            "under it is a payload nothing renders."
        )

        over_budget = _call(cur, point_budget=4)
        assert over_budget["clustered"] is True
        assert over_budget["cells"] and sum(c["n"] for c in over_budget["cells"]) == 5


def test_all_three_prefilter_id_spaces_are_applied(conn):
    """listing_id, obec_id AND property_id each narrow the cohort.

    The SPA's applyPrefilters emits `.in()` on all three; browse_stats_properties carries
    only two (the legacy city-quality path reaches it as city_index_rules instead), so an
    RPC modelled on its parameter list alone would drop the listing_id allowlist SILENTLY
    — the map would show the unfiltered market while Cards/Table/Count show the filtered
    one. Live whenever ?cityQualityLegacy=1 is remembered in localStorage.

    RED by: deleting any of the three `= any(...)` predicates from migration 439 — the
    corresponding call below then returns 3 instead of 1.
    """
    with conn.cursor() as cur:
        _park(cur)
        _seed(cur, [(50.0, 14.0), (50.5, 14.5), (51.0, 15.0)])

        for param in ("listing_ids_filter", "obec_ids_filter", "property_ids_filter"):
            r = _call(cur, point_budget=0, **{param: [2]})
            assert r["total"] == 1, (
                f"{param} did not narrow the cohort (total={r['total']}) — migration 439 "
                "is ignoring an allowlist the read it replaces applies."
            )


def test_an_empty_cohort_is_not_a_small_one(conn):
    """Zero rows must report clustered=false with cells null, not an empty grid.

    RED by: writing `clustered := v_total >= point_budget` — at budget 0 an EMPTY cohort
    then reports itself clustered, and the SPA renders "grouped into 0 cells — zoom in
    for pins" over a map that has nothing to zoom into. The `>` / `>=` distinction is
    invisible everywhere else because both are correct for every non-degenerate cohort.
    """
    with conn.cursor() as cur:
        _park(cur)
        r = _call(cur, point_budget=0)

    assert r["total"] == 0
    assert r["off_grid"] == 0
    assert r["clustered"] is False
    assert r["cells"] is None
