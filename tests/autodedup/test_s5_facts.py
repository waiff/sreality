"""S5's readings (E200-E204), each built from the adverts that named it.

Every case here is a real pair: the Ústí `Purkyňova 1093/7` rental project (cohort 7 CL10850),
the two `ul. Stará` 1+kk flats of one Ústí broker (CL7958), the Hrobčice/Razice house sold as
two land packages (CL193011), the Bílina `ul. Bezejmenná` 2+1 rentals (CL319881) and the
HK-Zámeček parcel sold whole against one third of it (cohort 5 CL183452). Where a body is
quoted it is quoted from the advert, with the sentences the reader has to see kept verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

from autodedup.body_align import overlap_ratio
from autodedup.dataset import Listing
from autodedup.indistinguishable import GATE, distinguishing_facts
from autodedup.settings import Settings
from autodedup.text_facts import (
    ground_or_upper,
    parcel_divisions,
    printed_floors,
    printed_floors_by_form,
    prose_plot_areas,
    same_form_floor_gap,
    stated_charges,
    subject_floors,
)

SETTINGS = Path(__file__).resolve().parents[2] / "autodedup/settings"
S4 = Settings.from_json(SETTINGS / "w19.json")
S5 = Settings.from_json(SETTINGS / "w20.json")


def variant(**kwargs: object) -> Settings:
    raw = json.loads((SETTINGS / "w20.json").read_text(encoding="utf-8"))
    raw.update(kwargs)
    return Settings.from_dict(raw)


def stamp(day: int) -> str:
    return f"2026-0{1 + day // 28}-{1 + day % 28:02d}T00:00:00+00:00"


def listing(listing_id: int, first: int = 0, last: int = 40, **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "source": "bazos",
        "category_main": "byt",
        "category_type": "pronajem",
        "first_seen_at": stamp(first),
        "last_seen_at": stamp(last),
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def names(a: Listing, b: Listing, cfg: Settings, mode: str = GATE) -> list[str]:
    return [fact.name for fact in distinguishing_facts(a, b, None, cfg, mode)]


# --- E200: two of one seller's order numbers, on two bodies of one template ----------------
# bazos 264492 and 415240, both live from 2026-06-29 to 2026-08-07 at 10,500 Kč for a 1+1 of
# 38 m² at Purkyňova 1093/7. The shared sentences are the project's template; the one that
# differs is the one that says what the flat IS.
PURKYNOVA = (
    "Ev.č. {code} Nově zrekonstruované byty k pronájmu. Bez kauce a bez provize Pro slušné a "
    "spolehlivé zájemce, kteří vyhledávají moderní bydlení v novém projektu v Ústí nad Labem "
    "představujeme kompletně zrekonstruovaný byt 1+1 o výměře 38 m2, který se nachází v "
    "cihlovém domě na ul. Purkyňova 1093/7. Dispozici bytu tvoří vstupní chodba, koupelna se "
    "sprchovým koutem. {rooms} Jednotka je díky své orientaci a velkým oknům dostatečně "
    "prosvětlena a působí vzdušným dojmem. Společné prostory domu nabízí pro rezidenty "
    "společné venkovní terasy a balkony. Vstup do budovy je hlídán kamerovým systémem. "
    "Samotný dům se nachází 10 min pěší chůze k vlakovému nádraží s pravidelnými spoji do "
    "Prahy či Teplic. Parkovaní je možné bezplatně před domem. K nastěhování ihned. "
    "Nájemné 10.500,- Kč Teplo 1.500,- Kč Vodné 500,- Kč/osoba Úklid 200,- Kč"
)
UNIT_00841 = PURKYNOVA.format(
    code="00841", rooms="Kuchyně vybavena kuchyňskou linkou se spotřebiči a jeden neprůchozí "
                        "pokoj.")
UNIT_01071 = PURKYNOVA.format(
    code="01071", rooms="Kuchyně je vybavena kuchyňskou linkou se spotřebiči. Celý byt je "
                        "zakončen prostorným pokojem. Společné prostory nabízí kočárkárnu.")


def purkynova(listing_id: int, body: str, first: int = 0, last: int = 40) -> Listing:
    return listing(listing_id, first=first, last=last, disposition="1+1", area_m2=38.0,
                   price=10_500.0, description=body)


CODES_ON = variant(d43_agency_code_conflict=True)


def test_two_order_codes_on_two_bodies_of_one_template_are_a_fact() -> None:
    """CL10850, and the reason the dial exists. S4 merges the two flats; g7 kept them apart.
    The dial ships OFF — see `test_the_order_code_dial_ships_off_because_it_is_refuted`."""
    a = purkynova(264492, UNIT_00841)
    b = purkynova(415240, UNIT_01071)
    assert "agency_code" not in names(a, b, S4)
    assert "agency_code" in names(a, b, CODES_ON)


def test_the_same_advert_re_posted_under_a_new_code_is_not_a_fact() -> None:
    """The bare code conflict is REFUTED: 263 certain duplicates of the seven cohorts carry
    disjoint codes while live together on one portal, because a trader's re-post is renumbered.
    What they have in common is that the two bodies are the same text (bazos 11565029 x
    11565031, `Ev.č. 00037` against `00037-1`, 51 days together at one price)."""
    a = purkynova(11565029, UNIT_01071.replace("01071", "00037"))
    b = purkynova(11565031, UNIT_01071.replace("01071", "00037-1"))
    assert overlap_ratio(a.description, b.description) == 1.0
    assert "agency_code" not in names(a, b, CODES_ON)


def test_two_codes_on_two_unrelated_bodies_claim_nothing() -> None:
    """Below the floor the two bodies are not one seller's template, so the codes are not one
    seller's numbering either — bazos 193698 x 205378 are a certain duplicate whose two bodies
    share 18 % of their text because one side is a stub."""
    a = purkynova(193698, UNIT_00841)
    b = purkynova(205378, "Ev.č. 932343 Prodej. Cena k jednání. Více informací u makléře. "
                          "Nemovitost je volná ihned a lze ji obratem prohlédnout po dohodě "
                          "telefonem s naším zástupcem v regionu, děkujeme za pochopení.")
    assert "agency_code" not in names(a, b, CODES_ON)


def test_two_codes_that_never_lived_together_are_a_renumbered_re_post() -> None:
    """bazos 415240 -> 17079054 -> 18290670: one flat, one code, three postings that never
    overlap. The same shape with two codes is a renumbering, and the operator's own ruling
    says a re-post may be renumbered."""
    a = purkynova(264492, UNIT_00841, first=0, last=10)
    b = purkynova(415240, UNIT_01071, first=20, last=40)
    assert "agency_code" not in names(a, b, CODES_ON)


def test_two_codes_on_two_portals_are_two_portals_numbering_one_advert() -> None:
    a = purkynova(264492, UNIT_00841)
    b = purkynova(415240, UNIT_01071, first=0, last=40)
    b.source = "sreality"
    assert "agency_code" not in names(a, b, CODES_ON)


def test_a_catalogue_of_codes_is_not_a_conflict() -> None:
    """A body that prints the seller's whole order book states no single order number."""
    a = purkynova(264492, UNIT_00841 + " Ev.č. 00842 Ev.č. 00843 Ev.č. 00844 Ev.č. 00845")
    b = purkynova(415240, UNIT_01071)
    assert "agency_code" not in names(a, b, CODES_ON)


