"""Hermetic tests for the C7.2 R2 Mapy affected-set inventory — arm predicates,
the SQL shape of scripts/location_mapy_inventory.py, and the evidence-table
guarantees migration 385 has to keep. No DB.

The one rule these tests exist to make un-breakable: the inventory records
IDENTITY and REASON CODES only. A coordinate, matched_type or confidence landing
in any of the three evidence tables would be the same storage violation the
remediation exists to end (06-migration-backfill.md 6.1.5).
"""

from __future__ import annotations

import hashlib
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

from scripts import location_mapy_inventory as inv

_ROOT = Path(__file__).resolve().parent.parent.parent
_MIGRATION = _ROOT / "migrations" / "385_location_w1_mapy_affected_inventory.sql"

_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
# Praha, Wenceslas Square-ish: one cached geocode result.
_CACHE_ROWS = [
    ("praha 1, vaclavske namesti 1", 50.081_39, 14.427_63, _NOW),
    ("brno, kobylisy 3", 49.195_22, 16.606_79, _NOW),
    ("nowhere at all", None, None, _NOW),  # negative cache: no coordinate at all
]


def _cells() -> set[tuple[int, int]]:
    return inv.build_cache_cells(_CACHE_ROWS)


# ---------------------------------------------------------------- arm 1

@pytest.mark.parametrize("token", ["geocode", "carry_forward", "street", "locality"])
def test_arm1_covers_every_mapy_derived_provenance_token(token: str) -> None:
    # 'street'/'locality' are the bazos in-parser geocoder (06 6.1.2 row 5) — a
    # deliberate superset of C7.2's literal two-token list.
    row = inv.evidence_for_listing(1, "bazos", token, None, None, None, set())
    assert row is not None and row["arm1_coords_source"] is True
    assert row["coords_source"] == token


@pytest.mark.parametrize("token", ["link", "page", None, "", "unknown"])
def test_arm1_leaves_first_party_and_unstamped_provenance_alone(token: str | None) -> None:
    assert inv.evidence_for_listing(1, "bazos", token, None, None, None, set()) is None


# ---------------------------------------------------------------- arm 2

def test_arm2_is_the_attempt_stamp_alone() -> None:
    row = inv.evidence_for_listing(2, "idnes", None, _NOW, None, None, set())
    assert row is not None
    assert row["arm2_geocode_attempted"] is True
    assert row["geocode_attempted_at"] == _NOW
    assert row["arm1_coords_source"] is False and row["coords_source"] is None


# ---------------------------------------------------------------- arm 3

def test_arm3_matches_an_exact_cache_coordinate() -> None:
    assert inv.geom_matches_cache(50.081_39, 14.427_63, _cells()) is True


def test_arm3_matches_across_a_cell_boundary_via_the_3x3_neighbourhood() -> None:
    # ~1e-5 deg off (about a metre) — the float round-trip through
    # geography(Point,4326) is smaller than this; without the 3x3 expansion a
    # coordinate that rounds the other way would be missed.
    assert inv.geom_matches_cache(50.081_39 + 1e-5, 14.427_63 - 1e-5, _cells()) is True


def test_arm3_does_not_match_a_pin_a_hundred_metres_away() -> None:
    assert inv.geom_matches_cache(50.081_39 + 1e-3, 14.427_63, _cells()) is False


def test_arm3_ignores_negative_cache_rows_and_missing_geometry() -> None:
    cells = _cells()
    assert len(cells) == 2, "a lat/lng-NULL cache row holds no coordinate to match"
    assert inv.geom_matches_cache(None, None, cells) is False
    assert inv.geom_matches_cache(50.0, None, cells) is False
    assert inv.geom_matches_cache(50.081_39, 14.427_63, set()) is False


