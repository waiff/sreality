"""The location drain's bulk member read must be servable by an index (migration 429).

`listing_location_current` carried 15 indexes and not one led on `property_id`, so the
drain's every-15-minutes bulk read — `WHERE property_id = ANY(...) ORDER BY property_id,
listing_id` — was a seq scan. Measured live 2026-08-25 from `pg_stat_statements`:
39,488 calls x 69,327 blocks = 2.74 billion blocks, the platform's single largest disk
consumer, 55% of it physical because the scan cycles the whole 1 GB buffer pool twice a
minute. After migration 429: 434 blocks for the same 200-property batch.

Nothing else in CI can see this. The statement type-checks either way, so
`test_sql_schema_prepare` passes with or without the index, and the fake connections in
the unit tests cannot produce a plan at all.

WHY `enable_seqscan = off`: CI's replayed `listing_location_current` is EMPTY, and at zero
rows a seq scan is genuinely the cheapest plan — so asserting "the planner prefers the
index" would fail in CI for a reason that has nothing to do with the defect. What CI *can*
prove, and what actually matters, is that the index **can serve this exact shape**: the
`WHERE` as an `Index Cond` and the `ORDER BY property_id, listing_id` with **no Sort
node**. That second half is what distinguishes the shipped composite index from the bare
`(property_id)` form two of the three design proposals asked for. The production plan
(`Index Scan using llc_property_listing`, no Sort) is asserted by node name in the PR body,
per amended Corollary D.

SKIP BEHAVIOUR, per the Cardinality Doctrine's standing rule that "a skipped rail must
never be mistaken for a green one". A bare `skipif(not TEST_DATABASE_URL)` — the pattern
the older DB tests use — means that if the migrations lane ever loses its env var, this
rail reports *skipped* and the lane stays green while asserting nothing. So the lane sets
`DB_RAILS_REQUIRED=1`, and the two signals combine:

    no DB, not required (local dev, the no-DB `pytest -q` lane)  -> skipped, correctly
    no DB, REQUIRED     (the migrations lane, misconfigured)     -> collected, RED
    DB present                                                   -> runs

That is what makes the required-check claim true rather than aspirational.
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

_INDEX = "llc_property_listing"


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


def _nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def _plan(conn) -> dict:
    from location_data.resolver.drain import _PROPERTY_MEMBERS_BULK_SQL

    with conn.cursor() as cur:
        # Plain SET, not SET LOCAL: this connection is autocommit, and outside a
        # transaction SET LOCAL is a silent no-op — the plan comes back as a Seq Scan
        # and the rail fails for a reason that has nothing to do with the index.
        cur.execute("set enable_seqscan = off")
        cur.execute(
            "EXPLAIN (FORMAT JSON) " + _PROPERTY_MEMBERS_BULK_SQL, ([1, 2, 3],)
        )
        return cur.fetchone()[0][0]["Plan"]


def test_index_exists_with_both_key_columns(conn):
    """Composite and partial, deliberately — the shape the drain needs."""
    with conn.cursor() as cur:
        cur.execute(
            "select indexdef from pg_indexes where schemaname='public' "
            "and tablename='listing_location_current' and indexname=%s",
            (_INDEX,),
        )
        row = cur.fetchone()

    assert row, (
        f"{_INDEX} is missing — the drain's bulk member read is back to a seq scan over "
        "687k rows, every 15 minutes, at 69,327 blocks a call"
    )
    indexdef = row[0].lower()
    assert "(property_id, listing_id)" in indexdef, (
        f"{_INDEX} lost its second key column, so ORDER BY property_id, listing_id needs "
        f"a Sort again: {indexdef}"
    )
    assert "property_id is not null" in indexdef, (
        f"{_INDEX} lost its partial predicate: {indexdef}"
    )


def test_bulk_member_read_is_servable_by_the_index(conn):
    plan = _plan(conn)
    nodes = list(_nodes(plan))
    assert any(n.get("Index Name") == _INDEX for n in nodes), (
        f"the drain's bulk member read cannot use {_INDEX}: "
        f"{[n['Node Type'] for n in nodes]}\n" + json.dumps(plan, indent=2)
    )


def test_composite_key_removes_the_sort(conn):
    """The second key column is what makes the ORDER BY free.

    A bare `(property_id)` index still satisfies the WHERE, so this is the assertion that
    distinguishes the shipped index from the one the other designs proposed.
    """
    node_types = [n["Node Type"] for n in _nodes(_plan(conn))]
    assert "Sort" not in node_types, (
        "a Sort node appeared — the index no longer serves ORDER BY property_id, "
        f"listing_id: {node_types}"
    )
