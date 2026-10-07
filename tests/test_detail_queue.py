"""Tests for the Phase-2 needs-detail queue, the write-boundary numeric guards and
the run counters (scraper.db). The listing write itself is scraper/listing_write.py
(tests/test_listing_write.py offline, tests/test_listing_write_live.py executed).

Hermetic: a scripted fake conn matches each executed statement against
(predicate -> rows) pairs and records every execution, so the tests assert the
SQL shape + the Python-side tally/dedup logic. The set-based SQL itself
(jsonb_to_recordset, IS DISTINCT FROM, FOR UPDATE SKIP LOCKED) is verified
out-of-band via the Supabase MCP.
"""

from __future__ import annotations

from typing import Any

import pytest

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


def test_sane_price_czk_clamps_overflow_to_none():
    assert db.sane_price_czk(None) is None
    assert db.sane_price_czk(5_000_000) == 5_000_000
    assert db.sane_price_czk(db.MAX_PRICE_CZK) == db.MAX_PRICE_CZK
    assert db.sane_price_czk(db.MAX_PRICE_CZK + 1) is None
    assert db.sane_price_czk(2_147_483_647) is None  # int4 max; seller placeholder


def test_sane_price_czk_drops_low_placeholders():
    # "1 Kč dohodou" / "0 Kč" is price-on-request, not a price.
    assert db.sane_price_czk(0) is None
    assert db.sane_price_czk(1) is None
    assert db.sane_price_czk(2) == 2  # boundary: the smallest kept price


def test_sane_listing_numerics_clamps_int4_and_numeric_overflow():
    obj = {
        "category_sub_cb": 3_000_000_000,  # a garbled portal code > int4 max
        "parking_lots": 2_500_000_000,  # > int4 max
        "total_floors": 10,  # in range
        "floor": 3,  # in range
        "area_m2": 120.5,  # numeric(7,1), in range
        "estate_area": 100_000_000.0,  # numeric(9,1) overflow
        "usable_area": 99_999_999.0,  # numeric(9,1), in range
        "price_czk": 5_000_000,  # in range
    }
    db.sane_listing_numerics(obj)
    assert obj["category_sub_cb"] is None
    assert obj["parking_lots"] is None
    assert obj["estate_area"] is None
    assert obj["total_floors"] == 10
    assert obj["floor"] == 3
    assert obj["area_m2"] == 120.5
    assert obj["usable_area"] == 99_999_999.0
    assert obj["price_czk"] == 5_000_000


def test_sane_listing_numerics_nulls_zero_areas():
    # 0 m² is a form placeholder, never a measurement; integer zeros (ground
    # floor, no parking lots) are real values and must survive.
    obj = {
        "area_m2": 0,
        "usable_area": 0.0,
        "estate_area": 0,
        "garden_area": 16.0,
        "floor": 0,
        "parking_lots": 0,
    }
    db.sane_listing_numerics(obj)
    assert obj["area_m2"] is None
    assert obj["usable_area"] is None
    assert obj["estate_area"] is None
    assert obj["garden_area"] == 16.0
    assert obj["floor"] == 0
    assert obj["parking_lots"] == 0


def test_sane_listing_numerics_leaves_text_bool_and_none_untouched():
    obj = {
        "disposition": "3+1",
        "condition": "po_rekonstrukci",
        "has_balcony": True,
        "parking_lots": None,
    }
    db.sane_listing_numerics(obj)
    assert obj == {
        "disposition": "3+1",
        "condition": "po_rekonstrukci",
        "has_balcony": True,
        "parking_lots": None,
    }


def test_numeric_abs_max_covers_every_numeric_column():
    numeric_cols = {c for c, t in db._LISTING_COLUMN_PGTYPE.items() if t == "numeric"}
    assert set(db._NUMERIC_ABS_MAX) == numeric_cols


# --- scrape_run counters (crash-survivable) ---------------------------------


def test_bump_scrape_run_counts_additive_update():
    conn = _FakeConn([(lambda s: "UPDATE scrape_runs SET" in s, [])])
    db.bump_scrape_run_counts(
        conn, 7, found_new=3, scraped_new=3, updated=1, inactive=2,
        errors=1, images_discovered=4,
    )
    sql, params = _find(conn.executed, "UPDATE scrape_runs SET")
    assert "listings_scraped_new = listings_scraped_new + %s" in sql
    assert "errors = errors + %s" in sql
    assert params == (3, 3, 1, 2, 1, 4, 7)


def test_bump_scrape_run_counts_noop_on_zero_delta_and_null_run():
    conn = _FakeConn([])
    db.bump_scrape_run_counts(conn, 7)  # all-zero delta -> no UPDATE
    db.bump_scrape_run_counts(conn, None, found_new=5)  # no run_id -> no UPDATE
    assert conn.executed == []


