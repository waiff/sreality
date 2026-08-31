"""ops_incidents: the onset arms, the single dispatch, and the three ways to close.

Hermetic — a scripted fake connection, no DB. That fake CANNOT enforce a CHECK, a
UNIQUE or an FK (a standing hazard in this repo), so the two invariants that live in
the schema rather than in Python — the partial unique index and the
`workflow_run_health` resolver predicate — are asserted against the emitted SQL text.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from toolkit import ops_incidents as oi

_T0 = dt.datetime(2026, 8, 26, 20, 15, tzinfo=dt.timezone.utc)
SIG = "checkviolation|new row for relation listings violates check constraint listings_area_basis_check"


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def execute(self, sql: str, params: Any = None) -> None:
        norm = " ".join(sql.split())
        c = self._conn
        c.executed.append((norm, params))
        self._rows, self.rowcount = [], 0
        if "FROM app_settings" in norm:
            self._rows = [(c.settings,)]
        elif "INSERT INTO ops_incidents" in norm:
            self._rows = [c.upsert_row] if c.upsert_row else []
        elif "INSERT INTO notification_dispatches" in norm:
            c.alerts.append(params)
            self.rowcount = 1
        elif "SET alerted_at = now()" in norm:
            self.rowcount = c.claim_rowcount
        elif "resolve_reason = 'success'" in norm:
            self._rows = c.resolved_by_success
        elif "resolve_reason = 'max_age'" in norm:
            self._rows = c.resolved_by_max_age
        elif "SELECT signature, workflow_paths FROM ops_incidents" in norm:
            self._rows = c.open_rows
        elif "UPDATE ops_incidents SET resolved_at" in norm:
            self.rowcount = c.manual_rowcount

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *_: Any) -> None:
        return None


class _Conn:
    def __init__(self, **kw: Any) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.alerts: list[Any] = []
        self.settings: Any = kw.get("settings", {})
        self.upsert_row: tuple[Any, ...] | None = kw.get("upsert_row")
        self.claim_rowcount: int = kw.get("claim_rowcount", 1)
        self.resolved_by_success: list = kw.get("resolved_by_success", [])
        self.resolved_by_max_age: list = kw.get("resolved_by_max_age", [])
        self.open_rows: list = kw.get("open_rows", [])
        self.manual_rowcount: int = kw.get("manual_rowcount", 1)

    def cursor(self) -> _Cur:
        return _Cur(self)


def _row(*, inc_id: int = 7, count: int = 1, paths: list[str] | None = None,
         url: str | None = "https://gh/run/1", excerpt: str | None = "boom",
         alerted: Any = None) -> tuple[Any, ...]:
    return (inc_id, count, _T0, paths or [], url, excerpt, alerted)


# --- the upsert ------------------------------------------------------------


def test_record_failure_reports_a_first_sighting_as_opened() -> None:
    conn = _Conn(upsert_row=_row(count=1))
    inc = oi.record_failure(conn, SIG, workflow_path=".github/workflows/a.yml")
    assert inc["opened"] is True
    assert inc["id"] == 7 and inc["failure_count"] == 1


def test_record_failure_reports_a_repeat_as_a_bump() -> None:
    conn = _Conn(upsert_row=_row(count=4, paths=["a.yml", "b.yml"]))
    inc = oi.record_failure(conn, SIG)
    assert inc["opened"] is False
    assert inc["workflow_paths"] == ["a.yml", "b.yml"]


def test_upsert_targets_the_partial_unique_index() -> None:
    """A plain unique index would let a RESOLVED incident block the next occurrence
    from ever opening. The fake conn cannot enforce that, so pin the SQL."""
    conn = _Conn(upsert_row=_row())
    oi.record_failure(conn, SIG)
    sql = conn.executed[0][0]
    assert "ON CONFLICT (signature) WHERE resolved_at IS NULL DO UPDATE" in sql
    assert "failure_count = ops_incidents.failure_count + 1" in sql


# --- onset -----------------------------------------------------------------


def test_a_lone_failure_on_one_workflow_does_not_alert() -> None:
    """35 of 63 measured red streaks are a single failure. Alerting on those is how a
    bell gets ignored."""
    conn = _Conn(upsert_row=_row(count=1, paths=["a.yml"]))
    inc = oi.record_failure_signature(conn, SIG, workflow_path="a.yml")
    assert inc["alerted"] is False
    assert conn.alerts == []


def test_the_second_failure_alerts_once() -> None:
    conn = _Conn(upsert_row=_row(count=2, paths=["a.yml"]))
    inc = oi.record_failure_signature(conn, SIG, workflow_path="a.yml")
    assert inc["alerted"] is True
    assert len(conn.alerts) == 1
    message, dedupe_key, channels = conn.alerts[0]
    assert dedupe_key == "sys:ops_incident:7:onset"
    assert SIG in message
    assert "2 failures since 2026-08-26 20:15 UTC" in message
    assert "https://gh/run/1" in message
    assert channels == []          # in-app only until the operator flips the setting


def test_breadth_alerts_before_the_count_does() -> None:
    """A fleet fan-out should not wait on a cadence it already has breadth for: the
    2026-08-26 signature reached its 2nd workflow 8 minutes after onset, while a
    same-workflow 2nd failure can be 164 minutes away under the Actions throttle."""
    conn = _Conn(upsert_row=_row(count=1, paths=["a.yml", "b.yml"]))
    inc = oi.record_failure_signature(conn, SIG, workflow_path="b.yml")
    assert inc["alerted"] is True


def test_an_already_alerted_incident_never_alerts_again() -> None:
    conn = _Conn(upsert_row=_row(count=99, paths=["a.yml"], alerted=_T0))
    inc = oi.record_failure_signature(conn, SIG)
    assert inc["alerted"] is False
    assert conn.alerts == []


def test_losing_the_alert_claim_emits_nothing() -> None:
    """Both producers can observe one incident in the same second; the dispatch's own
    dedupe_key would let the loser believe it alerted."""
    conn = _Conn(upsert_row=_row(count=2, paths=["a.yml"]), claim_rowcount=0)
    assert oi.maybe_alert(conn, oi.record_failure(conn, SIG)) is False
    assert conn.alerts == []


def test_the_alert_carries_the_excerpt() -> None:
    conn = _Conn(upsert_row=_row(count=2, excerpt="psycopg.errors.CheckViolation: ..."))
    oi.record_failure_signature(conn, SIG)
    assert "psycopg.errors.CheckViolation" in conn.alerts[0][0]


# --- thresholds ------------------------------------------------------------


def test_operator_thresholds_override_the_code_defaults() -> None:
    conn = _Conn(settings={"ops_incident_min_failures": 5}, upsert_row=_row(count=3, paths=["a.yml"]))
    assert oi.record_failure_signature(conn, SIG, workflow_path="a.yml")["alerted"] is False


def test_non_scalar_threshold_values_are_ignored() -> None:
    """verify_pipeline's load_thresholds merges only int/float out of this blob, so a
    JSON array here is dropped silently — read it the same way rather than diverging."""
    conn = _Conn(settings={"ops_incident_min_failures": [6, 24, 72]})
    assert oi._thresholds(conn)["ops_incident_min_failures"] == float(oi.DEFAULT_MIN_FAILURES)


def test_a_failed_settings_read_falls_back_instead_of_raising() -> None:
    class _Boom(_Conn):
        def cursor(self) -> Any:
            raise RuntimeError("db down")

    assert oi._thresholds(_Boom())["ops_incident_max_age_hours"] == float(oi.DEFAULT_MAX_AGE_HOURS)


# --- resolve ---------------------------------------------------------------


def test_success_resolver_requires_every_member_workflow_and_skips_empty_ones() -> None:
    """An incident with no member workflow (the always-on Railway worker has none)
    would otherwise satisfy `NOT EXISTS` vacuously and close on its first sighting."""
    conn = _Conn()
    oi.auto_resolve(conn)
    sql = next(s for s, _ in conn.executed if "resolve_reason = 'success'" in s)
    assert "coalesce(array_length(i.workflow_paths, 1), 0) > 0" in sql
    assert "LEFT JOIN workflow_run_health h ON h.workflow_path = p" in sql
    assert "h.last_success_at IS NULL OR h.last_success_at <= i.last_seen_at" in sql


def test_max_age_close_uses_the_operator_hours() -> None:
    conn = _Conn(settings={"ops_incident_max_age_hours": 72})
    oi.auto_resolve(conn)
    sql, params = next((s, p) for s, p in conn.executed if "resolve_reason = 'max_age'" in s)
    assert params == {"hours": 72}
    assert "last_seen_at <" in sql


def test_resolution_notifies_only_incidents_that_actually_rang() -> None:
    conn = _Conn(
        resolved_by_success=[(7, SIG, _T0), (8, "quiet|thing", None)],
        resolved_by_max_age=[(9, "stale|thing", _T0)],
    )
    stats = oi.auto_resolve(conn)
    assert stats["resolved_success"] == 2 and stats["resolved_max_age"] == 1
    keys = [k for (_m, k, _c) in conn.alerts]
    assert keys == ["sys:ops_incident:7:resolved", "sys:ops_incident:9:resolved"]
    assert "member workflows recovered" in conn.alerts[0][0]


def test_manual_resolve_records_its_note() -> None:
    conn = _Conn()
    assert oi.resolve_incident(conn, 7, "known, tracked in #1210") is True
    _sql, params = conn.executed[-1]
    assert params == ("manual: known, tracked in #1210", 7)


def test_manual_resolve_on_an_already_closed_incident_is_false() -> None:
    conn = _Conn(manual_rowcount=0)
    assert oi.resolve_incident(conn, 7) is False


# --- poller helpers --------------------------------------------------------


def test_open_signature_by_workflow_maps_every_member_path() -> None:
    conn = _Conn(open_rows=[(SIG, ["a.yml", "b.yml"]), ("other|x", ["b.yml"])])
    out = oi.open_signature_by_workflow(conn)
    assert out["a.yml"] == SIG
    assert out["b.yml"] == SIG      # newest-first wins; the older row does not clobber


def test_actions_context_parses_the_workflow_ref(monkeypatch: Any) -> None:
    monkeypatch.setenv(
        "GITHUB_WORKFLOW_REF",
        "waiff/sreality/.github/workflows/idnes_drain.yml@refs/heads/main")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "waiff/sreality")
    monkeypatch.setenv("GITHUB_RUN_ID", "32788072691")
    path, url = oi.actions_context()
    # Must match the shape the poller writes, or the success resolver joins nothing.
    assert path == ".github/workflows/idnes_drain.yml"
    assert url == "https://github.com/waiff/sreality/actions/runs/32788072691"


def test_actions_context_is_empty_off_ci(monkeypatch: Any) -> None:
    for k in ("GITHUB_WORKFLOW_REF", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"):
        monkeypatch.delenv(k, raising=False)
    assert oi.actions_context() == (None, None)
