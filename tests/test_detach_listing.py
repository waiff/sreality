"""The one undo: `toolkit.property_identity.detach_listings` (and `detach_listing`, its
one-advert adapter) and the origins route over it (decision 8). Over the stateful fake in
tests/_property_ledger.py, so a merge and its undo replay end to end here (and in
tests/test_property_merge_set.py), each carrier recorded at the seam and each after-step in
`db.changed`; executed: tests/test_merge_safety_live.py and tests/test_property_carriers_live.py.
The operator's split route over it is tests/test_property_split.py.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

import toolkit.property_identity as pi
from tests._property_ledger import OP, T0, _Ledger, ledger_carriers  # noqa: F401 — the fixture
from toolkit import property_carriers as carriers
from toolkit.property_carriers import DetachStep, Hop
from toolkit.property_identity import (
    MergeError,
    detach_listing,
    detach_listings,
    merge_property_set,
)

pytestmark = pytest.mark.usefixtures("ledger_carriers")


def _merged(db: _Ledger, ids: list[int], *, source: str = "operator") -> dict[str, Any]:
    return merge_property_set(db, ids, source=source, reason="manual_subset",
                              decided_by=OP)["data"]


def _appended(db: _Ledger) -> list[Any]:
    """The rulings appended, by the one pair writer's own statement (a bare veto written down
    first is a different statement: `usql.VERDICT_PAIR_FROM_VETO_SQL`)."""
    appended = " ".join(pi.usql.VERDICT_PAIR_APPEND_SQL.split())
    return [p for s, p in db.log if s == appended]


def _verdicts(db: _Ledger) -> list[tuple[int, int, str, str]]:
    return [(r["listing_lo"], r["listing_hi"], r["verdict"], r["note"])
            for r in _appended(db)]


def _restores(db: _Ledger, name: str = "pipeline") -> list[DetachStep]:
    """The steps one carrier was handed by a detach (only one that reactivated a property)."""
    return [step for kind, carrier, step in db.carried if kind == "detach" and carrier == name]


def test_one_advert_goes_back_to_the_merged_away_property_it_came_from():
    db = _Ledger({1: 10, 2: 20, 3: 20})
    group = _merged(db, [10, 20])["merge_group_id"]
    out = detach_listing(db, 2, decided_by=OP, reason="jiné patro")["data"]
    assert out == {**out, "detached": True, "outcome": "detached", "listing_id": 2,
                   "survivor_property_id": 10, "restored_property_id": 20,
                   "reactivated": True, "merge_group_ids": [group]}
    assert db.listings == {1: 10, 2: 20, 3: 10} and db.props[20] == "active"
    # the ledger keeps every row; the restored property's card comes from THAT merge's snapshot
    assert [(e["listing"], e["undone_by"]) for e in db.events] == [(2, OP), (3, None)]
    assert _restores(db) == [DetachStep(restored=20, left=10, undo=(Hop(1, group, 10, 20),),
                                        source="operator")]
    # its sibling joins it without reactivating (or restoring) anything a second time
    assert not detach_listing(db, 3, decided_by=OP)["data"]["reactivated"]
    assert db.listings == {1: 10, 2: 20, 3: 20}
    assert len(_restores(db)) == 1


def test_a_second_detach_and_a_native_advert_are_no_ops_that_say_so():
    db = _Ledger({1: 10, 2: 20})
    _merged(db, [10, 20])
    detach_listing(db, 2, decided_by=OP)
    rulings = len(_verdicts(db))
    for lid, where in ((2, 20), (1, 10)):
        again = detach_listing(db, lid, decided_by=OP)["data"]
        assert (again["detached"], again["outcome"], again["rulings_written"]) == (
            False, "not_merged", 0)
        assert again["restored_property_id"] == again["survivor_property_id"] == where
    assert len(_verdicts(db)) == rulings


def test_an_advert_goes_back_to_its_origin_across_a_chain_of_merges():
    """2 came from 20 into 10, then 10 merged into the older 5: the advert returns to the
    record it sat on before every merge that still stands."""
    db = _Ledger({1: 10, 2: 20, 9: 5}, first_seen={5: T0 - timedelta(days=9)})
    _merged(db, [10, 20])
    _merged(db, [5, 10], source="autodedup")
    # with the merge that took each advert from its origin: the oldest that stands
    assert pi.listing_origins(db, [1, 2, 9]) == {1: (10, "autodedup", T0),
                                                 2: (20, "operator", T0)}
    out = detach_listing(db, 2, decided_by=OP)["data"]
    assert out["restored_property_id"] == 20 and len(out["merge_group_ids"]) == 2
    assert db.listings == {1: 5, 2: 20, 9: 5}
    assert [e["undone_by"] for e in db.events if e["listing"] == 2] == [OP, OP]
    # the whole path goes to the carriers: 20 -> 10 -> 5; the pipeline restores 20's card and
    # drops the one that followed where it sits now unless it may carry another deal
    # (test_property_carriers, live 4b to 4m)
    (step,) = _restores(db)
    assert (step.restored, step.left, [h.survivor for h in step.undo]) == (20, 5, [10, 5])
    assert step.left != step.undo[0].survivor


def test_a_group_scoped_detach_undoes_only_that_merge_while_it_is_the_newest():
    db = _Ledger({1: 10, 2: 20, 9: 5}, first_seen={5: T0 - timedelta(days=9)})
    first = _merged(db, [10, 20], source="autodedup")["merge_group_id"]
    second = _merged(db, [5, 10], source="autodedup")["merge_group_id"]
    scoped = {"decided_by": "autodedup-unapply:r", "source": "autodedup"}
    moved_on = detach_listing(db, 2, merge_group_id=first, **scoped)["data"]
    assert moved_on["outcome"] == "moved_on" and db.listings[2] == 5
    back = detach_listing(db, 2, merge_group_id=second, **scoped)["data"]
    assert back["restored_property_id"] == 10 and back["merge_group_ids"] == [second]
    assert db.props[10] == "active" and db.listings[2] == 10
    assert back["rulings_written"] == 0 and db.sql("autodedup.") == [], "an engine undo rules nothing"


def test_an_advert_moved_outside_the_ledger_stays_and_a_missing_one_is_an_error():
    db = _Ledger({1: 10, 2: 20, 7: 70})
    _merged(db, [10, 20])
    db.listings[2] = 70
    assert detach_listing(db, 2, decided_by=OP)["data"]["outcome"] == "moved_since"
    assert db.listings[2] == 70
    # moved by a concurrent merge between the ledger read and the property locks: left there
    db.listings[2], dispatch = 10, db.dispatch
    db.dispatch = lambda s, p: (db.listings.update({2: 70}) if "FOR UPDATE" in s
                                else None) or dispatch(s, p)
    assert detach_listing(db, 2, decided_by=OP)["data"]["outcome"] == "moved_since"
    assert db.listings[2] == 70 and db.sql("UPDATE property_merge_events SET undone_at") == []
    with pytest.raises(MergeError):
        detach_listing(db, 99, decided_by=OP)


def test_an_origin_a_later_merge_retired_elsewhere_is_left_merged_and_the_advert_stays():
    """x1, x2 were 30's own. {10, 30} merged; x1 detached (30 back); the engine then merged 30
    into the older 5. Detaching x2 from 10 would half-undo that merge and restore 30's card a
    second time: nothing moves, and the outcome says why (rule 22)."""
    db = _Ledger({1: 10, 31: 30, 32: 30, 9: 5}, first_seen={5: T0 - timedelta(days=9)})
    _merged(db, [10, 30])
    assert detach_listing(db, 31, decided_by=OP)["data"]["reactivated"]
    _merged(db, [5, 30], source="autodedup")
    restores = len(_restores(db))
    # x1 may go back to 30 from 5; x2 may not from 10 while that merge stands
    assert pi.detach_outcomes(db, [31, 32]) == {31: "detached", 32: "origin_moved_on"}
    out = detach_listing(db, 32, decided_by=OP)["data"]
    assert (out["detached"], out["outcome"], out["rulings_written"]) == (
        False, "origin_moved_on", 0)
    assert db.listings[32] == 10 and db.props[30] == "merged_away" and db.into[30] == 5
    assert len(_restores(db)) == restores
    assert [e["undone_by"] for e in db.events if e["listing"] == 32] == [None]
    # retired elsewhere between the unlocked read and the lock: the lock's read decides
    dispatch = db.dispatch
    db.dispatch = lambda s, p: (db.into.update({30: 7}) if "FOR UPDATE" in s
                                else None) or dispatch(s, p)
    assert detach_listing(db, 31, decided_by=OP)["data"]["outcome"] == "origin_moved_on"
    assert db.listings[31] == 5


@pytest.mark.parametrize("same_asset", [False, True])
def test_the_asset_link_a_merge_carried_goes_back_with_its_property(same_asset):
    """Decision 17's carry, inverted: 20 (asset 7) merged into the older 10; the detach that
    brings 20 back gives it its link, and 10 keeps one only if it held it before the merge."""
    db = _Ledger({1: 10, 2: 20, 3: 20}, assets={20: 7, **({10: 7} if same_asset else {})})
    group = _merged(db, [10, 20])["merge_group_id"]
    assert db.assets == {10: 7, 20: None}
    detach_listing(db, 2, decided_by=OP)
    assert db.assets == {10: 7 if same_asset else None, 20: 7}
    assert (7, 20, "linked", f"detach {group}") in db.asset_events
    detach_listing(db, 3, decided_by=OP)
    assert db.assets == {10: 7 if same_asset else None, 20: 7}


def test_an_operator_detach_rules_the_advert_different_from_every_advert_that_stays():
    db = _Ledger({1: 10, 2: 10, 3: 20})
    _merged(db, [10, 20])
    db.log.clear()
    out = detach_listing(db, 3, decided_by=OP, reason="jiné patro")["data"]
    note = "operator detach from 10: jiné patro"
    assert _verdicts(db) == [(1, 3, "different", note), (2, 3, "different", note)]
    assert out["rulings_written"] == 2
    assert {(r["listing_lo"], r["listing_hi"]) for r in
            db.sql("INSERT INTO autodedup.must_not_link")} == {(1, 3), (2, 3)}
    assert {r["decided_by"] for r in _appended(db)} == {OP}


def test_a_reactivating_detach_walks_the_carriers_in_reverse_and_changes_both_once():
    """Undo in the reverse of the carry: every carrier gets ONE step, the asset link last; which
    of them gives anything back (the pipeline card and the asset link; curation, dispatches and
    dismissals stay on the property left) is each carrier's own, tests/test_property_carriers.py."""
    db = _Ledger({1: 10, 2: 20}, assets={20: 7})
    group = _merged(db, [10, 20])["merge_group_id"]
    for seen in (db.log, db.carried, db.changed, db.browse, db.broker):
        seen.clear()
    detach_listing(db, 2, decided_by=OP)
    step = DetachStep(restored=20, left=10, undo=(Hop(1, group, 10, 20),), source="operator")
    walked = [s for s, _p in db.log if s.startswith("carrier:") or s.startswith("WITH carried AS")]
    assert walked == [f"carrier:{c.name}" for c in reversed(carriers.PROPERTY_CARRIERS)
                      if c.name != "asset_link"] + [" ".join(
                          carriers._RESTORE_ASSET_LINK_SQL.split())]
    assert {entry for entry in db.carried} == {("detach", c.name, step) for c in
                                                carriers.PROPERTY_CARRIERS if c.name != "asset_link"}
    assert db.assets == {10: None, 20: 7}
    written = " ".join(s for s, _p in db.log)
    for table in ("property_status_events", "DELETE FROM properties",
                  "DELETE FROM property_merge_events"):
        assert table not in written, f"a detach touched {table}"
    assert db.changed == db.browse == db.broker == [[10, 20]]
    (reactivate,) = [s for s, _p in db.log if "SET status = 'active'" in s]
    assert "merged_into = NULL, merged_at = NULL, is_active = EXISTS (" in reactivate


