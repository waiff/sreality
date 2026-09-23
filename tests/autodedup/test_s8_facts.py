"""S8's readings (E230, E231), each built from the advert that named it.

Every case is a real pair out of cohort 10: the Na Zlaté stoce administrative building whose
one agency template lets `prostor č.201` and `prostor č.303` as nine adverts of two 15 m²
offices at 4,000 Kč under one order number (116475 / 212814 / 251259 / 364420 against
18908144 / 18908175 / 18908331 / 18908753, with realitymix 452659 the row the agency edited in
place), and the Klášterská 75 bakery in Jindřichův Hradec let twice on ceskereality under one
460 m² column (550064 ground floor against 550065's 2. + 3. NP). Where a body is quoted it is
quoted from the advert, with the sentences the reader must see verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

from autodedup.dataset import Listing
from autodedup.indistinguishable import CLUSTER, GATE, PROMOTE, distinguishing_facts
from autodedup.settings import Settings
from autodedup.text_facts import further_areas, printed_space_numbers

SETTINGS = Path(__file__).resolve().parents[2] / "autodedup/settings"
S7 = Settings.from_json(SETTINGS / "w22.json")
S8 = Settings.from_json(SETTINGS / "w23.json")
S8_HOLD = Settings.from_json(SETTINGS / "w23_rentals_hold.json")


def variant(**kwargs: object) -> Settings:
    raw = json.loads((SETTINGS / "w23.json").read_text(encoding="utf-8"))
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


# --- E230: the space number a commercial letting plan prints --------------------------------
STOKA = (
    "Naše společnost Vám zprostředkuje pronájem komerčního prostoru, kanceláře o 15 m² "
    "{floor} dvoupatrové administrativní budovy, na atraktivní adrese, kousek od Výstaviště, "
    "u křižovatky ulic Na Zlaté stoce a Branišovská, České Budějovice. Konkrétně se jedná o "
    "nabídku {lead}prostor č.{number}, o půdorysu 3m x 5m s velkým oknem, viz půdorys ve foto "
    "galerii. Čisté měsíční nájemné činní 4.000,-/měs., + 1.250,-Kč /měsíčně paušální platby "
    "za služby s nájmem spojené. Kauce 5.250,-. Na každém podlaží jsou sdílené toalety, "
    "pánské a dámské. Ev. číslo: 652795."
)
UNIT_A = STOKA.format(floor="v prvním patře", lead="", number="201")
UNIT_B = STOKA.format(floor="ve druhém patře",
                      lead="východním směrem orientovaný ", number="303")


def stoka(first_b: int = 0, last_b: int = 40, source_b: str = "sreality"
          ) -> tuple[Listing, Listing]:
    a = listing(116475, first=0, last=40, area_m2=15.0, price=4000.0, subtype="kancelar",
                total_floors=3, description=UNIT_A)
    b = listing(18908331, first=first_b, last=last_b, area_m2=15.0, price=4000.0,
                subtype="kancelar", total_floors=3, source=source_b, description=UNIT_B)
    return a, b


def test_e230_reads_the_number_under_the_commercial_noun() -> None:
    assert printed_space_numbers(UNIT_A) == frozenset({"201"})
    assert printed_space_numbers(UNIT_B) == frozenset({"303"})


def test_e230_never_reads_the_order_number_beside_it() -> None:
    """`Ev. číslo: 652795` carries the anchor and no space noun, and both bodies print it."""
    assert "652795" not in printed_space_numbers(UNIT_A)


def test_e230_needs_the_anchor() -> None:
    """`kanceláře o 15 m²` is a size, not a name; only `č.` makes the number an identity."""
    assert printed_space_numbers(
        "Pronájem komerčního prostoru, kanceláře o 15 m² v prvním patře.") == frozenset()


def test_e230_abstains_on_a_letting_plan() -> None:
    plan = ("Volné jsou prostor č. 101, prostor č. 102, kancelář č. 203 a kancelář č. 204 "
            "v naší administrativní budově.")
    assert printed_space_numbers(plan) == frozenset()


def test_e230_separates_the_two_zlata_stoka_offices() -> None:
    a, b = stoka()
    assert names(a, b, S7) == []
    for mode in (PROMOTE, GATE, CLUSTER):
        assert "space_number" in names(a, b, S8, mode)


def test_e230_keeps_the_two_postings_of_ONE_office_together() -> None:
    """Four portals carry unit 201 under one template; the number is the same on all four."""
    a, _ = stoka()
    twin = listing(364420, area_m2=15.0, price=4000.0, source="ceskereality",
                   description=UNIT_A)
    assert "space_number" not in names(a, twin, S8)


def test_e230_reads_the_row_the_agency_edited_in_place() -> None:
    """realitymix 452659 prints unit 201's number with unit B's storey: 201 against 303."""
    bridge = listing(452659, area_m2=15.0, price=4000.0, source="realitymix",
                     description=STOKA.format(floor="ve druhém patře", lead="", number="201"))
    _, b = stoka()
    assert "space_number" in names(bridge, b, S8)


def test_e230_is_commercial_only() -> None:
    """A flat's body that numbers a room is numbering a room, not the offer."""
    a, b = stoka()
    flat_a = listing(1, area_m2=15.0, price=4000.0, category_main="byt",
                     description=UNIT_A)
    flat_b = listing(2, area_m2=15.0, price=4000.0, category_main="byt",
                     description=UNIT_B)
    assert "space_number" not in names(flat_a, flat_b, S8)
    assert "space_number" in names(a, b, S8)


