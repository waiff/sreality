"""The write-path wiring of listings.discovery_seq / listing_detail_queue.discovery_seq
(migration 368 — docs/design/portal-order-fidelity.md, Phase 1).

Unlike published_at (a parsed portal field, in LISTING_COLUMNS, preserve-if-null),
discovery_seq is a PIPELINE-assigned value carried from the claimed queue row — never
parsed from portal content, so it stays out of LISTING_COLUMNS and out of ScrapedListing's
contract entirely; ingest_scraped_listing / upsert_listing take it as an explicit
parameter, mirroring how source_id_native / geom are handled. Its semantics are SET-ONCE
(COALESCE(listings.discovery_seq, EXCLUDED.discovery_seq) — favor the STORED value), not
preserve-if-null (which favors the INCOMING value) — a listing's discovery position is a
first-discovery fact, not something a later fetch should ever be allowed to correct."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from scraper import db


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _RecordingCur:
    """One cursor, reused across every execute() in a single `with conn.transaction(),
    conn.cursor() as cur:` block (upsert_listing's shape) — records every statement so
    a test can pick out a specific one, and pops staged fetchone() results in call order."""

    def __init__(self, fetchone_results: list[Any]) -> None:
        self._fetchone_results = list(fetchone_results)
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self) -> "_RecordingCur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        return self._fetchone_results.pop(0) if self._fetchone_results else None


class _UpsertConn:
    """Fakes upsert_listing's connection: one cursor for the whole call."""

    def __init__(self, fetchone_results: list[Any]) -> None:
        self.cur = _RecordingCur(fetchone_results)

    def transaction(self) -> _Ctx:
        return _Ctx()

    def cursor(self) -> _RecordingCur:
        return self.cur


class _NewCurEachTimeConn:
    """Fakes ingest_scraped_listing's connection: a FRESH cursor per `with
    conn.cursor() as cur:` block, each pre-loaded with the next staged fetchone()
    result (or None once exhausted — fine for execute-only cursors)."""

    def __init__(self, fetchone_results: list[Any]) -> None:
        self._results = list(fetchone_results)

    def cursor(self) -> _RecordingCur:
        result = self._results.pop(0) if self._results else None
        return _RecordingCur([result])

    def transaction(self) -> _Ctx:
        return _Ctx()


def test_discovery_seq_is_not_a_listing_column() -> None:
    # Deliberately NOT parsed from portal content -- it must never round-trip
    # through the generic preserve-if-null / hash machinery LISTING_COLUMNS drives.
    assert "discovery_seq" not in db.LISTING_COLUMNS
    assert "discovery_seq" not in db._PRESERVE_IF_NULL_COLUMNS


def test_upsert_listing_sql_inserts_and_set_once_preserves_discovery_seq() -> None:
    # fetchone() order inside upsert_listing: (1) the upsert's RETURNING
    # (inserted, listing_id), (2) the prior-snapshot content_hash lookup (None ->
    # no prior snapshot -> "new", so the snapshot INSERT also runs harmlessly).
    conn = _UpsertConn(fetchone_results=[(True, 1), None])
    db.upsert_listing(
        conn,
        row={"sreality_id": 1, "discovery_seq": 501},
        raw_json={"id": 1},
        content_hash="h1",
    )
    upsert_sql, upsert_params = conn.cur.executed[0]
    assert "INSERT INTO listings (" in upsert_sql
    assert "discovery_seq" in upsert_sql
    assert "%(discovery_seq)s" in upsert_sql
    assert (
        "discovery_seq = COALESCE(listings.discovery_seq, EXCLUDED.discovery_seq)"
        in upsert_sql
    )
    assert upsert_params["discovery_seq"] == 501


def test_upsert_listing_defaults_discovery_seq_to_none_when_absent() -> None:
    conn = _UpsertConn(fetchone_results=[(True, 1), None])
    # A caller outside the queue-driven drain (e.g. a manual re-ingest) passes no
    # discovery_seq at all -- must default to NULL, not raise.
    db.upsert_listing(conn, row={"sreality_id": 1}, raw_json={}, content_hash="h1")
    _, upsert_params = conn.cur.executed[0]
    assert upsert_params["discovery_seq"] is None


def test_batch_upsert_sql_declares_and_set_once_preserves_discovery_seq() -> None:
    assert "discovery_seq bigint" in db._BATCH_UPSERT_SQL
    assert "j.discovery_seq" in db._BATCH_UPSERT_SQL
    assert (
        "discovery_seq = COALESCE(listings.discovery_seq, EXCLUDED.discovery_seq)"
        in db._BATCH_UPSERT_SQL
    )


class _Listing:
    def __init__(self, source: str, source_id_native: str) -> None:
        self.source = source
        self.source_id_native = source_id_native
        self.source_url = f"https://example.test/{source_id_native}"
        self.raw: dict[str, Any] = {}

    def to_row(self, legacy_sreality_id: int | None) -> dict[str, Any]:
        return {"sreality_id": legacy_sreality_id}

    def content_hash(self) -> str:
        return "h1"


def test_ingest_scraped_listing_threads_discovery_seq_into_row(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_upsert_listing(conn, row, raw_json, content_hash):
        captured.update(row)
        return "new"

    monkeypatch.setattr(db, "upsert_listing", _fake_upsert_listing)
    monkeypatch.setattr(db, "_ensure_property", lambda *a, **k: None)
    # Skip the nextval-cursor branch so the canned fetchone sequence below only
    # has to cover the two SELECTs ingest_scraped_listing actually issues:
    # (1) the pre-transaction existence check (None -> "not found yet"), (2) the
    # post-upsert surrogate-id lookup ((7,)). The UPDATE/INSERT cursors that
    # follow never call fetchone.
    monkeypatch.setattr(db, "_gate2_null_sreality_id_enabled", lambda conn: True)

    conn = _NewCurEachTimeConn(fetchone_results=[None, (7,)])
    listing_id, result = db.ingest_scraped_listing(
        conn, _Listing("bazos", "abc"), discovery_seq=777,
    )
    assert result == "new"
    assert listing_id == 7
    assert captured["discovery_seq"] == 777


def test_ingest_scraped_listing_defaults_discovery_seq_to_none(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_upsert_listing(conn, row, raw_json, content_hash):
        captured.update(row)
        return "new"

    monkeypatch.setattr(db, "upsert_listing", _fake_upsert_listing)
    monkeypatch.setattr(db, "_ensure_property", lambda *a, **k: None)
    monkeypatch.setattr(db, "_gate2_null_sreality_id_enabled", lambda conn: True)

    conn = _NewCurEachTimeConn(fetchone_results=[None, (9,)])
    # No discovery_seq kwarg passed -- the default (None) must reach the row,
    # never raise, for callers outside the queue-driven drain.
    db.ingest_scraped_listing(conn, _Listing("idnes", "xyz"))
    assert captured["discovery_seq"] is None


# --- discovered_at (migration 444): the same set-once contract, in time ------
#
# discovery_seq answers "in what ORDER did we discover this"; discovered_at
# answers "WHEN". Both are pipeline-assigned from the claimed queue row, both are
# set-once. The pair exists because first_seen_at means "when the drain WROTE
# it", which diverged from discovery by nine days during the 2026-08-17
# starvation and silently biased every series built on it.


def test_upsert_listing_sql_inserts_and_set_once_preserves_discovered_at() -> None:
    conn = _UpsertConn(fetchone_results=[(True, 1), None])
    stamp = datetime(2026, 8, 25, 15, 39, tzinfo=timezone.utc)
    db.upsert_listing(
        conn,
        row={"sreality_id": 1, "discovered_at": stamp},
        raw_json={"id": 1},
        content_hash="h1",
    )
    upsert_sql, upsert_params = conn.cur.executed[0]
    assert "%(discovered_at)s" in upsert_sql
    assert (
        "discovered_at = COALESCE(listings.discovered_at, EXCLUDED.discovered_at)"
        in upsert_sql
    )
    assert upsert_params["discovered_at"] == stamp


def test_upsert_listing_defaults_discovered_at_to_none_when_absent() -> None:
    conn = _UpsertConn(fetchone_results=[(True, 1), None])
    db.upsert_listing(conn, row={"sreality_id": 1}, raw_json={}, content_hash="h1")
    _, upsert_params = conn.cur.executed[0]
    assert upsert_params["discovered_at"] is None


def test_batch_upsert_sql_declares_and_set_once_preserves_discovered_at() -> None:
    assert "discovered_at timestamptz" in db._BATCH_UPSERT_SQL
    assert "j.discovered_at" in db._BATCH_UPSERT_SQL
    assert (
        "discovered_at = COALESCE(listings.discovered_at, EXCLUDED.discovered_at)"
        in db._BATCH_UPSERT_SQL
    )


def test_discovered_at_is_not_a_listing_column() -> None:
    """Pipeline-assigned, never parsed from portal content — so it stays out of
    LISTING_COLUMNS exactly like discovery_seq (published_at, which IS parsed
    from the page, is the deliberate contrast)."""
    assert "discovered_at" not in db.LISTING_COLUMNS
    assert "published_at" in db.LISTING_COLUMNS


def test_ingest_scraped_listing_threads_discovered_at_into_row(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_upsert_listing(conn, row, raw_json, content_hash):
        captured.update(row)
        return "new"

    monkeypatch.setattr(db, "upsert_listing", _fake_upsert_listing)
    monkeypatch.setattr(db, "_ensure_property", lambda *a, **k: None)
    monkeypatch.setattr(db, "_gate2_null_sreality_id_enabled", lambda conn: True)

    stamp = datetime(2026, 8, 25, 15, 39, tzinfo=timezone.utc)
    conn = _NewCurEachTimeConn(fetchone_results=[None, (7,)])
    _listing_id, result = db.ingest_scraped_listing(
        conn, _Listing("bazos", "abc"), discovery_seq=777, discovered_at=stamp,
    )
    assert result == "new"
    assert captured["discovered_at"] == stamp
