"""enqueue_entry maps a stale-image listing to a listing_detail_queue entry."""

from __future__ import annotations

import sys
from typing import Any

import pytest

pytest.importorskip("psycopg")  # scraper.db imports psycopg at module load

import scripts.refresh_stale_image_urls as rsi
from scraper import db
from scripts.refresh_stale_image_urls import _CANDIDATES_SQL, enqueue_entry


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


def test_candidates_sql_joins_images_on_surrogate():
    sql = " ".join(_CANDIDATES_SQL.split())
    assert "i.listing_id = l.id" in sql          # image↔listing join on the surrogate
    assert "i.sreality_id = l.sreality_id" not in sql
    assert "SELECT l.id, l.sreality_id" in sql   # surrogate carried for the batch key


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        self._rows = self._conn.candidate_rows if s.startswith("SELECT l.id") else []

    def fetchall(self) -> Any:
        return self._rows


class _FakeConn:
    def __init__(self, candidate_rows: list[tuple[Any, ...]]) -> None:
        self.candidate_rows = candidate_rows
        self.executed: list[tuple[str, Any]] = []
        self.committed = False

    def cursor(self) -> _Cur:
        return _Cur(self)

    def commit(self) -> None:
        self.committed = True

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def test_main_stamps_cooldown_on_surrogate_id(monkeypatch):
    # Row order matches _CANDIDATES_SQL: (id, sreality_id, source, source_id_native, url).
    # The second row is a non-sreality listing with NULL sreality_id (post-Gate-2). The
    # cooldown UPDATE must key on listings.id so BOTH rows get stamped — keying on
    # sreality_id would stamp neither the NULL row nor collapse them.
    rows = [
        (500, 12345, "sreality", "12345", "https://s/x"),
        (501, None, "idnes", "ABC", "https://reality.idnes.cz/y"),
    ]
    conn = _FakeConn(rows)
    monkeypatch.setattr(rsi.db, "connect", lambda: conn)
    monkeypatch.setattr(rsi.db, "enqueue_detail", lambda c, source, entries: len(entries))
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://x")
    monkeypatch.setattr(sys, "argv", ["refresh"])

    assert rsi.main() == 0
    updates = [(s, p) for s, p in conn.executed if s.startswith("UPDATE listings")]
    assert len(updates) == 1
    sql, params = updates[0]
    assert "WHERE id = ANY(%s)" in sql
    assert "sreality_id" not in sql
    assert params == ([500, 501],)   # both surrogates, incl. the NULL-sreality row
    assert conn.committed
