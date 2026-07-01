"""Routing tests for scraper.db.connect_session.

Hermetic: monkeypatches psycopg.connect and db.connect so no real socket is
opened. Asserts the session-pooler endpoint is used (without
prepare_threshold=None, so psycopg3 auto-prepares) when SUPABASE_DB_SESSION_URL
is set, and that it falls back to the transaction pooler otherwise.
"""

from __future__ import annotations

from typing import Any

import psycopg

from scraper import db


def test_connect_session_uses_session_url_and_allows_prepared_statements(
    monkeypatch,
):
    captured: dict[str, Any] = {}

    def _fake_connect(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(psycopg, "connect", _fake_connect)
    monkeypatch.setenv("SUPABASE_DB_SESSION_URL", "postgres://session:5432/db")

    db.connect_session()

    assert captured["url"] == "postgres://session:5432/db"
    assert captured["kwargs"]["autocommit"] is True
    # Crucially NOT disabled — the session pooler gives each client a dedicated
    # backend, so leaving prepare_threshold at psycopg3's default lets the hot
    # write loop's repeated SQL get server-side prepared.
    assert "prepare_threshold" not in captured["kwargs"]


def test_connect_session_falls_back_when_session_url_unset(monkeypatch):
    monkeypatch.delenv("SUPABASE_DB_SESSION_URL", raising=False)
    sentinel = object()
    # The fallback forwards its retry budget, so the fake must accept kwargs.
    monkeypatch.setattr(db, "connect", lambda **kwargs: sentinel)

    assert db.connect_session() is sentinel


def test_connect_session_explicit_url_overrides_env(monkeypatch):
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        psycopg, "connect",
        lambda url, **kwargs: captured.update(url=url, kwargs=kwargs),
    )
    monkeypatch.setenv("SUPABASE_DB_SESSION_URL", "postgres://from-env/db")

    db.connect_session("postgres://explicit/db")

    assert captured["url"] == "postgres://explicit/db"
