"""Hermetic tests for score_listing_condition.

No DB connection: scripted cursor returns prepared rows in order.
LLMClient is replaced with a fake that records call kwargs and
returns prepared LLMResponse-shaped objects.

Coverage:
  * Tool schema invariants (required fields, level + confidence bounds).
  * Cache hit returns immediately without an LLM call.
  * Cache miss: LLM call, cache write, listings UPDATE inside one
    transaction (asserted via conn.transactions_opened).
  * Tool-call validation rejects out-of-range levels, non-list
    markers_found, malformed payloads, missing fields.
  * Resolves app_settings.llm_condition_rubric and
    llm_condition_marker_dictionary; raises ScoringError when either
    is still {} (placeholder pre-seed).
  * System-prompt placeholder substitution is unit-tested separately.
  * Envelope shape (tool name, filters_used, queried_at, data_freshness).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from toolkit import condition_scoring


_NOW = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)


# ---- Tool-schema invariants -----------------------------------------------


def test_tool_schema_has_all_required_fields():
    schema = condition_scoring.RECORD_LISTING_CONDITION_TOOL["input_schema"]
    assert set(schema["required"]) == {
        "building_level", "apartment_level",
        "building_markers_found", "apartment_markers_found",
        "building_confidence", "apartment_confidence",
        "notes",
    }


def test_level_bounds_in_schema():
    schema = condition_scoring.RECORD_LISTING_CONDITION_TOOL["input_schema"]
    for axis in ("building_level", "apartment_level"):
        prop = schema["properties"][axis]
        assert prop["minimum"] == 1 and prop["maximum"] == 5


def test_confidence_bounds_in_schema():
    schema = condition_scoring.RECORD_LISTING_CONDITION_TOOL["input_schema"]
    for axis in ("building_confidence", "apartment_confidence"):
        prop = schema["properties"][axis]
        assert prop["minimum"] == 0.0 and prop["maximum"] == 1.0


# ---- Cache hit path -------------------------------------------------------


def test_cache_hit_returns_without_llm_call(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        # _resolve_snapshot
        ("fetchone", (42, _NOW, {"text": "..."})),
        # _cache_lookup
        ("fetchone", (
            5, 4,                    # building_level, apartment_level
            ["B002"], ["A001", "A002"],   # markers_found
            0.85, 0.72,              # confidence
            "", 0,                   # notes, n_images
            "claude-sonnet-4-5", 0.0214,  # model, cost_usd
        )),
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([])

    res = condition_scoring.score_listing_condition(
        conn, llm, sreality_id=123,  # type: ignore[arg-type]
    )
    assert llm.calls == []
    assert res["data"]["cache_hit"] is True
    assert res["data"]["building_level"] == 5
    assert res["data"]["apartment_level"] == 4
    assert res["data"]["building_markers_found"] == ["B002"]
    assert res["data"]["apartment_markers_found"] == ["A001", "A002"]


# ---- Cache miss path: LLM call + transactional write ----------------------


def test_cache_miss_invokes_llm_then_writes_atomically(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {"text": "Po rekonstrukci"})),     # snapshot
        ("fetchone", None),                                         # cache miss
        ("fetchone", _listing_row()),                               # _fetch_listing
        # _resolve_jsonb_setting calls: rubric, then dictionary
        ("fetchone", ({"level_count": 5, "building_levels": [],
                       "apartment_levels": []},)),
        ("fetchone", ({"schema_version": 1, "building": [], "apartment": []},)),
        # _cache_store_and_update_listings: INSERT + UPDATE
        ("execute_write", None),
        ("execute_write", None),
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_llm_response(_good_score())])

    res = condition_scoring.score_listing_condition(
        conn, llm, sreality_id=123,  # type: ignore[arg-type]
    )

    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["called_for"] == "score_listing_condition"
    assert call["tools"][0]["name"] == "record_listing_condition"
    assert "system" in call

    assert conn.transactions_opened == 1, "cache write + listings update must be atomic"
    assert res["data"]["cache_hit"] is False
    assert res["data"]["building_level"] == 4
    assert res["data"]["apartment_level"] == 5


# ---- Settings resolution --------------------------------------------------


def test_raises_when_rubric_is_empty_placeholder(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {"text": "..."})),
        ("fetchone", None),                              # cache miss
        ("fetchone", _listing_row()),
        ("fetchone", ({},)),                              # rubric is still {}
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([])
    with pytest.raises(condition_scoring.ScoringError, match="empty"):
        condition_scoring.score_listing_condition(
            conn, llm, sreality_id=123,  # type: ignore[arg-type]
        )


def test_raises_when_dictionary_is_empty_placeholder(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {"text": "..."})),
        ("fetchone", None),
        ("fetchone", _listing_row()),
        ("fetchone", ({"level_count": 5, "building_levels": [],
                       "apartment_levels": []},)),
        ("fetchone", ({},)),                              # dictionary is still {}
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([])
    with pytest.raises(condition_scoring.ScoringError, match="empty"):
        condition_scoring.score_listing_condition(
            conn, llm, sreality_id=123,  # type: ignore[arg-type]
        )


# ---- Tool-call validation -------------------------------------------------


def test_missing_tool_call_raises(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", None),
        ("fetchone", _listing_row()),
        ("fetchone", ({"level_count": 5, "building_levels": [],
                       "apartment_levels": []},)),
        ("fetchone", ({"schema_version": 1, "building": [], "apartment": []},)),
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_LLMResp(text="oops", tool_calls=[])])
    with pytest.raises(condition_scoring.ScoringError, match="did not invoke"):
        condition_scoring.score_listing_condition(
            conn, llm, sreality_id=123,  # type: ignore[arg-type]
        )


def test_out_of_range_level_rejected(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", None),
        ("fetchone", _listing_row()),
        ("fetchone", ({"level_count": 5, "building_levels": [],
                       "apartment_levels": []},)),
        ("fetchone", ({"schema_version": 1, "building": [], "apartment": []},)),
    ]
    conn = _make_conn(plan)
    bad = _good_score() | {"building_level": 7}
    llm = _FakeLLM([_llm_response(bad)])
    with pytest.raises(condition_scoring.ScoringError, match="out of range"):
        condition_scoring.score_listing_condition(
            conn, llm, sreality_id=123,  # type: ignore[arg-type]
        )


def test_out_of_range_confidence_rejected(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", None),
        ("fetchone", _listing_row()),
        ("fetchone", ({"level_count": 5, "building_levels": [],
                       "apartment_levels": []},)),
        ("fetchone", ({"schema_version": 1, "building": [], "apartment": []},)),
    ]
    conn = _make_conn(plan)
    bad = _good_score() | {"apartment_confidence": 1.5}
    llm = _FakeLLM([_llm_response(bad)])
    with pytest.raises(condition_scoring.ScoringError, match="out of range"):
        condition_scoring.score_listing_condition(
            conn, llm, sreality_id=123,  # type: ignore[arg-type]
        )


def test_missing_field_rejected(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", None),
        ("fetchone", _listing_row()),
        ("fetchone", ({"level_count": 5, "building_levels": [],
                       "apartment_levels": []},)),
        ("fetchone", ({"schema_version": 1, "building": [], "apartment": []},)),
    ]
    conn = _make_conn(plan)
    bad = {k: v for k, v in _good_score().items() if k != "notes"}
    llm = _FakeLLM([_llm_response(bad)])
    with pytest.raises(condition_scoring.ScoringError, match="missing field: notes"):
        condition_scoring.score_listing_condition(
            conn, llm, sreality_id=123,  # type: ignore[arg-type]
        )


def test_non_list_markers_rejected(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", None),
        ("fetchone", _listing_row()),
        ("fetchone", ({"level_count": 5, "building_levels": [],
                       "apartment_levels": []},)),
        ("fetchone", ({"schema_version": 1, "building": [], "apartment": []},)),
    ]
    conn = _make_conn(plan)
    bad = _good_score() | {"building_markers_found": "B002"}
    llm = _FakeLLM([_llm_response(bad)])
    with pytest.raises(condition_scoring.ScoringError, match="must be a list"):
        condition_scoring.score_listing_condition(
            conn, llm, sreality_id=123,  # type: ignore[arg-type]
        )


# ---- Snapshot resolution --------------------------------------------------


def test_no_snapshot_raises(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    conn = _make_conn([("fetchone", None)])
    with pytest.raises(condition_scoring.ScoringError, match="no snapshot"):
        condition_scoring.score_listing_condition(
            conn, _FakeLLM([]), sreality_id=999,  # type: ignore[arg-type]
        )


def test_explicit_snapshot_id_filters_by_both_columns(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (777, _NOW, {})),
        ("fetchone", (3, 3, [], [], 0.5, 0.5, "", 0, "claude-sonnet-4-5", 0.0)),
    ]
    conn = _make_conn(plan)
    condition_scoring.score_listing_condition(
        conn, _FakeLLM([]), sreality_id=123, snapshot_id=777,  # type: ignore[arg-type]
    )
    snap_sql = conn.cursor_obj.executed[0]
    assert "WHERE id = %s AND sreality_id = %s" in snap_sql[0]
    assert snap_sql[1] == (777, 123)


# ---- Envelope -------------------------------------------------------------


def test_envelope_metadata_shape(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", (3, 3, [], [], 0.5, 0.5, "", 0, "claude-sonnet-4-5", 0.0)),
    ]
    conn = _make_conn(plan)
    res = condition_scoring.score_listing_condition(
        conn, _FakeLLM([]), sreality_id=123,  # type: ignore[arg-type]
    )
    md = res["metadata"]
    assert md["tool"] == "score_listing_condition"
    assert md["filters_used"] == {
        "sreality_id": 123, "snapshot_id": None,
        "n_images": 0, "force_refresh": False,
    }
    assert md["result_count"] == 1
    assert md["data_freshness"] == _NOW.isoformat()
    assert md["queried_at"]


# ---- System prompt construction -------------------------------------------


def test_build_system_prompt_substitutes_placeholders():
    template = (
        "Score the listing.\n\n"
        "Marker dictionary: <MARKER_DICTIONARY>\n\n"
        "Rubric: <RUBRIC>\n"
    )
    rubric = {"level_count": 5}
    dictionary = {
        "schema_version": 1,
        "building": [{
            "marker_id": "B001", "canonical": "novostavba",
            "sentiment_majority": "positive", "level_hint_majority": "high",
            "variants": ["novostavba"], "count": 100,
            "sentiment_counts": {"positive": 100},
            "examples": ["x"],
        }],
        "apartment": [],
    }
    out = condition_scoring._build_system_prompt(
        template, rubric=rubric, dictionary=dictionary,
    )
    assert "Marker dictionary: {" in out
    assert "Rubric: {" in out
    assert '"count":100' not in out, "compact dictionary must drop noisy fields"
    assert '"sentiment_counts"' not in out, "compact dictionary must drop sentiment_counts"
    assert '"examples"' not in out, "compact dictionary must drop examples"
    assert "B001" in out
    assert '"level_count":5' in out


def test_build_system_prompt_appends_when_placeholders_absent():
    template = "Just score it."
    out = condition_scoring._build_system_prompt(
        template, rubric={"level_count": 5}, dictionary={"building": [], "apartment": []},
    )
    assert "Rubric:" in out
    assert "Marker dictionary:" in out


# ---- Listings UPDATE guard ------------------------------------------------


def test_listings_update_uses_latest_wins_guard(monkeypatch):
    """The UPDATE SQL must compare the snapshot's scraped_at against
    MAX(scraped_at) for that sreality_id, so a stale-snapshot scorer
    can't overwrite a fresher score."""
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {})),                                # snapshot
        ("fetchone", None),                                          # cache miss
        ("fetchone", _listing_row()),                                # listing
        ("fetchone", ({"level_count": 5, "building_levels": [],
                       "apartment_levels": []},)),
        ("fetchone", ({"schema_version": 1, "building": [], "apartment": []},)),
        ("execute_write", None),
        ("execute_write", None),
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_llm_response(_good_score())])
    condition_scoring.score_listing_condition(
        conn, llm, sreality_id=123,  # type: ignore[arg-type]
    )
    update_sql = conn.cursor_obj.executed[-1][0]
    assert "UPDATE listings" in update_sql
    assert "MAX(scraped_at)" in update_sql, "UPDATE must compare against MAX(scraped_at)"
    assert "building_condition_level" in update_sql
    assert "apartment_condition_level" in update_sql


