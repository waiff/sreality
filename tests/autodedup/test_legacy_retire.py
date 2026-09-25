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
SALES = frozenset({"prodej"})


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
        """Every read after AREA_SQL takes the area as the listing ids it read."""
        return lid in set(p["area_ids"])

    def dispatch(self, sql: str, p: dict[str, Any]) -> list[tuple]:
        if sql == L.AREA_SQL:
            return [(lid,) for lid, (town, quarter) in sorted(self.location.items())
                    if town in p["towns"] or quarter in p["quarters"]]
        if sql == L.GROUPS_SQL:
            # Driven from the area: its adverts' own ledger rows, and the rows whose SURVIVOR
            # holds one of them (never the retired side: `retired_property_id` is unindexed).
            area_props = {self.listings[lid]["property_id"] for lid in p["area_ids"]
                          if lid in self.listings} - {None}
            touching = {e["merge_group_id"] for e in self.events
                        if not e["undone"] and e["source"] == "auto"
                        and p["merge_group_id"] in (None, e["merge_group_id"])
                        and (self.inside(e["listing"], p) or e["survivor"] in area_props)}
            out = []
            for e in sorted(self.events, key=lambda e: (e["merge_group_id"], e["id"])):
                if e["merge_group_id"] not in touching:
                    continue
                row = self.listings.get(e["listing"])
                out.append((e["id"], e["merge_group_id"], e["source"], e["created_at"],
                            e["survivor"], e["retired"], e["listing"], row is not None,
                            row["property_id"] if row else None, self.inside(e["listing"], p),
                            e["undone"], e.get("undone_by")))
            return out
        if sql == L.AUTO_MOVED_SQL:
            return sorted({(e["listing"],) for e in self.events if not e["undone"]
                           and e["source"] == "auto" and e["listing"] in p["listing_ids"]})
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
    # deal types: a rental pair (outside the scope's `prodej`) and a sale merged with a rental
    db.advert(27, 2600, ct="pronajem")
    db.advert(28, 2700, ct="pronajem")
    g["rental"] = db.merged(2600, 2700)
    g["mixed"] = _mixed(db, 2800, 2900, 29)
    return db, g


def _mixed(db: RetireDb, survivor: int, retired: int, lid: int,
           where: tuple[int | None, int | None] = IN_TOWN) -> str:
    """A sale and a rental on one property (the retired record's own type unknown, so the
    chokepoint let it through): the old engine's wrong-by-construction merge."""
    db.advert(lid, survivor, where)
    db.prop(retired, ct=None)
    db.listing(lid + 1, retired, ct="pronajem")
    db.location[lid + 1] = where
    return db.merged(survivor, retired)


def test_selection_reads_every_class_the_probe_names_and_retires_only_both_sides_inside() -> None:
    db, g = _world()
    groups = L.read_groups(db, L.read_area(db, TRIAL), SALES)
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
        g["rental"]: "skipped:outside_scope_categories",
        g["mixed"]: "to_retire:mixed_deal_type",
    }
    # newest first
    assert [x.merge_group_id for x in groups] == [
        g[k] for k in ("mixed", "rental", "listing_gone", "built_on", "children_moved",
                       "survivor_merged_on",
                       "survivor_side_only", "survivor_straddle", "moved_straddle", "intact")]
    assert next(x for x in groups if x.merge_group_id == g["survivor_straddle"]).outside == [7]


def test_the_dry_run_counts_reproduce_the_probe_and_write_nothing() -> None:
    db, g = _world()
    before = db.state()
    calls: list[dict[str, Any]] = []
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=True, run_id="r1",
                          detach=db.detach_recording(calls))
    assert calls == [] and db.state() == before
    assert set(db.statements) == {L.AREA_SQL, L.GROUPS_SQL, S.PROPERTY_STATE_SQL, L.ADVERTS_SQL,
                                  L.BUILT_ON_SQL, S.PAIR_VERDICTS_SQL, S.MUST_NOT_LINK_SQL,
                                  L.AUTO_MOVED_SQL}
    counts = out["counts"]
    assert counts["groups_touching"] == 10
    assert counts["touching_only_through_survivor_side"] == 1
    # block-only, every deal type: the probe's grain (672 intact in the trial)
    assert counts["probe_r1c_by_health"] == {"built_on": 1, "children_moved": 1, "intact": 5,
                                             "listing_gone": 1, "survivor_merged_on": 1}
    assert counts["probe_r1_area"] == {"inside": 8, "straddles": 1}
    assert counts["intact_both_sides_inside"] == 3 and counts["intact_straddling"] == 3
    assert counts["intact_both_sides_inside_by_category_type"] == {
        "mixed": 1, "pronajem": 1, "prodej": 1}
    # ... and the set the scope's deal types retire
    assert counts["retire_set"] == 2 and counts["retire_set_mixed_deal_type"] == 1
    assert counts["outside_scope_categories_by_category_type"] == {"pronajem": 1}
    assert {k: v for k, v in counts["outcomes"].items() if not k.startswith("skipped:")} == {
        "would_retire": 1, "would_retire:mixed_deal_type": 1}
    assert counts["outcomes"]["skipped:outside_scope_categories"] == 1
    assert {x["merge_group_id"]: x["outcome"] for x in out["groups"]
            if x["outcome"].startswith("would_retire")} == {
        g["intact"]: "would_retire", g["mixed"]: "would_retire:mixed_deal_type"}
    assert out["undone_by"] == STAMP and out["category_types"] == ["prodej"]


