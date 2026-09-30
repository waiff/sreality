"""E927 (operator ruling 2026-09-30): a share-sale advert merges with the sale of the same
property, and the engine does it only at one stated price.

sreality alone files a share sale under its own deal type (`podil`); every other portal lists
the same advert as a plain sale. The deal CLASS is the one definition (toolkit.room_taxonomy);
the price condition is the engine's own limb (guards.share_price_conflict)."""

from __future__ import annotations

from typing import Any

import pytest

from autodedup.apply import SKIP_CATEGORY_TYPE, Member, Scope, _set_reasons
from autodedup.dataset import Listing, Location
from autodedup.decide import decide_pair
from autodedup.features import ABSENT, FEATURE_ORDER
from autodedup.fingerprint import Fingerprint, build_fingerprint
from autodedup.guards import (
    SHARE_PRICE_VETO,
    cluster_invariants_ok,
    pair_veto,
    share_price_conflict,
)
from autodedup.hazard_context import category_group, confusable_twins
from autodedup.indistinguishable import distinguishing_facts
from autodedup.settings import Settings
from tests.autodedup.whole_cohort import build_index
from toolkit.property_identity import category_clash
from toolkit.room_taxonomy import category_type_compatible, crosses_deal_type, deal_class_of

SETTINGS = Settings()


def _listing(listing_id: int, **over: Any) -> Listing:
    row = Listing(
        id=listing_id, block="b", source="sreality", category_main="pozemek",
        category_type="prodej", area_m2=1117.0, price=32_000.0, description="x" * 300,
        first_seen_at="2026-09-01T00:00:00+00:00", last_seen_at="2026-09-29T00:00:00+00:00",
        location=Location(obec_kod=572551, granularity_rank=60),
    )
    for key, value in over.items():
        setattr(row, key, value)
    return row


def _fp(listing_id: int, **over: Any) -> Fingerprint:
    return build_fingerprint(_listing(listing_id, **over), [], SETTINGS)


# ------------------------------------------------------------------ the one definition


def test_a_share_sale_is_in_the_sale_class_and_nothing_else_moves() -> None:
    assert deal_class_of("podil") == "prodej"
    assert [deal_class_of(t) for t in ("prodej", "pronajem", "drazba", None)] == [
        "prodej", "pronajem", "drazba", None]


@pytest.mark.parametrize(("a", "b", "compatible", "crosses"), [
    ("podil", "prodej", True, True),
    ("prodej", "podil", True, True),
    ("podil", "podil", True, False),
    ("prodej", "prodej", True, False),
    ("podil", "pronajem", False, False),
    ("podil", "drazba", False, False),
    ("drazba", "prodej", False, False),
    ("pronajem", "prodej", False, False),
    (None, "podil", True, False),
])
def test_deal_classes(a: str | None, b: str | None, compatible: bool, crosses: bool) -> None:
    assert category_type_compatible(a, b) is compatible
    assert crosses_deal_type(a, b) is crosses


# --------------------------------------------- the chokepoint and E925 read the class only


def test_the_chokepoint_gate_admits_a_share_sale_with_a_sale_at_any_price() -> None:
    assert category_clash(("podil", "pozemek"), ("prodej", "pozemek")) is None
    assert category_clash(("podil", "pozemek"), ("pronajem", "pozemek")) == (
        "category_type", "podil", "pronajem")
    assert category_clash(("drazba", "byt"), ("podil", "byt")) == (
        "category_type", "drazba", "podil")


# ------------------------------------------------------------- the rule floor (E2)


@pytest.mark.parametrize(("other", "expected"), [
    ("prodej", None), ("pronajem", "category_type"), ("drazba", "category_type")])
def test_pair_veto_reads_the_deal_class(other: str, expected: str | None) -> None:
    assert pair_veto(_fp(1, category_type="podil"), _fp(2, category_type=other)) == expected


def test_the_d43_deal_type_fact_reads_the_deal_class() -> None:
    share, sale = _listing(1, category_type="podil"), _listing(2, category_type="prodej")
    names = {fact.name for fact in distinguishing_facts(share, sale, None, SETTINGS)}
    assert "category_type" not in names
    rental = _listing(3, category_type="pronajem")
    names = {fact.name for fact in distinguishing_facts(share, rental, None, SETTINGS)}
    assert "category_type" in names


# ------------------------------------------------------- the price limb (E927)


@pytest.mark.parametrize(("share", "sale", "conflict"), [
    ({"price": 32_000.0}, {"price": 32_000.0}, False),
    # The coarser figure is the finer one printed to fewer digits (E160's identity bar).
    ({"price": 253_930.0}, {"price": 253_927.0}, False),
    # The paths met once; a later cut on one portal does not part them.
    ({"price": 30_000.0, "price_history": [("2026-09-01", 32_000.0)]},
     {"price": 32_000.0}, False),
    # A half share of the whole is two prices.
    ({"price": 16_000.0}, {"price": 32_000.0}, True),
    # 0.3 % apart and of one granularity: two numbers, not one rendered twice.
    ({"price": 32_100.0}, {"price": 32_000.0}, True),
    # A missing price is not the same price, on either side.
    ({"price": None}, {"price": 32_000.0}, True),
    ({"price": 32_000.0}, {"price": None}, True),
    ({"price": None}, {"price": None}, True),
])
def test_a_share_sale_and_a_sale_are_one_property_only_at_one_stated_price(
        share: dict[str, Any], sale: dict[str, Any], conflict: bool) -> None:
    a, b = _fp(1, category_type="podil", **share), _fp(2, category_type="prodej", **sale)
    assert share_price_conflict(a, b) is conflict
    assert share_price_conflict(b, a) is conflict


