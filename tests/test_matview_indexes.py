"""Every served matview stays CONCURRENTLY-refreshable, and the broker matview keeps
its covering index (Broker Unify W1).

Migration 508 DROP+CREATEd broker_region_type_stats and silently lost migration 414's
covering index — its header even claimed the "two indexes ... are unchanged" while the
object had three. The leaderboard's fast arm regressed from a 56 ms index-only scan to
a 3.2–5.4 s heap-visiting scan, and nothing went red. These rails make an index-set
regression a CI failure instead of a silent one, and pin the structural precondition
of the migration-578 chokepoint: REFRESH ... CONCURRENTLY needs one unique,
predicate-free, expression-free index per matview.

Lane: migrations, with `DB_RAILS_REQUIRED=1` (the test_derived_artifacts_stamping
idiom) so a lane that loses `TEST_DATABASE_URL` goes RED instead of skipping green.
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

# The exact published index set of the broker leaderboard's read model. A new index is
# a deliberate change: update this pin in the same PR that ships the migration.
_BROKER_MATVIEW_INDEXES = {
    "CREATE UNIQUE INDEX broker_region_type_stats_pk ON public.broker_region_type_stats "
    "USING btree (broker_id, geo_level, geo_id, category_main, category_type)",
    "CREATE INDEX broker_region_type_stats_rank_idx ON public.broker_region_type_stats "
    "USING btree (geo_level, geo_id, category_main, category_type, active_property_count DESC) "
    "INCLUDE (broker_id, listing_count, property_count, active_listing_count)",
}


@pytest.fixture(scope="module")
def conn():
    if not _DB_URL:
        pytest.fail(
            "DB_RAILS_REQUIRED=1 but TEST_DATABASE_URL is not set — the migrations "
            "lane is misconfigured and this rail would otherwise have skipped green."
        )
    import psycopg

    with psycopg.connect(_DB_URL, autocommit=True) as c:
        yield c


def test_every_matview_is_concurrently_refreshable(conn):
    """RED by: creating a matview without a unique index, or making the only unique
    index partial or expression-based. Any of those makes REFRESH ... CONCURRENTLY
    impossible, so the matview could only ever be published by the blocking form —
    the class migration 578 exists to end."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select m.matviewname
              from pg_matviews m
             where m.schemaname = 'public'
               and not exists (
                    select 1
                      from pg_index i
                      join pg_class c on c.oid = i.indexrelid
                      join pg_class t on t.oid = i.indrelid
                      join pg_namespace n on n.oid = t.relnamespace
                     where n.nspname = 'public'
                       and t.relname = m.matviewname
                       and i.indisunique
                       and i.indpred is null
                       and i.indexprs is null
               )
             order by 1
            """
        )
        offenders = [r[0] for r in cur.fetchall()]
    assert not offenders, (
        f"matview(s) with no plain unique index, i.e. not CONCURRENTLY-refreshable: "
        f"{offenders}. Add a unique index over the natural key in the same migration."
    )


def test_broker_region_type_stats_keeps_its_index_set(conn):
    """RED by: a DROP+CREATE of the matview that re-creates fewer or different indexes
    — exactly how migration 508 lost 414's covering index without any test noticing."""
    with conn.cursor() as cur:
        cur.execute(
            "select indexdef from pg_indexes "
            "where schemaname = 'public' and tablename = 'broker_region_type_stats'"
        )
        found = {r[0] for r in cur.fetchall()}
    assert found == _BROKER_MATVIEW_INDEXES, (
        "broker_region_type_stats' index set drifted.\n"
        f"  missing: {sorted(_BROKER_MATVIEW_INDEXES - found)}\n"
        f"  extra:   {sorted(found - _BROKER_MATVIEW_INDEXES)}\n"
        "If this is a deliberate redesign, update _BROKER_MATVIEW_INDEXES in the same PR."
    )
