"""Phase 1 auth — verify_jwt dual-auth dependency."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from api import dependencies as deps

jwt = pytest.importorskip("jwt")  # PyJWT (api extra)

SECRET = "test-jwt-secret"


def _token(claims: dict) -> str:
    return jwt.encode({"aud": "authenticated", **claims}, SECRET, algorithm="HS256")


def test_missing_header_401():
    with pytest.raises(HTTPException) as ei:
        deps.verify_jwt(authorization=None)
    assert ei.value.status_code == 401


def test_legacy_token_resolves_to_operator(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "legacy-secret")
    claims = deps.verify_jwt(authorization="Bearer legacy-secret")
    assert claims["legacy"] is True and claims["is_admin"] is True


def test_no_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    with pytest.raises(HTTPException) as ei:
        deps.verify_jwt(authorization="Bearer whatever")
    assert ei.value.status_code == 503


def test_valid_supabase_jwt(monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    tok = _token({"sub": "11111111-1111-1111-1111-111111111111"})
    claims = deps.verify_jwt(authorization=f"Bearer {tok}")
    assert claims["sub"] == "11111111-1111-1111-1111-111111111111"


def test_bad_signature_401(monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    forged = jwt.encode({"aud": "authenticated", "sub": "x"}, "wrong-secret", algorithm="HS256")
    with pytest.raises(HTTPException) as ei:
        deps.verify_jwt(authorization=f"Bearer {forged}")
    assert ei.value.status_code == 401


def test_require_admin_rejects_non_admin(monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    tok = _token({"sub": "u", "is_admin": False})
    with pytest.raises(HTTPException) as ei:
        deps.require_admin(deps.verify_jwt(authorization=f"Bearer {tok}"))
    assert ei.value.status_code == 403