def test_the_live_run_detaches_newest_first_in_ledger_order_and_writes_no_ruling() -> None:
    db = RetireDb()
    older = _intact_pair(db, 100, 200, 1)
    db.advert(10, 300)
    db.advert(12, 520)
    db.advert(11, 510)
    newer = db.merged(300, 520, 510)                   # ledger order 12, then 11
    calls: list[dict[str, Any]] = []
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
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

    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
                          detach=detach)
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

    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
                          detach=detach)
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

    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
                          detach=detach)
    by = {x["merge_group_id"]: x["outcome"] for x in out["groups"]}
    assert by == {newer: "retired", older: "refused:operator_split"}
    assert db.listings[2]["property_id"] == 100 and db.listings[3]["property_id"] == 200


def test_emptying_the_scope_stops_the_run_between_two_groups() -> None:
    db = RetireDb()
    older = _intact_pair(db, 100, 200, 1)
    newer = _intact_pair(db, 300, 400, 3)
    answers = iter([None, "the live scope names no area"])
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
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
        L.run(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1", out_dir=tmp_path)
    body = json.loads((tmp_path / "apply" / "legacy_retire.json").read_text())
    by = {x["merge_group_id"]: x["outcome"] for x in body["groups"]}
    assert by == {newer: "retired", middle: "failed", older: "not_attempted"}
    assert "statement timeout" in body["aborted"]
    assert db.listings[4]["property_id"] == 300 and db.listings[6]["property_id"] == 600
    assert "**Aborted:**" in page.read_text()


def test_deal_types_decide_all_but_a_group_that_mixes_them() -> None:
    db = RetireDb()
    sale = _intact_pair(db, 100, 200, 1)
    db.advert(3, 300, ct="pronajem")
    db.advert(4, 400, ct="pronajem")
    rental = db.merged(300, 400)
    db.advert(5, 500)
    db.prop(600, ct=None)
    db.listing(6, 600, ct=None)                        # a sale beside an advert of no type
    db.location[6] = IN_TOWN
    untyped = db.merged(500, 600)
    mixed = _mixed(db, 700, 800, 7)
    mixed_straddling = _mixed(db, 900, 1000, 9)
    db.location[10] = OUT                              # its rental sits outside the blocks
    by = {x.merge_group_id: x.outcome
          for x in L.read_groups(db, L.read_area(db, TRIAL), SALES)}
    assert by == {sale: L.TO_RETIRE, rental: "skipped:outside_scope_categories",
                  untyped: "skipped:outside_scope_categories",
                  mixed: "to_retire:mixed_deal_type", mixed_straddling: "skipped:straddling"}
    # a scope naming no deal types admits every one, as `apply.Scope` reads it
    every = {x.merge_group_id: x.outcome for x in L.read_groups(db, L.read_area(db, TRIAL), None)}
    assert every[rental] == every[untyped] == L.TO_RETIRE

    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
                          detach=db.detach_recording([]))
    done = {x["merge_group_id"]: x["outcome"] for x in out["groups"]}
    assert done == {**by, sale: "retired", mixed: "retired:mixed_deal_type"}
    assert db.listings[8]["property_id"] == 800 and db.properties[800]["status"] == "active"
    assert db.listings[4]["property_id"] == 300, "the rental merge stays for its own wave"
    assert out["counts"]["outside_scope_categories_by_category_type"] == {
        "prodej/none": 1, "pronajem": 1}
    assert out["counts"]["retire_set"] == 2 and out["counts"]["retire_set_mixed_deal_type"] == 1


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
    db.group(10, [1, 2])                               # the engine groups the intact pair
    A.run_apply(_factory(db), {"generation": GEN}, tmp_path)
    assert L.GROUPS_SQL not in db.statements and not (tmp_path / "apply").exists()

    page = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    out = A.run_apply(_factory(db), {"generation": GEN, "retire_legacy": "1"}, tmp_path)
    assert out["dry_run"] is True
    assert out["legacy_retire"]["counts"]["retire_set"] == 2
    assert out["legacy_retire"]["category_types"] == ["prodej"]
    assert out["legacy_retire"]["undone_by"].startswith(f"{L.RETIRE_BY}:")
    body = json.loads((tmp_path / "apply" / "legacy_retire.json").read_text())
    assert len(body["groups"]) == 10
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


