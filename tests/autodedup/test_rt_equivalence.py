"""W9j — `--mode rt_equivalence`: the LIVE store against the batch engine (E99).

The replay-equivalence proof runs the incremental path against the batch engine over one
artifact, in one process, from an EMPTY store. It proves the MECHANISM and it has proved it
four times. What it cannot see is what a real generation HOLDS — which is where both of this
lane's shipped defects lived: a live pass that issued zero K-C against the batch generation's
1,771 (E90), and a re-seed that left 15,923 pairs of a superseded scorer behind (E97).

So this mode reads both stores and diffs them: the pairs both sides hold, the pairs one side
holds with the cause it holds them by, and the clusters trimmed to the scope. A one-sided pair
with no cause is `unexplained`, and `unexplained` fails the verdict — which is the difference
between a check and a description.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from autodedup.incremental_lane import parity_baseline_key, scope_setting_key
from autodedup.rt_equivalence import EQUIVALENCE_FILE, run_equivalence
from tests.autodedup.fake_pg import FakePg

LIVE = "rt"
BATCH = "g6"
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
EXPORTED = NOW - timedelta(days=1)


def _pair(score: float = 0.9, zone: str = "merge", certificate: str | None = "K-C",
          **kw: Any) -> dict:
    row = {"probes": ["K1"], "from_lo": True, "from_hi": True, "families": 3,
           "certificate": certificate, "features": {}, "fp_lo": "a", "fp_hi": "b",
           "score": score, "zone": zone, "decision": zone, "guard_veto": None,
           "evidence": {}, "context": {}, "calibration_digest": "d",
           "feature_version": 1, "model_version": "w6_gold", "cluster_key": None}
    row.update(kw)
    return row


def _fp_row() -> dict:
    return {"category_main": None, "category_type": None, "area_m2": None,
            "disposition": None, "floor": None, "fp_digest": "d", "cell_key": "o1",
            "cell_group": "byt", "is_active": True, "ev_images": 0, "ev_phash": 0,
            "ev_clip": 0, "ev_tags": 0, "ev_complete": True, "first_decided_at": NOW}


def _db(ids: range = range(1, 5)) -> FakePg:
    """A live generation seeded on a two-block scope, with `ids` inside it."""
    db = FakePg(now=NOW)
    db.calibration[LIVE] = {"digest": "d", "n_listings": len(ids), "payload": {},
                            "artifact_url": None, "settings": {"store_floor": 0.02},
                            "model_version": "w6_gold"}
    db.settings[scope_setting_key(LIVE)] = [{"grain": "obec", "code": 563510}]
    db.settings[parity_baseline_key(LIVE)] = {"rows": {}, "n": 0,
                                              "exported_at": EXPORTED.isoformat()}
    for listing_id in ids:
        db.scope_ids[(LIVE, "obec:563510", listing_id)] = {"resolved_at": NOW}
        db.rt_fp[(LIVE, listing_id)] = _fp_row()
        db.listings[listing_id] = {"id": listing_id, "first_seen_at": EXPORTED - timedelta(days=5)}
    return db


def _run(db: FakePg, tmp_path, **args: str) -> dict:
    return run_equivalence(lambda: db, {"generation": LIVE, "batch": BATCH, **args}, tmp_path)


# ------------------------------------------------------------------ the agreeing case


def test_two_stores_that_agree_pass(tmp_path) -> None:
    db = _db()
    for key in ((1, 2), (3, 4)):
        db.pairs[(LIVE, *key)] = _pair()
        db.pairs[(BATCH, *key)] = _pair()
    for generation in (LIVE, BATCH):
        db.cluster_members.add((generation, 77, 1))
        db.cluster_members.add((generation, 77, 2))

    out = _run(db, tmp_path)

    assert out["verdict"] == {"ok": True, "reasons": []}
    assert out["pairs"]["both"] == 2 and out["pairs"]["differing"] == 0
    assert out["clusters"]["member_sets_identical"] is True
    assert out["clusters"]["keys_identical"] is True
    assert json.loads((tmp_path / EQUIVALENCE_FILE).read_text())["verdict"]["ok"] is True


def test_the_mode_writes_nothing(tmp_path) -> None:
    """Not a `public` row, not an `autodedup` row. The instrument's whole contract."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair()
    db.pairs[(BATCH, 1, 2)] = _pair()
    _run(db, tmp_path)

    assert not [sql for sql in db.statements
                if sql.strip().lower().startswith(("insert", "update", "delete", "with"))]


# ------------------------------------------------------------------ the disagreeing case


