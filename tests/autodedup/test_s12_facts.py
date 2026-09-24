"""S12's readings (E270-E277), each built from the advert out of cohort 14 that named it.

`Pod Sekvojí` (Trutnov, Horní Staré Město) is the whole of E270: ten plots of ONE idnes land
project whose bodies are byte-identical but for a trailing `Označení pozemku v projektu A13` /
`A14` / ... / `A40`, all 1,001 m² at 3,900 Kč/m² = 3,903,900, nine of them live together — and
every generation since S4 fused them into one group. Zelené údolí / Kunratice is E271 and E272:
`Pod Haltýřem 1497/9` (3. patro, 15,000 → 14,500) and `1497/11` (2. patro, 14,000 → 13,500),
two entrances of one building under ONE template body. Rezidence Na Mariánské cestě is E273:
101 m² at 12,823,000 → 12,438,310 against 102 m² at 11,700,460, the one sreality × sreality
hole in two otherwise fact-separated cross-portal groups. Radimovice / Petříkov is E274 (a
family package at 45,000,000 against an investment package at 57,000,000 adding `parc. č.
45/1`), Zeleneč 2+kk is E275 (44 m² against 45 m² inside one bažoš train under `Ev.č. 945210`),
Herínk is E276 (`Ev.č. 03105` at 257,698 against `Ev.č. 03104` at 440,370) and the Říčany
Jívová plot is E277 (five portals, 7,900,000 then 7,390,000, torn by a detection stamp).
"""

from __future__ import annotations

import json
from pathlib import Path

from autodedup.dataset import Listing
from autodedup.indistinguishable import CLUSTER, GATE, distinguishing_facts
from autodedup.settings import Settings
from autodedup.text_facts import printed_lot_labels

SETTINGS = Path(__file__).resolve().parents[2] / "autodedup/settings"
S11 = Settings.from_json(SETTINGS / "w26.json")
S12 = Settings.from_json(SETTINGS / "w27.json")


def variant(**kwargs: object) -> Settings:
    raw = json.loads((SETTINGS / "w26.json").read_text(encoding="utf-8"))
    raw.update(kwargs)
    return Settings.from_dict(raw)


def stamp(day: int) -> str:
    return f"2026-0{1 + day // 28}-{1 + day % 28:02d}T00:00:00+00:00"


def names(a: Listing, b: Listing, cfg: Settings, mode: str = GATE,
          feats: dict[str, tuple[float, bool]] | None = None) -> list[str]:
    return [fact.name for fact in distinguishing_facts(a, b, feats, cfg, mode)]


# E185 asks for an identity beside the price move: the same photographs, one body inside the
# other, or the seller's own order code on both. A re-post train carries all three.
ONE_ADVERT: dict[str, tuple[float, bool]] = {
    "phash_tight_matches": (6.0, True),
    "containment_max": (1.0, True),
    "ref_code_shared": (1.0, True),
}


# --- E270: the lot label a land project prints for its own plot -------------------------------
SEKVOJI = (
    "Prodej pozemek Bydlení, 1001m², Trutnov, Horní Staré Město Nabízíme ke koupi pozemek o "
    "výměře 1 001 m² v oblíbeném projektu Pod Sekvojí. Základní parametry pozemku: - výměra: "
    "1 001 m² - cena: 3900,- Kč/m² - pozemky jsou určeny k výstavbě rodinného domu Máte zájem "
    "nebo potřebujete více informací? Označení pozemku v projektu {label}"
)
# The project advert that publishes the whole roster instead of naming one plot.
SEKVOJI_ROSTER = SEKVOJI.format(label="A13") + " Volné pozemky: A13, A14, A15, A16."