def test_listings_update_clears_propagation_provenance(monkeypatch):
    """An own genuine score must reset condition_levels_propagated_from to
    NULL — it supersedes any sibling-propagated copy."""
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {})),                                # snapshot
        ("fetchone", None),                                          # cache miss
        ("fetchone", _listing_row()),                                # listing
        ("fetchone", ({"level_count": 5, "building_levels": [],
                       "apartment_levels": []},)),
        ("fetchone", ({"schema_version": 1, "building": [], "apartment": []},)),
        ("execute_write", None),
        ("execute_write", None),
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_llm_response(_good_score())])
    condition_scoring.score_listing_condition(
        conn, llm, sreality_id=123,  # type: ignore[arg-type]
    )
    update_sql = conn.cursor_obj.executed[-1][0]
    assert "UPDATE listings" in update_sql
    assert "condition_levels_propagated_from = NULL" in update_sql


# ---- propagate_condition_levels ---------------------------------------------


def test_propagate_condition_levels_sql_structure():
    conn = _make_conn([("execute_write", None)])
    n = condition_scoring.propagate_condition_levels(conn)  # type: ignore[arg-type]
    assert n == 0
    sql = conn.cursor_obj.executed[0][0]
    assert "UPDATE listings" in sql
    # Source = genuine scores only, preferring the freshest cache row.
    assert "l.condition_levels_propagated_from IS NULL" in sql
    assert "MAX(cs.created_at)" in sql
    # Targets gain provenance pointing at the source listing.
    assert "condition_levels_propagated_from = s.sreality_id" in sql
    assert "t.sreality_id <> s.sreality_id" in sql


