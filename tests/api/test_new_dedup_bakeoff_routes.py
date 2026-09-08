"""Tests for /new-dedup/tagging-bakeoff/* — the tagging bake-off's read surface.

Admin-gated (require_admin); the happy-path tests override it, and one test does
not, to prove the gate is actually on the router. The toolkit module is
monkeypatched, so what is under test here is the route layer: filters reaching
the reader intact, 404 on an unknown run, 422 on a filter combination that has no
meaning, and the response envelope the frontend codes against.
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from toolkit import tag_head_bakeoff as bo
from toolkit import tag_heads as th

PREFIX = "/new-dedup/tagging-bakeoff"


class _FakeConn:
    """Not a SQL fake — the toolkit functions are monkeypatched wholesale."""


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    api_main.app.dependency_overrides[deps.get_db_conn] = _FakeConn
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    monkeypatch.setattr(bo, "get_run", lambda conn, *, run_id: (
        {"id": run_id, "label": "set-1", "min_train_positives": 100}
        if run_id == 3 else None))
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


ARM = bo.Arm(
    id=7, run_id=3, arm="dinov3-b16@768/bf16", dim=768, status="ok", note=None,
    encoder=th.EncoderIdentity(
        model="facebook/dinov3-vitb16", revision="5931719e", library="transformers",
        pooling="cls", resolution=768, preprocessing="squash", dtype="bfloat16"),
)


def test_runs_carry_their_arms(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bo, "list_runs", lambda conn, *, limit: [
        {"id": 3, "created_at": "2026-09-08T12:00:00+00:00", "label": "set-1",
         "note": None, "status": "ok", "manifest_key": None,
         "heads": [11, 12], "min_train_positives": 100}])
    monkeypatch.setattr(bo, "list_arms", lambda conn, *, run_id: [ARM])

    body = client.get(f"{PREFIX}/runs").json()["data"]
    assert body[0]["heads"] == [11, 12]
    arm = body[0]["arms"][0]
    # The seven identity facts travel flattened onto the arm — the page renders
    # them beside the short name, so "which encoder was this" needs no join.
    assert arm["arm"] == "dinov3-b16@768/bf16" and arm["dim"] == 768
    for field in th.ENCODER_FIELDS:
        assert field in arm


def test_metrics_returns_the_whole_table_in_one_payload(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"arm_id": 7, "arm": "a", "mode": "pos_neg", "tag_id": 11,
             "tag_label": "kuchyně", "cv_precision": 0.94, "exam_precision": None,
             "exam_graded_n": 0, "status": "ok"}]
    monkeypatch.setattr(bo, "run_metrics", lambda conn, *, run_id: rows)
    body = client.get(f"{PREFIX}/runs/3/metrics").json()["data"]
    assert body == rows
    # A null rate is a null rate all the way to the wire: "nothing was proposed"
    # must not arrive at the page as 0.
    assert body[0]["exam_precision"] is None


def test_unknown_run_is_404_on_every_route(client) -> None:
    assert client.get(f"{PREFIX}/runs/99/metrics").status_code == 404
    assert client.get(f"{PREFIX}/runs/99/images").status_code == 404
    assert client.get(
        f"{PREFIX}/runs/99/buckets?arm_id=7&mode=pos_neg&tag_id=11").status_code == 404


def test_images_passes_every_filter_through(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(conn: Any, **kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"images": [], "next_after_image_id": None}

    monkeypatch.setattr(bo, "run_images", fake)
    resp = client.get(f"{PREFIX}/runs/3/images?split=exam&arms=7,8&mode=pos_neg"
                      "&tag_id=11&outcome=fp&after_image_id=41&limit=10")
    assert resp.status_code == 200
    assert seen == {"run_id": 3, "split": "exam", "arm_ids": [7, 8],
                    "mode": "pos_neg", "tag_id": 11, "outcome": "fp",
                    "after_image_id": 41, "limit": 10}


def test_images_rejects_a_bad_arm_list_and_an_unknown_split(client) -> None:
    assert client.get(f"{PREFIX}/runs/3/images?arms=seven").status_code == 422
    assert client.get(f"{PREFIX}/runs/3/images?split=holdout").status_code == 422
    assert client.get(f"{PREFIX}/runs/3/images?outcome=maybe").status_code == 422


def test_images_surfaces_the_readers_own_refusal_as_422(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(conn: Any, **kw: Any) -> dict[str, Any]:
        raise ValueError("outcome needs a tag_id — it is one head's verdict")

    monkeypatch.setattr(bo, "run_images", boom)
    resp = client.get(f"{PREFIX}/runs/3/images?outcome=fp")
    assert resp.status_code == 422 and "tag_id" in resp.json()["detail"]


def test_images_envelope_carries_the_storage_path_and_the_cursor(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bo, "run_images", lambda conn, **kw: {
        "images": [{"image_id": 41, "listing_id": 900, "storage_path": "a.jpg",
                    "scores": [{"arm_id": 7, "arm": "a", "mode": "pos_neg",
                                "tag_id": 11, "tag_label": "k", "split": "cv",
                                "fold": 2, "label": 1, "score": 0.9,
                                "predicted": True, "outcome": "tp"}]}],
        "next_after_image_id": 41})
    data = client.get(f"{PREFIX}/runs/3/images").json()["data"]
    assert data["images"][0]["storage_path"] == "a.jpg"
    assert data["next_after_image_id"] == 41


def test_buckets_passes_its_keys_through_and_needs_them(
        client, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(conn: Any, **kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {"buckets": {}, "histogram": {}}

    monkeypatch.setattr(bo, "run_buckets", fake)
    ok = client.get(f"{PREFIX}/runs/3/buckets?arm_id=7&mode=pos_neg&tag_id=11"
                    "&split=exam&limit=8&offset=16")
    assert ok.status_code == 200
    assert seen == {"arm_id": 7, "mode": "pos_neg", "tag_id": 11, "split": "exam",
                    "limit": 8, "offset": 16}
    # An outcome bucket is one head under one arm and mode; none of the three is
    # optional, so a call missing one is a 422, not a silent aggregate.
    assert client.get(f"{PREFIX}/runs/3/buckets?arm_id=7").status_code == 422


def test_every_bakeoff_route_is_admin_gated() -> None:
    # Recent FastAPI wraps an included router rather than splicing its routes
    # into app.routes, so the walk has to descend — the same shape
    # tests/api/test_admin_route_coverage.py::_collect uses.
    from tests.api.test_admin_route_coverage import _collect

    routes = [r for r in _collect(api_main.app.routes)
              if r.path.startswith(PREFIX)]
    assert len(routes) == 4, [r.path for r in routes]
    for route in routes:
        names = {getattr(d.call, "__name__", "") for d in route.dependant.dependencies}
        nested = {getattr(sub.call, "__name__", "")
                  for d in route.dependant.dependencies for sub in d.dependencies}
        assert "require_admin" in (names | nested), route.path


def test_the_gate_actually_rejects_without_an_admin_session() -> None:
    api_main.app.dependency_overrides[deps.get_db_conn] = _FakeConn
    try:
        resp = TestClient(api_main.app).get(f"{PREFIX}/runs")
        assert resp.status_code in (401, 403)
    finally:
        api_main.app.dependency_overrides.clear()
