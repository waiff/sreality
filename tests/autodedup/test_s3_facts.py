"""S3's readings (E180-E185, D57), each built from the adverts that named it.

Every case here is a real pair of cohort 3, 4 or 5 — the positives are the fusions the
cohort-5 confirmation hand-read out of S2's groups, the negatives are the duplicates the same
confirmation hand-read as ONE object and which each rule must therefore leave alone. Nothing
is invented: where a body is quoted it is quoted from the advert.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodedup.dataset import Listing
from autodedup.demonstrate import (
    price_demonstrated,
    price_granularity,
    prices_round_equal,
)
from autodedup.features import plot_reading, plot_residue
from autodedup.floor_convention import same_camp
from autodedup.indistinguishable import (
    CLUSTER,
    distinguishing_facts,
    price_paths_agree,
    selected_parcels,
)
from autodedup.settings import Settings
from autodedup.text_facts import (
    ground_or_upper,
    parcel_numbers_wider,
    parcel_table,
    stated_unit_counts,
    subject_floors,
)

SETTINGS = Path(__file__).resolve().parents[2] / "autodedup/settings"
S2 = Settings.from_json(SETTINGS / "w17.json")
S3 = Settings.from_json(SETTINGS / "w18.json")

DAY = 86_400.0


def stamp(day: int) -> str:
    return f"2026-0{1 + day // 28}-{1 + day % 28:02d}T00:00:00+00:00"


def listing(listing_id: int, first: int = 0, last: int = 60, **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "source": "sreality",
        "category_main": "byt",
        "category_type": "prodej",
        "disposition": "3+kk",
        "area_m2": 54.0,
        "price": 6_919_000.0,
        "first_seen_at": stamp(first),
        "last_seen_at": stamp(last),
    }
    fields.update(kwargs)
    return Listing(id=listing_id, block="b", **fields)  # type: ignore[arg-type]


def names(a: Listing, b: Listing, cfg: Settings, feats: object = None) -> list[str]:
    return [f.name for f in distinguishing_facts(a, b, feats, cfg, CLUSTER)]  # type: ignore[arg-type]


def variant(**overrides: object) -> Settings:
    raw = json.loads((SETTINGS / "w18.json").read_text())
    raw.update(overrides)
    return Settings(**raw)


# --- E180: the convention excuses a storey only ACROSS camps --------------------------------


def test_same_portal_is_always_one_camp() -> None:
    assert same_camp(S3.floor_camps, "sreality", "sreality")
    assert same_camp(S3.floor_camps, "bazos", "bazos")


def test_two_high_counting_portals_are_one_camp() -> None:
    """realitymix and sreality both count the ground floor; a storey between them is real."""
    assert same_camp(S3.floor_camps, "realitymix", "sreality")


def test_a_camp_boundary_is_not_one_camp() -> None:
    assert not same_camp(S3.floor_camps, "sreality", "idnes")
    assert not same_camp(S3.floor_camps, "sreality", "maxima")


def test_rezidence_prazska_84_two_sreality_adverts_one_storey_apart() -> None:
    """G21020: floor 2 and floor 3 on sreality, 136 days together at 6,919,000 and 53.8 m².

    Different brokers, so E154's feed rule never fired and S2 read the gap as vocabulary —
    inside one portal there is no vocabulary to read."""
    a = listing(21020, floor=3, broker_key="b801ce", first=0, last=60)
    b = listing(27958, floor=2, broker_key="ba207e", first=0, last=60)
    assert "floor" not in names(a, b, S2)
    assert "floor" in names(a, b, S3)


def test_the_camp_table_is_not_read_as_evidence_of_agreement() -> None:
    """S3's scope is `source`, not `camp`. G380908's realitymix-against-sreality storey is a
    fact and the camp scope would read it — but the same scope splits 182 certain duplicates on
    cohort 5, almost all idnes against ceskereality at one price and one area, one storey
    apart: the table places both portals at level 0 and the data says their offset is 1. A
    table fitted to excuse a gap may not be read as proof that there is nothing to excuse.
    G380908 is separated by E181's worded storey instead."""
    a = listing(380908, source="realitymix", floor=1, category_type="pronajem",
                price=17_000.0, area_m2=65.0)
    b = listing(12273157, source="sreality", floor=2, category_type="pronajem",
                price=17_000.0, area_m2=65.0)
    assert "floor" not in names(a, b, S3)
    wide = variant(d43_floor_within_camp_scope="camp")
    assert "floor" in names(a, b, wide)


