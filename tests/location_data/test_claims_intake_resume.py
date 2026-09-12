"""The scan must never leave rows behind a cursor that has already moved past them.

`location_claim_batches` is the lane's only memory, and since W1-a2 the cursor is ALL of it:
no watermark is left to cross-check against. This module drives the real `run()` loop over an
in-memory `listings` + `listing_snapshots` pair to pin what that costs and what it buys.

  * INCREMENTAL walks `listing_snapshots.id`, the append-on-content-change log. Its cursor
    survives EVERY terminal outcome — a completed run resumes from the end of the log, not
    from zero — because the cursor is a position in an append-only sequence, not a claim
    about coverage. A run that re-read the log from 0 each hour would re-insert claims that
    exist; one that dropped its cursor on 'ok' would do exactly that.
  * FULL resumes from an UNFINISHED predecessor — 'stopped' (out of budget) or 'failed'
    (W1-a5) — because its cursor is a keyset position that only advances past a batch whose
    transaction closed. Only 'ok' restarts at id 0: the whole table was walked, and the next
    full pass is the contract-bump re-walk.
  * A pre-W1-a2 incremental cursor is a LISTING id and is refused: reading it back as a
    snapshot id would silently skip every snapshot below it.

The fake connection is a real (small) query engine over a list of rows, not an assertion
recorder: the invariant is "every change is seen exactly once across the sequence of runs".
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

    def record(self, snapshot_cursor: int | None) -> tuple[Any, ...]:
        # The selections' column order, verbatim: identity, payload, sighting, the two geom
        # ordinates, inventory membership, the latest stored detail body (id, is it
        # unmined, page_kind, sha, first seen), the portal's active contract version and the
        # snapshot cursor — the LAST column since W1-c deleted the legacy-column tail. This
        # listing has no stored body.
        return (self.id, "sreality", f"n{self.id}", dict(SREALITY_POST_CUTOVER),
                self.last_seen_at, None, None, False,
                None, None, None, None, None, 1, snapshot_cursor)


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
    """`listings` + `listing_snapshots` + the batch ledger, keyset arithmetic and all."""

    def __init__(self, listings: list[_Listing],
                 snapshots: list[tuple[int, int, datetime]] | None = None) -> None:
        self.listings = listings
        # (snapshot id, listing id, scraped_at), ascending — the append-only change log.
        self.snapshots = snapshots if snapshots is not None else []
        self.batches: list[dict[str, Any]] = []
        self.seen: list[int] = []
        self.now = BASE_TS

    def change(self, listing_ids: list[int], *, age_minutes: int = 30) -> None:
        """Append one snapshot per listing, as a content change does (rule 2).

        `age_minutes` is how long ago the change committed. The default clears the lane's
        15-minute lag; a smaller one is a change still inside the window where a concurrent
        `write_detail_batch` transaction could still be allocating ids."""
        next_id = (self.snapshots[-1][0] if self.snapshots else 0) + 1
        at = self.now - timedelta(minutes=age_minutes)
        for offset, listing_id in enumerate(listing_ids):
            self.snapshots.append((next_id + offset, listing_id, at))

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
            }
            self.batches.append(batch)
            cur._result = [(batch["id"],)]
            return
        if sql.startswith("UPDATE location_claim_batches"):
            batch = self.batches[params["batch_id"] - 1]
            batch["outcome"] = params["outcome"]
            batch["cursor_after_id"] = params["cursor_after_id"]
            return
        if "cursor_after_ts IS NOT NULL" in sql:
            # The cutover anchor: the newest row that still carries a pre-W1-a2 timestamp
            # cursor, else the last `ok` watermark, both minus the old 3-hour overlap.
            timestamped = [b for b in self.batches
                           if b["source"] == params["source"]
                           and b.get("cursor_after_ts") is not None]
            anchor = None
            if timestamped:
                anchor = max(timestamped,
                             key=lambda b: (b["started_at"], b["id"]))["cursor_after_ts"]
            else:
                oks = [b.get("coverage_since") or b["started_at"] for b in self.batches
                       if b["source"] == params["source"] and b["outcome"] == "ok"]
                anchor = max(oks) if oks else None
            cur._result = [(anchor - timedelta(hours=3) if anchor else None,)]
            return
        if "coalesce(max(id), 0) FROM listing_snapshots" in sql:
            visible = [row for row in self._visible_snapshots()
                       if params["watermark"] is None or row[2] <= params["watermark"]]
            cur._result = [(visible[-1][0] if visible else 0,)]
            return
        if "SELECT outcome, cursor_after_id, cursor_after_ts" in sql:
            candidates = [b for b in self.batches
                          if b["source"] == params["source"]
                          and b["scan_mode"] == params["scan_mode"]
                          and b["resumable"]
                          and b["cursor_after_id"] is not None
                          and b["outcome"] in ("ok", "stopped", "failed")]
            if candidates:
                last = max(candidates, key=lambda b: (b["started_at"], b["id"]))
                cur._result = [(last["outcome"], last["cursor_after_id"],
                                last["cursor_after_ts"])]
            return
        if "FROM listing_snapshots s" in sql or "FROM listings l" in sql:
            cur._result = self._scan(sql, params)
            self.seen.extend(r[0] for r in cur._result)
            return
        if "INSERT INTO location_claims" in sql:
            cur._result = [(0, 0)]
            return
        if sql.startswith("UPDATE portal_raw_payloads"):
            return
        raise AssertionError(f"unhandled SQL: {sql[:120]}")

    def _visible_snapshots(self) -> list[tuple[int, int, datetime]]:
        """`scraped_at < now() - interval '15 minutes'` — the lag, as the SQL applies it."""
        return [row for row in self.snapshots
                if row[2] < self.now - timedelta(minutes=15)]

    def _scan(self, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
        if "FROM listing_snapshots s" in sql:
            window = [(sid, lid) for sid, lid, _ in self._visible_snapshots()
                      if sid > params["after_id"]][:params["batch_size"]]
            newest: dict[int, int] = {}
            for sid, lid in window:
                newest[lid] = max(newest.get(lid, 0), sid)
            by_id = {row.id: row for row in self.listings}
            return [by_id[lid].record(sid)
                    for lid, sid in sorted(newest.items()) if lid in by_id]
        rows = [r for r in sorted(self.listings, key=lambda r: r.id)
                if r.id > params["after_id"]]
        return [r.record(None) for r in rows[:params["batch_size"]]]


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
        "limit": None, "start_after_id": 0, "statement_timeout": 60,
        "dry_run": False, "note": None,
    }
    defaults.update(kwargs)
    return run(conn, **defaults)


# ------------------------------------------------------------------ full mode


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

    # The scan finished, so the NEXT full pass legitimately starts over: full mode is the
    # contract-bump re-walk, and a contract bump has to reach every row again.
    fourth = _run(conn, limit=10)
    assert fourth["resumed_from_id"] == 0
    assert conn.seen[-10:] == list(range(1, 11))


def test_a_failed_full_run_is_resumed_from_and_never_resets_the_walk_to_zero():
    """W1-a5, from run 34689928656: it died in the bodies pass, was stamped 'failed' with the
    position it had reached, and its successor restarted at id 0 — 765 s re-walking the oldest
    rows. The cursor advances only past a batch whose transaction closed."""
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])

    _run(conn, limit=10)
    conn.batches[-1]["outcome"] = "failed"       # as the run() wrapper stamps a crash
    conn.seen.clear()

    after = _run(conn, limit=10)
    assert after["resumed_from_id"] == 10
    assert conn.seen == list(range(11, 21)), "carries on, never re-walks the prefix"


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


# ------------------------------------------------------------------ incremental mode


def test_the_incremental_scan_opens_only_what_changed_not_what_was_re_sighted():
    """THE W1-a2 REGRESSION. Every active listing is re-sighted within hours by the index
    walks, so the `last_seen_at` watermark opened ~180 000 listings an hour to re-mine
    payloads whose claims already existed. Moving every listing's sighting forward must now
    cost the lane nothing at all; appending three snapshots must cost it three listings."""
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])
    seed = _run(conn, mode="incremental")
    assert seed["outcome"] == "ok" and conn.seen == []

    conn.now += timedelta(hours=1)
    for row in conn.listings:
        row.last_seen_at = conn.now

    quiet = _run(conn, mode="incremental")
    assert quiet["outcome"] == "ok"
    assert conn.seen == [], "a re-sighting is not a change"

    conn.change([4, 17, 4])
    busy = _run(conn, mode="incremental")
    assert busy["outcome"] == "ok"
    # Listing 4 changed twice in one window and is opened ONCE.
    assert conn.seen == [4, 17]


def test_the_incremental_cursor_survives_a_completed_run():
    """It is a position in an append-only log, not a coverage claim. A run that dropped it
    on 'ok' would re-walk the whole log every hour — which is the cost W1-a2 deleted."""
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])
    _run(conn, mode="incremental")          # seeds the cursor at the head of the log
    conn.change([1, 2, 3])

    first = _run(conn, mode="incremental")
    assert first["outcome"] == "ok"
    assert conn.seen == [1, 2, 3]
    assert conn.batches[-1]["cursor_after_id"] == 3

    conn.seen.clear()
    conn.change([9])
    second = _run(conn, mode="incremental")
    assert second["resumed_from_id"] == 3
    assert conn.seen == [9], "only the snapshots above the cursor"


def test_a_budget_stopped_incremental_run_resumes_at_the_snapshot_it_reached():
    """The cursor advances by SNAPSHOT, so a window cut short by a budget leaves the
    remaining snapshots — not the remaining listings — for the next run."""
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])
    _run(conn, mode="incremental")          # seeds the cursor at the head of the log
    conn.change(list(range(1, 21)))
    conn.seen.clear()

    stopped = _run(conn, mode="incremental", batch_size=10, limit=10)
    assert stopped["outcome"] == "stopped"
    assert conn.seen == list(range(1, 11))
    assert conn.batches[-1]["cursor_after_id"] == 10

    resumed = _run(conn, mode="incremental", batch_size=10)
    assert resumed["outcome"] == "ok"
    # 20 changes, each opened exactly once across the two runs.
    assert conn.seen == list(range(1, 21))


def test_a_cold_incremental_lane_seeds_at_the_head_of_the_log():
    """Not at 0. Every listing's payload has already been mined many times over by the
    watermark-driven lane this replaces, so re-walking the whole log would re-insert claims
    that exist; `--mode full` is the exhaustive pass."""
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])
    conn.change([1, 2, 3, 4, 5])

    first = _run(conn, mode="incremental")
    assert first["resumed_from_id"] == 5
    assert conn.seen == []
    assert first["outcome"] == "ok"


def test_a_pre_w1a2_incremental_cursor_is_never_read_as_a_snapshot_id():
    """The epoch guard. The old cursor was `(last_seen_at, id)` and always carried a
    timestamp; reading its LISTING id back as a SNAPSHOT id would skip every snapshot below
    it — silently, and permanently for anything that never changes again."""
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])
    conn.change([7, 8])
    conn.batches.append({
        "id": 1, "started_at": BASE_TS, "source": "sreality",
        "scan_mode": "incremental", "resumable": True, "outcome": "stopped",
        "cursor_after_id": 500_000, "cursor_after_ts": BASE_TS,
    })

    stats = _run(conn, mode="incremental")
    # Seeded from that row's TIMESTAMP (the cutover anchor, minus the old 3-hour overlap),
    # never from its listing id: below every snapshot in the log, so both changes are
    # re-walked rather than skipped.
    assert stats["resumed_from_id"] == 0
    assert conn.seen == [7, 8]


def test_the_cutover_anchor_prefers_the_old_lane_s_stopped_cursor_over_its_ok_watermark():
    """The old lane's runs were being CANCELLED at the job timeout: 12 `stopped` rows and no
    `ok` in three days, so the ok watermark is stale by days and re-walking from it would be
    heavy. The newest `cursor_after_ts` is where the old lane actually got to."""
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])
    stale_ok = BASE_TS - timedelta(days=3)
    conn.batches.append({
        "id": 1, "started_at": stale_ok, "source": "sreality", "scan_mode": "incremental",
        "resumable": True, "outcome": "ok", "cursor_after_id": None,
        "cursor_after_ts": None, "coverage_since": stale_ok,
    })
    conn.batches.append({
        "id": 2, "started_at": BASE_TS - timedelta(hours=1), "source": "sreality",
        "scan_mode": "incremental", "resumable": True, "outcome": "stopped",
        "cursor_after_id": 500_000, "cursor_after_ts": BASE_TS - timedelta(hours=4),
    })
    # Either side of the stopped cursor's anchor: 4h ago MINUS the 3h overlap = 7h ago.
    conn.change([3], age_minutes=480)      # 8h ago: below the anchor, already mined
    conn.change([4], age_minutes=100)      # 1h40 ago: above it, must be re-walked

    stats = _run(conn, mode="incremental")

    assert stats["resumed_from_id"] == 1, "the id of the snapshot at/below the anchor"
    assert conn.seen == [4]


def test_a_change_still_inside_the_lag_window_is_left_for_the_next_run():
    """THE RACE the lag closes. `listing_snapshots.id` is a bigserial — allocated at INSERT,
    visible at COMMIT — and `write_detail_batch` writes N of them in one transaction
    concurrently across the drains, so a row with an id BELOW an advanced cursor can appear
    after the cursor moved. `s.id > after_id` never looks back, so that change would be
    skipped permanently. Standing 15 minutes back costs one run of latency instead."""
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])
    _run(conn, mode="incremental")              # seeds
    conn.change([11], age_minutes=2)            # still in flight
    conn.seen.clear()

    assert _run(conn, mode="incremental")["outcome"] == "ok"
    assert conn.seen == [], "inside the lag window"

    conn.now += timedelta(minutes=30)
    assert _run(conn, mode="incremental")["outcome"] == "ok"
    assert conn.seen == [11], "and picked up whole on the next run"


def test_a_full_cursor_is_never_resumed_by_an_incremental_scan():
    """The two cursors are different keysets — `listings.id` vs `listing_snapshots.id`. A
    full cursor is only ever read back by a FULL phase (W1-a7's continuation included), never
    as a position in the snapshot log."""
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])
    conn.change([3])

    stopped_full = _run(conn, mode="full", limit=5)
    assert stopped_full["outcome"] == "stopped"
    assert conn.batches[-1]["cursor_after_id"] == 5

    conn.seen.clear()
    incremental = _run(conn, mode="incremental")
    # It seeded from the snapshot log's own head, not from the full scan's listing id 5.
    assert incremental["mode"] == "incremental"
    assert incremental["resumed_from_id"] == 1
    # …and then carried the stopped FULL walk on from 5, in its own phase (W1-a7 below).
    assert incremental["full_walk_continued"] is True
    assert conn.seen == list(range(6, 26))


# ------------------------------------- an incremental run continues a stopped full walk


def test_an_incremental_run_continues_a_stopped_full_walk_in_a_full_batch_row():
    """W1-a7. The chain re-dispatches a run with its OWN mode and the hourly cron is
    `incremental`, so when a hop yields to a waiting cron run (13:06Z, 2026-09-12) the
    successor inherits `incremental` — and the stopped full walk (a contract-bump re-walk,
    80 000 of ~376k served listings) falls off the chain until somebody dispatches `full` by
    hand. An incremental run that has emptied the change log has budget and nothing to spend
    it on. The continuation is a `full` PHASE with its own batch row: `_resume_point` keys on
    `scan_mode`, so a continued walk stamped `incremental` would be invisible to the next
    resume, and the two cursors are different keysets that must never share a column."""
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])

    stopped = _run(conn, mode="full", limit=10)
    assert stopped["outcome"] == "stopped" and conn.batches[-1]["cursor_after_id"] == 10
    conn.seen.clear()

    carried = _run(conn, mode="incremental", batch_size=10)

    assert carried["full_walk_continued"] is True
    assert carried["full_walk_cursor"] == 10
    assert conn.seen == list(range(11, 26)), "it carried on, it did not restart"
    # The continued walk's own end is what the summary reports — the chain's "work left"
    # decision reads exactly this field.
    assert carried["reached_end"] is True
    incremental_row, full_row = conn.batches[-2], conn.batches[-1]
    assert incremental_row["scan_mode"] == "incremental"
    assert incremental_row["outcome"] == "ok" and incremental_row["cursor_after_id"] == 0
    assert full_row["scan_mode"] == "full" and full_row["resumable"] is True
    assert full_row["outcome"] == "ok" and full_row["cursor_after_id"] == 25


def test_an_incremental_run_never_starts_a_full_walk_of_its_own():
    """It CONTINUES, it never STARTS. `--mode full` keeps its one meaning: no stopped or
    failed full cursor means there is nothing to continue, and a `full` run that finished
    left `ok` — the next full pass is the contract-bump re-walk, and an hourly incremental
    run is not the thing that should decide to open one."""
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])
    for _ in range(3):                      # walk the table to completion: 'ok', no cursor
        _run(conn, mode="full", limit=10)
    assert conn.batches[-1]["outcome"] == "ok"
    conn.seen.clear()
    batches_before = len(conn.batches)

    quiet = _run(conn, mode="incremental")

    assert quiet["full_walk_continued"] is False and quiet["full_walk_cursor"] is None
    assert quiet["reached_end"] is True
    assert conn.seen == []
    assert len(conn.batches) == batches_before + 1, "one row, no full row opened"


def test_a_run_with_no_room_for_another_batch_hands_off_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget is ONE budget and the bodies half spends its share first, so the handoff
    asks the same question every batch start asks: is there room for ANOTHER batch. Opening
    a full batch row with no budget behind it would stamp a position it never walked to and
    cost the run a bookkeeping write for nothing."""
    conn = _Conn([_Listing(i, BASE_TS) for i in range(1, 26)])
    _run(conn, mode="full", limit=10)
    conn.seen.clear()
    batches_before = len(conn.batches)
    # Each clock reading 40 s later than the last: the first payload batch starts, finds the
    # log empty, and by the handoff check the 100 s budget is spent.
    clock = iter(range(0, 10_000, 40))
    monkeypatch.setattr(claims_intake.time, "monotonic", lambda: float(next(clock)))

    stats = _run(conn, mode="incremental", max_seconds=100.0)

    assert stats["full_walk_continued"] is False
    assert stats["reached_end"] is True
    assert conn.seen == []
    assert len(conn.batches) == batches_before + 1
