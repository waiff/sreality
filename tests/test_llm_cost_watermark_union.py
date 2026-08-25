"""The rollup/live seam must neither drop a call nor count one twice (migration 437).

/costs now reads `[rollup: bucket_hour < W] UNION ALL [live: called_at >= W]`, where W is
`llm_cost_rollup_state.complete_through`. That is an exact partition only because
`bucket_hour = floor_hour(called_at)`, so for an hour-aligned W, `called_at >= W` and
`bucket_hour >= W` select the same rows. Get the comparison direction wrong in either branch
and one hour of spend is doubled or dropped — silently, forever, with every row type and
every row count still looking plausible.

Nothing else in CI can see it. The views compile either way, so the PREPARE sweep passes; the
fake connections cannot evaluate SQL at all. Only executing the seam against real rows
straddling real boundaries can tell the two apart, which is what this file does — inside a
transaction that is ALWAYS rolled back, because it deletes and reseeds `llm_calls`.

WHAT MAKES IT RED
  * `<=` in the rollup branch (the boundary hour is counted twice) or `>` in the live branch
    (it is dropped) — cases 1-3 below.
  * a non-hour-aligned watermark being accepted — case 4. `update llm_cost_rollup_state set
    complete_through = now()` is the one-statement operator mistake the CHECK exists for.
  * replacing the `coalesce((select ...), '-infinity')` scalar subqueries with a join to the
    state table — case 5. With a join, a missing singleton row makes BOTH views return zero
    rows and /costs goes blank; with the coalesce it degrades to exactly the pre-437
    behaviour.
  * `GREATEST` instead of `LEAST` in the refresh's window arithmetic — the convergence
    section's last case. Under GREATEST, `refresh_llm_cost_rollups('-infinity')` is a silent
    no-op: greatest('-infinity', W - 3h) = W - 3h, so the "full repair" repairs nothing.
  * deleting the anti-join `delete` in the refresh — a rollup group whose source rows are
    gone survives forever and "converges" becomes false.
  * turning any ON CONFLICT assignment into an accumulation (`r.x + excluded.x`) — the
    idempotency case catches it on the second refresh.

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

# The refresh's own ceiling expression, so the fixture and the assertions agree on where the
# open edge starts. `now()` is the transaction timestamp and the whole test runs in one
# transaction, so this is frozen for the duration — no test can straddle an hour boundary
# mid-run.
_CEIL = "(date_trunc('hour', now() at time zone 'UTC') at time zone 'UTC')"

# Rows placed to straddle every boundary the watermark is moved to: exactly ON the -6h mark,
# one second either side of it, the last second of the last closed hour, and one row in the
# still-open hour that no refresh may ever roll up.
_SEED = f"""
insert into llm_calls
  (called_at, called_for, provider, model, input_tokens, output_tokens,
   cache_read_tokens, cache_write_tokens, cost_usd, error)
values
  ({_CEIL} - interval '10 hours',              'parse_url', 'anthropic', 'm-a', 1, 1, 0, 0, 0.100000, null),
  ({_CEIL} - interval '10 hours',              'parse_url', 'anthropic', 'm-b', 2, 2, 0, 0, 0.200000, 'boom'),
  ({_CEIL} - interval '6 hours' - interval '1 second',
                                               'parse_url', 'anthropic', 'm-a', 1, 1, 0, 0, 0.300000, null),
  ({_CEIL} - interval '6 hours',               'parse_url', 'anthropic', 'm-a', 1, 1, 0, 0, 0.400000, null),
  ({_CEIL} - interval '6 hours' + interval '1 second',
                                               'summarize_listing', 'openai', 'm-c', 3, 3, 1, 1, 0.500000, null),
  ({_CEIL} - interval '1 hour',                'parse_url', 'gemini', 'm-d', 4, 4, 0, 0, 0.600000, null),
  ({_CEIL} - interval '1 second',              'parse_url', 'gemini', 'm-d', 5, 5, 0, 0, 0.700000, null),
  (now(),                                      'parse_url', 'anthropic', 'm-open', 6, 6, 0, 0, 0.800000, null)
