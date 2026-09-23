"""S7's readings (E220-E224) and D65's merge policy, each built from the advert that named it.

Every case is a real pair out of cohort 9: the Cihelní serviced residence (532849 x 534528),
the Opava Pekařská 2+kk twins (507689 x 511488), the Vnější Praha-Michle pair uploaded two
seconds apart (18896333 x 18896336), the Přívozská 3+kk let on two portals (12196 x 141452),
the Provaznická garage spaces (18607308 x 18650335), the DOV Vítkovice hall units (43923 x
43926), the Velká Polom volnočasové centrum (449963 / 456436 / 456437), the Opava MG Medical
premises (189086 x 524985 and 253379 x 254222), the Vávrovice building plots (18777318 x
18868076) and the HQ Laso serviced offices (90478 x 90525). Where a body is quoted it is
quoted from the advert, with the sentences the reader must see verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

from autodedup.dataset import Listing
from autodedup.decide import Decision, apply_merge_policy, merge_policy_verdict
from autodedup.indistinguishable import CLUSTER, GATE, distinguishing_facts
from autodedup.settings import Settings
from autodedup.text_facts import (
    commercial_product_class,
    english_unit_codes,
    floor_coverings,
    furnished_state,
    parking_level,
    plot_attributes,
    renovation_state,
    sanitary_arrangement,
    slug_areas,
    slug_unit_codes,
    stated_charges,
    stated_charges_wide,
)

SETTINGS = Path(__file__).resolve().parents[2] / "autodedup/settings"
S6 = Settings.from_json(SETTINGS / "w21.json")
S7 = Settings.from_json(SETTINGS / "w22.json")
S7_HOLD = Settings.from_json(SETTINGS / "w22_rentals_hold.json")


def variant(**kwargs: object) -> Settings:
    raw = json.loads((SETTINGS / "w22.json").read_text(encoding="utf-8"))
    raw.update(kwargs)
    return Settings.from_dict(raw)


def stamp(day: int) -> str:
    return f"2026-0{1 + day // 28}-{1 + day % 28:02d}T00:00:00+00:00"


def listing(listing_id: int, first: int = 0, last: int = 40, **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "source": "sreality",
        "category_main": "byt",
        "category_type": "pronajem",
        "first_seen_at": stamp(first),
        "last_seen_at": stamp(last),
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def names(a: Listing, b: Listing, cfg: Settings, mode: str = GATE) -> list[str]:
    return [fact.name for fact in distinguishing_facts(a, b, None, cfg, mode)]


# --- E220: two lets of one house, on sale together, parted by one stated fact ---------------
# Cihelní 2674/91: one serviced residence lets two 2+1s of 55 m², both at 12,000 Kč, three
# days together on ONE portal under one broker. Nothing but the second numbers parts them.
CIHELNI_A = (
    "Nabízíme k dlouhodobému pronájmu byt o dispozici 2+1 a velikosti 55 m², který se nachází "
    "na ulici Cihelní v Ostravě. Byt se nachází ve 4. nadzemním podlaží. Nájemné: 12.000 Kč. "
    "Služby včetně elektřiny a topení: 3.900 Kč (spočítáno pro 2 osoby, každá další osoba 700 "
    "Kč). Kauce: 36.000 Kč. Kauci lze rozdělit do 3 splátek."
)
CIHELNI_B = (
    "Nabízíme k dlouhodobému pronájmu byt o dispozici 2+1 a velikosti 55 m², který se nachází "
    "na ulici Cihelní v Ostravě. Byt se nachází ve 4. nadzemním podlaží. Nájemné: 12.000 Kč. "
    "Služby včetně elektřiny a topení: 3.660 Kč (spočítáno pro 2 osoby, každá další osoba 700 "
    "Kč). Kauce: 39.000 Kč. Kauci lze rozdělit do 3 splátek."
)


def cihelni() -> tuple[Listing, Listing]:
    a = listing(532849, first=0, last=28, area_m2=55.0, price=12000.0, floor=3,
                total_floors=4, disposition="2+1", broker_key="firm-111",
                broker_firm_id=111, description=CIHELNI_A)
    b = listing(534528, first=0, last=3, area_m2=55.0, price=12000.0, floor=3,
                total_floors=4, disposition="2+1", broker_key="firm-111",
                broker_firm_id=111, description=CIHELNI_B)
    return a, b


def test_e220_reads_two_service_advances_at_one_rent() -> None:
    a, b = cihelni()
    assert names(a, b, S6) == []
    assert "rental_colive" in names(a, b, S7)


def test_e220_needs_the_wide_charge_table_for_this_spelling() -> None:
    """E203's own table has no `Služby včetně …`, and widening it would move w21."""
    assert stated_charges(CIHELNI_A).get("services") is None
    assert stated_charges_wide(CIHELNI_A)["services"] == frozenset({3900.0})
    services_only = CIHELNI_A.split(" Kauce:")[0], CIHELNI_B.split(" Kauce:")[0]
    a, b = cihelni()
    a, b = (listing(532849, first=0, last=28, area_m2=55.0, price=12000.0, floor=3,
                    broker_key="firm-111", description=services_only[0]),
            listing(534528, first=0, last=3, area_m2=55.0, price=12000.0, floor=3,
                    broker_key="firm-111", description=services_only[1]))
    assert "rental_colive" in names(a, b, S7)
    assert "rental_colive" not in names(a, b, variant(d43_charge_keywords_wide=False))


