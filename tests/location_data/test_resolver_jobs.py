"""The resolver JOB and the seams between it and the pure core.

These are the parts a pure-core test cannot reach: what the drain claims and in what order,
what it writes, and what the sweep compares to decide a row is stale. The SQL-text assertions
are deliberate — a fake connection cannot tell you whether a query joins `listings`, and the
schema-replay job is the only other place that would notice.

W2-a's shape, and every assertion below is about one half of it: the drain's write path is
ONE statement, and the sweep's version tuple is TWO columns.
"""

from __future__ import annotations

import contextlib
import inspect
import itertools
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from location_data.resolver import core, drain, projection, resolve_db
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm

REGISTRY = "2026-07"
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
# The `enqueued_at` the fake queue hands back on a slice — the value every statement that
# finishes one of those rows is bounded by.
CLAIMED_AT = datetime(2026, 9, 12, 7, 13, tzinfo=timezone.utc)


class _FakeCursor:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.rowcount = -1
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        text = " ".join(sql.split()).lower()
        self.state["executed"].append((text, params))
        if text.startswith("select id, label from registry_versions"):
            self._result = [(7, REGISTRY)]
        elif text.startswith("select coalesce(max(id), 0) from listings"):
            self._result = [(self.state.get("max_listing_id", 0),)]
        else:
            self._result = []

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result


class _FakeConn:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.state)

    @contextmanager
    def transaction(self):
        self.state["transactions"] += 1
        yield


def _state(max_listing_id: int = 0) -> dict[str, Any]:
    return {"executed": [], "transactions": 0, "max_listing_id": max_listing_id}


# ------------------------------------------------------------------ the drain's queue


def test_the_queue_slice_has_a_unique_tiebreaker():
    """A batch enqueue shares one `now()`, so a bare `ORDER BY enqueued_at` returns a
    different order on every call and a poisonous row can be re-claimed forever while another
    starves."""
    flat = " ".join(drain._CLAIM_SLICE_SQL.split()).lower()
    assert "order by enqueued_at, listing_id" in flat


# ------------------------------------------------------------------- the one sweep


def _sweep_windows(state: dict[str, Any]) -> list[tuple[str, Any]]:
    return [(t, p) for t, p in state["executed"] if t.startswith("insert into dirty_locations")]


def test_the_sweep_compares_the_two_version_columns_on_the_new_answer_table():
    """THE cutover mechanism. `resolver:v4` in `version.py` is the whole of it: this
    statement sees every row still stamped v3 and enqueues the corpus, and the worker lane
    drains it. `policy_version` is gone from the tuple with the three policy tables — policy
    is code now, so a policy change IS a resolver-version bump."""
    flat = " ".join(drain._SWEEP_SQL.split()).lower()
    assert "from listings l" in flat and "left join listing_location p" in flat
    assert "listing_location_current" not in flat and "property_location_current" not in flat
    assert "l.is_active" in flat
    assert "p.listing_id is null" in flat
    assert "p.resolver_version <> %s" in flat
    assert "p.registry_version <> %s" in flat
    assert "policy_version" not in flat and "registry_version_id" not in flat
    assert "location_claims" not in flat and "distinct" not in flat and "kraj_kod" not in flat
    # The offline placeholder guard and the schema-aware PREPARE sweep only see module-level
    # `*_SQL` constants, so the statement is whole and prepareable.
    assert re.sub(r"%\(\w+\)s|%s", "", flat).count("%") == 0
    assert flat.count("%s") == 4


def test_the_sweep_walks_listing_id_windows_each_in_its_own_transaction():
    """One bounded transaction per window means an interrupted sweep loses at most a window
    and re-running is idempotent; the upper bound is an index probe on `listings`' key."""
    state = _state(max_listing_id=600_000)
    drain.enqueue_full_sweep(_FakeConn(state), window=250_000)
    windows = _sweep_windows(state)
    assert [(p[-2], p[-1]) for _, p in windows] == [
        (0, 250_000), (250_000, 500_000), (500_000, 600_000),
    ]
    assert all("l.id > %s and l.id <= %s" in t for t, _ in windows)
    assert all(p[0] == RESOLVER_VERSION and p[1] == REGISTRY for _, p in windows)
    # one bounded transaction per window, plus the bounded upper-bound probe
    assert state["transactions"] == len(windows) + 1
    assert any(t.startswith("select coalesce(max(id), 0) from listings") for t, _ in state["executed"])


def test_the_sweep_skips_rows_already_queued_without_touching_their_locks():
    """A drain slice is almost always in flight, holding its rows FOR UPDATE; an INSERT ...
    ON CONFLICT on one of those keys waits for that transaction and the 5 s lock_timeout kills
    the window. The NOT EXISTS is an MVCC read: skip, never wait.

    It is also the ONE producer that must not bump (W2-a2): every other enqueue is evidence
    arriving, the sweep carries none, and bumping a queued row would reset a poisoned row's
    `attempts` backoff and move the oldest row in the queue to the back of it."""
    flat = " ".join(drain._SWEEP_SQL.split()).lower()
    assert "not exists (select 1 from dirty_locations d where d.listing_id = l.id)" in flat
    assert "on conflict (listing_id) do nothing" in flat
    assert "do update" not in flat


def test_the_sweep_revisits_every_active_czech_listing_without_a_town():
    """Rule 2 / rule 25's invariant, made mechanical. A row resolved WITHOUT an `obec_kod` is
    stamped at the current version tuple, so the three version arms see a fresh row and walk
    past it for ever: on 2026-09-12 that left 384,365 rows red with nothing able to re-enqueue
    them. The fourth arm re-resolves them nightly until each has a town or is determined
    `foreign` — a determination, never a default, so `undetermined` stays in scope.

    No new placeholder: `'foreign'` is a literal, and `enqueue_full_sweep` still binds exactly
    (RESOLVER_VERSION, registry_label, after, hi)."""
    flat = " ".join(drain._SWEEP_SQL.split()).lower()
    assert "or (p.obec_kod is null and p.country_status <> 'foreign')" in flat
    assert flat.count("%s") == 4
    # ...and it is one arm of the SAME disjunction, not a second statement.
    where = flat[flat.index("where l.is_active"):]
    assert where.index("p.obec_kod is null") < where.index("and l.id > %s")
    # Served by migration 501's index, so the arm costs the red count, not the corpus.
    ddl = (MIGRATIONS / "501_location_w2a_listing_location.sql").read_text().lower()
    assert "on listing_location (obec_kod, granularity)" in " ".join(ddl.split())


