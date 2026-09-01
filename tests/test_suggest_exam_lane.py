"""The suggest lane — machine pre-answers for exam sittings (migration 461).

Same spending discipline as the screen lane (same engine, same rails), plus the
things only this lane can get wrong: it must accept a SEALED cohort (sittings
happen after sealing), its prompt must lean the opposite way from the screener's
(precision over recall — a wrong mark anchors the human), and a stored
suggestion must never be served against a question list it did not answer.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
LANE = ROOT / ".github" / "workflows" / "screen_exam_cohort.yml"


@pytest.fixture(scope="module")
def lane() -> dict[str, Any]:
    return yaml.safe_load(LANE.read_text())


def _step(lane: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in lane["jobs"]["screen"]["steps"] if s.get("name") == "Screen")


def test_the_lane_offers_the_suggest_action(lane: dict[str, Any]) -> None:
    action = lane[True]["workflow_dispatch"]["inputs"]["action"]
    assert "suggest" in action["options"]
    assert "set" in lane[True]["workflow_dispatch"]["inputs"]
    step = _step(lane)
    assert "scripts.suggest_exam_answers" in step["run"]
    assert "EXAM_SET" in step["env"]


def test_the_set_input_is_validated_like_every_other_free_text(lane: dict[str, Any]) -> None:
    run = _step(lane)["run"]
    assert "suggest needs set=" in run


def test_the_called_for_value_is_in_both_the_literal_and_the_check() -> None:
    from api.llm_client import CalledFor
    import typing
    assert "suggest_exam_answer" in typing.get_args(CalledFor)
    mig = (ROOT / "migrations" / "461_exam_letters_sets12_and_suggestions.sql").read_text()
    assert "'suggest_exam_answer'" in mig


def test_the_token_budget_leaves_room_for_reasoning() -> None:
    # The screen lane's lesson, measured twice: gpt-5-mini spends output tokens
    # on reasoning first, and a budget sized for the ~30-token answer returns an
    # empty string — billed in full.
    import scripts.suggest_exam_answers as mod
    assert mod.MAX_TOKENS >= 4096


def test_a_failed_suggestion_is_offered_again() -> None:
    # An errored row is an absence of evidence, not a suggestion; excluding it
    # from the resume query would strand it forever.
    from toolkit import exam_suggestions
    sql = " ".join(exam_suggestions._UNSUGGESTED_MEMBERS_SQL.split())
    assert "s.error IS NULL" in sql


def test_a_stale_suggestion_is_refilled_not_just_refused() -> None:
    """Serve-time refusal is only half the staleness mechanism. Measured live:
    set_2 grew 8 -> 10 tags, the API correctly refused every stored suggestion,
    and both 'successful' re-runs found zero members to suggest — the lane's
    resume filter counted the stale rows as done. The resume query must demand
    the CURRENT question list (mutual containment = set equality, no dups)."""
    from toolkit import exam_suggestions
    sql = " ".join(exam_suggestions._UNSUGGESTED_MEMBERS_SQL.split())
    assert "s.asked_tag_ids <@ %(tag_ids)s::bigint[]" in sql
    assert "s.asked_tag_ids @> %(tag_ids)s::bigint[]" in sql


def test_both_lanes_share_one_engine() -> None:
    # A copied worker loop is the kind of drift where one copy learns a lesson
    # (the budget lock, the per-worker connection) and the other repeats it.
    import inspect
    import scripts.screen_exam_cohort as screen
    import scripts.suggest_exam_answers as suggest
    assert "run_vision_batch" in inspect.getsource(screen._screen_batch)
    assert "run_vision_batch" in inspect.getsource(suggest._suggest_batch)


def test_the_prompt_leans_toward_precision_not_recall() -> None:
    # The screener errs toward including (a miss costs coverage); a suggestion
    # errs toward omitting (a wrong mark anchors the human toward a wrong
    # answer). Same JSON contract, opposite tuning.
    from toolkit import exam_suggestions
    prompt = exam_suggestions.build_prompt([{"id": 26, "label": "interier - ložnice"}])
    assert "ONLY what you would defend" in prompt
    assert "Err towards INCLUDING" not in prompt
    assert '{"ids":' in prompt
    assert "26: interier - ložnice" in prompt


# --- the function actually RUNS ---------------------------------------------


def test_suggest_batch_really_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive _suggest_batch end to end against fakes — the screen lane shipped a
    NameError once because every test read source and none executed it."""
    import scripts.suggest_exam_answers as mod

    written: list[dict[str, Any]] = []

    class _FakeLLM:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        def call(self, **kw: Any) -> Any:
            class _R:
                cost_usd = 0.001
                text = '{"ids": [26]}'
            return _R()

    class _FakeConn:
        def close(self) -> None: ...

    monkeypatch.setattr("api.llm_client.LLMClient", _FakeLLM)
    monkeypatch.setattr("api.providers.openai.OpenAIProvider", lambda *a, **k: object())
    monkeypatch.setattr("scraper.db.connect", lambda *a, **k: _FakeConn())
    monkeypatch.setattr("toolkit.vision_images.image_block", lambda *a, **k: {})
    monkeypatch.setattr("toolkit.exam_suggestions.record_suggestion",
                        lambda conn, **kw: written.append(kw))

    stats = mod._suggest_batch(
        object(), cohort_id=1, set_id=2, rows=[(i, f"img/{i}.jpg") for i in range(1, 9)],
        tags=[{"id": 26, "label": "ložnice"}, {"id": 30, "label": "předsíň"}],
        model="gpt-5-mini", max_usd=0, max_seconds=0, workers=4,
    )
    assert stats["ok"] == 8 and stats["errors"] == 0
    assert len(written) == 8
    # Every row freezes the FULL question list it answered, in set order — that
    # frozen list is what lets the API refuse a stale suggestion after a set edit.
    assert all(w["asked_tag_ids"] == [26, 30] for w in written)
    assert all(w["suggested_tag_ids"] == [26] and w["error"] is None for w in written)
    assert all(w["set_id"] == 2 for w in written)


