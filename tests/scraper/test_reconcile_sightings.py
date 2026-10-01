"""The index walk's ONE sighting diff (rules #3/#4/#19): `portal_runner.reconcile_sightings`
plus the two db reads it and the drain dry-run lean on. Hermetic: the db functions are
patched through `portal_runner.db`, the same module object every adapter's `db` is."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from scraper import db, portal_runner


class _Cursor:
    def __init__(self, rows_for: Any) -> None:
        self.rows_for = rows_for
        self.executed: list[tuple[str, Any]] = []
        self._rows: list[tuple] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))
        self._rows = self.rows_for(sql, params)

    def fetchall(self) -> list[tuple]:
        return self._rows


class _CursorConn:
    def __init__(self, rows_for: Any) -> None:
        self.cur = _Cursor(rows_for)

    def cursor(self) -> _Cursor:
        return self.cur


# --- db.index_summary_native / db.claimable_counts ---------------------------


def test_index_summary_native_chunks_at_5000() -> None:
    def rows_for(_sql: str, params: Any) -> list[tuple]:
        _source, ids = params
        return [(n, int(n), None, 100, None) for n in ids if int(n) % 1000 == 0]

    conn = _CursorConn(rows_for)
    ids = [str(i) for i in range(12_001)] + ["0", "5000", "12000"]
    out = db.index_summary_native(conn, "ceskereality", ids)

    executed = conn.cur.executed
    assert len(executed) == 3
    assert [len(p[1]) for _, p in executed] == [5000, 5000, 2001]
    assert all(p[0] == "ceskereality" for _, p in executed)
    assert sorted(out, key=int) == [str(i) for i in range(0, 12_001, 1000)]
    assert out["5000"] == {"id": 5000, "sreality_id": None, "price_czk": 100, "last_seen_at": None}


def test_index_summary_native_empty_makes_no_statement() -> None:
    conn = _CursorConn(lambda *_a: [])
    assert db.index_summary_native(conn, "remax", []) == {}
    assert conn.cur.executed == []


def test_claimable_counts_all_sources_and_one() -> None:
    def rows_for(_sql: str, params: Any) -> list[tuple]:
        if params is None:
            return [("bazos", 3), ("remax", 7)]
        return [(params[0], 4)]

    conn = _CursorConn(rows_for)
    assert db.claimable_counts(conn) == {"bazos": 3, "remax": 7}
    assert db.claimable_counts(conn, "idnes") == {"idnes": 4}
    (all_sql, all_params), (one_sql, one_params) = conn.cur.executed
    assert all_params is None and "GROUP BY source" in all_sql
    assert one_params == ("idnes",) and "source = %s" in one_sql


# --- portal_runner.reconcile_sightings ---------------------------------------

_CONN = object()


def _patch_db(monkeypatch: pytest.MonkeyPatch, stored: dict[str, dict[str, Any]]) -> dict[str, list]:
    calls: dict[str, list] = {"order": [], "touched": [], "entries": []}

    def summary(conn: Any, source: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        calls["order"].append("index_summary_native")
        return {n: stored[n] for n in ids if n in stored}

    def touch(conn: Any, ids: list[int]) -> int:
        calls["order"].append("touch_listings_by_id")
        calls["touched"].append(list(ids))
        return len(ids)

    def enqueue(conn: Any, source: str, entries: list[tuple]) -> int:
        calls["order"].append("enqueue_detail")
        calls["entries"].append(list(entries))
        return len(entries)

    monkeypatch.setattr(portal_runner.db, "index_summary_native", summary)
    monkeypatch.setattr(portal_runner.db, "touch_listings_by_id", touch)
    monkeypatch.setattr(portal_runner.db, "enqueue_detail", enqueue)
    return calls


def _row(pk: int, price: int | None) -> dict[str, Any]:
    return {"id": pk, "sreality_id": None, "price_czk": price, "last_seen_at": None}


def test_touches_every_sighted_known_row_whatever_its_price(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_db(monkeypatch, {"a": _row(1, 100), "b": _row(2, 100)})
    out = portal_runner.reconcile_sightings(
        _CONN, "remax", {"a": ("ra", 100), "b": ("rb", 200), "c": ("rc", 50)},
        min_change_pct=0.0,
    )
    assert calls["touched"] == [[1, 2]]
    assert calls["entries"] == [[
        ("b", "rb", 200, portal_runner.db.QUEUE_PRIORITY_CHANGED),
        ("c", "rc", 50, portal_runner.db.QUEUE_PRIORITY_NEW),
    ]]
    assert out == {"found_new": 1, "enqueued": 2}


def test_touch_runs_before_enqueue(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_db(monkeypatch, {"a": _row(1, 100)})
    portal_runner.reconcile_sightings(
        _CONN, "idnes", {"a": (None, 300), "z": (None, 1_000)}, min_change_pct=0.0)
    assert calls["order"] == ["index_summary_native", "touch_listings_by_id", "enqueue_detail"]


def test_placeholder_price_reads_unchanged_against_stored_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """D3: a "1 Kč" (price on request) index card clamps to NULL before the
    compare, so it no longer reads as a reprice of a stored NULL every walk."""
    calls = _patch_db(monkeypatch, {"a": _row(7, None)})
    out = portal_runner.reconcile_sightings(
        _CONN, "ceskereality", {"a": ("ref", 1)}, min_change_pct=0.0)
    assert calls["touched"] == [[7]]
    assert calls["entries"] == []
    assert out == {"found_new": 0, "enqueued": 0}


def test_min_change_pct_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_db(monkeypatch, {"a": _row(1, 1_000_000)})
    portal_runner.reconcile_sightings(
        _CONN, "bazos", {"a": ("r", 1_000_500)}, min_change_pct=0.01)
    assert calls["entries"] == []
    portal_runner.reconcile_sightings(
        _CONN, "bazos", {"a": ("r", 1_000_500)}, min_change_pct=0.0)
    assert calls["entries"] == [[("a", "r", 1_000_500, portal_runner.db.QUEUE_PRIORITY_CHANGED)]]


def test_retry_first_puts_failures_first(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_db(monkeypatch, {"b": _row(2, 100), "d": _row(4, 100)})
    seen_changed: list[list[str]] = []

    def retry_first(conn: Any, changed: list[str]) -> set[str]:
        seen_changed.append(list(changed))
        return {"d"}

    portal_runner.reconcile_sightings(
        _CONN, "sreality", {"b": (None, 150), "d": (None, 150), "n": (None, 9)},
        min_change_pct=0.0, retry_first=retry_first,
    )
    assert seen_changed == [["b", "d"]]
    db_ = portal_runner.db
    assert calls["entries"] == [[
        ("d", None, 150, db_.QUEUE_PRIORITY_FAILURE),
        ("b", None, 150, db_.QUEUE_PRIORITY_CHANGED),
        ("n", None, 9, db_.QUEUE_PRIORITY_NEW),
    ]]


def test_retry_first_is_not_called_without_changed_rows_or_conn(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_db(monkeypatch, {"a": _row(1, 100)})

    def boom(conn: Any, changed: list[str]) -> set[str]:
        raise AssertionError("retry_first must not run")

    portal_runner.reconcile_sightings(
        _CONN, "sreality", {"a": (None, 100), "n": (None, 5)}, min_change_pct=0.0,
        retry_first=boom)
    portal_runner.reconcile_sightings(
        None, "sreality", {"a": (None, 999)}, min_change_pct=0.0, retry_first=boom)


def test_detail_ref_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_db(monkeypatch, {})
    portal_runner.reconcile_sightings(
        _CONN, "bezrealitky", {"1": (None, 10)}, min_change_pct=0.0)
    portal_runner.reconcile_sightings(
        _CONN, "idnes", {"2": ("https://reality.idnes.cz/detail/x/2", 20)}, min_change_pct=0.0)
    assert [e[0][1] for e in calls["entries"]] == [None, "https://reality.idnes.cz/detail/x/2"]


def test_dry_run_reads_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a dry run touched the database")

    for name in ("index_summary_native", "touch_listings_by_id", "enqueue_detail"):
        monkeypatch.setattr(portal_runner.db, name, boom)
    out = portal_runner.reconcile_sightings(
        None, "maxima", {"a": ("r", 1_000), "b": ("r", 2_000)}, min_change_pct=0.0)
    assert out == {"found_new": 2, "enqueued": 0}


def test_empty_sightings_make_no_db_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_db(monkeypatch, {})
    out = portal_runner.reconcile_sightings(_CONN, "remax", {}, min_change_pct=0.0)
    assert calls["order"] == []
    assert out == {"found_new": 0, "enqueued": 0}


def test_enqueue_line_shape(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    _patch_db(monkeypatch, {"a": _row(1, 100), "b": _row(2, 100)})
    caplog.set_level(logging.INFO, logger="scraper.portal_runner")
    sightings = {"a": ("r", 100), "b": ("r", 200), "c": ("r", 5_000)}
    portal_runner.reconcile_sightings(_CONN, "realitymix", sightings, min_change_pct=0.0)
    portal_runner.reconcile_sightings(
        _CONN, "maxima", sightings, min_change_pct=0.0, label=" cm=byt ct=prodej")
    lines = [(r.name, r.getMessage()) for r in caplog.records if r.getMessage().startswith("ENQUEUE")]
    assert lines == [
        ("scraper.portal_runner",
         "ENQUEUE source=realitymix new=1 changed=1 unchanged=1 enqueued=2"),
        ("scraper.portal_runner",
         "ENQUEUE source=maxima cm=byt ct=prodej new=1 changed=1 unchanged=1 enqueued=2"),
    ]