def test_the_sweep_binds_its_placeholders_in_the_order_the_statement_reads_them():
    state = _state(max_listing_id=100_000)
    drain.enqueue_full_sweep(_FakeConn(state), window=250_000)
    (_, params), = _sweep_windows(state)
    assert params == (RESOLVER_VERSION, REGISTRY, 0, 100_000)


def test_a_window_retries_a_lock_wait_then_gives_up_loudly(monkeypatch):
    import psycopg
    monkeypatch.setattr(drain, "SWEEP_WINDOW_RETRY_S", 0.0)
    calls = {"n": 0}

    class _Cur:
        rowcount = 7
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            if not sql.lower().startswith("insert"):
                return  # _bounded's SET LOCAL guards pass through
            calls["n"] += 1
            if calls["n"] < 3:
                raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")

    class _Conn:
        def cursor(self): return _Cur()
        @contextmanager
        def transaction(self): yield

    assert drain._execute_window(_Conn(), 10, "INSERT ...", ()) == 7
    assert calls["n"] == 3  # two lock waits, third insert lands
    calls["n"] = -10  # never succeeds within the attempts
    import pytest
    with pytest.raises(psycopg.errors.LockNotAvailable):
        drain._execute_window(_Conn(), 10, "INSERT ...", ())


def test_main_enqueues_the_sweep_before_the_drain_lease_is_even_attempted(monkeypatch):
    """Run 34482389394 (2026-09-10): a corpus-wide full-resolve that finished in 20 s as
    'DRAIN skipped' and enqueued NOTHING, because the enqueue sat inside the lease block and
    the Railway lane holds that lease ~94% of the time. The default fake never grants the
    lease (fetchone -> None), which is exactly the production condition."""
    import contextlib as _cl
    state = _state(max_listing_id=300_000)
    monkeypatch.setattr(drain, "open_connection", lambda: _cl.nullcontext(_FakeConn(state)))
    assert drain.main(["--full-sweep", "--max-seconds", "1"]) == 0
    executed = [t for t, _ in state["executed"]]
    inserts = [i for i, t in enumerate(executed) if t.startswith("insert into dirty_locations")]
    acquires = [i for i, t in enumerate(executed) if "location_jobs" in t and "lease" in t]
    assert inserts, "the sweep did not enqueue"
    assert acquires, "the drain never attempted its lease"
    assert max(inserts) < min(acquires), "the enqueue must run BEFORE the lease is attempted"


def test_an_empty_corpus_sweeps_nothing_and_a_non_positive_window_is_refused():
    state = _state(max_listing_id=0)
    assert drain.enqueue_full_sweep(_FakeConn(state)) == 0
    assert _sweep_windows(state) == []
    import pytest
    with pytest.raises(ValueError):
        drain.enqueue_full_sweep(_FakeConn(_state(max_listing_id=5)), window=0)


def test_the_cli_has_one_sweep_flag_and_no_policy_knob():
    parser_source = inspect.getsource(drain.main)
    assert "--full-sweep" in parser_source
    for gone in ("--orphan-sweep", "--kraje", "--policy-version"):
        assert gone not in parser_source


def test_the_github_lane_keeps_one_connection():
    """W2-a5 is a WORKER-LANE change. The CLI gets no `--workers` and `main` passes none, so
    `location_resolve.yml` keeps draining on one connection: it is RTT-bound against a US
    runner rather than IO-bound beside the instance, and N runner connections into the
    session pooler is the wrong place to spend them."""
    main_source = inspect.getsource(drain.main)
    assert "--workers" not in main_source
    assert "workers=" not in main_source
    assert inspect.signature(drain.run).parameters["workers"].default == 1


# --------------------------------------------------------- the drain's round-trip budget


class _DrainCursor:
    """Answers the drain's reads well enough for the loop to complete a batch."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.rowcount = -1
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_DrainCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        text = " ".join(sql.split()).lower()
        self.state["executed"].append((text, params))
        self._result = []
        if text.startswith("select id, label from registry_versions"):
            self._result = [(7, REGISTRY)]
        elif text.startswith("select listing_id, attempts, enqueued_at from dirty_locations"):
            self._result = self.state["slices"].pop(0) if self.state["slices"] else []
        elif text.startswith("select count(*)") and "dirty_locations" in text:
            self._result = [(len(self.state["slices"]), 0)]

    def executemany(self, sql: str, params_seq: Any = None) -> None:
        self.execute(sql, params_seq)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result


class _DrainConn:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    def cursor(self) -> _DrainCursor:
        return _DrainCursor(self.state)

    @contextmanager
    def transaction(self):
        self.state["transactions"] += 1
        yield


def _drained(slices: list[list[tuple[int, int]]]) -> dict[str, Any]:
    state: dict[str, Any] = {
        "executed": [], "transactions": 0,
        "slices": [[(lid, att, CLAIMED_AT) for lid, att in s] for s in slices],
    }
    state["stats"] = drain.run(_DrainConn(state), batch_size=10, max_seconds=30)
    return state


def _count(state: dict[str, Any], needle: str) -> int:
    return sum(1 for text, _ in state["executed"] if needle in text)


def test_the_run_reads_one_corpus_constant_once():
    """Five policy loads and an epoch probe became ONE question: which registry version is
    current. Everything else the resolver used to read per run is a code constant."""
    state = _drained([[(101, 0), (102, 0), (103, 0)], [(104, 0), (105, 0)]])
    assert _count(state, "from registry_versions") == 1
    for gone in (
        "from pin_cluster_epochs",
        "from location_constants",
        "from location_granularity_rank",
        "from location_field_policy",
        "from location_uncertainty_policy",
        "from location_collision_policy",
    ):
        assert _count(state, gone) == 0, gone


def test_the_per_listing_reads_are_prefetched_once_per_slice():
    """Five prefetch queries became TWO: the claims and the source. The previous consumed
    inputs, `listings.property_id` and the open findings went with the auto-close engine, the
    property rollup and the contradiction ledger."""
    state = _drained([[(101, 0), (102, 0), (103, 0)], [(104, 0), (105, 0)]])
    assert state["stats"].claimed == 5
    for needle in (
        "from location_claims c where c.licence_class in ('portal', 'operator')",
        "select id, source from listings where id = any(",
    ):
        assert _count(state, needle) == 2, needle
    assert _count(state, "and c.listing_id = any(") == 2
    # ...and never the single-listing forms the prefetch replaced.
    assert _count(state, "and c.listing_id = %s") == 0
    for gone in (
        "location_contradictions_open", "location_resolutions",
        "select id, property_id from listings",
    ):
        assert _count(state, gone) == 0, gone


def test_the_only_projection_write_is_listing_location():
    """The write path was seven statements per slice — resolutions, candidates,
    contradictions, dispositions, a read-your-writes disputed read, the listing projection
    and the property rebuild. It is ONE upsert, plus the queue delete."""
    state = _drained([[(101, 0), (102, 0)]])
    writes = [t for t, _ in state["executed"]
              if t.startswith(("insert into", "update ", "delete from"))]
    assert [w[:36] for w in writes] == [
        "insert into listing_location as ll (",
        "delete from dirty_locations d using ",
    ], writes
    for gone in ("listing_location_current", "property_location_current",
                 "location_resolutions", "location_resolution_candidates",
                 "location_contradictions", "location_contradiction_dispositions"):
        assert _count(state, gone) == 0, gone


def test_the_drain_no_longer_rebuilds_properties():
    """It was the drain's ONE cross-listing write and the stated reason the lane could not
    run `--workers`. Every remaining write is keyed on a listing the slice already holds
    FOR UPDATE."""
    for gone in ("_rebuild_properties", "_MEMBER_FIELDS", "_PROPERTY_MEMBERS_BULK_SQL"):
        assert not hasattr(drain, gone), gone
    body = inspect.getsource(drain._write_slice)
    code = body.split('"""')[-1]  # the statements, not the prose that explains the deletion
    assert "property" not in code.lower()
    # ...and W2-a5 spent the unblocking: the drain takes a worker count.
    assert "workers" in inspect.signature(drain.run).parameters


