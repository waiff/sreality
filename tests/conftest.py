"""Shared test setup."""

from __future__ import annotations

import os

import pytest


def pytest_configure(config):  # noqa: ARG001 — pytest hook signature
    """Disable lifespan side-effects that touch the real database.

    `api.main`'s lifespan spawns a background asyncio task (the health
    teller) and also sweeps stuck estimation/building rows on startup —
    both open their own DB connection. Tests that exercise the
    TestClient would otherwise see noisy never-exiting loops trying
    (and failing) to reach Postgres. Setting these env vars keeps the
    lifespan a no-op without touching production behaviour.
    """
    os.environ.setdefault("HEALTH_TELLER_DISABLED", "1")
    os.environ.setdefault("STUCK_ROW_SWEEP_DISABLED", "1")
    # The gate accepts only a Supabase JWT and the whole suite hits gated routes
    # without one, so opt into the local-dev exemption globally; the auth tests
    # delenv this to prove the 401/503 paths.
    os.environ.setdefault("API_AUTH_OPTIONAL", "1")


@pytest.fixture(autouse=True)
def _no_valuo_token(monkeypatch):
    """No test can reach Valuo, a paid API: both credentials are removed before every test, and a
    test that needs a configured posture sets a fake token over a faked transport."""
    monkeypatch.delenv("VALUO_API_TOKEN", raising=False)
    monkeypatch.delenv("VALUO_SANDBOX_API_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _no_vapid_key(monkeypatch):
    """No test can push to Apple: the VAPID key is removed before every test (the push lane's brake), and a
    test that sends sets a throwaway key over a faked `requests.post` (tests/api/test_push.py)."""
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
