"""Rails from the 2026-08-10 location-batch incident.

That night four heavy location lanes ran at once against the shared 75 GB production
instance: a RUIAN baseline (3 M-row COPY), the boundary pack (per-unit PostGIS), a Mapy
inventory scan and a full-corpus claims intake, with the resolve drain on its cadence.
Backends dropped ("SSL connection has been closed unexpectedly", one AdminShutdown), the
live Browse rebuild degraded to multi-minute DataFileReads, and TWO lanes wedged with no
error at all:

  * boundary pack (run 31434818469) — 2 h 04 min of silence inside OBCE_P, killed by hand;
  * resolve drain (run 31439340945) — 30 min of silence mid-batch, killed by the job ceiling.

Two rails answer that, and this file is the gate on both:

  1. the lanes share ONE outer concurrency group, so they queue instead of competing;
  2. no batch statement runs without a ceiling — a wedge has to become an error that the
     existing per-row / per-unit resilience already knows how to handle.

AMENDED 2026-09-10 (operator decision): the RESOLVE DRAIN left the group. Rail 1 assumed a
skipped tick is free, which held while the queue was a few hundred rows per tick. It stopped
holding when the W2-13 archived-HTML sweeps began self-chaining ~55-minute runs back to back
against a queue above 100k: the drain resolves 0.7 listings/s and got zero ticks in three
hours. The drain is also the one member that is latency-bound rather than instance-bound
(11 small indexed reads + one projection write per listing — no COPY, no corpus scan, no
detoast), so it contributed least to the incident and lost most to the queueing. It READS
the claim spine that the intake and the archive sweep WRITE, so it never carried their
"must never overlap" constraint. Its guards are now the job-level `location-resolve` group
and the `location_jobs` lease CAS; `test_the_resolve_drain_is_out_of_the_shared_group` below
is the rail that keeps it out.

AMENDED 2026-09-12 (W1-a3): a member SELF-CHAINS again. The hourly cron fires ~7 times a day
and a contract bump leaves a 250 000-body backlog, so the intake dispatches one successor
while it has work left. The ban this file used to hold became the yield it always named as
the fix — `test_a_self_chaining_member_yields_to_every_other_member_first`, plus seven tests
that execute the chain script itself against a stub `gh`, because a yield that is wrong shows
up only as somebody else's cancelled run hours later.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
from pathlib import Path

import psycopg
import pytest
import yaml

from location_data import claims_intake, loader_db, ruian_boundaries as rb, ruian_load
from location_data.resolver import drain, resolve_db

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _ROOT / ".github" / "workflows"

# Every heavy location batch lane. A new one belongs in this tuple AND in the group.
LOCATION_BATCH_WORKFLOWS = (
    "location_registry_load.yml",
    # THE claim lane (rule 25 W1-a): one hourly pass over `listings.raw_json` AND the
    # stored page body, so it carries what the four deleted lanes carried — a corpus scan,
    # an R2 fan-out and the claim writes in one transaction.
    "location_claims_intake.yml",
)
OUTER_GROUP = "location-batch"


def _workflow(name: str) -> dict:
    return yaml.safe_load((_WORKFLOWS / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 1. serialization


@pytest.mark.parametrize("name", LOCATION_BATCH_WORKFLOWS)
def test_every_location_batch_lane_is_in_the_shared_outer_group(name: str):
    """One group across every lane, so at most one heavy lane runs at a time."""
    wf = _workflow(name)
    concurrency = wf.get("concurrency")
    assert concurrency, f"{name}: no workflow-level concurrency block"
    assert concurrency["group"] == OUTER_GROUP, (
        f"{name}: workflow-level group is {concurrency['group']!r}, not {OUTER_GROUP!r} — "
        "the per-lane group belongs on the JOB, the cross-lane one on the workflow"
    )


@pytest.mark.parametrize("name", LOCATION_BATCH_WORKFLOWS)
def test_the_shared_group_never_cancels_a_lane_in_flight(name: str):
    """A cancelled COPY / batch / unit loop is work thrown away, and for the registry
    lanes it is work that has to be redone from a checkpoint. Queue, never pre-empt."""
    assert _workflow(name)["concurrency"]["cancel-in-progress"] is False


@pytest.mark.parametrize("name", LOCATION_BATCH_WORKFLOWS)
def test_the_per_lane_group_survives_as_a_job_level_group(name: str):
    """The outer group stops CROSS-lane overlap; each lane still needs its own guard
    against overlapping ITSELF (two intakes fighting one watermark, two inventory runs
    fighting one keyset cursor, a boundary pack overlapping a baseline)."""
    jobs = _workflow(name)["jobs"]
    inner = {
        job["concurrency"]["group"]
        for job in jobs.values()
        if isinstance(job.get("concurrency"), dict)
    }
    assert inner, f"{name}: no job-level concurrency group — the per-lane guard was lost"
    assert OUTER_GROUP not in inner, (
        f"{name}: the job-level group repeats {OUTER_GROUP!r}, which serializes nothing "
        "extra; it must be the lane's own group"
    )


def test_the_four_lanes_are_the_only_members_of_the_group():
    """A workflow that joins `location-batch` without being a heavy location lane would
    queue behind a 3-hour registry load for no reason."""
    members = {
        path.name
        for path in sorted(_WORKFLOWS.glob("*.yml"))
        if (yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        .get("concurrency", {})
        .get("group") == OUTER_GROUP
    }
    assert members == set(LOCATION_BATCH_WORKFLOWS)


def test_the_resolve_drain_is_out_of_the_shared_group():
    """The 2026-09-10 amendment, as a rail rather than a comment.

    Re-adding the drain to `location-batch` would re-starve it behind the self-chaining
    archive sweeps — the failure this reverses. Its own two guards have to stay: the
    per-lane job group (never overlaps itself) and, in the drain, the `location_jobs`
    lease CAS (never overlaps the Railway worker lane)."""
    wf = _workflow("location_resolve.yml")
    group = (wf.get("concurrency") or {}).get("group", "")
    # Mode-conditional, not absent: `full-resolve` opens with a corpus-wide bulk INSERT into
    # dirty_locations and MUST still queue with the heavy lanes — on 2026-09-10 it overlapped
    # the archive sweep, which bulk-inserts the same keys, and killed it with
    # LockNotAvailable (run 34456211996). The drain must NOT be grouped that way.
    assert "github.event.inputs.mode == 'full-resolve'" in group, (
        "the resolve lane's group is no longer mode-conditional — a full-resolve enqueue "
        "running beside an archive sweep deadlocks it out of dirty_locations"
    )
    assert f"'{OUTER_GROUP}'" in group, "full-resolve must still land in the heavy-lane group"
    assert group.split("||")[-1].strip().strip("}").strip() != f"'{OUTER_GROUP}'", (
        "the drain branch of the group expression is the heavy-lane group — that re-starves it"
    )
    inner = {
        job["concurrency"]["group"]
        for job in wf["jobs"].values()
        if isinstance(job.get("concurrency"), dict)
    }
    assert inner == {"location-resolve"}, (
        f"the drain's per-lane guard is {inner!r} — without it two ticks overlap"
    )
    assert "lease" in inspect.getsource(drain.main), (
        "drain.main no longer takes the location_jobs lease — with the outer group gone "
        "that CAS is the only thing keeping a second drainer out"
    )


# ---------------------------------------------------------------- 2. loader_db helpers


def test_env_timeout_s_never_yields_an_unbounded_or_broken_budget(monkeypatch):
    """0 means "no timeout" to Postgres — the exact state this mechanism exists to stop —
    so it is not reachable from an env var, and neither is a typo."""
    assert loader_db.env_timeout_s("LOCATION_TEST_TIMEOUT_S", 90) == 90
    for bad in ("0", "-5", "", "abc", "90s"):
        monkeypatch.setenv("LOCATION_TEST_TIMEOUT_S", bad)
        assert loader_db.env_timeout_s("LOCATION_TEST_TIMEOUT_S", 90) == 90
    monkeypatch.setenv("LOCATION_TEST_TIMEOUT_S", "45")
    assert loader_db.env_timeout_s("LOCATION_TEST_TIMEOUT_S", 90) == 45


def test_bounded_sets_transaction_local_timeouts_not_session_ones():
    """`SET LOCAL` scope is the whole point: the loader session runs
    statement_timeout = 0 for COPY, and a session-level SET here would clamp the next
    bulk phase instead of just this one."""
    conn = _RecordingConn()
    with loader_db.bounded(conn, 180) as cur:
        cur.execute("SELECT 1")
    guard, params = conn.executed[0]
    assert "set_config('statement_timeout'" in guard
    assert "set_config('lock_timeout'" in guard
    # third argument of both set_config calls == is_local
    assert guard.count("true") == 2
    assert params == {"statement_timeout": "180s", "lock_timeout": "5s"}
    assert conn.transactions == 1, "the guard must be inside a transaction or it no-ops"


# ---------------------------------------------------------------- 3. boundary loader


def test_the_per_unit_transaction_arms_a_bounded_statement_timeout(monkeypatch):
    """The wedge site. Run 31434818469 sat inside OBCE_P for 2 h 04 min under the
    session's statement_timeout = 0, with lock_timeout = 5 s proving it was not a lock
    wait and the libpq keepalives proving the socket was alive: a busy backend inside one
    per-unit PostGIS statement, which nothing could interrupt."""
    conn = _BoundaryConn(unit_id=7)
    layer = next(x for x in rb.LAYERS if x.token == "OBCE_P")
    rb.load_feature(conn, _feature("obec", 554782), layer, 3, with_pip=True)

    guarded = conn.executed[conn.first_in_transaction]
    assert "set_config('statement_timeout'" in guarded[0]
    assert guarded[1]["statement_timeout"] == f"{rb.DEFAULT_UNIT_TIMEOUT_S}s"
    # and it is armed BEFORE any geometry statement, not after
    geometry_at = next(
        i for i, (sql, _) in enumerate(conn.executed)
        if "INSERT INTO ruian_admin_unit_geometries" in sql
    )
    assert conn.first_in_transaction < geometry_at


def test_the_per_unit_budget_is_env_overridable(monkeypatch):
    monkeypatch.setenv(rb.UNIT_TIMEOUT_ENV, "45")
    conn = _BoundaryConn(unit_id=7)
    layer = next(x for x in rb.LAYERS if x.token == "OBCE_P")
    rb.load_feature(conn, _feature("obec", 1), layer, 3, with_pip=False)
    assert conn.executed[conn.first_in_transaction][1]["statement_timeout"] == "45s"


def test_the_name_upgrade_shares_the_units_guarded_transaction():
    """`upgrade_name` is part of the unit's transaction; opening its own cursor outside
    the guard would leave it as the one statement that can still hang."""
    assert list(inspect.signature(rb.upgrade_name).parameters) == ["cur", "unit_id", "name"]
    assert "conn.cursor()" not in inspect.getsource(rb.upgrade_name)
    source = inspect.getsource(rb.load_feature)
    assert "with loader_db.bounded(conn, budget) as cur:" in source
    assert "upgraded = upgrade_name(cur, unit_id, feature.name)" in source


def test_a_timed_out_unit_is_a_data_fault_and_never_spends_the_reconnect_budget():
    """`db.is_transient_db_error` answers True for EVERY OperationalError and
    QueryCanceled is one, so without the split a pathological geometry would burn two
    reconnects, and ~20 of them would abort a 253 MB pack with "reconnects exhausted" —
    blaming the environment for the data."""
    assert rb._is_unit_fault(psycopg.errors.QueryCanceled("canceling statement"))
    assert not rb._is_unit_fault(psycopg.OperationalError("SSL connection has been closed"))

    reconnects: list[int] = []

    def _never_called() -> object:  # pragma: no cover - asserted not to run
        reconnects.append(1)
        raise AssertionError("a statement timeout must not trigger a reconnect")

    calls: list[int] = []

    def _timeout(conn, feature, layer, version_id, *, with_pip, unit_timeout_s=None):
        calls.append(1)
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    original = rb.load_feature
    rb.load_feature = _timeout
    try:
        loaded, upgraded, conn, error = rb.load_feature_resilient(
            "conn", _feature("obec", 1),
            next(x for x in rb.LAYERS if x.token == "OBCE_P"), 3,
            with_pip=True, reconnector=rb.Reconnector(_never_called),
        )
    finally:
        rb.load_feature = original

    assert (loaded, upgraded) == (False, False)
    assert isinstance(error, psycopg.errors.QueryCanceled)
    assert calls == [1], "the unit is attempted once, not retried on a fresh session"
    assert reconnects == []


def test_the_unit_loop_emits_a_heartbeat_so_silence_is_diagnosable():
    """The whole diagnosis of run 31434818469 was "two hours of no output": the only
    per-unit lines the loader had were failure lines, so healthy-but-slow and wedged
    looked identical. The heartbeat names the code it is about to load."""
    source = inspect.getsource(rb.load_layers)
    assert "PROGRESS_EVERY" in source
    assert "BOUNDARY layer=%s at code=%s" in source
    assert rb.PROGRESS_EVERY > 0


# ---------------------------------------------------------------- 4. resolve drain


def test_the_batch_guard_is_set_local_and_env_overridable(monkeypatch):
    statements = drain._batch_guc(drain.DEFAULT_BATCH_TIMEOUT_S)
    assert statements[0] == "SET LOCAL statement_timeout = '30s'"
    assert statements[1] == f"SET LOCAL lock_timeout = '{drain.LOCK_TIMEOUT_S}s'"
    assert drain._batch_timeout_s() == drain.DEFAULT_BATCH_TIMEOUT_S
    monkeypatch.setenv(drain.BATCH_TIMEOUT_ENV, "90")
    assert drain._batch_timeout_s() == 90
    assert drain._batch_guc(90)[0] == "SET LOCAL statement_timeout = '90s'"


def test_every_statement_the_drain_runs_outside_a_batch_is_bounded_too():
    """The batch transaction already had a ceiling on 2026-08-10 — what did NOT was
    everything the loop runs between batches on the autocommit connection, where
    `SET LOCAL` from the previous transaction is long gone."""
    for fn in (drain._queue_health, drain.enqueue_full_sweep):
        assert "_bounded(" in inspect.getsource(fn), f"{fn.__name__} runs unbounded"
    start = inspect.getsource(drain.run)
    # ONE run-start read survives W2-a (which registry version is current); the five policy
    # loads and the epoch probe went with their tables. It still has to be bounded — a run
    # that hangs on its first statement logs nothing at all.
    assert start.count("with _bounded(conn, batch_timeout_s)") >= 1, (
        "the run-start constant load must be bounded"
    )


def test_the_sweep_gets_its_own_much_larger_budget():
    """One corpus-wide anti-join is honestly minutes of work, so the per-batch ceiling
    would fail it every time — but "minutes" is not "forever"."""
    assert drain.DEFAULT_SWEEP_TIMEOUT_S > drain.DEFAULT_BATCH_TIMEOUT_S * 10
    assert "SWEEP_TIMEOUT_ENV" in inspect.getsource(drain.enqueue_full_sweep)


def test_bounded_opens_a_transaction_so_set_local_is_not_a_no_op():
    """`db.connect_session()` is autocommit; outside a transaction `SET LOCAL` silently
    applies to nothing, which is the failure this helper exists to prevent."""
    source = inspect.getsource(drain._bounded)
    assert "with conn.transaction():" in source
    assert "_batch_guc(seconds)" in source


# ---------------------------------------------------------------- 5. claims intake


def test_the_failure_stamp_is_guarded_and_never_masks_the_real_exception():
    """Whatever broke the run may be the same pressure that hangs this one-row UPDATE.
    A bookkeeping write must not replace the exception the operator needs — the lesson
    `loader_db.record_discrepancy` already carries."""
    source = inspect.getsource(claims_intake.run)
    assert "with guarded(conn, _FAILURE_STAMP_TIMEOUT_S) as cur:" in source
    assert "INTAKE could not stamp batch" in source
    assert claims_intake._FAILURE_STAMP_TIMEOUT_S < claims_intake.DEFAULT_STATEMENT_TIMEOUT_S


def test_the_intake_preflight_reads_are_bounded_too():
    """A run that hangs before its first batch row exists leaves nothing to diagnose.
    Each preflight read must be the FIRST statement of a guarded block, not a bare
    `conn.cursor()` on the autocommit connection."""
    sources = {
        "_ACTIVE_CONTRACT_SQL": inspect.getsource(claims_intake.run),
        # W1-a2 replaced the watermark read with the cutover seed, in its own helper —
        # two reads, two guarded blocks, neither able to hang the run before its first
        # batch row exists.
        "_LEGACY_WATERMARK_SQL": inspect.getsource(claims_intake._snapshot_seed),
        "_SNAPSHOT_SEED_SQL": inspect.getsource(claims_intake._snapshot_seed),
        # ... and added one more: the drain's window and its backlog readout. (W1-a4 split
        # the selection in two; the WINDOW is the statement that opens the block.)
        "_UNMINED_WINDOW_SQL": inspect.getsource(claims_intake.drain_unmined_bodies),
        "_UNMINED_BODY_BACKLOG_SQL": inspect.getsource(
            claims_intake._unmined_body_backlog),
    }
    for sql, source in sources.items():
        opener = source.split(f"cur.execute({sql}")[0].rstrip().splitlines()[-1].strip()
        assert opener == "with guarded(conn, statement_timeout) as cur:", (
            f"{sql} is not read inside a guarded transaction (opener was {opener!r})"
        )
    # And the drain's SECOND statement rides the SAME transaction — a window whose ids were
    # resolved by a later, separate transaction could see a listing delisted in between.
    drain = inspect.getsource(claims_intake.drain_unmined_bodies)
    between = drain.split("cur.execute(_UNMINED_WINDOW_SQL")[1].split(
        "cur.execute(_UNMINED_BODIES_SQL")[0]
    assert "with guarded(" not in between, (
        "the window and the join statement must share one guarded transaction"
    )


def test_the_intake_batch_budget_is_env_overridable(monkeypatch):
    """The CLI default is resolved from the env at parse time, so a lane can be widened
    without a deploy."""
    assert (
        "loader_db.env_timeout_s(STATEMENT_TIMEOUT_ENV, DEFAULT_STATEMENT_TIMEOUT_S)"
        in " ".join(inspect.getsource(claims_intake.build_parser).split())
    )
    monkeypatch.setenv(claims_intake.STATEMENT_TIMEOUT_ENV, "120")
    assert loader_db.env_timeout_s(
        claims_intake.STATEMENT_TIMEOUT_ENV, claims_intake.DEFAULT_STATEMENT_TIMEOUT_S
    ) == 120


# ---------------------------------------------------------------- 6. registry lookups


def _flat(sql: str) -> str:
    return " ".join(sql.split()).lower()


def test_address_point_lookups_address_the_indexed_column():
    """`ruian_ap_obec_hn` is (obec_unit_id, cislo_domovni); `obec_kod` has no index at all.
    Measured on the live 3,020,222-row mirror: 21,494 ms -> 25 ms. (The `cast_obce_kod`
    half of this lesson went with `_CAST_OBCE_EXTENT_SQL` in W2-a — the question had no
    reader left.)"""
    by_number = _flat(resolve_db._ADDRESS_POINTS_BY_NUMBER_SQL)
    assert "ap.obec_unit_id in (select u.id from ruian_admin_units u" in by_number
    assert "ap.obec_kod = %s" not in by_number


def test_the_unit_id_hop_does_not_narrow_the_answer_to_open_scd2_rows():
    """`ruian_admin_units` is SCD-2. An address point points at the unit row that was
    current when it was loaded, so restricting the code->id hop to `valid_to IS NULL`
    would return nothing for rows whose unit has since been superseded — strictly less
    than `obec_kod = %s` matched."""
    hop = _flat(resolve_db._ADDRESS_POINTS_BY_NUMBER_SQL).split(
        "from ruian_admin_units u", 1)[1].split(")", 1)[0]
    assert "valid_to" not in hop, f"the unit hop narrows to open rows only: {hop!r}"


def test_the_level_predicate_casts_the_parameter_not_the_column():
    """`u.level::text = %s` casts the COLUMN and throws away the leading column of
    `ruian_admin_units_code (level, code)`; `u.level = %s::ruian_level` casts the
    parameter and keeps both."""
    for sql in (resolve_db._ADMIN_CHAIN_BY_CODE_SQL,
                resolve_db._ADDRESS_POINTS_BY_NUMBER_SQL):
        flat = _flat(sql)
        assert "u.level::text = %s" not in flat
        if "ruian_admin_units u" in flat:
            assert "u.level = " in flat


def test_containing_obec_lets_the_partial_pip_index_do_its_job():
    """`purpose IN ('pip','authoritative')` cannot use `ruian_aug_pip_gist ... WHERE
    purpose = 'pip'`, so ST_Covers ran against raw obec polygons (194 ms, 1,225 buffers).
    Two branches under one LIMIT keep the same preference and let Append stop at the
    first row (0.24 ms/point measured across a 50-point batch)."""
    flat = _flat(resolve_db._CONTAINING_OBEC_SQL)
    assert "purpose in ('pip', 'authoritative')" not in flat
    assert "g.purpose = 'pip'" in flat
    assert "g.purpose = 'authoritative'" in flat
    assert flat.count("union all") == 1


def test_every_point_keyed_question_binds_one_array_shape():
    """The coordinate-keyed questions have ONE statement each, taking parallel arrays,
    so `warm_points` (whole slice, one round trip) and the lazy single-point path cannot
    drift — which is what makes warming invisible to the pure core. A placeholder mismatch
    here is a runtime error on the first coordinate the drain resolves."""
    for sql, expected in (
        (resolve_db._CONTAINING_OBEC_SQL, 5),      # 3 arrays + a version per branch
        (resolve_db._NEAREST_OBEC_SQL, 9),         # 3 arrays + (version, box, radius) x2
        (resolve_db._IN_CZ_SQL, 4),                # 3 arrays + a version
    ):
        assert sql.count("%s") == expected, _flat(sql)
    for name in ("containing_obec", "in_czechia_polygon"):
        single = inspect.getsource(getattr(resolve_db.SqlRegistryView, name))
        assert f"self.{name}_bulk([(lat, lon)]).get(0)" in single


def test_the_geography_predicate_carries_an_index_usable_bbox():
    """`ST_DWithin(geom::geography, ...)` is a FILTER against a geometry GiST index, so the
    unbounded form scanned everything: 6,752 ms/point for `nearest_obec_within`, every
    boundary row cast to geography, the state polygon included. The `&&` box is the Index
    Cond that bounds it; 60,000 under-estimates metres per degree at CZ latitudes, so the box
    strictly CONTAINS the geodesic circle and cannot hide a row the exact `ST_DWithin` kept.

    (`cast_obce_for_point` carried the same lesson and went with the question in W2-a — FILL
    takes the quarter off the bound entity's own chain now.)"""
    flat = _flat(resolve_db._NEAREST_OBEC_SQL)
    assert "&& st_expand(" in flat, flat
    assert "/ 60000.0)" in flat, flat
    assert "st_dwithin(" in flat, flat


def test_nearest_obec_prefers_the_subdivided_pieces_like_containment_does():
    """The pip pieces TILE the authoritative polygon, so the minimum distance over them IS
    the distance to the polygon — and they are small enough that the geography cast is cheap
    (2.95 ms/point vs 6,752). The authoritative branch stays for a partially loaded boundary
    pack, exactly as in `_CONTAINING_OBEC_SQL`."""
    flat = _flat(resolve_db._NEAREST_OBEC_SQL)
    assert "g.purpose = 'pip'" in flat
    assert "g.purpose = 'authoritative'" in flat
    assert flat.count("union all") == 1


def test_the_sliver_fallback_is_asked_lazily_not_warmed():
    """It is reached only when `containing_obec` missed — ~1 % of listings — so warming it
    would run a two-branch geography lateral for all 250 of a slice's points to answer the
    two or three that ask. The other two point-keyed questions ARE warmed."""
    warm = inspect.getsource(resolve_db.warm_points)
    assert "containing_obec_bulk" in warm and "in_czechia_polygon_bulk" in warm
    assert "nearest_obec" not in warm


def test_the_registry_lookup_index_migration_ships_as_a_file():
    """Additive-only, and deliberately NOT applied with the code: two of the three build
    over 3.02 M rows and want a quiet instance."""
    path = _ROOT / "migrations" / "389_location_w1_registry_lookup_indexes.sql"
    sql = path.read_text(encoding="utf-8").lower()
    assert "set local lock_timeout" in sql
    assert "set local statement_timeout" in sql
    for forbidden in ("drop ", "alter table", "truncate", "delete from"):
        assert forbidden not in sql, f"389 is additive; found {forbidden!r}"
    assert sql.count("create index if not exists") == 3


# ---------------------------------------------------------------- fakes


def _feature(level: str, code: int) -> rb.BoundaryFeature:
    return rb.BoundaryFeature(level=level, code=code, name=f"unit {code}", wkb=b"")


class _Cursor:
    def __init__(self, conn: "_RecordingConn") -> None:
        self.conn = conn
        self.rowcount = 1

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self.conn.executed.append((sql, params))

    def fetchone(self):
        return (self.conn.result,)

    def fetchall(self):
        return []


class _Tx:
    def __init__(self, conn: "_RecordingConn") -> None:
        self.conn = conn

    def __enter__(self) -> "_Tx":
        self.conn.transactions += 1
        self.conn.depth += 1
        return self

    def __exit__(self, *exc: object) -> bool:
        self.conn.depth -= 1
        return False


class _RecordingConn:
    def __init__(self, result: object = None) -> None:
        self.executed: list[tuple[str, object]] = []
        self.transactions = 0
        self.depth = 0
        self.result = result

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Tx:
        return _Tx(self)


class _BoundaryConn(_RecordingConn):
    """`unit_id_for` runs BEFORE the guarded transaction (it is the lookup that decides
    whether there is a unit at all), so the fake records where the transaction starts."""

    def __init__(self, unit_id: int) -> None:
        super().__init__(result=unit_id)
        self.first_in_transaction = -1

    def cursor(self) -> _Cursor:
        return _GuardAwareCursor(self)


class _GuardAwareCursor(_Cursor):
    def execute(self, sql: str, params: object = None) -> None:
        conn: _BoundaryConn = self.conn  # type: ignore[assignment]
        if conn.depth and conn.first_in_transaction < 0:
            conn.first_in_transaction = len(conn.executed)
        super().execute(sql, params)

# --------------------------------------------- 4. the intake's chain, as YAML and as shell

CHAIN_STEP = "Chain the next run"
INTAKE = "location_claims_intake.yml"


def _chain_step() -> dict:
    steps = _workflow(INTAKE)["jobs"]["intake"]["steps"]
    return next(s for s in steps if s.get("name") == CHAIN_STEP)


def _chain_script() -> str:
    """The chain step's shell, verbatim. It carries no `${{ }}`, which is what makes the
    step's DECISION testable rather than only its text: everything it reads is an env var,
    so the tests below run this exact script against a stub `gh`."""
    step = _chain_step()
    assert "${{" not in step["run"], (
        "the chain script interpolates a GitHub expression; keep it env-var-only so the "
        "yield can be tested")
    return step["run"]


def _group_members() -> set[str]:
    """Every workflow that can hold `location-batch`, read off the workflows themselves.

    NOT `LOCATION_BATCH_WORKFLOWS`, and the difference is the whole point of this helper:
    `location_resolve.yml` joins the group through a mode-conditional EXPRESSION (a
    full-resolve dispatch, or its own 03:17 cron), so a test that compares group membership
    by equality — as `test_the_four_lanes_are_the_only_members_of_the_group` does, correctly,
    for the permanent members — cannot see it. The chain must yield to it all the same: a
    full-resolve is one of the two runs a chain hop evicted on 2026-09-10.
    """
    members = set()
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        group = ((yaml.safe_load(path.read_text(encoding="utf-8")) or {})
                 .get("concurrency") or {}).get("group", "")
        if OUTER_GROUP in str(group):
            members.add(path.name)
    return members


def test_a_self_chaining_member_yields_to_every_other_member_first():
    """The one pending slot of `location-batch` is a trap for a self-chaining lane: GitHub
    supersedes the OLDER pending run, so a chain hop dispatched in the last seconds evicts
    whatever was waiting. 2026-09-10: the hourly intake (10:00Z) and the corpus-wide
    full-resolve (12:15Z) were both cancelled that way, and the fix was a yield step in the
    chaining lane. Rule 25 W1-a then removed the only self-chaining member outright.

    W1-a3 REINTRODUCES ONE, deliberately: GitHub fires the hourly cron ~7 times a day, and a
    contract bump leaves a 250 000-body backlog that drains at ~6 000 bodies a fired tick.
    The trap is answered by the yield rather than by the ban — so the rail is now the yield
    itself, and it is checked INSIDE the chain step's own script rather than anywhere in the
    file: a `gh run list` in some unrelated step would satisfy a substring search and yield
    nothing.
    """
    for name in sorted(_group_members()):
        script = (_WORKFLOWS / name).read_text(encoding="utf-8")
        if f"gh workflow run {name}" not in script and 'gh workflow run "$WORKFLOW"' not in script:
            continue
        assert name == INTAKE, (
            f"{name} re-dispatches itself; only the intake's chain has been reviewed for "
            f"the {OUTER_GROUP!r} pending-slot trap")
        chain = _chain_script()
        dispatch = chain.index('gh workflow run "$WORKFLOW"')
        yields = chain.index("gh run list")
        assert yields < dispatch, (
            "the chain dispatches its successor before asking whether anything is waiting; "
            f"a self-chaining member of {OUTER_GROUP!r} evicts whatever else is in the "
            "group's one pending slot")


def test_the_chain_asks_every_workflow_that_can_hold_the_group():
    """The list the yield walks is a hand-written env var, and the one member most easily
    left off it — `location_resolve.yml` — is the one the incident was about."""
    declared = set(_chain_step()["env"]["GROUP_WORKFLOWS"].split())

    assert declared == _group_members(), (
        f"GROUP_WORKFLOWS is {sorted(declared)}; the workflows that can hold "
        f"{OUTER_GROUP!r} are {sorted(_group_members())}"
    )


# The chain's `gh run list` calls differ only by the jq filter they pass, so a stub that
# ignored the filter would test the shell around the yield and not the yield. This one emits
# the real `--json databaseId,status,event` shape and hands it to jq, exactly as gh does —
# and answers PER WORKFLOW, because the resolve lane's rule is not the others'.
_GH_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_CALLS"
case "$*" in
  *"run list"*) ;;
  *) exit 0 ;;