def test_the_order_code_dial_ships_off_because_it_is_refuted() -> None:
    """The band does not separate two units of one project from ONE object re-pitched under a
    second advert number. Hand-read on the seven cohorts: `rodinný dům` against `chatu` for one
    34 m² Čeperka cottage at 1,200,000 (codes N117852 / N117854, bodies 90.3 % one text); one
    145 m² Plzeň shop whose second posting adds the street-works note (944918 / 945479, 85.9 %);
    one Pardubice villa offered `jako sídlo firmy` and `pro zdravotnické zařízení s 6
    ordinacemi` (656917 / 657002, 89–90 %). CL10850's two flats sit at 86.8 and 87.6 % — inside
    the same band. The dial stays, measured and OFF, for the operator to rule on."""
    a = purkynova(264492, UNIT_00841)
    b = purkynova(415240, UNIT_01071)
    assert "agency_code" not in names(a, b, S5)
    assert S5.d43_agency_code_conflict is False


# --- E201: the storey written as an ordinal WORD -------------------------------------------
# sreality 7958 and 14859, one broker, ul. Stará, Ústí nad Labem, both 1+kk of 34 m².
STARA_GROUND = (
    "Nabízíme Vám k pronájmu nezařízený byt o velikosti 1+kk (34 m2), který se nachází v "
    "přízemí cihlového domu v Ústí nad Labem ulice Stará. Koupelna je se sprchovým koutem, "
    "skříňkou s umyvadlem a je obložena keramickým obkladem, toaleta je samostatně. K uvedené "
    "ceně nájmu budou účtovány zálohy na energie ve výši 2 935,-Kč/měsíc, elektřinu hradí "
    "nájemce samostatně přímo dodavateli + provize RK ve výši 8 000,- Kč. Je požadována "
    "vratná kauce ve výši 3 nájmů tj. 15 000,- Kč."
)
STARA_THIRD = (
    "Nabízíme Vám k pronájmu nezařízený byt o velikosti 1+kk (34 m2) s prostorným balkonem, "
    "který se nachází ve třetím patře cihlového domu v Ústí nad Labem ulice Stará. Koupelna "
    "je s vanou, skříňkou s umyvadlem a je obložena keramickým obkladem, toaleta je "
    "samostatně. K uvedené ceně nájmu budou účtovány zálohy na energie ve výši 3 015,-Kč/"
    "měsíc, elektřinu hradí nájemce sám dodavateli + provize RK 8000,- Kč. Je požadována "
    "vratná kauce ve výši 3 nájmů tj. 19 000,- Kč."
)


