"""The two resolver JOBS and the seams between them (03 §3.8.4, §3.14, 00 §8.2).

These are the parts a pure-core test cannot reach: what the epoch job is allowed to mint,
what the drain claims and in what order, and when a contradiction may be auto-closed. The
SQL-text assertions are deliberate — a fake connection cannot tell you whether a query
joins `listings`, and the schema-replay job is the only other place that would notice.
"""

from __future__ import annotations

import inspect
import re
from contextlib import contextmanager
from typing import Any

from location_data.resolver import collision, core, drain, epoch_job, reconciler, resolve_db
from location_data.resolver.types import Precision
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm


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
            self._result = [(7, "2026-07")]
        elif "from pin_cluster_epochs" in text and text.startswith("select id"):
            self._result = [(11,)]
        elif "from location_collision_policy" in text:
            self._result = [("v1", "*", None, 4, 0, 2, "suspect")]
        elif text.startswith("select p.listing_id, p.source"):
            self._result = list(self.state["pins"])
        elif text.startswith("insert into pin_cluster_epochs"):
            self._result = [(99,)]
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


def _state(
    pins: list[tuple[Any, ...]] | None = None, max_listing_id: int = 0,
) -> dict[str, Any]:
    return {
        "executed": [], "transactions": 0, "pins": pins or [],
        "max_listing_id": max_listing_id,
    }


def _wrote_an_epoch(state: dict[str, Any]) -> bool:
    return any(t.startswith("insert into pin_cluster_epochs") for t, _ in state["executed"])


# --------------------------------------------------------- the epoch is corpus-complete


def test_a_source_scoped_epoch_is_never_minted():
    """`current_epoch` is a bare `ORDER BY computed_at DESC LIMIT 1`, so a subset epoch
    becomes THE epoch for every portal — and every listing on an unselected portal then
    resolves against an epoch holding no cluster for it, which reads as
    `classification='normal'`: the exact false negative the detector exists to catch."""
    state = _state()
    assert epoch_job.run(_FakeConn(state), sources=["bazos"]) == 0
    assert not _wrote_an_epoch(state)


def test_a_corpus_complete_epoch_is_minted():
    state = _state()
    epoch_job.run(_FakeConn(state))
    assert _wrote_an_epoch(state)


def test_the_epoch_counts_only_listings_that_are_still_on_the_market():
    """Both reads, or the comparison is not like-for-like: an all-rows previous membership
    against an active-only current one would enqueue every listing that merely went
    inactive."""
    for sql in (epoch_job._PIN_ROWS_SQL, epoch_job._PREVIOUS_MEMBERS_SQL):
        flat = " ".join(sql.split()).lower()
        assert "join listings l on l.id = p.listing_id" in flat
        assert "l.is_active" in flat


# ------------------------------------------------------------------ the drain's queue


def test_the_queue_slice_has_a_unique_tiebreaker():
    """A batch enqueue shares one `now()`, so a bare `ORDER BY enqueued_at` returns a
    different order on every call and a poisonous row can be re-claimed forever while
    another starves."""
    flat = " ".join(drain._CLAIM_SLICE_SQL.split()).lower()
    assert "order by enqueued_at, listing_id" in flat


# ------------------------------------------------------- the kraj-scoped full sweep


def test_the_kraj_scoped_sweep_is_a_whole_prepareable_constant():
    """A second constant, never `_FULL_SWEEP_SQL` + a formatted predicate: the offline
    placeholder guard and the schema-aware PREPARE sweep only see module-level `*_SQL`."""
    flat = " ".join(drain._FULL_SWEEP_KRAJE_SQL.split()).lower()
    assert "p.kraj_kod = any(%s::bigint[])" in flat
    assert re.sub(r"%\(\w+\)s|%s", "", flat).count("%") == 0
    assert flat.count("%s") == 4


def test_the_kraj_scoped_sweep_drives_off_the_projection_not_the_claim_corpus():
    """The 2026-09-10 QueryCanceled (run 34459466027) in one assertion.

    Driving off `location_claims_live` and DISTINCT-ing it down cost work proportional to
    the CLAIM corpus — which the archive sweeps grow by ~150k rows an hour — for an answer
    proportional to the projection, and blew the 900 s ceiling. The driving relation must
    stay the projection, with the claims reached only through a correlated EXISTS, and the
    MATERIALIZED fence must stay: `location_claims_live` is a view over a view, so without
    it the planner may flatten the EXISTS back into a hash semi-join over every claim."""
    flat = " ".join(drain._FULL_SWEEP_KRAJE_SQL.split()).lower()
    assert "with stale as materialized" in flat
    assert "from listing_location_current p" in flat
    assert "exists (select 1 from location_claims_live c where c.listing_id = s.listing_id)" in flat
    # `listing_id` is the projection's PRIMARY KEY, so DISTINCT is not merely cheaper here
    # than in the old shape — it has nothing left to deduplicate.
    assert "distinct" not in flat
    assert "join location_claims_live" not in flat


