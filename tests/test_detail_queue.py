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


def test_sane_price_czk_clamps_overflow_to_none():
    assert db.sane_price_czk(None) is None
    assert db.sane_price_czk(5_000_000) == 5_000_000
    assert db.sane_price_czk(db.MAX_PRICE_CZK) == db.MAX_PRICE_CZK
    assert db.sane_price_czk(db.MAX_PRICE_CZK + 1) is None
    assert db.sane_price_czk(2_147_483_647) is None  # int4 max; seller placeholder


def test_write_detail_batch_nulls_overflow_price():
    # A single >int4 price must not crash the jsonb_to_recordset cast of a ~100-row
    # batch; it's clamped to NULL in BOTH the listings upsert and the snapshot.
    conn = _FakeConn([
        (lambda s: "INSERT INTO listings (" in s, [(True,)]),
        (lambda s: "INSERT INTO listing_snapshots" in s, [(0,)]),
        (lambda s: "DELETE FROM listing_fetch_failures" in s, []),
    ])
    db.write_detail_batch(conn, [_result(1, price=2_147_483_647, content_hash="h1")])
    upsert = _find(conn.executed, "INSERT INTO listings (")
    assert upsert[1][0].obj[0]["price_czk"] is None
    snap = _find(conn.executed, "INSERT INTO listing_snapshots")
    assert snap[1][0].obj[0]["price_czk"] is None


def test_sane_listing_numerics_clamps_int4_and_numeric_overflow():
    obj = {
        "locality_municipality_id": 1_281_819_603,  # foreign synthetic id > int4 max
        "street_id": 1_248_927_013,  # > int4 max
        "locality_region_id": 10,  # in range
        "floor": 3,  # in range
        "area_m2": 120.5,  # numeric(7,1), in range
        "estate_area": 100_000_000.0,  # numeric(9,1) overflow
        "usable_area": 99_999_999.0,  # numeric(9,1), in range
        "price_czk": 5_000_000,  # in range
    }
    db.sane_listing_numerics(obj)
    assert obj["locality_municipality_id"] is None
    assert obj["street_id"] is None
    assert obj["estate_area"] is None
    assert obj["locality_region_id"] == 10
    assert obj["floor"] == 3
    assert obj["area_m2"] == 120.5
    assert obj["usable_area"] == 99_999_999.0
    assert obj["price_czk"] == 5_000_000


def test_sane_listing_numerics_leaves_text_bool_and_none_untouched():
    obj = {
        "locality": "Praha",
        "condition": "po_rekonstrukci",
        "has_balcony": True,
        "locality_municipality_id": None,
    }
    db.sane_listing_numerics(obj)
    assert obj == {
        "locality": "Praha",
        "condition": "po_rekonstrukci",
        "has_balcony": True,
        "locality_municipality_id": None,
    }


def test_numeric_abs_max_covers_every_numeric_column():
    numeric_cols = {c for c, t in db._LISTING_COLUMN_PGTYPE.items() if t == "numeric"}
    assert set(db._NUMERIC_ABS_MAX) == numeric_cols


def test_write_detail_batch_nulls_overflow_locality_id():
    # A foreign listing's >int4 municipality_id / street_id must not crash the
    # batch's jsonb_to_recordset ::integer cast; both clamp to NULL in the upsert.
    conn = _FakeConn([
        (lambda s: "INSERT INTO listings (" in s, [(True,)]),
        (lambda s: "INSERT INTO listing_snapshots" in s, [(0,)]),
        (lambda s: "DELETE FROM listing_fetch_failures" in s, []),
    ])
    row = {
        "sreality_id": 1,
        "price_czk": 100,
        "locality_municipality_id": 1_281_819_603,
        "street_id": 1_248_927_013,
    }
    res = SimpleNamespace(row=row, raw={"id": 1}, content_hash="h1", images=[])
    db.write_detail_batch(conn, [res])
    obj = _find(conn.executed, "INSERT INTO listings (")[1][0].obj[0]
    assert obj["locality_municipality_id"] is None
    assert obj["street_id"] is None


# --- queue helpers ----------------------------------------------------------


def test_enqueue_detail_idempotent_greatest_priority():
    conn = _FakeConn([(lambda s: "INSERT INTO listing_detail_queue" in s, [(1,)])])
    db.enqueue_detail(conn, "sreality", [
        ("1", None, 100, db.QUEUE_PRIORITY_NEW),
        ("2", None, None, db.QUEUE_PRIORITY_FAILURE),
    ])
    sql, params = _find(conn.executed, "INSERT INTO listing_detail_queue")
    assert "ON CONFLICT (source, native_id) DO UPDATE" in sql
    assert "GREATEST(listing_detail_queue.priority, EXCLUDED.priority)" in sql
    assert "WHERE listing_detail_queue.claimed_at IS NULL" in sql
    # sreality sets the bigint sreality_id from the numeric native_id.
    assert "THEN u.nid::bigint ELSE NULL END" in sql
    assert params["source"] == "sreality"
    assert params["nids"] == ["1", "2"]
    assert params["prios"] == [0, 2]


