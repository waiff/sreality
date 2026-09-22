import pytest

from scraper.attribute_contract import CONTRACT, floor_convention
from scraper.floor import (
    floor_from_portal,
    floor_from_text,
    is_plausible_floor,
    normalize_floor,
)


def test_is_plausible_floor():
    assert is_plausible_floor(3, None) is True
    assert is_plausible_floor(0, 6) is True
    assert is_plausible_floor(-1, None) is True       # suterén
    assert is_plausible_floor(None, 5) is False
    assert is_plausible_floor(8, 5) is False           # above the building total
    assert is_plausible_floor(99, None) is False       # out of band
    assert is_plausible_floor(-5, None) is False       # too deep
    # total_floors counts PODLAŽÍ including the ground storey, so under ground=0 the
    # top storey is total-1 and the count itself is one storey too high.
    assert is_plausible_floor(5, 6) is True
    assert is_plausible_floor(6, 6) is False
    assert is_plausible_floor(0, 1) is True


def test_normalize_floor_grammar():
    assert normalize_floor("3. patro") == 3
    assert normalize_floor("ve 4. patře") == 4
    assert normalize_floor("2. patra") == 2
    assert normalize_floor("přízemí") == 0
    assert normalize_floor("prizemi") == 0
    assert normalize_floor("suterén") == -1
    # NP is 1-indexed from ground: 1.NP = ground = 0.
    assert normalize_floor("1. NP") == 0
    assert normalize_floor("6. NP") == 5
    assert normalize_floor("4. nadzemní podlaží") == 3
    assert normalize_floor("6. nadzemním podlaží") == 5
    # PP is below ground.
    assert normalize_floor("1. PP") == -1
    assert normalize_floor("2. podzemní podlaží") == -2


def test_normalize_floor_rejects_unknown_and_bare_int():
    # A bare integer carries no convention -> never guessed.
    assert normalize_floor("4") is None
    assert normalize_floor("") is None
    assert normalize_floor(None) is None
    assert normalize_floor("mezonet") is None
    # The building ADJECTIVE must not read as a floor.
    assert normalize_floor("šestipodlažní") is None
    assert normalize_floor("6podlažní dům") is None


def test_floor_from_text_high_precision_cases():
    # Bare ordinal + patro.
    assert floor_from_text("Byt se nachází ve 4. patře bytového domu.") == (4, None)
    # NP form.
    assert floor_from_text("situovaný ve 4. nadzemním podlaží") == (3, None)
    # RK 'Podlaží:' label carrying an NP form.
    f, t = floor_from_text("Užitná plocha: 42 m2 Podlaží: 4. NP Parkování: vyhrazené")
    assert (f, t) == (3, None)
    # přízemí + a digit building total.
    assert floor_from_text("byt v přízemí 6podlažního domu") == (0, 6)
    # 'Podlaží celkem' total alongside a unit floor.
    assert floor_from_text("Podlaží: 2. patro Podlaží celkem: 6") == (2, 6)


def test_floor_from_text_building_total_trap():
    # Unit floor (digit ordinal noun) captured; building total (digit adjectival)
    # read only as total_floors — never as the floor.
    assert floor_from_text("v 6. patře šestipodlažního domu") == (6, None)
    # PATRA are storeys ABOVE the ground one; total_floors is a podlaží count, so the
    # patra-worded cues are +1 ("10 pater" = 11 podlaží, "6patrový" = 7).
    assert floor_from_text("ve 3. patře (z celkových 10 pater)") == (3, 11)
    assert floor_from_text("byt ve 2. patře 6patrového domu") == (2, 7)
    # The unit floor exceeding the stated building total is dropped (we grabbed a
    # building number), the total is kept.
    f, t = floor_from_text("Podlaží: 8. patro Podlaží celkem: 5")
    assert f is None and t == 5


def test_floor_from_text_defers_ambiguous_tail():
    # Spelled-out ordinals -> LLM, not regex.
    assert floor_from_text("v devátém patře udržovaného domu") == (None, None)
    # 'pater' (building total, no unit cue) yields only the total.
    assert floor_from_text("panelový dům o deseti patrech") == (None, None)
    # mezonet / no floor info.
    assert floor_from_text("mezonetový byt s galerií") == (None, None)
    assert floor_from_text("hezký byt 3+1 po rekonstrukci") == (None, None)
    # bare 'Podlaží: 7' (no NP/patro keyword) -> convention unknown -> deferred.
    assert floor_from_text("Podlaží: 7 Parkování: ne")[0] is None


