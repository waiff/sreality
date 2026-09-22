"""E140-E145: the five readings W15 adds, each against the adverts that produced it.

Every positive example below is a real body from the W14 cohorts (the listing id is named);
every negative is the shape the same rule must NOT fire on, which is where each of these
readings was nearly wrong the first time.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from autodedup.dataset import Listing, Location
from autodedup.indistinguishable import (
    distinguishing_facts,
    offered_extent,
    overlap_days,
    two_unit_signature,
)
from autodedup.settings import Settings
from autodedup.text_facts import (
    accessory_designators,
    capacity_counts,
    offered_room_counts,
    parcel_numbers,
)

W15 = Settings(
    d43_parcel_numbers=True,
    d43_accessory_designators=True,
    d43_offered_extent=True,
    d43_two_unit_signature=True,
    floor_same_source_feed="broker",
)


def listing(listing_id: int, **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "id": listing_id, "block": "b", "source": "sreality",
        "category_main": "byt", "category_type": "prodej",
        "location": Location(obec_kod=1, street_key="zizkova"),
        "first_seen_at": "2026-05-01T00:00:00+00:00",
        "last_seen_at": "2026-09-01T00:00:00+00:00",
    }
    fields.update(kwargs)
    return Listing(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- E140 the parcel
@pytest.mark.parametrize("text, want", [
    # 403714 / 403716: the two Jílové u Držkova forest plots g8 fused.
    ("Lesní pozemek o výměře 3 439 m2, parcelní číslo 934/11, podíl 1/1.", {"934/11"}),
    ("Lesní pozemek o výměře 3 532 m2, parcelní číslo 935/4, podíl 1/1.", {"935/4"}),
    # 184102: the colon form, with the area on the next line.
    ("Specifikace nemovitosti: Parcelní číslo: 2150/1 Celková výměra: 2 590 m²", {"2150/1"}),
    # 10418905: an entity-escaped body, and the `parc. č.` abbreviation.
    ("Nab&iacute;z&iacute;me pozemky:\n- parc. č. 1633 - v&yacute;měra 1 973 m2", {"1633"}),
    ("jedná se o pozemky p.č.2361 a p.č.2362 v k.ú. Semily", {"2361", "2362"}),
    ("prodej pozemku st. p. č. 44 - zastavěná plocha", {"44"}),
    ("parcelní čísla: 4544, 4554 a 4556 včetně porostu", {"4544", "4554", "4556"}),
    # `podíl 1/1` after a comma is a SHARE, not a second parcel.
    ("parcelní číslo 3255, podíl 1/1, katastrální území Velké Hamry", {"3255"}),
    # 166968: spaced thousands. Without the area guard this reads `2` and `393` as parcels and
    # makes two different Červenice plots share one.
    ("Pozemek Červenice - parc. č. 123, 2 393m² Nabízíme tři samostatné pozemky", {"123"}),
    ("Prodej bytu 3+kk, cena 5 499 000 Kč, číslo 12", set()),
])
def test_parcel_numbers(text: str, want: set[str]) -> None:
    assert parcel_numbers(text) == want


def test_parcel_conflict_is_disjointness_not_inequality() -> None:
    """A subset is not a conflict: an advert also names the access road and the neighbour."""
    a = listing(1, category_main="pozemek", description="parc. č. 934/11")
    b = listing(2, category_main="pozemek", description="parc. č. 935/4")
    assert [f.name for f in distinguishing_facts(a, b, None, W15)] == ["parcel"]
    same = listing(3, category_main="pozemek",
                   description="parc. č. 934/11 a přístup po parcele 2600/2")
    assert "parcel" not in {f.name for f in distinguishing_facts(a, same, None, W15)}
    assert "parcel" not in {f.name for f in distinguishing_facts(a, b, None, Settings())}


# ----------------------------------------------------------------- E141 the accessory number
def test_accessory_designators_reads_the_turnov_pair() -> None:
    """74774 against 464713 — one V Ráji flat's parking space against another's."""
    left = accessory_designators(
        "V ceně nájmu je zahrnuto jedno parkovací stání pro auto č. 47 v suterénu budovy "
        "s možností pronájmu dalšího parkovacího stání za 2.500,- Kč měsíčně.")
    right = accessory_designators(
        "V ceně nájmu je zahrnuto jedno parkovací stání pro auto č. 32 v suterénu budovy "
        "a sklepní kóje č. 25 s možností pronájmu dalšího parkovacího stání č. 6.")
    assert left == {"stani": frozenset({"47"})}
    assert right == {"stani": frozenset({"32", "6"}), "sklep": frozenset({"25"})}


def test_accessory_ignores_a_metro_stop_and_a_room_number() -> None:
    assert accessory_designators("stanice metra Jinonice je 10 minut pěšky") == {}
    assert accessory_designators("pokoj č. 2 (19 m2), pokoj č. 3 (13 m2)") == {}


def test_accessory_conflict_needs_both_sides_and_disjoint_numbers() -> None:
    a = listing(1, description="parkovací stání pro auto č. 47")
    b = listing(2, description="parkovací stání pro auto č. 32 a sklepní kóje č. 25")
    assert "accessory" in {f.name for f in distinguishing_facts(a, b, None, W15)}
    # A cellar named on one side only is silence, not a difference (E12).
    silent = listing(3, description="byt s balkonem, sklepní kóje č. 25")
    assert "accessory" not in {f.name for f in distinguishing_facts(a, silent, None, W15)}


# ------------------------------------------------------------------ E142 the offered extent
def test_capacity_needs_an_office_noun() -> None:
    """18574455 / 18574459: the Regus Empiria product tiers, and the prose that looks like it."""
    assert capacity_counts(
        "Tato nabídka zahrnuje soukromou servisovanou kancelář pro 1 osobu") == {1}
    assert capacity_counts(
        "soukromou servisovanou kancelář pro 2 pracovní místa a přístup do sdílených prostor"
    ) == {2}
    assert capacity_counts("Servisované kancelářské prostory pro 10 pracovních míst") == {10}
    assert capacity_counts("Zálohy na služby pro 1 osobu jsou 2 552 Kč.") == set()
    assert capacity_counts("Byt se nejlépe hodí pro 1 osobu, pracující, bez zvířat.") == set()
    assert capacity_counts("restaurace s kuchyní a barem pro 80 osob") == set()


def test_offered_rooms_needs_an_offer_verb() -> None:
    """552565 against 18013683 — one room at 8,500 against two joined at 12,000."""
    assert offered_room_counts("Pronajmu pokoj s vlastním WC a sprchovým koutem.") == {1}
    assert offered_room_counts("Pronajmu 2 spojené pokoje s vlastním WC.") == {2}
    assert offered_room_counts("Nejedná se o ubytovnu, v domě je jen 6 pokojů.") == set()
    assert offered_room_counts("Pronajmu byt 2+kk v centru, 55 m2.") == set()


def test_extent_reads_a_stated_capacity_whatever_the_prices_do() -> None:
    """43617 and 38043: Regus publishes the 1-person and the 2-desk product at ONE price."""
    a = listing(1, category_main="komercni", category_type="pronajem", price=16290.0,
                description="soukromou servisovanou kancelář pro 1 osobu")
    b = listing(2, category_main="komercni", category_type="pronajem", price=16290.0,
                description="soukromou servisovanou kancelář pro 2 pracovní místa")
    assert offered_extent(a, b, W15) is not None
    assert offered_extent(a, replace(b, price=15590.0), W15) is not None


def test_extent_conjunction_stays_available_as_a_dial() -> None:
    """The W14 group attack's reading, kept measurable: a price gap AND a co-live window."""
    conjunction = replace(W15, d43_offered_extent_requires_price_gap=True)
    a = listing(1, category_main="komercni", category_type="pronajem", price=16290.0,
                description="soukromou servisovanou kancelář pro 1 osobu")
    b = listing(2, category_main="komercni", category_type="pronajem", price=16290.0,
                description="soukromou servisovanou kancelář pro 2 pracovní místa")
    assert offered_extent(a, b, conjunction) is None
    priced = replace(b, price=10890.0)
    assert offered_extent(a, priced, conjunction) is not None
    later = replace(priced, first_seen_at="2026-10-01T00:00:00+00:00",
                    last_seen_at="2026-11-01T00:00:00+00:00")
    assert offered_extent(a, later, conjunction) is None