def test_scrape_run_finalize_drain_does_not_rewrite_bumped_counters():
    conn = _FakeConn([(lambda s: "UPDATE scrape_runs" in s, [])])
    db.scrape_run_finalize(
        conn, 9, listings_scraped_new=999, listings_updated=999,
        index_pages=0, images_stored=2, by_category=[], bump_already_applied=True,
    )
    sql, _ = _find(conn.executed, "UPDATE scrape_runs")
    # The drain bumped these incrementally; finalize must leave them alone.
    assert "listings_scraped_new" not in sql
    assert "listings_updated" not in sql
    assert "ended_at = now()" in sql and "images_stored = %s" in sql


def test_scrape_run_finalize_default_writes_all_counters():
    conn = _FakeConn([(lambda s: "UPDATE scrape_runs" in s, [])])
    db.scrape_run_finalize(conn, 9, listings_scraped_new=5, listings_updated=3)
    sql, _ = _find(conn.executed, "UPDATE scrape_runs")
    assert "listings_scraped_new = %s" in sql
    assert "listings_updated = %s" in sql


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
    # Re-enqueue must KEEP the original enqueued_at: the claim order is
    # (priority DESC, enqueued_at ASC), so re-stamping now() pushed every
    # still-queued row behind the walk's fresh inserts each run — a backlog
    # bigger than one drain budget then starved its tail forever (the remax
    # rent-coverage bug).
    assert "enqueued_at" not in sql
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
        (lambda s: "FOR UPDATE SKIP LOCKED" in s,
         [("5", None, 100, None), ("6", "/p", None, None)]),
    ])
    claimed = db.claim_detail_batch(conn, "sreality", 50)
    assert claimed == [("5", None, 100, None), ("6", "/p", None, None)]
    sql, params = conn.executed[0]
    # Acquisition is claimed by age alone; the old ranking survives only INSIDE refresh.
    assert "AND priority = %(new_priority)s ORDER BY enqueued_at" in sql
    assert ("AND priority <> %(new_priority)s AND (source, native_id) NOT IN "
            "(SELECT source, native_id FROM ver) ORDER BY priority DESC, enqueued_at") in sql
    # Presence checks (priority -1) get their own reserved slice, oldest first.
    assert "AND priority = %(verify_priority)s ORDER BY enqueued_at" in sql
    assert params["verify_priority"] == db.QUEUE_PRIORITY_VERIFY
    assert "claimed_at IS NULL AND given_up = false" in sql
    assert "SET claimed_at = now()" in sql
    # migration 444: the claim carries when the walk first SAW the id (W5: and no
    # discovery_seq, which nothing writes any more).
    assert "RETURNING q.native_id, q.detail_ref, q.index_price_czk, q.enqueued_at" in sql
    assert params["source"] == "sreality"
    assert params["limit"] == 50
    assert params["new_priority"] == db.QUEUE_PRIORITY_NEW


def test_claim_detail_batch_reserves_half_the_batch_for_new_listings():
    conn = _FakeConn([(lambda s: "FOR UPDATE SKIP LOCKED" in s, [])])
    db.claim_detail_batch(conn, "sreality", 200)
    _, params = conn.executed[0]
    assert params["acq_limit"] == 100
    # Refresh takes the rest, and only the rest — unused reserve backfills to it
    # inside SQL (GREATEST(limit - count(acq), 0)), never in Python.
    sql, _ = conn.executed[0]
    assert ("LIMIT GREATEST(%(limit)s - (SELECT count(*) FROM acq) - "
            "(SELECT count(*) FROM ver), 0)") in sql
    # The presence share is bounded by what acquisition left over, so tiny
    # batches still acquire first.
    assert "LIMIT LEAST(%(ver_limit)s, GREATEST(%(limit)s - (SELECT count(*) FROM acq), 0))" in sql
    assert params["ver_limit"] == 40                      # 20% of 200


def test_claim_detail_batch_reserves_a_share_for_presence_checks():
    """Rule #3 (2026-09-07): closures come only from page checks, which sit at
    the bottom of the refresh class by design. Without a reserve of their own an
    unbounded refresh inflow would mean no listing is ever delisted again."""
    conn = _FakeConn([(lambda s: "FOR UPDATE SKIP LOCKED" in s, [])])
    db.claim_detail_batch(conn, "ceskereality", 10)
    _, params = conn.executed[0]
    assert params["ver_limit"] == 2
    assert 0 < db.QUEUE_VERIFY_RESERVE < db.QUEUE_ACQUISITION_RESERVE