# ------------------------------------------------------------------ review round (fix-first)


def test_1_a_group_the_operator_split_in_part_before_selection_is_reported_not_retired() -> None:
    """Reviewer S1: X taken back by hand (ruled apart from Y); undoing the rest would put Y
    beside X on the origin."""
    db = RetireDb()
    db.advert(1, 100)                                  # the survivor's own advert Z
    db.advert(2, 200)                                  # X
    db.advert(3, 200)                                  # Y
    g = db.merged(100, 200)
    db.detach([])(db, 2, decided_by="operator")        # no merge_group_id: X back to 200
    calls: list[dict[str, Any]] = []
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
                          detach=db.detach_recording(calls))
    assert {x["merge_group_id"]: x["outcome"] for x in out["groups"]} == {
        g: "skipped:operator_split"}
    assert calls == [] and db.listings[3]["property_id"] == 100
    group = out["groups"][0]
    assert group["undone_by_others"] == ["unknown"] and group["moved"] == [3]
    # the probe's intact is ours plus operator_split
    assert out["counts"]["probe_r1c_by_health"] == {"operator_split": 1}


def test_1_the_undo_never_lands_an_advert_beside_one_an_operator_negative_keeps_it_from() -> None:
    db = RetireDb()
    older = _intact_pair(db, 100, 200, 1)
    newer = _intact_pair(db, 300, 400, 3)
    db.listing(9, 200)                                 # an advert on the older origin since
    db.location[9] = IN_TOWN
    db.listing(8, 400)
    db.location[8] = IN_TOWN
    db.mnl.append((2, 9, "operator"))
    db.verdicts.append({"kind": "pair", "lo": 4, "hi": 8, "verdict": "different"})
    expected = {older: "skipped:operator_ruled_different",
                newer: "skipped:operator_ruled_different"}
    dry = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=True, run_id="r0")
    assert {x["merge_group_id"]: x["outcome"] for x in dry["groups"]} == expected
    calls: list[dict[str, Any]] = []
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
                          detach=db.detach_recording(calls))
    assert {x["merge_group_id"]: x["outcome"] for x in out["groups"]} == expected
    assert calls == [] and out["groups"][0]["joins_different"] in ([[2, 9]], [[4, 8]])
    assert db.listings[2]["property_id"] == 100 and db.listings[4]["property_id"] == 300


def test_7_a_group_whose_undo_would_separate_an_operator_same_pair_is_left() -> None:
    db = RetireDb()
    ruled = _intact_pair(db, 100, 200, 1)
    free = _intact_pair(db, 300, 400, 3)
    db.verdicts.append({"kind": "pair", "lo": 1, "hi": 2, "verdict": "same"})
    db.verdicts.append({"kind": "pair", "lo": 3, "hi": 99, "verdict": "same"})  # not on it
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
                          detach=db.detach_recording([]))
    by = {x["merge_group_id"]: x for x in out["groups"]}
    assert by[ruled]["outcome"] == "skipped:operator_ruled_same"
    assert by[ruled]["separates_same"] == [[1, 2]]
    assert by[free]["outcome"] == "retired" and db.listings[2]["property_id"] == 100


def test_4_a_native_sale_beside_rentals_is_not_a_mix_the_merge_made() -> None:
    """Reviewer S4: the survivor already held a sale and a rental; the old merge added a
    rental. Not created by the merge, so the deal types decide: a rental merge, left."""
    db = RetireDb()
    db.advert(1, 100, ct="pronajem")
    db.listing(9, 100, ct="prodej")
    db.location[9] = IN_TOWN
    db.advert(2, 200, ct="pronajem")
    g = db.merged(100, 200)
    calls: list[dict[str, Any]] = []
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
                          detach=db.detach_recording(calls))
    assert {x["merge_group_id"]: x["outcome"] for x in out["groups"]} == {
        g: "skipped:outside_scope_categories"}
    assert calls == [] and db.listings[2]["property_id"] == 100
    assert out["groups"][0]["mixed"] is False


def test_5_a_chain_is_undone_in_one_dispatch_pass_after_pass() -> None:
    """Reviewer S6: R1 -> S, then S -> T. The newer merge first; its undo leaves the older one
    intact, and the next pass of the same dispatch takes it."""
    db = RetireDb()
    db.advert(1, 100)
    db.advert(2, 200)
    older = db.merged(100, 200)
    db.advert(3, 50)
    newer = db.merged(50, 100)
    dry = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=True, run_id="r0")
    assert dry["counts"]["chained_behind_retire_set"] == 1
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
                          detach=db.detach_recording([]))
    by = {x["merge_group_id"]: (x["outcome"], x["retired_in_pass"]) for x in out["groups"]}
    assert by == {newer: ("retired", 1), older: ("retired", 2)}
    assert out["passes"] == 2 and out["counts"]["retire_set_after_first_pass"] == 1
    assert {lid: db.listings[lid]["property_id"] for lid in (1, 2, 3)} == {1: 100, 2: 200, 3: 50}
    # the probe's grain is the selection as first read
    assert out["counts"]["probe_r1c_by_health"] == {"intact": 1, "survivor_merged_on": 1}