def test_e230_reads_a_sequential_re_post_under_always_and_not_under_colive() -> None:
    """The Zlaté stoky units are SEQUENTIAL: unit A came down in July, unit B went up in
    September. `colive` therefore cannot reach them, which is why S8 ships `always`."""
    a, b = stoka(first_b=60, last_b=80)
    assert "space_number" in names(a, b, S8)
    assert "space_number" not in names(a, b, variant(d43_space_numbers="colive"))


def test_e230_is_a_dial() -> None:
    a, b = stoka()
    assert "space_number" not in names(a, b, variant(d43_space_numbers="off"))
    cross = stoka(source_b="idnes")
    assert "space_number" in names(cross[0], cross[1], S8)
    assert "space_number" not in names(
        cross[0], cross[1], variant(d43_space_numbers_same_source_only=True))


# --- E231: two adverts that each decompose one stored column --------------------------------
KLASTERSKA_GROUND = (
    "Nabízíme k pronájmu komerční objekt v centru Jindřichova Hradce, který po dlouhá léta "
    "sloužil jako výrobna a pekárna. Jedná se o prostor v zadní části domu přístupný z hlavní "
    "ulice o celkové výměře přes 200 m². K přízemí je možno pronajmout i kancelářské/skladové "
    "místnosti ve 2. a 3. NP objektu, které nabízí dalších téměř 250 m² plochy."
)
KLASTERSKA_UPPER = (
    "Nabízíme k pronájmu komerční objekt v centru Jindřichova Hradce, který po dlouhá léta "
    "sloužil jako výrobna a pekárna s administrativním zázemím. Jedná se o prostory s výtahem "
    "ve 2. a 3. nadzemním podlaží zadní části domu o celkové výměře téměř 260 m². K "
    "místnostem je možné navíc pronajmout přízemní prostory (bývalé pekárny a výrobny), které "
    "nabízí dalších 200 m² plochy."
)


def klasterska() -> tuple[Listing, Listing]:
    a = listing(550064, area_m2=460.0, price=20000.0, source="ceskereality",
                description=KLASTERSKA_GROUND)
    b = listing(550065, area_m2=460.0, price=20000.0, source="ceskereality",
                description=KLASTERSKA_UPPER)
    return a, b


def test_e231_reads_the_addition_the_body_names() -> None:
    assert further_areas(KLASTERSKA_GROUND) == frozenset({250.0})
    assert further_areas(KLASTERSKA_UPPER) == frozenset({200.0})


def test_e231_reads_no_addition_where_the_body_states_only_its_own_size() -> None:
    assert further_areas("Jedná se o prostor o celkové výměře přes 200 m².") == frozenset()


def test_e231_separates_the_two_halves_of_the_klasterska_bakery() -> None:
    a, b = klasterska()
    assert "part_addition" not in names(a, b, S7)
    for mode in (PROMOTE, GATE, CLUSTER):
        assert "part_addition" in names(a, b, S8, mode)


