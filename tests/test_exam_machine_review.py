"""The definition-driven machine review (migration 467): verdicts beside the
human answers, never labels. What can go wrong is specific — a reply parsed as
a row of no's instead of an error, a prompt that asks by name instead of by
definition, a stale verdict served under new wording, a lane action that spends
without the rails — and each has a test here.
"""

from __future__ import annotations

import json
import pathlib
import typing
from typing import Any

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]

DEFINITIONS = [
    {"tag_id": 22, "label": "interier - koupelna", "version": 8,
     "definition": {"means": "A photo whose subject is a bathroom.",
                    "counts": ["A bathroom with a shower"],
                    "does_not_count": [{"case": "A separate toilet room", "goes_to_tag_id": 36}],
                    "leave_out_when": "A bathroom is clearly in frame but the photo is of the hallway.",
                    "referenced_tags": [{"tag_id": 36, "label": "interier - wc"}]}},
    {"tag_id": 36, "label": "interier - wc", "version": 3,
     "definition": {"means": "A photo whose subject is a separate toilet room.",
                    "counts": ["A standalone WC room"], "does_not_count": [],
                    "leave_out_when": None}},
]


# ------------------------------------------------------------------ the prompt
def test_the_prompt_carries_every_definition_and_the_rule_once() -> None:
    from toolkit import exam_machine_review as mr
    from toolkit.tag_definition_render import THREE_TIER_RULE

    prompt = mr.build_prompt(DEFINITIONS)
    assert "[TAG ID 22]" in prompt and "[TAG ID 36]" in prompt
    assert 'TAG: "interier - koupelna"' in prompt
    assert "A separate toilet room" in prompt and 'belongs to "interier - wc"' in prompt
    assert "LEAVE OUT (answer skip) WHEN" in prompt
    # The three-tier rule is the closing instruction, stated exactly once —
    # eighteen copies would bury the JSON contract.
    assert prompt.count(THREE_TIER_RULE) == 1
    assert prompt.index(THREE_TIER_RULE) > prompt.index("[TAG ID 36]")
    assert '"verdicts"' in prompt and "(22, 36)" in prompt


# ------------------------------------------------------------------ the parser
def test_a_full_well_formed_reply_parses() -> None:
    from toolkit import exam_machine_review as mr

    text = '```json\n{"verdicts": {"22": "yes", "36": " No "}}\n```'
    assert mr.parse_verdicts(text, valid_ids={22, 36}) == {22: "yes", 36: "no"}


@pytest.mark.parametrize("text", [
    "I cannot decide",
    '{"ids": [22]}',
    '{"verdicts": {"22": "yes"}}',            # 36 missing: an error, never a no
    '{"verdicts": {"22": "maybe", "36": "no"}}',
])
def test_anything_short_of_a_full_answer_is_an_error_not_a_row_of_nos(text: str) -> None:
    from toolkit import exam_machine_review as mr

    with pytest.raises(ValueError):
        mr.parse_verdicts(text, valid_ids={22, 36})


def test_invented_tags_are_dropped_silently() -> None:
    from toolkit import exam_machine_review as mr

    text = '{"verdicts": {"22": "yes", "36": "skip", "999": "yes", "x": "no"}}'
    assert mr.parse_verdicts(text, valid_ids={22, 36}) == {22: "yes", 36: "skip"}


# ------------------------------------------------------------------ provenance
class _Cur:
    def __init__(self, rows: list[tuple], log: list) -> None:
        self._rows, self._log = rows, log

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: Any) -> None: ...

    def execute(self, sql: str, params: Any = None) -> None:
        self._log.append((sql, params))

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


def test_needing_review_demands_the_current_list_and_versions() -> None:
    # The staleness rail lives in SQL: both containment directions on the asked
    # list (set equality) AND jsonb equality on the definition versions.
    from toolkit import exam_machine_review as mr

    conn = _Conn([(555, "img/1/555.jpg")])
    rows = mr.members_needing_review(
        conn, cohort_id=7, tag_ids=[22, 36], versions={"22": 8, "36": 3}, limit=10)
    assert rows == [(555, "img/1/555.jpg")]
    sql, params = conn.log[0]
    assert "asked_tag_ids <@" in sql and "asked_tag_ids @>" in sql
    assert "definition_versions = %(versions)s::jsonb" in sql
    assert json.loads(params["versions"]) == {"22": 8, "36": 3}


