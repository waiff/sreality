"""FastAPI dependencies: per-request DB connection, shared SrealityClient, auth."""

from __future__ import annotations

import contextlib
import hmac
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
    from api.providers.openai import OpenAIProvider
    return {
        "anthropic": AnthropicProvider(),
        "gemini":    GeminiProvider(),
        # Session-3 bake-off provider, promoted to production so a lane whose
        # app_settings model is a gpt-* id resolves (llm_client.provider_for_model).
        # Lazy key (OPENAI_API_KEY) — absent, it fails only at the call that uses it.
        "openai":    OpenAIProvider(),
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
    """Bearer-token gate. Fails CLOSED: with API_TOKEN unset the API refuses every
    request (503) UNLESS the operator explicitly opts out for local dev with
    API_AUTH_OPTIONAL=1. A forgotten prod secret can therefore never silently
    disable auth (the old behaviour was fail-open). The compare is timing-safe."""
    expected = os.environ.get("API_TOKEN")
    if not expected:
        if os.environ.get("API_AUTH_OPTIONAL") == "1":
            return
        raise HTTPException(
            status_code=503,
            detail="API auth is not configured (set API_TOKEN, or API_AUTH_OPTIONAL=1 for local dev)",
        )
    # Compare as bytes: hmac.compare_digest on two str raises TypeError for a
    # non-ASCII Authorization header (Starlette decodes headers as latin-1), which
    # would surface as a 500 instead of a clean 401.
    if not authorization or not hmac.compare_digest(
        authorization.encode("utf-8", "ignore"), f"Bearer {expected}".encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing token")


# Fixed system account id (mirrors migrations/286_accounts_foundation.sql). The
# fallback owner for a run/write whose caller has no resolvable account.
SYSTEM_ACCOUNT_ID = "00000000-0000-0000-0000-000000000000"

_JWKS_CLIENT: Any = None


# PyJWT's default JWKS cache lifespan is 300 s, so once every five minutes the
# NEXT identity-gated request pays a blocking outbound HTTPS fetch to Supabase's
# JWKS endpoint before it may even look at the token — on one uvicorn worker that
# stalls the whole process, on top of the ~270-410 ms floor every Railway call
# already pays. Rotation stays safe at an hour: PyJWKClient.get_signing_key falls
# through to get_signing_keys(refresh=True) whenever the token's `kid` is absent
# from the cached set, so a newly rotated key is fetched on its first use rather
# than waited for. The lifespan is the staleness ceiling for keys we no longer
# need, not the latency of adopting a new one.
_JWKS_CACHE_SECONDS = 3600


def _jwks_client(jwks_url: str) -> Any:
    """Cached PyJWKClient — fetches + caches the project's public signing keys
    (no per-request network call). Instantiated once per process."""
    global _JWKS_CLIENT
    if _JWKS_CLIENT is None:
        import jwt
        _JWKS_CLIENT = jwt.PyJWKClient(jwks_url, lifespan=_JWKS_CACHE_SECONDS)
    return _JWKS_CLIENT


def verify_jwt(authorization: str | None = Header(default=None)) -> dict:
    """Phase 1 auth: verify a Supabase user JWT and return its claims.

    Preferred path (this project): asymmetric signing keys (ES256/RS256) verified
    against the project's public JWKS — no shared secret needed. Set SUPABASE_URL.
    Falls back to a legacy shared HS256 secret (SUPABASE_JWT_SECRET) if that is all
    that is configured.

    The dual-auth window (accepting the static API_TOKEN here as a synthetic
    operator/admin identity) has been retired: that branch made every
    require_admin route only as protected as require_token, since the token is
    embedded in the shipped SPA bundle and extractable via devtools. A caller
    must now present a real Supabase-signed JWT. `require_token`-gated routes
    are unaffected — that gate doesn't call this function. Fails closed when
    nothing is configured.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization[len("Bearer "):]

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


def account_scope(
    authorization: str | None = Header(default=None),
    conn: Any = Depends(get_db_conn),
) -> list[str]:
    """Read scope for routes that must serve BOTH a logged-in browser session and a
    non-browser static-token caller (the future MCP / ClickUp / script integrations)
    over the one Authorization header the transport gives us.

    Returns the account ids the caller may read, ALWAYS including SYSTEM — mirroring
    the estimation_runs_tenant_read policy (migration 291) rather than inventing a
    second definition of tenancy. A verified JWT adds its own account; the static
    token is NOT an identity (it ships inside the SPA bundle, in a public repo), so
    it stays SYSTEM-only.

    Fails CLOSED at every branch: a missing header, an unconfigured gate, and an
    expired or unparseable JWT all raise. No branch degrades silently to a wider or
    narrower row set, and verify_jwt's HTTPException is never swallowed.

    Why not require_token + tenant_conn: `listings` and `parsed_url_cache` are
    RLS-enabled-with-zero-policies, so the /estimations list query (which LEFT JOINs
    both for locality_display) returns NULL for every row on a tenant connection —
    silently, no error. The service-role connection plus an explicit predicate is the
    only shape that scopes without blanking data.
    """
    expected = os.environ.get("API_TOKEN")
    # Gate-configuration contract mirrors require_token EXACTLY, so swapping that
    # dependency for this one changes no status code: unset API_TOKEN is a 503
    # (never a silent open door), with the documented local-dev opt-out pinned to
    # SYSTEM so it can never widen scope to another account's rows.
    if not expected:
        if os.environ.get("API_AUTH_OPTIONAL") == "1":
            return [SYSTEM_ACCOUNT_ID]
        raise HTTPException(
            status_code=503,
            detail="API auth is not configured (set API_TOKEN, or API_AUTH_OPTIONAL=1 for local dev)",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    if hmac.compare_digest(
        authorization.encode("utf-8", "ignore"), f"Bearer {expected}".encode("utf-8")
    ):
        return [SYSTEM_ACCOUNT_ID]
    # Not the shared secret, so it must be a real Supabase JWT. Require the JWS
    # shape before calling verify_jwt: otherwise a merely WRONG static token would
    # surface as verify_jwt's 503 ("auth is not configured") on a deployment with
    # no Supabase auth env, masking a bad credential as a server fault.
    if authorization[len("Bearer "):].count(".") != 2:
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    claims = verify_jwt(authorization)
    from api import tenant_pool  # lazy: tenant_pool imports this module

    account_id = tenant_pool.resolve_account_id(conn, claims)
    if account_id is None:
        return [SYSTEM_ACCOUNT_ID]
    return [str(account_id), SYSTEM_ACCOUNT_ID]


def require_admin(claims: dict = Depends(verify_jwt)) -> dict:
    """Gate admin-only routes on the is_admin claim (stamped onto the JWT's
    app_metadata from the admins table). Requires a real Supabase JWT — see
    verify_jwt's docstring."""
    meta = claims.get("app_metadata") or {}
    if claims.get("is_admin") is not True and meta.get("is_admin") is not True:
        raise HTTPException(status_code=403, detail="Admin only")
    return claims
