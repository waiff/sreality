"""S10's readings (E250, E251, E252, E253, E254), each built from the advert that named it.

Every body below is a real advert out of cohort 12. The Stará Lípa project of three houses
(`13438069` and its re-posts, `13466906`, `13438071` and its re-posts, and a project advert
naming no house at all) is the whole of E250: one plot, one 116 m², one 9,300,000 Kč, and the
only thing that parts the houses is `Pro více informací k domu č.1` against `domluvte si
schůzku na domě č.3`. Rezidence Chodovec in Praha-Chodov is E251: Nebřenická 2433/1 let
`nezařízený` at 25,500 → 23,900 against 2433/3 let `částečně zařízený — k dispozici je postel
a prostorná šatní skříň` at 25,500, both on sreality and both on bezrealitky at once. The
Masarykova villa in Liberec is E252: two 35 m² offices at one 6,000 Kč, one `ve sdíleném
administrativním prostoru` with `sdílené zázemí (kuchyň, koupelna, WC)` and one a `Samostatná
kancelář s vlastním sociálním zařízením (WC)`. The Tanvaldská surgery is E254: 96 m² at 24,000
Kč on three portals and `250` on realitymix, which is 250 × 96 to the koruna.
"""

from __future__ import annotations

import json
from pathlib import Path

from autodedup.dataset import Listing
from autodedup.indistinguishable import (
    CLUSTER, GATE, distinguishing_facts, rental_colive_conflict)
from autodedup.repartition import Edge, partition
from autodedup.settings import Settings
from autodedup.text_facts import (
    facility_tenure, furnished_state_wide, printed_designators)

SETTINGS = Path(__file__).resolve().parents[2] / "autodedup/settings"
S9 = Settings.from_json(SETTINGS / "w24.json")
S10 = Settings.from_json(SETTINGS / "w25.json")


def variant(**kwargs: object) -> Settings:
    raw = json.loads((SETTINGS / "w25.json").read_text(encoding="utf-8"))
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
        "location": {"obec_kod": 554782, "street_key": "nebrenicka",
                     "granularity": "address_point", "granularity_rank": 100,
                     "is_address_grain": True},
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def names(a: Listing, b: Listing, cfg: Settings, mode: str = GATE) -> list[str]:
    return [fact.name for fact in distinguishing_facts(a, b, None, cfg, mode)]


# --- E250: the designator noun, inflected ----------------------------------------------------
LIPA_HOUSE_1 = (
    "Představujeme Vám moderní novostavbu rodinného domu o dispozici 4+kk a podlahové "
    "ploše 116 m², která je součástí komorního rezidenčního projektu pouhých tří domů v "
    "klidné části České Lípy – Stará Lípa. Dům nabízí promyšlenou dispozici ve dvou "
    "podlažích, prostorný obývací pokoj propojený s kuchyňským koutem, tři samostatné "
    "pokoje, dvě koupelny, balkon, vlastní zahradu i parkovací stání. Celý areál je "
    "oplocený, vybaven elektrickou vjezdovou bránou a společným technickým zázemím. "
    "Pro více informací k domu č.1 a sjednání prohlídky, volejte makléře."
)
LIPA_HOUSE_3 = (
    "Moderní architektura, kvalitní materiály a technologie, které myslí na budoucnost. "
    "Přesně tak lze charakterizovat tuto novostavbu rodinného domu o dispozici 4+kk s "
    "podlahovou plochou 116 m². Dům nabízí komfortní bydlení ve dvou podlažích s vlastním "
    "balkonem, zahradou a parkovacím stáním. Projekt je navržen jako uzavřený rezidenční "
    "areál se třemi domy. Tento dům má navíc boční vchod, který lze využít dle vlastního "
    "uvážení. Pro více informací a sjednání prohlídky, volejte makléře a domluvte si "
    "schůzku na domě č.3."
)
LIPA_HOUSE_2 = (
    "Hledáte moderní bydlení, které spojuje kvalitní stavební provedení, nízké provozní "
    "náklady a dostatek soukromí? Pak Vás zaujme tato novostavba rodinného domu o dispozici "
    "4+kk a podlahové ploše 116 m². Celý projekt tvoří pouze tři domy v uzavřeném "
    "rezidenčním areálu. Pro více informací a sjednání prohlídky, volejte makléře a "
    "domluvte si schůzku na domě č. 2."
)
LIPA_PROJECT = (
    "Prodej bytu 4+kk v České Lípě, ul. Liberecká. Nabízíme moderní mezonetové bydlení "
    "4+kk o podlahové ploše 116 m², které je součástí komorního rezidenčního projektu "
    "pouhých tří jednotek v České Lípě. Pro více informací volejte makléře a domluvte si "
    "prohlídku ještě dnes."
)


