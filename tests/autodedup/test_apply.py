"""A1 (PROGRAM.md E900-E906): engine groups into production merges, dark by default.

The fake below STORES what the apply path writes and serves it back, dispatching on the SQL
constant itself (the `fake_pg` idiom): a statement this file does not know raises. The set merge
and the per-advert detach it hands the apply path mimic the toolkit's contract — the oldest
record survives, one asset link is carried and two refuse (two linked units, too, for the
engine), re-point, soft-retire, refuse a category mismatch, move one advert back off the ledger
(the toolkit's own `_detach_plan` and `_origin_gone`; a dry run reads the real
`detach_outcomes` over the fake ledger) —
inside nested transactions that roll back on an exception, so a group's atomicity is
observable here.
"""

from __future__ import annotations

import copy
import json
import typing
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pytest

from autodedup import apply as A
from autodedup import apply_sql as S
from autodedup import lane
from toolkit import property_identity
from toolkit.property_identity import (
    AssetLinkConflict,
    MergeError,
    _detach_plan,
    _origin_gone,
    survivor_of,
)
from toolkit.room_taxonomy import category_main_compatible

GEN = "g12"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _Tx:
    def __init__(self, db: "FakeDb") -> None:
        self.db = db

    def __enter__(self) -> "_Tx":
        if self.db.depth == 0:
            self.db.now += timedelta(minutes=1)  # now(): the outermost transaction's start
        self.db.depth += 1
        self.saved = self.db.state()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.db.depth -= 1
        if exc_type is not None:
            self.db.restore(self.saved)
        return False


class _Cursor:
    def __init__(self, db: "FakeDb") -> None:
        self.db = db
        self.rows: list[tuple] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> None:
        self.db.statements.append(sql)
        self.rows = self.db.dispatch(sql, dict(params or {}))

    def executemany(self, sql: str, seq: Any) -> None:
        for params in seq:
            self.execute(sql, params)

    def fetchall(self) -> list[tuple]:
        return list(self.rows)


class FakeDb:
    def __init__(self) -> None:
        self.clusters: dict[tuple[str, int], dict[str, Any]] = {}
        self.members: list[tuple[str, int, int]] = []
        self.listings: dict[int, dict[str, Any]] = {}
        self.properties: dict[int, dict[str, Any]] = {}
        self.mnl: list[tuple[int, int, str]] = []
        self.verdicts: list[dict[str, Any]] = []
        self.settings: dict[str, Any] = {}
        self.ledger: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.statements: list[str] = []
        self.closed = False
        self.now = T0
        self.depth = 0

    # --- plumbing
    _STATE = ("listings", "properties", "ledger", "events", "settings")

    def state(self) -> dict[str, Any]:
        return {name: copy.deepcopy(getattr(self, name)) for name in self._STATE}

    def restore(self, saved: Mapping[str, Any]) -> None:
        for name, value in saved.items():
            setattr(self, name, value)

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Tx:
        return _Tx(self)

    def close(self) -> None:
        self.closed = True

    # --- builders
    def prop(self, pid: int, *, ct: str | None = "prodej", cm: str | None = "byt",
             first: datetime | None = T0, status: str = "active",
             asset: int | None = None) -> None:
        self.properties[pid] = {"status": status, "category_type": ct, "category_main": cm,
                                "first_seen_at": first, "merged_into": None,
                                "merged_at": None, "asset_id": asset}

    def listing(self, lid: int, pid: int | None, *, ct: str | None = "prodej",
                cm: str | None = "byt") -> None:
        self.listings[lid] = {"property_id": pid, "category_type": ct, "category_main": cm}

    def group(self, key: int, lids: list[int], *, gen: str = GEN, status: str = "proposed",
              block: tuple[str, int] | None = ("o", 563510), score: float | None = 0.99) -> None:
        self.clusters[(gen, key)] = {
            "size": len(lids), "status": status,
            "block_grain": block[0] if block else None, "block_key": block[1] if block else None,
            "category_main": "byt", "category_type": "prodej", "min_edge_score": score,
            "model_version": "m1", "feature_version": 9,
        }
        self.members.extend((gen, key, lid) for lid in lids)

    def live_scope(self, **extra: Any) -> None:
        self.settings[A.SCOPE_SETTING] = {"category_types": ["prodej"],
                                          "blocks": ["town:563510"], **extra}

    # --- the statements
    def dispatch(self, sql: str, p: dict[str, Any]) -> list[tuple]:  # noqa: C901
        if sql == S.SETTING_SQL:
            return [(self.settings[p["key"]],)] if p["key"] in self.settings else []
        if sql == S.CLUSTERS_SQL:
            return [(k, c["size"], c["status"], c["block_key"], c["block_grain"],
                     c["category_main"], c["category_type"], c["min_edge_score"],
                     c["model_version"], c["feature_version"])
                    for (g, k), c in sorted(self.clusters.items()) if g == p["generation"]]
        if sql == S.MEMBERS_SQL:
            out = []
            for g, k, lid in sorted(self.members):
                if g != p["generation"]:
                    continue
                row = self.listings.get(lid, {})
                out.append((k, lid, row.get("property_id"), row.get("category_type"),
                            row.get("category_main")))
            return out
        if sql == S.PROPERTIES_SQL:
            return [(pid, r["status"], r["category_type"], r["category_main"],
                     r["first_seen_at"])
                    for pid, r in sorted(self.properties.items()) if pid in p["property_ids"]]
        if sql == S.LOCK_PROPERTIES_SQL:
            return [(pid, r["status"], r["category_type"], r["category_main"])
                    for pid, r in sorted(self.properties.items()) if pid in p["property_ids"]]
        if sql == S.MEMBER_PROPERTIES_SQL:
            return [(lid, self.listings[lid]["property_id"]) for lid in p["listing_ids"]
                    if lid in self.listings]
        if sql in (S.PROPERTY_LISTINGS_SQL, S.LOCK_PROPERTY_LISTINGS_SQL):
            return [(r["property_id"], lid, r["category_type"], r["category_main"])
                    for lid, r in sorted(self.listings.items())
                    if r["property_id"] in p["property_ids"]]
        if sql == S.MUST_NOT_LINK_SQL:
            ids = set(p["listing_ids"])
            return [row for row in self.mnl if row[0] in ids and row[1] in ids]
        if sql == S.PAIR_VERDICTS_SQL:
            ids = set(p["listing_ids"])
            return [(v["lo"], v["hi"], v["verdict"]) for v in self.verdicts
                    if v["kind"] == "pair" and v["verdict"] in p["negatives"]
                    and v["lo"] in ids and v["hi"] in ids]
        if sql == S.CLUSTER_VERDICTS_SQL:
            ids = set(p["listing_ids"])
            return [(v["cluster_key"], v["verdict"], v.get("generation"), v.get("member_ids"),
                     v.get("decided_by", "op"), v.get("decided_at"), n + 1)
                    for n, v in enumerate(self.verdicts)
                    if v["kind"] == "cluster"
                    and ((v.get("member_ids") is not None and ids & set(v["member_ids"]))
                         or (v.get("member_ids") is None
                             and v["verdict"] in p["negatives"]
                             and v["cluster_key"] in p["cluster_keys"]))]
        if sql == S.LEDGER_HISTORY_SQL:
            return [(r["generation"], r["cluster_key"], r["retired_property_id"],
                     r["outcome"], r["undone_at"] is not None, r["undone_by"])
                    for r in self.ledger
                    if not r["dry_run"] and r["outcome"] in ("applied", "refused")
                    and (r["generation"] == p["generation"]
                         or r["retired_property_id"] in p["property_ids"])]
        if sql == S.ENGINE_MERGES_SQL:
            ids, seen, out = set(p["listing_ids"]), set(), []
            for r in self.ledger:
                if (r["dry_run"] or r["outcome"] != "applied"
                        or r["merge_group_id"] in seen or not ids & set(r["member_ids"])):
                    continue
                seen.add(r["merge_group_id"])
                out.append((r["generation"], r["cluster_key"], r["merge_group_id"],
                            r["survivor_property_id"], list(r["member_ids"]),
                            r["undone_at"] is not None, r["undone_by"]))
            return out
        if sql == S.LATER_LIVE_MERGES_SQL:
            return [(r["generation"], r["cluster_key"], r["merge_group_id"],
                     r["survivor_property_id"], r["retired_property_id"], r["applied_at"],
                     json.loads(r["plan_json"]))
                    for r in self.ledger
                    if not r["dry_run"] and r["outcome"] == "applied"
                    and r["undone_at"] is None and r["id"] > p["after_id"]
                    and p["property_id"] in (r["survivor_property_id"],
                                             r["retired_property_id"])]
        if sql == S.PROPERTY_STATE_SQL:
            return [(pid, r["status"], r["merged_into"], r["merged_at"])
                    for pid, r in sorted(self.properties.items()) if pid in p["property_ids"]]
        if sql == S.LEDGER_INSERT_SQL:
            return self._ledger_insert(p)
        if sql == property_identity._PLACES_SQL:
            return [(lid, self.listings[lid]["property_id"]) for lid in p["ids"]
                    if lid in self.listings]
        if sql == property_identity._LIVE_MOVES_SQL:
            return [(e["listing"], e["id"], e["merge_group_id"], e["survivor"], e["retired"],
                     e["source"], None)
                    for e in sorted(self.events, key=lambda e: (e["listing"], e["id"]))
                    if e["listing"] in p["ids"] and not e["undone"]]
        if sql == property_identity._STATUS_SQL:
            return [(pid, r["status"], r["merged_into"])
                    for pid, r in sorted(self.properties.items()) if pid in p["ids"]]
        if sql == S.UNAPPLY_TARGETS_SQL:
            groups: dict[str, dict[str, Any]] = {}
            for r in self.ledger:
                if r["dry_run"] or r["outcome"] != "applied" or r["undone_at"] is not None:
                    continue
                if any(p[key] is not None and r[col] != p[key] for key, col in (
                        ("generation", "generation"), ("cluster_key", "cluster_key"),
                        ("run", "run_id"))):
                    continue
                if ((p["since"] is not None and r["applied_at"] < p["since"])
                        or (p["until"] is not None and r["applied_at"] >= p["until"])):
                    continue
                g = groups.setdefault(r["merge_group_id"], {
                    "gen": r["generation"], "key": r["cluster_key"],
                    "surv": r["survivor_property_id"],
                    "retired": [], "last": 0, "members": list(r["member_ids"]),
                    "plan": json.loads(r["plan_json"]),  # jsonb reads back as a dict
                    "at": r["applied_at"]})
                g["retired"].append(r["retired_property_id"])
                g["last"] = max(g["last"], r["id"])
                g["at"] = min(g["at"], r["applied_at"])
            ordered = sorted(groups.items(), key=lambda kv: -kv[1]["last"])
            return [(gid, g["gen"], g["key"], g["surv"], g["retired"], g["last"], g["members"],
                     g["plan"], g["at"]) for gid, g in ordered]
        if sql == S.LEDGER_UNDO_SQL:
            for r in self.ledger:
                if (r["merge_group_id"] == p["merge_group_id"] and not r["dry_run"]
                        and r["outcome"] == "applied" and r["undone_at"] is None):
                    r.update(undone_at=T0, undone_by=p["undone_by"],
                             undo_result=json.loads(p["undo_result"]))
            return []
        raise AssertionError(f"unknown statement: {sql[:80]!r}")

    def _ledger_insert(self, p: dict[str, Any]) -> list[tuple]:
        # The migration's constraints, enforced where they are cheapest to meet.
        assert p["outcome"] in ("planned", "applied", "skipped", "refused", "failed")
        if p["outcome"] == "applied":
            assert not p["dry_run"] and p["merge_group_id"] is not None
            assert p["survivor_property_id"] is not None and p["retired_property_id"] is not None
            for r in self.ledger:
                if (r["outcome"] == "applied" and r["undone_at"] is None
                        and r["survivor_property_id"] == p["survivor_property_id"]
                        and r["retired_property_id"] == p["retired_property_id"]):
                    raise AssertionError("unique violation: live (survivor, retired) pair")
        json.loads(p["plan_json"])
        self.ledger.append({**p, "id": len(self.ledger) + 1, "applied_at": self.now,
                            "undone_at": None, "undone_by": None, "undo_result": None})
        return []

    # --- the toolkit's contract, faked
    def merge_pair(self, survivor_id: int, retired_id: int, *, group: str | None = None,
                   source: str = "operator", fail: bool = False) -> int:
        """The chokepoint; a test fabricating history calls it straight, any survivor."""
        group = group or str(uuid.uuid4())
        s, r = self.properties[survivor_id], self.properties[retired_id]
        if s["status"] != "active" or r["status"] != "active":
            raise MergeError("not active")
        if fail or (s["category_type"] and r["category_type"]
                    and s["category_type"] != r["category_type"]):
            raise MergeError(f"category_type mismatch ({retired_id}); refusing to merge")
        if not category_main_compatible(s["category_main"], r["category_main"]):
            raise MergeError("category_main mismatch; refusing to merge")
        moved = sorted(lid for lid, row in self.listings.items()
                       if row["property_id"] == retired_id)
        for lid in moved:
            self.listings[lid]["property_id"] = survivor_id
            self.events.append({"id": len(self.events) + 1, "merge_group_id": group,
                                "survivor": survivor_id, "retired": retired_id, "listing": lid,
                                "generation": None, "undone": False, "source": source})
        if r["asset_id"] is not None:
            s["asset_id"], r["asset_id"] = r["asset_id"], None
        r.update(status="merged_away", merged_into=survivor_id, merged_at=self.now)
        return len(moved)

    def merge(self, calls: list[dict[str, Any]], fail_on: set[int] | None = None) -> Any:
        def merge(conn: "FakeDb", property_ids: list[int], *, source: str, reason: str,
                  merge_group_id: str | None = None, confidence: float | None = None,
                  markers: dict[str, Any] | None = None,
                  decided_by: str | None = None) -> dict[str, Any]:
            group = merge_group_id or str(uuid.uuid4())
            ids = sorted(set(property_ids))
            with conn.transaction():
                if any(conn.properties[pid]["status"] != "active" for pid in ids):
                    raise MergeError("not active")
                carriers = [pid for pid in ids if conn.properties[pid]["asset_id"] is not None]
                assets = {conn.properties[pid]["asset_id"] for pid in carriers}
                if len(assets) > 1 or (len(carriers) > 1 and source != "operator"):
                    raise AssetLinkConflict(f"asset links {sorted(assets)} on {carriers}")
                survivor = survivor_of({pid: conn.properties[pid]["first_seen_at"]
                                        for pid in ids})
                retired = [pid for pid in ids if pid != survivor]
                moved = 0
                for rid in retired:
                    calls.append({"survivor_id": survivor, "retired_id": rid, "reason": reason,
                                  "source": source, "confidence": confidence,
                                  "markers": markers, "merge_group_id": group})
                    moved += conn.merge_pair(survivor, rid, group=group, source=source,
                                             fail=bool(fail_on and rid in fail_on))
            return {"data": {"merge_group_id": group, "survivor_id": survivor,
                             "retired_ids": retired, "listings_moved": moved}}
        return merge

    def detach(self, calls: list[str | None]) -> Any:
        def detach(conn: "FakeDb", listing_id: int, *, decided_by: str, reason: str | None = None,
                   source: str = "operator", merge_group_id: str | None = None
                   ) -> dict[str, Any]:
            calls.append(merge_group_id)
            with conn.transaction():
                current = conn.listings[listing_id]["property_id"]
                moves = [(e["id"], e["merge_group_id"], e["survivor"], e["retired"])
                         for e in conn.events if e["listing"] == listing_id and not e["undone"]]
                here = [lid for lid, row in conn.listings.items() if row["property_id"] == current]
                merged = {e["listing"] for e in conn.events if not e["undone"]}
                outcome, undo, target = _detach_plan(current, moves, merge_group_id,
                                                     (len(here), len(set(here) - merged)))
                if outcome == "detached" and _origin_gone(
                        (conn.properties[target]["status"],
                         conn.properties[target]["merged_into"]), undo):
                    outcome = "origin_moved_on"
                if outcome == "detached":
                    conn.listings[listing_id]["property_id"] = target
                    for e in (e for e in conn.events if e["id"] in {m[0] for m in undo}):
                        e["undone"] = True
                    back = conn.properties[target]
                    if back["status"] == "merged_away":
                        back.update(status="active", merged_into=None, merged_at=None)
            return {"data": {"detached": outcome == "detached", "outcome": outcome}}
        return detach

    def undo_group(self, merge_group_id: str) -> None:
        """The operator takes one merge apart by hand: every advert it moved, detached."""
        detach = self.detach([])
        for e in [e for e in self.events if e["merge_group_id"] == merge_group_id]:
            detach(self, e["listing"], decided_by="operator", merge_group_id=merge_group_id)


