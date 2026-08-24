"""`pipeline_checks_public` must still mean "the newest row per check_key" (migration 422).

/health reads 15 rows out of a 6,234-row append-only table. The old body said
`distinct on (check_key) … order by check_key, run_at desc`, which is correct but had to
walk every index entry to find each group boundary — 6,234 rows read for 15, and getting
worse with every check run. 422 replaces it with a loose index scan: a recursive CTE hops
key-to-key through `(check_key, run_at DESC)`, then one LATERAL `limit 1` per key.

That rewrite is subtle in ways a shape assertion cannot see — the key-hop terminates by
emitting a NULL, the seek is a strict `>`, and an empty table must yield no rows rather
than an error. So this executes the view against the replayed schema instead of reading it.

Runs in CI's migrations job (`TEST_DATABASE_URL`); skipped locally.
"""

from __future__ import annotations

import os

import pytest

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job",
)

_SEED = """
insert into pipeline_check_results (run_at, check_key, status, value, details) values
  ('2026-01-01T00:00:00Z', 'zzz_last',  'ok',   1, '{"n":1}'),
  ('2026-01-03T00:00:00Z', 'zzz_last',  'fail', 3, '{"n":3}'),
  ('2026-01-02T00:00:00Z', 'zzz_last',  'warn', 2, '{"n":2}'),
  ('2026-01-05T00:00:00Z', 'aaa_first', 'warn', 9, '{"n":9}'),
  ('2026-01-04T00:00:00Z', 'aaa_first', 'ok',   8, '{"n":8}'),
  ('2026-01-06T00:00:00Z', 'mmm_mid',   'ok',   7, '{"n":7}')
"""


@pytest.fixture()
def cur():
    """Each test runs in a transaction that is ALWAYS rolled back.

    These tests delete and reseed a real table, so the rollback is not tidiness — it is what
    keeps them from corrupting the schema the rest of the CI job asserts against.
    """
    import psycopg

    conn = psycopg.connect(_DB_URL)  # autocommit off: everything below is one transaction
    try:
        with conn.cursor() as c:
            yield c
    finally:
        conn.rollback()
        conn.close()


def _gate_is_open(cur) -> bool:
    cur.execute("select is_platform_admin()")
    return bool(cur.fetchone()[0])


def test_admin_gate_is_open_for_this_session(cur):
    """Guard against the rest of this file passing vacuously.

    The view is admin-gated; a session the gate rejects sees zero rows, which would make
    every assertion below trivially true.
    """
    assert _gate_is_open(cur), "is_platform_admin() is false — the tests below prove nothing"


def test_returns_exactly_the_newest_row_per_key(cur):
    cur.execute("delete from pipeline_check_results")
    cur.execute(_SEED)
    cur.execute(
        "select check_key, status, value::int from pipeline_checks_public order by check_key"
    )
    assert cur.fetchall() == [
        ("aaa_first", "warn", 9),
        ("mmm_mid", "ok", 7),
        ("zzz_last", "fail", 3),
    ]


def test_key_hop_terminator_never_leaks_a_null_row(cur):
    """The recursive CTE ends by selecting a NULL key; that row must be filtered out."""
    cur.execute("delete from pipeline_check_results")
    cur.execute(_SEED)
    cur.execute("select count(*) from pipeline_checks_public where check_key is null")
    assert cur.fetchone()[0] == 0


def test_every_distinct_key_is_represented_exactly_once(cur):
    """The `>` seek must not skip a key or revisit one."""
    cur.execute("delete from pipeline_check_results")
    cur.execute(_SEED)
    cur.execute(
        "select (select count(*) from pipeline_checks_public), "
        "       (select count(distinct check_key) from pipeline_check_results)"
    )
    rows, keys = cur.fetchone()
    assert rows == keys == 3


def test_empty_table_yields_no_rows_and_no_error(cur):
    """The recursive base case finds nothing; the CTE must terminate rather than fail."""
    cur.execute("delete from pipeline_check_results")
    cur.execute("select count(*) from pipeline_checks_public")
    assert cur.fetchone()[0] == 0


def test_single_key_still_terminates(cur):
    """One key means the very first hop is already the last — the tightest loop."""
    cur.execute("delete from pipeline_check_results")
    cur.execute(
        "insert into pipeline_check_results (run_at, check_key, status) values "
        "('2026-01-01T00:00:00Z','only','ok'), ('2026-01-02T00:00:00Z','only','fail')"
    )
    cur.execute("select check_key, status from pipeline_checks_public")
    assert cur.fetchall() == [("only", "fail")]


def test_matches_the_distinct_on_it_replaced(cur):
    """Equivalence with the old body, asserted rather than trusted."""
    cur.execute("delete from pipeline_check_results")
    cur.execute(_SEED)
    # Each operand lives in its own CTE: an ORDER BY cannot sit directly in front of an
    # EXCEPT, and the DISTINCT ON needs one to mean anything.
    cur.execute(
        "with ref as ("
        "  select distinct on (check_key) check_key, run_at, status, value, details, created_at"
        "    from pipeline_check_results order by check_key, run_at desc"
        "), live as ("
        "  select check_key, run_at, status, value, details, created_at"
        "    from pipeline_checks_public"
        ") select (select count(*) from (select * from ref except select * from live) a)"
        "       + (select count(*) from (select * from live except select * from ref) b)"
    )
    assert cur.fetchone()[0] == 0


def test_view_still_uses_the_key_hop_not_a_full_scan(cur):
    """A revert to plain DISTINCT ON would stay CORRECT and silently go back to reading the
    whole table, so correctness alone cannot guard this. Pin the mechanism."""
    cur.execute("select pg_get_viewdef('public.pipeline_checks_public'::regclass, true)")
    body = cur.fetchone()[0].lower()
    assert "recursive" in body, "the loose index scan is gone — /health is full-scanning again"
    assert "distinct on" not in body