def test_propagate_condition_levels_never_clobbers_genuine_scores():
    conn = _make_conn([("execute_write", None)])
    condition_scoring.propagate_condition_levels(conn)  # type: ignore[arg-type]
    sql = " ".join(conn.cursor_obj.executed[0][0].split())
    # Targets: levels still NULL, or themselves propagated — a genuine
    # own-score (levels set + provenance NULL) is never overwritten.
    assert (
        "(t.building_condition_level IS NULL "
        "AND t.apartment_condition_level IS NULL) "
        "OR t.condition_levels_propagated_from IS NOT NULL"
    ) in sql
    # Idempotency: rows already equal to the source are skipped.
    assert "IS DISTINCT FROM" in sql


# ---- Helpers --------------------------------------------------------------


def _stub_image_storage(monkeypatch, *, configured: bool) -> None:
    from scraper import image_storage
    monkeypatch.setattr(image_storage, "is_configured", lambda: configured)


def _good_score() -> dict[str, Any]:
    return {
        "building_level": 4,
        "apartment_level": 5,
        "building_markers_found": ["B008", "B013"],
        "apartment_markers_found": ["A001"],
        "building_confidence": 0.8,
        "apartment_confidence": 0.92,
        "notes": "",
    }


def _listing_row() -> tuple[Any, ...]:
    return (
        "byt", "pronajem", 25000, "měsíc", 65.0, "2+kk",
        "Praha 2", "Praha 2", 3, 5, True, False, True,
        "cihla", "po rekonstrukci", "B",
    )


