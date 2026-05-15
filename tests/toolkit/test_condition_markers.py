"""Hermetic tests for discover_condition_markers.

No DB connection: a scripted cursor returns prepared rows in order.
LLMClient is replaced with a fake that records calls and returns
prepared LLM responses. R2 is stubbed via `scraper.image_storage.is_configured`
returning False unless explicitly overridden, so the image phase is
a no-op by default.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from toolkit import condition_markers


_NOW = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)


# ---- Tool-schema invariants -----------------------------------------------


def test_tool_schema_has_all_required_top_level_fields():
    schema = condition_markers.RECORD_LISTING_MARKERS_TOOL["input_schema"]
    assert set(schema["required"]) == {"markers", "notes"}


def test_marker_entry_schema_has_all_required_fields():
    schema = condition_markers.RECORD_LISTING_MARKERS_TOOL["input_schema"]
    item = schema["properties"]["markers"]["items"]
    assert set(item["required"]) == {
        "marker_text", "scope", "evidence_quote",
        "sentiment", "suggested_level_implication", "source",
    }
    assert item["properties"]["scope"]["enum"] == ["building", "apartment"]
    assert item["properties"]["sentiment"]["enum"] == [
        "positive", "negative", "neutral",
    ]


def test_max_markers_is_bounded():
    schema = condition_markers.RECORD_LISTING_MARKERS_TOOL["input_schema"]
    assert schema["properties"]["markers"]["maxItems"] == 30


# ---- Cache hit path -------------------------------------------------------


def test_cache_hit_does_not_call_llm(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    markers = _example_markers()
    plan = [
        ("fetchone", (42, _NOW, {"text": "..."})),                # _resolve_snapshot
        ("fetchone", (markers, "ambiguities here", 5,             # _cache_lookup
                      "claude-sonnet-4-5", 0.0123)),
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([])

    res = condition_markers.discover_condition_markers(
        conn, llm,  # type: ignore[arg-type]
        sreality_id=123,
    )

    assert llm.calls == []
    assert res["data"]["cache_hit"] is True
    assert res["data"]["markers"] == markers
    assert res["data"]["notes"] == "ambiguities here"
    assert res["data"]["sreality_id"] == 123
    assert res["data"]["snapshot_id"] == 42


# ---- Cache miss path (no images) ------------------------------------------


def test_cache_miss_no_images_calls_llm_then_writes(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    markers = _example_markers()
    plan = [
        ("fetchone", (42, _NOW, {"text": "Po rekonstrukci"})),    # snapshot
        ("fetchone", None),                                       # cache miss
        ("fetchone", _listing_row()),                             # _fetch_listing
        ("execute_write", None),                                  # _cache_store
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_llm_response(markers, "no ambiguities")])

    res = condition_markers.discover_condition_markers(
        conn, llm,  # type: ignore[arg-type]
        sreality_id=123,
    )

    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["called_for"] == "discover_condition_markers"
    assert call["tools"][0]["name"] == "record_listing_markers"
    assert conn.transactions_opened == 1
    assert res["data"]["cache_hit"] is False
    assert res["data"]["markers"] == markers
    assert res["data"]["n_images"] == 0


# ---- Snapshot resolution --------------------------------------------------


def test_no_snapshot_raises(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    conn = _make_conn([("fetchone", None)])
    with pytest.raises(condition_markers.DiscoveryError, match="no snapshot"):
        condition_markers.discover_condition_markers(
            conn, _FakeLLM([]), sreality_id=999,  # type: ignore[arg-type]
        )


def test_explicit_snapshot_id_filters_by_both_columns(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (777, _NOW, {})),
        ("fetchone", (_example_markers(), "", 0, "claude-sonnet-4-5", 0.001)),
    ]
    conn = _make_conn(plan)
    condition_markers.discover_condition_markers(
        conn, _FakeLLM([]), sreality_id=123, snapshot_id=777,  # type: ignore[arg-type]
    )
    snap_sql = conn.cursor_obj.executed[0]
    assert "WHERE id = %s AND sreality_id = %s" in snap_sql[0]
    assert snap_sql[1] == (777, 123)


# ---- LLM response validation ----------------------------------------------


def test_missing_tool_call_raises(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", None),
        ("fetchone", _listing_row()),
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_LLMResp(text="oops", tool_calls=[])])
    with pytest.raises(condition_markers.DiscoveryError, match="did not invoke"):
        condition_markers.discover_condition_markers(
            conn, llm, sreality_id=123,  # type: ignore[arg-type]
        )


def test_marker_with_unknown_scope_rejected(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", None),
        ("fetchone", _listing_row()),
    ]
    conn = _make_conn(plan)
    bad = [{
        "marker_text": "zateplena budova", "scope": "neither",
        "evidence_quote": "x", "sentiment": "positive",
        "suggested_level_implication": "high", "source": "text",
    }]
    llm = _FakeLLM([_LLMResp(
        text="",
        tool_calls=[{"name": "record_listing_markers",
                     "input": {"markers": bad, "notes": ""}}],
    )])
    with pytest.raises(condition_markers.DiscoveryError, match="scope="):
        condition_markers.discover_condition_markers(
            conn, llm, sreality_id=123,  # type: ignore[arg-type]
        )


def test_empty_markers_list_is_accepted(monkeypatch):
    """A listing with no concrete condition markers is legitimate."""
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {"text": "Apartment for rent."})),
        ("fetchone", None),
        ("fetchone", _listing_row()),
        ("execute_write", None),
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_LLMResp(
        text="",
        tool_calls=[{"name": "record_listing_markers",
                     "input": {"markers": [], "notes": ""}}],
    )])
    res = condition_markers.discover_condition_markers(
        conn, llm, sreality_id=123,  # type: ignore[arg-type]
    )
    assert res["data"]["markers"] == []
    assert res["metadata"]["result_count"] == 0


def test_too_many_markers_rejected(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", None),
        ("fetchone", _listing_row()),
    ]
    conn = _make_conn(plan)
    too_many = [
        {
            "marker_text": f"marker {i}", "scope": "apartment",
            "evidence_quote": "x", "sentiment": "neutral",
            "suggested_level_implication": "low", "source": "text",
        }
        for i in range(31)
    ]
    llm = _FakeLLM([_LLMResp(
        text="",
        tool_calls=[{"name": "record_listing_markers",
                     "input": {"markers": too_many, "notes": ""}}],
    )])
    with pytest.raises(condition_markers.DiscoveryError, match="max is 30"):
        condition_markers.discover_condition_markers(
            conn, llm, sreality_id=123,  # type: ignore[arg-type]
        )


# ---- Prompt-too-long fallback --------------------------------------------


def test_prompt_too_long_retries_without_images(monkeypatch):
    """When the LLM rejects an image-heavy prompt for length, the tool
    transparently retries with image_blocks=[] and persists n_images=0.
    """
    _stub_image_storage(monkeypatch, configured=True)

    # Stub R2 + image-key fetch so the first call attempt does send images.
    fake_keys = ["img/1.jpg", "img/2.jpg", "img/3.jpg"]
    monkeypatch.setattr(
        condition_markers, "_fetch_image_keys",
        lambda conn, sid, n: fake_keys,
    )
    class _FakeR2:
        def download_bytes(self, key):
            return b"\xff\xd8\xff\xe0"  # tiny jpeg header — irrelevant for stub
    from scraper import image_storage
    monkeypatch.setattr(image_storage, "R2Client", type(
        "R2C", (), {"from_env": classmethod(lambda cls: _FakeR2())},
    ))

    from api.providers.base import ProviderError

    markers = _example_markers()
    plan = [
        ("fetchone", (42, _NOW, {"text": "..."})),     # snapshot
        ("fetchone", None),                            # cache miss
        ("fetchone", _listing_row()),                  # _fetch_listing
        ("execute_write", None),                       # _cache_store
    ]
    conn = _make_conn(plan)

    # First call raises prompt-too-long; second call succeeds.
    raising = _LLMResp(text="", tool_calls=[])
    class _TwoCallLLM(_FakeLLM):
        def call(self, **kwargs: Any) -> _LLMResp:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise ProviderError(
                    "anthropic call failed: Error code: 400 - "
                    "prompt is too long: 210659 tokens > 200000 maximum"
                )
            return _llm_response(markers, "")

    llm = _TwoCallLLM([raising, _llm_response(markers, "")])

    res = condition_markers.discover_condition_markers(
        conn, llm, sreality_id=123,  # type: ignore[arg-type]
    )

    assert len(llm.calls) == 2
    # First attempt sent image blocks; second did not.
    first_content = llm.calls[0]["messages"][0]["content"]
    second_content = llm.calls[1]["messages"][0]["content"]
    assert any(b.get("type") == "image" for b in first_content)
    assert not any(b.get("type") == "image" for b in second_content)
    assert res["data"]["markers"] == markers
    assert res["data"]["n_images"] == 0


def test_other_provider_errors_propagate(monkeypatch):
    """Provider errors that aren't 'prompt is too long' must NOT trigger
    the fallback — they should surface to the caller."""
    _stub_image_storage(monkeypatch, configured=False)
    from api.providers.base import ProviderError

    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", None),
        ("fetchone", _listing_row()),
    ]
    conn = _make_conn(plan)
    class _OnlyRaises(_FakeLLM):
        def call(self, **kwargs: Any) -> _LLMResp:
            self.calls.append(kwargs)
            raise ProviderError("anthropic call failed: 503 Service Unavailable")
    llm = _OnlyRaises([])
    with pytest.raises(ProviderError, match="503"):
        condition_markers.discover_condition_markers(
            conn, llm, sreality_id=123,  # type: ignore[arg-type]
        )
    assert len(llm.calls) == 1  # no retry


# ---- Envelope -------------------------------------------------------------


def test_envelope_metadata_shape(monkeypatch):
    _stub_image_storage(monkeypatch, configured=False)
    plan = [
        ("fetchone", (42, _NOW, {})),
        ("fetchone", (_example_markers(), "", 0, "claude-sonnet-4-5", 0.005)),
    ]
    conn = _make_conn(plan)
    res = condition_markers.discover_condition_markers(
        conn, _FakeLLM([]), sreality_id=123,  # type: ignore[arg-type]
    )
    md = res["metadata"]
    assert md["tool"] == "discover_condition_markers"
    assert md["filters_used"] == {
        "sreality_id": 123, "snapshot_id": None,
        "n_images": 5, "force_refresh": False,
    }
    assert md["result_count"] == len(_example_markers())
    assert md["data_freshness"] == _NOW.isoformat()
    assert md["queried_at"]


# ---- Helpers --------------------------------------------------------------


def _stub_image_storage(monkeypatch, *, configured: bool) -> None:
    from scraper import image_storage
    monkeypatch.setattr(image_storage, "is_configured", lambda: configured)


def _example_markers() -> list[dict[str, Any]]:
    return [
        {
            "marker_text": "zateplená budova", "scope": "building",
            "evidence_quote": "Dům je po zateplení",
            "sentiment": "positive",
            "suggested_level_implication": "high", "source": "text",
        },
        {
            "marker_text": "po kompletní rekonstrukci", "scope": "apartment",
            "evidence_quote": "Byt po kompletní rekonstrukci",
            "sentiment": "positive",
            "suggested_level_implication": "high", "source": "text",
        },
    ]


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
    cost_usd: float = 0.0042
    llm_call_id: int = 555
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    duration_ms: int = 0


def _llm_response(markers: list[dict[str, Any]], notes: str) -> _LLMResp:
    return _LLMResp(
        text="",
        tool_calls=[{
            "name": "record_listing_markers",
            "input": {"markers": markers, "notes": notes},
        }],
    )


class _FakeLLM:
    def __init__(self, responses: list[_LLMResp]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def resolve_system_prompt(self, key: str) -> str:
        return f"[prompt for {key}]"

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