def _pair_group(db: FakeDb, key: int, lids: list[int], pids: list[int], **kw: Any) -> None:
    for lid, pid in zip(lids, pids):
        if pid not in db.properties:
            db.prop(pid)
        db.listing(lid, pid)
    db.group(key, lids, **kw)


def _plan(db: FakeDb, **scope: Any) -> A.Plan:
    return A.plan_apply(db, GEN, A.Scope(**scope))


def _only(plan: A.Plan) -> A.GroupPlan:
    assert len(plan.groups) == 1, plan.groups
    return plan.groups[0]


# ------------------------------------------------------------------ the plan


def test_the_plan_names_the_toolkits_survivor_the_oldest_record() -> None:
    # decision 17: one rule for the engine and the operator; holding more adverts wins nothing
    db = FakeDb()
    _pair_group(db, 10, [10, 11, 12, 13], [100, 200, 300, 200])  # 200 carries two members
    group = _only(_plan(db))
    assert group.reasons == []
    assert group.survivor_id == 100 and group.retired_ids == [200, 300]


def test_survivor_tie_goes_to_the_oldest_first_seen_then_the_lowest_id() -> None:
    db = FakeDb()
    db.prop(100, first=T0 + timedelta(days=5))
    db.prop(200, first=T0)
    db.prop(300, first=T0)
    _pair_group(db, 10, [10, 11, 12], [100, 200, 300])
    group = _only(_plan(db))
    assert group.survivor_id == 200 and group.retired_ids == [100, 300]


def test_a_group_already_on_one_property_is_a_counted_no_op() -> None:
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 100])
    plan = _plan(db)
    assert plan.groups == [] and plan.counts["already_one_property"] == 1


def test_operator_negatives_refuse_a_group() -> None:
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    _pair_group(db, 20, [20, 21], [300, 400])
    _pair_group(db, 30, [30, 31], [500, 600])
    db.verdicts.append({"kind": "pair", "lo": 10, "hi": 11, "verdict": "different"})
    db.mnl.append((20, 21, "operator"))
    db.verdicts.append({"kind": "cluster", "cluster_key": 30, "verdict": "different",
                        "generation": GEN, "member_ids": [30, 31]})
    reasons = {g.cluster_key: g.reasons for g in _plan(db).groups}
    assert A.SKIP_PAIR_VERDICT in reasons[10]
    assert reasons[20] == [A.SKIP_MUST_NOT_LINK]
    assert reasons[30] == [A.SKIP_CLUSTER_VERDICT]


def test_a_group_verdict_about_a_wider_set_does_not_refuse_a_subset() -> None:
    # "30, 31 and 32 are not one property" says nothing against 30 and 31 alone.
    db = FakeDb()
    _pair_group(db, 30, [30, 31], [500, 600])
    db.verdicts.append({"kind": "cluster", "cluster_key": 30, "verdict": "different",
                        "generation": "g11", "member_ids": [30, 31, 32]})
    assert _only(_plan(db)).reasons == []


def test_a_group_verdict_refuses_every_later_superset_of_its_set() -> None:
    # g11 ruled {10, 11} different; g12 regrouped them with a re-listing, 12. Uniting all
    # three unites the two the operator separated, whatever else joins them.
    db = FakeDb()
    _pair_group(db, 10, [10, 11, 12], [100, 200, 300])
    db.verdicts.append({"kind": "cluster", "cluster_key": 10, "verdict": "different",
                        "generation": "g11", "member_ids": [10, 11]})
    group = _only(_plan(db))
    assert group.reasons == [A.SKIP_CLUSTER_VERDICT]
    assert group.detail["group_verdicts"] == [[10, 11]]


def test_a_group_verdict_is_found_under_a_changed_cluster_key() -> None:
    # The re-listing has the lowest id, so the g12 group is keyed 5, not 10: the ruling is
    # found by the listings it names, not by the key it was taken under.
    db = FakeDb()
    _pair_group(db, 5, [5, 10, 11], [50, 100, 200])
    db.verdicts.append({"kind": "cluster", "cluster_key": 10, "verdict": "different",
                        "generation": "g11", "member_ids": [10, 11]})
    assert _only(_plan(db)).reasons == [A.SKIP_CLUSTER_VERDICT]


def test_a_group_verdict_with_no_member_set_refuses_its_key_in_every_generation() -> None:
    # A ruling that names no set cannot be matched to listings, so it fails CLOSED on its key.
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    _pair_group(db, 20, [20, 21], [300, 400])
    _pair_group(db, 30, [30, 31], [500, 600])
    db.verdicts.append({"kind": "cluster", "cluster_key": 10, "verdict": "different",
                        "generation": None, "member_ids": None})
    db.verdicts.append({"kind": "cluster", "cluster_key": 20, "verdict": "different",
                        "generation": "g11", "member_ids": None})
    reasons = {g.cluster_key: g.reasons for g in _plan(db).groups}
    assert reasons == {10: [A.SKIP_CLUSTER_VERDICT], 20: [A.SKIP_CLUSTER_VERDICT], 30: []}


def test_the_newest_group_verdict_of_each_operator_on_a_set_is_the_one_that_stands() -> None:
    # g10 {10, 11} ruled different, then g12's {10, 11} ruled same by the same operator: the
    # newer ruling retracts the older one. Another operator's standing negative still refuses,
    # and so does a negative newer than the positive.
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    db.verdicts.append({"kind": "cluster", "cluster_key": 10, "verdict": "different",
                        "generation": "g10", "member_ids": [10, 11], "decided_by": "op",
                        "decided_at": T0})
    db.verdicts.append({"kind": "cluster", "cluster_key": 10, "verdict": "same",
                        "generation": GEN, "member_ids": [10, 11], "decided_by": "op",
                        "decided_at": T0 + timedelta(days=2)})
    assert _only(_plan(db)).reasons == []
    db.verdicts.append({"kind": "cluster", "cluster_key": 10, "verdict": "different",
                        "generation": "g11", "member_ids": [10, 11], "decided_by": "op2",
                        "decided_at": T0 + timedelta(days=1)})
    assert _only(_plan(db)).reasons == [A.SKIP_CLUSTER_VERDICT]
    db.verdicts.pop()
    db.verdicts.append({"kind": "cluster", "cluster_key": 10,
                        "verdict": "same_building_different_unit", "generation": GEN,
                        "member_ids": [10, 11], "decided_by": "op",
                        "decided_at": T0 + timedelta(days=3)})
    group = _only(_plan(db))
    assert group.reasons == [A.SKIP_CLUSTER_VERDICT]
    assert group.detail["group_verdicts"] == [[10, 11]]
    # Who ruled is a grouping key only: it never reaches the ledger or the artifact.
    db.verdicts[-1]["decided_by"] = "someone@example.cz"
    A.apply_plan(db, _plan(db), dry_run=True)
    assert "example.cz" not in json.dumps([r["plan_json"] for r in db.ledger])


