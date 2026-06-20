"""FastAPI dependencies: per-request DB connection, shared SrealityClient, auth."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from fastapi import Depends, Header, HTTPException

from scraper import db
from scraper.sreality_client import SrealityClient

if TYPE_CHECKING:
    import psycopg


def get_db_conn() -> "Iterator[psycopg.Connection]":
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def open_background_conn() -> "Iterator[psycopg.Connection]":
    """Open a dedicated DB connection for a FastAPI BackgroundTask.

    The request-scoped `get_db_conn` connection is closed once the HTTP
    response is sent, so background work that runs after the response
    must open its own.
    """
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


_CLIENT: SrealityClient | None = None


def get_sreality_client() -> SrealityClient:
    """Module-level singleton so the per-instance throttle persists across requests."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = SrealityClient()
    return _CLIENT


_PROVIDERS: dict[str, Any] | None = None


def _build_providers() -> dict[str, Any]:
    """Construct provider singletons once. SDK clients lazy-init on first
    `.complete()`, so a missing API key here doesn't fail at boot — it
    fails at the request that tries to use it, with a clear ProviderError.
    """
    from api.providers.anthropic import AnthropicProvider
    from api.providers.gemini import GeminiProvider
    return {
        "anthropic": AnthropicProvider(),
        "gemini":    GeminiProvider(),
    }


def get_providers() -> dict[str, Any]:
    global _PROVIDERS
    if _PROVIDERS is None:
        _PROVIDERS = _build_providers()
    return _PROVIDERS


def get_llm_client(conn: Any = Depends(get_db_conn)) -> Any:
    """Per-request LLMClient bound to the request's DB connection.

    Imported lazily so the module loads in environments without the
    `anthropic` package (e.g. tests that don't exercise this path).
    """
    from api.llm_client import LLMClient
    return LLMClient(conn, providers=get_providers())


_TRANSPORTS: dict[str, Any] | None = None


def _build_transports() -> dict[str, Any]:
    """Construct channel-transport singletons once (the `_build_providers`
    mirror for notification delivery). Each transport reads its own secret
    lazily and raises only on `send()`, so a missing key never fails boot.

    Each transport reads its own secret lazily and raises only on `send()`, so
    a missing key never fails boot; `is_configured()` lets a caller skip an
    unconfigured channel. Email (Resend) is registered here from PR 2 — but it
    only sends once `RESEND_API_KEY` + `EMAIL_FROM` are set AND a watchdog opts
    into the 'email' channel (so `target_channels` is non-empty). PR 3 adds
    Telegram as a second entry.
    """
    from api.transports.email_resend import ResendEmail
    return {"email": ResendEmail()}


def get_transports() -> dict[str, Any]:
    global _TRANSPORTS
    if _TRANSPORTS is None:
        _TRANSPORTS = _build_transports()
    return _TRANSPORTS


def get_channel_client(conn: Any = Depends(get_db_conn)) -> Any:
    """Per-request ChannelClient bound to the request's DB connection (the
    `get_llm_client` mirror). Imported lazily to keep import-time light."""
    from api.channel_client import ChannelClient
    return ChannelClient(conn, transports=get_transports())


def require_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token gate. No-op if API_TOKEN env var is unset (local dev)."""
    expected = os.environ.get("API_TOKEN")
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid or missing token")
