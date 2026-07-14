"""Hermetic tests for the operator-editable per-family vision-model routing
(toolkit.dedup_model_overrides)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from toolkit.dedup_model_overrides import (
    SITE_PLAN_OVERRIDE_KEY,
    load_model_overrides,
    resolve_model_for_family,
    set_family_site_plan_model,
    site_plan_model_overrides_view,
)


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._row: tuple[Any, ...] | None = None

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        if s.startswith("SELECT value FROM app_settings"):
            self._row = (self._conn.value,) if self._conn.value is not None else None
        elif s.startswith("INSERT INTO app_settings"):
            self._conn.written = params
            self._row = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, value: Any = None) -> None:
        self.value = value
        self.written: Any = None

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Ctx:
        return _Ctx()


class _FakeLLMClient:
    def __init__(self, default_model: str = "gpt-5-mini") -> None:
        self.default_model = default_model
        self.resolved_keys: list[str] = []

    def resolve_model(self, key: str) -> str:
        self.resolved_keys.append(key)
        return self.default_model


def test_load_overrides_validates_and_ignores_junk() -> None:
    conn = _FakeConn({
        "pozemek": "claude-sonnet-4-5",
        "dum": "",                       # blank -> ignored
        "bogus_family": "claude-sonnet-4-5",  # unknown family -> ignored
        "komercni": 123,                 # non-string -> ignored
    })
    out = load_model_overrides(conn, SITE_PLAN_OVERRIDE_KEY)
    assert out == {"pozemek": "claude-sonnet-4-5"}


def test_load_overrides_empty_when_absent_or_malformed() -> None:
    assert load_model_overrides(_FakeConn(None), SITE_PLAN_OVERRIDE_KEY) == {}
    assert load_model_overrides(_FakeConn(["not", "a", "dict"]), SITE_PLAN_OVERRIDE_KEY) == {}


def test_resolve_model_for_family_overridden_family_wins() -> None:
    conn = _FakeConn({"pozemek": "claude-sonnet-4-5"})
    llm = _FakeLLMClient(default_model="gpt-5-mini")
    model = resolve_model_for_family(
        conn, llm, setting_key=SITE_PLAN_OVERRIDE_KEY,
        default_key="llm_site_plan_match_model", family="pozemek",
    )
    assert model == "claude-sonnet-4-5"


def test_resolve_model_for_family_falls_back_for_unoverridden_family() -> None:
    conn = _FakeConn({"pozemek": "claude-sonnet-4-5"})
    llm = _FakeLLMClient(default_model="gpt-5-mini")
    assert resolve_model_for_family(
        conn, llm, setting_key=SITE_PLAN_OVERRIDE_KEY,
        default_key="llm_site_plan_match_model", family="byt",
    ) == "gpt-5-mini"
    # None family (e.g. an unclassified pair) also falls back, never KeyErrors.
    assert resolve_model_for_family(
        conn, llm, setting_key=SITE_PLAN_OVERRIDE_KEY,
        default_key="llm_site_plan_match_model", family=None,
    ) == "gpt-5-mini"


def test_resolve_model_for_family_uses_preloaded_overrides_without_requerying() -> None:
    conn = _FakeConn(None)  # would resolve empty if re-queried
    llm = _FakeLLMClient(default_model="gpt-5-mini")
    model = resolve_model_for_family(
        conn, llm, setting_key=SITE_PLAN_OVERRIDE_KEY,
        default_key="llm_site_plan_match_model", family="pozemek",
        overrides={"pozemek": "claude-sonnet-4-5"},
    )
    assert model == "claude-sonnet-4-5"


def test_site_plan_model_overrides_view_reports_defaults_and_overrides() -> None:
    conn = _FakeConn({"pozemek": "claude-sonnet-4-5"})
    llm = _FakeLLMClient(default_model="gpt-5-mini")
    view = {v["family"]: v for v in site_plan_model_overrides_view(conn, llm)}
    assert len(view) == 5
    assert view["byt"]["is_override"] is False
    assert view["byt"]["model"] == "gpt-5-mini"
    assert view["pozemek"]["is_override"] is True
    assert view["pozemek"]["model"] == "claude-sonnet-4-5"
    assert view["pozemek"]["default_model"] == "gpt-5-mini"


def test_set_family_site_plan_model_persists_and_merges_blob() -> None:
    conn = _FakeConn({"byt": "gpt-5-mini"})  # an existing entry on another family
    stored = set_family_site_plan_model(conn, "pozemek", "claude-sonnet-4-5")
    assert stored == {"byt": "gpt-5-mini", "pozemek": "claude-sonnet-4-5"}
    written_blob = json.loads(conn.written[1])
    assert written_blob == stored


def test_set_family_site_plan_model_clears_on_none() -> None:
    conn = _FakeConn({"pozemek": "claude-sonnet-4-5", "byt": "gpt-5-mini"})
    stored = set_family_site_plan_model(conn, "pozemek", None)
    assert stored == {"byt": "gpt-5-mini"}


def test_set_family_site_plan_model_rejects_unknown_family() -> None:
    with pytest.raises(ValueError):
        set_family_site_plan_model(_FakeConn(None), "garaz", "claude-sonnet-4-5")


def test_set_family_site_plan_model_rejects_blank_model() -> None:
    with pytest.raises(ValueError):
        set_family_site_plan_model(_FakeConn(None), "pozemek", "   ")
