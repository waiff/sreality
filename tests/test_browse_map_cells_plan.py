"""The map grid must stay an INDEX-ONLY aggregate (migration 439).

W6b's whole claim is that the map can count its entire cohort for less than it used to
cost to ship half of it. Measured live 2026-08-26 on the default cohort
(byt/pronajem, no bbox):

    today's read   Limit -> Index Scan using properties_map_mv_cover
                   Buffers: shared hit=799 read=3139   = 3,938 blocks / 50,000 rows
    the aggregate  HashAggregate -> Index Only Scan using properties_map_mv_cover
                   Heap Fetches: 0
                   Buffers: shared hit=1627            = 1,627 blocks / 104,232 rows

That 1,627 is contingent on ONE thing, and it is not a tuning knob. properties_map_mv's
only usable index is

    (category_main, category_type, lat, lng)
    INCLUDE (sreality_id, price_czk, disposition, subtype, area_m2, district,
             last_seen_at, first_seen_at, is_active)

so `lat` and `lng` are index-resident and the aggregate never touches the heap. Naming
ANY column outside that list in the aggregate's target list -- `obec_id`, `property_id`,
`source` and `listing_id` are the four that a future editor is most likely to reach for --
turns the Index Only Scan into an Index Scan against a 433 MB matview and the win is gone.

Nothing else in CI can see that. The statement returns identical numbers either way, so
the behavioural rail (tests/test_browse_map_cells_live.py) passes with or without it; the
PREPARE sweep type-checks a target list without costing it; and no fake connection can
produce a plan at all.

THE TARGET LIST IS READ OUT OF THE SHIPPED FUNCTION, not transcribed here -- that is what
makes this a rail on the code rather than on a copy of it. The WHERE is the test's own
(one cohort, two literals): the fragile half is the projection, and pinning the predicate
too would just re-assert migration 436's text.

WHY THIS IS A CATALOG ASSERTION AND NOT A PLAN ONE. The first cut of this rail EXPLAINed
the extracted projection with seqscan, bitmapscan AND indexscan disabled, and asserted the
node came back `Index Only Scan`. It went RED in CI with a `Seq Scan` priced at 1e10 -- and
the reason is documented Postgres behaviour, not a CI artefact: **`enable_indexonlyscan`
has no effect when `enable_indexscan` is off**, because an index-only scan is a variant of
an index scan. Disabling all three left the planner with no scan method at all, so it took
the least-penalised one. Removing that third GUC is necessary but not sufficient: CI's
replayed matview is EMPTY, `relallvisible` is 0, and at zero visible pages an index-only
scan is priced identically to an index scan -- so "the planner preferred index-only" on
this table is a coin flip that has nothing to do with the defect.

The property that actually produces `Heap Fetches: 0` is not a preference at all. It is
containment: every column the aggregate projects off `properties_map_mv` must live in the
cover index's key or INCLUDE list. That is decidable from the catalog, needs no rows, no
statistics and no planner, and it cannot flip. So it is asserted directly -- against the
SHIPPED function body, not a transcription of it -- and the plan-shape half of the claim
stays where it can be measured honestly: against production, recorded in the PR body
(HashAggregate -> Index Only Scan, Heap Fetches: 0, 1,627 blocks).

Skip posture, per the Cardinality Doctrine's standing rule that a skipped rail must never
be mistaken for a green one: the migrations lane sets DB_RAILS_REQUIRED=1, so a lane that
loses its TEST_DATABASE_URL goes RED instead of reporting a green skip.
"""

from __future__ import annotations

import os
import re

import pytest

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_REQUIRED = os.environ.get("DB_RAILS_REQUIRED") == "1"

pytestmark = pytest.mark.skipif(
    not _DB_URL and not _REQUIRED,
    reason="TEST_DATABASE_URL not set — this rail runs in CI's migrations lane",
)

_INDEX = "properties_map_mv_cover"

# The plpgsql locals the aggregate's target list closes over. Bound here to the values the
# function itself computes for a bbox-less call (the CZ extent, 20 x 13), so the EXPLAINed
# projection is arithmetically the production one.
_GRID_LOCALS = {
    "v_s": "48.5", "v_n": "51.1", "v_w": "12.0", "v_e": "18.9",
    "v_cw": "0.345", "v_ch": "0.2",
    "c_cols": "20", "c_rows": "13",
}