def test_floor_from_text_empty():
    assert floor_from_text(None) == (None, None)
    assert floor_from_text("") == (None, None)


# --- the per-portal converter ------------------------------------------------


def test_floor_from_portal_ground1_shifts_only_positive_storeys():
    assert floor_from_portal("ground1", 1) == 0
    assert floor_from_portal("ground1", 3) == 2
    # sreality emits BOTH 0 and 1 for the ground storey ('zvýšené přízemí'), so a
    # blanket decrement would invent basements; -1 is suterén under either reading.
    assert floor_from_portal("ground1", 0) == 0
    assert floor_from_portal("ground1", -1) == -1
    assert floor_from_portal("ground1", "3") == 2
    assert floor_from_portal("ground1", None) is None


def test_floor_from_portal_ground0_is_a_passthrough():
    assert floor_from_portal("ground0", "2.") == 2
    assert floor_from_portal("ground0", 0) == 0
    assert floor_from_portal("ground0", "-1.") == -1
    # ceskereality's own out-of-band values survive untouched: correcting them is not
    # this conversion's job, and blanking a stated number is never a heal's to do.
    assert floor_from_portal("ground0", "126.") == 126


def test_the_word_wins_over_the_keys_convention():
    # A value that spells the storey out is the portal speaking about THAT advert.
    assert floor_from_portal("ground1", "přízemí") == 0
    assert floor_from_portal("ground0", "přízemí") == 0
    # 'zvýšené přízemí' is still the ground storey — the reading sreality's own
    # 4,597 floor=0 rows carry, and the reason those rows must not be decremented.
    assert floor_from_portal("ground1", "zvýšené přízemí") == 0
    assert floor_from_portal("ground1", "suterén") == -1
    assert floor_from_portal("word", "2. patro (3. NP)") == 2
    assert floor_from_portal("word", "-1. patro, suterén (1. PP)") == -1
    # 'snížené přízemí' is the ground storey the advert names, not the 1. PP its
    # parenthetical files it as.
    assert floor_from_portal("word", "snížené přízemí (1. PP)") == 0
    # A word portal that states no word states nothing: a bare int is not guessed.
    assert floor_from_portal("word", "7") is None


def test_a_bare_int_with_no_declared_convention_is_refused():
    with pytest.raises(ValueError):
        floor_from_portal(None, 3)


def test_the_conversion_is_idempotent_because_it_reads_the_source_not_the_column():
    # The heal re-derives from the stored payload; running it twice on the same
    # payload gives the same storey. `floor = floor - 1` would move on every pass.
    payload = 3
    once = floor_from_portal("ground1", payload)
    assert floor_from_portal("ground1", payload) == once == 2
    # And re-deriving is NOT re-applying: feeding the already-converted column back
    # would be the double-decrement bug, which no call site can express.
    assert floor_from_portal("ground1", once) == 1


def test_every_portal_declares_its_floor_convention():
    """The refusal above only binds if no cell can reach a parser without one."""
    missing = [p for p in CONTRACT if floor_convention(p) is None]
    assert missing == []
    assert floor_convention("ceskereality") == "ground0"
    assert floor_convention("idnes") == "word"
    assert {floor_convention(p) for p in
            ("sreality", "realitymix", "mmreality", "remax", "bezrealitky", "maxima")
            } == {"ground1"}


def test_a_storey_range_is_not_a_basement():
    # The hyphen between two ordinals is a separator, not a sign: "1.-2. patro" is a
    # maisonette on the 1st and 2nd storeys, and a bare `-?` read it as suterén-2.
    assert normalize_floor("1.-2. patro") == 2
    assert normalize_floor("2-3. patře") == 3
    assert floor_from_text("Podlaží: 1.-2. patro") == (2, None)
    # The one value that really does carry a sign still reads it (idnes).
    assert normalize_floor("-1. patro") == -1


def test_an_out_of_band_word_is_refused_not_re_read_as_a_bare_int():
    # "45. patro" is out of the plausibility band. Falling through to the key's
    # convention would decrement it to 44 — the OPPOSITE scale, on the one value that
    # already stated its own.
    assert floor_from_portal("ground1", "45. patro") is None
    assert floor_from_portal("ground1", "-9. patro") is None


def test_the_label_arm_reads_its_own_value_not_the_next_clause():
    # The spec-label capture runs past the value into the neighbouring text; a
    # 'přízemí' in that tail must not beat the label's own explicit storey.
    assert floor_from_text("Podlaží: 3. NP, přízemí s garáží") == (2, None)
    assert floor_from_text("Podlaží: přízemí, výtah") == (0, None)
