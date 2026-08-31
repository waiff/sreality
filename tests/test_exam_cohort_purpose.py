"""Migration 464 — the exam instrument decouples from the holdout role.

Two cohort purposes: 'holdout' (graded, excluded from training — the original
contract, unchanged for exam_v1) and 'curated' (operator-marked images seated
for careful re-labeling; their answers ARE training material). The invariants
worth pinning are the ones a refactor would silently bend: the exclusion must
name the purpose, the warm-up must stay cohort-blind, and the curated draw
must never touch a holdout.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIG = (ROOT / "migrations" / "464_exam_cohort_purpose_and_draft_labels.sql").read_text()
NORM = " ".join(MIG.split())


def test_the_exclusion_names_the_purpose() -> None:
    # A bare member anti-join would also exclude curated training material —
    # the exact opposite of why curated cohorts exist.
    from toolkit import tag_holdout
    sql = " ".join(tag_holdout.HOLDOUT_EXCLUSION.split())
    assert "JOIN tag_exam_cohorts hc" in sql
    assert "hc.purpose = 'holdout'" in sql


def test_the_protection_census_counts_only_holdouts() -> None:
    from toolkit import tag_holdout
    sql = " ".join(tag_holdout._HOLDOUT_SIZE_SQL.split())
    assert "purpose = 'holdout'" in sql


def test_the_warmup_stays_cohort_blind() -> None:
    # The answer-refusal rail only refuses NON-members: a curated member served
    # as practice would be silently accepted as a real answer. So the warm-up
    # excludes members of EVERY cohort, deliberately not the narrowed exclusion.
    from toolkit import tag_exam
    sql = " ".join(tag_exam._WARMUP_SQL.split())
    assert "NOT EXISTS ( SELECT 1 FROM tag_exam_members wm" in sql
    assert "purpose" not in sql


def test_the_curated_seed_never_reseats_a_member_of_any_exam() -> None:
    from toolkit import tag_exam
    sql = " ".join(tag_exam._CURATED_SEED_SQL.split())
    assert "NOT EXISTS ( SELECT 1 FROM tag_exam_members m" in sql
    assert "source = 'human_draft'" in sql


def test_the_frame_vocabulary_gained_curated_everywhere() -> None:
    from toolkit import tag_exam
    assert "curated" in tag_exam.FRAMES
    assert "check (frame in ('pure_random', 'stratified', 'curated'))" in NORM


def test_464_restates_every_source_value_it_inherited() -> None:
    # The source CHECK is a drop-and-add: a value omitted here is silently
    # removed from the vocabulary and every insert with it starts failing.
    for v in ("human", "human_confirmed", "human_draft", "machine", "backfill_442"):
        assert f"'{v}'" in NORM, f"464 drops source value {v!r}"


def test_the_demotion_spares_only_holdout_member_labels() -> None:
    assert "set source = 'human_draft'" in NORM
    assert "c.purpose = 'holdout'" in NORM


def test_drafts_never_win_an_upsert() -> None:
    # A draft is a demoted opinion: the exam's careful answer AND the machine
    # labeler both overwrite it; it overwrites nothing.
    from toolkit import tag_annotations as ta
    sql = " ".join(ta._UPSERT_STATE_RETURNING_SQL.split())
    assert "image_tag_labels.source IN ('machine', 'backfill_442', 'human_draft')" in sql
    assert "human_draft" in ta.SOURCES


# --- the curated draw actually RUNS -----------------------------------------


class _Cur:
    def __init__(self, owner: "_Conn") -> None:
        self.owner = owner

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: Any) -> None: ...

    def execute(self, sql: str, params: Any = None) -> None:
        self._sql, self._params = " ".join(sql.split()), params

    def fetchall(self) -> list[tuple]:
        if "count(*)" in self._sql:              # _DRAFT_POSITIVE_COUNTS_SQL
            return [(t, len(v)) for t, v in self.owner.drafts.items()]
        taken = set(self._params["taken"])       # _CURATED_SEED_SQL
        tag = self._params["tag_id"]
        avail = [i for i in self.owner.drafts.get(tag, []) if i not in taken]
        return [(i,) for i in avail[: self._params["per_tag"]]]


class _Conn:
    def __init__(self, drafts: dict[int, list[int]]) -> None:
        self.drafts = drafts

    def cursor(self) -> _Cur:
        return _Cur(self)


def test_curated_draw_is_rarest_first_and_never_double_seats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from toolkit import tag_exam

    monkeypatch.setattr(tag_exam, "_get_cohort_by_id",
                        lambda conn, **kw: {"id": 9, "name": "gold_v1",
                                            "purpose": "curated", "sealed_at": None})
    seated: list[dict[str, Any]] = []
    monkeypatch.setattr(tag_exam, "add_members",
                        lambda conn, *, cohort_id, rows: seated.extend(rows) or len(rows))

    # Tag 20 is rare (2 drafts) and SHARES image 101 with common tag 25. Rarest
    # first means 20 takes 101; 25 must then seat around it, not re-seat it.
    conn = _Conn({25: [101, 102, 103], 20: [101, 104]})
    res = tag_exam.draw_curated_from_drafts(
        conn, cohort_id=9, tag_ids=[25, 20], per_tag=2)

    assert res["tags"][20]["seated"] == 2          # rare tag got its full quota
    assert res["tags"][25]["seated"] == 2          # 102, 103 — around the overlap
    ids = [r["image_id"] for r in seated]
    assert sorted(ids) == [101, 102, 103, 104]     # no image seated twice
    assert all(r["frame"] == "curated" and r["inclusion_probability"] == 1.0
               for r in seated)
    assert {r["stratum"] for r in seated} == {"curated:20", "curated:25"}


def test_curated_draw_refuses_a_holdout_cohort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Training material inside the yardstick is the one contamination the whole
    # split exists to prevent.
    from toolkit import tag_exam

    monkeypatch.setattr(tag_exam, "_get_cohort_by_id",
                        lambda conn, **kw: {"id": 1, "name": "exam_v1",
                                            "purpose": "holdout", "sealed_at": "t"})
    with pytest.raises(ValueError, match="holdout"):
        tag_exam.draw_curated_from_drafts(
            _Conn({}), cohort_id=1, tag_ids=[25], per_tag=20)


def test_the_lane_offers_the_curated_action() -> None:
    import yaml
    lane = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "draw_exam_cohort.yml").read_text())
    inputs = lane[True]["workflow_dispatch"]["inputs"]
    assert "curated" in inputs["action"]["options"]
    assert "per_tag" in inputs
    step = next(s for s in lane["jobs"]["draw"]["steps"]
                if s.get("name") == "Draw exam cohort")
    assert "--curated-per-tag" in step["run"]
    assert "PER_TAG" in step["env"]
