"""E918 — the two causes G1-live could not name: a HOLD, and an advert the export never had.

G1-live (run 36253408857, 2026-09-26) compared the worker lane's live generation `rt` with the
batch generation g15 scored on the export taken 12:38–12:47 UTC and failed on 44 cases:

  * 28 shared pairs the batch MERGES (K-C, D43 promotions, the model) and the live lane holds in
    the band as `evidence_pending` — the F3 complete-evidence hold (E908): a merge that rests on
    photographs waits until every photograph carries its pHash, CLIP vector and tags, at most
    48 h. A hold, not a decision, and the safe way round;
  * 16 live-only pairs on adverts 19054614 / 19054616 / 19055674, which the export never held:
    they arrived as it ran. The old arrival test compared `first_seen_at` with the minute typed on
    the command line and never asked the question that matters — was the listing IN the cohort.

Both are named causes now, and neither is allowed to name more than it evidences: a hold
explains a pair only while the lane's own release rule still keeps it and only when the decision
it holds IS the batch's; an arrival is read against the export artifact's own listing set, the
cut is the export's start from its own ledger row, and a listing the batch store holds is never
one.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pytest

from autodedup.incremental import EVIDENCE_HOLD_REASON
from autodedup.rt_equivalence import (
    EQUIVALENCE_FILE,
    EVIDENCE_HOLD,
    HOLD_CAP_S,
    Hold,
    Pair,
    arrival_of,
    cohort_listing_ids,
    hold_cause,
    run_equivalence,
)
from autodedup.features import WindowRule
from autodedup.incremental_lane import scope_setting_key
from tests.autodedup.fake_pg import FakePg
from tests.autodedup.test_rt_equivalence import (
    BATCH,
    EXPORTED,
    LIVE,
    MODEL,
    NOW,
    _db,
    _feats,
    _fp_row,
    _listing,
    _pair,
    _score_of,
)

EXPORT_RUN = "36251000000"
DATA = Path(__file__).with_name("data_g1_live_rt_equivalence.json")


def _held(batch_certificate: str | None = None, **kw: Any) -> dict:
    """A live row the lane HOLDS: the band, no certificate, and what it decided kept beside it."""
    return _pair(zone="band", certificate=None, decision=EVIDENCE_HOLD_REASON,
                 evidence={"held_zone": "merge", "held_reason": "model",
                           "held_certificate": batch_certificate or ""}, **kw)


def _incomplete(db: FakePg, listing_id: int, *, age: timedelta) -> None:
    """An endpoint decided `age` ago with a gallery the photo producers had not finished."""
    db.rt_fp[(LIVE, listing_id)] = {**_fp_row(), "ev_complete": False,
                                    "first_decided_at": NOW - age}


def _cohort(tmp: Path, ids: Iterable[int]) -> Path:
    """An export artifact in the shape `export.py` writes: meta, listings, then images."""
    path = tmp / "cohort.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": "meta", "exported_at": EXPORTED.isoformat()}) + "\n")
        for listing_id in sorted(ids):
            fh.write(json.dumps({"t": "listing", "id": listing_id}) + "\n")
        fh.write(json.dumps({"t": "image", "image_id": 1, "listing_id": 1}) + "\n")
    return path


def _fetcher(ids: Iterable[int], calls: list[str] | None = None):
    """A stand-in for `gh run download`: writes the artifact into the directory it is handed."""
    wanted = sorted(ids)

    def fetch(export_run: str, dest: Path) -> Path:
        if calls is not None:
            calls.append(export_run)
        dest.mkdir(parents=True, exist_ok=True)
        return _cohort(dest, wanted)
    return fetch


def _refuse(export_run: str, dest: Path) -> Path:
    raise SystemExit("GH_TOKEN (or GITHUB_TOKEN) must be set to download the export artifact")


def _with_export(db: FakePg, *, started: datetime = EXPORTED,
                 finished: datetime | None = None) -> None:
    """The batch pass names its export, and the export left its own ledger row."""
    db.score_runs[BATCH] = {"settings": {}, "model_version": MODEL, "export_run": EXPORT_RUN,
                            "finished_at": NOW}
    db.ledger_runs[int(EXPORT_RUN)] = {"started_at": started,
                                       "finished_at": finished or started + timedelta(minutes=9)}


def _run(db: FakePg, tmp_path: Path, fetch=_refuse, **args: str) -> dict:
    out = tmp_path / "out"
    return run_equivalence(lambda: db, {"generation": LIVE, "batch": BATCH, **args}, out,
                           fetch_cohort=fetch)


# ------------------------------------------------------------------ the hold (E908, E918)


def test_a_merge_the_lane_is_HOLDING_for_evidence_is_named_and_does_not_fail(tmp_path):
    """G1-live's 28, in one pair: the batch merges, the live lane decided the same merge and is
    waiting for the photographs before it acts. Named, counted apart, and the verdict says how
    many are waiting and for how long."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _held()
    db.pairs[(BATCH, 1, 2)] = _pair(certificate=None, decision="model")
    _incomplete(db, 2, age=timedelta(hours=3))

    out = _run(db, tmp_path)

    assert out["pairs"]["shared_causes"] == {EVIDENCE_HOLD: 1}
    assert out["pairs"]["by_field"] == {"zone": 1}
    hold = out["pairs"]["evidence_hold"]
    assert (hold["shared"], hold["explained"], hold["unexplained"]) == (1, 1, 0)
    assert hold["oldest_h"] == pytest.approx(3.0)
    assert out["verdict"]["ok"] is True
    assert out["verdict"]["notes"] == [
        "1 shared pairs are HELD for complete photo evidence (E908) — a hold, not a decision; "
        "the oldest has waited 3.0 h of the 48 h cap"]