esac
workflow=""
for arg in "$@"; do
  case "$arg" in --workflow=*) workflow="${arg#--workflow=}" ;; esac
done
slug=$(printf '%s' "$workflow" | tr 'a-z.-' 'A-Z__')
per_workflow="GH_RUNS_WAITING_$slug"
payload="${!per_workflow:-}"
[ -n "$payload" ] || payload="$GH_RUNS_WAITING"
case "$*" in *in_progress*) payload="$GH_RUNS_RUNNING" ;; esac
filter=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -q|--jq) filter="$2"; shift 2 ;;
    *) shift ;;
  esac
done
if command -v jq >/dev/null 2>&1; then
  printf '%s' "$payload" | jq -r "$filter"
else
  # No jq: emit every id in the payload and let the fixture be the filter. The tests that
  # need the filter ITSELF applied are skipped in this environment.
  printf '%s' "$payload" | python3 -c 'import json,sys
for run in json.load(sys.stdin):
    print(run["databaseId"])'
fi
"""

NEEDS_JQ = pytest.mark.skipif(
    shutil.which("jq") is None,
    reason="the stub applies gh's jq filter with jq; GitHub runners have it")


def _runs(*rows: tuple[int, str] | tuple[int, str, str]) -> str:
    """`gh run list --json databaseId,status,event`'s own shape. The event defaults to the
    cron tick, which is what most queued runs in this fleet are."""
    return json.dumps([
        {"databaseId": row[0], "status": row[1],
         "event": row[2] if len(row) > 2 else "schedule"}
        for row in rows
    ])


def _run_chain(tmp_path: Path, summary: dict | str, *, waiting: str = "[]",
               running: str = "[]", run_id: str = "77",
               waiting_by_workflow: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """Execute the chain script with a stub `gh`; returns (stdout, the gh calls it made)."""
    calls = tmp_path / "gh-calls.txt"
    calls.write_text("", encoding="utf-8")
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "gh").write_text(_GH_STUB, encoding="utf-8")
    (stub / "gh").chmod(0o755)
    body = summary if isinstance(summary, str) else f"INTAKE done {json.dumps(summary)}"
    (tmp_path / "intake.log").write_text(
        f"2026-09-12 05:00:00 INFO location_data.claims_intake {body}\n", encoding="utf-8")
    script = tmp_path / "chain.sh"
    script.write_text(_chain_script(), encoding="utf-8")
    # THE STEP'S OWN ENV, not a copy of it: `GROUP_WORKFLOWS` is the list the yield walks,
    # and a test that supplied its own would pass while the deployed list was short.
    step_env = {k: v for k, v in _chain_step()["env"].items() if "${{" not in str(v)}
    env = {
        "PATH": f"{stub}:{os.environ['PATH']}", "GH_CALLS": str(calls),
        "GH_RUNS_WAITING": waiting, "GH_RUNS_RUNNING": running,
        **{f"GH_RUNS_WAITING_{name.upper().replace('.', '_').replace('-', '_')}": rows
           for name, rows in (waiting_by_workflow or {}).items()},
        "GITHUB_RUN_ID": run_id, "GITHUB_REF_NAME": "main",
        "CHAIN_SOURCE": "", "CHAIN_MODE": "incremental",
        "CHAIN_MAX_SECONDS": "2400", "CHAIN_BATCH_SIZE": "10000",
        **step_env,
    }
    # `bash -e`, the runner's own shell for a `run:` block: a step that aborts on a stray
    # non-zero is a chain that silently stops, and that is a property of THIS script.
    done = subprocess.run(["bash", "-e", str(script)], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    dispatched = [line for line in calls.read_text(encoding="utf-8").splitlines()
                  if line.startswith("workflow run")]
    return done.stdout, dispatched


# `bodies_mined` / `listings` are the PROGRESS terms: work left is not enough, the run must
# also have moved the half that is unfinished.
BACKLOG = {"bodies_pass_complete": False, "bodies_mined": 1500,
           "reached_end": True, "listings": 400, "outcome": "stopped"}
DRAINED = {"bodies_pass_complete": True, "bodies_mined": 12,
           "reached_end": True, "listings": 400, "outcome": "ok"}


def test_the_chain_dispatches_one_successor_with_this_run_s_own_budget(tmp_path: Path):
    """The backlog case, which is every run for the days after a contract bump. ONE
    successor, and it inherits the budget and batch size this run actually used — a chained
    run is a `workflow_dispatch`, so it never sees the cron's defaults."""
    out, dispatched = _run_chain(tmp_path, BACKLOG)

    assert len(dispatched) == 1, out
    assert "--ref main" in dispatched[0]
    assert "-f max_seconds=2400" in dispatched[0]
    assert "-f batch_size=10000" in dispatched[0]
    assert "-f mode=incremental" in dispatched[0]
    assert "-f chain=true" in dispatched[0]