def test_a_poisoned_slice_falls_back_to_per_listing_savepoints():
    """The optimistic slice is ONE savepoint, so a single bad listing rolls back 250 rows.
    That is only acceptable because the retry is the per-listing path with a SAVEPOINT each —
    without it the batch write would be a regression on "one bad row, one bad row"."""
    body = inspect.getsource(drain._run_slice)
    assert "_write_slice" in body
    assert "conn.transaction()" in body
    assert "_FAIL_ROW_SQL" in body
    assert body.index("_write_slice") < body.index("_resolve_one")


def test_the_slice_is_computed_before_anything_is_written():
    """`_compute_one` is the pure half — it may not write, or the optimistic batch could not
    roll back cleanly and the fallback would double-write."""
    body = inspect.getsource(drain._compute_one)
    for writer in ("upsert_", "_DELETE_ROW", "executemany"):
        assert writer not in body, writer


def test_a_failed_warm_degrades_instead_of_ending_the_run():
    """The warm runs INSIDE the batch transaction, so an unguarded statement timeout there
    would abort the batch and take the whole run with it. Rolled back, every point falls
    through to its own lazy query."""
    body = inspect.getsource(drain._drain_loop)
    warm = body[body.index("warm_started"):body.index("_run_slice(")]
    assert "try:" in warm and "conn.transaction()" in warm
    assert "WARM %sfailed" in warm


def test_a_queued_listing_with_no_live_claims_gets_a_row_not_a_skip():
    """The queue row was deleted and the listing either kept a stale row or never got one —
    the 10,679 active orphans the 2026-09-11 audit measured. The row states "nothing to go
    on" so coverage can count it (rule 25)."""
    slice_ = drain._Slice(claims={}, sources={4242: "remax"})
    item = drain._compute_one(4242, mm.context(), REGISTRY, slice_, dry_run=False)
    assert item is not None
    assert item.listing_id == 4242 and item.source == "remax"
    assert item.country_status == "undetermined"
    assert item.granularity == "unknown"
    assert (item.lat, item.lon) == (None, None)
    # ...and it is a WRITEABLE row, not a sentinel the upsert would reject.
    row = projection.build_listing_row(item)
    assert set(row) == set(projection.ROW_PARAMS)


# --------------------------------------------------- the claim projection, positionally


def test_the_claims_select_maps_onto_claim_positionally():
    """`_claim` unpacks `_CLAIMS_SELECT`'s row BY INDEX, so a column added, removed or
    reordered in one and not the other is a silent mis-mapping: every value still has the
    right TYPE one slot over (three texts in a row, two floats, two nullable texts), so
    nothing raises — the resolver just reads the surface as the extraction method."""
    select = resolve_db._CLAIMS_SELECT
    body = select[select.index("SELECT") + len("SELECT"):select.index("FROM location_claims")]
    names, depth, current = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            names.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    names.append("".join(current).strip())
    # A CASE expression has no name of its own; the two of them are the lat/lon pair.
    labels = [n.split("::")[0].split()[0].lower() if not n.upper().startswith("CASE")
              else ("st_y" if "ST_Y" in n else "st_x") for n in names]
    assert labels == [
        "id", "listing_id", "source", "claim_type", "surface", "extraction_method",
        "licence_class", "first_observed_at", "value_text", "value_num", "st_y", "st_x",
        "value_jsonb", "declared_precision_label", "declared_radius_m", "blur_evidence",
        "claim_confidence", "subject_scoped",
    ], labels

    row = (11, 22, "sreality", "street_name", "api_json", "portal_structured_field",
           "portal", mm._T0, "Dlouhá", 3.5, 50.1, 14.4, {"k": "v"}, "exact_address",
           25.0, "declared", "high", True)
    assert len(row) == len(labels)
    claim = resolve_db._claim(row)

    assert (claim.id, claim.listing_id, claim.source) == (11, 22, "sreality")
    assert (claim.claim_type, claim.surface) == ("street_name", "api_json")
    assert claim.extraction_method == "portal_structured_field"
    assert claim.licence_class == "portal"
    assert claim.observed_at == mm._T0
    assert (claim.value_text, claim.value_num) == ("Dlouhá", 3.5)
    assert (claim.lat, claim.lon) == (50.1, 14.4)          # ST_Y is lat, ST_X is lon
    assert claim.value_jsonb == {"k": "v"}
    assert claim.declared_precision_label == "exact_address"
    assert claim.declared_radius_m == 25.0
    assert (claim.blur_evidence, claim.claim_confidence) == ("declared", "high")
    assert claim.subject_scoped is True
    # The six the pure core never read keep their names and their defaults (W1-b).
    assert (claim.extractor_id, claim.declared_confidence, claim.page_kind) == ("", None, "none")
    assert (claim.snapshot_id, claim.distance_m, claim.target_text) == (None, None, None)

    # A NULL geometry must not become 0.0 — the resolver's `has_position` reads both.
    assert resolve_db._claim(row[:10] + (None, None) + row[12:]).has_position is False