def test_6_the_dry_run_lists_the_engine_groups_touching_the_retire_set() -> None:
    db = RetireDb()
    _intact_pair(db, 100, 200, 1)
    _intact_pair(db, 300, 400, 3)
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=True, run_id="r0",
                          engine=L.EngineMaps(merging={1: 10, 2: 10, 3: 20, 77: 30}))
    assert out["engine_clusters"] == [10, 20]
    assert out["counts"]["engine_clusters_touching_retire_set"] == 2
    assert out["counts"]["retire_set_without_engine_cluster"] == 0
    assert "Engine groups touching the retire set: 10 20" in L.summary_markdown(out)


def test_2_a_generation_with_nothing_to_re_merge_undoes_nothing(
        tmp_path: Path, monkeypatch: Any) -> None:
    """Reviewer S2: a typo'd generation would have undone every old merge and merged nothing."""
    db = RetireDb()
    db.live_scope(blocks=sorted(TRIAL))
    _intact_pair(db, 100, 200, 1)
    db.group(10, [1, 2])
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(L, "detach_listing", db.detach_recording(calls))
    for dry_run in ("1", "0"):
        with pytest.raises(SystemExit, match="no proposed group inside the scope"):
            A.run_apply(_factory(db), {"generation": "g21-typo", "retire_legacy": "1",
                                       "dry_run": dry_run}, tmp_path)
    assert calls == [] and db.listings[2]["property_id"] == 100
    assert not (tmp_path / "apply" / "legacy_retire.json").exists()


def test_3_listing_ids_are_refused_and_the_cap_is_counted_not_hidden(
        tmp_path: Path, monkeypatch: Any) -> None:
    """Reviewer S3: the step reads blocks only, so a listing narrowing is refused; the run cap
    limits the engine's merges, so what it deferred is counted for the next dispatch."""
    db = RetireDb()
    db.live_scope(blocks=sorted(TRIAL))
    first = _intact_pair(db, 100, 200, 1)
    second = _intact_pair(db, 300, 400, 3)
    db.group(10, [1, 2])
    db.group(20, [3, 4])
    monkeypatch.setattr(L, "detach_listing", db.detach_recording([]))
    merged: list[dict[str, Any]] = []
    original = A.apply_plan
    monkeypatch.setattr(A, "apply_plan", lambda conn, plan, dry_run: original(
        conn, plan, dry_run, merge=db.merge(merged)))
    with pytest.raises(SystemExit, match="listing_ids"):
        A.run_apply(_factory(db), {"generation": GEN, "retire_legacy": "1", "dry_run": "0",
                                   "listing_ids": "1 2"}, tmp_path)
    assert db.listings[2]["property_id"] == 100 and db.listings[4]["property_id"] == 300
    page = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    out = A.run_apply(_factory(db), {"generation": GEN, "retire_legacy": "1", "dry_run": "0",
                                     "max_clusters_per_run": "1"}, tmp_path)
    assert out["legacy_retire"]["counts"]["outcomes"] == {"retired": 2}
    assert out["counts"]["applied"] == 1 and out["deferred"] == [20]
    assert out["legacy_retire"]["counts"]["undone_but_engine_group_deferred_by_cap"] == 1
    body = json.loads((tmp_path / "apply" / "legacy_retire.json").read_text())
    assert body["deferred_by_cap"] == [second] and first not in body["deferred_by_cap"]
    assert "the run cap deferred" in page.read_text()


# ------------------------------------------------------------------ review round 2


def _chain(db: RetireDb) -> tuple[str, str]:
    """G1: R1(200) -> S(100) moves X=2, Y=4; G2: S(100) -> T(50) moves Z=1, X, Y."""
    db.advert(1, 100)
    db.advert(2, 200)
    db.advert(4, 200)
    g1 = db.merged(100, 200)
    db.advert(3, 50)
    g2 = db.merged(50, 100)
    return g1, g2


def _by(out: dict[str, Any]) -> dict[str, str]:
    return {x["merge_group_id"]: x["outcome"] for x in out["groups"]}


def _wire(db: RetireDb, monkeypatch: Any) -> list[dict[str, Any]]:
    monkeypatch.setattr(L, "detach_listing", db.detach_recording([]))
    merged: list[dict[str, Any]] = []
    original = A.apply_plan
    monkeypatch.setattr(A, "apply_plan", lambda conn, plan, dry_run: original(
        conn, plan, dry_run, merge=db.merge(merged)))
    return merged


