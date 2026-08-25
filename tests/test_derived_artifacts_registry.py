"""Every derived artifact declares its own freshness — or CI says so (migration 437).

Corollary E of the Cardinality Doctrine: a precomputed artifact that does not declare its
producer, its cadence and its staleness budget is an artifact nobody can tell is stale. The
platform has fifteen of them and, before 437, exactly one pair of hard-coded columns on a
singleton state table to describe two.

`derived_artifacts` is the F5-MINIMAL shape that fixes that: one row per artifact. This rail
is the catalog diff that keeps it honest — it enumerates what the database actually derives
(every materialized view in `public`, plus a curated list of rollup TABLES, which the catalog
cannot distinguish from ordinary tables) and requires a registry row for each.

WHAT MAKES IT RED: `create materialized view foo_mv as select 1;` in a new migration with no
`derived_artifacts` row. That is the whole point — the check fires on the NEXT artifact, not
on the backlog. `_W7_BACKLOG` names what already existed when the registry shipped, and W7's
job is literally to empty that frozenset.

It also pins the two DELIBERATE ABSENCES, because both are the kind of column a later
session would "helpfully" add back:

  * `last_error` / `has_error`. A plpgsql `exception when others then update ...; raise;`
    handler CANNOT persist its write — the re-raise unwinds the subtransaction the handler
    ran in (verified live on this instance: the table comes back EMPTY). So the column could
    only ever be NULL and the published flag could only ever read `false` — including during
    a total outage, when the panel would render it green. Same defect class as migration
    432's guard that could not fire. The durable failure record is `cron.job_run_details`;
    the observable signal is `last_succeeded_at` falling behind `staleness_budget`, which
    stays correct precisely BECAUSE it rolls back with a failed run.
  * `last_started_at`, for exactly the same reason.

Lane: migrations, with `DB_RAILS_REQUIRED=1` so a lane that loses its env var goes RED
instead of reporting a green skip.
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

# Derived artifacts that are ordinary relations rather than materialized views, so the
# catalog cannot recognise them on its own. Curated deliberately: this is a short list that a
# new rollup joins by hand, which is the same one-line cost as its registry row.
_ROLLUP_TABLES = frozenset({"browse_list", "llm_cost_hour_rollup"})

# Everything that already existed, unregistered, when the registry shipped (the public
# matviews live on this instance on 2026-08-25, plus the browse read model). W7 empties this
# set; nothing may be ADDED to it — a new artifact gets a registry row instead.
_W7_BACKLOG = frozenset({
    "broker_region_type_stats",
    "browse_list",
    "category_trends_mv",
    "health_mv_refresh_stamp",
    "health_summary_mv",
    "image_storage_overview_mv",
    "images_failure_overview_mv",
    "portal_health_mv",
    "price_stat_choropleth",
    "properties_map_mv",
    "rent_map_choropleth",
    "scraper_health_checks_mv",
    "snapshot_churn_24h_mv",
})

_PUBLIC_VIEW_COLUMNS = [
    "name", "producer", "host", "cadence", "staleness_budget", "complete_through",
    "last_succeeded_at", "last_duration_ms", "last_rows", "is_serving",
]

_FORBIDDEN_COLUMNS = ("last_error", "has_error", "last_started_at")


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


def _columns(conn, relation: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "select attname from pg_attribute "
            " where attrelid = %s::regclass and attnum > 0 and not attisdropped "
            " order by attnum",
            (relation,),
        )
        return [r[0] for r in cur.fetchall()]


@pytest.fixture(scope="module")
def registered(conn) -> dict[str, tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "select name, producer, host, cadence, staleness_budget, is_serving "
            "  from public.derived_artifacts"
        )
        return {r[0]: r for r in cur.fetchall()}


def test_every_derived_artifact_outside_the_w7_backlog_is_registered(conn, registered):
    with conn.cursor() as cur:
        cur.execute(
            "select c.relname from pg_class c "
            " where c.relkind = 'm' and c.relnamespace = 'public'::regnamespace"
        )
        matviews = {r[0] for r in cur.fetchall()}
        cur.execute(
            "select c.relname from pg_class c "
            " where c.relkind = 'r' and c.relnamespace = 'public'::regnamespace "
            "   and c.relname = any(%s)",
            (sorted(_ROLLUP_TABLES),),
        )
        rollup_tables = {r[0] for r in cur.fetchall()}

    discovered = matviews | rollup_tables
    assert discovered, "no derived artifacts found at all — the catalog read is wrong"

    missing = sorted(discovered - _W7_BACKLOG - set(registered))
    assert not missing, (
        f"derived artifact(s) with no derived_artifacts row: {missing}. Every precomputed "
        "artifact declares its producer, cadence and staleness budget (Corollary E) — add a "
        "seed INSERT in the migration that creates it. Do NOT add it to _W7_BACKLOG; that "
        "set only names what predates the registry."
    )


def test_the_wave_s_own_artifact_is_registered(registered):
    """Guards the check above against passing vacuously on an empty registry."""
    row = registered.get("llm_cost_hour_rollup")
    assert row is not None, (
        "llm_cost_hour_rollup has no registry row — migration 437's seed INSERT is gone"
    )
    _, producer, host, cadence, staleness_budget, is_serving = row
    assert producer == "refresh_llm_cost_rollups"
    assert host == "pg_cron"
    assert cadence, "the artifact declares no cadence"
    assert staleness_budget is not None
    assert is_serving is True


def test_every_registry_row_declares_a_staleness_budget(registered):
    """A row without a budget publishes freshness nobody can evaluate."""
    incomplete = sorted(
        name for name, row in registered.items()
        if row[4] is None or not row[1] or not row[2] or not row[3]
    )
    assert not incomplete, (
        f"derived_artifacts row(s) missing producer/host/cadence/staleness_budget: "
        f"{incomplete}"
    )


def test_the_public_view_exposes_exactly_the_ten_declared_columns(conn):
    """`.select('*')` reads this view, so its column list IS the contract."""
    assert _columns(conn, "public.derived_artifacts_public") == _PUBLIC_VIEW_COLUMNS


@pytest.mark.parametrize("column", _FORBIDDEN_COLUMNS)
def test_the_unwritable_columns_stay_absent(conn, column):
    """RED by: adding `last_error` (or `has_error`, or `last_started_at`) back.

    A column no code path can populate is worse than no column: `has_error` would publish
    `false` throughout an outage and the panel would render it green.
    """
    assert column not in _columns(conn, "public.derived_artifacts"), (
        f"derived_artifacts grew a {column} column that no producer can durably write — "
        "see this file's docstring for why the write cannot survive its own re-raise"
    )
    assert column not in _columns(conn, "public.derived_artifacts_public"), (
        f"derived_artifacts_public publishes {column}, which can only ever read as "
        "'nothing is wrong'"
    )


def test_the_registry_is_readable_only_through_the_public_view(conn):
    """The base table stays behind RLS; the aggregate-only view is what the SPA reads."""
    with conn.cursor() as cur:
        cur.execute(
            "select has_table_privilege('anon', 'public.derived_artifacts_public', 'SELECT'), "
            "       has_table_privilege('authenticated', 'public.derived_artifacts_public', 'SELECT'), "
            "       has_table_privilege('authenticated', 'public.derived_artifacts', 'SELECT'), "
            "       has_table_privilege('anon', 'public.derived_artifacts', 'SELECT'), "
            "       (select relrowsecurity from pg_class "
            "         where oid = 'public.derived_artifacts'::regclass)"
        )
        anon_view, auth_view, auth_table, anon_table, rls = cur.fetchone()

    assert anon_view is False, "derived_artifacts_public became anon-readable"
    assert auth_view is True, "derived_artifacts_public lost its authenticated grant"
    assert auth_table is False, "authenticated reached the derived_artifacts base table"
    assert anon_table is False, "anon reached the derived_artifacts base table"
    assert rls is True, "row level security was disabled on derived_artifacts"