def house(listing_id: int, body: str, source: str = "ceskereality") -> Listing:
    return listing(listing_id, source=source, category_main="dum", category_type="prodej",
                   area_m2=116.0, price=9300000.0, description=body,
                   location={"obec_kod": 561380, "street_key": "liberecka",
                             "granularity": "street", "granularity_rank": 60})


def test_the_inflected_noun_is_read_in_every_case_the_project_wrote() -> None:
    assert printed_designators(LIPA_HOUSE_1) == {"dum": frozenset({"1"})}
    assert printed_designators(LIPA_HOUSE_3) == {"dum": frozenset({"3"})}
    assert printed_designators(LIPA_HOUSE_2) == {"dum": frozenset({"2"})}
    assert printed_designators("v bytě č. 7 o výměře 40 m²") == {"byt": frozenset({"7"})}
    assert printed_designators("prohlídka domu č.p. 12") == {"dum": frozenset({"12"})}


def test_the_project_advert_names_no_house_and_a_menu_abstains() -> None:
    assert printed_designators(LIPA_PROJECT) == {}
    assert printed_designators("prodáváme dům č.1, dům č. 2 a dům č.3") == {}
    assert printed_designators("novostavbu rodinného domu o dispozici 4+kk") == {}
    # A bare numeral after a Czech noun is grammar, never a name.
    assert printed_designators("tři domy 4+kk a dva byty 2+1") == {}


def test_a_number_bigger_than_a_designator_is_an_address_and_E240_owns_it() -> None:
    assert printed_designators("dům č.p. 1561 v Praze") == {}


def test_the_three_houses_are_parted_and_the_project_advert_is_not() -> None:
    one, three, two = (house(13438069, LIPA_HOUSE_1), house(13438071, LIPA_HOUSE_3),
                       house(13466906, LIPA_HOUSE_2, "bazos"))
    project = house(13462796, LIPA_PROJECT, "idnes")
    assert "printed_designator" not in names(one, three, S9)
    for left, right in ((one, three), (one, two), (three, two)):
        assert names(left, right, S10) == ["printed_designator"]
        assert names(left, right, S10, CLUSTER) == ["printed_designator"]
    assert names(one, project, S10) == []
    assert names(three, project, S10) == []


def test_one_house_re_posted_is_still_one_house() -> None:
    assert names(house(13438069, LIPA_HOUSE_1),
                 house(13462784, LIPA_HOUSE_1, "sreality"), S10) == []


# --- E251: the fit-out a letting states about itself -----------------------------------------
CHODOVEC_UNFURNISHED = (
    "Hledáte moderní bydlení v lokalitě s výbornou občanskou vybaveností? Pak by vás mohl "
    "zaujmout tento prostorný byt 2+kk o výměře 56 m² v ulici Nebřenická, Praha – Chodov. "
    "Byt se nachází ve 3. podlaží oblíbené Rezidence Chodovec. Pronajímá se nezařízený, "
    "takže si jej můžete zařídit přesně podle vlastních představ. Kuchyňská linka je "
    "vybavena lednicí s mrazákem, indukční varnou deskou, troubou a myčkou nádobí. "
    "Poplatky včetně elektřiny činí 6 589 Kč měsíčně a jsou zúčtovatelné."
)
CHODOVEC_PART = (
    "Hledáte moderní bydlení v lokalitě s kompletní občanskou vybaveností? Pak by vás mohl "
    "oslovit tento prostorný byt o dispozici 2+kk a výměře 56 m² v ulici Nebřenická na "
    "Praze 4 – Chodov. Byt se nachází ve 3. nadzemním podlaží oblíbené Rezidence Chodovec. "
    "Pronajímá se částečně zařízený – k dispozici je postel a prostorná šatní skříň v "
    "ložnici, zbytek si můžete vybavit podle vlastního vkusu. Kuchyňská linka je vybavena "
    "lednicí s mrazákem, indukční varnou deskou, troubou a myčkou nádobí. "
    "Poplatky včetně elektřiny činí 6 589 Kč měsíčně a jsou zúčtovatelné."
)