def test_enqueue_detail_empty_noop():
    conn = _FakeConn([])
    assert db.enqueue_detail(conn, "sreality", []) == 0
    assert conn.executed == []


def test_enqueue_detail_nulls_overflow_index_price():
    # The index price feeds %(prices)s::int[]; an oversized value would crash the
    # whole enqueue, so it's clamped to NULL (the listing still enqueues).
    conn = _FakeConn([(lambda s: "INSERT INTO listing_detail_queue" in s, [(1,)])])
    db.enqueue_detail(conn, "bazos", [
        ("9", "/p", 9_999_999_999, db.QUEUE_PRIORITY_NEW),
        ("10", "/q", 4_200_000, db.QUEUE_PRIORITY_NEW),
    ])
    _, params = _find(conn.executed, "INSERT INTO listing_detail_queue")
    assert params["prices"] == [None, 4_200_000]


def test_claim_detail_batch_skip_locked_priority_order():
    conn = _FakeConn([
        (lambda s: "FOR UPDATE SKIP LOCKED" in s, [("5", None, 100), ("6", "/p", None)]),
    ])
    claimed = db.claim_detail_batch(conn, "sreality", 50)
    assert claimed == [("5", None, 100), ("6", "/p", None)]
    sql, params = conn.executed[0]
    assert "ORDER BY priority DESC, enqueued_at" in sql
    assert "source = %s AND claimed_at IS NULL AND given_up = false" in sql
    assert "SET claimed_at = now()" in sql
    assert "RETURNING q.native_id, q.detail_ref, q.index_price_czk" in sql
    assert params == ("sreality", 50)


def test_claim_detail_batch_zero_limit_noop():
    conn = _FakeConn([])
    assert db.claim_detail_batch(conn, "sreality", 0) == []
    assert conn.executed == []


def test_fail_detail_gives_up_at_threshold():
    conn = _FakeConn([(lambda s: "UPDATE listing_detail_queue" in s, [])])
    db.fail_detail(conn, "sreality", ["7", "8"], "boom")
    sql, params = conn.executed[0]
    assert "attempts = attempts + 1" in sql
    assert "given_up = (attempts + 1) >= %s" in sql
    assert "claimed_at = NULL" in sql
    assert "source = %s AND native_id = ANY(%s)" in sql
    assert params[0] == db.FAILURE_GIVE_UP_THRESHOLD
    assert params[2] == "sreality"
    assert params[3] == ["7", "8"]


def test_complete_detail_deletes_by_native_id():
    conn = _FakeConn([(lambda s: "DELETE FROM listing_detail_queue" in s, [])])
    db.complete_detail(conn, "sreality", ["1", "2", "3"])
    sql, params = conn.executed[0]
    assert "DELETE FROM listing_detail_queue WHERE source = %s AND native_id = ANY(%s)" in sql
    assert params == ("sreality", ["1", "2", "3"])


def test_reclaim_stale_claims_releases_old_claims():
    conn = _FakeConn([(lambda s: "UPDATE listing_detail_queue" in s, [("1",), ("2",)])])
    n = db.reclaim_stale_claims(conn, "sreality", older_than_minutes=30)
    assert n == 2
    sql, params = conn.executed[0]
    assert "SET claimed_at = NULL" in sql
    assert "claimed_at < now() - make_interval(mins => %s)" in sql
    assert params == ("sreality", 30)


# --- Phase 3: dirty-property enqueue ----------------------------------------


def test_write_detail_batch_enqueues_dirty_for_changed_listings():
    conn = _FakeConn([
        (lambda s: "INSERT INTO listings (" in s, [(True,), (False,)]),
        # both listings changed -> RETURNING sreality_id gives both ids
        (lambda s: "INSERT INTO listing_snapshots" in s, [(1,), (2,)]),
        (lambda s: "DELETE FROM listing_fetch_failures" in s, []),
        (lambda s: "INSERT INTO dirty_properties" in s, []),
    ])
    db.write_detail_batch(conn, [
        _result(1, price=100, content_hash="h1"),
        _result(2, price=200, content_hash="h2"),
    ])
    dirty = _find(conn.executed, "INSERT INTO dirty_properties")
    assert dirty is not None
    sql, params = dirty
    assert "FROM listings" in sql and "property_id IS NOT NULL" in sql
    assert "ON CONFLICT (property_id) DO UPDATE SET marked_at = now()" in sql
    assert params == ([1, 2],)