def test_extent_reads_a_strictly_larger_parcel_inventory() -> None:
    """239319 against 10473440: a share of one field against a share of four."""
    one = listing(1, category_main="pozemek", price=132152.0, description="podíl na p.č. 498/15")
    four = listing(2, category_main="pozemek", price=136060.0,
                   description="podíly na p.č. 498/15, 498/21, 498/43 a 498/44")
    assert offered_extent(one, four, W15) is not None
    # Intersecting but neither a subset: two portals parsing ONE body's parcel list differently
    # (the Krkonoše roubenka, 8 certain-duplicate pairs) is not an extent difference.
    left = listing(3, category_main="dum", price=12000000.0, description="p.č. 24 a 986/2")
    right = listing(4, category_main="dum", price=12000000.0,
                    description="p.č. 245, 79/2 a 986/2")
    assert offered_extent(left, right, W15) is None


# --------------------------------------------------------------- E143 the two-unit signature
def test_two_unit_signature_fires_on_the_rokytna_resort_pair() -> None:
    """25321 against 25322: 76.1 m² at 12,937,000 against 77.8 m² at 13,226,000, one rate."""
    a = listing(1, area_m2=76.1, price=12937000.0, disposition="3+kk",
                description="apartmán o dispozici 3+kk s podlahovou plochou 76,1 m²",
                price_history=[("2026-05-01T00:00:00+00:00", 12937000.0)])
    b = listing(2, area_m2=77.8, price=13226000.0, disposition="3+kk",
                description="apartmán o dispozici 3+kk s podlahovou plochou 77,8 m²",
                price_history=[("2026-05-01T00:00:00+00:00", 13226000.0)])
    assert two_unit_signature(a, b, W15)
    assert "two_unit" in {f.name for f in distinguishing_facts(a, b, None, W15)}
    assert "two_unit" not in {f.name for f in distinguishing_facts(a, b, None, Settings())}


