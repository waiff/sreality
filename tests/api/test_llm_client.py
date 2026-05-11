"""Tests for api.llm_client. Hermetic — no real Anthropic calls, no real DB."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from api import llm_client as lc


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._last: list[Any] | None = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | dict[str, Any] = ()) -> None:
        sql_norm = " ".join(sql.split()).lower()
        if sql_norm.startswith("select value from app_settings"):
            key = params[0] if isinstance(params, tuple) else params["key"]
            value = self._conn.app_settings.get(key)
            self._last = [value] if value is not None else None
        elif sql_norm.startswith("insert into llm_calls"):
            row_id = self._conn.next_id
            self._conn.next_id += 1
            self._conn.llm_calls_rows.append({"id": row_id, "params": params})
            self._last = [row_id]
        elif "sum(cost_usd)" in sql_norm:
            total = sum(
                row["params"][6] for row in self._conn.llm_calls_rows
            )
            self._last = [total]
        else:
            self._last = None

    def fetchone(self) -> Any:
        return self._last


class _FakeConn:
    def __init__(
        self,
        app_settings: dict[str, Any] | None = None,
    ) -> None:
        self.app_settings: dict[str, Any] = dict(app_settings or {})
        self.llm_calls_rows: list[dict[str, Any]] = []
        self.next_id = 1

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    @contextmanager
    def transaction(self):
        yield self


class _Block:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _FakeUsage(dict):
    pass


class _FakeRaw:
    def __init__(
        self,
        text: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        blocks: list[_Block] = []
        if text:
            blocks.append(_Block(type="text", text=text))
        for tc in tool_calls or []:
            blocks.append(_Block(
                type="tool_use",
                id=tc.get("id", "tu_1"),
                name=tc["name"],
                input=tc.get("input", {}),
            ))
        self.content = blocks
        self.usage = _FakeUsage(usage or {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        })


class _FakeAnthropic:
    def __init__(self, raw: _FakeRaw, *, error: Exception | None = None) -> None:
        self._raw = raw
        self._error = error
        self.calls: list[dict[str, Any]] = []
        self.messages = self  # so anthropic.messages.create works

    def create(self, **kwargs: Any) -> _FakeRaw:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._raw


def _patch_anthropic(monkeypatch, fake: _FakeAnthropic) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake_module = type("FakeAnthropicModule", (), {})()
    fake_module.Anthropic = lambda api_key=None: fake
    monkeypatch.setitem(__import__("sys").modules, "anthropic", fake_module)


# ----------------------------------------------------------------------
# compute_cost_usd
# ----------------------------------------------------------------------

def test_cost_zero_when_no_tokens():
    assert lc.compute_cost_usd(
        model="claude-sonnet-4-5",
        input_tokens=0, output_tokens=0,
    ) == 0.0


def test_cost_input_output_only_sonnet_45():
    # 10k input, 1k output: (10000 * 3 + 1000 * 15) / 1e6 = 0.045
    cost = lc.compute_cost_usd(
        model="claude-sonnet-4-5",
        input_tokens=10_000, output_tokens=1_000,
    )
    assert cost == pytest.approx(0.045)


def test_cost_includes_cache_tokens():
    # 1000 input + 9000 cache_read + 5000 cache_write + 500 output:
    # (1000*3 + 500*15 + 9000*0.30 + 5000*3.75) / 1e6
    # = (3000 + 7500 + 2700 + 18750) / 1e6 = 31950 / 1e6 = 0.03195
    cost = lc.compute_cost_usd(
        model="claude-sonnet-4-5",
        input_tokens=1_000, output_tokens=500,
        cache_read_tokens=9_000, cache_write_tokens=5_000,
    )
    assert cost == pytest.approx(0.03195)


def test_cost_unknown_model_returns_zero_and_warns(caplog):
    with caplog.at_level("WARNING"):
        cost = lc.compute_cost_usd(
            model="claude-unknown",
            input_tokens=1_000, output_tokens=1_000,
        )
    assert cost == 0.0
    assert any("no price configured" in m for m in caplog.messages)


# ----------------------------------------------------------------------
# resolve_model / resolve_system_prompt
# ----------------------------------------------------------------------

def test_resolve_model_reads_app_settings():
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-6"})
    client = lc.LLMClient(conn, api_key="x")
    assert client.resolve_model() == "claude-sonnet-4-6"


def test_resolve_model_falls_back_when_row_missing():
    conn = _FakeConn(app_settings={})
    client = lc.LLMClient(conn, api_key="x")
    assert client.resolve_model() == lc.DEFAULT_MODEL


def test_resolve_system_prompt_reads_app_settings():
    conn = _FakeConn(app_settings={"llm_parse_system_prompt": "Be excellent."})
    client = lc.LLMClient(conn, api_key="x")
    assert client.resolve_system_prompt() == "Be excellent."


def test_resolve_system_prompt_fallback_when_missing(caplog):
    conn = _FakeConn(app_settings={})
    client = lc.LLMClient(conn, api_key="x")
    with caplog.at_level("WARNING"):
        prompt = client.resolve_system_prompt()
    assert prompt == lc.DEFAULT_SYSTEM_PROMPT_FALLBACK
    assert any("missing" in m for m in caplog.messages)


# ----------------------------------------------------------------------
# call() end-to-end
# ----------------------------------------------------------------------

def test_call_records_llm_calls_row(monkeypatch):
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    fake = _FakeAnthropic(_FakeRaw(
        text="Hello",
        usage={
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        },
    ))
    _patch_anthropic(monkeypatch, fake)

    client = lc.LLMClient(conn)
    resp = client.call(
        called_for="parse_url",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert resp.text == "Hello"
    assert resp.input_tokens == 100
    assert resp.output_tokens == 50
    assert resp.model == "claude-sonnet-4-5"
    # cost: 100*3/1e6 + 50*15/1e6 = 0.0003 + 0.00075 = 0.00105
    assert resp.cost_usd == pytest.approx(0.00105)
    assert resp.llm_call_id == 1
    assert len(conn.llm_calls_rows) == 1
    params = conn.llm_calls_rows[0]["params"]
    assert params[0] == "parse_url"           # called_for
    assert params[1] == "claude-sonnet-4-5"   # model
    assert params[2] == 100                    # input_tokens
    assert params[3] == 50                     # output_tokens
    assert params[8] is None                   # estimation_run_id


def test_call_extracts_tool_use_blocks(monkeypatch):
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    fake = _FakeAnthropic(_FakeRaw(
        tool_calls=[{"name": "record_listing", "input": {"area_m2": 65}}],
        usage={"input_tokens": 200, "output_tokens": 10,
               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    ))
    _patch_anthropic(monkeypatch, fake)

    client = lc.LLMClient(conn)
    resp = client.call(
        called_for="parse_url",
        messages=[{"role": "user", "content": "x"}],
        tools=[{"name": "record_listing", "input_schema": {}}],
    )
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0]["name"] == "record_listing"
    assert resp.tool_calls[0]["input"] == {"area_m2": 65}


def test_call_propagates_estimation_run_id(monkeypatch):
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    fake = _FakeAnthropic(_FakeRaw(
        text="ok",
        usage={"input_tokens": 1, "output_tokens": 1,
               "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    ))
    _patch_anthropic(monkeypatch, fake)
    client = lc.LLMClient(conn)
    client.call(
        called_for="parse_url",
        messages=[{"role": "user", "content": "x"}],
        estimation_run_id=42,
    )
    assert conn.llm_calls_rows[0]["params"][8] == 42


def test_call_propagates_anthropic_errors(monkeypatch):
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    fake = _FakeAnthropic(
        _FakeRaw(),
        error=RuntimeError("rate limited"),
    )
    _patch_anthropic(monkeypatch, fake)
    client = lc.LLMClient(conn)
    with pytest.raises(RuntimeError, match="rate limited"):
        client.call(
            called_for="parse_url",
            messages=[{"role": "user", "content": "x"}],
        )
    # No row written if the call failed.
    assert conn.llm_calls_rows == []


def test_call_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    client = lc.LLMClient(conn, api_key=None)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        client.call(
            called_for="parse_url",
            messages=[{"role": "user", "content": "x"}],
        )


# ----------------------------------------------------------------------
# Daily-cost soft guardrail
# ----------------------------------------------------------------------

def _heavy_call(monkeypatch, conn, cost_input_tokens: int, cost_output_tokens: int):
    """Helper: run a call with a usage that produces a known cost.

    Sonnet 4.5 = $3 input / $15 output per MTok. Pass big-enough token
    counts to push cost over the threshold in one shot.
    """
    fake = _FakeAnthropic(_FakeRaw(
        text="ok",
        usage={
            "input_tokens": cost_input_tokens,
            "output_tokens": cost_output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    ))
    _patch_anthropic(monkeypatch, fake)
    client = lc.LLMClient(conn)
    return client.call(
        called_for="parse_url",
        messages=[{"role": "user", "content": "x"}],
    )


def test_cost_guard_does_not_warn_under_threshold(monkeypatch, caplog):
    monkeypatch.setenv("LLM_DAILY_COST_WARN_USD", "5.0")
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    with caplog.at_level("WARNING"):
        # 1M input + 100K output = $3 + $1.5 = $4.50, under $5.
        _heavy_call(monkeypatch, conn, 1_000_000, 100_000)
    assert not any("crossed soft threshold" in m for m in caplog.messages)


def test_cost_guard_warns_on_threshold_crossing(monkeypatch, caplog):
    monkeypatch.setenv("LLM_DAILY_COST_WARN_USD", "5.0")
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    with caplog.at_level("WARNING"):
        # 2M input + 1M output = $6 + $15 = $21, over $5.
        _heavy_call(monkeypatch, conn, 2_000_000, 1_000_000)
    assert any("crossed soft threshold" in m for m in caplog.messages)


def test_cost_guard_does_not_re_warn_after_first_crossing(monkeypatch, caplog):
    monkeypatch.setenv("LLM_DAILY_COST_WARN_USD", "5.0")
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})
    # First call crosses threshold and warns.
    with caplog.at_level("WARNING"):
        _heavy_call(monkeypatch, conn, 2_000_000, 1_000_000)
    first_warnings = [m for m in caplog.messages if "crossed" in m]
    assert len(first_warnings) == 1
    # Second call adds more cost but should NOT warn again — we already did.
    caplog.clear()
    with caplog.at_level("WARNING"):
        _heavy_call(monkeypatch, conn, 100_000, 10_000)
    assert not any("crossed" in m for m in caplog.messages)


def test_cost_guard_uses_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("LLM_DAILY_COST_WARN_USD", raising=False)
    assert lc._resolve_threshold() == lc.DEFAULT_DAILY_COST_WARN_USD


def test_cost_guard_falls_back_on_invalid_env(monkeypatch, caplog):
    monkeypatch.setenv("LLM_DAILY_COST_WARN_USD", "not a number")
    with caplog.at_level("WARNING"):
        assert lc._resolve_threshold() == lc.DEFAULT_DAILY_COST_WARN_USD
    assert any("invalid" in m for m in caplog.messages)


def test_cost_guard_swallows_db_errors_silently(monkeypatch, caplog):
    """If the SUM query fails, we must not break the call's return path."""
    monkeypatch.setenv("LLM_DAILY_COST_WARN_USD", "5.0")
    conn = _FakeConn(app_settings={"llm_parse_model": "claude-sonnet-4-5"})

    real_cursor = conn.cursor

    def broken_cursor():
        cur = real_cursor()
        original_execute = cur.execute

        def execute(sql: str, params: Any = ()) -> None:
            if "sum(cost_usd)" in sql.lower():
                raise RuntimeError("boom")
            return original_execute(sql, params)

        cur.execute = execute
        return cur

    conn.cursor = broken_cursor
    # Should still return a valid response.
    resp = _heavy_call(monkeypatch, conn, 100, 50)
    assert resp.text == "ok"


def test_parse_tool_input_json_handles_dict_and_string():
    assert lc.parse_tool_input_json({"a": 1}) == {"a": 1}
    assert lc.parse_tool_input_json('{"a": 2}') == {"a": 2}
    with pytest.raises(ValueError):
        lc.parse_tool_input_json(123)