def _engine_merged(db: FakeDb, key: int, lids: list[int], pids: list[int],
                   gen: str = "g11") -> None:
    """`gen` merged `lids` (on `pids`) through the apply path, as a live engine merge."""
    _pair_group(db, key, lids, pids, gen=gen)
    db.live_scope()
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    result = A.apply_plan(db, A.plan_apply(db, gen, scope), dry_run=False, merge=db.merge([]))
    assert result["counts"]["applied"] == 1, result


def test_a_group_verdict_counts_across_both_properties_full_listing_sets() -> None:
    # g11 merged 12 onto 200 with 11; g12 groups 10 with 11 and leaves 12 out. The operator
    # ruled {10, 12} different: the merge would put both on one property, so it is refused
    # although neither the group nor its key is the one the ruling was taken on.
    db = FakeDb()
    _engine_merged(db, 11, [11, 12], [200, 250])
    _pair_group(db, 10, [10, 11], [100, 200])
    db.verdicts.append({"kind": "cluster", "cluster_key": 12, "verdict": "different",
                        "generation": "g9", "member_ids": [10, 12]})
    group = _only(_plan(db))
    assert group.reasons == [A.SKIP_CLUSTER_VERDICT]
    assert group.detail["group_verdicts"] == [[10, 12]]
    assert group.listing_ids == [10, 11, 12]


def test_a_negative_against_a_listing_the_merge_would_carry_along_refuses() -> None:
    # 200 holds 11 (clustered) and 12 (not): the merge moves 12 too, so 10 x 12 counts.
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    db.listing(12, 200)
    db.mnl.append((10, 12, "guard"))
    group = _only(_plan(db))
    assert group.reasons == [A.SKIP_MUST_NOT_LINK, A.SKIP_CARRIES_UNGROUPED]
    assert group.detail["must_not_link"] == [[10, 12, "guard"]]


def test_a_listing_this_engine_merged_with_a_member_may_ride_along() -> None:
    # g11 merged 12 onto 200 together with 11; g12 groups 10 with 11 and leaves 12 out. 12
    # shares 200 with a grouped member by THIS engine's live merge, so it may move with it —
    # and every check reads the property the merge would build: scope, categories.
    db = FakeDb()
    _engine_merged(db, 11, [11, 12], [200, 250])
    _pair_group(db, 10, [10, 11], [100, 200])
    group = _only(_plan(db))
    assert group.reasons == [] and group.listing_ids == [10, 11, 12]
    assert group.survivor_id == 100 and group.retired_ids == [200]
    narrow = _only(_plan(db, listing_ids=frozenset({10, 11})))
    assert narrow.reasons == [A.SKIP_CARRIES_OUT_OF_SCOPE]
    assert narrow.detail["out_of_scope_listings"] == [12]
    db.listings[12]["category_type"] = "pronajem"
    assert _only(_plan(db)).reasons == [A.SKIP_CATEGORY_TYPE]


def test_an_undone_engine_merge_vouches_for_nothing() -> None:
    db = FakeDb()
    _engine_merged(db, 11, [11, 12], [200, 250])
    A.unapply(db, "g11", dry_run=False, detach=db.detach([]))
    db.listing(12, 200)  # back on 200 by some other merge: not the engine's word any more
    _pair_group(db, 10, [10, 11], [100, 200])
    group = _only(_plan(db))
    assert group.reasons == [A.SKIP_CARRIES_UNGROUPED]
    assert group.detail["ungrouped_listings"] == [12]


def test_a_property_carrying_listings_no_group_holds_is_refused() -> None:
    # 100 is an older multi-listing property (10 plus 97-99); g12 grouped 10 with 11 only.
    # Merging would put 11 on one property with three adverts the engine never matched.
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    for lid in (97, 98, 99):
        db.listing(lid, 100)
    group = _only(_plan(db))
    assert group.reasons == [A.SKIP_CARRIES_UNGROUPED]
    assert group.detail["ungrouped_listings"] == [97, 98, 99]


def test_category_guards_mirror_the_chokepoint() -> None:
    db = FakeDb()
    db.prop(200, ct="pronajem")
    _pair_group(db, 10, [10, 11], [100, 200])
    db.listing(11, 200, ct="pronajem")
    db.prop(400, cm="dum")
    _pair_group(db, 20, [20, 21], [300, 400])
    db.listing(21, 400, cm="dum")
    db.prop(600, cm="komercni")
    db.prop(500, cm="dum")
    _pair_group(db, 30, [30, 31], [500, 600])
    db.listing(30, 500, cm="dum")
    db.listing(31, 600, cm="komercni")
    reasons = {g.cluster_key: g.reasons for g in _plan(db).groups}
    assert reasons[10] == [A.SKIP_CATEGORY_TYPE]
    assert reasons[20] == [A.SKIP_CATEGORY_MAIN]
    assert reasons[30] == []  # dum <-> komercni is the one sanctioned cross-type


def test_the_merges_own_asset_refusal_is_reported_as_asset_linked_units() -> None:
    # The plan does not second-guess asset links: the merge carries one onto the survivor and
    # refuses two different ones (decision 17) — and, the engine's, two units the operator
    # linked into ONE asset, "different units, do not collapse" (rule 15, E903) — and that
    # refusal is the group's reason.
    db = FakeDb()
    db.live_scope()
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    db.prop(100)
    db.prop(200, asset=7)
    _pair_group(db, 10, [10, 11], [100, 200])
    db.prop(300, asset=8)
    db.prop(400, asset=9)
    _pair_group(db, 20, [20, 21], [300, 400])
    db.prop(500, asset=5)
    db.prop(600, asset=5)
    _pair_group(db, 30, [30, 31], [500, 600])
    plan = A.plan_apply(db, GEN, scope)
    assert [g.reasons for g in plan.groups] == [[], [], []]
    calls: list[dict[str, Any]] = []
    result = A.apply_plan(db, plan, dry_run=False, merge=db.merge(calls))
    assert [(c["survivor_id"], c["retired_id"]) for c in calls] == [(100, 200)]
    assert db.properties[100]["asset_id"] == 7 and db.properties[200]["asset_id"] is None
    assert [(g["cluster_key"], g["reasons"]) for g in result["skipped_at_apply"]] == [
        (20, [A.SKIP_ASSET_LINKED]), (30, [A.SKIP_ASSET_LINKED])]
    assert {db.listings[lid]["property_id"] for lid in (30, 31)} == {500, 600}
    skipped = next(r for r in db.ledger if r["cluster_key"] == 20)
    assert skipped["outcome"] == "skipped" and skipped["error"] == A.SKIP_ASSET_LINKED
    assert {db.listings[lid]["property_id"] for lid in (20, 21)} == {300, 400}


def test_merged_away_and_unattached_are_refused_and_size_is_the_engines_cap_alone() -> None:
    db = FakeDb()
    db.prop(200, status="merged_away")
    _pair_group(db, 10, [10, 11], [100, 200])
    _pair_group(db, 20, [20, 21], [300, 400])
    db.listing(22, None)
    db.members.append((GEN, 20, 22))
    # Twelve listings on twelve properties: the engine capped the group when it built it.
    _pair_group(db, 30, list(range(30, 42)), list(range(500, 512)))
    reasons = {g.cluster_key: g.reasons for g in _plan(db).groups}
    assert reasons[10] == [A.SKIP_INACTIVE_PROPERTY]
    assert A.SKIP_UNATTACHED in reasons[20]
    assert reasons[30] == []


def test_a_property_the_engine_split_across_two_groups_bridges_nothing() -> None:
    # 200 carries 11 (group 10) and 21 (group 20): applying either would fuse both.
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    _pair_group(db, 20, [20, 21], [300, 200])
    reasons = {g.cluster_key: g.reasons for g in _plan(db).groups}
    assert reasons == {10: [A.SKIP_SPANS_GROUPS], 20: [A.SKIP_SPANS_GROUPS]}


def test_scope_filters_categories_blocks_and_listing_ids() -> None:
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    _pair_group(db, 20, [20, 21], [300, 400], block=("c", 490245))
    _pair_group(db, 30, [30, 31], [500, 600], block=None)
    db.listing(40, 700, ct="pronajem")
    db.listing(41, 800, ct="pronajem")
    db.prop(700, ct="pronajem")
    db.prop(800, ct="pronajem")
    db.group(40, [40, 41])
    scope = A.effective_scope(
        None, {"category_types": "prodej", "blocks": "town:563510 quarter:490245"}, live=False)
    plan = A.plan_apply(db, GEN, scope)
    assert [g.cluster_key for g in plan.to_apply] == [10, 20]
    assert plan.counts["out_of_scope"] == 2
    ids_only = A.plan_apply(db, GEN, A.Scope(listing_ids=frozenset({30, 31})))
    assert [g.cluster_key for g in ids_only.to_apply] == [30]


def test_the_run_cap_defers_the_rest_in_key_order() -> None:
    db = FakeDb()
    for key in (10, 20, 30):
        _pair_group(db, key, [key, key + 1], [key * 10, key * 10 + 1])
    plan = _plan(db, max_clusters_per_run=2)
    assert [g.cluster_key for g in plan.to_apply] == [10, 20]
    assert plan.deferred == [30] and plan.counts["deferred_run_cap"] == 1


def test_a_block_has_one_spelling_the_export_lanes() -> None:
    assert A.normalize_block(" Town:0563510 ") == "town:563510"
    assert A.normalize_block("quarter:490245") == "quarter:490245"
    assert A.block_of({"block_grain": "c", "block_key": 490245}) == "quarter:490245"
    for other in ("obec:563510", "cast_obce:490245", "o563510", "563510", "town:x"):
        with pytest.raises(ValueError, match="town:<code>"):
            A.normalize_block(other)
    with pytest.raises(ValueError):
        A.scope_fields({"blokcs": "town:1"})
    with pytest.raises(ValueError, match="max_cluster_size"):
        A.scope_fields({"max_cluster_size": 8})  # the engine's cap is the one cap


def test_a_live_scope_is_narrowed_by_a_run_and_never_widened() -> None:
    setting = {"category_types": ["prodej", "pronajem"], "blocks": ["town:1", "town:2"],
               "max_clusters_per_run": 6}
    scope = A.effective_scope(setting, {"category_types": "prodej", "blocks": "town:2 town:3",
                                        "max_clusters_per_run": "12", "all_blocks": "1"},
                              live=True)
    assert scope.category_types == frozenset({"prodej"})
    assert scope.blocks == frozenset({"town:2"})
    assert scope.max_clusters_per_run == 6 and scope.all_blocks is False
    with pytest.raises(ValueError):
        A.effective_scope(None, {"category_types": "prodej", "blocks": "town:1"}, live=True)
    assert A.Scope().live_problems() and A.Scope(category_types=frozenset({"prodej"})) \
        .live_problems()
    assert A.Scope(category_types=frozenset({"prodej"}), all_blocks=True).live_problems() == []


# ------------------------------------------------------------------ the rollout control