@pytest.mark.parametrize("backwards", [False, True])
def test_merging_then_detaching_every_advert_restores_every_original_property(backwards):
    """W3's gate at unit scale — a refactor of the mechanics changes no grouping: three set
    merges, one built on another; every advert a merge moved detached in turn, in either order,
    returns to its own record, and every asset link to its own property — 30's rode 30 -> 10 ->
    60 (the oldest)."""
    original = {1: 10, 2: 10, 3: 20, 4: 30, 5: 30, 6: 40, 7: 50, 8: 60, 9: 70, 11: 80,
                12: 80, 13: 20}
    links = {30: 7, 50: 8}
    db = _Ledger(original, first_seen={pid: T0 + timedelta(days=pid % 60)
                                       for pid in set(original.values())}, assets=links)
    _merged(db, [10, 20, 30])
    _merged(db, [40, 50], source="autodedup")
    _merged(db, [60, 10, 80])
    assert len(set(db.listings.values())) == 3 and db.assets[60] == 7
    for lid in sorted(pi.listing_origins(db, list(original)), reverse=backwards):
        detach_listing(db, lid, decided_by=OP)
    assert db.listings == original and set(db.props.values()) == {"active"}
    assert {pid: a for pid, a in db.assets.items() if a} == links
    assert all(e["undone_by"] for e in db.events), "a detach left a live ledger row behind"


