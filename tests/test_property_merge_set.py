"""The one merge, `toolkit.property_identity.merge_property_set` (decisions 8 and 17): the oldest
record survives, ONE group and ONE after-step
(`properties_changed`) per set, every carrier walked per retired property and what it carried
written to the carry record before the retire, rule 15's gate over the set's ads, the operator's
cards ruled "same"; and migration 560's copy. Over tests/_property_ledger's stateful fake, each
carrier recorded at the seam."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

import toolkit.property_identity as pi
from tests._property_ledger import (  # noqa: F401 — the fixture
    OP,
    T0,
    RecordingCarrier,
    _Ledger,
    keep_real,
    ledger_carriers,
)
from tests.test_detach_listing import _appended
from toolkit import property_carriers as carriers
from toolkit.property_carriers import Carried, MergeStep
from toolkit.property_identity import CategoryClash, MergeError

pytestmark = pytest.mark.usefixtures("ledger_carriers")

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "560_one_merge_one_undo.sql"


def _merge(db: _Ledger, ids: list[int], source: str = "autodedup", **kw) -> dict:
    return pi.merge_property_set(db, ids, source=source, reason="r", **kw)["data"]


def test_the_oldest_record_survives_whatever_holds_more_adverts():
    db = _Ledger({1: 7, 2: 7, 3: 7, 4: 3, 5: 9},
                 first_seen={7: T0 + timedelta(days=3), 3: T0 + timedelta(days=1), 9: T0})
    out = _merge(db, [7, 3, 9])
    assert (out["survivor_id"], out["retired_ids"], out["listings_moved"]) == (9, [3, 7], 4)
    assert {e["group"] for e in db.events} == {out["merge_group_id"]}, "ONE group per set"
    assert set(db.listings.values()) == {9}
    assert pi.survivor_of({5: T0, 2: T0, 9: T0 + timedelta(days=1)}) == 2
    assert pi.survivor_of({1: None, 4: T0}) == 4 and pi.survivor_of({6: None, 3: None}) == 3


def test_the_set_needs_two_active_properties_and_an_operator_merge_an_identity():
    with pytest.raises(MergeError):
        _merge(_Ledger({1: 5}), [5, 5])
    with pytest.raises(MergeError, match="not active"):
        _merge(_Ledger({1: 3}, props={7: "merged_away"}), [3, 7])
    with pytest.raises(MergeError, match="decided_by"):
        _merge(_Ledger({1: 3, 2: 7}), [3, 7], source="operator")


@pytest.mark.parametrize("cats, clash", [
    ({3: (None, "byt"), 7: ("prodej", "byt"), 9: ("pronajem", "byt")}, "category_type"),
    ({3: ("prodej", None), 7: ("prodej", "byt"), 9: ("prodej", "dum")}, "category_main"),
])
def test_a_set_whose_members_clash_is_refused_though_the_survivor_is_unknown(cats, clash):
    """The survivor's stored category is recomputed once, after the whole set: a NULL there
    must not let a sale and a rent (or a flat and a house) through, pair by pair."""
    db = _Ledger({1: 3, 2: 7, 3: 9}, cats=cats)
    with pytest.raises(CategoryClash, match=clash) as refused:
        _merge(db, [3, 7, 9])
    assert refused.value.field == clash
    assert db.events == [] and db.listings == {1: 3, 2: 7, 3: 9}
    sanctioned = _Ledger({1: 3, 2: 7, 3: 9},
                         cats={3: ("prodej", None), 7: ("prodej", "dum"), 9: ("prodej", "komercni")})
    assert _merge(sanctioned, [3, 7, 9])["retired_ids"] == [7, 9]


def _by(source: str) -> dict[str, str]:
    return {"decided_by": OP} if source == "operator" else {}


@pytest.mark.parametrize("source", ["operator", "autodedup"])
@pytest.mark.parametrize("other", ["dum", "komercni"])
def test_land_merges_with_a_house_or_a_commercial_property(source, other):
    """E935 (2026-10-04): a pozemek and a dům or komerční property are one property when the
    operator or the engine says so — property 38803's plot with a house, filed as a house on
    one portal and as land on another."""
    db = _Ledger({1: 3, 2: 7}, cats={3: ("prodej", other), 7: ("prodej", "pozemek")},
                 canonical={3: 1, 7: 2})
    out = _merge(db, [3, 7], source=source, **_by(source))
    assert (out["survivor_id"], out["retired_ids"]) == (3, [7])
    assert db.listings == {1: 3, 2: 3}
    three = _Ledger({1: 3, 2: 7, 3: 9}, cats={3: ("prodej", "pozemek"), 7: ("prodej", "dum"),
                                              9: ("prodej", "komercni")})
    assert _merge(three, [3, 7, 9], source=source, **_by(source))["retired_ids"] == [7, 9]


@pytest.mark.parametrize("source", ["operator", "autodedup"])
def test_a_flat_merges_with_a_commercial_property(source):
    """E938 (2026-10-06): one studio filed as a flat on one portal and as a commercial unit on
    another is one property when the operator or the engine says so."""
    db = _Ledger({1: 3, 2: 7}, cats={3: ("prodej", "byt"), 7: ("prodej", "komercni")},
                 canonical={3: 1, 7: 2})
    out = _merge(db, [3, 7], source=source, **_by(source))
    assert (out["survivor_id"], out["retired_ids"]) == (3, [7])
    assert db.listings == {1: 3, 2: 3}


@pytest.mark.parametrize("source", ["operator", "autodedup"])
@pytest.mark.parametrize("other", ["dum", "pozemek"])
def test_a_set_of_a_flat_a_commercial_unit_and_a_house_or_land_is_refused(source, other):
    """Rule 15 is a set of PAIRS: komerční meets the flat and the house (or the land), and the
    set is still refused, on the flat–house (flat–land) pair, before anything merges."""
    db = _Ledger({1: 3, 2: 7, 3: 9}, cats={3: ("prodej", "byt"), 7: ("prodej", "komercni"),
                                           9: ("prodej", other)})
    with pytest.raises(CategoryClash, match="category_main") as refused:
        _merge(db, [3, 7, 9], source=source, **_by(source))
    assert (refused.value.field, refused.value.a, refused.value.b) == ("category_main", "byt", other)
    assert db.events == [] and db.listings == {1: 3, 2: 7, 3: 9}


@pytest.mark.parametrize("source", ["operator", "autodedup"])
@pytest.mark.parametrize("pair", [("byt", "pozemek"), ("byt", "dum"), ("ostatni", "pozemek")],
                         ids=["flat vs land", "flat vs house", "other vs land"])
def test_a_flat_never_merges_with_a_house_or_land_nor_other_with_land(source, pair):
    db = _Ledger({1: 3, 2: 7}, cats={3: ("prodej", pair[0]), 7: ("prodej", pair[1])},
                 canonical={3: 1, 7: 2})
    with pytest.raises(CategoryClash, match="category_main") as refused:
        _merge(db, [3, 7], source=source, **_by(source))
    assert (refused.value.field, refused.value.a, refused.value.b) == ("category_main", *pair)
    assert db.events == [] and db.listings == {1: 3, 2: 7}


def test_land_and_a_house_for_rent_and_for_sale_are_still_two_properties():
    db = _Ledger({1: 3, 2: 7}, cats={3: ("pronajem", "dum"), 7: ("prodej", "pozemek")})
    with pytest.raises(CategoryClash, match="category_type"):
        _merge(db, [3, 7])
    assert db.events == []


# --- the gate reads ads (operator, 2026-10-06; hand-over addendum 14) ----------------------

FLAT, HOUSE, SHOP, LAND = (("prodej", m) for m in ("byt", "dum", "komercni", "pozemek"))


@pytest.mark.parametrize("source", ["operator", "autodedup"])
def test_a_flat_and_a_house_never_meet_on_one_property_by_way_of_a_commercial_one(source):
    """Addendum 14: a byt + komerční property (10 after its first merge, stored komerční) with a
    house would put a flat and a house on one property; the stored category let it through. The
    refusal names the two properties and an ad of each."""
    db = _Ledger({1: 10, 2: 20, 3: 30}, ad_cats={1: SHOP, 2: FLAT, 3: HOUSE},
                 canonical={10: 1, 20: 2, 30: 3})
    assert _merge(db, [10, 20], source=source, **_by(source))["retired_ids"] == [20]
    with pytest.raises(CategoryClash) as refused:
        _merge(db, [10, 30], source=source, **_by(source))
    clash = refused.value
    assert (clash.field, clash.a, clash.b, clash.properties, clash.ads) == (
        "category_main", "byt", "dum", (10, 30), (2, 3))
    assert db.listings == {1: 10, 2: 10, 3: 30} and db.props[30] == "active"


def test_a_contentless_record_never_counts():
    """302749's shape: a cottage (dům) and a contentless record left under the default byt. A
    house joins it; a flat does not, the record being no flat; an all-contentless property joins
    anything, a rental house included."""
    def cottage() -> _Ledger:
        return _Ledger({1: 10, 2: 10, 3: 20, 4: 30, 5: 40}, contentless={2, 5},
                       ad_cats={1: HOUSE, 2: FLAT, 3: HOUSE, 4: FLAT, 5: FLAT})
    assert _merge(cottage(), [10, 20])["retired_ids"] == [20]
    with pytest.raises(CategoryClash) as refused:
        _merge(cottage(), [10, 30])
    assert (refused.value.properties, refused.value.ads) == ((10, 30), (1, 4))
    db = cottage()
    db.ad_cats[1] = ("pronajem", "dum")
    assert _merge(db, [10, 40])["retired_ids"] == [40]
    assert _merge(cottage(), [30, 40])["retired_ids"] == [40]


def test_a_pair_a_member_already_holds_is_not_this_merges():
    """The default: a property kept as one though it holds a flat and a house takes another flat;
    land, a pair none of the members holds, is refused."""
    held = {1: FLAT, 2: HOUSE, 3: FLAT, 4: LAND}
    assert _merge(_Ledger({1: 10, 2: 10, 3: 20}, ad_cats=held), [10, 20])["retired_ids"] == [20]
    with pytest.raises(CategoryClash, match="category_main"):
        _merge(_Ledger({1: 10, 2: 10, 4: 40}, ad_cats=held), [10, 40])


def test_the_whole_set_is_brought_current_once():
    """One `properties_changed` over the survivor and every retired id: the rollup (a retired id
    holds no advert, so the recompute skips it), the Browse patch (its row goes) and the broker
    queue, each once, after the last retire."""
    db = _Ledger({1: 1, 2: 2, 3: 3, 4: 3, 5: 4})
    _merge(db, [4, 3, 2, 1])
    assert db.changed == db.browse == db.broker == [[1, 2, 3, 4]]
    assert db.sql("status = 'merged_away'") == [(1, 2), (1, 3), (1, 4)]
    order = [s for s, _p in db.log]
    last_retire = max(i for i, s in enumerate(order) if "status = 'merged_away'" in s)
    assert last_retire < order.index(next(s for s in order if s.startswith("WITH batch AS")))


class _RefuseNine:
    name, columns, sql = "refuse_nine", (), ()

    def on_merge(self, cur, step):
        if step.retired == 9:
            raise MergeError("a carrier refused 9")
        return []


def test_a_refusal_on_a_later_pair_rolls_the_whole_set_back(monkeypatch):
    """7 merges in full, then a carrier aborts 9's: nothing of the set stands, 7's included."""
    monkeypatch.setattr(carriers, "PROPERTY_CARRIERS",
                        (_RefuseNine(), *carriers.PROPERTY_CARRIERS))
    db = _Ledger({1: 3, 2: 7, 3: 9})
    with pytest.raises(MergeError, match="refused 9"):
        _merge(db, [3, 7, 9])
    assert any(step.retired == 7 for s, step in db.log if s.startswith("carrier:"))
    assert db.listings == {1: 3, 2: 7, 3: 9} and db.events == []
    assert db.carried == [] and db.changed == db.browse == db.broker == []
    assert db.props == {3: "active", 7: "active", 9: "active"}
    assert db.sql("WITH batch AS") == []


def test_each_retired_property_walks_every_carrier_then_retires():
    """Per retired id, ascending: the ledger, the re-point, every carrier in list order with one
    `MergeStep`, then that id's retire; never a delete of a property."""
    db = _Ledger({1: 3, 2: 7, 3: 9, 4: 9})
    out = _merge(db, [9, 3, 7])
    group = out["merge_group_id"]
    names = [c.name for c in carriers.PROPERTY_CARRIERS]
    seen = [(s, p) for s, p in db.log if s.startswith(("INSERT INTO property_merge_events",
                                                         "UPDATE listings SET property_id",
                                                         "carrier:", "UPDATE properties SET"))]
    expected = []
    for rid in (7, 9):
        step = MergeStep(3, rid, group, "autodedup")
        expected += ["ledger", "repoint", *names, f"retire {rid}"]
        assert [e for e in db.carried if e[2].retired == rid] == [
            ("merge", name, step) for name in names]
    labels = []
    for s, p in seen:
        if s.startswith("INSERT INTO property_merge_events"):
            labels.append("ledger")
        elif s.startswith("UPDATE listings SET property_id"):
            labels.append("repoint")
        elif s.startswith("carrier:"):
            labels.append(s.removeprefix("carrier:"))
        else:
            labels.append(f"retire {p[1]}")
    assert labels == expected
    assert db.sql("DELETE FROM properties") == []