@pytest.mark.parametrize("row", [
    None, {"category_types": ["prodej"]}, {"category_types": ["prodej"], "blocks": []},
    {"category_types": [], "all_blocks": True}, {"category_types": ["prodej"], "x": 1},
])
def test_a_scope_row_naming_no_area_merges_nothing(row: Any) -> None:
    # The scope row is the one rollout control, read before every group: absent, malformed,
    # or naming no deal types or no area, it stops even a plan made under a scope that did.
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    if row is not None:
        db.settings[A.SCOPE_SETTING] = row
    plan = A.plan_apply(db, GEN, A.Scope(category_types=frozenset({"prodej"}), all_blocks=True))
    calls: list[dict[str, Any]] = []
    result = A.apply_plan(db, plan, dry_run=False, merge=db.merge(calls))
    assert calls == [] and db.ledger == [] and result["counts"]["not_attempted"] == 1
    assert A.SCOPE_SETTING in result["stopped"]


def test_live_is_refused_on_a_scope_that_names_no_area() -> None:
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    for scope in (A.Scope(category_types=frozenset({"prodej"})),
                  A.Scope(category_types=frozenset({"prodej"}), blocks=frozenset())):
        plan = A.plan_apply(db, GEN, scope)
        with pytest.raises(A.ApplyRefused, match="area"):
            A.apply_plan(db, plan, dry_run=False, merge=db.merge([]))
    assert db.ledger == []


def test_emptying_the_scope_area_mid_run_stops_before_the_next_group() -> None:
    db = FakeDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200])
    _pair_group(db, 20, [20, 21], [300, 400])
    calls: list[dict[str, Any]] = []
    base = db.merge(calls)

    def merge_then_empty(conn: FakeDb, ids: list[int], **kw: Any) -> dict[str, Any]:
        out = base(conn, ids, **kw)
        conn.settings[A.SCOPE_SETTING] = {"category_types": ["prodej"], "blocks": None}
        return out

    plan = A.plan_apply(db, GEN, A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True))
    result = A.apply_plan(db, plan, dry_run=False, merge=merge_then_empty)
    assert result["counts"]["applied"] == 1 and result["counts"]["not_attempted"] == 1
    assert "names no area" in result["stopped"]


# ------------------------------------------------------------------ dry run and live


def test_a_dry_run_writes_the_plan_and_nothing_else() -> None:
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    _pair_group(db, 20, [20, 21], [300, 400])
    db.mnl.append((20, 21, "operator"))
    before = copy.deepcopy((db.listings, db.properties, db.events))
    calls: list[dict[str, Any]] = []
    result = A.apply_plan(db, _plan(db), dry_run=True, merge=db.merge(calls))
    assert calls == []
    assert (db.listings, db.properties, db.events) == before
    assert all(r["dry_run"] for r in db.ledger)
    assert sorted((r["cluster_key"], r["outcome"]) for r in db.ledger) == [
        (10, "planned"), (20, "skipped")]
    assert result["planned"][0]["survivor_id"] == 100
    assert not any("property_merge_events" in s for s in db.statements)


def test_live_merges_go_through_the_chokepoint_one_group_per_engine_group() -> None:
    db = FakeDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11, 12], [100, 200, 300])
    calls: list[dict[str, Any]] = []
    plan = A.plan_apply(db, GEN, A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True))
    result = A.apply_plan(db, plan, dry_run=False, merge=db.merge(calls))
    assert [c["retired_id"] for c in calls] == [200, 300]
    assert {c["merge_group_id"] for c in calls} == {result["applied"][0]["merge_group_id"]}
    first = calls[0]
    assert first["source"] == "autodedup" and first["survivor_id"] == 100
    assert first["reason"] == f"autodedup {GEN} 10" and first["confidence"] == 0.99
    assert first["markers"]["generation"] == GEN and first["markers"]["cluster_key"] == 10
    assert first["markers"]["feature_version"] == 9
    assert {row["property_id"] for row in db.listings.values()} == {100}
    # Who merged is the chokepoint's `source`; nothing else touches the production ledger.
    assert {(e["source"], e["generation"]) for e in db.events} == {("autodedup", None)}
    assert not any("property_merge_events" in s for s in db.statements)
    applied = [r for r in db.ledger if r["outcome"] == "applied"]
    assert [(r["survivor_property_id"], r["retired_property_id"]) for r in applied] == [
        (100, 200), (100, 300)]
    assert result["counts"]["applied"] == 1 and result["counts"]["listings_moved"] == 2


def test_the_default_merge_and_undo_are_the_toolkits_one_merge_and_one_undo() -> None:
    import inspect

    assert inspect.signature(A.apply_plan).parameters["merge"].default \
        is property_identity.merge_property_set
    assert inspect.signature(A.unapply).parameters["detach"].default \
        is property_identity.detach_listing
    assert A.MERGE_SOURCE in typing.get_args(property_identity.MergeSource)


def test_a_second_run_is_idempotent() -> None:
    db = FakeDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200])
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    calls: list[dict[str, Any]] = []
    A.apply_plan(db, A.plan_apply(db, GEN, scope), dry_run=False, merge=db.merge(calls))
    again = A.plan_apply(db, GEN, scope)
    assert again.groups == [] and again.counts["already_one_property"] == 1
    A.apply_plan(db, again, dry_run=False, merge=db.merge(calls))
    assert len(calls) == 1


def test_a_refusal_rolls_the_whole_group_back_and_is_final_for_the_generation() -> None:
    db = FakeDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11, 12], [100, 200, 300])
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    calls: list[dict[str, Any]] = []
    result = A.apply_plan(db, A.plan_apply(db, GEN, scope), dry_run=False,
                          merge=db.merge(calls, fail_on={300}))
    assert len(calls) == 2  # 200 merged, then 300 refused ...
    assert {row["property_id"] for row in db.listings.values()} == {100, 200, 300}  # ... undone
    assert db.properties[200]["status"] == "active" and db.events == []
    assert result["counts"]["refused"] == 1 and result["counts"]["applied"] == 0
    assert {r["outcome"] for r in db.ledger} == {"refused"}
    assert _only(A.plan_apply(db, GEN, scope)).reasons == [A.SKIP_REFUSED_BEFORE]


def test_a_negative_recorded_during_a_run_stops_the_group_it_names() -> None:
    # The plan read no negative; the operator rules 20 x 21 different while group 10 merges.
    # Group 20 re-reads its negatives inside its own transaction and is skipped, not merged.
    db = FakeDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200])
    _pair_group(db, 20, [20, 21], [300, 400])
    calls: list[dict[str, Any]] = []
    base = db.merge(calls)

    def merge_then_rule(conn: FakeDb, ids: list[int], **kw: Any) -> dict[str, Any]:
        out = base(conn, ids, **kw)
        conn.verdicts.append({"kind": "pair", "lo": 20, "hi": 21, "verdict": "different"})
        return out

    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    plan = A.plan_apply(db, GEN, scope)
    assert [g.reasons for g in plan.groups] == [[], []]
    result = A.apply_plan(db, plan, dry_run=False, merge=merge_then_rule)
    assert [c["retired_id"] for c in calls] == [200]
    assert result["counts"]["applied"] == 1 and result["counts"]["skipped_at_apply"] == 1
    assert result["skipped_at_apply"] == [
        {"cluster_key": 20, "survivor_id": 300, "retired_ids": [400],
         "reasons": [A.SKIP_PAIR_VERDICT]}]
    assert db.listings[21]["property_id"] == 400
    assert [(r["cluster_key"], r["outcome"], r["error"]) for r in db.ledger
            if r["cluster_key"] == 20] == [(20, "skipped", A.SKIP_PAIR_VERDICT)]


def test_a_property_that_changed_since_the_plan_is_not_merged() -> None:
    db = FakeDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200])
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    plan = A.plan_apply(db, GEN, scope)
    db.listing(12, 200)  # merged onto 200 by someone after the plan was read
    calls: list[dict[str, Any]] = []
    result = A.apply_plan(db, plan, dry_run=False, merge=db.merge(calls))
    assert calls == [] and result["counts"]["skipped_at_apply"] == 1
    assert result["skipped_at_apply"][0]["reasons"] == [A.SKIP_CHANGED_SINCE_PLAN]
    row = next(r for r in db.ledger if r["outcome"] == "skipped")
    assert json.loads(row["plan_json"])["detail"]["arrived_since_plan"] == [12]


def test_the_apply_time_recheck_locks_and_re_reads_categories_and_scope() -> None:
    # The listing ids are unchanged, but after the plan a re-parse flips 11 to a rental (200's
    # rollup not recomputed yet), both of group 20's listings and properties become rentals
    # (no mix, but outside the prodej scope), and the operator links 500 and 600 to two
    # different assets, which the merge itself then refuses.
    db = FakeDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200])
    _pair_group(db, 20, [20, 21], [300, 400])
    _pair_group(db, 30, [30, 31], [500, 600])
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    plan = A.plan_apply(db, GEN, scope)
    assert [g.reasons for g in plan.groups] == [[], [], []]
    db.listings[11]["category_type"] = "pronajem"
    for lid, pid in ((20, 300), (21, 400)):
        db.listings[lid]["category_type"] = "pronajem"
        db.properties[pid]["category_type"] = "pronajem"
    db.properties[500]["asset_id"], db.properties[600]["asset_id"] = 3, 4
    calls: list[dict[str, Any]] = []
    result = A.apply_plan(db, plan, dry_run=False, merge=db.merge(calls))
    assert calls == [] and result["counts"]["skipped_at_apply"] == 3
    assert {row["cluster_key"]: row["reasons"] for row in result["skipped_at_apply"]} == {
        10: [A.SKIP_CATEGORY_TYPE, A.SKIP_CARRIES_OUT_OF_SCOPE],
        20: [A.SKIP_CARRIES_OUT_OF_SCOPE], 30: [A.SKIP_ASSET_LINKED]}
    # The properties are locked FOR UPDATE, then their listings FOR SHARE, before any check.
    assert "for update" in S.LOCK_PROPERTIES_SQL and "for share" in S.LOCK_PROPERTY_LISTINGS_SQL
    first_lock = db.statements.index(S.LOCK_PROPERTIES_SQL)
    assert first_lock < db.statements.index(S.LOCK_PROPERTY_LISTINGS_SQL)


def test_an_operator_split_during_the_run_that_keeps_the_same_listings_stops_the_group(
) -> None:
    # g12 merged 200 into 100 (10, 11). g13 plans {10, 11, 80}: 100 and 800. Before the run
    # reaches it the operator undoes g12's merge and merges 200 into 800: the SAME listings on
    # the same two properties, so the set check passes — but 10 and 11 are now separated.
    db = FakeDb()
    scope, calls = _applied_two(db)
    db.prop(800)
    db.listing(80, 800)
    db.group(10, [10, 11, 80], gen="g13")
    plan = A.plan_apply(db, "g13", scope)
    assert [(g.survivor_id, g.retired_ids, g.reasons) for g in plan.groups] == [
        (100, [800], [])]
    db.undo_group(calls[0]["merge_group_id"])
    db.merge_pair(800, 200)
    late: list[dict[str, Any]] = []
    result = A.apply_plan(db, plan, dry_run=False, merge=db.merge(late))
    assert late == [] and result["skipped_at_apply"][0]["reasons"] == [
        A.SKIP_RESTORED_ELSEWHERE]


