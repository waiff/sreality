from scraper.floor import floor_from_text, is_plausible_floor, normalize_floor


def test_is_plausible_floor():
    assert is_plausible_floor(3, None) is True
    assert is_plausible_floor(0, 6) is True
    assert is_plausible_floor(-1, None) is True       # suterén
    assert is_plausible_floor(None, 5) is False
    assert is_plausible_floor(8, 5) is False           # above the building total
    assert is_plausible_floor(99, None) is False       # out of band
    assert is_plausible_floor(-5, None) is False       # too deep


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
    assert floor_from_text("ve 3. patře (z celkových 10 pater)") == (3, 10)
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
