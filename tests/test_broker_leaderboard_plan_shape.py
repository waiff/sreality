"""The LIMIT must sit BELOW the hydration join (migration 435), in BOTH of the
leaderboard's branches (448, 469) — and each branch's inactive twin must be fully
pruned from the plan.

W4's whole claim is that the leaderboard ranks on cheap columns alone, truncates, and only
then joins `brokers` and `firms` to decorate at most `p_limit` rows. Measured before:
between 87% and 99.2% of every joined-and-decorated row was discarded by the LIMIT — 2,067
of the default shape's 3,140 blocks, and 20,355 of the region chip's 22,910.

448 added a second branch (reads `listings` directly); 469 widened its gate so a SUBTYPE
filter routes there too, making it the general "filters the matview cannot express" path.
It sits beside the unfiltered one (reads broker_region_type_stats), UNIONed in one `sql`
function so each stays plan-inlinable — see the migration's own header for why a `language
plpgsql` if/else was tried and reverted (it made the whole function an opaque "Function
Scan" to EXPLAIN, which would have made every test in this file vacuous). Both the
ranking-before-hydration property AND the "the other branch costs nothing" pruning property
need proving here, live, because both "type-check either way" — nothing else in CI can see a
regression in either.

Nothing else can see a regression here. The function returns identical rows either way (the
under-fill case is `tests/test_broker_leaderboard_live.py`'s subject); only the plan differs,
and only against a real schema.

`firms` is the unambiguous hydration marker — the function touches it for nothing else — so
"is `firms` inside the Limit's subtree" is exactly the question, asked on the parsed node
tree rather than by counting substrings in the EXPLAIN text. `broker_region_type_stats` and
`listings` are the unambiguous PER-BRANCH markers — each is read by exactly one branch — so
"does the OTHER branch's marker appear anywhere in this plan at all" is the pruning question,
asked the same way.

WHY `enable_seqscan = off`: CI's replayed tables are EMPTY, where a seq scan genuinely is
the cheapest plan, so asserting planner *preference* would fail for reasons unrelated to the
defect (the migration-429 precedent). What CI can prove is the STRUCTURE: a LIMIT inside a
subquery is a hard optimisation barrier, so `firms` cannot appear beneath it whatever the
costs say — and a WHERE clause that's provably false for the whole branch (`p_min_price_czk
is null` when the call passes a value, or `is not null` when it does not) prunes that
branch's relations out of the plan entirely, which is a presence/absence question, not a
cost comparison, so it holds on an empty replayed table too.

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


def _explain(conn, *, min_price_czk=None, subtypes=None, category_main="byt"):
    with conn.cursor() as cur:
        # Plain SET, not SET LOCAL: this connection is autocommit, and outside a
        # transaction SET LOCAL is a silent no-op (the trap migration 429's rail hit).
        cur.execute("set enable_seqscan = off")
        cur.execute(
            "explain (format json) select * from public.broker_leaderboard("
            "null, null, null, %s, 'prodej', 'active_property_count', 100, "
            "null, %s, false, %s, false)",
            (category_main, min_price_czk, subtypes),
        )
        return cur.fetchone()[0][0]["Plan"]


@pytest.fixture(scope="module")
def plan_unfiltered(conn):
    """The default, by-far-most-common call: no value filter. Should be the fast
    (broker_region_type_stats) branch, with `listings` nowhere in the plan."""
    return _explain(conn, min_price_czk=None)


@pytest.fixture(scope="module")
def plan_priced(conn):
    """A value-filtered call. Should be the live (`listings`) branch, with
    `broker_region_type_stats` nowhere in the plan."""
    return _explain(conn, min_price_czk=5_000_000)


@pytest.fixture(scope="module")
def plan_subtyped(conn):
    """A SUBTYPE-filtered call with NO price filter (migration 469). This is the shape
    that proves the live branch is gated on "any live-only filter", not on price
    specifically: it must reach `listings` and prune the matview exactly like the priced
    shape does. Before 469 widened the gate, this call would have fallen through to the
    matview — which cannot express subtype at all, so it would have answered with
    silently UNFILTERED counts."""
    return _explain(conn, subtypes=["kancelar", "sklad"], category_main="komercni")


def _nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def _find_limit(plan):
    for node in _nodes(plan):
        if node["Node Type"] == "Limit":
            return node
    return None


def _relation_names(plan):
    return {n.get("Relation Name") for n in _nodes(plan) if n.get("Relation Name")}


# --- shape shared by both branches: rank first, hydrate at most p_limit rows ------


def test_the_unfiltered_plan_has_a_limit_node(plan_unfiltered):
    assert _find_limit(plan_unfiltered) is not None, (
        "no Limit node in the unfiltered leaderboard plan:\n"
        + json.dumps(plan_unfiltered, indent=2)
    )


def test_the_priced_plan_has_a_limit_node(plan_priced):
    assert _find_limit(plan_priced) is not None, (
        "no Limit node in the price-filtered leaderboard plan:\n"
        + json.dumps(plan_priced, indent=2)
    )


def test_firms_is_not_touched_below_the_limit_unfiltered(plan_unfiltered):
    """RED by: restoring migration 414's body, where `brokers_public` (which embeds the
    `firms` LEFT JOIN) is joined to the whole candidate set before the LIMIT."""
    limit = _find_limit(plan_unfiltered)
    below = _relation_names(limit)
    assert "firms" not in below, (
        "`firms` is scanned BELOW the Limit in the unfiltered plan — the hydration join "
        f"is running against the whole candidate set again: {below}\n"
        + json.dumps(limit, indent=2)
    )


def test_firms_is_not_touched_below_the_limit_priced(plan_priced):
    """The same guarantee, for the new (448) live branch — nothing structural exempts
    a price-filtered call from the ranking-then-hydrate rule."""
    limit = _find_limit(plan_priced)
    below = _relation_names(limit)
    assert "firms" not in below, (
        "`firms` is scanned BELOW the Limit in the price-filtered plan — the hydration "
        f"join is running against the whole candidate set again: {below}\n"
        + json.dumps(limit, indent=2)
    )


def test_firms_is_touched_above_the_limit_unfiltered(plan_unfiltered):
    """The mirror assertion: hydration must still happen. Without this, a rewrite that
    simply stopped returning firm columns would pass the test above for the wrong
    reason."""
    limit = _find_limit(plan_unfiltered)
    below_ids = {id(n) for n in _nodes(limit)}
    above = {
        n.get("Relation Name") for n in _nodes(plan_unfiltered) if id(n) not in below_ids
    }
    assert "firms" in above, (
        f"`firms` is not joined above the Limit in the unfiltered plan — hydration is "
        f"missing entirely: {above}"
    )


def test_firms_is_touched_above_the_limit_priced(plan_priced):
    limit = _find_limit(plan_priced)
    below_ids = {id(n) for n in _nodes(limit)}
    above = {n.get("Relation Name") for n in _nodes(plan_priced) if id(n) not in below_ids}
    assert "firms" in above, (
        f"`firms` is not joined above the Limit in the price-filtered plan — hydration "
        f"is missing entirely: {above}"
    )


def test_the_active_set_resolves_once_below_the_limit_unfiltered(plan_unfiltered):
    """The activity filter runs inside the ranking CTE, resolved ONCE.

    A `CTE Scan on active_brokers` below the Limit is the signature of the MATERIALIZED
    form. If the CTE inlines instead, `brokers` appears directly under the join and the
    planner probes it once per candidate row — measured 3,942 loops / 11,826 blocks on a
    single region chip, i.e. the nested loop this wave exists to delete.

    Deliberately NOT asserted: which access method reaches `brokers`. A sequential scan
    of 1,776 pages is the CORRECT plan here — an index-only scan on this table pays
    heavy heap fetches because `brokers` is only ~39% all-visible (a rollup updates
    every active broker every 10 minutes), which is why the originally-designed partial
    index was measured and dropped.

    RED by: deleting `as materialized` from the (shared, migration 448) `active_brokers`
    CTE.
    """
    limit = _find_limit(plan_unfiltered)
    below = list(_nodes(limit))
    assert any(n["Node Type"] == "CTE Scan" and n.get("CTE Name") == "active_brokers"
               for n in below), (
        "no `CTE Scan on active_brokers` below the Limit in the unfiltered plan — the "
        "active set is not being resolved once:\n" + json.dumps(limit, indent=2)
    )


def test_the_active_set_resolves_once_below_the_limit_priced(plan_priced):
    limit = _find_limit(plan_priced)
    below = list(_nodes(limit))
    assert any(n["Node Type"] == "CTE Scan" and n.get("CTE Name") == "active_brokers"
               for n in below), (
        "no `CTE Scan on active_brokers` below the Limit in the price-filtered plan — "
        "the active set is not being resolved once:\n" + json.dumps(limit, indent=2)
    )


# --- 448's specific claim: the inactive branch costs nothing ---------------------


def test_unfiltered_call_never_touches_listings(plan_unfiltered):
    """The whole point of gating each branch's base CTE on `p_min_price_czk is null` /
    `is not null` rather than a runtime plpgsql if/else: Postgres's planner PRUNES the
    branch whose gate is provably false, so the default (by far most common) call never
    pays for a scan of `listings` at all — confirmed live via PREPARE/EXECUTE with a
    real bound NULL parameter before this migration shipped.

    RED by: a rewrite that removes the `p_min_price_czk is null` guard from any of the
    fast branch's `broker_region_type_stats` arms (which would stop the planner from
    being able to prove the live branch is dead), or that merges the two branches back
    into a single ungated UNION.
    """
    names = _relation_names(plan_unfiltered)
    assert "listings" not in names, (
        f"`listings` appears in the unfiltered (min_price_czk IS NULL) plan — the live "
        f"branch was not pruned, so every unfiltered call now also pays to scan "
        f"listings: {names}\n" + json.dumps(plan_unfiltered, indent=2)
    )


def test_priced_call_never_touches_broker_region_type_stats(plan_priced):
    """The mirror of the test above: a value-filtered call must not ALSO pay to scan
    the matview — the two branches are alternatives, not additive.

    RED by: the same class of rewrite as above, in the other direction (losing the
    `p_min_price_czk is null` guard on any fast-branch arm).
    """
    names = _relation_names(plan_priced)
    assert "broker_region_type_stats" not in names, (
        f"`broker_region_type_stats` appears in the price-filtered plan — the fast "
        f"branch was not pruned, so every value-filtered call now also pays to scan "
        f"the matview: {names}\n" + json.dumps(plan_priced, indent=2)
    )


def test_priced_call_does_touch_listings(plan_priced):
    """The mirror of test_unfiltered_call_never_touches_listings — without this, a
    rewrite that broke the live branch so badly it always returned zero rows (e.g. an
    always-false predicate) would pass every other test in this file for the wrong
    reason."""
    assert "listings" in _relation_names(plan_priced), (
        "`listings` is missing from the price-filtered plan — the live branch is not "
        "running at all:\n" + json.dumps(plan_priced, indent=2)
    )


# --- migration 469: subtype routes to the same live branch, on its own ------------


def test_subtype_only_call_reaches_listings_and_prunes_the_matview(plan_subtyped):
    """The whole point of 469's gate widening, in one assertion pair: a subtype filter
    with NO price filter must behave exactly like a price filter does — reach `listings`
    and drop `broker_region_type_stats` from the plan.

    RED by: gating the live branch on `p_min_price_czk is not null` alone (the pre-469
    spelling), which sends this call to the matview — a table with no subtype column,
    which would therefore answer it with unfiltered counts and no error anywhere.
    """
    names = _relation_names(plan_subtyped)
    assert "listings" in names, (
        "`listings` is missing from the subtype-only plan — the live branch is not "
        "running, so the filter is being answered by the matview, which cannot express "
        f"subtype at all: {names}\n" + json.dumps(plan_subtyped, indent=2)
    )
    assert "broker_region_type_stats" not in names, (
        "`broker_region_type_stats` appears in the subtype-only plan — the fast branch "
        f"was not pruned, so both branches are being paid for: {names}\n"
        + json.dumps(plan_subtyped, indent=2)
    )


def test_the_subtype_only_plan_keeps_deferred_hydration(plan_subtyped):
    """Every structural guarantee the other two shapes get, the subtype shape gets too —
    a filter reaching the live branch by a different gate must not reach a different
    plan shape."""
    limit = _find_limit(plan_subtyped)
    assert limit is not None, (
        "no Limit node in the subtype-only plan:\n" + json.dumps(plan_subtyped, indent=2)
    )
    assert "firms" not in _relation_names(limit), (
        "`firms` is scanned BELOW the Limit in the subtype-only plan — hydration is "
        "running against the whole candidate set"
    )
    below_ids = {id(n) for n in _nodes(limit)}
    above = {n.get("Relation Name") for n in _nodes(plan_subtyped) if id(n) not in below_ids}
    assert "firms" in above, (
        f"`firms` is not joined above the Limit — hydration is missing entirely: {above}"
    )