@pytest.fixture(scope="module")
def conn():
    if not _DB_URL:
        pytest.fail(
            "DB_RAILS_REQUIRED=1 but TEST_DATABASE_URL is not set — the migrations lane "
            "is misconfigured and this rail would otherwise have skipped green."
        )
    import psycopg

    with psycopg.connect(_DB_URL, autocommit=True) as c:
        yield c


def _functiondef(conn) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "select pg_get_functiondef(p.oid) from pg_proc p "
            "join pg_namespace n on n.oid = p.pronamespace "
            "where n.nspname = 'public' and p.proname = 'browse_map_cells'"
        )
        row = cur.fetchone()
    assert row, "public.browse_map_cells is not in the catalog — migration 439 did not apply"
    return row[0]


def _aggregate_target_list(conn) -> str:
    """The SELECT list of migration 439's grid aggregate, taken from the catalog and with
    its plpgsql locals bound to the values a bbox-less call produces."""
    src = re.sub(r"--[^\n]*", "", _functiondef(conn))
    start = src.index("with g as (")
    sel = src.index("select", start) + len("select")
    end = src.index("from properties_map_mv l", sel)
    target = src[sel:end]
    for name, value in _GRID_LOCALS.items():
        target = re.sub(rf"\b{name}\b", value, target)
    assert "l.lat" in target and "l.lng" in target, (
        "the extracted target list does not look like the grid aggregate — the function "
        f"body's shape changed and this rail's extraction needs updating: {target!r}"
    )
    return target


def _cover_index_columns(conn) -> set[str]:
    """Every column the cover index can serve without a heap fetch: its key AND its
    INCLUDE list. Read from the catalog rather than transcribed, so a rebuild that
    changes the index is reflected here automatically."""
    with conn.cursor() as cur:
        cur.execute("select indexdef from pg_indexes where indexname = %s", (_INDEX,))
        row = cur.fetchone()
    assert row, f"{_INDEX} does not exist — rebuild_properties_map_mv() no longer creates it"
    return {
        col.strip().strip('"')
        for group in re.findall(r"\(([^()]*)\)", row[0])
        for col in group.split(",")
    }


def test_the_grid_aggregate_projects_only_index_resident_columns(conn):
    """No column outside the cover index may appear in the grid aggregate's projection.

    This is what makes `Heap Fetches: 0` reachable, and it is the whole basis of the
    1,627-block figure against a 433 MB matview. `obec_id`, `property_id`, `source` and
    `listing_id` are the four an editor is most likely to reach for; none is in the index.

    RED by: adding `l.obec_id` (or any other off-index column) to the aggregate's target
    list in migration 439 — verified against production, where doing so turns the
    Index Only Scan into a plain Index Scan.
    """
    target = _aggregate_target_list(conn)
    projected = {m.group(1) for m in re.finditer(r"\bl\.([a-z_][a-z0-9_]*)", target)}
    assert projected, f"no l.<column> references found in the target list: {target!r}"
    resident = _cover_index_columns(conn)
    off_index = sorted(projected - resident)
    assert off_index == [], (
        "migration 439's grid aggregate projects column(s) that are not in "
        f"{_INDEX}'s key or INCLUDE list: {off_index}. The scan can no longer be served "
        "without the heap, so W6b's 1,627-block claim no longer holds. Index serves: "
        f"{sorted(resident)}"
    )


def test_the_cover_index_still_leads_on_lat_lng(conn):
    """The index W6b's numbers rest on, asserted by shape rather than by hope.

    `properties_map_mv` is DROP+CREATEd by rebuild_properties_map_mv() on every refresh,
    so its indexes are re-created from that function's body — a rebuild that reorders or
    drops the trailing (lat, lng) leaves every query working and silently un-does this
    wave. RED by: editing the cover index's key list in the rebuild function.
    """
    with conn.cursor() as cur:
        cur.execute("select indexdef from pg_indexes where indexname = %s", (_INDEX,))
        row = cur.fetchone()
    assert row, f"{_INDEX} does not exist — rebuild_properties_map_mv() no longer creates it"
    key = row[0][row[0].index("(") + 1: row[0].index(")")]
    assert [c.strip() for c in key.split(",")] == [
        "category_main", "category_type", "lat", "lng",
    ], f"{_INDEX}'s key changed: {row[0]}"
