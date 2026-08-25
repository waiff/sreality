"""The /costs rollups must stay INDEXABLE and must not round early (migrations 421, 437).

Casting or truncating a `timestamptz` depends on the session TimeZone, so
`called_at::date` and `date_trunc('hour', called_at)` are only STABLE — and Postgres
refuses to build an index on a STABLE expression:

    ERROR 42P17: functions in index expression must be marked IMMUTABLE

That is why /costs could not simply be given the "date-expression index" it wanted: the
bucket expressions had to be pinned to an explicit zone first. Migration 437 then moved the
closed hours into `llm_cost_hour_rollup` and serves reads as
`[rollup: bucket_hour < W] UNION ALL [live: called_at >= W]`, so the zone-pinning property
now lives one level down, in `llm_cost_hour_union` — that is what this file guards, together
with the property that makes the stored numbers trustworthy: the rollup stores the
UNROUNDED hourly sum, and `round()` happens once, at the outer projection of each view.

Storing `round(sum(cost_usd), 4)` per hour instead and summing 24 of them is sum-of-rounds,
not round-of-sum; measured over all 293,561 rows of history that corrupts 74 of 331 daily
groups (max error $0.0003). Nothing else can see it — every row count stays right, every
type stays right, and the money is wrong in the fourth decimal.

Runs against the replayed schema in CI's migrations job.

SKIP BEHAVIOUR, per the Cardinality Doctrine's standing rule that "a skipped rail must never
be mistaken for a green one". A bare `skipif(not TEST_DATABASE_URL)` — what this file used
before W3 — means a migrations lane that loses its env var reports *skipped* and stays green
while asserting nothing. The lane sets `DB_RAILS_REQUIRED=1`, and the two signals combine:

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

# Repointed by migration 437. The two PUBLIC views no longer touch `llm_calls` directly —
# they read `llm_cost_hour_union`, whose live branch is the only thing left that these
# migration-421 indexes serve. Asserting the zone on the daily view would now ACCIDENTALLY
# pass on its Prague literal; the UTC hour grain is what must never drift, and
# tests/test_llm_cost_zone_literals.py owns the Prague half.
_ROLLUPS = (("llm_cost_hour_union", "llm_calls_utc_hour_rollup_idx"),)

# The gated, browser-reachable pair. Separate from _ROLLUPS because the union source above
# is deliberately un-grantable: it has no admin gate of its own, so no browser role may
# reach it at all.
_PUBLIC_VIEWS = ("llm_cost_daily_public", "llm_cost_hourly_public")

# One row per hour bucket, chosen so that three properties are visible at once:
#   * 2026-03-15 08/09/10 UTC, model 'm-neg-control': three buckets, one Prague day, each
#     costing 0.000060 — so sum-of-rounds (0.0003) and round-of-sum (0.0002) DISAGREE.
#   * 2026-03-16 22:10 and 23:30 UTC: the same UTC day, DIFFERENT Prague days (23:30 UTC is
#     00:30 Prague on the 17th). A daily view that lost its zone puts them in one group.
#   * two rows in "today": one in the still-open hour (served by the live branch) and one in
#     the hour before it (served by the rollup), sharing one (called_for, provider, model)
#     so the daily group and llm_cost_today_usd() aggregate exactly the same set.
_SEED = """
insert into llm_calls
  (called_at, called_for, provider, model, input_tokens, output_tokens,
   cache_read_tokens, cache_write_tokens, cost_usd, error)
values
  ('2026-03-15T08:20:00Z', 'parse_url', 'anthropic', 'm-neg-control', 1, 2, 3, 4, 0.000060, null),
  ('2026-03-15T09:40:00Z', 'parse_url', 'anthropic', 'm-neg-control', 1, 2, 3, 4, 0.000060, null),
  ('2026-03-15T10:05:00Z', 'parse_url', 'anthropic', 'm-neg-control', 1, 2, 3, 4, 0.000060, null),
  ('2026-03-15T08:15:00Z', 'summarize_listing', 'openai', 'm-mixed', 10, 20, 30, 40, 1.234567, 'boom'),
  ('2026-03-15T08:45:00Z', 'summarize_listing', 'openai', 'm-mixed', 11, 21, 31, 41, 2.345678, null),
  ('2026-03-16T22:10:00Z', 'parse_url', 'gemini', 'm-edge', 5, 6, 7, 8, 0.111111, null),
  ('2026-03-16T23:30:00Z', 'parse_url', 'gemini', 'm-edge', 5, 6, 7, 8, 0.222222, null),
  ((date_trunc('hour', now() at time zone 'UTC') at time zone 'UTC') - interval '30 minutes',
   'parse_url', 'anthropic', 'm-today', 1, 1, 0, 0, 0.250000, null),
  (now(), 'parse_url', 'anthropic', 'm-today', 1, 1, 0, 0, 0.500000, null)