@dataclass
class _LLMResp:
    text: str
    tool_calls: list[dict[str, Any]]
    model: str = "claude-sonnet-4-5"
    cost_usd: float = 0.0214
    llm_call_id: int = 555
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    duration_ms: int = 0


def _llm_response(payload: dict[str, Any]) -> _LLMResp:
    return _LLMResp(
        text="",
        tool_calls=[{"name": "record_listing_condition", "input": payload}],
    )


class _FakeLLM:
    def __init__(self, responses: list[_LLMResp]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def resolve_system_prompt(self, key: str) -> str:
        return (
            f"[prompt for {key}]\n\n"
            "Marker dictionary: <MARKER_DICTIONARY>\n\n"
            "Rubric: <RUBRIC>\n"
        )

    def resolve_model(self, key: str) -> str:
        return "claude-sonnet-4-5"

    def call(self, **kwargs: Any) -> _LLMResp:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("Unexpected LLM.call")
        return self._responses.pop(0)


class _ScriptedCursor:
    def __init__(self, plan: list[tuple[str, Any]]) -> None:
        self._plan = plan
        self._idx = 0
        self.executed: list[tuple[str, Any]] = []
        self._next: tuple[str, Any] | None = None
        self.rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        if self._idx >= len(self._plan):
            raise AssertionError(
                f"execute past plan end (sql={sql[:80]!r})"
            )
        step = self._plan[self._idx]
        self.executed.append((sql, params))
        if step[0] == "execute_write":
            self._idx += 1
            self._next = None
            return
        self._next = step

    def fetchone(self) -> Any:
        assert self._next is not None and self._next[0] == "fetchone"
        out = self._next[1]
        self._idx += 1
        self._next = None
        return out

    def fetchall(self) -> list[Any]:
        assert self._next is not None and self._next[0] == "fetchall"
        out = self._next[1] or []
        self._idx += 1
        self._next = None
        return out

    def __enter__(self) -> "_ScriptedCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Transaction:
    def __init__(self, conn: "_ScriptedConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Transaction":
        self._conn.transactions_opened += 1
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _ScriptedConn:
    def __init__(self, plan: list[tuple[str, Any]]) -> None:
        self.cursor_obj = _ScriptedCursor(plan)
        self.transactions_opened = 0

    def cursor(self) -> _ScriptedCursor:
        return self.cursor_obj

    def transaction(self) -> _Transaction:
        return _Transaction(self)


def _make_conn(plan: list[tuple[str, Any]]) -> _ScriptedConn:
    return _ScriptedConn(plan)
