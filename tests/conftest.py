"""Shared test setup."""

from __future__ import annotations

import os


def pytest_configure(config):  # noqa: ARG001 — pytest hook signature
    """Disable the notifications matcher loop in every test session.

    `api.main`'s lifespan spawns a background asyncio task that opens
    its own DB connection; tests that exercise the TestClient would
    otherwise see a noisy never-exiting loop trying (and failing) to
    reach Postgres. Setting this env var keeps the lifespan a no-op
    without touching production behaviour.
    """
    os.environ.setdefault("NOTIFICATIONS_MATCHER_DISABLED", "1")