def test_a_held_K_C_merge_is_the_batch_s_K_C_merge(tmp_path) -> None:
    """The hold writes no certificate, so a certified merge differs in TWO fields while held —
    and `evidence.held_certificate` is what says it is the same certificate."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _held("K-C")
    db.pairs[(BATCH, 1, 2)] = _pair(certificate="K-C", decision="certificate:K-C")
    _incomplete(db, 1, age=timedelta(hours=1))

    out = _run(db, tmp_path)

    assert sorted(out["pairs"]["by_field"]) == ["certificate", "zone"]
    assert out["pairs"]["shared_causes"] == {EVIDENCE_HOLD: 1}
    assert out["verdict"]["ok"] is True


def test_a_hold_past_the_48h_cap_is_unexplained_again(tmp_path) -> None:
    """Past the horizon the lane's release arm owes a re-decision. A hold still sitting in the
    store then is a hold that never ends, and nothing about it is safe any more."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _held()
    db.pairs[(BATCH, 1, 2)] = _pair(certificate=None, decision="model")
    _incomplete(db, 2, age=timedelta(hours=49))

    out = _run(db, tmp_path)

    assert out["pairs"]["shared_causes"] == {"unexplained": 1}
    assert out["pairs"]["evidence_hold"]["unexplained"] == 1
    assert out["pairs"]["evidence_hold"]["unexplained_examples"][0]["age_h"] == \
        pytest.approx(49.0)
    assert out["verdict"]["ok"] is False
    assert any("past the 48 h evidence cap" in reason for reason in out["verdict"]["reasons"])


def test_the_hold_is_aged_from_the_youngest_INCOMPLETE_endpoint(tmp_path) -> None:
    """The lane keeps a hold while EITHER side is incomplete inside the horizon, so a complete
    old endpoint does not age it and an incomplete young one keeps it alive."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _held()
    db.pairs[(BATCH, 1, 2)] = _pair(certificate=None, decision="model")
    db.rt_fp[(LIVE, 1)] = {**_fp_row(), "ev_complete": True,
                           "first_decided_at": NOW - timedelta(days=9)}
    _incomplete(db, 2, age=timedelta(hours=5))

    out = _run(db, tmp_path)

    assert out["pairs"]["shared_causes"] == {EVIDENCE_HOLD: 1}
    assert out["pairs"]["evidence_hold"]["oldest_h"] == pytest.approx(5.0)


@pytest.mark.parametrize("endpoint", [
    {"ev_complete": True},                                  # both galleries complete
    {"ev_complete": False, "first_decided_at": None},       # cannot be aged
])
def test_a_hold_the_lane_would_release_is_not_excused(tmp_path, endpoint) -> None:
    """The lane's own release rule, read the other way round: nothing incomplete inside the
    horizon means the lane owes a release, and a stamp that is missing means the instrument
    cannot show the hold is inside the cap — the SQL arm would keep such a hold for ever."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _held()
    db.pairs[(BATCH, 1, 2)] = _pair(certificate=None, decision="model")
    db.rt_fp[(LIVE, 2)] = {**_fp_row(), **endpoint}

    out = _run(db, tmp_path)

    assert out["pairs"]["shared_causes"] == {"unexplained": 1}
    assert out["verdict"]["ok"] is False


