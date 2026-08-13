"""The W2a-5 pruner lane — the scheduled half of P4 retention.

* The load-bearing property is NOT what this lane removes, it is that it removes nothing
  until the operator says so: the workflow ships with a live weekly cron, so a disabled
  lane has to no-op before it takes a lease and before it reads the archive. That is the
  hermetic half, and it runs everywhere.
* The retention semantics (cap re-assertion across a group nobody re-appended, the pin
  predicate surviving every path, hot-window arithmetic) live in SQL against real
  constraints — a fake connection can prove a statement ran, not that a foreign key held —
  so they run against the replayed schema (TEST_DATABASE_URL), the same lane
  `tests/location_data/test_payloads.py` uses.
* Nothing here touches production: live rows are keyed on a per-test uuid.
"""

from __future__ import annotations

import ast
import inspect
import os
import re
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psycopg
import pytest

from location_data import payload_prune, payloads
from location_data.payload_norm import VolatileProfile
from location_data.resolver import lease

_DB_URL = os.environ.get("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — the pruner's semantics run in the CI DB job",
)

_JSON = "application/json"


# --------------------------------------------------------------------- offline


def test_the_only_removal_target_in_the_module_is_the_payload_store() -> None:
    """02 P4: claims and resolutions are never pruned, and the legacy staging archive is
    preservation substrate for the whole program. Asserted structurally, so a future edit
    to the hot-window statement cannot quietly widen onto another table."""
    source = Path(payload_prune.__file__).read_text(encoding="utf-8").lower()
    targets = [
        line.split("delete from", 1)[1].split()[0]
        for line in source.splitlines()
        if "delete from" in line
    ]

    assert targets == ["portal_raw_payloads"]


def test_no_other_destructive_verb_appears_in_the_module() -> None:
    """A cap that grew a TRUNCATE or a DROP would pass the DELETE-target test above."""
    source = Path(payload_prune.__file__).read_text(encoding="utf-8").lower()

    assert not re.search(r"\b(truncate|drop\s+table)\b", source)


