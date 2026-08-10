"""The two resolver JOBS and the seams between them (03 §3.8.4, §3.14, 00 §8.2).

These are the parts a pure-core test cannot reach: what the epoch job is allowed to mint,
what the drain claims and in what order, and when a contradiction may be auto-closed. The
SQL-text assertions are deliberate — a fake connection cannot tell you whether a query
joins `listings`, and the schema-replay job is the only other place that would notice.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from location_data.resolver import collision, drain, epoch_job, reconciler, resolve_db
from location_data.resolver.types import Precision


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