def test_an_engine_merge_ruled_different_afterwards_is_reported_not_counted_away(
        tmp_path: Path, monkeypatch: Any) -> None:
    db = FakeDb()
    scope, calls = _applied_two(db)
    again = A.plan_apply(db, GEN, scope)
    assert again.groups == [] and again.counts["already_one_property"] == 2
    db.verdicts.append({"kind": "cluster", "cluster_key": 10, "verdict": "different",
                        "generation": GEN, "member_ids": [10, 11]})
    # ... and a pair on one property that no engine merge put there.
    _pair_group(db, 30, [30, 31], [500, 500])
    db.mnl.append((30, 31, "operator"))
    plan = A.plan_apply(db, GEN, scope)
    ruled = {g.cluster_key: g for g in plan.groups}
    assert set(ruled) == {10, 30} and plan.counts[A.RULED_AFTER_MERGE] == 2
    assert plan.counts["already_one_property"] == 1 and plan.counts["skipped"] == 0
    assert ruled[10].reasons == [A.RULED_AFTER_MERGE]
    assert ruled[10].detail["negatives"] == [A.SKIP_CLUSTER_VERDICT]
    assert [m["unapply"] for m in ruled[10].detail["engine_merges"]] == [
        f"generation={GEN},cluster_key=10"]
    assert ruled[30].detail["engine_merges"] == []

    page = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    out = A.run_apply(_factory(db), {"generation": GEN}, tmp_path)
    assert [r["cluster_key"] for r in out[A.RULED_AFTER_MERGE]] == [10, 30]
    assert out["skipped"] == [] and out["planned"] == []
    text = page.read_text()
    assert "carry an operator negative" in text
    assert f"generation={GEN},cluster_key=10" in text


def test_real_time_generations_are_refused(tmp_path: Path) -> None:
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200], gen="rt")
    with pytest.raises(ValueError, match="real-time"):
        A.plan_apply(db, "rt", A.Scope())
    for gen in ("rt", "RT", "rt_seed_g12"):
        with pytest.raises(SystemExit, match="real-time"):
            A.run_apply(_factory(db), {"generation": gen}, tmp_path)
    assert db.ledger == [] and not db.statements


# ------------------------------------------------------------------ undo


def _applied_two(db: FakeDb) -> tuple[A.Scope, list[dict[str, Any]]]:
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200])
    _pair_group(db, 20, [20, 21], [300, 400])
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    calls: list[dict[str, Any]] = []
    A.apply_plan(db, A.plan_apply(db, GEN, scope), dry_run=False, merge=db.merge(calls))
    return scope, calls


def test_unapply_undoes_a_generation_newest_first_and_a_later_apply_may_redo_it() -> None:
    db = FakeDb()
    scope, calls = _applied_two(db)
    groups = [c["merge_group_id"] for c in calls]
    listing = A.unapply(db, GEN, dry_run=True)
    assert [g["merge_group_id"] for g in listing["groups"]] == groups[::-1]
    assert all(r["undone_at"] is None for r in db.ledger)

    del db.settings[A.SCOPE_SETTING]  # undo is NOT gated by the scope
    undone: list[str] = []
    result = A.unapply(db, GEN, dry_run=False, detach=db.detach(undone))
    assert undone == groups[::-1] and result["counts"]["undone"] == 2
    assert {lid: row["property_id"] for lid, row in db.listings.items()} == {
        10: 100, 11: 200, 20: 300, 21: 400}
    assert all(r["undone_at"] is not None for r in db.ledger if r["outcome"] == "applied")

    # The engine's own undo is a brake, not a ruling: the same generation, or a later one,
    # may merge the same properties again when an apply is dispatched.
    assert {g.cluster_key: g.reasons for g in A.plan_apply(db, GEN, scope).groups} == {
        10: [], 20: []}
    db.group(10, [10, 11], gen="g13")
    later = A.plan_apply(db, "g13", scope)
    assert [g.reasons for g in later.groups] == [[]]


def test_unapply_picks_groups_by_generation_run_and_time_window() -> None:
    db = FakeDb()
    db.live_scope()
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    _pair_group(db, 10, [10, 11], [100, 200])
    A.apply_plan(db, A.plan_apply(db, GEN, scope), dry_run=False, merge=db.merge([]),
                 run_id="run-a")
    _pair_group(db, 20, [20, 21], [300, 400], gen="g13")
    A.apply_plan(db, A.plan_apply(db, "g13", scope), dry_run=False, merge=db.merge([]),
                 run_id="run-b")
    second = next(r["applied_at"] for r in db.ledger if r["cluster_key"] == 20)

    def picked(generation: str | None = None, **selectors: Any) -> list[tuple[str, int]]:
        found = A.unapply(db, generation, dry_run=True, **selectors)["groups"]
        return [(g["generation"], g["cluster_key"]) for g in found]

    assert picked(GEN) == [(GEN, 10)] and picked(GEN, cluster_key=20) == []
    assert picked(run="run-b") == [("g13", 20)]
    assert picked(since=second) == [("g13", 20)] and picked(until=second) == [(GEN, 10)]
    assert picked(since=second - timedelta(hours=1)) == [("g13", 20), (GEN, 10)]
    assert picked(GEN, run="run-b") == []  # every selector given must hold
    for bad in ({}, {"cluster_key": 10}):
        with pytest.raises(ValueError):
            A.unapply(db, None, dry_run=True, **bad)
    live = A.unapply(db, None, dry_run=False, run="run-a", detach=db.detach([]))
    assert live["counts"]["undone"] == 1 and "run=run-a" in A.summary_markdown(
        live, mode="unapply")
    assert db.listings[11]["property_id"] == 200 and db.listings[21]["property_id"] == 300


def test_unapply_one_group_and_one_already_undone_elsewhere() -> None:
    db = FakeDb()
    _scope, calls = _applied_two(db)
    first, second = calls[0]["merge_group_id"], calls[1]["merge_group_id"]
    db.undo_group(second)  # the ledger UI's undo
    dry = A.unapply(db, GEN, dry_run=True)
    assert dry["counts"]["already_undone"] == 1 and dry["counts"]["blocked"] == 0
    assert "already undone outside the engine" in dry["groups"][0]["note"]
    result = A.unapply(db, GEN, dry_run=False, detach=db.detach([]))
    assert result["counts"] == {"groups": 2, "undone": 1, "already_undone": 1, "blocked": 0,
                                "taken_apart_before": 0, "listings_moved_back": 1,
                                "conflicts": 0}
    one = A.unapply(db, GEN, dry_run=True, cluster_key=10)
    assert one["groups"] == []  # both groups are now recorded as undone
    assert first != second
    external = [r for r in db.ledger if r["merge_group_id"] == second]
    assert [r["undone_by"] for r in external] == [A.EXTERNAL_UNDO]
    assert external[0]["undo_result"]["noted_by"].startswith(A.UNAPPLY_BY_PREFIX)


def test_an_operator_undo_noted_by_unapply_still_blocks_a_later_generation() -> None:
    # g12 merged 400 into 300; the operator undid it on the merge ledger, then `unapply` rolled
    # back the rest of g12. g13 may re-merge what the ENGINE undid (group 10), never what the
    # operator separated (group 20).
    db = FakeDb()
    scope, calls = _applied_two(db)
    db.undo_group(calls[1]["merge_group_id"])
    assert {g.cluster_key: g.reasons for g in A.plan_apply(db, GEN, scope).groups} == {
        20: [A.SKIP_RESTORED_ELSEWHERE]}
    A.unapply(db, GEN, dry_run=False, detach=db.detach([]))
    db.group(10, [10, 11], gen="g13")
    db.group(20, [20, 21], gen="g13")
    later = {g.cluster_key: g.reasons for g in A.plan_apply(db, "g13", scope).groups}
    assert later == {10: [], 20: [A.SKIP_RESTORED_ELSEWHERE]}


def test_unapply_skips_a_group_whose_survivor_a_later_generation_merged_away() -> None:
    # g12 merged 200 into 100; g13 then merged 100 into 700. Undoing g12 first would only
    # reactivate 200 with no listings and call the group undone while Browse still shows it
    # merged, so it is skipped with the merge to undo first — and works once that is undone.
    db = FakeDb()
    scope, _calls = _applied_two(db)
    db.prop(700, first=T0 - timedelta(days=1))  # a tie on size; the older 700 survives
    db.listing(70, 700)
    db.listing(71, 700)
    db.group(10, [10, 11, 70, 71], gen="g13")
    g13 = A.plan_apply(db, "g13", scope)
    assert [(g.cluster_key, g.survivor_id, g.retired_ids, g.reasons) for g in g13.groups] == [
        (10, 700, [100], [])]
    A.apply_plan(db, g13, dry_run=False, merge=db.merge([]))
    before = copy.deepcopy((db.listings, db.properties))

    listing = A.unapply(db, GEN, dry_run=True, cluster_key=10)
    assert listing["counts"]["blocked"] == 1
    assert listing["groups"][0]["unapply_first"][0]["unapply"] == "generation=g13,cluster_key=10"
    result = A.unapply(db, GEN, dry_run=False, cluster_key=10, detach=db.detach([]))
    assert result["counts"]["blocked"] == 1 and result["counts"]["undone"] == 0
    assert "undo the later engine merge first" in result["groups"][0]["blocked"]
    assert (db.listings, db.properties) == before
    assert [r["undone_at"] for r in db.ledger
            if r["generation"] == GEN and r["cluster_key"] == 10 and r["outcome"] == "applied"
            ] == [None]

    A.unapply(db, "g13", dry_run=False, detach=db.detach([]))
    again = A.unapply(db, GEN, dry_run=False, cluster_key=10, detach=db.detach([]))
    assert again["counts"]["undone"] == 1 and again["counts"]["blocked"] == 0
    assert db.listings[11]["property_id"] == 200


def test_unapply_never_calls_undone_a_merge_that_would_move_nothing_back() -> None:
    db = FakeDb()
    _applied_two(db)
    db.prop(999)
    db.listing(11, 999)  # moved on since the merge (an operator split, say)
    before = copy.deepcopy((db.listings, db.properties))
    result = A.unapply(db, GEN, dry_run=False, cluster_key=10, detach=db.detach([]))
    assert result["counts"]["blocked"] == 1 and result["counts"]["undone"] == 0
    assert result["groups"][0]["conflicts"] == [11]
    assert (db.listings, db.properties) == before  # rolled back: 200 stays merged away
    assert all(r["undone_at"] is None for r in db.ledger if r["outcome"] == "applied")


def _outcome(target: Mapping[str, Any]) -> tuple[Any, ...]:
    return (target.get("blocked"), sorted(target.get("conflicts") or []),
            target.get("unapply_first"), target.get("taken_apart"))


def test_the_ledger_records_which_property_each_listing_sat_on_as_it_merged() -> None:
    db = FakeDb()
    _applied_two(db)
    plans = {r["cluster_key"]: json.loads(r["plan_json"]) for r in db.ledger}
    assert plans[10]["listings_by_property"] == {"100": [10], "200": [11]}
    assert plans[20]["listings_by_property"] == {"300": [20], "400": [21]}


def test_the_ledger_records_the_placement_it_locked_not_the_one_it_planned() -> None:
    # After the plan, 10 is re-pointed from 100 to 200: the same two listings on the same two
    # properties, so the group still merges — moving BOTH off 200. The ledger records that, so
    # the dry-run undo counts both as the live undo moves them.
    db = FakeDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200])
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    plan = A.plan_apply(db, GEN, scope)
    assert _only(plan).listings_by_property == {100: [10], 200: [11]}
    db.listings[10]["property_id"] = 200
    result = A.apply_plan(db, plan, dry_run=False, merge=db.merge([]))
    assert result["counts"]["applied"] == 1 and result["counts"]["listings_moved"] == 2
    (row,) = [r for r in db.ledger if r["outcome"] == "applied"]
    assert json.loads(row["plan_json"])["listings_by_property"] == {"100": [], "200": [10, 11]}
    dry = A.unapply(db, GEN, dry_run=True)
    live = A.unapply(db, GEN, dry_run=False, detach=db.detach([]))
    assert dry["counts"]["listings_moved_back"] == live["counts"]["listings_moved_back"] == 2