def _sql_constants() -> dict[str, str]:
    """The module's module-level `*_SQL` PLAIN STRING LITERALS, keyed by name.

    Parsed rather than imported, so a constant built by an f-string or a `.replace()` is
    absent here exactly as it would be absent from the PREPARE corpus.
    """
    tree = ast.parse(Path(payload_prune.__file__).read_text(encoding="utf-8"))
    return {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and target.id.endswith("_SQL")
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def test_every_executed_statement_is_discoverable_by_the_sql_corpus() -> None:
    """The PREPARE sweep only sees module-level `*_SQL` string CONSTANTS; an f-string or a
    runtime-composed statement is invisible to it and would ship untyped — and this module
    is exactly the kind the check exists for, since two of its statements remove rows."""
    assert set(_sql_constants()) == {
        "_REGCLASS_SQL", "_ENSURE_LANE_SQL", "_LANE_ENABLED_SQL", "_KEYS_SQL",
        "_GROUPS_SQL", "_HOT_WINDOW_SQL", "_BATCH_INSERT_SQL", "_BATCH_FINISH_SQL",
    }


def test_the_pin_predicate_is_the_writers_own_not_a_second_copy() -> None:
    """One definition of "pinned", or the two drift and the sweep starts evicting bodies
    the writer would have kept. The pruner calls the writer's statements; it does not
    restate the predicate."""
    source = inspect.getsource(payload_prune.prune_one_group)
    assert "payloads.repin_group(" in source
    assert "payloads.prune_group(" in source
    # And the re-pin runs FIRST: the cap and the window both read `pinned` rather than
    # recomputing it, so a stale pin set would be read as authoritative.
    assert source.index("payloads.repin_group(") < source.index("payloads.prune_group(")
    assert source.index("payloads.repin_group(") < source.index("_HOT_WINDOW_SQL")


def test_the_writer_and_the_pruner_share_one_cap_statement() -> None:
    """The reuse has to be real: `append_payload` must go through the same two helpers,
    or "shared" is only true of the sweep's half."""
    source = inspect.getsource(payloads.append_payload)
    assert "repin_group(cur, **group)" in source
    assert "prune_group(cur, **group, version_cap=cap)" in source


def test_the_hot_window_cutoff_is_the_observation_horizon() -> None:
    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)

    assert payload_prune.hot_window_cutoff(90, now=now) == now - timedelta(days=90)
    assert payload_prune.hot_window_cutoff(1, now=now) == datetime(
        2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def test_the_hot_window_is_env_overridable_and_never_unbounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0 days would mean "evict everything unpinned on sight", which is precisely the
    state `env_positive_int` refuses to make reachable from a typo."""
    assert payload_prune.hot_window_days() == payload_prune.DEFAULT_HOT_WINDOW_DAYS
    for bad in ("0", "-1", "", "abc", "90d"):
        monkeypatch.setenv(payload_prune.HOT_WINDOW_ENV, bad)
        assert payload_prune.hot_window_days() == payload_prune.DEFAULT_HOT_WINDOW_DAYS
    monkeypatch.setenv(payload_prune.HOT_WINDOW_ENV, "30")
    assert payload_prune.hot_window_days() == 30


def test_the_lane_row_is_seeded_disabled_and_never_flipped_by_code() -> None:
    """`lease.held` creates a lane row with `enabled = true` — right for every other lane
    in the program, wrong for the one that ships behind a storage gate with a live cron.
    So this module seeds its own row, disabled, and nothing here ever sets it true."""
    seed = " ".join(payload_prune._ENSURE_LANE_SQL.split()).lower()
    assert "insert into location_jobs" in seed
    assert "false," in seed or " false" in seed
    assert "on conflict (job_name) do nothing" in seed

    # Over the EXECUTED statements, not the file: `main` prints the operator's own
    # enabling command, and a raw-text scan cannot tell a log line from a write.
    for name, sql in _sql_constants().items():
        flat = " ".join(sql.split()).lower()
        assert "update location_jobs" not in flat, f"{name} writes the ops-calendar row"
        assert "enabled = true" not in flat, f"{name} enables the lane"

    # The claim above about the shared upsert is checked against the real thing, so a
    # change to `lease.held` cannot silently invalidate this lane's whole safety story.
    assert "enabled)" in lease._UPSERT_JOB_SQL
    assert "true" in lease._UPSERT_JOB_SQL.lower()


def test_the_lease_refuses_a_disabled_lane_as_the_second_rail() -> None:
    """Even if the gate in `main` were bypassed, acquiring is a conditional UPDATE that
    carries `AND enabled` — so a disabled lane yields False and `run` is unreachable."""
    acquire = " ".join(lease._ACQUIRE_SQL.split()).lower()
    assert "and enabled" in acquire


def test_the_disabled_gate_precedes_the_lease_and_the_sweep_in_main() -> None:
    source = inspect.getsource(payload_prune.main)
    gate = source.index("if not lane_enabled(")
    assert source.index("ensure_lane(conn") < gate
    assert gate < source.index("lease.held(")
    assert gate < source.index("run(conn, **kwargs)")


def test_a_disabled_lane_reads_nothing_takes_no_lease_and_exits_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole reason a live weekly `schedule` is safe to ship. Executed rather than
    asserted from the source: the claim is about what reaches the database."""
    conn = _RecordingConn(enabled=False)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://unused")
    monkeypatch.setattr(payload_prune.db, "connect", lambda: _Ctx(conn))
    monkeypatch.setattr(payload_prune.lease, "held", _never_leased)

    assert payload_prune.main([]) == 0

    executed = " ".join(sql.lower() for sql, _ in conn.executed)
    assert "portal_raw_payloads" not in executed, "the archive was read while disabled"
    assert "location_claim_batches" not in executed, "a batch row was opened while disabled"
    assert "delete" not in executed
    # Exactly the two statements the gate needs, each inside its own bounded transaction.
    # Exactly the preflight plus the two statements the gate needs. The preflight is three
    # `to_regclass` catalog probes — it reads no row of any of them.
    assert [sql for sql, _ in conn.executed if "set_config" not in sql] == [
        payload_prune._REGCLASS_SQL, payload_prune._REGCLASS_SQL,
        payload_prune._REGCLASS_SQL,
        payload_prune._ENSURE_LANE_SQL, payload_prune._LANE_ENABLED_SQL,
    ]


def test_an_unmigrated_database_refuses_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ensure_lane` WRITES to `location_jobs`, one of the checked relations, so the
    preflight has to lead. Before it did, an unmigrated database got an UndefinedTable
    traceback out of the seed instead of the refusal this message exists to be."""
    conn = _RecordingConn(enabled=False, relations_present=False)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://unused")
    monkeypatch.setattr(payload_prune.db, "connect", lambda: _Ctx(conn))
    monkeypatch.setattr(payload_prune.lease, "held", _never_leased)

    assert payload_prune.main([]) == 2

    executed = [sql for sql, _ in conn.executed if "set_config" not in sql]
    assert executed == [payload_prune._REGCLASS_SQL] * 3, (
        "the lane seed ran against a database that has no location schema")


@pytest.mark.parametrize("flag", ["--version-cap", "--hot-window-days"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_non_positive_retention_budget_is_refused_not_silently_replaced(
    monkeypatch: pytest.MonkeyPatch, flag: str, value: str,
) -> None:
    """`value or fallback` silently swallowed an explicit 0, running the sweep under the
    default instead. Refused rather than floored: this lane deletes rows, so a typo'd
    budget must fail loudly instead of quietly meaning something else."""
    conn = _RecordingConn(enabled=False)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://unused")
    monkeypatch.setattr(payload_prune.db, "connect", lambda: _Ctx(conn))

    assert payload_prune.main([flag, value]) == 2
    assert conn.executed == [], "the database was touched before the budget was validated"


def test_an_explicit_budget_beats_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(payload_prune.HOT_WINDOW_ENV, "45")
    assert payload_prune._explicit_or_env(
        "--hot-window-days", 7, payload_prune.HOT_WINDOW_ENV, 90) == 7
    assert payload_prune._explicit_or_env(
        "--hot-window-days", None, payload_prune.HOT_WINDOW_ENV, 90) == 45


def test_an_enabled_lane_does_reach_the_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate has to be a gate, not a permanent stop: with the flag flipped, the run
    proceeds to the lease — which is where the second rail then applies."""
    conn = _RecordingConn(enabled=True)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://unused")
    monkeypatch.setattr(payload_prune.db, "connect", lambda: _Ctx(conn))
    reached: list[str] = []
    monkeypatch.setattr(payload_prune.lease, "held", _busy_lease(reached))

    assert payload_prune.main([]) == 0
    assert reached == [payload_prune.JOB_NAME]


def test_the_lane_is_the_ops_calendar_name_on_the_designs_cadence() -> None:
    """02 §2.3.2 P4.2 schedules `payload_archive_prune` weekly, and `location_jobs_stale`
    (migration 384) pages on `now() - last_success_at > 3 x cadence` — so a cadence that
    disagrees with the cron is a false page or a silent gap."""
    assert payload_prune.JOB_NAME == "payload_archive_prune"
    assert payload_prune.CADENCE == "7 days"
    assert payload_prune.CONCURRENCY_GROUP == "location-payload"


def test_the_workflow_cron_matches_the_lanes_cadence() -> None:
    import yaml

    root = Path(__file__).resolve().parents[2]
    wf = yaml.safe_load(
        (root / ".github" / "workflows" / "location_payload_prune.yml").read_text(
            encoding="utf-8"))
    # `on:` parses as the boolean True in YAML 1.1 unless quoted.
    triggers = wf.get("on", wf.get(True))

    assert [t["cron"] for t in triggers["schedule"]] == ["0 4 * * 0"]
    assert "workflow_dispatch" in triggers


def test_the_dry_run_neither_removes_nor_leases() -> None:
    run_source = inspect.getsource(payload_prune.run)
    main_source = inspect.getsource(payload_prune.main)
    # No batch row, so a dry run cannot stamp `last_success_at` for a sweep that
    # reclaimed nothing, and the per-group work is skipped entirely.
    assert "if not dry_run:" in run_source
    assert "if dry_run:\n                    continue" in run_source
    assert "not taking the %s lease" in main_source


# ------------------------------------------------------------------------ live


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(_DB_URL, autocommit=True) as c:
        yield c


def _key() -> str:
    return f"prune-{uuid.uuid4().hex}"


def _isolated_source() -> str:
    """A private portal label, so a keyset sweep sees only one test's rows.

    The `run`-level tests walk `portal_raw_payloads` by (source, source_id_native) with no
    upper bound, so a shared source would let a concurrently written group from another
    test drift into the scan and make the counts non-deterministic.
    """
    return f"prunetest{uuid.uuid4().hex[:12]}"


def _append(
    conn: psycopg.Connection, native: str, body: bytes, *, source: str = "idnes",
    **kwargs: Any,
) -> Any:
    """A body in the archive with the writer's own retention effectively OFF.

    The high cap is the point: it produces exactly the state this lane exists for — a
    group deeper than the policy allows, because the append that would have capped it
    ran under a different one, or because the pin set has moved since.
    """
    kwargs.setdefault("version_cap", 10_000)
    return payloads.append_payload(
        conn, source=source, source_id_native=native, page_kind="detail",
        listing_id=None, body=body, content_type=_JSON, http_status=200,
        contract_version=1, observed_at=datetime.now(timezone.utc),
        volatile=VolatileProfile(), **kwargs)


def _versions(conn: psycopg.Connection, native: str) -> list[int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version_seq FROM portal_raw_payloads WHERE source_id_native = %s "
            "ORDER BY version_seq", (native,))
        return [int(r[0]) for r in cur.fetchall()]


def _pinned(conn: psycopg.Connection, native: str) -> list[tuple[int, bool]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version_seq, pinned FROM portal_raw_payloads "
            "WHERE source_id_native = %s ORDER BY version_seq", (native,))
        return [(int(r[0]), bool(r[1])) for r in cur.fetchall()]


def _backdate(conn: psycopg.Connection, native: str, seqs: list[int], days: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE portal_raw_payloads SET last_observed_at = now() - %s::interval "
            "WHERE source_id_native = %s AND version_seq = ANY(%s)",
            (f"{days} days", native, seqs))


def _sweep(
    conn: psycopg.Connection, native: str, *, source: str = "idnes", **kwargs: Any,
) -> dict[str, Any]:
    """One group through the lane's own entry point, on the lane's own ordering."""
    kwargs.setdefault("version_cap", 10_000)
    kwargs.setdefault("hot_window", 10_000)
    return payload_prune.prune_one_group(
        conn, source=source, source_id_native=native, page_kind="detail",
        cutoff=payload_prune.hot_window_cutoff(kwargs.pop("hot_window")),
        statement_timeout=60, **kwargs)


@requires_db
def test_the_lane_row_is_created_disabled(conn: psycopg.Connection) -> None:
    # The seed is idempotent and must never read back as enabled on a fresh schema.
    payload_prune.ensure_lane(conn, statement_timeout=30)
    payload_prune.ensure_lane(conn, statement_timeout=30)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT enabled, cadence, concurrency_group FROM location_jobs "
            "WHERE job_name = %s", (payload_prune.JOB_NAME,))
        row = cur.fetchone()

    assert row is not None, "the ops-calendar row was not created"
    assert row[0] is False
    assert row[1] == timedelta(days=7)
    assert row[2] == payload_prune.CONCURRENCY_GROUP
    assert payload_prune.lane_enabled(conn, statement_timeout=30) is False


@requires_db
def test_a_missing_lane_row_reads_as_disabled(conn: psycopg.Connection) -> None:
    # The gate must fail closed: an unseeded schema is not an invitation to sweep.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM location_jobs WHERE job_name = %s",
                    (payload_prune.JOB_NAME,))

    assert payload_prune.lane_enabled(conn, statement_timeout=30) is False


@requires_db
def test_the_sweep_re_asserts_a_cap_the_writer_never_applied(
    conn: psycopg.Connection,
) -> None:
    # The reason this lane exists: eight versions written under a cap of 10,000, then the
    # policy tightened. No append is coming to notice, so the sweep has to.
    native = _key()
    for i in range(8):
        _append(conn, native, f'{{"v": {i}}}'.encode())
    assert _versions(conn, native) == [1, 2, 3, 4, 5, 6, 7, 8]

    result = _sweep(conn, native, version_cap=5)

    # Five ranks survive, plus version 1 — pinned as the first body and therefore exempt
    # from the cap rather than counted inside it. Identical to the writer's own arithmetic.
    assert _versions(conn, native) == [1, 4, 5, 6, 7, 8]
    assert result["capped"] == 2
    assert result["cold"] == 0


@requires_db
def test_the_first_and_latest_bodies_survive_the_cap(conn: psycopg.Connection) -> None:
    native = _key()
    for i in range(6):
        _append(conn, native, f'{{"v": {i}}}'.encode())

    _sweep(conn, native, version_cap=2)

    assert _versions(conn, native) == [1, 5, 6]
    assert _pinned(conn, native) == [(1, True), (5, False), (6, True)]


@requires_db
def test_the_first_and_latest_bodies_survive_the_hot_window(
    conn: psycopg.Connection,
) -> None:
    # The edge pins are age-exempt, not merely cap-exempt: backdating the WHOLE group past
    # the window must still leave its history addressable at both ends.
    native = _key()
    for i in range(4):
        _append(conn, native, f'{{"v": {i}}}'.encode())
    _backdate(conn, native, [1, 2, 3, 4], days=400)

    result = _sweep(conn, native, hot_window=90)

    assert _versions(conn, native) == [1, 4]
    assert result["cold"] == 2
    assert result["capped"] == 0


@requires_db
def test_a_cold_body_is_dropped_and_a_recent_one_is_not(
    conn: psycopg.Connection,
) -> None:
    # Hot-window arithmetic, both directions, with the cap deliberately out of the way:
    # version 2 is older than the window and unpinned, version 3 is inside it. Under a cap
    # of 2 the ranking would have taken version 3 as well — the window is a SEPARATE axis,
    # and a body inside it survives on age alone.
    native = _key()
    for i in range(4):
        _append(conn, native, f'{{"v": {i}}}'.encode())
    _backdate(conn, native, [2], days=120)

    result = _sweep(conn, native, hot_window=90)

    assert _versions(conn, native) == [1, 3, 4]
    assert (result["cold"], result["capped"]) == (1, 0)


@requires_db
def test_a_body_a_claim_points_at_survives_every_path(conn: psycopg.Connection) -> None:
    """The FK, not a policy: `location_claims.payload_id` references the store with NO
    ACTION (382), so an unpinned removal of a referenced body raises ForeignKeyViolation
    and rolls the group's whole transaction back. Age and depth are both applied here."""
    from tests.location_data.test_payloads import _claim_on_payload

    native = _key()
    for i in range(6):
        _append(conn, native, f'{{"v": {i}}}'.encode())
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM portal_raw_payloads WHERE source_id_native = %s "
            "AND version_seq = 3", (native,))
        middle = int(cur.fetchone()[0])
    _claim_on_payload(conn, native, middle)
    _backdate(conn, native, [2, 3, 4], days=400)

    _sweep(conn, native, version_cap=2, hot_window=90)

    # v1 first, v6 latest, v3 claim-referenced — all pinned, and v3 is BOTH out of the cap
    # (rank 4 of a cap of 2) and out of the window (400 days), so it is the row that
    # proves the pin beats each removal path independently. v5 is inside the cap and
    # inside the window; v2 and v4 are outside both.
    assert _versions(conn, native) == [1, 3, 5, 6]
    assert dict(_pinned(conn, native))[3] is True


@requires_db
def test_a_disputed_body_survives_every_path(conn: psycopg.Connection) -> None:
    # P4's third pin. The control arm carries byte-identical bodies under a different
    # native id, so the sweep is also held to the writer's rule that a content address is
    # not a listing: one listing's dispute must not freeze another's history.
    from tests.location_data.test_payloads import _open_contradiction

    control, disputed = _key(), _key()
    for native in (control, disputed):
        for i in range(5):
            _append(conn, native, f'{{"v": {i}}}'.encode())
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload_sha256 FROM portal_raw_payloads WHERE source_id_native = %s "
            "AND version_seq = 2", (disputed,))
        sha = bytes(cur.fetchone()[0])
    _open_contradiction(conn, disputed, sha)
    for native in (control, disputed):
        _backdate(conn, native, [2, 3, 4], days=400)
        _sweep(conn, native, version_cap=2, hot_window=90)

    assert _versions(conn, control) == [1, 5]
    assert _versions(conn, disputed) == [1, 2, 5]
    assert dict(_pinned(conn, disputed))[2] is True


@requires_db
def test_the_sweep_reports_the_bytes_it_reclaimed(conn: psycopg.Connection) -> None:
    native = _key()
    # Well over the 4 KB gzip threshold and highly compressible, so the two figures are
    # visibly different rather than coincidentally equal.
    body = b'{"filler": "' + b"x" * 50_000 + b'"}'
    for i in range(4):
        _append(conn, native, body.replace(b"filler", f"f{i}".encode()))
    _backdate(conn, native, [2, 3], days=400)

    result = _sweep(conn, native, hot_window=90)

    assert result["cold"] == 2
    # Freed = what Postgres was holding (gzipped); uncompressed = the archive dropped.
    assert result["bytes_freed"] > 0
    assert result["bytes_uncompressed"] > 100_000
    assert result["bytes_uncompressed"] > result["bytes_freed"]


@requires_db
def test_cap_evictions_are_counted_in_the_bytes_reported(
    conn: psycopg.Connection,
) -> None:
    """The cap is the majority of a first sweep — a listing's fetch history is far deeper
    than 20 versions — and its statement returned only (id, key), so every capped row was
    invisible to the one number the storage sign-off is read from."""
    native = _key()
    body = b'{"filler": "' + b"x" * 50_000 + b'"}'
    for i in range(5):
        _append(conn, native, body.replace(b"filler", f"f{i}".encode()))

    # Nothing is cold: this is purely the count-based path.
    result = _sweep(conn, native, version_cap=2, hot_window=10_000)

    assert (result["capped"], result["cold"]) == (2, 0)
    assert result["bytes_freed"] > 0
    assert result["bytes_uncompressed"] > 100_000


@requires_db
def test_a_group_whose_only_old_body_is_its_first_version_is_not_visited(
    conn: psycopg.Connection,
) -> None:
    """Steady state for any listing tracked longer than the hot window. The first version
    is always pinned, so selecting the group would open a transaction that removes
    nothing — the discovery predicate excludes the edge pins from its cold count."""
    source, native = _isolated_source(), _key()
    for i in range(3):
        _append(conn, native, f'{{"v": {i}}}'.encode(), source=source)
    _backdate(conn, native, [1], days=400)

    stats = payload_prune.run(
        conn, source=source, version_cap=10_000, hot_window=90, key_page=10_000,
        max_seconds=None, max_groups=None, start_after_source="", start_after_native="",
        statement_timeout=60, dry_run=True, note=None)

    assert stats["keys_scanned"] == 1
    assert stats["groups_examined"] == 0
    assert _versions(conn, native) == [1, 2, 3]


@requires_db
def test_a_single_version_group_is_never_a_candidate(conn: psycopg.Connection) -> None:
    """After W2a-4's migration every backfilled page is a one-row group whose only body is
    simultaneously first and latest. Visiting all 445k to delete nothing is the difference
    between a cheap weekly sweep and an unaffordable one."""
    source, native = _isolated_source(), _key()
    _append(conn, native, b'{"v": 1}', source=source)
    _backdate(conn, native, [1], days=4_000)

    stats = payload_prune.run(
        conn, source=source, version_cap=1, hot_window=1, key_page=10_000,
        max_seconds=None, max_groups=None, start_after_source="", start_after_native="",
        statement_timeout=60, dry_run=True, note=None)

    assert stats["keys_scanned"] == 1, "the key was not in the sweep's range at all"
    assert stats["groups_examined"] == 0
    assert _versions(conn, native) == [1]


@requires_db
def test_the_full_sweep_walks_the_keyset_and_prunes_what_it_finds(
    conn: psycopg.Connection,
) -> None:
    # End to end through `run`, on a key page small enough to force three pages, so the
    # cursor's handover between pages is exercised rather than assumed.
    source = _isolated_source()
    natives = sorted(_key() for _ in range(3))
    for native in natives:
        for i in range(5):
            _append(conn, native, f'{{"v": {i}}}'.encode(), source=source)

    # One key per page — `main` clamps to MIN_KEY_PAGE, `run` takes what it is given, so
    # the handover is reachable from a test without a 50-listing fixture.
    stats = payload_prune.run(
        conn, source=source, version_cap=2, hot_window=10_000, key_page=1,
        max_seconds=None, max_groups=None, start_after_source="", start_after_native="",
        statement_timeout=60, dry_run=False, note="test")

    assert stats["outcome"] == "ok"
    assert stats["reached_end"] is True
    assert stats["group_failures"] == 0
    assert (stats["keys_scanned"], stats["groups_changed"]) == (3, 3)
    for native in natives:
        assert _versions(conn, native) == [1, 4, 5]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT outcome, row_count, resumable, scan_mode FROM location_claim_batches "
            "WHERE id = %s", (stats["batch_id"],))
        row = cur.fetchone()
    assert row[0] == "ok"
    assert row[1] >= 6
    # A weekly full sweep starts over rather than resuming, so its cursor certifies
    # nothing and must never be picked up by another lane's resume lookup.
    assert row[2] is False
    assert row[3] == "full"


# ------------------------------------------------------------------------ fakes


def _never_leased(*args: object, **kwargs: object):  # pragma: no cover - asserted unused
    raise AssertionError("a disabled lane must not reach the lease")


def _busy_lease(reached: list[str]):
    from contextlib import contextmanager

    @contextmanager
    def _held(conn: object, job_name: str, **kwargs: object):
        reached.append(job_name)
        yield False

    return _held


class _Ctx:
    def __init__(self, conn: "_RecordingConn") -> None:
        self.conn = conn

    def __enter__(self) -> "_RecordingConn":
        return self.conn

    def __exit__(self, *exc: object) -> bool:
        return False


class _Cursor:
    def __init__(self, conn: "_RecordingConn") -> None:
        self.conn = conn
        self._last = ""

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self._last = sql
        self.conn.executed.append((sql, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        if "to_regclass" in self._last:
            return ("present",) if self.conn.relations_present else (None,)
        if "enabled from location_jobs" in " ".join(self._last.split()).lower():
            return (self.conn.enabled,)
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _Tx:
    def __init__(self, conn: "_RecordingConn") -> None:
        self.conn = conn

    def __enter__(self) -> "_Tx":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _RecordingConn:
    """Records every statement. `enabled` is what `location_jobs` reports for the lane."""

    def __init__(self, enabled: bool, relations_present: bool = True) -> None:
        self.executed: list[tuple[str, object]] = []
        self.enabled = enabled
        self.relations_present = relations_present

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Tx:
        return _Tx(self)
