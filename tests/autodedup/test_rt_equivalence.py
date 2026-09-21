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
from typing import Any, Mapping

import pytest

from autodedup.harness import model_of_version
from autodedup.incremental_lane import parity_baseline_key, scope_setting_key
from autodedup.rt_equivalence import (
    EQUIVALENCE_FILE,
    SCORE_DEFECT,
    SCORE_ONLY_MAX,
    run_equivalence,
)
from autodedup.store_score import narrow
from tests.autodedup.fake_pg import FakePg

LIVE = "rt"
BATCH = "g6"
MODEL = "w6_gold"
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
EXPORTED = NOW - timedelta(days=1)


def _score_of(features: Mapping[str, list[Any]] | None) -> float:
    """The score the stored vector IMPLIES — the only score a stored row may carry (E121).

    `decide_pair` sets `score = model.predict_proba(feats)` in every branch but the veto, so a
    fixture that spells a free-floating score is spelling a defect, and the ones below that do
    it say so."""
    vector = {name: (float(entry[0]), bool(entry[1]))
              for name, entry in (features or {}).items()}
    return model_of_version(MODEL).predict_proba(vector)


def _pair(score: float | None = None, zone: str = "merge", certificate: str | None = "K-C",
          **kw: Any) -> dict:
    row = {"probes": ["K1"], "from_lo": True, "from_hi": True, "families": 3,
           "certificate": certificate, "features": {}, "fp_lo": "a", "fp_hi": "b",
           "score": 0.0, "zone": zone, "decision": zone, "guard_veto": None,
           "evidence": {}, "context": {}, "calibration_digest": "d",
           "feature_version": 1, "model_version": MODEL, "cluster_key": None}
    row.update(kw)
    row["score"] = _score_of(row["features"]) if score is None else score
    return row


def _feats(**values: float | None) -> dict[str, list[Any]]:
    """The score lane's stored `{name: [value, present]}` shape (E12)."""
    return {name: ([0.0, False] if value is None else [value, True])
            for name, value in values.items()}


def _listing(listing_id: int, *, first_seen: Any = None, last_seen: Any = None,
             inactive: Any = None, active: bool = True) -> dict:
    """A `public.listings` row as psycopg hands it back — timestamps, not strings."""
    return {"id": listing_id,
            "first_seen_at": first_seen or EXPORTED - timedelta(days=5),
            "last_seen_at": last_seen, "inactive_at": inactive, "is_active": active}


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


def test_a_score_that_moved_on_a_corpus_frequency_feature_is_the_calibration_cohort(tmp_path):
    """The batch pass calibrates over its whole cohort and the real-time generation over its
    scope, so corpus token frequencies reach the features and the scores differ a little under
    identical decisions (measured 2026-09-21: 2,277 pairs move `tfidf_cos`, 61 by enough to
    move a score, max 0.0212, 0 decision moves). Named, so it does not fail the verdict.

    Both scores follow from their own vectors — the movement is the INPUT's (E121)."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair(features=_feats(tfidf_cos=0.9))
    db.pairs[(BATCH, 1, 2)] = _pair(features=_feats(tfidf_cos=0.4))

    out = _run(db, tmp_path)

    assert out["pairs"]["differing"] == 1
    assert out["pairs"]["decisions_moved"] == 0
    assert out["pairs"]["score_only"] == 1
    assert out["pairs"]["shared_causes"] == {"calibration_cohort": 1}
    assert out["verdict"]["ok"] is True


def test_a_score_that_moved_with_an_IDENTICAL_vector_is_unexplained(tmp_path) -> None:
    """The sharpest finding the instrument can make: the same inputs reached two answers.

    One vector cannot imply two scores, so E121 names the side that does not follow from it —
    the two findings are the same defect read at two grains."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair(features=_feats(tfidf_cos=0.4))
    db.pairs[(BATCH, 1, 2)] = _pair(score=0.9 + 1e-3, features=_feats(tfidf_cos=0.4))

    out = _run(db, tmp_path)

    assert out["pairs"]["shared_causes"] == {"unexplained": 1}
    assert out["pairs"]["score_self_consistency"]["defects"] == {"live": 0, "batch": 1}
    assert out["verdict"]["ok"] is False
    assert "differ with no cause" in " ".join(out["verdict"]["reasons"])