def test_e220_keeps_a_sequential_re_post_with_a_moved_deposit_whole() -> None:
    """The co-live window IS the guard: a re-post is never on sale beside itself."""
    a, b = cihelni()
    late = listing(534528, first=60, last=80, area_m2=55.0, price=12000.0, floor=3,
                   total_floors=4, disposition="2+1", broker_key="firm-111",
                   broker_firm_id=111, description=CIHELNI_B)
    assert "rental_colive" not in names(a, late, S7)


def test_e220_is_a_dial() -> None:
    a, b = cihelni()
    assert "rental_colive" not in names(a, b, variant(d43_rental_colive=False))
    assert "rental_colive" not in names(a, b, variant(d43_rental_colive_charges=False))


def test_e220_never_reads_a_sale() -> None:
    a, b = cihelni()
    sale_a = listing(1, area_m2=55.0, price=12000.0, category_type="prodej",
                     description=CIHELNI_A)
    sale_b = listing(2, area_m2=55.0, price=12000.0, category_type="prodej",
                     description=CIHELNI_B)
    assert "rental_colive" not in names(sale_a, sale_b, S7)
    assert "rental_colive" in names(a, b, S7)


# ul. Vnější, Praha 4 - Michle: two 2+kk of 37 m² uploaded two SECONDS apart by one agency,
# one template, and four words of daylight between them.
VNEJSI_SEPARATE = (
    "Nabízíme k dlouhodobému pronájmu nezařízený byt 2+kk po celkové rekonstrukci o výměře 37 "
    "m². Byt je prakticky dispozičně řešen a nabízí předsíň, obytný pokoj s kuchyňskou "
    "linkou, ložnici s komorou, koupelnu se sprchovým koutem a samostatnou toaletu. Jistota "
    "činí 35.000 Kč."
)
VNEJSI_COMBINED = (
    "Nabízíme k dlouhodobému pronájmu nezařízený byt 2+kk po celkové rekonstrukci o výměře 37 "
    "m². Byt je prakticky dispozičně řešen a nabízí předsíň, obytný pokoj s kuchyňskou "
    "linkou, ložnici s komorou, koupelnu se sprchovým koutem a toaletou. Jistota činí 35.000 "
    "Kč."
)


