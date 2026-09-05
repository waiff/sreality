"""Bulk machine labeling: the LLM building training sets for named heads.

The failure modes here are the expensive kind — a poisoned training set is
invisible until a classifier trained on it underperforms for reasons nobody can
trace. So the tests pin the things that would poison it: a leave-out written as
a negative, an unusable reply written as anything at all, an exam member
labeled, and a head labeled without being named.
"""

from __future__ import annotations

import pathlib
import typing
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Cur:
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


def test_the_two_targeted_draws_are_mutually_exclusive() -> None:
    # They answer different questions; silently letting one win would make the
    # run's provenance unreadable afterwards.
    src = (ROOT / "scripts" / "label_images.py").read_text()
    assert "--from-drafts and --near-tag are different draws; pick one" in src


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


def test_the_page_refuses_a_state_or_source_it_does_not_understand() -> None:
    from toolkit import machine_labeling as ml

    with pytest.raises(ValueError):
        ml.training_set_page(_Conn([]), tag_id=22, state="maybe")
    with pytest.raises(ValueError):
        ml.training_set_page(_Conn([]), tag_id=22, source_class="robot")


def test_the_page_caps_its_limit_and_floors_its_offset() -> None:
    from toolkit import machine_labeling as ml

    conn = _Conn([])
    ml.training_set_page(conn, tag_id=22, limit=10_000, offset=-5)
    params = conn.log[0][2]
    assert params["limit"] == 200 and params["offset"] == 0


def test_counts_split_positives_by_who_decided_them() -> None:
    # A head carried entirely by machine work reads differently from one the
    # operator has confirmed, and the picker has to show that.
    from toolkit import machine_labeling as ml

    conn = _Conn([(22, "positive", True, 400), (22, "positive", False, 20),
                  (22, "negative", True, 5000)])
    out = ml.training_set_counts(conn, tag_ids=[22, 25])
    assert out[22]["positive"] == 420
    assert out[22]["machine_positive"] == 400 and out[22]["human_positive"] == 20
    assert out[22]["negative"] == 5000
    assert out[25]["positive"] == 0


def test_a_label_written_under_replaced_wording_is_flagged() -> None:
    from toolkit import machine_labeling as ml

    import datetime as dt
    now = dt.datetime(2026, 9, 5)
    conn = _Conn([(5, "img/a.jpg", "positive", "machine", None, now, 3, "superseded"),
                  (6, "img/b.jpg", "positive", "machine", None, now, 4, "active")])
    rows = ml.training_set_page(conn, tag_id=22)
    assert rows[0]["definition_stale"] is True and rows[0]["definition_version"] == 3
    assert rows[1]["definition_stale"] is False


# ------------------------------------------------------------------ the cutoff
def test_the_set_is_a_query_ranked_human_first_then_machine_oldest_first() -> None:
    # Confirmed positives are always in; the machine's fill the rest in a total
    # order, so "position 300" is the same image on every read and removing one
    # pulls exactly the next one in.
    from toolkit import machine_labeling as ml

    cte = ml._RANKED_POSITIVES_CTE
    assert "ORDER BY (l.source = 'machine') ASC, l.created_at ASC, l.image_id ASC" in cte
    assert "PARTITION BY l.tag_id" in cte
    assert "coalesce(t.training_target, %(default_target)s::int)" in cte
    assert "tag_exam_cohorts hc" in cte  # the holdout never ranks into a set


def test_the_summary_counts_set_reserve_and_the_review_backlog() -> None:
    from toolkit import machine_labeling as ml

    conn = _Conn([(3, 300, 300, 849, 282)])
    out = ml.set_summary(conn, tag_ids=[3, 2])
    assert out[3] == {"target": 300, "in_set": 300, "reserve": 849, "in_set_unreviewed": 282}
    # A head the query returned nothing for still carries the default target.
    assert out[2] == {"target": 300, "in_set": 0, "reserve": 0, "in_set_unreviewed": 0}


def test_the_ranked_page_filters_membership_in_sql_and_orders_by_rank() -> None:
    from toolkit import machine_labeling as ml

    sql = ml._TRAINING_PAGE_RANKED_SQL
    assert "%(membership)s::text = 'set' AND r.set_rank <= tg.target" in sql
    assert "%(membership)s::text = 'reserve' AND r.set_rank > tg.target" in sql
    assert "ORDER BY r.set_rank ASC NULLS LAST, l.updated_at DESC, l.image_id DESC" in sql
    with pytest.raises(ValueError):
        ml.training_set_page_ranked(_Conn([]), tag_id=3, membership="maybe")


def test_the_target_is_bounded_and_none_restores_the_default() -> None:
    from toolkit import machine_labeling as ml

    with pytest.raises(ValueError):
        ml.set_training_target(_Conn([]), tag_id=3, target=0)
    with pytest.raises(ValueError):
        ml.set_training_target(_Conn([]), tag_id=3, target=99_999)
    out = ml.set_training_target(_Conn([(3, 300)]), tag_id=3, target=None)
    assert out == {"tag_id": 3, "target": 300, "is_default": True}
    with pytest.raises(KeyError):
        ml.set_training_target(_Conn([]), tag_id=999, target=200)


def test_the_reads_survive_474_not_being_applied() -> None:
    # Merge is not apply: until the column exists every head has the default.
    import psycopg

    from toolkit import machine_labeling as ml

    class _NoColumn(_Conn):
        def cursor(self) -> _Cur:
            raise psycopg.errors.UndefinedColumn("column training_target does not exist")

    out = ml.set_summary(_NoColumn(), tag_ids=[3])
    assert out[3]["target"] == ml.DEFAULT_TRAINING_TARGET
    assert ml.training_set_positive_ids(_NoColumn(), tag_id=3) == []


def test_a_trainer_reads_the_set_never_the_reserve() -> None:
    # What was reviewed and what is trained on must be one list.
    from toolkit import machine_labeling as ml

    assert "WHERE r.set_rank <= tg.target" in ml._SET_POSITIVE_IDS_SQL
    conn = _Conn([(11,), (12,), (13,)])
    assert ml.training_set_positive_ids(conn, tag_id=3) == [11, 12, 13]
