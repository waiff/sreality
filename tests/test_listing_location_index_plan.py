"""`listing_location`'s two indexes must be able to serve the reads they exist for
(migration 501).

This rail replaces the migration-429 one, and the reason is the W2-a deletion: that rail
EXPLAINed the drain's bulk member read of `listing_location_current` — the property-rollup
read that `llc_property_listing` was built for — and the drain does not make that read any
more. `_rebuild_properties` was deleted with `property_location_current`, so the rail had
nothing left to plan and the index it guards drops with the table in W2-b.

What migration 501 ships instead is two indexes and one write path, and only a replayed
catalog can tell whether the DDL actually produced them:

* `(obec_kod, granularity)` — the cohort read every W3 consumer makes, "the listings in
  this town, at this precision". Granularity is compared by EQUALITY against a set of
  labels resolved from `location_granularity_rank`, never as an ordinal range, so the
  index's second column is an `= ANY(...)` and the composite shape is what serves it.
* a GiST on `geom` — the map/bbox read.

Nothing else in CI can see this. The DDL type-checks either way, `test_sql_schema_prepare`
PREPAREs the statements with or without an index, and the fake connections in the unit
tests cannot produce a plan at all.

WHY `enable_seqscan = off`: CI's replayed `listing_location` is EMPTY, and at zero rows a
seq scan is genuinely the cheapest plan — so asserting "the planner prefers the index"
would fail in CI for a reason that has nothing to do with the schema. What CI *can* prove,
and what matters, is that the index **can serve this exact shape**.

SKIP BEHAVIOUR, per the standing rule that "a skipped rail must never be mistaken for a
green one". A bare `skipif(not TEST_DATABASE_URL)` means that if the migrations lane ever
loses its env var, this rail reports *skipped* and the lane stays green while asserting
nothing. So the lane sets `DB_RAILS_REQUIRED=1`, and the two signals combine:

    no DB, not required (local dev, the no-DB `pytest -q` lane)  -> skipped, correctly
    no DB, REQUIRED     (the migrations lane, misconfigured)     -> collected, RED
    DB present                                                   -> runs
"""

from __future__ import annotations

import json
import os

import pytest

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_REQUIRED = os.environ.get("DB_RAILS_REQUIRED") == "1"

pytestmark = pytest.mark.skipif(
    not _DB_URL and not _REQUIRED,
    reason="TEST_DATABASE_URL not set — this rail runs in CI's migrations lane",
)

_TABLE = "listing_location"
_COHORT_INDEX = "listing_location_obec_granularity"
_GEOM_INDEX = "listing_location_geom_gist"

# The shape the composite index exists for. It is spelled here rather than imported,
# because its consumers land in W3 — and it is spelled with EQUALITY on `granularity`
# deliberately: comparing the enum ordinally in a predicate is forbidden (01 §0.4), so a
# precision floor arrives as the SET of labels at or above it.
_COHORT_SQL = f"""
SELECT listing_id FROM {_TABLE}
 WHERE obec_kod = ANY(%s::bigint[])
   AND granularity = ANY(%s::location_granularity[])
"""


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


def _indexdef(conn, name: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "select indexdef from pg_indexes where schemaname='public' "
            "and tablename=%s and indexname=%s",
            (_TABLE, name),
        )
        row = cur.fetchone()
    return None if row is None else row[0].lower()


def _nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def test_the_answer_table_exists_with_its_primary_key(conn):
    with conn.cursor() as cur:
        cur.execute(
            "select a.attname from pg_index i "
            "join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey) "
            "where i.indrelid = %s::regclass and i.indisprimary",
            (_TABLE,),
        )
        assert [r[0] for r in cur.fetchall()] == ["listing_id"]


def test_the_cohort_index_keeps_both_key_columns(conn):
    """A bare `(obec_kod)` index still satisfies the town half, so the second key column is
    the thing worth asserting: without it every precision-filtered cohort read re-checks
    `granularity` on the heap."""
    indexdef = _indexdef(conn, _COHORT_INDEX)
    assert indexdef, f"{_COHORT_INDEX} is missing — migration 501 did not ship its cohort index"
    assert "(obec_kod, granularity)" in indexdef, indexdef


def test_the_geometry_index_is_a_gist(conn):
    indexdef = _indexdef(conn, _GEOM_INDEX)
    assert indexdef, f"{_GEOM_INDEX} is missing — the map read has no index"
    assert "using gist" in indexdef and "(geom)" in indexdef, indexdef


def test_the_cohort_read_is_servable_by_the_composite_index(conn):
    with conn.cursor() as cur:
        # Plain SET, not SET LOCAL: this connection is autocommit, and outside a
        # transaction SET LOCAL is a silent no-op — the plan comes back as a Seq Scan and
        # the rail fails for a reason that has nothing to do with the index.
        cur.execute("set enable_seqscan = off")
        cur.execute(
            "EXPLAIN (FORMAT JSON) " + _COHORT_SQL, ([554782], ["address_point", "building"])
        )
        plan = cur.fetchone()[0][0]["Plan"]
    nodes = list(_nodes(plan))
    assert any(n.get("Index Name") == _COHORT_INDEX for n in nodes), (
        f"the cohort read cannot use {_COHORT_INDEX}: "
        f"{[n['Node Type'] for n in nodes]}\n" + json.dumps(plan, indent=2)
    )