def test_e220_reads_where_the_lavatory_is() -> None:
    assert sanitary_arrangement(VNEJSI_SEPARATE) == frozenset({"separate"})
    assert sanitary_arrangement(VNEJSI_COMBINED) == frozenset({"combined"})
    a = listing(18896333, source="idnes", first=0, last=7, area_m2=37.0, price=19000.0,
                floor=1, total_floors=3, disposition="2+kk", broker_key="agent-2976",
                broker_firm_id=2976, description=VNEJSI_SEPARATE)
    b = listing(18896336, source="idnes", first=0, last=7, area_m2=37.0, price=18500.0,
                floor=1, total_floors=3, disposition="2+kk", broker_key="agent-2976",
                broker_firm_id=2976, description=VNEJSI_COMBINED)
    assert names(a, b, S6) == []
    assert "rental_colive" in names(a, b, S7)
    assert "rental_colive" not in names(a, b, variant(d43_rental_colive_sanitary=False))


# Přívozská 154/9: one house, two lets, 42 days together on two portals at 19,700 and 20,650.
PRIVOZSKA_PARQUET = (
    "Pod nohama Vás okouzlí krásné broušené parkety ve světlém odstínu, které odrážejí světlo "
    "a dodávají prostoru noblesu. Původní dveře byly citlivě obnoveny a vnáší do bytu "
    "charakter, který se dnes jen tak nevidí. Kuchyně ve vintage stylu působí jako z jiného "
    "času."
)
PRIVOZSKA_FLOATING = (
    "Byt bude po rekonstrukci, prostorný a dispozičně promyšlený do posledního detailu. "
    "Všechny pokoje jsou velkorysé, světlé a vzdušné, s novými plovoucími podlahami v jemných "
    "světlých tónech. Kuchyně je srdcem domova – moderní rohová kuchyňská linka s myčkou."
)


def privozska() -> tuple[Listing, Listing]:
    a = listing(12196, first=0, last=42, area_m2=75.0, price=19700.0, floor=4,
                total_floors=5, disposition="3+kk", broker_key="agent-2240",
                broker_firm_id=2240, description=PRIVOZSKA_PARQUET)
    b = listing(141452, source="bezrealitky", first=0, last=42, area_m2=75.0, price=20650.0,
                floor=4, disposition="3+kk", description=PRIVOZSKA_FLOATING)
    return a, b


def test_e220_reads_what_is_under_foot_across_two_portals() -> None:
    assert floor_coverings(PRIVOZSKA_PARQUET) == frozenset({"parquet"})
    assert floor_coverings(PRIVOZSKA_FLOATING) == frozenset({"floating"})
    a, b = privozska()
    assert names(a, b, S6) == []
    assert "rental_colive" in names(a, b, S7)


def test_e220_flooring_is_refused_when_the_limb_is_not_lifted_across_portals() -> None:
    a, b = privozska()
    assert "rental_colive" not in names(
        a, b, variant(d43_rental_colive_cross_portal_limbs=[]))


def test_e220_reads_the_tense_of_a_refurbishment_and_bude_is_not_done() -> None:
    assert renovation_state(PRIVOZSKA_FLOATING) == frozenset({"future"})
    assert renovation_state("Byt je po kompletní rekonstrukci.") == frozenset({"done"})


def test_e220_refuses_a_body_that_names_two_coverings() -> None:
    a, b = privozska()
    both = listing(2, source="bezrealitky", first=0, last=42, area_m2=75.0, price=20650.0,
                   floor=4, disposition="3+kk",
                   description="V pokojích jsou plovoucí podlahy, v hale původní parkety.")
    assert "rental_colive" not in names(
        a, both, variant(d43_rental_colive_renovation=False))


# ul. Provaznická, Opava: two garage spaces of one new building, both 18 m² at 1,500 Kč.
PROVAZNICKA_GROUND = (
    "Nabízíme pronájem vnitřního, garážového parkovacího stání na ulici Provaznická v Opavě. "
    "Garážové stání se nachází v přízemí bytového domu, s vjezdem z ulice Provaznická. "
    "Měsíční nájemné činí 1500,- Kč. Kauce je 3.000,- Kč."
)
PROVAZNICKA_BASEMENT = (
    "Nabízíme pronájem garážového parkovacího místa. Parkovací místo je v 1. PP v nově "
    "vybudovaném bytovém domě na ulici Provaznická v Opavě. Nájemné je 1500,-Kč. Kauce jeden "
    "měsíční nájem."
)