def test_each_step_writes_its_carry_rows_after_every_carrier_and_before_its_retire(monkeypatch):
    """One INSERT per carried row, under the merge's group with the step's survivor, after the
    last carrier and before that step's retire, so the next step's came-from lookups see it;
    a step that carried nothing writes none."""
    note = Carried("property_notes", 5, T0, None, 7, "moved", None)
    card = Carried("property_pipeline", None, T0, "acc", 7, "folded", {"stage_id": 2})
    monkeypatch.setattr(carriers, "PROPERTY_CARRIERS", (
        RecordingCarrier("notes", rows=(note,)), RecordingCarrier("pipeline", rows=(card,))))
    db = _Ledger({1: 3, 2: 7, 3: 9})
    group = _merge(db, [3, 7, 9])["merge_group_id"]
    assert [(c["table"], c["from_property"], c["survivor"], c["group"]) for c in db.carries] == [
        ("property_notes", 7, 3, group), ("property_pipeline", 7, 3, group)] * 2
    assert db.carries[1]["snapshot"].obj == {"stage_id": 2} and db.carries[0]["snapshot"] is None
    order = [s.split(" (")[0] if s.startswith("INSERT") else s for s, _p in db.log]
    retires = [i for i, s in enumerate(order) if s.startswith("UPDATE properties SET status")]
    assert [db.log[i][1][1] for i in retires] == [7, 9]
    for at in retires:
        assert order[at - 3: at] == ["carrier:pipeline", "INSERT INTO property_merge_carries",
                                     "INSERT INTO property_merge_carries"]
    monkeypatch.setattr(carriers, "PROPERTY_CARRIERS", (RecordingCarrier("notes"),))
    quiet = _Ledger({1: 3, 2: 7})
    _merge(quiet, [3, 7])
    assert quiet.carries == [] and quiet.sql("property_merge_carries") == []


