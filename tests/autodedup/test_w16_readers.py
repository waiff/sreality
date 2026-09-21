"""E150-E156: the W16 readers, each against the adverts that produced it.

Every positive below is real body text from a hand-confirmed FUSED group (the listing ids are
named); every negative is a real body from a duplicate the operator, the gold judge or a
structural rule confirmed is ONE unit, and is the shape the same reader was wrong on first.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from autodedup.body_align import aligned_difference, rounding_equal, rounding_equal_values
from autodedup.dataset import Listing, Location
from autodedup.indistinguishable import (
    CLUSTER,
    distinguishing_facts,
    printed_area_conflict,
    prose_obec_conflict,
    prose_street_conflict,
)
from autodedup.repartition import Edge, partition
from autodedup.settings import Settings
from autodedup.text_facts import parcel_numbers, printed_areas, prose_streets, streets_agree

W16 = Settings.from_json("autodedup/settings/w16_l.json")

# ---------------------------------------------------------------- real bodies, by listing id
# 17421 / 27453 and 22476 / 27455: two flats of the Loudova V. etapa, ONE template.
LOUDOVA_B122 = (
    "Z pověření developera Vám realitní kancelář v exkluzivním zastoupení nabízí již V. etapu "
    "úspěšného projektu na ulici Loudova v Olomouci, jež navazuje prodejem – budovy B. "
    "Projekt umožňuje svým rezidentům dostupné bydlení v příjemné a klidné lokalitě města "
    "Olomouce. Byt B1.2.2 o dispozici 2+kk má podlahovou plochu 53,5 m² a terasa o velikosti "
    "11,4 m² se nachází ve 2.NP bytového domu s nízkým počtem bytových jednotek. Velkou "
    "předností lokality je dostatek zeleně, odpočinková zóna kolem přírodního rybníku."
)
LOUDOVA_B123 = LOUDOVA_B122.replace("B1.2.2", "B1.2.3")
LOUDOVA_B222 = LOUDOVA_B122.replace("B1.2.2", "B2.2.2")

# 193359 / 242642: plots 8 and 7 of the Štarnov parcelling, on idnes.
STARNOV_8 = (
    "Prodej stavebního pozemku 1081 m² / Štarnov Ve výhradním zastoupení majitele nabízíme k "
    "prodeji stavební pozemek s označením 8 o výměře 1081 m², který vznikne v rámci nového "
    "území v obci Štarnov. Pozemek je určen na výstavbu samostatně stojícího rodinného domu o "
    "maximálně dvou nadzemních podlažích. Pozemek bude kompletně zasíťován - elektřina, "
    "vodovod a kanalizace, optický kabel, dále bude k pozemku zbudována nová příjezdová "
    "komunikace včetně veškeré infrastruktury, která přejde do vlastnictví obce."
)
STARNOV_7 = STARNOV_8.replace("1081 m²", "1080 m²").replace("označením 8", "označením 7")

# 103812 / 103827: Rokytná Resort on bazos, where the stored column is the TERRACE for both.
ROKYTNA_761 = (
    "Rokytná Resort nabízí k prodeji apartmán Tvarožník s podlahovou plochou 76,1 m² a "
    "terasou o velikosti 10 m². Apartmán je v posledním patře s výhledem do údolí. "
    "Součástí prodeje je kompletní vybavení, parkovací stání a podíl na společných prostorách "
    "resortu, který nabízí wellness, restauraci a celoroční správu."
)
ROKYTNA_778 = ROKYTNA_761.replace("Tvarožník", "Dřevařská").replace("76,1 m²", "77,8 m²")

# 5239 / 5479: two 2+kk of one Ostravská novostavba, on sreality, stored 58 m² for both.
OSTRAVSKA_5890 = (
    "Nabízíme k pronájmu zcela nový byt o dispozici 2+kk a výměře 58,90 m², který se nachází "
    "ve 2. nadzemním podlaží moderní novostavby multifunkčního domu na ulici Ostravská v "
    "Olomouci – městská část Hodolany. Dům je aktuálně ve fázi dokončování, takže budoucí "
    "nájemce bude patřit mezi první obyvatele bytového domu."
)
OSTRAVSKA_5870 = OSTRAVSKA_5890.replace("58,90 m²", "58,7 m²")

# 36079 / 359820 and 86335 / 103778: ONE flat, two portals, two spellings of one number.
SPELLING_A = (
    "Pronajmu byt 3+1 o celkové ploše 140m2 ve druhém nadzemním podlaží cihlového domu v "
    "centru města. Byt je po kompletní rekonstrukci, k dispozici od 06/2026. K bytu náleží "
    "sklep a možnost parkování ve dvoře. Vytápění je ústřední plynové, okna jsou nová."
)
SPELLING_B = (
    "Pronajmu byt 3+1 o celkové ploše 140m ve 2.nadzemním podlaží cihlového domu v "
    "centru města. Byt je po kompletní rekonstrukci, k dispozici od 07/2026. K bytu náleží "
    "sklep a možnost parkování ve dvoře. Vytápění je ústřední plynové, okna jsou nová."
)

# 138148 / 348637: one Gerstnerova flat measured two ways — and the second advert prints BOTH.
BASIS_ONE = (
    "Nabízíme k prodeji byt 2+kk o podlahové ploše 48,4 m² v ulici Gerstelova. Byt se skládá "
    "z obývacího pokoje s kuchyňským koutem o výměře 26,7 m², ložnice 13,6 m² a koupelny. "
    "Součástí bytu je sklepní kóje o výměře 9 m². Dům prošel celkovou revitalizací."
)
BASIS_TWO = (
    "Nabízíme k prodeji byt 2+kk o užitné ploše 52,4 m² v ulici Gerstelova. Byt se skládá "
    "z obývacího pokoje s kuchyňským koutem o výměře 26,7 m², ložnice 13,6 m² a koupelny. "
    "Součástí bytu je sklepní kóje o výměře 9 m². Podlahová plocha bytu je 48,4 m²."
)


def listing(listing_id: int, **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "id": listing_id, "block": "b", "source": "sreality",
        "category_main": "byt", "category_type": "prodej",
        "location": Location(obec_kod=1, obec_name="Olomouc", street_key="loudova"),
        "first_seen_at": "2026-05-01T00:00:00+00:00",
        "last_seen_at": "2026-09-01T00:00:00+00:00",
    }
    fields.update(kwargs)
    return Listing(**fields)  # type: ignore[arg-type]


# ------------------------------------------------------- E150 the reader that knows no form
@pytest.mark.parametrize("left, right, want", [
    (LOUDOVA_B122, LOUDOVA_B123, ("b1.2.2", "b1.2.3")),
    (LOUDOVA_B122, LOUDOVA_B222, ("b1.2.2", "b2.2.2")),
    (STARNOV_8, STARNOV_7, ("1081", "1080")),
    (ROKYTNA_761, ROKYTNA_778, ("76,1", "77,8")),
    (OSTRAVSKA_5890, OSTRAVSKA_5870, ("58,90", "58,7")),
])
def test_e150_reads_the_position_that_names_the_unit(left, right, want) -> None:
    assert aligned_difference(left, right, 0.60) == want


@pytest.mark.parametrize("left, right", [
    # One number, two spellings, plus an availability month that is not a unit.
    (SPELLING_A, SPELLING_B),
    # Two bases of one flat, where the second advert prints both figures itself.
    (BASIS_ONE, BASIS_TWO),
    # Truncation is an insert, never a replace: a number on one side has contradicted nothing.
    (LOUDOVA_B122, LOUDOVA_B122[:260]),
    # Two adverts that share nothing align on nothing, and an alignment below the floor is
    # not read at all.
    (LOUDOVA_B122, ROKYTNA_761),
])
def test_e150_is_silent_where_one_unit_is_written_twice(left, right) -> None:
    assert aligned_difference(left, right, 0.60) is None


def test_e150_never_reads_a_unit_symbol_as_a_number() -> None:
    """`m²` survives an HTML strip as `sup2` on one portal and `m2` on another, and both have
    the shape of a block label. 122488 x 540766 was split on exactly that."""
    left = LOUDOVA_B122.replace("53,5 m²", "53,5 m2")
    right = LOUDOVA_B122.replace("53,5 m²", "53,5 m<sup>2</sup>")
    assert aligned_difference(left, right, 0.60) is None


def test_rounding_is_arithmetic_not_a_tolerance() -> None:
    assert rounding_equal("50", "50,5")
    assert rounding_equal("58,9", "58,90")
    assert not rounding_equal("58,90", "58,70")
    assert not rounding_equal("1081", "1080")
    assert rounding_equal_values(50.0, 0, 50.5, 1)
    assert not rounding_equal_values(58.9, 2, 58.7, 2)


# ------------------------------------------------------------- E151 the street the body names
def test_e151_reads_the_street_the_prose_names() -> None:
    assert prose_streets("Kancelář na ulici Krapkova 3 v Olomouci.") == frozenset({"krapkova"})
    assert "litovelska" in prose_streets("Nabízíme prostory v ulici Litovelská 101/17.")


def test_e151_cuts_the_capture_at_a_word_that_is_not_a_street() -> None:
    """261802: the title is truncated after `ul.` and the body resumes with a capitalised
    verb, so `Nabízíme` became the only street on its side and refused a real one."""
    assert prose_streets("garážové stání v Olomouci, ul. Nabízíme k pronájmu") == frozenset()
    assert prose_streets("v Olomouci, ul. Milana Ticháka Nabízíme k pronájmu") == frozenset(
        {"milana", "milana tichaka"})


def test_e151_never_lets_a_numeral_be_a_street_on_its_own() -> None:
    """`tř. 20. dubna` numbers its street; `20.` alone names nothing and would refuse every
    advert that named a real one (142542 x 430172)."""
    assert prose_streets("Byt na tř. 20. Dubna v Olomouci.") == frozenset({"20. dubna"})


def test_e151_ignores_a_street_named_as_a_landmark() -> None:
    assert prose_streets("Nedaleko ulice Krapkova je park.") == frozenset()


def test_e151_needs_both_bodies_to_name_one() -> None:
    """Prose against prose. A resolved street key is the `street` fact's business, and reading
    it here turned every gap in the geocoder into a refusal (292271 x 346593)."""
    a = listing(1, description="Kancelář na ulici Krapkova 3.",
                location=Location(obec_kod=1, street_key="krapkova"))
    b = listing(2, description="Kancelář bez uvedené ulice.",
                location=Location(obec_kod=1, street_key="litovelska"))
    assert prose_street_conflict(a, b) is None
    c = listing(3, description="Nabízíme prostory v ulici Litovelská 101/17.",
                location=Location(obec_kod=1))
    assert prose_street_conflict(a, c) is not None


def test_streets_agree_on_an_inflection_not_on_two_streets() -> None:
    assert streets_agree({"krapkova"}, {"krapkove"})
    assert not streets_agree({"krapkova"}, {"litovelska"})


# ---------------------------------------------------------------- E152 the town the body names
def test_e152_reads_two_towns_each_body_prints_its_own() -> None:
    a = listing(1, description="Nabízíme pozemek v Droždíně u Olomouce, klidná lokalita.",
                location=Location(obec_kod=500496, obec_name="Droždín"))
    b = listing(2, description="Prodej pozemku cca 600 m² Oplocany u Tovačova.",
                location=Location(obec_kod=513750, obec_name="Oplocany"))
    assert prose_obec_conflict(a, b) is True


def test_e152_is_silent_when_a_village_is_advertised_under_its_district_town() -> None:
    a = listing(1, description="Prodej pozemku v obci Droždín u Olomouce.",
                location=Location(obec_kod=500496, obec_name="Droždín"))
    b = listing(2, description="Prodej pozemku v Droždíně, okres Olomouc.",
                location=Location(obec_kod=554782, obec_name="Olomouc"))
    assert prose_obec_conflict(a, b) is False


# ------------------------------------------------------------------- E153 the printed area
def test_e153_scopes_each_area_by_the_noun_that_carries_it() -> None:
    read = {(value, scope) for value, _, scope in printed_areas(ROKYTNA_761)}
    assert (76.1, "unit") in read
    assert (10.0, "accessory") in read


def test_e153_sees_past_a_degenerate_stored_column() -> None:
    """bazos stores `area_m2 = 10.0` — the terrace — for a 76 m² apartment, and every reader
    that goes through `stated_areas`' stored-column window is blind to the 76."""
    a = listing(1, source="bazos", area_m2=10.0, description=ROKYTNA_761)
    b = listing(2, source="bazos", area_m2=10.0, description=ROKYTNA_778)
    assert printed_area_conflict(a, b) is not None


