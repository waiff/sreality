"""Tests for the Phase-2 needs-detail queue + batched detail-drain writes
(scraper.db).

Hermetic: a scripted fake conn matches each executed statement against
(predicate -> rows) pairs and records every execution, so the tests assert the
SQL shape + the Python-side tally/dedup logic. The set-based SQL itself
(jsonb_to_recordset, IS DISTINCT FROM, FOR UPDATE SKIP LOCKED) is verified
out-of-band via the Supabase MCP.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from psycopg.types.json import Jsonb

from scraper import db


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        for predicate, rows in self._conn.script:
            if predicate(s):
                self._rows = list(rows)
                self.rowcount = len(rows)
                return
        self._rows = []
        self.rowcount = 0

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, script: list[tuple[Any, list[tuple[Any, ...]]]]) -> None:
        self.script = script
        self.executed: list[tuple[str, Any]] = []

    def transaction(self) -> _Ctx:
        return _Ctx()

    def cursor(self) -> _Cur:
        return _Cur(self)


def _find(executed, needle: str) -> tuple[str, Any] | None:
    return next((e for e in executed if needle in e[0]), None)


def _result(sid: int, *, price: int, content_hash: str, images=None):
    row = {"sreality_id": sid, "price_czk": price}
    return SimpleNamespace(
        row=row, raw={"id": sid}, content_hash=content_hash, images=images or [],
    )


# --- write_detail_batch -----------------------------------------------------


def test_write_detail_batch_tallies_new_updated_unchanged():
    """new = #(xmax=0); snapshots rowcount = new+updated; unchanged = rest."""
    conn = _FakeConn([
        # 1 inserted + 2 conflicted-updated
        (lambda s: "INSERT INTO listings (" in s, [(True,), (False,), (False,)]),
        # 2 snapshots inserted (the new one + one changed) -> rowcount 2
        (lambda s: "INSERT INTO listing_snapshots" in s, [(0,), (0,)]),
        # 1 image inserted
        (lambda s: "INSERT INTO images" in s, [(True,)]),
        (lambda s: "DELETE FROM listing_fetch_failures" in s, []),
    ])
    results = [
        _result(1, price=100, content_hash="h1", images=[{"url": "a", "sequence": 0}]),
        _result(2, price=200, content_hash="h2"),
        _result(3, price=300, content_hash="h3"),
    ]
    counts = db.write_detail_batch(conn, results)
    assert counts == {"new": 1, "updated": 1, "unchanged": 1, "images_discovered": 1}

    # All four statements ran, in order, on one transaction.
    kinds = [e[0] for e in conn.executed]
    assert any("INSERT INTO listings (" in k for k in kinds)
    assert any("jsonb_to_recordset" in k for k in kinds)
    assert any("INSERT INTO listing_snapshots" in k for k in kinds)
    assert any("DELETE FROM listing_fetch_failures" in k for k in kinds)
    # The listings upsert carries one jsonb array of all three rows.
    upsert = _find(conn.executed, "INSERT INTO listings (")
    listing_objs = upsert[1][0].obj
    assert len(listing_objs) == 3
    assert {o["sreality_id"] for o in listing_objs} == {1, 2, 3}
    # Snapshot payload is the lean 3-field shape (raw_json read back from listings).
    snap = _find(conn.executed, "INSERT INTO listing_snapshots")
    assert set(snap[1][0].obj[0]) == {"sreality_id", "price_czk", "content_hash"}


def test_write_detail_batch_dedupes_images_per_listing_sequence():
    conn = _FakeConn([
        (lambda s: "INSERT INTO listings (" in s, [(True,)]),
        (lambda s: "INSERT INTO listing_snapshots" in s, [(0,)]),
        (lambda s: "INSERT INTO images" in s, [(True,)]),
        (lambda s: "DELETE FROM listing_fetch_failures" in s, []),
    ])
    # Same (sid, sequence=0) twice -> one image obj; a NULL-sequence one is kept.
    results = [_result(1, price=100, content_hash="h1", images=[
        {"url": "a", "sequence": 0},
        {"url": "b", "sequence": 0},
        {"url": "c", "sequence": None},
    ])]
    db.write_detail_batch(conn, results)
    img = _find(conn.executed, "INSERT INTO images")
    image_objs = img[1][0].obj
    assert len(image_objs) == 2
    assert image_objs[0] == {"sreality_id": 1, "sreality_url": "a", "sequence": 0}
    assert image_objs[1] == {"sreality_id": 1, "sreality_url": "c", "sequence": None}


