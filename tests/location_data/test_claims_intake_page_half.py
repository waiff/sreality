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
    """One scan row in the three selections' column order."""
    return (
        listing_id, "sreality", f"n{listing_id}", dict(SREALITY_POST_CUTOVER),
        BASE_TS + timedelta(minutes=listing_id),
        body_id, unmined, "detail" if body_id else None,
        "ab" * 32 if body_id else None, BASE_TS if body_id else None, ACTIVE_VERSION,
        None,
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

    def __init__(self, rows: list[tuple[int, int | None, bool]],
                 unmined_bodies: list[tuple[int, int | None, bool]] | None = None,
                 orphans: set[int] | None = None) -> None:
        self.rows = rows
        # What the bodies-first pass finds. A stamp removes a row from it, exactly as the
        # `contract_version IS DISTINCT FROM` predicate does in production.
        self.unmined_bodies = list(unmined_bodies or ())
        # Payload ids the WINDOW names and the outer statement's joins then drop — an
        # inactive listing, or a body some later row has superseded. They never reach the
        # drain and they are not in the backlog, but the cursor must still walk past them.
        self.orphans = set(orphans or ())
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.stamped: list[dict[str, Any]] = []
        self.claim_writes: list[list[dict[str, Any]]] = []
        self.body_selections: list[int] = []
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
            cur._result = [(7,)]
            return
        if "coalesce(max(id), 0) FROM listing_snapshots" in sql:
            cur._result = [(0,)]
            return
        if "SELECT outcome, cursor_after_id" in sql:
            return
        if sql.startswith("SELECT count(*) FROM portal_raw_payloads p"):
            # The readout carries the joins too, so an orphan is not in the backlog.
            cur._result = [(len([r for r in self.unmined_bodies
                                 if r[1] not in self.orphans]),)]
            return
        if "FROM portal_raw_payloads p" in sql and "%(cap)s" in sql:
            # THE WINDOW: the in-run keyset over payload ids, then the cap. It knows
            # nothing about `listings`, so an orphan is in it like any other row.
            # A stamped row leaves `unmined_bodies` exactly as `contract_version` moves it
            # out of the predicate in production; an unstamped one stays, and only the
            # keyset keeps the pass off it.
            rows = [r for r in self.unmined_bodies
                    if r[1] is not None and r[1] > params["after_body_id"]]
            cur._result = [(r[1],) for r in rows[:params["cap"]]]
            self.body_selections.append(len(cur._result))
            return
        if sql.startswith("SELECT l.id") and "%(ids)s::bigint[]" in sql:
            # The outer statement: the joins, over the window's ids ONLY.
            by_id = {r[1]: r for r in self.unmined_bodies}
            cur._result = [_record(*by_id[i]) for i in params["ids"]
                           if i in by_id and i not in self.orphans]
            return
        if "cursor_after_ts IS NOT NULL" in sql:
            cur._result = [(None,)]
            return
        if "FROM listings l" in sql or "FROM listing_snapshots s" in sql:
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
            stamped = {row["id"] for row in params["rows"].obj}
            self.unmined_bodies = [r for r in self.unmined_bodies if r[1] not in stamped]
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


def _run(conn: _Conn, store: Any, **kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "mode": "full", "source": "sreality", "batch_size": 10, "max_seconds": None,
        "limit": None, "start_after_id": 0, "statement_timeout": 60,
        "dry_run": False, "note": None, "store": store,
    }
    defaults.update(kwargs)
    return claims_intake.run(conn, **defaults)


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


def test_a_worker_that_timed_out_costs_one_body_and_commits_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fourth way a page can fail, introduced with the pool (W1-a3): the extractor never
    came back for this body. Sorted with the bucket miss, not with the lane bugs — the body
    is immutable, so failing the batch would fail it again every hour, while this way the
    batch commits and the unstamped body is asked for again."""
    def _hang(*_a: Any, **_k: Any) -> IntakeResult:
        raise TimeoutError("a worker did not return body 1/1 within 120s")

    monkeypatch.setattr(page_readers, "extract_page", _hang)
    conn = _Conn([UNMINED, ALREADY_MINED, NO_BODY])

    stats = _run(conn, _Store())

    assert conn.stamped == []
    assert stats["outcome"] == "ok"
    assert stats["refusal_reasons"]["page_extract_timeout:sreality"] == 1
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


def test_an_unclassified_exception_still_fails_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line between the three isolated failures and a lane BUG, now that extraction
    hands its failures back as values (W1-a3). `IntakeRefused` is the readers' word for "the
    body said something I may not write"; anything else is a defect in this lane, and mining
    a corpus minus the rows nobody looked at would hide it behind a green run."""
    def _bug(*_a: Any, **_k: Any) -> IntakeResult:
        raise ValueError("a reader returned a tuple")

    monkeypatch.setattr(page_readers, "extract_page", _bug)
    conn = _Conn([UNMINED, ALREADY_MINED, NO_BODY])

    with pytest.raises(ValueError, match="returned a tuple"):
        _run(conn, _Store())

    assert conn.stamped == []
    failed = [params for sql, params in conn.executed
              if sql.startswith("UPDATE location_claim_batches")]
    assert failed and failed[-1]["outcome"] == "failed"


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
        limit=None, start_after_id=0, statement_timeout=60,
        dry_run=False, note=None)

    assert stats["bodies_eligible"] == 0 and conn.stamped == []
    written = [row["listing_id"] for chunk in conn.claim_writes for row in chunk]
    assert set(written) == {1, 2, 3}
    # AND IT REPORTS A COMPLETE PASS. There is no store to read a backlog from, so there is
    # no backlog this run can claim to be behind on — see the chain rail below.
    assert stats["bodies_pass_complete"] is True


