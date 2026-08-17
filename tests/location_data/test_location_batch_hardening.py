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

  1. the four lanes share ONE outer concurrency group, so they queue instead of competing;
  2. no batch statement runs without a ceiling — a wedge has to become an error that the
     existing per-row / per-unit resilience already knows how to handle.
"""

from __future__ import annotations

import inspect
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
    "location_claims_intake.yml",
    "location_mapy_inventory.yml",
    "location_resolve.yml",
    # W2a-3. Heavy in TIME rather than in rows — the probe holds one portal for ~10
    # minutes per 200 listings and the readout aggregates the whole instrument — but the
    # rails are the same ones, and a probe overlapping a claims intake would put the
    # scrape's egress and a corpus-wide sweep on the instance at once.
    "location_payload_churn.yml",
    # W2a-4. The heaviest single read the program performs — 445,191 detoasted bodies out
    # of a 14 GB table and back in gzipped — so it queues behind the other lanes rather
    # than putting that IO on the instance alongside a registry COPY.
    "location_payload_backfill.yml",
    # W2a-5. The only member with a real `schedule`, so it is also the only one that can
    # arrive unannounced: a weekly sweep of the whole payload archive landing on top of a
    # monthly registry baseline is exactly the overlap the outer group exists to prevent.
    "location_payload_prune.yml",
    # W3. A one-pass backfill over 1,574,313 `listing_snapshots` rows sharing the SAME
    # instance a claims intake or a registry load hits — exactly the corpus-wide-sweep
    # collision the outer group exists to serialize away.
    "location_claims_remine.yml",
)
OUTER_GROUP = "location-batch"


def _workflow(name: str) -> dict:
    return yaml.safe_load((_WORKFLOWS / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 1. serialization


@pytest.mark.parametrize("name", LOCATION_BATCH_WORKFLOWS)
def test_every_location_batch_lane_is_in_the_shared_outer_group(name: str):
    """One group across all four, so at most one heavy lane runs at a time."""
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
    assert start.count("with _bounded(conn, batch_timeout_s)") >= 2, (
        "the run-start constant loads must be bounded: a run that hangs there logs "
        "nothing at all"
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
    source = inspect.getsource(claims_intake.run)
    for sql in ("_ACTIVE_CONTRACT_SQL", "_WATERMARK_SQL"):
        opener = source.split(f"cur.execute({sql}")[0].rstrip().splitlines()[-1].strip()
        assert opener == "with guarded(conn, statement_timeout) as cur:", (
            f"{sql} is not read inside a guarded transaction (opener was {opener!r})"
        )


def test_the_intake_batch_budget_is_env_overridable(monkeypatch):
    """The CLI default is resolved from the env at parse time, so a lane can be widened
    without a deploy."""
    assert (
        "loader_db.env_timeout_s(STATEMENT_TIMEOUT_ENV, DEFAULT_STATEMENT_TIMEOUT_S)"
        in " ".join(inspect.getsource(claims_intake.main).split())
    )
    monkeypatch.setenv(claims_intake.STATEMENT_TIMEOUT_ENV, "120")
    assert loader_db.env_timeout_s(
        claims_intake.STATEMENT_TIMEOUT_ENV, claims_intake.DEFAULT_STATEMENT_TIMEOUT_S
    ) == 120


# ---------------------------------------------------------------- 6. registry lookups


def _flat(sql: str) -> str:
    return " ".join(sql.split()).lower()


def test_address_point_lookups_address_the_indexed_column():
    """`ruian_ap_obec_hn` is (obec_unit_id, cislo_domovni) and `ruian_ap_cast_obce` is
    (cast_obce_unit_id); `obec_kod` / `cast_obce_kod` have no index at all. Measured on
    the live 3,020,222-row mirror: 21,494 ms -> 25 ms and 5,059 ms -> 35 ms."""
    by_number = _flat(resolve_db._ADDRESS_POINTS_BY_NUMBER_SQL)
    assert "ap.obec_unit_id in (select u.id from ruian_admin_units u" in by_number
    assert "ap.obec_kod = %s" not in by_number

    extent = _flat(resolve_db._CAST_OBCE_EXTENT_SQL)
    assert "ap.cast_obce_unit_id in (" in extent
    assert "ap.cast_obce_kod = %s" not in extent


def test_the_unit_id_hop_does_not_narrow_the_answer_to_open_scd2_rows():
    """`ruian_admin_units` is SCD-2. An address point points at the unit row that was
    current when it was loaded, so restricting the code->id hop to `valid_to IS NULL`
    would return nothing for rows whose unit has since been superseded — strictly less
    than `obec_kod = %s` matched."""
    for sql in (resolve_db._ADDRESS_POINTS_BY_NUMBER_SQL, resolve_db._CAST_OBCE_EXTENT_SQL):
        hop = _flat(sql).split("from ruian_admin_units u", 1)[1].split(")", 1)[0]
        assert "valid_to" not in hop, f"the unit hop narrows to open rows only: {hop!r}"


def test_the_level_predicate_casts_the_parameter_not_the_column():
    """`u.level::text = %s` casts the COLUMN and throws away the leading column of
    `ruian_admin_units_code (level, code)`; `u.level = %s::ruian_level` casts the
    parameter and keeps both."""
    for sql in (resolve_db._ADMIN_BY_CODE_SQL, resolve_db._ADDRESS_POINTS_BY_NUMBER_SQL,
                resolve_db._CAST_OBCE_EXTENT_SQL):
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
    """The five coordinate-keyed questions have ONE statement each, taking parallel arrays,
    so `warm_points` (whole slice, one round trip) and the lazy single-point path cannot
    drift — which is what makes warming invisible to the pure core. A placeholder mismatch
    here is a runtime error on the first coordinate the drain resolves."""
    for sql, expected in (
        (resolve_db._CONTAINING_OBEC_SQL, 5),      # 3 arrays + a version per branch
        (resolve_db._NEAREST_OBEC_SQL, 9),         # 3 arrays + (version, box, radius) x2
        (resolve_db._CAST_OBCE_FOR_POINT_SQL, 5),  # 3 arrays + box + radius
        (resolve_db._IN_CZ_SQL, 4),                # 3 arrays + a version
        (resolve_db._BOUNDARY_DISTANCE_SQL, 5),    # 4 arrays + a version
    ):
        assert sql.count("%s") == expected, _flat(sql)
    for name in ("containing_obec", "cast_obce_for_point", "in_czechia_polygon"):
        single = inspect.getsource(getattr(resolve_db.SqlRegistryView, name))
        assert f"self.{name}_bulk([(lat, lon)]).get(0)" in single


def test_the_geography_predicates_carry_an_index_usable_bbox():
    """`ST_DWithin(geom::geography, ...)` is a FILTER against a geometry GiST index, so the
    unbounded forms scanned everything: 6,752 ms/point for `nearest_obec_within` (every
    boundary row cast to geography, the state polygon included) and 2,944 ms/point for
    `cast_obce_for_point` (a KNN walk of all 3.02 M address points when nothing is near).
    The `&&` box is the Index Cond that bounds both; 60,000 under-estimates metres per
    degree at CZ latitudes, so the box strictly CONTAINS the geodesic circle."""
    for sql in (resolve_db._NEAREST_OBEC_SQL, resolve_db._CAST_OBCE_FOR_POINT_SQL):
        flat = _flat(sql)
        assert "&& st_expand(" in flat, flat
        assert "/ 60000.0)" in flat, flat
        assert "st_dwithin(" in flat, flat


def test_nearest_obec_prefers_the_subdivided_pieces_like_containment_does():
    """The pip pieces TILE the authoritative polygon, so the minimum distance over them IS
    the distance to the polygon — and they are small enough that the geography cast is
    cheap (2.95 ms/point vs 6,752). The authoritative branch stays for a partially loaded
    boundary pack, exactly as in `_CONTAINING_OBEC_SQL`."""
    flat = _flat(resolve_db._NEAREST_OBEC_SQL)
    assert "g.purpose = 'pip'" in flat
    assert "g.purpose = 'authoritative'" in flat
    assert flat.count("union all") == 1


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
