"""Bulk machine labeling: the LLM building training sets for named heads.

The failure modes here are the expensive kind — a poisoned training set is
invisible until a classifier trained on it underperforms for reasons nobody can
trace. So the tests pin the things that would poison it: a leave-out written as
a negative, an unusable reply written as anything at all, an exam member
labeled, and a head labeled without being named.
"""

from __future__ import annotations

import contextlib
import pathlib
import typing
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Cur:
    rowcount = 0

    def __init__(self, rows: list[tuple], log: list) -> None:
        self._rows, self._log = rows, log

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: Any) -> None: ...

    def execute(self, sql: str, params: Any = None) -> None:
        self._log.append(("execute", sql, params))

    def executemany(self, sql: str, params: Any = None) -> None:
        self._log.append(("executemany", sql, params))

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, *batches: list[tuple]) -> None:
        self._batches = list(batches)
        self.log: list = []

    def cursor(self) -> _Cur:
        rows = self._batches.pop(0) if self._batches else []
        return _Cur(rows, self.log)

    @contextlib.contextmanager
    def transaction(self) -> typing.Iterator[None]:
        """psycopg's explicit block. connect() is autocommit, so a unit that must
        not be split (a redraw: delete then insert) has to open one."""
        self.log.append(("transaction", "", None))
        yield


# ------------------------------------------------------------------ the vocabulary
def test_a_leave_out_is_stored_as_excluded_never_as_a_negative() -> None:
    # The ratified rule: skip means the subject is present but the photo is of
    # something else. Storing it as a negative would teach the classifier the
    # exact opposite of what the operator decided.
    from toolkit import machine_labeling as ml

    assert ml.VERDICT_STATE["skip"] == ("excluded", "pruned")
    assert ml.VERDICT_STATE["yes"] == ("positive", None)
    assert ml.VERDICT_STATE["no"] == ("negative", None)


def test_record_labels_groups_by_state_through_the_existing_chokepoint() -> None:
    from toolkit import machine_labeling as ml
    from toolkit import tag_annotations as ta

    conn = _Conn([], [], [])
    out = ml.record_labels(conn, image_id=5,
                           verdicts={22: "yes", 25: "no", 36: "skip", 28: "yes"},
                           model="gpt-5-mini")
    assert out == {"image_id": 5, "cells": 4}
    writes = [c for c in conn.log if c[0] == "executemany"]
    assert len(writes) == 3  # one per distinct state, not one per tag
    states = {p[0]["state"] for _, _, params in writes for p in [params]}
    assert states == {"positive", "negative", "excluded"}
    for _, _, params in writes:
        for row in params:
            assert row["source"] == ta.SOURCE_MACHINE
            assert row["model"] == "gpt-5-mini"
            # verified_at is for humans; a machine cell is never pre-verified.
            assert row["verified"] is False


def test_an_unknown_verdict_raises_rather_than_guessing() -> None:
    from toolkit import machine_labeling as ml

    with pytest.raises(ValueError):
        ml.record_labels(_Conn([]), image_id=5, verdicts={22: "maybe"}, model="m")


# ------------------------------------------------------------------ the rails
@pytest.mark.parametrize("sql_name", ["_SAMPLE_SQL", "_BY_IDS_SQL"])
def test_every_strategy_excludes_exam_members_and_resumes_by_definition(sql_name: str) -> None:
    from toolkit import machine_labeling as ml

    sql = getattr(ml, sql_name)
    assert "FROM tag_exam_members m WHERE m.image_id = i.id" in sql
    assert "d.status = 'active'" in sql and "l.definition_id = d.id" in sql
    assert "l.source = 'machine'" in sql


def test_the_random_strategy_does_not_secretly_order_by_id() -> None:
    # ORDER BY id would label the OLDEST images and call it a sample; the
    # sampling decision belongs to the operator, not to a default.
    from toolkit import machine_labeling as ml

    assert "ORDER BY random()" in ml._SAMPLE_SQL
    assert "ORDER BY i.id" not in ml._SAMPLE_SQL


