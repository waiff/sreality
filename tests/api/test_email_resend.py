"""Hermetic tests for the Resend email transport (Sprint N PR 2).

No network: `requests.post` is monkeypatched. Verifies the configured-gate,
the success/failed SendResult mapping, and that a missing key fails at send().
"""

from __future__ import annotations

from typing import Any

import pytest

import api.transports.email_resend as er
from api.transports.base import RenderedMessage, TransportError

_MSG = RenderedMessage(
    body_text="Nový 2+kk Praha 2 — 6 900 000 Kč",
    deep_link="https://app.example/listing/123",
    subject="Hlídač: nový 2+kk",
)


class _Resp:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = b"x" if payload is not None or text else b""

    def json(self) -> dict[str, Any]:
        return self._payload or {}


def _configure(monkeypatch: Any) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("EMAIL_FROM", "alerts@example.cz")


def test_is_configured_false_without_env(monkeypatch: Any) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("EMAIL_FROM", raising=False)
    t = er.ResendEmail()
    assert t.is_configured() is False
    with pytest.raises(TransportError):
        t.send(recipient="op@example.cz", message=_MSG)


def test_send_success_maps_provider_id(monkeypatch: Any) -> None:
    _configure(monkeypatch)
    captured: dict[str, Any] = {}

    def _fake_post(url: str, json: dict[str, Any], headers: dict[str, str], timeout: int) -> _Resp:
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _Resp(200, {"id": "re_abc123"})

    monkeypatch.setattr(er.requests, "post", _fake_post)
    result = er.ResendEmail().send(recipient="op@example.cz", message=_MSG)

    assert result.status == "sent"
    assert result.provider_message_id == "re_abc123"
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["json"]["to"] == ["op@example.cz"]
    assert captured["json"]["from"] == "alerts@example.cz"
    assert captured["json"]["subject"] == "Hlídač: nový 2+kk"
    assert _MSG.deep_link in captured["json"]["text"]
    assert captured["headers"]["Authorization"] == "Bearer re_test_key"


def test_send_http_error_is_failed_not_raised(monkeypatch: Any) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(
        er.requests, "post",
        lambda *a, **k: _Resp(422, text="invalid from address"),
    )
    result = er.ResendEmail().send(recipient="op@example.cz", message=_MSG)
    assert result.status == "failed"
    assert "422" in (result.error or "")


def test_send_network_exception_is_failed(monkeypatch: Any) -> None:
    _configure(monkeypatch)

    def _boom(*a: Any, **k: Any) -> Any:
        raise er.requests.ConnectionError("dns")

    monkeypatch.setattr(er.requests, "post", _boom)
    result = er.ResendEmail().send(recipient="op@example.cz", message=_MSG)
    assert result.status == "failed"
    assert "ConnectionError" in (result.error or "")