def test_a_hold_on_a_decision_the_batch_did_not_take_is_not_excused(tmp_path) -> None:
    """The hold explains WHEN the lane acts, never WHAT it decided. A held K-C against a batch
    merge that carries no certificate is a certificate disagreement the hold postpones."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _held("K-C")
    db.pairs[(BATCH, 1, 2)] = _pair(certificate=None, decision="model")
    _incomplete(db, 2, age=timedelta(hours=2))

    out = _run(db, tmp_path)

    assert out["pairs"]["shared_causes"] == {"unexplained": 1}
    assert out["verdict"]["ok"] is False


def test_a_held_merge_hiding_behind_two_band_rows_is_still_a_difference(tmp_path) -> None:
    """Both stores hold a band row, so the rows agree — and the live lane has DECIDED a merge
    the batch did not make. The held decision is compared, not only the row it wrote."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _held()
    db.pairs[(BATCH, 1, 2)] = _pair(zone="band", certificate=None, decision="band")
    _incomplete(db, 2, age=timedelta(hours=2))

    out = _run(db, tmp_path)

    assert out["pairs"]["differing"] == 1
    assert out["pairs"]["by_field"] == {"held_zone": 1}
    assert out["pairs"]["shared_causes"] == {"unexplained": 1}
    assert out["verdict"]["ok"] is False


