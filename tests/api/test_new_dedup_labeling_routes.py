"""Tests for /new-dedup/labeling/* — tag taxonomy CRUD, sample management,
proposal review, and the tri-state annotation matrix. Admin-gated
(require_admin); happy-path tests override it."""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from toolkit import dedup_sim_labeling as dsl
from toolkit import tag_annotations as ta
from toolkit import tag_definitions as td


class _FakeConn:
    """Minimal stand-in exercising the same toolkit functions the routes
    call — not a SQL fake (see tests/toolkit/test_tag_annotations.py and
    tests/toolkit/test_dedup_sim_labeling.py for that); here we monkeypatch the
    toolkit modules themselves so the route layer (status codes, argument
    plumbing, error mapping) is what's under test."""


@pytest.fixture()
def fake_conn() -> _FakeConn:
    return _FakeConn()


@pytest.fixture()
def client(fake_conn: _FakeConn):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: fake_conn
    api_main.app.dependency_overrides[deps.require_admin] = lambda: {"is_admin": True}
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


_RESPONSES: dict[str, Any] = {
    "tag_overview": {
        "sample_size": 3, "ambiguity_threshold": 0.15, "ambiguity_min_decisions": 20,
        "tags": [{"id": 1, "label": "a", "human_count": 2, "machine_count": 0,
                  "backfill_count": 41, "ambiguous_count": 1, "pruned_count": 0,
                  "decided_count": 3, "ambiguity_rate": 0.3333, "ambiguity_alert": True}],
    },
    "add_tag": {"id": 1, "label": "a", "family": None, "active": True, "created_at": "t"},
    "rename_tag": {"id": 1, "label": "b", "family": None, "active": True, "created_at": "t"},
    "remove_tag": {"label": "a", "deleted_annotations": 2},
    "set_tag_flags": {"id": 1, "label": "a", "family": None, "active": True,
                      "priority": True, "ready_for_training": False, "created_at": "t"},
    "list_images_for_tag": [
        {"image_id": 1, "storage_path": "img/1.jpg", "state": "untouched",
         "updated_at": None, "created_by": None, "source": None,
         "excluded_reason": None},
    ],
    "list_tags_for_image": [
        {"id": 1, "label": "a", "family": None, "state": "positive", "updated_at": "t",
         "source": "human", "excluded_reason": None},
    ],
    "list_positive_tags_for_images": [
        {"image_id": 1, "tag_id": 2, "label": "a"},
    ],
    "set_state": {"image_id": 1, "tag_id": 2, "state": "positive", "source": "human",
                  "excluded_reason": None, "definition_id": 9, "verified_at": "t",
                  "updated_at": "t", "applied": True},
    "bulk_set_state": {"updated": 2, "tag_id": 2, "state": "negative", "source": "human",
                       "excluded_reason": None, "image_ids": [1, 2]},
    "bulk_set_state_for_image": {"updated": 2, "image_id": 1, "state": "negative",
                                 "source": "human", "excluded_reason": None,
                                 "tag_ids": [2, 3]},
    "clear_state": {"image_id": 1, "tag_id": 2, "deleted": True},
    "grow_sample": {"added": 10},
    "list_proposals": [
        {"image_id": 1, "model": "m", "label": "a", "confidence": 0.9, "proposed_at": "t",
         "status": "pending", "reviewed_at": None, "reviewed_by": None,
         "current_state": None, "current_excluded_reason": None},
    ],
    "set_proposal_state": {"image_id": 1, "model": "m", "label": "a", "state": "positive",
                           "status": "confirmed", "proposed_label": "a", "corrected": False,
                           "excluded_reason": None},
    "bulk_set_proposal_state": {"updated": 2, "model": "m", "state": "negative",
                                "excluded_reason": None, "image_ids": [1, 2]},
    "list_definition_status": [
        {"tag_id": 1, "definition_id": 9, "version": 2, "means": "A kitchen.",
         "created_at": "t"},
    ],
    "get_active_definition": {
        "id": 9, "tag_id": 1, "version": 2, "means": "A kitchen.",
        "counts": ["a galley kitchen"],
        "does_not_count": [{"case": "a kitchenette", "goes_to_tag_id": 3}],
        "confusable_with": [{"tag_id": 2, "tell": "no worktop"}],
        "leave_out_when": None, "example_image_ids": [11],
        "status": "active", "created_at": "t", "created_by": "operator",
        "referenced_tags": [{"tag_id": 2, "label": "b"}],
    },
    "save_definition": {
        "id": 10, "tag_id": 1, "version": 3, "means": "A kitchen.",
        "counts": [], "does_not_count": [], "confusable_with": [],
        "leave_out_when": None, "example_image_ids": [],
        "status": "active", "created_at": "t", "created_by": "operator",
        "referenced_tags": [],
    },
    "list_definition_versions": [
        {"id": 9, "version": 2, "status": "active", "means": "A kitchen.",
         "created_at": "t", "created_by": "operator"},
    ],
    "get_definition_version": {
        "id": 8, "tag_id": 1, "version": 1, "means": "First take.",
        "counts": [], "does_not_count": [], "confusable_with": [],
        "leave_out_when": None, "example_image_ids": [],
        "status": "superseded", "created_at": "t", "created_by": "operator",
        "referenced_tags": [],
    },
    "list_positive_images": [
        {"image_id": 11, "storage_path": "img/11.jpg",
         "sreality_url": "https://cdn/11.jpg", "updated_at": "t"},
    ],
    "nearest_tags": [
        {"tag_id": 2, "label": "b", "family": None,
         "embedded_positive_count": 31, "cosine_distance": 0.0412},
    ],
}