def flat(listing_id: int, body: str, number: str, ruian: int, price: float,
         source: str = "sreality", first: int = 0, last: int = 40) -> Listing:
    return listing(listing_id, first=first, last=last, source=source, area_m2=56.0,
                   disposition="2+kk", price=price, description=body,
                   location={"obec_kod": 554782, "street_key": "nebrenicka",
                             "house_number": number, "ruian_adm_kod": ruian,
                             "granularity": "address_point", "granularity_rank": 100,
                             "is_address_grain": True})


def test_the_wide_reader_knows_the_lettings_own_sentence() -> None:
    assert furnished_state_wide(CHODOVEC_UNFURNISHED) == frozenset({"unfurnished"})
    assert furnished_state_wide(CHODOVEC_PART) == frozenset({"part"})
    assert furnished_state_wide("Pronajímá se plně zařízený") == frozenset({"furnished"})
    # The column answers only where the body has not.
    assert furnished_state_wide("žádná zmínka", "castecne") == frozenset({"part"})
    assert furnished_state_wide(CHODOVEC_PART, "ne") == frozenset({"part"})


def test_the_two_chodovec_units_are_parted_on_each_portal() -> None:
    for source in ("sreality", "bezrealitky"):
        a = flat(464492, CHODOVEC_UNFURNISHED, "2433/1", 87511592, 23900.0, source)
        b = flat(508658, CHODOVEC_PART, "2433/3", 87511614, 25500.0, source)
        assert names(a, b, S9) == []
        assert rental_colive_conflict(a, b, S10) is not None
        assert "rental_colive" in names(a, b, S10)


def test_two_postings_of_ONE_chodovec_unit_are_not_parted() -> None:
    a = flat(508658, CHODOVEC_PART, "2433/3", 87511614, 25500.0)
    b = flat(18585574, CHODOVEC_PART, "2433/3", 87511614, 25000.0, "bezrealitky")
    assert names(a, b, S10) == []


def test_the_fit_out_alone_is_not_a_fact_without_the_split() -> None:
    # One address point, one house number: W22's refusal stands where nothing corroborates.
    a = flat(1, CHODOVEC_UNFURNISHED, "2433/1", 87511592, 23900.0)
    b = flat(2, CHODOVEC_PART, "2433/1", 87511592, 23900.0)
    assert rental_colive_conflict(a, b, S10) is None
    assert rental_colive_conflict(a, b, variant(
        d43_rental_colive_furnishing_corroboration="any")) is not None


def test_a_re_post_that_was_never_live_beside_its_twin_is_one_flat() -> None:
    a = flat(1, CHODOVEC_UNFURNISHED, "2433/1", 87511592, 23900.0, first=0, last=10)
    b = flat(2, CHODOVEC_PART, "2433/3", 87511614, 23900.0, first=20, last=30)
    assert rental_colive_conflict(a, b, S10) is None


# --- E252: whose the kitchen and the lavatory are --------------------------------------------
VILLA_SHARED = (
    "Nabízíme k podnájmu samostatnou, neprůchozí kancelář o velikosti 35 m² v přízemí "
    "kompletně zrekonstruované vily na prestižní adrese Masarykova ulice v Liberci. "
    "Kancelář se nachází ve sdíleném administrativním prostoru s další firmou, což "
    "vytváří příjemné a profesionální pracovní prostředí. K dispozici jsou společné "
    "prostory – kuchyňka, koupelna a toaleta. Nájemné: 6 000 Kč/měsíc + služby."
)
VILLA_OWN = (
    "Nabízíme k podnájmu reprezentativní a světlou kancelář o výměře 35 m², situovanou v "
    "přízemí prvorepublikové vily v ulici Masarykova. Soukromí a komfort: Samostatná "
    "kancelář s vlastním sociálním zařízením (WC) zajišťuje maximální pohodlí pro vás i "
    "vaše klienty. Soukromá terasa: Přímý vstup na terasu nabízí příjemné místo pro "
    "odpočinek. Prostor se nachází ve společných obytných prostorech vily, což zajišťuje "
    "komorní a kultivované prostředí."
)


def office(listing_id: int, body: str, source: str = "ceskereality") -> Listing:
    return listing(listing_id, source=source, category_main="komercni", area_m2=35.0,
                   price=6000.0, description=body,
                   location={"obec_kod": 563889, "street_key": "masarykova",
                             "granularity": "street", "granularity_rank": 60})


