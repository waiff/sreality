"""Hermetic tests for select_failed_runs() in record_workflow_failures.

No DB, no network — psycopg import lives inside main(), so importing the
filter is clean.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from scripts.record_workflow_failures import (
    CANCELLED_MIN_DURATION_MINUTES,
    MAX_PAGES,
    MONITOR_WORKFLOW_NAME,
    PER_PAGE,
    _advance_cursor,
    _read_cursor,
    _write_cursor,
    parse_ts,
    select_failed_runs,
    select_latest_successes,
)

SINCE = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


def _run(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 101,
        "name": "Scraping: Sreality index walk",
        "path": ".github/workflows/index_walk.yml",
        "conclusion": "failure",
        "updated_at": "2026-06-11T12:30:00Z",
        "run_started_at": "2026-06-11T12:05:00Z",
        "html_url": "https://github.com/waiff/sreality/actions/runs/101",
    }
    base.update(overrides)
    return base


def test_keeps_only_alerting_conclusions():
    runs = [
        _run(id=1, conclusion="success"),
        _run(id=2, conclusion="failure"),
        # a QUICK cancel (1 min) — a supersession, not a failure → excluded
        _run(id=3, conclusion="cancelled", run_started_at="2026-06-11T12:29:00Z"),
        _run(id=4, conclusion="timed_out"),
        _run(id=5, conclusion="startup_failure"),
        _run(id=6, conclusion="skipped"),
        _run(id=7, conclusion=None),
    ]
    kept = select_failed_runs(runs, since=SINCE)
    assert [r["run_id"] for r in kept] == [2, 4, 5]


def test_cancelled_long_but_never_started_is_skipped():
    """A superseded run that sat QUEUED >= 8 min looks like a timeout kill by duration
    (run_started_at = queue entry), but its jobs never started — with started_check
    provided, it must be skipped; a genuinely started long cancel is kept. Without
    started_check (None), duration-only behavior is preserved."""
    long_cancel = _run(id=1, conclusion="cancelled",
                       run_started_at="2026-06-11T12:00:00Z",
                       updated_at="2026-06-11T12:20:00Z")
    # never started -> skipped
    assert select_failed_runs([long_cancel], since=SINCE,
                              started_check=lambda rid: False) == []
    # actually ran -> kept
    assert [r["run_id"] for r in select_failed_runs(
        [long_cancel], since=SINCE, started_check=lambda rid: True)] == [1]
    # no checker (pure mode) -> kept, as before
    assert [r["run_id"] for r in select_failed_runs([long_cancel], since=SINCE)] == [1]
    # the checker is NOT consulted for plain failures (would raise if called)
    def _boom(rid):
        raise AssertionError("started_check must not run for conclusion=failure")
    assert [r["run_id"] for r in select_failed_runs(
        [_run(id=2, conclusion="failure")], since=SINCE, started_check=_boom)] == [2]


def test_cancelled_kept_only_when_it_ran_long_enough():
    long_min = CANCELLED_MIN_DURATION_MINUTES + 5
    runs = [
        # timeout-minutes kill: ran to its budget → kept
        _run(id=1, conclusion="cancelled",
             run_started_at="2026-06-11T12:00:00Z",
             updated_at=f"2026-06-11T12:{long_min:02d}:00Z"),
        # cancel-in-progress supersession: killed in 2 min → dropped
        _run(id=2, conclusion="cancelled",
             run_started_at="2026-06-11T12:28:00Z", updated_at="2026-06-11T12:30:00Z"),
        # cancelled with no start time → can't judge → dropped (conservative)
        _run(id=3, conclusion="cancelled", run_started_at=None),
    ]
    assert [r["run_id"] for r in select_failed_runs(runs, since=SINCE)] == [1]


def test_excludes_the_monitor_itself():
    runs = [
        _run(id=1, name=MONITOR_WORKFLOW_NAME),
        _run(id=2, name="Monitoring: LLM pipeline liveness"),
    ]
    kept = select_failed_runs(runs, since=SINCE)
    assert [r["run_id"] for r in kept] == [2]


def test_drops_runs_completed_before_the_window():
    runs = [
        _run(id=1, updated_at="2026-06-11T11:59:59Z"),
        _run(id=2, updated_at="2026-06-11T12:00:00Z"),  # boundary: kept
        _run(id=3, updated_at=None),
        _run(id=4, updated_at="not-a-timestamp"),
    ]
    kept = select_failed_runs(runs, since=SINCE)
    assert [r["run_id"] for r in kept] == [2]


def test_row_shape_and_timestamp_parsing():
    (row,) = select_failed_runs([_run()], since=SINCE)
    assert row == {
        "run_id": 101,
        "workflow_name": "Scraping: Sreality index walk",
        "workflow_path": ".github/workflows/index_walk.yml",
        "conclusion": "failure",
        "run_started_at": datetime(2026, 6, 11, 12, 5, tzinfo=timezone.utc),
        "html_url": "https://github.com/waiff/sreality/actions/runs/101",
    }


def test_tolerates_missing_optional_fields():
    (row,) = select_failed_runs(
        [_run(name=None, path=None, run_started_at=None, html_url=None)], since=SINCE,
    )
    assert row["workflow_name"] == "(unnamed)"
    assert row["workflow_path"] is None
    assert row["run_started_at"] is None
    assert row["html_url"] is None


# --- select_latest_successes -----------------------------------------------


def test_latest_success_picks_newest_per_path():
    runs = [
        _run(id=1, conclusion="success", path="a.yml", run_started_at="2026-06-11T10:00:00Z"),
        _run(id=2, conclusion="success", path="a.yml", run_started_at="2026-06-11T12:00:00Z"),
        _run(id=3, conclusion="success", path="b.yml", run_started_at="2026-06-11T09:00:00Z"),
    ]
    by_path = {s["workflow_path"]: s for s in select_latest_successes(runs)}
    assert by_path["a.yml"]["last_success_run_id"] == 2  # newer wins
    assert by_path["a.yml"]["last_success_at"] == datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    assert by_path["b.yml"]["last_success_run_id"] == 3


def test_latest_success_ignores_non_success_and_pathless():
    runs = [
        _run(id=1, conclusion="failure", path="a.yml"),
        _run(id=2, conclusion="cancelled", path="a.yml"),
        _run(id=3, conclusion="success", path=None),
    ]
    assert select_latest_successes(runs) == []


def test_parse_ts_handles_z_suffix_and_garbage():
    assert parse_ts("2026-06-11T12:00:00Z") == SINCE
    assert parse_ts(None) is None
    assert parse_ts("") is None
    assert parse_ts("garbage") is None


# --- high-water-mark cursor -------------------------------------------------


class _FakeCursor:
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store
        self._row: Any = None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        if "SELECT value" in sql:
            self._row = (self._store.get("val"),)
        elif "INSERT INTO app_settings" in sql:
            self._store["val"] = params[1]  # the ISO timestamp string

    def fetchone(self) -> Any:
        return self._row


class _FakeConn:
    def __init__(self) -> None:
        self.store: dict[str, Any] = {}

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.store)


def test_cursor_read_write_roundtrip():
    conn = _FakeConn()
    assert _read_cursor(conn) is None  # unset on first ever run
    ts = datetime(2026, 7, 9, 21, 50, tzinfo=timezone.utc)
    _write_cursor(conn, ts)
    assert _read_cursor(conn) == ts


# --- W0.1: the high-water mark only advances over a window it actually covered


def test_cursor_jumps_to_newest_when_the_window_was_fully_covered():
    completions = [SINCE, SINCE + timedelta(minutes=30), SINCE + timedelta(minutes=90)]
    assert _advance_cursor(completions, reached_since=True) == SINCE + timedelta(minutes=90)


def test_cursor_is_held_when_the_page_cap_was_hit_first():
    """The permanent-loss bug. Pages come back newest-first, so hitting the cap means
    every completion seen is NEWER than `since` and the runs that were skipped are older
    than all of them. The old code advanced to min(completions) — believing it was
    "crawling to oldest-seen" — and the next poll's `completed_at < since` filter then
    dropped the skipped runs forever. Only 2 of the 6 portals that failed on 2026-08-26
    were ever recorded."""
    newest = SINCE + timedelta(minutes=200)
    oldest_seen = SINCE + timedelta(minutes=120)
    completions = [oldest_seen, newest]
    # The dangerous answer is anything that moves the mark past `since`.
    assert _advance_cursor(completions, reached_since=False) is None


def test_cursor_untouched_when_nothing_completed():
    assert _advance_cursor([], reached_since=True) is None
    assert _advance_cursor([], reached_since=False) is None


def test_page_budget_covers_the_worst_observed_inter_poll_gap():
    """The cron is */30 but the Actions throttle really runs it 80-256 min apart, so the
    budget has to hold a 256-minute window's worth of completed runs."""
    assert MAX_PAGES * PER_PAGE >= 1000