@pytest.mark.parametrize("field,changed", [
    ("zone", {"zone": "band", "decision": "band"}),
    ("certificate", {"certificate": "K-B"}),
])
def test_a_decision_that_moved_is_counted_by_field(tmp_path, field, changed) -> None:
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair()
    db.pairs[(BATCH, 1, 2)] = _pair(**changed)

    out = _run(db, tmp_path)

    assert out["pairs"]["differing"] == 1
    assert out["pairs"]["decisions_moved"] == 1
    assert out["pairs"]["by_field"][field] == 1
    assert out["verdict"]["ok"] is False
    assert out["pairs"]["differing_examples"][0]["moved"] == [field]


def test_a_score_that_moved_under_the_same_decision_is_reported_not_failed(tmp_path) -> None:
    """The batch pass calibrates over its whole cohort and the real-time generation over its
    scope, so scores differ a little under identical decisions (measured: 61 pairs, max 0.0158)."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair()
    db.pairs[(BATCH, 1, 2)] = _pair(score=0.9 + 1e-3)

    out = _run(db, tmp_path)

    assert out["pairs"]["differing"] == 1
    assert out["pairs"]["decisions_moved"] == 0
    assert out["pairs"]["score_only"] == 1
    assert out["verdict"]["ok"] is True


def test_a_large_score_movement_fails_even_with_the_decision_unchanged(tmp_path) -> None:
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair(score=0.99)
    db.pairs[(BATCH, 1, 2)] = _pair(score=0.90)

    out = _run(db, tmp_path)

    assert out["pairs"]["score_only_max"] > 0.05
    assert out["verdict"]["ok"] is False


def test_two_empty_stores_do_not_pass(tmp_path) -> None:
    """Every other criterion is true of two empty stores; a comparison of nothing is not a pass."""
    out = _run(_db(), tmp_path)

    assert out["pairs"]["both"] == 0
    assert out["verdict"]["ok"] is False


def test_a_score_inside_the_tolerance_is_not_a_difference(tmp_path) -> None:
    """The two sides run the same model on the same features in the same float64: a 1e-9
    difference is arithmetic, and only a difference in the INPUTS is a finding."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair(score=0.9)
    db.pairs[(BATCH, 1, 2)] = _pair(score=0.9 + 1e-9)

    assert _run(db, tmp_path)["pairs"]["differing"] == 0


def test_the_2026_09_20_defect_is_what_this_mode_reports(tmp_path) -> None:
    """The live store held 15,923 pairs with `model_version` NULL on every row, written by a
    pass running a scorer nobody chose, and every later pass left them alone because their
    fingerprint digests still agreed. The mode names it twice: scores that moved, and a
    model version that is not the batch generation's."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair(score=0.41, zone="band", certificate=None,
                                   model_version=None)
    db.pairs[(BATCH, 1, 2)] = _pair(score=0.98, zone="merge", certificate="K-C")

    out = _run(db, tmp_path)

    assert out["pairs"]["differing"] == 1
    assert sorted(out["pairs"]["by_field"]) == ["certificate", "score", "zone"]
    assert out["model_version"]["live_pairs"] == []
    assert out["model_version"]["batch_pairs"] == ["w6_gold"]
    assert out["verdict"]["ok"] is False


# ------------------------------------------------------------------ one-sided pairs, by cause


def test_a_batch_pair_outside_the_scope_is_explained(tmp_path) -> None:
    """The batch cohort carries two whole towns and the assembled negative control; the live
    scope carries one block of it. A pair the scope never held was never this lane's."""
    db = _db()
    db.listings[900] = {"id": 900, "first_seen_at": EXPORTED - timedelta(days=5)}
    db.pairs[(BATCH, 1, 900)] = _pair()
    db.pairs[(LIVE, 1, 2)] = _pair()
    db.pairs[(BATCH, 1, 2)] = _pair()

    out = _run(db, tmp_path)

    assert out["pairs"]["causes"] == {"scope": 1}
    assert out["verdict"]["ok"] is True


def test_a_listing_the_build_has_not_reached_is_explained_and_fails_the_verdict(tmp_path):
    """Explained is not the same as fine: a one-sided pair touching a listing the live store
    has never fingerprinted says the build is not finished, and a verdict taken mid-build
    would read as a disagreement that is really an incomplete build."""
    db = _db()
    db.scope_ids[(LIVE, "obec:563510", 9)] = {"resolved_at": NOW}
    db.listings[9] = {"id": 9, "first_seen_at": EXPORTED - timedelta(days=5)}
    db.pairs[(BATCH, 1, 9)] = _pair()

    out = _run(db, tmp_path)

    assert out["pairs"]["causes"] == {"not_in_live_store": 1}
    assert out["verdict"]["ok"] is False
    assert "the build is not finished" in " ".join(out["verdict"]["reasons"])


