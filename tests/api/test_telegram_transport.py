"""Hermetic tests for the Telegram transport (Sprint N PR 4). No network."""

from __future__ import annotations

from typing import Any

import pytest

import api.transports.telegram as tg
from api.transports.base import RenderedMessage, TransportError

_MSG = RenderedMessage(
    body_text="Zlevněno: 2+kk Praha\n5 000 000 Kč → 4 500 000 Kč",
    deep_link="https://app.example/listing/7",
    subject="Zlevněno: 2+kk Praha",
)


class _Resp:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = b"x" if payload is not None or text else b""

    def json(self) -> dict[str, Any]:
        return self._payload or {}


def test_is_configured_false_without_token(monkeypatch: Any) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    t = tg.Telegram()
    assert t.is_configured() is False
    with pytest.raises(TransportError):
        t.send(recipient="123456", message=_MSG)


def test_send_success_maps_message_id(monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    captured: dict[str, Any] = {}

    def _fake_post(url: str, json: dict[str, Any], timeout: int) -> _Resp:
        captured["url"] = url
        captured["json"] = json
        return _Resp(200, {"ok": True, "result": {"message_id": 99}})

    monkeypatch.setattr(tg.requests, "post", _fake_post)
    result = tg.Telegram().send(recipient="555", message=_MSG)

    assert result.status == "sent"
    assert result.provider_message_id == "99"
    assert captured["url"].endswith("/botbot-token/sendMessage")
    assert captured["json"]["chat_id"] == "555"
    assert _MSG.deep_link in captured["json"]["text"]


def test_send_ok_false_is_failed(monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setattr(
        tg.requests, "post",
        lambda *a, **k: _Resp(200, {"ok": False, "description": "chat not found"}),
    )
    result = tg.Telegram().send(recipient="555", message=_MSG)
    assert result.status == "failed"
    assert "chat not found" in (result.error or "")


def test_send_http_error_is_failed(monkeypatch: Any) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setattr(
        tg.requests, "post", lambda *a, **k: _Resp(429, text="Too Many Requests"),
    )
    result = tg.Telegram().send(recipient="555", message=_MSG)
    assert result.status == "failed"
    assert "429" in (result.error or "")
