"""S6's readings (E210-E219), each built from the adverts that named it.

Every case is a real pair out of cohort 8: the OD Centrum Kobližná office (cluster 91377), the
Brno Business Park floor (44096 x 91427), the Brno-Tuřany two-storey office block (325959 x
325960), the Voroněžská parking house (18665717 x 18667672), the Vídeňská 18b 1+kk pair
(516195 x 18598828), the Dornych co-live residence (134311 x 134313), the Želivecká 4+kk twins
(323247 x 323524), the Regus Spielberk serviced offices (37701 x 91595), the Újezd u Brna
project built again in Brno-Chrlice (341616 x 18934291), the Rebešovice semi-detached halves
(263056 x 12736198) and the Ochoz u Brna four-parcel disposal (18775894 x 18775798). Where a
body is quoted it is quoted from the advert, with the sentences the reader must see verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

from autodedup.dataset import Listing
from autodedup.indistinguishable import GATE, distinguishing_facts
from autodedup.settings import Settings
from autodedup.text_facts import (
    accessory_areas,
    body_localities,
    built_connection,
    capacity_counts,
    capacity_counts_english,
    labelled_unit_ids,
    offered_storeys,
    place_names_match,
    priced_land_rows,
    prose_plot_areas_wide,
    states_second_plot,
)

SETTINGS = Path(__file__).resolve().parents[2] / "autodedup/settings"
S5 = Settings.from_json(SETTINGS / "w20.json")
S6 = Settings.from_json(SETTINGS / "w21.json")


def variant(**kwargs: object) -> Settings:
    raw = json.loads((SETTINGS / "w21.json").read_text(encoding="utf-8"))
    raw.update(kwargs)
    return Settings.from_dict(raw)


def stamp(day: int) -> str:
    return f"2026-0{1 + day // 28}-{1 + day % 28:02d}T00:00:00+00:00"


def listing(listing_id: int, first: int = 0, last: int = 40, **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "source": "sreality",
        "category_main": "komercni",
        "category_type": "pronajem",
        "first_seen_at": stamp(first),
        "last_seen_at": stamp(last),
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def names(a: Listing, b: Listing, cfg: Settings, mode: str = GATE) -> list[str]:
    return [fact.name for fact in distinguishing_facts(a, b, None, cfg, mode)]


# --- E210: `7. patro` and `8. NP` are one storey -------------------------------------------
# OD Centrum, Kobližná 24: five adverts of one 57 m² office at 7,900 Kč. The sreality body
# states its own storey in `patro` and the BUILDING's storeys in `NP` (Lidl on 1. and 2. NP,
# Sinsay on 3. NP); the realitymix body states the same storey as `8. nadzemní podlaží`.
KOBLIZNA_PATRO = (
    "Nabízíme vám k pronájmu kancelář o rozloze 57 m2 v 7. patře OD Centrum, Kobližná 24, v "
    "Brně. S touto kanceláří sousedí nově vybudované WC, které užívají jen někteří z nájemců "
    "kanceláří v 7. patře. V roce 2016 byly v domě vybudovány dva nové výtahy, které již "
    "jezdí až do 7. patra (dříve byl výtah jen do 6. patra). OD Centrum je dlouhodobě "
    "stoprocentně pronajat, kdy generálním nájemcem jsou potraviny Lidl, které užívají 1. PP, "
    "1. NP a 2. NP obchodního domu, ve 3. NP je nově otevřená prodejna Sinsay."
)
KOBLIZNA_NP = (
    "Nabízíme k pronájmu kancelářský prostor o velikosti 57 m² v centru Brna. Tento prostor se "
    "nachází v 8. nadzemním podlaží montované budovy, což zajišťuje moderní a flexibilní "
    "prostředí pro vaši firmu."
)


def test_e210_patro_and_np_agree_on_the_converted_scale() -> None:
    a = listing(91377, area_m2=57.0, price=7900.0, description=KOBLIZNA_PATRO)
    b = listing(452625, source="realitymix", area_m2=57.0, price=7900.0,
                description=KOBLIZNA_NP)
    assert "prose_floor" in names(a, b, S5)
    assert names(a, b, S6) == []


def test_e210_does_not_forgive_a_worded_gap_the_bodies_never_share() -> None:
    a = listing(1, category_main="byt", area_m2=60.0,
                description="Byt se nachází ve třetím patře cihlového domu.")
    b = listing(2, category_main="byt", area_m2=60.0,
                description="Byt se nachází v šestém patře cihlového domu.")
    assert "prose_floor" in names(a, b, S6)


def test_e210_is_a_dial() -> None:
    a = listing(91377, area_m2=57.0, description=KOBLIZNA_PATRO)
    b = listing(452625, source="realitymix", area_m2=57.0, description=KOBLIZNA_NP)
    assert "prose_floor" in names(a, b, variant(d43_floor_cross_form_agreement=False))


# --- E211: the headline a commercial body leads with, against its own column ---------------
BUSINESS_PARK = (
    "Bez realitního poplatku nabízíme pronájem moderních kancelářských prostor v "
    "administrativním centru Brno Business Park při ulici Vídeňská. Kanceláře o ploše "
    "přibližně {lead} m² se nachází v 5.NP budovy C. Je možné oddělit plochu od cca 450 m² a "
    "více, přičemž na výběr je z více možností v několika podlažích."
)


def test_e211_a_lead_that_contradicts_its_own_column_is_a_fact() -> None:
    a = listing(44096, area_m2=860.0, description=BUSINESS_PARK.format(lead="860"))
    b = listing(91427, area_m2=860.0, description=BUSINESS_PARK.format(lead="450"))
    assert "headline_area" not in names(a, b, S5)
    assert "headline_area" in names(a, b, S6)


def test_e211_refuses_two_leads_that_each_match_their_own_column() -> None:
    a = listing(1, area_m2=860.0, description=BUSINESS_PARK.format(lead="860"))
    b = listing(2, area_m2=450.0, description=BUSINESS_PARK.format(lead="450"))
    assert "headline_area" not in names(a, b, S6)


def test_e211_is_commercial_only_and_colive_only() -> None:
    flat_a = listing(1, category_main="byt", area_m2=860.0,
                     description=BUSINESS_PARK.format(lead="860"))
    flat_b = listing(2, category_main="byt", area_m2=860.0,
                     description=BUSINESS_PARK.format(lead="450"))
    assert "headline_area" not in names(flat_a, flat_b, S6)
    early = listing(3, first=0, last=5, area_m2=860.0,
                    description=BUSINESS_PARK.format(lead="860"))
    late = listing(4, first=30, last=40, area_m2=860.0,
                   description=BUSINESS_PARK.format(lead="450"))
    assert "headline_area" not in names(early, late, S6)


# --- E212: the storey a letting names as the OFFER -----------------------------------------
TURANY_GROUND = (
    "Pronájem přízemního podlaží v rámci kancelářského objektu s parkováním 300 m², Brno - "
    "Tuřany. Jedná se o přízemní podlaží samostatného dvoupodlažního kancelářského objektu. "
    "Podlaží je již členěné na 2 malé kanceláře 17 a 23m² a 2 větší kanceláře 43m² a 40m²."
)
TURANY_FIRST = (
    "Pronájem samostatného kancelářského podlaží s parkováním 300 m², Brno - Tuřany. V rámci "
    "kancelářského objektu je možnost pronájmu samostatného 1. patra s vl. zázemím i "
    "kuchyňkou. Možnost příručního skládku v přízemí."
)
VORONEZSKA_GROUND = (
    "Nabízíme k prodeji družstevní podíl ke krytému parkovacímu stání na ul. Voroněžská. "
    "Místo o ploše 14m² se nachází v nejžádanějším přízemí parkovacího domu. V 1.NP se "
    "nachází mycí box. CELKEM NABÍZÍM 3 PARKOVACÍ MÍSTA - k prodeji jsou ještě další 2 stání "
    "(jedno se nachází v přízemí, druhé ve 3.NP)."
)
VORONEZSKA_THIRD = (
    "Nabízíme k prodeji družstevní podíl ke krytému parkovacímu stání č. 314 na ul. "
    "Voroněžská. Místo o ploše 14m² se nachází v 3 nadzemním podlaží parkovacího domu. V 1.NP "
    "se nachází mycí box."
)


def test_e212_reads_the_storey_the_letting_offers() -> None:
    assert offered_storeys(TURANY_GROUND) == frozenset({1})
    assert offered_storeys(TURANY_FIRST) == frozenset({2})
    assert offered_storeys(VORONEZSKA_GROUND) == frozenset({1})
    assert offered_storeys(VORONEZSKA_THIRD) == frozenset({3})


def test_e212_one_storey_inside_one_feed_is_a_fact() -> None:
    a = listing(325959, source="idnes", broker_key="k", area_m2=300.0,
                description=TURANY_GROUND)
    b = listing(325960, source="idnes", broker_key="k", area_m2=300.0,
                description=TURANY_FIRST)
    assert "offered_storey" not in names(a, b, S5)
    assert "offered_storey" in names(a, b, S6)


def test_e212_one_storey_across_two_portals_is_the_convention() -> None:
    a = listing(1, source="idnes", area_m2=300.0, description=TURANY_GROUND)
    b = listing(2, source="bazos", area_m2=300.0, description=TURANY_FIRST)
    assert "offered_storey" not in names(a, b, S6)


def test_e212_two_storeys_are_a_fact_across_portals() -> None:
    a = listing(18665717, category_main="ostatni", category_type="prodej", area_m2=14.0,
                price=680000.0, description=VORONEZSKA_GROUND)
    b = listing(18667672, source="ceskereality", category_main="ostatni",
                category_type="prodej", area_m2=14.0, price=680000.0,
                description=VORONEZSKA_THIRD)
    assert "offered_storey" in names(a, b, S6)


def test_e212_does_not_read_a_notice_board_in_the_entrance() -> None:
    board = ("Možnost pronájmu vývěsky – nerezové prosklené vitrínky umístěné v přízemí "
             "vstupu do domu z ulice Kobližná.")
    assert offered_storeys(KOBLIZNA_PATRO + " " + board) == frozenset()


# --- E213: one AGENCY on one portal is one feed --------------------------------------------
# Vídeňská 1014/18b: two 41 m² 1+kk of one landlord, posted by two of its agents and never on
# sale together, so E180's within-camp excuse stands and only the FEED can decide.
def test_e213_the_agency_carries_the_convention_not_the_agent() -> None:
    a = listing(516195, first=0, last=10, category_main="byt", area_m2=41.0, floor=0,
                price=14990.0, broker_key="agent-a", broker_firm_id=2058,
                description="Byt se nachází v 1. nadzemním podlaží.")
    b = listing(18598828, first=25, last=35, category_main="byt", area_m2=41.0, floor=1,
                price=12990.0, broker_key="agent-b", broker_firm_id=2058,
                description="Byt se nachází ve 2. nadzemním podlaží.")
    assert "floor" not in names(a, b, S5)
    assert "floor" in names(a, b, S6)


def test_e213_two_agencies_on_one_portal_are_two_feeds() -> None:
    a = listing(1, first=0, last=10, category_main="byt", area_m2=41.0, floor=0,
                price=14990.0, broker_key="agent-a", broker_firm_id=2058,
                description="Byt se nachází v 1. nadzemním podlaží.")
    b = listing(2, first=25, last=35, category_main="byt", area_m2=41.0, floor=1,
                price=12990.0, broker_key="agent-b", broker_firm_id=3466,
                description="Byt se nachází ve 2. nadzemním podlaží.")
    assert "floor" not in names(a, b, S6)


# --- E214: the unit id printed under its own LABEL -----------------------------------------
DORNYCH = (
    "Pronájem moderního 1+kk ({tier}) u Vaňkovky BEZ PROVIZE Nabízíme k pronájmu světlý, "
    "zařízený byt 1+kk (24 m²) na adrese Dornych. Nájemné: 16 900 Kč Zálohy za energie, "
    "služby a Internet: 4 000 Kč Vratná kauce: 25 000 Kč ID jednotky: {code} FOR ENGLISH - "
    "please do not hesitate to contact the real estate agent."
)


def test_e214_reads_a_letters_only_id_under_its_label() -> None:
    assert labelled_unit_ids(DORNYCH.format(tier="Double B", code="DOUBLE B")) == frozenset(
        {"double b"})
    assert labelled_unit_ids(DORNYCH.format(tier="Standard", code="STANDARD")) == frozenset(
        {"standard"})


def test_e214_two_printed_unit_ids_are_two_units() -> None:
    a = listing(134311, category_main="byt", area_m2=24.0, price=16900.0, source="idnes",
                description=DORNYCH.format(tier="Double B", code="DOUBLE B"))
    b = listing(134313, category_main="byt", area_m2=24.0, price=16900.0, source="idnes",
                description=DORNYCH.format(tier="Double A", code="DOUBLE A"))
    assert "labelled_unit" not in names(a, b, S5)
    assert "labelled_unit" in names(a, b, S6)


def test_e214_one_id_twice_is_one_unit() -> None:
    a = listing(1, category_main="byt", area_m2=24.0,
                description=DORNYCH.format(tier="Double B", code="DOUBLE B"))
    b = listing(2, category_main="byt", area_m2=24.0, source="idnes",
                description=DORNYCH.format(tier="Double B", code="DOUBLE B"))
    assert "labelled_unit" not in names(a, b, S6)


def test_e214_needs_the_label() -> None:
    assert labelled_unit_ids("Nabízíme byt typu DOUBLE B u Vaňkovky.") == frozenset()


# --- E215: the cellar the body states ------------------------------------------------------
ZELIVECKA_NINE = (
    "Tento krásný byt 4+kk na Zahradním Městě se nachází ve třetím nadzemním podlaží menšího "
    "bytového domu o šesti jednotkách. Součástí bytu je zasklená lodžie a dva sklepy o "
    "celkové ploše 9 m², které nabízejí dostatek úložného prostoru."
)
ZELIVECKA_TEN = (
    "Tento krásný byt na Zahradním Městě se nachází ve 3. nadzemním podlaží komorního domu o "
    "pouhých šesti jednotkách. Součástí bytu je zasklená lodžie a také dva sklepní prostory – "
    "jeden pod schody a druhý jako klasická sklepní kóje. Celkem 10 m² úložného prostoru."
)


def test_e215_reads_the_stated_cellar() -> None:
    assert accessory_areas(ZELIVECKA_NINE) == frozenset({9.0})
    assert accessory_areas(ZELIVECKA_TEN) == frozenset({10.0})


def test_e215_nine_against_ten_is_two_flats() -> None:
    a = listing(323247, category_main="byt", category_type="prodej", area_m2=90.0, floor=2,
                price=10900000.0, description=ZELIVECKA_NINE)
    b = listing(323524, category_main="byt", category_type="prodej", area_m2=90.0, floor=2,
                price=10900000.0, description=ZELIVECKA_TEN)
    assert "accessory_area" not in names(a, b, S5)
    assert "accessory_area" in names(a, b, S6)


def test_e215_refuses_a_rounding_and_a_sequential_posting() -> None:
    rounded = listing(1, category_main="byt", category_type="prodej", area_m2=90.0,
                      description=ZELIVECKA_NINE.replace("9 m²", "9,4 m²"))
    other = listing(2, category_main="byt", category_type="prodej", area_m2=90.0,
                    description=ZELIVECKA_TEN.replace("10 m²", "9 m²"))
    assert "accessory_area" not in names(rounded, other, S6)
    early = listing(3, first=0, last=5, category_main="byt", category_type="prodej",
                    area_m2=90.0, price=10900000.0, description=ZELIVECKA_NINE)
    late = listing(4, first=30, last=40, category_main="byt", category_type="prodej",
                   area_m2=90.0, price=10900000.0, description=ZELIVECKA_TEN)
    assert "accessory_area" not in names(early, late, S6)


# --- E216: the capacity written in English -------------------------------------------------
REGUS_CZ = ("Tato nabídka zahrnuje soukromou servisovanou kancelář pro 1 osobu a navíc "
            "přístup do sdílených prostor: zasedacích místností a coffee pointu.")
REGUS_EN = ("This offer includes a private serviced office space for 2 workstations, ideal "
            "for 2 employees, plus access to shared meeting rooms and a coffee point.")


def test_e216_reads_workstations_and_employees() -> None:
    assert capacity_counts(REGUS_CZ) == {1}
    assert capacity_counts_english(REGUS_CZ) == set()
    assert capacity_counts_english(REGUS_EN) == {2}


def test_e216_one_person_against_two_workstations_is_two_products() -> None:
    a = listing(37701, area_m2=50.0, price=8190.0, description=REGUS_CZ)
    b = listing(91595, area_m2=50.0, price=8190.0, description=REGUS_EN)
    assert "extent" not in names(a, b, S5)
    assert "extent" in names(a, b, S6)


def test_e216_needs_an_office_noun() -> None:
    assert capacity_counts_english("A bright flat, suitable for 2 people, near the park.") == set()


# --- E217: the place the BODY names --------------------------------------------------------
UJEZD = ("Nabízíme k prodeji mezonetový byt 5+kk v novostavbě domu se třemi samostatnými "
         "jednotkami v Újezdu u Brna. K bytu náleží parkovací stání na pozemku domu.")
CHRLICE = ("Vzorová dokončená nemovitost se nachází v Újezdu u Brna. V Újezdu u Brna zároveň "
           "nabízíme k prodeji dokončené jednotky tohoto projektu, včetně tohoto typu "
           "mezonetového bytu 5+kk o ploše 126 m2.")


def flat(listing_id: int, obec: str, cast: str | None, body: str, **kwargs: object) -> Listing:
    location: dict[str, object] = {"obec_name": obec, "granularity": "cast_obce_or_quarter"}
    if cast is not None:
        location["cast_obce_name"] = cast
    return listing(listing_id, category_main="byt", category_type="prodej", area_m2=126.0,
                   price=9499000.0, description=body, location=location, **kwargs)


def test_e217_reads_the_place_and_its_declension() -> None:
    assert body_localities(UJEZD) == frozenset({"ujezdu u brna"})
    assert place_names_match("ujezdu u brna", "ujezd u brna")
    assert place_names_match("brne", "brno")
    assert not place_names_match("ujezdu u brna", "brno")


def test_e217_a_project_built_in_two_municipalities() -> None:
    a = flat(341616, "Újezd u Brna", "Újezd u Brna", UJEZD)
    b = flat(18934291, "Brno", "Chrlice", CHRLICE)
    assert "body_obec" not in names(a, b, S5)
    assert "body_obec" in names(a, b, S6)


def test_e217_a_village_filed_under_its_town_is_not_a_fact() -> None:
    a = flat(1, "Brno", "Chrlice", "Nabízíme byt 5+kk v Chrlicích, v klidné části.")
    b = flat(2, "Brno", "Chrlice", CHRLICE)
    assert "body_obec" not in names(a, b, S6)


def test_e217_needs_the_other_side_known_down_to_the_part() -> None:
    a = flat(1, "Újezd u Brna", "Újezd u Brna", UJEZD)
    b = flat(2, "Brno", None, CHRLICE)
    assert "body_obec" not in names(a, b, S6)


def test_e217_needs_the_speaker_to_name_its_own_place() -> None:
    a = flat(1, "Brno", "Chrlice", CHRLICE)
    b = flat(2, "Újezd u Brna", "Újezd u Brna", "Byt 5+kk, novostavba, ihned k nastěhování.")
    assert "body_obec" not in names(a, b, S6)


# --- E218: the land the body states --------------------------------------------------------
REBESOVICE = (
    "Novostavba RD 5+kk, CP {plot} m² Rebešovice. V exkluzivním zastoupení nabízíme k prodeji "
    "cihlový rodinný dům s dispozicí 5+kk a zahradou v Rebešovicích. Celková plocha pozemku "
    "činí {plot} m², zastavěná plocha 73 m², užitná plocha RD 156 m², zahrada {garden} m²."
)
OCHOZ_ONE = (
    "Prodej pozemku v Ochozi u Brna vedle stávající zástavby v ulici Za Oborou (818 m2) - "
    "inženýrské sítě v dosahu. Cena za pozemek 1.880.000,- Kč."
)
OCHOZ_CATALOGUE = OCHOZ_ONE + (
    " Orná půda pole (1794 m2) - možnost využití pro pěstování plodin. Cena za pozemek "
    "484.000,- Kč. Obora (4840 m2) je zde vybudována obora z dřevěných kůlů a lesnického "
    "pletiva, a seník. Přes pozemek teče potok. Stavby jsou legální (schválení ze strany "
    "CHKO). Cena za pozemek 958.000,- Kč včetně staveb. Les (1258 m2) byl plánovaný jako "
    "lesní školka. Cena za pozemek 198.000,- Kč."
)


def test_e218_reads_the_plot_written_before_the_noun() -> None:
    assert prose_plot_areas_wide(REBESOVICE.format(plot="1.288", garden="1.168")) == frozenset(
        {1288.0})
    assert prose_plot_areas_wide(REBESOVICE.format(plot="1.294", garden="1.172")) == frozenset(
        {1294.0})


def test_e218_six_square_metres_of_parcel_are_two_houses() -> None:
    a = listing(263056, source="idnes", category_main="dum", category_type="prodej",
                area_m2=156.0, price=19000000.0,
                description=REBESOVICE.format(plot="1.288", garden="1.168"))
    b = listing(12736198, first=30, last=60, source="idnes", category_main="dum",
                category_type="prodej", area_m2=156.0, price=15990000.0,
                description=REBESOVICE.format(plot="1.294", garden="1.172"))
    assert "plot_prose_exact" not in names(a, b, S5)
    assert "plot_prose_exact" in names(a, b, S6)


def test_e218_refuses_one_parcel_written_twice_and_a_flat() -> None:
    same_a = listing(1, category_main="dum", category_type="prodej", area_m2=156.0,
                     description=REBESOVICE.format(plot="1.288", garden="1.168"))
    same_b = listing(2, category_main="dum", category_type="prodej", area_m2=156.0,
                     description=REBESOVICE.format(plot="1.288", garden="1.170"))
    assert "plot_prose_exact" not in names(same_a, same_b, S6)
    coarse_a = listing(3, category_main="dum", category_type="prodej", area_m2=156.0,
                       description=REBESOVICE.format(plot="897", garden="800"))
    coarse_b = listing(4, category_main="dum", category_type="prodej", area_m2=156.0,
                       description=REBESOVICE.format(plot="900", garden="800"))
    assert "plot_prose_exact" not in names(coarse_a, coarse_b, S6)
    flat_a = listing(5, category_main="byt", category_type="prodej", area_m2=156.0,
                     description=REBESOVICE.format(plot="1.288", garden="1.168"))
    flat_b = listing(6, category_main="byt", category_type="prodej", area_m2=156.0,
                     description=REBESOVICE.format(plot="1.294", garden="1.172"))
    assert "plot_prose_exact" not in names(flat_a, flat_b, S6)


def test_e218_reads_the_sellers_price_list() -> None:
    assert priced_land_rows(OCHOZ_ONE) == ((818.0, 1880000.0),)
    assert priced_land_rows(OCHOZ_CATALOGUE) == (
        (818.0, 1880000.0), (1258.0, 198000.0), (1794.0, 484000.0), (4840.0, 958000.0))


def test_e218_the_price_says_which_plot_this_advert_is() -> None:
    a = listing(18775894, source="bazos", category_main="pozemek", category_type="prodej",
                area_m2=818.0, price=1880000.0, description=OCHOZ_ONE)
    b = listing(18775798, source="bazos", category_main="pozemek", category_type="prodej",
                area_m2=818.0, price=958000.0, description=OCHOZ_CATALOGUE)
    assert "priced_row" not in names(a, b, S5)
    assert "priced_row" in names(a, b, S6)


def test_e218_one_row_chosen_twice_is_one_plot() -> None:
    a = listing(1, source="bazos", category_main="pozemek", category_type="prodej",
                area_m2=818.0, price=1880000.0, description=OCHOZ_CATALOGUE)
    b = listing(2, source="bazos", category_main="pozemek", category_type="prodej",
                area_m2=818.0, price=1880000.0, description=OCHOZ_CATALOGUE)
    assert "priced_row" not in names(a, b, S6)


# --- E219: two plots the sellers say are both on offer -------------------------------------
OPATOVICE = (
    "Dobrý den, nabízíme k prodeji stavební pozemek o výměře 500 m² v ulici Velké dráhy v "
    "Opatovicích u Rajhradu. {connection}Na hranici pozemku jsou dostupné inženýrské sítě – "
    "vodovod, splašková kanalizace a kabelové komunikační vedení. Současně je nabízen také "
    "bezprostředně sousedící stavební pozemek o výměře 500 m². Oba pozemky lze po dohodě s "
    "jejich vlastníky koupit společně jako celek o výměře 1 000 m²."
)
BUILT = ("Významnou výhodou tohoto pozemku je již vybudovaná elektrická přípojka s osazeným "
         "elektroměrem. ")


def test_e219_reads_the_second_plot_and_the_built_connection() -> None:
    assert states_second_plot(OPATOVICE.format(connection=""))
    assert built_connection(OPATOVICE.format(connection=BUILT))
    assert not built_connection(OPATOVICE.format(connection=""))
    assert not states_second_plot("Nabízíme k prodeji stavební pozemek o výměře 500 m².")


def test_e219_one_stated_difference_between_two_offered_plots() -> None:
    a = listing(404778, source="realitymix", category_main="pozemek", category_type="prodej",
                area_m2=500.0, price=3990000.0,
                description=OPATOVICE.format(connection=""))
    b = listing(18674033, source="ceskereality", category_main="pozemek",
                category_type="prodej", area_m2=500.0, price=3990000.0,
                description=OPATOVICE.format(connection=BUILT))
    assert "neighbour_plot" not in names(a, b, S5)
    assert "neighbour_plot" in names(a, b, S6)


def test_e219_absence_alone_is_never_the_fact() -> None:
    plain = OPATOVICE.format(connection="").replace(
        "Současně je nabízen také bezprostředně sousedící stavební pozemek o výměře 500 m². "
        "Oba pozemky lze po dohodě s jejich vlastníky koupit společně jako celek o výměře "
        "1 000 m².", "")
    a = listing(1, category_main="pozemek", category_type="prodej", area_m2=500.0,
                price=3990000.0, description=plain)
    b = listing(2, source="ceskereality", category_main="pozemek", category_type="prodej",
                area_m2=500.0, price=3990000.0, description=BUILT + plain)
    assert "neighbour_plot" not in names(a, b, S6)


def test_e219_two_built_connections_are_no_difference() -> None:
    a = listing(1, category_main="pozemek", category_type="prodej", area_m2=500.0,
                price=3990000.0, description=OPATOVICE.format(connection=BUILT))
    b = listing(2, source="ceskereality", category_main="pozemek", category_type="prodej",
                area_m2=500.0, price=3990000.0,
                description=OPATOVICE.format(connection=BUILT))
    assert "neighbour_plot" not in names(a, b, S6)


def test_e219_needs_the_two_to_be_on_sale_together() -> None:
    a = listing(1, first=0, last=5, category_main="pozemek", category_type="prodej",
                area_m2=500.0, price=3990000.0,
                description=OPATOVICE.format(connection=""))
    b = listing(2, first=30, last=40, source="ceskereality", category_main="pozemek",
                category_type="prodej", area_m2=500.0, price=3990000.0,
                description=OPATOVICE.format(connection=BUILT))
    assert "neighbour_plot" not in names(a, b, S6)


# --- the bars each reading needs, measured on the cohorts and pinned here ------------------
def test_e211_refuses_two_bodies_that_both_contradict_their_columns() -> None:
    body = ("Do prodeje se dostává stavba občanského komerčního vybavení. Stavba z cihel na "
            "ploše {lead} m2 je z větší části podsklepena.")
    a = listing(46382, area_m2=1284.0, description=body.format(lead="641"))
    b = listing(46383, area_m2=1284.0, description=body.format(lead="1000"))
    assert "headline_area" not in names(a, b, S6)


def test_e211_refuses_a_lead_whose_body_also_states_its_column() -> None:
    a = listing(40457, area_m2=654.0, description=(
        "Nabízím k prodeji zděný objekt s celkovou užitnou plochou 654 m² pro investory."))
    b = listing(48439, category_main="dum", area_m2=654.0, description=(
        "1. NP: atypický byt 3kk o výměře 120 m2; 2. NP: byt 6+1 o výměře 203 m2. "
        "Celková užitná plocha činí 654 m²."))
    assert "headline_area" not in names(a, b, S6)


UJEZD_FLAT = ("Nabízíme k pronájmu moderní byt 2+kk s terasou o velikosti 66 m² v klidné "
              "části obce Újezd. Byt se nachází ve {storey}. nadzemním podlaží novostavby. "
              "Celkem má budova 4 nadzemní podlaží a 1 podzemní podlaží.")


def test_e212_refuses_a_storey_that_contradicts_its_own_column() -> None:
    a = listing(34819, category_main="byt", category_type="pronajem", area_m2=66.0, floor=2,
                price=20000.0, description=UJEZD_FLAT.format(storey="2"))
    b = listing(378669, source="realitymix", category_main="byt", category_type="pronajem",
                area_m2=66.0, floor=2, price=21000.0, description=UJEZD_FLAT.format(storey="4"))
    assert offered_storeys(a.description) == frozenset({2})
    assert offered_storeys(b.description) == frozenset({4})
    assert "offered_storey" not in names(a, b, S6)


def test_e210_column_limb_yields_to_the_storey_both_bodies_state() -> None:
    body = ("Nabízíme ke koupi zkolaudovaný byt 3+kk s velkou terasou, situovaný ve druhém "
            "patře novostavby cihlového bytového domu se třemi bytovými jednotkami.")
    a = listing(16337, first=0, last=10, category_main="byt", category_type="prodej",
                area_m2=86.0, floor=2, price=12390000.0, broker_firm_id=7, description=body)
    b = listing(18934706, first=25, last=35, category_main="byt", category_type="prodej",
                area_m2=86.0, floor=1, price=10800000.0, broker_firm_id=7, description=body)
    assert "floor" in names(a, b, variant(d43_floor_cross_form_agreement=False))
    assert "floor" not in names(a, b, S6)


def test_e215_needs_a_stated_total() -> None:
    a = listing(463564, category_main="byt", category_type="prodej", area_m2=70.0,
                description=("K bytu náleží klasická sklepní kóje 2m2 s okýnkem a navíc podíl "
                             "na velké sklepní místnosti o výměře 13m2."))
    b = listing(14048448, source="realitymix", category_main="byt", category_type="prodej",
                area_m2=70.0, description=("K bytu náleží sklepní kóje s okýnkem o velikosti "
                                           "2 m² a navíc podíl na velké sklepní místnosti."))
    assert accessory_areas(a.description) == frozenset()
    assert "accessory_area" not in names(a, b, S6)


def test_e217_needs_one_sellers_own_feed() -> None:
    a = flat(47803, "Dobřany", "Šlovice", "V Šlovice nabízíme novostavbu rodinného domu.",
             broker_key="k", broker_firm_id=9)
    b = flat(186296, "Plzeň", "Plzeň", "Nabízíme dům, a přitom jste v Plzeň za 10 minut.",
             source="bazos", broker_key="k", broker_firm_id=9)
    assert "body_obec" not in names(a, b, S6)


def test_e217_reads_a_capitalised_preposition() -> None:
    assert body_localities("V Šlovice nabízíme novostavbu.") == frozenset({"slovice"})


def test_e218_reads_only_the_band_between_the_exact_bar_and_the_tolerance() -> None:
    a = listing(291231, category_main="pozemek", category_type="prodej", area_m2=913.0,
                price=3700000.0,
                description="Celková plocha pozemku činí 913 m² v obci Bělkovice-Lašťany.")
    b = listing(18777561, source="bazos", category_main="pozemek", category_type="prodej",
                area_m2=913.0, price=3700000.0,
                description="Celková plocha pozemku činí 690 m² s příjezdovou cestou 223 m².")
    assert "plot_prose_exact" not in names(a, b, S6)