"""

# The direct, single-statement answer the daily view must reproduce exactly — grouped on the
# Prague day, with round() applied ONCE to the whole group.
_DIRECT_DAILY = """
select (l.called_at at time zone 'Europe/Prague')::date as day,
       l.called_for, l.provider, l.model,
       count(*)::integer as calls,
       count(*) filter (where l.error is not null)::integer as error_calls,
       round(sum(l.cost_usd), 4) as cost_usd,
       sum(l.input_tokens)::bigint as input_tokens,
       sum(l.output_tokens)::bigint as output_tokens,
       sum(l.cache_read_tokens)::bigint as cache_read_tokens,
       sum(l.cache_write_tokens)::bigint as cache_write_tokens
  from llm_calls l
 group by 1, 2, 3, 4
"""

# Migration 421's body, verbatim in meaning: the UTC hour grain, straight off llm_calls.
_DIRECT_HOURLY = """
select (date_trunc('hour', l.called_at at time zone 'UTC') at time zone 'UTC') as bucket,
       l.called_for, l.provider, l.model,
       count(*)::integer as calls,
       count(*) filter (where l.error is not null)::integer as error_calls,
       round(sum(l.cost_usd), 4) as cost_usd,
       sum(l.input_tokens)::bigint as input_tokens,
       sum(l.output_tokens)::bigint as output_tokens,
       sum(l.cache_read_tokens)::bigint as cache_read_tokens,
       sum(l.cache_write_tokens)::bigint as cache_write_tokens
  from llm_calls l
 group by 1, 2, 3, 4
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


@pytest.fixture()
def seeded():
    """A cursor over a transaction that is ALWAYS rolled back, with the fixture loaded.

    These tests delete and reseed `llm_calls` and rebuild the rollup from it, so the
    rollback is not tidiness — it is what keeps them from corrupting the schema the rest of
    the CI job asserts against. Autocommit is OFF, which also freezes `now()` for the whole
    test: the refresh's ceiling, the seed's "today" rows and llm_cost_today_usd() all read
    the same transaction timestamp and cannot straddle an hour boundary mid-test.
    """
    if not _DB_URL:
        pytest.fail(
            "DB_RAILS_REQUIRED=1 but TEST_DATABASE_URL is not set — the migrations lane "
            "is misconfigured and this rail would otherwise have skipped green."
        )
    import psycopg

    c = psycopg.connect(_DB_URL)
    try:
        with c.cursor() as cur:
            cur.execute("delete from llm_calls")
            cur.execute("delete from llm_cost_hour_rollup")
            cur.execute(_SEED)
            cur.execute("select refresh_llm_cost_rollups('-infinity')")
            yield cur
    finally:
        c.rollback()
        c.close()


def _nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


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

    RED by: reverting the union's live branch to `date_trunc('hour', l.called_at)`.
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


