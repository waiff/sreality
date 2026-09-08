"""W2-10 candidates probe — the pure parts: text matching, relating picks to the
reference, and the report. Hermetic: no DB, no network, no provider."""

from __future__ import annotations

import pytest

from scripts.location_llm_bakeoff import CallRecord, _SAMPLE_SQL
from scripts.location_llm_candidates_probe import (
    DISTANCE_TIERS_KM,
    ProbeRow,
    _PROBE_SAMPLE_SQL,
    _word_matches,
    relate,
    summarise,
    summary_markdown,
    text_match_candidates,
)

NAMES = ["Aš", "Cheb", "Kolín", "Karlovy Vary", "Mladá Boleslav", "Praha", "Prachatice",
         "Ústí nad Labem", "Nová Ves", "Boleslav", "Vestec", "Brno"]


@pytest.mark.parametrize("token, word, expected", [
    ("koline", "kolin", True),      # v Kolíně
    ("praze", "praha", True),       # v Praze — the stem consonant changes, the prefix holds
    ("chebu", "cheb", True),
    ("brne", "brno", True),
    ("boleslavi", "boleslav", True),
    ("boleslavskou", "boleslav", False),   # too long to be a form of the name
    ("kolo", "kolin", True),        # over-generation, accepted on purpose
    ("as", "as", True), ("asi", "as", False),   # short words match exactly
    ("nad", "nad", True), ("nade", "nad", False),
    ("ko", "kolin", False),         # a token shorter than the required prefix
    ("pace", "paka", True),         # v Nové Pace — k/c alternation inside the stem
    ("nove", "nova", True),
    ("pate", "paka", False),        # t is not an alternation of k
])
def test_word_matching_is_declension_tolerant_but_bounded(token, word, expected):
    assert _word_matches(token, word) is expected


def test_text_matching_survives_stem_alternation():
    assert text_match_candidates("prodej bytu v Nové Pace", NAMES + ["Nová Paka"]) == ["Nová Paka"]


def test_text_matching_finds_declined_and_multi_word_names_in_order():
    text = ("Prodej bytu 3+1 v Kolíně, dojezd do Prahy 40 minut. Chata u Karlových Varů, "
            "kousek od Ústí nad Labem.")
    found = text_match_candidates(text, NAMES)
    assert "Kolín" in found and "Praha" in found
    assert "Karlovy Vary" in found
    assert "Ústí nad Labem" in found
    # Over-generation is bounded by length: "prahy" shares "pra" with Prachatice but a
    # ten-letter name needs eight shared characters, so Prachatice stays off the list.
    assert "Prachatice" not in found
    assert "Kolín" in text_match_candidates("prodám kolo", NAMES)   # the accepted kind
    # Multi-word names need every word, consecutively: "Nová Ves" is not in this text.
    assert "Nová Ves" not in found
    assert "Aš" not in found and "Vestec" not in found


def test_text_matching_folds_accents_and_case_and_returns_sorted_unique_names():
    found = text_match_candidates("BYT V PRAZE. Praha 8. praha-karlin", NAMES)
    assert found.count("Praha") == 1
    assert found == sorted(found)
    assert text_match_candidates("", NAMES) == []
    assert text_match_candidates("nic tu neni", []) == []


def test_the_probe_sample_is_the_bake_off_cohort_plus_the_pin():
    assert _PROBE_SAMPLE_SQL != _SAMPLE_SQL
    assert "ST_Y(l.geom::geometry), ST_X(l.geom::geometry)" in _PROBE_SAMPLE_SQL
    assert "Zahraničí" in _PROBE_SAMPLE_SQL


def _row(listing_id=1, **picks) -> ProbeRow:
    row = ProbeRow(model="a", listing_id=listing_id, url=f"u{listing_id}", title="t",
                   psc="35301", lat=50.0, lon=12.0,
                   sizes={"psc": 3, "text": 2, "pin": 10, "union": 11, "national": 5000})
    for arm, obec in picks.items():
        row.picks[arm] = {"obec": obec, "valid": obec is not None, "quote": obec,
                          "quote_valid": obec is not None, "confidence": "high"}
    return row


def test_relate_tests_the_reference_against_every_list_and_the_pin_tiers():
    row = _row(national="Cheb", psc="Cheb", union="Aš")
    relate(row, psc_list=["Cheb", "Aš"], text_list=["Cheb"], union_list=["Aš", "Cheb"],
           distances={"cheb": 3200.0, "as": 20000.0}, radius_km=25)
    assert row.reference == "Cheb" and row.reference_distance_km == 3.2
    assert row.membership["psc"] is True and row.membership["text"] is True
    assert row.membership["union"] is True
    assert row.membership["pin15"] is True and row.membership["pin40"] is True
    assert row.membership["pin_radius_25"] is True
    assert row.agreement == {"psc": True, "union": False}