# --- W3.1 backstop: reading WHY a run went red ----------------------------

import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

from scripts import record_workflow_failures as rwf  # noqa: E402


class _Resp:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._body if n < 0 else self._body[:n]

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *_: Any) -> None:
        return None


def _job(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 555, "name": "drain", "conclusion": "failure",
        "steps": [{"name": "Checkout", "conclusion": "success"},
                  {"name": "Run drain", "conclusion": "failure"}],
    }
    base.update(kw)
    return base


def test_select_failed_job_names_the_step_that_broke():
    got = rwf.select_failed_job([_job(id=1, conclusion="success"), _job(id=2)])
    assert got == {"job_id": 2, "job_name": "drain",
                   "step_name": "Run drain", "exit_code": "failure"}


def test_select_failed_job_distinguishes_a_timeout_kill():
    """The Actions API never exposes a process exit code; the step conclusion is what
    separates a timeout kill from a plain non-zero exit in the fallback key."""
    got = rwf.select_failed_job([_job(conclusion="cancelled", steps=[
        {"name": "Run drain", "conclusion": "cancelled"}])])
    assert got["exit_code"] == "cancelled"


def test_select_failed_job_returns_none_when_every_job_passed():
    assert rwf.select_failed_job([_job(conclusion="success")]) is None