@pytest.mark.parametrize("pair", [("podil", "podil"), ("prodej", "prodej"),
                                  ("podil", "pronajem"), ("podil", None)])
def test_the_price_limb_reads_only_a_pair_that_crosses_deal_types(pair: tuple) -> None:
    a = _fp(1, category_type=pair[0], price=16_000.0)
    b = _fp(2, category_type=pair[1], price=32_000.0)
    assert share_price_conflict(a, b) is False


def _feats() -> dict[str, tuple[float, bool]]:
    return {name: ABSENT for name in FEATURE_ORDER}


class _Model:
    def predict_proba(self, feats: Any) -> float:
        return 0.999


def test_decide_vetoes_a_share_sale_priced_apart_from_its_sale() -> None:
    share = _listing(1, category_type="podil", price=16_000.0)
    sale = _listing(2, category_type="prodej", price=32_000.0)
    decision = decide_pair(build_fingerprint(share, [], SETTINGS),
                           build_fingerprint(sale, [], SETTINGS), share, sale, _feats(),
                           {"attr_area"}, _Model(), SETTINGS)
    assert (decision.zone, decision.veto, decision.reason) == (
        "veto", SHARE_PRICE_VETO, f"guard:{SHARE_PRICE_VETO}")


def test_decide_scores_a_share_sale_at_its_sales_price() -> None:
    share = _listing(1, category_type="podil")
    sale = _listing(2, category_type="prodej")
    decision = decide_pair(build_fingerprint(share, [], SETTINGS),
                           build_fingerprint(sale, [], SETTINGS), share, sale, _feats(),
                           {"attr_area"}, _Model(), SETTINGS)
    assert decision.zone != "veto"


# ------------------------------------------------------------- the cluster limb


def test_a_group_holds_a_share_sale_with_sales_at_its_price() -> None:
    members = [_fp(1, category_type="podil"), _fp(2, category_type="prodej", source="idnes"),
               _fp(3, category_type="prodej", source="bazos")]
    assert cluster_invariants_ok(members, SETTINGS) is None


def test_a_group_may_not_carry_a_share_sale_to_a_sale_at_another_price() -> None:
    members = [_fp(1, category_type="podil"), _fp(2, category_type="prodej", source="idnes"),
               _fp(3, category_type="prodej", source="bazos", price=40_000.0,
                   price_history=[("2026-09-01", 40_000.0)])]
    assert cluster_invariants_ok(members, SETTINGS) == SHARE_PRICE_VETO


def test_the_operators_closure_is_honoured_across_a_price_gap() -> None:
    members = [_fp(1, category_type="podil"), _fp(2, category_type="prodej", source="idnes"),
               _fp(3, category_type="prodej", source="bazos", price=40_000.0)]
    assert cluster_invariants_ok(members, SETTINGS, closure_of={1: 1, 3: 1}) is None


def test_a_group_never_mixes_the_sale_class_with_an_auction_or_a_rental() -> None:
    for other in ("drazba", "pronajem"):
        members = [_fp(1, category_type="podil"), _fp(2, category_type=other)]
        assert cluster_invariants_ok(members, SETTINGS) == "category_type"


# --------------------------------------------------------------------- blocking


def test_a_share_sale_meets_its_sale_on_the_attribute_and_town_probes() -> None:
    settings = Settings(attr_probe_town_grain=True)
    share = build_fingerprint(_listing(1, category_type="podil"), [], settings)
    sale = build_fingerprint(_listing(2, category_type="prodej", source="idnes"), [], settings)
    rental = build_fingerprint(_listing(3, category_type="pronajem", source="idnes"), [],
                               settings)
    index = build_index([share, sale, rental], settings)
    shared = set(index.index_keys(share)) & set(index.probe_keys(sale))
    assert {"attr_area", "town"} <= {probe for probe, _key in shared}
    # The text probe is type-free (E14); the attribute probes keep the rental out.
    reached = set(index.index_keys(rental)) & set(index.probe_keys(share))
    assert {probe for probe, _key in reached} == {"text"}
    assert index.price_cuts.keys() == {("pozemek", "prodej"), ("pozemek", "pronajem")}


# -------------------------------------------------------- the hazard cell and apply


def test_the_hazard_cell_and_its_twins_read_the_deal_class() -> None:
    share, sale = _listing(1, category_type="podil"), _listing(2, category_type="prodej")
    rental = _listing(3, category_type="pronajem")
    assert category_group(share) == category_group(sale) == "pozemek|prodej"
    assert confusable_twins(share, [share, sale, rental]) == [2]


def test_apply_does_not_refuse_a_share_sale_with_a_sale() -> None:
    members = {1: Member(1, 10, "podil", "pozemek"), 2: Member(2, 20, "prodej", "pozemek")}
    props = [{"category_type": "podil", "category_main": "pozemek"},
             {"category_type": "prodej", "category_main": "pozemek"}]
    reasons, _ = _set_reasons({1, 2}, members, props, Scope())
    assert SKIP_CATEGORY_TYPE not in reasons
    members[2] = Member(2, 20, "drazba", "pozemek")
    reasons, detail = _set_reasons({1, 2}, members, props[:1], Scope())
    assert SKIP_CATEGORY_TYPE in reasons and detail["category_types"] == ["drazba", "podil"]