def test_two_low_counting_portals_are_not_known_to_agree() -> None:
    """112399 x 401485: idnes floor 1 against ceskereality floor 2, 120 m² and 9,490,000 on
    both sides for 85 days — one flat, and the camp scope splits it."""
    a = listing(112399, source="idnes", floor=1, area_m2=120.0, price=9_490_000.0)
    b = listing(401485, source="ceskereality", floor=2, area_m2=120.0, price=9_490_000.0)
    assert "floor" not in names(a, b, S3)


def test_a_storey_across_the_camp_boundary_is_still_the_convention() -> None:
    """The rule must not reach what N1 was built for: idnes counts one lower than sreality."""
    a = listing(1, source="sreality", floor=3)
    b = listing(2, source="idnes", floor=2)
    assert "floor" not in names(a, b, S3)


def test_the_colive_guard_reads_the_honest_window_not_the_detection_one() -> None:
    """13410297 x 15427848: two ceskereality re-posts of one Jablonec flat, sighted three
    hours apart and both DETECTED gone on 8 September. `overlap_days` runs on `inactive_at`
    and calls them 27.6 days co-live; the sightings say they never overlapped at all."""
    a = listing(13410297, source="ceskereality", floor=1, area_m2=131.0, price=6_980_000.0,
                broker_key="b1a082b4", first=0, last=7,
                inactive_at=stamp(35), is_active=False)
    b = listing(15427848, source="ceskereality", floor=2, area_m2=131.0, price=6_980_000.0,
                broker_key=None, first=8, last=15, inactive_at=stamp(35), is_active=False)
    detection = variant(d43_floor_within_camp_honest_window=False)
    assert "floor" in names(a, b, detection)
    assert "floor" not in names(a, b, S3)


def test_a_jablonec_vila_relisted_at_a_moved_price_keeps_e154s_escape() -> None:
    """418942 x 15427848: one ceskereality 131 m² 4+1 at 6,988,000 with its storey written 1
    and, still live 27 days later, at 6,980,000 with it written 2 — and the second row names no
    broker. E154 excused that because the asking price is what separates a unit from a typist,
    and the price MOVED, so the storey moved with the re-post."""
    a = listing(418942, source="ceskereality", floor=1, area_m2=131.0, price=6_988_000.0,
                broker_key="b1a082b4", first=0, last=50)
    b = listing(15427848, source="ceskereality", floor=2, area_m2=131.0, price=6_980_000.0,
                broker_key=None, first=20, last=50)
    assert "floor" not in names(a, b, S2)
    assert "floor" not in names(a, b, S3)


def test_chrudimska_99_at_one_unmoved_price_is_two_statements() -> None:
    """62868 x 65870: both sreality, both brokerless, 2,475,000 and 17 m² on both sides, 5.4
    days together with the storey written 1 and 2. Nothing moved, so nothing is a re-post."""
    a = listing(62868, floor=1, area_m2=17.0, price=2_475_000.0, broker_key=None,
                first=10, last=17)
    b = listing(65870, floor=2, area_m2=17.0, price=2_475_000.0, broker_key=None,
                first=11, last=17)
    assert "floor" not in names(a, b, S2)
    assert "floor" in names(a, b, S3)


def test_bazos_reposts_that_drift_a_storey_stay_one_advert() -> None:
    """29 sequential bazos re-posts of one Dašice advert drift 0/1; they never co-live."""
    a = listing(1, source="bazos", floor=0, first=0, last=1)
    b = listing(2, source="bazos", floor=1, first=5, last=6)
    assert "floor" not in names(a, b, S3)


def test_two_adverts_live_together_have_no_drift_excuse() -> None:
    a = listing(1, source="bazos", floor=0, first=0, last=40)
    b = listing(2, source="bazos", floor=1, first=5, last=40)
    assert "floor" in names(a, b, S3)


