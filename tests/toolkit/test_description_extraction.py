"""The text lane's rails, all DB-less (field-capture W7).

Every one of these is a defect the DELETED lane actually shipped: a selector keyed on a
column that was NULL for 99.6 % of its target, a write that overwrote and left no property
mark, a `false` inferred from silence at 92.9 % precision, a floor the model converted
itself at ~73 %, and enums stated in prose so a condition reached `building_type`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scraper import attribute_contract as contract
from toolkit import description_extraction as tx

DESCRIPTION = (
    "Prodám byt 2+kk ve 3. patře cihlového domu po rekonstrukci. "
    "V domě bohužel bez výtahu, k bytu náleží balkon. "
    "Dům má celkem 5 pater, energetická třída C."
)


def _cell(value: Any, quote: str | None) -> dict[str, Any]:
    return {"value": value, "evidence_quote": quote}


# --- the selector (R8) -------------------------------------------------------


def test_the_lane_and_the_check_share_one_predicate() -> None:
    """The critical invariant. The 2026-07 outage was a lane and its monitors disagreeing
    about who was eligible; a re-spelled predicate recreates it."""
    where = tx._eligible_where(contract.extracted_cells())
    assert where in tx.SELECT_INFLOW_SQL
    assert where in tx.ELIGIBLE_LAG_SQL


def test_the_selector_keys_on_listing_id_and_never_on_sreality_id() -> None:
    sql = tx._DECLARED_SELECT_SQL
    assert "sreality_id" not in sql
    assert "e.listing_id = l.id" in sql
    assert "e.extractor_version = %(version)s" in sql


def test_the_selector_covers_every_extractable_cell_of_every_declared_portal() -> None:
    """Over the DECLARED gates, because with every gate closed the live predicate is
    `false` — which is the shipping state and the point of the wave."""
    sql = tx._DECLARED_SELECT_SQL
    declared = contract.gated_cells()
    assert declared, "the contract declares no gated text cell at all"
    for portal, fields in declared.items():
        assert f"l.source = '{portal}'" in sql
        for field in fields:
            assert f"l.{field} IS NULL" in sql


OPEN_GATES = {"bazos": ("floor", "has_lift")}


def test_the_lane_extracts_exactly_the_open_gates() -> None:
    """The lane's scope is the OPEN gates and nothing else: a closed gate is not asked
    for, not billed, not written. The draft that extracted every gated cell and wrote
    only the passed ones would have paid ~$113 for cache rows that, because a cache row
    retires its listing, could never have become a column value. The W7 bake-off
    (2026-09-22) opened floor + has_lift on bazos; the selector names those two columns."""
    assert contract.extracted_cells() == OPEN_GATES
    assert "WHERE false" not in tx.SELECT_INFLOW_SQL
    for field in OPEN_GATES["bazos"]:
        assert field in tx.SELECT_INFLOW_SQL
    for field in ("condition", "building_type", "energy_rating", "has_parking"):
        assert field not in tx.SELECT_INFLOW_SQL


def test_the_hash_stays_out_of_the_index_condition() -> None:
    """`IS NOT DISTINCT FROM`, not `=`: a plain equality lets the planner make the hash an
    index condition, which detoasts and hashes all ~50k descriptions every pass forever,
    including the steady state where nothing is eligible."""
    assert "text_hash IS NOT DISTINCT FROM" in tx._DECLARED_SELECT_SQL
    assert "text_hash =" not in tx._DECLARED_SELECT_SQL


def test_one_arm_newest_first_reaches_the_backlog() -> None:
    """An extracted row leaves the predicate, so DESC walks BACKWARDS through a backlog at
    PASS_SLICE a pass (~57k a day against a 1,940-a-day inflow). The oldest-first arm an
    earlier draft added bought nothing and cost a measured 9.4-10.0 s per run."""
    assert "ORDER BY l.first_seen_at DESC" in tx.SELECT_INFLOW_SQL
    assert not hasattr(tx, "SELECT_BACKLOG_SQL")
    assert not hasattr(tx, "BACKLOG_EVERY_PASSES")


def test_a_pass_issues_exactly_one_select() -> None:
    seen: list[str] = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None): seen.append(sql)
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _Cur()

    tx.select_eligible(_Conn(), version="v")
    assert seen == [tx.SELECT_INFLOW_SQL]


def test_the_extractor_version_carries_the_model() -> None:
    """Migration 249's lesson, preserved through the re-key: a cached answer is an answer
    from a particular model, so a model swap must re-attempt rather than serve the old."""
    assert tx.extractor_version("gpt-5-mini") != tx.extractor_version("gpt-5.6-luna")
    assert "gpt-5-mini" in tx.extractor_version("gpt-5-mini")


def test_the_extractor_version_carries_the_open_gate_set(monkeypatch) -> None:
    """Without this, opening a gate would write NOTHING to the existing corpus: every one
    of those listings already has a cache row at this version, and the anti-join retires
    it for ever. The fingerprint is what re-opens them."""
    closed = tx.extractor_version("m")
    monkeypatch.setattr(contract, "extracted_cells",
                        lambda: {"bazos": ("has_lift",)})
    one_open = tx.extractor_version("m")
    monkeypatch.setattr(contract, "extracted_cells",
                        lambda: {"bazos": ("has_balcony", "has_lift")})
    two_open = tx.extractor_version("m")
    assert len({closed, one_open, two_open}) == 3


# --- the write ---------------------------------------------------------------


def test_the_write_can_only_fill_a_null_and_marks_the_property() -> None:
    sql = tx.write_sql(["floor", "has_lift"])
    assert "floor = coalesce(l.floor, %(floor)s::integer)" in sql
    assert "has_lift = coalesce(l.has_lift, %(has_lift)s::boolean)" in sql
    assert "INSERT INTO dirty_properties" in sql
    # One statement: the mark cannot outlive a rolled-back write (rule 20).
    assert sql.strip().count(";") == 0
    assert "WITH updated AS" in sql


def test_the_write_touches_neither_history_nor_the_sighting_clock() -> None:
    sql = tx.write_sql(list(tx._FIELD_SPEC))
    for forbidden in ("listing_snapshots", "last_seen_at", "sync_browse_list", "raw_json"):
        assert forbidden not in sql


def test_the_write_skips_a_row_it_would_not_change() -> None:
    sql = tx.write_sql(["condition"])
    assert "(l.condition IS NULL AND %(condition)s::text IS NOT NULL)" in sql


# --- the gate allowlist (R7) -------------------------------------------------


class _FakeCursor:
    def __init__(self, sink: list[tuple[str, Any]]) -> None:
        self._sink = sink

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._sink.append((sql, params))


class _FakeTxn:
    def __enter__(self) -> "_FakeTxn":
        return self

    def __exit__(self, *a: Any) -> None:
        return None


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.calls)

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()


def test_an_open_gate_carries_the_measurement_that_opened_it() -> None:
    """R7: a gate opens only on a measured precision >= 95 % (floor: within +-1), and the
    measurement travels with the row. Every gate not measured past that stays closed —
    OUT OF SCOPE: not extracted, not billed, not written."""
    declared = contract.gated_cells()
    assert declared
    for portal, fields in declared.items():
        for field in fields:
            gate = contract.CONTRACT[portal][field].gate
            assert gate is not None
            if gate.passed:
                assert field in OPEN_GATES.get(portal, ())
                assert gate.precision is not None and gate.precision >= 0.95
                assert gate.panel_n and gate.measured_on
            else:
                assert field not in OPEN_GATES.get(portal, ())


def test_a_passed_gate_writes_only_its_own_column(monkeypatch) -> None:
    monkeypatch.setattr(contract, "extracted_cells",
                        lambda: {"bazos": ("has_lift",)})
    conn = _FakeConn()
    written = tx.record_extraction(
        conn, {"id": 7, "source": "bazos", "description": DESCRIPTION},
        values={"has_lift": False, "condition": "po_rekonstrukci"},
        extracted={}, model="m", version="v", llm_call_id=None, cost_usd=0.0,
    )
    assert written == ["has_lift"]
    update_sql, params = conn.calls[0]
    assert "has_lift = coalesce" in update_sql
    assert "condition = coalesce" not in update_sql
    assert params["has_lift"] is False
    assert json.loads(conn.calls[1][1]["filled"]) == {"has_lift": False}


def test_a_failed_attempt_is_recorded_so_it_is_not_re_billed_for_ever() -> None:
    """Rule #5's shape in the table that already exists: the deleted lane dropped a
    failure, so the same listing was re-selected and re-billed on every pass."""
    conn = _FakeConn()
    tx.record_failure(conn, {"id": 7, "source": "bazos", "description": DESCRIPTION},
                      error="the model returned no record_description_facts call",
                      model="m", version="v", cost_usd=0.00225)
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert "INSERT INTO listing_description_enrichments" in sql
    payload = json.loads(params["extracted"])
    assert payload["attempts"] == 1
    assert "no record_description_facts call" in payload["error"]
    # Billed, and said so: a failure that reports $0 hides the spend from the daily series.
    assert params["cost_usd"] == 0.00225
    assert json.loads(params["filled"]) == {}


def test_the_selector_retires_a_listing_only_after_give_up_attempts() -> None:
    sql = tx._DECLARED_SELECT_SQL
    assert "e.extracted -> 'error' IS NULL" in sql
    assert f">= {tx.GIVE_UP_AFTER}" in sql
    assert tx.GIVE_UP_AFTER == 5


def test_a_retry_bumps_the_attempt_count_and_never_rewrites_an_answer() -> None:
    sql = tx._CACHE_INSERT_SQL
    assert "ON CONFLICT (listing_id, extractor_version, text_hash) DO UPDATE" in sql
    # Only over a row that is itself a failure.
    assert "WHERE e.extracted -> 'error' IS NOT NULL" in sql
    assert "'attempts'" in sql
    assert "cost_usd = coalesce(e.cost_usd, 0) + coalesce(excluded.cost_usd, 0)" in sql


# --- the merge rules ---------------------------------------------------------


FIELDS = ("floor", "total_floors", "has_lift", "has_balcony", "condition")


def test_floor_is_converted_from_the_adverts_own_words() -> None:
    values, dropped = merge({"floor": _cell("3. patro", "ve 3. patře cihlového domu")})
    assert values["floor"] == 3
    assert not dropped


@pytest.mark.parametrize("words,expected", [
    ("3. patro", 3),
    ("3. NP", 2),      # nadzemní podlaží is 1-indexed from the ground
    ("přízemí", 0),
    ("suterén", -1),
])
def test_the_two_czech_conventions_are_read_by_scraper_floor(words, expected) -> None:
    values, _ = merge({"floor": _cell(words, DESCRIPTION[:40])})
    assert values["floor"] == expected


def test_a_bare_number_has_no_convention_and_is_refused() -> None:
    """The ~73 %-correct, two-convention column the deleted lane left behind started
    exactly here: an integer with no word beside it cannot be read."""
    values, dropped = merge({"floor": _cell("3", "ve 3. patře cihlového domu")})
    assert "floor" not in values
    assert dropped["floor"] == "floor_words_unreadable"

    values, dropped = merge({"floor": _cell(3, "ve 3. patře cihlového domu")})
    assert dropped["floor"] == "floor_not_words"


def test_a_floor_above_the_stated_total_is_refused() -> None:
    values, dropped = merge({
        "floor": _cell("9. patro", "ve 3. patře"),
        "total_floors": _cell(5, "Dům má celkem 5 pater"),
    })
    assert values == {"total_floors": 5}
    assert dropped["floor"] == "implausible_floor"


def test_false_needs_an_explicit_negation_in_the_quote() -> None:
    values, _ = merge({"has_lift": _cell(False, "V domě bohužel bez výtahu")})
    assert values["has_lift"] is False


def test_false_from_silence_is_refused() -> None:
    """The measured defect: 8,202 cached `has_lift` false values at 92.9 % precision,
    against a filter predicate where a false negative is the expensive error."""
    values, dropped = merge({"has_lift": _cell(False, "cihlového domu po rekonstrukci")})
    assert "has_lift" not in values
    assert dropped["has_lift"] == "false_without_negation"


def test_bezbarierovy_is_not_a_negation() -> None:
    """'bez' has to be a whole word."""
    assert not tx._NEGATION_RE.search(tx._flat("bezbariérový přístup"))
    assert tx._NEGATION_RE.search(tx._flat("bez výtahu"))


def test_true_needs_no_negation_only_a_quote() -> None:
    values, _ = merge({"has_balcony": _cell(True, "k bytu náleží balkon")})
    assert values["has_balcony"] is True


def test_a_quote_that_is_not_in_the_text_drops_the_value() -> None:
    """The one rule that replaces a confidence filter: it kills the whole leaked-markup
    class (`{"value": null, "confidence": null"}` reached `energy_rating` 8 times) and
    makes every stored value auditable."""
    values, dropped = merge({"condition": _cell("novostavba", "zcela nový dům")})
    assert "condition" not in values
    assert dropped["condition"] == "quote_not_in_text"


def test_a_missing_quote_drops_the_value() -> None:
    _, dropped = merge({"condition": _cell("po_rekonstrukci", None)})
    assert dropped["condition"] == "quote_not_in_text"


def test_a_reflowed_quote_still_counts_as_copied() -> None:
    values, _ = merge({"condition": _cell(
        "po_rekonstrukci", "cihlového\n  domu   PO REKONSTRUKCI")})
    assert values["condition"] == "po_rekonstrukci"


def test_an_off_vocabulary_value_is_refused_and_counted() -> None:
    """'novostavba' is a CONDITION and reached `building_type` 123 times through a prose
    enum. It is refused here even if a provider ignores the JSON enum."""
    from scraper import vocabulary

    vocabulary.take_unmapped()
    values, dropped = merge(
        {"condition": _cell("sehr_dobry", "cihlového domu po rekonstrukci")})
    assert "condition" not in values
    assert dropped["condition"] == "off_vocabulary"
    assert any("sehr_dobry" in key for key, _ in vocabulary.take_unmapped())


def test_a_null_value_is_silence_not_a_drop() -> None:
    values, dropped = merge({"has_lift": _cell(None, None)})
    assert values == {} and dropped == {}


def merge(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    return tx.merge_extraction(payload, description=DESCRIPTION, fields=FIELDS)


# --- the tool schema ---------------------------------------------------------


def test_the_enums_are_real_json_arrays_generated_from_the_vocabulary() -> None:
    from scraper import vocabulary

    tool = tx.extraction_tool(("condition", "building_type", "energy_rating"))
    props = tool["input_schema"]["properties"]
    for field in ("condition", "building_type", "energy_rating"):
        enum = props[field]["properties"]["value"]["enum"]
        assert set(enum) == set(vocabulary.CANON[field]) | {None}


def test_floor_is_a_string_so_the_model_never_does_the_arithmetic() -> None:
    value = tx.extraction_tool(("floor",))["input_schema"]["properties"]["floor"]
    assert value["properties"]["value"]["type"] == ["string", "null"]
    assert "enum" not in value["properties"]["value"]


def test_every_field_demands_an_evidence_quote() -> None:
    tool = tx.extraction_tool(tuple(tx._FIELD_SPEC))
    for field, spec in tool["input_schema"]["properties"].items():
        assert spec["required"] == ["value", "evidence_quote"], field
        assert spec["additionalProperties"] is False


def test_the_schema_covers_exactly_the_contract_and_nothing_else() -> None:
    assert contract.gated_cells(), "nothing declared: this test would be vacuous"
    for fields in contract.gated_cells().values():
        assert set(fields) <= set(tx._FIELD_SPEC)
        tool = tx.extraction_tool(fields)
        assert set(tool["input_schema"]["properties"]) == set(fields)


def test_confidence_is_gone() -> None:
    """Deleted, not tuned: 0.08-5.39 % of values carried 'low' (the model expresses doubt
    by returning null), and the quote check is strictly stronger."""
    assert "confidence" not in json.dumps(tx.extraction_tool(tuple(tx._FIELD_SPEC)))


# --- the pass ----------------------------------------------------------------


def test_a_pass_with_no_model_configured_claims_nothing(monkeypatch) -> None:
    """No hardcoded fallback: `LLMClient`'s own default is a claude id and this project
    does not use Anthropic models."""
    monkeypatch.setattr(contract, "extracted_cells",
                        lambda: {"bazos": ("has_lift",)})
    monkeypatch.setattr(tx, "resolve_model", lambda conn: None)
    assert tx.run_pass(object()) == {"claimed": 0, "reason": "no_model"}


def test_a_pass_with_no_open_gate_claims_nothing(monkeypatch) -> None:
    """The shipping state: no gate is open, so there is nothing the lane may write and
    therefore nothing it may pay for. Not even a query."""
    monkeypatch.setattr(contract, "extracted_cells", dict)
    assert tx.run_pass(object()) == {"claimed": 0, "reason": "no_open_gate"}


def test_the_called_for_is_the_one_the_cost_series_already_knows() -> None:
    """Renaming would need a migration and would break the 60-day per-lane cost series
    plus the two frontend cost pages."""
    from api.llm_client import CalledFor

    assert tx.CALLED_FOR in CalledFor.__args__


def test_the_budget_guard_is_the_shared_engines_and_binds_before_the_call() -> None:
    import inspect

    from toolkit import vision_batch

    assert tx.MAX_USD_PER_PASS > 0
    src = inspect.getsource(vision_batch.run_batch)
    assert 'stats["spent"] >= max_usd' in src
    # ...under the lock, in the worker, BEFORE the call.
    assert (src.index("with lock:\n                    if _stop()")
            < src.index("cost, result = call(llm, row)"))


def test_the_tool_call_is_forced(monkeypatch) -> None:
    """A model that answers in prose produces no tool call. Forcing it narrows that;
    `record_failure` is what stops the remaining causes being re-billed for ever."""
    import inspect

    src = inspect.getsource(tx.run_pass)
    assert 'tool_choice=tool["name"]' in src
    # The cost comes back even when the answer is unusable, so the pre-call guard sees it.
    assert "raise" not in src
    assert "record_failure(" in src


def test_the_lane_can_serve_the_model_the_one_switch_may_name() -> None:
    """`app_settings.enrichment_model` may be an `oss:` id — the bake-off's own default
    arms are two of them. A registry the switch cannot reach turns every row of every pass
    into a ProviderError raised before a single call."""
    import inspect

    from api.llm_client import provider_for_model

    registry = tx._providers()
    assert provider_for_model("oss:Qwen/Qwen3-VL-32B-Instruct") in registry
    assert provider_for_model("gpt-5.6-luna") in registry
    assert "providers=_providers" in inspect.getsource(tx.run_pass)


def test_a_fatal_provider_error_does_not_burn_a_listings_attempts() -> None:
    """A dead key is the PROVIDER's state, not the listing's; counting it would retire
    rows for an outage they had no part in."""
    import inspect

    src = inspect.getsource(tx.run_pass)
    assert "vision_batch.is_fatal(error)" in src
    assert src.index("vision_batch.is_fatal(error)") < src.index("record_failure(")


# --- area: a quantity the grammar could not read ------------------------------

AREA_DESCRIPTION = (
    "Prodám byt 2+kk, plocha padesát čtyři metrů čtverečních, ve 3. patře. "
    "K domu patří pozemek o výměře 12 arů a zahrada 0,5 ha. Cena 5 000 000 Kč."
)
AREA_FIELDS = (*FIELDS, "area_m2")


def merge_area(payload: dict[str, Any], category_main: str = "byt") -> tuple[dict, dict]:
    return tx.merge_extraction(payload, description=AREA_DESCRIPTION, fields=AREA_FIELDS,
                               category_main=category_main)


def test_an_area_figure_must_itself_appear_in_the_quote() -> None:
    """R7 for a quantity: the number the model returns is the number the text carries.
    'padesát čtyři' is words, so the model's 54 has no digits to match and is refused."""
    values, dropped = merge_area({"area_m2": _cell(54, "plocha padesát čtyři metrů čtverečních")})
    assert "area_m2" not in values
    assert dropped["area_m2"] == "figure_not_in_quote"


