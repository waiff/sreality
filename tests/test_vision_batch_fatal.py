"""What a vision pass does when the provider stops answering.

Written from a live incident: a 2,500-image labeling run hit "no credits
remaining" on image 461, then retried the dead account 2,039 more times —
seven minutes of runner, 2,039 identical log lines — and exited 0, because the
only failure test asked whether ANY image had succeeded.

Two properties follow, and both are about telling apart the errors worth
retrying from the ones that will never get better.
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("message", [
    'HTTP 429 {"error": {"message": "You have no credits remaining. Add credits"}}',
    'HTTP 429 {"error": {"code": "insufficient_quota"}}',
    "You exceeded your current quota, please check your plan",
    'HTTP 401 {"error": {"code": "invalid_api_key"}}',
    "HTTP 403 forbidden",
])
def test_an_exhausted_or_rejected_key_is_fatal(message: str) -> None:
    from toolkit import vision_batch

    assert vision_batch.is_fatal(message)


@pytest.mark.parametrize("message", [
    # A PLAIN rate limit shares the 429 status and is exactly what retrying is
    # for — matching on the status code would have stopped these too.
    'HTTP 429 {"error": {"message": "Rate limit reached for gpt-5-mini"}}',
    "HTTP 500 internal server error",
    "connection reset by peer",
    "no JSON object in review reply: 'I cannot tell'",
])
def test_a_retryable_failure_is_not_fatal(message: str) -> None:
    from toolkit import vision_batch

    assert not vision_batch.is_fatal(message)


def test_the_engine_stops_the_pass_on_a_fatal_error() -> None:
    # The stop check consults the fatal flag BEFORE the budget and the clock,
    # so the remaining images are never attempted.
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "toolkit" / "vision_batch.py").read_text()
    stop = src.split("def _stop() -> bool:")[1].split("def ")[0]
    assert 'if stats["fatal"]:' in stop
    assert stop.index('stats["fatal"]') < stop.index("max_usd")


@pytest.mark.parametrize("script", ["label_images", "review_exam_answers"])
def test_a_mostly_failed_run_exits_non_zero(script: str) -> None:
    import importlib

    mod = importlib.import_module(f"scripts.{script}")
    base = {"ok": 0, "errors": 0, "spent": 0.0, "aborted": False, "fatal": None}
    # Provider died: fail, however many images had already landed.
    assert mod._exit_code({**base, "ok": 461, "errors": 2039,
                           "fatal": "no credits remaining"}, "T") == 1
    # No fatal marker, but the majority failed: still a failed run.
    assert mod._exit_code({**base, "ok": 10, "errors": 90}, "T") == 1
    # A handful of errors among successes is a warning, not a failure — those
    # images stay eligible and the next run picks them up.
    assert mod._exit_code({**base, "ok": 990, "errors": 10}, "T") == 0
    assert mod._exit_code({**base, "ok": 100, "errors": 0}, "T") == 0