def test_claim_detail_batch_reserve_rounds_up_so_tiny_batches_still_acquire():
    # Rounding down would give a 1-row batch zero acquisition slots forever —
    # the same starvation in miniature.
    for limit, expected in ((1, 1), (2, 1), (3, 2), (7, 4)):
        conn = _FakeConn([(lambda s: "FOR UPDATE SKIP LOCKED" in s, [])])
        db.claim_detail_batch(conn, "bazos", limit)
        _, params = conn.executed[0]
        assert params["acq_limit"] == expected, limit


def test_claim_detail_batch_reserve_never_exceeds_the_batch():
    conn = _FakeConn([(lambda s: "FOR UPDATE SKIP LOCKED" in s, [])])
    db.claim_detail_batch(conn, "bazos", 10, acquisition_reserve=1.5)
    _, params = conn.executed[0]
    assert params["acq_limit"] == 10


def test_claim_detail_batch_reserve_can_be_disabled():
    conn = _FakeConn([(lambda s: "FOR UPDATE SKIP LOCKED" in s, [])])
    db.claim_detail_batch(conn, "bazos", 10, acquisition_reserve=0)
    _, params = conn.executed[0]
    assert params["acq_limit"] == 0


def test_claim_detail_batch_zero_limit_noop():
    conn = _FakeConn([])
    assert db.claim_detail_batch(conn, "sreality", 0) == []
    assert conn.executed == []


def test_fail_detail_gives_up_at_threshold():
    conn = _FakeConn([(lambda s: "UPDATE listing_detail_queue" in s, [])])
    db.fail_detail(conn, "sreality", ["7", "8"], "boom")
    sql, params = conn.executed[0]
    assert "attempts = q.attempts + 1" in sql
    assert "given_up = (q.attempts + 1) >= %(max)s" in sql
    assert "claimed_at = NULL" in sql
    assert "q.source = %(source)s AND q.native_id = ANY(%(ids)s)" in sql
    assert params["max"] == db.FAILURE_GIVE_UP_THRESHOLD
    assert params["source"] == "sreality"
    assert params["ids"] == ["7", "8"]


def test_fail_detail_logs_give_up_transition_to_completion_ledger():
    conn = _FakeConn([(lambda s: "UPDATE listing_detail_queue" in s, [])])
    db.fail_detail(conn, "sreality", ["7"], "boom")
    sql, _ = conn.executed[0]
    assert "INSERT INTO detail_queue_completions" in sql
    assert "'given_up'" in sql
    # Only the flip edge logs: an already-given-up row replayed by a resilient
    # retry must not produce a second ledger row.
    assert "WHERE given_up AND NOT was_given_up" in sql
    # The ledger keeps the failed attempt's claim time, which the SET nulls.
    assert "old.claimed_at AS old_claimed_at" in sql


def test_complete_detail_deletes_and_logs_completion():
    conn = _FakeConn([(lambda s: "DELETE FROM listing_detail_queue" in s, [])])
    db.complete_detail(conn, "sreality", ["1", "2", "3"])
    sql, params = conn.executed[0]
    assert "DELETE FROM listing_detail_queue WHERE source = %(source)s AND native_id = ANY(%(ids)s)" in sql
    assert "INSERT INTO detail_queue_completions" in sql
    assert "SELECT source, native_id, priority, attempts, enqueued_at, claimed_at, %(outcome)s" in sql
    assert params == {"source": "sreality", "ids": ["1", "2", "3"], "outcome": "written"}


def test_complete_detail_gone_outcome():
    conn = _FakeConn([(lambda s: "DELETE FROM listing_detail_queue" in s, [])])
    db.complete_detail(conn, "bazos", ["9"], outcome="gone")
    _, params = conn.executed[0]
    assert params["outcome"] == "gone"


def test_reclaim_stale_claims_releases_old_claims_and_prunes_ledger():
    conn = _FakeConn([(lambda s: "UPDATE listing_detail_queue" in s, [("1",), ("2",)])])
    n = db.reclaim_stale_claims(conn, "sreality", older_than_minutes=30)
    assert n == 2
    prune_sql, prune_params = conn.executed[0]
    assert "DELETE FROM detail_queue_completions" in prune_sql
    assert "completed_at < now() - make_interval(days => %s)" in prune_sql
    assert prune_params == ("sreality", db.COMPLETION_RETENTION_DAYS)
    sql, params = conn.executed[1]
    assert "SET claimed_at = NULL" in sql
    assert "claimed_at < now() - make_interval(mins => %s)" in sql
    assert params == ("sreality", 30)


# --- Phase 3: dirty-property enqueue ----------------------------------------


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