# ------------------------------------------------------------------ the run-scoped cache


class _CountingMirror:
    def __init__(self) -> None:
        self.inner = mm.default_mirror()
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return attr(*args, **kwargs)

        return wrapper


def test_the_cached_view_answers_every_question_the_protocol_declares():
    """It forwards by explicit method, not by `__getattr__` — so a question added to
    `RegistryView` and to `SqlRegistryView` but not here would raise AttributeError mid-drain
    rather than quietly falling through."""
    from location_data.resolver.types import RegistryView

    declared = {
        name for name in dir(RegistryView)
        if not name.startswith("_") and callable(getattr(RegistryView, name, None))
    }
    assert declared
    missing = [name for name in declared if not hasattr(resolve_db.CachedRegistryView, name)]
    assert not missing, missing


def test_the_protocol_is_nine_questions():
    """Fifteen query kinds became nine. Three went with the engines they served, one with an
    unreachable rung, one with the epoch, and two folded into `admin_chain`. A question added
    back is a round trip per listing — and `nearest_obec_within` earns its one because it is
    BIND's last rung, the thing that keeps a border pin from having no town (rule 25)."""
    from location_data.resolver.types import RegistryView

    declared = {
        name for name in dir(RegistryView)
        if not name.startswith("_") and callable(getattr(RegistryView, name, None))
    }
    assert declared == {
        "address_point", "address_points_by_number", "streets_in_obec",
        "admin_units_by_name", "admin_chain", "admin_chain_by_code",
        "obec_codes_for_psc", "containing_obec", "in_czechia_polygon",
        "nearest_obec_within",
    }
    for gone in ("parcels", "distance_to_admin_boundary_m",
                 "cast_obce_for_point", "cast_obce_extent_m", "admin_unit",
                 "admin_unit_by_code"):
        assert gone not in declared, gone


def test_the_registry_cache_asks_each_distinct_question_exactly_once():
    """The mirror is immutable at a pinned `registry_version_id`, so the same question has one
    answer for the whole run."""
    inner = _CountingMirror()
    view = resolve_db.CachedRegistryView(inner, resolve_db.RunCache())
    first = view.streets_in_obec(554782)
    for _ in range(5):
        assert view.streets_in_obec(554782) == first
    view.streets_in_obec(599212)
    view.admin_units_by_name("praha", levels=("obec",))
    view.admin_units_by_name("praha", levels=("obec",))
    assert inner.calls == 3


def test_a_cached_run_replays_bit_for_bit_against_an_uncached_one():
    """THE constraint on every optimisation in this file: caching is an I/O-layer concern, so
    the row it produces must be byte-identical to the one the bare mirror produces."""
    import dataclasses

    from location_data.resolver import serialize

    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
        mm.claim(3, "psc", value_text="160 00"),
        mm.claim(4, "coordinate", lat=50.10102, lon=14.34804, declared_precision_label="gps"),
        mm.claim(5, "cast_obce_name", value_text="Vokovice"),
    ]

    def _resolve(registry: Any) -> str:
        return serialize.canonical(dataclasses.asdict(core.resolve(
            claims, mm.context(registry), resolver_version=RESOLVER_VERSION,
            registry_version=REGISTRY,
        )))

    bare = _resolve(mm.default_mirror())
    cached_view = resolve_db.CachedRegistryView(mm.default_mirror(), resolve_db.RunCache())
    assert _resolve(cached_view) == bare
    # And again on the SAME warm cache — a second listing must not see a mutated answer.
    assert _resolve(cached_view) == bare


def test_the_cache_memory_rail_cannot_change_an_answer():
    """`max_entries` drops the whole memo when it fills. Correctness may not depend on what
    happens to be resident."""
    inner = _CountingMirror()
    view = resolve_db.CachedRegistryView(inner, resolve_db.RunCache(max_entries=1))
    answers = {code: view.streets_in_obec(code) for code in (554782, 599212)}
    for code, expected in answers.items():
        assert view.streets_in_obec(code) == expected


def test_a_name_is_asked_once_however_many_ways_it_is_narrowed():
    """The level tuple used to be part of the key, so `praha` asked for `('obec',)`, then for
    `('obec','cast_obce','momc','zsj')`, then unnarrowed, was three round trips for one
    immutable answer. One query, narrowed in Python."""
    inner = _CountingMirror()
    view = resolve_db.CachedRegistryView(inner, resolve_db.RunCache())
    view.admin_units_by_name("praha", levels=("obec",))
    view.admin_units_by_name("praha", levels=("obec", "cast_obce", "momc", "zsj"))
    view.admin_units_by_name("praha")
    view.admin_units_by_name("praha", levels=("katastralni_uzemi",))
    assert inner.calls == 1


def test_the_python_narrowing_returns_what_the_narrowed_query_returned():
    """The widening is only safe because the unnarrowed answer is the SUPERSET of every
    narrowing and its ordering is level-independent — so a stable filter reproduces the
    narrowed list element for element, not merely as a set."""
    mirror = mm.default_mirror()
    view = resolve_db.CachedRegistryView(mirror, resolve_db.RunCache())
    names = {u.name_norm for u in mirror.units}
    levels = sorted({u.level for u in mirror.units})
    for name in sorted(names):
        for wanted in ((), *((lvl,) for lvl in levels), tuple(levels[:2])):
            assert list(view.admin_units_by_name(name, levels=wanted)) == \
                mirror.admin_units_by_name(name, levels=wanted), (name, wanted)


def test_the_cache_reports_which_question_misses():
    cache = resolve_db.RunCache()
    cache.get(("streets_in_obec", 1), lambda: ())
    cache.get(("streets_in_obec", 1), lambda: ())
    cache.get(("containing_obec", 1.0, 2.0), lambda: None)
    assert cache.missed_by_kind == {"streets_in_obec": 1, "containing_obec": 1}
    assert cache.asked_by_kind == {"streets_in_obec": 2, "containing_obec": 1}
    assert "containing_obec" in cache.report()


def test_the_query_stats_rank_by_total_time_not_by_call():
    """A 3 ms question asked 20,000 times outranks a 700 ms one asked twice; a report sorted
    by per-call average would name the wrong offender."""
    stats = resolve_db.QueryStats()
    for _ in range(10):
        stats.record("cheap_but_hot", 0.01, 1)
    stats.record("slow_but_rare", 0.05, 1)
    assert stats.report().startswith("cheap_but_hot(q=10,")
    assert "slow_but_rare(q=1," in stats.report()


