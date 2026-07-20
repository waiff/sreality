"""Config sanity for the two thin OpenAICompatibleProvider subclasses.

Wire-format behavior (block translation, tool_choice, usage extraction) is
covered once in test_openai_compatible.py; these just check each subclass
wires the right base_url / api_key env / max_tokens_param / prices — the
only things that actually differ between them.
"""

from __future__ import annotations

from typing import Any

from api.providers.openai import OpenAIProvider
from api.providers.openai import PRICES as OPENAI_PRICES
from api.providers.qwen import PRICES as QWEN_PRICES
from api.providers.qwen import QwenProvider


class _FakeSession:
    def post(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - not exercised
        raise AssertionError("no HTTP call expected in a config-only test")


def test_openai_provider_config():
    p = OpenAIProvider(api_key="k", session=_FakeSession())
    assert p.name == "openai"
    assert p._base_url == "https://api.openai.com/v1"
    assert p._api_key_env == "OPENAI_API_KEY"
    assert p._max_tokens_param == "max_completion_tokens"
    assert "gpt-5-mini" in OPENAI_PRICES
    assert p.price_for("gpt-5-mini") == OPENAI_PRICES["gpt-5-mini"]


def test_openai_provider_reads_env_when_no_explicit_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    p = OpenAIProvider(session=_FakeSession())
    assert p._api_key == "env-key"


def test_qwen_provider_config():
    p = QwenProvider(api_key="k", session=_FakeSession())
    assert p.name == "qwen"
    assert p._base_url == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    assert p._api_key_env == "QWEN_API_KEY"
    assert p._max_tokens_param == "max_tokens"
    assert "qwen3-vl-235b-a22b-instruct" in QWEN_PRICES
    assert "qwen3-vl-30b-a3b-instruct" in QWEN_PRICES


def test_qwen_provider_reads_env_when_no_explicit_key(monkeypatch):
    monkeypatch.setenv("QWEN_API_KEY", "env-key")
    p = QwenProvider(session=_FakeSession())
    assert p._api_key == "env-key"