def test_the_worded_storey_is_read_on_the_same_scale_as_the_numeral() -> None:
    assert printed_floors(STARA_THIRD) == frozenset()
    assert printed_floors(STARA_THIRD, True) == frozenset({4})
    assert subject_floors(STARA_THIRD, True) == frozenset({4})
    assert printed_floors("byt ve 3. patře", True) == printed_floors(
        "byt ve třetím patře", True)


def test_ground_against_a_worded_upper_storey_is_a_fact() -> None:
    """CL7958. Neither advert fills the floor column on both sides and the two are sequential
    by seventeen hours, so the price is no help either — the storey is in the prose."""
    a = listing(7958, source="sreality", disposition="1+kk", area_m2=34.0, price=5_125.0,
                description=STARA_GROUND, first=0, last=2)
    b = listing(14859, source="sreality", disposition="1+kk", area_m2=34.0, price=6_355.0,
                description=STARA_THIRD, first=2, last=38)
    assert ground_or_upper(STARA_GROUND, True) == frozenset({"ground"})
    assert ground_or_upper(STARA_THIRD, True) == frozenset({"upper"})
    assert "storey_word" not in names(a, b, S4)
    assert "storey_word" in names(a, b, S5)
    assert "storey_word" not in names(a, b, variant(d43_prose_floor_words=False))