def test_e220_reads_the_storey_a_parking_space_is_on() -> None:
    assert parking_level(PROVAZNICKA_GROUND) == frozenset({"ground"})
    assert parking_level(PROVAZNICKA_BASEMENT) == frozenset({"basement"})
    a = listing(18607308, source="idnes", category_main="ostatni", first=0, last=4,
                area_m2=18.0, price=1500.0, broker_key="agent-p",
                description=PROVAZNICKA_GROUND)
    b = listing(18650335, source="idnes", category_main="ostatni", first=3, last=25,
                area_m2=18.0, price=1500.0, broker_key="agent-p",
                description=PROVAZNICKA_BASEMENT)
    assert names(a, b, S6) == []
    assert "rental_colive" in names(a, b, S7)


def test_e220_furnishing_ships_off() -> None:
    """Measured at 6 certain duplicates on the trial cohort and 0 buys — refuted (M478)."""
    assert furnished_state("Byt je plně zařízený.") == frozenset({"furnished"})
    assert furnished_state("Nabízíme nezařízený byt.") == frozenset({"unfurnished"})
    assert S7.d43_rental_colive_furnishing is False


# --- E221: the unit code nobody wrote in Czech ----------------------------------------------
# DOV Vítkovice: one 152,000 m² park, two adverts both carried at the park's 2,872 m² headline.
DOV_NJ1 = (
    "Nabízíme prostory pro skladování či lehkou výrobu v Ostravě - DOV. K dispozici ihned "
    "Hala M2 Unit NJ1 - 4084 m2 (390 m2 kanceláří, 7x rampa, 1x přímý vjezd, čistá výška 10 m)"
)
DOV_NJ2 = (
    "Nabízíme prostory pro skladování či lehkou výrobu v Ostravě - DOV. K dispozici ihned "
    "Hala M2 Unit NJ2 - 4391 m2 ( kanceláře budou vystaveny na míru, 7 x rampa, 1 přímý "
    "vjezd, čistá výška 10 m)"
)


def test_e221_reads_the_unit_and_keeps_the_hall_out_of_it() -> None:
    assert english_unit_codes(DOV_NJ1) == {"unit": frozenset({"NJ1"}),
                                           "hall": frozenset({"M2"})}
    a = listing(43923, category_main="komercni", first=0, last=135, area_m2=2872.0,
                broker_key="agent-2500", broker_firm_id=2500, description=DOV_NJ1)
    b = listing(43926, category_main="komercni", first=0, last=135, area_m2=2872.0,
                broker_key="agent-2500", broker_firm_id=2500, description=DOV_NJ2)
    assert names(a, b, S6) == []
    assert "english_unit_code" in names(a, b, S7)


def test_e221_does_not_split_two_adverts_of_one_hall() -> None:
    a = listing(1, category_main="komercni", area_m2=2872.0,
                description="K dispozici Hala M2 Unit NJ1 - 4084 m2, 7x rampa.")
    b = listing(2, category_main="komercni", area_m2=2872.0,
                description="K dispozici Hala M2 Unit NJ1 - 4084 m2, sedm ramp.")
    assert "english_unit_code" not in names(a, b, S7)


def test_e221_reads_a_building_letter() -> None:
    a = listing(120604, source="idnes", category_type="prodej", area_m2=55.0,
                description="Prodej bytu 2+kk budova A2, terasa, zahrada.")
    b = listing(2, source="idnes", category_type="prodej", area_m2=55.0,
                description="Prodej bytu 2+kk budova B2, terasa, zahrada.")
    assert "english_unit_code" in names(a, b, S7)


# realitymix files three units of one Velká Polom development under one project blurb and
# tells them apart only in its own url.
POLOM = "Komplex volnočasového centra v obci Velká Polom. Areál se skládá ze 14 domů."
POLOM_C1_103 = ("https://realitymix.cz/detail/velka-polom/volnocasove-centrum-obce-velka-"
                "polom-c1-nebytovy-prostor-103-m2-2211-8582601.html")
