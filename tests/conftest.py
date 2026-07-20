"""Shared test setup."""

from __future__ import annotations

import os


def pytest_configure(config):  # noqa: ARG001 — pytest hook signature
    """Disable lifespan side-effects that touch the real database.

    `api.main`'s lifespan spawns a background asyncio task (notifications
    matcher) and also sweeps stuck estimation/building rows on startup —
    both open their own DB connection. Tests that exercise the
    TestClient would otherwise see noisy never-exiting loops trying
    (and failing) to reach Postgres. Setting these env vars keeps the
    lifespan a no-op without touching production behaviour.
    """
    os.environ.setdefault("NOTIFICATIONS_MATCHER_DISABLED", "1")
    os.environ.setdefault("STUCK_ROW_SWEEP_DISABLED", "1")
    # require_token now fails CLOSED (503) when API_TOKEN is unset — a forgotten
    # prod secret can no longer silently disable auth. The whole test suite hits
    # gated routes without setting a token, so opt into the local-dev exemption
    # globally; individual auth tests still delenv this to prove the 503 path.
    os.environ.setdefault("API_AUTH_OPTIONAL", "1")