@pytest.mark.parametrize("value,quote,category,expected", [
    (1200, "pozemek o výměře 12 arů", "pozemek", 1200.0),   # ares convert, and only ares
    (5000, "zahrada 0,5 ha", "pozemek", 5000.0),            # hectares, decimal comma
    (5000000, "Cena 5 000 000 Kč", "pozemek", None),        # a price is not an area
])
def test_ares_and_hectares_are_the_only_conversions(value, quote, category, expected) -> None:
    values, dropped = merge_area({"area_m2": _cell(value, quote)}, category_main=category)
    if expected is None:
        assert dropped["area_m2"] == "area_out_of_range"
    else:
        assert values["area_m2"] == expected and "area_m2" not in dropped


def test_the_grammars_category_bounds_apply_to_a_lane_area() -> None:
    """5 m² is the floor for a flat (scraper.area.MIN_AREA_M2) and no bound for land —
    the one function that stamps `area_basis` at ingest decides, not this lane."""
    values, dropped = merge_area({"area_m2": _cell(3, "pozemek o výměře 12 arů")})
    assert dropped["area_m2"] == "figure_not_in_quote"
    values, dropped = merge_area({"area_m2": _cell(12, "pozemek o výměře 12 arů")},
                                 category_main="byt")
    assert values["area_m2"] == 12.0
    values, dropped = merge_area({"area_m2": _cell(4, "0,5 ha")}, category_main="byt")
    assert dropped["area_m2"] == "figure_not_in_quote"


