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

WHY enable_indexscan = off AS WELL as seqscan and bitmapscan. CI's replayed
properties_map_mv is EMPTY, and at zero rows `relallvisible` is 0, which prices an
index-only scan exactly the same as an index scan -- so on this table "the planner
preferred index-only" is a coin flip that has nothing to do with the defect. Disabling the
plain index scan removes the coin flip and asks the question that actually matters: CAN
this index serve this exact projection without the heap? With an off-index column in the
list, no index-only path exists at all and the plan comes back as `Index Scan` (verified
against production, both directions).

Skip posture, per the Cardinality Doctrine's standing rule that a skipped rail must never
be mistaken for a green one: the migrations lane sets DB_RAILS_REQUIRED=1, so a lane that
loses its TEST_DATABASE_URL goes RED instead of reporting a green skip.
"""

from __future__ import annotations

import json
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


def _nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


@pytest.fixture(scope="module")
def plan(conn) -> dict:
    stmt = (
        "select " + _aggregate_target_list(conn)
        + " from properties_map_mv l"
        + " where l.category_main = any(array['byt']) and l.category_type = 'pronajem'"
        + " group by 1, 2"
    )
    with conn.cursor() as cur:
        # Plain SET, not SET LOCAL: this connection is autocommit, and outside a
        # transaction SET LOCAL is a silent no-op — the plan comes back as a Seq Scan and
        # the rail fails for a reason that has nothing to do with the projection.
        cur.execute("set enable_seqscan = off")
        cur.execute("set enable_bitmapscan = off")
        cur.execute("set enable_indexscan = off")
        cur.execute("EXPLAIN (FORMAT JSON) " + stmt)
        return json.loads(json.dumps(cur.fetchone()[0]))[0]["Plan"]


def test_the_grid_aggregate_is_served_without_the_heap(plan):
    """The scan under the aggregate must be an INDEX ONLY Scan on the cover index.

    RED by: adding `l.obec_id`, `l.property_id`, `l.source` or `l.listing_id` to the
    aggregate's target list in migration 439 — none is in the cover index, so no
    index-only path exists and the node comes back as `Index Scan` (verified against
    production in both directions). Also RED if a future rebuild drops lat/lng out of
    the index.
    """
    scans = [
        n for n in _nodes(plan)
        if n.get("Relation Name") == "properties_map_mv"
    ]
    assert scans, f"the plan never scans properties_map_mv: {plan}"
    assert [n["Node Type"] for n in scans] == ["Index Only Scan"], (
        "migration 439's grid aggregate no longer reads properties_map_mv index-only. "
        "Every column in its target list must be in properties_map_mv_cover's key or "
        f"INCLUDE list. Plan: {scans}"
    )
    assert scans[0].get("Index Name") == _INDEX, (
        f"the aggregate is served by {scans[0].get('Index Name')!r}, not {_INDEX!r} — the "
        "block counts W6b claims were measured on the cover index."
    )


def test_the_cohort_predicate_reaches_the_index(plan):
    """category_main + category_type must land as an Index Cond, not a post-scan Filter.

    RED by: reordering the cover index so it no longer leads on
    (category_main, category_type) — every cohort read then scans the whole matview and
    filters, which is the shape the 3,938-block measurement was taken against.
    """
    scan = next(n for n in _nodes(plan) if n.get("Relation Name") == "properties_map_mv")
    cond = scan.get("Index Cond", "")
    assert "category_main" in cond and "category_type" in cond, (
        f"the cohort predicate is not an Index Cond on {_INDEX}: Index Cond={cond!r}, "
        f"Filter={scan.get('Filter')!r}"
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