POLOM_B1_113 = ("https://realitymix.cz/detail/velka-polom/volnocasove-centrum-obce-velka-"
                "polom-b1-nebytovy-prostor-113-m2-1211-8206931.html")
POLOM_C1_58 = ("https://realitymix.cz/detail/velka-polom/volnocasove-centrum-obce-velka-"
               "polom-c1-nebytovy-prostor-58-m2-2212-8206927.html")


def polom(listing_id: int, url: str, price: float | None, area: float | None) -> Listing:
    return listing(listing_id, source="realitymix", category_main="komercni", first=0,
                   last=15, area_m2=area, price=price, source_url=url, description=POLOM)


def test_e221_reads_the_building_a_portal_files_in_its_own_slug() -> None:
    assert slug_unit_codes(POLOM_C1_103) == frozenset({"C1"})
    assert slug_unit_codes(POLOM_B1_113) == frozenset({"B1"})
    a = polom(449963, POLOM_C1_103, None, 103.0)
    b = polom(456436, POLOM_B1_113, 42375.0, None)
    assert names(a, b, S6) == []
    assert "slug_unit" in names(a, b, S7)


def test_e221_does_not_read_the_measurement_as_a_building() -> None:
    assert "M2" not in slug_unit_codes(POLOM_C1_103)
    assert slug_areas(POLOM_C1_103) == frozenset({103.0})


def test_e221_slug_area_ships_off() -> None:
    """109 certain duplicates of the region cohort against 18 buys — refuted (M479)."""
    assert S7.d43_slug_area is False
    a = polom(449963, POLOM_C1_103, None, 103.0)
    b = polom(456437, POLOM_C1_58, 21750.0, None)
    assert "slug_unit" in names(a, b, variant(d43_slug_area=True))


# --- E222: D61 stands; two order codes plus a second stated difference are a fact ------------
MG_RETAIL = (
    "Dovolujeme si Vám nabídnout k pronájmu komerční prostor o velikosti 25 m² v 1.NP v nově "
    "vznikajícím MG Medical a Senior centru na Masarykově třídě v Opavě. Samotný prostor je "
    "nabízen k využití pro obchodní, poradenské, či podobné využití. Evidenční číslo: 933144"
)
MG_CAFE = (
    "Dovolujeme si Vám nabídnout k pronájmu komerční prostor o velikosti 25 m² v 1.NP v nově "
    "vznikajícím MG Medical a Senior centru na Masarykově třídě v Opavě. Samotný prostor je "
    "nabízen k využití jako menší kavárna. Kavárna bude mít svůj samostatný vchod, s "
    "případnou předzahrádkou. Evidenční číslo: 933138"
)


def test_e222_reads_a_code_conflict_with_a_second_difference() -> None:
    a = listing(189086, source="bazos", category_main="komercni", subtype="obchodni_prostor",
                first=0, last=60, area_m2=25.0, price=20000.0, description=MG_RETAIL)
    b = listing(524985, source="bazos", category_main="komercni", subtype="restaurace",
                first=38, last=40, area_m2=25.0, price=20000.0, description=MG_CAFE)
    assert names(a, b, S6) == []
    assert "agency_code_plus" in names(a, b, S7)


def test_e222_refuses_two_codes_on_their_own() -> None:
    """D61: 263 certain duplicates of the nine cohorts carry two co-live codes."""
    a = listing(1, source="bazos", category_main="komercni", subtype="obchodni_prostor",
                area_m2=25.0, price=20000.0, description=MG_RETAIL)
    b = listing(2, source="bazos", category_main="komercni", subtype="obchodni_prostor",
                area_m2=25.0, price=20000.0,
                description=MG_RETAIL.replace("933144", "933138"))
    assert "agency_code_plus" not in names(a, b, S7)


