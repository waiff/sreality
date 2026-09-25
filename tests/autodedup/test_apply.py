"""A1 (PROGRAM.md E900-E906): engine groups into production merges, dark by default.

The fake below STORES what the apply path writes and serves it back, dispatching on the SQL
constant itself (the `fake_pg` idiom): a statement this file does not know raises. The merge
and unmerge it hands the apply path mimic the chokepoint's contract — re-point, soft-retire,
refuse a category mismatch, replay a group's events — inside nested transactions that roll
back on an exception, so a group's atomicity is observable here.
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
from toolkit.property_identity import MergeError
from toolkit.room_taxonomy import category_main_compatible

GEN = "g12"
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _Tx:
    def __init__(self, db: "FakeDb") -> None:
        self.db = db

    def __enter__(self) -> "_Tx":
        self.saved = self.db.state()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
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
        self.unapplied: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.statements: list[str] = []
        self.closed = False

    # --- plumbing
    _STATE = ("listings", "properties", "ledger", "unapplied", "events", "settings")

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
                                "asset_id": asset}

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
        self.settings[A.ENABLED_SETTING] = True
        self.settings[A.SCOPE_SETTING] = {"category_types": ["prodej"],
                                          "blocks": ["obec:563510"], **extra}

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
        if sql in (S.PROPERTIES_SQL, S.LOCK_PROPERTIES_SQL):
            return [(pid, r["status"], r["category_type"], r["category_main"],
                     r["first_seen_at"], r["asset_id"])
                    for pid, r in sorted(self.properties.items()) if pid in p["property_ids"]]
        if sql == S.ABSORBED_ASSETS_SQL:
            out = []
            for root in p["property_ids"]:
                seen, frontier = {root}, [root]
                while frontier:  # down merged_into, as the recursive CTE walks it
                    frontier = [q for q, r in self.properties.items()
                                if r["merged_into"] in frontier and q not in seen]
                    seen |= set(frontier)
                out += [(root, self.properties[q]["asset_id"]) for q in sorted(seen)
                        if q in self.properties and self.properties[q]["asset_id"] is not None]
            return out
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
            return [(r["generation"], r["cluster_key"], r["survivor_property_id"],
                     r["retired_property_id"], r["outcome"], r["undone_at"] is not None,
                     r["undone_by"])
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
            members = set(p["member_ids"])
            return [(r["generation"], r["cluster_key"], r["merge_group_id"])
                    for r in self.ledger
                    if not r["dry_run"] and r["outcome"] == "applied"
                    and r["undone_at"] is None and r["id"] > p["after_id"]
                    and (p["property_id"] in (r["survivor_property_id"],
                                              r["retired_property_id"])
                         or members & set(r["member_ids"]))]
        if sql == S.UNAPPLIED_GENERATION_SQL:
            return [(u["id"], u["undone_by"], u["unapplied_at"], u["released_at"] is not None)
                    for u in self.unapplied if u["generation"] == p["generation"]]
        if sql == S.STAMP_UNAPPLIED_SQL:
            self.unapplied.append({**p, "id": len(self.unapplied) + 1, "unapplied_at": T0,
                                   "released_at": None, "released_by": None})
            return []
        if sql == S.RELEASE_UNAPPLIED_SQL:
            for u in self.unapplied:
                if u["id"] in p["ids"] and u["released_at"] is None:
                    u.update(released_at=T0, released_by=p["released_by"])
            return []
        if sql == S.LEDGER_INSERT_SQL:
            return self._ledger_insert(p)
        if sql == S.STAMP_GENERATION_SQL:
            for event in self.events:
                if event["merge_group_id"] == p["merge_group_id"] and event["generation"] is None:
                    event["generation"] = p["stamp"]
            return []
        if sql == S.UNAPPLY_TARGETS_SQL:
            groups: dict[str, dict[str, Any]] = {}
            for r in self.ledger:
                if (r["generation"] != p["generation"] or r["dry_run"]
                        or r["outcome"] != "applied" or r["undone_at"] is not None):
                    continue
                if p["cluster_key"] is not None and r["cluster_key"] != p["cluster_key"]:
                    continue
                g = groups.setdefault(r["merge_group_id"], {
                    "key": r["cluster_key"], "surv": r["survivor_property_id"],
                    "retired": [], "last": 0, "members": list(r["member_ids"])})
                g["retired"].append(r["retired_property_id"])
                g["last"] = max(g["last"], r["id"])
            ordered = sorted(groups.items(), key=lambda kv: -kv[1]["last"])
            return [(gid, g["key"], g["surv"], g["retired"], g["last"], g["members"])
                    for gid, g in ordered]
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
        self.ledger.append({**p, "id": len(self.ledger) + 1, "undone_at": None,
                            "undone_by": None, "undo_result": None})
        return []

    # --- the chokepoint's contract, faked
    def merge(self, calls: list[dict[str, Any]], fail_on: set[int] | None = None) -> Any:
        def merge(conn: "FakeDb", *, survivor_id: int, retired_id: int, reason: str,
                  source: str, confidence: float | None = None,
                  markers: dict[str, Any] | None = None,
                  merge_group_id: str | None = None) -> dict[str, Any]:
            calls.append({"survivor_id": survivor_id, "retired_id": retired_id,
                          "reason": reason, "source": source, "confidence": confidence,
                          "markers": markers, "merge_group_id": merge_group_id})
            group = merge_group_id or str(uuid.uuid4())
            with conn.transaction():
                s, r = conn.properties[survivor_id], conn.properties[retired_id]
                if s["status"] != "active" or r["status"] != "active":
                    raise MergeError("not active")
                if fail_on and retired_id in fail_on:
                    raise MergeError(f"category_type mismatch ({retired_id}); refusing to merge")
                if (s["category_type"] and r["category_type"]
                        and s["category_type"] != r["category_type"]):
                    raise MergeError("category_type mismatch; refusing to merge")
                if not category_main_compatible(s["category_main"], r["category_main"]):
                    raise MergeError("category_main mismatch; refusing to merge")
                moved = [lid for lid, row in conn.listings.items()
                         if row["property_id"] == retired_id]
                for lid in moved:
                    conn.listings[lid]["property_id"] = survivor_id
                    conn.events.append({"merge_group_id": group, "survivor": survivor_id,
                                        "retired": retired_id, "listing": lid,
                                        "generation": None, "undone": False,
                                        "source": source})
                r.update(status="merged_away", merged_into=survivor_id)
            return {"data": {"merge_group_id": group, "survivor_id": survivor_id,
                             "retired_id": retired_id, "listings_moved": len(moved)}}
        return merge

    def unmerge(self, calls: list[str]) -> Any:
        def unmerge(conn: "FakeDb", *, merge_group_id: str, undone_by: str) -> dict[str, Any]:
            calls.append(merge_group_id)
            with conn.transaction():
                events = [e for e in conn.events
                          if e["merge_group_id"] == merge_group_id and not e["undone"]]
                if not events:
                    raise MergeError(f"no active merge events for group {merge_group_id}")
                back, conflicts = 0, []
                for e in events:
                    if conn.listings[e["listing"]]["property_id"] == e["survivor"]:
                        conn.listings[e["listing"]]["property_id"] = e["retired"]
                        back += 1
                    else:
                        conflicts.append(e["listing"])
                    conn.properties[e["retired"]].update(status="active", merged_into=None)
                    e["undone"] = True
            return {"data": {"merge_group_id": merge_group_id, "listings_moved_back": back,
                             "conflicts": conflicts}}
        return unmerge


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


def test_survivor_is_the_property_with_the_most_listings() -> None:
    db = FakeDb()
    _pair_group(db, 10, [10, 11, 12, 13], [100, 200, 300, 200])  # 200 carries two members
    group = _only(_plan(db))
    assert group.reasons == []
    assert group.survivor_id == 200 and group.retired_ids == [100, 300]


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
    # and every check reads the property the merge would build: size, scope, categories.
    db = FakeDb()
    _engine_merged(db, 11, [11, 12], [200, 250])
    _pair_group(db, 10, [10, 11], [100, 200])
    group = _only(_plan(db))
    assert group.reasons == [] and group.listing_ids == [10, 11, 12]
    assert group.survivor_id == 200 and group.retired_ids == [100]
    assert _only(_plan(db, max_cluster_size=2)).reasons == [A.SKIP_OVERSIZE]
    narrow = _only(_plan(db, listing_ids=frozenset({10, 11})))
    assert narrow.reasons == [A.SKIP_CARRIES_OUT_OF_SCOPE]
    assert narrow.detail["out_of_scope_listings"] == [12]
    db.listings[12]["category_type"] = "pronajem"
    assert _only(_plan(db)).reasons == [A.SKIP_CATEGORY_TYPE]


def test_an_undone_engine_merge_vouches_for_nothing() -> None:
    db = FakeDb()
    _engine_merged(db, 11, [11, 12], [200, 250])
    A.unapply(db, "g11", dry_run=False, unmerge=db.unmerge([]))
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
    group = _only(_plan(db, max_cluster_size=8))
    assert group.reasons == [A.SKIP_CARRIES_UNGROUPED]
    assert group.detail["ungrouped_listings"] == [97, 98, 99]
    # ... and the size cap reads what the merge would build, not the members alone.
    assert A.SKIP_OVERSIZE in _only(_plan(db, max_cluster_size=4)).reasons


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


def test_properties_the_operator_asset_linked_are_never_merged() -> None:
    # An asset link is the operator's "different units in one building, do not collapse"
    # (rule 15, migration 224) — even across the sanctioned dum <-> komercni pair, which the
    # chokepoint itself would let through.
    db = FakeDb()
    db.prop(100, cm="dum", asset=7)
    db.prop(200, cm="komercni", asset=7)
    _pair_group(db, 10, [10, 11], [100, 200])
    db.listing(10, 100, cm="dum")
    db.listing(11, 200, cm="komercni")
    db.prop(300, asset=8)
    db.prop(400, asset=9)
    _pair_group(db, 20, [20, 21], [300, 400])  # two different assets: one link would be lost
    db.prop(500, asset=5)
    _pair_group(db, 30, [30, 31, 32], [500, 600, 600])  # only one side linked
    plan = _plan(db)
    groups = {g.cluster_key: g for g in plan.groups}
    assert {k: g.reasons for k, g in groups.items()} == {
        10: [A.SKIP_ASSET_LINKED], 20: [A.SKIP_ASSET_LINKED], 30: []}
    assert groups[10].detail["asset_ids"] == [7]
    assert groups[20].detail["asset_linked_properties"] == [300, 400]
    # The chokepoint leaves asset_id on the row it retires, so the linked unit survives even
    # where 600's two listings would otherwise win.
    assert (groups[30].survivor_id, groups[30].retired_ids) == (500, [600])


def test_an_asset_link_left_on_a_retired_property_still_keeps_its_units_apart() -> None:
    # The operator asset-linked A=100 (10) and C=300 (30). g12 grouped {10, 20}: the linked A
    # survives, not the older B, so its link stays live. Even had a merge retired A first (an
    # operator's, say, leaving asset 7 on the merged_away row), g13 grouping {10, 30} is refused.
    db = FakeDb()
    db.live_scope()
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    db.prop(100, asset=7)
    db.prop(200, first=T0 - timedelta(days=9))  # older: it would win the size tie
    db.prop(300, asset=7)
    for lid, pid in ((10, 100), (20, 200), (30, 300)):
        db.listing(lid, pid)
    db.group(10, [10, 20], gen="g12")
    calls: list[dict[str, Any]] = []
    A.apply_plan(db, A.plan_apply(db, "g12", scope), dry_run=False, merge=db.merge(calls))
    assert [(c["survivor_id"], c["retired_id"]) for c in calls] == [(100, 200)]
    db.group(10, [10, 30], gen="g13")
    group = _only(A.plan_apply(db, "g13", scope))
    assert group.reasons == [A.SKIP_ASSET_LINKED] and group.detail["asset_ids"] == [7]

    # B survived and A retired still holding asset 7 (B has none): the link is read down
    # merged_into, a `properties` read (D7).
    db = FakeDb()
    db.live_scope()
    db.prop(100, asset=7)
    db.prop(200, first=T0 - timedelta(days=9))
    db.prop(300, asset=7)
    for lid, pid in ((10, 100), (20, 200), (30, 300)):
        db.listing(lid, pid)
    db.merge([])(db, survivor_id=200, retired_id=100, reason="operator", source="operator")
    db.group(10, [10, 20, 30], gen="g13")
    group = _only(A.plan_apply(db, "g13", scope))
    assert group.property_ids == [200, 300]
    assert group.reasons == [A.SKIP_ASSET_LINKED]
    assert group.detail["asset_linked_properties"] == [200, 300]
    # Different assets (7 on the absorbed A, 8 on C) refuse too: one link would be lost.
    db.properties[300]["asset_id"] = 8
    assert _only(A.plan_apply(db, "g13", scope)).detail["asset_ids"] == [7, 8]


def test_an_asset_link_set_after_the_plan_on_a_retiree_stops_the_group() -> None:
    db = FakeDb()
    db.live_scope()
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    _pair_group(db, 10, [10, 11], [100, 200])
    plan = A.plan_apply(db, GEN, scope)
    assert [(g.survivor_id, g.retired_ids) for g in plan.groups] == [(100, [200])]
    db.properties[200]["asset_id"] = 4  # the operator links the unit about to retire
    calls: list[dict[str, Any]] = []
    result = A.apply_plan(db, plan, dry_run=False, merge=db.merge(calls))
    assert calls == [] and result["skipped_at_apply"][0]["reasons"] == [A.SKIP_ASSET_LINKED]


def test_merged_away_unattached_and_oversize_are_refused() -> None:
    db = FakeDb()
    db.prop(200, status="merged_away")
    _pair_group(db, 10, [10, 11], [100, 200])
    _pair_group(db, 20, [20, 21], [300, 400])
    db.listing(22, None)
    db.members.append((GEN, 20, 22))
    _pair_group(db, 30, [30, 31, 32], [500, 600, 700])
    reasons = {g.cluster_key: g.reasons for g in _plan(db, max_cluster_size=2).groups}
    assert reasons[10] == [A.SKIP_INACTIVE_PROPERTY]
    assert A.SKIP_UNATTACHED in reasons[20]
    assert reasons[30] == [A.SKIP_OVERSIZE]


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


def test_block_spellings_normalise_and_nonsense_is_refused() -> None:
    assert A.normalize_block("obec:563510") == "o563510"
    assert A.normalize_block("town:563510") == "o563510"
    assert A.normalize_block("cast_obce:490245") == "c490245"
    assert A.normalize_block("c490245") == "c490245"
    with pytest.raises(ValueError):
        A.normalize_block("563510")
    with pytest.raises(ValueError):
        A.scope_fields({"blokcs": "o1"})


def test_a_live_scope_is_narrowed_by_a_run_and_never_widened() -> None:
    setting = {"category_types": ["prodej", "pronajem"], "blocks": ["obec:1", "obec:2"],
               "max_cluster_size": 6}
    scope = A.effective_scope(setting, {"category_types": "prodej", "blocks": "obec:2 obec:3",
                                        "max_cluster_size": "12", "all_blocks": "1"},
                              live=True)
    assert scope.category_types == frozenset({"prodej"})
    assert scope.blocks == frozenset({"o2"})
    assert scope.max_cluster_size == 6 and scope.all_blocks is False
    with pytest.raises(ValueError):
        A.effective_scope(None, {"category_types": "prodej", "blocks": "o1"}, live=True)
    assert A.Scope().live_problems() and A.Scope(category_types=frozenset({"prodej"})) \
        .live_problems()
    assert A.Scope(category_types=frozenset({"prodej"}), all_blocks=True).live_problems() == []


# ------------------------------------------------------------------ the kill switch


def test_live_is_refused_while_the_switch_is_off_and_absent_means_off() -> None:
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    scope = A.Scope(category_types=frozenset({"prodej"}), all_blocks=True)
    calls: list[dict[str, Any]] = []
    plan = A.plan_apply(db, GEN, scope)
    with pytest.raises(A.ApplyRefused):
        A.apply_plan(db, plan, dry_run=False, merge=db.merge(calls))
    db.settings[A.ENABLED_SETTING] = False
    with pytest.raises(A.ApplyRefused):
        A.apply_plan(db, plan, dry_run=False, merge=db.merge(calls))
    assert calls == [] and db.ledger == []
    A.apply_plan(db, plan, dry_run=True, merge=db.merge(calls))
    assert calls == [] and {r["outcome"] for r in db.ledger} == {"planned"}


@pytest.mark.parametrize("value, on", [
    (True, True), ("true", True), ({"enabled": True}, True),
    (False, False), (None, False), ("false", False), ({}, False), (1, True), (0, False),
])
def test_the_switch_reads_like_every_other_app_settings_flag(value: Any, on: bool) -> None:
    db = FakeDb()
    db.settings[A.ENABLED_SETTING] = value
    assert A.apply_enabled(db) is on
    assert A.apply_enabled(FakeDb()) is False  # no row at all


def test_live_is_refused_on_a_scope_that_names_no_area() -> None:
    db = FakeDb()
    db.settings[A.ENABLED_SETTING] = True
    _pair_group(db, 10, [10, 11], [100, 200])
    plan = A.plan_apply(db, GEN, A.Scope(category_types=frozenset({"prodej"})))
    with pytest.raises(A.ApplyRefused, match="area"):
        A.apply_plan(db, plan, dry_run=False, merge=db.merge([]))


def test_turning_the_switch_off_mid_run_stops_before_the_next_group() -> None:
    db = FakeDb()
    db.live_scope()
    _pair_group(db, 10, [10, 11], [100, 200])
    _pair_group(db, 20, [20, 21], [300, 400])
    calls: list[dict[str, Any]] = []
    base = db.merge(calls)

    def merge_then_flip(conn: FakeDb, **kw: Any) -> dict[str, Any]:
        out = base(conn, **kw)
        conn.settings[A.ENABLED_SETTING] = False
        return out

    plan = A.plan_apply(db, GEN, A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True))
    result = A.apply_plan(db, plan, dry_run=False, merge=merge_then_flip)
    assert result["counts"]["applied"] == 1 and result["counts"]["not_attempted"] == 1
    assert "stopped" in result


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
    assert {e["generation"] for e in db.events} == {f"autodedup:{GEN}"}
    applied = [r for r in db.ledger if r["outcome"] == "applied"]
    assert [(r["survivor_property_id"], r["retired_property_id"]) for r in applied] == [
        (100, 200), (100, 300)]
    assert result["counts"]["applied"] == 1 and result["counts"]["listings_moved"] == 2


def test_the_default_merge_is_the_chokepoint_and_its_source_is_registered() -> None:
    import inspect

    assert inspect.signature(A.apply_plan).parameters["merge"].default \
        is property_identity.merge_properties
    assert inspect.signature(A.unapply).parameters["unmerge"].default \
        is property_identity.unmerge_group
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

    def merge_then_rule(conn: FakeDb, **kw: Any) -> dict[str, Any]:
        out = base(conn, **kw)
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


def test_the_apply_time_recheck_locks_and_re_reads_categories_scope_and_asset_links() -> None:
    # The listing ids are unchanged, but after the plan a re-parse flips 11 to a rental (200's
    # rollup not recomputed yet), both of group 20's listings and properties become rentals
    # (no mix, but outside the prodej scope), and the operator asset-links 500 and 600.
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
    db.properties[500]["asset_id"] = db.properties[600]["asset_id"] = 3
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
    db.unmerge([])(db, merge_group_id=calls[0]["merge_group_id"], undone_by="operator")
    db.merge([])(db, survivor_id=800, retired_id=200, reason="operator", source="operator")
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


def test_unapply_undoes_a_generation_newest_first_and_it_never_re_applies() -> None:
    db = FakeDb()
    scope, calls = _applied_two(db)
    groups = [c["merge_group_id"] for c in calls]
    listing = A.unapply(db, GEN, dry_run=True)
    assert [g["merge_group_id"] for g in listing["groups"]] == groups[::-1]
    assert all(r["undone_at"] is None for r in db.ledger)

    db.settings[A.ENABLED_SETTING] = False  # undo is NOT gated by the switch
    undone: list[str] = []
    result = A.unapply(db, GEN, dry_run=False, unmerge=db.unmerge(undone))
    assert undone == groups[::-1] and result["counts"]["undone"] == 2
    assert {lid: row["property_id"] for lid, row in db.listings.items()} == {
        10: 100, 11: 200, 20: 300, 21: 400}
    assert all(r["undone_at"] is not None for r in db.ledger if r["outcome"] == "applied")

    replan = A.plan_apply(db, GEN, scope)
    assert {g.cluster_key: g.reasons for g in replan.groups} == {
        10: [A.SKIP_GENERATION_UNAPPLIED], 20: [A.SKIP_GENERATION_UNAPPLIED]}
    # A LATER generation may merge the same properties again.
    db.group(10, [10, 11], gen="g13")
    later = A.plan_apply(db, "g13", scope)
    assert [g.reasons for g in later.groups] == [[]]


def test_unapply_one_group_and_one_already_undone_elsewhere() -> None:
    db = FakeDb()
    _scope, calls = _applied_two(db)
    first, second = calls[0]["merge_group_id"], calls[1]["merge_group_id"]
    db.unmerge([])(db, merge_group_id=second, undone_by="operator")  # the ledger UI's undo
    result = A.unapply(db, GEN, dry_run=False, unmerge=db.unmerge([]))
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
    db.unmerge([])(db, merge_group_id=calls[1]["merge_group_id"], undone_by="operator")
    assert {g.cluster_key: g.reasons for g in A.plan_apply(db, GEN, scope).groups} == {
        20: [A.SKIP_RESTORED_ELSEWHERE]}
    A.unapply(db, GEN, dry_run=False, unmerge=db.unmerge([]))
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
    result = A.unapply(db, GEN, dry_run=False, cluster_key=10, unmerge=db.unmerge([]))
    assert result["counts"]["blocked"] == 1 and result["counts"]["undone"] == 0
    assert "undo the later engine merge first" in result["groups"][0]["blocked"]
    assert (db.listings, db.properties) == before
    assert [r["undone_at"] for r in db.ledger
            if r["generation"] == GEN and r["cluster_key"] == 10 and r["outcome"] == "applied"
            ] == [None]

    A.unapply(db, "g13", dry_run=False, unmerge=db.unmerge([]))
    again = A.unapply(db, GEN, dry_run=False, cluster_key=10, unmerge=db.unmerge([]))
    assert again["counts"]["undone"] == 1 and again["counts"]["blocked"] == 0
    assert db.listings[11]["property_id"] == 200


def test_unapply_never_calls_undone_a_merge_that_would_move_nothing_back() -> None:
    db = FakeDb()
    _applied_two(db)
    db.prop(999)
    db.listing(11, 999)  # moved on since the merge (an operator split, say)
    before = copy.deepcopy((db.listings, db.properties))
    result = A.unapply(db, GEN, dry_run=False, cluster_key=10, unmerge=db.unmerge([]))
    assert result["counts"]["blocked"] == 1 and result["counts"]["undone"] == 0
    assert result["groups"][0]["conflicts"] == [11]
    assert (db.listings, db.properties) == before  # rolled back: 200 stays merged away
    assert all(r["undone_at"] is None for r in db.ledger if r["outcome"] == "applied")


def test_a_merge_restored_outside_the_engine_is_never_re_merged() -> None:
    db = FakeDb()
    scope, calls = _applied_two(db)
    db.unmerge([])(db, merge_group_id=calls[0]["merge_group_id"], undone_by="operator")
    reasons = {g.cluster_key: g.reasons for g in A.plan_apply(db, GEN, scope).groups}
    assert reasons == {10: [A.SKIP_RESTORED_ELSEWHERE]}


def test_an_operator_undo_survives_the_restored_property_being_merged_on() -> None:
    # g12 merged 200 (11) into 100 (10). The operator undid it, then merged 200 into an older
    # 500 holding 11's true duplicate 50. g13 groups {10, 11, 50} on 100 and 500: 200 is no
    # longer among its properties, but 10 and 11 are the listings the operator separated.
    db = FakeDb()
    scope, calls = _applied_two(db)
    db.unmerge([])(db, merge_group_id=calls[0]["merge_group_id"], undone_by="operator")
    db.prop(500, first=T0 - timedelta(days=30))
    db.listing(50, 500)
    db.merge([])(db, survivor_id=500, retired_id=200, reason="operator", source="operator")
    db.group(10, [10, 11, 50], gen="g13")
    group = _only(A.plan_apply(db, "g13", scope))
    assert group.property_ids == [100, 500]
    assert group.reasons == [A.SKIP_RESTORED_ELSEWHERE]
    assert group.detail["separated_engine_merges"] == [{
        "generation": GEN, "cluster_key": 10, "merge_group_id": calls[0]["merge_group_id"],
        "listings": [10, 11]}]
    # ... and still once `unapply` has noted that undo as someone else's.
    A.unapply(db, GEN, dry_run=False, unmerge=db.unmerge([]))
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
    result = A.unapply(db, GEN, dry_run=False, cluster_key=10, unmerge=db.unmerge([]))
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
    result = A.unapply(db, GEN, dry_run=False, unmerge=db.unmerge([]))
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
    live = A.unapply(db, GEN, dry_run=False, unmerge=db.unmerge([]))
    assert live["counts"]["blocked"] == 1 and live["counts"]["undone"] == 0
    assert "undo the later engine merge first" in live["groups"][0]["blocked"]
    assert (db.listings, db.properties) == before

    A.unapply(db, "g13", dry_run=False, unmerge=db.unmerge([]))
    again = A.unapply(db, GEN, dry_run=False, unmerge=db.unmerge([]))
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


def test_a_whole_generation_unapply_keeps_every_group_of_it_out_until_reapply() -> None:
    # The first live run applies 10 and defers 20 and 30 under the cap. `unapply` of the whole
    # generation stamps it: NO group re-applies — not the one it undid, not the two it never
    # reached — until an apply dispatched with reapply=1 releases the stamp.
    db = FakeDb()
    db.live_scope(max_clusters_per_run=1)
    for key in (10, 20, 30):
        _pair_group(db, key, [key, key + 1], [key * 10, key * 10 + 1])
    scope = A.effective_scope(db.settings[A.SCOPE_SETTING], {}, live=True)
    first = A.apply_plan(db, A.plan_apply(db, GEN, scope), dry_run=False, merge=db.merge([]))
    assert [a["cluster_key"] for a in first["applied"]] == [10]
    assert first["deferred"] == [20, 30]
    assert A.unapply(db, GEN, dry_run=True)["generation_stamp"] == "would be written"
    assert db.unapplied == []
    undo = A.unapply(db, GEN, dry_run=False, unmerge=db.unmerge([]))
    assert undo["counts"]["undone"] == 1 and undo["generation_stamp"] == "written"

    replan = A.plan_apply(db, GEN, scope)
    assert {g.cluster_key: g.reasons for g in replan.groups} == {
        10: [A.SKIP_GENERATION_UNAPPLIED], 20: [A.SKIP_GENERATION_UNAPPLIED],
        30: [A.SKIP_GENERATION_UNAPPLIED]}
    assert replan.deferred == [] and replan.groups[1].detail["generation_unapplied"][
        "release"] == "reapply=1"
    calls: list[dict[str, Any]] = []
    blocked = A.apply_plan(db, replan, dry_run=False, merge=db.merge(calls))
    assert calls == [] and blocked["counts"]["applied"] == 0
    assert "reapply=1" in A.summary_markdown(blocked, mode="apply")

    # reapply=1 in a dry run previews the release and writes none of it ...
    preview = A.plan_apply(db, GEN, scope, reapply=True)
    assert [g.cluster_key for g in preview.to_apply] == [10] and preview.deferred == [20, 30]
    A.apply_plan(db, preview, dry_run=True)
    assert [u["released_at"] for u in db.unapplied] == [None]
    # ... and a live one releases it for this run and every later one.
    released = A.apply_plan(db, preview, dry_run=False, merge=db.merge(calls))
    assert released["released_unapply"] == [1] and db.unapplied[0]["released_at"] is not None
    assert [c["retired_id"] for c in calls] == [101]
    assert [g.cluster_key for g in A.plan_apply(db, GEN, scope).to_apply] == [20]


def test_a_one_group_unapply_writes_no_generation_stamp() -> None:
    db = FakeDb()
    scope, _calls = _applied_two(db)
    _pair_group(db, 30, [30, 31], [500, 600])
    one = A.unapply(db, GEN, dry_run=False, cluster_key=10, unmerge=db.unmerge([]))
    assert one["counts"]["undone"] == 1 and one["generation_stamp"] is None
    assert db.unapplied == []
    reasons = {g.cluster_key: g.reasons for g in A.plan_apply(db, GEN, scope).groups}
    assert reasons == {10: [A.SKIP_GENERATION_UNAPPLIED], 30: []}
    # reapply releases a whole-generation stamp only: the one group undone stays undone.
    again = {g.cluster_key: g.reasons for g in A.plan_apply(db, GEN, scope, reapply=True).groups}
    assert again == reasons


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


def test_the_lane_refuses_live_without_the_switch_or_the_scope(tmp_path: Path) -> None:
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    # The switch is read first; the scope decides only once the switch is on.
    db.settings[A.SCOPE_SETTING] = {"category_types": ["prodej"], "all_blocks": True}
    with pytest.raises(SystemExit, match=A.ENABLED_SETTING):
        A.run_apply(_factory(db), {"generation": GEN, "dry_run": "0"}, tmp_path)
    db.settings[A.ENABLED_SETTING] = True
    del db.settings[A.SCOPE_SETTING]
    with pytest.raises(SystemExit, match="scope"):
        A.run_apply(_factory(db), {"generation": GEN, "dry_run": "0"}, tmp_path)
    # A run's args cannot supply the area the operator's setting does not name.
    db.settings[A.SCOPE_SETTING] = {"category_types": ["prodej"]}
    with pytest.raises(SystemExit, match="area"):
        A.run_apply(_factory(db), {"generation": GEN, "dry_run": "0",
                                   "blocks": "obec:563510"}, tmp_path)
    # An empty dry_run= is a dry run, never a live one.
    assert A.run_apply(_factory(db), {"generation": GEN, "dry_run": ""}, tmp_path)["dry_run"]
    db.ledger.clear()
    with pytest.raises(SystemExit, match="unknown arg"):
        A.run_apply(_factory(db), {"generation": GEN, "scope": "x"}, tmp_path)
    with pytest.raises(SystemExit, match="generation"):
        A.run_apply(_factory(db), {}, tmp_path)
    assert db.ledger == []


def test_the_lane_applies_live_when_switched_on(tmp_path: Path, monkeypatch: Any) -> None:
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

    def merge_or_crash(conn: FakeDb, **kw: Any) -> dict[str, Any]:
        if kw["retired_id"] == 400:
            raise RuntimeError("canceling statement due to statement timeout")
        return base(conn, **kw)

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

    def merge_or_cancel(conn: FakeDb, **kw: Any) -> dict[str, Any]:
        if kw["retired_id"] == 201:
            raise KeyboardInterrupt()
        return base(conn, **kw)

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
    undo = db.unmerge([])
    seen: list[str] = []

    def unmerge_or_crash(conn: FakeDb, **kw: Any) -> dict[str, Any]:
        seen.append(kw["merge_group_id"])
        if len(seen) == 2:
            raise RuntimeError("canceling statement due to statement timeout")
        return undo(conn, **kw)

    original = A.unapply
    monkeypatch.setattr(A, "unapply", lambda conn, gen, *, dry_run, cluster_key=None: original(
        conn, gen, dry_run=dry_run, cluster_key=cluster_key, unmerge=unmerge_or_crash))
    page = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    with pytest.raises(RuntimeError, match="statement timeout"):
        A.run_unapply(_factory(db), {"generation": GEN, "dry_run": "0"}, tmp_path)
    body = json.loads((tmp_path / "unapply.json").read_text())
    assert [(g["cluster_key"], g["outcome"]) for g in body["groups"]] == [
        (20, "undone"), (10, "aborted")]
    assert body["counts"]["undone"] == 1 and body["generation_stamp"] == "written"
    assert body["aborted"].startswith("stopped at group 10: RuntimeError")
    text = page.read_text()
    assert "**Aborted:**" in text and "WERE undone" in text and db.closed


def test_an_empty_cluster_key_never_widens_unapply_to_the_generation(tmp_path: Path) -> None:
    db = FakeDb()
    _applied_two(db)
    with pytest.raises(SystemExit, match="cluster_key= is empty"):
        A.run_unapply(_factory(db), lane.parse_kv_args(
            f"generation={GEN},dry_run=0,cluster_key="), tmp_path)
    assert db.unapplied == [] and all(r["undone_at"] is None for r in db.ledger)


def test_the_lane_run_summary_is_a_lane_summary(tmp_path: Path) -> None:
    db = FakeDb()
    _pair_group(db, 10, [10, 11], [100, 200])
    code = lane.run("apply", f"generation={GEN}", tmp_path, conn_factory=_factory(db))
    assert code == 0
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["ok"] and summary["result"]["counts"]["planned"] == 1


def test_the_seeded_switches_leave_a_live_lane_run_inert(tmp_path: Path) -> None:
    # Migration 558's seed, in shape: OFF, and a scope with a deal type but no area.
    db = FakeDb()
    db.settings[A.ENABLED_SETTING] = False
    db.settings[A.SCOPE_SETTING] = {
        "category_types": ["prodej"], "blocks": None, "listing_ids": None,
        "all_blocks": False, "max_cluster_size": 8, "max_clusters_per_run": 200}
    _pair_group(db, 10, [10, 11], [100, 200])
    before = copy.deepcopy((db.listings, db.properties, db.events))
    with pytest.raises(SystemExit, match=A.ENABLED_SETTING):
        A.run_apply(_factory(db), {"generation": GEN, "dry_run": "0"}, tmp_path)
    db.settings[A.ENABLED_SETTING] = True
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