def test_a_model_failure_is_recorded_as_an_error_not_an_empty_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.suggest_exam_answers as mod

    written: list[dict[str, Any]] = []

    class _FakeLLM:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        def call(self, **kw: Any) -> Any:
            class _R:
                cost_usd = 0.001
                text = ""          # the truncation failure, exactly
            return _R()

    class _FakeConn:
        def close(self) -> None: ...

    monkeypatch.setattr("api.llm_client.LLMClient", _FakeLLM)
    monkeypatch.setattr("api.providers.openai.OpenAIProvider", lambda *a, **k: object())
    monkeypatch.setattr("scraper.db.connect", lambda *a, **k: _FakeConn())
    monkeypatch.setattr("toolkit.vision_images.image_block", lambda *a, **k: {})
    monkeypatch.setattr("toolkit.exam_suggestions.record_suggestion",
                        lambda conn, **kw: written.append(kw))

    stats = mod._suggest_batch(
        object(), cohort_id=1, set_id=2, rows=[(1, "p"), (2, "p")],
        tags=[{"id": 26, "label": "l"}], model="m",
        max_usd=0, max_seconds=0, workers=2,
    )
    assert stats["errors"] == 2 and stats["ok"] == 0
    assert all(w["suggested_tag_ids"] is None and w["error"] for w in written)


def test_a_sealed_cohort_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The screen lane refuses sealed cohorts; this lane MUST NOT — sittings
    happen after sealing, and suggestions exist for sittings. Proven by driving
    main() past the cohort stage with a sealed cohort: it proceeds to set
    resolution instead of refusing."""
    import scripts.suggest_exam_answers as mod

    class _FakeConn:
        def __enter__(self) -> "_FakeConn": return self
        def __exit__(self, *a: Any) -> None: ...
        def close(self) -> None: ...

    monkeypatch.setattr("scraper.db.connect", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(
        "toolkit.tag_holdout.get_cohort",
        lambda conn, **kw: {"id": 1, "name": "exam_v1", "sealed_at": "2026-08-29"})
    seen: dict[str, Any] = {}
    monkeypatch.setattr("toolkit.exam_suggestions.get_set",
                        lambda conn, **kw: seen.update(kw) or None)
    monkeypatch.setattr(
        "sys.argv",
        ["suggest_exam_answers", "--cohort", "exam_v1", "--set", "nope"])
    assert mod.main() == 1          # fails on the missing SET, not the seal
    assert seen == {"name": "nope"}


def test_a_stale_suggestion_is_not_served() -> None:
    """Sets grow by columns: a suggestion computed for the 3-tag set_2 says
    nothing about the 8-tag set_2, and serving it would mark a subset of the
    buttons while looking complete."""
    from toolkit import exam_suggestions

    class _Cur:
        def __init__(self, row: Any) -> None: self._row = row
        def __enter__(self) -> "_Cur": return self
        def __exit__(self, *a: Any) -> None: ...
        def execute(self, sql: str, params: Any = None) -> None: ...
        def fetchone(self) -> Any: return self._row

    class _Conn:
        def __init__(self, row: Any) -> None: self._row = row
        def cursor(self) -> _Cur: return _Cur(self._row)

    # Asked the 3-tag list, sitting now asks 5 -> None, not a partial mark.
    stale = exam_suggestions.suggestion_for(
        _Conn(([28, 20, 27], [28])), cohort_id=1, image_id=5, set_id=2,
        current_tag_ids=[28, 20, 27, 30, 26])
    assert stale is None
    # Same list (order-insensitively) -> served, filtered to the current set.
    fresh = exam_suggestions.suggestion_for(
        _Conn(([27, 20, 28], [28, 99])), cohort_id=1, image_id=5, set_id=2,
        current_tag_ids=[28, 20, 27])
    assert fresh == [28]
    # No row at all -> None ("not computed"), which the client shows as nothing.
    none = exam_suggestions.suggestion_for(
        _Conn(None), cohort_id=1, image_id=5, set_id=2, current_tag_ids=[28])
    assert none is None