def test_the_chain_ends_when_both_halves_reached_their_end(tmp_path: Path):
    """STEADY STATE IS CRON-ONLY. A lane that chained on every run would hold the group's
    one pending slot for ever and turn an hourly lane into a permanent one."""
    out, dispatched = _run_chain(tmp_path, DRAINED)

    assert dispatched == []
    assert "chain ends" in out


def test_an_unfinished_listing_scan_chains_too(tmp_path: Path):
    """The other half's backlog. After a cron gap the snapshot window is hours wide, and a
    run that stopped on its budget mid-scan has the same claim on a successor as one that
    stopped mid-drain."""
    _, dispatched = _run_chain(tmp_path, dict(DRAINED, reached_end=False, listings=20000))

    assert len(dispatched) == 1


def test_a_run_that_mined_nothing_does_not_chain(tmp_path: Path):
    """THE ENDLESS-CHAIN DEFECT. `bodies_pass_complete` is False until the drain sets it, so
    a run with no page-capable portal (`--source sreality`) or no R2 credential used to
    report a backlog it had never looked at — and a chain keyed on that alone would dispatch
    clean short runs for ever, each holding `location-batch` on the way through. Work left
    only counts when this run moved it."""
    out, dispatched = _run_chain(
        tmp_path, dict(BACKLOG, bodies_mined=0, bodies_pass_complete=False))

    assert dispatched == []
    assert "chain ends" in out


