"""The candidate-town builder: pure, hermetic, no registry."""

from __future__ import annotations

import pytest

from location_data.town_candidates import (
    DEFAULT_RADIUS_KM,
    MAX_CANDIDATES,
    ObecIndex,
    ObecPoint,
    _word_matches,
    candidate_towns,
    haversine_km,
    load_obec_index,
    text_match_candidates,
)

CHEB = ObecPoint(code=554481, name="Cheb", lat=50.0796, lon=12.3740)
AS = ObecPoint(code=554499, name="Aš", lat=50.2239, lon=12.1950)          # ~20 km from Cheb
FRANTISKOVY = ObecPoint(code=554529, name="Františkovy Lázně", lat=50.1206, lon=12.3520)  # ~5 km
PRAHA = ObecPoint(code=554782, name="Praha", lat=50.0755, lon=14.4378)
BRNO = ObecPoint(code=582786, name="Brno", lat=49.1951, lon=16.6068)
KOLIN = ObecPoint(code=533165, name="Kolín", lat=50.0281, lon=15.2005)
NOVA_PAKA = ObecPoint(code=573248, name="Nová Paka", lat=50.4946, lon=15.5150)
LHOTA_A = ObecPoint(code=1001, name="Lhota", lat=50.10, lon=12.40)          # near Cheb
LHOTA_B = ObecPoint(code=1002, name="Lhota", lat=49.20, lon=16.60)          # near Brno
UNPLACED = ObecPoint(code=1003, name="Nikde", lat=None, lon=None)
POINTS = (CHEB, AS, FRANTISKOVY, PRAHA, BRNO, KOLIN, NOVA_PAKA, LHOTA_A, LHOTA_B, UNPLACED)


@pytest.fixture
def index() -> ObecIndex:
    return ObecIndex(POINTS)


@pytest.mark.parametrize("token, word, expected", [
    ("koline", "kolin", True),      # v Kolíně
    ("praze", "praha", True),       # v Praze — the stem consonant changes, the prefix holds
    ("pace", "paka", True),         # v Nové Pace — k/c alternation inside the stem
    ("chebu", "cheb", True),
    ("brne", "brno", True),
    ("boleslavi", "boleslav", True),
    ("boleslavskou", "boleslav", False),   # too long to be a form of the name
    ("kolo", "kolin", True),        # over-generation, accepted on purpose
    ("pate", "paka", False),        # t is not an alternation of k
    ("as", "as", True), ("asi", "as", False),   # short words match exactly
    ("nad", "nad", True), ("nade", "nad", False),
    ("ko", "kolin", False),         # a token shorter than the required prefix
])
def test_word_matching_is_declension_tolerant_but_bounded(token, word, expected):
    assert _word_matches(token, word) is expected


def test_text_matching_finds_declined_and_multi_word_names_in_order(index):
    text = ("Prodej bytu 3+1 v Kolíně, dojezd do Prahy 40 minut. Chata u Františkových "
            "Lázní, v Nové Pace, kousek od Chebu.")
    found = index.text_matches(text)
    assert found == ["Cheb", "Františkovy Lázně", "Kolín", "Nová Paka", "Praha"]
    assert text_match_candidates(text, [p.name for p in POINTS]) == found
    assert index.text_matches("") == [] and text_match_candidates("x", []) == []


def test_text_matching_folds_accents_and_case_and_dedupes():
    found = text_match_candidates("BYT V PRAZE. Praha 8. praha-karlin", ["Praha", "Aš"])
    assert found == ["Praha"]


def test_haversine_is_sane():
    assert abs(haversine_km(PRAHA.lat, PRAHA.lon, BRNO.lat, BRNO.lon) - 184) < 3
    assert haversine_km(CHEB.lat, CHEB.lon, CHEB.lat, CHEB.lon) == 0.0


def test_within_km_uses_centroids_and_skips_unplaced_obce(index):
    near = index.within_km(CHEB.lat, CHEB.lon, DEFAULT_RADIUS_KM)
    assert near == ["Cheb", "Františkovy Lázně", "Lhota"]
    assert "Aš" in index.within_km(CHEB.lat, CHEB.lon, 25)
    assert "Nikde" not in index.within_km(CHEB.lat, CHEB.lon, 10_000)


def test_candidate_towns_is_text_union_pin_radius_sorted_and_capped(index):
    towns = candidate_towns(index, text="byt v Praze", lat=CHEB.lat, lon=CHEB.lon)
    assert towns == ["Cheb", "Františkovy Lázně", "Lhota", "Praha"]
    # No pin: the text is the only source.
    assert candidate_towns(index, text="byt v Praze", lat=None, lon=None) == ["Praha"]
    # Nothing at all: an empty list, never an exception.
    assert candidate_towns(index, text="krásný byt", lat=None, lon=None) == []
    capped = candidate_towns(index, text="byt v Praze", lat=CHEB.lat, lon=CHEB.lon, cap=2)
    assert capped == ["Cheb", "Františkovy Lázně"] and MAX_CANDIDATES >= 300


def test_codes_and_the_nearest_homonym(index):
    assert index.codes_for_name("cheb") == [554481]
    assert index.codes_for_name("Lhota") == [1001, 1002]
    assert index.codes_for_name("Atlantis") == []
    assert index.nearest_code("Cheb", None, None) == 554481
    assert index.nearest_code("Lhota", None, None) is None          # ambiguous, no pin
    assert index.nearest_code("Lhota", CHEB.lat, CHEB.lon) == 1001
    assert index.nearest_code("Lhota", BRNO.lat, BRNO.lon) == 1002
    assert index.nearest_code("Atlantis", CHEB.lat, CHEB.lon) is None
    assert len(index) == len(POINTS) and index.names == sorted({p.name for p in POINTS})


class _Cursor:
    def __init__(self, rows):
        self.rows, self.executed = rows, []

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows


def test_load_obec_index_reads_one_query_and_tolerates_a_missing_centroid():
    cur = _Cursor([(554481, "Cheb", 50.08, 12.37), (1003, "Nikde", None, None)])
    index = load_obec_index(cur, version_id=7)
    assert cur.executed[0][1] == {"version": 7}
    assert "centroid_point" in cur.executed[0][0]
    assert index.names == ["Cheb", "Nikde"]
    assert index.within_km(50.08, 12.37, 1) == ["Cheb"]
