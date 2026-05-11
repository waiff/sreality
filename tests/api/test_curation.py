"""Tests for the curation endpoints (collections, notes, tags).

Hermetic — overrides get_db_conn so no real DB is hit, and patches the
persistence helpers in api.curation to in-memory dicts. Pydantic
validation tests run unmodified through the route layer; CHECK
constraints and unique indexes are exercised in a separate integration
test path against a real Postgres (out of scope for this file).
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import curation
from api import dependencies as deps
from api import main as api_main


@pytest.fixture()
def client():
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


@pytest.fixture()
def store(monkeypatch):
    """In-memory replacement for the curation.* persistence functions."""
    state: dict[str, Any] = {
        "collections":      {},
        "next_collection":  1,
        "memberships":      set(),
        "notes":            {},
        "next_note":        1,
        "tags":             {},
        "next_tag":         1,
        "tag_links":        set(),
    }

    def _coll_to_dict(cid: int) -> dict[str, Any]:
        c = state["collections"][cid]
        count = sum(1 for (cc, _) in state["memberships"] if cc == cid)
        return {**c, "listing_count": count}

    def _tag_to_dict(tid: int) -> dict[str, Any]:
        t = state["tags"][tid]
        count = sum(1 for (_, tt) in state["tag_links"] if tt == tid)
        return {**t, "listing_count": count}

    def fake_create_collection(conn, body):
        for c in state["collections"].values():
            if c["name"].lower() == body.name.lower():
                from fastapi import HTTPException
                raise HTTPException(409, "collection name already exists")
        cid = state["next_collection"]
        state["next_collection"] += 1
        state["collections"][cid] = {
            "id": cid,
            "name": body.name,
            "description": body.description,
            "created_at": "2026-05-10T00:00:00+00:00",
            "updated_at": "2026-05-10T00:00:00+00:00",
        }
        return {**state["collections"][cid], "listing_count": 0}

    def fake_list_collections(conn):
        rows = [_coll_to_dict(cid) for cid in state["collections"]]
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        return {"data": rows, "total": len(rows)}

    def fake_get_collection(conn, cid):
        if cid not in state["collections"]:
            from fastapi import HTTPException
            raise HTTPException(404, "collection not found")
        listings = [
            {
                "sreality_id": sid, "district": None, "disposition": None,
                "area_m2": None, "price_czk": None,
                "last_seen_at": "2026-05-10T00:00:00+00:00",
                "is_active": True,
                "added_at": "2026-05-10T00:00:00+00:00",
            }
            for (cc, sid) in state["memberships"] if cc == cid
        ]
        return {"collection": _coll_to_dict(cid), "listings": listings}

    def fake_update_collection(conn, cid, body):
        if cid not in state["collections"]:
            from fastapi import HTTPException
            raise HTTPException(404, "collection not found")
        if body.name is not None:
            for other_cid, c in state["collections"].items():
                if other_cid != cid and c["name"].lower() == body.name.lower():
                    from fastapi import HTTPException
                    raise HTTPException(409, "collection name already exists")
            state["collections"][cid]["name"] = body.name
        if body.description is not None:
            state["collections"][cid]["description"] = body.description
        return _coll_to_dict(cid)

    def fake_delete_collection(conn, cid):
        if cid not in state["collections"]:
            from fastapi import HTTPException
            raise HTTPException(404, "collection not found")
        del state["collections"][cid]
        state["memberships"] = {
            (cc, sid) for (cc, sid) in state["memberships"] if cc != cid
        }
        return {"deleted": True}

    def fake_add_listings(conn, cid, body):
        if cid not in state["collections"]:
            from fastapi import HTTPException
            raise HTTPException(404, "collection not found")
        added = 0
        for sid in body.sreality_ids:
            key = (cid, sid)
            if key not in state["memberships"]:
                state["memberships"].add(key)
                added += 1
        return {"added": added, "skipped": len(body.sreality_ids) - added}

    def fake_remove_listing(conn, cid, sid):
        key = (cid, sid)
        removed = key in state["memberships"]
        state["memberships"].discard(key)
        return {"removed": removed}

    def fake_list_notes(conn, sid):
        items = [n for n in state["notes"].values() if n["sreality_id"] == sid]
        items.sort(key=lambda n: (n["created_at"], n["id"]), reverse=True)
        return {"data": items}

    def fake_create_note(conn, sid, body):
        nid = state["next_note"]
        state["next_note"] += 1
        note = {
            "id": nid,
            "sreality_id": sid,
            "body": body.body,
            "created_at": f"2026-05-10T00:00:0{nid}+00:00",
        }
        state["notes"][nid] = note
        return note

    def fake_list_tags(conn):
        rows = [_tag_to_dict(tid) for tid in state["tags"]]
        rows.sort(key=lambda r: r["name"].lower())
        return {"data": rows}

    def fake_create_tag(conn, body):
        for t in state["tags"].values():
            if t["name"].lower() == body.name.lower():
                from fastapi import HTTPException
                raise HTTPException(409, "tag name already exists")
        tid = state["next_tag"]
        state["next_tag"] += 1
        state["tags"][tid] = {
            "id": tid,
            "name": body.name,
            "color": body.color,
            "created_at": "2026-05-10T00:00:00+00:00",
        }
        return {**state["tags"][tid], "listing_count": 0}

    def fake_delete_tag(conn, tid):
        if tid not in state["tags"]:
            from fastapi import HTTPException
            raise HTTPException(404, "tag not found")
        del state["tags"][tid]
        state["tag_links"] = {
            (sid, tt) for (sid, tt) in state["tag_links"] if tt != tid
        }
        return {"deleted": True}

    def fake_attach_tag(conn, sid, body):
        if body.tag_id not in state["tags"]:
            from fastapi import HTTPException
            raise HTTPException(404, "tag not found")
        key = (sid, body.tag_id)
        attached = key not in state["tag_links"]
        state["tag_links"].add(key)
        return {"attached": attached}

    def fake_detach_tag(conn, sid, tid):
        key = (sid, tid)
        detached = key in state["tag_links"]
        state["tag_links"].discard(key)
        return {"detached": detached}

    monkeypatch.setattr(curation, "create_collection", fake_create_collection)
    monkeypatch.setattr(curation, "list_collections", fake_list_collections)
    monkeypatch.setattr(curation, "get_collection", fake_get_collection)
    monkeypatch.setattr(curation, "update_collection", fake_update_collection)
    monkeypatch.setattr(curation, "delete_collection", fake_delete_collection)
    monkeypatch.setattr(
        curation, "add_listings_to_collection", fake_add_listings,
    )
    monkeypatch.setattr(
        curation, "remove_listing_from_collection", fake_remove_listing,
    )
    monkeypatch.setattr(curation, "list_notes", fake_list_notes)
    monkeypatch.setattr(curation, "create_note", fake_create_note)
    monkeypatch.setattr(curation, "list_tags", fake_list_tags)
    monkeypatch.setattr(curation, "create_tag", fake_create_tag)
    monkeypatch.setattr(curation, "delete_tag", fake_delete_tag)
    monkeypatch.setattr(curation, "attach_tag", fake_attach_tag)
    monkeypatch.setattr(curation, "detach_tag", fake_detach_tag)

    return state


# --- collections -----------------------------------------------------------

def test_create_collection_returns_row(client, store):
    res = client.post("/collections", json={"name": "Vinohrady picks"})
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == 1
    assert body["name"] == "Vinohrady picks"
    assert body["listing_count"] == 0
    assert body["description"] is None


def test_create_collection_duplicate_name_409(client, store):
    client.post("/collections", json={"name": "shortlist"})
    res = client.post("/collections", json={"name": "SHORTLIST"})
    assert res.status_code == 409


def test_create_collection_blank_name_422(client, store):
    res = client.post("/collections", json={"name": ""})
    assert res.status_code == 422


def test_list_collections_includes_listing_count(client, store):
    client.post("/collections", json={"name": "a"})
    client.post("/collections", json={"name": "b"})
    client.post("/collections/1/listings", json={"sreality_ids": [10, 11]})

    res = client.get("/collections")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 2
    counts = {c["name"]: c["listing_count"] for c in body["data"]}
    assert counts["a"] == 2
    assert counts["b"] == 0


def test_get_collection_404_when_missing(client, store):
    res = client.get("/collections/999")
    assert res.status_code == 404


def test_get_collection_returns_metadata_and_listings(client, store):
    client.post("/collections", json={"name": "shortlist"})
    client.post("/collections/1/listings", json={"sreality_ids": [42, 43]})
    res = client.get("/collections/1")
    assert res.status_code == 200
    body = res.json()
    assert body["collection"]["name"] == "shortlist"
    assert {l["sreality_id"] for l in body["listings"]} == {42, 43}


def test_patch_rename_collection(client, store):
    client.post("/collections", json={"name": "old"})
    res = client.patch("/collections/1", json={"name": "new"})
    assert res.status_code == 200
    assert res.json()["name"] == "new"


def test_patch_rename_collection_conflict_409(client, store):
    client.post("/collections", json={"name": "a"})
    client.post("/collections", json={"name": "b"})
    res = client.patch("/collections/2", json={"name": "A"})
    assert res.status_code == 409


def test_delete_collection_404_when_missing(client, store):
    res = client.delete("/collections/999")
    assert res.status_code == 404


def test_delete_collection_succeeds(client, store):
    client.post("/collections", json={"name": "doomed"})
    res = client.delete("/collections/1")
    assert res.status_code == 200
    assert res.json() == {"deleted": True}
    assert client.get("/collections/1").status_code == 404


# --- collection_listings ---------------------------------------------------

def test_add_listings_returns_added_skipped(client, store):
    client.post("/collections", json={"name": "x"})
    res = client.post(
        "/collections/1/listings", json={"sreality_ids": [1, 2, 3]},
    )
    assert res.status_code == 200
    assert res.json() == {"added": 3, "skipped": 0}

    # Re-adding 2 + new is skipped+added.
    res = client.post(
        "/collections/1/listings", json={"sreality_ids": [2, 3, 4]},
    )
    assert res.json() == {"added": 1, "skipped": 2}


def test_add_listings_empty_body_422(client, store):
    client.post("/collections", json={"name": "x"})
    res = client.post(
        "/collections/1/listings", json={"sreality_ids": []},
    )
    assert res.status_code == 422


def test_add_listings_to_missing_collection_404(client, store):
    res = client.post(
        "/collections/999/listings", json={"sreality_ids": [1]},
    )
    assert res.status_code == 404


def test_remove_listing_from_collection(client, store):
    client.post("/collections", json={"name": "x"})
    client.post("/collections/1/listings", json={"sreality_ids": [10, 11]})
    res = client.delete("/collections/1/listings/10")
    assert res.status_code == 200
    assert res.json() == {"removed": True}
    res = client.delete("/collections/1/listings/10")  # idempotent
    assert res.json() == {"removed": False}


# --- notes -----------------------------------------------------------------

def test_create_note_round_trip(client, store):
    res = client.post("/listings/12345/notes", json={"body": "first"})
    assert res.status_code == 200
    assert res.json()["body"] == "first"
    client.post("/listings/12345/notes", json={"body": "second"})

    res = client.get("/listings/12345/notes")
    assert res.status_code == 200
    bodies = [n["body"] for n in res.json()["data"]]
    assert bodies == ["second", "first"]  # newest first


def test_create_note_blank_body_422(client, store):
    res = client.post("/listings/12345/notes", json={"body": ""})
    assert res.status_code == 422


def test_create_note_too_long_422(client, store):
    res = client.post(
        "/listings/12345/notes", json={"body": "x" * 4001},
    )
    assert res.status_code == 422


def test_notes_for_unknown_listing_returns_empty(client, store):
    res = client.get("/listings/0/notes")
    assert res.status_code == 200
    assert res.json() == {"data": []}


# --- tags ------------------------------------------------------------------

def test_create_tag(client, store):
    res = client.post("/tags", json={"name": "hot", "color": "brick"})
    assert res.status_code == 200
    assert res.json()["color"] == "brick"
    assert res.json()["listing_count"] == 0


def test_create_tag_duplicate_name_409(client, store):
    client.post("/tags", json={"name": "hot", "color": "brick"})
    res = client.post("/tags", json={"name": "HOT", "color": "sage"})
    assert res.status_code == 409


def test_create_tag_invalid_color_422(client, store):
    res = client.post("/tags", json={"name": "hot", "color": "neon"})
    assert res.status_code == 422


def test_create_tag_missing_color_422(client, store):
    res = client.post("/tags", json={"name": "hot"})
    assert res.status_code == 422


def test_list_tags_alphabetic(client, store):
    client.post("/tags", json={"name": "zebra", "color": "slate"})
    client.post("/tags", json={"name": "alpha", "color": "copper"})
    res = client.get("/tags")
    names = [t["name"] for t in res.json()["data"]]
    assert names == ["alpha", "zebra"]


def test_attach_tag_idempotent(client, store):
    client.post("/tags", json={"name": "hot", "color": "brick"})
    res = client.post("/listings/42/tags", json={"tag_id": 1})
    assert res.status_code == 200
    assert res.json() == {"attached": True}
    res = client.post("/listings/42/tags", json={"tag_id": 1})
    assert res.json() == {"attached": False}


def test_attach_unknown_tag_404(client, store):
    res = client.post("/listings/42/tags", json={"tag_id": 999})
    assert res.status_code == 404


def test_detach_tag(client, store):
    client.post("/tags", json={"name": "hot", "color": "brick"})
    client.post("/listings/42/tags", json={"tag_id": 1})
    res = client.delete("/listings/42/tags/1")
    assert res.status_code == 200
    assert res.json() == {"detached": True}
    res = client.delete("/listings/42/tags/1")
    assert res.json() == {"detached": False}


def test_delete_tag_cascades_attachments(client, store):
    client.post("/tags", json={"name": "hot", "color": "brick"})
    client.post("/listings/42/tags", json={"tag_id": 1})
    res = client.delete("/tags/1")
    assert res.status_code == 200
    assert res.json() == {"deleted": True}
    # In-memory mock cascades; tag is gone.
    res = client.get("/tags")
    assert res.json()["data"] == []


def test_delete_unknown_tag_404(client, store):
    res = client.delete("/tags/999")
    assert res.status_code == 404


# --- direct unit coverage of curation helpers (no FastAPI) -----------------

class _Cur:
    def __init__(self, conn):
        self._conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def execute(self, sql, params=()):
        self._conn.executions.append((sql, params))
        self.rowcount = self._conn._rowcounts.pop(0) if self._conn._rowcounts else 0

    def fetchone(self):
        return self._conn._results.pop(0) if self._conn._results else None

    def fetchall(self):
        return self._conn._results.pop(0) if self._conn._results else []


class _Tx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class _FakeConn:
    def __init__(self, results=None, rowcounts=None):
        self.executions: list[tuple[Any, Any]] = []
        self._results: list[Any] = list(results or [])
        self._rowcounts: list[int] = list(rowcounts or [])

    def cursor(self):
        return _Cur(self)

    def transaction(self):
        return _Tx()


def test_create_collection_helper_inserts_and_returns():
    from api import schemas as s
    conn = _FakeConn(results=[
        (1, "x", None, "2026-05-10T00:00:00+00:00", "2026-05-10T00:00:00+00:00"),
    ])
    out = curation.create_collection(conn, s.CreateCollectionIn(name="x"))
    assert out["id"] == 1
    assert out["listing_count"] == 0
    assert "INSERT INTO collections" in conn.executions[0][0]


def test_create_tag_helper_409_on_unique_violation():
    from api import schemas as s
    from fastapi import HTTPException

    class _BoomCur(_Cur):
        def execute(self, sql, params=()):
            raise psycopg.errors.UniqueViolation("dup")

    class _BoomConn(_FakeConn):
        def cursor(self):
            return _BoomCur(self)

    with pytest.raises(HTTPException) as exc:
        curation.create_tag(
            _BoomConn(), s.CreateTagIn(name="hot", color="brick"),
        )
    assert exc.value.status_code == 409


def test_list_notes_helper_orders_newest_first():
    conn = _FakeConn(results=[[
        (2, 42, "second", "2026-05-10T00:00:02+00:00"),
        (1, 42, "first",  "2026-05-10T00:00:01+00:00"),
    ]])
    out = curation.list_notes(conn, sreality_id=42)
    assert [n["body"] for n in out["data"]] == ["second", "first"]
    assert "ORDER BY created_at DESC" in conn.executions[0][0]