def test_a_dry_run_blocks_a_split_group_exactly_as_the_live_run_does() -> None:
    # g12 merged 200 (11) into 100 (10). The operator split 100: 10 stayed, 11 went to 901.
    # Nothing the merge moved off 200 is on 100 any more, so the live undo would move nothing
    # back and is refused — and the dry run the operator approves says so, not "undoable".
    db = FakeDb()
    _applied_two(db)
    db.prop(901)
    db.listings[11]["property_id"] = 901
    before = copy.deepcopy((db.listings, db.properties))
    dry = A.unapply(db, GEN, dry_run=True, cluster_key=10)
    live = A.unapply(db, GEN, dry_run=False, cluster_key=10, detach=db.detach([]))
    assert dry["counts"] == live["counts"] == {
        "groups": 1, "undone": 0, "already_undone": 0, "blocked": 1, "taken_apart_before": 0,
        "listings_moved_back": 0, "conflicts": 0}
    assert _outcome(dry["groups"][0]) == _outcome(live["groups"][0]) == (
        "nothing would move back (1 listings moved on since the merge); left as it stands",
        [11], [], None)
    assert live["groups"][0]["outcome"] == "blocked"
    assert (db.listings, db.properties) == before
    assert all(r["undone_at"] is None for r in db.ledger if r["outcome"] == "applied")


def test_a_later_merge_on_the_survivor_is_not_named_when_undoing_it_frees_nothing() -> None:
    # As above, and then g13 merged 700 (70) into 100 alongside 10. Undoing g13 would leave
    # 11 on 901 all the same, so g12's group is refused for what it is, naming nothing.
    db = FakeDb()
    scope, _calls = _applied_two(db)
    db.prop(901)
    db.listings[11]["property_id"] = 901
    db.prop(700)
    db.listing(70, 700)
    db.group(10, [10, 70], gen="g13")
    g13 = A.plan_apply(db, "g13", scope)
    assert [(g.survivor_id, g.retired_ids, g.reasons) for g in g13.groups] == [(100, [700], [])]
    A.apply_plan(db, g13, dry_run=False, merge=db.merge([]))
    dry = A.unapply(db, GEN, dry_run=True, cluster_key=10)
    live = A.unapply(db, GEN, dry_run=False, cluster_key=10, detach=db.detach([]))
    for result in (dry, live):
        assert result["counts"]["blocked"] == 1
        assert result["groups"][0]["unapply_first"] == []
        assert result["groups"][0]["blocked"].startswith("nothing would move back")
    assert db.listings[70]["property_id"] == 100  # g13 stands


def _undone_the_same(dry: Mapping[str, Any], live: Mapping[str, Any]) -> None:
    # The dry run the operator approves reports every count the live run then produces, bar
    # `undone`: a dry run undoes nothing.
    assert {k: v for k, v in dry["counts"].items() if k != "undone"} == {
        k: v for k, v in live["counts"].items() if k != "undone"}
    assert [_outcome(g) for g in dry["groups"]] == [_outcome(g) for g in live["groups"]]


@pytest.mark.parametrize("built_on", [False, True])
def test_a_group_the_operator_partly_detached_is_undone_or_blocked_as_its_dry_run_says(
        built_on: bool) -> None:
    # g12 merged 200 (11) and 300 (12) into 100 (10); the operator detached 11 from the page
    # (200 back). The rest stands: both runs move 12 back, or, once g13 merged 700 onto 100,
    # both name g13 to undo first.
    db = FakeDb()
    db.live_scope()
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    _pair_group(db, 10, [10, 11, 12], [100, 200, 300])
    A.apply_plan(db, A.plan_apply(db, GEN, scope), dry_run=False, merge=db.merge([]))
    db.detach([])(db, 11, decided_by="operator")
    if built_on:
        db.prop(700)
        db.listing(70, 700)
        db.group(10, [10, 12, 70], gen="g13")
        A.apply_plan(db, A.plan_apply(db, "g13", scope), dry_run=False, merge=db.merge([]))
    dry = A.unapply(db, GEN, dry_run=True)
    live = A.unapply(db, GEN, dry_run=False, detach=db.detach([]))
    _undone_the_same(dry, live)
    if built_on:
        assert [g["cluster_key"] for g in live["groups"][0]["unapply_first"]] == [10]
        assert db.listings[12]["property_id"] == 100
    else:
        assert live["groups"][0]["outcome"] == "undone_after_outside_split"
        assert live["counts"]["listings_moved_back"] == 1
        assert {lid: row["property_id"] for lid, row in db.listings.items()} == {
            10: 100, 11: 200, 12: 300}


def test_a_group_the_operator_undid_is_noted_undone_after_its_survivor_merged_on() -> None:
    # g12 merged 200 (11) into 100 (10). The operator undid it on the merge ledger, then merged
    # 100 into an older 500 holding 50. g13 grouped {10, 50, 60} and merged 600 into 500 — a
    # good merge. g12 no longer stands, wherever its survivor went: `unapply` notes it undone
    # as the operator's, names nothing, and leaves g13 and every property as they are.
    db = FakeDb()
    db.live_scope()
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    _pair_group(db, 10, [10, 11], [100, 200])
    calls: list[dict[str, Any]] = []
    A.apply_plan(db, A.plan_apply(db, GEN, scope), dry_run=False, merge=db.merge(calls))
    db.undo_group(calls[0]["merge_group_id"])
    db.prop(500, first=T0 - timedelta(days=30))
    db.listing(50, 500)
    db.merge_pair(500, 100)
    db.prop(600)
    db.listing(60, 600)
    db.group(50, [10, 50, 60], gen="g13")
    g13 = A.plan_apply(db, "g13", scope)
    assert [(g.survivor_id, g.retired_ids, g.reasons) for g in g13.groups] == [(500, [600], [])]
    A.apply_plan(db, g13, dry_run=False, merge=db.merge([]))
    before = copy.deepcopy((db.listings, db.properties))

    dry = A.unapply(db, GEN, dry_run=True)
    live = A.unapply(db, GEN, dry_run=False, detach=db.detach([]))
    _undone_the_same(dry, live)
    assert live["counts"] == {"groups": 1, "undone": 0, "already_undone": 1, "blocked": 0,
                              "taken_apart_before": 0, "listings_moved_back": 0,
                              "conflicts": 0}
    assert "already undone outside the engine" in dry["groups"][0]["note"]
    assert live["groups"][0]["outcome"] == "already_undone"
    for result in (dry, live):
        (group,) = result["groups"]
        assert not group.get("unapply_first") and "g13" not in json.dumps(group)
    assert [r["undone_by"] for r in db.ledger if r["merge_group_id"]
            == calls[0]["merge_group_id"]] == [A.EXTERNAL_UNDO]
    assert (db.listings, db.properties) == before
    assert [r["undone_at"] for r in db.ledger
            if r["generation"] == "g13" and r["outcome"] == "applied"] == [None]


def test_unapply_never_names_a_later_merge_of_a_group_taken_apart_outside_the_engine() -> None:
    # g12 merged 200 (11) into 100 (10), and it stands — but the operator merged 100 into an
    # older 500 holding 50. g13 grouped {10, 11, 50, 60} and merged 600 into 500. Undoing g13
    # would not bring 100 back (an operator merge retired it), so `unapply` of g12 reports the
    # group taken apart outside the engine and names nothing.
    db = FakeDb()
    scope, _calls = _applied_two(db)
    db.prop(500, first=T0 - timedelta(days=30))
    db.listing(50, 500)
    db.merge_pair(500, 100)
    db.prop(600)
    db.listing(60, 600)
    db.group(50, [10, 11, 50, 60], gen="g13")
    g13 = A.plan_apply(db, "g13", scope)
    assert [(g.survivor_id, g.retired_ids, g.reasons) for g in g13.groups] == [(500, [600], [])]
    A.apply_plan(db, g13, dry_run=False, merge=db.merge([]))
    before = copy.deepcopy((db.listings, db.properties))

    dry = A.unapply(db, GEN, dry_run=True, cluster_key=10)
    live = A.unapply(db, GEN, dry_run=False, cluster_key=10, detach=db.detach([]))
    _undone_the_same(dry, live)
    for result in (dry, live):
        (group,) = result["groups"]
        assert result["counts"]["blocked"] == 1
        assert group["unapply_first"] == [] and group["taken_apart"] == [10, 11]
        assert "taken apart outside the engine" in group["blocked"]
        assert "g13" not in json.dumps(group)
    assert (db.listings, db.properties) == before
    assert [r["undone_at"] for r in db.ledger if r["outcome"] == "applied"] == [
        None, None, None]


def test_a_later_merge_whose_own_undo_would_move_nothing_back_blocks_nothing() -> None:
    # g12 merged 200 (11) into 100 (10). g13 grouped {10, 11, 70} and merged 700 (70) onto
    # 100; the operator then re-pointed 70 to 905. g13's own undo would move nothing back and
    # is refused, so naming it would block g12 for ever: g12 is undone, moving 11 back.
    db = FakeDb()
    scope, _calls = _applied_two(db)
    db.prop(700)
    db.listing(70, 700)
    db.group(10, [10, 11, 70], gen="g13")
    g13 = A.plan_apply(db, "g13", scope)
    assert [(g.survivor_id, g.retired_ids, g.reasons) for g in g13.groups] == [(100, [700], [])]
    A.apply_plan(db, g13, dry_run=False, merge=db.merge([]))
    db.prop(905)
    db.listings[70]["property_id"] = 905

    own = A.unapply(db, "g13", dry_run=False, detach=db.detach([]))
    assert own["counts"]["blocked"] == 1 and own["groups"][0]["conflicts"] == [70]
    dry = A.unapply(db, GEN, dry_run=True, cluster_key=10)
    live = A.unapply(db, GEN, dry_run=False, cluster_key=10, detach=db.detach([]))
    _undone_the_same(dry, live)
    assert live["counts"]["undone"] == 1 and live["counts"]["listings_moved_back"] == 1
    assert "unapply_first" not in dry["groups"][0]
    assert {lid: db.listings[lid]["property_id"] for lid in (10, 11, 70)} == {
        10: 100, 11: 200, 70: 905}


def test_a_later_merge_someone_already_undid_is_named_and_noting_it_frees_the_group() -> None:
    # As above, but the operator undid g13 on the merge ledger instead: 70 is back on 700.
    # g13's own `unapply` notes that undo, so it is the one named; once it has, g12 undoes.
    db = FakeDb()
    scope, _calls = _applied_two(db)
    db.prop(700)
    db.listing(70, 700)
    db.group(10, [10, 11, 70], gen="g13")
    g13_calls: list[dict[str, Any]] = []
    A.apply_plan(db, A.plan_apply(db, "g13", scope), dry_run=False, merge=db.merge(g13_calls))
    db.undo_group(g13_calls[0]["merge_group_id"])

    blocked = A.unapply(db, GEN, dry_run=True, cluster_key=10)
    assert [m["unapply"] for m in blocked["groups"][0]["unapply_first"]] == [
        "generation=g13,cluster_key=10"]
    assert A.unapply(db, "g13", dry_run=False, detach=db.detach([]))["counts"][
        "already_undone"] == 1
    live = A.unapply(db, GEN, dry_run=False, cluster_key=10, detach=db.detach([]))
    assert live["counts"]["undone"] == 1 and db.listings[11]["property_id"] == 200