def test_a_large_score_movement_fails_even_with_the_decision_unchanged(tmp_path) -> None:
    """Two free-floating scores over one empty vector: unattributed at pair grain, and two
    rows that do not follow from their own vectors at row grain (E121)."""
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
    assert out["model_version"]["batch_pairs"] == [MODEL]
    # A row that does not NAME its scorer cannot be re-scored, so E121 exempts it BY NAME
    # rather than inventing a model for it — and the count is in the report.
    assert out["pairs"]["score_self_consistency"]["exempt"] == {"no_model_version": 1}
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


# --------------------------------------------- W9m: the defects the verdict must NAME (E120)


def test_a_certificate_column_the_batch_lane_never_wrote_is_a_DEFECT(tmp_path) -> None:
    """M171, reproduced. `rt_base_w13` held 0 certificates in the column and named one in
    `decision` on 3,866 rows, so the instrument read 'K-C' against NULL and called 3,462
    shared pairs moved DECISIONS. The column is the definition (D41); the string is the
    witness that it should have been written. It is a defect of the STORE, and the pairs it
    touches are attributed to it rather than counted as disagreements."""
    db = _db()
    for key in ((1, 2), (3, 4)):
        db.pairs[(LIVE, *key)] = _pair(decision="merge certificate:K-C")
        db.pairs[(BATCH, *key)] = _pair(certificate=None, decision="merge certificate:K-C")

    out = _run(db, tmp_path)

    assert [defect["defect"] for defect in out["defects"]] == ["store_write_asymmetry"]
    assert out["defects"][0]["side"] == "batch"
    assert out["defects"][0]["rows_naming_one"] == 2
    assert out["pairs"]["by_field"] == {"certificate": 2}
    assert out["pairs"]["shared_causes"] == {"store_write_asymmetry": 2}
    assert out["verdict"]["ok"] is False
    assert not [reason for reason in out["verdict"]["reasons"] if "DECISION" in reason]