def test_e153_lets_the_stored_column_reconcile_two_printed_readings() -> None:
    """16438 x 92824: one portal prints 47,6 and the other 46,6, and both store 47."""
    a = listing(1, area_m2=47.0, description="Byt 2+kk o výměře 47,6 m² v cihlovém domě.")
    b = listing(2, area_m2=47.0, description="Byt 2+kk o výměře 46,6 m² v cihlovém domě.")
    assert printed_area_conflict(a, b) is None


def test_e153_says_nothing_when_only_one_body_prints_an_area() -> None:
    a = listing(1, area_m2=51.0, description="Byt 2+kk o výměře 51 m² v cihlovém domě.")
    b = listing(2, area_m2=54.0, description="Byt 2+kk v cihlovém domě po rekonstrukci.")
    assert printed_area_conflict(a, b) is None


def test_e153_meets_on_whichever_basis_the_two_share() -> None:
    a = listing(1, area_m2=54.0, description="Byt o užitné ploše 62 m², podlahová 54 m².")
    b = listing(2, area_m2=54.0, description="Byt o podlahové ploše 54 m² s balkonem.")
    assert printed_area_conflict(a, b) is None


# -------------------------------------------------------------------- E154 E145 fails closed
def _floor_pair(**kwargs: object) -> tuple[Listing, Listing]:
    left = dict(source="bezrealitky", floor=3, price=60948.0, broker_key=None,
                category_type="pronajem", area_m2=63.0,
                description="Pronájem bytu 2+kk v novostavbě Vrbenského.")
    right = dict(left, floor=4, price=65418.0)
    right.update(kwargs)
    return listing(1, **left), listing(2, **right)  # type: ignore[arg-type]