_PATCHED = {
    ta: ["add_tag", "rename_tag", "remove_tag", "set_tag_flags", "list_images_for_tag",
         "set_state", "bulk_set_state", "bulk_set_state_for_image", "clear_state",
         "list_tags_for_image",
         "list_positive_tags_for_images"],
    dsl: ["grow_sample", "list_proposals", "set_proposal_state", "bulk_set_proposal_state"],
    # list_definition_status takes the connection only; _record handles that fine
    # (it records {}), so it needs no special case.
    td: ["list_definition_status", "get_active_definition", "save_definition",
         "list_definition_versions", "get_definition_version", "list_positive_images",
         "nearest_tags"],
}


@pytest.fixture(autouse=True)
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Every toolkit entry point the routes call, replaced by a recorder that
    returns a canned payload. The dict maps name -> the kwargs it was handed."""
    recorded: dict[str, Any] = {}

    def _record(name: str):
        def _fn(conn: Any, **kwargs: Any) -> Any:
            recorded[name] = kwargs
            return _RESPONSES[name]
        return _fn

    for module, names in _PATCHED.items():
        for name in names:
            monkeypatch.setattr(module, name, _record(name))
    # tag_overview takes the connection only — no kwargs to record.
    monkeypatch.setattr(
        ta, "tag_overview",
        lambda conn: (recorded.setdefault("tag_overview", {}), _RESPONSES["tag_overview"])[1],
    )
    return recorded


def _raises(exc: Exception):
    def _fn(conn: Any, **kwargs: Any) -> Any:
        raise exc
    return _fn


# --- overview ---------------------------------------------------------------


def test_get_overview(client, calls):
    res = client.get("/new-dedup/labeling/overview")
    assert res.status_code == 200
    body = res.json()["data"]
    assert body["sample_size"] == 3
    assert body["tags"][0]["label"] == "a"


# --- taxonomy ---------------------------------------------------------------


def test_post_tag(client, calls):
    res = client.post(
        "/new-dedup/labeling/taxonomy",
        json={"label": "interier - kuchyne", "family": "interier"},
    )
    assert res.status_code == 200
    assert res.json()["data"]["label"] == "a"  # fixture return value
    assert calls["add_tag"] == {"label": "interier - kuchyne", "family": "interier"}


def test_post_tag_family_is_optional(client, calls):
    assert client.post("/new-dedup/labeling/taxonomy", json={"label": "a"}).status_code == 200
    assert calls["add_tag"]["family"] is None


def test_post_tag_duplicate_422s(client, monkeypatch):
    monkeypatch.setattr(ta, "add_tag", _raises(ValueError("tag 'a' already exists")))
    assert client.post("/new-dedup/labeling/taxonomy", json={"label": "a"}).status_code == 422


def test_put_tag_rename(client, calls):
    res = client.put("/new-dedup/labeling/taxonomy/1", json={"label": "b"})
    assert res.status_code == 200
    assert calls["rename_tag"] == {"tag_id": 1, "new_label": "b"}


def test_put_tag_unknown_404s(client, monkeypatch):
    monkeypatch.setattr(ta, "rename_tag", _raises(KeyError(999)))
    assert client.put("/new-dedup/labeling/taxonomy/999", json={"label": "b"}).status_code == 404


def test_put_tag_duplicate_422s(client, monkeypatch):
    monkeypatch.setattr(ta, "rename_tag", _raises(ValueError("tag 'b' already exists")))
    assert client.put("/new-dedup/labeling/taxonomy/1", json={"label": "b"}).status_code == 422


def test_delete_tag(client, calls):
    res = client.delete("/new-dedup/labeling/taxonomy/1")
    assert res.status_code == 200
    assert res.json()["data"]["deleted_annotations"] == 2
    assert calls["remove_tag"] == {"tag_id": 1}


def test_patch_tag_flags_priority_only(client, calls):
    res = client.patch("/new-dedup/labeling/taxonomy/1/flags", json={"priority": True})
    assert res.status_code == 200
    assert res.json()["data"]["priority"] is True
    assert calls["set_tag_flags"] == {"tag_id": 1, "priority": True, "ready_for_training": None}


def test_patch_tag_flags_both(client, calls):
    res = client.patch(
        "/new-dedup/labeling/taxonomy/1/flags",
        json={"priority": True, "ready_for_training": True},
    )
    assert res.status_code == 200
    assert calls["set_tag_flags"] == {"tag_id": 1, "priority": True, "ready_for_training": True}


def test_patch_tag_flags_unknown_404s(client, monkeypatch):
    monkeypatch.setattr(ta, "set_tag_flags", _raises(KeyError(999)))
    res = client.patch("/new-dedup/labeling/taxonomy/999/flags", json={"priority": True})
    assert res.status_code == 404


def test_patch_tag_flags_no_op_422s(client, monkeypatch):
    monkeypatch.setattr(ta, "set_tag_flags", _raises(ValueError("nothing to update")))
    res = client.patch("/new-dedup/labeling/taxonomy/1/flags", json={})
    assert res.status_code == 422


def test_delete_tag_unknown_404s(client, monkeypatch):
    monkeypatch.setattr(ta, "remove_tag", _raises(KeyError(999)))
    assert client.delete("/new-dedup/labeling/taxonomy/999").status_code == 404


def test_taxonomy_routes_key_on_a_numeric_tag_id(client):
    # tag_id is the surrogate key (migration 442), not label text — a non-numeric
    # path segment must be a 422, never a lookup by name.
    assert client.delete("/new-dedup/labeling/taxonomy/kuchyne").status_code == 422


# --- sample -----------------------------------------------------------------


def test_post_grow_sample(client, calls):
    res = client.post("/new-dedup/labeling/sample/grow", json={"count": 50})
    assert res.status_code == 200
    assert res.json()["data"]["added"] == 10
    assert calls["grow_sample"] == {"count": 50, "category_main": None}


def test_post_grow_sample_rejects_bad_count(client, monkeypatch):
    monkeypatch.setattr(dsl, "grow_sample", _raises(ValueError("count must be at least 1")))
    assert client.post(
        "/new-dedup/labeling/sample/grow", json={"count": 0},
    ).status_code == 422


# --- proposals --------------------------------------------------------------


def test_get_proposals(client, calls):
    res = client.get("/new-dedup/labeling/proposals?status=pending")
    assert res.status_code == 200
    assert res.json()["data"][0]["image_id"] == 1
    assert calls["list_proposals"]["status"] == "pending"


def test_get_proposals_all(client, calls):
    assert client.get("/new-dedup/labeling/proposals?status=all").status_code == 200
    assert calls["list_proposals"]["status"] == "all"


def test_get_proposals_filters_by_original_tag(client, calls):
    # A real, deterministic value from list_original_tags — not mocked, since
    # it has no DB dependency (see toolkit test coverage for the vocabulary
    # itself).
    res = client.get("/new-dedup/labeling/proposals?original_tag=kitchen")
    assert res.status_code == 200
    assert calls["list_proposals"]["original_tag"] == "kitchen"


def test_get_proposals_rejects_an_unknown_original_tag(client, calls):
    res = client.get("/new-dedup/labeling/proposals?original_tag=not-a-real-tag")
    assert res.status_code == 422
    assert "list_proposals" not in calls


def test_get_original_tags(client):
    res = client.get("/new-dedup/labeling/original-tags")
    assert res.status_code == 200
    data = res.json()["data"]
    assert "kitchen" in data
    assert "bathroom" in data


def test_get_original_tags_requires_admin(client):
    api_main.app.dependency_overrides.pop(deps.require_admin, None)
    assert client.get("/new-dedup/labeling/original-tags").status_code == 401


def test_get_proposals_rejects_an_unknown_status(client, calls):
    # Silently ignoring it would list EVERY proposal while the tab claims to
    # be filtered — the failure mode is invisible, so make it loud.
    assert client.get("/new-dedup/labeling/proposals?status=pendign").status_code == 422
    assert "list_proposals" not in calls


def test_post_proposal_state(client, calls):
    res = client.post(
        "/new-dedup/labeling/proposals/state",
        json={"image_id": 1, "model": "m", "state": "positive"},
    )
    assert res.status_code == 200
    assert res.json()["data"]["status"] == "confirmed"
    # An omitted label means "decide against the suggestion as-is".
    assert calls["set_proposal_state"] == {
        "image_id": 1, "model": "m", "state": "positive", "label": None,
        "excluded_reason": None,
    }


def test_post_proposal_state_passes_a_corrected_label_through(client, calls):
    res = client.post(
        "/new-dedup/labeling/proposals/state",
        json={"image_id": 1, "model": "m", "state": "positive",
              "label": "interier - loznice"},
    )
    assert res.status_code == 200
    assert calls["set_proposal_state"]["label"] == "interier - loznice"


@pytest.mark.parametrize("state", ["positive", "negative", "excluded"])
def test_post_proposal_state_accepts_every_tri_state_value(client, calls, state):
    res = client.post(
        "/new-dedup/labeling/proposals/state",
        json={"image_id": 1, "model": "m", "state": state},
    )
    assert res.status_code == 200
    assert calls["set_proposal_state"]["state"] == state


def test_post_proposal_state_rejects_an_unknown_state(client, calls):
    res = client.post(
        "/new-dedup/labeling/proposals/state",
        json={"image_id": 1, "model": "m", "state": "confirmed"},
    )
    assert res.status_code == 422
    # Rejected at the route boundary, before any write is attempted.
    assert "set_proposal_state" not in calls


def test_post_proposal_state_bad_label_422s(client, monkeypatch):
    monkeypatch.setattr(
        dsl, "set_proposal_state", _raises(ValueError("a tag label is at most 100 characters")),
    )
    res = client.post(
        "/new-dedup/labeling/proposals/state",
        json={"image_id": 1, "model": "m", "state": "positive", "label": "x" * 101},
    )
    assert res.status_code == 422


def test_post_proposal_state_unknown_404s(client, monkeypatch):
    monkeypatch.setattr(dsl, "set_proposal_state", _raises(KeyError((1, "m"))))
    res = client.post(
        "/new-dedup/labeling/proposals/state",
        json={"image_id": 1, "model": "m", "state": "positive"},
    )
    assert res.status_code == 404


def test_post_bulk_proposal_state(client, calls):
    res = client.post(
        "/new-dedup/labeling/proposals/bulk-state",
        json={"model": "m", "image_ids": [1, 2], "state": "negative"},
    )
    assert res.status_code == 200
    assert res.json()["data"]["updated"] == 2
    assert calls["bulk_set_proposal_state"] == {
        "model": "m", "image_ids": [1, 2], "state": "negative",
        "excluded_reason": None,
    }


def test_post_bulk_proposal_state_rejects_an_unknown_state(client, calls):
    res = client.post(
        "/new-dedup/labeling/proposals/bulk-state",
        json={"model": "m", "image_ids": [1], "state": "dismissed"},
    )
    assert res.status_code == 422
    assert "bulk_set_proposal_state" not in calls


def test_post_bulk_proposal_state_rejects_empty(client, monkeypatch):
    monkeypatch.setattr(
        dsl, "bulk_set_proposal_state", _raises(ValueError("no proposals selected")),
    )
    res = client.post(
        "/new-dedup/labeling/proposals/bulk-state",
        json={"model": "m", "image_ids": [], "state": "positive"},
    )
    assert res.status_code == 422


# --- the annotation matrix --------------------------------------------------


def test_get_images_for_tag(client, calls):
    res = client.get("/new-dedup/labeling/tags/2/images?state=excluded&limit=25")
    assert res.status_code == 200
    assert res.json()["data"][0]["image_id"] == 1
    assert calls["list_images_for_tag"] == {"tag_id": 2, "state": "excluded", "limit": 25}


def test_get_images_for_tag_state_is_optional(client, calls):
    assert client.get("/new-dedup/labeling/tags/2/images").status_code == 200
    assert calls["list_images_for_tag"] == {"tag_id": 2, "state": None, "limit": 100}


def test_get_images_for_tag_bad_state_422s(client, monkeypatch):
    # 'untouched' is a valid filter here but not a storable state, so the
    # validation lives in the toolkit, not in the route's _check_state.
    monkeypatch.setattr(
        ta, "list_images_for_tag", _raises(ValueError("state must be one of ...")),
    )
    assert client.get("/new-dedup/labeling/tags/2/images?state=maybe").status_code == 422


def test_post_annotation(client, calls):
    res = client.post(
        "/new-dedup/labeling/tags/2/annotations",
        json={"image_id": 1, "state": "positive"},
    )
    assert res.status_code == 200
    assert res.json()["data"]["state"] == "positive"
    assert calls["set_state"] == {
        "image_id": 1, "tag_id": 2, "state": "positive", "excluded_reason": None,
    }


def test_post_annotation_rejects_an_unknown_state(client, calls):
    res = client.post(
        "/new-dedup/labeling/tags/2/annotations",
        json={"image_id": 1, "state": "untouched"},
    )
    assert res.status_code == 422
    # "untouched" is the ABSENCE of a row — it is cleared, never written.
    assert "set_state" not in calls


def test_post_bulk_annotation(client, calls):
    res = client.post(
        "/new-dedup/labeling/tags/2/annotations/bulk",
        json={"image_ids": [1, 2], "state": "negative"},
    )
    assert res.status_code == 200
    assert res.json()["data"]["updated"] == 2
    assert calls["bulk_set_state"] == {
        "image_ids": [1, 2], "tag_id": 2, "state": "negative", "excluded_reason": None,
    }


def test_post_bulk_annotation_rejects_an_unknown_state(client, calls):
    res = client.post(
        "/new-dedup/labeling/tags/2/annotations/bulk",
        json={"image_ids": [1], "state": "maybe"},
    )
    assert res.status_code == 422
    assert "bulk_set_state" not in calls


def test_post_bulk_annotation_over_max_422s(client, monkeypatch):
    monkeypatch.setattr(
        ta, "bulk_set_state", _raises(ValueError("at most 200 images per batch")),
    )
    res = client.post(
        "/new-dedup/labeling/tags/2/annotations/bulk",
        json={"image_ids": [1], "state": "positive"},
    )
    assert res.status_code == 422


def test_delete_annotation(client, calls):
    res = client.delete("/new-dedup/labeling/tags/2/annotations/1")
    assert res.status_code == 200
    assert res.json()["data"]["deleted"] is True
    assert calls["clear_state"] == {"image_id": 1, "tag_id": 2}


def test_get_tags_for_image(client, calls):
    res = client.get("/new-dedup/labeling/images/1/tags")
    assert res.status_code == 200
    assert res.json()["data"][0]["label"] == "a"
    assert calls["list_tags_for_image"] == {"image_id": 1}


def test_post_bulk_set_image_tags(client, calls):
    res = client.post(
        "/new-dedup/labeling/images/1/tags/bulk",
        json={"tag_ids": [2, 3], "state": "negative"},
    )
    assert res.status_code == 200
    assert res.json()["data"]["updated"] == 2
    assert calls["bulk_set_state_for_image"] == {
        "image_id": 1, "tag_ids": [2, 3], "state": "negative", "excluded_reason": None,
    }


def test_post_bulk_set_image_tags_rejects_an_unknown_state(client, calls):
    res = client.post(
        "/new-dedup/labeling/images/1/tags/bulk",
        json={"tag_ids": [2], "state": "maybe"},
    )
    assert res.status_code == 422
    assert "bulk_set_state_for_image" not in calls


def test_post_bulk_set_image_tags_over_max_422s(client, monkeypatch):
    monkeypatch.setattr(
        ta, "bulk_set_state_for_image", _raises(ValueError("at most 200 tags per batch")),
    )
    res = client.post(
        "/new-dedup/labeling/images/1/tags/bulk",
        json={"tag_ids": [2], "state": "positive"},
    )
    assert res.status_code == 422


def test_post_positive_tags_for_images(client, calls):
    res = client.post(
        "/new-dedup/labeling/images/tags/batch", json={"image_ids": [1, 2]},
    )
    assert res.status_code == 200
    assert res.json()["data"][0] == {"image_id": 1, "tag_id": 2, "label": "a"}
    assert calls["list_positive_tags_for_images"] == {"image_ids": [1, 2]}


def test_post_positive_tags_for_images_over_max_422s(client, monkeypatch):
    monkeypatch.setattr(
        ta, "list_positive_tags_for_images",
        _raises(ValueError("at most 200 images per batch")),
    )
    res = client.post(
        "/new-dedup/labeling/images/tags/batch", json={"image_ids": [1]},
    )
    assert res.status_code == 422


# --- tag definitions (migration 446) ----------------------------------------


_SAVE_BODY = {
    "means": "A kitchen inside a flat.",
    "counts": ["a galley kitchen"],
    "does_not_count": [{"case": "a kitchenette", "goes_to_tag_id": 3}],
    "confusable_with": [{"tag_id": 2, "tell": "no worktop = living room"}],
    "leave_out_when": None,
    "example_image_ids": [11],
    "base_version": 2,
}


def test_get_definition_status(client, calls):
    res = client.get("/new-dedup/labeling/definitions")
    assert res.status_code == 200
    assert res.json()["data"][0]["version"] == 2
    # Connection-only call — nothing to plumb, but it must have been reached.
    assert calls["list_definition_status"] == {}


def test_get_tag_definition(client, calls):
    res = client.get("/new-dedup/labeling/tags/1/definition")
    assert res.status_code == 200
    assert res.json()["data"]["means"] == "A kitchen."
    assert calls["get_active_definition"] == {"tag_id": 1}


def test_get_tag_definition_is_a_200_with_a_null_body_when_undefined(client, monkeypatch):
    # A known tag with no definition yet is NOT a 404 — the editor opens empty.
    monkeypatch.setattr(td, "get_active_definition", lambda conn, **kw: None)
    res = client.get("/new-dedup/labeling/tags/1/definition")
    assert res.status_code == 200
    assert res.json()["data"] is None


def test_get_tag_definition_unknown_tag_404s(client, monkeypatch):
    monkeypatch.setattr(td, "get_active_definition", _raises(KeyError(999)))
    assert client.get("/new-dedup/labeling/tags/999/definition").status_code == 404


def test_put_tag_definition(client, calls):
    res = client.put("/new-dedup/labeling/tags/1/definition", json=_SAVE_BODY)
    assert res.status_code == 200  # a new version is a 200, never a 201
    assert res.json()["data"]["version"] == 3
    assert calls["save_definition"]["tag_id"] == 1
    assert calls["save_definition"]["means"] == "A kitchen inside a flat."
    assert calls["save_definition"]["example_image_ids"] == [11]
    # The version the editor was written against — the lost-update guard.
    assert calls["save_definition"]["base_version"] == 2


def test_put_tag_definition_plumbs_nested_models_through_as_plain_dicts(client, calls):
    # The toolkit validates dicts (unknown-key rejection, id coercion); handing it
    # pydantic objects would make `isinstance(item, dict)` false and every entry a
    # 422. model_dump() is load-bearing, not cosmetic.
    client.put("/new-dedup/labeling/tags/1/definition", json=_SAVE_BODY)
    saved = calls["save_definition"]
    assert saved["does_not_count"] == [{"case": "a kitchenette", "goes_to_tag_id": 3}]
    assert saved["confusable_with"] == [{"tag_id": 2, "tell": "no worktop = living room"}]
    assert all(type(i) is dict for i in saved["does_not_count"] + saved["confusable_with"])


def test_put_tag_definition_defaults_every_optional_field(client, calls):
    res = client.put("/new-dedup/labeling/tags/1/definition", json={"means": "A kitchen."})
    assert res.status_code == 200
    saved = calls["save_definition"]
    assert saved["counts"] == []
    assert saved["does_not_count"] == []
    assert saved["confusable_with"] == []
    assert saved["example_image_ids"] == []
    assert saved["leave_out_when"] is None
    # No base_version means "I loaded a tag with no definition" — an assertion
    # the toolkit checks, never "don't check".
    assert saved["base_version"] is None


def test_put_tag_definition_unknown_tag_404s(client, monkeypatch):
    monkeypatch.setattr(td, "save_definition", _raises(KeyError(999)))
    res = client.put("/new-dedup/labeling/tags/999/definition", json={"means": "x"})
    assert res.status_code == 404


def test_put_tag_definition_rejected_input_422s(client, monkeypatch):
    monkeypatch.setattr(
        td, "save_definition", _raises(ValueError("a tag cannot be confusable with itself")),
    )
    res = client.put("/new-dedup/labeling/tags/1/definition", json={"means": "x"})
    assert res.status_code == 422


def test_put_tag_definition_concurrent_save_422s(client, monkeypatch):
    # A stale base_version (or the unique index losing a race) is an input problem
    # for the operator ("reload and save again"), not a 409 — this file's
    # vocabulary is 404/422.
    monkeypatch.setattr(
        td, "save_definition",
        _raises(ValueError("this tag's definition changed in another tab")),
    )
    res = client.put("/new-dedup/labeling/tags/1/definition", json={"means": "x"})
    assert res.status_code == 422
    assert "another tab" in res.json()["detail"]


def test_put_tag_definition_requires_a_means(client, calls):
    res = client.put("/new-dedup/labeling/tags/1/definition", json={"counts": []})
    assert res.status_code == 422  # pydantic's own validation
    assert "save_definition" not in calls


def test_get_tag_definition_versions(client, calls):
    res = client.get("/new-dedup/labeling/tags/1/definition/versions")
    assert res.status_code == 200
    assert res.json()["data"][0]["status"] == "active"
    assert calls["list_definition_versions"] == {"tag_id": 1}


def test_get_tag_definition_versions_unknown_tag_404s(client, monkeypatch):
    monkeypatch.setattr(td, "list_definition_versions", _raises(KeyError(999)))
    assert client.get(
        "/new-dedup/labeling/tags/999/definition/versions",
    ).status_code == 404


def test_get_tag_definition_version(client, calls):
    res = client.get("/new-dedup/labeling/tags/1/definition/versions/1")
    assert res.status_code == 200
    assert res.json()["data"]["status"] == "superseded"
    assert calls["get_definition_version"] == {"tag_id": 1, "version": 1}


def test_get_tag_definition_version_missing_404s(client, monkeypatch):
    monkeypatch.setattr(td, "get_definition_version", _raises(KeyError((1, 7))))
    res = client.get("/new-dedup/labeling/tags/1/definition/versions/7")
    assert res.status_code == 404
    assert "v7" in res.json()["detail"]


def test_definition_version_route_does_not_shadow_the_definition_route(client, calls):
    # /tags/{id}/definition and /tags/{id}/definition/versions/{v} are distinct
    # paths; a regression that collapsed them would silently serve the wrong doc.
    client.get("/new-dedup/labeling/tags/1/definition")
    client.get("/new-dedup/labeling/tags/1/definition/versions/1")
    assert "get_active_definition" in calls and "get_definition_version" in calls


def test_get_positive_images(client, calls):
    res = client.get("/new-dedup/labeling/tags/1/positive-images?limit=50")
    assert res.status_code == 200
    assert res.json()["data"][0]["image_id"] == 11
    assert calls["list_positive_images"] == {"tag_id": 1, "limit": 50}


def test_get_positive_images_defaults_its_limit(client, calls):
    assert client.get("/new-dedup/labeling/tags/1/positive-images").status_code == 200
    assert calls["list_positive_images"] == {"tag_id": 1, "limit": 200}


def test_get_tag_neighbours(client, calls):
    res = client.get("/new-dedup/labeling/tags/1/neighbours?limit=3")
    assert res.status_code == 200
    assert res.json()["data"][0]["cosine_distance"] == 0.0412
    assert calls["nearest_tags"] == {"tag_id": 1, "limit": 3}


def test_get_tag_neighbours_empty_is_a_200_not_an_error(client, monkeypatch):
    # Too few embedded positives to have a centroid degrades to [], never a 4xx.
    monkeypatch.setattr(td, "nearest_tags", lambda conn, **kw: [])
    res = client.get("/new-dedup/labeling/tags/1/neighbours")
    assert res.status_code == 200
    assert res.json()["data"] == []


# --- provenance plumbing (migration 446) ------------------------------------
#
# `source` is deliberately NOT a request field on ANY model in this router: every
# write from an admin-gated, human-driven UI is a human decision, and a browser
# that could name its own provenance could corrupt the record migration 446
# exists to protect. The tests below assert the reason plumbing and the 422s; the
# absence of `source` is asserted directly.


_REASON_ROUTES = [
    ("/new-dedup/labeling/proposals/state",
     {"image_id": 1, "model": "m"}, "set_proposal_state"),
    ("/new-dedup/labeling/proposals/bulk-state",
     {"model": "m", "image_ids": [1]}, "bulk_set_proposal_state"),
    ("/new-dedup/labeling/tags/2/annotations",
     {"image_id": 1}, "set_state"),
    ("/new-dedup/labeling/tags/2/annotations/bulk",
     {"image_ids": [1]}, "bulk_set_state"),
    ("/new-dedup/labeling/images/1/tags/bulk",
     {"tag_ids": [2]}, "bulk_set_state_for_image"),
]


@pytest.mark.parametrize("path,body,call", _REASON_ROUTES)
def test_every_write_route_plumbs_the_exclusion_reason_through(client, calls, path, body, call):
    res = client.post(path, json={**body, "state": "excluded", "excluded_reason": "pruned"})
    assert res.status_code == 200
    assert calls[call]["excluded_reason"] == "pruned"


@pytest.mark.parametrize("path,body,call", _REASON_ROUTES)
def test_an_unknown_exclusion_reason_is_a_422_before_any_write(client, calls, path, body, call):
    res = client.post(path, json={**body, "state": "excluded", "excluded_reason": "dunno"})
    assert res.status_code == 422
    assert call not in calls


@pytest.mark.parametrize("path,body,call", _REASON_ROUTES)
def test_a_reason_on_a_non_excluded_state_is_a_422(client, calls, path, body, call):
    # 'ambiguous' and 'pruned' mean nothing on a positive row, and the DB CHECK
    # forbids the combination outright — so it is caught loudly at the edge (a
    # frontend bug) rather than silently normalised into a lie.
    res = client.post(path, json={**body, "state": "positive", "excluded_reason": "ambiguous"})
    assert res.status_code == 422
    assert "only valid with state='excluded'" in res.json()["detail"]
    assert call not in calls


@pytest.mark.parametrize("path,body,call", _REASON_ROUTES)
def test_omitting_the_reason_sends_null_not_a_guess(client, calls, path, body, call):
    # The server never picks a reason on the operator's behalf; the client says
    # which one it means (the UI defaults ⊘ to ambiguous).
    res = client.post(path, json={**body, "state": "excluded"})
    assert res.status_code == 200
    assert calls[call]["excluded_reason"] is None


@pytest.mark.parametrize("path,body,call", _REASON_ROUTES)
def test_no_write_route_lets_the_client_name_its_own_source(client, calls, path, body, call):
    res = client.post(path, json={**body, "state": "positive", "source": "human_confirmed"})
    assert res.status_code == 200
    # pydantic ignores the unknown field; what matters is that it never reaches
    # the toolkit, whose default (or dedup_sim_labeling's derivation) decides.
    assert "source" not in calls[call]


def test_post_annotation_maps_a_rejected_vocabulary_to_a_422(client, monkeypatch):
    # This was the one write route in the file with no ValueError handler, and
    # set_state can now raise for a bad source/reason — a 500 would be wrong.
    monkeypatch.setattr(ta, "set_state", _raises(ValueError("source must be one of ...")))
    res = client.post(
        "/new-dedup/labeling/tags/2/annotations",
        json={"image_id": 1, "state": "positive"},
    )
    assert res.status_code == 422


def test_the_overview_carries_the_ambiguity_signal_and_its_threshold(client, calls):
    # The route is unchanged — the new fields ride inside tag_overview's dict, so
    # the SPA renders "above 15 percent" without a second hardcoded copy.
    res = client.get("/new-dedup/labeling/overview")
    assert res.status_code == 200
    body = res.json()["data"]
    assert body["ambiguity_threshold"] == 0.15
    assert body["ambiguity_min_decisions"] == 20
    assert body["tags"][0]["ambiguity_alert"] is True
    assert body["tags"][0]["backfill_count"] == 41


def test_delete_annotation_takes_no_provenance_arguments(client, calls):
    # Clearing a cell is recorded by the migration-445 trigger; no parameter can
    # reach it, so the route and the toolkit signature both stay as they were.
    res = client.delete("/new-dedup/labeling/tags/2/annotations/1")
    assert res.status_code == 200
    assert calls["clear_state"] == {"image_id": 1, "tag_id": 2}


# --- the gate ---------------------------------------------------------------


def test_new_dedup_labeling_requires_admin(client):
    api_main.app.dependency_overrides.pop(deps.require_admin, None)
    assert client.get("/new-dedup/labeling/overview").status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/new-dedup/labeling/definitions",
        "/new-dedup/labeling/tags/1/definition",
        "/new-dedup/labeling/tags/1/definition/versions",
        "/new-dedup/labeling/tags/1/positive-images",
        "/new-dedup/labeling/tags/1/neighbours",
    ],
)
def test_definition_routes_are_admin_gated(client, path):
    api_main.app.dependency_overrides.pop(deps.require_admin, None)
    assert client.get(path).status_code == 401