def test_counts_only_credit_labels_under_the_active_definition() -> None:
    from toolkit import machine_labeling as ml

    conn = _Conn([(22, "positive", 40), (22, "negative", 160)])
    out = ml.labelled_counts(conn, tag_ids=[22, 25])
    assert out[22] == {"positive": 40, "negative": 160, "excluded": 0}
    assert out[25] == {"positive": 0, "negative": 0, "excluded": 0}
    sql = conn.log[0][1]
    assert "d.status = 'active' AND d.id = l.definition_id" in sql


# ------------------------------------------------------------------ the script
def test_the_script_requires_heads_to_be_named() -> None:
    import importlib

    mod = importlib.import_module("scripts.label_images")
    with pytest.raises(ValueError):
        mod._parse_tags("")
    assert mod._parse_tags("22, 25,22") == [22, 25]


def test_an_unusable_reply_writes_nothing() -> None:
    # The one way a bulk pass poisons a training set irrecoverably: recording a
    # failed call as a row of confident negatives.
    src = (ROOT / "scripts" / "label_images.py").read_text()
    assert "if error is not None or not verdicts:" in src
    assert "return" in src.split("if error is not None or not verdicts:")[1][:400]


def test_the_called_for_value_is_in_the_literal_and_the_migration() -> None:
    from api.llm_client import CalledFor
    from toolkit import machine_labeling as ml

    assert ml.CALLED_FOR in typing.get_args(CalledFor)
    mig = (ROOT / "migrations" / "468_bulk_label_called_for.sql").read_text()
    assert "'label_image_bulk'" in mig
    # The list is restated whole; the previous values must survive.
    for kept in ("'review_exam_image'", "'suggest_exam_answer'", "'parse_url'"):
        assert kept in mig


def test_the_lane_defaults_to_dry_run_and_validates_its_inputs() -> None:
    import yaml

    lane = yaml.safe_load((ROOT / ".github" / "workflows" / "label_images.yml").read_text())
    inputs = lane[True]["workflow_dispatch"]["inputs"]
    assert inputs["dry_run"]["default"] == "true"   # spending is opt-in
    assert inputs["tags"]["required"] is True
    assert "all" not in inputs                      # no label-everything switch
    run = next(s for s in lane["jobs"]["label"]["steps"] if s.get("name") == "Label")["run"]
    assert "tags must be a comma-separated list of ids" in run
    assert "max_usd must be a number" in run


# ------------------------------------------------------------------ the targeted draw
def test_the_near_tag_draw_seeds_only_on_positives_outside_the_holdout() -> None:
    # Seeding a training draw on the yardstick's own images would let the
    # holdout shape the material it is supposed to grade.
    from toolkit import machine_labeling as ml

    # The marker is baked into the constant, not formatted in by a caller —
    # that is what lets the holdout census SEE it.
    sql = ml._NEAR_TAG_SQL
    assert "itl.state = 'positive'" in sql
    assert "tag_exam_members hx" in sql and "tag_exam_cohorts hc" in sql
    # And the eligibility rails still apply to what it returns.
    assert "FROM tag_exam_members m WHERE m.image_id = i.id" in sql
    assert "l.definition_id = d.id" in sql


def test_the_near_tag_draw_is_bounded_and_refuses_a_thin_centroid() -> None:
    # No ann index exists on 9.4M embeddings, so the draw samples a slice and
    # ranks within it; and a centroid over three images would concentrate the
    # whole budget on three images' worth of the corpus.
    from toolkit import machine_labeling as ml

    sql = ml._NEAR_TAG_SQL
    assert "TABLESAMPLE SYSTEM (%(pct)s)" in sql
    assert "c.seeds >= %(min_seeds)s::int" in sql
    assert "ORDER BY e.embedding <=> c.vec" in sql
    # Rank FIRST, filter after: applying the rails to every sampled vector is
    # what timed this query out live. The pool bounds what they run on.
    assert "LIMIT %(pool)s" in sql
    assert sql.index("LIMIT %(pool)s") < sql.index("tag_exam_members m")