def plot(listing_id: int, label: str, first: int = 0, last: int = 78,
         body: str | None = None, **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "source": "idnes",
        "category_main": "pozemek",
        "category_type": "prodej",
        "area_m2": 1001.0,
        "price": 3903900.0,
        "description": body if body is not None else SEKVOJI.format(label=label),
        "first_seen_at": stamp(first),
        "last_seen_at": stamp(last),
        "price_history": [[stamp(first), 3903900.0]],
        "location": {"obec_kod": 579025, "granularity": "obec", "granularity_rank": 20},
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def test_the_lot_label_is_read_in_the_form_the_project_writes() -> None:
    assert printed_lot_labels(SEKVOJI.format(label="A13")) == {"pozemek": frozenset({"A13"})}
    assert printed_lot_labels(SEKVOJI.format(label="A40")) == {"pozemek": frozenset({"A40"})}


def test_no_reader_before_E270_knew_the_form() -> None:
    assert "lot_label" not in names(plot(156917, "A22"), plot(157923, "A13"), S11)


def test_two_plots_of_one_project_are_parted_by_their_labels() -> None:
    a, b = plot(156917, "A22"), plot(157923, "A13")
    assert "lot_label" in names(a, b, S12)


def test_the_re_post_of_ONE_plot_keeps_its_label() -> None:
    # 157876 (06-01..08-18) and 17979630 (08-22..09-23) are both A14.
    a, b = plot(157876, "A14", first=0, last=78), plot(17979630, "A14", first=82, last=114)
    assert "lot_label" not in names(a, b, S12)


def test_a_body_that_names_no_label_refuses_nothing() -> None:
    plain = plot(289137, "", body=SEKVOJI.split(" Máte zájem")[0])
    assert "lot_label" not in names(plain, plot(157923, "A13"), S12)


def test_the_project_ROSTER_is_not_a_label() -> None:
    assert printed_lot_labels(SEKVOJI_ROSTER) == {}
    roster = plot(400000, "A13", body=SEKVOJI_ROSTER)
    assert "lot_label" not in names(roster, plot(157923, "A13"), S12)


def test_an_area_is_never_read_as_a_lot() -> None:
    assert printed_lot_labels("Označení pozemku 1 001 m2") == {}
    assert printed_lot_labels("Nabízíme pozemek 1001 m2 za 3900 Kč/m2") == {}


def test_a_bare_code_after_a_DWELLING_noun_is_the_building_not_the_flat() -> None:
    # E161's own refusal, kept: `byt B2` names the building every flat in it shares.
    assert printed_lot_labels("byt B2 o dispozici 3+kk") == {}
    assert printed_lot_labels("Označení jednotky B12", land_only=True) == {}
    assert printed_lot_labels("Označení jednotky B12") == {"jednotka": frozenset({"B12"})}


def test_the_english_roster_is_read_too() -> None:
    assert printed_lot_labels("Building plot no. 7 of the project") == {
        "lot": frozenset({"7"})}


# --- E271 / E272: two entrances of one building -----------------------------------------------
ZELENE_UDOLI = (
    "pronájem 1kk 30m2 P4 Kunratice Pod Haltýřem novostavba nezařízen. Nachází se v komplexu "
    "novostaveb Zelené údolí, 3 stanice autobusem na metro Kačerov. Byt je zařízen kuchyňskou "
    "linkou s varnou deskou. K bytu náleží sklep."
)


def flat(listing_id: int, house_number: str, ruian: int, floor: int, price: float,
         opening: float, first: int, last: int, **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "source": "sreality",
        "category_main": "byt",
        "category_type": "pronajem",
        "area_m2": 30.0,
        "disposition": "1+kk",
        "floor": floor,
        "price": price,
        "description": ZELENE_UDOLI,
        "first_seen_at": stamp(first),
        "last_seen_at": stamp(last),
        "price_history": [[stamp(first), opening], [stamp(first + 8), price]],
        "broker_key": f"broker-{house_number}",
        "broker_firm_id": 995 if house_number.endswith("9") else 2115,
        "location": {"obec_kod": 554782, "street_key": "pod haltyrem",
                     "house_number": house_number, "ruian_adm_kod": ruian,
                     "granularity": "address_point", "granularity_rank": 100,
                     "is_address_grain": True},
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def kunratice() -> tuple[Listing, Listing]:
    return (flat(326369, "1497/9", 26703785, 3, 14500.0, 15000.0, 15, 36),
            flat(507121, "1497/11", 26703793, 2, 13500.0, 14000.0, 37, 65))


def test_a_shared_cp_with_a_different_co_is_two_entrances() -> None:
    a, b = kunratice()
    assert "stored_house_number" in names(a, b, S12)
    assert "stored_house_number" not in names(a, b, S11)


def test_the_entrance_reading_needs_ONE_portal_and_two_address_points() -> None:
    a, b = kunratice()
    cross = flat(507121, "1497/11", 26703793, 2, 13500.0, 14000.0, 37, 65, source="idnes")
    assert "stored_house_number" not in names(a, cross, S12)
    one_point = flat(507121, "1497/11", 26703785, 2, 13500.0, 14000.0, 37, 65)
    assert "stored_house_number" not in names(a, one_point, S12)


def test_one_advert_re_filed_at_the_same_entrance_is_no_conflict() -> None:
    a = flat(326369, "1497/9", 26703785, 3, 14500.0, 15000.0, 15, 36,
             broker_key="a", broker_firm_id=995)
    b = flat(507121, "1497/9", 26703785, 3, 13500.0, 14000.0, 37, 65,
             broker_key="b", broker_firm_id=2115)
    assert "stored_house_number" not in names(a, b, S12)


def test_the_storey_between_two_sequential_postings_at_two_addresses_is_a_fact() -> None:
    a, b = kunratice()
    assert "floor" in names(a, b, S12)
    assert "floor" not in names(a, b, S11)


def test_the_storey_of_a_re_post_at_ONE_address_is_still_the_portal_drifting() -> None:
    # Two agencies of one building, as the real pair is, so only E272 can lift the guard.
    a = flat(326369, "1497/9", 26703785, 3, 14500.0, 15000.0, 15, 36,
             broker_key="a", broker_firm_id=995)
    b = flat(507121, "1497/9", 26703785, 2, 13500.0, 14000.0, 37, 65,
             broker_key="b", broker_firm_id=2115)
    assert "floor" not in names(a, b, S12)


# --- E273: the same-source price bar where the area column moved too ---------------------------
MARIANSKA_A = (
    "Máte jedinečnou šanci dostat k bytu 4+KK GARÁŽOVÉ STÁNÍ ZDARMA!! Nabízíme poslední byty "
    "ihned k nastěhování v nejžádanější Rezidenci Na Mariánské cestě inspirovaná "
    "skandinávským stylem, která získala ocenění Projekt roku! Ideální místo pro rodiny s "
    "dětmi, páry, jednotlivce i starší generaci, kteří si chtějí užívat klidné a harmonické "
    "prostředí. Pečlivě navržené byty poskytují maximální pohodlí, vzdušnost a funkčnost, "
    "zatímco okolí rezidence nabízí široké možnosti relaxace i aktivního trávení volného "
    "času. K dispozici je workoutové hřiště, komunitní zahrádka i sdílené ohniště. Bezpečné "
    "a pohodlné parkování zajistí vnitřní parkovací stání. Rezervujte si svůj vysněný byt!"
)
MARIANSKA_B = MARIANSKA_A.replace(
    "Máte jedinečnou šanci dostat k bytu 4+KK GARÁŽOVÉ STÁNÍ ZDARMA!!",
    "LETNÍ SLEVA 3% z ceny bytu + GARÁŽOVÉ STÁNÍ ZDARMA!!")
# The same advert without a word of a project in it — a flat, not a development.
ZELENEC = (
    "Nabízíme k prodeji byt o dispozici 2+kk v obci Zeleneč - Praha východ, která se nachází "
    "cca 20 min od Prahy a nabízí okolí plné zeleně. Dominantou bytu je obývací část s "
    "kuchyňským koutem a pracovnou, ze které je vstup na prostorný balkon orientovaný do "
    "klidné části. Součástí bytu je koupelna se sprchovým koutem, samostatná toaleta a "
    "komora. K bytu náleží sklepní kóje a parkovací stání před domem. Byt je volný ihned "
    "a je připraven k nastěhování. Ev.č. 945210"
)


def unit(listing_id: int, area: float, price: float, path: list[float], first: int, last: int,
         body: str = MARIANSKA_A, source: str = "sreality", **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "source": source,
        "category_main": "byt",
        "category_type": "prodej",
        "area_m2": area,
        "disposition": "4+kk",
        "floor": 1,
        "price": price,
        "description": body,
        "first_seen_at": stamp(first),
        "last_seen_at": stamp(last),
        "price_history": [[stamp(first + index * 10), value]
                          for index, value in enumerate(path)],
        "location": {"obec_kod": 538094, "street_key": "drevcicka", "house_number": "2716",
                     "granularity": "address_point", "granularity_rank": 100,
                     "is_address_grain": True},
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def marianska() -> tuple[Listing, Listing]:
    return (unit(18482, 101.0, 12438310.0, [12823000.0, 12438310.0], 0, 74),
            unit(14063960, 102.0, 11700460.0, [11700460.0], 91, 140, body=MARIANSKA_B))


def test_two_units_of_one_residence_are_parted_by_price_where_the_column_moved() -> None:
    a, b = marianska()
    assert "price" in names(a, b, S12)
    assert "price" not in names(a, b, S11)


def test_one_unit_re_posted_at_a_new_price_is_still_one_unit() -> None:
    a = unit(18482, 101.0, 12438310.0, [12823000.0, 12438310.0], 0, 74)
    b = unit(14063960, 101.0, 11700460.0, [11700460.0], 91, 140, body=MARIANSKA_B)
    assert "price" not in names(a, b, S12)


def test_one_text_re_posted_at_a_new_price_is_one_advert() -> None:
    # Zelene udoli 3+kk, ceskereality: BYTE-IDENTICAL body, 79 m2 at 10,990,000 re-posted at
    # 78 m2 and 11,990,000. A re-post copies its own text; two units do not.
    a = unit(11385029, 79.0, 10990000.0, [10990000.0], 0, 3, source="ceskereality")
    b = unit(15225124, 78.0, 11990000.0, [11990000.0], 16, 28, source="ceskereality")
    assert a.description == b.description
    assert "price" not in names(a, b, S12)


def test_the_wider_bar_reaches_only_a_DEVELOPMENT() -> None:
    a = unit(1, 44.0, 5300000.0, [5300000.0], 0, 13, body=ZELENEC, source="bazos")
    b = unit(2, 45.0, 4990000.0, [4990000.0], 20, 30, body=ZELENEC, source="bazos")
    assert "price" not in names(a, b, S12)


# --- E274: two packages of one object ----------------------------------------------------------
RADIMOVICE_FAMILY = (
    "V exkluzivním zastoupení majitele nabízíme k prodeji výjimečný soubor nemovitostí v obci "
    "Radimovice u Prahy. Nemovitost tvoří několik samostatných budov a rozsáhlý pozemek o "
    "celkové výměře 3 526 m², který nabízí komfortní bydlení i prostor pro další rozvoj."
)
RADIMOVICE_INVEST = (
    "V exkluzivním zastoupení majitele nabízíme k prodeji výjimečnou investiční nemovitost v "
    "obci Radimovice. Další významnou přidanou hodnotou je pozemek parc. č. 45/1, který nabízí "
    "možnost dalšího rozdělení na 2-3 stavební parcely pro rodinné domy."
)


def areal(listing_id: int, price: float, body: str, source: str = "remax",
          **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "source": source,
        "category_main": "dum",
        "category_type": "prodej",
        "area_m2": 1145.0,
        "price": price,
        "description": body,
        "first_seen_at": stamp(0),
        "last_seen_at": stamp(104),
        "price_history": [[stamp(0), price]],
        "location": {"obec_kod": 538345, "granularity": "obec", "granularity_rank": 20},
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def test_two_packages_of_one_areal_are_two_extents() -> None:
    a = areal(276682, 45000000.0, RADIMOVICE_FAMILY)
    b = areal(276686, 57000000.0, RADIMOVICE_INVEST)
    assert "extent_package" in names(a, b, S12)
    assert "extent_package" not in names(a, b, S11)


def test_the_price_may_be_carried_by_the_PATH_alone() -> None:
    # The sreality rows print no price column at all; the path is where the number lives.
    a = areal(272497, None, RADIMOVICE_FAMILY, source="sreality",
              price_history=[[stamp(0), 45000000.0]])
    b = areal(46006, None, RADIMOVICE_INVEST, source="sreality",
              price_history=[[stamp(0), 57000000.0]])
    assert "extent_package" in names(a, b, S12)


def test_two_portals_carrying_ONE_package_are_not_parted() -> None:
    a = areal(276682, 45000000.0, RADIMOVICE_FAMILY)
    b = areal(392415, 45000000.0, RADIMOVICE_FAMILY, source="realitymix")
    assert "extent_package" not in names(a, b, S12)


def test_a_body_that_states_no_extent_refuses_nothing() -> None:
    a = areal(276682, 45000000.0, RADIMOVICE_FAMILY)
    silent = areal(999, 57000000.0, "Prodej areálu v Radimovicích u Prahy.")
    assert "extent_package" not in names(a, silent, S12)


def test_two_extents_at_ONE_price_are_one_offer() -> None:
    a = areal(276682, 45000000.0, RADIMOVICE_FAMILY)
    b = areal(276686, 45000000.0, RADIMOVICE_INVEST)
    assert "extent_package" not in names(a, b, S12)


# --- E275 / E277: the re-post train the clusterer may not carve --------------------------------
def train(listing_id: int, source: str, area: float, price: float, first: int, last: int,
          **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "source": source,
        "category_main": "byt",
        "category_type": "prodej",
        "area_m2": area,
        "disposition": "2+kk",
        "price": price,
        "description": ZELENEC,
        "first_seen_at": stamp(first),
        "last_seen_at": stamp(last),
        "price_history": [[stamp(first), price]],
        "location": {"obec_kod": 538442, "granularity": "obec", "granularity_rank": 20},
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def test_one_square_metre_inside_a_train_is_the_portals_rounding() -> None:
    a = train(264187, "bazos", 44.0, 5300000.0, 7, 20)
    b = train(388677, "mmreality", 45.0, 4990000.0, 27, 29)
    assert "price" in names(a, b, S11, feats=ONE_ADVERT)
    assert "price" not in names(a, b, S12, feats=ONE_ADVERT)


def test_the_tolerance_never_reaches_a_development() -> None:
    a, b = marianska()
    assert "price" in names(a, b, S12, CLUSTER, feats=ONE_ADVERT)


def test_a_real_area_difference_still_refuses_the_escape() -> None:
    a = train(264187, "bazos", 44.0, 5300000.0, 7, 20)
    b = train(388677, "mmreality", 62.0, 4990000.0, 27, 29)
    assert "price" in names(a, b, S12, feats=ONE_ADVERT)


def test_the_detection_stamp_may_not_make_a_re_post_co_live() -> None:
    # Říčany / Jívová: 461181 last SIGHTED 08-04 and detected gone days later, 13462979 first
    # sighted 08-04 — the detection clock hands the pair an overlap it never had.
    a = train(461181, "sreality", 513.0, 7900000.0, 0, 34,
              inactive_at=stamp(40), category_main="pozemek")
    b = train(13462979, "idnes", 513.0, 7390000.0, 34, 74, category_main="pozemek")
    assert "price" in names(a, b, S11, feats=ONE_ADVERT)
    assert "price" not in names(a, b, S12, feats=ONE_ADVERT)


def test_two_adverts_genuinely_on_sale_together_keep_their_price_fact() -> None:
    a = train(461181, "sreality", 513.0, 7900000.0, 0, 60, category_main="pozemek")
    b = train(13462979, "idnes", 513.0, 7390000.0, 5, 74, category_main="pozemek")
    assert "price" in names(a, b, S12, feats=ONE_ADVERT)


# --- E276: the Herínk conjunction ---------------------------------------------------------------
HERINK = (
    "Nabízíme k pronájmu halu o výměře 1 106 m² v areálu Herínk u Prahy. Hala je vytápěná, s "
    "vjezdovými vraty a kancelářským zázemím. Ev.č. {code}"
)


def hall(listing_id: int, code: str, price: float, **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "source": "bazos",
        "category_main": "komercni",
        "category_type": "pronajem",
        "area_m2": 1106.0,
        "price": price,
        "description": HERINK.format(code=code),
        "first_seen_at": stamp(35),
        "last_seen_at": stamp(91),
        "price_history": [[stamp(35), price]],
        "location": {"obec_kod": 538345, "granularity": "obec", "granularity_rank": 20},
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def test_the_herink_conjunction_is_read_when_the_dial_is_on() -> None:
    a, b = hall(224525, "03105", 257698.0), hall(224526, "03104", 440370.0)
    on = variant(d43_agency_code_colive_price=True)
    assert "agency_code_colive" in names(a, b, on)


def test_two_codes_at_prices_that_MEET_are_one_object() -> None:
    on = variant(d43_agency_code_colive_price=True)
    a, b = hall(224525, "03105", 257698.0), hall(224526, "03104", 257698.0)
    assert "agency_code_colive" not in names(a, b, on)


def test_two_codes_that_never_lived_together_are_a_re_post() -> None:
    on = variant(d43_agency_code_colive_price=True)
    a = hall(224525, "03105", 257698.0, first_seen_at=stamp(0), last_seen_at=stamp(20))
    b = hall(224526, "03104", 440370.0, first_seen_at=stamp(30), last_seen_at=stamp(60))
    assert "agency_code_colive" not in names(a, b, on)


# --- the refinements the fourteen-cohort hand read of S12's own losses named ----------------
def test_one_agency_re_filing_its_own_address_is_not_two_entrances() -> None:
    # Kolmanova 2438/18 then 2438/20: one broker, one firm, one byte-identical body.
    a = flat(14040386, "2438/18", 1, 2, 25900.0, 24900.0, 0, 10, broker_key="k", broker_firm_id=9)
    b = flat(17301787, "2438/20", 2, 2, 24900.0, 25900.0, 11, 14, broker_key="k", broker_firm_id=9)
    assert "stored_house_number" not in names(a, b, S12)
    assert "floor" not in names(a, b, S12)


def test_a_re_let_at_a_new_rent_is_not_a_development_unit() -> None:
    a = unit(7337, 50.0, 23000.0, [23500.0, 23000.0], 0, 36, category_type="pronajem")
    b = unit(18597494, 49.0, 24900.0, [25500.0, 24900.0], 116, 141, body=MARIANSKA_B,
             category_type="pronajem")
    assert "price" not in names(a, b, S12)


def test_a_re_post_that_cut_the_plot_is_not_a_second_package() -> None:
    a = areal(448583, 6990000.0, RADIMOVICE_FAMILY, first_seen_at=stamp(0),
              last_seen_at=stamp(72))
    b = areal(18815653, 4990000.0, RADIMOVICE_INVEST, first_seen_at=stamp(72),
              last_seen_at=stamp(83))
    assert "extent_package" not in names(a, b, S12)