def test_an_area_is_a_number_never_words() -> None:
    values, dropped = merge_area({"area_m2": _cell("54", "12 arů")})
    assert dropped["area_m2"] == "not_number"
    values, dropped = merge_area({"area_m2": _cell(True, "12 arů")})
    assert dropped["area_m2"] == "not_number"


def test_the_selector_carries_the_category_the_basis_stamp_needs() -> None:
    assert "l.category_main" in tx.SELECT_INFLOW_SQL


def test_area_basis_follows_area_in_the_same_update_and_never_alone() -> None:
    sql = tx.write_sql(["area_m2"], {"area_basis": "area_m2"})
    assert "area_m2 = coalesce(l.area_m2, %(area_m2)s::numeric)" in sql
    assert ("area_basis = CASE WHEN l.area_m2 IS NULL THEN %(area_basis)s::text "
            "ELSE l.area_basis END") in sql
    changed = sql.split("AND (")[1].split(")\n")[0]
    assert "area_basis" not in changed


def test_a_lane_area_carries_the_ingest_basis_stamp_in_the_same_update(monkeypatch) -> None:
    """`plot` on land, `unknown` elsewhere — the stamp `scraper.area` gives a grammar-read
    area, so a lane-filled row is indistinguishable downstream; and it rides in `filled`
    so the rollback script reverts both."""
    monkeypatch.setattr(contract, "extracted_cells", lambda: {"bazos": ("area_m2",)})
    conn = _FakeConn()
    written = tx.record_extraction(
        conn, {"id": 7, "source": "bazos", "description": DESCRIPTION,
               "category_main": "pozemek"},
        values={"area_m2": 1200.0},
        extracted={}, model="m", version="v", llm_call_id=None, cost_usd=0.0,
    )
    assert written == ["area_m2"]
    update_sql, params = conn.calls[0]
    assert "area_basis = CASE WHEN l.area_m2 IS NULL" in update_sql
    assert params["area_m2"] == 1200.0 and params["area_basis"] == "plot"
    assert json.loads(conn.calls[1][1]["filled"]) == {"area_m2": 1200.0, "area_basis": "plot"}
