"""Hermetic tests for compare_listing_images.

No DB connection, no R2 calls: scripted cursor + fake R2Client +
fake LLMClient.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from toolkit import image_similarity as ic


_NOW = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)


# ---- Tool-schema invariants -----------------------------------------------


def test_tool_schema_lists_all_six_dimensions():
    schema = ic.RECORD_IMAGE_COMPARISON_TOOL["input_schema"]
    dims = schema["properties"]["dimensions"]["properties"]
    assert set(dims.keys()) == {
        "exterior", "kitchen", "windows_and_light",
        "floor_finish", "lighting", "styling",
    }
    for dim in dims.values():
        required = set(dim["required"])
        assert required == {"score", "observed", "reasoning"}


def test_dimensions_constant_matches_schema():
    schema = ic.RECORD_IMAGE_COMPARISON_TOOL["input_schema"]
    dims = schema["properties"]["dimensions"]["properties"]
    assert set(ic.DIMENSIONS) == set(dims.keys())


# ---- Validation -----------------------------------------------------------


def test_self_compare_raises():
    conn = _make_conn([])
    llm = _FakeLLM([])
    with pytest.raises(ic.ImageCompareError, match="itself"):
        ic.compare_listing_images(
            conn, llm, sreality_id_a=5, sreality_id_b=5,  # type: ignore[arg-type]
        )


def test_canonicalises_pair_order(monkeypatch):
    """Canonical order is by listing_id, NOT sreality_id — the sreality_id with
    the smaller listing_id must land in slot A even when its sreality_id is
    numerically larger (the exact 77%-of-rows-sort-differently case the R2
    identity chain's PR3 exists to fix)."""
    _patch_r2_configured(monkeypatch, True)
    monkeypatch.setattr(ic.image_storage.R2Client, "from_env", classmethod(lambda cls: _FakeR2()))

    plan = [
        ("fetchall", [(20, 5), (10, 8)]),  # resolve: sreality_id 20 -> listing_id 5, 10 -> 8
        ("fetchone", _comparison_row()),   # cache hit on (5, 8)
        ("fetchone", (_NOW,)),             # last_seen lookup
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([])

    res = ic.compare_listing_images(
        conn, llm, sreality_id_a=20, sreality_id_b=10,  # type: ignore[arg-type]
    )

    cache_sql = conn.cursor_obj.executed[1]
    assert cache_sql[1] == (5, 8)
    # sreality_id 20 carries the smaller listing_id (5) -> canonical slot A.
    assert res["data"]["sreality_id_a"] == 20
    assert res["data"]["sreality_id_b"] == 10


def test_unresolvable_sreality_id_raises():
    plan = [("fetchall", [(10, 100)])]  # sreality_id 20 not found in listings
    conn = _make_conn(plan)
    with pytest.raises(ic.ImageCompareError, match="20"):
        ic.compare_listing_images(
            conn, _FakeLLM([]),  # type: ignore[arg-type]
            sreality_id_a=10, sreality_id_b=20,
        )


# ---- Cache hit path -------------------------------------------------------


def test_cache_hit_does_not_call_r2_or_llm(monkeypatch):
    _patch_r2_configured(monkeypatch, True)

    plan = [
        ("fetchall", [(10, 100), (20, 200)]),  # resolve listing_ids
        ("fetchone", _comparison_row()),
        ("fetchone", (_NOW,)),  # max(last_seen)
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([])

    res = ic.compare_listing_images(
        conn, llm, sreality_id_a=10, sreality_id_b=20,  # type: ignore[arg-type]
    )

    assert llm.calls == []
    assert res["data"]["cache_hit"] is True
    assert res["data"]["comparison"]["overall_similarity"] == 0.75


# ---- Cache miss path ------------------------------------------------------


def test_cache_miss_fetches_images_and_calls_vision(monkeypatch):
    _patch_r2_configured(monkeypatch, True)
    fake_r2 = _FakeR2()
    monkeypatch.setattr(
        ic.image_storage.R2Client, "from_env",
        classmethod(lambda cls: fake_r2),
    )

    comparison = _example_comparison()
    plan = [
        ("fetchall", [(10, 100), (20, 200)]),     # resolve listing_ids
        ("fetchone", None),                       # cache miss
        ("fetchall", [("10/0000.jpg",), ("10/0001.jpg",)]),  # images A
        ("fetchall", [("20/0000.jpg",)]),                     # images B
        ("execute_write", None),                  # cache write
        ("fetchone", (_NOW,)),                    # max(last_seen)
    ]
    conn = _make_conn(plan)
    llm = _FakeLLM([_llm_response(comparison)])

    res = ic.compare_listing_images(
        conn, llm, sreality_id_a=10, sreality_id_b=20,  # type: ignore[arg-type]
    )

    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["called_for"] == "compare_listing_images"
    assert call["tools"][0]["name"] == "record_image_comparison"

    # Message content: text + 2 images for A, text + 1 image for B, then prompt.
    content = call["messages"][0]["content"]
    image_blocks = [b for b in content if b.get("type") == "image"]
    assert len(image_blocks) == 3
    for block in image_blocks:
        assert block["source"]["type"] == "base64"
        assert block["source"]["media_type"] == "image/jpeg"

    assert fake_r2.downloads == ["10/0000.jpg", "10/0001.jpg", "20/0000.jpg"]
    assert conn.transactions_opened == 1
    assert res["data"]["cache_hit"] is False
    assert res["data"]["n_images_a"] == 2
    assert res["data"]["n_images_b"] == 1


def test_no_images_raises(monkeypatch):
    _patch_r2_configured(monkeypatch, True)
    monkeypatch.setattr(
        ic.image_storage.R2Client, "from_env",
        classmethod(lambda cls: _FakeR2()),
    )
    plan = [
        ("fetchall", [(10, 100), (20, 200)]),  # resolve listing_ids
        ("fetchone", None),
        ("fetchall", []),  # listing A has none
    ]
    conn = _make_conn(plan)
    with pytest.raises(ic.ImageCompareError, match="no R2-stored images"):
        ic.compare_listing_images(
            conn, _FakeLLM([]),  # type: ignore[arg-type]
            sreality_id_a=10, sreality_id_b=20,
        )


def test_r2_not_configured_raises(monkeypatch):
    _patch_r2_configured(monkeypatch, False)
    plan = [
        ("fetchall", [(10, 100), (20, 200)]),  # resolve listing_ids
        ("fetchone", None),
    ]
    conn = _make_conn(plan)
    with pytest.raises(ic.ImageCompareError, match="R2 is not configured"):
        ic.compare_listing_images(
            conn, _FakeLLM([]),  # type: ignore[arg-type]
            sreality_id_a=10, sreality_id_b=20,
        )


# ---- LLM response validation ----------------------------------------------


def test_missing_dimension_raises(monkeypatch):
    _patch_r2_configured(monkeypatch, True)
    monkeypatch.setattr(
        ic.image_storage.R2Client, "from_env",
        classmethod(lambda cls: _FakeR2()),
    )
    plan = [
        ("fetchall", [(10, 100), (20, 200)]),  # resolve listing_ids
        ("fetchone", None),
        ("fetchall", [("10/0000.jpg",)]),
        ("fetchall", [("20/0000.jpg",)]),
    ]
    conn = _make_conn(plan)
    bad = {
        "dimensions": {dim: _dim_payload() for dim in ic.DIMENSIONS[:5]},
        "overall_similarity": 0.5, "summary": "...",
    }
    llm = _FakeLLM([_LLMResp(
        text="", tool_calls=[{"name": "record_image_comparison", "input": bad}],
    )])
    with pytest.raises(ic.ImageCompareError, match="missing dimension"):
        ic.compare_listing_images(
            conn, llm, sreality_id_a=10, sreality_id_b=20,  # type: ignore[arg-type]
        )


# ---- Envelope -------------------------------------------------------------


def test_envelope_metadata_shape(monkeypatch):
    _patch_r2_configured(monkeypatch, True)
    plan = [
        ("fetchall", [(10, 100), (20, 200)]),  # resolve listing_ids
        ("fetchone", _comparison_row()),
        ("fetchone", (_NOW,)),
    ]
    conn = _make_conn(plan)
    res = ic.compare_listing_images(
        conn, _FakeLLM([]),  # type: ignore[arg-type]
        sreality_id_a=10, sreality_id_b=20, n_images=4,
    )
    md = res["metadata"]
    assert md["tool"] == "compare_listing_images"
    assert md["filters_used"] == {
        "sreality_id_a": 10, "sreality_id_b": 20,
        "n_images": 4, "force_refresh": False,
    }
    assert md["result_count"] == 1
    assert md["data_freshness"] == _NOW.isoformat()


# ---- Helpers --------------------------------------------------------------


def _patch_r2_configured(monkeypatch: pytest.MonkeyPatch, configured: bool) -> None:
    monkeypatch.setattr(ic.image_storage, "is_configured", lambda: configured)


def _example_comparison() -> dict[str, Any]:
    return {
        "dimensions": {dim: _dim_payload() for dim in ic.DIMENSIONS},
        "overall_similarity": 0.75,
        "summary": "Strong match on exterior; kitchen differs.",
    }


def _dim_payload() -> dict[str, Any]:
    return {"score": 0.75, "observed": True, "reasoning": "looks similar"}


def _comparison_row() -> tuple[Any, ...]:
    return (_example_comparison(), 3, 2, "claude-sonnet-4-5", 0.05)


@dataclass
class _LLMResp:
    text: str
    tool_calls: list[dict[str, Any]]
    model: str = "claude-sonnet-4-5"
    cost_usd: float = 0.05
    llm_call_id: int = 1234
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    duration_ms: int = 0
    raw: Any = None


def _llm_response(comparison: dict[str, Any]) -> _LLMResp:
    return _LLMResp(
        text="",
        tool_calls=[{"name": "record_image_comparison", "input": comparison}],
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


class _FakeR2:
    def __init__(self) -> None:
        self.downloads: list[str] = []

    def download_bytes(self, key: str) -> bytes:
        self.downloads.append(key)
        return b"\xff\xd8\xff\xe0fake-jpeg"


class _ScriptedCursor:
    def __init__(self, plan: list[tuple[str, Any]]) -> None:
        self._plan = plan
        self._idx = 0
        self.executed: list[tuple[str, Any]] = []
        self._next: tuple[str, Any] | None = None

    def execute(self, sql: str, params: Any = None) -> None:
        if self._idx >= len(self._plan):
            raise AssertionError(f"execute past plan end (sql={sql[:80]!r})")
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
