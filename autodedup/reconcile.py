"""A9 — the real-time lane's reconcile: the live stream's groups onto production, in the pass.

The worker's `autodedup` lane decides, groups AND merges in one pass under one lease
(`autodedup.rt_lease`): after the pass's transaction commits, `run` takes the groups it
re-clustered plus a slice swept past the `rt_reconcile` cursor and hands them to THE apply
path — `apply.plan_groups` (every refusal E903 names: operator negatives, categories, the scope,
carry-along, spans-groups, refused-before, restored-elsewhere) and `apply.apply_group`
(`recheck_group` over locked rows, `merge_property_set(source='autodedup')` and the ledger row
in one transaction). Nothing here decides a refusal of its own; the reconcile is the batch
apply's brain run by the lane.

What it adds is WHICH groups and WHEN:
  * a group waits while any of its adverts sits in a scope block the lane has not fully read
    (a snapshot row with no fingerprint), because a partly read block can be missing a member;
  * a group already on one property is a no-op, and one an operator negative now covers is
    reported (`ruled_different_after_merge`), never undone — the reconcile NEVER splits
    (Decision 9): a grouping the stream no longer supports is a proposal;
  * a skipped or refused row is filed only when a member set's outcome CHANGES — at plan time
    and at apply time alike (an asset-link conflict surfaces only at the merge) — so a group
    waiting on the same rule files one row, not one a minute;
  * a group that failed `QUARANTINE_AFTER` passes running on an error nothing names is
    QUARANTINED (reported, not attempted) until `QUARANTINE_RETRY_H` after its last failure; an
    error inside one group never escapes the pass, and `MAX_ERRORS` of them stop the reconcile;
  * each group's transaction runs under its own local statement and lock timeouts;
  * it spends what is left of the pass's time and stops BETWEEN groups, never inside one, and
    merges at most the scope row's `max_clusters_per_run` groups a pass (the rest wait for the
    next pass, which re-plans them from fresh state).

The rollout control is unchanged until W6: `app_settings.autodedup_apply_scope` must admit the
group, and the WHOLE row is re-read before every group (blocks, deal types) and handed to the
apply-time re-check, so narrowing it stops the lane's merges between two groups. The brake is
the lane interval (0 = stop), then `mode=unapply`.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from autodedup import apply as A
from autodedup import apply_sql as S
from autodedup.incremental_sql import (
    RT_CURSOR_READ_SQL,
    RT_CURSOR_SET_SQL,
    RT_LOCK_GUARD_SQL,
    RT_SCOPE_SCAN_SEEN_SQL,
    RT_STATEMENT_GUARD_SQL,
)
from toolkit.property_identity import merge_property_set

CURSOR: str = "rt_reconcile"
RUN_PREFIX: str = "rt:"
# How many of the generation's groups one pass sweeps past the cursor, besides the ones it
# re-clustered. The trial scope holds ~1,100 groups, so a full cycle is ~6 passes.
SWEEP_SLICE: int = 200
# A group is started only while this much of the pass's time is left: one group is a handful
# of statements and one merge, and the reconcile stops between groups, never inside one.
GROUP_MARGIN_S: float = 30.0
# Each group's own transaction is bounded LOCALLY (the pass's guards ended with its commit):
# no statement outlives the margin above, and a lock an operator's merge holds is waited on
# for seconds, not for the rest of the pass.
GROUP_STATEMENT_TIMEOUT_MS: int = 25_000
GROUP_LOCK_TIMEOUT_MS: int = 5_000
# A member set whose newest QUARANTINE_AFTER outcomes are all `failed` is not attempted again
# until QUARANTINE_RETRY_H after the last of them; MAX_ERRORS errors nothing names stop one
# reconcile (a dead connection fails every group alike).
QUARANTINE_AFTER: int = 3
QUARANTINE_RETRY_H: float = 24.0
MAX_ERRORS: int = 3
WAITING = "block_not_fully_read"
QUARANTINED = "quarantined"
SUMMARY_CAP: int = 50


def _rows(conn: Any, sql: str, params: Mapping[str, Any]) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, dict(params))
        return list(cur.fetchall())


def _exec(conn: Any, sql: str, params: Mapping[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(sql, dict(params))


def _cursor(conn: Any) -> int:
    for name, last_listing_id, _snapshot, _watermark in _rows(
            conn, RT_CURSOR_READ_SQL, {"names": [CURSOR]}):
        if name == CURSOR:
            return int(last_listing_id or 0)
    return 0


def read_scope(conn: Any) -> tuple[A.Scope | None, str | None]:
    """The operator's scope row, read NOW: the whole Scope when it admits a live merge, else
    None and why (E904: absent, malformed, or naming no deal types or no area is closed)."""
    try:
        scope = A.Scope(**A.scope_fields(A.read_scope_setting(conn)))
    except ValueError as exc:
        return None, str(exc)
    problems = scope.live_problems()
    return (None, "; ".join(problems)) if problems else (scope, None)


def _unread(conn: Any, generation: str, listing_ids: Sequence[int],
            blocks: Sequence[str]) -> set[int]:
    """The adverts whose scope block the lane has not fully read: a block never walked, or one
    whose snapshot still holds an advert with no fingerprint."""
    walked = {str(row[0]) for row in _rows(conn, RT_SCOPE_SCAN_SEEN_SQL,
                                           {"generation": generation})}
    unread = {str(row[0]) for row in _rows(conn, S.RC_UNREAD_BLOCKS_SQL,
                                           {"generation": generation})}
    unread |= {block for block in blocks if block not in walked}
    if not unread or not listing_ids:
        return set()
    return {int(lid) for lid, block in _rows(conn, S.RC_MEMBER_BLOCKS_SQL, {
        "generation": generation, "listing_ids": sorted(listing_ids)}) if block in unread}


def _history(conn: Any, generation: str, listing_ids: Sequence[int]
             ) -> dict[frozenset[int], list[tuple[str, str | None, Any]]]:
    """Per member set, its newest outcomes (newest first): (outcome, error, at)."""
    if not listing_ids:
        return {}
    out: dict[frozenset[int], list[tuple[str, str | None, Any]]] = {}
    for members, outcome, error, at in _rows(conn, S.RC_OUTCOME_HISTORY_SQL, {
            "generation": generation, "listing_ids": sorted(listing_ids),
            "depth": QUARANTINE_AFTER}):
        out.setdefault(frozenset(int(x) for x in members), []).append(
            (str(outcome), error, at))
    return out


def _quarantined(past: Sequence[tuple[str, str | None, Any]], now: datetime) -> bool:
    streak = list(past[:QUARANTINE_AFTER])
    if len(streak) < QUARANTINE_AFTER or any(outcome != "failed" for outcome, _e, _a in streak):
        return False
    at = streak[0][2]
    if not isinstance(at, datetime):
        return True
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return (now - at).total_seconds() < QUARANTINE_RETRY_H * 3600.0


def run(
    conn: Any,
    generation: str,
    touched: Sequence[int],
    *,
    run_id: str,
    deadline: float,
    blocks: Sequence[str] = (),
    merge: Callable[..., dict[str, Any]] = merge_property_set,
    clock: Callable[[], float] = time.perf_counter,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Reconcile the pass's groups onto production. Returns the readout the pass summary and
    the worker's heartbeat carry; writes only through `apply.apply_group` and the ledger."""
    scope, closed = read_scope(conn)
    if scope is None:
        return {"skipped": "scope_closed", "reason": closed}
    after = _cursor(conn)
    swept = [int(row[0]) for row in _rows(conn, S.RC_SWEEP_SQL, {
        "generation": generation, "after": after, "limit": SWEEP_SLICE})]
    # A short slice is the end of the cycle: the cursor wraps, the next pass starts again.
    _exec(conn, RT_CURSOR_SET_SQL, {
        "name": CURSOR,
        "last_listing_id": swept[-1] if len(swept) >= SWEEP_SLICE else 0})
    keys = sorted({int(k) for k in touched} | set(swept))
    counts: dict[str, Any] = {"groups": len(keys), "touched": len(set(touched)),
                              "swept": len(swept)}
    out: dict[str, Any] = {"run_id": run_id, "counts": counts, "applied": [],
                           "skipped_at_apply": [], "refused": [], "failed": [],
                           QUARANTINED: []}
    if not keys:
        return out
    cluster_rows = [dict(zip(("cluster_key", "size", "status", "min_edge_score",
                              "model_version", "feature_version"), row))
                    for row in _rows(conn, S.RC_CLUSTERS_SQL,
                                     {"generation": generation, "keys": keys})]
    members_by: dict[int, list[A.Member]] = {}
    for row in _rows(conn, S.RC_MEMBERS_SQL, {"generation": generation, "keys": keys}):
        members_by.setdefault(int(row[0]), []).append(A.Member.read(row[1:]))
    unread = _unread(conn, generation,
                     sorted({m.listing_id for ms in members_by.values() for m in ms}), blocks)
    if unread:
        waiting = {key for key, ms in members_by.items()
                   if any(m.listing_id in unread for m in ms)}
        counts[WAITING] = len(waiting)
        cluster_rows = [row for row in cluster_rows if int(row["cluster_key"]) not in waiting]

    def group_of(listing_ids: set[int]) -> dict[int, int]:
        return {int(lid): int(key) for lid, key in _rows(conn, S.RC_GROUP_OF_SQL, {
            "generation": generation, "listing_ids": sorted(listing_ids)})}

    # The scope row's run cap holds here too: at most `max_clusters_per_run` merges a pass;
    # the groups past it are re-planned by a later pass (touched again, or by the sweep).
    plan = A.plan_groups(conn, generation, cluster_rows, members_by, scope, group_of=group_of)
    counts.update(plan.counts)

    history = _history(conn, generation, sorted({lid for g in plan.groups
                                                 for lid in g.member_ids}))

    def last(group: A.GroupPlan) -> tuple[str, str | None] | None:
        past = history.get(frozenset(group.member_ids))
        return (past[0][0], past[0][1]) if past else None

    # A waiting or refused group files a row only when its outcome changed.
    changed = [g for g in plan.skipped if last(g) != ("skipped", g.reasons[0])]
    counts["skipped_rows_written"] = A.file_skipped(conn, run_id, generation, changed)
    out[A.RULED_AFTER_MERGE] = [A.ruled_brief(g) for g in plan.skipped
                                if g.ruled_after_merge][:SUMMARY_CAP]

    todo = plan.to_apply
    for key in ("applied", "skipped_at_apply", "refused", "failed", QUARANTINED):
        counts[key] = 0
    counts["listings_moved"] = 0
    errors = 0
    guards = ((RT_STATEMENT_GUARD_SQL, {"statement_timeout_ms": GROUP_STATEMENT_TIMEOUT_MS}),
              (RT_LOCK_GUARD_SQL, {"lock_timeout_ms": GROUP_LOCK_TIMEOUT_MS}))
    for index, group in enumerate(todo):
        if clock() >= deadline - GROUP_MARGIN_S:
            out["stopped"] = "the pass's time is spent"
            counts["not_attempted"] = len(todo) - index
            break
        # E39: the operator's WHOLE scope row, read fresh before every group, is the one the
        # group's re-check reads over its locked rows.
        scope, closed = read_scope(conn)
        if scope is None:
            out["stopped"] = f"app_settings.{A.SCOPE_SETTING} admits no live merge: {closed}"
            counts["not_attempted"] = len(todo) - index
            break
        past = history.get(frozenset(group.member_ids), [])
        if _quarantined(past, now()):
            counts[QUARANTINED] += 1
            if len(out[QUARANTINED]) < SUMMARY_CAP:
                out[QUARANTINED].append(A.group_brief(group, error=past[0][1]))
            continue
        try:
            outcome, brief = A.apply_group(conn, group, scope, run_id=run_id,
                                           generation=generation, merge=merge,
                                           last=last(group), guards=guards)
        except Exception as exc:  # noqa: BLE001 — recorded by apply_group, counted here
            errors += 1
            counts["failed"] += 1
            if len(out["failed"]) < SUMMARY_CAP:
                out["failed"].append(A.group_brief(group, error=f"{type(exc).__name__}: {exc}"))
            if errors >= MAX_ERRORS:
                out["stopped"] = f"{errors} errors nothing names; the last: {type(exc).__name__}"
                counts["not_attempted"] = len(todo) - index - 1
                break
            continue
        counts[outcome] += 1
        if outcome == "applied":
            counts["listings_moved"] += int(brief["listings_moved"])
        if len(out[outcome]) < SUMMARY_CAP:
            out[outcome].append(brief)
    return out
