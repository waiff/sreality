"""Hermetic tests for api.model_compare — snapshot + per-model GitHub dispatch guardrails.

No DB, no network: a fake conn returns a scripted snapshot rowcount and `requests.post` is
monkeypatched to capture dispatches.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from api import model_compare


class _FakeCursor:
    def __init__(self, snapshot_rows: int) -> None:
        self._snapshot_rows = snapshot_rows
        self._last: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any) -> None:
        # The snapshot INSERT ... RETURNING sreality_id_a returns one row per snapshotted pair.
        self._last = [(i,) for i in range(self._snapshot_rows)]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._last

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, snapshot_rows: int) -> None:
        self._snapshot_rows = snapshot_rows

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._snapshot_rows)

    @contextmanager
    def transaction(self):
        yield self


def test_no_comparable_pairs_raises_404(monkeypatch):
    monkeypatch.setenv("GH_DISPATCH_TOKEN", "tok")
    with pytest.raises(Exception) as ei:
        model_compare.compare_models(_FakeConn(0), limit=25)
    assert getattr(ei.value, "status_code", None) == 404


def test_missing_dispatch_token_raises_503_after_snapshot(monkeypatch):
    monkeypatch.delenv("GH_DISPATCH_TOKEN", raising=False)
    with pytest.raises(Exception) as ei:
        model_compare.compare_models(_FakeConn(3), limit=25)
    assert getattr(ei.value, "status_code", None) == 503


def test_success_dispatches_once_per_model(monkeypatch):
    monkeypatch.setenv("GH_DISPATCH_TOKEN", "tok")
    calls: list[dict[str, Any]] = []

    class _Resp:
        status_code = 204
        text = ""

    def _fake_post(url: str, *, headers: dict, json: dict, timeout: int) -> _Resp:
        calls.append({"url": url, "json": json})
        return _Resp()

    monkeypatch.setattr(model_compare.requests, "post", _fake_post)
    out = model_compare.compare_models(_FakeConn(4), limit=25)

    assert out["dispatched"] is True
    assert out["pair_count"] == 4
    assert out["models"] == list(model_compare.MODELS_ALL)
    assert len(calls) == len(model_compare.MODELS_ALL)
    # every dispatch carries the same run_label and its own candidate_model
    labels = {c["json"]["inputs"]["run_label"] for c in calls}
    assert labels == {out["run_label"]}
    dispatched_models = [c["json"]["inputs"]["candidate_model"] for c in calls]
    assert dispatched_models == list(model_compare.MODELS_ALL)
    assert out["model_testing_url"] == f"/model-testing?run={out['run_label']}"


def test_candidate_ids_are_passed_to_the_snapshot(monkeypatch):
    monkeypatch.setenv("GH_DISPATCH_TOKEN", "tok")
    seen: dict[str, Any] = {}

    class _CaptureCursor(_FakeCursor):
        def execute(self, sql: str, params: Any) -> None:
            seen["params"] = params
            super().execute(sql, params)

    class _CaptureConn(_FakeConn):
        def cursor(self) -> _CaptureCursor:
            return _CaptureCursor(self._snapshot_rows)

    monkeypatch.setattr(model_compare.requests, "post",
                        lambda *a, **k: type("R", (), {"status_code": 204, "text": ""})())
    model_compare.compare_models(_CaptureConn(2), candidate_ids=[11, 22], limit=25)
    assert seen["params"]["ids"] == [11, 22]
