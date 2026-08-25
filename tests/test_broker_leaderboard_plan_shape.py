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


def test_brokers_is_reached_by_the_partial_index_below_the_limit(plan):
    """The activity semi-join runs inside the ranking CTE, on migration 434's index.

    RED by: dropping `brokers_active_id_idx`, or moving the semi-join above the LIMIT.
    """
    limit = _find_limit(plan)
    broker_nodes = [n for n in _nodes(limit) if n.get("Relation Name") == "brokers"]
    assert broker_nodes, (
        "`brokers` is not reached below the Limit — the activity semi-join is not inside "
        "the ranking CTE, which is the under-fill defect:\n" + json.dumps(limit, indent=2)
    )
    assert not any(n["Node Type"] == "Seq Scan" for n in broker_nodes), (
        "the activity semi-join seq-scans brokers (1,776 blocks) — migration 434's "
        f"brokers_active_id_idx is missing: {[n['Node Type'] for n in broker_nodes]}"
    )
