"""enqueue_entry maps a stale-image listing to a listing_detail_queue entry."""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")  # scraper.db imports psycopg at module load

from scraper import db
from scripts.refresh_stale_image_urls import enqueue_entry


def test_sreality_derives_detail_ref_from_id():
    # sreality fetches detail by id, so detail_ref is None; native_id is the id as text.
    assert enqueue_entry(12345, "sreality", "12345", "https://www.sreality.cz/detail/x") == (
        "12345", None, None, db.QUEUE_PRIORITY_NEW,
    )


def test_crawler_portal_uses_source_url_as_detail_ref():
    # Crawler portals fetch by URL → detail_ref is the stored source_url; native_id is
    # the portal-native id (negative-synthetic listings carry source_id_native).
    assert enqueue_entry(-678, "idnes", "ABC", "https://reality.idnes.cz/detail/y") == (
        "ABC", "https://reality.idnes.cz/detail/y", None, db.QUEUE_PRIORITY_NEW,
    )


def test_lowest_priority_so_it_never_delays_new_listings():
    _nid, _ref, _price, prio = enqueue_entry(1, "sreality", "1", None)
    assert prio == db.QUEUE_PRIORITY_NEW  # 0, the lowest tier