def test_record_resets_dismissals_and_freezes_provenance() -> None:
    from toolkit import exam_machine_review as mr

    conn = _Conn([])
    mr.record_review(
        conn, cohort_id=7, image_id=555, asked_tag_ids=[22, 36],
        versions={"22": 8, "36": 3}, verdicts={22: "yes", 36: "no"}, model="m")
    sql, params = conn.log[0]
    assert "dismissed_tag_ids = '{}'::bigint[]" in sql
    assert json.loads(params["verdicts"]) == {"22": "yes", "36": "no"}
    assert json.loads(params["versions"]) == {"22": 8, "36": 3}
    assert params["asked_tag_ids"] == [22, 36]


def test_reviews_for_answers_serves_nothing_when_a_tag_has_no_definition() -> None:
    # A tag without an active definition means no review can be current, and
    # serving one would surface verdicts against wording that no longer exists.
    from toolkit import exam_machine_review as mr

    conn = _Conn([(22, 8), (36, None)])
    assert mr.reviews_for_answers(conn, cohort_id=7, tag_ids=[22, 36]) == {}


def test_reviews_for_answers_keeps_only_the_sittings_tags() -> None:
    from toolkit import exam_machine_review as mr

    conn = _Conn(
        [(22, 8), (36, 3)],
        [(555, {"22": "yes", "36": "no", "99": "yes"}, [36, 99], None)],
    )
    out = mr.reviews_for_answers(conn, cohort_id=7, tag_ids=[22, 36])
    assert out == {555: {"verdicts": {22: "yes", 36: "no"},
                         "dismissed_tag_ids": [36], "reviewed_at": None}}


def test_dismiss_raises_when_there_is_no_review_row() -> None:
    from toolkit import exam_machine_review as mr

    with pytest.raises(KeyError):
        mr.dismiss_proposal(_Conn([]), cohort_id=7, image_id=555, tag_id=22)
    assert mr.dismiss_proposal(_Conn([([22, 36],)]), cohort_id=7, image_id=555,
                               tag_id=36) == [22, 36]


def test_active_definitions_refuses_a_tag_without_one() -> None:
    from toolkit import exam_machine_review as mr

    with pytest.raises(ValueError, match=r"\[36\]"):
        mr.active_definitions(_Conn([(22, "koupelna", 8), (36, "wc", None)]), tag_ids=[22, 36])


# ------------------------------------------------------------------ never labels
def test_the_module_never_touches_image_tag_labels() -> None:
    src = (ROOT / "toolkit" / "exam_machine_review.py").read_text()
    assert "image_tag_labels" not in src
    mig = (ROOT / "migrations" / "467_exam_machine_review.sql").read_text()
    assert "create table tag_exam_machine_reviews" in mig
    assert "'review_exam_image'" in mig
    assert "revoke all on tag_exam_machine_reviews from anon, authenticated" in mig


def test_the_called_for_value_is_in_the_literal() -> None:
    from api.llm_client import CalledFor
    from toolkit import exam_machine_review as mr

    assert mr.CALLED_FOR in typing.get_args(CalledFor)


# ------------------------------------------------------------------ the lane
def test_the_lane_offers_the_review_action() -> None:
    lane = yaml.safe_load((ROOT / ".github" / "workflows" / "screen_exam_cohort.yml").read_text())
    action = lane[True]["workflow_dispatch"]["inputs"]["action"]
    assert "review" in action["options"]
    step = next(s for s in lane["jobs"]["screen"]["steps"] if s.get("name") == "Screen")
    assert "scripts.review_exam_answers" in step["run"]
    assert "review needs set=" in step["run"]


# ------------------------------------------------------------------ the answers read
def test_answers_route_attaches_the_machine_block() -> None:
    # The route composes tag_exam.answers with reviews_for_answers keyed by
    # image; a row without a current review carries machine=None, never a
    # missing key (the page distinguishes "no review" from "agrees").
    src = (ROOT / "api" / "new_dedup_labeling.py").read_text()
    assert 'row["machine"] = reviews.get(row["image_id"])' in src
    assert '/exam/{cohort_name}/machine-review/dismiss' in src