def test_a_scan_that_opened_no_listing_does_not_chain_either(tmp_path: Path):
    """The same rail on the payload half: `reached_end` is False on a run that stopped
    before its first batch, and a successor would stop in the same place."""
    _, dispatched = _run_chain(
        tmp_path, dict(DRAINED, reached_end=False, listings=0))

    assert dispatched == []


def test_the_chain_yields_to_a_run_already_waiting_in_the_group(tmp_path: Path):
    """The 2026-09-10 eviction, as a rail. The cron tick queued behind this run is the run
    that must get the slot — the chain's work is the same work, so yielding costs the
    backlog nothing and costs the operator nothing."""
    out, dispatched = _run_chain(tmp_path, BACKLOG, waiting=_runs((4242, "queued")))

    assert dispatched == []
    assert "chain yields" in out


@NEEDS_JQ
def test_the_waiting_query_sees_a_queued_sibling_among_finished_runs(tmp_path: Path):
    """gh's own filter, applied to gh's own shape. The list a real query returns is mostly
    COMPLETED runs; a filter that let those through would make the chain yield for ever, and
    one that dropped the queued run would make it never yield at all."""
    out, dispatched = _run_chain(
        tmp_path, BACKLOG,
        waiting=_runs((10, "completed"), (11, "completed"), (4242, "queued")))

    assert dispatched == []
    assert "chain yields" in out and "4242" in out