# ------------------------------------------------- warming is invisible to the pure core


class _BulkMirror:
    """The `*_bulk` half of `SqlRegistryView`, answered from the mini mirror — so the warm
    path can be tested without a live PostGIS."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def containing_obec_bulk(self, coords):
        found = {i: self.inner.containing_obec(*c) for i, c in enumerate(coords)}
        return {i: unit for i, unit in found.items() if unit is not None}

    def in_czechia_polygon_bulk(self, coords):
        found = {i: self.inner.in_czechia_polygon(*c) for i, c in enumerate(coords)}
        return {i: value for i, value in found.items() if value is not None}


def _warm_claims():
    """A PIN-positioned listing, which is the shape the warm exists for: the two point-keyed
    questions are asked of the pin, and the pin is a claim coordinate."""
    return [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "psc", value_text="160 00"),
        mm.claim(3, "coordinate", lat=50.10102, lon=14.34804, declared_precision_label="gps"),
        mm.claim(4, "cast_obce_name", value_text="Vokovice"),
    ]


def _resolve_with(registry: Any) -> Any:
    return core.resolve(
        _warm_claims(), mm.context(registry), resolver_version=RESOLVER_VERSION,
        registry_version=REGISTRY,
    )


def test_a_warmed_run_replays_bit_for_bit_against_an_unwarmed_one():
    """THE constraint on warming: it is an I/O-layer concern, so pre-seeding the memo must
    produce the byte-identical row a cold run produces. `warm_points` writes exactly the
    key/value `CachedRegistryView` would have computed."""
    import dataclasses

    from location_data.resolver import serialize

    bare = serialize.canonical(dataclasses.asdict(_resolve_with(mm.default_mirror())))

    cache = resolve_db.RunCache()
    mirror = mm.default_mirror()
    resolve_db.warm_points(_BulkMirror(mirror), cache, [(50.10102, 14.34804)])
    warmed = resolve_db.CachedRegistryView(mirror, cache)
    assert serialize.canonical(dataclasses.asdict(_resolve_with(warmed))) == bare


def test_warming_actually_serves_the_coordinate_keyed_questions():
    """A warm that produced identical bytes by simply never being consulted would pass the
    replay gate and buy nothing — the point is that the pin's questions are ANSWERED from the
    memo, i.e. asked and never missed."""
    cache = resolve_db.RunCache()
    inner = _CountingMirror()
    resolve_db.warm_points(_BulkMirror(mm.default_mirror()), cache, [(50.10102, 14.34804)])
    _resolve_with(resolve_db.CachedRegistryView(inner, cache))
    assert cache.asked_by_kind.get("in_czechia_polygon", 0) > 0
    assert cache.missed_by_kind.get("in_czechia_polygon", 0) == 0
    view = resolve_db.CachedRegistryView(inner, cache)
    before = inner.calls
    view.containing_obec(50.10102, 14.34804)
    view.in_czechia_polygon(50.10102, 14.34804)
    assert inner.calls == before


def test_a_registry_bound_row_asks_no_point_keyed_question_at_all():
    """CHECK asks both point questions of the PORTAL PIN only. A registry point and an admin
    centroid come out of the mirror, so "is it in Czechia" and "is it inside its own obec"
    are fixed by construction — and they would be the one key the slice warm cannot hold."""
    cache = resolve_db.RunCache()
    core.resolve(
        [
            mm.claim(1, "obec_name", value_text="Praha"),
            mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
        ],
        mm.context(resolve_db.CachedRegistryView(mm.default_mirror(), cache)),
        resolver_version=RESOLVER_VERSION, registry_version=REGISTRY,
    )
    assert cache.asked_by_kind.get("in_czechia_polygon", 0) == 0
    assert cache.asked_by_kind.get("containing_obec", 0) == 0


def test_a_null_answer_is_warmed_too():
    """Otherwise every rural point — no obec polygon at all — would fall through to a per-call
    query and the warm would help exactly the listings that need it least."""
    cache = resolve_db.RunCache()
    resolve_db.warm_points(_BulkMirror(mm.default_mirror()), cache, [(0.0, 0.0)])
    calls = _CountingMirror()
    view = resolve_db.CachedRegistryView(calls, cache)
    assert view.containing_obec(0.0, 0.0) is None
    assert calls.calls == 0


# -------------------------------------------------------------------- the connection mode


def test_the_drain_opens_the_session_pooler_connection(monkeypatch):
    """`prepare_threshold=None` on the transaction pooler re-parses and re-plans every
    recurring statement on every listing; the session pooler's dedicated backend lets psycopg
    prepare them once."""
    monkeypatch.setenv("SUPABASE_DB_SESSION_URL", "postgres://session/db")
    opened: list[str] = []
    monkeypatch.setattr(drain.db, "connect_session", lambda: opened.append("session") or "conn")
    assert drain.open_connection() == "conn"
    assert opened == ["session"]


def test_the_transaction_pooler_fallback_is_announced(monkeypatch, caplog):
    """`connect_session()` falls back silently by design; a drain that has quietly lost its
    prepared statements looks exactly like a drain that is simply slow."""
    monkeypatch.delenv("SUPABASE_DB_SESSION_URL", raising=False)
    monkeypatch.setattr(drain.db, "connect_session", lambda: "conn")
    with caplog.at_level("WARNING"):
        drain.open_connection()
    assert "SUPABASE_DB_SESSION_URL" in caplog.text


# ------------------------------------------------- W2-a2: the queue is re-entrant
#
# 2026-09-12 07:13Z. The nine contract bumps re-mined ~60k listings whose rows were ALREADY
# queued from the previous sweep (~540k rows), so every `claim_insert` enqueue hit ON CONFLICT
# DO NOTHING; the drain resolved them from the OLD claims and deleted the queue row. Result:
# 384,500 `listing_location` rows, 135 with a town, and nothing able to re-enqueue them — the
# sweep saw a current-version row and walked past. Two rules close it, and these pin both.


def _enqueue_ctes() -> dict[str, str]:
    """Every evidence-producing `INSERT INTO dirty_locations` in the programme.

    The sweep is deliberately NOT here (see the test below): it carries no evidence.
    """
    from location_data import claims_intake, contracts, operator_corrections
    return {
        "claims_intake._CLAIM_WRITE_SQL": claims_intake._CLAIM_WRITE_SQL,
        "contracts._RETRACT_BATCH_SQL": contracts._RETRACT_BATCH_SQL,
        "operator_corrections._OPERATOR_CLAIM_SQL": operator_corrections._OPERATOR_CLAIM_SQL,
    }


def test_every_evidence_producer_bumps_the_queue_row_instead_of_skipping_it():
    """Rule 1. A producer that learns something new about a listing already in the queue must
    move `enqueued_at` forward — that is what re-arms the row against an in-flight slice (the
    drain's delete is bounded by the `enqueued_at` it claimed) and clears a stale backoff.
    `DO NOTHING` made the new evidence invisible for ever."""
    for name, sql in _enqueue_ctes().items():
        flat = " ".join(sql.split()).lower()
        assert "insert into dirty_locations" in flat, name
        assert "on conflict (listing_id) do nothing" not in flat, name
        assert "on conflict (listing_id) do update" in flat, name
        clause = flat.split("on conflict (listing_id) do update")[1]
        clause = clause[:clause.index("returning")]
        for fragment in ("set enqueued_at = now()", "reason = excluded.reason",
                         "attempts = 0", "next_eligible_at = now()"):
            assert fragment in clause, (name, fragment)


def test_the_bump_only_touches_columns_dirty_locations_actually_has():
    """`next_eligible_at` is `not null default now()` (migration 384), so the bump re-arms it
    with `now()` — writing NULL would violate the constraint and abort the producer's whole
    claim-insert transaction. The five columns are 384's, unchanged by 404 and 498 (both only
    re-state the `reason` CHECK)."""
    import re as _re
    ddl = (MIGRATIONS / "384_location_w1_serving.sql").read_text()
    body = ddl.split("create table dirty_locations (")[1].split(");")[0]
    columns = {m.group(1) for m in _re.finditer(r"^\s{2}(\w+)\s", body, _re.M)}
    assert columns == {"listing_id", "enqueued_at", "reason", "attempts",
                       "last_error", "next_eligible_at"}, columns
    assert "next_eligible_at timestamptz not null" in " ".join(body.split())
    for sql in _enqueue_ctes().values():
        clause = " ".join(sql.split()).lower().split("do update")[1]
        assert "next_eligible_at = null" not in clause


def test_the_slice_carries_the_enqueued_at_every_finishing_statement_is_bounded_by():
    """Rule 1's other half. Deleting on `listing_id` alone throws away a bump that landed
    while the slice was in flight; the row is gone and its new claims are never resolved."""
    slice_sql = " ".join(drain._CLAIM_SLICE_SQL.split()).lower()
    assert slice_sql.startswith("select listing_id, attempts, enqueued_at from dirty_locations")
    batch = " ".join(drain._DELETE_ROWS_SQL.split()).lower()
    assert "unnest(%s::bigint[], %s::timestamptz[])" in batch
    assert "d.enqueued_at <= claimed.enqueued_at" in batch
    one = " ".join(drain._DELETE_ROW_SQL.split()).lower()
    assert one == "delete from dirty_locations where listing_id = %s and enqueued_at <= %s"
    fail = " ".join(drain._FAIL_ROW_SQL.split()).lower()
    assert fail.endswith("where listing_id = %s and enqueued_at <= %s")


def test_the_batch_delete_is_bounded_by_the_timestamps_the_slice_claimed():
    state = _drained([[(101, 0), (102, 0)]])
    deletes = [(t, p) for t, p in state["executed"] if t.startswith("delete from dirty_locations")]
    assert len(deletes) == 1
    _, params = deletes[0]
    assert params == ([101, 102], [CLAIMED_AT, CLAIMED_AT])


class _QueueCursor(_DrainCursor):
    """`_DrainCursor` plus a real (tiny) model of the two queue statements: the slice reads
    `state["queue"]`, and the bounded delete removes only rows whose STORED `enqueued_at` is
    still the one the slice claimed. Nothing else about SQL is modelled — this exists so the
    surviving-row assertion is about the drain's behaviour, not about its SQL text."""

    def execute(self, sql: str, params: Any = None) -> None:
        text = " ".join(sql.split()).lower()
        if text.startswith("select listing_id, attempts, enqueued_at from dirty_locations"):
            self.state["executed"].append((text, params))
            queue = self.state["queue"]
            self._result = sorted((lid, 0, at) for lid, at in queue.items())
            self.state["claimed"].append([row[0] for row in self._result])
            # ...and a producer bumps one of the claimed rows while the slice is in flight.
            for listing_id in self.state.pop("bump", []):
                queue[listing_id] = queue[listing_id] + timedelta(minutes=1)
            return
        if text.startswith("delete from dirty_locations d using"):
            self.state["executed"].append((text, params))
            listing_ids, bounds = params
            gone = [lid for lid, bound in zip(listing_ids, bounds)
                    if self.state["queue"].get(lid) is not None
                    and self.state["queue"][lid] <= bound]
            for listing_id in gone:
                del self.state["queue"][listing_id]
            self.state["deleted"].append(gone)
            return
        super().execute(sql, params)


