"""The two resolver JOBS and the seams between them (03 §3.8.4, §3.14, 00 §8.2).

These are the parts a pure-core test cannot reach: what the epoch job is allowed to mint,
what the drain claims and in what order, and when a contradiction may be auto-closed. The
SQL-text assertions are deliberate — a fake connection cannot tell you whether a query
joins `listings`, and the schema-replay job is the only other place that would notice.
"""

from __future__ import annotations

import inspect
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


def _state(pins: list[tuple[Any, ...]] | None = None) -> dict[str, Any]:
    return {"executed": [], "transactions": 0, "pins": pins or []}


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
    """The ONE per-listing read that may NOT be prefetched: it is a read-your-writes read of
    the contradictions written a few statements earlier, so a slice-start snapshot would serve
    a projection that denies a major finding this very run raised."""
    body = inspect.getsource(drain._resolve_one)
    assert body.index("write_contradictions") < body.index("location_disputed(")
    assert "location_disputed" not in inspect.getsource(drain._prefetch)


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
                      "source_claim_ids": (1,)}
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
    members = " ".join(drain._PROPERTY_MEMBERS_SQL.split()).lower()
    assert "position_quality_class" in members