def test_write_detail_batch_no_dirty_when_nothing_changed():
    conn = _FakeConn([
        (lambda s: "INSERT INTO listings (" in s, [(False,)]),
        (lambda s: "INSERT INTO listing_snapshots" in s, []),  # no content change
        (lambda s: "DELETE FROM listing_fetch_failures" in s, []),
    ])
    db.write_detail_batch(conn, [_result(1, price=100, content_hash="h1")])
    assert _find(conn.executed, "INSERT INTO dirty_properties") is None


def test_mark_properties_dirty_drops_null_and_uses_unnest():
    conn = _FakeConn([(lambda s: "INSERT INTO dirty_properties" in s, [(10,)])])
    db.mark_properties_dirty(conn, [10, None, 10])
    sql, params = conn.executed[0]
    assert "unnest(%s::bigint[])" in sql
    assert "ON CONFLICT (property_id) DO UPDATE SET marked_at = now()" in sql
    assert params == ([10, 10],)  # NULL dropped; SQL DISTINCT collapses dups


def test_mark_properties_dirty_empty_noop():
    conn = _FakeConn([])
    assert db.mark_properties_dirty(conn, [None]) == 0
    assert conn.executed == []


def test_mark_inactive_enqueues_flipped_properties_and_returns_count():
    conn = _FakeConn([
        (lambda s: "UPDATE listings SET is_active = false WHERE is_active = true" in s,
         [(5,), (5,), (None,)]),
        (lambda s: "INSERT INTO dirty_properties" in s, []),
    ])
    n = db.mark_inactive(conn, "byt", "prodej", {1, 2})
    assert n == 3  # three listings flipped
    dirty = _find(conn.executed, "INSERT INTO dirty_properties")
    assert dirty is not None
    assert dirty[1] == ([5],)  # NULL-property listing excluded; deduped


def test_mark_inactive_no_dirty_when_no_flips():
    conn = _FakeConn([
        (lambda s: "UPDATE listings SET is_active = false WHERE is_active = true" in s, []),
    ])
    assert db.mark_inactive(conn, "byt", "prodej", {1}) == 0
    assert _find(conn.executed, "INSERT INTO dirty_properties") is None


def test_mark_inactive_is_source_scoped():
    # Rule #15: a sreality index walk must only flip sreality rows. Bazos rows
    # carry the same canon categories but are never in sreality's seen_ids, so
    # without the source clause every sreality walk would sweep them inactive.
    conn = _FakeConn([
        (lambda s: "UPDATE listings SET is_active = false WHERE is_active = true" in s, []),
    ])
    db.mark_inactive(conn, "byt", "prodej", {1, 2}, source="sreality")
    sql, params = conn.executed[0]
    assert "AND source = %s" in sql
    assert params[0] == "sreality"          # source bound first
    assert params[1:3] == ("byt", "prodej")
    assert sorted(params[3]) == [1, 2]


def test_active_count_is_source_scoped():
    conn = _FakeConn([
        (lambda s: "SELECT count(*) FROM listings" in s, [(42,)]),
    ])
    assert db.active_count(conn, "byt", "prodej", source="sreality") == 42
    sql, params = conn.executed[0]
    assert "AND source = %s" in sql
    assert params == ("sreality", "byt", "prodej")


def test_mark_listing_inactive_enqueues_its_property():
    conn = _FakeConn([
        (lambda s: "WHERE sreality_id = %s RETURNING property_id" in s, [(42,)]),
        (lambda s: "INSERT INTO dirty_properties" in s, []),
    ])
    db.mark_listing_inactive(conn, 999)
    dirty = _find(conn.executed, "INSERT INTO dirty_properties")
    assert dirty is not None
    assert dirty[1] == (42,)


def test_mark_listing_inactive_no_property_no_dirty():
    conn = _FakeConn([
        (lambda s: "WHERE sreality_id = %s RETURNING property_id" in s, [(None,)]),
    ])
    db.mark_listing_inactive(conn, 999)
    assert _find(conn.executed, "INSERT INTO dirty_properties") is None


def test_touch_listings_enqueues_reactivated_properties():
    conn = _FakeConn([
        (lambda s: "WITH react AS" in s, []),
        (lambda s: "SET last_seen_at = now(), is_active = true" in s, [(1,), (2,)]),
    ])
    db.touch_listings(conn, [1, 2])
    react = _find(conn.executed, "WITH react AS")
    assert react is not None
    # only listings currently inactive are captured for re-activation dirtying
    assert "listings.is_active = false" in react[0]
    assert "INSERT INTO dirty_properties" in react[0]