def test_e222_reads_what_the_body_offers_the_space_for_not_the_portal_filing() -> None:
    """The region cohort's own re-pitches carry two codes AND two subtypes — one chalet filed
    as `chata` and as `restaurace` — so the second difference has to come from the body."""
    a = listing(1, source="bazos", category_main="komercni", subtype="chata",
                category_type="prodej", area_m2=160.0, price=7990000.0,
                description="Nabízíme chalupu. Evidenční číslo: 105952")
    b = listing(2, source="bazos", category_main="komercni", subtype="restaurace",
                category_type="prodej", area_m2=160.0, price=7990000.0,
                description="Nabízíme chalupu. Evidenční číslo: 110933")
    assert "agency_code_plus" not in names(a, b, S7)
    assert "agency_code_plus" in names(a, b, variant(d43_offered_use_conflict=False))


def test_e222_refuses_two_codes_across_portals() -> None:
    a = listing(1, source="bazos", category_main="komercni", subtype="obchodni_prostor",
                area_m2=25.0, price=20000.0, description=MG_RETAIL)
    b = listing(2, source="idnes", category_main="komercni", subtype="restaurace",
                area_m2=25.0, price=20000.0, description=MG_CAFE)
    assert "agency_code_plus" not in names(a, b, S7)


def test_e222_bare_subtype_limb_ships_off() -> None:
    """118 certain duplicates of the region cohort against 11 buys — refuted (M480)."""
    assert S7.d43_commercial_subtype_colive is False


# --- E223: the plot attribute a dropdown states ---------------------------------------------
# Vávrovice: one agency sells at least three 500 m² parcels at 925,000 under one template.
VAVROVICE_SLOPE = (
    "Nabízíme k prodeji stavební pozemek poblíž Opavy Vávrovice o velikosti 500 m2. Číslo "
    "zakázky: 135653 Druh pozemku: bydlení Celková plocha: 500 m2 Prodej po částech: Ne "
    "Inženýrské sítě: vodovod, elektřina Doprava: silnice, autobus Sklon pozemku: mírný svah"
)
VAVROVICE_FLAT = (
    "Nabízíme k prodeji stavební pozemek poblíž Opavy Vávrovice o velikosti 500 m2. Číslo "
    "zakázky: 135656 Druh pozemku: bydlení Celková plocha: 500 m2 Prodej po částech: Ne "
    "Inženýrské sítě: vodovod, elektřina Doprava: silnice, autobus Sklon pozemku: rovina"
)


def vavrovice() -> tuple[Listing, Listing]:
    a = listing(18777318, source="bazos", category_main="pozemek", category_type="prodej",
                first=0, last=4, area_m2=500.0, price=925000.0,
                description=VAVROVICE_SLOPE)
    b = listing(18868076, source="bazos", category_main="pozemek", category_type="prodej",
                first=5, last=13, area_m2=500.0, price=925000.0,
                description=VAVROVICE_FLAT)
    return a, b


def test_e223_reads_the_slope_two_parcels_of_one_parcelling_state() -> None:
    assert plot_attributes(VAVROVICE_SLOPE)["slope"] == frozenset({"mild"})
    assert plot_attributes(VAVROVICE_FLAT)["slope"] == frozenset({"flat"})
    a, b = vavrovice()
    assert names(a, b, S6) == []
    assert "plot_attribute" in names(a, b, S7)


def test_e223_two_contracts_stand_in_for_the_co_live_window() -> None:
    """These two never overlap: the FIRST comes down the day the second goes up, and what
    says the second is not a re-post of the first is the agency's own second order number."""
    a, b = vavrovice()
    assert "plot_attribute" not in names(
        a, b, variant(d43_plot_attribute_code_escape=False))


def test_e223_refuses_a_body_that_offers_two_values() -> None:
    a, b = vavrovice()
    both = listing(2, source="bazos", category_main="pozemek", category_type="prodej",
                   first=5, last=13, area_m2=500.0, price=925000.0,
                   description=VAVROVICE_FLAT.replace(
                       "Sklon pozemku: rovina", "Sklon pozemku: rovina až mírný svah"))
    assert "plot_attribute" not in names(a, both, S7)


def test_e223_never_reads_a_building() -> None:
    a = listing(1, category_main="byt", category_type="prodej", area_m2=60.0,
                description=VAVROVICE_SLOPE)
    b = listing(2, category_main="byt", category_type="prodej", area_m2=60.0,
                description=VAVROVICE_FLAT)
    assert "plot_attribute" not in names(a, b, S7)