def test_write_detail_batch_empty_is_noop():
    conn = _FakeConn([])
    assert db.write_detail_batch(conn, []) == {
        "new": 0, "updated": 0, "unchanged": 0, "images_discovered": 0,
    }
    assert conn.executed == []


def test_write_detail_batch_skips_image_insert_when_no_images():
    conn = _FakeConn([
        (lambda s: "INSERT INTO listings (" in s, [(False,)]),
        (lambda s: "INSERT INTO listing_snapshots" in s, []),
        (lambda s: "DELETE FROM listing_fetch_failures" in s, []),
    ])
    db.write_detail_batch(conn, [_result(1, price=100, content_hash="h1")])
    assert _find(conn.executed, "INSERT INTO images") is None


# --- queue helpers ----------------------------------------------------------


def test_enqueue_detail_idempotent_greatest_priority():
    conn = _FakeConn([(lambda s: "INSERT INTO listing_detail_queue" in s, [(1,)])])
    db.enqueue_detail(conn, [(1, 100, db.QUEUE_PRIORITY_NEW), (2, None, db.QUEUE_PRIORITY_FAILURE)])
    sql, params = _find(conn.executed, "INSERT INTO listing_detail_queue")
    assert "ON CONFLICT (sreality_id) DO UPDATE" in sql
    assert "GREATEST(listing_detail_queue.priority, EXCLUDED.priority)" in sql
    assert "WHERE listing_detail_queue.claimed_at IS NULL" in sql
    # (source, sids, prices, priorities)
    assert params[0] == "sreality"
    assert params[1] == [1, 2]
    assert params[3] == [0, 2]


def test_enqueue_detail_empty_noop():
    conn = _FakeConn([])
    assert db.enqueue_detail(conn, []) == 0
    assert conn.executed == []


def test_claim_detail_batch_skip_locked_priority_order():
    conn = _FakeConn([
        (lambda s: "FOR UPDATE SKIP LOCKED" in s, [(5, 100), (6, None)]),
    ])
    claimed = db.claim_detail_batch(conn, 50)
    assert claimed == [(5, 100), (6, None)]
    sql, params = conn.executed[0]
    assert "ORDER BY priority DESC, enqueued_at" in sql
    assert "claimed_at IS NULL AND given_up = false" in sql
    assert "SET claimed_at = now()" in sql
    assert params == (50,)


def test_claim_detail_batch_zero_limit_noop():
    conn = _FakeConn([])
    assert db.claim_detail_batch(conn, 0) == []
    assert conn.executed == []


def test_fail_detail_gives_up_at_threshold():
    conn = _FakeConn([(lambda s: "UPDATE listing_detail_queue" in s, [])])
    db.fail_detail(conn, [7, 8], "boom")
    sql, params = conn.executed[0]
    assert "attempts = attempts + 1" in sql
    assert "given_up = (attempts + 1) >= %s" in sql
    assert "claimed_at = NULL" in sql
    assert params[0] == db.FAILURE_GIVE_UP_THRESHOLD
    assert params[2] == [7, 8]


def test_complete_detail_deletes_by_id():
    conn = _FakeConn([(lambda s: "DELETE FROM listing_detail_queue" in s, [])])
    db.complete_detail(conn, [1, 2, 3])
    sql, params = conn.executed[0]
    assert "DELETE FROM listing_detail_queue WHERE sreality_id = ANY(%s)" in sql
    assert params == ([1, 2, 3],)


def test_reclaim_stale_claims_releases_old_claims():
    conn = _FakeConn([(lambda s: "UPDATE listing_detail_queue" in s, [(1,), (2,)])])
    n = db.reclaim_stale_claims(conn, older_than_minutes=30)
    assert n == 2
    sql, params = conn.executed[0]
    assert "SET claimed_at = NULL" in sql
    assert "claimed_at < now() - make_interval(mins => %s)" in sql
    assert params == (30,)