class _QueueConn(_DrainConn):
    def cursor(self) -> _QueueCursor:
        return _QueueCursor(self.state)


def test_a_row_bumped_mid_slice_is_resolved_again_instead_of_being_deleted():
    """THE regression, end to end. Listing 202 gains new claims after the slice read its OLD
    ones, so the write the drain is about to make is stale for it. The bounded delete leaves
    202 queued, the next slice claims it again and resolves it against the claims that
    arrived, and only then is it finished. 201 changed under nobody and finishes first time.

    On `DELETE ... WHERE listing_id = ANY(...)` the first delete takes BOTH rows and 202's new
    claims are never read — 384,365 rows' worth of that on 2026-09-12."""
    state: dict[str, Any] = {
        "executed": [], "transactions": 0,
        "slices": [],  # _DrainCursor's queue-health branch reads it; the queue is below
        "queue": {201: CLAIMED_AT, 202: CLAIMED_AT},
        "claimed": [], "deleted": [], "bump": [202],
    }
    drain.run(_QueueConn(state), batch_size=10, max_seconds=30)
    assert state["claimed"] == [[201, 202], [202], []]
    assert state["deleted"] == [[201], [202]]
    assert state["queue"] == {}


def test_the_failure_stamp_does_not_clobber_a_newer_enqueue(monkeypatch):
    """A backoff written over a bumped row would push a listing that JUST gained evidence
    behind up to six hours of `next_eligible_at` — and reset `attempts` to a count the new
    evidence never earned."""
    def _boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("poisoned")
    monkeypatch.setattr(drain, "_write_slice", _boom)
    state = _drained([[(303, 2)]])
    stamps = [(t, p) for t, p in state["executed"] if t.startswith("update dirty_locations")]
    assert len(stamps) == 1
    text, params = stamps[0]
    assert text.endswith("where listing_id = %s and enqueued_at <= %s")
    assert params[1] == drain.BACKOFF_SECONDS[2]
    assert params[2:] == (303, CLAIMED_AT)


