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