def test_a_generation_that_certified_nothing_is_not_a_defect(tmp_path) -> None:
    """The guard on the guard: a store with no certificates because the engine issued none
    looks identical to one that lost them, unless the decision strings are read."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair(certificate=None, decision="model")
    db.pairs[(BATCH, 1, 2)] = _pair(certificate=None, decision="model")

    assert _run(db, tmp_path)["defects"] == []


def test_two_generations_on_different_clocks_are_a_DEFECT(tmp_path) -> None:
    """`live_window_from_sighting` decides whether a window ends at the last sighting or at
    the delisting stamp, and rule #3's detection lag runs to 70 days. Two generations that do
    not agree about it are not measuring the same thing, and every co-live feature says so."""
    db = _db()
    db.calibration[LIVE]["settings"]["live_window_from_sighting"] = True
    db.score_runs[BATCH] = {"settings": {"live_window_from_sighting": False},
                            "model_version": MODEL}
    db.pairs[(LIVE, 1, 2)] = _pair(features=_feats(both_active=1.0))
    db.pairs[(BATCH, 1, 2)] = _pair(features=_feats(both_active=0.0))

    out = _run(db, tmp_path)

    assert out["clock"] == {"live": True, "batch": False, "one_definition": False}
    assert [defect["defect"] for defect in out["defects"]] == ["clock_anchor"]
    assert out["pairs"]["shared_causes"] == {"clock_anchor": 1}
    assert out["verdict"]["ok"] is False


def test_a_float4_score_column_is_a_DEFECT_and_widens_the_tolerance(tmp_path) -> None:
    """E114/E115 and M178 in one read. `cluster.edge_rank` ranks on `pairs.score`; while the
    column was `real` it could not carry the number the engine ranked, and `tol = 1e-6` was
    ~16 of its ULPs — so the score counter sat on a knife edge as well."""
    db = _db()
    db.score_column_type = "real"
    db.pairs[(LIVE, 1, 2)] = _pair()
    db.pairs[(BATCH, 1, 2)] = _pair()

    out = _run(db, tmp_path)

    assert [defect["defect"] for defect in out["defects"]] == ["store_score_precision"]
    assert out["score_tolerance"]["used"] > out["score_tolerance"]["asked"] == 1e-6
    assert out["verdict"]["ok"] is False


def test_the_shipped_score_column_leaves_the_tolerance_where_it_was_asked(tmp_path) -> None:
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair()
    db.pairs[(BATCH, 1, 2)] = _pair()

    out = _run(db, tmp_path)

    assert out["defects"] == []
    assert out["score_tolerance"]["used"] == out["score_tolerance"]["asked"] == 1e-6
    assert out["verdict"]["ok"] is True


# ------------------------------------------- W9m: drift attributed with evidence (E119)


def test_a_self_healed_delisting_explains_a_decision_that_moved(tmp_path) -> None:
    """M173, reproduced. Eleven sreality listings were `is_active=false` in the export and
    active again by the time the lane read them — rule #3's delisting, self-healed by
    `touch_listings`. They produced all four decision moves and the 0.3924 score movement that
    tripped the verdict. The live value is the one that matches the facts as they stand now,
    so the instrument names it and does not fail."""
    db = _db()
    db.listings[1] = _listing(1, last_seen=NOW - timedelta(hours=2))
    db.listings[2] = _listing(2, last_seen=NOW - timedelta(hours=2))
    db.pairs[(LIVE, 1, 2)] = _pair(zone="merge", features=_feats(both_active=1.0))
    db.pairs[(BATCH, 1, 2)] = _pair(zone="band", decision="band",
                                    features=_feats(both_active=0.0))

    out = _run(db, tmp_path)

    assert out["pairs"]["shared_causes"] == {"drifted_since_export": 1}
    assert out["pairs"]["decisions_moved"] == 1
    assert out["pairs"]["score_only_max_unattributed"] == 0.0
    assert out["verdict"]["ok"] is True


def test_a_clock_feature_that_matches_NEITHER_side_is_unexplained(tmp_path) -> None:
    """Drift is a claim about the facts, so it has to be checked against them. A live value
    that is no closer to today's listings than the batch's explains nothing."""
    db = _db()
    db.listings[1] = _listing(1, active=False, inactive=NOW - timedelta(days=3))
    db.listings[2] = _listing(2, active=False, inactive=NOW - timedelta(days=3))
    db.pairs[(LIVE, 1, 2)] = _pair(features=_feats(both_active=1.0))
    db.pairs[(BATCH, 1, 2)] = _pair(features=_feats(both_active=0.0))

    out = _run(db, tmp_path)

    assert out["pairs"]["shared_causes"] == {"unexplained": 1}
    assert out["verdict"]["ok"] is False


def test_a_large_attributed_score_movement_no_longer_fails_the_verdict(tmp_path) -> None:
    """The verdict's old second reason. 0.3924 exceeded the 0.05 bar with the decision
    unchanged — and every bit of it was the eleven healed listings. An UNATTRIBUTED movement
    of the same size still fails; this one is carried by its cause."""
    db = _db()
    db.listings[1] = _listing(1, last_seen=NOW - timedelta(hours=2))
    db.listings[2] = _listing(2, last_seen=NOW - timedelta(hours=2))
    db.pairs[(LIVE, 1, 2)] = _pair(features=_feats(both_active=1.0))
    db.pairs[(BATCH, 1, 2)] = _pair(features=_feats(both_active=0.0))

    out = _run(db, tmp_path)

    assert out["pairs"]["score_only_max"] > SCORE_ONLY_MAX
    assert out["pairs"]["score_only_max_unattributed"] == 0.0
    assert out["pairs"]["shared_causes"] == {"drifted_since_export": 1}
    assert out["verdict"]["ok"] is True


def test_a_feature_outside_the_two_named_causes_is_unexplained(tmp_path) -> None:
    """`drifted_since_export` and `calibration_cohort` are the two things that legitimately
    move an input. A photograph feature that moved is neither, and waving it through is how a
    check becomes a description."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair(features=_feats(phash_tight_matches=4.0))
    db.pairs[(BATCH, 1, 2)] = _pair(zone="band", decision="band",
                                    features=_feats(phash_tight_matches=0.0))

    assert _run(db, tmp_path)["pairs"]["shared_causes"] == {"unexplained": 1}


def test_a_pair_the_store_kept_no_vector_for_cannot_be_attributed(tmp_path) -> None:
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair(features=None)
    db.pairs[(BATCH, 1, 2)] = _pair(score=0.8, features=None)

    out = _run(db, tmp_path)

    assert out["pairs"]["shared_causes"] == {"no_feature_vector": 1}
    # ...and it cannot be re-scored either: no vector, no claim either way (E121).
    assert out["pairs"]["score_self_consistency"]["exempt"] == {"no_feature_vector": 2}
    assert out["pairs"]["score_self_consistency"]["checked"] == 0
    assert out["verdict"]["ok"] is False