# One Bílina agency re-writes its own advert for č.p. 707, Sídliště U Nového nádraží — same
# 36 m², same 8,556 Kč rent, same broker, same portal — from the worded `patro` to the numbered
# `NP`. On the NP scale that is a one-storey gap, and it is nothing but the noun.
BILINA_PATRO = (
    "Nabízíme do pronájmu byt 1 + 1, situovaný na adrese Ulice Sídliště U Nového nádraží "
    "Č. p. 707, v části obce Teplické Předměstí, obci Bílina, okres Teplice. Tento panelový "
    "byt o velikosti 1 + 1 se nachází ve velmi dobrém stavu a je umístěn v šestém patře "
    "osmipodlažního objektu, což zajišťuje příjemný výhled a dostatek přirozeného světla. "
    "V objektu se nachází výtah. Nájemné včetně služeb + elektřina + plyn. Pro jednu osobu "
    "+ 10 000,- provize + 10 000,- kauce."
)
BILINA_NP = (
    "Nabízíme k pronájmu prostorný byt 1 + 1 v osobním vlastnictví, situovaný na klidné "
    "adrese v ulici Sídliště U Nového nádraží, č. p. 707, v části obce Teplické Předměstí, "
    "města Bílina, okres Teplice. Byt se nachází ve 6. nadzemním podlaží osmi podlažního "
    "panelového objektu, který je ve velmi dobrém stavu. Celková užitná plocha bytu činí "
    "36 m². Nájemné včetně služeb + elektřina + plyn. Pro jednu osobu + 10 000,- provize "
    "+ 10 000,- kauce."
)


def test_patro_against_np_is_a_vocabulary_and_not_a_storey() -> None:
    """ceskereality 437664 x 18907864 and idnes 449020 x 18906827. `_same_feed` cannot see
    this one: it IS one feed, writing its own storey two ways."""
    assert printed_floors(BILINA_PATRO, True) == frozenset({7})
    assert printed_floors(BILINA_NP, True) == frozenset({6})
    assert same_form_floor_gap(printed_floors_by_form(BILINA_PATRO, True),
                               printed_floors_by_form(BILINA_NP, True)) is None
    a = listing(437664, source="ceskereality", disposition="1+1", area_m2=36.0, floor=5,
                price=8_556.0, description=BILINA_PATRO, first=0, last=20)
    b = listing(18907864, source="ceskereality", disposition="1+1", area_m2=36.0, floor=5,
                price=8_556.0, description=BILINA_NP, first=20, last=40)
    assert "prose_floor" not in names(a, b, S5)
    assert "subject_floor" not in names(a, b, S5)


def test_two_storeys_apart_across_the_two_nouns_is_still_the_nouns() -> None:
    """sreality 464422 `ve druhém patře` against bezrealitky 509662 `v prvním podlaží`, one
    45 m² Prague 1+kk at 20,553 Kč on both: the patro adds one and the two portals' own
    ground-floor camps add the other. Neither of those is a storey."""
    assert same_form_floor_gap(
        printed_floors_by_form("byt ve druhém patře", True),
        printed_floors_by_form("bydlení je situováno v prvním podlaží", True)) is None


def test_one_noun_written_twice_is_still_a_storey() -> None:
    """Mariánské Lázně, Kubelíkova: two 1+kk of 21 m² in one house, `v pátém patře` against
    `ve druhém patře`. One noun, three storeys, a fact."""
    assert same_form_floor_gap(
        printed_floors_by_form("Prodej bytu 1+kk v pátém patře", True),
        printed_floors_by_form("Prodej bytu 1+kk ve druhém patře", True)) == 3


def test_an_ordinal_word_away_from_a_storey_noun_is_not_a_storey() -> None:
    assert printed_floors("na druhé straně ulice je park, třetí dům od rohu", True) == (
        frozenset())
    assert ground_or_upper("výhled na druhé nádvoří", True) == frozenset()


