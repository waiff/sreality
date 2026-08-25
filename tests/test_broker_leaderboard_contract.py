"""Offline contracts W4 must not break (migration 435).

Three things about the broker leaderboard are load-bearing, invisible in the normal test
suite, and would each fail only in production:

  * `api/outreach.py` calls the 8-parameter function with SEVEN positional arguments. That
    is legal only because migration 410 appended `p_firm_ids` LAST and WITH A DEFAULT. Any
    re-sign breaks that call at runtime with CI green — `tests/api/test_outreach_routes.py`
    mentions neither `leaderboard` nor `select_targets`.
  * `CREATE OR REPLACE` preserves a function's ACL; `DROP` + `CREATE` resets it to the
    default, which is `EXECUTE TO PUBLIC`. Since this function returns `primary_email` and
    `primary_phone`, a DROP+CREATE silently re-exposes broker PII to `anon` — undoing
    migration 299. The existing rule-4 gate scans for literal `grant` statements and cannot
    see a default-ACL restoration.
  * The active-broker set must be resolved ONCE, via `AS MATERIALIZED`. Written as an
    inlinable `IN (select ... from brokers)`, the planner probes `brokers` once per
    candidate row on every geo shape — 3,942 loops and 11,826 blocks on a single region
    chip — reproducing the exact nested loop this wave exists to delete. Nothing else in
    CI can see that: the rows returned are identical either way.

All offline text assertions — they run in the normal `pytest -q` lane. The plan-shape half
lives in `tests/test_broker_leaderboard_plan_shape.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

# Reused, not re-derived: both are dollar-quote aware, so a `$function$` body stays ONE
# statement. A naive regex has already matched prose inside a migration's own header
# comment in this build.
from tests.test_migration_rls_grants import _statements, _strip_comments

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS = _ROOT / "migrations"

_FN_MIGRATION = _MIGRATIONS / "435_broker_leaderboard_deferred_hydration.sql"
_OUTREACH = _ROOT / "api" / "outreach.py"

# Every migration at or above this number must keep the CREATE OR REPLACE discipline.
_MIN_REPLACE_ONLY = 435


def _sql(path: Path) -> str:
    return _strip_comments(path.read_text())


# --- the outreach contract -------------------------------------------------


def _leaderboard_call_args(source: str) -> str:
    """The text INSIDE `broker_leaderboard(` ... `)` in outreach's SQL literal.

    Counting `%s` across the whole statement gives 10 and proves nothing — 7 function
    arguments plus two campaign_id binds plus the LIMIT. Count the wrapper, not the name.
    """
    start = source.index("broker_leaderboard(")
    i = start + len("broker_leaderboard(")
    depth = 1
    while depth:
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
        i += 1
    return source[start + len("broker_leaderboard(") : i - 1]


def test_outreach_passes_seven_positional_args():
    """RED by: changing the call to 8 placeholders, or re-signing the function."""
    args = _leaderboard_call_args(_OUTREACH.read_text())
    assert args.count("%s") == 7, (
        f"api/outreach.py passes {args.count('%s')} arguments to broker_leaderboard, "
        f"expected 7: {args!r}"
    )


def test_every_function_parameter_carries_a_default():
    """The actual invariant that keeps a 7-argument call against 8 parameters legal.

    RED by: un-defaulting any parameter in 435.
    """
    body = _sql(_FN_MIGRATION)
    params = body[body.index("broker_leaderboard(") + len("broker_leaderboard(") : body.index(")\nreturns")]
    lines = [p.strip() for p in params.split(",\n") if p.strip()]
    assert len(lines) == 8, f"expected 8 parameters, found {len(lines)}: {lines}"
    missing = [p for p in lines if "default" not in p.lower()]
    assert not missing, (
        "every broker_leaderboard parameter must carry a DEFAULT — api/outreach.py:123 "
        f"passes only 7 of 8 positionally: {missing}"
    )


def test_outreach_filters_by_email_after_the_rpc_not_inside_it():
    """Top-N by metric first, email filter second — the ordering the campaign depends on.

    RED by: hoisting `primary_email IS NOT NULL` into the RPC (which would change which
    2,000 brokers are considered), or changing the 7th bound argument away from 2000.
    """
    source = _OUTREACH.read_text()
    call_end = source.index("broker_leaderboard(")
    email_filter = source.index("lb.primary_email IS NOT NULL")
    assert email_filter > call_end, (
        "the email filter must apply AFTER broker_leaderboard's top-N, not inside it"
    )
    assert "metric, 2000," in source, (
        "outreach must request the top 2000 from the RPC before filtering by email"
    )


# --- the grant posture -----------------------------------------------------


def test_no_migration_drops_the_leaderboard_function():
    """`DROP` + `CREATE` resets the ACL to EXECUTE TO PUBLIC — a PII re-exposure.

    RED by: changing 435 to DROP FUNCTION + CREATE FUNCTION.
    """
    offenders: list[str] = []
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        match = re.match(r"^(\d+)_", path.name)
        if not match or int(match.group(1)) < _MIN_REPLACE_ONLY:
            continue
        for stmt in _statements(path.read_text()):
            normalised = " ".join(stmt.split()).lower()
            if normalised.startswith("drop function") and "broker_leaderboard" in normalised:
                offenders.append(f"{path.name}: {normalised[:120]}")
    assert not offenders, (
        "DROP FUNCTION resets broker_leaderboard's ACL to the default EXECUTE TO PUBLIC, "
        "re-exposing primary_email/primary_phone to anon. Use CREATE OR REPLACE:\n"
        + "\n".join(offenders)
    )


def test_the_function_migration_replaces_and_reasserts_the_revokes():
    """RED by: dropping the three REVOKE statements from 435."""
    statements = [" ".join(s.split()).lower() for s in _statements(_FN_MIGRATION.read_text())]
    assert any(s.startswith("create or replace function") for s in statements), (
        "435 must use CREATE OR REPLACE FUNCTION"
    )
    revoked = {
        role
        for role in ("public", "anon", "authenticated")
        if any(s.startswith("revoke execute") and s.endswith(f"from {role}") for s in statements)
    }
    assert revoked == {"public", "anon", "authenticated"}, (
        f"435 must re-assert EXECUTE revokes for all three roles, got {sorted(revoked)}"
    )


# --- the tiebreaker --------------------------------------------------------


def test_both_order_by_clauses_carry_the_same_tiebreaker():
    """Once the LIMIT moves under the join, an unstable sort decides MEMBERSHIP.

    Measured: seven brokers tie at the default byt/prodej limit-100 boundary. The ranking
    CTE and the outer display ORDER BY must break that tie the SAME way, or membership and
    display order disagree with no error.

    RED by: deleting `, a.broker_id` from the `top` CTE, or flipping either to `desc`.
    """
    body = _sql(_FN_MIGRATION)
    fn = body[body.index("as $function$") : body.index("$function$;")]
    # The two ORDER BYs are the ranking one (inside `top`) and the display one.
    orders = re.findall(r"order by\s+case p_metric.*?end desc,\s*(\w+)\.broker_id(\s+desc)?",
                        fn, re.IGNORECASE | re.DOTALL)
    assert len(orders) == 2, (
        f"expected exactly 2 metric ORDER BY clauses each ending in a broker_id "
        f"tiebreaker, found {len(orders)}"
    )
    directions = {(d or "").strip().lower() for _, d in orders}
    assert directions == {""}, (
        "both tiebreakers must be ASCENDING and identical — mismatched directions make "
        f"membership and display order disagree: {orders}"
    )


def test_the_limit_lives_in_the_ranking_cte_not_after_the_join():
    """The whole point of W4: truncate before hydrating.

    RED by: moving `limit greatest(...)` back below the `join brokers`.
    """
    body = _sql(_FN_MIGRATION)
    fn = body[body.index("as $function$") : body.index("$function$;")]
    limit_at = fn.lower().index("limit greatest(1, least(p_limit, 2000))")
    hydration_at = fn.lower().index("join brokers b on b.id = t.broker_id")
    assert limit_at < hydration_at, (
        "the LIMIT must sit ABOVE the hydration join in the text (inside the `top` CTE) — "
        "a LIMIT in a subquery is the optimisation barrier that makes the plan shape "
        "structural rather than planner whim"
    )
    assert fn.lower().count("limit greatest(1, least(p_limit, 2000))") == 1, (
        "exactly one LIMIT clause, and it is migration 414's clamp character for character"
    )


def test_the_activity_filter_is_inside_the_aggregating_cte():
    """The blocking correctness rule: the doctrine moves invariants, not predicates.

    Leaving `status='active'` above the LIMIT lets a merged_away broker holding stats rows
    consume a top-N slot and then be discarded — an under-filled page. LIVE today: 5 such
    brokers exist, and 717 merged-away brokers carry a metric at or above the default cut.

    RED by: moving the semi-join out of `agg` into the final SELECT's WHERE.
    """
    body = _sql(_FN_MIGRATION)
    fn = body[body.index("as $function$") : body.index("$function$;")]
    semi_at = fn.lower().index("where b.status = 'active'")
    join_at = fn.lower().index("join active_brokers ab on ab.id = r.broker_id")
    limit_at = fn.lower().index("limit greatest(1, least(p_limit, 2000))")
    assert semi_at < join_at < limit_at, (
        "the active-broker set must be resolved and joined BEFORE the LIMIT — otherwise "
        "the page silently under-fills when a merged_away broker holds stats rows"
    )


def test_the_active_set_is_materialized():
    """`AS MATERIALIZED` is load-bearing, not style.

    Without it the CTE inlines and the planner probes `brokers` once per candidate row:
    measured 3,942 loops / 11,826 blocks on a single region chip, i.e. it reproduces the
    nested loop W4 exists to delete. With it, the active set resolves once (a sequential
    1,776-block scan) and hash-joins: 22,952 -> 2,479 blocks.

    RED by: deleting `as materialized`.
    """
    fn = _sql(_FN_MIGRATION)
    assert "active_brokers as materialized" in fn.lower(), (
        "the active-broker CTE lost `AS MATERIALIZED` — the planner will inline it and "
        "probe brokers once per candidate row, restoring the nested loop"
    )