def test_e231_needs_the_body_to_decompose_its_OWN_column() -> None:
    """200 + 250 must come to the 460 m² the portal stored: without that arithmetic the lead
    is one of two bodies leading with different parts of one offer, which E186 already refused.
    """
    a, b = klasterska()
    loose_a = listing(550064, area_m2=1200.0, price=20000.0, source="ceskereality",
                      description=KLASTERSKA_GROUND)
    loose_b = listing(550065, area_m2=1200.0, price=20000.0, source="ceskereality",
                      description=KLASTERSKA_UPPER)
    assert "part_addition" not in names(loose_a, loose_b, S8)
    assert "part_addition" in names(a, b, S8)


def test_e231_says_nothing_when_the_two_leads_are_one_number() -> None:
    a, _ = klasterska()
    twin = listing(550066, area_m2=460.0, price=20000.0, source="ceskereality",
                   description=KLASTERSKA_GROUND)
    assert "part_addition" not in names(a, twin, S8)


def test_e231_is_a_dial() -> None:
    a, b = klasterska()
    assert "part_addition" not in names(a, b, variant(d43_part_addition=False))
    cross = listing(550065, area_m2=460.0, price=20000.0, source="idnes",
                    description=KLASTERSKA_UPPER)
    assert "part_addition" not in names(a, cross, S8)
    assert "part_addition" in names(
        a, cross, variant(d43_part_addition_same_source_only=False))


def test_e231_needs_the_two_adverts_on_sale_together() -> None:
    a, _ = klasterska()
    late = listing(550065, first=60, last=80, area_m2=460.0, price=20000.0,
                   source="ceskereality", description=KLASTERSKA_UPPER)
    assert "part_addition" not in names(a, late, S8)
    assert "part_addition" in names(a, late, variant(d43_part_addition_colive_only=False))


# --- the generation contract -----------------------------------------------------------------
def test_w23_differs_from_w22_only_in_the_dials_this_wave_names() -> None:
    w22 = Settings.from_json(SETTINGS / "w22.json").to_dict()
    w23 = Settings.from_json(SETTINGS / "w23.json").to_dict()
    assert {key for key in w23 if w22.get(key) != w23[key]} == {
        "d43_space_numbers", "d43_part_addition"}


def test_w23_rentals_hold_is_w23_plus_one_table() -> None:
    w23 = Settings.from_json(SETTINGS / "w23.json").to_dict()
    hold = Settings.from_json(SETTINGS / "w23_rentals_hold.json").to_dict()
    assert {key for key in hold if w23.get(key) != hold[key]} == {"merge_policy"}
    assert hold["merge_policy"] == {"pronajem|*": "propose"}


def test_no_shipped_generation_before_w23_names_an_s8_dial() -> None:
    dials = {"d43_space_numbers", "d43_space_numbers_same_source_only", "d43_part_addition",
             "d43_part_addition_same_source_only", "d43_part_addition_colive_only",
             "d43_part_addition_sum_tol"}
    for arm in ("w13", "w15", "w17", "w18", "w19", "w20", "w21", "w22",
                "w22_rentals_hold"):
        raw = json.loads((SETTINGS / f"{arm}.json").read_text(encoding="utf-8"))
        assert not (dials & set(raw)), f"{arm} names an S8 dial"


def test_every_s8_reading_stays_silent_under_w22() -> None:
    """The replay-parity direction, read at the fact grain rather than the settings grain."""
    new = {"space_number", "part_addition"}
    for pair in (stoka(), klasterska()):
        for mode in (PROMOTE, GATE, CLUSTER):
            assert not (new & set(names(pair[0], pair[1], S7, mode)))


def test_the_hold_holds_rentals_and_keeps_both_readings() -> None:
    a, b = stoka()
    assert "space_number" in names(a, b, S8_HOLD)
    assert S8_HOLD.merge_policy == {"pronajem|*": "propose"}


def test_the_settings_refuse_a_nonsense_space_mode() -> None:
    for bad in ("colive_price", "on", ""):
        try:
            variant(d43_space_numbers=bad)
        except ValueError:
            continue
        raise AssertionError(f"d43_space_numbers {bad} must not load")