# --- E181: the storey stated of the OFFERED unit --------------------------------------------


DASICE_GROUND = (
    "Předmětem této nabídky je pronájem nebytového prostoru umístěného v 1.NP (přízemí) "
    "s celkovou výměrou 141,8 m2, světlou výškou 3,5 m."
)
DASICE_UPPER = (
    "Předmětem této nabídky je pronájem nebytového prostoru umístěného v 2.NP "
    "(bezbariérový přístup) s celkovou výměrou 141,8 m2. Datové rozvody a okruhy zásuvek "
    "jsou vybudovány včetně možnosti užívání WC v 1.NP."
)


def test_subject_floor_reads_the_placement_clause_only() -> None:
    assert subject_floors(DASICE_GROUND) == frozenset({1})
    assert subject_floors(DASICE_UPPER) == frozenset({2})


def test_dasice_mill_two_storeys_of_one_building() -> None:
    """18851023 x 18931770: `printed_floors` reads {1} against {1, 2} and they meet."""
    a = listing(18851023, source="bazos", description=DASICE_GROUND, floor=0,
                category_main="komercni", category_type="pronajem", price=33_000.0,
                area_m2=141.8, first=0, last=40)
    b = listing(18931770, source="bazos", description=DASICE_UPPER, floor=1,
                category_main="komercni", category_type="pronajem", price=33_000.0,
                area_m2=141.8, first=5, last=40)
    assert "prose_floor" not in names(a, b, S2)
    assert "subject_floor" in names(a, b, S3)


def test_subject_floor_abstains_where_no_clause_places_the_unit() -> None:
    text = "V 1.NP je kočárkárna a ve 2.NP prádelna."
    assert subject_floors(text) == frozenset()


def test_a_shared_subject_storey_is_never_a_conflict() -> None:
    a = listing(1, source="bazos", description=DASICE_GROUND, first=0, last=40)
    b = listing(2, source="bazos", description=DASICE_GROUND, first=0, last=40)
    assert "subject_floor" not in names(a, b, S3)


# --- E181: the storey written in words -------------------------------------------------------


def test_ground_and_upper_are_read_from_the_preposition() -> None:
    assert ground_or_upper("byt 3+kk s terasou 15 m2 v přízemí novostavby") == frozenset(
        {"ground"})
    assert ground_or_upper("byt 3+kk s balkonem v patře novostavby") == frozenset({"upper"})


def test_the_building_is_not_the_unit() -> None:
    """`v přízemí domu je kočárkárna` says where the pram store is, not where the flat is —
    the reader cannot tell them apart, so the pair below is why the limb needs measuring."""
    assert ground_or_upper("v přízemí domu je kočárkárna") == frozenset({"ground"})


def test_pouchov_ground_against_upper() -> None:
    a = listing(380908, source="realitymix", floor=1, category_type="pronajem", price=17_000.0,
                description="byt 3+kk s terasou 15 m2 v přízemí novostavby")
    b = listing(12256088, source="idnes", floor=1, category_type="pronajem", price=17_000.0,
                description="byt 3+kk s balkonem v patře novostavby")
    assert "storey_word" not in names(a, b, S2)
    assert "storey_word" in names(a, b, S3)


# --- E182: the parcel, read exactly and through a truncating carrier -------------------------


def test_a_truncating_carrier_is_read_by_its_residue_not_blanked() -> None:
    a = listing(469928, source="ceskereality", category_main="dum", area_m2=121.0,
                attrs={"estate_area": 227.0})
    b = listing(469929, source="ceskereality", category_main="dum", area_m2=121.0,
                attrs={"estate_area": 191.0})
    assert plot_reading(a) == (227.0, False)
    assert "plot_area" not in names(a, b, S2)
    assert "plot_area" in names(a, b, S3)