# remax 171943 and 17601990: one Abertamy 8+1 of 220 m² on a 507 m² plot, re-listed by one
# broker at 7,390,000 and then 6,490,000, whose body enumerates its own storeys — and which
# storey word heads the enumeration changes between the two writings.
ABERTAMY_PATRO = (
    "Patrový rodinný dům 8+1 s velkou terasou v srdci Krušných hor nabízí ideální kombinaci "
    "prostoru, soukromí a krásné horské přírody. Dům má zastavěnou plochu 142 m² a užitnou "
    "plochu 220 m². V prvním patře se nachází dvě samostatné místnosti, další pokoj s vlastní "
    "koupelnou, samostatná toaleta s komorou a vstup na prostornou terasu. Druhé patro tvoří "
    "světlý obývací pokoj, kuchyň s jídelnou, ložnice, další pokoj a koupelna s vanou. "
    "Součástí domu je také sklep se třemi místnostmi a garáž."
)
ABERTAMY_PRIZEMI = (
    "Nabízíme k prodeji prostorný rodinný dům o dispozici 8+1 a užitné ploše 220 m², který se "
    "nachází v oblíbených Abertamech, přímo v srdci Krušných hor. Dům stojí na pozemku se "
    "zastavěnou plochou 142 m². V přízemí najdete dvě samostatné místnosti, pokoj s vlastní "
    "koupelnou, samostatné WC, komoru a přímý vstup na prostornou terasu. Ve druhém nadzemním "
    "podlaží se nachází obývací pokoj s krbem, kuchyně s jídelnou, ložnice, další pokoj a "
    "koupelna s vanou a WC. K domu náleží také sklep se třemi místnostmi a garáž."
)


def test_a_house_is_sold_with_every_storey_it_has() -> None:
    """The worded readings are not read for a whole building: the storey heads a list of ROOMS,
    and the offer is the house. Both spellings describe the same two storeys of one house."""
    a = listing(171943, source="remax", category_main="dum", category_type="prodej",
                disposition=None, area_m2=220.0, price=7_390_000.0,
                description=ABERTAMY_PATRO, first=0, last=20)
    b = listing(17601990, source="remax", category_main="dum", category_type="prodej",
                disposition=None, area_m2=220.0, price=6_490_000.0,
                description=ABERTAMY_PRIZEMI, first=20, last=40)
    assert ground_or_upper(ABERTAMY_PATRO, True) == frozenset({"upper"})
    assert ground_or_upper(ABERTAMY_PRIZEMI, True) == frozenset({"ground"})
    assert "storey_word" not in names(a, b, S5)


def test_a_flat_is_on_one_storey_and_the_guard_does_not_reach_it() -> None:
    """The same shape in a `byt` still reads: one Mariánské Lázně office is `v přízemí` and a
    second of the same 25 m² at the same 5,000 Kč is `ve druhém patře`, 60 days together."""
    a = listing(1, category_main="komercni", area_m2=25.0, price=5_000.0,
                description="Nabízíme k pronájmu nebytový prostor o výměře 25 m², který se "
                            "nachází v přízemí reprezentativního domu v centru.")
    b = listing(2, category_main="komercni", area_m2=25.0, price=5_000.0,
                description="Nabízíme k pronájmu nebytový prostor o výměře 25 m², který se "
                            "nachází ve druhém patře reprezentativního domu v centru.")
    assert "storey_word" in names(a, b, S5)


def test_the_same_worded_storey_on_both_sides_is_no_fact() -> None:
    a = listing(1, source="sreality", area_m2=34.0, description=STARA_THIRD)
    b = listing(2, source="sreality", area_m2=34.0,
                description=STARA_THIRD.replace("s prostorným balkonem, ", ""))
    assert "storey_word" not in names(a, b, S5)
    assert "prose_floor" not in names(a, b, S5)


# --- E203: a service advance or a deposit beside a co-live price gap -----------------------
# bazos 319881 / 523747 against 497383, ul. Bezejmenná, Bílina, 8.0 and 12.8 days together.
BEZEJMENNA_A = (
    "Pronájem bytu 2+1 s balkónem v Bílině, ul. Bezejmenná. Byt je nově po rekonstrukci, nové "
    "štuky a výmalba, kuchyňská linka, plovoucí podlaha Nájemné je 11200 Kč + zálohy na "
    "služby 3800 Kč ( elektřina,voda, topení). V případě zájmu je potřeba složit jistinu ve "
    "výši nájmu a nájemné na daný měsíc, a provizi."
)
BEZEJMENNA_B = (
    "Nabízím k dlouhodobému pronájmu světlý, zrekonstruovaný byt 2+1 v klidné části Bíliny. "
    "Byt je čistý, ihned připravený k nastěhování a nabízí hezký výhled do okolní krajiny. "
    "Zděná koupelna disponuje moderním sprchovým koutem a umyvadlovou skříňkou. Elektřina se "
    "přepisuje přímo na nájemníka. Nájemné: 9.000 Kč / měsíc Zálohy na služby: 4.500 Kč / "
    "měsíc (voda, teplo, spol. prostory) Vratná kauce: 20.000 Kč Provize RK: 6.000 Kč"
)