# --- a native advert (grouped at ingest, no merge moved it) --------------------------------


def test_a_native_advert_is_born_a_new_record_through_the_one_birth_path():
    """1, 2, 3 were grouped at ingest: no ledger row moved any of them. Splitting 2 gives it a
    NEW record by `scraper.db.NEW_SINGLETONS_SQL` and writes ONE ledger row — the ingest grouping
    as the merge it amounts to (the new record into 10), closed as undone by this split."""
    from scraper.db import NEW_SINGLETONS_SQL

    db = _Ledger({1: 10, 2: 10, 3: 10})
    assert pi.detach_outcomes(db, [1, 2, 3]) == dict.fromkeys((1, 2, 3), "split_native")
    out = detach_listing(db, 2, decided_by=OP, reason="jiné patro")["data"]
    born = out["restored_property_id"]
    assert born not in (None, 10) and db.listings == {1: 10, 2: born, 3: 10}
    assert out == {**out, "detached": True, "outcome": "split_native", "listing_id": 2,
                   "survivor_property_id": 10, "reactivated": False, "rulings_written": 2}
    (birth,) = [(s, p) for s, p in db.log if "INSERT INTO properties" in s]
    assert birth == (" ".join(NEW_SINGLETONS_SQL.split()), {"ids": [2]})
    (row,) = db.events
    assert row == {**row, "survivor": 10, "prev": born, "listing": 2, "source": "operator",
                   "undone_by": OP}
    assert out["merge_group_ids"] == [row["group"]]
    (written,) = [p for s, p in db.log if s.startswith("INSERT INTO property_merge_events")]
    assert (written["left"], written["born"], written["by"]) == (10, born, OP)
    # the advert's origin is its new record: no live row, alone there -> a second split is a no-op
    assert pi.listing_origins(db, [2]) == {} and pi.detach_outcomes(db, [2]) == {2: "not_merged"}
    note = "operator detach from 10: jiné patro"
    assert _verdicts(db) == [(1, 2, "different", note), (2, 3, "different", note)]
    again = detach_listing(db, 2, decided_by=OP)["data"]
    assert (again["detached"], again["outcome"], again["restored_property_id"]) == (
        False, "not_merged", born)
    assert len(db.events) == 1 and len(_verdicts(db)) == 2


