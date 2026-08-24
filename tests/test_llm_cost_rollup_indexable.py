"""The /costs rollups must stay INDEXABLE (migration 421).

Casting or truncating a `timestamptz` depends on the session TimeZone, so
`called_at::date` and `date_trunc('hour', called_at)` are only STABLE — and Postgres
refuses to build an index on a STABLE expression:

    ERROR 42P17: functions in index expression must be marked IMMUTABLE

That is why /costs could not simply be given the "date-expression index" it wanted: the
views had to be pinned to an explicit UTC zone first. This test guards the property that
makes the indexes possible AND correct. Revert either view to the bare cast and the index
silently stops matching — the plan quietly falls back to the seq scan that discarded
231,189 rows for 93 (daily) and 293,491 for 12 (hourly), with nothing else failing.

Pinning the zone is also the correctness half: without it, which day/hour a call is
attributed to depends on whatever TimeZone the *reading* session carries.

Runs against the replayed schema in CI's migrations job; skipped locally.
"""

from __future__ import annotations

import os

import pytest

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job",
)

_ROLLUPS = (
    ("llm_cost_daily_public", "llm_calls_utc_day_rollup_idx"),
    ("llm_cost_hourly_public", "llm_calls_utc_hour_rollup_idx"),
)


@pytest.fixture(scope="module")
def conn():
    import psycopg

    with psycopg.connect(_DB_URL, autocommit=True) as c:
        yield c


@pytest.mark.parametrize(("view", "index"), _ROLLUPS)
def test_rollup_index_exists(conn, view, index):
    with conn.cursor() as cur:
        cur.execute(
            "select 1 from pg_indexes where schemaname='public' and tablename='llm_calls' "
            "and indexname=%s",
            (index,),
        )
        assert cur.fetchone(), f"{index} is missing — {view} is back to a seq scan"


@pytest.mark.parametrize(("view", "index"), _ROLLUPS)
def test_rollup_bucket_is_zone_pinned(conn, view, index):
    """The view's bucket expression must name a zone explicitly.

    Without this the expression is STABLE, the index above cannot match it, and the
    attributed day/hour depends on the reader's TimeZone.
    """
    with conn.cursor() as cur:
        cur.execute("select pg_get_viewdef(%s::regclass, true)", (f"public.{view}",))
        body = cur.fetchone()[0].lower()
    assert "at time zone 'utc'" in body, f"{view} lost its explicit UTC bucket"


@pytest.mark.parametrize(("view", "index"), _ROLLUPS)
def test_rollup_index_expression_matches_the_view(conn, view, index):
    """The index and the view must agree on the expression, or the index is dead weight.

    Compares the deparsed index expression against the view body rather than against a
    hard-coded string, so a future rewrite of BOTH in step still passes and a rewrite of
    only one fails — which is the defect this guards.
    """
    with conn.cursor() as cur:
        cur.execute("select indexdef from pg_indexes where indexname=%s", (index,))
        indexdef = cur.fetchone()[0].lower()
        cur.execute("select pg_get_viewdef(%s::regclass, true)", (f"public.{view}",))
        body = cur.fetchone()[0].lower()

    # The zone-pinned core both must share, normalised of whitespace/casts.
    core = "at time zone 'utc'"
    assert core in indexdef, f"{index} is not zone-pinned"
    assert core in body

    # date vs hour: the two rollups must not accidentally point at each other's index.
    if "day" in index:
        assert "::date" in indexdef
    else:
        assert "date_trunc('hour'" in indexdef


def test_rollups_are_admin_only_not_anon_readable(conn):
    """These carry spend data. They were `anon`-dark before 421 and must stay so."""
    with conn.cursor() as cur:
        for view, _ in _ROLLUPS:
            cur.execute(
                "select has_table_privilege('anon', %s, 'SELECT'), "
                "       has_table_privilege('authenticated', %s, 'SELECT')",
                (f"public.{view}", f"public.{view}"),
            )
            anon_select, auth_select = cur.fetchone()
            assert anon_select is False, f"{view} became anon-readable"
            assert auth_select is True, f"{view} lost its authenticated grant"