def test_r2_1_only_groups_this_run_may_merge_count_as_re_merging(
        tmp_path: Path, monkeypatch: Any) -> None:
    """Reviewer T5b / T5c: a rejected engine group, or one in another block, never merges; it
    is counted apart. Here each holds only PART of the legacy group, so the group still goes."""
    for status, block in (("rejected", ("o", TOWN)), ("proposed", ("o", 999999))):
        db = RetireDb()
        db.live_scope(blocks=sorted(TRIAL))
        _intact_pair(db, 100, 200, 1)
        db.group(10, [1, 99], status=status, block=block)
        db.listing(99, None)
        db.advert(7, 700)
        db.advert(8, 800)
        db.group(20, [7, 8])                           # passes the plan-first check
        page = tmp_path / f"{status}.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
        out = A.run_apply(_factory(db), {"generation": GEN, "retire_legacy": "1"}, tmp_path)
        counts = out["legacy_retire"]["counts"]
        assert counts["outcomes"] == {"would_retire": 1}
        assert counts["engine_clusters_touching_retire_set"] == 0
        assert counts["retire_set_without_engine_cluster"] == 1
        assert counts["engine_clusters_not_merging_touching_retire_set"] == 1
        body = json.loads((tmp_path / "apply" / "legacy_retire.json").read_text())
        assert body["engine_clusters"] == [] and body["engine_clusters_not_merging"] == [10]
        assert "this run will not merge (not proposed, or outside the scope): 10" \
            in page.read_text()


def test_r3_the_engine_holding_a_whole_group_outside_the_scope_keeps_it_for_w6(
        tmp_path: Path, monkeypatch: Any) -> None:
    """The trial dry run (36147150175): 90 of 177 groups sat whole in ONE engine group the scope
    does not admit (the trial edge cuts it). Undoing them would split what the engine calls one
    property, and nothing would re-merge it until the scope widens. Left, with the group and
    why; where the engine splits the group itself (different groups, some or all ungrouped),
    it goes."""
    db = RetireDb()
    db.live_scope(blocks=sorted(TRIAL))
    shape: dict[str, str] = {}
    shape["other_block"] = _intact_pair(db, 100, 200, 1)
    db.group(10, [1, 2], block=("o", 999999))          # reaches outside the trial blocks
    shape["not_proposed"] = _intact_pair(db, 300, 400, 3)
    db.group(20, [3, 4], status="rejected")
    shape["different_groups"] = _intact_pair(db, 500, 600, 5)
    db.group(30, [5], block=("o", 999999))
    db.group(31, [6], block=("o", 999999))
    shape["partly_grouped"] = _intact_pair(db, 700, 800, 7)
    db.group(40, [7, 97], block=("o", 999999))
    db.listing(97, None)
    shape["ungrouped"] = _intact_pair(db, 900, 1000, 9)
    shape["admitted"] = _intact_pair(db, 1100, 1200, 11)
    db.group(50, [11, 12])                             # proposed, in scope: merges
    shape["mixed_admitted_and_not"] = _intact_pair(db, 1300, 1400, 13)
    db.group(60, [13, 98])
    db.listing(98, None)
    db.group(61, [14], block=("o", 999999))
    page = tmp_path / "s.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    out = A.run_apply(_factory(db), {"generation": GEN, "retire_legacy": "1"}, tmp_path)
    body = json.loads((tmp_path / "apply" / "legacy_retire.json").read_text())
    by = {x["merge_group_id"]: x for x in body["groups"]}
    agrees = f"skipped:{L.ENGINE_AGREES}"
    assert {name: by[gid]["outcome"] for name, gid in shape.items()} == {
        "other_block": agrees, "not_proposed": agrees,
        "different_groups": "would_retire", "partly_grouped": "would_retire",
        "ungrouped": "would_retire", "admitted": "would_retire",
        "mixed_admitted_and_not": "would_retire"}
    assert (by[shape["other_block"]]["engine_agrees_cluster"],
            by[shape["other_block"]]["engine_agrees_why"]) == (10, "block town:999999 outside "
                                                                  "the scope")
    assert (by[shape["not_proposed"]]["engine_agrees_cluster"],
            by[shape["not_proposed"]]["engine_agrees_why"]) == (20, "status rejected")
    counts = out["legacy_retire"]["counts"]
    assert counts[L.ENGINE_AGREES] == 2 and counts["retire_set"] == 5
    assert counts[f"{L.ENGINE_AGREES}_by_reason"] == {
        "block town:999999 outside the scope": 1, "status rejected": 1}
    assert f"skipped:{L.ENGINE_AGREES} (10: block town:999999 outside the scope)" \
        in page.read_text()

    # live, the same: the two stay merged, the rest come apart
    monkeypatch.setattr(L, "detach_listing", db.detach_recording([]))
    merged: list[dict[str, Any]] = []
    original = A.apply_plan
    monkeypatch.setattr(A, "apply_plan", lambda conn, plan, dry_run: original(
        conn, plan, dry_run, merge=db.merge(merged)))
    live = A.run_apply(_factory(db), {"generation": GEN, "retire_legacy": "1", "dry_run": "0"},
                       tmp_path)
    assert live["legacy_retire"]["counts"]["outcomes"] == {agrees: 2, "retired": 5}
    assert db.listings[2]["property_id"] == 100 and db.listings[4]["property_id"] == 300
    assert db.listings[6]["property_id"] == 600 and db.listings[10]["property_id"] == 1000