RESOLVE = "location_resolve.yml"


def test_only_the_resolve_lane_is_counted_by_dispatch(tmp_path: Path):
    """The exception has to stay an exception. Every other member of `location-batch` is in
    it whatever fired the run; the resolve lane is the one whose group is decided per run."""
    env = _chain_step()["env"]
    dispatch_only = set(env["DISPATCH_ONLY_WORKFLOWS"].split())

    assert dispatch_only == {RESOLVE}
    assert dispatch_only <= set(env["GROUP_WORKFLOWS"].split())
    # The rule is only expressible if the query asks for the field it keys on.
    assert "--json databaseId,status,event" in _chain_script()


@NEEDS_JQ
def test_a_queued_resolve_drain_tick_does_not_stop_the_chain(tmp_path: Path):
    """THE PRODUCTION DEFECT (2026-09-12). Hop 34681906422 yielded to run 34682089264 — a
    routine 07:58 `schedule` tick of the resolve lane, mode=drain, which runs in
    `location-resolve-lane` and never touches `location-batch`. `gh run list` cannot report
    a run's group, so counting every queued resolve run meant a `*/15` cadence stopped the
    chain almost every time and handed a 212 000-body backlog back to a cron that fires ~7
    times a day."""
    out, dispatched = _run_chain(
        tmp_path, BACKLOG,
        waiting_by_workflow={RESOLVE: _runs((34682089264, "queued", "schedule"))})

    assert len(dispatched) == 1, out
    assert "chain yields" not in out