# --- E224: the serviced-office product an offer leads with -----------------------------------
LASO_CATALOGUE = (
    " NEPLATÍTE PROVIZI Nabízíme pronájem soukromých kanceláří a coworkingových prostor v HQ "
    "Laso nacházející se na Masarykově náměstí v centru Ostravy. Kanceláře k dispozici – 8 "
    "m2, 10 m2, 15 m2, 20 m2, 30 m2, 45 m2, 100 m2."
)
LASO_DESK = "Nabízíme coworkingové prostory v HQ Laso. Můžete okamžitě začít pracovat v naší sdílené kanceláři." + LASO_CATALOGUE
LASO_ROOM = "Nabízíme soukromé kancelářské prostory pro 2 osoby v HQ Laso." + LASO_CATALOGUE


def test_e224_reads_the_product_off_the_first_sentence_only() -> None:
    assert commercial_product_class(LASO_DESK) == frozenset({"coworking"})
    assert commercial_product_class(LASO_ROOM) == frozenset({"private_office"})
    a = listing(90478, category_main="komercni", first=0, last=21, area_m2=10.0, price=3290.0,
                broker_key="agent-2722", broker_firm_id=2722, description=LASO_DESK)
    b = listing(90525, category_main="komercni", first=0, last=21, area_m2=10.0, price=8090.0,
                broker_key="agent-2722", broker_firm_id=2722, description=LASO_ROOM)
    assert names(a, b, S6) == []
    assert "product_class" in names(a, b, S7)


def test_e224_refuses_a_body_whose_first_sentence_names_the_catalogue() -> None:
    both = ("Nabízíme pronájem soukromých kanceláří a coworkingových prostor v HQ Laso."
            + LASO_CATALOGUE)
    assert commercial_product_class(both) == frozenset({"coworking", "private_office"})
    a = listing(1, category_main="komercni", area_m2=10.0, price=3290.0, description=both)
    b = listing(2, category_main="komercni", area_m2=10.0, price=8090.0,
                description=LASO_ROOM)
    assert "product_class" not in names(a, b, S7)


# --- D65: the per-category merge policy ------------------------------------------------------
def merged(lo: int, hi: int) -> Decision:
    return Decision(lo, hi, "merge", 0.99, {"text"}, None, None, "model")


def test_d65_holds_a_rental_propose_only_and_leaves_a_sale_alone() -> None:
    rental = listing(1, category_type="pronajem", category_main="byt", area_m2=50.0)
    sale = listing(2, category_type="prodej", category_main="byt", area_m2=50.0)
    held = apply_merge_policy(merged(1, 2), rental, rental, S7_HOLD)
    assert held.zone == "band" and "policy_hold" in held.reason
    kept = apply_merge_policy(merged(1, 2), sale, sale, S7_HOLD)
    assert kept.zone == "merge"


def test_d65_either_side_holds_the_pair() -> None:
    rental = listing(1, category_type="pronajem", category_main="byt", area_m2=50.0)
    unknown = listing(2, category_type=None, category_main="byt", area_m2=50.0)
    held = apply_merge_policy(merged(1, 2), unknown, rental, S7_HOLD)
    assert held.zone == "band"


def test_d65_never_promotes_and_never_touches_a_band_row() -> None:
    rental = listing(1, category_type="pronajem", category_main="byt", area_m2=50.0)
    banded = Decision(1, 2, "band", 0.4, set(), None, None, "model")
    assert apply_merge_policy(banded, rental, rental, S7_HOLD) is banded
    rejected = Decision(1, 2, "reject", 0.01, set(), None, None, "model")
    assert apply_merge_policy(rejected, rental, rental, S7_HOLD) is rejected


def test_d65_empty_table_holds_nothing() -> None:
    rental = listing(1, category_type="pronajem", category_main="byt", area_m2=50.0)
    assert S7.merge_policy == {}
    assert apply_merge_policy(merged(1, 2), rental, rental, S7).zone == "merge"
    assert merge_policy_verdict(rental, S7) is None


