"""Hermetic tests for scripts/compute_image_phash.py — the selection query's
active-first ordering, executed against an in-memory SQLite mirror of the
images/listings join. No network, no Postgres.
"""

from __future__ import annotations

import sqlite3

from scripts.compute_image_phash import _SELECT_SQL


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE listings (sreality_id INTEGER PRIMARY KEY, is_active BOOLEAN)")
    conn.execute(
        "CREATE TABLE images ("
        "id INTEGER PRIMARY KEY, sreality_id INTEGER, storage_path TEXT, phash INTEGER)"
    )
    return conn


def _select(conn: sqlite3.Connection, limit: int = 100) -> list[int]:
    sql = _SELECT_SQL.replace("%(limit)s", ":limit")
    return [row[0] for row in conn.execute(sql, {"limit": limit})]


def test_active_listing_images_order_before_inactive() -> None:
    conn = _db()
    conn.execute("INSERT INTO listings VALUES (1, TRUE), (2, FALSE), (3, TRUE)")
    # Inactive-listing images carry the HIGHEST ids: a plain ORDER BY id DESC
    # (the old query) would return them first.
    conn.execute(
        "INSERT INTO images VALUES "
        "(10, 1, 'a/10.jpg', NULL), (11, 3, 'a/11.jpg', NULL), "
        "(90, 2, 'b/90.jpg', NULL), (91, 2, 'b/91.jpg', NULL)"
    )
    assert _select(conn) == [11, 10, 91, 90]


def test_id_desc_within_each_activity_group() -> None:
    conn = _db()
    conn.execute("INSERT INTO listings VALUES (1, TRUE), (2, FALSE)")
    conn.execute(
        "INSERT INTO images VALUES "
        "(10, 1, 'a/10.jpg', NULL), (30, 1, 'a/30.jpg', NULL), "
        "(20, 2, 'b/20.jpg', NULL), (40, 2, 'b/40.jpg', NULL)"
    )
    assert _select(conn) == [30, 10, 40, 20]


def test_orphaned_images_still_selected_last() -> None:
    # An image whose listing row is missing (or sreality_id NULL) must still be
    # hashed — the LEFT JOIN keeps it, ordered after active-listing images.
    conn = _db()
    conn.execute("INSERT INTO listings VALUES (1, TRUE)")
    conn.execute(
        "INSERT INTO images VALUES "
        "(10, 1, 'a/10.jpg', NULL), (50, 999, 'x/50.jpg', NULL), "
        "(60, NULL, 'x/60.jpg', NULL)"
    )
    assert _select(conn) == [10, 60, 50]


def test_hashed_and_unstored_images_excluded() -> None:
    conn = _db()
    conn.execute("INSERT INTO listings VALUES (1, TRUE)")
    conn.execute(
        "INSERT INTO images VALUES "
        "(10, 1, 'a/10.jpg', NULL), (11, 1, 'a/11.jpg', 42), (12, 1, NULL, NULL)"
    )
    assert _select(conn) == [10]


def test_limit_keeps_active_priority() -> None:
    conn = _db()
    conn.execute("INSERT INTO listings VALUES (1, TRUE), (2, FALSE)")
    conn.execute(
        "INSERT INTO images VALUES "
        "(10, 1, 'a/10.jpg', NULL), (90, 2, 'b/90.jpg', NULL), (91, 2, 'b/91.jpg', NULL)"
    )
    assert _select(conn, limit=2) == [10, 91]