def test_arm3_alone_is_enough_to_be_in_the_inventory() -> None:
    row = inv.evidence_for_listing(3, "remax", None, None, 49.195_22, 16.606_79, _cells())
    assert row is not None
    assert row["arm3_geom_matches_cache"] is True
    assert row["reason_code"] == "mapy_derived_coordinate"


# ---------------------------------------------------------------- reason codes

def test_reason_code_names_a_coordinate_only_when_a_coordinate_arm_fired() -> None:
    assert inv.reason_code_for(True, False) == "mapy_derived_coordinate"
    assert inv.reason_code_for(False, True) == "mapy_derived_coordinate"
    # attempt-only: success and failure are indistinguishable (06 6.1.3).
    assert inv.reason_code_for(False, False) == "coordinate_provenance_unknown"


def test_evidence_row_carries_no_coordinate_of_any_kind() -> None:
    row = inv.evidence_for_listing(4, "idnes", "geocode", _NOW, 50.081_39, 14.427_63, _cells())
    assert row is not None
    assert set(row) == {
        "listing_id", "source", "arm1_coords_source", "coords_source",
        "arm2_geocode_attempted", "geocode_attempted_at", "arm3_geom_matches_cache",
        "reason_code",
    }
    # Every arm-3 output is a bool; a float (or a rounded cell key) reaching the
    # row would be a Mapy coordinate in a new disguise.
    assert not [v for v in row.values() if isinstance(v, float)]
    assert isinstance(row["arm3_geom_matches_cache"], bool)


# ---------------------------------------------------------------- arm 4

def test_cache_identity_row_is_identity_and_reason_only() -> None:
    row = inv.cache_identity_row("praha 1, vaclavske namesti 1", _NOW, 7, True)
    assert row == {
        "query_key": "praha 1, vaclavske namesti 1",
        "query_key_sha256": hashlib.sha256(
            b"praha 1, vaclavske namesti 1").hexdigest(),
        "resolved_at": _NOW,
        "reason_code": "mapy_derived_coordinate",
        "run_id": 7,
    }


def test_a_negative_cache_row_is_not_labelled_a_derived_coordinate() -> None:
    # It is a Mapy query that returned nothing: it never held a coordinate, so
    # 'mapy_derived_coordinate' would be a false statement in a ledger whose whole
    # value is being true. 06 6.1.5's vocabulary is closed to two codes.
    row = inv.cache_identity_row("nowhere at all", _NOW, 7, False)
    assert row["reason_code"] == "coordinate_provenance_unknown"
    assert row["query_key"] == "nowhere at all"


def test_the_cache_ledger_reason_codes_stay_inside_the_migration_vocabulary() -> None:
    codes = {inv.cache_identity_row("k", _NOW, 1, flag)["reason_code"]
             for flag in (True, False)}
    assert codes == {"mapy_derived_coordinate", "coordinate_provenance_unknown"}


# ---------------------------------------------------------------- SQL shape

def _norm(sql: str) -> str:
    return " ".join(sql.split()).lower()


def test_every_evidence_insert_is_conflict_tolerant() -> None:
    for sql in (inv._AFFECTED_INSERT_SQL, inv._CACHE_INSERT_SQL, inv._PROPS_INSERT_SQL):
        assert "on conflict" in _norm(sql) and "do nothing" in _norm(sql)
        assert "do update" not in _norm(sql), "an upsert would trip the immutability trigger"


def test_no_statement_updates_or_deletes_an_evidence_table() -> None:
    evidence = ("mapy_affected", "mapy_affected_cache", "mapy_affected_props")
    for name, sql in vars(inv).items():
        if not (name.endswith("_SQL") and isinstance(sql, str)):
            continue
        text = _norm(sql)
        if not text.startswith(("update", "delete", "truncate")):
            continue
        assert not any(re.search(rf"\b{t}\b", text) for t in evidence), name