def test_daily_cost_guard_matches_the_same_index(conn):
    """The write-side spend guard must reach an index, on every recorded LLM call.

    `api.llm_client.DAILY_COST_TODAY_SQL` is now `SELECT public.llm_cost_today_usd()`, and
    EXPLAINing THAT proves nothing: a function call plans as a bare `Result` node and the
    body never appears (the pre-437 version of this test asserted an `Index Name` against
    that plan and would have failed on a perfectly indexed function). `llm_cost_today_usd`
    is LANGUAGE sql, so its `prosrc` IS executable SQL — this pulls the body out of the
    catalog and explains the real statement.

    RED by: dropping `llm_cost_hour_rollup_prague_day_idx`, or respelling the function's
    predicate so it can no longer match it (e.g. `u.bucket::date = current_date`, which is
    STABLE and matches nothing). Neither changes a single returned number.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select prosrc from pg_proc where proname='llm_cost_today_usd' "
            "and pronamespace='public'::regnamespace"
        )
        row = cur.fetchone()
        assert row, "llm_cost_today_usd() is gone — the spend guard's SQL cannot run at all"
        prosrc = row[0]

        # Plain SET, not SET LOCAL: this connection is autocommit, and outside a
        # transaction SET LOCAL is a silent no-op (the trap migration 429's rail hit).
        # CI's replayed tables are EMPTY, where a seq scan genuinely is the cheapest plan,
        # so asserting planner *preference* would fail for reasons unrelated to the defect.
        # What CI can prove is that the index CAN serve this exact predicate.
        cur.execute("set enable_seqscan = off")
        cur.execute("set enable_bitmapscan = off")
        cur.execute("EXPLAIN (FORMAT JSON) " + prosrc)
        plan = cur.fetchone()[0][0]["Plan"]
        cur.execute("reset enable_seqscan")
        cur.execute("reset enable_bitmapscan")

    found = list(_nodes(plan))
    assert not any(
        n["Node Type"].endswith("Seq Scan") and n.get("Relation Name") == "llm_calls"
        for n in found
    ), (
        "the spend guard seq-scans llm_calls — its live branch lost the zone-pinned "
        "spelling that matches llm_calls_utc_hour_rollup_idx:\n" + json.dumps(plan, indent=2)
    )
    assert any(
        n.get("Index Name") == "llm_cost_hour_rollup_prague_day_idx" for n in found
    ), (
        "the guard's rollup branch does not reach llm_cost_hour_rollup_prague_day_idx: "
        f"{[(n['Node Type'], n.get('Index Name')) for n in found]}\n"
        + json.dumps(plan, indent=2)
    )


def test_rollups_are_admin_only_not_anon_readable(conn):
    """These carry spend data. They were `anon`-dark before 421 and must stay so.

    Extended for 437: `llm_cost_hour_union` carries NO admin gate of its own (the gate must
    stay in the outermost scope of each public view, where it hoists into a One-Time Filter),
    so a grant on it would hand `authenticated` ungated spend data. It must be dark to BOTH
    browser roles.
    """
    with conn.cursor() as cur:
        for view in _PUBLIC_VIEWS:
            cur.execute(
                "select has_table_privilege('anon', %s, 'SELECT'), "
                "       has_table_privilege('authenticated', %s, 'SELECT')",
                (f"public.{view}", f"public.{view}"),
            )
            anon_select, auth_select = cur.fetchone()
            assert anon_select is False, f"{view} became anon-readable"
            assert auth_select is True, f"{view} lost its authenticated grant"

        cur.execute(
            "select has_table_privilege('anon', %s, 'SELECT'), "
            "       has_table_privilege('authenticated', %s, 'SELECT')",
            ("public.llm_cost_hour_union", "public.llm_cost_hour_union"),
        )
        anon_union, auth_union = cur.fetchone()
        assert anon_union is False, "llm_cost_hour_union became anon-readable"
        assert auth_union is False, (
            "llm_cost_hour_union became readable by `authenticated` — it has no "
            "is_platform_admin() gate, so that is ungated spend data in the browser"
        )


def test_the_admin_gate_is_open_for_this_session(seeded):
    """Guard against every value assertion below passing vacuously.

    Both public views are admin-gated; a session the gate rejects sees zero rows, and a
    zero-row EXCEPT ALL comparison is trivially equal.
    """
    seeded.execute("select is_platform_admin()")
    assert seeded.fetchone()[0] is True, (
        "is_platform_admin() is false for this connection — the value rails below would "
        "compare two empty sets and prove nothing"
    )


def test_daily_view_equals_a_direct_prague_day_aggregation(seeded):
    """Zero rows in EITHER symmetric difference, cost column included.

    RED by: storing `round(sum(cost_usd), 4)` in the rollup instead of the exact sum (the
    daily view then reports sum-of-rounds), or dropping the `::integer` / `::bigint` casts
    from the re-aggregation (six column types change silently under `.select('*')`).
    """
    seeded.execute(
        "select (select count(*) from ("
        "  select * from llm_cost_daily_public except all " + _DIRECT_DAILY + ") a),"
        "       (select count(*) from ("
        + _DIRECT_DAILY
        + " except all select * from llm_cost_daily_public) b)"
    )
    view_minus_direct, direct_minus_view = seeded.fetchone()
    assert (view_minus_direct, direct_minus_view) == (0, 0), (
        "llm_cost_daily_public disagrees with a direct Prague-day aggregation of "
        f"llm_calls: {view_minus_direct} row(s) only in the view, "
        f"{direct_minus_view} only in the direct answer"
    )


def test_hourly_view_equals_the_migration_421_body(seeded):
    """The hour grain must be byte-identical to what /costs read before the rollup existed.

    RED by: flipping the union's rollup branch to `<=` (the boundary hour is then counted
    twice) or its live branch to `>` (dropped).
    """
    seeded.execute(
        "select (select count(*) from ("
        "  select * from llm_cost_hourly_public except all " + _DIRECT_HOURLY + ") a),"
        "       (select count(*) from ("
        + _DIRECT_HOURLY
        + " except all select * from llm_cost_hourly_public) b)"
    )
    view_minus_direct, direct_minus_view = seeded.fetchone()
    assert (view_minus_direct, direct_minus_view) == (0, 0), (
        "llm_cost_hourly_public no longer reproduces migration 421's body: "
        f"{view_minus_direct} row(s) only in the view, {direct_minus_view} only in the "
        "direct answer"
    )


def test_the_fixture_can_actually_tell_the_two_roundings_apart(seeded):
    """The negative control, without which the two rails above prove nothing.

    If sum-of-rounds and round-of-sum agreed on this fixture, a rollup that rounded early
    would pass every comparison in this file. They must DISAGREE here — three hour buckets
    of $0.000060 round to $0.0001 each ($0.0003 summed) while the day sums to $0.00018,
    which rounds to $0.0002.

    RED by: changing the fixture's costs to values that survive rounding (which is exactly
    what would make the value rails vacuous).
    """
    seeded.execute(
        "select round(sum(cost_usd), 4), sum(round(cost_usd, 4)) "
        "from llm_cost_hour_union "
        "where model = 'm-neg-control'"
    )
    round_of_sum, sum_of_rounds = seeded.fetchone()
    assert round_of_sum != sum_of_rounds, (
        f"the fixture cannot distinguish the two roundings (both {round_of_sum}) — the "
        "value rails in this file would pass even with the double-rounding bug present"
    )

    seeded.execute(
        "select cost_usd from llm_cost_daily_public where model = 'm-neg-control'"
    )
    rows = seeded.fetchall()
    assert len(rows) == 1, f"expected one daily group for m-neg-control, got {rows}"
    assert rows[0][0] == round_of_sum, (
        f"llm_cost_daily_public reports {rows[0][0]} for a day whose exact sum rounds to "
        f"{round_of_sum} — it is summing pre-rounded hours"
    )


def test_today_usd_matches_the_daily_view(seeded):
    """The spend guard and the page must mean the same "today", to the cent.

    This is also the rail that catches `llm_cost_hour_union` being flipped to
    `security_invoker` — a change that reads like a security improvement. The function is
    SECURITY INVOKER by design; it sees through the RLS wall on the rollup only because the
    view it reads is not. Flip that view and the rollup branch returns zero rows for any
    caller without BYPASSRLS, the guard silently reports only the live edge, and NO exception
    is raised for the caller's try/except to catch.

    The fixture puts one "today" row in the still-open hour and one in the closed hour before
    it, so a guard that lost either branch reports the wrong total rather than zero. The
    amounts are NOT hard-coded: for roughly one hour a day (Prague 00:00-01:00) the closed
    row belongs to yesterday's Prague date, and a fixed expectation would make this rail red
    on the clock rather than on the code.
    """
    seeded.execute("select llm_cost_today_usd()")
    today_usd = seeded.fetchone()[0]

    seeded.execute(
        "select coalesce(sum(cost_usd), 0), coalesce(sum(calls), 0) "
        "from llm_cost_daily_public "
        "where day = (now() at time zone 'Europe/Prague')::date"
    )
    view_usd, view_calls = seeded.fetchone()

    seeded.execute(
        "select round(coalesce(sum(cost_usd), 0), 4), count(*) from llm_calls "
        "where (called_at at time zone 'Europe/Prague')::date "
        "    = (now() at time zone 'Europe/Prague')::date"
    )
    direct_usd, direct_calls = seeded.fetchone()

    assert direct_calls >= 1, (
        "the fixture put no call on today's Prague day — this rail would compare two zeros"
    )
    assert today_usd == direct_usd, (
        f"llm_cost_today_usd() reports {today_usd} while llm_calls itself totals "
        f"{direct_usd} for the same Prague day"
    )
    assert today_usd == view_usd, (
        f"llm_cost_today_usd() reports {today_usd} while llm_cost_daily_public reports "
        f"{view_usd} for the same Prague day"
    )
    assert view_calls == direct_calls

    # Branch coverage, stated independently of the wall clock: the closed-hour row is in the
    # rollup, the open-hour row is not, and the union serves both.
    seeded.execute("select count(*) from llm_cost_hour_rollup where model = 'm-today'")
    assert seeded.fetchone()[0] == 1, (
        "the closed 'today' hour is not in the rollup — the refresh's ceiling is wrong"
    )
    seeded.execute("select count(*) from llm_cost_hour_union where model = 'm-today'")
    assert seeded.fetchone()[0] == 2, (
        "the union does not serve both the rolled-up hour and the open edge for 'm-today'"
    )