def test_kraje_picks_the_scoped_statement_and_passes_the_codes_first():
    state = _state()
    drain.enqueue_full_sweep(_FakeConn(state), policy_version="v1", kraje=(19, 27))
    sql, params = next(
        (text, p) for text, p in state["executed"] if "insert into dirty_locations" in text
    )
    assert "kraj_kod = any(%s::bigint[])" in sql
    assert params[0] == [19, 27]
    assert params[1] == RESOLVER_VERSION


def test_no_kraje_keeps_the_corpus_wide_sweep_that_also_sees_unprojected_rows():
    state = _state(max_listing_id=1_000)
    drain.enqueue_full_sweep(_FakeConn(state), policy_version="v1", kraje=())
    sql, params = next(
        (text, p) for text, p in state["executed"] if text.startswith("insert into dirty_locations")
    )
    assert "kraj_kod" not in sql
    assert "p.listing_id is null" in sql
    assert params[0] == RESOLVER_VERSION


def _sweep_windows(state: dict[str, Any]) -> list[tuple[str, Any]]:
    return [(t, p) for t, p in state["executed"] if t.startswith("insert into dirty_locations")]


def test_the_corpus_wide_sweep_walks_listing_id_windows_each_in_its_own_transaction():
    """The unscoped sweep MUST drive off the claim corpus (a listing with no projection
    exists nowhere else), and that corpus grows ~150k rows an hour under the archive
    sweeps — so one statement over all of it cannot finish under the 900 s ceiling (the
    kraj-scoped cousin already died that way, run 34459466027, 2026-09-10). Windows bound
    the work by id range; one bounded transaction per window means an interrupted sweep
    loses at most a window and re-running is idempotent."""
    state = _state(max_listing_id=600_000)
    drain.enqueue_full_sweep(_FakeConn(state), policy_version="v1", kraje=(), window=250_000)
    windows = _sweep_windows(state)
    assert [(p[-2], p[-1]) for _, p in windows] == [
        (0, 250_000), (250_000, 500_000), (500_000, 600_000),
    ]
    assert all("c.listing_id > %s and c.listing_id <= %s" in t for t, _ in windows)
    assert state["transactions"] == len(windows)
    # The bound is an index probe on listings' primary key, never max() over the claims view.
    assert any(t.startswith("select coalesce(max(id), 0) from listings") for t, _ in state["executed"])
    assert not any("max(" in t and "location_claims" in t for t, _ in state["executed"])
    flat = " ".join(drain._FULL_SWEEP_SQL.split()).lower()
    assert flat.count("%s") == 5


def test_both_sweep_statements_skip_rows_already_queued_without_touching_their_locks():
    """Since 8b a drain slice is almost always in flight, holding its rows FOR UPDATE; an
    INSERT ... ON CONFLICT on one of those keys waits for that transaction and the 5 s
    lock_timeout kills the window. The NOT EXISTS is an MVCC read: skip, never wait."""
    for sql, placeholders in ((drain._FULL_SWEEP_SQL, 5), (drain._FULL_SWEEP_KRAJE_SQL, 4)):
        flat = " ".join(sql.split()).lower()
        assert "not exists (select 1 from dirty_locations d where d.listing_id =" in flat
        assert "on conflict (listing_id) do nothing" in flat
        assert flat.count("%s") == placeholders


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


def test_main_enqueues_the_full_sweep_before_the_drain_lease_is_even_attempted(monkeypatch):
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
    assert drain.enqueue_full_sweep(_FakeConn(state), policy_version="v1", kraje=()) == 0
    assert _sweep_windows(state) == []
    import pytest
    with pytest.raises(ValueError):
        drain.enqueue_full_sweep(_FakeConn(_state(max_listing_id=5)), policy_version="v1", kraje=(), window=0)


def test_the_kraje_argument_parses_a_comma_separated_workflow_input():
    assert drain._parse_kraje("") == ()
    assert drain._parse_kraje("19,27") == (19, 27)
    assert drain._parse_kraje("19, 27") == (19, 27)


