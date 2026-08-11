"""The scan must never leave rows behind a watermark that has already moved past them.

`location_claim_batches` is the lane's only memory. Before migration 387 it remembered a
single terminal `outcome` and the runner stamped every non-raising run 'ok' — including a
run that stopped because it hit `--max-seconds` or `--limit`. Two failures came out of
that, and this module drives the real `run()` loop against an in-memory `listings` table
to pin both:

  * INCREMENTAL: the watermark is `max(started_at) WHERE outcome='ok'`, so a run that
    scanned the first slice and stopped moved the floor past everything it never opened.
    Those rows only come back if `last_seen_at` moves again — which for a delisted
    listing it never does, and a delisted listing's payload is exactly the evidence the
    history waves need.
  * FULL: with no cursor to resume from, every budgeted full pass restarts at id 0 and
    re-walks the same prefix forever.

The fake connection here is a real (small) query engine over a list of rows, not an
assertion recorder: the invariant under test is "every listing is seen exactly once
across the sequence of runs", and only executing the keyset arithmetic can show that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from location_data import claims_intake
from location_data.claims_intake import run
from tests.location_data.claim_intake_fixtures import (
    SREALITY_POST_CUTOVER,
    entries_for,
)

BASE_TS = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class _Listing:
    def __init__(self, listing_id: int, last_seen_at: datetime) -> None:
        self.id = listing_id
        self.last_seen_at = last_seen_at

    def record(self) -> tuple[Any, ...]:
        # The batch queries' column order, verbatim: identity, payload, sighting, the two
        # geom ordinates, inventory membership, then the class-B legacy columns.
        return (self.id, "sreality", f"n{self.id}", dict(SREALITY_POST_CUTOVER),
                self.last_seen_at, None, None, False, None)


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cursor":
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
    """An in-memory `listings` + `location_claim_batches` pair, keyset arithmetic and all."""

    def __init__(self, listings: list[_Listing]) -> None:
        self.listings = listings
        self.batches: list[dict[str, Any]] = []
        self.seen: list[int] = []
        self.now = BASE_TS

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Cursor:
        return _Cursor(self)

    def dispatch(self, cur: _Cursor, sql: str, params: dict[str, Any]) -> None:
        cur._result = []
        if "set_config" in sql:
            return
        if "FROM portal_contracts WHERE source" in sql:
            cur._result = [(1, 1)]
            return
        if sql.startswith("INSERT INTO location_claim_batches"):
            self.now += timedelta(minutes=1)
            batch = {
                "id": len(self.batches) + 1, "started_at": self.now,
                "source": params["source"], "scan_mode": params["scan_mode"],
                "resumable": params["resumable"], "outcome": "running",
                "cursor_after_id": None, "cursor_after_ts": None,
                "coverage_since": params["coverage_since"] or self.now,
            }
            self.batches.append(batch)
            cur._result = [(batch["id"], batch["coverage_since"])]
            return
        if sql.startswith("UPDATE location_claim_batches"):
            batch = self.batches[params["batch_id"] - 1]
            batch["outcome"] = params["outcome"]
            batch["cursor_after_id"] = params["cursor_after_id"]
            batch["cursor_after_ts"] = params["cursor_after_ts"]
            return
        if "SELECT max(coalesce(coverage_since, started_at))" in sql:
            oks = [b["coverage_since"] for b in self.batches
                   if b["outcome"] == "ok" and b["source"] == params["source"]]
            cur._result = [(max(oks) if oks else None,)]
            return
        if "SELECT outcome, cursor_after_id, cursor_after_ts" in sql:
            candidates = [b for b in self.batches
                          if b["source"] == params["source"]
                          and b["scan_mode"] == params["scan_mode"]
                          and b["resumable"]
                          and b["outcome"] in ("ok", "stopped", "failed")]
            if candidates:
                last = max(candidates, key=lambda b: (b["started_at"], b["id"]))
                cur._result = [(last["outcome"], last["cursor_after_id"],
                                last["cursor_after_ts"], last["coverage_since"])]
            return
        if "FROM listings l" in sql:
            cur._result = self._scan(sql, params)
            self.seen.extend(r[0] for r in cur._result)
            return
        if "INSERT INTO location_claims" in sql:
            cur._result = [(0, 0, 0)]
            return
        if sql.startswith("INSERT INTO location_claim_absences"):
            return
        if sql.startswith("INSERT INTO location_enrichment_state"):
            return
        raise AssertionError(f"unhandled SQL: {sql[:120]}")

    def _scan(self, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
        rows = sorted(self.listings, key=lambda r: r.id)
        if "l.last_seen_at >= %(watermark)s" in sql:
            rows = sorted(self.listings, key=lambda r: (r.last_seen_at, r.id))
            after_ts = params["after_ts"]
            rows = [r for r in rows
                    if r.last_seen_at >= params["watermark"]
                    and (r.last_seen_at, r.id) > (after_ts, params["after_id"])]
        else:
            rows = [r for r in rows if r.id > params["after_id"]]
        return [r.record() for r in rows[:params["batch_size"]]]


@pytest.fixture(autouse=True)
def _stub_preconditions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal gates have their own tests; this module is about the scan."""
    monkeypatch.setattr(claims_intake, "missing_relations", lambda conn: [])
    monkeypatch.setattr(claims_intake, "assert_inventory_ready", lambda conn: 2201)
    monkeypatch.setattr(
        claims_intake, "load_entries", lambda conn: {"sreality": entries_for("sreality")})