def test_the_scan_covers_every_listing_and_is_keyset_paginated() -> None:
    sql = _norm(inv._LISTING_BATCH_SQL)
    # The licence gate and the R4 purge key on listing_id; an inactive row's
    # coordinate is published exactly like an active one's.
    assert "is_active" not in sql
    assert "l.id > %(after_id)s" in sql and "order by l.id" in sql and "limit" in sql


def test_the_scan_reads_the_three_listing_arm_inputs() -> None:
    sql = _norm(inv._LISTING_BATCH_SQL)
    assert "raw_json->'coords'->>'source'" in sql
    assert "geocode_attempted_at" in sql


def test_the_property_closure_is_the_full_child_intersection() -> None:
    sql = _norm(inv._PROPS_INSERT_SQL)
    assert "join mapy_affected" in sql and "distinct l.property_id" in sql
    assert "property_id is not null" in sql


def test_the_resume_query_ignores_operator_started_runs() -> None:
    assert "where resumable" in _norm(inv._RESUME_SQL)


def test_the_resume_query_is_scoped_to_one_restart_epoch() -> None:
    # An unscoped max() lets a COMPLETED earlier sweep's end-of-table mark mask an
    # interrupted --restart, which would then be unresumable and the next dispatch
    # would print `complete` after scanning nothing.
    assert "restart_epoch = %(restart_epoch)s" in _norm(inv._RESUME_SQL)
    assert "max(restart_epoch)" in _norm(inv._EPOCH_SQL)
    assert "%(restart_epoch)s" in _norm(inv._RUN_INSERT_SQL)


def test_timeout_guard_is_transaction_local() -> None:
    # connect() is autocommit on the transaction-mode pooler: a session-level SET
    # can land on a different backend than the statement it was meant to guard.
    sql = _norm(inv._TIMEOUT_GUARD_SQL)
    assert "set_config('statement_timeout', %(statement_timeout)s, true)" in sql
    assert "set_config('lock_timeout', %(lock_timeout)s, true)" in sql


# ---------------------------------------------------------------- migration 385

def _migration_sql() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def test_migration_exists_and_is_numbered_for_the_scanners() -> None:
    assert _MIGRATION.is_file()
    assert re.match(r"^\d+_[a-z0-9_]+\.sql$", _MIGRATION.name)


def test_migration_creates_the_three_evidence_tables_plus_the_run_table() -> None:
    sql = _migration_sql().lower()
    for table in ("mapy_affected", "mapy_affected_cache", "mapy_affected_props",
                  "mapy_inventory_runs"):
        assert f"create table {table} (" in sql


def test_every_evidence_table_is_immutable_and_the_run_table_is_not() -> None:
    sql = _migration_sql().lower()
    for table in ("mapy_affected", "mapy_affected_cache", "mapy_affected_props"):
        trigger = re.search(
            rf"create trigger \w+\s+before update or delete or truncate on {table}\b", sql)
        assert trigger, f"{table} has no immutability trigger"
    assert "on mapy_inventory_runs\n  for each statement" not in sql


def test_no_evidence_table_declares_a_coordinate_column() -> None:
    sql = _migration_sql()
    bodies = re.findall(r"create table (mapy_\w+) \((.*?)\n\);", sql, re.S)
    assert len(bodies) == 4
    # The one column whose NAME names geometry while holding only a boolean.
    allowed = {"arm3_geom_matches_cache"}
    seen = 0
    for name, body in bodies:
        if name == "mapy_inventory_runs":
            continue
        for line in body.splitlines():
            stripped = line.strip()
            # Column definitions only: skip comments, table constraints and the
            # wrapped continuation lines of a multi-line CHECK.
            if not re.match(r"^[a-z][a-z0-9_]*\s+\S", stripped):
                continue
            column, _, rest = stripped.partition(" ")
            if column in ("constraint", "check", "primary", "unique", "foreign"):
                continue
            seen += 1
            if column in allowed:
                continue
            for forbidden in ("lat", "lng", "lon", "geom", "matched_type", "confidence",
                              "cell_key", "coordinate"):
                assert forbidden not in column, f"{name}.{column}"
            # A numeric/geographic type on an evidence table is how a coordinate
            # would get in under an innocent name.
            for banned_type in ("double precision", "numeric", "real", "geography",
                                "geometry", "point"):
                assert banned_type not in rest.lower(), f"{name}.{column} :: {rest}"
    assert seen >= 15, "the column scanner matched almost nothing — check the regex"