# ------------------------------------------------------------ the drain's round-trip budget
#
# The drain's cost is POOLER ROUND TRIPS, not CPU: the first production run measured ~3 s per
# listing (~28 h for the 34 k queue, ~23 days for the corpus) on ~33 statements per listing.
# The tests below pin the three structural properties that removed most of them — none of
# which may change what the pure core is handed.


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
            self._result = [(7, "2026-07")]
        elif "from pin_cluster_epochs" in text:
            self._result = [(11,)]
        elif "from location_constants" in text:
            self._result = [("cz_bbox", None, 12.0, 48.0, 19.0, 51.5)]
        elif "from location_granularity_rank" in text:
            self._result = [("obec", 3)]
        elif "from location_collision_policy" in text:
            self._result = [("v1", "*", None, 4, 0, 2, "suspect")]
        elif text.startswith("select listing_id, attempts from dirty_locations"):
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
    state: dict[str, Any] = {"executed": [], "transactions": 0, "slices": list(slices)}
    state["stats"] = drain.run(_DrainConn(state), batch_size=10, max_seconds=30)
    return state


def _count(state: dict[str, Any], needle: str) -> int:
    return sum(1 for text, _ in state["executed"] if needle in text)


def test_the_corpus_constants_are_read_once_per_run_not_once_per_listing():
    """Policies, constants, granularity ranks, the current registry version and the epoch are
    all pinned or operator-curated: they cannot change under a run. Re-reading any of them per
    listing is a pure round trip, and round trips are the whole cost model here."""
    state = _drained([[(101, 0), (102, 0), (103, 0)], [(104, 0), (105, 0)]])
    for needle in (
        "from registry_versions",
        "from pin_cluster_epochs",
        "from location_constants",
        "from location_granularity_rank",
        "from location_field_policy",
        "from location_uncertainty_policy",
        "from location_collision_policy",
    ):
        assert _count(state, needle) == 1, needle


def test_the_per_listing_reads_are_prefetched_once_per_slice():
    """Claims, the previous consumed inputs, `listings.property_id` and the open findings are
    all readable BEFORE the slice writes anything, so they cost one query per SLICE. Five
    listings over two slices means two of each — never five."""
    state = _drained([[(101, 0), (102, 0), (103, 0)], [(104, 0), (105, 0)]])
    assert state["stats"].claimed == 5
    for needle in (
        "from location_claims_live where listing_id = any(",
        "join location_resolutions r on r.id = p.resolution_id where p.listing_id = any(",
        "select id, property_id from listings where id = any(",
        "from location_contradictions_open c where c.listing_id = any(",
    ):
        assert _count(state, needle) == 2, needle
    # ...and never the single-listing forms the prefetch replaced.
    assert _count(state, "from location_claims_live where listing_id = %s") == 0


def test_location_disputed_is_read_after_the_run_writes_its_contradictions():
    """The ONE read that may NOT be prefetched: it is a read-your-writes read of the
    contradictions written a few statements earlier, so a slice-start snapshot would serve a
    projection that denies a major finding this very run raised. Batching the write side did
    not move it — it moved from once per listing to once per slice, still AFTER the
    contradictions and the auto-closes."""
    body = inspect.getsource(drain._write_slice)
    assert body.index("write_contradictions_bulk") < body.index("location_disputed_bulk")
    assert body.index("append_auto_close") < body.index("location_disputed_bulk")
    assert body.index("location_disputed_bulk") < body.index("build_listing_row")
    assert "location_disputed" not in inspect.getsource(drain._prefetch)


def test_the_slice_write_order_survives_batching():
    """Batching may reorder statements WITHIN a stage; it may not reorder the stages. Each
    of these is a data dependency: candidates need the resolution's id, the property rebuild
    reads back the listing projections this slice just wrote."""
    body = inspect.getsource(drain._write_slice)
    assert body.index("write_resolutions_bulk") < body.index("write_candidates_bulk")
    assert body.index("upsert_listing_projections_bulk") < body.index("_rebuild_properties")


def test_a_poisoned_slice_falls_back_to_per_listing_savepoints():
    """The optimistic slice is ONE savepoint, so a single bad listing rolls back 250 rows.
    That is only acceptable because the retry is the per-listing path with a SAVEPOINT each —
    without it the batch write would be a regression on rule "one bad row, one bad row"."""
    body = inspect.getsource(drain._run_slice)
    assert "_write_slice" in body
    assert "conn.transaction()" in body
    assert "_FAIL_ROW_SQL" in body
    assert body.index("_write_slice") < body.index("_resolve_one")


