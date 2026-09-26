"""The one merge, `toolkit.property_identity.merge_property_set` (decisions 8 and 17): the oldest
record survives, one asset link rides onto it and two refuse, ONE group and ONE recompute per
set, the operator's cards ruled "same"; and migration 560's copy. Over tests/test_detach_listing's
stateful fake."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest

import toolkit.property_identity as pi
from tests.test_detach_listing import OP, T0, _Ledger
from toolkit.property_identity import AssetLinkConflict, MergeError

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
    with pytest.raises(MergeError, match=clash):
        _merge(db, [3, 7, 9])
    assert db.events == [] and db.listings == {1: 3, 2: 7, 3: 9}
    sanctioned = _Ledger({1: 3, 2: 7, 3: 9},
                         cats={3: ("prodej", None), 7: ("prodej", "dum"), 9: ("prodej", "komercni")})
    assert _merge(sanctioned, [3, 7, 9])["retired_ids"] == [7, 9]


def test_two_different_asset_links_refuse_the_set_before_anything_merges():
    db = _Ledger({1: 3, 2: 7, 3: 9}, assets={3: 41, 9: 42})
    with pytest.raises(AssetLinkConflict):
        _merge(db, [3, 7, 9])
    assert db.events == [] and ("rollback", None) in db.log


def test_the_survivor_is_recomputed_once_for_the_whole_set():
    db = _Ledger({1: 1, 2: 2, 3: 3, 4: 3, 5: 4})
    _merge(db, [4, 3, 2, 1])
    assert [p["pid"] for p in db.sql("WITH batch AS")] == [1]
    assert db.sql("DELETE FROM browse_list") == [([1, 2, 3, 4],)]
    assert db.sql("status = 'merged_away'") == [(1, 2), (1, 3), (1, 4)]


def test_a_refusal_on_a_later_pair_rolls_the_whole_set_back(monkeypatch):
    chokepoint = pi.merge_properties

    def refuse_nine(conn, **kw):
        if kw["retired_id"] == 9:
            raise MergeError("category_main mismatch (byt vs dum)")
        return chokepoint(conn, **kw)

    monkeypatch.setattr(pi, "merge_properties", refuse_nine)
    db = _Ledger({1: 3, 2: 7, 3: 9})
    with pytest.raises(MergeError):
        _merge(db, [3, 7, 9])
    assert db.listings == {1: 3, 2: 7, 3: 9} and db.events == []
    assert db.sql("WITH batch AS") == []


def test_an_operator_merge_rules_every_cross_pair_of_the_ticked_cards_same():
    db = _Ledger({30: 3, 31: 3, 70: 7, 90: 9}, canonical={3: 30, 7: 70, 9: 90})
    out = _merge(db, [3, 7, 9], source="operator", decided_by=OP)
    rows = db.sql("INSERT INTO autodedup.verdicts")
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


def test_an_engine_merge_is_never_a_ruling():
    db = _Ledger({30: 3, 70: 7}, canonical={3: 30, 7: 70})
    assert _merge(db, [3, 7])["pairs_ruled_same"] == 0
    assert db.sql("autodedup.") == [] and db.sql("p.repr_listing_ref_id FROM") == []


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