"""

# Every position is hour-aligned, which is what the CHECK constraint enforces; case 4 proves
# the constraint rejects everything else.
_WATERMARKS = (
    ("-infinity", "'-infinity'::timestamptz"),
    ("ceiling - 6h", f"{_CEIL} - interval '6 hours'"),
    ("ceiling - 1h", f"{_CEIL} - interval '1 hour'"),
    ("ceiling", _CEIL),
    ("ceiling + 1h", f"{_CEIL} + interval '1 hour'"),
)


@pytest.fixture()
def cur():
    """A cursor over a transaction that is ALWAYS rolled back, fixture loaded and rolled up.

    Autocommit is deliberately OFF — everything below is one transaction, so `now()` (and
    therefore the refresh's ceiling) is frozen, and nothing this file writes can survive to
    corrupt the schema the rest of the CI job asserts against.
    """
    if not _DB_URL:
        pytest.fail(
            "DB_RAILS_REQUIRED=1 but TEST_DATABASE_URL is not set — the migrations lane "
            "is misconfigured and this rail would otherwise have skipped green."
        )
    import psycopg

    conn = psycopg.connect(_DB_URL)
    try:
        with conn.cursor() as c:
            c.execute("delete from llm_calls")
            c.execute("delete from llm_cost_hour_rollup")
            c.execute(_SEED)
            c.execute("select refresh_llm_cost_rollups('-infinity')")
            yield c
    finally:
        conn.rollback()
        conn.close()


def _one(cur, sql: str, params: tuple = ()):
    cur.execute(sql, params)
    return cur.fetchone()


def _snapshot(cur) -> list[tuple]:
    cur.execute(
        "select bucket_hour, called_for, provider, model, calls, error_calls, cost_usd, "
        "       input_tokens, output_tokens, cache_read_tokens, cache_write_tokens "
        "  from llm_cost_hour_rollup order by 1, 2, 3, 4"
    )
    return cur.fetchall()


def _set_watermark(cur, expr: str) -> None:
    cur.execute(f"update llm_cost_rollup_state set complete_through = {expr}")


# --- 5.2 the watermark-correctness rail -------------------------------------------------


@pytest.mark.parametrize(("label", "expr"), _WATERMARKS)
def test_no_closed_hour_is_dropped_or_double_counted(cur, label, expr):
    """Cases 1-3, at every watermark position: the seam is an exact partition.

    Asserted over CLOSED hours only, which is the set the union claims to cover completely:
    a watermark parked in the future legitimately leaves the still-open hour to neither
    branch, and that is not the defect this rail is about.
    """
    _set_watermark(cur, expr)
    row = _one(
        cur,
        f"""
        with u as (select * from llm_cost_hour_union where bucket < {_CEIL})
        select (select coalesce(sum(u.calls), 0) from u),
               (select coalesce(sum(u.error_calls), 0) from u),
               (select count(*) from u),
               (select count(*) from (select distinct u.bucket, u.called_for, u.provider,
                                             u.model from u) d),
               (select coalesce(sum(u.cost_usd), 0) from u),
               (select count(*) from llm_calls where called_at < {_CEIL}),
               (select count(*) from llm_calls
                 where called_at < {_CEIL} and error is not null),
               (select coalesce(sum(cost_usd), 0) from llm_calls where called_at < {_CEIL})
        """,
    )
    (u_calls, u_errors, u_rows, u_distinct, u_cost,
     direct_calls, direct_errors, direct_cost) = row

    assert direct_calls > 0, "the fixture seeded no closed-hour calls — nothing is proven"
    assert u_calls == direct_calls, (
        f"watermark at {label}: the union reports {u_calls} closed-hour calls but "
        f"llm_calls holds {direct_calls} — the seam drops or doubles the boundary hour"
    )
    assert u_errors == direct_errors, f"watermark at {label}: error_calls disagree"
    assert u_rows == u_distinct, (
        f"watermark at {label}: {u_rows} union rows for {u_distinct} distinct "
        "(bucket, called_for, provider, model) keys — a bucket is served by both branches"
    )
    assert u_cost == direct_cost, (
        f"watermark at {label}: union cost {u_cost} != llm_calls cost {direct_cost} "
        "(exact, unrounded)"
    )


def test_an_off_hour_watermark_is_rejected_by_the_database(cur):
    """Case 4 — the partition proof, enforced as a CHECK rather than left to habit.

    `bucket_hour = floor_hour(called_at)` makes the seam exact ONLY for an hour-aligned W.
    One careless `update llm_cost_rollup_state set complete_through = now()` would otherwise
    corrupt the straddling hour for every reader, forever, with nothing failing anywhere.
    """
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        # A savepoint, so the aborted statement does not poison the rest of the test.
        with cur.connection.transaction():
            _set_watermark(cur, f"{_CEIL} + interval '30 minutes'")

    cur.execute("select complete_through from llm_cost_rollup_state")
    (kept,) = cur.fetchone()
    assert kept is not None, "the state row vanished with the rejected update"


def test_a_missing_state_row_degrades_to_the_whole_table_not_to_zero_rows(cur):
    """Case 5 — the fail-safe. A missing singleton must cost money, never correctness.

    With the shipped `coalesce((select ...), '-infinity')` the rollup branch goes empty and
    the live branch covers the whole table: exactly the pre-437 numbers at the pre-437 cost.
    With the inner join the design originally proposed, both views return ZERO rows and the
    page silently goes blank.
    """
    cur.execute("delete from llm_cost_rollup_state")
    row = _one(
        cur,
        """
        select (select coalesce(sum(calls), 0) from llm_cost_hour_union),
               (select coalesce(sum(cost_usd), 0) from llm_cost_hour_union),
               (select count(*) from llm_calls),
               (select coalesce(sum(cost_usd), 0) from llm_calls)
        """,
    )
    u_calls, u_cost, direct_calls, direct_cost = row
    assert u_calls == direct_calls > 0, (
        f"with no state row the union reports {u_calls} calls against {direct_calls} in "
        "llm_calls — /costs is blank or partial instead of merely slower"
    )
    assert u_cost == direct_cost


def test_the_public_views_serve_exactly_what_the_union_holds(cur):
    """The admin gate must sit outside the set operation and change no row.

    Also guards against this file passing vacuously: if the gate were closed for this
    session, the hourly view would return zero rows against a non-empty union.
    """
    cur.execute("select is_platform_admin()")
    assert cur.fetchone()[0] is True, (
        "is_platform_admin() is false for this connection — the row-count comparison below "
        "would compare an empty set against the union and prove nothing"
    )
    row = _one(
        cur,
        """
        select (select count(*) from llm_cost_hour_union),
               (select count(*) from llm_cost_hourly_public),
               (select count(*) from (select distinct (bucket at time zone 'Europe/Prague')::date,
                                             called_for, provider, model
                                        from llm_cost_hour_union) d),
               (select count(*) from llm_cost_daily_public)
        """,
    )
    union_rows, hourly_rows, expected_days, daily_rows = row
    assert union_rows > 0, "the fixture produced no union rows — nothing is proven"
    assert hourly_rows == union_rows, (
        f"llm_cost_hourly_public serves {hourly_rows} rows over a {union_rows}-row union — "
        "the gate is filtering rows instead of short-circuiting the whole plan"
    )
    assert daily_rows == expected_days, (
        f"llm_cost_daily_public serves {daily_rows} rows for {expected_days} distinct "
        "(Prague day, called_for, provider, model) groups"
    )


# --- 5.3 the idempotency / convergence rail ---------------------------------------------


def test_three_refreshes_leave_a_byte_identical_table(cur):
    """Case 1. A FULL recompute of every touched bucket, never a delta.

    RED by: changing any ON CONFLICT assignment to `r.x + excluded.x`, which looks like a
    reasonable "accumulate" and doubles every trailing bucket on the very next tick.
    """
    snapshots = []
    for _ in range(3):
        cur.execute("select refresh_llm_cost_rollups()")
        snapshots.append(_snapshot(cur))
    assert snapshots[0] == snapshots[1] == snapshots[2], (
        "the rollup is not idempotent — re-running the refresh changes the table"
    )


def test_overlapping_windows_in_any_order_converge(cur):
    """Case 2. Order-independence is what makes a manual repair safe at any time."""
    baseline = _snapshot(cur)
    cur.execute(f"select refresh_llm_cost_rollups({_CEIL} - interval '6 hours')")
    cur.execute("select refresh_llm_cost_rollups('-infinity')")
    cur.execute(f"select refresh_llm_cost_rollups({_CEIL} - interval '1 hour')")
    assert _snapshot(cur) == baseline, (
        "overlapping refresh windows applied in a different order produced a different table"
    )


def test_a_rollup_group_whose_source_rows_vanished_is_deleted(cur):
    """Case 3. ON CONFLICT can never REMOVE, so the anti-join delete is load-bearing.

    RED by: deleting that `delete from llm_cost_hour_rollup ... where not exists (...)`.
    Without it a phantom group survives every future refresh and "converges" is simply false
    — and this is not hypothetical in CI, where the schema-replay suite really does delete
    llm_calls rows.
    """
    cur.execute(
        f"""
        insert into llm_cost_hour_rollup
          (bucket_hour, called_for, provider, model, calls, error_calls, cost_usd,
           input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
        values ({_CEIL} - interval '5 hours', 'parse_url', 'anthropic', 'm-ghost',
                7, 0, 9.990000, 0, 0, 0, 0)
        """
    )
    cur.execute("select refresh_llm_cost_rollups('-infinity')")
    cur.execute("select count(*) from llm_cost_hour_rollup where model = 'm-ghost'")
    assert cur.fetchone()[0] == 0, (
        "a rollup group with no surviving source rows outlived a full recompute"
    )


def test_a_full_repair_equals_a_from_scratch_rebuild(cur):
    """Case 4. `refresh_llm_cost_rollups('-infinity')` must be the whole truth, row for row."""
    incremental = _snapshot(cur)
    cur.execute("delete from llm_cost_hour_rollup")
    cur.execute("select refresh_llm_cost_rollups('-infinity')")
    assert _snapshot(cur) == incremental, (
        "a from-scratch rebuild disagrees with the incrementally maintained table"
    )


def test_a_full_repair_reaches_a_bucket_ten_days_behind_the_watermark(cur):
    """Case 5 — the LEAST rail, and the one assertion that fails under the design's GREATEST.

    `p_from` is a "start no LATER than" override: '-infinity' means full repair and NULL
    means the mandatory 3-hour trailing re-scan, which no caller may shrink. GREATEST
    discards '-infinity' entirely (greatest('-infinity', W - 3h) = W - 3h), which would have
    made this migration's own backfill and every operator repair a silent no-op.
    """
    _set_watermark(cur, _CEIL)
    cur.execute(
        f"""
        insert into llm_calls
          (called_at, called_for, provider, model, input_tokens, output_tokens,
           cache_read_tokens, cache_write_tokens, cost_usd, error)
        values ({_CEIL} - interval '10 days', 'parse_url', 'anthropic', 'm-late',
                1, 1, 0, 0, 1.500000, null)
        """
    )
    cur.execute("select refresh_llm_cost_rollups('-infinity')")
    cur.execute(
        "select calls, cost_usd from llm_cost_hour_rollup where model = 'm-late'"
    )
    rows = cur.fetchall()
    assert len(rows) == 1, (
        "a full repair did not reach a bucket 10 days behind the watermark — the refresh is "
        "clamping p_from with GREATEST, so '-infinity' repairs nothing"
    )
    assert rows[0][0] == 1