def test_an_operator_merge_rules_every_cross_pair_of_the_ticked_cards_same():
    db = _Ledger({30: 3, 31: 3, 70: 7, 90: 9}, canonical={3: 30, 7: 70, 9: 90})
    out = _merge(db, [3, 7, 9], source="operator", decided_by=OP)
    rows = _appended(db)
    assert [(r["listing_lo"], r["listing_hi"]) for r in rows] == [(30, 70), (30, 90), (70, 90)]
    assert {(r["verdict"], r["decided_by"], r["note"]) for r in rows} == {
        ("same", OP, f"operator merge {out['merge_group_id']}")}
    assert out["pairs_ruled_same"] == 3, "31, grouped there by someone else, is never ruled"
    # "same" takes back the operator's own veto on each pair; it never writes one
    assert len(db.sql("DELETE FROM autodedup.must_not_link")) == 3
    assert db.sql("INSERT INTO autodedup.must_not_link") == []
    order = [s for s, _p in db.log]
    first_merge = next(i for i, s in enumerate(order) if "INTO property_merge_events" in s)
    assert order.index(next(s for s in order if "p.repr_listing_ref_id FROM" in s)) < first_merge
    # the rulings come before the after-step (the one lock order, rulings then `properties`)
    last_ruling = max(i for i, s in enumerate(order) if "autodedup." in s)
    assert last_ruling < next(i for i, s in enumerate(order) if s.startswith("WITH batch AS"))
    assert db.changed == [[3, 7, 9]]