def test_the_slice_is_computed_before_anything_is_written():
    """`_compute_one` is the pure half — it may not write, or the optimistic batch could not
    roll back cleanly and the fallback would double-write."""
    body = inspect.getsource(drain._compute_one)
    for writer in ("write_resolutions_bulk", "upsert_", "_DELETE_ROW", "executemany"):
        assert writer not in body, writer


# ------------------------------------------------------------------ the run-scoped registry


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


def test_the_registry_cache_asks_each_distinct_question_exactly_once():
    """The mirror is immutable at a pinned `registry_version_id`, so the same question has one
    answer for the whole run — and the resolver asks `streets_in_obec(Praha)` twice per Prague
    listing plus once more from the reconciler."""
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
    the resolution it produces must be byte-identical to the one the bare mirror produces."""
    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
        mm.claim(3, "psc", value_text="160 00"),
        mm.claim(4, "coordinate", lat=50.10102, lon=14.34804,
                 declared_precision_label="gps"),
        mm.claim(5, "cast_obce_name", value_text="Vokovice"),
    ]

    def _resolve(registry: Any) -> Any:
        return core.resolve(
            claims, mm.context(registry), resolver_version=RESOLVER_VERSION,
            registry_version_id=7, policy_version="v1", collision_epoch_id=11,
        )

    bare = _resolve(mm.default_mirror())
    cached_view = resolve_db.CachedRegistryView(mm.default_mirror(), resolve_db.RunCache())
    assert _resolve(cached_view).content_hash == bare.content_hash
    # And again on the SAME warm cache — a second listing must not see a mutated answer.
    assert _resolve(cached_view).content_hash == bare.content_hash


def test_the_cache_memory_rail_cannot_change_an_answer():
    """`max_entries` drops the whole memo when it fills. Correctness may not depend on what
    happens to be resident."""
    inner = _CountingMirror()
    view = resolve_db.CachedRegistryView(inner, resolve_db.RunCache(max_entries=1))
    answers = {code: view.streets_in_obec(code) for code in (554782, 599212)}
    for code, expected in answers.items():
        assert view.streets_in_obec(code) == expected


# -------------------------------------------------------------------- the connection mode


def test_the_drain_opens_the_session_pooler_connection(monkeypatch):
    """`prepare_threshold=None` on the transaction pooler re-parses and re-plans every one of
    the ~40 recurring statements on every listing; the session pooler's dedicated backend lets
    psycopg prepare them once. Same pattern as the scraper's hot detail-write loop."""
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


# ------------------------------------------------------------------- auto-close inputs


def _resolution(**overrides: Any) -> Any:
    values = {
        "claim_set_hash": "aa", "registry_version_id": 7, "policy_version": "v1",
        "collision_epoch_id": 11,
    }
    values.update(overrides)
    return type("R", (), values)()


def test_auto_close_is_silent_when_the_inputs_did_not_change():
    """00 §8.2: "a re-run that merely happens again closes nothing". The guard used to be
    hard-wired True, so every drain pass retired every finding whose predicate did not
    re-fire — including ones that stopped firing because an INPUT WENT MISSING."""
    previous = ("aa", 7, "v1", 11)
    assert drain._inputs_changed(previous, _resolution()) is False


def test_each_of_the_four_consumed_inputs_changing_counts():
    previous = ("aa", 7, "v1", 11)
    for field, value in (
        ("claim_set_hash", "bb"), ("registry_version_id", 8),
        ("policy_version", "v2"), ("collision_epoch_id", 12),
    ):
        assert drain._inputs_changed(previous, _resolution(**{field: value})) is True


def test_with_no_previous_projection_nothing_is_closed():
    """No evidence of what was consumed before is not evidence of change."""
    assert drain._inputs_changed(None, _resolution()) is False


def test_open_keys_can_be_scoped_to_the_rules_a_run_evaluated():
    flat = " ".join(resolve_db._OPEN_KEYS_SQL.split()).lower()
    assert "c.rule = any(" in flat


# ------------------------------------------------- which rules a run actually evaluated


