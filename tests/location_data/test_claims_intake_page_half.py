"""`run()` driven end to end through the PAGE half, on a fake connection.

The unit tests either side of this one pin the pieces: `test_claims_intake_run` pins the
scan SQL and the hash gate as TEXT, `test_page_readers` pins the readers and the fetch.
Neither can answer the question this file exists for — what the RUN LOOP does with them:
which bodies it asks the bucket for, which it stamps as mined, and whether a batch survives
the three ways one listing's page can fail.

That last part is the whole point. The batch is ONE transaction carrying every portal's
PAYLOAD claims, so anything that escapes the page half takes them down with it, stamps the
batch `failed`, leaves the watermark (`outcome='ok'` only) where it was — and hands the
next hourly run the same immutable body to die on again. The three failures are a bad
object, a content-triggered `IntakeRefused`, and a scoper that fails closed; all three must
cost exactly one listing's page entries and leave that body UNSTAMPED so it is retried.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from location_data import claims_intake, page_readers
from location_data.claims_common import IntakeRefused, IntakeResult
from location_data.html_scope import ScopeRegister
from tests.location_data.claim_intake_fixtures import (
    SREALITY_POST_CUTOVER,
    entries_for,
)

BASE_TS = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
BODY = b"<html><body><h1>Byt 3+1</h1></body></html>"
ACTIVE_VERSION = 1

# (listing_id, body_id or None, is the body unmined?)
UNMINED, ALREADY_MINED, NO_BODY = (1, 101, True), (2, 102, False), (3, None, False)


def _key(body_id: int) -> str:
    return f"payloads/sreality/{body_id}/body.html"


def _record(listing_id: int, body_id: int | None, unmined: bool) -> tuple[Any, ...]:
    """One scan row in the two batch queries' column order."""
    return (
        listing_id, "sreality", f"n{listing_id}", dict(SREALITY_POST_CUTOVER),
        BASE_TS + timedelta(minutes=listing_id), None, None, False,
        body_id, unmined, "detail" if body_id else None,
        "ab" * 32 if body_id else None, BASE_TS if body_id else None, ACTIVE_VERSION,
        None, None, None,
    )


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self._conn.dispatch(self, " ".join(sql.split()), params or {})

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result


