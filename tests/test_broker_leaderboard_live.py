"""The broker leaderboard's behaviour, executed (migrations 434, 435).

Two properties nothing else in CI can see, because both are about which rows come BACK:

  * **The page must never under-fill.** W4 pushes the LIMIT under the display join. If the
    `status='active'` filter were left ABOVE that LIMIT, a merged_away broker holding
    surviving `broker_region_type_stats` rows would consume a top-N slot and then be
    discarded — returning fewer than `p_limit` rows, and shrinking `api/outreach.py`'s
    limit=2000 candidate pool before its own email filter runs. The statement type-checks
    either way; only executing it can tell.

    This is LIVE, not theoretical: 5 brokers with `status <> 'active'` hold matview rows
    right now, and of 19,200 merged-away brokers, 717 carry an `active_property_count` at
    or above the default byt/prodej cut of 26.

  * **Ties must resolve deterministically.** A top-N heapsort breaks ties by INPUT order, so
    "run it 100 times and compare" is a no-op assertion against a stable plan. The rail
    below loads the same rows in ascending and descending id order instead — which actually
    discriminates, because without the tiebreaker the two loads return different brokers.

THE OBSTACLE, AND THE TECHNIQUE. `broker_region_type_stats` is a MATERIALIZED VIEW: you
cannot INSERT into it, and CI's replayed copy is empty. So each test PARKS the matview
(renames it) and creates a real table with the same shape in its place. Two catalog facts
make that safe, both verified: a matview's dependents are tracked by OID so a rename carries
them along (no CASCADE, nothing breaks), and `broker_leaderboard` is a `LANGUAGE sql`
function whose body is a string — it records no dependency and re-resolves names at plan
time, so it picks up the substitute. Everything runs inside a transaction that always rolls
back.

Skip posture: the migrations lane sets `DB_RAILS_REQUIRED=1`, so a lane that loses its
`TEST_DATABASE_URL` goes RED instead of reporting a green skip.
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

_PARK = """
alter materialized view public.broker_region_type_stats rename to brts_parked_for_rail;
create table public.broker_region_type_stats (like public.brts_parked_for_rail);
"""


@pytest.fixture()
def conn():
    if not _DB_URL:
        pytest.fail(
            "DB_RAILS_REQUIRED=1 but TEST_DATABASE_URL is not set — the migrations lane "
            "is misconfigured and this rail would otherwise have skipped green."
        )
    import psycopg

    with psycopg.connect(_DB_URL, autocommit=False) as c:
        yield c
        c.rollback()


def _park(cur):
    cur.execute(_PARK)


def _seed_broker(cur, broker_id: int, status: str = "active", firm_id=None):
    cur.execute(
        "insert into brokers (id, display_name, status, primary_firm_id) "
        "values (%s, %s, %s, %s)",
        (broker_id, f"broker-{broker_id}", status, firm_id),
    )


def _seed_stats(cur, broker_id: int, apc: int, geo_level: str = "region", geo_id: int = 1):
    cur.execute(
        "insert into broker_region_type_stats "
        "(broker_id, geo_level, geo_id, category_main, category_type, "
        " listing_count, property_count, active_listing_count, active_property_count) "
        "values (%s, %s, %s, 'byt', 'prodej', %s, %s, %s, %s)",
        (broker_id, geo_level, geo_id, apc, apc, apc, apc),
    )


def _leaderboard(cur, limit: int = 10, **kw):
    cur.execute(
        "select broker_id from public.broker_leaderboard("
        "  p_category_main => 'byt', p_category_type => 'prodej',"
        "  p_metric => 'active_property_count', p_limit => %s)",
        (limit,),
    )
    return [r[0] for r in cur.fetchall()]


def test_page_stays_full_under_merge_staleness(conn):
    """A merged_away broker with surviving stats rows must not eat a slot.

    RED by: moving the semi-join out of `agg` into the final SELECT's WHERE (the
    shipped-before-W4 shape) — the call then returns 10 rows' worth of candidates minus
    the discarded one, i.e. 9.
    """
    with conn.cursor() as cur:
        _park(cur)
        # 12 live brokers, descending metric, plus one retired broker that would outrank
        # every one of them. This reproduces the live condition exactly.
        for i, apc in enumerate(range(100, 88, -1), start=1):
            _seed_broker(cur, i)
            _seed_stats(cur, i, apc)
        _seed_broker(cur, 999, status="merged_away")
        _seed_stats(cur, 999, 1000)
        cur.execute("analyze brokers; analyze broker_region_type_stats;")

        rows = _leaderboard(cur, limit=10)

    assert len(rows) == 10, (
        f"the page under-filled: {len(rows)} rows for p_limit=10. The activity filter is "
        "being applied AFTER the LIMIT, so the merged_away broker consumed a slot."
    )
    assert 999 not in rows, "a merged_away broker was returned"
    assert rows == list(range(1, 11)), f"expected the top 10 actives in order, got {rows}"


def test_ties_resolve_the_same_way_regardless_of_insert_order(conn):
    """A top-N heapsort breaks ties by INPUT order — so load order is the discriminator.

    RED by: deleting `, a.broker_id` from the `top` CTE's ORDER BY. The descending load
    then returns [20..11] instead of [1..10].
    """
    results = []
    for order in (range(1, 21), range(20, 0, -1)):
        with conn.cursor() as cur:
            cur.execute("savepoint tie_rail")
            _park(cur)
            for i in order:
                _seed_broker(cur, i)
                _seed_stats(cur, i, 5)  # a total tie
            cur.execute("analyze brokers; analyze broker_region_type_stats;")
            results.append(_leaderboard(cur, limit=10))
            cur.execute("rollback to savepoint tie_rail")

    ascending_load, descending_load = results
    assert ascending_load == list(range(1, 11)), (
        f"ascending load returned {ascending_load}, expected the 10 lowest ids"
    )
    assert descending_load == list(range(1, 11)), (
        f"descending load returned {descending_load} — the tiebreaker is missing, so which "
        "of the tied brokers make the page depends on physical row order"
    )


def test_empty_firm_array_still_means_no_firm_matches(conn):
    """`'{}'` must keep meaning "nothing", not "everything".

    The fail-open `x or None` idiom in toolkit/brokers.py means `[]` never reaches the
    function from the API today, which is exactly what makes this safe to break unnoticed.

    RED by: rewriting `p_firm_ids is null` as `coalesce(array_length(p_firm_ids,1),0) = 0`.
    """
    with conn.cursor() as cur:
        _park(cur)
        for i in range(1, 6):
            _seed_broker(cur, i)
            _seed_stats(cur, i, 10 * i)
        cur.execute("analyze brokers; analyze broker_region_type_stats;")

        cur.execute(
            "select count(*) from public.broker_leaderboard("
            "  p_category_main => 'byt', p_category_type => 'prodej', p_firm_ids => '{}')"
        )
        empty_array = cur.fetchone()[0]
        cur.execute(
            "select count(*) from public.broker_leaderboard("
            "  p_category_main => 'byt', p_category_type => 'prodej', p_firm_ids => null)"
        )
        null_array = cur.fetchone()[0]

    assert empty_array == 0, (
        f"p_firm_ids => '{{}}' returned {empty_array} rows — an empty array must match no "
        "firm, not every firm"
    )
    assert null_array == 5, f"p_firm_ids => null must be unconstrained, got {null_array}"


def test_geo_arms_do_not_duplicate_a_broker_present_at_two_levels(conn):
    """The UNION ALL must sit BELOW one GROUP BY.

    Splitting the 4-way OR into level-guarded arms is the single most likely place to
    introduce a duplicate: a per-arm GROUP BY would emit two CTE rows for a broker holding
    both a region and an okres row, doubling its counts AND its output row.

    RED by: giving each arm its own GROUP BY.
    """
    with conn.cursor() as cur:
        _park(cur)
        _seed_broker(cur, 1)
        _seed_stats(cur, 1, 10, geo_level="region", geo_id=1)
        _seed_stats(cur, 1, 7, geo_level="okres", geo_id=2)
        cur.execute("analyze brokers; analyze broker_region_type_stats;")

        cur.execute(
            "select broker_id, active_property_count from public.broker_leaderboard("
            "  p_region_ids => '{1}', p_okres_ids => '{2}',"
            "  p_category_main => 'byt', p_category_type => 'prodej')"
        )
        rows = cur.fetchall()

    assert len(rows) == 1, f"broker 1 was duplicated across geo arms: {rows}"
    assert rows[0][1] == 17, (
        f"expected the two levels summed once (10 + 7 = 17), got {rows[0][1]} — a per-arm "
        "GROUP BY would double it"
    )