def test_a_later_merge_that_retired_the_survivor_blocks_only_while_the_survivor_is_away(
) -> None:
    # g12 merged 200 (11) into 100 (10); g13 merged 100 into 700. The operator undid g13 on the
    # merge ledger: 100 is active again, holding 10 and 11. g13 put nothing ON 100, so g12 is
    # not blocked by it and moves 11 back.
    db = FakeDb()
    scope, _calls = _applied_two(db)
    db.prop(700, first=T0 - timedelta(days=1))
    db.listing(70, 700)
    db.listing(71, 700)
    db.group(10, [10, 11, 70, 71], gen="g13")
    g13_calls: list[dict[str, Any]] = []
    A.apply_plan(db, A.plan_apply(db, "g13", scope), dry_run=False, merge=db.merge(g13_calls))
    assert [(c["survivor_id"], c["retired_id"]) for c in g13_calls] == [(700, 100)]
    db.undo_group(g13_calls[0]["merge_group_id"])

    dry = A.unapply(db, GEN, dry_run=True, cluster_key=10)
    live = A.unapply(db, GEN, dry_run=False, cluster_key=10, detach=db.detach([]))
    _undone_the_same(dry, live)
    assert live["counts"]["undone"] == 1 and live["counts"]["blocked"] == 0
    assert {lid: db.listings[lid]["property_id"] for lid in (10, 11, 70, 71)} == {
        10: 100, 11: 200, 70: 700, 71: 700}


@pytest.mark.parametrize("hand_into", [800, 700])
def test_a_later_retiring_merge_the_operator_undid_is_not_named(hand_into: int) -> None:
    # As above, and the operator then merged 100 on by hand — into 800, or back into 700 (the
    # same pair as g13, at another time). No standing engine merge retired 100 now: g12 was
    # taken apart outside the engine and names nothing (undoing g13 would free nothing).
    db = FakeDb()
    scope, _calls = _applied_two(db)
    db.prop(700, first=T0 - timedelta(days=1))
    db.listing(70, 700)
    db.listing(71, 700)
    db.prop(800)
    db.group(10, [10, 11, 70, 71], gen="g13")
    g13_calls: list[dict[str, Any]] = []
    A.apply_plan(db, A.plan_apply(db, "g13", scope), dry_run=False, merge=db.merge(g13_calls))
    db.undo_group(g13_calls[0]["merge_group_id"])
    db.merge_pair(hand_into, 100)
    before = copy.deepcopy((db.listings, db.properties))

    dry = A.unapply(db, GEN, dry_run=True, cluster_key=10)
    live = A.unapply(db, GEN, dry_run=False, cluster_key=10, detach=db.detach([]))
    _undone_the_same(dry, live)
    for result in (dry, live):
        (group,) = result["groups"]
        assert result["counts"]["blocked"] == 1 and group["unapply_first"] == []
        assert "taken apart outside the engine" in group["blocked"]
    assert (db.listings, db.properties) == before


def test_a_merge_undone_and_then_redone_by_hand_is_noted_undone_by_the_dry_run_too() -> None:
    # g12 merged 200 (11) into 100 (10). The operator undid it on the merge ledger and later
    # merged 200 into 100 again by hand: `merged_into` is the same, but that is THEIR merge
    # (its `merged_at` is not g12's `applied_at`). `unmerge_group` finds nothing live of g12,
    # so the dry run says "already undone" exactly as the live run then records it.
    db = FakeDb()
    _scope, calls = _applied_two(db)
    db.undo_group(calls[0]["merge_group_id"])
    db.merge_pair(100, 200)
    before = copy.deepcopy((db.listings, db.properties))

    dry = A.unapply(db, GEN, dry_run=True, cluster_key=10)
    live = A.unapply(db, GEN, dry_run=False, cluster_key=10, detach=db.detach([]))
    _undone_the_same(dry, live)
    assert live["counts"]["already_undone"] == 1 and live["counts"]["listings_moved_back"] == 0
    assert "already undone outside the engine" in dry["groups"][0]["note"]
    assert (db.listings, db.properties) == before  # the operator's own merge stands
    assert [r["undone_by"] for r in db.ledger if r["merge_group_id"]
            == calls[0]["merge_group_id"]] == [A.EXTERNAL_UNDO]


def test_a_merge_restored_outside_the_engine_is_never_re_merged() -> None:
    db = FakeDb()
    scope, calls = _applied_two(db)
    db.undo_group(calls[0]["merge_group_id"])
    reasons = {g.cluster_key: g.reasons for g in A.plan_apply(db, GEN, scope).groups}
    assert reasons == {10: [A.SKIP_RESTORED_ELSEWHERE]}


def test_an_operator_undo_survives_the_restored_property_being_merged_on() -> None:
    # g12 merged 200 (11) into 100 (10). The operator undid it, then merged 200 into an older
    # 500 holding 11's true duplicate 50. g13 groups {10, 11, 50} on 100 and 500: 200 is no
    # longer among its properties, but 10 and 11 are the listings the operator separated.
    db = FakeDb()
    scope, calls = _applied_two(db)
    db.undo_group(calls[0]["merge_group_id"])
    db.prop(500, first=T0 - timedelta(days=30))
    db.listing(50, 500)
    db.merge_pair(500, 200)
    db.group(10, [10, 11, 50], gen="g13")
    group = _only(A.plan_apply(db, "g13", scope))
    assert group.property_ids == [100, 500]
    assert group.reasons == [A.SKIP_RESTORED_ELSEWHERE]
    assert group.detail["separated_engine_merges"] == [{
        "generation": GEN, "cluster_key": 10, "merge_group_id": calls[0]["merge_group_id"],
        "listings": [10, 11]}]
    # ... and still once `unapply` has noted that undo as someone else's.
    A.unapply(db, GEN, dry_run=False, detach=db.detach([]))
    assert [r["undone_by"] for r in db.ledger if r["merge_group_id"]
            == calls[0]["merge_group_id"]] == [A.EXTERNAL_UNDO]
    assert _only(A.plan_apply(db, "g13", scope)).reasons == [A.SKIP_RESTORED_ELSEWHERE]


def test_unapply_after_an_operator_split_records_the_split_as_the_operators() -> None:
    # g12 merged 200 (11) into 100 (10). The operator split 100 into singletons: 11 stayed, 10
    # went to a fresh 901. `unapply` then moves 11 back to 200 — but separating 10 and 11 was
    # the operator's word, so the ledger records the undo as theirs and g13 never re-unites them.
    db = FakeDb()
    scope, calls = _applied_two(db)
    db.prop(901)
    db.listings[10]["property_id"] = 901
    db.group(10, [10, 11], gen="g13")
    assert [g.reasons for g in A.plan_apply(db, "g13", scope).groups] == [
        [A.SKIP_RESTORED_ELSEWHERE]]
    dry = A.unapply(db, GEN, dry_run=True, cluster_key=10)
    assert dry["groups"][0]["taken_apart"] == [10] and dry["counts"]["taken_apart_before"] == 1
    result = A.unapply(db, GEN, dry_run=False, cluster_key=10, detach=db.detach([]))
    assert result["counts"]["undone"] == 1 and result["counts"]["taken_apart_before"] == 1
    assert result["groups"][0]["outcome"] == "undone_after_outside_split"
    assert db.listings[11]["property_id"] == 200
    rows = [r for r in db.ledger if r["merge_group_id"] == calls[0]["merge_group_id"]]
    assert [r["undone_by"] for r in rows] == [A.EXTERNAL_UNDO]
    assert rows[0]["undo_result"]["noted_by"].startswith(A.UNAPPLY_BY_PREFIX)
    assert rows[0]["undo_result"]["taken_apart"] == [10]
    group = _only(A.plan_apply(db, "g13", scope))
    assert group.property_ids == [200, 901] and group.reasons == [A.SKIP_RESTORED_ELSEWHERE]


def test_a_whole_generation_unapply_after_a_split_with_conflicts_keeps_the_split() -> None:
    # g12 retired 200 (11) and 300 (12) into 100 (10). The operator split 100: 11 stayed, 10
    # and 12 went to 901 / 902. The whole-generation undo moves 11 back and reports 12 as a
    # conflict; recorded as the operator's undo, so g13's {10, 11} stays refused.
    db = FakeDb()
    db.live_scope()
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    db.prop(100, first=T0 - timedelta(days=5))
    _pair_group(db, 10, [10, 11, 12], [100, 200, 300])
    A.apply_plan(db, A.plan_apply(db, GEN, scope), dry_run=False, merge=db.merge([]))
    db.prop(901)
    db.prop(902)
    db.listings[10]["property_id"] = 901
    db.listings[12]["property_id"] = 902
    result = A.unapply(db, GEN, dry_run=False, detach=db.detach([]))
    assert result["counts"]["conflicts"] == 1 and result["counts"]["taken_apart_before"] == 1
    assert {r["undone_by"] for r in db.ledger if r["outcome"] == "applied"} == {
        A.EXTERNAL_UNDO}
    db.group(10, [10, 11], gen="g13")
    assert _only(A.plan_apply(db, "g13", scope)).reasons == [A.SKIP_RESTORED_ELSEWHERE]


def test_unapply_refuses_a_group_a_later_generation_built_on() -> None:
    # g12 merged 200 (11) into 100 (10). g13 grouped {11, 40} and left 10 out; 10 rode along,
    # vouched by g12, and 400 retired into 100. Undoing g12 now would leave 100 holding
    # {10, 40}, a pair no generation proposed — so it is refused, naming g13's merge.
    db = FakeDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200])
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    A.apply_plan(db, A.plan_apply(db, GEN, scope), dry_run=False, merge=db.merge([]))
    db.prop(400)
    db.listing(40, 400)
    db.group(11, [11, 40], gen="g13")
    g13 = A.plan_apply(db, "g13", scope)
    assert [(g.survivor_id, g.retired_ids, g.reasons) for g in g13.groups] == [
        (100, [400], [])]
    A.apply_plan(db, g13, dry_run=False, merge=db.merge([]))
    before = copy.deepcopy((db.listings, db.properties))

    dry = A.unapply(db, GEN, dry_run=True)
    assert dry["counts"]["blocked"] == 1
    assert [m["unapply"] for m in dry["groups"][0]["unapply_first"]] == [
        "generation=g13,cluster_key=11"]
    live = A.unapply(db, GEN, dry_run=False, detach=db.detach([]))
    assert live["counts"]["blocked"] == 1 and live["counts"]["undone"] == 0
    assert "undo the later engine merge first" in live["groups"][0]["blocked"]
    assert (db.listings, db.properties) == before

    A.unapply(db, "g13", dry_run=False, detach=db.detach([]))
    again = A.unapply(db, GEN, dry_run=False, detach=db.detach([]))
    assert again["counts"]["undone"] == 1 and again["counts"]["blocked"] == 0
    assert {lid: db.listings[lid]["property_id"] for lid in (10, 11, 40)} == {
        10: 100, 11: 200, 40: 400}


def test_a_later_group_the_same_dry_run_undoes_first_blocks_nothing() -> None:
    # g13 built on g12's survivor; unapplying g13 lists it and nothing is blocked, and a dry
    # run of g12 alone still names g13 until g13 is actually undone.
    db = FakeDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200])
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    A.apply_plan(db, A.plan_apply(db, GEN, scope), dry_run=False, merge=db.merge([]))
    db.prop(400)
    db.listing(40, 400)
    db.group(11, [11, 40], gen=GEN + "b")
    A.apply_plan(db, A.plan_apply(db, GEN + "b", scope), dry_run=False, merge=db.merge([]))
    assert A.unapply(db, GEN + "b", dry_run=True)["counts"]["blocked"] == 0
    assert A.unapply(db, GEN, dry_run=True)["counts"]["blocked"] == 1