def test_a_native_split_takes_the_merges_lock_order_and_leaves_curation_behind():
    db = _Ledger({1: 10, 2: 10})
    detach_listing(db, 2, decided_by=OP)
    order = [s for s, _p in db.log]
    lock = next(i for i, s in enumerate(order) if s.endswith("FOR UPDATE"))
    move = next(i for i, s in enumerate(order) if s.startswith("UPDATE listings SET property_id"))
    assert lock < move < next(i for i, s in enumerate(order) if "INSERT INTO properties" in s)
    assert db.sql("SELECT id, status, merged_into FROM properties")[-1] == {"ids": [10]}
    # no carrier runs on a native split: the new record starts clean (rules 18, 22)
    assert db.carried == []
    written = " ".join(order)
    for table in ("asset_membership_events", "DELETE FROM properties"):
        assert table not in written, f"a native split touched {table} (rules 18, 22)"
    born = db.listings[2]
    assert db.changed == db.browse == db.broker == [[10, born]]


def test_a_split_advert_merged_back_is_detached_to_its_new_record():
    """The round trip: the operator (or the engine) merges the two records again through the
    one merge; a detach then returns the advert to the record it was born on."""
    db = _Ledger({1: 10, 2: 10})
    born = detach_listing(db, 2, decided_by=OP)["data"]["restored_property_id"]
    merged = _merged(db, [10, born], source="autodedup")
    assert merged["survivor_id"] == 10 and db.listings == {1: 10, 2: 10}
    assert pi.listing_origins(db, [1, 2]) == {2: (born, "autodedup", T0)}
    back = detach_listing(db, 2, decided_by=OP)["data"]
    assert (back["outcome"], back["restored_property_id"], back["reactivated"]) == (
        "detached", born, True)
    assert db.listings == {1: 10, 2: born} and db.props[born] == "active"