def test_the_run_table_carries_restart_lineage() -> None:
    sql = _migration_sql().lower()
    assert re.search(r"restart_epoch\s+integer not null default 0", sql)


def test_the_migration_sets_its_timeouts_transaction_locally() -> None:
    # apply_migration wraps the file in a transaction; a bare `set` would leak the
    # timeout onto whatever the pooled backend serves next.
    sql = _migration_sql().lower()
    assert "set local lock_timeout" in sql
    assert not re.search(r"^\s*set\s+lock_timeout", sql, re.M)


def test_new_relations_are_dark_to_browser_roles() -> None:
    body = "\n".join(line.split("--", 1)[0] for line in _migration_sql().lower().splitlines())
    sql = " ".join(body.split())
    # One statement per relation, not a comma list: test_location_schema_contracts
    # parses these heads per object, and a comma-list revoke is invisible to it.
    for table in ("mapy_inventory_runs", "mapy_affected", "mapy_affected_cache",
                  "mapy_affected_props"):
        assert re.search(rf"revoke all on {table}\s+from anon, authenticated;", sql), table
    assert "revoke all on sequence mapy_inventory_runs_id_seq from anon, authenticated" in sql
    # `public` too: a function's default ACL is EXECUTE TO PUBLIC, which anon and
    # authenticated inherit, so revoking only the named roles leaves it callable.
    assert "revoke execute on function mapy_inventory_immutable() from public, anon, authenticated" in sql
    for table in ("mapy_affected", "mapy_affected_cache", "mapy_affected_props",
                  "mapy_inventory_runs"):
        assert f"alter table {table} enable row level security" in sql
    assert "grant " not in sql, "this subsystem is service-role only"


# ---------------------------------------------------------------- run control flow

