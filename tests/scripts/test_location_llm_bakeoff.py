"""W2-10: the three-model bake-off's scoring, over canned model outputs.

Hermetic — no DB, no network, no provider. Everything here drives `score()`,
`evaluate_answer()` and `compare_values()` with hand-built observations, because the
numbers this script prints are what the operator picks a model on: a scoring bug that
flatters one model is indistinguishable from that model being better.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.location_llm_bakeoff import (
    RELATIONS,
    CallRecord,
    ChoiceObservation,
    FieldObservation,
    build_constrained_message,
    compare_values,
    evaluate_answer,
    evaluate_choice,
    psc_digits,
    relation_of,
    score,
    score_constrained,
    summary_markdown,
)
from location_data.claims_llm import (
    BLOCK_ORDER,
    FIELD_ORDER,
    PAGE_KIND,
    _validated_groups,
    llm_entries,
)
from location_data.html_scope import ScopeRegister, scope_html
from tests.location_data.claim_intake_fixtures import entries_for

_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (_ROOT / "tests" / "fixtures" / "location_w2" / "bazos_detail.html").read_bytes()
REGISTER = ScopeRegister.from_zones("bazos", (
    {"locator_kind": "html_selector", "locator": {"css": ".podobne, #podobne"}},
    {"locator_kind": "html_selector", "locator": {"css": "footer, .hlavicka"}},
))
# `scripts.location_llm_bakeoff.block_css` reads the two nodes off the DEPLOYED bazos
# contract, so this reads them off the same contract on disk. Pinning a second copy of the
# selectors here would let the harness and its test agree with each other while both drifted
# away from the page production reads.
BLOCK_CSS, _GROUPS = _validated_groups(llm_entries(entries_for("bazos"), PAGE_KIND))


class FakeGazetteer:
    def name_exists(self, name_norm): return name_norm in {"praha 8", "karlin"}
    def obec_codes_for_name(self, name_norm):
        return [554782] if name_norm == "praha 8" else []
    def street_in_obec(self, obec_kod, street_norm): return street_norm == "sokolovska"
    def address_point_exists(self, *, obec_kod, street_norm, cp, co): return cp == 234
    def obec_codes_for_psc(self, psc): return [554782] if psc == "18600" else []


def obs(model, listing_id, field, value, *, block="description", quote_valid=True,
        resolved=None, confidence="high") -> FieldObservation:
    return FieldObservation(
        model=model, listing_id=listing_id, block=block, field=field, value=value,
        quote=value, confidence=confidence, quote_valid=quote_valid, resolved=resolved)


def call(model, listing_id, *, duration_ms=1000, cost=0.001, error=None) -> CallRecord:
    return CallRecord(
        model=model, listing_id=listing_id, source_id_native=str(listing_id),
        duration_ms=duration_ms, input_tokens=1200, output_tokens=800, cost_usd=cost,
        error=error)


# ------------------------------------------------------------------ value comparison

@pytest.mark.parametrize("field,left,right,expected", [
    ("street", "Sokolovská", "sokolovska", True),
    ("street", "ulice Sokolovská", "Sokolovská", True),
    ("street", "náměstí Míru", "Míru", False),
    ("obec", "Praha 8", "praha 8", True),
    ("obec", "Praha 8", "Praha 9", False),
    ("house_number", "1216/46", "1216 / 46", True),
    ("house_number", "234", "235", False),
])
def test_two_models_are_compared_on_meaning_not_on_bytes(field, left, right, expected):
    assert compare_values(field, left, right) is expected


# ------------------------------------------------------------------ the report

def test_per_field_yield_is_measured_against_the_listings_actually_scored():
    observations = [
        obs("a", 1, "street", "Sokolovská", resolved=True),
        obs("a", 2, "street", None),
        obs("b", 1, "street", "Nový", resolved=False),
        obs("b", 2, "street", "Sokolovská", resolved=True),
    ]
    report = score(observations, [call("a", 1), call("b", 1)], ["a", "b"],
                   listing_count=2)
    assert report["per_model"]["a"]["fields"]["street"]["stated"] == 1
    assert report["per_model"]["a"]["fields"]["street"]["yield"] == 0.5
    assert report["per_model"]["b"]["fields"]["street"]["yield"] == 1.0
    # Resolution is scored over the GATED answers only — "not checked" and "checked and
    # failed" must never collapse into one number.
    assert report["per_model"]["b"]["fields"]["street"]["gazetteer_resolved"] == 0.5


def test_a_fabricated_citation_shows_up_as_a_quote_validity_miss():
    observations = [
        obs("a", 1, "street", "Sokolovská", quote_valid=True),
        obs("a", 2, "street", "Vymyšlená", quote_valid=False),
    ]
    report = score(observations, [call("a", 1)], ["a"], listing_count=2)
    assert report["per_model"]["a"]["quote_valid_rate"] == 0.5


def test_a_model_with_no_prices_row_is_flagged_rather_than_read_as_free():
    """A model whose whole run cost exactly nothing has NO `PRICES` row, and every
    downstream spend signal (llm_burn_rate's 24h total, llm_cost_today_usd, the lane's own
    --max-usd) is then lying about it."""
    report = score([obs("a", 1, "street", "Sokolovská")],
                   [call("a", 1, cost=0.0)], ["a"], listing_count=1)
    assert report["per_model"]["a"]["unpriced"] is True
    priced = score([obs("b", 1, "street", "Sokolovská")],
                   [call("b", 1, cost=0.002)], ["b"], listing_count=1)
    assert priced["per_model"]["b"]["unpriced"] is False


def test_a_failed_call_is_an_error_row_never_an_empty_answer():
    report = score([], [call("a", 1, error="429 rate limited")], ["a"], listing_count=1)
    assert report["per_model"]["a"]["errors"] == 1
    assert report["per_model"]["a"]["calls"] == 1
    assert report["per_model"]["a"]["latency_ms_p50"] is None
    assert len(report["errors"]) == 1
    assert "429" in report["errors"][0]["error"]


def test_the_agreement_matrix_is_computed_only_where_both_models_answered():
    observations = [
        obs("a", 1, "street", "Sokolovská"),
        obs("b", 1, "street", "sokolovska"),
        obs("a", 2, "street", "Nový"),
        obs("b", 2, "street", "Sokolovská"),
        obs("a", 3, "street", "Krymská"),
        # model b says nothing on listing 3 -> not comparable, not a disagreement
    ]
    report = score(observations, [], ["a", "b"], listing_count=3)
    street = report["agreement"]["a|b"]["street"]
    assert street["comparable"] == 2
    assert street["agreed"] == 1
    assert street["rate"] == 0.5
    assert len(report["disagreements"]) == 1
    assert report["disagreements"][0]["listing_id"] == 2


def test_the_agreement_matrix_compares_the_description_first_collapse():
    """The lane emits ONE claim per field, description-first. Scoring the blocks
    separately would report agreement on a value neither model would ever have claimed."""
    observations = [
        obs("a", 1, "street", "Sokolovská", block="description"),
        obs("a", 1, "street", "Krymská", block="title"),
        obs("b", 1, "street", "Sokolovská", block="description"),
        obs("b", 1, "street", "Nádražní", block="title"),
    ]
    report = score(observations, [], ["a", "b"], listing_count=1)
    assert report["agreement"]["a|b"]["street"]["rate"] == 1.0
    assert report["disagreements"] == []


def test_the_disagreement_list_is_capped():
    observations = []
    for listing_id in range(1, 11):
        observations.append(obs("a", listing_id, "street", f"A{listing_id}"))
        observations.append(obs("b", listing_id, "street", f"B{listing_id}"))
    report = score(observations, [], ["a", "b"], listing_count=10,
                   max_disagreements=3)
    assert len(report["disagreements"]) == 3


def test_latency_percentiles_and_cost_totals():
    calls = [call("a", i, duration_ms=d, cost=0.001)
             for i, d in enumerate([100, 200, 300, 400, 5000], start=1)]
    report = score([], calls, ["a"], listing_count=5)
    assert report["per_model"]["a"]["latency_ms_p50"] == 300
    assert report["per_model"]["a"]["latency_ms_p95"] == 5000
    assert report["per_model"]["a"]["cost_usd_total"] == 0.005
    assert report["cost"]["total_usd"] == 0.005
    assert report["cost"]["per_model_usd"]["a"] == 0.005


def test_an_empty_denominator_reports_none_not_zero():
    """A rate of 0/0 is not 0%. Printing one would read as "this model resolved nothing"
    when the truth is "nothing was asked of it"."""
    report = score([], [], ["a"], listing_count=0)
    assert report["per_model"]["a"]["quote_valid_rate"] is None
    assert report["per_model"]["a"]["gazetteer_resolved_rate"] is None
    assert report["per_model"]["a"]["fields"]["street"]["yield"] is None


# ------------------------------------------------------------------ evaluate_answer

def _blocks(**fields):
    from location_data.claims_llm import FIELD_CLAIM_TYPES
    return {name: fields.get(name) or {"value": None, "quote": None, "confidence": "low"}
            for name in FIELD_CLAIM_TYPES}


def test_evaluate_answer_uses_the_production_quote_and_gazetteer_checks():
    """A bake-off scored by a looser validator than production would pick the model that
    is best at fooling the looser validator."""
    document = scope_html(FIXTURE, register=REGISTER)
    nodes = {b: document.css_first(css) for b, css in BLOCK_CSS.items()}
    answer = {
        "from_description": _blocks(
            obec={"value": "Praha 8", "quote": "Praha 8 - Karlín", "confidence": "high"},
            street={"value": "Sokolovská", "quote": "Sokolovská 234",
                    "confidence": "high"},
            house_number={"value": "234", "quote": "Sokolovská 234",
                          "confidence": "high"}),
        "from_title": _blocks(
            street={"value": "Vymyšlená", "quote": "Vymyšlená 9", "confidence": "high"}),
    }
    observations = evaluate_answer(
        model="a", listing_id=1, answer=answer, document=document, nodes=nodes,
        gazetteer=FakeGazetteer(), obec_kod=554782)
    by_key = {(o.block, o.field): o for o in observations}
    assert len(observations) == len(FIELD_ORDER) * len(BLOCK_ORDER)

    street = by_key[("description", "street")]
    assert street.value == "Sokolovská" and street.quote_valid and street.resolved is True

    fabricated = by_key[("title", "street")]
    assert fabricated.value == "Vymyšlená"
    assert fabricated.quote_valid is False
    assert fabricated.resolved is False

    # A field with no registry gate reports `resolved=None`, never False.
    assert by_key[("description", "landmark")].resolved is None


def test_evaluate_answer_records_a_null_field_without_inventing_a_refusal():
    document = scope_html(FIXTURE, register=REGISTER)
    nodes = {b: document.css_first(css) for b, css in BLOCK_CSS.items()}
    observations = evaluate_answer(
        model="a", listing_id=1, answer={"from_description": _blocks(),
                                         "from_title": _blocks()},
        document=document, nodes=nodes, gazetteer=FakeGazetteer(), obec_kod=None)
    assert all(o.value is None for o in observations)
    assert all(o.resolved is None for o in observations)
    assert all(o.refusal is None for o in observations)


# ------------------------------------------------------------------ the summary

def test_the_markdown_summary_renders_every_model_and_flags_an_unpriced_one():
    report = score(
        [obs("a", 1, "street", "Sokolovská", resolved=True),
         obs("b", 1, "street", "Nový", resolved=False)],
        [call("a", 1, cost=0.002), call("b", 1, cost=0.0)],
        ["a", "b"], listing_count=1)
    report.update({"seed": "w2-10", "prompt_version": "bzs.loc@1"})
    text = summary_markdown(report)
    assert "`a`" in text and "`b`" in text
    assert "UNPRICED" in text
    for field in FIELD_ORDER:
        assert field in text
    assert "a|b" in text


# ------------------------------------------------------------------ the constrained arm

def test_psc_digits_accepts_the_spaced_form_and_rejects_anything_else():
    assert psc_digits("186 00") == "18600"
    assert psc_digits("18600") == "18600"
    assert psc_digits("1860") is None
    assert psc_digits(None) is None
    assert psc_digits("PSČ") is None


def test_the_constrained_message_carries_both_blocks_and_then_the_closed_list():
    text = build_constrained_message({"title": "T", "description": "D"}, ["Aš", "Cheb"])
    assert "TITULEK:\nT" in text and "POPIS:\nD" in text
    assert text.index("SEZNAM OBCÍ") > text.index("POPIS")
    assert text.endswith("\nAš\nCheb\n")


def _choice(**answer):
    document = scope_html(FIXTURE, register=REGISTER)
    nodes = {b: document.css_first(css) for b, css in BLOCK_CSS.items()}
    return evaluate_choice(
        model="a", listing_id=1, answer=answer, candidates=["Praha", "Říčany"],
        document=document, nodes=nodes)


def test_a_pick_is_valid_only_when_it_is_a_list_member():
    """The list is compared through the name normaliser: a case slip is not an invention,
    a town outside the list is — and it stays recorded, never silently dropped."""
    assert _choice(obec="Praha", quote="Sokolovská 234", confidence="high").valid is True
    assert _choice(obec="praha", quote="Sokolovská 234", confidence="high").valid is True
    outside = _choice(obec="Brno", quote="Sokolovská 234", confidence="high")
    assert outside.valid is False and outside.obec == "Brno"
    assert outside.candidates == 2


def test_an_abstention_is_neither_valid_nor_invalid():
    choice = _choice(obec=None, quote=None, confidence="low")
    assert choice.obec is None and choice.valid is None and choice.quote_valid is False
    blank = _choice(obec="   ", quote=None, confidence="low")
    assert blank.obec is None and blank.valid is None


def test_a_fabricated_quote_is_a_quote_validity_miss_in_the_constrained_arm_too():
    assert _choice(obec="Praha", quote="Sokolovská 234", confidence="high").quote_valid
    assert not _choice(obec="Praha", quote="nikde v textu", confidence="high").quote_valid
    assert not _choice(obec="Praha", quote=None, confidence="high").quote_valid


def test_an_unknown_confidence_reads_as_low_never_as_high():
    assert _choice(obec="Praha", quote="Sokolovská 234", confidence="sure").confidence == "low"
    assert _choice(obec="Praha", quote="Sokolovská 234", confidence=None).confidence == "low"


@pytest.mark.parametrize("pick, valid, free, resolved, expected", [
    (None, None, None, None, "neither"),
    (None, None, "Chebu", False, "free_only"),
    ("Cheb", True, None, None, "pick_only"),
    ("Cheb", True, "Cheb", True, "same"),
    ("Cheb", True, "cheb", True, "same"),
    ("Cheb", True, "Chebu", False, "free_unresolved_pick_valid"),
    ("Aš", True, "Cheb", True, "different"),
    # An invalid pick never counts as "the list fixed it", whatever the free arm did.
    ("Brno", False, "Chebu", False, "different"),
])
def test_relation_of(pick, valid, free, resolved, expected):
    assert relation_of(pick, valid, free, resolved) == expected
    assert expected in RELATIONS


def _pick(model, listing_id, obec, *, valid=True, confidence="high", relation=None,
          candidates=50) -> ChoiceObservation:
    return ChoiceObservation(
        model=model, listing_id=listing_id, candidates=candidates, obec=obec,
        valid=None if obec is None else valid, quote=obec,
        quote_valid=obec is not None, confidence=confidence, relation=relation)


def test_constrained_scoring_counts_picks_validity_and_pairwise_agreement():
    choices = [
        _pick("a", 1, "Cheb"), _pick("b", 1, "Cheb"),
        _pick("a", 2, "Aš"), _pick("b", 2, "Cheb"),
        _pick("a", 3, None), _pick("b", 3, "Cheb"),
        _pick("a", 4, None), _pick("b", 4, None),
        _pick("a", 5, "Brno", valid=False), _pick("b", 5, "Brno", valid=False),
    ]
    calls = ([call("a", i, cost=0.001) for i in range(1, 6)]
             + [call("b", i, cost=0.0) for i in range(1, 6)])
    section = score_constrained(
        choices, calls, ["a", "b"], listing_count=5, listings_without_candidates=1,
        titles={2: "Prodej domu v Aši"}, urls={2: "https://reality.bazos.cz/inzerat/2/x.php"})
    a = section["per_model"]["a"]
    assert a["scored"] == 5 and a["picked"] == 3 and a["abstained"] == 2
    assert a["valid_picks"] == 2 and a["invalid_picks"] == 1
    assert a["valid_rate"] == round(2 / 3, 4) and a["pick_rate"] == 0.6
    assert a["relations"] is None
    assert section["per_model"]["b"]["unpriced"] is True
    assert section["agreement"]["a|b"] == {
        "both_picked": 3, "agreed": 2, "rate": round(2 / 3, 4),
        "one_sided": 1, "both_abstained": 1,
    }
    listed = {d["listing_id"]: d for d in section["disagreements"]}
    assert set(listed) == {2, 3}
    assert listed[2]["url"].startswith("https://reality.bazos.cz/")
    assert listed[2]["title"] == "Prodej domu v Aši"
    assert listed[2]["a"] == "Aš" and listed[2]["b"] == "Cheb"
    assert listed[3]["a"] is None and listed[3]["b"] == "Cheb"
    assert section["listings_without_candidates"] == 1


def test_constrained_scoring_reports_the_relation_histogram_when_the_free_arm_ran():
    section = score_constrained(
        [_pick("a", 1, "Cheb", relation="same"),
         _pick("a", 2, "Cheb", relation="free_unresolved_pick_valid"),
         _pick("a", 3, None, relation="free_only")],
        [call("a", i) for i in (1, 2, 3)], ["a"], listing_count=3,
        listings_without_candidates=0, titles={}, urls={})
    relations = section["per_model"]["a"]["relations"]
    assert relations["same"] == 1 and relations["free_unresolved_pick_valid"] == 1
    assert relations["free_only"] == 1 and relations["different"] == 0


def test_the_summary_renders_the_constrained_section_next_to_the_free_arm():
    report = score([obs("a", 1, "obec", "Cheb", resolved=True)],
                   [call("a", 1, cost=0.001)], ["a"], listing_count=1)
    report.update({"seed": "w2-10", "prompt_version": "bzs.loc@1", "mode": "both",
                   "cost_all_arms_usd": 0.002})
    report["constrained"] = score_constrained(
        [_pick("a", 1, "Cheb", relation="same")], [call("a", 1, cost=0.001)], ["a"],
        listing_count=1, listings_without_candidates=0, titles={}, urls={})
    text = summary_markdown(report)
    assert "Per-field yield" in text
    assert "Constrained arm" in text and "bzs.loc.pick@1" in text
    assert "free_unresolved_pick_valid" in text
    assert "total $0.0020" in text


def test_a_constrained_only_run_does_not_render_an_empty_free_arm_table():
    report = score([], [], ["a"], listing_count=0)
    report.update({"seed": "s", "prompt_version": "bzs.loc@1", "mode": "constrained"})
    report["constrained"] = score_constrained(
        [_pick("a", 1, "Cheb")], [call("a", 1)], ["a"], listing_count=1,
        listings_without_candidates=0, titles={}, urls={})
    text = summary_markdown(report)
    assert "Per-field yield" not in text and "Pairwise agreement" not in text
    assert "Constrained arm" in text
    assert "— 1 bazos listings" in text