def test_only_the_operator_splits_a_native_advert_and_a_group_undo_never_does():
    """Decision 9: an engine caller gets `propose_only` — nothing born, nothing written — and a
    group-scoped undo leaves a native advert where it is."""
    db = _Ledger({1: 10, 2: 10, 3: 30})
    group = _merged(db, [10, 30], source="autodedup")["merge_group_id"]
    scoped = {"decided_by": "autodedup-unapply:r", "source": "autodedup"}
    assert pi.detach_outcomes(db, [1, 2], merge_group_id=group) == {1: "not_merged",
                                                                    2: "not_merged"}
    assert detach_listing(db, 1, merge_group_id=group, **scoped)["data"]["outcome"] == "not_merged"
    events, db.log[:] = list(db.events), []
    out = detach_listing(db, 1, decided_by="autodedup-reconcile", source="autodedup")["data"]
    assert (out["detached"], out["outcome"], out["restored_property_id"]) == (
        False, "propose_only", 10)
    assert db.events == events and db.listings == {1: 10, 2: 10, 3: 10}
    assert not any("INSERT INTO properties" in s or s.startswith("UPDATE") for s, _p in db.log)
    assert detach_listing(db, 1, decided_by=OP)["data"]["outcome"] == "split_native"


def test_the_last_own_advert_stays_while_merged_adverts_share_its_property():
    """The review's reproduction: 10 = {1 its own, 2 merged from 20}. Splitting 1 would leave 10
    with only 2, and 2's detach (the operator's, or `unapply`'s group loop) would then leave 10
    active with no advert: a ghost card holding its notes and pipeline card. So 1 stays
    (`last_native`) and the merged advert goes home instead; the property keeps its own."""
    for source in ("operator", "autodedup"):
        db = _Ledger({1: 10, 2: 20})
        group = _merged(db, [10, 20], source=source)["merge_group_id"]
        assert pi.detach_outcomes(db, [1, 2]) == {1: "last_native", 2: "detached"}
        out = detach_listing(db, 1, decided_by=OP)["data"]
        assert (out["detached"], out["outcome"], out["rulings_written"]) == (
            False, "last_native", 0)
        assert db.listings == {1: 10, 2: 10} and len(db.events) == 1
        undo = {"merge_group_id": group, "source": "autodedup"} if source == "autodedup" else {}
        assert detach_listing(db, 2, decided_by=OP, **undo)["data"]["outcome"] == "detached"
        assert db.listings == {1: 10, 2: 20} and db.props == {10: "active", 20: "active"}
        assert pi.detach_outcomes(db, [1]) == {1: "not_merged"}