def test_an_engine_merge_is_never_a_ruling():
    db = _Ledger({30: 3, 70: 7}, canonical={3: 30, 7: 70})
    out = _merge(db, [3, 7])
    assert out["pairs_ruled_same"] == out["rulings_taken_back"] == 0
    assert db.sql("autodedup.") == [] and db.sql("p.repr_listing_ref_id FROM") == []


def test_an_operator_merge_counts_the_different_rulings_it_takes_back():
    """A canonical pair whose newest ruling was negative is taken back; one newest "same" is
    not."""
    db = _Ledger({30: 3, 70: 7, 90: 9}, canonical={3: 30, 7: 70, 9: 90})
    db.rule(30, 70, "different")
    db.rule(30, 90, "same")
    db.rule(70, 90, "different")
    db.rule(70, 90, "same")
    out = _merge(db, [3, 7, 9], source="operator", decided_by=OP)
    assert (out["pairs_ruled_same"], out["rulings_taken_back"]) == (3, 1)


def test_an_operator_merge_takes_back_every_different_between_its_members():
    """MS12: 3 = {30, 31}, 7 = {70}. The cross negative (31, 70) is ruled "same" with the merge's
    note and its must-not-link retracted; the negative inside 3, (30, 31), is not between what
    the merge joins and stands; a negative set spanning both gets its cluster "same", a set
    inside 3 does not; the count is the preview's."""
    db = _Ledger({30: 3, 31: 3, 70: 7}, canonical={3: 30, 7: 70})
    db.rule(31, 70, "different", note="jiné patro")
    db.rule(30, 31, "same_building_different_unit")
    db.rule_set(500, [31, 70], "different")
    db.rule_set(501, [30, 31], "different")
    assert pi.merge_preview(db, [7, 3]) == {"property_ids": [3, 7], "rulings_taken_back": 2}
    out = _merge(db, [3, 7], source="operator", decided_by=OP)
    note = f"operator merge {out['merge_group_id']}"
    assert (out["pairs_ruled_same"], out["rulings_taken_back"]) == (2, 2)
    assert db.word(31, 70) == ("same", note) and db.word(30, 70) == ("same", note)
    assert (31, 70) not in db.mnl and db.history(31, 70)[0][0] == "different"
    assert db.word(30, 31)[0] == "same_building_different_unit" and (30, 31) in db.mnl
    assert db.newest_set([31, 70])["verdict"] == "same"
    assert db.newest_set([31, 70])["note"] == note
    assert db.newest_set([30, 31])["verdict"] == "different"
    assert len(db.sets) == 3, "one cluster row appended, under the set's own key"


