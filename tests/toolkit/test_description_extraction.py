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
    sql = tx.SELECT_INFLOW_SQL
    assert "sreality_id" not in sql
    assert "e.listing_id = l.id" in sql
    assert "e.extractor_version = %(version)s" in sql


OPEN_GATES = {"bazos": ("floor", "has_lift")}


def test_the_scope_is_the_open_gates_plus_the_llm_text_portals_and_names_no_portal() -> None:
    """W1 (2026-09-30): the NULL arms are gone — a row is in scope because its PORTAL is, via
    an open R7 gate or an active contract's `llm_text` entry read from the DB (rule 21)."""
    assert contract.extracted_cells() == OPEN_GATES
    for sql in (tx.SELECT_INFLOW_SQL, tx.ELIGIBLE_LAG_SQL):
        assert "pe.extraction_method = 'llm_text'" in sql and "pc.is_active" in sql
        assert "SELECT 'bazos'" in sql
        assert "IS NULL OR" not in sql and "l.floor" not in sql and "l.has_lift" not in sql


def test_a_delisted_advert_is_read_once_per_schema_and_an_active_one_on_every_change() -> None:
    """Q1 (2026-09-30). The delisted arm is hash-free and runs FIRST; the CASE also fences the
    anti-join into a per-row probe (a new version pulled up into a join grew towards 120 s)."""
    where = tx._eligible_where(OPEN_GATES)
    arm = where[where.index("CASE WHEN l.is_active OR NOT EXISTS"):where.index("   THEN NOT EXISTS")]
    assert f"d.extractor_version LIKE '{tx.SCHEMA_VERSION}:%%'" in arm
    assert "d.extracted -> 'error' IS NULL" in arm
    assert "text_hash" not in arm and where.endswith(")) END")
    assert tx.SCHEMA_VERSION == "2"


def test_the_advert_text_is_headline_newline_description_hashed_only_in_sql() -> None:
    """`bazos_parser.ad_haystack`'s composition, spelled once in SQL; no Python twin."""
    from scraper.bazos_parser import ad_haystack

    assert ad_haystack("T", "D") == "T\nD"
    assert tx._TEXT_EXPR == (
        "coalesce(l.raw_json ->> 'title', '') || E'\\n' || coalesce(l.description, '')")
    assert f"{tx._HASH_EXPR} AS text_hash" in tx.SELECT_INFLOW_SQL
    assert not hasattr(tx, "text_hash")


def test_the_lane_scope_unions_the_open_gates_with_the_llm_text_portals(monkeypatch) -> None:
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None): assert sql == tx.LLM_TEXT_SOURCES_SQL
        def fetchall(self): return [("idnes",)]

    class _Conn:
        def cursor(self): return _Cur()

    assert tx.lane_scope(_Conn()) == {"bazos": ("floor", "has_lift"), "idnes": ()}


def test_the_hash_stays_out_of_the_index_condition() -> None:
    """`IS NOT DISTINCT FROM`, not `=`: a plain equality lets the planner make the hash an
    index condition, which detoasts and hashes every advert every pass forever,
    including the steady state where nothing is eligible."""
    assert "text_hash IS NOT DISTINCT FROM" in tx.SELECT_INFLOW_SQL
    assert "text_hash =" not in tx.SELECT_INFLOW_SQL


def test_one_arm_newest_first_reaches_the_backlog() -> None:
    """An extracted row leaves the predicate, so DESC walks BACKWARDS through a backlog at
    PASS_SLICE a pass (~57k a day against a 1,940-a-day inflow). The oldest-first arm an
    earlier draft added bought nothing and cost a measured 9.4-10.0 s per run."""
    assert "ORDER BY l.first_seen_at DESC" in tx.SELECT_INFLOW_SQL
    assert "CROSS JOIN LATERAL" in tx.SELECT_INFLOW_SQL and "l.source = s.src" in tx.SELECT_INFLOW_SQL
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


def test_the_extractor_version_moves_with_the_prompt_and_the_schema_and_nothing_else(
        monkeypatch) -> None:
    """What was ASKED is what a cached answer is an answer from. Without the schema half,
    opening a gate would write NOTHING to the existing corpus: every listing already has a
    cache row at this version, and the anti-join retires it for ever."""
    base = tx.extractor_version("m")
    assert tx.extractor_version("m") == base
    monkeypatch.setattr(contract, "extracted_cells", lambda: {"bazos": ("has_lift",)})
    one_open = tx.extractor_version("m")
    monkeypatch.setattr(tx, "_SYSTEM_PROMPT", tx._SYSTEM_PROMPT + " ")
    reprompted = tx.extractor_version("m")
    assert len({base, one_open, reprompted}) == 3
    assert reprompted.startswith(f"{tx.SCHEMA_VERSION}:")