# ----------------------------------------------- W9m: the one-sided causes, split by side


def test_the_one_sided_causes_are_reported_per_side(tmp_path) -> None:
    """M178. Summed across both sides, `scope` 3,006 + `store_floor` 14 +
    `arrival_after_export` 1 = 3,021 read as if it should equal `only_batch` 3,017 when it
    equalled 3,017 + 4, and the report looked wrong where it was right."""
    db = _db()
    db.listings[900] = _listing(900)
    db.pairs[(BATCH, 1, 900)] = _pair()
    db.pairs[(LIVE, 1, 2)] = _pair(score=0.05, zone="reject", certificate=None)
    db.pairs[(LIVE, 2, 3)] = _pair()
    db.pairs[(BATCH, 2, 3)] = _pair()

    out = _run(db, tmp_path)

    assert out["pairs"]["causes_by_side"] == {"live": {"store_floor": 1},
                                              "batch": {"scope": 1}}
    assert out["pairs"]["causes"] == {"store_floor": 1, "scope": 1}
    assert out["verdict"]["ok"] is True


# ---------------------------------------------- W9m: clusters attributed by component (E119)


def test_a_component_whose_two_sides_hold_the_SAME_edges_is_unexplained(tmp_path) -> None:
    """E114's exact shape, and the reason the attribution exists. In scope the merge-edge sets
    differed by three edges while 305 listings sat in different member sets; 45 of the 47
    difference components held identical edges on both sides. Three edges cannot move 305
    listings, and the instrument now says so instead of reporting 81 against 76."""
    db = _db()
    for key in ((1, 2), (1, 3), (2, 3)):
        db.pairs[(LIVE, *key)] = _pair()
        db.pairs[(BATCH, *key)] = _pair()
    db.cluster_members.update({(LIVE, 1, 1), (LIVE, 1, 3)})
    db.cluster_members.update({(BATCH, 2, 2), (BATCH, 2, 3)})

    out = _run(db, tmp_path)

    assert out["clusters"]["components"] == 1
    assert out["clusters"]["component_causes"] == {"unexplained": 1}
    # The singletons are reported too: the component's question is where each listing
    # LANDED, and "2 is on its own here" is half the answer.
    assert out["clusters"]["component_examples"][0]["live"] == [[1, 3], [2]]
    assert out["clusters"]["component_examples"][0]["batch"] == [[1], [2, 3]]
    assert out["verdict"]["ok"] is False
    assert "partition them differently" in " ".join(out["verdict"]["reasons"])