def test_the_bias_of_the_targeted_draw_is_written_down() -> None:
    # It returns what CLIP already believes; a head trained only on it will
    # evaluate better than it performs. That has to be stated where it is read.
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "toolkit" / "machine_labeling.py").read_text()
    assert "blind spots" in src
    assert "look better in evaluation than it is in the world" in src


def test_the_lane_validates_near_tag_as_digits_only() -> None:
    import pathlib
    import yaml

    lane = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1]
                           / ".github" / "workflows" / "label_images.yml").read_text())
    run = next(s for s in lane["jobs"]["label"]["steps"] if s.get("name") == "Label")["run"]
    # `*[0-9]` would accept "abc1"; the exclusion class is the correct test.
    assert "*[!0-9]*) echo \"::error::near_tag must be a tag id\"" in run


# ------------------------------------------------------------------ mining drafts
def test_the_draft_draw_takes_only_drafted_positives_and_still_excludes_the_exam() -> None:
    # A draft is the operator's earlier GUESS, demoted deliberately. It selects
    # the candidate; it never becomes the label.
    from toolkit import machine_labeling as ml

    sql = ml._FROM_DRAFTS_SQL
    assert "dl.state = 'positive'" in sql and "dl.source = 'human_draft'" in sql
    assert "FROM tag_exam_members m WHERE m.image_id = i.id" in sql
    assert "l.definition_id = d.id" in sql


def test_the_draft_pool_is_reportable_before_anything_is_spent() -> None:
    from toolkit import machine_labeling as ml

    conn = _Conn([(17, 61), (2, 8)])
    out = ml.draft_pool_counts(conn, tag_ids=[17, 2, 48])
    assert out == {17: 61, 2: 8, 48: 0}


def test_the_targeted_draws_are_mutually_exclusive() -> None:
    # They answer different questions; silently letting one win would make the
    # run's provenance unreadable afterwards. Three of them now, so the check is
    # a count rather than a hard-coded pair — a fourth must not slip past it.
    src = (ROOT / "scripts" / "label_images.py").read_text()
    assert "are different draws; pick one" in src
    assert 'chosen = [n for n, v in (("--from-drafts"' in src
    for flag in ("--near-tag", "--like-tag"):
        assert flag in src.split("chosen = [")[1][:300]


def test_the_rejudge_draw_reopens_what_a_revision_left_standing() -> None:
    # A definition revision makes stale labels ELIGIBLE but nothing draws them
    # preferentially, so old answers under old wording survive indefinitely.
    # This draw is the companion: re-ask what the head already answered.
    from toolkit import machine_labeling as ml

    sql = ml._REJUDGE_SQL
    assert "l.tag_id = %(rejudge_tag)s::bigint" in sql
    # Scoped to the verdict they CURRENTLY carry — a narrowing revision can only
    # lose positives, so re-asking those alone is the cheap half of a redo.
    assert "%(rejudge_state)s::text IS NULL OR l.state = %(rejudge_state)s::text" in sql
    # _ELIGIBLE is what makes it a RE-judge: an image already answered under the
    # ACTIVE definition is not eligible, so this never re-asks the same wording.
    assert "l.definition_id = d.id" in sql
    assert "FROM tag_exam_members m WHERE m.image_id = i.id" in sql
    assert "ORDER BY random()" in sql and "ORDER BY i.id" not in sql


def test_the_rejudge_draw_refuses_a_state_that_is_not_a_verdict() -> None:
    from toolkit import machine_labeling as ml

    conn = _Conn([])
    ml.rejudge_candidates(conn, tag_ids=[48], rejudge_tag=48, state="positive", limit=5)
    assert conn.log[0][2]["rejudge_state"] == "positive"
    with pytest.raises(ValueError):
        ml.rejudge_candidates(_Conn([]), tag_ids=[48], rejudge_tag=48, state="lift")