def _run(conn: _Conn, **kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "mode": "full", "source": "sreality", "batch_size": 10, "max_seconds": None,
        "limit": None, "start_after_id": 0, "overlap_hours": 3, "statement_timeout": 60,
        "dry_run": False, "note": None,
    }
    defaults.update(kwargs)
    return run(conn, **defaults)


def test_a_budget_stopped_full_run_is_stamped_stopped_and_resumes_where_it_left_off():
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])

    first = _run(conn, limit=10)
    assert first["outcome"] == "stopped"
    assert first["reached_end"] is False
    assert conn.seen == list(range(1, 11))
    assert conn.batches[0]["cursor_after_id"] == 10

    second = _run(conn, limit=10)
    assert second["resumed_from_id"] == 10
    assert second["outcome"] == "stopped"
    assert conn.seen == list(range(1, 21))

    third = _run(conn, limit=10)
    assert third["outcome"] == "ok"
    assert third["reached_end"] is True
    # Every listing exactly once across the three budgeted runs — no re-walked prefix,
    # and nothing skipped.
    assert conn.seen == list(range(1, 26))

    # The scan finished, so the NEXT full pass legitimately starts over.
    fourth = _run(conn, limit=10)
    assert fourth["resumed_from_id"] == 0
    assert conn.seen[-10:] == list(range(1, 11))


def test_the_watermark_never_advances_past_rows_a_stopped_run_never_opened():
    """The delisting failure, end to end. Row 25's `last_seen_at` is old and will never
    move again (it is delisted); if the first budgeted run's watermark advanced past it,
    nothing would ever mine its payload."""
    old = BASE_TS - timedelta(days=40)
    conn = _Conn(
        [_Listing(i, old) for i in range(1, 26)]
        + [_Listing(i, BASE_TS) for i in range(26, 31)])

    # Seed a completed pass so incremental mode has a floor to work from at all.
    seed = _run(conn, mode="full")
    assert seed["outcome"] == "ok"
    conn.seen.clear()

    # Everything now moves forward in time and is re-mined incrementally, but the run's
    # budget only covers the first 10.
    for row in conn.listings:
        row.last_seen_at = conn.now + timedelta(minutes=5)
    conn.now += timedelta(hours=1)

    stopped = _run(conn, mode="incremental", limit=10)
    assert stopped["outcome"] == "stopped"
    assert stopped["mode"] == "incremental"
    assert len(conn.seen) == 10

    resumed = _run(conn, mode="incremental")
    assert resumed["outcome"] == "ok"
    # 30 rows, seen once each: the stopped run's 10 plus the resumed run's 20. If the
    # stopped run had been stamped 'ok' the floor would have jumped to its start time and
    # the remaining 20 would never have been visited again.
    assert sorted(conn.seen) == list(range(1, 31))


def test_a_resumed_chain_claims_coverage_only_back_to_where_the_chain_started():
    """The watermark asserts "everything written before this instant has been mined". A
    chain of three budgeted runs can only assert that back to the FIRST run's start —
    taking the completing run's own `started_at` would silently skip anything re-scraped
    underneath the chain while it was still walking."""
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])

    _run(conn, limit=10)
    chain_start = conn.batches[0]["coverage_since"]
    _run(conn, limit=10)
    finished = _run(conn, limit=10)
    assert finished["outcome"] == "ok"

    assert [b["coverage_since"] for b in conn.batches] == [chain_start] * 3
    assert conn.batches[-1]["started_at"] > chain_start

    # And the next fresh scan starts its own coverage clock.
    _run(conn, limit=1)
    assert conn.batches[-1]["coverage_since"] == conn.batches[-1]["started_at"]


def test_an_operator_anchored_run_neither_resumes_nor_becomes_a_resume_point():
    """`--start-after-id` says "start here", not "everything below is done", so its cursor
    must be invisible to the next run (the guard migration 385 puts on
    `mapy_inventory_runs.resumable`, for the same reason)."""
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])

    anchored = _run(conn, start_after_id=20, limit=2)
    assert anchored["outcome"] == "stopped"
    assert conn.batches[0]["resumable"] is False
    assert conn.seen == [21, 22]

    conn.seen.clear()
    following = _run(conn, limit=5)
    assert following["resumed_from_id"] == 0
    assert conn.seen == [1, 2, 3, 4, 5]


def test_a_full_cursor_is_never_resumed_by_an_incremental_scan():
    """The two cursors are different keysets — `listings.id` vs `(last_seen_at, id)`."""
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])

    assert _run(conn, mode="full")["outcome"] == "ok"          # gives incremental a floor
    conn.now += timedelta(hours=1)
    for row in conn.listings:
        row.last_seen_at = conn.now
    stopped_full = _run(conn, mode="full", limit=5)
    assert stopped_full["outcome"] == "stopped"

    conn.seen.clear()
    incremental = _run(conn, mode="incremental")
    # It read the incremental keyset from its own watermark, not the full scan's id 5.
    assert incremental["mode"] == "incremental"
    assert sorted(conn.seen) == list(range(1, 26))