def test_a_pair_on_a_listing_that_arrived_after_the_export_is_explained(tmp_path) -> None:
    db = _db()
    db.scope_ids[(LIVE, "obec:563510", 9)] = {"resolved_at": NOW}
    db.rt_fp[(LIVE, 9)] = _fp_row()
    db.listings[9] = {"id": 9, "first_seen_at": EXPORTED + timedelta(hours=3)}
    db.pairs[(LIVE, 1, 9)] = _pair()
    db.pairs[(LIVE, 1, 2)] = _pair()
    db.pairs[(BATCH, 1, 2)] = _pair()

    out = _run(db, tmp_path)

    assert out["pairs"]["causes"] == {"arrival_after_export": 1}
    assert out["verdict"]["ok"] is True


def test_the_store_floor_and_retention_explain_the_reject_tail(tmp_path) -> None:
    """`storable` keeps the merge and band zones whatever they score and the reject tail only
    at or above the floor, so a reject is exactly the row the floor decides. A row already
    BELOW the floor is in the store because nothing has re-scored it yet, not because the two
    engines disagree."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair(score=0.05, zone="reject", certificate=None)
    db.pairs[(BATCH, 3, 4)] = _pair(score=0.004, zone="reject", certificate=None)
    db.pairs[(LIVE, 2, 3)] = _pair()
    db.pairs[(BATCH, 2, 3)] = _pair()

    out = _run(db, tmp_path)

    assert out["pairs"]["causes"] == {"store_floor": 1, "retention": 1}
    assert out["verdict"]["ok"] is True


def test_a_one_sided_merge_with_no_cause_fails_the_verdict(tmp_path) -> None:
    """The finding this mode exists for: a MERGE one store holds and the other does not, on
    two listings both stores know, inside the scope, from before the export."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair()

    out = _run(db, tmp_path)

    assert out["pairs"]["causes"] == {"unexplained": 1}
    assert out["pairs"]["unexplained_examples"][0]["side"] == "live"
    assert out["verdict"]["ok"] is False


# ------------------------------------------------------------------ clusters


def test_a_member_set_that_differs_inside_the_scope_fails(tmp_path) -> None:
    db = _db()
    db.cluster_members.update({(LIVE, 77, 1), (LIVE, 77, 2)})
    db.cluster_members.update({(BATCH, 77, 1), (BATCH, 77, 2), (BATCH, 77, 3)})

    out = _run(db, tmp_path)

    assert out["clusters"]["member_sets_identical"] is False
    assert out["clusters"]["only_live"] == 1 and out["clusters"]["only_batch"] == 1
    assert out["verdict"]["ok"] is False


def test_a_batch_cluster_is_trimmed_to_the_scope_before_it_is_compared(tmp_path) -> None:
    """A batch cluster spanning the negative control would otherwise read as a difference on
    every single comparison, which is a report nobody can act on."""
    db = _db()
    db.cluster_members.update({(LIVE, 77, 1), (LIVE, 77, 2)})
    db.cluster_members.update({(BATCH, 77, 1), (BATCH, 77, 2), (BATCH, 77, 900)})

    out = _run(db, tmp_path)

    assert out["clusters"]["member_sets_identical"] is True
    assert out["clusters"]["shared"] == 1


def test_a_cluster_key_that_moved_is_reported_without_failing_the_member_sets(tmp_path):
    db = _db()
    db.cluster_members.update({(LIVE, 77, 1), (LIVE, 77, 2)})
    db.cluster_members.update({(BATCH, 78, 1), (BATCH, 78, 2)})

    out = _run(db, tmp_path)

    assert out["clusters"]["member_sets_identical"] is True
    assert out["clusters"]["keys_identical"] is False


# ------------------------------------------------------------------ arguments


def test_the_batch_generation_is_required(tmp_path) -> None:
    db = _db()
    with pytest.raises(SystemExit, match="batch=<generation> is required"):
        run_equivalence(lambda: db, {"generation": LIVE}, tmp_path)


def test_a_generation_compared_with_itself_is_refused(tmp_path) -> None:
    db = _db()
    with pytest.raises(SystemExit, match="trivially"):
        run_equivalence(lambda: db, {"generation": LIVE, "batch": LIVE}, tmp_path)


def test_an_unseeded_generation_is_refused(tmp_path) -> None:
    db = FakePg(now=NOW)
    with pytest.raises(SystemExit, match="no frozen calibration"):
        run_equivalence(lambda: db, {"generation": LIVE, "batch": BATCH}, tmp_path)