def test_a_component_whose_edges_differ_is_attributed_upstream(tmp_path) -> None:
    """The pair grain already accounted for it — counting it again would report one cause
    twice and leave the operator chasing a cluster bug that is a pair difference."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair()
    db.pairs[(BATCH, 1, 2)] = _pair(zone="band", decision="band")
    db.cluster_members.update({(LIVE, 1, 1), (LIVE, 1, 2)})

    out = _run(db, tmp_path)

    assert out["clusters"]["component_causes"] == {"upstream_pair": 1}
    assert not [reason for reason in out["verdict"]["reasons"]
                if "partition them differently" in reason]


def test_a_component_holding_a_listing_that_arrived_after_the_export_is_an_arrival(tmp_path):
    db = _db()
    db.scope_ids[(LIVE, "obec:563510", 9)] = {"resolved_at": NOW}
    db.rt_fp[(LIVE, 9)] = _fp_row()
    db.listings[9] = _listing(9, first_seen=EXPORTED + timedelta(hours=3))
    db.pairs[(LIVE, 1, 9)] = _pair()
    db.pairs[(LIVE, 1, 2)] = _pair()
    db.pairs[(BATCH, 1, 2)] = _pair()
    db.cluster_members.update({(LIVE, 1, 1), (LIVE, 1, 9)})

    out = _run(db, tmp_path)

    assert out["clusters"]["component_causes"] == {"arrival": 1}
    assert out["verdict"]["ok"] is True


# --------- R1 / E121: a stored score must follow from its own stored vector (the hole closed)


def _drifted(db: FakePg) -> None:
    """Two listings whose delisting self-healed — the drift M173 measured, and the one an
    attribution will legitimately name. Every case below rides on exactly this."""
    db.listings[1] = _listing(1, last_seen=NOW - timedelta(hours=2))
    db.listings[2] = _listing(2, last_seen=NOW - timedelta(hours=2))


def test_a_decision_bug_riding_on_an_EVIDENCED_drift_is_no_longer_excused(tmp_path) -> None:
    """The hole an adversarial read of the W9m attribution demonstrated, reproduced.

    `shared_cause` classifies by WHICH inputs moved and never asks whether the moved inputs
    can ACCOUNT for the difference. So a genuine, evidenced clock drift — the live
    `both_active` IS what today's `public.listings` say — was allowed to carry a zone flip and
    a corrupted score along with it, and `verdict.ok` stayed TRUE over a real decision bug.

    The drift is still named. What it no longer carries is a score the row's own vector does
    not imply: R1 is asked first, it is a DEFECT of that row, and no cause outranks it."""
    db = _db()
    _drifted(db)
    db.pairs[(LIVE, 1, 2)] = _pair(score=0.98, zone="reject", decision="model",
                                   features=_feats(both_active=1.0))
    db.pairs[(BATCH, 1, 2)] = _pair(features=_feats(both_active=0.0))

    out = _run(db, tmp_path)

    assert out["pairs"]["shared_causes"] == {"drifted_since_export": 1}
    assert out["pairs"]["decisions_moved"] == 1
    assert out["pairs"]["score_self_consistency"]["defects"] == {"live": 1, "batch": 0}
    assert [defect["defect"] for defect in out["defects"]] == [SCORE_DEFECT]
    assert out["verdict"]["ok"] is False
    assert "does not reproduce" in " ".join(out["verdict"]["reasons"])


def test_the_rule_reads_BOTH_stores(tmp_path) -> None:
    """The same corruption on the batch side is the same finding. A comparison that only
    re-scored the live store would trust whichever generation it was pointed at."""
    db = _db()
    _drifted(db)
    db.pairs[(LIVE, 1, 2)] = _pair(features=_feats(both_active=1.0))
    db.pairs[(BATCH, 1, 2)] = _pair(score=0.44, features=_feats(both_active=0.0))

    out = _run(db, tmp_path)

    assert out["pairs"]["shared_causes"] == {"drifted_since_export": 1}
    assert out["pairs"]["score_self_consistency"]["defects"] == {"live": 0, "batch": 1}
    assert out["defects"][0]["side"] == "batch"
    assert out["verdict"]["ok"] is False


def test_a_drift_whose_two_scores_both_follow_from_their_vectors_still_passes(tmp_path):
    """The other half of the rule: it must not fail an honest drift. Both rows carry the score
    their own vector implies, they differ because the INPUT moved, and the verdict is ok."""
    db = _db()
    _drifted(db)
    db.pairs[(LIVE, 1, 2)] = _pair(features=_feats(both_active=1.0))
    db.pairs[(BATCH, 1, 2)] = _pair(features=_feats(both_active=0.0))

    out = _run(db, tmp_path)

    consistency = out["pairs"]["score_self_consistency"]
    assert consistency["checked"] == 2 and consistency["defective"] == 0
    assert consistency["max_gap"] == 0.0
    assert out["defects"] == []
    assert out["verdict"]["ok"] is True


@pytest.mark.parametrize("reason,changed", [
    ("guard_veto", {"guard_veto": "unit_designator", "score": 0.0}),
    ("no_feature_vector", {"features": None}),
    ("no_model_version", {"model_version": None}),
    ("model_unavailable", {"model_version": "a_model_this_build_does_not_carry"}),
])
def test_a_row_the_rule_cannot_be_asked_of_is_NAMED_not_skipped(tmp_path, reason, changed):
    """The four exemptions, each one counted in the report. A veto writes 0.0 without
    consulting the model; a probe-only update coalesces and leaves the vector the decision was
    taken on, so a row can hold none; a row that does not name its scorer cannot be re-scored
    without the instrument inventing one; and a model this build does not carry cannot be
    loaded. Silence on any of them is how a check passes on nothing."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair(zone="band", decision="band", **changed)
    db.pairs[(BATCH, 1, 2)] = _pair()

    consistency = _run(db, tmp_path)["pairs"]["score_self_consistency"]

    assert consistency["exempt"] == {reason: 1}
    assert consistency["rows"] == 2 and consistency["checked"] == 1
    assert consistency["defects"] == {"live": 0, "batch": 0}
    assert consistency["models_unavailable"] == (
        ["a_model_this_build_does_not_carry"] if reason == "model_unavailable" else [])


