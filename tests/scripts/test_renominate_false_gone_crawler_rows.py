"""The false-gone re-nomination queues each eligible row once, at VERIFY, in bounded batches.

Hermetic: a ledger-style fake conn keeps a `queue` keyed (source, native_id). The selection
reports a row as `queued` when the fake queue already holds it, and the enqueue INSERT adds
to it, so a second run over the same state is observable. The selection SQL itself is
PREPAREd against the real schema by tests/test_sql_schema_prepare.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from scraper import db
from scripts import renominate_false_gone_crawler_rows as ren


class _Cur:
    def __init__(self, conn: "_LedgerConn") -> None:
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
        if s.startswith("WITH verdict AS"):
            want = params["source"]
            self._rows = [
                (src, nid, url, price, (src, nid) in self._conn.queue, rechecked)
                for src, nid, url, price, rechecked in self._conn.false_gone
                if want is None or src == want
            ]
        elif s.startswith("INSERT INTO listing_detail_queue"):
            source = params["source"]
            for nid, prio in zip(params["nids"], params["prios"]):
                key = (source, nid)
                self._conn.queue[key] = max(self._conn.queue.get(key, prio), prio)
            self._rows = []
            self.rowcount = len(params["nids"])
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _LedgerConn:
    def __init__(self, false_gone: list[tuple[str, str, str | None, int | None, bool]],
                 queue: dict[tuple[str, str], int] | None = None) -> None:
        self.false_gone = false_gone
        self.queue: dict[tuple[str, str], int] = dict(queue or {})
        self.executed: list[tuple[str, Any]] = []

    def transaction(self) -> Any:
        raise AssertionError("the re-nomination opens no transaction of its own")

    def cursor(self) -> _Cur:
        return _Cur(self)

    def inserts(self) -> list[dict[str, Any]]:
        return [p for s, p in self.executed if s.startswith("INSERT INTO listing_detail_queue")]


def _rows(source: str, n: int, *, start: int = 1) -> list[tuple[str, str, str, int, bool]]:
    return [(source, f"{source[:2]}{i}", f"https://{source}.example/{i}", 1_000_000 + i, False)
            for i in range(start, start + n)]


def test_a_dry_run_counts_per_source_and_writes_nothing(caplog: pytest.LogCaptureFixture) -> None:
    conn = _LedgerConn(_rows("bazos", 3) + _rows("idnes", 2))
    with caplog.at_level("INFO", logger=ren.LOG.name):
        tallies = ren.run(conn, apply=False, limit=None, batch_size=10)
    assert {s: (t.false_gone, t.taken, t.enqueued) for s, t in tallies.items()} == {
        "bazos": (3, 3, 0), "idnes": (2, 2, 0)}
    assert conn.inserts() == [] and conn.queue == {}
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("RENOMINATE")]
    assert lines == [
        "RENOMINATE source=bazos false_gone=3 already_queued=0 rechecked_gone=0 "
        "eligible=3 would_enqueue=3",
        "RENOMINATE source=idnes false_gone=2 already_queued=0 rechecked_gone=0 "
        "eligible=2 would_enqueue=2",
    ]


def test_apply_writes_bounded_batches_at_verify_priority() -> None:
    conn = _LedgerConn(_rows("bazos", 5))
    tallies = ren.run(conn, apply=True, limit=None, batch_size=2)
    batches = conn.inserts()
    assert [p["nids"] for p in batches] == [["ba1", "ba2"], ["ba3", "ba4"], ["ba5"]]
    assert {pr for p in batches for pr in p["prios"]} == {db.QUEUE_PRIORITY_VERIFY}
    assert batches[0]["refs"] == ["https://bazos.example/1", "https://bazos.example/2"]
    assert batches[0]["prices"] == [1_000_001, 1_000_002]
    assert tallies["bazos"].enqueued == 5
    assert set(conn.queue) == {("bazos", f"ba{i}") for i in range(1, 6)}


def test_the_limit_is_per_source() -> None:
    conn = _LedgerConn(_rows("bazos", 4) + _rows("remax", 4))
    tallies = ren.run(conn, apply=True, limit=3, batch_size=2)
    assert {s: (len(t.eligible), t.enqueued) for s, t in tallies.items()} == {
        "bazos": (4, 3), "remax": (4, 3)}
    assert [p["nids"] for p in conn.inserts() if p["source"] == "remax"] == [
        ["re1", "re2"], ["re3"]]


def test_rows_already_queued_or_rechecked_by_their_portal_are_never_enqueued() -> None:
    rechecked = ("bazos", "ba9", "https://bazos.example/9", None, True)
    conn = _LedgerConn(_rows("bazos", 3) + [rechecked],
                       queue={("bazos", "ba2"): db.QUEUE_PRIORITY_NEW})
    tallies = ren.run(conn, apply=True, limit=None, batch_size=10)
    t = tallies["bazos"]
    assert (t.false_gone, t.already_queued, t.rechecked_gone, t.enqueued) == (4, 1, 1, 2)
    assert [p["nids"] for p in conn.inserts()] == [["ba1", "ba3"]]
    # The row the walk had already queued as NEW keeps its class.
    assert conn.queue[("bazos", "ba2")] == db.QUEUE_PRIORITY_NEW


def test_a_rerun_over_the_same_state_enqueues_nothing() -> None:
    conn = _LedgerConn(_rows("idnes", 3))
    ren.run(conn, apply=True, limit=2, batch_size=10)
    ren.run(conn, apply=True, limit=2, batch_size=10)
    sent = [nid for p in conn.inserts() for nid in p["nids"]]
    assert sent == ["id1", "id2", "id3"]
    assert len(sent) == len(set(sent))


def test_a_duplicate_selection_row_is_counted_once() -> None:
    conn = _LedgerConn(_rows("bazos", 2) + _rows("bazos", 1))
    tallies = ren.run(conn, apply=True, limit=None, batch_size=10)
    assert tallies["bazos"].false_gone == 2
    assert [p["nids"] for p in conn.inserts()] == [["ba1", "ba2"]]


def test_the_source_filter_reaches_the_selection() -> None:
    conn = _LedgerConn(_rows("bazos", 1) + _rows("idnes", 1))
    tallies = ren.run(conn, apply=False, limit=None, batch_size=10, source="idnes")
    assert list(tallies) == ["idnes"]
    assert conn.executed[0][1] == {"source": "idnes"}


@pytest.mark.parametrize("kwargs", [{"batch_size": 0, "limit": None},
                                    {"batch_size": 10, "limit": 0}])
def test_nonpositive_bounds_are_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ren.run(_LedgerConn([]), apply=True, **kwargs)


def test_the_selection_is_the_false_gone_signature() -> None:
    sql = " ".join(ren.FALSE_GONE_SQL.split())
    for clause in (
        "f.outcome = 'gone'",
        "f.sreality_id < 0",
        "JOIN listings l ON l.sreality_id = f.sreality_id",
        "l.source <> 'sreality'",
        "l.is_active = false",
        # Two-sided: the old writer flipped first and logged the check after.
        "l.inactive_at BETWEEN f.checked_at - interval '1 hour' "
        "AND f.checked_at + interval '1 hour'",
        "(l.last_seen_at IS NULL OR l.last_seen_at <= l.inactive_at)",
        "FROM listing_detail_queue q WHERE q.source = l.source "
        "AND q.native_id = l.source_id_native",
        "c.outcome = 'gone'",
        "COALESCE(pg.gone_at > v.verdict_at, false) AS rechecked",
    ):
        assert clause in sql, clause
    assert "DELETE" not in sql.upper() and "UPDATE" not in sql.upper()