def test_the_body_states_what_the_tenant_pays_beside_the_rent() -> None:
    assert stated_charges(BEZEJMENNA_A) == {"services": frozenset({3800.0})}
    assert stated_charges(BEZEJMENNA_B) == {
        "deposit": frozenset({20000.0}), "services": frozenset({4500.0})}
    # `kauce ve výši 3 nájmů tj. 15 000,- Kč` states the multiplier first: 3 is not a charge.
    assert stated_charges(STARA_GROUND) == {
        "deposit": frozenset({15000.0}), "services": frozenset({2935.0})}


def test_two_service_advances_at_one_moment_are_two_tenancies() -> None:
    """CL319881. The price gap alone stays refused (D49) — what fires is the second number."""
    a = listing(319881, price=11_200.0, disposition="2+1", description=BEZEJMENNA_A,
                first=0, last=27)
    b = listing(497383, price=8_500.0, disposition="2+1", description=BEZEJMENNA_B,
                first=19, last=50)
    assert "charge" not in names(a, b, S4)
    assert "price" not in names(a, b, S5)
    assert "charge" in names(a, b, S5)


def test_one_advert_re_posted_quotes_one_advance() -> None:
    """bazos 319881 -> 523747 is the same advert re-posted: same rent, same 3,800 advance."""
    a = listing(319881, price=11_200.0, description=BEZEJMENNA_A, first=0, last=27)
    b = listing(523747, price=11_200.0, description=BEZEJMENNA_A, first=27, last=39)
    assert "charge" not in names(a, b, S5)


def test_the_advance_is_read_only_where_the_price_also_contradicts() -> None:
    """D49's refusal kept from the other side: two live adverts at ONE price are one advert a
    portal re-read, and a differing advance there is a body edit, not a second tenancy."""
    a = listing(1, price=11_200.0, description=BEZEJMENNA_A, first=0, last=27)
    b = listing(2, price=11_200.0, description=BEZEJMENNA_B, first=19, last=50)
    assert "charge" not in names(a, b, S5)
    assert "charge" in names(a, b, variant(d43_colive_charge_requires_price_gap=False))


def test_two_advances_that_never_lived_together_are_a_price_move() -> None:
    a = listing(1, price=11_200.0, description=BEZEJMENNA_A, first=0, last=10)
    b = listing(2, price=8_500.0, description=BEZEJMENNA_B, first=20, last=50)
    assert "charge" not in names(a, b, S5)


# --- E202: the plot the body states, where no portal fills the column -----------------------
# bazos 193011 and 193922, posted 25 minutes apart, 36.9 days together, Hrobčice k.ú. Razice.
RAZICE = (
    "Nabízíme k prodeji prostorný rodinný dům o zastavěné ploše 326 m², který se nachází v "
    "obci Hrobčice, katastrální území Razice. Součástí prodeje je {land}. Pozemek je kompletně "
    "zpevněný asfaltem, vybaven kamerovým systémem. K domu náleží také zahrada s terasou o "
    "velikosti 87 m², která poskytuje příjemné místo pro odpočinek. V přízemí domu se nachází "
    "bytová jednotka o dispozici 3+1. V patře je umístěn prostorný byt 4+kk o ploše 120 m²."
)
RAZICE_AREAL = RAZICE.format(land="také rozsáhlý přilehlý areál o rozloze 2 830 m²")
RAZICE_POZEMEK = RAZICE.format(land="i pozemek o rozloze 1 483 m²")


