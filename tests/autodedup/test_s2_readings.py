"""W17 / S2: every E160-E165 reading, on real advert text, positive and negative.

Each case is a sentence a Czech portal actually published, taken from the pair it was found on
— the cohort-3 and cohort-4 residues and the region skeptic's named false splits. A reading
that only works on text somebody wrote for it is a reading that has not been measured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodedup.body_align import aligned_difference, mask
from autodedup.dataset import Listing, Location
from autodedup.demonstrate import (
    area_demonstrated,
    demonstration_shortfall,
    development_context,
    onesided_fact,
    price_demonstrated,
    strong_corroboration,
)
from autodedup.indistinguishable import (
    distinguishing_facts,
    printed_area_conflict_cfg,
    two_unit_signature,
)
from autodedup.settings import Settings
from autodedup.text_facts import printed_unit_codes

SETTINGS_DIR = Path(__file__).resolve().parents[2] / "autodedup" / "settings"


def s2() -> Settings:
    return Settings.from_json(SETTINGS_DIR / "w17.json")


def shipped() -> Settings:
    return Settings.from_json(SETTINGS_DIR / "w16_s.json")


def listing(
    listing_id: int,
    *,
    description: str = "",
    price: float | None = None,
    area: float | None = None,
    source: str = "sreality",
    floor: int | None = None,
    disposition: str | None = "3+kk",
    obec: int | None = 554782,
    first: str = "2026-01-01T00:00:00",
    last: str = "2026-03-01T00:00:00",
    inactive: str | None = None,
    price_history: tuple[tuple[str, float], ...] = (),
) -> Listing:
    return Listing(
        id=listing_id,
        block="b",
        source=source,
        description=description,
        price=price,
        area_m2=area,
        disposition=disposition,
        floor=floor,
        category_main="byt",
        category_type="prodej",
        location=Location(obec_kod=obec, obec_name="Plzeň"),
        first_seen_at=first,
        last_seen_at=last,
        inactive_at=inactive,
        price_history=list(price_history),
    )


# --- E161: the printed unit code, whole and bare -------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        # Slavonínské zahrady, the fusion every W16 arm carried: the keyword reader truncated
        # `B2.2.1` to `B2` and read the bare form not at all.
        ("Nabízíme byt s označením B2.2.1 o dispozici 3+kk.", {"B2.2.1"}),
        ("Byt B1.2.1 o dispozici 3+kk v novostavbě.", {"B1.2.1"}),
        # cohort 3, the pairs the skeptic's probe separates.
        ("bytová jednotka F2.103 4+kk je orientována na jihozápad", {"F2.103"}),
        ("Prodej bytu H2-204 v nové etapě", {"H2-204"}),
        ("Nabízíme mezonetový byt 3+kk č. 2.07", {"2.07"}),
    ],
)
def test_printed_unit_code_is_read_whole(text: str, expected: set[str]) -> None:
    assert set(printed_unit_codes(text)) == expected


@pytest.mark.parametrize(
    "text",
    [
        # A single segment is a BUILDING, and every flat in it shares it.
        "Byt v domě B je k dispozici.",
        "Prodej bytu označení B36 v centru.",
        # A disposition is not a code.
        "Nabízíme jednotku 4+kk s balkonem v novém projektu.",
        # An area is not a code.
        "Prodej bytu 58,90 m2 po rekonstrukci v cihlovém domě.",
    ],
)
def test_printed_unit_code_reads_nothing_it_should_not(text: str) -> None:
    assert printed_unit_codes(text) == frozenset()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Prodáváme vilu II v uzavřeném areálu.", {"II"}),
        ("Nabízíme dům číslo tři z pěti dokončených.", {"III"}),
        ("K dispozici je jednotka označením A ve vstupním podlaží.", {"A"}),
    ],
)
def test_wide_unit_forms_need_their_marker(text: str, expected: set[str]) -> None:
    assert set(printed_unit_codes(text, wide=True)) == expected
    assert printed_unit_codes(text) == frozenset()


@pytest.mark.parametrize(
    "text",
    [
        # Czech grammar, not a unit name: `i` is a conjunction and `a` joins two things.
        "Prodáváme dům i zahradu v klidné části obce.",
        "K bytu a garáži patří také sklep.",
    ],
)
def test_wide_unit_forms_do_not_read_conjunctions(text: str) -> None:
    assert printed_unit_codes(text, wide=True) == frozenset()


def test_unit_code_conflict_is_a_fact() -> None:
    cfg = s2()
    a = listing(1, description="Byt B1.2.1 o dispozici 3+kk v novostavbě Slavonínské zahrady.")
    b = listing(2, description="Byt s označením B2.2.1 o dispozici 3+kk, novostavba.")
    assert "unit_code" in {fact.name for fact in distinguishing_facts(a, b, None, cfg)}
    # And the arm that shipped could not see it.
    assert "unit_code" not in {
        fact.name for fact in distinguishing_facts(a, b, None, shipped())
    }


def test_unit_code_agreement_is_not_a_fact() -> None:
    cfg = s2()
    a = listing(1, description="Byt B1.2.1 o dispozici 3+kk v novostavbě.")
    b = listing(2, description="Byt s označením B1.2.1, 3+kk, novostavba, k nastěhování.")
    assert "unit_code" not in {fact.name for fact in distinguishing_facts(a, b, None, cfg)}


# --- E160: exactness where identity is claimed ---------------------------------------------

def test_price_within_five_percent_is_no_longer_a_demonstration() -> None:
    """Kozolupy: 11,250,000 and 11,500,000 sat in one cluster of 25 under the 5 % bar."""
    a = listing(1, price=11_250_000.0, source="sreality")
    b = listing(2, price=11_500_000.0, source="idnes")
    assert price_demonstrated(a, b, shipped(), False, 60.0)
    assert not price_demonstrated(a, b, s2(), False, 60.0)


def test_an_exact_price_still_demonstrates() -> None:
    a = listing(1, price=11_250_000.0, source="sreality")
    b = listing(2, price=11_250_000.0, source="idnes")
    assert price_demonstrated(a, b, s2(), False, 60.0)


def test_a_price_on_the_other_s_path_still_demonstrates() -> None:
    """A cut is one unit at two moments — the path, not a tolerance, is what says so."""
    a = listing(1, price=4_800_000.0, price_history=(("2026-01-01T00:00:00", 5_000_000.0),))
    b = listing(2, price=5_000_000.0, source="idnes")
    assert price_demonstrated(a, b, s2(), True, 30.0)


def test_two_printed_decimals_that_differ_are_two_areas() -> None:
    """Chotěšov: 75,52 m² and 75,64 m², both portals storing 76, fused under the union."""
    a = listing(1, description="Prodej domu o podlahové ploše 75,52 m2 se zahradou.", area=76.0)
    b = listing(2, description="Prodej domu o podlahové ploše 75,64 m2 se zahradou.", area=76.0)
    assert area_demonstrated(a, b, printed_decides=False)
    assert not area_demonstrated(a, b, printed_decides=True)
    assert printed_area_conflict_cfg(a, b, shipped()) is None
    assert printed_area_conflict_cfg(a, b, s2()) is not None


def test_an_integer_equals_a_decimal_that_rounds_to_it() -> None:
    a = listing(1, description="Byt o podlahové ploše 75,5 m2 s balkonem a sklepem.")
    b = listing(2, description="Byt o podlahové ploše 75 m2 s balkonem a sklepem.")
    assert area_demonstrated(a, b, printed_decides=True)


def test_the_body_is_read_where_the_stored_column_is_degenerate() -> None:
    """bazos stores the terrace: `area_m2 = 10.0` for a 76 m² apartment."""
    body = "Prodej bytu o podlahové ploše 76,1 m2 s terasou o velikosti 10 m2."
    a = listing(1, description=body, area=10.0, source="bazos")
    b = listing(2, description="Prodej bytu o podlahové ploše 76,1 m2 s terasou.", area=76.0)
    assert area_demonstrated(a, b, printed_decides=True)


# --- E163: the healed generic reader --------------------------------------------------------

HEALED = (
    # A charge, in the spellings the shipped mask misses.
    ("Provize RK: 12.000.- Kč + 21 % DPH.", "Provize RK: 13.200.- Kč + 21 % DPH."),
    ("Cena, provize RK, náklady na bydlení 5500.", "Cena, provize RK, náklady na bydlení 4000."),
    ("Volný ihned za 19tis.kč + el., a voda, kauce jeden.",
     "Volný ihned za 20tis.kč + el., a voda, kauce jeden."),
    ("The monthly rent for the apartment is CZK 41,500 per month.",
     "The monthly rent for the apartment is CZK 39,900 per month."),
    # The term of the contract.
    ("Minimální délka nájmu činí 3 měsíce, realitní kanceláře nevolat.",
     "Minimální délka nájmu činí 12 měsíců, realitní kanceláře nevolat."),
    ("Splátka anuity bytu je nyní nastavena na 35 let, není třeba hypotéka.",
     "Splátka anuity bytu je nyní nastavena na 30 let, není třeba hypotéka."),
    # A date with no year.
    ("Byt je k nastěhování od 1.7. a uvedená cena nájmu platí při počtu 2 osob.",
     "Byt je k nastěhování od 1.10. a uvedená cena nájmu platí při počtu 2 osob."),
    # An order code the population cap is too short to mask.
    ("Rád Vám vše sdělí na uvedených kontaktech, ev.č. 0831, třída energetické náročnosti E.",
     "Rád Vám vše sdělí na uvedených kontaktech, ev.č. 0883, třída energetické náročnosti E."),
    # An inventory multiplier: the rooms of a whole house.
    ("Schodištěm se vchází do II. NP, kde je kuchyň s obývacím pokojem, 6x pokoj a koupelny.",
     "Schodištěm se vchází do II. NP, kde je kuchyň s obývacím pokojem, 5x pokoj a koupelny."),
    # A length in metres: the building's dimensions, a garage door's width.
    ("Objekt je dlouhý 79 m a široký 10 m, v některých místech 14 m.",
     "Objekt je dlouhý 80 m a široký 10 m, v některých místech 14 m."),
    # A range one portal printed without its hyphen.
    ("Výstavba rodinných domů, cca 6 samostatných domů cca 150-180m² na pozemcích.",
     "Výstavba rodinných domů, cca 6 samostatných domů cca 150180m² na pozemcích."),
    # One storey, two vocabularies.
    ("Byt se nachází ve 2. NP cihlového domu s výtahem a vlastním sklepem.",
     "Byt se nachází v 1. patře cihlového domu s výtahem a vlastním sklepem."),
    # A full stop the tokeniser glued to a motorway's name.
    ("Blízké napojení na dálnici D5.Více informací u makléře kdykoliv.",
     "Blízké napojení na dálnici D5 více informací u makléře kdykoliv."),
)

STILL_READ = (
    # Two plots of one parcelling.
    ("Nabízíme k prodeji stavební pozemek s označením 8 o výměře 1081 m2, který vznikne.",
     "Nabízíme k prodeji stavební pozemek s označením 9 o výměře 1081 m2, který vznikne."),
    # Two parking spaces in one underground garage.
    ("Výzva na nabídku nejvyššího nájemného za pronájem garážového stání prostoru č. 260 v objektu.",
     "Výzva na nabídku nejvyššího nájemného za pronájem garážového stání prostoru č. 255 v objektu."),
    # Two areas, printed.
    ("Nabízíme k prodeji světlý byt 2+1 o užitné ploše 55 m2 na Masarykově třídě v Olomouci.",
     "Nabízíme k prodeji světlý byt 2+1 o užitné ploše 51 m2 na Masarykově třídě v Olomouci."),
)


def _pad(text: str) -> str:
    """The reader needs `MIN_TOKENS` before it looks at anything; the filler is identical."""
    filler = (" Dům je po celkové rekonstrukci, plastová okna, nová elektroinstalace, "
              "podlahy, dveře, koupelna a kuchyňská linka jsou zcela nové a připravené.")
    return text + filler * 2


@pytest.mark.parametrize("left,right", HEALED)
def test_the_heal_closes_the_named_false_splits(left: str, right: str) -> None:
    left, right = _pad(left), _pad(right)
    assert aligned_difference(left, right, 0.6, heal=False) is not None
    assert aligned_difference(left, right, 0.6, heal=True) is None


@pytest.mark.parametrize("left,right", STILL_READ)
def test_the_heal_keeps_every_reading_that_names_a_unit(left: str, right: str) -> None:
    left, right = _pad(left), _pad(right)
    assert aligned_difference(left, right, 0.6, heal=True) is not None


def test_the_storey_is_left_to_its_own_fact() -> None:
    """E166: reading a storey inside `body_align` newly split 126 certain duplicates."""
    left = _pad("Byt o výměře 52 m2, balkón, se nachází ve 3.NP domu B s výtahem.")
    right = _pad("Byt o výměře 52 m2, balkón, se nachází ve 4.NP domu B s výtahem.")
    assert aligned_difference(left, right, 0.6, heal=True) is None
    a = listing(1, description=left, source="idnes")
    b = listing(2, description=right, source="idnes")
    names = {fact.name for fact in distinguishing_facts(a, b, None, s2())}
    assert "prose_floor" in names
    assert "prose_floor" not in {
        fact.name for fact in distinguishing_facts(a, b, None, shipped())
    }


def test_np_and_patro_are_one_storey_for_the_prose_fact() -> None:
    a = listing(1, description=_pad("Byt se nachází ve 2. NP cihlového domu s výtahem."))
    b = listing(2, description=_pad("Byt se nachází v 1. patře cihlového domu s výtahem."))
    assert "prose_floor" not in {
        fact.name for fact in distinguishing_facts(a, b, None, s2())
    }


def test_a_one_storey_gap_across_portals_is_vocabulary_not_a_fact() -> None:
    a = listing(1, description=_pad("Byt se nachází ve 2. NP cihlového domu."), source="idnes")
    b = listing(2, description=_pad("Byt se nachází ve 3. NP cihlového domu."),
                source="bezrealitky")
    assert "prose_floor" not in {
        fact.name for fact in distinguishing_facts(a, b, None, s2())
    }


def test_the_heal_leaves_square_metres_alone() -> None:
    healed = mask("Plocha 52 m² a pozemek 265 m², objekt dlouhý 79 m.", heal=True)
    assert "52" in healed and "265" in healed and "[ROZMER]" in healed


def test_np_and_patro_land_on_one_scale() -> None:
    assert mask("byt ve 2. NP", heal=True).split()[-1] == mask(
        "byt v 1. patře", heal=True).split()[-1]
    assert mask("byt ve 3. NP", heal=True).split()[-1] != mask(
        "byt ve 4. NP", heal=True).split()[-1]


# --- E162: a one-sided fact inside a NARROW development ------------------------------------

NEW_BUILD = ("Novostavba v projektu Rezidence, developer nabízí byty ke kolaudaci letos. "
             "Kvalitní materiály, vlastní parkování a sklepní kóje jsou samozřejmostí.")


def test_development_context_is_not_the_whole_corpus() -> None:
    """`projekt`/`novostavba` on ONE side is 41.8 % of cohort-3 certain pairs, not a context."""
    cfg = s2()
    a = listing(1, description=NEW_BUILD)
    b = listing(2, description="Prodej bytu 3+kk v cihlovém domě po rekonstrukci, klidná ulice.")
    assert not development_context(a, b, cfg)
    vocab = s2()
    vocab.development_context_mode = "vocab"
    assert development_context(a, b, vocab)


def test_development_context_needs_the_project_to_name_a_unit() -> None:
    cfg = s2()
    a = listing(1, description=NEW_BUILD)
    b = listing(2, description=NEW_BUILD)
    assert not development_context(a, b, cfg)
    named = listing(3, description=NEW_BUILD + " Byt s označením B1.2.1 je stále volný.")
    assert development_context(named, b, cfg)


def test_a_silent_side_fails_closed_inside_a_development() -> None:
    cfg = s2()
    a = listing(1, description=NEW_BUILD + " Byt s označením B1.2.1 je stále volný.")
    b = listing(2, description=NEW_BUILD + " Tento byt je stále volný k nastěhování.")
    assert onesided_fact(a, b, cfg) == "code"


def test_a_silent_side_is_tolerated_outside_one() -> None:
    cfg = s2()
    a = listing(1, description="Prodej bytu, jednotka B1.2.1, po rekonstrukci, klidná ulice.")
    b = listing(2, description="Prodej bytu po rekonstrukci v klidné ulici, volný ihned.")
    assert onesided_fact(a, b, cfg) is None


# --- E164: what is safe to recover from a missing reading ----------------------------------

def _feats(**values: float) -> dict[str, tuple[float, bool]]:
    return {name: (value, True) for name, value in values.items()}


def test_a_missing_reading_is_named_as_missing() -> None:
    cfg = s2()
    a = listing(1, price=None, area=70.0, description="Prodej bytu 3+kk o ploše 70 m2 v centru.")
    b = listing(2, price=None, area=70.0, description="Prodej bytu 3+kk o ploše 70 m2 v centru.")
    shortfall = demonstration_shortfall(a, b, cfg, False, 30.0)
    assert shortfall == ("price", "missing")


BODY = "Prodej bytu 3+kk v cihlovém domě po celkové rekonstrukci. " * 6


def test_three_tight_photo_files_recover_a_missing_reading() -> None:
    cfg = s2()
    a = listing(1, description=BODY)
    b = listing(2, description=BODY)
    assert strong_corroboration(a, b, _feats(phash_tight_matches=3.0), cfg) == "photos:3"
    assert strong_corroboration(a, b, _feats(phash_tight_matches=2.0), cfg) is None


def test_a_weak_interior_match_refuses_the_photo_recovery() -> None:
    cfg = s2()
    a, b = listing(1, description=BODY), listing(2, description=BODY)
    feats = _feats(phash_tight_matches=4.0, tag_room_clip_min2=0.5)
    assert strong_corroboration(a, b, feats, cfg) is None


def test_an_advert_that_states_nothing_demonstrates_nothing() -> None:
    """One ceskereality row of a Slavonín house carries an empty body and no price."""
    cfg = s2()
    a = listing(1, description=BODY)
    b = listing(2, description="")
    assert strong_corroboration(a, b, _feats(phash_tight_matches=6.0), cfg) is None
    # ...but the seller's own order code names ONE object and needs no prose beside it.
    assert strong_corroboration(a, b, _feats(ref_code_shared=1.0), cfg) == "code"


def test_a_shared_order_code_recovers_a_missing_reading() -> None:
    cfg = s2()
    assert strong_corroboration(listing(1, description=BODY), listing(2, description=BODY),
                                _feats(ref_code_shared=1.0), cfg) == "code"


def test_a_shared_body_recovers_only_outside_a_development() -> None:
    cfg = s2()
    long_body = "Prodej bytu 3+kk v cihlovém domě. " * 20
    a = listing(1, description=long_body)
    b = listing(2, description=long_body)
    assert strong_corroboration(a, b, _feats(containment_max=0.99), cfg) == "body"
    dev_a = listing(3, description=NEW_BUILD + " Byt s označením B1.2.1. " + long_body)
    dev_b = listing(4, description=NEW_BUILD + " " + long_body)
    assert strong_corroboration(dev_a, dev_b, _feats(containment_max=0.99), cfg) is None


def test_a_contradiction_is_never_recovered() -> None:
    cfg = s2()
    a = listing(1, price=9_000_000.0, description="Prodej bytu 3+kk o ploše 70 m2.", area=70.0)
    b = listing(2, price=7_000_000.0, description="Prodej bytu 3+kk o ploše 70 m2.", area=70.0)
    shortfall = demonstration_shortfall(a, b, cfg, False, 60.0)
    assert shortfall is not None and shortfall[1] == "contradiction"


# --- E165: E143 reads two numbers as simultaneous -------------------------------------------

def test_a_price_cut_between_sequential_postings_is_not_two_units() -> None:
    cfg = s2()
    a = listing(1, price=5_000_000.0, area=74.0,
                first="2026-01-01T00:00:00", last="2026-02-02T00:00:00",
                inactive="2026-02-02T00:00:00")
    b = listing(2, price=4_700_000.0, area=76.0, source="idnes",
                first="2026-02-01T00:00:00", last="2026-04-01T00:00:00")
    assert two_unit_signature(a, b, shipped())
    assert not two_unit_signature(a, b, cfg)


def test_two_co_live_adverts_still_carry_the_signature() -> None:
    cfg = s2()
    a = listing(1, price=5_000_000.0, area=74.0,
                first="2026-01-01T00:00:00", last="2026-06-01T00:00:00")
    b = listing(2, price=4_700_000.0, area=76.0, source="idnes",
                first="2026-01-02T00:00:00", last="2026-06-01T00:00:00")
    assert two_unit_signature(a, b, cfg)


# --- the replay pins ------------------------------------------------------------------------

def test_every_shipped_arm_is_unchanged_by_the_w17_fields() -> None:
    """Every new field defaults to the value the shipped arms ran under."""
    new = (
        "demonstrate_price_exact", "demonstrate_area_printed_decides",
        "d43_printed_area_decimals_decide", "d43_unit_codes", "d43_unit_codes_wide",
        "demonstrate_onesided", "d43_body_align_heal", "demonstrate_recover_missing",
        "d43_two_unit_requires_colive", "d43_prose_floor",
    )
    for name in ("w13", "w14", "w15", "w16_s", "w16_m", "w16_l"):
        row = json.loads((SETTINGS_DIR / f"{name}.json").read_text())
        cfg = Settings.from_json(SETTINGS_DIR / f"{name}.json")
        for field in new:
            assert field not in row, f"{name}.json must not carry {field}"
            assert getattr(cfg, field) is False, f"{name} must not switch {field} on"
        assert cfg.development_context_mode == "off"


def test_w17_is_the_s_arm_plus_the_new_readings() -> None:
    s_row = json.loads((SETTINGS_DIR / "w16_s.json").read_text())
    s2_row = json.loads((SETTINGS_DIR / "w17.json").read_text())
    for key, value in s_row.items():
        if key in s2_row and key not in ("demonstrate_price_exact_tol",):
            assert s2_row[key] == value or key in {
                "demonstrate_price_exact", "demonstrate_area_printed_decides",
                "d43_printed_area_decimals_decide", "d43_unit_codes", "d43_unit_codes_wide",
                "demonstrate_onesided", "development_context_mode", "d43_body_align_heal",
                "demonstrate_recover_missing", "d43_two_unit_requires_colive",
                "d43_prose_floor",
            }, key
    cfg = Settings.from_json(SETTINGS_DIR / "w17.json")
    assert cfg.development_context_mode == "narrow"
    assert cfg.demonstrate_price_exact and cfg.d43_unit_codes and cfg.d43_body_align_heal