@pytest.mark.parametrize("page_sources,source", [
    (set(), None),          # no portal's contract declares a page entry at all
    ({"remax"}, "sreality"),  # --source names a portal with no page entry of its own
])
def test_a_pass_that_could_never_run_reports_itself_complete(
    page_sources: set[str], source: str | None,
) -> None:
    """`bodies_pass_complete` starts False and is otherwise only set inside the drain loop,
    so both of the pass's early returns used to leave a run reporting a backlog it had never
    looked at. The workflow's chain reads that field: a `--source sreality` run (sreality's
    contract is API-JSON and declares no page entry) would have dispatched a successor, which
    would have reported the same, for ever — each hop holding `location-batch` on its way
    through. Nothing could be drained IS the pass reaching its end."""
    stats = {"bodies_pass_complete": False}

    claims_intake.drain_unmined_bodies(
        None, source=source, page_sources=page_sources, entries_by_source={},
        registers={}, store=_Store(), cap=1500, statement_timeout=60,
        budget=claims_intake._Budget(None), batch_id=1, dry_run=False,
        max_value_bytes=1024, stats=stats, refusals={})

    assert stats["bodies_pass_complete"] is True


# ------------------------------------------------------- the bodies-first backlog pass

BACKLOG = [(10 + i, 200 + i, True) for i in range(7)]


