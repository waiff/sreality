"""realitymix portal seams — the listing-identity Gate-2 lifecycle guards.

realitymix had no test module; this pins the identity-sensitive gone-flip the
Gate-2 refactor touched, so a regression can't silently no-op it. The surrogate-
keyed media write now lives in scraper.listing_write (tests/test_listing_write_live.py).
"""

from __future__ import annotations

from typing import Any

import pytest

from scraper import realitymix_main
from scraper.portal import default_config


def _portal() -> realitymix_main.RealitymixPortal:
    return realitymix_main.RealitymixPortal(default_config("realitymix"))


def test_mark_gone_flips_native_inactive(monkeypatch):
    # Gate 2: the gone-flip keys on the native id (mark_listing_inactive_native),
    # NOT a sreality_id resolved out of the DB — a post-Gate-2 realitymix row has
    # sreality_id = NULL, so the legacy sreality_id-keyed flip would silently no-op.
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        realitymix_main.db, "mark_listing_inactive_native",
        lambda _c, source, nid: captured.update(source=source, nid=nid),
    )
    monkeypatch.setattr(
        realitymix_main.db, "mark_listing_inactive",
        lambda *a, **k: pytest.fail("legacy sreality_id-keyed gone-flip must not be used"),
    )
    _portal().mark_gone(object(), "rm-500001")
    assert captured == {"source": "realitymix", "nid": "rm-500001"}