def test_a_truncation_that_agrees_on_the_residue_claims_nothing() -> None:
    """870 may be 5,870 — an agreeing residue is absence of evidence, never a merge fact."""
    a = listing(1, source="ceskereality", category_main="dum", attrs={"estate_area": 870.0})
    b = listing(2, source="sreality", category_main="dum", attrs={"estate_area": 5870.0})
    assert plot_residue(5870.0) == 870.0
    assert "plot_area" not in names(a, b, S3)


def test_raby_packages_are_three_tenths_of_a_percent_apart() -> None:
    """70914 x 18626576: 1,001 m² against 998 m², both printed by trusted carriers."""
    a = listing(70914, source="sreality", category_main="dum", area_m2=89.0,
                price=10_999_000.0, attrs={"estate_area": 1001.0})
    b = listing(18626576, source="idnes", category_main="dum", area_m2=89.0,
                price=10_988_000.0, attrs={"estate_area": 998.0})
    assert "plot_area" not in names(a, b, S2)
    assert "plot_area" in names(a, b, S3)


def test_a_flats_building_plot_is_not_read_exactly() -> None:
    """Two portals resolve one block to two entrances; the plot there is the building's."""
    a = listing(1, category_main="byt", attrs={"estate_area": 1001.0})
    b = listing(2, source="idnes", category_main="byt", attrs={"estate_area": 998.0})
    assert "plot_area" not in names(a, b, S3)


def test_one_parcel_printed_by_two_portals_still_meets() -> None:
    a = listing(1, category_main="pozemek", disposition=None, area_m2=1458.0,
                attrs={"estate_area": 1458.0})
    b = listing(2, source="idnes", category_main="pozemek", disposition=None, area_m2=1458.0,
                attrs={"estate_area": 1458.0})
    assert "plot_area" not in names(a, b, S3)


# --- E183: the catalogue row that is THIS advert's -------------------------------------------


MORASICE_TABLE = (
    "Nabízíme k prodeji stavební pozemky v Morašicích — celkem 7 parcel.\n"
    "• 277/2 + 277/3 — 1 465 m² — 3 469 000 Kč\n"
    "• 274/8 + 274/13 — 1 458 m² — 3 459 000 Kč\n"
    "• 274/9 + 274/14 — 1 433 m² — 3 469 000 Kč\n"
    "• 277/6 — 1 211 m² — 3 179 000 Kč\n"
)
MORASICE_ONE = (
    "Nabízíme k prodeji stavební pozemek v Morašicích o rozloze 1433 m². "
    "• číslo pozemku: 274/9 + 274/14 • Dostupnost pozemku: Chrudim 10 min."
)


def test_the_table_is_read_as_rows() -> None:
    rows = parcel_table(MORASICE_TABLE)
    assert len(rows) == 4
    assert (frozenset({"274/8", "274/13"}), 1458.0, 3459000.0) in rows


def test_the_wider_keyword_reads_the_czech_word_order() -> None:
    assert parcel_numbers_wider(MORASICE_ONE) == {"274/9", "274/14"}


def test_the_advert_selects_its_own_row_by_its_own_numbers() -> None:
    seller = listing(287798, category_main="pozemek", disposition=None, area_m2=1458.0,
                     price=3_459_000.0, attrs={"estate_area": 1458.0},
                     description=MORASICE_TABLE)
    assert selected_parcels(seller, S3) == {"274/8", "274/13"}


def test_morasice_two_plots_of_one_parcelling() -> None:
    a = listing(181261, source="idnes", category_main="pozemek", disposition=None,
                area_m2=1433.0, price=3_469_000.0, attrs={"estate_area": 1433.0},
                description=MORASICE_ONE)
    b = listing(287798, category_main="pozemek", disposition=None, area_m2=1458.0,
                price=3_459_000.0, attrs={"estate_area": 1458.0}, description=MORASICE_TABLE)
    assert "parcel" not in names(a, b, S2)
    assert "parcel" in names(a, b, S3)


def test_an_unresolvable_catalogue_falls_back_to_the_union() -> None:
    """Two rows at one price and one area name no single row; the reader may not guess."""
    text = ("• 100/1 — 500 m² — 1 000 000 Kč\n• 100/2 — 500 m² — 1 000 000 Kč\n")
    row = listing(1, category_main="pozemek", disposition=None, area_m2=500.0,
                  price=1_000_000.0, attrs={"estate_area": 500.0}, description=text)
    assert selected_parcels(row, S3) == set()