def test_log_fetch_never_forwards_the_token_to_the_signed_blob(monkeypatch):
    """CPython's redirect handler copies EVERY header onto the redirect, Authorization
    included. That leaks GITHUB_TOKEN to a third-party host AND does not work — Azure
    answers 401 when the bearer header is present."""
    signed = "https://productionresultssa15.blob.core.windows.net/x?sig=abc"

    class _Opener:
        def open(self, req: Any, timeout: int = 0) -> Any:
            assert req.get_header("Authorization") is not None   # the API call is authed
            raise urllib.error.HTTPError(
                req.full_url, 302, "Found", {"Location": signed}, None)

    seen: list[tuple[str, dict[str, str]]] = []

    def _bare(req: Any, timeout: int = 0) -> Any:
        seen.append((req.full_url, dict(req.header_items())))
        rng = req.get_header("Range")
        if rng == "bytes=0-0":
            return _Resp(b"x", {"Content-Range": "bytes 0-0/27247"})
        return _Resp(b"##[error]boom")

    monkeypatch.setattr(rwf, "_NO_REDIRECT_OPENER", _Opener())
    monkeypatch.setattr(urllib.request, "urlopen", _bare)

    out = rwf.fetch_job_log("waiff/sreality", "ghs_secret", 555, cap_bytes=1024)
    assert out == "##[error]boom"
    assert [u for u, _h in seen] == [signed, signed]
    for _u, headers in seen:
        assert not any(k.lower() == "authorization" for k in headers)
        assert "ghs_secret" not in str(headers)


def test_log_fetch_asks_for_a_closed_range_not_a_suffix_range(monkeypatch):
    """Azure IGNORES suffix ranges: `Range: bytes=-500` came back 200 with the whole
    27KB body. The length has to be learned first."""
    ranges: list[str] = []

    class _Opener:
        def open(self, req: Any, timeout: int = 0) -> Any:
            raise urllib.error.HTTPError(
                req.full_url, 302, "Found", {"Location": "https://blob/x"}, None)

    def _bare(req: Any, timeout: int = 0) -> Any:
        ranges.append(req.get_header("Range"))
        if len(ranges) == 1:
            return _Resp(b"x", {"Content-Range": "bytes 0-0/27247"})
        return _Resp(b"tail")

    monkeypatch.setattr(rwf, "_NO_REDIRECT_OPENER", _Opener())
    monkeypatch.setattr(urllib.request, "urlopen", _bare)
    rwf.fetch_job_log("r", "t", 1, cap_bytes=1000)
    assert ranges == ["bytes=0-0", "bytes=26247-27246"]