def test_the_garden_is_not_the_plot() -> None:
    """Both bodies print the same 87 m² garden terrace; a set reader would meet on it."""
    assert prose_plot_areas(RAZICE_AREAL) == frozenset({2830.0})
    assert prose_plot_areas(RAZICE_POZEMEK) == frozenset({1483.0})


def test_two_land_packages_of_one_house_priced_apart_are_two_offers() -> None:
    """CL193011. Neither bazos row fills a plot column, so `plot_area` reads nothing at all."""
    a = listing(193011, category_main="dum", category_type="prodej", disposition="3+1",
                area_m2=326.0, price=7_999_000.0, description=RAZICE_AREAL, first=0, last=37)
    b = listing(193922, category_main="dum", category_type="prodej", disposition="3+1",
                area_m2=326.0, price=6_190_000.0, description=RAZICE_POZEMEK, first=0, last=37)
    assert "plot_area" not in names(a, b, S5)
    assert "plot_prose" not in names(a, b, S4)
    assert "plot_prose" in names(a, b, S5)


def test_the_re_post_of_one_package_keeps_its_own_plot() -> None:
    """bazos 18832235 re-posts 193922 two months later: the same 1,483 m² at the same price."""
    a = listing(193922, category_main="dum", category_type="prodej", area_m2=326.0,
                price=6_190_000.0, description=RAZICE_POZEMEK, first=0, last=37)
    b = listing(18832235, category_main="dum", category_type="prodej", area_m2=326.0,
                price=6_190_000.0, description=RAZICE_POZEMEK, first=60, last=71)
    assert "plot_prose" not in names(a, b, S5)


def test_one_plot_printed_to_two_precisions_is_one_plot() -> None:
    """g7's own cluster 68071 prints the same areál as 2,826 and 2,830 m² — 0.14 % apart."""
    a = listing(1, category_main="dum", category_type="prodej", area_m2=326.0,
                price=7_999_000.0, description=RAZICE_AREAL, first=0, last=37)
    b = listing(2, category_main="dum", category_type="prodej", area_m2=326.0,
                price=6_190_000.0,
                description=RAZICE_AREAL.replace("2 830", "2 826"), first=0, last=37)
    assert "plot_prose" not in names(a, b, S5)


def test_the_stated_plot_is_read_only_beside_a_price_contradiction() -> None:
    a = listing(1, category_main="dum", category_type="prodej", area_m2=326.0,
                price=7_999_000.0, description=RAZICE_AREAL, first=0, last=37)
    b = listing(2, category_main="dum", category_type="prodej", area_m2=326.0,
                price=7_999_000.0, description=RAZICE_POZEMEK, first=0, last=37)
    assert "plot_prose" not in names(a, b, S5)


# --- E204: the part and the whole ------------------------------------------------------------
# ceskereality 18583900 against idnes 183452, ul. Zámeček, Nový Hradec Králové. BOTH rows carry
# the portal's 2,195 m² in the area column — the part's own 732 m² exists only in the prose.
ZAMECEK_WHOLE = (
    "Nabízíme k prodeji mimořádně atraktivní pozemek určený k výstavbě luxusní nemovitosti "
    "nebo domu o třech bytových jednotkách, o celkové výměře 2.195 m². Pozemek je situovaný v "
    "jedné z nejžádanějších částí Hradce Králové – v ulici Zámeček na Novém Hradci Králové. "
    "Parcela zaujme nejen svou prestižní polohou, ale také nádhernými panoramatickými výhledy "
    "na Roudničku a okolní vzrostlou zeleň. Celá parcela je oplocena."
)
ZAMECEK_PART = (
    "Nabízíme k prodeji atraktivní stavební pozemek o výměře 732 m², určený pro realizaci "
    "jedné části moderního trojdomu. Nachází se v jedné z nejžádanějších lokalit Hradce "
    "Králové – v ulici Zámeček na Novém Hradci Králové. Pozemek vznikne rozdělením parcely o "
    "celkové výměře 2 195 m² na tři části. Záměrem je výstavba moderního trojdomu, přičemž "
    "každá část nabídne samostatné bydlení, vlastní zahradu a prostor pro terasu."
)


