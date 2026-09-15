"""The land headline-area heal: ONE predicate, two columns, no snapshot, no marker.

This job moves a value the portal already published (`estate_area`) into the column the
one headline rule would put it in today (`area_m2`), on 52,183 rows three parsers left
without a headline area. What has to hold: it selects and writes exactly that population,
it writes nothing else, it is idempotent because the write empties its own selection, and
every batch carries its timeout guards inside a transaction (a `SET LOCAL` on an
autocommit connection binds to nothing).
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from scripts import backfill_land_headline_area as mod
from scripts import backfill_support as support


def test_the_pending_predicate_is_spelled_once_and_used_everywhere() -> None:
    assert mod._PENDING in mod._COUNT_BY_SOURCE_SQL
    assert mod._PENDING in mod._SELECT_SQL
    # the write re-asserts it, so a row a concurrent drain healed is not rewritten
    assert mod._PENDING in mod._UPDATE_SQL
    assert "category_main = 'pozemek'" in mod._PENDING
    assert "(area_m2 IS NULL OR area_m2 <= 0)" in mod._PENDING
    assert "estate_area > 0" in mod._PENDING


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


def test_the_selection_is_not_scoped_to_active_rows() -> None:
    # coverage is EVERY listing: an inactive parcel is still read by history, the
    # statistics and the dedup simulation.
    for statement in (mod._SELECT_SQL, mod._UPDATE_SQL, mod._PENDING):
        assert "is_active" not in statement
    # the dry-run readout still SPLITS by active, per source
    assert "FILTER (WHERE is_active)" in mod._COUNT_BY_SOURCE_SQL
    assert "GROUP BY source" in mod._COUNT_BY_SOURCE_SQL


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