def test_unapply_has_no_dry_run_default() -> None:
    import inspect

    param = inspect.signature(A.unapply).parameters["dry_run"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        A.unapply(FakeDb(), GEN)  # type: ignore[call-arg]


# ------------------------------------------------------------------ the lane


def _factory(db: FakeDb) -> Any:
    return lambda: db


def test_the_lane_mode_dry_runs_by_default_and_writes_its_artifact(tmp_path: Path) -> None:
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    out = A.run_apply(_factory(db), {"generation": GEN}, tmp_path)
    assert out["dry_run"] is True and out["counts"]["planned"] == 1
    body = json.loads((tmp_path / "apply.json").read_text())
    assert body["plan"][0]["survivor_id"] == 100
    assert db.closed and all(r["dry_run"] for r in db.ledger)
    assert lane.MODES["apply"] is A.run_apply and lane.MODES["unapply"] is A.run_unapply


def test_the_lane_refuses_live_without_the_scope(tmp_path: Path) -> None:
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    with pytest.raises(SystemExit, match="scope"):
        A.run_apply(_factory(db), {"generation": GEN, "dry_run": "0"}, tmp_path)
    # A run's args cannot supply the area the operator's setting does not name.
    db.settings[A.SCOPE_SETTING] = {"category_types": ["prodej"]}
    with pytest.raises(SystemExit, match="area"):
        A.run_apply(_factory(db), {"generation": GEN, "dry_run": "0",
                                   "blocks": "town:563510"}, tmp_path)
    with pytest.raises(SystemExit, match="unknown arg"):
        A.run_apply(_factory(db), {"generation": GEN, "reapply": "1"}, tmp_path)
    # An empty dry_run= is a dry run, never a live one.
    assert A.run_apply(_factory(db), {"generation": GEN, "dry_run": ""}, tmp_path)["dry_run"]
    db.ledger.clear()
    with pytest.raises(SystemExit, match="unknown arg"):
        A.run_apply(_factory(db), {"generation": GEN, "scope": "x"}, tmp_path)
    with pytest.raises(SystemExit, match="generation"):
        A.run_apply(_factory(db), {}, tmp_path)
    assert db.ledger == []


def test_the_lane_applies_live_inside_the_scope(tmp_path: Path, monkeypatch: Any) -> None:
    db = FakeDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200])
    calls: list[dict[str, Any]] = []
    original = A.apply_plan
    monkeypatch.setattr(A, "apply_plan",
                        lambda conn, plan, dry_run: original(conn, plan, dry_run,
                                                             merge=db.merge(calls)))
    out = A.run_apply(_factory(db), {"generation": GEN, "dry_run": "0"}, tmp_path)
    assert out["counts"]["applied"] == 1 and len(calls) == 1
    un = A.run_unapply(_factory(db), {"generation": GEN}, tmp_path)
    assert un["dry_run"] is True and len(un["groups"]) == 1


def test_a_crash_mid_run_still_publishes_what_merged(tmp_path: Path, monkeypatch: Any) -> None:
    db = FakeDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200])
    _pair_group(db, 20, [20, 21], [300, 400])
    _pair_group(db, 30, [30, 31], [500, 600])
    calls: list[dict[str, Any]] = []
    base = db.merge(calls)

    def merge_or_crash(conn: FakeDb, ids: list[int], **kw: Any) -> dict[str, Any]:
        if 400 in ids:
            raise RuntimeError("canceling statement due to statement timeout")
        return base(conn, ids, **kw)

    original = A.apply_plan
    monkeypatch.setattr(A, "apply_plan",
                        lambda conn, plan, dry_run: original(conn, plan, dry_run,
                                                             merge=merge_or_crash))
    page = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    with pytest.raises(RuntimeError, match="statement timeout"):
        A.run_apply(_factory(db), {"generation": GEN, "dry_run": "0"}, tmp_path)
    body = json.loads((tmp_path / "apply.json").read_text())
    assert [row["cluster_key"] for row in body["applied"]] == [10]
    assert [row["cluster_key"] for row in body["failed"]] == [20]
    assert "statement timeout" in body["aborted"] and body["counts"]["not_attempted"] == 1
    text = page.read_text()
    assert "**Aborted:**" in text and "| 10 | 100 | 200 |" in text
    assert db.closed and db.listings[11]["property_id"] == 100


def test_a_cancelled_run_still_publishes_what_merged(tmp_path: Path, monkeypatch: Any) -> None:
    # GitHub's cancel sends SIGINT: a KeyboardInterrupt, not an Exception.
    db = FakeDb()
    db.live_scope()
    for key in (10, 20, 30):
        _pair_group(db, key, [key, key + 1], [key * 10, key * 10 + 1])
    calls: list[dict[str, Any]] = []
    base = db.merge(calls)

    def merge_or_cancel(conn: FakeDb, ids: list[int], **kw: Any) -> dict[str, Any]:
        if 201 in ids:
            raise KeyboardInterrupt()
        return base(conn, ids, **kw)

    original = A.apply_plan
    monkeypatch.setattr(A, "apply_plan",
                        lambda conn, plan, dry_run: original(conn, plan, dry_run,
                                                             merge=merge_or_cancel))
    page = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    with pytest.raises(KeyboardInterrupt):
        A.run_apply(_factory(db), {"generation": GEN, "dry_run": "0"}, tmp_path)
    body = json.loads((tmp_path / "apply.json").read_text())
    assert [row["cluster_key"] for row in body["applied"]] == [10]
    assert body["aborted"].startswith("stopped at group 20: KeyboardInterrupt")
    assert body["counts"]["not_attempted"] == 2
    assert "**Aborted:**" in page.read_text() and db.closed
    assert db.listings[21]["property_id"] == 201  # the interrupted group rolled back whole


def test_an_unapply_that_crashes_mid_run_still_publishes_what_it_undid(
        tmp_path: Path, monkeypatch: Any) -> None:
    db = FakeDb()
    _applied_two(db)
    undo = db.detach([])
    seen: list[str] = []

    def detach_or_crash(conn: FakeDb, listing_id: int, **kw: Any) -> dict[str, Any]:
        seen.append(kw["merge_group_id"])
        if len(seen) == 2:
            raise RuntimeError("canceling statement due to statement timeout")
        return undo(conn, listing_id, **kw)

    original = A.unapply
    monkeypatch.setattr(A, "unapply", lambda conn, gen, **kw: original(
        conn, gen, **kw, detach=detach_or_crash))
    page = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    with pytest.raises(RuntimeError, match="statement timeout"):
        A.run_unapply(_factory(db), {"generation": GEN, "dry_run": "0"}, tmp_path)
    body = json.loads((tmp_path / "unapply.json").read_text())
    assert [(g["cluster_key"], g["outcome"]) for g in body["groups"]] == [
        (20, "undone"), (10, "aborted")]
    assert body["counts"]["undone"] == 1
    assert body["aborted"].startswith("stopped at group 10: RuntimeError")
    text = page.read_text()
    assert "**Aborted:**" in text and "WERE undone" in text and db.closed


@pytest.mark.parametrize("args, error", [
    (f"generation={GEN},dry_run=0,cluster_key=", "cluster_key= is empty"),
    (f"generation={GEN},dry_run=0,since=", "since= is empty"),
    ("dry_run=0,cluster_key=10", "cluster_key= needs generation="),
    ("dry_run=0", "unapply needs"),
    ("dry_run=0,since=yesterday", "ISO time"),
    ("dry_run=0,reapply=1", "unknown arg"),
])
def test_the_lane_never_widens_an_unapply_selection(tmp_path: Path, args: str,
                                                     error: str) -> None:
    db = FakeDb()
    _applied_two(db)
    with pytest.raises(SystemExit, match=error):
        A.run_unapply(_factory(db), lane.parse_kv_args(args), tmp_path)
    assert all(r["undone_at"] is None for r in db.ledger)


def test_the_lane_unapplies_by_time_window(tmp_path: Path) -> None:
    db = FakeDb()
    _applied_two(db)
    out = A.run_unapply(_factory(db), {"since": "2026-01-01T00:00", "until": "2027-01-01"},
                        tmp_path)
    assert out["dry_run"] is True and [g["cluster_key"] for g in out["groups"]] == [20, 10]
    assert out["since"] == "2026-01-01T00:00:00+00:00"


def test_the_lane_run_summary_is_a_lane_summary(tmp_path: Path) -> None:
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    code = lane.run("apply", f"generation={GEN}", tmp_path, conn_factory=_factory(db))
    assert code == 0
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["ok"] and summary["result"]["counts"]["planned"] == 1


def test_the_seeded_scope_leaves_a_live_lane_run_inert(tmp_path: Path) -> None:
    # Migration 558's seed, in shape: a scope with a deal type but no area, so OFF.
    db = FakeDb()
    db.settings[A.SCOPE_SETTING] = {
        "category_types": ["prodej"], "blocks": None, "listing_ids": None,
        "all_blocks": False, "max_clusters_per_run": 200}
    _pair_group(db, 10, [10, 11], [100, 200])
    before = copy.deepcopy((db.listings, db.properties, db.events))
    with pytest.raises(SystemExit, match="area"):
        A.run_apply(_factory(db), {"generation": GEN, "dry_run": "0"}, tmp_path)
    assert (db.listings, db.properties, db.events) == before and db.ledger == []


def test_the_run_summary_names_every_refusal(tmp_path: Path, monkeypatch: Any) -> None:
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    _pair_group(db, 20, [20, 21], [300, 400])
    db.mnl.append((20, 21, "operator"))
    page = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    A.run_apply(_factory(db), {"generation": GEN}, tmp_path)
    text = page.read_text()
    assert "DRY RUN" in text and "| planned | 1 |" in text
    assert "| must_not_link | 1 |" in text
    assert "| 10 | 100 | 200 |" in text and "| 20 | 300 | 400 | must_not_link |" in text


def test_nothing_but_the_lane_reaches_the_apply_path() -> None:
    # Inert when off: no module, schedule or worker calls it, only an operator's dispatch.
    import re

    root = Path(A.__file__).resolve().parent.parent
    importer = re.compile(r"from autodedup\.apply import|from autodedup import apply\b"
                          r"|import autodedup\.apply\b")
    callers = sorted(
        path.relative_to(root).as_posix()
        for top in ("autodedup", "scraper", "toolkit", "api", "scripts", "location_data")
        for path in (root / top).rglob("*.py")
        if importer.search(path.read_text(encoding="utf-8"))
    )
    assert callers == ["autodedup/lane.py"]
    workflow = (root / ".github" / "workflows" / "autodedup.yml").read_text(encoding="utf-8")
    trigger = workflow.split("\non:\n", 1)[1].split("\nconcurrency:", 1)[0]
    assert "schedule" not in trigger and "workflow_dispatch" in trigger


def test_the_rule_docs_name_only_the_control_the_adapter_reads() -> None:
    # CLAUDE.md is loaded by every session: a deleted switch named there is a stop button that does nothing.
    import re

    root = Path(A.__file__).resolve().parent.parent
    docs = [root / "CLAUDE.md", root / "docs" / "architecture.md",
            *sorted((root / ".claude" / "skills").glob("*/SKILL.md"))]
    named = {
        (path.relative_to(root).as_posix(), key)
        for path in docs
        for key in re.findall(r"autodedup_apply_\w+", path.read_text(encoding="utf-8"))
    }
    assert {key for _, key in named} == {A.SCOPE_SETTING}
    assert ("CLAUDE.md", A.SCOPE_SETTING) in named