def _fake_resolution(street: str | None, obec_kod: int | None) -> Any:
    fields = {}
    if street is not None:
        fields["street_name"] = type(
            "W", (), {"value": street, "method": "portal_structured_field",
                      "rule": "policy:v1:rank300:sreality", "source_claim_ids": (1,)}
        )()
    admin = type("A", (), {"obec_name": "Praha", "obec_kod": obec_kod})()
    precision = Precision(
        granularity="obec", position_source="portal_pin", match_confidence="medium",
        blur_evidence="none", uncertainty_radius_m=1000.0,
        radius_semantics="geometric_bound", position_quality_class="area",
        collision={"n_exact": 1, "threshold_n": 4, "heterogeneity": 0},
    )
    return type(
        "R", (),
        {"listing_id": 1, "fields": fields, "admin": admin, "precision": precision,
         "contradiction_signals": (), "candidates": ()},
    )()


def test_a_rule_whose_guard_never_ran_is_not_reported_as_evaluated():
    """`street_not_in_obec` cannot have "stopped firing" on a run where survivorship
    produced no street at all — it was not asked."""
    _, evaluated = reconciler.run_with_coverage(
        _fake_resolution(None, 554782), [], {}, registry=None
    )
    assert "street_not_in_obec" not in evaluated
    assert "house_number_disagreement" in evaluated  # unguarded, always evaluated


def test_the_same_rule_is_reported_when_its_guard_did_run():
    class _Registry:
        def streets_in_obec(self, obec_kod: int):
            return []

    _, evaluated = reconciler.run_with_coverage(
        _fake_resolution("Nad Bořislavkou", 554782), [], {}, registry=_Registry()
    )
    assert "street_not_in_obec" in evaluated


def test_the_registry_street_override_is_evaluated_whenever_it_ran():
    """Its findings are opened by S7, not by a reconciler detector, so without this the rule
    is never in the evaluated set and an open `street_form_registry_override` could never
    auto-close once the portal fixed its spelling."""
    class _Registry:
        def streets_in_obec(self, obec_kod: int):
            return []

    resolution = _fake_resolution("Nad Bořislavkou", 554782)
    assert "street_form_registry_override" not in reconciler.run_with_coverage(
        resolution, [], {}, registry=_Registry()
    )[1]
    resolution.fields["street_name"].rule = "registry:street"
    _, evaluated = reconciler.run_with_coverage(
        resolution, [], {}, registry=_Registry()
    )
    assert "street_form_registry_override" in evaluated


# ------------------------------------------------------- one threshold, one comparison


def test_the_epoch_classifier_and_the_s6_cap_read_the_threshold_the_same_way():
    """03 §3.8.4 states the rule as `n >= threshold` with >=2 distinct streets. A cluster of
    EXACTLY `threshold_n` used to be capped at `obec` by S6 while the epoch went on calling
    it `normal` — the projection then served an `area`-grade pin badged as fine."""
    policy = collision.CollisionPolicyRow("v1", "*", None, 4, 0, 2, "suspect")
    pins = [
        collision.PinRow(listing_id=i, source="bazos", lat=50.0, lon=14.0,
                         street_key=f"ulice {i}", obec_kod=554782)
        for i in range(1, 5)  # exactly threshold_n
    ]
    cluster = collision.build_clusters(pins, (policy,))[0]
    assert cluster.listing_count == 4
    assert cluster.classification == "parser_collapse_suspect"

    from location_data.resolver import precision as s6
    from location_data.resolver.types import ClusterEvidence

    evidence = ClusterEvidence(
        cluster_id=1, source="bazos", cell_key=cluster.cell_key, listing_count=4,
        distinct_streets=4, distinct_obec_kods=1, classification=cluster.classification,
    )
    assert s6.cluster_caps(evidence, policy) == ("obec", True)


# --------------------------------------------------- the projection carries both columns


def test_the_projection_upsert_writes_the_two_derived_columns():
    """03 §3.10 requires both; the builder computed them and the writer popped them, so
    `position_quality_class` — the ONE gate for metric-radius membership — was never
    stored, and `property_location_current` picked its winner on a constant."""
    flat = " ".join(resolve_db._UPSERT_LISTING_PROJECTION_SQL.split()).lower()
    for column in ("position_quality_class", "collision_epoch_id"):
        assert f"%({column})s" in flat
        assert f"{column} = excluded.{column}" in flat
    members = " ".join(drain._PROPERTY_MEMBERS_BULK_SQL.split()).lower()
    assert "position_quality_class" in members
    assert "position_quality_class" in drain._MEMBER_FIELDS


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

    def cast_obce_for_point_bulk(self, coords):
        found = {i: self.inner.cast_obce_for_point(*c) for i, c in enumerate(coords)}
        return {i: unit for i, unit in found.items() if unit is not None}

    def distance_to_admin_boundary_m_bulk(self, keys):
        found = {
            i: self.inner.distance_to_admin_boundary_m(unit_id, lat, lon)
            for i, (unit_id, lat, lon) in enumerate(keys)
        }
        return {i: value for i, value in found.items() if value is not None}