@NEEDS_JQ
def test_a_dispatched_resolve_run_still_stops_the_chain(tmp_path: Path):
    """The other half, and the reason the resolve lane is in the list at all: an operator's
    full-resolve IS in `location-batch`, and it is one of the two runs a chain hop evicted on
    2026-09-10."""
    out, dispatched = _run_chain(
        tmp_path, BACKLOG,
        waiting_by_workflow={RESOLVE: _runs((555, "queued", "workflow_dispatch"))})

    assert dispatched == []
    assert "chain yields" in out and "555" in out


@NEEDS_JQ
def test_a_scheduled_tick_of_a_permanent_member_still_stops_the_chain(tmp_path: Path):
    """The exception is ONE workflow wide. The intake's own cron tick is a `schedule` run
    that really is queued on `location-batch` — the run the chain exists to let through."""
    out, dispatched = _run_chain(
        tmp_path, BACKLOG,
        waiting_by_workflow={INTAKE: _runs((4242, "queued", "schedule"))})

    assert dispatched == []
    assert "chain yields" in out


def test_the_chain_never_stacks_on_an_intake_already_running(tmp_path: Path):
    """Two intakes share one watermark and re-read each other's window."""
    out, dispatched = _run_chain(tmp_path, BACKLOG, running=_runs((9999, "in_progress")))

    assert dispatched == []
    assert "chain yields" in out