def test_an_engine_merge_takes_back_nothing():
    db = _Ledger({30: 3, 70: 7}, canonical={3: 30, 7: 70})
    db.rule(30, 70, "different")
    db.rule_set(500, [30, 70], "different")
    out = _merge(db, [3, 7])
    assert out["rulings_taken_back"] == 0 and db.word(30, 70)[0] == "different"
    assert len(db.sets) == 1 and db.sql("autodedup.") == []


def _alerted() -> _Ledger:
    """3 survives 7. Collection 5 alerted 'new' on both (twins, subscription and snapshot NULL)
    and a price drop on 7; collection 6 (another account's) 'new' on 7 only; a watchdog
    subscription 'new' on each, different subscriptions. Every alert on 7 was delivered."""
    return _Ledger(
        {1: 3, 2: 7},
        dispatches={
            "s-new": {"property_id": 3, "collection_id": 5, "change_kind": "new"},
            "r-new": {"property_id": 7, "collection_id": 5, "change_kind": "new"},
            "r-drop": {"property_id": 7, "collection_id": 5, "change_kind": "price_drop",
                       "trigger_snapshot_id": 99},
            "b-new": {"property_id": 7, "collection_id": 6, "change_kind": "new"},
            "s-sub": {"property_id": 3, "subscription_id": "sub-1", "change_kind": "new"},
            "r-sub": {"property_id": 7, "subscription_id": "sub-2", "change_kind": "new"},
        },
        sends={
            1: {"consumer": "collection_monitor", "notification_id": "r-new"},
            2: {"consumer": "collection_monitor", "notification_id": "r-drop"},
            3: {"consumer": "collection_monitor", "notification_id": "b-new"},
            4: {"consumer": "watchdog", "notification_id": "r-sub"},
            5: {"consumer": "collection_monitor", "notification_id": "s-new"},
            6: {"consumer": "outreach", "notification_id": None},
        },
    )


def test_a_delivered_alert_moves_to_the_kept_twin_and_the_merge_commits(monkeypatch):
    """The collapse would null a send's event (ON DELETE SET NULL), which channel_sends_check
    refuses: the send moves to the survivor's twin first. Only a twin pairs, every key equal
    (NULL with NULL), so another collection's or subscription's alert moves with its row."""
    keep_real(monkeypatch, carriers.Dispatches())
    db = _alerted()
    _merge(db, [3, 7], source="operator", decided_by=OP)
    assert {nid: row["property_id"] for nid, row in db.dispatches.items()} == {
        "s-new": 3, "r-drop": 3, "b-new": 3, "s-sub": 3, "r-sub": 3}
    assert {sid: send["notification_id"] for sid, send in db.sends.items()} == {
        1: "s-new", 2: "r-drop", 3: "b-new", 4: "r-sub", 5: "s-new", 6: None}
    order = [s for s, _p in db.log]
    resend = order.index(next(s for s in order if s.startswith("UPDATE channel_sends")))
    assert resend < order.index(next(s for s in order
                                     if s.startswith("DELETE FROM notification_dispatches")))
    assert db.sql("UPDATE channel_sends") == [{"retired": 7, "survivor": 3}]