def test_log_fetch_tolerates_an_evicted_blob(monkeypatch):
    """The signed URL 404s (BlobNotFound) on very fresh or very old runs; that is a
    degraded incident, never a poller failure."""
    class _Opener:
        def open(self, req: Any, timeout: int = 0) -> Any:
            raise urllib.error.HTTPError(
                req.full_url, 302, "Found", {"Location": "https://blob/gone"}, None)

    def _bare(req: Any, timeout: int = 0) -> Any:
        raise urllib.error.HTTPError("https://blob/gone", 404, "BlobNotFound", {}, None)

    monkeypatch.setattr(rwf, "_NO_REDIRECT_OPENER", _Opener())
    monkeypatch.setattr(urllib.request, "urlopen", _bare)
    assert rwf.fetch_job_log("r", "t", 1) is None


class _IncidentConn:
    """Stands in for toolkit.ops_incidents, which record_incidents imports lazily."""

    def cursor(self) -> Any:
        raise AssertionError("record_incidents must go through ops_incidents")


def _patch_incidents(monkeypatch, *, already_recorded: set[int] | None = None) -> list[dict]:
    from toolkit import ops_incidents as oi

    calls: list[dict] = []
    claimed = set(already_recorded or ())

    def _claim(_c: Any, run_id: int | None) -> bool:
        if run_id is None:
            return True
        if run_id in claimed:
            return False
        claimed.add(run_id)
        return True

    monkeypatch.setattr(oi, "claim_run", _claim)
    monkeypatch.setattr(oi, "excerpt_byte_budget", lambda _c: 4000)
    monkeypatch.setattr(oi, "record_failure_signature",
                        lambda _c, sig, **kw: calls.append({"sig": sig, **kw}))
    monkeypatch.setattr(oi, "auto_resolve", lambda _c, **_k: {"resolved_success": 0})
    return calls


def _failure(**kw: Any) -> dict[str, Any]:
    base = {"run_id": 900, "workflow_path": ".github/workflows/drain.yml",
            "conclusion": "failure", "html_url": "https://gh/run/900"}
    base.update(kw)
    return base


def test_record_incidents_extracts_the_reason_from_the_log(monkeypatch):
    calls = _patch_incidents(monkeypatch)
    monkeypatch.setattr(rwf, "_fetch_failed_job", lambda *_a: {"job_id": 5, "step_name": "s", "exit_code": "failure"})
    monkeypatch.setattr(
        rwf, "fetch_job_log",
        lambda *_a, **_k: "2026-08-26T20:15:03.1234567Z psycopg.errors.CheckViolation: "
                          'new row for relation "listings" violates check constraint '
                          '"listings_area_basis_check"\n')
    stats = rwf.record_incidents(_IncidentConn(), "r", "t", [_failure()])
    assert stats["fetched"] == 1 and stats["recorded"] == 1
    assert calls[0]["sig"].startswith("checkviolation|")
    assert "listings_area_basis_check" in calls[0]["sig"]
    assert calls[0]["workflow_path"] == ".github/workflows/drain.yml"


def test_a_run_the_chokepoint_already_recorded_is_neither_fetched_nor_counted(monkeypatch):
    """The double-count fix AND the API-budget saver, in one mechanism. The in-process
    producer records the crash at t+0 and the run then concludes `failure`; when the
    poller meets that same run 80-256 minutes later it must NOT bump failure_count a
    second time (a lone crash would cross the measured onset threshold on its own) and
    must not spend a job-log download re-deriving a reason already on file."""
    calls = _patch_incidents(monkeypatch, already_recorded={900})
    monkeypatch.setattr(rwf, "fetch_job_log", lambda *_a, **_k: _must_not_fetch())
    monkeypatch.setattr(rwf, "_fetch_failed_job", lambda *_a: _must_not_fetch())
    stats = rwf.record_incidents(_IncidentConn(), "r", "t", [_failure(run_id=900)])
    assert stats["already_recorded"] == 1 and stats["fetched"] == 0
    assert calls == []


def test_record_incidents_claims_the_run_so_it_cannot_be_counted_twice(monkeypatch):
    """Every recorded red carries its run id through to the incident write, with
    `run_claimed` set — the poller already won the claim above."""
    calls = _patch_incidents(monkeypatch)
    monkeypatch.setattr(rwf, "_fetch_failed_job", lambda *_a: None)
    monkeypatch.setattr(rwf, "fetch_job_log", lambda *_a, **_k: None)
    rwf.record_incidents(_IncidentConn(), "r", "t", [_failure(run_id=900)])
    assert calls[0]["run_id"] == 900 and calls[0]["run_claimed"] is True