def test_this_run_is_not_mistaken_for_a_second_intake(tmp_path: Path):
    """The in-progress query returns the CHAINING run itself — every time, since it is still
    running when it asks. Reading that as "another intake is up" would mean the chain never
    fires at all."""
    _, dispatched = _run_chain(
        tmp_path, BACKLOG, running=_runs((77, "in_progress")), run_id="77")

    assert len(dispatched) == 1


@NEEDS_JQ
def test_the_running_query_excludes_only_this_run_not_finished_ones(tmp_path: Path):
    """The two halves of that filter, together: `in_progress` selects, `grep -v` drops this
    run, and a COMPLETED sibling must survive neither — it is neither in progress nor this
    run, and treating it as a live intake would stop every chain."""
    _, dispatched = _run_chain(
        tmp_path, BACKLOG,
        running=_runs((77, "in_progress"), (10, "completed")), run_id="77")

    assert len(dispatched) == 1


def test_a_summary_the_chain_cannot_read_chains_nothing(tmp_path: Path):
    """A run whose last line is missing or malformed is a run nobody can say has work left.
    Chaining on an unreadable summary is how a lane loops on its own failure."""
    out, dispatched = _run_chain(tmp_path, "INTAKE done not-json-at-all")

    assert dispatched == []
    assert "chain ends" in out