def test_relate_distinguishes_no_list_from_outside_the_list():
    row = _row(national="Cheb")
    relate(row, psc_list=[], text_list=["Aš"], union_list=[], distances=None, radius_km=25)
    assert row.membership["psc"] is None and row.membership["union"] is None
    assert row.membership["text"] is False
    assert all(row.membership[f"pin{int(t)}"] is None for t in DISTANCE_TIERS_KM)
    assert row.reference_distance_km is None
    assert row.agreement == {"psc": None, "union": None}


def test_relate_reads_a_reference_beyond_reach_as_outside_every_pin_tier():
    row = _row(national="Cheb", psc=None)
    relate(row, psc_list=["Aš"], text_list=[], union_list=["Aš"],
           distances={"as": 1000.0}, radius_km=25)
    assert row.membership["psc"] is False
    assert row.membership["pin25"] is False and row.reference_distance_km is None
    assert row.agreement["psc"] is None


def test_relate_without_a_valid_reference_records_nothing():
    row = _row(national=None, psc="Cheb")
    relate(row, psc_list=["Cheb"], text_list=[], union_list=[], distances={}, radius_km=25)
    assert row.reference is None and row.membership == {} and row.agreement == {}
    invalid = _row(national="Nowhere")
    invalid.picks["national"]["valid"] = False
    relate(invalid, psc_list=["Cheb"], text_list=[], union_list=[], distances={},
           radius_km=25)
    assert invalid.reference is None


def _call(arm, listing_id, *, cost=0.001, error=None) -> CallRecord:
    return CallRecord(model="a", listing_id=listing_id, source_id_native=str(listing_id),
                      duration_ms=1000, input_tokens=100, output_tokens=10, cost_usd=cost,
                      error=error, arm=arm)


def test_summarise_reports_membership_rates_sizes_and_per_arm_agreement():
    r1 = _row(1, national="Cheb", psc="Cheb", union="Cheb")
    relate(r1, psc_list=["Cheb"], text_list=["Cheb"], union_list=["Cheb"],
           distances={"cheb": 0.0}, radius_km=25)
    r2 = _row(2, national="Aš", psc=None, union="Aš")
    relate(r2, psc_list=["Cheb"], text_list=["Aš"], union_list=["Aš"],
           distances={"as": 30000.0}, radius_km=25)
    r3 = _row(3, national=None)
    relate(r3, psc_list=[], text_list=[], union_list=[], distances=None, radius_km=25)
    calls = [_call("national", i) for i in (1, 2, 3)] + [_call("psc", 1), _call("psc", 2),
                                                         _call("union", 1), _call("union", 2)]
    report = summarise([r1, r2, r3], calls, ["a"], radius_km=25)
    m = report["per_model"]["a"]
    assert m["rows"] == 3 and m["with_reference"] == 2
    assert m["membership"]["psc"] == {"decided": 2, "inside": 1, "rate": 0.5}
    assert m["membership"]["text"] == {"decided": 2, "inside": 2, "rate": 1.0}
    assert m["membership"]["pin25"] == {"decided": 2, "inside": 1, "rate": 0.5}
    assert m["membership"]["pin40"]["inside"] == 2
    assert m["sizes"]["psc"]["p50"] == 3 and m["sizes"]["national"]["max"] == 5000
    psc = m["arms"]["psc"]
    assert psc["ran"] == 2 and psc["picked"] == 1
    assert psc["agrees_with_reference"] == {"comparable": 1, "agreed": 1, "rate": 1.0}
    assert m["arms"]["national"]["agrees_with_reference"] is None
    assert m["arms"]["union"]["agrees_with_reference"]["agreed"] == 2
    outside = m["outside_some_list"]
    assert [o["listing_id"] for o in outside] == [2]
    assert outside[0]["url"] == "u2" and outside[0]["in_pin25"] is False
    assert report["listing_count"] == 3 and len(report["rows"]) == 3


def test_the_summary_markdown_renders_every_section():
    r1 = _row(1, national="Cheb", psc="Cheb", union="Cheb")
    relate(r1, psc_list=["Cheb"], text_list=["Cheb"], union_list=["Cheb"],
           distances={"cheb": 0.0}, radius_km=25)
    report = summarise([r1], [_call("national", 1, cost=0.0)], ["a"], radius_km=25)
    report["seed"] = "w2-10"
    text = summary_markdown(report)
    assert "Candidate-list probe — 1 bazos listings" in text
    assert "in the PSČ-okres list" in text and "within 25 km of the pin" in text
    assert "| national ⚠️ UNPRICED |" in text
    assert "| psc |" in text and "| union |" in text