class _FakeCursor:
    """Answers by statement identity; records what was written."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.rowcount = -1
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        text = _norm(sql)
        self.state["executed"].append((text, params))
        if "to_regclass" in text:
            self._result = [("present",)]
        elif "from geocode_cache" in text:
            self._result = [tuple(r) for r in _CACHE_ROWS]
        elif text.startswith("insert into mapy_inventory_runs"):
            run = {
                "id": len(self.state["runs"]) + 1,
                "restart_epoch": params["restart_epoch"],
                "resumable": params["resumable"],
                "scanned_through_listing_id": 0,
            }
            self.state["runs"].append(run)
            self._result = [(run["id"],)]
        elif "max(restart_epoch)" in text:
            self._result = [(max((r["restart_epoch"] for r in self.state["runs"]),
                                 default=0),)]
        elif "max(scanned_through_listing_id)" in text:
            epoch = params["restart_epoch"] if params else 0
            marks = [r["scanned_through_listing_id"] for r in self.state["runs"]
                     if r["resumable"] and r["restart_epoch"] == epoch]
            self._result = [(max(marks, default=0),)]
        elif text.startswith("select l.id"):
            after = params["after_id"] if params else 0
            page = [r for r in self.state["listings"] if r[0] > after][:params["batch_size"]]
            self._result = page
        elif text.startswith("insert into mapy_affected_props"):
            self.state["props"] += 1
            self.rowcount = 3
        elif text.startswith("update mapy_inventory_runs"):
            self.state["run_updates"].append((text, params))
            if params and "last_id" in params:
                for run in self.state["runs"]:
                    if run["id"] == params["run_id"]:
                        run["scanned_through_listing_id"] = params["last_id"]
        elif "count(*) filter" in text:
            self._result = [(1, 1, 1, 2)]
        elif text.startswith("select count(*)"):
            self._result = [(3,)]
        else:
            self._result = [(None,)]

    def executemany(self, sql: str, seq: list[dict[str, Any]]) -> None:
        text = _norm(sql)
        key = "cache_inserts" if "mapy_affected_cache" in text else "affected_inserts"
        self.state[key].extend(seq)
        self.rowcount = len(seq)

    def fetchone(self) -> tuple[Any, ...]:
        return self._result[0]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result


class _FakeConn:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.state)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.state["transactions"] += 1
        yield


def _run(monkeypatch: pytest.MonkeyPatch, argv: list[str], state: dict[str, Any]) -> int:
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://fake")
    monkeypatch.setattr(inv.db, "connect", lambda *a, **k: _FakeConn(state))
    monkeypatch.setattr(sys, "argv", ["location_mapy_inventory", *argv])
    return inv.main()


def _state(listings: list[tuple[Any, ...]], resume_from: int = 0) -> dict[str, Any]:
    # A prior resumable run in the current (epoch 0) lineage is what a resume mark
    # actually is — the table has no free-floating high-water mark.
    runs: list[dict[str, Any]] = []
    if resume_from:
        runs.append({"id": 1, "restart_epoch": 0, "resumable": True,
                     "scanned_through_listing_id": resume_from})
    return {
        "executed": [], "affected_inserts": [], "cache_inserts": [], "run_updates": [],
        "listings": listings, "runs": runs, "props": 0, "transactions": 0,
    }


def _listing(lid: int, coords_source: str | None = None, attempted: datetime | None = None,
             lat: float | None = None, lng: float | None = None) -> tuple[Any, ...]:
    return (lid, "idnes", coords_source, attempted, lat, lng)


def test_a_full_run_records_every_arm_and_walks_to_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listings = [
        _listing(1, "geocode"),
        _listing(2, "page"),
        _listing(3, None, _NOW),
        _listing(4, None, None, 50.081_39, 14.427_63),
        _listing(5, "link", None, 12.0, 12.0),
    ]
    state = _state(listings)
    assert _run(monkeypatch, ["--batch-size", "10000"], state) == 0

    recorded = {row["listing_id"] for row in state["affected_inserts"]}
    assert recorded == {1, 3, 4}
    assert len(state["cache_inserts"]) == len(_CACHE_ROWS)
    assert state["props"] == 1
    # Terminal statuses only: the run must not be left looking in-flight.
    statuses = [p["status"] for _t, p in state["run_updates"] if p and "status" in p]
    assert statuses == ["completed"]


def test_a_resumed_run_starts_after_the_recorded_high_water_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listings = [_listing(1, "geocode"), _listing(9, "geocode")]
    state = _state(listings, resume_from=5)
    assert _run(monkeypatch, [], state) == 0
    assert {row["listing_id"] for row in state["affected_inserts"]} == {9}


def test_a_budgeted_run_stops_and_stays_resumable(monkeypatch: pytest.MonkeyPatch) -> None:
    listings = [_listing(i, "geocode") for i in range(1, 40)]
    state = _state(listings)
    assert _run(monkeypatch, ["--limit", "10"], state) == 0
    statuses = [p["status"] for _t, p in state["run_updates"] if p and "status" in p]
    assert statuses == ["stopped"]
    progress = [p for t, p in state["run_updates"] if "scanned_through_listing_id" in t]
    assert progress and progress[-1]["last_id"] == 10


def test_an_explicit_start_marks_the_run_unresumable(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state([_listing(7, "geocode")])
    assert _run(monkeypatch, ["--start-after-id", "6"], state) == 0
    run_insert = [p for t, p in state["executed"]
                  if t.startswith("insert into mapy_inventory_runs")]
    assert run_insert and run_insert[0]["resumable"] is False


def _first_scan_after_id(state: dict[str, Any]) -> int:
    return next(p["after_id"] for t, p in state["executed"] if t.startswith("select l.id"))


def test_an_interrupted_restart_resumes_from_its_own_mark_not_the_finished_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """complete run -> --restart -> interrupted -> plain dispatch resumes the RESTART.

    The bug this pins: with an unscoped `max(scanned_through_listing_id) WHERE
    resumable`, the completed sweep's end-of-table mark wins over the restart's
    own mark, so the restart can never be resumed and the next dispatch reports
    `complete` after scanning nothing — in a ledger whose only job is completeness.
    """
    listings = [_listing(i, "geocode") for i in range(1, 31)]
    state = _state(listings)

    assert _run(monkeypatch, [], state) == 0
    assert state["runs"][-1]["restart_epoch"] == 0
    assert state["runs"][-1]["scanned_through_listing_id"] == 30

    # The restart opens a new epoch and is cut short with its own mark at 10. A
    # SIGKILL leaves exactly this row minus the terminal stamp, which resume
    # ignores — it reads the mark, not the status.
    state["executed"] = []
    assert _run(monkeypatch, ["--restart", "--limit", "10"], state) == 0
    restart_run = state["runs"][-1]
    assert restart_run["restart_epoch"] == 1
    assert _first_scan_after_id(state) == 0, "--restart rescans from the top"
    assert restart_run["scanned_through_listing_id"] == 10

    state["executed"] = []
    state["affected_inserts"] = []
    assert _run(monkeypatch, [], state) == 0
    assert state["runs"][-1]["restart_epoch"] == 1, "a plain dispatch stays in the epoch"
    assert _first_scan_after_id(state) == 10, "resumed the finished sweep, not the restart"
    assert {row["listing_id"] for row in state["affected_inserts"]} == set(range(11, 31))


def test_an_operator_started_run_never_becomes_a_restart_epochs_resume_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listings = [_listing(i, "geocode") for i in range(1, 31)]
    state = _state(listings)
    assert _run(monkeypatch, ["--restart", "--limit", "10"], state) == 0
    assert _run(monkeypatch, ["--start-after-id", "25"], state) == 0
    assert state["runs"][-1]["resumable"] is False

    state["executed"] = []
    assert _run(monkeypatch, [], state) == 0
    assert _first_scan_after_id(state) == 10


def test_the_failure_stamp_uses_a_fresh_connection_and_never_masks_the_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The sweep may die BECAUSE the connection died; reusing it would swap the real
    # traceback for a psycopg error and lose the run's terminal status either way.
    opened: list[int] = []

    class _DeadConn(_FakeConn):
        def cursor(self) -> _FakeCursor:
            raise RuntimeError("connection is closed")

    state = _state([])

    def _fresh(*_a: object, **_k: object) -> _FakeConn:
        opened.append(1)
        return _FakeConn(state)

    monkeypatch.setattr(inv.db, "connect", _fresh)
    inv.record_failure(7, ValueError("boom"))
    assert opened == [1]
    fails = [p for t, p in state["executed"] if t.startswith("update mapy_inventory_runs")]
    assert fails and fails[0]["run_id"] == 7 and "ValueError: boom" in fails[0]["note"]

    monkeypatch.setattr(inv.db, "connect", lambda *a, **k: _DeadConn(state))
    inv.record_failure(7, ValueError("boom"))  # swallowed, so `raise` re-raises the cause


def test_a_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _state([_listing(1, "geocode")], resume_from=99)
    assert _run(monkeypatch, ["--dry-run"], state) == 0
    assert state["affected_inserts"] == [] and state["cache_inserts"] == []
    assert state["run_updates"] == [] and state["props"] == 0
    # A dry run also ignores the resume mark — it reports the whole table.
    assert any(t.startswith("select l.id") and p["after_id"] == 0
               for t, p in state["executed"])
