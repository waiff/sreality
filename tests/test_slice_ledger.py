"""The index-slice ledger (migration 454) — the memory that ends the restart.

An index walk starts at the first category's first page every time it runs. When
the budget runs out, the next run starts from the same place, so a catalogue
bigger than one budget does not get walked slowly — the same HEAD gets walked
over and over while the tail is never reached at all. idnes is the proof: 11 of
its last 14 runs were killed by the clock, and the one that finished covered 2
of the 10 category pairs we hold. The other 8 were not walked slowly. They were
not walked.

These tests pin the two properties that make the ledger fix that rather than
merely observe it: an unknown slice must sort FIRST, and a broken ledger must
cause more walking, never less.
"""

from __future__ import annotations

from typing import Any

from scraper import db


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((" ".join(sql.split()), params))
        if self._conn.raise_on_execute:
            raise RuntimeError("relation does not exist")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._conn.rows)


class _Conn:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []
        self.executed: list[tuple[str, Any]] = []
        self.raise_on_execute = False

    def cursor(self) -> _Cur:
        return _Cur(self)


def _record(conn: _Conn, **over: Any) -> None:
    kw: dict[str, Any] = dict(
        source="idnes", category_main="byt", category_type="prodej",
        slice_key="praha", outcome="exhausted", declared_total=5000,
        collected=5000, pages=193,
    )
    kw.update(over)
    db.record_index_slice(conn, **kw)


def test_a_slice_is_recorded_latest_wins() -> None:
    conn = _Conn()
    _record(conn)
    sql, params = conn.executed[0]
    assert "INSERT INTO portal_index_slices" in sql
    assert "ON CONFLICT" in sql and "DO UPDATE SET walked_at = now()" in sql
    assert "idnes" in params and "praha" in params and "exhausted" in params


def test_only_exhausted_is_the_positive_outcome() -> None:
    """Named once, in the module every caller reads, so a portal cannot invent
    its own spelling of success."""
    assert db.SLICE_OUTCOME_POSITIVE == "exhausted"


def test_a_failed_record_never_breaks_the_walk() -> None:
    """Bookkeeping must not be able to kill a scrape — and the failure mode is
    safe in the right direction: an unrecorded slice looks stale next run, so
    the ledger errs toward walking it again."""
    conn = _Conn()
    conn.raise_on_execute = True
    _record(conn)   # must not raise


def test_staleness_reads_hours_per_slice() -> None:
    conn = _Conn(rows=[
        ("byt", "prodej", "praha", 3.5),
        ("byt", "prodej", "__abroad__", 40.0),
    ])
    got = db.slice_staleness(conn, "idnes")
    assert got == {("byt", "prodej", "praha"): 3.5,
                   ("byt", "prodej", "__abroad__"): 40.0}


def test_a_never_walked_slice_is_ABSENT_not_zero() -> None:
    """The single most important property here. If an unknown slice came back as
    0.0 hours it would sort as the FRESHEST thing in the portal, so the slices
    that have never been walked would go last — which is precisely the
    starvation this table exists to end. Absent means the caller decides, and
    every caller reads absent as infinitely stale.
    """
    conn = _Conn(rows=[("byt", "prodej", "praha", 1.0)])
    got = db.slice_staleness(conn, "idnes")
    assert ("byt", "prodej", "zlinsky-kraj") not in got
    assert got.get(("byt", "prodej", "zlinsky-kraj")) is None


def test_an_unreadable_ledger_means_walk_everything() -> None:
    """Fail-safe direction: no memory is 'walk it all', never 'skip it all'. The
    opposite default would let one bad query silently stop the portal being
    covered while every check stayed green."""
    conn = _Conn()
    conn.raise_on_execute = True
    assert db.slice_staleness(conn, "idnes") == {}