def test_the_part_names_the_whole_it_will_be_cut_from() -> None:
    assert parcel_divisions(ZAMECEK_PART) == frozenset({2195.0})
    assert parcel_divisions(ZAMECEK_WHOLE) == frozenset()


def test_one_third_of_a_parcel_is_not_the_parcel() -> None:
    """CL183452 of cohort 5. Read as SETS the two bodies share 2 195 and never contradict."""
    a = listing(183452, source="idnes", category_main="pozemek", category_type="prodej",
                disposition=None, area_m2=2195.0, price=21_947_805.0,
                description=ZAMECEK_WHOLE)
    b = listing(18583900, source="ceskereality", category_main="pozemek",
                category_type="prodej", disposition=None, area_m2=2195.0,
                price=21_947_805.0, description=ZAMECEK_PART)
    assert "part_whole" not in names(a, b, S4)
    assert "part_whole" in names(a, b, S5)
    assert "part_whole" not in names(a, b, variant(d43_part_whole=False))


def test_two_parts_of_one_division_state_it_both_ways_and_nothing_tells_them_apart() -> None:
    """D43 exactly: three 732 m² thirds of one trojdům print one body, and no stated fact
    separates them. The reader abstains rather than guessing which third is which."""
    a = listing(18583900, source="ceskereality", category_main="pozemek",
                category_type="prodej", area_m2=2195.0, description=ZAMECEK_PART)
    b = listing(18601032, source="idnes", category_main="pozemek", category_type="prodej",
                area_m2=2195.0, description=ZAMECEK_PART)
    assert "part_whole" not in names(a, b, S5)


def test_a_part_the_same_size_as_the_whole_is_not_a_part() -> None:
    a = listing(1, source="idnes", category_main="pozemek", category_type="prodej",
                area_m2=2195.0, description=ZAMECEK_WHOLE)
    b = listing(2, source="ceskereality", category_main="pozemek", category_type="prodej",
                area_m2=2195.0,
                description=ZAMECEK_PART.replace("o výměře 732 m²", "o výměře 2 190 m²"))
    assert "part_whole" not in names(a, b, S5)


# --- the contract every earlier generation depends on ---------------------------------------


S5_DIALS: dict[str, object] = {
    "d43_agency_code_conflict": False,
    "d43_prose_floor_words": False,
    "d43_colive_charge_conflict": False,
    "d43_prose_plot_conflict": False,
    "d43_part_whole": False,
}


def test_every_s5_dial_is_inert_by_default() -> None:
    live = Settings()
    for name, off in S5_DIALS.items():
        assert getattr(live, name) == off, name


def test_no_shipped_generation_before_w20_names_an_s5_dial() -> None:
    for arm in ("w13", "w14", "w15", "w17", "w18", "w19"):
        raw = json.loads((SETTINGS / f"{arm}.json").read_text(encoding="utf-8"))
        assert not set(raw) & set(S5_DIALS), (arm, sorted(set(raw) & set(S5_DIALS)))
        shipped = Settings.from_json(SETTINGS / f"{arm}.json")
        for name, off in S5_DIALS.items():
            assert getattr(shipped, name) == off, (arm, name)


def test_w20_differs_from_w19_only_in_the_dials_the_wave_names() -> None:
    """Four of the five ship ON; E200's ships OFF, refuted and measured, so w20 moves four."""
    w19 = Settings.from_json(SETTINGS / "w19.json").to_dict()
    w20 = Settings.from_json(SETTINGS / "w20.json").to_dict()
    moved = {key for key in w20 if w19.get(key) != w20[key]}
    assert moved == set(S5_DIALS) - {"d43_agency_code_conflict"}, sorted(moved)
