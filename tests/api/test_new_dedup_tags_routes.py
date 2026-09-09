"""Tests for /new-dedup/tags/* — the versioned tag model's read surface.

Admin-gated (require_admin); the happy-path tests override it, and one does not, to
prove the gate is on the router. The toolkit module is monkeypatched, so what is
under test here is the route layer: the envelope the frontend will code against,
the active-model resolution, and the 404s that keep "not scored yet" from arriving
as "no tag".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from toolkit import tag_heads as th
from toolkit import tag_models as tm

PREFIX = "/new-dedup/tags"

STAMP = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)

ENCODER = th.EncoderIdentity(
    model="facebook/dinov2-large", revision="cafebabe", library="transformers",
    pooling="cls", resolution=504, preprocessing="letterbox_pad", dtype="bfloat16",
)


def _model(version: str = "v1", status: str = tm.STATUS_ACTIVE) -> tm.TagModel:
    return tm.TagModel(
        id=1, version=version, label="run 1, dinov2-l14 @504", status=status,
        mode=th.MODE_POS_NEG, encoder=ENCODER, heads=(11, 12),
        source_run_id=1, source_arm="dinov2-l14-reg@504/bf16",
        dataset_hash="3c9f", note=None, created_at=STAMP, activated_at=STAMP,
        n_heads=2, n_scored=9514)


class _FakeConn:
    """Not a SQL fake — the toolkit functions are monkeypatched wholesale."""


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    api_main.app.dependency_overrides[deps.get_db_conn] = _FakeConn
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_models_lists_versions_and_names_the_active_one(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tm, "list_models", lambda conn, *, limit: [
        _model("v2", tm.STATUS_CANDIDATE), _model("v1", tm.STATUS_ACTIVE)])
    body = client.get(f"{PREFIX}/models").json()["data"]
    assert body["active_version"] == "v1"
    assert [m["version"] for m in body["models"]] == ["v2", "v1"]
    first = body["models"][0]
    # The head set travels with the row: a winner is an argmax over exactly it.
    assert first["heads"] == [11, 12]
    assert first["n_heads"] == 2 and first["n_scored"] == 9514
    # The seven identity facts are flattened onto the model, as on a bake-off arm.
    for field in th.ENCODER_FIELDS:
        assert field in first


def test_models_finds_an_active_version_outside_the_page(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tm, "list_models",
                        lambda conn, *, limit: [_model("v9", tm.STATUS_CANDIDATE)])
    monkeypatch.setattr(tm, "active_model", lambda conn: _model("v1"))
    body = client.get(f"{PREFIX}/models?limit=1").json()["data"]
    assert body["active_version"] == "v1"


def test_models_reports_no_active_version_as_null(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tm, "list_models", lambda conn, *, limit: [])
    monkeypatch.setattr(tm, "active_model", lambda conn: None)
    body = client.get(f"{PREFIX}/models").json()["data"]
    assert body["active_version"] is None and body["models"] == []


def test_heads_omit_the_weights_and_carry_the_copied_metrics(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    head = tm.ModelHead(
        tag_id=19, label="kuchyně",
        artifact={"kind": th.ARTIFACT_KIND, "threshold": 0.5, "dimension": 1024,
                  "weights": [0.1] * 1024, "bias": -0.3},
        metrics={"cv": {"f1": 0.95, "graded_n": 1235}})
    monkeypatch.setattr(tm, "get_model", lambda conn, *, version: _model(version))
    monkeypatch.setattr(tm, "model_heads", lambda conn, *, model_id: [head])
    body = client.get(f"{PREFIX}/models/v1/heads").json()["data"]
    assert body["version"] == "v1"
    row = body["heads"][0]
    assert row["tag_id"] == 19 and row["tag_label"] == "kuchyně"
    assert row["threshold"] == 0.5 and row["metrics"]["cv"]["f1"] == 0.95
    assert "weights" not in row, "megabytes of floats no page can use"


def test_unknown_version_is_404(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tm, "get_model", lambda conn, *, version: None)
    assert client.get(f"{PREFIX}/models/v404/heads").status_code == 404


def test_image_returns_every_head_score_and_the_winner(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _winners(conn, *, image_ids, version=None):
        seen["image_ids"] = list(image_ids)
        seen["version"] = version
        return {84211: tm.Winner(
            image_id=84211, model_id=1, version="v1", winner_tag_id=12,
            winner_score=0.9713, scores={11: 0.0021, 12: 0.9713},
            scored_at=STAMP)}

    monkeypatch.setattr(tm, "winners", _winners)
    body = client.get(f"{PREFIX}/images/84211").json()["data"]
    assert seen == {"image_ids": [84211], "version": None}
    assert body["winner_tag_id"] == 12 and body["winner_score"] == 0.9713
    # Every head, and no boolean anywhere: the product has no per-head yes/no.
    assert body["scores"] == {"11": 0.0021, "12": 0.9713}
    assert "predicted" not in body and "threshold" not in body


def test_image_passes_an_explicit_version_through(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _winners(conn, *, image_ids, version=None):
        seen["version"] = version
        return {}

    monkeypatch.setattr(tm, "winners", _winners)
    assert client.get(f"{PREFIX}/images/1?version=v2").status_code == 404
    assert seen["version"] == "v2"


def test_an_unscored_image_is_404_not_an_empty_tag(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tm, "winners",
                        lambda conn, *, image_ids, version=None: {})
    resp = client.get(f"{PREFIX}/images/7")
    assert resp.status_code == 404
    assert "not been scored" in resp.json()["detail"]


def test_no_active_model_is_404_with_the_reason(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(conn, *, image_ids, version=None):
        raise tm.TagModelError("no tag model is active — promote and activate one first")

    monkeypatch.setattr(tm, "winners", _raise)
    resp = client.get(f"{PREFIX}/images/7")
    assert resp.status_code == 404
    assert "no tag model is active" in resp.json()["detail"]


def test_the_router_is_admin_gated() -> None:
    """No require_admin override here: an unauthenticated call must not reach the
    handler. SPA route-gating is not a security boundary."""
    api_main.app.dependency_overrides[deps.get_db_conn] = _FakeConn
    try:
        resp = TestClient(api_main.app).get(f"{PREFIX}/models")
        assert resp.status_code in (401, 403)
    finally:
        api_main.app.dependency_overrides.clear()