def test_e154_a_null_broker_key_no_longer_silences_the_floor() -> None:
    """141021 x 141022 and 93417 x 93419: bezrealitky is 100 % null, so as shipped the
    same-portal one-storey fact went silent and floors 3 and 4 of one new-build fused."""
    a, b = _floor_pair()
    open_cfg = Settings.from_json("autodedup/settings/w15.json")
    assert not [f for f in distinguishing_facts(a, b, None, open_cfg, CLUSTER)
                if f.name == "floor"]
    assert [f for f in distinguishing_facts(a, b, None, W16, CLUSTER) if f.name == "floor"]


def test_e154_needs_the_price_to_disagree_too() -> None:
    """418942 x 15427848: one 131 m² 4+1 re-posted on ceskereality at 6,988,000 and 6,980,000
    with its storey written both `1. NP` and `2. NP`. A storey typed two ways is not a flat."""
    a, b = _floor_pair(price=60948.0)
    assert not [f for f in distinguishing_facts(a, b, None, W16, CLUSTER) if f.name == "floor"]


def test_e154_leaves_a_known_and_different_feed_alone() -> None:
    """Two brokers' feeds are two conventions, which is what E145 was built to read."""
    a, b = _floor_pair()
    a, b = replace(a, broker_key="x"), replace(b, broker_key="y")
    assert not [f for f in distinguishing_facts(a, b, None, W16, CLUSTER) if f.name == "floor"]