def test_without_the_resend_the_collapse_strands_a_send_and_the_set_rolls_back(monkeypatch):
    """The defect the resend fixes, modelled: the plain SET collapse aborts the whole merge."""
    keep_real(monkeypatch, carriers.CurationTable("notification_dispatches",
                                                  carriers.Dispatches().keys))
    db = _alerted()
    before = ({k: dict(v) for k, v in db.dispatches.items()},
              {k: dict(v) for k, v in db.sends.items()})
    with pytest.raises(psycopg.errors.CheckViolation, match="channel_sends_check"):
        _merge(db, [3, 7], source="operator", decided_by=OP)
    assert (db.dispatches, db.sends) == before
    assert db.listings == {1: 3, 2: 7} and db.events == [] and db.props == {3: "active",
                                                                           7: "active"}


def _claimed() -> _Ledger:
    """3 survives 7, collection 5 alerted 'new' on both; nothing delivered yet, but the outbox
    is claiming the send for 7's alert (its insert not yet committed) as the merge runs."""
    return _Ledger(
        {1: 3, 2: 7},
        dispatches={"s-new": {"property_id": 3, "collection_id": 5, "change_kind": "new"},
                    "r-new": {"property_id": 7, "collection_id": 5, "change_kind": "new"}},
        claiming={1: {"consumer": "collection_monitor", "notification_id": "r-new"}},
    )


def test_a_send_the_outbox_claims_mid_merge_waits_for_the_lock_and_moves_too(monkeypatch):
    """The lock on the retired rows runs as its own statement before the resend: it waits for
    the in-flight claim to commit, so the resend sees that send and the collapse strands none."""
    keep_real(monkeypatch, carriers.Dispatches())
    db = _claimed()
    _merge(db, [3, 7], source="operator", decided_by=OP)
    assert db.claiming == {} and db.sends == {
        1: {"consumer": "collection_monitor", "notification_id": "s-new"}}
    assert set(db.dispatches) == {"s-new"}
    order = [s for s, _p in db.log]
    lock = order.index(next(s for s in order if s.endswith("FOR UPDATE")
                            and "notification_dispatches" in s))
    resend = order.index(next(s for s in order if s.startswith("UPDATE channel_sends")))
    assert lock < resend
    assert db.log[lock][1] == {"retired": 7, "survivor": 3}


def test_without_the_lock_a_claim_lands_after_the_resend_and_the_set_rolls_back(monkeypatch):
    """The window the lock closes, modelled: an unserialized claim commits between the resend
    and the collapse, whose SET NULL then trips channel_sends_check."""
    unlocked = carriers.Dispatches()
    unlocked.sql = tuple(s for s in unlocked.sql if s != carriers.Dispatches.LOCK_SQL)
    keep_real(monkeypatch, unlocked)
    with pytest.raises(psycopg.errors.CheckViolation, match="channel_sends_check"):
        _merge(_claimed(), [3, 7], source="operator", decided_by=OP)


def _code() -> str:
    body = "\n".join(line.split("--")[0] for line in MIGRATION.read_text().lower().splitlines())
    return re.sub(r"\s+", " ", body)


def test_the_copy_rules_only_live_operator_merges_same_and_is_idempotent():
    code = _code()
    for fragment in (
        "where e.source = 'operator'", "having bool_or(e.undone_at is null)",
        "'pair', p.lo, p.hi, 'same'", "'operator', p.merged_at",
        # never over a ruling or an operator veto already on the pair
        "select 1 from autodedup.verdicts v where v.kind = 'pair'", "n.source = 'operator'",
        "on conflict (kind, listing_lo, listing_hi, decided_by) where kind = 'pair' do nothing;",
        # a side is the advert's origin; a pair must still share one property
        "order by v.listing_ref_id, v.id", "and b.side <> a.side and b.now_on = a.now_on",
        "create index if not exists property_merge_events_listing_live_idx on "
        "property_merge_events (listing_ref_id, id) where undone_at is null;",
    ):
        assert fragment in code, fragment
    assert not re.search(r"\b(update|delete from|drop|truncate)\b", code), "additive only"