class _BulkCollision:
    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def for_point_bulk(self, points):
        found = {i: self.inner.for_point(*p) for i, p in enumerate(points)}
        return {i: value for i, value in found.items() if value is not None}


def _warm_claims():
    return [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
        mm.claim(3, "psc", value_text="160 00"),
        mm.claim(4, "coordinate", lat=50.10102, lon=14.34804,
                 declared_precision_label="gps"),
        mm.claim(5, "cast_obce_name", value_text="Vokovice"),
    ]


def _resolve_with(registry: Any) -> Any:
    return core.resolve(
        _warm_claims(), mm.context(registry), resolver_version=RESOLVER_VERSION,
        registry_version_id=7, policy_version="v1", collision_epoch_id=11,
    )


def test_a_warmed_run_replays_bit_for_bit_against_an_unwarmed_one():
    """THE constraint on warming: it is an I/O-layer concern, so pre-seeding the memo must
    produce the byte-identical resolution a cold run produces. `warm_points` writes exactly
    the key/value `CachedRegistryView` would have computed — that is the whole claim, and it
    is the one worth a gate."""
    bare = _resolve_with(mm.default_mirror())

    cache = resolve_db.RunCache()
    mirror = mm.default_mirror()
    resolve_db.warm_points(
        _BulkMirror(mirror), _BulkCollision(mm.StaticCollision()), cache,
        [("sreality", 50.10102, 14.34804)],
    )
    warmed = resolve_db.CachedRegistryView(mirror, cache)
    assert _resolve_with(warmed).content_hash == bare.content_hash


def test_warming_actually_serves_the_coordinate_keyed_questions():
    """A warm that produced identical bytes by simply never being consulted would pass the
    replay gate and buy nothing — the point is that the pin's questions are ANSWERED from the
    memo, i.e. asked and never missed."""
    cache = resolve_db.RunCache()
    inner = _CountingMirror()
    resolve_db.warm_points(
        _BulkMirror(mm.default_mirror()), _BulkCollision(mm.StaticCollision()), cache,
        [("sreality", 50.10102, 14.34804)],
    )
    _resolve_with(resolve_db.CachedRegistryView(inner, cache))
    # S2 asks this of the pin on every listing that has one.
    assert cache.asked_by_kind.get("in_czechia_polygon", 0) > 0
    assert cache.missed_by_kind.get("in_czechia_polygon", 0) == 0
    # ...and the questions this fixture's address match happens not to reach are warmed all
    # the same, so the listings that DO reach them (no RUIAN address hit — the majority) pay
    # nothing either.
    view = resolve_db.CachedRegistryView(inner, cache)
    before = inner.calls
    view.containing_obec(50.10102, 14.34804)
    view.cast_obce_for_point(50.10102, 14.34804)
    assert inner.calls == before


def test_a_null_answer_is_warmed_too():
    """Otherwise every rural point — no obec polygon, no address point within 250 m — would
    fall through to a per-call query and the warm would help exactly the listings that need
    it least."""
    cache = resolve_db.RunCache()
    resolve_db.warm_points(
        _BulkMirror(mm.default_mirror()), _BulkCollision(mm.StaticCollision()), cache,
        [("sreality", 0.0, 0.0)],
    )
    calls = _CountingMirror()
    view = resolve_db.CachedRegistryView(calls, cache)
    assert view.containing_obec(0.0, 0.0) is None
    assert view.cast_obce_for_point(0.0, 0.0) is None
    assert calls.calls == 0


def test_the_cache_reports_which_question_misses():
    """Round 1 logged one aggregate hit rate, so round 2 had to re-derive by hand which
    question the 38 % of misses were. Per-kind is the difference between "the cache is at
    62 %" and "the misses are the five coordinate-keyed questions, warm them"."""
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


def test_a_failed_warm_degrades_instead_of_ending_the_run():
    """The warm runs INSIDE the batch transaction, so an unguarded statement timeout there
    would abort the batch and take the whole run with it. It is an optimisation: rolled back,
    every point falls through to its own lazy query."""
    body = inspect.getsource(drain.run)
    warm = body[body.index("warm_started"):body.index("_run_slice(")]
    assert "try:" in warm and "conn.transaction()" in warm
    assert "WARM failed" in warm