def test_two_adverts_of_one_plot_still_meet() -> None:
    a = listing(1, category_main="pozemek", disposition=None, area_m2=1433.0,
                price=3_469_000.0, attrs={"estate_area": 1433.0}, description=MORASICE_ONE)
    b = listing(2, source="idnes", category_main="pozemek", disposition=None, area_m2=1433.0,
                price=3_469_000.0, attrs={"estate_area": 1433.0}, description=MORASICE_ONE)
    assert "parcel" not in names(a, b, S3)


# --- E184: how many dwellings the object holds ------------------------------------------------


def test_the_stated_count_is_read_in_both_forms() -> None:
    assert stated_unit_counts("Výnosový dům se 2 byty 3+kk") == frozenset({2})
    assert stated_unit_counts("Výnosový dům se 4 byty 3+kk") == frozenset({4})
    assert stated_unit_counts("dům se dvěma bytovými jednotkami") == frozenset({2})


def test_dritec_two_flats_against_four() -> None:
    """18868307 x 18868308: both bazos, 7.3 days together, 11,100,000 against 21,500,000."""
    a = listing(18868307, source="bazos", category_main="ostatni", disposition=None,
                area_m2=None, price=11_100_000.0, first=0, last=20,
                description="Výnosový dům se 2 byty 3+kk, po rekonstrukci, Dříteč.")
    b = listing(18868308, source="bazos", category_main="ostatni", disposition=None,
                area_m2=None, price=21_500_000.0, first=5, last=20,
                description="Výnosový dům se 4 byty 3+kk, po rekonstrukci, Dříteč.")
    assert "unit_count" not in names(a, b, S2)
    assert "unit_count" in names(a, b, S3)


def test_d49s_refusal_stands_two_prices_of_one_object() -> None:
    """One advert carries a freehold price and a co-operative share; no count separates them,
    so the co-live price alone must still not be a fact."""
    a = listing(1, source="bazos", category_main="dum", disposition=None, price=8_350_000.0,
                first=0, last=40, description="Prodej domu, osobní vlastnictví.")
    b = listing(2, source="bazos", category_main="dum", disposition=None, price=1_670_000.0,
                first=0, last=40, description="Prodej domu, družstevní podíl.")
    assert "unit_count" not in names(a, b, S3)


def test_a_count_stated_on_one_side_only_is_no_conflict() -> None:
    a = listing(1, source="bazos", category_main="dum", disposition=None, first=0, last=40,
                description="Výnosový dům se 2 byty 3+kk.")
    b = listing(2, source="bazos", category_main="dum", disposition=None, first=0, last=40,
                description="Výnosový dům po rekonstrukci.")
    assert "unit_count" not in names(a, b, S3)


def test_a_count_difference_without_a_price_gap_is_not_read() -> None:
    """The conjunction is the rule: D49 refused the price limb alone and the count alone is a
    template's prose until a live price disagrees with it."""
    a = listing(1, source="bazos", category_main="dum", disposition=None, price=9_000_000.0,
                first=0, last=40, description="Výnosový dům se 2 byty 3+kk.")
    b = listing(2, source="bazos", category_main="dum", disposition=None, price=9_000_000.0,
                first=0, last=40, description="Výnosový dům se 4 byty 3+kk.")
    assert "unit_count" not in names(a, b, S3)


# --- E185: a price move between postings that never overlapped ---------------------------------


BODY = "Rodinný dům se dvěma bytovými jednotkami ve Vysoké nad Labem. " * 8
FEATS = {"containment_max": (1.0, True), "phash_tight_matches": (27.0, True)}