def test_a_float4_store_is_re_scored_AS_a_float4_store(tmp_path) -> None:
    """The tolerance accounts for the column's precision instead of assuming float64.

    Before migration 541 `pairs.score` was `real`, so the stored number is the column's image
    of the model's float64 — the recomputation is narrowed the same way before it is compared
    (`store_score.narrow`, one definition, E115). A row that IS its own vector's score passes
    under `real`; it would not if the check pretended the store were exact."""
    db = _db()
    _drifted(db)
    db.score_column_type = "real"
    vector = _feats(both_active=1.0)
    db.pairs[(LIVE, 1, 2)] = _pair(score=narrow(_score_of(vector), "real"), features=vector)
    db.pairs[(BATCH, 1, 2)] = _pair(features=_feats(both_active=0.0))

    consistency = _run(db, tmp_path)["pairs"]["score_self_consistency"]

    assert consistency["score_column"] == "real"
    assert consistency["checked"] == 2 and consistency["defective"] == 0


def test_a_float4_store_does_not_excuse_a_score_that_does_not_follow(tmp_path) -> None:
    """...and the widened tolerance is the column's resolution, not an amnesty: a score off by
    a thousandth is still a score its vector does not imply."""
    db = _db()
    _drifted(db)
    db.score_column_type = "real"
    vector = _feats(both_active=1.0)
    db.pairs[(LIVE, 1, 2)] = _pair(score=narrow(_score_of(vector), "real") + 1e-3,
                                   features=vector)
    db.pairs[(BATCH, 1, 2)] = _pair(features=_feats(both_active=0.0))

    out = _run(db, tmp_path)

    assert out["pairs"]["score_self_consistency"]["defects"] == {"live": 1, "batch": 0}
    assert [defect["defect"] for defect in out["defects"]] == ["store_score_precision",
                                                               SCORE_DEFECT]
    assert out["verdict"]["ok"] is False


def test_the_report_says_how_many_rows_were_checked_and_how_many_were_exempt(tmp_path):
    """The denominator is the deliverable: a rule that re-scored nothing and a rule that
    re-scored everything both report `defective: 0`, and only the counters tell them apart."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair(features=_feats(tfidf_cos=0.9))
    db.pairs[(BATCH, 1, 2)] = _pair(features=_feats(tfidf_cos=0.4))
    db.pairs[(LIVE, 3, 4)] = _pair(features=None)
    db.pairs[(BATCH, 3, 4)] = _pair(zone="band", decision="band", features=None)

    out = _run(db, tmp_path)

    assert out["pairs"]["score_self_consistency"] == {
        "rows": 4, "checked": 2, "exempt": {"no_feature_vector": 2},
        "defects": {"live": 0, "batch": 0}, "defective": 0, "max_gap": 0.0,
        "tolerance": 1e-6, "score_column": "double precision",
        "models_unavailable": [], "examples": []}
    assert json.loads((tmp_path / EQUIVALENCE_FILE).read_text())[
        "pairs"]["score_self_consistency"]["checked"] == 2


def test_the_defect_carries_the_example_that_names_the_row(tmp_path) -> None:
    """A count nobody can chase is a rumour: the report names the pair, the model it claims
    to have been scored by, what it holds and what its vector implies."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _pair(score=0.98, features=_feats(tfidf_cos=0.4))
    db.pairs[(BATCH, 1, 2)] = _pair(features=_feats(tfidf_cos=0.4))

    example = _run(db, tmp_path)["pairs"]["score_self_consistency"]["examples"][0]

    assert example["side"] == "live"
    assert (example["listing_lo"], example["listing_hi"]) == (1, 2)
    assert example["model_version"] == MODEL
    assert example["stored"] == 0.98
    assert example["recomputed"] == pytest.approx(_score_of(_feats(tfidf_cos=0.4)))
    assert example["gap"] == pytest.approx(0.98 - _score_of(_feats(tfidf_cos=0.4)))
