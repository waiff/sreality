"""Tests for /new-dedup/labeling/* — Taxonomy v1 CRUD, sample management,
proposal review. Admin-gated (require_admin); happy-path tests override it."""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main


class _FakeConn:
    """Minimal stand-in exercising the same toolkit functions the routes
    call — not a SQL fake (see tests/toolkit/test_dedup_sim_labeling.py for
    that); here we monkeypatch the toolkit module itself so the route layer
    (status codes, error mapping) is what's under test."""


@pytest.fixture()
def fake_conn() -> _FakeConn:
    return _FakeConn()


@pytest.fixture()
def client(fake_conn: _FakeConn):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: fake_conn
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _patch_toolkit(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from toolkit import dedup_sim_labeling as dsl

    calls: dict[str, Any] = {}

    def _record(name):
        def _fn(conn, **kwargs):
            calls[name] = kwargs
            return _RESPONSES[name]
        return _fn

    _RESPONSES = {
        "taxonomy_overview": {"sample_size": 3, "labels": [{"id": 1, "label": "a"}]},
        "add_taxonomy_label": {"id": 1, "label": "a", "family": None, "active": True,
                                "created_at": "t"},
        "rename_taxonomy_label": {"id": 1, "label": "b", "family": None, "active": True,
                                   "created_at": "t"},
        "remove_taxonomy_label": {"label": "a", "deleted_training_examples": 2,
                                   "deleted_proposals": 1},
        "grow_sample": {"added": 10},
        "list_proposals": [{"image_id": 1, "model": "m", "label": "a", "confidence": 0.9,
                             "proposed_at": "t", "status": "pending", "reviewed_at": None,
                             "reviewed_by": None}],
        "confirm_proposal": {"image_id": 1, "model": "m", "label": "a", "status": "confirmed"},
        "dismiss_proposal": {"image_id": 1, "model": "m", "label": "a", "status": "dismissed"},
        "bulk_confirm_proposals": {"confirmed": 2, "model": "m", "image_ids": [1, 2]},
        "bulk_dismiss_proposals": {"dismissed": 2, "model": "m", "image_ids": [1, 2]},
    }

    monkeypatch.setattr(
        dsl, "taxonomy_overview",
        lambda conn: (calls.setdefault("taxonomy_overview", True), _RESPONSES["taxonomy_overview"])[1],
    )
    monkeypatch.setattr(dsl, "add_taxonomy_label", _record("add_taxonomy_label"))
    monkeypatch.setattr(dsl, "rename_taxonomy_label", _record("rename_taxonomy_label"))
    monkeypatch.setattr(dsl, "remove_taxonomy_label", _record("remove_taxonomy_label"))
    monkeypatch.setattr(dsl, "grow_sample", _record("grow_sample"))
    monkeypatch.setattr(
        dsl, "list_proposals",
        lambda conn, **kw: (calls.setdefault("list_proposals", kw), _RESPONSES["list_proposals"])[1],
    )
    monkeypatch.setattr(dsl, "confirm_proposal", _record("confirm_proposal"))
    monkeypatch.setattr(dsl, "dismiss_proposal", _record("dismiss_proposal"))
    monkeypatch.setattr(dsl, "bulk_confirm_proposals", _record("bulk_confirm_proposals"))
    monkeypatch.setattr(dsl, "bulk_dismiss_proposals", _record("bulk_dismiss_proposals"))
    return calls


def test_get_overview(client):
    res = client.get("/new-dedup/labeling/overview")
    assert res.status_code == 200
    assert res.json()["data"]["sample_size"] == 3


def test_post_taxonomy_label(client, _patch_toolkit):
    res = client.post("/new-dedup/labeling/taxonomy", json={"label": "interier - kuchyne"})
    assert res.status_code == 200
    assert res.json()["data"]["label"] == "a"  # fixture return value
    assert _patch_toolkit["add_taxonomy_label"]["label"] == "interier - kuchyne"


def test_post_taxonomy_label_duplicate_422s(client, monkeypatch):
    from toolkit import dedup_sim_labeling as dsl

    def _raise(conn, **kw):
        raise ValueError("taxonomy label 'a' already exists")

    monkeypatch.setattr(dsl, "add_taxonomy_label", _raise)
    res = client.post("/new-dedup/labeling/taxonomy", json={"label": "a"})
    assert res.status_code == 422


def test_put_taxonomy_label_rename(client, _patch_toolkit):
    res = client.put("/new-dedup/labeling/taxonomy/1", json={"label": "b"})
    assert res.status_code == 200
    assert _patch_toolkit["rename_taxonomy_label"] == {"label_id": 1, "new_label": "b"}


def test_put_taxonomy_label_unknown_404s(client, monkeypatch):
    from toolkit import dedup_sim_labeling as dsl

    def _raise(conn, **kw):
        raise KeyError(999)

    monkeypatch.setattr(dsl, "rename_taxonomy_label", _raise)
    res = client.put("/new-dedup/labeling/taxonomy/999", json={"label": "b"})
    assert res.status_code == 404


def test_put_taxonomy_label_duplicate_422s(client, monkeypatch):
    from toolkit import dedup_sim_labeling as dsl

    def _raise(conn, **kw):
        raise ValueError("taxonomy label 'b' already exists")

    monkeypatch.setattr(dsl, "rename_taxonomy_label", _raise)
    res = client.put("/new-dedup/labeling/taxonomy/1", json={"label": "b"})
    assert res.status_code == 422


def test_delete_taxonomy_label(client, _patch_toolkit):
    res = client.delete("/new-dedup/labeling/taxonomy/1")
    assert res.status_code == 200
    assert res.json()["data"]["deleted_training_examples"] == 2


def test_delete_taxonomy_label_unknown_404s(client, monkeypatch):
    from toolkit import dedup_sim_labeling as dsl

    def _raise(conn, **kw):
        raise KeyError(999)

    monkeypatch.setattr(dsl, "remove_taxonomy_label", _raise)
    res = client.delete("/new-dedup/labeling/taxonomy/999")
    assert res.status_code == 404


def test_post_grow_sample(client, _patch_toolkit):
    res = client.post("/new-dedup/labeling/sample/grow", json={"count": 50})
    assert res.status_code == 200
    assert res.json()["data"]["added"] == 10
    assert _patch_toolkit["grow_sample"] == {"count": 50, "category_main": None}


def test_post_grow_sample_rejects_bad_count(client, monkeypatch):
    from toolkit import dedup_sim_labeling as dsl

    def _raise(conn, **kw):
        raise ValueError("count must be at least 1")

    monkeypatch.setattr(dsl, "grow_sample", _raise)
    res = client.post("/new-dedup/labeling/sample/grow", json={"count": 0})
    assert res.status_code == 422


def test_get_proposals(client, _patch_toolkit):
    res = client.get("/new-dedup/labeling/proposals?status=pending")
    assert res.status_code == 200
    assert res.json()["data"][0]["image_id"] == 1
    assert _patch_toolkit["list_proposals"]["status"] == "pending"


def test_get_proposals_all(client, _patch_toolkit):
    res = client.get("/new-dedup/labeling/proposals?status=all")
    assert res.status_code == 200
    assert _patch_toolkit["list_proposals"]["status"] == "all"


def test_get_proposals_rejects_an_unknown_status(client, _patch_toolkit):
    # Silently ignoring it would list EVERY proposal while the tab claims to
    # be filtered — the failure mode is invisible, so make it loud.
    res = client.get("/new-dedup/labeling/proposals?status=pendign")
    assert res.status_code == 422
    assert "list_proposals" not in _patch_toolkit


def test_post_confirm_proposal(client, _patch_toolkit):
    res = client.post("/new-dedup/labeling/proposals/confirm", json={"image_id": 1, "model": "m"})
    assert res.status_code == 200
    assert res.json()["data"]["status"] == "confirmed"
    # An omitted label means "accept the suggestion as-is".
    assert _patch_toolkit["confirm_proposal"]["label"] is None


def test_post_confirm_proposal_passes_a_corrected_label_through(client, _patch_toolkit):
    res = client.post(
        "/new-dedup/labeling/proposals/confirm",
        json={"image_id": 1, "model": "m", "label": "interier - loznice"},
    )
    assert res.status_code == 200
    assert _patch_toolkit["confirm_proposal"]["label"] == "interier - loznice"


def test_post_confirm_proposal_bad_label_422s(client, monkeypatch):
    from toolkit import dedup_sim_labeling as dsl

    def _raise(conn, **kw):
        raise ValueError("a taxonomy label is at most 100 characters")

    monkeypatch.setattr(dsl, "confirm_proposal", _raise)
    res = client.post(
        "/new-dedup/labeling/proposals/confirm",
        json={"image_id": 1, "model": "m", "label": "x" * 101},
    )
    assert res.status_code == 422


def test_post_confirm_proposal_unknown_404s(client, monkeypatch):
    from toolkit import dedup_sim_labeling as dsl

    def _raise(conn, **kw):
        raise KeyError((1, "m"))

    monkeypatch.setattr(dsl, "confirm_proposal", _raise)
    res = client.post("/new-dedup/labeling/proposals/confirm", json={"image_id": 1, "model": "m"})
    assert res.status_code == 404


def test_post_dismiss_proposal(client, _patch_toolkit):
    res = client.post("/new-dedup/labeling/proposals/dismiss", json={"image_id": 1, "model": "m"})
    assert res.status_code == 200
    assert res.json()["data"]["status"] == "dismissed"


def test_post_dismiss_proposal_unknown_404s(client, monkeypatch):
    from toolkit import dedup_sim_labeling as dsl

    def _raise(conn, **kw):
        raise KeyError((1, "m"))

    monkeypatch.setattr(dsl, "dismiss_proposal", _raise)
    res = client.post("/new-dedup/labeling/proposals/dismiss", json={"image_id": 1, "model": "m"})
    assert res.status_code == 404


def test_post_bulk_confirm_proposals(client, _patch_toolkit):
    res = client.post(
        "/new-dedup/labeling/proposals/bulk-confirm",
        json={"model": "m", "image_ids": [1, 2]},
    )
    assert res.status_code == 200
    assert res.json()["data"]["confirmed"] == 2
    assert _patch_toolkit["bulk_confirm_proposals"] == {"model": "m", "image_ids": [1, 2]}


def test_post_bulk_confirm_proposals_rejects_empty(client, monkeypatch):
    from toolkit import dedup_sim_labeling as dsl

    def _raise(conn, **kw):
        raise ValueError("no proposals selected")

    monkeypatch.setattr(dsl, "bulk_confirm_proposals", _raise)
    res = client.post(
        "/new-dedup/labeling/proposals/bulk-confirm", json={"model": "m", "image_ids": []},
    )
    assert res.status_code == 422


def test_post_bulk_dismiss_proposals(client, _patch_toolkit):
    res = client.post(
        "/new-dedup/labeling/proposals/bulk-dismiss",
        json={"model": "m", "image_ids": [1, 2]},
    )
    assert res.status_code == 200
    assert res.json()["data"]["dismissed"] == 2


def test_post_bulk_dismiss_proposals_rejects_empty(client, monkeypatch):
    from toolkit import dedup_sim_labeling as dsl

    def _raise(conn, **kw):
        raise ValueError("no proposals selected")

    monkeypatch.setattr(dsl, "bulk_dismiss_proposals", _raise)
    res = client.post(
        "/new-dedup/labeling/proposals/bulk-dismiss", json={"model": "m", "image_ids": []},
    )
    assert res.status_code == 422


def test_new_dedup_labeling_requires_admin(client):
    api_main.app.dependency_overrides.pop(deps.require_admin, None)
    assert client.get("/new-dedup/labeling/overview").status_code == 401