def test_vysoka_nad_labem_relisted_at_a_cut_price() -> None:
    """66227 x 306779: one sreality advert, 14,500,000 until 11 June and 13,700,000 after."""
    a = listing(66227, category_main="dum", disposition=None, area_m2=280.0,
                price=14_500_000.0, first=0, last=22, description=BODY)
    b = listing(306779, category_main="dum", disposition=None, area_m2=280.0,
                price=13_700_000.0, first=23, last=60, description=BODY)
    cross_a = listing(384524, source="realitymix", category_main="dum", disposition=None,
                      area_m2=280.0, price=13_700_000.0, first=23, last=60, description=BODY)
    assert "price" in names(a, cross_a, S2, FEATS)
    assert "price" not in names(a, cross_a, S3, FEATS)
    assert "price" not in names(a, b, S3, FEATS)


def test_a_co_live_price_gap_is_untouched_by_the_relist_rule() -> None:
    a = listing(1, category_main="dum", disposition=None, area_m2=280.0, price=14_500_000.0,
                first=0, last=60, description=BODY)
    b = listing(2, source="realitymix", category_main="dum", disposition=None, area_m2=280.0,
                price=13_700_000.0, first=0, last=60, description=BODY)
    assert "price" in names(a, b, S3, FEATS)


def test_the_relist_rule_needs_the_rest_of_the_identity() -> None:
    """A different area is a different object however far apart the two windows are."""
    a = listing(1, category_main="dum", disposition=None, area_m2=280.0, price=14_500_000.0,
                first=0, last=22, description=BODY)
    b = listing(2, source="realitymix", category_main="dum", disposition=None, area_m2=240.0,
                price=13_700_000.0, first=23, last=60, description=BODY)
    assert "price" in names(a, b, S3, FEATS)


def test_the_relist_rule_needs_an_area_on_BOTH_sides() -> None:
    """74696 x 140706: a Pouchovská 2+kk at 18,500 and a Slezské Předměstí 2+kk at 21,000, two
    different flats, bridged by a bazos row that states no area at all. `area_rel_diff`
    abstains on a missing side, and abstention is not agreement (E164)."""
    a = listing(74696, category_type="pronajem", area_m2=60.0, price=18_500.0,
                first=0, last=2, description=BODY)
    bridge = listing(136541, source="bazos", category_type="pronajem", area_m2=None,
                     price=21_000.0, first=3, last=60, description=BODY)
    assert "price" in names(a, bridge, S3, FEATS)


def test_the_relist_rule_needs_the_body_or_the_photographs() -> None:
    a = listing(1, category_main="dum", disposition=None, area_m2=280.0, price=14_500_000.0,
                first=0, last=22, description=BODY)
    b = listing(2, source="realitymix", category_main="dum", disposition=None, area_m2=280.0,
                price=13_700_000.0, first=23, last=60, description=BODY)
    thin = {"containment_max": (0.3, True), "phash_tight_matches": (0.0, True)}
    assert "price" in names(a, b, S3, thin)


def test_a_storey_apart_is_still_a_fact_between_two_live_relists() -> None:
    """E185 waives the PRICE, never a storey. G19114's Chrudimská 99 micro-units: 62868 states
    floor 1 and 65870 floor 2, both sreality, 5.4 days together at one price and one area —
    the pair the cohort-5 confirmation named, and the one E180 has to separate."""
    a = listing(62868, floor=1, area_m2=17.0, price=2_475_000.0, first=10, last=17)
    b = listing(65870, floor=2, area_m2=17.0, price=2_475_000.0, first=11, last=17)
    assert "floor" not in names(a, b, S2, FEATS)
    assert "floor" in names(a, b, S3, FEATS)


def test_an_idnes_copy_of_the_same_unit_is_still_the_convention() -> None:
    """306657 (sreality, floor 2) and 312734 (idnes, floor 1) are 72.9 days together and are
    ONE unit: the camp boundary is exactly what N1 was built to excuse."""
    a = listing(306657, floor=2, area_m2=17.0, price=2_475_000.0, first=0, last=60)
    b = listing(312734, source="idnes", floor=1, area_m2=17.0, price=2_475_000.0,
                first=0, last=60)
    assert "floor" not in names(a, b, S3, FEATS)


# --- D57: exactness where identity is claimed -------------------------------------------------