def test_r2_2_a_negative_the_undo_does_not_newly_join_refuses_nothing() -> None:
    """Reviewer T4: q and r already sit together (on S) and go back together (to R): the undo
    joins nothing new. T4b: a machine must-not-link is not the operator's ruling."""
    db = RetireDb()
    db.advert(1, 100)
    db.advert(2, 200)
    db.advert(5, 300)
    g_old = db.merged(200, 300)
    g_new = db.merged(100, 200)
    db.verdicts.append({"kind": "pair", "lo": 2, "hi": 5, "verdict": "different"})
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
                          detach=db.detach_recording([]))
    # the newer undone; the older, now intact, would put q beside... nothing new: q's origin
    # 300 holds no advert, so it is undone on the next pass too
    assert _by(out) == {g_new: "retired", g_old: "retired"}

    db = RetireDb()
    db.advert(1, 100)
    db.advert(2, 200)
    db.advert(3, 200)
    g = db.merged(100, 200)
    db.mnl.append((2, 3, "guard"))
    db.listing(9, 200)                                 # the origin holds another advert since
    db.location[9] = IN_TOWN
    db.mnl.append((2, 9, "guard"))                     # ... a machine guard would newly join
    dry = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=True, run_id="r0")
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
                          detach=db.detach_recording([]))
    assert _by(dry) == {g: "would_retire"} and _by(out) == {g: "retired"}


def test_r2_3_a_mix_made_in_two_steps_is_recognised() -> None:
    """Reviewer T10: two old merges each put a rental onto a sale. T8: one merge took a sale and
    a rental from two properties onto a sale."""
    db = RetireDb()
    db.advert(1, 100)
    for lid, pid in ((2, 200), (3, 300)):
        db.prop(pid, ct=None)
        db.listing(lid, pid, ct="pronajem")
        db.location[lid] = IN_TOWN
    g0 = db.merged(100, 200)
    g1 = db.merged(100, 300)
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
                          detach=db.detach_recording([]))
    assert _by(out) == {g0: "retired:mixed_deal_type", g1: "retired:mixed_deal_type"}
    assert db.listings[2]["property_id"] == 200 and db.listings[3]["property_id"] == 300

    db = RetireDb()
    db.advert(1, 100)
    db.advert(2, 200)
    db.prop(300, ct=None)
    db.listing(3, 300, ct="pronajem")
    db.location[3] = IN_TOWN
    g = db.merged(100, 200, 300)
    (group,) = L.read_groups(db, L.read_area(db, TRIAL), SALES)
    assert group.mixed and group.outcome == "to_retire:mixed_deal_type"


def test_r2_4_later_passes_report_what_they_read_and_the_pass_cap_says_so() -> None:
    """Reviewer T2 / T3b: a group pass 2 re-reads is reported as pass 2 read it. T9: a chain
    longer than MAX_PASSES stops, says so, and leaves the rest not attempted."""
    db = RetireDb()
    db.advert(1, 100)
    db.advert(2, 200)
    db.advert(4, 200)
    g1 = db.merged(100, 200)
    db.detach([])(db, 2, decided_by="operator")
    db.advert(3, 50)
    g2 = db.merged(50, 100)
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
                          detach=db.detach_recording([]))
    assert _by(out) == {g2: "retired", g1: "skipped:operator_split"}
    assert out["counts"]["probe_r1c_by_health"]["survivor_merged_on"] == 1   # the first read

    db = RetireDb()
    g1, g2 = _chain(db)
    db.verdicts.append({"kind": "pair", "lo": 1, "hi": 2, "verdict": "same"})
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
                          detach=db.detach_recording([]))
    assert _by(out) == {g2: "retired", g1: "skipped:operator_ruled_same"}

    db = RetireDb()
    pid = 1000
    db.advert(1, pid)
    chain = []
    for i in range(12):
        db.advert(100 + i, pid - 10)
        chain.append(db.merged(pid - 10, pid))
        pid -= 10
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1",
                          detach=db.detach_recording([]))
    assert out["passes"] == L.MAX_PASSES and "passes reached" in out["stopped"]
    assert out["counts"]["outcomes"] == {"not_attempted": 1, "retired": L.MAX_PASSES,
                                         "skipped:survivor_merged_on": 1}