def test_the_like_tag_draw_takes_a_siblings_pool_not_its_verdicts() -> None:
    # "Same coverage as the neighbour" means the same IMAGES. Filtering by the
    # sibling's verdict would seed a new head on its neighbour's positives and
    # bake that boundary in as ground truth before anyone reviewed it.
    from toolkit import machine_labeling as ml

    sql = ml._LIKE_TAG_SQL
    assert "l.tag_id = %(like_tag)s::bigint" in sql
    assert "l.state" not in sql
    assert "ORDER BY random()" in sql and "ORDER BY i.id" not in sql
    # The shared rails still apply to what it returns.
    assert "FROM tag_exam_members m WHERE m.image_id = i.id" in sql
    assert "l.definition_id = d.id" in sql


def test_the_lane_validates_from_drafts_as_digits_only() -> None:
    import yaml

    lane = yaml.safe_load((ROOT / ".github" / "workflows" / "label_images.yml").read_text())
    assert "from_drafts" in lane[True]["workflow_dispatch"]["inputs"]
    run = next(s for s in lane["jobs"]["label"]["steps"] if s.get("name") == "Label")["run"]
    assert "*[!0-9]*) echo \"::error::from_drafts must be a tag id\"" in run


def test_the_near_tag_draw_raises_the_timeout_for_its_own_scan_and_restores_it() -> None:
    # A deliberate analytical scan over millions of vectors, on an AUTOCOMMIT
    # connection — so SET LOCAL would apply to nothing and the session setting
    # has to be put back.
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "toolkit" / "machine_labeling.py").read_text()
    fn = src.split("def near_tag_candidates(")[1].split("\ndef ")[0]
    # SET takes a literal, never a bound parameter: `SET x = %s` is a syntax
    # error at "$1". set_config is the parameterised form.
    assert "set_config('statement_timeout', %s, false)" in fn
    assert "SET statement_timeout = %s" not in fn
    assert "finally:" in fn
    assert "RESET statement_timeout" in fn


def test_no_statement_carries_a_bare_percent_sign() -> None:
    # A literal % in executed SQL — prose in a comment counts — is read by
    # psycopg as the start of a placeholder, and Postgres then raises a syntax
    # error. It has bitten this repo before; the SQL gate catches it a CI round
    # trip later, this catches it instantly.
    import re

    from toolkit import machine_labeling as ml

    for name in dir(ml):
        if not name.endswith("_SQL"):
            continue
        sql = getattr(ml, name)
        if not isinstance(sql, str):
            continue
        bare = re.findall(r"%(?!\()", sql)
        assert not bare, f"{name} carries {len(bare)} bare percent sign(s)"


# ------------------------------------------------------------------ the review read
def test_the_training_set_read_excludes_the_holdout() -> None:
    # Correcting a yardstick image from a training-review page would quietly
    # train on the thing that grades us.
    from toolkit import machine_labeling as ml

    for sql in (ml._TRAINING_PAGE_SQL, ml._TRAINING_COUNTS_SQL):
        assert "tag_exam_members hx" in sql and "tag_exam_cohorts hc" in sql


def test_the_page_orders_by_a_unique_tiebreaker() -> None:
    # A bulk write stamps thousands of rows in the same second; updated_at
    # alone would reshuffle rows between pages and show duplicates.
    from toolkit import machine_labeling as ml

    assert "ORDER BY l.updated_at DESC, l.image_id DESC" in ml._TRAINING_PAGE_SQL


def test_the_page_refuses_a_state_it_does_not_understand() -> None:
    from toolkit import machine_labeling as ml

    with pytest.raises(ValueError):
        ml.training_set_page(_Conn([]), tag_id=22, state="maybe")


def test_the_page_caps_its_limit_and_floors_its_offset() -> None:
    from toolkit import machine_labeling as ml

    conn = _Conn([])
    ml.training_set_page(conn, tag_id=22, limit=50_000, offset=-5)
    params = conn.log[0][2]
    assert params["limit"] == ml.PAGE_MAX == 10_000 and params["offset"] == 0
    # 10,000 is a page the operator can ask for, not a value the cap eats: the
    # largest tray (a head's ~10.5k negatives) fits in one.
    conn = _Conn([])
    ml.training_set_page(conn, tag_id=22, limit=10_000)
    assert conn.log[0][2]["limit"] == 10_000


