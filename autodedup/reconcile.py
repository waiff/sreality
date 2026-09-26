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
  * a skipped row is filed only when a member set's first reason CHANGES, so a group waiting
    on the same rule files one row, not one a minute;
  * it spends what is left of the pass's time and stops BETWEEN groups, never inside one.

The rollout control is unchanged until W6: `app_settings.autodedup_apply_scope` must admit the
group, and it is re-read before every group, so emptying its blocks stops the lane's merges
between two groups. The brake is the lane interval (0 = stop), then `mode=unapply`.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Mapping, Sequence

from autodedup import apply as A
from autodedup import apply_sql as S
from autodedup.incremental_sql import (
    RT_CURSOR_READ_SQL,
    RT_CURSOR_SET_SQL,
    RT_SCOPE_SCAN_SEEN_SQL,
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
WAITING = "block_not_fully_read"
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


def _last_outcomes(conn: Any, generation: str, listing_ids: Sequence[int]
                   ) -> dict[frozenset[int], tuple[str, str | None]]:
    if not listing_ids:
        return {}
    return {frozenset(int(x) for x in members): (str(outcome), error)
            for members, outcome, error in _rows(conn, S.RC_LAST_OUTCOME_SQL, {
                "generation": generation, "listing_ids": sorted(listing_ids)})}


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
) -> dict[str, Any]:
    """Reconcile the pass's groups onto production. Returns the readout the pass summary and
    the worker's heartbeat carry; writes only through `apply.apply_group` and the ledger."""
    closed = A.scope_closed(conn)
    if closed:
        return {"skipped": "scope_closed", "reason": closed}
    scope = A.Scope(**A.scope_fields(A.read_scope_setting(conn)))
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
                           "skipped_at_apply": [], "refused": [], "failed": []}
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

    plan = A.plan_groups(conn, generation, cluster_rows, members_by, scope,
                         group_of=group_of, cap=False)
    counts.update({key: value for key, value in plan.counts.items()
                   if key not in ("deferred_run_cap",)})

    # A waiting or refused group files a row only when its FIRST reason changed.
    last = _last_outcomes(conn, generation,
                          sorted({lid for g in plan.skipped for lid in g.member_ids}))
    changed = [g for g in plan.skipped
               if last.get(frozenset(g.member_ids)) != ("skipped", g.reasons[0])]
    counts["skipped_rows_written"] = len(changed)
    if changed:
        with conn.transaction():
            A._exec_many(conn, S.LEDGER_INSERT_SQL, [
                row for g in changed for row in A._rows_for(
                    run_id, generation, g, dry_run=False, outcome="skipped",
                    error=g.reasons[0])])
    out[A.RULED_AFTER_MERGE] = [A._ruled_brief(g) for g in plan.skipped
                                if g.ruled_after_merge][:SUMMARY_CAP]

    todo = plan.to_apply
    for key in ("applied", "skipped_at_apply", "refused", "failed"):
        counts[key] = 0
    counts["listings_moved"] = 0
    for index, group in enumerate(todo):
        if clock() >= deadline - GROUP_MARGIN_S:
            out["stopped"] = "the pass's time is spent"
            counts["not_attempted"] = len(todo) - index
            break
        # E39: the operator's scope row, read fresh before every group.
        closed = A.scope_closed(conn)
        if closed:
            out["stopped"] = f"app_settings.{A.SCOPE_SETTING} admits no live merge: {closed}"
            counts["not_attempted"] = len(todo) - index
            break
        outcome, brief = A.apply_group(conn, group, scope, run_id=run_id,
                                       generation=generation, merge=merge)
        counts[outcome] += 1
        if outcome == "applied":
            counts["listings_moved"] += int(brief["listings_moved"])
        if len(out[outcome]) < SUMMARY_CAP:
            out[outcome].append(brief)
    return out