def test_e155_never_reads_an_area_as_a_parcel() -> None:
    for text in ("parcely o celkové výměře 1000 m2 jsou zasíťované",
                 "na parcele je vzrostlá zeleň", "pozemek o výměře 1 204 m2"):
        assert parcel_numbers(text, wide=True) == set()


# ------------------------------------------------------------------- E155 the parcel forms
@pytest.mark.parametrize("text, want", [
    ("Parcelní číslo: st. 661, výměra 1 204 m²", {"661"}),
    ("prodáváme pozemek č. 2150 v katastru obce", {"2150"}),
    ("jedná se o parcelu 934/11 se vzrostlými stromy", {"934/11"}),
    ("pozemek je veden pod číslem parcely 417", {"417"}),
    ("stavba stojí na parcele 1633 o výměře 1 973 m2", {"1633"}),
    ("č. parc. 2361 v k.ú. Semily", {"2361"}),
])
def test_e155_reads_the_forms_the_narrow_keyword_missed(text, want) -> None:
    assert parcel_numbers(text, wide=True) >= want


def test_e155_widening_never_takes_a_parcel_away() -> None:
    narrow = "parcelní číslo 934/11 a parc. č. 935/4"
    assert parcel_numbers(narrow, wide=True) >= parcel_numbers(narrow)


# ------------------------------------------- E156 no fact, no separation (Penzion Horálka)
def test_e156_puts_back_a_separation_no_fact_justifies() -> None:
    """10060166 x 10064638 — same 374 m², same price, same body, sreality against mmreality —
    were cut to two singletons by a conflict neither of them was party to."""
    members = [1, 2, 3]
    edges = [Edge(1, 2, 0.9, False), Edge(2, 3, 0.9, False)]

    def invariants(group):
        return "conflict" if 1 in group and 3 in group else None

    without = partition(members, edges, invariants, 4, False)
    with_repair = partition(members, edges, invariants, 4, True)
    assert sorted(len(cell) for cell in without) == [1, 2]
    assert sorted(len(cell) for cell in with_repair) == [1, 2]
    # 2 must end up WITH one of them rather than alone, whichever way the repair falls.
    assert max(len(cell) for cell in with_repair) == 2


def test_e156_leaves_a_member_that_carries_a_fact_where_it_is() -> None:
    members = [1, 2, 3]
    edges = [Edge(1, 2, 0.9, False), Edge(2, 3, 0.9, False), Edge(1, 3, 0.9, False)]

    def invariants(group):
        return "conflict" if len(group) > 2 else None

    cells = partition(members, edges, invariants, 4, True)
    assert sorted(len(cell) for cell in cells) == [1, 2]


def test_e156_is_a_function_of_the_edge_set_not_its_order() -> None:
    members = [1, 2, 3, 4]
    edges = [Edge(1, 2, 0.9, False), Edge(2, 3, 0.8, False), Edge(3, 4, 0.7, False)]

    def invariants(group):
        return "conflict" if 1 in group and 4 in group else None

    first = partition(members, edges, invariants, 4, True)
    second = partition(members, list(reversed(edges)), invariants, 4, True)
    assert sorted(map(sorted, first)) == sorted(map(sorted, second))