# ------------------------------------------- W2-a5: N slice loops, N connections, one run


class _SharedQueue:
    """The fake queue the worker connections claim from, with `SKIP LOCKED`'s one guarantee
    modelled and nothing else: a listing handed to one claimer is never handed to another.

    `parties`, when set, holds the first round open: every worker's FIRST claim waits for all
    of them to arrive, and no worker gets a SECOND slice until all of them have had one. The
    fake work is instant, so without that the first thread scheduled would drain the queue
    before the others ran a statement — and the test would be about the scheduler."""

    def __init__(self, listing_ids: list[int], *, parties: int | None = None) -> None:
        self._lock = threading.Lock()
        self._pending = list(listing_ids)
        self._parties = parties
        self._barrier = threading.Barrier(parties, timeout=10) if parties else None
        self._arrived: set[int] = set()
        self._round_done = threading.Event()
        self.claimed_by: dict[int, list[int]] = {}

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._pending)

    def claim(self, worker: int, limit: int) -> list[int]:
        if self._barrier is not None:
            if worker in self._arrived:
                self._round_done.wait(10)
            else:
                self._arrived.add(worker)
                with contextlib.suppress(threading.BrokenBarrierError):
                    self._barrier.wait()
        with self._lock:
            taken, self._pending = self._pending[:limit], self._pending[limit:]
            self.claimed_by.setdefault(worker, []).extend(taken)
            if self._parties is not None and len(self.claimed_by) >= self._parties:
                self._round_done.set()
        return taken


class _WorkerCursor(_DrainCursor):
    """`_DrainCursor` with the two queue statements answered from the SHARED queue instead of
    from a per-connection script."""

    def execute(self, sql: str, params: Any = None) -> None:
        text = " ".join(sql.split()).lower()
        if text.startswith("select listing_id, attempts, enqueued_at from dirty_locations"):
            self.state["executed"].append((text, params))
            claimed = self.state["queue"].claim(self.state["worker"], params[0])
            self._result = [(listing_id, 0, CLAIMED_AT) for listing_id in claimed]
            return
        if text.startswith("select count(*)") and "dirty_locations" in text:
            self.state["executed"].append((text, params))
            self._result = [(self.state["queue"].depth, 0)]
            return
        super().execute(sql, params)


class _WorkerConn(_DrainConn):
    def __init__(self, queue: _SharedQueue, worker: int) -> None:
        super().__init__({"executed": [], "transactions": 0, "slices": [],
                          "queue": queue, "worker": worker})
        self.closed = False

    def cursor(self) -> _WorkerCursor:
        return _WorkerCursor(self.state)

    def close(self) -> None:
        self.closed = True


def _texts(conn: _WorkerConn) -> list[str]:
    return [text for text, _ in conn.state["executed"]]


def _drain_with_workers(
    listing_ids: list[int],
    *,
    workers: int,
    batch_size: int,
    monkeypatch: Any,
    parties: int | None = None,
    dying: frozenset[int] = frozenset(),
    max_seconds: int = 30,
) -> tuple[Any, _WorkerConn, list[_WorkerConn], _SharedQueue]:
    queue = _SharedQueue(listing_ids, parties=parties)
    opened: list[_WorkerConn] = []
    counter = itertools.count(1)
    lock = threading.Lock()

    def _open() -> _WorkerConn:
        with lock:
            index = next(counter)
            if index in dying:
                raise RuntimeError(f"worker {index} could not open a connection")
            conn = _WorkerConn(queue, index)
            opened.append(conn)
            return conn

    monkeypatch.setattr(drain, "_worker_connection", _open)
    caller = _WorkerConn(queue, 0)
    stats = drain.run(
        caller, batch_size=batch_size, max_seconds=max_seconds, workers=workers)
    return stats, caller, opened, queue


def test_n_workers_claim_n_disjoint_slices_and_the_run_aggregates_them(monkeypatch):
    """THE wave. Four loops, four connections, one queue: `FOR UPDATE SKIP LOCKED` is what
    makes the slices disjoint, and it is the same statement the single loop always ran."""
    ids = list(range(101, 113))
    stats, _, opened, queue = _drain_with_workers(
        ids, workers=4, batch_size=3, parties=4, monkeypatch=monkeypatch)

    assert len(opened) == 4, "one connection per worker — psycopg is not thread-safe"
    assert all(conn.closed for conn in opened)
    assert sorted(queue.claimed_by) == [1, 2, 3, 4], "every worker claimed a slice"
    claimed = [frozenset(v) for v in queue.claimed_by.values()]
    assert sorted(len(c) for c in claimed) == [3, 3, 3, 3]
    assert frozenset().union(*claimed) == frozenset(ids)
    for a, b in itertools.combinations(claimed, 2):
        assert not (a & b), "two workers held the same queue row"
    assert (stats.claimed, stats.resolved, stats.failed) == (12, 12, 0)
    assert (stats.batches, stats.workers, stats.failed_passes) == (4, 4, 0)


def test_every_worker_writes_its_own_slice_and_deletes_its_own_queue_rows(monkeypatch):
    """Each loop runs the UNCHANGED per-slice pipeline — prefetch, warm, compute, then the
    one upsert and the enqueued_at-bounded delete in its own transaction."""
    ids = list(range(201, 207))
    _, _, opened, _ = _drain_with_workers(
        ids, workers=2, batch_size=3, parties=2, monkeypatch=monkeypatch)

    for conn in opened:
        writes = [t[:36] for t in _texts(conn)
                  if t.startswith(("insert into", "update ", "delete from"))]
        assert writes == ["insert into listing_location as ll (",
                          "delete from dirty_locations d using "], writes
        assert sum(1 for t in _texts(conn) if "from location_claims c" in t) == 1