def test_the_adjective_must_reach_a_sanitary_noun() -> None:
    assert facility_tenure(VILLA_SHARED) == frozenset({"shared"})
    # `ve společných obytných prostorech` is not a lavatory, and the body that says it states
    # the OPPOSITE about its facilities.
    assert facility_tenure(VILLA_OWN) == frozenset({"own"})
    assert facility_tenure("byt ve společném domě s výtahem") == frozenset()


def test_the_two_villa_offices_are_parted() -> None:
    a, b = office(491405, VILLA_SHARED), office(18322128, VILLA_OWN)
    assert names(a, b, S9) == []
    assert "rental_colive" in names(a, b, S10)
    for source in ("idnes",):
        assert rental_colive_conflict(office(251785, VILLA_SHARED, source),
                                      office(18605494, VILLA_OWN, source), S10) is not None


def test_one_office_re_posted_is_one_office() -> None:
    assert names(office(1, VILLA_SHARED), office(2, VILLA_SHARED, "idnes"), S10) == []


# --- E254: a rent quoted per square metre ----------------------------------------------------
def surgery(listing_id: int, price: float, source: str) -> Listing:
    return listing(listing_id, source=source, category_main="komercni", area_m2=96.0,
                   price=price, description="Ordinace 96 m² v poliklinice, 2. NP.",
                   location={"obec_kod": 563889, "street_key": "tanvaldska",
                             "granularity": "street", "granularity_rank": 60})


def test_the_same_rent_in_two_units_is_not_a_price_gap() -> None:
    a, b = surgery(12299572, 24000.0, "idnes"), surgery(12345299, 250.0, "realitymix")
    assert "price" in names(a, b, S9)
    assert names(a, b, S10) == []


def test_a_genuine_gap_cannot_wear_the_per_square_metre_reading() -> None:
    a, b = surgery(1, 24000.0, "idnes"), surgery(2, 300.0, "realitymix")
    assert "price" in names(a, b, S10)


def test_a_sale_price_is_never_read_per_square_metre() -> None:
    a = surgery(1, 24000.0, "idnes")
    b = surgery(2, 250.0, "realitymix")
    a.category_type = b.category_type = "prodej"
    assert "price" in names(a, b, S10)


# --- E253: the cell that sheds what blocks a cut merge edge ----------------------------------
def test_a_cell_sheds_the_blocker_when_that_keeps_more_evidence_inside() -> None:
    # `blocker` is the third advert: it carries a fact against `far`, and nothing else does.
    members = [1, 2, 3, 4, 9]
    edges = [Edge(1, 4, 0.90, False), Edge(1, 2, 0.85, False), Edge(1, 3, 0.85, False),
             Edge(1, 9, 0.80, False), Edge(2, 9, 0.80, False), Edge(3, 9, 0.80, False)]

    def invariants(group):
        group = list(group)
        return "blocked" if 4 in group and 9 in group else None

    cells = partition(members, edges, invariants, keep_factless=True, rejoin_cells=True)
    assert sorted(cells) == [[1, 2, 3, 4], [9]]
    shed = partition(members, edges, invariants, keep_factless=True, rejoin_cells=True,
                     shed_blockers=lambda group: [(4, 9)] if 4 in group and 9 in group else [],
                     shed_max=1, outer_rounds=3)
    assert sorted(shed) == [[1, 2, 3, 9], [4]]


def test_the_shed_refuses_the_move_that_loses_evidence() -> None:
    members = [1, 2, 3, 4, 9]
    edges = [Edge(1, 4, 0.99, True), Edge(2, 4, 0.99, True), Edge(3, 4, 0.99, True),
             Edge(1, 9, 0.2, False)]

    def invariants(group):
        group = list(group)
        return "blocked" if 4 in group and 9 in group else None

    shed = partition(members, edges, invariants, keep_factless=True, rejoin_cells=True,
                     shed_blockers=lambda group: [(4, 9)] if 4 in group and 9 in group else [],
                     shed_max=1, outer_rounds=3)
    assert [9] in shed


def test_neither_end_of_the_cut_edge_is_ever_shed() -> None:
    members = [1, 2]
    edges = [Edge(1, 2, 0.99, True)]
    shed = partition(members, edges, lambda group: "blocked" if len(group) > 1 else None,
                     keep_factless=True, rejoin_cells=True,
                     shed_blockers=lambda group: [(1, 2)] if len(group) > 1 else [],
                     shed_max=1, outer_rounds=3)
    assert sorted(shed) == [[1], [2]]
