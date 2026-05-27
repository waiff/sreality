"""Tests for the Tier-2 sweep classifier + dispatch (scripts.dedup_sweep).

classify_pair is the auto-merge policy in one pure function, so it gets the bulk
of the coverage. One monkeypatched run_sweep test checks the dispatch wiring
(tight+corroborated -> merge; otherwise -> queue) without touching a DB.
"""

from __future__ import annotations

from typing import Any

import scripts.dedup_sweep as sweep
from scripts.dedup_sweep import PairSignals, classify_pair


def _sig(**kw: Any) -> PairSignals:
    base = dict(
        distance_m=10.0, price_a=1_000_000, price_b=1_010_000,
        area_a=50.0, area_b=50.5, disposition_a="2+kk", disposition_b="2+1",
        address_similarity=0.5,
    )
    base.update(kw)
    return PairSignals(**base)


def test_reject_incompatible_disposition():
    assert classify_pair(_sig(disposition_a="2+kk", disposition_b="3+kk")).action == "reject"


def test_reject_missing_area():
    assert classify_pair(_sig(area_b=None)).action == "reject"


def test_reject_too_far():
    assert classify_pair(_sig(distance_m=200.0)).action == "reject"


def test_reject_price_drift_too_large():
    assert classify_pair(_sig(price_a=1_000_000, price_b=1_200_000)).action == "reject"


def test_auto_merge_on_tight_plus_address():
    d = classify_pair(_sig(address_similarity=0.95))
    assert d.action == "auto_merge"
    assert d.corroborator == "address"


def test_auto_merge_on_tight_plus_vision():
    d = classify_pair(_sig(address_similarity=0.4, vision_similarity=0.9))
    assert d.action == "auto_merge"
    assert d.corroborator == "vision"


def test_queue_when_tight_but_no_corroborator():
    d = classify_pair(_sig(address_similarity=0.4))
    assert d.action == "queue"
    assert d.corroborator is None


def test_queue_when_corroborated_but_not_tight():
    # 100m is inside the 150m generation gate but outside the 30m auto gate.
    d = classify_pair(_sig(distance_m=100.0, address_similarity=0.95))
    assert d.action == "queue"


def test_disposition_loose_equivalence_is_compatible():
    # 2+kk vs 2+1 are loose-equivalent, so this is a real auto-merge candidate.
    assert classify_pair(_sig(disposition_a="2+kk", disposition_b="2+1",
                              address_similarity=0.95)).action == "auto_merge"


# --- dispatch wiring ------------------------------------------------------


def test_run_sweep_auto_merges_tight_pair(monkeypatch):
    pair = {
        "a_id": 1, "b_id": 2, "distance_m": 10.0,
        "a_price": 1_000_000, "b_price": 1_010_000,
        "a_area": 50.0, "b_area": 50.5, "a_disp": "2+kk", "b_disp": "2+1",
        "a_locality": "Praha", "a_district": "Praha 2",
        "b_locality": "Praha", "b_district": "Praha 2",
        "a_first_seen": 1, "b_first_seen": 2, "a_repr": 100, "b_repr": -5,
    }
    monkeypatch.setattr(sweep, "generate_candidate_pairs", lambda *a, **k: [pair])
    merges: list[dict[str, Any]] = []
    monkeypatch.setattr(
        sweep, "merge_properties",
        lambda conn, **kw: merges.append(kw) or {"data": {}},
    )

    def _no_queue(*a: Any, **k: Any) -> None:
        raise AssertionError("tight+address pair should auto-merge, not queue")

    monkeypatch.setattr(sweep, "_enqueue_candidate", _no_queue)

    stats = sweep.run_sweep(object(), max_auto_merges=10)

    assert stats["auto_merged"] == 1
    assert merges[0]["survivor_id"] == 1  # older first_seen wins
    assert merges[0]["retired_id"] == 2
    assert merges[0]["source"] == "auto"


def test_run_sweep_queues_overflow_when_cap_reached(monkeypatch):
    pair = {
        "a_id": 1, "b_id": 2, "distance_m": 10.0,
        "a_price": 1_000_000, "b_price": 1_010_000,
        "a_area": 50.0, "b_area": 50.5, "a_disp": "2+kk", "b_disp": "2+1",
        "a_locality": "Praha", "a_district": "Praha 2",
        "b_locality": "Praha", "b_district": "Praha 2",
        "a_first_seen": 1, "b_first_seen": 2, "a_repr": 100, "b_repr": -5,
    }
    monkeypatch.setattr(sweep, "generate_candidate_pairs", lambda *a, **k: [pair])
    monkeypatch.setattr(sweep, "merge_properties",
                        lambda conn, **kw: {"data": {}})
    queued: list[tuple[int, int]] = []
    monkeypatch.setattr(sweep, "_enqueue_candidate",
                        lambda conn, lo, hi, *a, **k: queued.append((lo, hi)))

    stats = sweep.run_sweep(object(), max_auto_merges=0)  # cap forces queue

    assert stats["auto_merged"] == 0
    assert stats["queued"] == 1
    assert queued == [(1, 2)]
