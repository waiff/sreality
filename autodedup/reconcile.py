"""A9 — the real-time lane's reconcile: the live stream's groups onto production, in the pass.

The worker's `autodedup` lane decides, groups AND merges in one pass under one lease
(`autodedup.rt_lease`): after the pass's transaction commits, `run` takes the groups it
re-clustered plus a slice swept past the `rt_reconcile` cursor and hands them to THE apply
path — `apply.plan_groups` (every refusal E903 names: operator negatives, categories, the scope,
carry-along, spans-groups, refused-before) and `apply.apply_group`
(`recheck_group` over locked rows, `merge_property_set(source='autodedup')` and the ledger row
in one transaction). Nothing here decides a refusal of its own; the reconcile is the batch
apply's brain run by the lane.

What it adds is WHICH groups and WHEN:
  * a group waits while any advert its merge would move — a member, or any advert of a property
    a member sits on — sits in a scope block the lane has not fully read (never walked, inside
    its first page cycle, or a snapshot row with no fingerprint), because a partly read block
    can be missing a member; an advert the snapshot does not hold yet is placed by its live
    location, and during a build one no scope block holds waits too (E964);
  * a group already on one property is a no-op, and one an operator negative now covers is
    reported (`ruled_different_after_merge`), never undone — the reconcile NEVER splits
    (Decision 9): a grouping the stream no longer supports is a proposal;
  * a skipped or refused row is filed only when a member set's outcome CHANGES — at plan time
    and at apply time alike — so a group
    waiting on the same rule files one row, not one a minute;
  * a group that failed `QUARANTINE_AFTER` passes running on an error nothing names is
    QUARANTINED (reported, not attempted) until `QUARANTINE_RETRY_H` after its last failure; an
    error inside one group never escapes the pass, and `MAX_ERRORS` of them stop the reconcile;
  * each group's transaction runs under its own local statement and lock timeouts;
  * it spends what is left of the pass's time and stops BETWEEN groups, never inside one, and
    merges at most the scope row's `max_clusters_per_run` groups a pass (the rest wait for the
    next pass, which re-plans them from fresh state);
  * a group waits (`stale_decision`) while a member not yet on its property was last refreshed
    more than `FRESH_DECISION_HOURS` ago, under another engine, or never (E960): a pass reads a
    neighbour as its row stored it, so a stored merge can rest on facts a sighting, a location
    correction or a reparse has since moved. Every group owed a merge and not merged this pass
    (waiting, past the run cap, out of time) is handed on (`rt_redecide`), oldest moved member
    first; the next pass re-claims whole groups of it, at least `STALE_FLOOR` adverts, and the
    reconcile re-plans every group handed on, so a group refreshed whole merges at once.

The rollout control is unchanged until W6: `app_settings.autodedup_apply_scope` must admit the
group, and the WHOLE row is re-read before every group (blocks, deal types) and handed to the
apply-time re-check, so narrowing it stops the lane's merges between two groups. The brake is
the lane interval (0 = stop), then `mode=unapply`.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from autodedup import apply as A
from autodedup import apply_sql as S
from autodedup import rt_lease
from autodedup.incremental_sql import (
    RT_KNOWN_SQL,
    RT_SETTING_WRITE_SQL,
    RT_STALE_SQL,
    RT_CURSOR_READ_SQL,
    RT_CURSOR_SET_SQL,
    RT_LOCK_GUARD_SQL,
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
# E960: a merge is applied only on facts at most this old. Half the sighting cycle (the walks
# see an active advert about once a day: 12,920 of 13,281 active within the store's first day),
# so an applied decision's clock features are off by at most half a day; a heal, a correction
# or a population move waits at most this long; and the groups a pass applies are mostly the
# ones it just re-clustered around a refreshed member, so the rule costs a pass of latency only
# where a member's facts are older.
# A policy window, for the operator to rule on.
FRESH_DECISION_HOURS: float = 12.0
STALE = "stale_decision"
REDECIDE_SETTING: str = "rt_redecide"
# The adverts the handed-on groups may name in all, whole groups, oldest wait first.
REDECIDE_CAP: int = 5000
# The adverts a pass re-claims for waiting groups at the least, whatever its time-bound share: a
# re-claim costs an advert's pairs (~36 decided at 39.7k), and a share of 1 (a pass of 9 adverts)
# would leave a backlog of thousands for days. A share is at most 500 // 5, so this is the
# arm's budget.
STALE_FLOOR: int = 100
# A group the lane tried to re-claim is not tried again for this long, whatever came of it: a
# re-claim the pair budget refused or the deadline cut must not hold the head of the queue.
TRIED_SETTING: str = "rt_redecide_tried"
TRIED_BACKOFF_HOURS: float = 6.0
# The re-claim's own budget, kept like the rate (E941): `{budget, inflight}`, the count a pass
# re-claims written AHEAD of it; a pass that finds a count still in flight (the last pass that
# carried re-claims died: its deadline, a kill, a raise) offers none and halves the budget, and
# a pass that commits with re-claims restores `STALE_FLOOR`.
BUDGET_SETTING: str = "rt_redecide_budget"
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


def _unread(conn: Any, generation: str, adverts: Sequence[A.Member],
            blocks: Sequence[str], *, building: bool = False,
            unread_blocks: set[str] | None = None, riders: Iterable[int] = (),
            scope: A.Scope | None = None) -> set[int]:
    """The adverts among `adverts` whose scope block the lane has not fully read — a block never
    walked, inside its first cycle of pages, or whose snapshot still holds an advert with no
    fingerprint. An advert's block is the snapshot's; one the snapshot does not hold yet (a
    forward-feed arrival its block's walk has not reached, E964 review F3) is placed by its LIVE
    location in the scope block that holds it, and during a build one that no scope block holds
    either reads as unread (fail closed). `unread_blocks` is `apply.rt_unread_blocks`' answer when
    the caller has it.

    `riders` are the adverts a merge would CARRY along, not members (E964 review R2): one this
    generation has never fingerprinted is unread whatever its block's state — it may have moved
    into a block after that block's last walk — when a scope block holds it or the apply `scope`
    admits it (an apply-scope block outside `rt_scope` lets in no advert the lane never read);
    one neither holds is the plan's refusal (`carries_out_of_scope`)."""
    unread = (A.rt_unread_blocks(conn, generation, blocks) if unread_blocks is None
              else unread_blocks)
    riders = {int(i) for i in riders}
    if not adverts or not (unread or building or riders):
        return set()
    placed: dict[int, set[str]] = {}
    for lid, block in _rows(conn, S.RC_MEMBER_BLOCKS_SQL, {
            "generation": generation,
            "listing_ids": sorted({advert.listing_id for advert in adverts})}):
        placed.setdefault(int(lid), set()).add(str(block))
    known = ({int(row[0]) for row in _rows(conn, RT_KNOWN_SQL, {
        "generation": generation, "ids": sorted(riders)})} if riders else set())
    keys = set(blocks)
    out: set[int] = set()
    for advert in adverts:
        held = placed.get(advert.listing_id) or (keys & {
            f"obec:{advert.obec_kod}", f"cast_obce:{advert.cast_obce_kod}",
            f"okres:{advert.okres_kod}"})
        if advert.listing_id in riders and advert.listing_id not in known and (
                held or (scope is not None and scope.outside(advert) is None)):
            out.add(advert.listing_id)
        elif (held & unread) if held else building:
            out.add(advert.listing_id)
    return out


def _carried(conn: Any, members_by: Mapping[int, Sequence[A.Member]]
             ) -> dict[int, list[A.Member]]:
    """Every advert of every property a group's members sit on, by property (E964 review F2):
    a merge moves them all, so an unread one holds the group back as an unread member does."""
    out: dict[int, list[A.Member]] = {}
    for chunk in A._chunks({m.property_id for ms in members_by.values() for m in ms
                            if m.property_id is not None}):
        for row in _rows(conn, S.PROPERTY_LISTINGS_SQL, {"property_ids": chunk}):
            advert = A.Member.read(row)
            out.setdefault(int(row[1]), []).append(advert)
    return out


def redecide_key(generation: str) -> str:
    """`rt_redecide:<generation>` — the members the reconcile hands the next pass (E960)."""
    return f"{REDECIDE_SETTING}:{generation}"


def budget_key(generation: str) -> str:
    """`rt_redecide_budget:<generation>` — the re-claim's budget and its count in flight."""
    return f"{BUDGET_SETTING}:{generation}"


def tried_key(generation: str) -> str:
    """`rt_redecide_tried:<generation>` — {a group's first advert: when the lane tried it}."""
    return f"{TRIED_SETTING}:{generation}"


def refreshed(conn: Any, generation: str, listing_ids: Sequence[int], *, fresh_hours: float,
              engine_since: str | None) -> dict[int, tuple[Any, bool]]:
    """Each advert's last refresh and whether a merge may NOT rest on it (E960): refreshed longer
    ago than `fresh_hours`, before `engine_since`, or never (no row: fail closed)."""
    if not listing_ids:
        return {}
    return {int(row[0]): (row[1], bool(row[2])) for row in _rows(conn, RT_STALE_SQL, {
        "generation": generation, "listing_ids": sorted({int(i) for i in listing_ids}),
        "fresh_s": float(fresh_hours) * 3600.0, "engine_since": engine_since})}


def stale_members(conn: Any, generation: str, listing_ids: Sequence[int], *,
                  fresh_hours: float, engine_since: str | None) -> set[int]:
    """The adverts among `listing_ids` a merge may not rest on (E960)."""
    return {listing_id for listing_id, (_at, stale) in refreshed(
        conn, generation, listing_ids, fresh_hours=fresh_hours,
        engine_since=engine_since).items() if stale}


def _oldest(members: Sequence[int], ages: Mapping[int, tuple[Any, bool]]) -> tuple[int, Any]:
    """A group's place in the queue: the oldest refresh first; a group holding an advert with no
    row (no claim can refresh it) last."""
    stamps = [ages[i][0] for i in members if i in ages]
    if not stamps or any(at is None for at in stamps):
        return (2, "")
    return (1, min(stamps))


def claims(groups: Sequence[Sequence[int]], ages: Mapping[int, tuple[Any, bool]],
           budget: int) -> list[int]:
    """The adverts a pass re-claims for the groups handed on (E960): WHOLE groups — every stale
    member a claim can refresh (it has a row) — oldest wait first, until `budget` adverts; the
    first group is taken whatever its size, so no group is ever cut across passes."""
    queue = []
    for members in groups:
        stale = sorted({int(i) for i in members if int(i) in ages and ages[int(i)][1]
                        and ages[int(i)][0] is not None})
        if stale:
            queue.append((_oldest(stale, ages), stale))
    queue.sort()
    out: list[int] = []
    for _rank, stale in queue:
        if out and len(out) + len(stale) > budget:
            break
        out.extend(i for i in stale if i not in out)
    return out


def select_claims(groups: Sequence[Sequence[int]], ages: Mapping[int, tuple[Any, bool]],
                  tried: Mapping[str, str], now: datetime, budget: int = STALE_FLOOR
                  ) -> tuple[list[int], dict[str, str]]:
    """The adverts the next pass re-claims (`claims` at `budget`) over the groups not tried
    within `TRIED_BACKOFF_HOURS`, and the back-off map with those groups stamped `now` and the
    expired or forgotten stamps dropped (E960). Keyed by a group's first advert."""
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    cutoff = now.timestamp() - TRIED_BACKOFF_HOURS * 3600.0
    queued = {str(min(group)) for group in groups if group}
    recent = {head: at for head, at in tried.items()
              if head in queued and _epoch(at) is not None and _epoch(at) > cutoff}
    open_groups = [group for group in groups if group and str(min(group)) not in recent]
    taken = claims(open_groups, ages, budget)
    chosen = set(taken)
    for group in open_groups:
        if chosen & set(group):
            recent[str(min(group))] = now.isoformat()
    return taken, recent


def _epoch(value: Any) -> float | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()


def handed_groups(value: Any) -> list[list[int]]:
    """`rt_redecide:<generation>` as written: lists of advert ids."""
    if not isinstance(value, list):
        return []
    return [[int(i) for i in group] for group in value
            if isinstance(group, list) and all(isinstance(i, int) for i in group)]


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
    fence: Callable[[Any], None] | None = None,
    fresh_hours: float = FRESH_DECISION_HOURS,
    engine_since: str | None = None,
    waiting: Sequence[Sequence[int]] = (),
    building: bool = False,
) -> dict[str, Any]:
    """Reconcile the pass's groups onto production. Returns the readout the pass summary and
    the worker's heartbeat carry; writes only through `apply.apply_group` and the ledger.

    `building` says the generation is in its build phase (E964): an advert the snapshot does
    not place and no scope block holds by its live location then holds its group back.

    `fence` (E941) is the lane's lease check, run last inside every group's transaction: a
    lease that ended under the reconcile — the worker's shutdown, a `release_lease=` dispatch —
    rolls that group back and stops the run (`stopped` = `lease_lost`)."""
    scope, closed = read_scope(conn)
    if scope is None:
        # E960: no merge can follow a re-claim, so the queue goes too (no pass re-claims it).
        if waiting:
            _hand_on(conn, generation, run_id, [], {})
        return {"skipped": "scope_closed", "reason": closed}
    after = _cursor(conn)
    swept = [int(row[0]) for row in _rows(conn, S.RC_SWEEP_SQL, {
        "generation": generation, "after": after, "limit": SWEEP_SLICE})]
    # A short slice is the end of the cycle: the cursor wraps, the next pass starts again.
    _exec(conn, RT_CURSOR_SET_SQL, {
        "name": CURSOR,
        "last_listing_id": swept[-1] if len(swept) >= SWEEP_SLICE else 0})
    def group_of(listing_ids: set[int]) -> dict[int, int]:
        return {int(lid): int(key) for lid, key in _rows(conn, S.RC_GROUP_OF_SQL, {
            "generation": generation, "listing_ids": sorted(listing_ids)})}

    # E960: every group handed on is re-planned, so one refreshed whole merges in this pass.
    handed = (set(group_of({int(i) for group in waiting for i in group}).values())
              if waiting else set())
    keys = sorted({int(k) for k in touched} | set(swept) | handed)
    counts: dict[str, Any] = {"groups": len(keys), "touched": len(set(touched)),
                              "swept": len(swept), "handed_replanned": len(handed)}
    out: dict[str, Any] = {"run_id": run_id, "counts": counts, "applied": [],
                           "skipped_at_apply": [], "refused": [], "failed": [],
                           QUARANTINED: []}
    if not keys:
        if waiting:
            counts["handed_on"], counts["handed_on_dropped"] = _hand_on(
                conn, generation, run_id, [], {})
        return out
    cluster_rows = [dict(zip(("cluster_key", "size", "status", "min_edge_score",
                              "model_version", "feature_version"), row))
                    for row in _rows(conn, S.RC_CLUSTERS_SQL,
                                     {"generation": generation, "keys": keys})]
    members_by: dict[int, list[A.Member]] = {}
    for row in _rows(conn, S.RC_MEMBERS_SQL, {"generation": generation, "keys": keys}):
        members_by.setdefault(int(row[0]), []).append(A.Member.read(row[1:]))
    # A group waits while any advert a merge would move — a member, or any advert of a property
    # a member sits on (E964 review F2: an unbuilt advert carried along is compared with nothing)
    # — sits in a block the lane has not fully read, or is carried along and was never
    # fingerprinted (R2).
    unread_blocks = A.rt_unread_blocks(conn, generation, blocks)
    carried = _carried(conn, members_by)
    adverts = {m.listing_id: m for ms in members_by.values() for m in ms}
    riders = {c.listing_id for cs in carried.values() for c in cs} - set(adverts)
    adverts.update((c.listing_id, c) for cs in carried.values() for c in cs)
    unread = _unread(conn, generation, list(adverts.values()), blocks, building=building,
                     unread_blocks=unread_blocks, riders=riders, scope=scope)
    if unread:
        waiting = {key for key, ms in members_by.items()
                   if any(m.listing_id in unread
                          or any(c.listing_id in unread for c in carried.get(m.property_id, ()))
                          for m in ms)}
        counts[WAITING] = len(waiting)
        cluster_rows = [row for row in cluster_rows if int(row["cluster_key"]) not in waiting]

    # The scope row's run cap holds here too, at most `max_clusters_per_run` merges a pass, but
    # over the groups that may merge NOW (E960): it is applied below, after the freshness rule.
    plan = A.plan_groups(conn, generation, cluster_rows, members_by, scope, group_of=group_of,
                         cap=False)
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

    # E960: every member a merge would move must have been refreshed lately, under this engine;
    # the run cap then counts only the groups that may merge now.
    owed = {group.cluster_key: sorted(m.listing_id for m in members_by.get(group.cluster_key, ())
                                      if m.property_id != group.survivor_id)
            for group in plan.to_apply}
    ages = refreshed(conn, generation, sorted({i for ids in owed.values() for i in ids}),
                     fresh_hours=fresh_hours, engine_since=engine_since)
    fresh = [group for group in plan.to_apply
             if not any(ages.get(i, (None, True))[1] for i in owed[group.cluster_key])]
    counts[STALE] = len(plan.to_apply) - len(fresh)
    todo = fresh[:scope.max_clusters_per_run]
    counts["deferred_run_cap"] = len(fresh) - len(todo)
    counts["planned"] = len(todo)
    for key in ("applied", "skipped_at_apply", "refused", "failed", QUARANTINED):
        counts[key] = 0
    counts["listings_moved"] = 0
    attempted: set[int] = set()
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
        attempted.add(group.cluster_key)
        past = history.get(frozenset(group.member_ids), [])
        if _quarantined(past, now()):
            counts[QUARANTINED] += 1
            if len(out[QUARANTINED]) < SUMMARY_CAP:
                out[QUARANTINED].append(A.group_brief(group, error=past[0][1]))
            continue
        try:
            outcome, brief = A.apply_group(conn, group, scope, run_id=run_id,
                                           generation=generation, merge=merge,
                                           last=last(group), guards=guards, fence=fence)
        except rt_lease.LeaseLost:
            # The group rolled back and filed nothing; the lease is someone else's now.
            out["stopped"] = rt_lease.LEASE_LOST
            counts["not_attempted"] = len(todo) - index
            break
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
    counts["handed_on"], counts["handed_on_dropped"] = _hand_on(
        conn, generation, run_id,
        [owed[g.cluster_key] for g in plan.to_apply
         if g.cluster_key not in attempted and owed[g.cluster_key]], ages)
    return out


def _hand_on(conn: Any, generation: str, run_id: str, groups: Sequence[Sequence[int]],
             ages: Mapping[int, tuple[Any, bool]]) -> tuple[int, int]:
    """Every group owed a merge and not attempted (waiting, past the run cap, out of time), as its
    moved members, oldest wait first, whole groups up to `REDECIDE_CAP` adverts (E960). It
    REPLACES the list: every group on it was re-planned this pass, so what stays owed is here."""
    kept: list[list[int]] = []
    total = 0
    for _rank, members in sorted((_oldest(m, ages), list(m)) for m in groups):
        if total + len(members) > REDECIDE_CAP:
            break
        kept.append(members)
        total += len(members)
    _exec(conn, RT_SETTING_WRITE_SQL, {"key": redecide_key(generation),
                                       "value": json.dumps(kept), "updated_by": run_id})
    return len(kept), len(groups) - len(kept)