@pytest.mark.parametrize("value,unit", [
    (5_500_000.0, 1e5), (5_499_000.0, 1e3), (10_999_000.0, 1e3), (9_895_960.0, 1e1),
    (11_250_000.0, 1e4), (7_000_000.0, 1e6),
])
def test_a_price_is_granular_to_the_coarsest_power_of_ten_it_divides(
        value: float, unit: float) -> None:
    assert price_granularity(value) == unit


def test_rounding_is_arithmetic_not_a_percentage() -> None:
    assert prices_round_equal(5_499_000.0, 5_500_000.0)
    assert prices_round_equal(11_250_000.0, 11_250_000.0)
    assert not prices_round_equal(10_999_000.0, 10_988_000.0)


def test_the_raby_packages_are_not_one_price() -> None:
    """A 0.1 % gap between two numbers of the same granularity is the next package."""
    a = listing(70914, category_main="dum", disposition=None, area_m2=89.0,
                price=10_999_000.0, first=0, last=60)
    b = listing(18626576, source="idnes", category_main="dum", disposition=None, area_m2=89.0,
                price=10_988_000.0, first=0, last=60)
    overlap = 60.0
    assert price_demonstrated(a, b, S2, price_paths_agree(a, b, S2.d43_price_path_tol), overlap)
    assert not price_demonstrated(
        a, b, S3, price_paths_agree(a, b, S3.d43_price_path_tol), overlap)


def test_the_exact_bar_is_not_readmitted_through_the_path() -> None:
    """The whole of D57: `price_paths_agree` ran at 0.5 % and undid the exact reading."""
    a = listing(1, price=10_999_000.0, first=0, last=60)
    b = listing(2, source="idnes", price=10_988_000.0, first=0, last=60)
    assert price_paths_agree(a, b, S3.d43_price_path_tol)
    assert not price_demonstrated(a, b, S3, True, 60.0)


def test_one_price_written_to_two_precisions_still_demonstrates() -> None:
    a = listing(1, price=5_499_000.0, first=0, last=60)
    b = listing(2, source="idnes", price=5_500_000.0, first=0, last=60)
    assert price_demonstrated(a, b, S3, False, 60.0)


# --- the ladder: nothing here may change an arm that does not ask for it ----------------------


@pytest.mark.parametrize("arm", ["w13.json", "w15.json", "w16_s.json", "w17.json"])
def test_every_earlier_arm_keeps_the_S2_reading(arm: str) -> None:
    cfg = Settings.from_json(SETTINGS / arm)
    for name in ("d43_floor_within_camp", "d43_subject_floor", "d43_ground_vs_upper",
                 "d43_plot_area_exact", "d43_plot_truncation_residue", "d43_parcel_table",
                 "d43_price_sequential_path", "demonstrate_price_path_exact",
                 "demonstrate_price_rounding_aware", "d43_offer_area",
                 "d43_floor_within_camp_price_escape"):
        assert getattr(cfg, name) is False, name
    assert cfg.d43_stated_unit_count == "off"


def test_every_new_fact_name_is_declared() -> None:
    from autodedup.indistinguishable import FACT_NAMES

    for name in ("subject_floor", "storey_word", "unit_count"):
        assert name in FACT_NAMES


def test_a_dependent_field_cannot_be_set_alone() -> None:
    with pytest.raises(ValueError):
        variant(d43_floor_within_camp=False, d43_floor_within_camp_colive=True)
    with pytest.raises(ValueError):
        variant(demonstrate_price_exact=False, demonstrate_price_path_exact=True)
    with pytest.raises(ValueError):
        variant(d43_stated_unit_count="sometimes")


# --- E186: measured, and REFUSED ---------------------------------------------------------


def test_the_offer_area_limb_is_off_in_s3() -> None:
    """E186 read the size a body LEADS with. Measured alone it splits 648 certain duplicates of
    cohort 5 and 386 of cohort 3 — two bodies routinely lead with the terrace, the plot or the
    building where the other leads with the flat — and the one fusion it was built for it does
    not even read: the HK-Zámeček advert writes its whole-parcel figure as `o celkové výměře`,
    which E153's `_BUILDING_TOTAL` skips as the building's own size. The field stays, off, so
    the measurement is on the record and a later wave can re-open it."""
    assert S3.d43_offer_area is False