def test_a_native_split_replans_under_the_lock():
    """Between the unlocked plan and the property lock, the only sibling left, the other own
    advert turned out merged in, or the advert itself moved: nothing is born, nothing ruled."""
    db = _Ledger({1: 10, 2: 10, 7: 70})
    dispatch = db.dispatch

    def meanwhile(change: Any) -> None:
        db.dispatch = lambda s, p: (change() if "FOR UPDATE" in s else None) or dispatch(s, p)

    meanwhile(lambda: db.listings.update({1: 70}))
    assert detach_listing(db, 2, decided_by=OP)["data"]["outcome"] == "not_merged"
    db.listings[1] = 10
    meanwhile(lambda: db.events.append({"id": 99, "group": "g", "survivor": 10, "listing": 1,
                                        "prev": 70, "source": "operator", "undone_by": None}))
    assert detach_listing(db, 2, decided_by=OP)["data"]["outcome"] == "last_native"
    db.events.clear()
    meanwhile(lambda: db.listings.update({2: 70}))
    assert detach_listing(db, 2, decided_by=OP)["data"]["outcome"] == "moved_since"
    assert db.events == [] and _verdicts(db) == [] and set(db.props) == {10, 70}


# --- the set writer: one lock, one ruling pass, one after-step -----------------------------


def _set(db: _Ledger, ids: list[int], **kw: Any) -> dict[str, Any]:
    return detach_listings(db, ids, decided_by=kw.pop("decided_by", OP), **kw)["data"]


def test_a_set_detach_rules_movers_only_against_the_stayers():
    """10 = {1 its own, 2 and 3 merged from 20}: detaching 2 and 3 in ONE call sends both home,
    reactivates 20 once (one carrier walk), rules each `different` from 1 only (never from each
    other: they sit together again) and brings 10 and 20 current ONCE."""
    db = _Ledger({1: 10, 2: 20, 3: 20})
    group = _merged(db, [10, 20])["merge_group_id"]
    for seen in (db.log, db.carried, db.changed, db.browse, db.broker):
        seen.clear()
    out = _set(db, [3, 2], reason="jiné patro")
    assert [(a["listing_id"], a["outcome"], a["left_property_id"], a["restored_property_id"],
             a["reactivated"], a["merge_group_ids"]) for a in out["adverts"]] == [
        (3, "detached", 10, 20, True, [group]), (2, "detached", 10, 20, False, [group])]
    assert db.listings == {1: 10, 2: 20, 3: 20} and db.props[20] == "active"
    note = "operator detach from 10: jiné patro"
    assert sorted(_verdicts(db)) == [(1, 2, "different", note), (1, 3, "different", note)]
    assert out["rulings_written"] == 2 and db.word(2, 3) is None and (2, 3) not in db.mnl
    assert len(_restores(db)) == 1
    assert db.changed == db.browse == db.broker == [[10, 20]], "the after-step runs once per set"
    # every property the steps lock, locked first in id order, before anything moves
    first_lock = next(i for i, (s, _p) in enumerate(db.log) if s.endswith("FOR UPDATE"))
    assert db.log[first_lock][1] == {"ids": [10, 20]}
    assert first_lock < next(i for i, (s, _p) in enumerate(db.log) if s.startswith("UPDATE"))


