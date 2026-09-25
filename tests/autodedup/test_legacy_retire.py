"""A2 (temporary, deleted in W5): the old engine's merges undone in the apply scope's blocks.

Over test_apply's stateful fake (`FakeDb`), extended with the area (`listing_location`) and the
three reads the retire step runs. Its detach mimics the toolkit's contract (`_detach_plan`,
`_origin_gone`) and records every call; the one test over the REAL `detach_listing` shows the
arguments the step passes write no ruling.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from autodedup import apply as A
from autodedup import apply_sql as S
from autodedup import legacy_retire as L
from tests.autodedup.test_apply import GEN, FakeDb
from tests.test_detach_listing import _Ledger
from toolkit.property_identity import (
    _detach_plan,
    _origin_gone,
    detach_listing,
    merge_property_set,
)

TOWN, QUARTER = 563510, 490245
TRIAL = frozenset({f"town:{TOWN}", f"quarter:{QUARTER}"})
IN_TOWN = (TOWN, None)
IN_QUARTER = (554782, QUARTER)
OUT = (999999, None)
STAMP = f"{L.RETIRE_BY}:r1"


class RetireDb(FakeDb):
    def __init__(self) -> None:
        super().__init__()
        self.location: dict[int, tuple[int | None, int | None]] = {}

    def advert(self, lid: int, pid: int, where: tuple[int | None, int | None] = IN_TOWN,
               ct: str = "prodej") -> None:
        if pid not in self.properties:
            self.prop(pid, ct=ct)
        self.listing(lid, pid, ct=ct)
        self.location[lid] = where

    def merged(self, survivor: int, *retired: int, source: str = "auto") -> str:
        """History: one merge group, an hour after the previous one."""
        self.now += timedelta(hours=1)
        group = str(uuid.uuid4())
        for rid in retired:
            start = len(self.events)
            self.merge_pair(survivor, rid, group=group, source=source)
            for event in self.events[start:]:
                event.update(created_at=self.now, undone_by=None)
        return group

    def inside(self, lid: int, p: dict[str, Any]) -> bool:
        town, quarter = self.location.get(lid, (None, None))
        return town in p["towns"] or quarter in p["quarters"]

    def dispatch(self, sql: str, p: dict[str, Any]) -> list[tuple]:
        if sql == L.GROUPS_SQL:
            area_props = {r["property_id"] for lid, r in self.listings.items()
                          if self.inside(lid, p)} - {None}
            touching = {e["merge_group_id"] for e in self.events
                        if not e["undone"] and e["source"] == "auto"
                        and p["merge_group_id"] in (None, e["merge_group_id"])
                        and (self.inside(e["listing"], p) or e["survivor"] in area_props
                             or e["retired"] in area_props)}
            out = []
            for e in sorted(self.events, key=lambda e: (e["merge_group_id"], e["id"])):
                if e["undone"] or e["merge_group_id"] not in touching:
                    continue
                row = self.listings.get(e["listing"])
                out.append((e["id"], e["merge_group_id"], e["source"], e["created_at"],
                            e["survivor"], e["retired"], e["listing"], row is not None,
                            row["property_id"] if row else None, self.inside(e["listing"], p)))
            return out
        if sql == L.ADVERTS_SQL:
            return [(r["property_id"], lid, r["category_type"], self.inside(lid, p))
                    for lid, r in sorted(self.listings.items())
                    if r["property_id"] in p["property_ids"]]
        if sql == L.BUILT_ON_SQL:
            last: dict[tuple[int, str], Any] = {}
            for e in self.events:
                if e["undone"] or e["source"] == "auto" or e["survivor"] not in p["property_ids"]:
                    continue
                key, at = (e["survivor"], e["source"]), e.get("created_at", self.now)
                last[key] = max(last.get(key, at), at)
            return [(pid, src, at) for (pid, src), at in sorted(last.items())]
        return super().dispatch(sql, p)

    def detach_recording(self, calls: list[dict[str, Any]]) -> Any:
        """The toolkit's per-advert detach, faked, stamping `undone_by` and writing a ruling
        only for the operator (as `detach_listing` does)."""
        def detach(conn: "RetireDb", listing_id: int, *, decided_by: str,
                   reason: str | None = None, source: str = "operator",
                   merge_group_id: str | None = None) -> dict[str, Any]:
            calls.append({"listing_id": listing_id, "decided_by": decided_by, "source": source,
                          "merge_group_id": merge_group_id})
            reactivated = False
            with conn.transaction():
                current = conn.listings[listing_id]["property_id"]
                moves = [(e["id"], e["merge_group_id"], e["survivor"], e["retired"])
                         for e in conn.events if e["listing"] == listing_id and not e["undone"]]
                outcome, undo, target = _detach_plan(current, moves, merge_group_id)
                if outcome == "detached" and _origin_gone(
                        (conn.properties[target]["status"],
                         conn.properties[target]["merged_into"]), undo):
                    outcome = "origin_moved_on"
                if outcome == "detached":
                    conn.listings[listing_id]["property_id"] = target
                    for e in conn.events:
                        if e["id"] in {m[0] for m in undo}:
                            e.update(undone=True, undone_by=decided_by)
                    back = conn.properties[target]
                    if back["status"] == "merged_away":
                        back.update(status="active", merged_into=None, merged_at=None)
                        reactivated = True
                    if source == "operator":
                        conn.verdicts.append({"kind": "pair", "verdict": "different"})
            return {"data": {"detached": outcome == "detached", "outcome": outcome,
                             "reactivated": reactivated}}
        return detach


def _intact_pair(db: RetireDb, survivor: int, retired: int, lid: int,
                 where: tuple[int | None, int | None] = IN_TOWN) -> str:
    db.advert(lid, survivor, where)
    db.advert(lid + 1, retired, where)
    return db.merged(survivor, retired)


def _world() -> tuple[RetireDb, dict[str, str]]:
    """One legacy group of every class the probe names, plus three the step never reads."""
    db = RetireDb()
    g: dict[str, str] = {}
    db.advert(1, 100)
    db.advert(2, 200, IN_QUARTER)                     # both grains of the area
    g["intact"] = db.merged(100, 200)
    db.advert(3, 300)
    db.advert(4, 400, OUT)
    db.advert(5, 400)
    g["moved_straddle"] = db.merged(300, 400)          # the probe's R1 'straddles'
    db.advert(6, 350)
    db.advert(7, 350, OUT)
    db.advert(8, 450)
    g["survivor_straddle"] = db.merged(350, 450)       # R1 'inside', one survivor advert out
    db.advert(9, 500)
    db.advert(10, 600, OUT)
    g["survivor_side_only"] = db.merged(500, 600)      # not in the probe's count at all
    g["survivor_merged_on"] = _intact_pair(db, 700, 800, 11)
    db.advert(13, 900)
    db.merged(900, 700, source="operator")
    g["children_moved"] = _intact_pair(db, 1000, 1100, 14)
    db.prop(1200)
    db.listings[15]["property_id"] = 1200            # re-pointed outside the ledger
    g["built_on"] = _intact_pair(db, 1300, 1400, 16)
    db.advert(18, 1500)
    db.merged(1300, 1500, source="operator")
    g["listing_gone"] = _intact_pair(db, 1600, 1700, 19)
    del db.listings[20]
    # never read: an operator merge inside, an undone legacy merge, a legacy merge outside
    db.advert(21, 2000)
    db.advert(22, 2100)
    db.merged(2000, 2100, source="operator")
    db.undo_group(_intact_pair(db, 2200, 2300, 23))
    _intact_pair(db, 2400, 2500, 25, OUT)
    return db, g


def test_selection_reads_every_class_the_probe_names_and_retires_only_both_sides_inside() -> None:
    db, g = _world()
    groups = L.read_groups(db, L.area_params(TRIAL))
    outcome = {x.merge_group_id: x.outcome for x in groups}
    assert outcome == {
        g["intact"]: L.TO_RETIRE,
        g["moved_straddle"]: "skipped:straddling",
        g["survivor_straddle"]: "skipped:straddling",
        g["survivor_side_only"]: "skipped:straddling",
        g["survivor_merged_on"]: "skipped:survivor_merged_on",
        g["children_moved"]: "skipped:children_moved",
        g["built_on"]: "skipped:built_on",
        g["listing_gone"]: "skipped:listing_gone",
    }
    # newest first
    assert [x.merge_group_id for x in groups] == [
        g[k] for k in ("listing_gone", "built_on", "children_moved", "survivor_merged_on",
                       "survivor_side_only", "survivor_straddle", "moved_straddle", "intact")]
    assert next(x for x in groups if x.merge_group_id == g["survivor_straddle"]).outside == [7]


def test_the_dry_run_counts_reproduce_the_probe_and_write_nothing() -> None:
    db, g = _world()
    before = db.state()
    calls: list[dict[str, Any]] = []
    out = L.retire_legacy(db, TRIAL, dry_run=True, run_id="r1",
                          detach=db.detach_recording(calls))
    assert calls == [] and db.state() == before
    assert set(db.statements) == {L.GROUPS_SQL, S.PROPERTY_STATE_SQL, L.ADVERTS_SQL,
                                  L.BUILT_ON_SQL}
    counts = out["counts"]
    assert counts["groups_touching"] == 8
    assert counts["touching_only_through_survivor_side"] == 1
    assert counts["probe_r1c_by_health"] == {"built_on": 1, "children_moved": 1, "intact": 3,
                                             "listing_gone": 1, "survivor_merged_on": 1}
    assert counts["probe_r1_area"] == {"inside": 6, "straddles": 1}
    assert counts["intact_both_sides_inside"] == 1 and counts["intact_straddling"] == 3
    assert counts["both_sides_inside_by_category_type"] == {"prodej": 1}
    assert counts["outcomes"]["would_retire"] == 1
    assert [x["merge_group_id"] for x in out["groups"] if x["outcome"] == "would_retire"] \
        == [g["intact"]]
    assert out["undone_by"] == STAMP


def test_the_live_run_detaches_newest_first_in_ledger_order_and_writes_no_ruling() -> None:
    db = RetireDb()
    older = _intact_pair(db, 100, 200, 1)
    db.advert(10, 300)
    db.advert(12, 520)
    db.advert(11, 510)
    newer = db.merged(300, 520, 510)                   # ledger order 12, then 11
    calls: list[dict[str, Any]] = []
    out = L.retire_legacy(db, TRIAL, dry_run=False, run_id="r1",
                          detach=db.detach_recording(calls))
    assert [(c["listing_id"], c["merge_group_id"]) for c in calls] == [
        (12, newer), (11, newer), (2, older)]
    assert {(c["decided_by"], c["source"]) for c in calls} == {(STAMP, L.LEGACY_SOURCE)}
    assert db.verdicts == []
    assert {lid: db.listings[lid]["property_id"] for lid in (1, 2, 10, 11, 12)} == {
        1: 100, 2: 200, 10: 300, 11: 510, 12: 520}
    assert all(db.properties[p]["status"] == "active" for p in (100, 200, 300, 510, 520))
    assert {e["undone_by"] for e in db.events} == {STAMP}
    assert out["counts"]["outcomes"] == {"retired": 2}
    assert out["counts"]["listings_moved_back"] == 3
    assert out["counts"]["properties_reactivated"] == 3


def test_the_arguments_it_passes_write_no_ruling_through_the_real_detach() -> None:
    db = _Ledger({1: 10, 2: 20})
    group = merge_property_set(db, [10, 20], source="auto", reason="legacy")["data"][
        "merge_group_id"]
    out = detach_listing(db, 2, decided_by=STAMP, source=L.LEGACY_SOURCE,
                         merge_group_id=group)["data"]
    assert out["detached"] and out["rulings_written"] == 0
    assert db.sql("autodedup.verdicts") == [] and db.sql("autodedup.must_not_link") == []
    assert [e["undone_by"] for e in db.events] == [STAMP]


def test_a_group_whose_detach_refuses_rolls_back_whole_and_the_run_goes_on() -> None:
    db = RetireDb()
    older = _intact_pair(db, 100, 200, 1)
    db.advert(10, 300)
    db.advert(11, 400)
    db.advert(12, 400)
    newer = db.merged(300, 400)
    calls: list[dict[str, Any]] = []
    base = db.detach_recording(calls)

    def detach(conn: RetireDb, lid: int, **kw: Any) -> dict[str, Any]:
        if lid == 12:
            return {"data": {"detached": False, "outcome": "moved_since"}}
        return base(conn, lid, **kw)

    out = L.retire_legacy(db, TRIAL, dry_run=False, run_id="r1", detach=detach)
    by = {x["merge_group_id"]: x["outcome"] for x in out["groups"]}
    assert by == {newer: "refused:moved_since", older: "retired"}
    assert db.listings[11]["property_id"] == 300, "the advert detached first is rolled back"
    assert db.properties[400]["status"] == "merged_away"
    assert not any(e["undone"] for e in db.events if e["merge_group_id"] == newer)


def test_a_group_that_changed_since_selection_is_refused_over_a_fresh_read() -> None:
    db = RetireDb()
    older = _intact_pair(db, 100, 200, 1)
    newer = _intact_pair(db, 300, 400, 3)
    db.prop(900)
    calls: list[dict[str, Any]] = []
    base = db.detach_recording(calls)

    def detach(conn: RetireDb, lid: int, **kw: Any) -> dict[str, Any]:
        conn.listings[2]["property_id"] = 900          # the older group's advert moves away
        return base(conn, lid, **kw)

    out = L.retire_legacy(db, TRIAL, dry_run=False, run_id="r1", detach=detach)
    by = {x["merge_group_id"]: x["outcome"] for x in out["groups"]}
    assert by == {newer: "retired", older: "refused:children_moved"}
    assert [c["merge_group_id"] for c in calls] == [newer]
    assert S.LOCK_PROPERTIES_SQL in db.statements


def test_a_group_the_operator_split_in_part_since_selection_is_left_to_them() -> None:
    db = RetireDb()
    db.advert(1, 100)
    db.advert(2, 200)
    db.advert(3, 200)
    older = db.merged(100, 200)
    newer = _intact_pair(db, 300, 400, 5)
    operator = db.detach([])
    calls: list[dict[str, Any]] = []
    base = db.detach_recording(calls)

    def detach(conn: RetireDb, lid: int, **kw: Any) -> dict[str, Any]:
        operator(conn, 3, decided_by="operator", merge_group_id=older)
        return base(conn, lid, **kw)

    out = L.retire_legacy(db, TRIAL, dry_run=False, run_id="r1", detach=detach)
    by = {x["merge_group_id"]: x["outcome"] for x in out["groups"]}
    assert by == {newer: "retired", older: "refused:changed_since_selection"}
    assert db.listings[2]["property_id"] == 100 and db.listings[3]["property_id"] == 200


def test_emptying_the_scope_stops_the_run_between_two_groups() -> None:
    db = RetireDb()
    older = _intact_pair(db, 100, 200, 1)
    newer = _intact_pair(db, 300, 400, 3)
    answers = iter([None, "the live scope names no area"])
    out = L.retire_legacy(db, TRIAL, dry_run=False, run_id="r1",
                          detach=db.detach_recording([]), closed=lambda _c: next(answers))
    by = {x["merge_group_id"]: x["outcome"] for x in out["groups"]}
    assert by == {newer: "retired", older: "not_attempted"}
    assert "names no area" in out["stopped"]


def test_a_crash_publishes_what_was_undone_and_re_raises(tmp_path: Path, monkeypatch: Any) -> None:
    db = RetireDb()
    older = _intact_pair(db, 100, 200, 1)
    middle = _intact_pair(db, 300, 400, 3)
    newer = _intact_pair(db, 500, 600, 5)
    base = db.detach_recording([])

    def detach(conn: RetireDb, lid: int, **kw: Any) -> dict[str, Any]:
        if lid == 4:
            raise RuntimeError("canceling statement due to statement timeout")
        return base(conn, lid, **kw)

    monkeypatch.setattr(L, "detach_listing", detach)
    page = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    with pytest.raises(RuntimeError, match="statement timeout"):
        L.run(db, TRIAL, dry_run=False, run_id="r1", out_dir=tmp_path)
    body = json.loads((tmp_path / "apply" / "legacy_retire.json").read_text())
    by = {x["merge_group_id"]: x["outcome"] for x in body["groups"]}
    assert by == {newer: "retired", middle: "failed", older: "not_attempted"}
    assert "statement timeout" in body["aborted"]
    assert db.listings[4]["property_id"] == 300 and db.listings[6]["property_id"] == 600
    assert "**Aborted:**" in page.read_text()


def test_the_step_needs_blocks() -> None:
    with pytest.raises(ValueError, match="no blocks"):
        L.area_params(None)
    with pytest.raises(ValueError, match="town:<code>"):
        L.area_params(["obec:1"])
    assert L.area_params(TRIAL) == {"towns": [TOWN], "quarters": [QUARTER]}


# ------------------------------------------------------------------ inside the apply mode


def _factory(db: RetireDb) -> Any:
    return lambda: db


def test_the_apply_mode_runs_it_only_when_asked_and_reports_it(
        tmp_path: Path, monkeypatch: Any) -> None:
    db, _g = _world()
    db.live_scope(blocks=sorted(TRIAL))
    A.run_apply(_factory(db), {"generation": GEN}, tmp_path)
    assert L.GROUPS_SQL not in db.statements and not (tmp_path / "apply").exists()

    page = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    out = A.run_apply(_factory(db), {"generation": GEN, "retire_legacy": "1"}, tmp_path)
    assert out["dry_run"] is True
    assert out["legacy_retire"]["counts"]["intact_both_sides_inside"] == 1
    assert out["legacy_retire"]["undone_by"].startswith(f"{L.RETIRE_BY}:")
    body = json.loads((tmp_path / "apply" / "legacy_retire.json").read_text())
    assert len(body["groups"]) == 8
    assert json.loads((tmp_path / "apply.json").read_text())["legacy_retire"]["counts"] \
        == out["legacy_retire"]["counts"]
    assert "legacy retire (A2, temporary) - DRY RUN" in page.read_text()

    with pytest.raises(SystemExit, match="retire_legacy"):
        A.run_apply(_factory(db), {"generation": GEN, "retire_legacy": "maybe"}, tmp_path)
    db.settings.clear()
    with pytest.raises(SystemExit, match="no blocks"):
        A.run_apply(_factory(db), {"generation": GEN, "retire_legacy": "1"}, tmp_path)


def test_live_the_old_merge_is_undone_before_the_plan_reads_the_area(
        tmp_path: Path, monkeypatch: Any) -> None:
    db = RetireDb()
    db.live_scope(blocks=sorted(TRIAL))
    _intact_pair(db, 100, 200, 1)                      # adverts 1 and 2 on property 100
    db.group(10, [1, 2])                               # the engine groups the same two
    detached: list[dict[str, Any]] = []
    monkeypatch.setattr(L, "detach_listing", db.detach_recording(detached))
    merged: list[dict[str, Any]] = []
    original = A.apply_plan
    monkeypatch.setattr(A, "apply_plan",
                        lambda conn, plan, dry_run: original(conn, plan, dry_run,
                                                             merge=db.merge(merged)))
    dry = A.run_apply(_factory(db), {"generation": GEN, "retire_legacy": "1"}, tmp_path)
    assert dry["counts"]["already_one_property"] == 1 and detached == []

    out = A.run_apply(_factory(db), {"generation": GEN, "retire_legacy": "1",
                                     "dry_run": "0"}, tmp_path)
    assert out["legacy_retire"]["counts"]["outcomes"] == {"retired": 1}
    assert out["counts"]["applied"] == 1
    assert [(m["survivor_id"], m["retired_id"], m["source"]) for m in merged] == [
        (100, 200, "autodedup")]
    sources = [e["source"] for e in db.events]
    assert sources == ["auto", "autodedup"] and db.events[0]["undone"]
    assert db.events[0]["undone_by"].startswith(f"{L.RETIRE_BY}:")
    assert db.listings[2]["property_id"] == 100