def test_the_draw_is_random_and_takes_the_existing_order_from_nowhere() -> None:
    # The operator asked for a RANDOM thousand shown in the order the negatives
    # already had. So the draw randomises and the PAGE orders — if the draw ever
    # acquired an ORDER BY of its own it would be a sorting mechanism, which is
    # exactly what they said not to build.
    from toolkit import machine_labeling as ml

    sql = ml._DRAW_SAMPLE_SQL
    assert "ORDER BY random()" in sql
    assert "updated_at" not in sql and "row_number()" not in sql
    # Same rails as every other training read: no holdout, no missing bytes.
    assert "tag_exam_cohorts hc" in sql
    assert "i.storage_path IS NOT NULL" in sql


def test_the_draw_replaces_the_whole_set_in_one_transaction() -> None:
    # Clearing the old draw and admitting the new one are one unit: a head with
    # its negatives cleared and nothing drawn trains on no negatives at all.
    from toolkit import machine_labeling as ml

    conn = _Conn([], [])
    ml.draw_training_set(conn, tag_id=17, size=99_999)
    kinds = [c[0] for c in conn.log]
    assert kinds[0] == "transaction"
    assert "SET in_training = false" in conn.log[1][1]
    assert conn.log[-1][2]["size"] == ml.DRAW_MAX == 5000
    with pytest.raises(ValueError):
        ml.draw_training_set(_Conn([]), tag_id=17, state="maybe")


def test_counts_are_five_trays_one_rule_for_both_signs() -> None:
    # Migration 486, the operator's renaming: membership is a fact about a
    # LABEL. Each sign has a training set and a reserve; "left out" trains
    # nothing whichever way the flag points.
    from toolkit import machine_labeling as ml

    rows = [(17, "positive", True, 300), (17, "positive", False, 120),
            (17, "negative", True, 1000), (17, "negative", False, 9626),
            (17, "excluded", False, 34)]
    out = ml.training_set_counts(_Conn(rows), tag_ids=[17, 25])
    assert out[17] == {"positive": 300, "positive_reserve": 120,
                       "negative": 1000, "negative_reserve": 9626, "excluded": 34}
    # A head with nothing still reports every tray, so the page never renders a
    # blank where a zero belongs.
    assert out[25] == {"positive": 0, "positive_reserve": 0,
                       "negative": 0, "negative_reserve": 0, "excluded": 0}


def test_locate_maps_state_and_membership_to_a_tray_and_a_row() -> None:
    # A link names a head and a photo; the page needs a tray and a page number.
    # An unadmitted positive is the RESERVE — the one mapping the page cannot
    # get wrong without sending the operator to a tray the photo is not in.
    from toolkit import machine_labeling as ml

    assert ml.locate_in_training_set(_Conn([("positive", False, 137)]),
                                     tag_id=42, image_id=12) == {
        "tray": "reserve", "state": "positive", "in_training": False, "rank": 137}
    assert ml.locate_in_training_set(_Conn([("positive", True, 0)]),
                                     tag_id=42, image_id=11)["tray"] == "positive"
    assert ml.locate_in_training_set(_Conn([("negative", True, 9)]),
                                     tag_id=42, image_id=13)["tray"] == "negative"
    # No label for that head, or a holdout image: nothing to page to.
    assert ml.locate_in_training_set(_Conn([]), tag_id=42, image_id=99) is None


def test_locate_ranks_under_the_pages_own_order_and_rails() -> None:
    # A rank computed against a different order or a different row set pages to
    # a DIFFERENT photo than the one the link named.
    from toolkit import machine_labeling as ml

    sql = ml._LOCATE_SQL
    assert "(o.updated_at, o.image_id) > (l.updated_at, l.image_id)" in sql
    assert "oi.storage_path IS NOT NULL" in sql and "i.storage_path IS NOT NULL" in sql
    # Membership splits the positives into two trays and nothing else.
    assert "l.state <> 'positive' OR o.in_training = l.in_training" in sql
    # The holdout is refused on both sides — the row itself and the rank's cohort.
    assert sql.count("tag_exam_cohorts hc") == 2