@pytest.fixture
def small_body_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three bodies a batch instead of 1 500, so a seven-row backlog takes three of them."""
    monkeypatch.setenv(claims_intake.BODY_FETCH_CAP_ENV, "3")


def test_the_bodies_first_pass_drains_the_backlog_before_the_listing_scan(
    monkeypatch: pytest.MonkeyPatch, small_body_cap: None,
) -> None:
    """W1-a2's point. The backlog is bounded by the CORPUS (~250 000 unmined bodies after
    the first wave), the listing scan by the hour's change — so riding the bodies on the
    scan capped the drain at one `body_cap` per 20 000-row batch and would have taken ~170
    runs. The pass asks for them directly, and the STAMP is its only cursor: each batch
    re-asks the same question and gets the remainder."""
    monkeypatch.setattr(page_readers, "extract_page", _page_claim)
    conn = _Conn([NO_BODY], unmined_bodies=BACKLOG)
    store = _Store()

    stats = _run(conn, store)

    assert conn.body_selections == [3, 3, 1], "three-row batches until one comes back short"
    assert [s["id"] for s in conn.stamped] == [b[1] for b in BACKLOG]
    assert stats["bodies_mined"] == 7 and stats["bodies_batches"] == 3
    assert stats["bodies_pass_complete"] is True
    assert stats["bodies_backlog_remaining"] == 0
    assert stats["outcome"] == "ok"


def test_bodies_the_pass_can_never_stamp_do_not_stall_the_backlog_behind_them(
    monkeypatch: pytest.MonkeyPatch, small_body_cap: None,
) -> None:
    """B2, the defect this keyset exists for. The three lowest payload ids are ungettable —
    a bucket 404, a scoper that fails closed and a content refusal all land here — and with
    no cursor they would be re-selected at the head of every batch, stamp nothing once `cap`
    of them accumulate, and stall the ENTIRE backlog silently. The keyset walks past them:
    the other four drain in the SAME run, and the poison costs one re-fetch per run."""
    monkeypatch.setattr(page_readers, "extract_page", _page_claim)
    conn = _Conn([NO_BODY], unmined_bodies=BACKLOG)
    poisoned = {_key(200), _key(201), _key(202)}
    store = _Store(failing=poisoned)

    stats = _run(conn, store)

    assert conn.body_selections == [3, 3, 1]
    assert [s["id"] for s in conn.stamped] == [203, 204, 205, 206]
    assert stats["bodies_mined"] == 4
    assert stats["bodies_pass_complete"] is True
    # The three stay in the backlog and are asked for again NEXT run, not next batch.
    assert stats["bodies_backlog_remaining"] == 3
    assert sorted(store.asked) == sorted(_key(200 + i) for i in range(7))


def test_the_bodies_first_pass_stops_at_half_the_run_budget(
    monkeypatch: pytest.MonkeyPatch, small_body_cap: None,
) -> None:
    """The payload half is never starved. A batch must not START unless the last one's
    measured duration fits in what is left of the HALF budget — the clock is checked
    against the next batch's expected cost, not merely against zero."""
    monkeypatch.setattr(page_readers, "extract_page", _page_claim)
    clock = iter(range(0, 10_000, 40))          # every reading 40 s later than the last
    monkeypatch.setattr(claims_intake.time, "monotonic", lambda: float(next(clock)))
    conn = _Conn([NO_BODY], unmined_bodies=BACKLOG)

    stats = _run(conn, _Store(), max_seconds=240.0)

    assert stats["bodies_pass_complete"] is False
    assert stats["bodies_mined"] < len(BACKLOG), "it stopped inside the backlog"
    assert stats["bodies_batches"] >= 1
    # Whatever it mined IS stamped: the half budget ends the pass, it never rolls it back.
    assert len(conn.stamped) == stats["bodies_mined"]


def test_a_pass_that_stamps_nothing_still_walks_its_keyset_to_the_end(
    monkeypatch: pytest.MonkeyPatch, small_body_cap: None,
) -> None:
    """Every GET fails. The pass must still reach the end of the keyset — its cursor is the
    payload id, not the stamp — so the run ends on the same terminal condition as a healthy
    one and the operator reads a backlog of 7 rather than a lane that quietly stopped."""
    monkeypatch.setattr(page_readers, "extract_page", _page_claim)
    conn = _Conn([NO_BODY], unmined_bodies=BACKLOG)
    store = _Store(failing={_key(200 + i) for i in range(7)})

    stats = _run(conn, store)

    assert conn.body_selections == [3, 3, 1]
    assert conn.stamped == []
    assert stats["bodies_mined"] == 0
    assert stats["bodies_pass_complete"] is True
    assert stats["bodies_backlog_remaining"] == 7