def test_d65_reads_the_most_specific_cell_first() -> None:
    cfg = variant(merge_policy={"pronajem|*": "propose", "pronajem|komercni": "merge"})
    flat = listing(1, category_type="pronajem", category_main="byt", area_m2=50.0)
    office = listing(2, category_type="pronajem", category_main="komercni", area_m2=50.0)
    assert merge_policy_verdict(flat, cfg) == "propose"
    assert merge_policy_verdict(office, cfg) == "merge"


def test_d65_refuses_a_malformed_table() -> None:
    for bad in ({"pronajem": "propose"}, {"pronajem|byt": "hold"}):
        try:
            variant(merge_policy=bad)
        except ValueError:
            continue
        raise AssertionError(f"merge_policy {bad} must not load")


# --- the generation contract -----------------------------------------------------------------
def test_w22_differs_from_w21_only_in_the_dials_this_wave_names() -> None:
    w21 = Settings.from_json(SETTINGS / "w21.json").to_dict()
    w22 = Settings.from_json(SETTINGS / "w22.json").to_dict()
    moved = {key for key in w22 if w21.get(key) != w22[key]}
    assert moved == {
        "d43_rental_colive", "d43_rental_colive_charges", "d43_charge_keywords_wide",
        "d43_rental_colive_cross_portal_limbs", "d43_rental_colive_sanitary",
        "d43_rental_colive_renovation", "d43_rental_colive_flooring",
        "d43_rental_colive_parking_level", "d43_unit_codes_english",
        "d43_unit_codes_slug", "d43_agency_code_with_difference",
        "d43_offered_use_conflict", "d43_plot_attribute_conflict",
        "d43_plot_attribute_code_escape", "d43_commercial_product_class",
    }


def test_w22_rentals_hold_is_w22_plus_one_table() -> None:
    w22 = Settings.from_json(SETTINGS / "w22.json").to_dict()
    hold = Settings.from_json(SETTINGS / "w22_rentals_hold.json").to_dict()
    assert {key for key in hold if w22.get(key) != hold[key]} == {"merge_policy"}
    assert hold["merge_policy"] == {"pronajem|*": "propose"}


def test_no_shipped_generation_before_w22_names_an_s7_dial() -> None:
    dials = {
        "d43_rental_colive", "d43_rental_colive_min_overlap_days",
        "d43_rental_colive_same_source_only", "d43_rental_colive_charges",
        "d43_rental_colive_house_number", "d43_rental_colive_sanitary",
        "d43_rental_colive_renovation", "d43_rental_colive_flooring",
        "d43_rental_colive_furnishing", "d43_rental_colive_parking_level",
        "d43_charge_keywords_wide", "d43_unit_codes_english", "d43_unit_codes_slug",
        "d43_slug_area", "d43_agency_code_with_difference", "d43_offered_use_conflict",
        "d43_commercial_subtype_colive", "d43_plot_attribute_conflict",
        "d43_plot_attribute_requires_colive", "d43_plot_attribute_code_escape",
        "d43_commercial_product_class", "merge_policy",
        "d43_rental_colive_number_same_source_only",
        "d43_rental_colive_cross_portal_limbs", "d43_rental_colive_honest_clock",
        "d43_rental_colive_charge_rent_multiple",
        "d43_rental_colive_charge_requires_equal_rent",
    }
    for arm in ("w13", "w15", "w17", "w18", "w19", "w20", "w21"):
        raw = json.loads((SETTINGS / f"{arm}.json").read_text(encoding="utf-8"))
        assert not (dials & set(raw)), f"{arm} names an S7 dial"


def test_every_s7_reading_stays_silent_under_w21() -> None:
    """The replay-parity direction, read at the fact grain rather than the settings grain."""
    new = {"rental_colive", "english_unit_code", "slug_unit", "agency_code_plus",
           "commercial_subtype", "plot_attribute", "product_class"}
    for pair in (cihelni(), privozska(), vavrovice()):
        for mode in (GATE, CLUSTER):
            assert not (new & set(names(pair[0], pair[1], S6, mode)))
