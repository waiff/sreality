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
    where = tx._eligible_where()
    assert where in tx.SELECT_INFLOW_SQL
    assert where in tx.SELECT_BACKLOG_SQL
    assert where in tx.ELIGIBLE_LAG_SQL


def test_the_selector_keys_on_listing_id_and_never_on_sreality_id() -> None:
    sql = tx.SELECT_INFLOW_SQL
    assert "sreality_id" not in sql
    assert "e.listing_id = l.id" in sql
    assert "e.extractor_version = %(version)s" in sql


def test_the_selector_covers_every_extractable_cell_of_every_declared_portal() -> None:
    sql = tx.SELECT_INFLOW_SQL
    scope = contract.extracted_cells()
    assert scope, "the contract declares no text cell for the lane at all"
    for portal, fields in scope.items():
        assert f"l.source = '{portal}'" in sql
        for field in fields:
            assert f"l.{field} IS NULL" in sql


def test_the_hash_stays_out_of_the_index_condition() -> None:
    """`IS NOT DISTINCT FROM`, not `=`: a plain equality lets the planner make the hash an
    index condition, which detoasts and hashes all ~50k descriptions every pass forever,
    including the steady state where nothing is eligible."""
    assert "text_hash IS NOT DISTINCT FROM" in tx.SELECT_INFLOW_SQL
    assert "text_hash =" not in tx.SELECT_INFLOW_SQL


def test_the_selector_reaches_the_backlog_as_well_as_the_inflow() -> None:
    """The deleted lane ordered `first_seen_at DESC` and nothing else, so its 50k backlog
    was structurally unreachable — inflow always filled the slice. The oldest-first arm is
    a SEPARATE statement because it costs a measured ~10 s once nothing is eligible, and
    it runs on its own cadence rather than every pass."""
    assert "ORDER BY l.first_seen_at DESC" in tx.SELECT_INFLOW_SQL
    assert "ORDER BY l.first_seen_at ASC" in tx.SELECT_BACKLOG_SQL
    assert 0 < tx.INFLOW_SLICE < tx.PASS_SLICE
    assert tx.BACKLOG_EVERY_PASSES > 1


def test_a_pass_only_runs_the_expensive_arm_when_asked() -> None:
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
    seen.clear()
    tx.select_eligible(_Conn(), version="v", backlog=True)
    assert seen == [tx.SELECT_INFLOW_SQL, tx.SELECT_BACKLOG_SQL]


def test_the_extractor_version_carries_the_model() -> None:
    """Migration 249's lesson, preserved through the re-key: a cached answer is an answer
    from a particular model, so a model swap must re-attempt rather than serve the old."""
    assert tx.extractor_version("gpt-5-mini") != tx.extractor_version("gpt-5.6-luna")
    assert "gpt-5-mini" in tx.extractor_version("gpt-5-mini")


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


def test_every_gate_ships_closed() -> None:
    """The bake-off flips them, one field at a time, with its measurement in the row."""
    for portal, fields in contract.extracted_cells().items():
        assert contract.writable_cells(portal) == (), portal
        for field in fields:
            gate = contract.CONTRACT[portal][field].gate
            assert gate is not None and gate.passed is False


def test_an_ungated_field_is_cached_and_not_written() -> None:
    conn = _FakeConn()
    written = tx.record_extraction(
        conn, {"id": 7, "source": "bazos", "description": DESCRIPTION},
        values={"has_lift": False, "condition": "po_rekonstrukci"},
        extracted={"has_lift": _cell(False, "bez výtahu")},
        model="gpt-5-mini", version="1:gpt-5-mini", llm_call_id=3, cost_usd=0.002,
    )
    assert written == []
    assert len(conn.calls) == 1
    sql, params = conn.calls[0]
    assert "INSERT INTO listing_description_enrichments" in sql
    # Cached with an EMPTY `filled`: the panel can be scored from `extracted` without any
    # column having moved.
    assert json.loads(params["filled"]) == {}
    assert json.loads(params["extracted"])["has_lift"]["value"] is False
    assert params["text_hash"] == tx.text_hash(DESCRIPTION)


def test_a_passed_gate_writes_only_its_own_column(monkeypatch) -> None:
    monkeypatch.setattr(contract, "writable_cells", lambda portal: ("has_lift",))
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
        assert set(enum) == set(vocabulary.known_values(field)) | {None}


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
    for fields in contract.extracted_cells().values():
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
    monkeypatch.setattr(tx, "resolve_model", lambda conn: None)
    assert tx.run_pass(object()) == {"claimed": 0, "reason": "no_model"}


def test_a_pass_with_nothing_declared_claims_nothing(monkeypatch) -> None:
    monkeypatch.setattr(contract, "extracted_cells", dict)
    assert tx.run_pass(object()) == {"claimed": 0, "reason": "no_text_cells"}


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
    """A model that answers in prose produces no tool call, the listing stays eligible and
    the next pass pays for the same refusal — for ever."""
    import inspect

    src = inspect.getsource(tx.run_pass)
    assert 'tool_choice=tool["name"]' in src