def test_a_set_of_native_adverts_is_born_apart_and_ruled_against_the_stayer():
    db = _Ledger({1: 10, 2: 10, 3: 10})
    out = _set(db, [2, 3])
    born = [a["restored_property_id"] for a in out["adverts"]]
    assert [a["outcome"] for a in out["adverts"]] == ["split_native", "split_native"]
    assert db.listings == {1: 10, 2: born[0], 3: born[1]} and 10 not in born
    assert {(lo, hi) for lo, hi, _v, _n in _verdicts(db)} == {(1, 2), (1, 3)}
    assert db.changed == [sorted({10, *born})]
    # the last own advert stays: the call's earlier adverts are seen by each re-plan
    db = _Ledger({1: 10, 2: 10})
    out = _set(db, [1, 2])
    assert [a["outcome"] for a in out["adverts"]] == ["split_native", "not_merged"]
    assert out["rulings_written"] == 1 and len(db.changed) == 1


def test_an_unknown_advert_refuses_the_set_before_anything_moves():
    db = _Ledger({1: 10, 2: 20})
    _merged(db, [10, 20])
    before, db.log[:] = (dict(db.listings), dict(db.props), [dict(e) for e in db.events]), []
    with pytest.raises(MergeError, match="listing 99 not found"):
        _set(db, [2, 99, 98])
    assert (dict(db.listings), dict(db.props), [dict(e) for e in db.events]) == before
    assert not any(s.startswith(("UPDATE", "INSERT")) or s.endswith("FOR UPDATE")
                   for s, _p in db.log)
    assert db.changed == [[10, 20]], "only the merge's after-step"


def test_an_empty_set_is_a_no_op():
    db = _Ledger({1: 10})
    out = detach_listings(db, [], decided_by=OP)
    assert out["data"] == {"adverts": [], "rulings_written": 0}
    assert out["metadata"]["tool"] == "detach_listings"
    assert db.log == [] and db.changed == []


def test_detach_outcomes_answers_propose_only_for_the_engine():
    """Decision 9, read-only: what the writer would answer each source now."""
    db = _Ledger({1: 10, 2: 10, 3: 30})
    _merged(db, [10, 30], source="autodedup")
    assert pi.detach_outcomes(db, [1, 2, 3]) == {1: "split_native", 2: "split_native",
                                                3: "detached"}
    assert pi.detach_outcomes(db, [1, 2, 3], source="autodedup") == {
        1: "propose_only", 2: "propose_only", 3: "detached"}
    engine = _set(db, [1], decided_by="autodedup-reconcile", source="autodedup")
    assert engine["adverts"][0]["outcome"] == "propose_only"


# --- the origins route (the split route: tests/test_property_split.py) --------------------


fastapi = pytest.importorskip("fastapi")


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    import api.property_merge as pm
    from api import dependencies as deps
    from api import main as api_main

    db = _Ledger({1: 10, 2: 20, 3: 30})
    _merged(db, [10, 20])
    db.log.clear()
    monkeypatch.setattr(pm, "resolve_active_property_id",
                        lambda conn, pid: {10: 10, 20: 10}.get(pid))
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: db
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {
        "is_admin": True, "email": OP}
    yield TestClient(api_main.app), db
    api_main.app.dependency_overrides.clear()


def test_the_origins_route_says_where_each_advert_came_from(client):
    http, _db = client
    res = http.get("/properties/10/origins")
    assert res.status_code == 200
    assert res.json() == {"property_id": 10, "adverts": [
        {"listing_id": 1, "origin_property_id": None, "merge_source": None, "merged_at": None,
         "detach_outcome": "last_native", "splittable": False},
        {"listing_id": 2, "origin_property_id": 20, "merge_source": "operator",
         "merged_at": T0.isoformat().replace("+00:00", "Z"), "detach_outcome": "detached",
         "splittable": True}]}