# --- the write ---------------------------------------------------------------


def test_the_write_can_only_fill_a_null_on_an_active_row_and_marks_the_property() -> None:
    sql = tx.write_sql(["floor", "has_lift"])
    assert "AND l.is_active" in sql
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
        conn, {"id": 7, "source": "bazos", "is_active": True, "text_hash": "h"},
        values={"has_lift": False, "condition": "po_rekonstrukci"},
        extracted={}, model="m", version="v", llm_call_id=None, cost_usd=0.0,
    )
    assert written == ["has_lift"]
    update_sql, params = conn.calls[0]
    assert "has_lift = coalesce" in update_sql
    assert "condition = coalesce" not in update_sql
    assert params["has_lift"] is False
    assert json.loads(conn.calls[1][1]["filled"]) == {"has_lift": False}
    assert conn.calls[1][1]["text_hash"] == "h"


def test_a_delisted_advert_is_read_but_never_filled(monkeypatch) -> None:
    """Field capture's ruling on inactive rows stands: the reading is cached, no column."""
    monkeypatch.setattr(contract, "extracted_cells", lambda: {"bazos": ("has_lift",)})
    conn = _FakeConn()
    written = tx.record_extraction(
        conn, {"id": 7, "source": "bazos", "is_active": False, "text_hash": "h"},
        values={"has_lift": True}, extracted={"location": {}}, model="m", version="v",
        llm_call_id=None, cost_usd=0.0)
    assert written == [] and len(conn.calls) == 1
    assert json.loads(conn.calls[0][1]["filled"]) == {}


def test_a_failed_attempt_is_recorded_so_it_is_not_re_billed_for_ever() -> None:
    """Rule #5's shape in the table that already exists: the deleted lane dropped a
    failure, so the same listing was re-selected and re-billed on every pass."""
    conn = _FakeConn()
    tx.record_failure(conn, {"id": 7, "source": "bazos", "text_hash": "h"},
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
    sql = tx.SELECT_INFLOW_SQL
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


def test_a_building_count_outside_the_storey_band_is_refused() -> None:
    """The lane obeys the parsers' band (`scraper.floor.total_floors_from_portal`): the
    backfill that NULLs a stored count outside 1..40 is source-agnostic, so any producer
    left unbounded would write the healed shape back."""
    for count in (0, 113):
        values, dropped = merge({"total_floors": _cell(count, "Dům má celkem 5 pater")})
        assert "total_floors" not in values
        assert dropped["total_floors"] == "implausible_total_floors"
    values, _ = merge({"total_floors": _cell(40, "Dům má celkem 5 pater")})
    assert values == {"total_floors": 40}


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
    return tx.merge_extraction(payload, text=DESCRIPTION, fields=FIELDS)


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
    props = tx.extraction_tool(tuple(tx._FIELD_SPEC))["input_schema"]["properties"]
    cells = {**props, **props["location"]["properties"]}
    del cells["location"]
    for field, spec in cells.items():
        assert spec["required"] == ["value", "evidence_quote"], field
        assert spec["additionalProperties"] is False


def test_the_schema_is_the_contract_plus_the_location_block_for_every_portal() -> None:
    """Every in-scope portal gets the location block — one in scope through `llm_text`
    alone gets nothing else."""
    assert contract.gated_cells(), "nothing declared: this test would be vacuous"
    for fields in (*contract.gated_cells().values(), ()):
        assert set(fields) <= set(tx._FIELD_SPEC)
        schema = tx.extraction_tool(fields)["input_schema"]
        assert set(schema["properties"]) == {*fields, "location"}
        assert "location" in schema["required"]


def test_confidence_is_gone() -> None:
    """Deleted, not tuned: 0.08-5.39 % of values carried 'low' (the model expresses doubt
    by returning null), and the quote check is strictly stronger."""
    assert "confidence" not in json.dumps(tx.extraction_tool(tuple(tx._FIELD_SPEC)))


# --- the pass ----------------------------------------------------------------


def test_a_pass_with_no_model_configured_claims_nothing(monkeypatch) -> None:
    """No hardcoded fallback: `LLMClient`'s own default is a claude id and this project
    does not use Anthropic models."""
    monkeypatch.setattr(tx, "lane_scope", lambda conn: {"bazos": ("has_lift",)})
    monkeypatch.setattr(tx, "resolve_model", lambda conn: None)
    assert tx.run_pass(object()) == {"claimed": 0, "reason": "no_model"}


def test_a_pass_with_an_empty_scope_claims_nothing(monkeypatch) -> None:
    """No open gate and no `llm_text` entry: nothing the lane may read or pay for."""
    monkeypatch.setattr(tx, "lane_scope", lambda conn: {})
    assert tx.run_pass(object()) == {"claimed": 0, "reason": "empty_scope"}


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