def test_the_trainer_reads_admitted_rows_only() -> None:
    from toolkit import machine_labeling as ml

    assert "AND l.in_training" in ml._TRAINING_ROWS_SQL
    assert "tag_exam_cohorts hc" in ml._TRAINING_ROWS_SQL


def test_the_page_filters_by_tray_not_by_who_wrote_it() -> None:
    from toolkit import machine_labeling as ml

    sql = ml._TRAINING_PAGE_SQL
    assert "l.in_training = %(in_training)s::boolean" in sql
    assert "source_class" not in sql
    conn = _Conn([])
    ml.training_set_page(conn, tag_id=42, state="positive", in_training=False)
    assert conn.log[0][2]["in_training"] is False


def test_a_label_written_under_replaced_wording_is_flagged() -> None:
    from toolkit import machine_labeling as ml

    import datetime as dt
    now = dt.datetime(2026, 9, 5)
    conn = _Conn([(5, "img/a.jpg", "positive", "machine", None, now, 3, "superseded", None, None, True),
                  (6, "img/b.jpg", "positive", "machine", None, now, 4, "active", None, None, False)])
    rows = ml.training_set_page(conn, tag_id=22)
    assert rows[0]["definition_stale"] is True and rows[0]["definition_version"] == 3
    assert rows[1]["definition_stale"] is False


# ------------------------------------------------------------------ the training set, no limits
def test_the_trainer_reads_every_positive_and_negative_human_and_machine() -> None:
    # The operator's ruling (2026-09-07): no target, no reserve, no ranking. A
    # head's set is every label it has, minus the holdout — and the page's
    # trays read the same rows, so they cannot disagree.
    from toolkit import machine_labeling as ml

    sql = ml._TRAINING_ROWS_SQL
    assert "tag_exam_cohorts hc" in sql            # the holdout never trains
    assert "l.source = ANY(%(sources)s::text[])" in sql
    conn = _Conn([(1, "positive"), (2, "negative")])
    assert ml.training_rows(conn, tag_id=17) == [(1, "positive"), (2, "negative")]
    params = conn.log[0][2]
    assert params["states"] == ["positive", "negative"]
    assert set(params["sources"]) == {"machine", "human", "human_confirmed"}
    with pytest.raises(ValueError):
        ml.training_rows(conn, tag_id=17, states=("excluded",))   # left out trains nothing


def test_the_page_read_carries_the_note_and_no_rank() -> None:
    from toolkit import machine_labeling as ml

    sql = ml._TRAINING_PAGE_SQL
    assert "LEFT JOIN LATERAL" in sql and "n.absorbed_definition_id IS NULL" in sql
    assert "set_rank" not in sql and "training_target" not in sql
    # Every row's membership is simply its state; there is nothing to rank.
    import datetime as dt
    conn = _Conn([(5, "img/a.jpg", "positive", "machine", None, dt.datetime(2026, 9, 7), 9,
                   "active", 36, "front shot", True)])
    rows = ml.training_set_page(conn, tag_id=3)
    assert rows[0]["note_id"] == 36 and rows[0]["note"] == "front shot"
    assert "in_set" not in rows[0]


def test_a_new_head_may_be_seeded_from_a_relatives_positives() -> None:
    # A head with no positives cannot seed itself. The eligibility rails key on
    # the heads being LABELED; the seed only says where to look.
    src = (ROOT / "scripts" / "label_images.py").read_text()
    # --from-drafts keeps its own-head check (a draft is a guess ABOUT that
    # head); only the near-tag seed is free to name a relative.
    assert "--near-tag %d must be one of the heads being labeled" not in src
    assert "--from-drafts %d must be one of the heads being labeled" in src
    assert "seeding a NEW" in src
    from toolkit import machine_labeling as ml

    # set_config, the draw and RESET share ONE cursor, so one batch feeds all.
    conn = _Conn([(5, "img/a.jpg")])
    rows = ml.near_tag_candidates(conn, seed_tag_id=46, tag_ids=[45], limit=10)
    assert rows == [(5, "img/a.jpg")]
    params = conn.log[1][2]
    assert params["seed_tag_id"] == 46 and params["tag_ids"] == [45]