def test_active_count_is_source_scoped():
    conn = _FakeConn([
        (lambda s: "SELECT count(*) FROM listings" in s, [(42,)]),
    ])
    assert db.active_count(conn, "byt", "prodej", source="sreality") == 42
    sql, params = conn.executed[0]
    assert "AND source = %s" in sql
    assert params == ("sreality", "byt", "prodej")


_FLIP = "AND is_active = true RETURNING property_id"
_EXISTS = "SELECT 1 FROM listings WHERE source = %s AND source_id_native = %s"
_LEDGER_CLEAR = "DELETE FROM listing_fetch_failures f USING listings l"


def test_mark_listing_inactive_enqueues_its_property():
    conn = _FakeConn([
        (lambda s: _FLIP in s, [(42,)]),
        (lambda s: "INSERT INTO dirty_properties" in s, []),
    ])
    assert db.mark_listing_inactive(conn, "sreality", "12345") is True
    dirty = _find(conn.executed, "INSERT INTO dirty_properties")
    assert dirty is not None
    assert dirty[1] == (42,)
    assert _find(conn.executed, _EXISTS) is None        # the no-match probe runs only on a no-op


def test_mark_listing_inactive_no_property_no_dirty():
    conn = _FakeConn([
        (lambda s: _FLIP in s, [(None,)]),
    ])
    assert db.mark_listing_inactive(conn, "sreality", "12345") is True
    assert _find(conn.executed, "INSERT INTO dirty_properties") is None


def test_an_already_inactive_listing_is_not_restamped_or_redirtied():
    """The guard matches nothing: no second inactive_at (no duplicate collection
    monitor 'inactive' dispatch) and no dirty mark -- False, the row exists."""
    conn = _FakeConn([
        (lambda s: _FLIP in s, []),
        (lambda s: _EXISTS in s, [(1,)]),
    ])
    assert db.mark_listing_inactive(conn, "sreality", "12345") is False
    assert _find(conn.executed, "INSERT INTO dirty_properties") is None
    assert _find(conn.executed, _EXISTS)[1] == ("sreality", "12345")


def test_a_gone_flip_that_matches_no_listing_reports_none():
    """None, not False: the drain logs it at a level only the queue priority can pick."""
    conn = _FakeConn([
        (lambda s: _FLIP in s, []),
        (lambda s: _EXISTS in s, []),
    ])
    assert db.mark_listing_inactive(conn, "sreality", "12345") is None
    assert _find(conn.executed, "INSERT INTO dirty_properties") is None


@pytest.mark.parametrize(("flip_rows", "exists_rows"), [([(42,)], []), ([(None,)], []), ([], [(1,)]), ([], [])])
def test_every_gone_flip_clears_the_failure_ledger(flip_rows, exists_rows):
    """Rule #5: the failure row goes when the listing's fate is known. Keyed on data,
    not the portal -- a crawler row's NULL / synthetic sreality_id matches no ledger row."""
    conn = _FakeConn([
        (lambda s: _FLIP in s, flip_rows),
        (lambda s: _EXISTS in s, exists_rows),
    ])
    db.mark_listing_inactive(conn, "sreality", "12345")
    clear = _find(conn.executed, _LEDGER_CLEAR)
    assert clear is not None
    assert "f.sreality_id = l.sreality_id" in clear[0]
    assert clear[1] == ("sreality", "12345")


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
    # both statements clear the delisting stamp on reactivation (migration 175)
    assert "inactive_at = NULL" in react[0]
    bulk = _find(conn.executed, "SET last_seen_at = now(), is_active = true")
    assert bulk is not None and "inactive_at = NULL" in bulk[0]


def test_touch_listings_by_id_keys_on_surrogate_not_sreality_id():
    # The portal analogue keys on listings.id, never sreality_id: a post-Gate-2
    # portal row has sreality_id = NULL, so a sreality_id-keyed touch would match
    # nothing and starve rule #4's last_seen_at signal for every unchanged row.
    conn = _FakeConn([
        (lambda s: "WITH react AS" in s, []),
        (lambda s: "SET last_seen_at = now(), is_active = true" in s, [(1,), (2,)]),
    ])
    db.touch_listings_by_id(conn, [1, 2])
    react = _find(conn.executed, "WITH react AS")
    assert react is not None
    assert "listings.id = u.id" in react[0]
    assert "listings.sreality_id" not in react[0]
    assert "inactive_at = NULL" in react[0]
    bulk = _find(conn.executed, "SET last_seen_at = now(), is_active = true")
    assert bulk is not None
    assert "listings.id = u.id" in bulk[0]
    assert "listings.sreality_id" not in bulk[0]