def test_a_new_reason_is_never_folded_into_the_last_incident_for_that_workflow(monkeypatch):
    """The misattribution the workflow-keyed `known` map committed: index_walk.yml opens
    a CheckViolation incident at 10:00 and then TIMES OUT at 10:30 for an unrelated
    reason. Reusing the workflow's last signature recorded the timeout as a
    CheckViolation, kept the wrong sample_run_url, and meant the timeout's own signature
    was never derived — so it could never open an incident or alert."""
    calls = _patch_incidents(monkeypatch)
    monkeypatch.setattr(rwf, "_fetch_failed_job",
                        lambda *_a: {"job_id": 5, "step_name": "Run walk", "exit_code": "timed_out"})
    monkeypatch.setattr(rwf, "fetch_job_log", lambda *_a, **_k: "##[error]The job running on runner X has exceeded the maximum execution time\n")
    stats = rwf.record_incidents(
        _IncidentConn(), "r", "t",
        [_failure(run_id=901, workflow_path=".github/workflows/index_walk.yml",
                  conclusion="timed_out")])
    assert stats["fetched"] == 1
    assert "checkviolation" not in calls[0]["sig"]


def test_record_incidents_stops_at_its_wall_clock_budget(monkeypatch):
    """MAX_LOG_FETCHES is not a time budget: 25 fetches x up to 4 round trips x
    API_TIMEOUT_S=30 is ~50 minutes inside a job with `timeout-minutes: 5`, and a job
    kill is not an exception anything can catch."""
    calls = _patch_incidents(monkeypatch)
    monkeypatch.setattr(rwf, "_fetch_failed_job", lambda *_a: None)
    monkeypatch.setattr(rwf, "fetch_job_log", lambda *_a, **_k: None)
    failures = [_failure(run_id=i, workflow_path=f".github/workflows/w{i}.yml")
                for i in range(5)]
    stats = rwf.record_incidents(_IncidentConn(), "r", "t", failures, budget_seconds=0.0)
    assert stats["expired"] == 5 and calls == []


def _must_not_fetch() -> None:
    raise AssertionError("must not fetch a log")


def test_record_incidents_falls_back_scoped_when_the_log_is_unreadable(monkeypatch):
    calls = _patch_incidents(monkeypatch)
    monkeypatch.setattr(rwf, "_fetch_failed_job",
                        lambda *_a: {"job_id": 5, "step_name": "Run tests", "exit_code": "timed_out"})
    monkeypatch.setattr(rwf, "fetch_job_log", lambda *_a, **_k: None)
    stats = rwf.record_incidents(_IncidentConn(), "r", "t", [_failure()])
    assert stats["unreadable"] == 1
    assert calls[0]["sig"].endswith("@.github/workflows/drain.yml")


def test_record_incidents_respects_the_api_budget(monkeypatch):
    """Never open an incident for a run we chose not to read: a scoped fallback key
    there would assert nothing while looking like a finding."""
    calls = _patch_incidents(monkeypatch)
    monkeypatch.setattr(rwf, "MAX_LOG_FETCHES", 1)
    monkeypatch.setattr(rwf, "_fetch_failed_job", lambda *_a: None)
    monkeypatch.setattr(rwf, "fetch_job_log", lambda *_a, **_k: None)
    failures = [_failure(run_id=i, workflow_path=f".github/workflows/w{i}.yml")
                for i in range(4)]
    stats = rwf.record_incidents(_IncidentConn(), "r", "t", failures)
    assert stats["skipped"] == 3 and len(calls) == 1


def test_the_cursor_is_written_before_the_incident_pass_runs():
    """Ordering, pinned. record_incidents does network I/O inside a job with
    `timeout-minutes: 5`; a job kill is not an exception, so running it before
    `_write_cursor` put the poller's input-coverage guarantee behind a best-effort
    bookkeeping pass. Incidents annotate rows already committed — they can wait."""
    import inspect

    src = inspect.getsource(rwf.main)
    assert src.index("_write_cursor(conn, new_cursor)") < src.index("record_incidents(conn")


def test_record_incidents_resolves_even_with_nothing_new(monkeypatch):
    """Auto-resolve must run on a quiet poll — that is exactly the poll on which the
    fleet has recovered."""
    _patch_incidents(monkeypatch)
    assert rwf.record_incidents(_IncidentConn(), "r", "t", [])["resolved_success"] == 0
