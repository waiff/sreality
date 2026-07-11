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


# The API opens a DB connection PER REQUEST, so the full batch-side handshake
# retry (3x10s) would hang request threads ~30s during a pooler outage. A quick
# single retry rides out a brief pooler blip without holding a thread through a
# sustained outage (the DB is down then anyway, so failing fast is correct). See
# scraper.db.connect's `attempts`/`retry_delay`.
_API_CONNECT_ATTEMPTS = 2
_API_CONNECT_RETRY_DELAY = 1.0


def get_db_conn() -> "Iterator[psycopg.Connection]":
    conn = db.connect(
        attempts=_API_CONNECT_ATTEMPTS, retry_delay=_API_CONNECT_RETRY_DELAY
    )
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
    conn = db.connect(
        attempts=_API_CONNECT_ATTEMPTS, retry_delay=_API_CONNECT_RETRY_DELAY
    )
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
    unconfigured channel. A transport only delivers once its secret is set AND a
    watchdog/collection opts into its channel (so `target_channels` is non-empty):
    email = Resend (`RESEND_API_KEY` + `EMAIL_FROM`), telegram = Bot API
    (`TELEGRAM_BOT_TOKEN`). Adding a channel is one import + one entry here.
    """
    from api.transports.email_resend import ResendEmail
    from api.transports.telegram import Telegram
    return {"email": ResendEmail(), "telegram": Telegram()}


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


# Fixed system account id (mirrors migrations/286_accounts_foundation.sql). Legacy
# static-token callers (the operator's current SPA/extension during the dual-auth
# window) resolve to this until they re-auth with a Supabase JWT.
SYSTEM_ACCOUNT_ID = "00000000-0000-0000-0000-000000000000"

_JWKS_CLIENT: Any = None


def _jwks_client(jwks_url: str) -> Any:
    """Cached PyJWKClient — fetches + caches the project's public signing keys
    (no per-request network call). Instantiated once per process."""
    global _JWKS_CLIENT
    if _JWKS_CLIENT is None:
        import jwt
        _JWKS_CLIENT = jwt.PyJWKClient(jwks_url)
    return _JWKS_CLIENT


def verify_jwt(authorization: str | None = Header(default=None)) -> dict:
    """Phase 1 auth: verify a Supabase user JWT and return its claims.

    Preferred path (this project): asymmetric signing keys (ES256/RS256) verified
    against the project's public JWKS — no shared secret needed. Set SUPABASE_URL.
    Falls back to a legacy shared HS256 secret (SUPABASE_JWT_SECRET) if that is all
    that is configured.

    Dual-auth window: also accepts the legacy static API_TOKEN so the operator's
    current SPA/extension keep working mid-migration (Phase 1 §2). Legacy callers
    get a synthetic operator/admin identity. Retire that branch once the last old
    client is gone. Fails closed when nothing is configured.
    """
    import hmac

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization[len("Bearer "):]

    legacy = os.environ.get("API_TOKEN")
    if legacy and hmac.compare_digest(token, legacy):
        return {"sub": None, "role": "operator", "is_admin": True, "legacy": True}

    import jwt  # PyJWT (api extra); imported lazily to keep boot light

    base = os.environ.get("SUPABASE_URL")
    if base:
        jwks_url = base.rstrip("/") + "/auth/v1/.well-known/jwks.json"
        try:
            signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token)
            return jwt.decode(
                token, signing_key.key,
                algorithms=["ES256", "RS256"], audience="authenticated",
            )
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail="Invalid token") from exc

    secret = os.environ.get("SUPABASE_JWT_SECRET")
    if not secret:
        # Fail closed: an unconfigured auth backend must never authenticate anyone.
        raise HTTPException(status_code=503, detail="Auth is not configured")
    try:
        return jwt.decode(
            token, secret, algorithms=["HS256"], audience="authenticated"
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc


def require_admin(claims: dict = Depends(verify_jwt)) -> dict:
    """Gate admin-only routes on the is_admin claim (stamped from the admins
    table via a Supabase access-token hook). The legacy operator token passes."""
    meta = claims.get("app_metadata") or {}
    if claims.get("is_admin") is not True and meta.get("is_admin") is not True:
        raise HTTPException(status_code=403, detail="Admin only")
    return claims
