"""The one merge, `toolkit.property_identity.merge_property_set` (decisions 8 and 17): the oldest
record survives, one asset link rides onto it and two refuse, ONE group and ONE after-step
(`properties_changed`) per set, every carrier walked per retired property, the operator's cards
ruled "same"; and migration 560's copy. Over tests/_property_ledger's stateful fake, each carrier
recorded at the seam."""

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
    _Ledger,
    keep_real,
    ledger_carriers,
)
from tests.test_detach_listing import _appended
from toolkit import property_carriers as carriers
from toolkit.property_carriers import MergeStep
from toolkit.property_identity import AssetLinkConflict, CategoryClash, MergeError

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


def test_the_one_asset_link_rides_onto_the_older_survivor():
    db = _Ledger({1: 3, 2: 7}, first_seen={7: T0 + timedelta(days=1)}, assets={7: 42})
    out = _merge(db, [3, 7])
    assert out["survivor_id"] == 3 and db.assets == {3: 42, 7: None}
    assert db.sql("INSERT INTO asset_membership_events") == [
        {"survivor": 3, "retired": 7, "asset": 42,
         "reason": f"merge {out['merge_group_id']}", "source": "auto"}]


def test_two_units_linked_into_one_asset_are_the_operators_to_merge_never_the_engines():
    """The link is the operator's "different units, do not collapse" (rule 15, E903): the
    engine is refused; the operator's own merge keeps the one link on the survivor."""
    held_twice = _Ledger({1: 3, 2: 7}, assets={3: 41, 7: 41})
    with pytest.raises(AssetLinkConflict):
        _merge(held_twice, [3, 7])
    assert held_twice.events == []
    assert _merge(held_twice, [3, 7], source="operator", decided_by=OP)["survivor_id"] == 3
    assert held_twice.assets == {3: 41, 7: None}


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


@pytest.mark.parametrize("other", ["pronajem", "drazba"])
def test_a_share_sale_is_refused_with_a_rental_or_an_auction(other):
    db = _Ledger({1: 3, 2: 7}, cats={3: ("podil", "pozemek"), 7: (other, "pozemek")})
    with pytest.raises(CategoryClash, match="category_type"):
        _merge(db, [3, 7])
    assert db.events == [] and db.listings == {1: 3, 2: 7}


def test_a_share_sale_merges_with_a_sale():
    """E932 (operator 2026-09-30): sreality alone files a share sale as `podil`; every other
    portal lists it as `prodej`. The gate reads the deal class; price is the engine's."""
    db = _Ledger({1: 3, 2: 7}, cats={3: ("podil", "pozemek"), 7: ("prodej", "pozemek")})
    assert _merge(db, [3, 7])["retired_ids"] == [7]
    assert set(db.listings.values()) == {3}


def test_two_different_asset_links_refuse_the_set_before_anything_merges():
    db = _Ledger({1: 3, 2: 7, 3: 9}, assets={3: 41, 9: 42})
    with pytest.raises(AssetLinkConflict):
        _merge(db, [3, 7, 9])
    assert db.events == [] and ("rollback", None) in db.log


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

    def on_detach(self, cur, step):
        return None


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
                                                         "SELECT asset_id FROM properties",
                                                         "carrier:", "UPDATE properties SET"))]
    expected = []
    for rid in (7, 9):
        step = MergeStep(3, rid, group, "autodedup")
        expected += ["ledger", "repoint", *names, f"retire {rid}"]
        assert [e for e in db.carried if e[2].retired == rid] == [
            ("merge", name, step) for name in names if name != "asset_link"]
    labels = []
    for s, p in seen:
        if s.startswith("INSERT INTO property_merge_events"):
            labels.append("ledger")
        elif s.startswith("UPDATE listings SET property_id"):
            labels.append("repoint")
        elif s.startswith("SELECT asset_id FROM properties"):
            labels.append("asset_link")
        elif s.startswith("carrier:"):
            labels.append(s.removeprefix("carrier:"))
        else:
            labels.append(f"retire {p[1]}")
    assert labels == expected
    assert db.sql("DELETE FROM properties") == []


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
    assert _merge(db, [3, 7])["pairs_ruled_same"] == 0
    assert db.sql("autodedup.") == [] and db.sql("p.repr_listing_ref_id FROM") == []


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