def test_r2_4_a_crash_on_pass_2_reports_its_leftovers_not_attempted(
        tmp_path: Path, monkeypatch: Any) -> None:
    """Reviewer T6b: two chains; pass 2's first group crashes, its second was never tried."""
    db = RetireDb()
    g1, g2 = _chain(db)
    db.advert(11, 1100)
    db.advert(12, 1200)
    h1 = db.merged(1100, 1200)
    db.advert(13, 1050)
    h2 = db.merged(1050, 1100)
    base = db.detach_recording([])

    def detach(conn: RetireDb, lid: int, **kw: Any) -> dict[str, Any]:
        if kw["merge_group_id"] in (g1, h1):
            raise RuntimeError("canceling statement due to statement timeout")
        return base(conn, lid, **kw)

    monkeypatch.setattr(L, "detach_listing", detach)
    with pytest.raises(RuntimeError):
        L.run(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1", out_dir=tmp_path)
    body = json.loads((tmp_path / "apply" / "legacy_retire.json").read_text())
    got = _by(body)
    assert got[g2] == got[h2] == "retired"
    assert sorted((got[g1], got[h1])) == ["failed", "not_attempted"]


def test_r2_5_the_dry_run_predicts_what_the_run_cap_will_defer(
        tmp_path: Path, monkeypatch: Any) -> None:
    """Reviewer C1: the dry-run plan sees the old merges (already one property), so it predicts
    from the engine groups touching the retire set; C2: a refused group is never 'undone'."""
    db = RetireDb()
    db.live_scope(blocks=sorted(TRIAL))
    _intact_pair(db, 100, 200, 1)
    _intact_pair(db, 300, 400, 3)
    db.group(10, [1, 2])
    db.group(20, [3, 4])
    _wire(db, monkeypatch)
    page = tmp_path / "s.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    dry = A.run_apply(_factory(db), {"generation": GEN, "retire_legacy": "1",
                                     "max_clusters_per_run": "1"}, tmp_path)
    counts = dry["legacy_retire"]["counts"]
    assert counts["predicted_engine_groups_after_undo"] == 2
    assert counts["predicted_deferred_by_cap"] == 1
    assert "a live run defers 1 to a re-dispatch" in page.read_text()
    live = A.run_apply(_factory(db), {"generation": GEN, "retire_legacy": "1", "dry_run": "0",
                                      "max_clusters_per_run": "1"}, tmp_path)
    assert live["legacy_retire"]["counts"]["undone_but_engine_group_deferred_by_cap"] == 1



def test_r2_5_a_refused_group_is_never_counted_as_undone(
        tmp_path: Path, monkeypatch: Any) -> None:
    """Reviewer C2: a group refused at its detach undid nothing, even if the cap deferred the
    engine group touching it."""
    db = RetireDb()
    db.live_scope(blocks=sorted(TRIAL))
    _intact_pair(db, 100, 200, 1)
    db.advert(3, 300)
    db.advert(4, 400)
    db.advert(5, 400)
    held = db.merged(300, 400)
    db.advert(9, 900)
    db.group(10, [1, 2])
    db.group(20, [3, 4, 5, 9])
    _wire(db, monkeypatch)
    base = db.detach_recording([])

    def detach(conn: RetireDb, lid: int, **kw: Any) -> dict[str, Any]:
        if lid == 5:
            return {"data": {"detached": False, "outcome": "moved_since"}}
        return base(conn, lid, **kw)

    monkeypatch.setattr(L, "detach_listing", detach)
    live = A.run_apply(_factory(db), {"generation": GEN, "retire_legacy": "1", "dry_run": "0",
                                      "max_clusters_per_run": "1"}, tmp_path)
    assert live["deferred"] == [20]
    assert live["legacy_retire"]["counts"]["outcomes"] == {"refused:moved_since": 1,
                                                           "retired": 1}
    assert _by(json.loads((tmp_path / "apply" / "legacy_retire.json").read_text()))[held] \
        == "refused:moved_since"
    assert live["legacy_retire"]["counts"]["undone_but_engine_group_deferred_by_cap"] == 0


def test_r2_6_a_ledger_row_with_no_listing_reads_as_a_gone_advert() -> None:
    db = RetireDb()
    g = _intact_pair(db, 100, 200, 1)
    db.events.append({"id": 99, "merge_group_id": g, "survivor": 100, "retired": 200,
                      "listing": None, "generation": None, "undone": False, "source": "auto",
                      "created_at": db.now, "undone_by": None})
    out = L.retire_legacy(db, TRIAL, category_types=SALES, dry_run=True, run_id="r0")
    assert _by(out) == {g: "skipped:listing_gone"} and out["groups"][0]["moved"] == [2]


# ------------------------------------------------------------------ statement cost (dry run 2)


MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def _migration_code(name: str) -> str:
    path = MIGRATIONS / name
    body = "\n".join(line.split("--")[0] for line in path.read_text().lower().splitlines())
    return " ".join(body.split())