def test_a_window_the_joins_empty_still_moves_the_cursor(
    monkeypatch: pytest.MonkeyPatch, small_body_cap: None,
) -> None:
    """W1-a4's cursor rule. The WINDOW is planned off `portal_raw_payloads` alone, so it
    names rows the outer statement's joins then drop — an inactive listing, a superseded
    body — and roughly two thirds of a production window are exactly that. If the cursor
    advanced on the SURVIVORS, a window that lost every row would leave `after_body_id`
    where it was and the next batch would ask the identical question: the pass would spin on
    the same three ids until the budget ended it, having mined nothing."""
    monkeypatch.setattr(page_readers, "extract_page", _page_claim)
    conn = _Conn([NO_BODY], unmined_bodies=BACKLOG, orphans={200, 201, 202})
    store = _Store()

    stats = _run(conn, store)

    # Three windows, the same sizes a healthy run measures: the first joined to nothing and
    # the walk carried on regardless.
    assert conn.body_selections == [3, 3, 1]
    assert [s["id"] for s in conn.stamped] == [203, 204, 205, 206]
    assert stats["bodies_mined"] == 4
    assert stats["bodies_pass_complete"] is True
    # An orphan is never fetched from R2 — it never reached the drain at all — and it is
    # not in the backlog either, since the readout carries the same joins.
    assert sorted(store.asked) == [_key(203 + i) for i in range(4)]
    assert stats["bodies_backlog_remaining"] == 0


def test_a_window_short_of_the_cap_ends_the_pass(
    monkeypatch: pytest.MonkeyPatch, small_body_cap: None,
) -> None:
    """THE WINDOW ends the pass, never the survivors. `cap` is a bound on the keyset slice,
    so a window shorter than `cap` means the payload table has no eligible row left above
    the cursor — whereas a FULL window that joined to nothing means the opposite, that the
    walk has more to do. Reading the end off the joined rows would stop the pass on the
    first all-orphan window and leave the backlog untouched behind it."""
    monkeypatch.setattr(page_readers, "extract_page", _page_claim)
    conn = _Conn([NO_BODY], unmined_bodies=BACKLOG[:2], orphans={200, 201})

    stats = _run(conn, _Store())

    assert conn.body_selections == [2], "one window, short of the cap of 3"
    assert conn.stamped == [] and stats["bodies_mined"] == 0
    assert stats["bodies_pass_complete"] is True
    assert stats["bodies_backlog_remaining"] == 0


def test_the_bodies_first_pass_is_skipped_without_an_object_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same rule as the rest of the page half: a rotated credential costs the bodies, never
    the hourly payload ingest for all nine portals."""
    monkeypatch.setattr(page_readers, "extract_page", _page_claim)
    monkeypatch.setattr(claims_intake, "_open_body_store", lambda page_capable: None)
    conn = _Conn([UNMINED, NO_BODY], unmined_bodies=BACKLOG)

    stats = claims_intake.run(
        conn, mode="full", source="sreality", batch_size=10, max_seconds=None,
        limit=None, start_after_id=0, statement_timeout=60, dry_run=False, note=None)

    assert conn.body_selections == [] and conn.stamped == []
    assert stats["bodies_backlog_remaining"] is None
    written = [row["listing_id"] for chunk in conn.claim_writes for row in chunk]
    assert set(written) == {1, 3}


def test_a_dry_run_takes_one_bodies_batch_and_stops(
    monkeypatch: pytest.MonkeyPatch, small_body_cap: None,
) -> None:
    """A dry run is a shape check, not a drain: it writes no claims, so spending the bucket
    on the rest of the backlog would buy nothing."""
    monkeypatch.setattr(page_readers, "extract_page", _page_claim)
    conn = _Conn([NO_BODY], unmined_bodies=BACKLOG)

    stats = _run(conn, _Store(), dry_run=True)

    assert conn.body_selections == [3]
    assert conn.stamped == [] and conn.claim_writes == []
    assert stats["bodies_batches"] == 1