class _Conn:
    """`listings` + its joined bodies + the batch ledger, as an in-memory dispatcher."""

    def __init__(self, rows: list[tuple[int, int | None, bool]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.stamped: list[dict[str, Any]] = []
        self.claim_writes: list[list[dict[str, Any]]] = []
        self._scanned = False

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Cursor:
        return _Cursor(self)

    def dispatch(self, cur: _Cursor, sql: str, params: dict[str, Any]) -> None:
        cur._result = []
        self.executed.append((sql, params))
        if "set_config" in sql or sql.startswith("UPDATE location_claim_batches"):
            return
        if "FROM portal_contracts WHERE source" in sql:
            cur._result = [(1, ACTIVE_VERSION)]
            return
        if sql.startswith("INSERT INTO location_claim_batches"):
            cur._result = [(7, BASE_TS)]
            return
        if "SELECT max(coalesce(coverage_since, started_at))" in sql:
            cur._result = [(None,)]
            return
        if "SELECT outcome, cursor_after_id" in sql:
            return
        if "FROM listings l" in sql:
            # One page of rows, then empty — so the scan reaches its end and stamps 'ok'.
            if not self._scanned:
                self._scanned = True
                cur._result = [_record(*r) for r in self.rows]
            return
        if "FROM portal_raw_payloads" in sql and sql.startswith("SELECT id, body"):
            cur._result = [
                (body_id, None, _key(body_id), "identity")
                for body_id in params["ids"]
            ]
            return
        if "INSERT INTO location_claims" in sql:
            self.claim_writes.append(params["rows"].obj)
            cur._result = [(len(params["rows"].obj), 1)]
            return
        if sql.startswith("UPDATE portal_raw_payloads"):
            self.stamped.extend(params["rows"].obj)
            return
        raise AssertionError(f"unhandled SQL: {sql[:120]}")


class _Store:
    """An R2 whose GET fails for the keys in `failing`."""

    def __init__(self, failing: set[str] | None = None) -> None:
        self.asked: list[str] = []
        self.failing = failing or set()

    def download_bytes(self, key: str) -> bytes:
        self.asked.append(key)
        if key in self.failing:
            raise OSError("R2 timed out")
        return BODY


@pytest.fixture(autouse=True)
def _stub_preconditions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal gates and the reader registry have their own tests; this module is about
    the run loop, so the contract is stubbed to "sreality has a page entry"."""
    monkeypatch.setattr(claims_intake, "missing_relations", lambda conn: [])
    monkeypatch.setattr(claims_intake, "assert_inventory_ready", lambda conn: 2201)
    monkeypatch.setattr(
        claims_intake, "load_entries", lambda conn: {"sreality": entries_for("sreality")})
    monkeypatch.setattr(
        page_readers, "load_registers",
        lambda conn: {"sreality": ScopeRegister.from_zones("sreality", ())})
    # sreality's contract is API-JSON and carries no DOM entry, so both page-half gates —
    # `run()`'s page_capable set and the per-body candidate filter — would be empty here.
    # Say that one of its entries is a page entry. WHAT such an entry extracts is
    # `test_page_readers`' business; this file is about the loop around it.
    monkeypatch.setattr(
        page_readers, "PAGE_READERS", {"scalar": lambda *a: []})
    monkeypatch.setattr(
        page_readers, "page_entries", lambda entries, page_kind: list(entries)[:1])


def _run(conn: _Conn, store: Any) -> dict[str, Any]:
    return claims_intake.run(
        conn, mode="full", source="sreality", batch_size=10, max_seconds=None,
        limit=None, start_after_id=0, overlap_hours=3, statement_timeout=60,
        dry_run=False, note=None, store=store)


def _page_claim(*_a: Any, **_k: Any) -> IntakeResult:
    result = IntakeResult()
    return result


# ---------------------------------------------------------------- the happy path

def test_only_an_unmined_body_is_fetched_and_only_it_is_stamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hash gate, end to end. A body already at the active contract version costs no
    R2 round trip at all — which is the whole reason the hourly cost is bounded by page
    churn rather than by corpus size — and a listing with no stored body is simply a
    payload-only listing."""
    monkeypatch.setattr(page_readers, "extract_page", _page_claim)
    conn = _Conn([UNMINED, ALREADY_MINED, NO_BODY])
    store = _Store()

    stats = _run(conn, store)

    assert store.asked == [_key(101)]
    assert stats["bodies_eligible"] == 1 and stats["bodies_fetched"] == 1
    assert stats["bodies_mined"] == 1
    assert conn.stamped == [{"id": 101, "version": ACTIVE_VERSION}]
    assert stats["outcome"] == "ok"


def test_every_listing_with_payload_entries_still_gets_its_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page half is an ADDITION. All three listings go through the payload readers
    whatever their body situation is."""
    monkeypatch.setattr(page_readers, "extract_page", _page_claim)
    conn = _Conn([UNMINED, ALREADY_MINED, NO_BODY])

    stats = _run(conn, _Store())

    written = [row["listing_id"] for chunk in conn.claim_writes for row in chunk]
    assert set(written) == {1, 2, 3}
    assert stats["claims_payload"] == len(written) and stats["claims_page"] == 0


# ------------------------------------------- the three ways one listing's page can fail

def test_a_failed_object_leaves_that_body_unstamped_and_the_batch_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1's regression. The GET fails, the batch still commits its payload claims, and the
    body is NOT stamped — so the next run asks for it again instead of skipping claims that
    were never written."""
    monkeypatch.setattr(page_readers, "extract_page", _page_claim)
    conn = _Conn([UNMINED, ALREADY_MINED, NO_BODY])
    store = _Store(failing={_key(101)})

    stats = _run(conn, store)

    assert store.asked == [_key(101)]
    assert conn.stamped == []
    assert stats["bodies_mined"] == 0
    assert stats["outcome"] == "ok"
    written = [row["listing_id"] for chunk in conn.claim_writes for row in chunk]
    assert set(written) == {1, 2, 3}, "the payload half must survive a bad object"


def test_a_content_triggered_refusal_costs_one_listing_not_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2's regression. `extract_page` raises `IntakeRefused` per LISTING — a coordinate
    read without its position branch, a span the evidence CHECK would reject, a blur class
    a migration may not write. The body is immutable, so propagating it would wedge the
    lane on the same row every hour."""
    def _boom(*_a: Any, **_k: Any) -> IntakeResult:
        raise IntakeRefused("rx.det.gps returned a coordinate without a position_branch")

    monkeypatch.setattr(page_readers, "extract_page", _boom)
    conn = _Conn([UNMINED, ALREADY_MINED, NO_BODY])

    stats = _run(conn, _Store())

    assert conn.stamped == []
    assert stats["bodies_mined"] == 0
    assert stats["outcome"] == "ok"
    assert stats["refusal_reasons"]["page_extract_refused:sreality"] == 1
    written = [row["listing_id"] for chunk in conn.claim_writes for row in chunk]
    assert set(written) == {1, 2, 3}


def test_an_incomplete_scope_is_never_stamped_as_mined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scoper fails CLOSED, so an incomplete parse yields no claims. Stamping it would
    record the body as mined AT this contract version and hide the miss until the next
    bump — the one thing the hash gate must not do."""
    def _incomplete(*_a: Any, **_k: Any) -> IntakeResult:
        result = IntakeResult()
        result.refusals["scope_incomplete"] += 2
        return result

    monkeypatch.setattr(page_readers, "extract_page", _incomplete)
    conn = _Conn([UNMINED, ALREADY_MINED, NO_BODY])

    stats = _run(conn, _Store())

    assert conn.stamped == []
    assert stats["refusal_reasons"]["scope_incomplete"] == 2
    assert stats["outcome"] == "ok"


def test_a_run_with_no_object_store_mines_nothing_and_still_writes_payload_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rotated-credential case: `_open_body_store` returns None, no candidate is even
    built, and the hourly ingest for all nine portals is untouched."""
    monkeypatch.setattr(page_readers, "extract_page", _page_claim)
    monkeypatch.setattr(claims_intake, "_open_body_store", lambda page_capable: None)
    conn = _Conn([UNMINED, ALREADY_MINED, NO_BODY])

    stats = claims_intake.run(
        conn, mode="full", source="sreality", batch_size=10, max_seconds=None,
        limit=None, start_after_id=0, overlap_hours=3, statement_timeout=60,
        dry_run=False, note=None)

    assert stats["bodies_eligible"] == 0 and conn.stamped == []
    written = [row["listing_id"] for chunk in conn.claim_writes for row in chunk]
    assert set(written) == {1, 2, 3}