def test_a_held_pair_whose_score_also_moved_needs_a_cause_for_the_score_too(tmp_path):
    """The hold explains the zone. A score that moved as well is attributed exactly as an
    unheld pair's is: named (here the calibration cohort), the pair stays `evidence_hold`."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _held(features=_feats(tfidf_cos=0.9))
    db.pairs[(BATCH, 1, 2)] = _pair(certificate=None, decision="model",
                                    features=_feats(tfidf_cos=0.4))
    _incomplete(db, 2, age=timedelta(hours=2))

    out = _run(db, tmp_path)

    assert out["pairs"]["shared_causes"] == {EVIDENCE_HOLD: 1}
    assert out["verdict"]["ok"] is True


def test_hold_cause_directly_names_only_what_the_hold_evidences() -> None:
    """The rule, asked without a store: alive and agreeing is the hold; anything else is not."""
    live = Pair([1, 2, 0.9, "band", None, EVIDENCE_HOLD_REASON, None, 3, MODEL])
    batch = Pair([1, 2, 0.9, "merge", "K-C", "certificate:K-C", None, 3, MODEL])
    live.features = batch.features = {"tfidf_cos": 0.4}
    kw = {"asymmetric": [], "one_clock": True, "facts": {},
          "settings": WindowRule(live_window_from_sighting=True), "tol": 1e-6}

    assert hold_cause(live, batch, [], Hold("merge", "K-C", 3600.0), **kw) == EVIDENCE_HOLD
    assert hold_cause(live, batch, [], Hold("merge", None, 3600.0), **kw) == "unexplained"
    assert hold_cause(live, batch, [], Hold("merge", "K-C", HOLD_CAP_S), **kw) == "unexplained"
    assert hold_cause(live, batch, [], Hold("merge", "K-C", None), **kw) == "unexplained"
    assert hold_cause(live, batch, [], None, **kw) == "unexplained"


def test_a_one_sided_hold_is_not_a_cause(tmp_path) -> None:
    """A pair the live side holds and the batch side does not store at all is a merge the lane
    decided and the batch did not keep; waiting to act on it explains nothing."""
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _held()
    db.pairs[(LIVE, 3, 4)] = _pair()
    db.pairs[(BATCH, 3, 4)] = _pair()
    _incomplete(db, 2, age=timedelta(hours=2))

    out = _run(db, tmp_path)

    assert out["pairs"]["causes"] == {"unexplained": 1}
    assert out["pairs"]["evidence_hold"]["only_live"] == 1


# ------------------------------------------------------------------ the arrival (E918)


def _arrival_world(*, first_seen: datetime, resolved: datetime) -> FakePg:
    """Listing 9 inside the scope and the live store, merged live with listing 1, with the two
    clocks given — and a shared pair so the comparison compares something."""
    db = _db()
    db.scope_ids[(LIVE, "obec:563510", 9)] = {"resolved_at": resolved}
    db.rt_fp[(LIVE, 9)] = _fp_row()
    db.listings[9] = _listing(9, first_seen=first_seen)
    db.pairs[(LIVE, 1, 9)] = _pair()
    db.pairs[(LIVE, 1, 2)] = _pair()
    db.pairs[(BATCH, 1, 2)] = _pair()
    for listing_id in range(1, 5):
        db.scope_ids[(LIVE, "obec:563510", listing_id)] = {
            "resolved_at": EXPORTED - timedelta(days=3)}
    return db


def test_an_advert_the_export_never_had_is_an_arrival_by_the_artifact(tmp_path) -> None:
    """G1-live's 16: first seen BEFORE the minute on the command line, so the old test passed
    over it — and absent from the export g15 was scored on, because its location was written
    after the export read its blocks. The artifact says it was not in the cohort; the location
    clock says why."""
    db = _arrival_world(first_seen=EXPORTED - timedelta(minutes=7),
                        resolved=EXPORTED + timedelta(minutes=14))
    _with_export(db)
    calls: list[str] = []

    out = _run(db, tmp_path, fetch=_fetcher([1, 2, 3, 4], calls))

    assert calls == [EXPORT_RUN]
    assert out["pairs"]["causes_by_side"]["live"] == {"arrival_after_export": 1}
    assert out["arrivals"]["by_clock"] == {"resolved_at": 1}
    assert out["arrivals"]["cohort"] == {"source": f"export_run:{EXPORT_RUN}", "listings": 4,
                                         "scope_absent": 1, "error": None}
    assert out["verdict"]["ok"] is True


def test_the_artifact_is_downloaded_outside_the_report_directory(tmp_path) -> None:
    """The lane uploads `out/` whole, and a 280 MB cohort is not a report."""
    db = _arrival_world(first_seen=EXPORTED - timedelta(minutes=7),
                        resolved=EXPORTED + timedelta(minutes=14))
    _with_export(db)

    _run(db, tmp_path, fetch=_fetcher([1, 2, 3, 4]))

    assert sorted(p.name for p in (tmp_path / "out").rglob("*")) == [EQUIVALENCE_FILE]


def test_the_cut_is_the_export_s_START_from_its_own_ledger_row(tmp_path) -> None:
    """The dispatch typed an hour too early; the export's ledger row says when it read its
    blocks. A listing first seen between the two was in the cohort, and is not excused."""
    db = _arrival_world(first_seen=EXPORTED - timedelta(minutes=30),
                        resolved=EXPORTED - timedelta(minutes=29))
    _with_export(db, started=EXPORTED)

    out = _run(db, tmp_path, exported_at=(EXPORTED - timedelta(hours=1)).isoformat())

    assert out["export_window"]["cut_source"] == "export_ledger"
    assert out["export_window"]["cut"] == EXPORTED.isoformat()
    assert out["pairs"]["causes_by_side"]["live"] == {"unexplained": 1}


def test_without_a_ledger_row_the_dispatch_s_stamp_is_the_cut(tmp_path) -> None:
    db = _arrival_world(first_seen=EXPORTED + timedelta(hours=2),
                        resolved=EXPORTED + timedelta(hours=2))

    out = _run(db, tmp_path, exported_at="2026-09-19T12:00Z")

    assert out["export_window"]["cut_source"] == "exported_at"
    assert out["pairs"]["causes_by_side"]["live"] == {"arrival_after_export": 1}
    assert out["arrivals"]["by_clock"] == {"first_seen_at": 1}


def test_without_the_artifact_a_re_resolved_location_proves_nothing(tmp_path) -> None:
    """`listing_location.resolved_at` is rewritten by every re-resolve, so without the cohort's
    own listing set only a listing that did not EXIST at the cut is provably absent. The
    fallback is the conservative side, and the report says why it fell back."""
    db = _arrival_world(first_seen=EXPORTED - timedelta(minutes=7),
                        resolved=EXPORTED + timedelta(minutes=14))
    _with_export(db)

    out = _run(db, tmp_path, fetch=_refuse)

    assert out["pairs"]["causes_by_side"]["live"] == {"unexplained": 1}
    assert "GH_TOKEN" in out["arrivals"]["cohort"]["error"]
    assert out["verdict"]["ok"] is False


def test_a_listing_the_cohort_lacks_with_both_clocks_before_the_cut_is_not_an_arrival(
        tmp_path) -> None:
    """Absent from the export and present in the scope since before it: that is a scope the
    export never covered, not an advert that arrived — and `scope_absent` shows its size."""
    db = _arrival_world(first_seen=EXPORTED - timedelta(days=4),
                        resolved=EXPORTED - timedelta(days=3))
    _with_export(db)

    out = _run(db, tmp_path, fetch=_fetcher([1, 2, 3, 4]))

    assert out["pairs"]["causes_by_side"]["live"] == {"unexplained": 1}
    assert out["arrivals"]["cohort"]["scope_absent"] == 1


def test_a_listing_the_COHORT_carries_is_never_an_arrival_however_recently_re_resolved(
        tmp_path) -> None:
    """`resolved_at` is rewritten by every re-resolve. A listing the artifact carries was in
    the cohort, so a location re-written after the export says nothing about its absence."""
    db = _arrival_world(first_seen=EXPORTED - timedelta(days=4),
                        resolved=EXPORTED + timedelta(minutes=14))
    _with_export(db)

    out = _run(db, tmp_path, fetch=_fetcher([1, 2, 3, 4, 9]))

    assert out["pairs"]["causes_by_side"]["live"] == {"unexplained": 1}
    assert out["arrivals"]["listings"] == 0
    assert out["arrivals"]["cohort"]["scope_absent"] == 0


def test_a_listing_the_BATCH_STORE_holds_is_never_an_arrival(tmp_path) -> None:
    """Whatever the clocks say: a listing the batch generation holds a row for was in its
    cohort by construction, so a cut typed wrong cannot excuse a pair on it."""
    db = _arrival_world(first_seen=EXPORTED + timedelta(hours=2),
                        resolved=EXPORTED + timedelta(hours=2))
    db.pairs[(BATCH, 3, 9)] = _pair(score=0.05, zone="reject", certificate=None)

    out = _run(db, tmp_path, exported_at=EXPORTED.isoformat())

    assert out["pairs"]["causes_by_side"]["live"]["unexplained"] == 1
    assert "arrival_after_export" not in out["pairs"]["causes_by_side"]["live"]


def test_arrival_of_directly() -> None:
    cut = datetime(2026, 9, 26, 12, 38, 10, tzinfo=timezone.utc)
    early = cut - timedelta(minutes=5)
    late = cut + timedelta(minutes=5)
    seen = {1: early, 2: late, 3: early}
    resolved = {1: late, 2: late, 3: early}

    kw = {"cut": cut, "seen": seen, "resolved": resolved, "batch_held": set()}
    assert arrival_of(1, cohort={3}, **kw) == "resolved_at"
    assert arrival_of(1, cohort=None, **kw) is None
    assert arrival_of(2, cohort=None, **kw) == "first_seen_at"
    assert arrival_of(2, cohort={2}, **kw) is None
    assert arrival_of(3, cohort=set(), **kw) is None
    assert arrival_of(2, cohort=None, **{**kw, "batch_held": {2}}) is None
    assert arrival_of(2, cohort=None, **{**kw, "cut": None}) is None
    # A naive command-line stamp is read as UTC, the zone every stamp in the lane carries.
    assert arrival_of(2, cohort=None, **{**kw, "seen": {2: "2026-09-26T12:40:00"}}) \
        == "first_seen_at"


def test_cohort_listing_ids_stops_at_the_first_image(tmp_path) -> None:
    path = _cohort(tmp_path, [5, 7])
    with gzip.open(path, "at", encoding="utf-8") as fh:
        fh.write(json.dumps({"t": "listing", "id": 99}) + "\n")

    assert cohort_listing_ids(path) == {5, 7}


# ------------------------------------------------------------------ components (E918)


def test_a_live_group_whose_extra_member_ARRIVED_is_an_arrival(tmp_path) -> None:
    """G1-live's group 22951: the batch holds five listings, the live lane the same five plus
    advert 19054616, joined to each of them by a K-C merge. Every edge that differs touches the
    arrival — the group is explained, and not filed under `upstream_pair`."""
    db = _arrival_world(first_seen=EXPORTED - timedelta(minutes=7),
                        resolved=EXPORTED + timedelta(minutes=14))
    _with_export(db)
    for key in ((1, 2), (1, 3), (2, 3)):
        db.pairs[(LIVE, *key)] = _pair()
        db.pairs[(BATCH, *key)] = _pair()
    for key in ((2, 9), (3, 9)):
        db.pairs[(LIVE, *key)] = _pair()
    db.cluster_members.update({(LIVE, 1, i) for i in (1, 2, 3, 9)})
    db.cluster_members.update({(BATCH, 1, i) for i in (1, 2, 3)})

    out = _run(db, tmp_path, fetch=_fetcher([1, 2, 3, 4]))

    assert out["clusters"]["component_causes"] == {"arrival": 1}
    assert out["clusters"]["component_examples"][0]["arrived"] == [9]
    assert out["verdict"]["ok"] is True


def test_a_batch_group_the_live_lane_is_HOLDING_is_an_evidence_hold(tmp_path) -> None:
    db = _db()
    db.pairs[(LIVE, 1, 2)] = _held()
    db.pairs[(BATCH, 1, 2)] = _pair(certificate=None, decision="model")
    _incomplete(db, 2, age=timedelta(hours=2))
    db.cluster_members.update({(BATCH, 5, 1), (BATCH, 5, 2)})

    out = _run(db, tmp_path)

    assert out["clusters"]["component_causes"] == {EVIDENCE_HOLD: 1}
    assert out["clusters"]["component_examples"][0]["held_edges"] == [[1, 2]]


def test_a_group_with_an_arrival_AND_another_moved_edge_stays_upstream(tmp_path) -> None:
    """An arrival explains the edges it touches and nothing else."""
    db = _arrival_world(first_seen=EXPORTED - timedelta(minutes=7),
                        resolved=EXPORTED + timedelta(minutes=14))
    _with_export(db)
    db.pairs[(BATCH, 2, 3)] = _pair()
    db.pairs[(LIVE, 2, 3)] = _pair(zone="band", decision="band", certificate=None)
    db.cluster_members.update({(LIVE, 1, 1), (LIVE, 1, 2), (LIVE, 1, 9)})
    db.cluster_members.update({(BATCH, 1, 1), (BATCH, 1, 2), (BATCH, 1, 3)})

    out = _run(db, tmp_path, fetch=_fetcher([1, 2, 3, 4]))

    assert out["clusters"]["component_causes"] == {"upstream_pair": 1}


# ------------------------------------------- the G1-live artefact, replayed (run 36253408857)


def _replay_world(g1: dict[str, Any], *, resolved_after_cut: bool = True) -> FakePg:
    """The comparison G1-live made, rebuilt from the rows its report stored.

    What the report carries is used as it stands: every pair's zone, certificate, decision and
    families, which side held it, and the one arrived group's membership and edges. What it
    does not carry is supplied, and each supply is the one the report forces:
      * the scores are the ones the fixture's vectors imply — E121 re-scores every differing row
        — and both sides carry the same one, as every stored example does (`moved_features` is
        empty on all eight);
      * a held pair holds what the batch decided (that is what `evidence.held_*` would say for a
        merge the lane took and waits on) with one endpoint's gallery incomplete, decided hours
        ago — the generation was re-seeded fresh on the day, so nothing in it is older than 48 h;
      * the three adverts were first seen BEFORE 12:38 (the old test compared `first_seen_at`
        with 12:38 and passed over them, so it cannot have been later) and their location was
        written after the export read its blocks — the one reason a listing that existed can be
        absent from a block cohort. `resolved_after_cut=False` is the counterfactual."""
    run_day = datetime(2026, 9, 26, tzinfo=timezone.utc)
    export_start = run_day.replace(hour=12, minute=38, second=10)
    now = run_day.replace(hour=15, minute=52)
    arrived = {19054614, 19054616, 19055674}
    db = FakePg(now=now)
    db.calibration[LIVE] = {"digest": "d", "n_listings": 0, "payload": {},
                            "artifact_url": None,
                            "settings": {"store_floor": 0.02, "live_window_from_sighting": True},
                            "model_version": MODEL}
    db.settings[scope_setting_key(LIVE)] = [{"grain": "obec", "code": 563510}]
    db.score_runs[BATCH] = {"settings": {"live_window_from_sighting": True},
                            "model_version": MODEL, "export_run": EXPORT_RUN,
                            "finished_at": run_day.replace(hour=13, minute=20)}
    db.ledger_runs[int(EXPORT_RUN)] = {"started_at": export_start,
                                       "finished_at": run_day.replace(hour=12, minute=47)}
    vector = _feats(tfidf_cos=0.9)

    def _row(side: dict[str, Any], **kw: Any) -> dict:
        return _pair(score=kw.pop("score", None), zone=side["zone"],
                     certificate=side["certificate"], decision=side["decision"],
                     families=side["families"], **kw)

    for example in g1["shared_unexplained_examples"]:
        live, batch = example["live"], example["batch"]
        key = (live["listing_lo"], live["listing_hi"])
        db.pairs[(LIVE, *key)] = _row(live, features=vector, evidence={
            "held_zone": batch["zone"], "held_reason": batch["decision"],
            "held_certificate": batch["certificate"] or ""})
        db.pairs[(BATCH, *key)] = _row(batch, features=vector)
    for example in g1["unexplained_examples"]:
        key = (example["listing_lo"], example["listing_hi"])
        db.pairs[(LIVE, *key)] = _row(example, score=example["score"])
    component = g1["component_example"]
    for lo, hi in component["batch_edges"]:
        db.pairs[(BATCH, lo, hi)] = _pair(certificate="K-C", decision="certificate:K-C")
        db.pairs[(LIVE, lo, hi)] = _pair(certificate="K-C", decision="certificate:K-C")
    for members in component["live"]:
        db.cluster_members.update({(LIVE, members[0], i) for i in members})
    for members in component["batch"]:
        if len(members) > 1:
            db.cluster_members.update({(BATCH, members[0], i) for i in members})

    ids = {i for (_g, lo, hi) in db.pairs for i in (lo, hi)}
    for listing_id in ids:
        new = listing_id in arrived
        db.scope_ids[(LIVE, "obec:563510", listing_id)] = {"resolved_at": (
            export_start + timedelta(minutes=14) if new and resolved_after_cut
            else export_start - timedelta(days=2))}
        db.rt_fp[(LIVE, listing_id)] = {**_fp_row(), "first_decided_at": now - timedelta(hours=3)}
        db.listings[listing_id] = {
            "id": listing_id, "is_active": True, "last_seen_at": None, "inactive_at": None,
            "first_seen_at": (export_start - timedelta(minutes=5) if new
                              else export_start - timedelta(days=30))}
    for example in g1["shared_unexplained_examples"]:
        db.rt_fp[(LIVE, example["live"]["listing_hi"])].update(
            {"ev_complete": False, "first_decided_at": now - timedelta(hours=3)})
    return db


def _g1() -> dict[str, Any]:
    return json.loads(DATA.read_text(encoding="utf-8"))


def _groups(edges: Iterable[tuple[int, int]]) -> list[set[int]]:
    """The groups a set of merge edges forms — what the batch clustered the held merges into."""
    groups: list[set[int]] = []
    for lo, hi in edges:
        touching = [group for group in groups if {lo, hi} & group]
        merged = {lo, hi}.union(*touching)
        groups = [group for group in groups if group not in touching] + [merged]
    return groups


def test_the_g1_live_artefact_is_what_this_replay_replays() -> None:
    """The fixture is the report's own words: 28 + 16 unexplained, 8 + 8 of them stored, every
    stored shared one a live HOLD of a batch merge and every stored one-sided one on the live
    side and on one of the three adverts."""
    g1 = _g1()
    assert g1["verdict"]["reasons"] == ["28 of 17056 shared pairs differ with no cause",
                                        "16 one-sided pairs have no cause"]
    assert len(g1["shared_unexplained_examples"]) == 8
    assert len(g1["unexplained_examples"]) == 8
    assert all(e["live"]["decision"] == EVIDENCE_HOLD_REASON and e["batch"]["zone"] == "merge"
               for e in g1["shared_unexplained_examples"])
    assert all(e["side"] == "live" and {e["listing_lo"], e["listing_hi"]}
               & {19054614, 19054616, 19055674} for e in g1["unexplained_examples"])


def test_every_stored_g1_live_case_now_carries_a_cause(tmp_path) -> None:
    """The 16 cases the report stored, replayed through the shipped classification: the eight
    shared ones are `evidence_hold`, the eight one-sided ones `arrival_after_export`, the group
    the adverts joined is an `arrival` and the one the holds would form an `evidence_hold` — and
    the verdict that failed on them passes, saying how many pairs are waiting and for how long."""
    g1 = _g1()
    db = _replay_world(g1)
    batch_ids = {i for (g, lo, hi) in db.pairs if g == BATCH for i in (lo, hi)}
    held = [(e["live"]["listing_lo"], e["live"]["listing_hi"])
            for e in g1["shared_unexplained_examples"]]
    for group in _groups(held):
        db.cluster_members.update({(BATCH, min(group), i) for i in group})

    out = _run(db, tmp_path, fetch=_fetcher(batch_ids | {18403171, 18889054}),
               exported_at=g1["exported_at_arg"])

    pairs = out["pairs"]
    assert pairs["shared_causes"] == {EVIDENCE_HOLD: 8}
    assert [example["cause"] for example in pairs["differing_examples"]] == [EVIDENCE_HOLD] * 8
    assert pairs["causes_by_side"] == {"live": {"arrival_after_export": 8}, "batch": {}}
    assert pairs["evidence_hold"]["explained"] == 8
    assert pairs["evidence_hold"]["oldest_h"] == pytest.approx(3.0)
    assert out["arrivals"]["listings"] == 3
    assert out["arrivals"]["by_clock"] == {"resolved_at": 3}
    assert out["export_window"]["cut_source"] == "export_ledger"
    assert out["clusters"]["component_causes"] == {"arrival": 1, EVIDENCE_HOLD: 6}
    assert out["verdict"]["ok"] is True, out["verdict"]["reasons"]
    assert out["verdict"]["notes"][0].startswith("8 shared pairs are HELD")


def test_the_g1_live_arrivals_stay_unexplained_when_the_facts_do_not_support_them(tmp_path):
    """The counterfactual that keeps the replay honest: had the adverts' location been written
    BEFORE the export read its blocks, their absence from the cohort would be a scope the export
    did not cover, and the eight one-sided pairs would still fail the verdict. The holds do not
    depend on it."""
    g1 = _g1()
    db = _replay_world(g1, resolved_after_cut=False)
    batch_ids = {i for (g, lo, hi) in db.pairs if g == BATCH for i in (lo, hi)}

    out = _run(db, tmp_path, fetch=_fetcher(batch_ids | {18403171, 18889054}),
               exported_at=g1["exported_at_arg"])

    assert out["pairs"]["shared_causes"] == {EVIDENCE_HOLD: 8}
    assert out["pairs"]["causes_by_side"]["live"] == {"unexplained": 8}
    assert out["verdict"]["reasons"] == ["8 one-sided pairs have no cause"]