def test_two_unit_signature_spares_a_rounded_area_at_one_price() -> None:
    """One advert on two portals: the area is rounded, the price is the same order."""
    a = listing(1, area_m2=76.1, price=12937000.0)
    b = listing(2, source="idnes", area_m2=76.0, price=12937000.0)
    assert not two_unit_signature(a, b, W15)


def test_two_unit_signature_spares_an_agreeing_price_path() -> None:
    """A price CUT one side already printed is a moment, not a second unit (E134/N2)."""
    a = listing(1, area_m2=64.3, price=3980000.0,
                price_history=[("2026-05-01T00:00:00+00:00", 3980000.0),
                               ("2026-08-01T00:00:00+00:00", 3690000.0)])
    b = listing(2, source="ceskereality", area_m2=64.0, price=3690000.0,
                price_history=[("2026-09-01T00:00:00+00:00", 3690000.0)])
    assert not two_unit_signature(a, b, W15)


def test_two_unit_signature_spares_an_area_basis_difference() -> None:
    """92809 against 530866: `užitná 51 m² (podlahová 55 m²)` both ways round, Harfa Living."""
    a = listing(1, area_m2=51.0, price=9500000.0,
                description="byt 2+kk o užitné ploše 51 m² (podlahová plocha 55 m²)")
    b = listing(2, source="idnes", area_m2=55.0, price=9300000.0,
                description="byt 2+kk o podlahové ploše 55 m² (užitná plocha 51 m²)")
    assert not two_unit_signature(a, b, W15)


def test_two_unit_area_tol_must_sit_below_the_engine_area_guard() -> None:
    with pytest.raises(ValueError, match="below area_band_pct"):
        Settings(d43_two_unit_signature=True, d43_two_unit_area_tol=0.03)


# ------------------------------------------------------------------ E144 the co-live overlap
def test_overlap_days_reads_a_duration() -> None:
    a = listing(1, first_seen_at="2026-05-01T00:00:00+00:00",
                last_seen_at="2026-06-01T00:00:00+00:00")
    b = listing(2, first_seen_at="2026-05-20T00:00:00+00:00",
                last_seen_at="2026-07-01T00:00:00+00:00")
    assert overlap_days(a, b) == pytest.approx(12.0)
    touching = listing(3, first_seen_at="2026-06-01T00:00:00+00:00",
                       last_seen_at="2026-07-01T00:00:00+00:00")
    assert overlap_days(a, touching) == pytest.approx(0.0)
    assert overlap_days(a, listing(4, first_seen_at=None)) is None


def test_colive_bar_drops_a_repost_boundary() -> None:
    """A three-day bar removes the pairs whose windows merely touch across a re-post."""
    cfg = replace(W15, d43_price_path=True, d43_price_colive_contradiction=True,
                  d43_price_colive_min_overlap_days=3.0)
    a = listing(1, area_m2=47.0, price=9125000.0,
                first_seen_at="2026-05-01T00:00:00+00:00",
                last_seen_at="2026-06-01T00:00:00+00:00")
    touching = listing(2, area_m2=47.0, price=3999000.0,
                       first_seen_at="2026-05-31T12:00:00+00:00",
                       last_seen_at="2026-07-01T00:00:00+00:00")
    assert "price" not in {f.name for f in distinguishing_facts(a, touching, None, cfg)}
    overlapping = replace(touching, first_seen_at="2026-05-02T00:00:00+00:00")
    assert "price" in {f.name for f in distinguishing_facts(a, overlapping, None, cfg)}


# ----------------------------------------------------------------- E145 whose floor is it
def test_same_portal_floor_gap_needs_a_shared_broker_feed() -> None:
    """F1: 15427848 (no broker) against 418942 — one ceskereality portal, two feeds."""
    a = listing(1, source="ceskereality", floor=2, area_m2=131.0, broker_key=None)
    b = listing(2, source="ceskereality", floor=1, area_m2=131.0, broker_key="b1a0")
    assert "floor" in {f.name for f in distinguishing_facts(a, b, None, Settings())}
    assert "floor" not in {f.name for f in distinguishing_facts(a, b, None, W15)}
    one_feed = listing(3, source="ceskereality", floor=1, area_m2=131.0, broker_key="x")
    two_in_one_feed = listing(4, source="ceskereality", floor=2, area_m2=131.0, broker_key="x")
    assert "floor" in {f.name for f in distinguishing_facts(one_feed, two_in_one_feed, None, W15)}


def test_two_storey_gap_is_a_fact_whatever_the_feed() -> None:
    a = listing(1, source="ceskereality", floor=1, area_m2=131.0, broker_key=None)
    b = listing(2, source="ceskereality", floor=3, area_m2=131.0, broker_key="b1a0")
    assert "floor" in {f.name for f in distinguishing_facts(a, b, None, W15)}
