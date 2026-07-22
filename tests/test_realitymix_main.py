"""realitymix portal seams — the listing-identity Gate-2 lifecycle guards.

realitymix had no test module; this pins the two identity-sensitive seams the
Gate-2 refactor touched (the immediate gone-flip and the surrogate-keyed media
write), so a regression can't silently no-op them.
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


def test_write_details_records_media_on_the_surrogate(monkeypatch):
    # ingest returns the SURROGATE listings.id; write_details must hand THAT to
    # record_media (which carries it straight into images.listing_id) — never the
    # legacy sreality_id, NULL for a post-Gate-2 row.
    from scraper.portal_runner import DrainItem

    listing = type("L", (), {"raw": {"image_urls": ["u1", "u2"]}})()
    items = [DrainItem("rm-1", "ok", payload={
        "listing": listing, "html": "<h>", "status": 200, "url": "/d/rm-1"})]
    monkeypatch.setattr(realitymix_main.db, "upsert_portal_raw_page", lambda *a, **k: 9)
    monkeypatch.setattr(realitymix_main.db, "ingest_scraped_listing", lambda _c, _l: (8201, "new"))
    monkeypatch.setattr(realitymix_main.db, "mark_portal_page_parsed", lambda *a, **k: None)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        realitymix_main.db, "record_media",
        lambda _c, listing_id, urls: seen.update(listing_id=listing_id, urls=list(urls)) or len(list(urls)),
    )
    counts = _portal().write_details(object(), items)
    assert seen["listing_id"] == 8201        # the surrogate, carried through
    assert counts["images_discovered"] == 2
