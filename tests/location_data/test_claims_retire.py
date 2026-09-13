"""Rails on the W6-a delete of claims under retired contract versions.

The script deletes ~9.5 M rows from a 5.8 GB live table. Three properties make that a job
rather than an outage, and every one of them is a property of the SQL SHAPE rather than of
the run, so they are asserted offline:

  1. **The batch is a KEYSET.** `id > cursor ORDER BY id LIMIT n` — an ordered, early-stopping
     scan of the primary key that re-derives itself on a re-run. Lose the `ORDER BY` or the
     `id >` and the statement becomes "any N matching rows", which on a set whose survivors
     stay matched-against re-scans the same head of the table on every batch.
  2. **Only portal claims under a retired header are reachable.** The predicate is
     `contract_entry_id = ANY(entry_ids)` and the entry list comes from `NOT pc.is_active`.
     An operator correction carries `contract_entry_id IS NULL`, and NULL is never `= ANY`
     of anything — so the operator's own rows are unreachable by construction, not by care.
  3. **Nothing is enqueued.** The verdicts do not change (the resolver has never read these
     rows), so a `dirty_locations` write here would push ~800 k listings through the resolve
     drain to recompute answers that cannot move. The word must not appear in the module —
     which is also what keeps this script distinct from `contracts.py --retract`, where the
     enqueue is mandatory and inseparable from the delete.

Plus the loop's own arithmetic against a stub connection: the cursor follows the deleted
rows, an empty batch ends the walk, and the budget stops it BETWEEN batches.
"""

from __future__ import annotations

import re

import pytest

from scripts import location_claims_retire as retire


# ------------------------------------------------------------------ 1. the SQL shape


def _squash(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


def test_the_batch_is_an_id_keyset_with_an_order_and_a_limit():
    sql = _squash(retire._BATCH_SQL)
    assert "c.id > %(after_id)s" in sql, "the batch lost its keyset lower bound"
    assert "order by c.id" in sql, (
        "without ORDER BY, LIMIT picks arbitrary rows and the cursor means nothing"
    )
    assert "limit %(batch_size)s" in sql, "the batch is unbounded"
    assert sql.index("order by c.id") < sql.index("limit %(batch_size)s")


def test_the_cursor_comes_from_the_rows_actually_deleted():
    """`max(id)` over the DELETE's RETURNING, not over the pick: a row that survived the
    delete must never be jumped over."""
    sql = _squash(retire._BATCH_SQL)
    assert "returning c.id" in sql
    assert "select count(*), max(id) from deleted" in sql


def test_the_delete_is_one_statement_with_its_pick():
    """The pick and the delete cannot separate, so a batch cannot half-commit."""
    sql = _squash(retire._BATCH_SQL)
    assert sql.startswith("with victims as (")
    assert sql.count("delete from location_claims") == 1


def test_operator_claims_are_unreachable():
    """`= ANY(entry_ids)` never matches NULL, so a claim with no contract entry — the
    operator's own corrections — cannot be deleted by this module."""
    sql = _squash(retire._BATCH_SQL)
    assert "contract_entry_id = any(%(entry_ids)s)" in sql
    assert "is null" not in sql, "a NULL-tolerant predicate would reach operator claims"


def test_the_doomed_set_is_exactly_the_retired_headers():
    sql = _squash(retire._RETIRED_ENTRIES_SQL)
    assert "not pc.is_active" in sql
    assert "join portal_contracts pc on pc.id = pce.contract_id" in sql


def test_every_batch_arms_transaction_local_timeouts():
    """SET LOCAL, never a session SET: the transaction-mode pooler rebinds the backend
    between transactions, so a session setting need not be there for the statement it was
    meant to bound."""
    for stmt in (retire._BATCH_TIMEOUTS_SQL, retire._BATCH_STATEMENT_TIMEOUT_SQL):
        assert stmt.lower().startswith("set local ")
    assert "'5s'" in retire._BATCH_TIMEOUTS_SQL
    assert "'120s'" in retire._BATCH_STATEMENT_TIMEOUT_SQL


def test_no_statement_this_module_runs_touches_the_queue():
    """The verdicts do not change, so nothing re-resolves. `contracts.py --retract` is the
    mechanism that DOES enqueue — one statement, delete and enqueue inseparable — and it
    stays; this is the other case, and its SQL must say so."""
    statements = {
        name: value for name, value in vars(retire).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }
    assert statements, "the SQL constants moved; this rail found nothing to check"
    for name, sql in statements.items():
        assert "dirty_locations" not in sql.lower(), f"{name} writes the re-resolve queue"


# ------------------------------------------------------------------ 2. the loop


class _StubCursor:
    def __init__(self, conn: "_StubConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_StubCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict | None = None) -> None:
        self._conn.executed.append((sql, params))
        if params is not None and "after_id" in params:
            self._conn.calls.append(params)

    def fetchone(self) -> tuple:
        return self._conn.batches.pop(0)


class _StubTxn:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


class _StubConn:
    def __init__(self, batches: list[tuple[int, int | None]]) -> None:
        self.batches = list(batches)
        self.calls: list[dict] = []
        self.executed: list[tuple[str, dict | None]] = []

    def cursor(self) -> _StubCursor:
        return _StubCursor(self)

    def transaction(self) -> _StubTxn:
        return _StubTxn()


def test_the_cursor_advances_and_an_empty_batch_ends_the_walk():
    conn = _StubConn([(3, 30), (2, 55), (0, None)])
    deleted, batches, complete = retire.delete_batches(
        conn, entry_ids=[1, 2], batch_size=3, pause=0.0)
    assert (deleted, batches, complete) == (5, 2, True)
    assert [c["after_id"] for c in conn.calls] == [0, 30, 55]


def test_the_budget_stops_between_batches_and_reports_incomplete():
    """Never mid-batch: the check is at the top of the loop, so no statement is left
    half-applied and a re-dispatch simply continues."""
    conn = _StubConn([(3, 30), (3, 60), (0, None)])
    deleted, batches, complete = retire.delete_batches(
        conn, entry_ids=[1], batch_size=3, pause=0.0, max_seconds=-1)
    assert (deleted, batches, complete) == (0, 0, False)
    assert conn.calls == []


def test_an_unconfirmed_run_deletes_nothing(monkeypatch: pytest.MonkeyPatch):
    """The CLI's arming gate, against a connection whose batch loop would explode."""
    conn = _StubConn([])
    monkeypatch.setattr(retire, "connect", lambda: _ConnCtx(conn))
    monkeypatch.setattr(retire, "retired_entry_ids", lambda c: [(7, "bazos", 1)])
    monkeypatch.setattr(retire, "count_doomed", lambda c, ids, **kw: 9_500_000)
    monkeypatch.setattr(retire, "delete_batches", _explode)
    assert retire.main(["--dry-run"]) == 0
    assert retire.main([]) == 0
    assert retire.main(["--confirm", "nope"]) == 0


class _ConnCtx:
    def __init__(self, conn: _StubConn) -> None:
        self._conn = conn

    def __enter__(self) -> _StubConn:
        return self._conn

    def __exit__(self, *exc: object) -> None:
        return None


def _explode(*a: object, **k: object) -> None:
    raise AssertionError("an unarmed run must not reach the delete loop")