def test_every_read_after_the_area_is_driven_by_ids_and_the_indexes_it_names() -> None:
    """Dry run 2 (36148892044) hit the 2-minute statement timeout: GROUPS_SQL scanned every
    live old-engine row with two correlated EXISTS each. Only AREA_SQL reads listing_location,
    once per dispatch; GROUPS_SQL is driven from the area's ids into the ledger's indexes."""
    sql = {name: getattr(L, name) for name in dir(L) if name.endswith("_SQL")}
    assert [n for n, text in sql.items() if "listing_location" in text] == ["AREA_SQL"]
    groups = " ".join(L.GROUPS_SQL.split())
    assert "exists (" not in groups
    touching = groups.split("touching as (")[1].split(") select")[0]
    assert "from area a join public.property_merge_events e on e.listing_ref_id = " \
        "a.listing_id" in touching
    assert "from area_properties ap join public.property_merge_events e on " \
        "e.survivor_property_id = ap.property_id" in touching
    assert "retired_property_id" not in touching
    assert "from touching t join public.property_merge_events e on e.merge_group_id = " \
        "t.merge_group_id" in groups
    for text in (L.ADVERTS_SQL, L.AUTO_MOVED_SQL, L.BUILT_ON_SQL):
        assert "%(area_ids)s" in text or "%(listing_ids)s" in text \
            or "%(property_ids)s" in text
    # ... and the indexes those joins ride exist, as the comments cite them.
    assert ("create index listing_location_obec_granularity on listing_location "
            "(obec_kod, granularity);") in _migration_code("501_location_w2a_listing_location.sql")
    assert ("create index property_merge_events_group_idx on property_merge_events "
            "(merge_group_id);") in _migration_code("100_property_merge_audit.sql")
    assert ("create index property_merge_events_survivor_idx on property_merge_events "
            "(survivor_property_id);") in _migration_code("100_property_merge_audit.sql")
    assert ("create index if not exists property_merge_events_listing_live_idx on "
            "property_merge_events (listing_ref_id, id) where undone_at is null;") \
        in _migration_code("560_one_merge_one_undo.sql")
    assert "create index listings_property_id_idx on listings (property_id);" \
        in _migration_code("091_properties_foundation.sql")


def test_the_area_is_read_once_per_dispatch_and_every_read_is_timed(tmp_path: Path,
                                                                     monkeypatch: Any) -> None:
    db = RetireDb()
    older = _intact_pair(db, 100, 200, 1)
    db.advert(3, 50)
    newer = db.merged(50, 100)                         # a chain: two passes, re-checks
    page = tmp_path / "s.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(page))
    monkeypatch.setattr(L, "detach_listing", db.detach_recording([]))
    out = L.run(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1", out_dir=tmp_path)
    assert _by(out) == {newer: "retired", older: "retired"} and out["passes"] == 2
    assert db.statements.count(L.AREA_SQL) == 1
    assert db.statements.count(L.GROUPS_SQL) > 3        # passes plus one re-check per group
    assert out["area_listings"] == 3
    timings = out["timings"]
    assert timings["area"]["n"] == 1 and timings["groups"]["n"] == \
        db.statements.count(L.GROUPS_SQL)
    assert {"adverts", "auto_moved", "built_on", "lock_properties", "pair_verdicts",
            "property_state", "must_not_link"} <= set(timings)
    assert all(t["max_s"] <= t["total_s"] for t in timings.values())
    assert "| read | n | total s | max s |" in page.read_text()
    assert "| area | 1 |" in page.read_text()


def test_a_read_that_fails_is_still_timed_in_what_is_published(tmp_path: Path,
                                                                monkeypatch: Any) -> None:
    db = RetireDb()
    _intact_pair(db, 100, 200, 1)
    _intact_pair(db, 300, 400, 3)
    seen = {"groups": 0}
    original = db.dispatch

    def dispatch(sql: str, p: dict[str, Any]) -> list[tuple]:
        if sql == L.GROUPS_SQL:
            seen["groups"] += 1
            if seen["groups"] == 3:                    # the second group's re-check
                raise RuntimeError("canceling statement due to statement timeout")
        return original(sql, p)

    monkeypatch.setattr(db, "dispatch", dispatch)
    monkeypatch.setattr(L, "detach_listing", db.detach_recording([]))
    with pytest.raises(RuntimeError, match="statement timeout"):
        L.run(db, TRIAL, category_types=SALES, dry_run=False, run_id="r1", out_dir=tmp_path)
    body = json.loads((tmp_path / "apply" / "legacy_retire.json").read_text())
    assert body["timings"]["groups"]["n"] == 3 and "statement timeout" in body["aborted"]


def test_a_group_touching_only_through_its_retired_property_is_not_read() -> None:
    """Never a retire: its moved adverts and survivor are outside, so it could only straddle.
    Not reading it keeps GROUPS_SQL on indexes (retired_property_id has none)."""
    db = RetireDb()
    db.advert(1, 100, OUT)
    db.advert(2, 200, OUT)
    db.merged(100, 200)
    db.listing(9, 200)                                 # an inside advert attached to 200 since
    db.location[9] = IN_TOWN
    assert L.read_groups(db, L.read_area(db, TRIAL), SALES) == []
