"""The LIMIT must sit BELOW the hydration join (migration 435).

W4's whole claim is that the leaderboard ranks on the matview alone, truncates, and only
then joins `brokers` and `firms` to decorate at most `p_limit` rows. Measured before:
between 87% and 99.2% of every joined-and-decorated row was discarded by the LIMIT — 2,067
of the default shape's 3,140 blocks, and 20,355 of the region chip's 22,910.

Nothing else can see a regression here. The function returns identical rows either way (the
under-fill case is `tests/test_broker_leaderboard_live.py`'s subject); only the plan differs,
and only against a real schema.

`firms` is the unambiguous hydration marker — the function touches it for nothing else — so
"is `firms` inside the Limit's subtree" is exactly the question, asked on the parsed node
tree rather than by counting substrings in the EXPLAIN text.

WHY `enable_seqscan = off`: CI's replayed tables are EMPTY, where a seq scan genuinely is
the cheapest plan, so asserting planner *preference* would fail for reasons unrelated to the
defect (the migration-429 precedent). What CI can prove is the STRUCTURE: a LIMIT inside a
subquery is a hard optimisation barrier, so `firms` cannot appear beneath it whatever the
costs say.

Skip posture: the migrations lane sets `DB_RAILS_REQUIRED=1`, so a lane that loses its
`TEST_DATABASE_URL` goes RED instead of reporting a green skip.
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


@pytest.fixture(scope="module")
def plan(conn):
    with conn.cursor() as cur:
        # Plain SET, not SET LOCAL: this connection is autocommit, and outside a
        # transaction SET LOCAL is a silent no-op (the trap migration 429's rail hit).
        cur.execute("set enable_seqscan = off")
        cur.execute(
            "explain (format json) select * from public.broker_leaderboard("
            "null, null, null, 'byt', 'prodej', 'active_property_count', 100, null)"
        )
        return cur.fetchone()[0][0]["Plan"]


def _nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def _find_limit(plan):
    for node in _nodes(plan):
        if node["Node Type"] == "Limit":
            return node
    return None


def test_the_plan_has_a_limit_node(plan):
    assert _find_limit(plan) is not None, (
        "no Limit node in the leaderboard's plan:\n" + json.dumps(plan, indent=2)
    )


def test_firms_is_not_touched_below_the_limit(plan):
    """The hydration join must be ABOVE the truncation.

    RED by: restoring migration 414's body, where `brokers_public` (which embeds the
    `firms` LEFT JOIN) is joined to the whole candidate set before the LIMIT.
    """
    limit = _find_limit(plan)
    below = [n.get("Relation Name") for n in _nodes(limit)]
    assert "firms" not in below, (
        "`firms` is scanned BELOW the Limit — the hydration join is running against the "
        f"whole candidate set again: {below}\n" + json.dumps(limit, indent=2)
    )


def test_firms_is_touched_above_the_limit(plan):
    """The mirror assertion: hydration must still happen.

    Without this, a rewrite that simply stopped returning firm columns would pass the
    test above for the wrong reason.
    """
    limit = _find_limit(plan)
    below_ids = {id(n) for n in _nodes(limit)}
    above = [
        n.get("Relation Name")
        for n in _nodes(plan)
        if id(n) not in below_ids
    ]
    assert "firms" in above, (
        f"`firms` is not joined above the Limit — hydration is missing entirely: {above}"
    )


def test_the_active_set_resolves_once_below_the_limit(plan):
    """The activity filter runs inside the ranking CTE, resolved ONCE.

    A `CTE Scan on active_brokers` below the Limit is the signature of the MATERIALIZED
    form. If the CTE inlines instead, `brokers` appears directly under the join and the
    planner probes it once per candidate row — measured 3,942 loops / 11,826 blocks on a
    single region chip, i.e. the nested loop this wave exists to delete.

    Deliberately NOT asserted: which access method reaches `brokers`. A sequential scan of
    1,776 pages is the CORRECT plan here — an index-only scan on this table pays heavy heap
    fetches because `brokers` is only ~39% all-visible (a rollup updates every active broker
    every 10 minutes), which is why the originally-designed partial index was measured and
    dropped.

    RED by: deleting `as materialized` from the CTE.
    """
    limit = _find_limit(plan)
    below = list(_nodes(limit))
    assert any(n["Node Type"] == "CTE Scan" and n.get("CTE Name") == "active_brokers"
               for n in below), (
        "no `CTE Scan on active_brokers` below the Limit — the active set is not being "
        "resolved once:\n" + json.dumps(limit, indent=2)
    )
