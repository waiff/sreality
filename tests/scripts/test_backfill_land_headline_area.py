"""The land headline-area heal: ONE predicate, two columns, no snapshot, no marker.

This job moves a value the portal already published (`estate_area`) into the column the
one headline rule would put it in today (`area_m2`), on 52,183 rows three parsers left
without a headline area. What has to hold: it selects and writes exactly that population —
including the ceiling, without which the FIRST page raises 22003 and the run heals nothing
— it writes nothing else, it is idempotent because the write empties its own selection, its
resume cursor never advances past a page it did not write, and every batch carries its
timeout guards inside a transaction (a `SET LOCAL` on an autocommit connection binds to
nothing).
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from scraper.area import LAND_CATEGORIES, MAX_AREA_M2
from scripts import backfill_land_headline_area as mod
from scripts import backfill_support as support


def test_the_pending_predicate_is_spelled_once_and_used_everywhere() -> None:
    assert mod._PENDING in mod._COUNT_BY_SOURCE_SQL
    assert mod._PENDING in mod._SELECT_SQL
    # the write re-asserts it, so a row a concurrent drain healed is not rewritten
    assert mod._PENDING in mod._UPDATE_SQL
    assert "(area_m2 IS NULL OR area_m2 <= 0)" in mod._PENDING
    assert "estate_area > 0" in mod._PENDING


def test_the_land_categories_come_from_the_one_vocabulary() -> None:
    # rendered from scraper.area.LAND_CATEGORIES, never re-spelled here
    for category in LAND_CATEGORIES:
        assert f"'{category}'" in mod._LAND_IN
    assert mod._LAND_IN in mod._PENDING
    assert "category_main IN (" in mod._PENDING


def test_the_ceiling_excludes_the_parcels_area_m2_cannot_store() -> None:
    """Without this the job heals NOTHING: `area_m2` is numeric(7,1) and `estate_area` is
    numeric(9,1), 20 pending rows hold a parcel above the ceiling (largest 16,809,800 m2),
    the first at rank ~4,741 in id order — so `SET area_m2 = estate_area` raises 22003
    inside the FIRST 5,000-row page, and a numeric overflow is not a lock fight to replay."""
    bound = f"estate_area < {MAX_AREA_M2:.1f}"
    assert bound in mod._PENDING
    assert bound in mod._SELECT_SQL and bound in mod._UPDATE_SQL
    # the excluded rows are counted and named, per portal, never silently dropped
    assert f"estate_area >= {MAX_AREA_M2:.1f}" in mod._UNSTORABLE
    assert mod._UNSTORABLE in mod._COUNT_BY_SOURCE_SQL
    assert "unstorable" in mod._COUNT_BY_SOURCE_SQL
    # and the bound is the column's, pinned to the write boundary's own copy
    from scraper.db import _NUMERIC_ABS_MAX

    assert MAX_AREA_M2 == float(_NUMERIC_ABS_MAX["area_m2"])


def test_the_write_touches_only_the_headline_area_and_its_basis() -> None:
    body = mod._UPDATE_SQL.upper()
    assert "SET AREA_M2 = ESTATE_AREA, AREA_BASIS = 'PLOT'" in " ".join(body.split())
    # never a snapshot: correcting our own mis-parse of the same stored page is the
    # sanctioned rule-2 exception, and the next detail refetch appends the one real
    # snapshot for a live row.
    assert "listing_snapshots" not in mod._UPDATE_SQL
    # and nothing else: one SET clause, one table
    assert mod._UPDATE_SQL.count("UPDATE ") == 1
    assert "estate_area =" not in mod._UPDATE_SQL


def test_the_dry_run_report_splits_active_and_excluded_per_portal() -> None:
    assert "GROUP BY source" in mod._COUNT_BY_SOURCE_SQL
    assert "pending_active" in mod._COUNT_BY_SOURCE_SQL
    assert "largest_unstorable" in mod._COUNT_BY_SOURCE_SQL


def test_the_selection_is_not_scoped_to_active_rows() -> None:
    # coverage is EVERY listing: an inactive parcel is still read by history, the
    # statistics and the dedup simulation.
    for statement in (mod._SELECT_SQL, mod._UPDATE_SQL, mod._PENDING):
        assert "is_active" not in statement


def test_the_page_read_names_no_wide_column() -> None:
    # id, source, property_id — a cheap primary-key walk that detoasts nothing.
    selected = mod._SELECT_SQL.split("FROM")[0]
    assert "raw_json" not in selected and "description" not in selected
    assert "property_id" in selected  # rule 20: the singleton mirror is re-derived


def test_every_batch_arms_its_guards_and_they_need_a_transaction() -> None:
    assert any("statement_timeout" in g for g in mod._BATCH_GUARDS)
    assert any("lock_timeout" in g for g in mod._BATCH_GUARDS)
    for guard in mod._BATCH_GUARDS:
        assert guard.startswith("SET LOCAL ")


def test_the_retry_window_outlasts_the_mf_yield_lock_hold() -> None:
    """The rail exists for the ~90 s of row locks the hourly MF-yield recompute holds on
    `listings`. A 5 s lock_timeout over 3 attempts came to ~60 s — it would give up while
    the holder was still working — so the bounded wait and the backoff both had to grow."""
    lock_seconds = 30.0
    assert f"lock_timeout = '{int(lock_seconds)}s'" in " ".join(mod._BATCH_GUARDS)
    attempts = support._LOCK_RETRY_ATTEMPTS
    backoff = sum(support._LOCK_WAIT_DELAY * a for a in range(1, attempts))
    assert backoff + attempts * lock_seconds > 120.0


# --- the shared rail: SET LOCAL only binds inside a transaction ---------------


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self.rowcount = 3

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append(sql)
        if self._conn.fail_once:
            self._conn.fail_once = False
            raise psycopg.errors.LockNotAvailable("lock_timeout")


class _Tx:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Tx":
        self._conn.transactions += 1
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, fail_once: bool = False) -> None:
        self.executed: list[str] = []
        self.transactions = 0
        self.fail_once = fail_once

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Tx:
        return _Tx(self)


def test_setup_statements_run_inside_one_transaction_before_the_batch() -> None:
    conn = _Conn()
    rows = support.execute_with_lock_retry(
        conn, mod._UPDATE_SQL, {"ids": [1, 2, 3]},
        label="test", setup=mod._BATCH_GUARDS,
    )
    assert rows == 3
    assert conn.transactions == 1
    assert conn.executed[:len(mod._BATCH_GUARDS)] == list(mod._BATCH_GUARDS)
    assert conn.executed[-1] == mod._UPDATE_SQL


def test_the_resume_cursor_never_advances_past_a_page_that_was_not_written() -> None:
    """The regression this pins: assigning the cursor from the page BEFORE the UPDATE means
    a failed page is skipped on `--after <cursor>` resume, silently and for ever. The
    assignment now sits after the write, and the failure path names the last written id."""
    source = (mod.__file__ and open(mod.__file__, encoding="utf-8").read()) or ""
    body = source.split("if not args.dry_run:", 1)[1]
    write_at = body.index("execute_with_lock_retry")
    dirty_at = body.index("mark_properties_dirty")
    cursor_at = body.index("cursor = ids[-1]")
    assert write_at < cursor_at and dirty_at < cursor_at
    assert "Resume with --after %d" in body


def test_a_dry_run_does_not_wait_for_a_rebuild_gap() -> None:
    # it takes no lock and writes nothing; waiting would only delay the report.
    source = open(mod.__file__, encoding="utf-8").read()
    assert "if not args.dry_run and not args.ignore_rebuild:" in source


def test_a_lock_timeout_is_replayed_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    # An armed `lock_timeout` fires as LockNotAvailable, not QueryCanceled — the batch is
    # idempotent, so the rail pauses and replays it rather than failing the run.
    monkeypatch.setattr(support.time, "sleep", lambda _s: None)
    conn = _Conn(fail_once=True)
    assert support.execute_with_lock_retry(
        conn, mod._UPDATE_SQL, {"ids": [1]}, label="test", setup=mod._BATCH_GUARDS,
    ) == 3
    assert conn.transactions == 2


def test_without_setup_the_statement_still_runs_unwrapped() -> None:
    conn = _Conn()
    support.execute_with_lock_retry(conn, "UPDATE x SET y = 1", {}, label="test")
    assert conn.transactions == 0
    assert conn.executed == ["UPDATE x SET y = 1"]