def test_the_lease_stays_on_the_callers_connection_and_no_worker_touches_it(monkeypatch):
    """The lease is ONE row CAS taken by the caller (drain.main, the worker lane) before the
    run. Four workers are still one run: a worker that acquired it would either fail (it is
    held) or hand a second drainer the lane."""
    stats, caller, opened, _ = _drain_with_workers(
        list(range(301, 307)), workers=3, batch_size=2, parties=3, monkeypatch=monkeypatch)

    for conn in [caller, *opened]:
        assert not [t for t in _texts(conn) if "location_jobs" in t]
    for fn in (drain.run, drain._run_workers):
        assert "lease.held" not in inspect.getsource(fn)
    # ...and the caller's own connection runs the ONE corpus constant and nothing else: it
    # is the lease's connection, and a thread must never share it.
    assert [t for t in _texts(caller) if not t.startswith("set local")] == [
        " ".join(resolve_db._CURRENT_REGISTRY_SQL.split()).lower()
    ]
    assert stats.claimed == 6


def test_a_worker_that_loses_its_connection_leaves_the_others_draining(monkeypatch):
    """A pooler that refuses one connection is not a failed run. The dead loop counts one
    `failed_passes`, the survivors finish the queue, and the stats say the run ran degraded
    rather than reporting four healthy workers."""
    ids = list(range(401, 413))
    stats, _, opened, queue = _drain_with_workers(
        ids, workers=4, batch_size=3, dying=frozenset({2}), monkeypatch=monkeypatch)

    assert len(opened) == 3
    assert 2 not in queue.claimed_by
    assert stats.failed_passes == 1
    assert stats.workers == 4, "what was ASKED for; failed_passes says what happened"
    assert stats.claimed == 12 and queue.depth == 0


def test_the_budget_is_one_deadline_and_it_stops_every_worker(monkeypatch):
    """`max_seconds` is the RUN's, computed once on the main thread and shared: every loop
    tests it between batches, so an exhausted budget stops all four at the same moment and
    the queue rows they did not claim simply stay queued."""
    ids = list(range(501, 513))
    stats, _, opened, queue = _drain_with_workers(
        ids, workers=4, batch_size=3, max_seconds=0, monkeypatch=monkeypatch)

    assert len(opened) == 4 and all(conn.closed for conn in opened)
    assert (stats.claimed, stats.batches) == (0, 0)
    assert queue.depth == 12
    assert "deadline = started + max_seconds" in inspect.getsource(drain.run)
    assert "while time.monotonic() < deadline" in inspect.getsource(drain._drain_loop)


def test_workers_1_is_the_single_connection_path_statement_for_statement(monkeypatch):
    """The default is the loop the drain has always had, on the CALLER's connection: no
    thread, no second connection, and the same statements in the same order."""
    def _never() -> Any:
        raise AssertionError("workers=1 must not open a second connection")

    monkeypatch.setattr(drain, "_worker_connection", _never)
    queue = _SharedQueue([601, 602])
    caller = _WorkerConn(queue, 0)
    stats = drain.run(caller, batch_size=10, max_seconds=30, workers=1)

    assert [t[:25] for t in _texts(caller)] == [
        "set local statement_timeo", "set local lock_timeout = ",
        "select id, label from reg",
        "set local statement_timeo", "set local lock_timeout = ",
        "select count(*), coalesce",
        "set local statement_timeo", "set local lock_timeout = ",
        "select listing_id, attemp",
        "select id, listing_id, so", "select id, source from li",
        "insert into listing_locat", "delete from dirty_locatio",
        "set local statement_timeo", "set local lock_timeout = ",
        "select count(*), coalesce",
        "set local statement_timeo", "set local lock_timeout = ",
        "select listing_id, attemp",
    ]
    assert (stats.workers, stats.claimed, stats.failed_passes) == (1, 2, 0)


def test_only_the_first_loop_counts_the_queue(monkeypatch):
    """The health read is a `count(*)` over the whole of `dirty_locations`. Four copies of it
    per batch buy four sequential scans of a 400k-row table and one number."""
    _, _, opened, _ = _drain_with_workers(
        list(range(701, 709)), workers=4, batch_size=2, parties=4, monkeypatch=monkeypatch)

    counted = {conn.state["worker"] for conn in opened
               if any(t.startswith("select count(*)") for t in _texts(conn))}
    assert counted == {1}


def test_the_stats_merge_keeps_wall_clock_and_sums_the_phases():
    """A worker's `seconds` is not the run's: four overlapping loops would report four times
    the elapsed time and a quarter of the real rate."""
    total = drain.DrainStats(workers=4)
    for _ in range(4):
        total.absorb(drain.DrainStats(
            claimed=10, resolved=9, failed=1, batches=2, fallbacks=1, failed_passes=1,
            core_seconds=5.0, seconds=100.0))
    assert (total.claimed, total.resolved, total.failed) == (40, 36, 4)
    assert (total.batches, total.fallbacks, total.failed_passes) == (8, 4, 4)
    assert total.core_seconds == 20.0
    assert total.seconds == 0.0, "wall clock belongs to the run, never to a worker"
    assert total.workers == 4
    total.seconds = 10.0
    assert total.rate == 4.0


# ------------------------------------------------- W2-a5: the run cache under four threads


def test_the_run_cache_counters_survive_concurrent_asks():
    """`hits += 1` is a read-modify-write. Without the lock the workers lose counts, and the
    hit rate — the number that says whether a key is too narrow — quietly under-reports."""
    cache = resolve_db.RunCache()
    asks = 400

    def _ask() -> None:
        for _ in range(asks):
            assert cache.get(("streets_in_obec", 554782), lambda: "answer") == "answer"

    threads = [threading.Thread(target=_ask) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert cache.hits + cache.misses == 4 * asks
    assert cache.asked_by_kind["streets_in_obec"] == 4 * asks


def test_the_run_cache_never_holds_its_lock_across_a_registry_question():
    """The whole point of the workers is to overlap the round trips. A lock held across
    `compute()` would serialize them back into one loop — with four connections open."""
    cache = resolve_db.RunCache()
    blocked = threading.Event()
    asking = threading.Event()

    def _slow() -> str:
        asking.set()
        blocked.wait(10)
        return "slow"

    slow = threading.Thread(target=lambda: cache.get(("admin_chain", 1), _slow))
    slow.start()
    try:
        assert asking.wait(10), "the blocking ask never reached its compute"
        # The other worker asks a DIFFERENT question while the first is still waiting.
        assert cache.get(("admin_chain", 2), lambda: "fast") == "fast"
    finally:
        blocked.set()
        slow.join(10)
    assert cache.get(("admin_chain", 1), lambda: "recomputed") == "slow"
