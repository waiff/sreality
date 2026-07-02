"""Tests for scraper.freshness.

Hermetic: monkeypatches the helpers that touch the DB, swaps in a stub
SrealityClient. No live psycopg connection.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import requests

from scraper import freshness, hashing


_FIXTURE = Path(__file__).parent / "fixtures" / "sample_listing.json"


def _load_raw() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text())


class _StubClient:
    def __init__(
        self,
        raw: dict[str, Any] | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self._raw = raw
        self._exc = exc
        self.calls = 0

    def get_detail(self, sreality_id: int) -> dict[str, Any]:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        assert self._raw is not None
        return self._raw


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self
    def __exit__(self, *exc: Any) -> None:
        return None


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
    def __enter__(self) -> "_Cur":
        return self
    def __exit__(self, *exc: Any) -> None:
        return None
    def execute(self, sql: str, params: Any = ()) -> None:
        self._conn.executions.append((sql, params))


class _FakeConn:
    """Records execute() calls. Helper functions are monkeypatched away,
    so the only SQL that reaches this conn is from _record_gone (the
    UPDATE listings statement)."""
    def __init__(self) -> None:
        self.executions: list[tuple[str, Any]] = []
    def transaction(self) -> _Ctx:
        return _Ctx()
    def cursor(self) -> _Cur:
        return _Cur(self)


def _patch_db(
    monkeypatch: pytest.MonkeyPatch,
    prev: dict[str, Any] | None,
    new_snap_id: int | None = 99,
) -> dict[str, list]:
    """Stub all DB helpers. Returns a dict of recorded calls."""
    calls: dict[str, list] = {
        "log": [], "upsert": [], "images": [],
    }
    monkeypatch.setattr(
        freshness, "_fetch_prev_snapshot", lambda c, sid: prev
    )
    monkeypatch.setattr(
        freshness, "_fetch_latest_snapshot_id", lambda c, sid: new_snap_id
    )
    monkeypatch.setattr(
        freshness, "_insert_log",
        lambda c, sid, o, prev_hash, new_hash, error: calls["log"].append(
            {"sreality_id": sid, "outcome": o, "prev_hash": prev_hash,
             "new_hash": new_hash, "error": error}
        ),
    )
    monkeypatch.setattr(
        freshness.db, "upsert_listing",
        lambda c, row, raw, h: calls["upsert"].append({"hash": h, "row": row}) or "updated",
    )
    monkeypatch.setattr(
        freshness.db, "record_images",
        lambda c, sid, imgs: calls["images"].append({"count": len(list(imgs))}) or 0,
    )
    return calls


def test_unchanged_writes_log_no_listings_writes(monkeypatch):
    raw = _load_raw()
    h = hashing.content_hash(raw)
    prev = {"id": 7, "content_hash": h, "raw_json": raw}
    calls = _patch_db(monkeypatch, prev)

    client = _StubClient(raw=raw)
    conn = _FakeConn()
    res = freshness.freshness_check(conn, client, sreality_id=2836292428)

    assert res["outcome"] == "unchanged"
    assert res["snapshot_id"] == 7
    assert res["prev_hash"] == h
    assert res["new_hash"] == h
    assert res["what_changed"] == []
    assert res["error_message"] is None

    assert calls["upsert"] == []
    assert calls["images"] == []
    assert len(calls["log"]) == 1
    assert calls["log"][0]["outcome"] == "unchanged"
    # No raw SQL hit our fake conn either (helpers monkeypatched).
    assert conn.executions == []


def test_updated_writes_snapshot_and_reports_diff(monkeypatch):
    prev_raw = _load_raw()
    prev_hash = hashing.content_hash(prev_raw)

    new_raw = copy.deepcopy(prev_raw)
    new_raw["price_czk"] = 22500
    new_raw["price_summary_czk"] = 22500
    new_hash = hashing.content_hash(new_raw)
    assert new_hash != prev_hash

    prev = {"id": 7, "content_hash": prev_hash, "raw_json": prev_raw}
    calls = _patch_db(monkeypatch, prev, new_snap_id=42)

    client = _StubClient(raw=new_raw)
    conn = _FakeConn()
    res = freshness.freshness_check(conn, client, sreality_id=2836292428)

    assert res["outcome"] == "updated"
    assert res["snapshot_id"] == 42
    assert res["prev_hash"] == prev_hash
    assert res["new_hash"] == new_hash
    assert "price_czk" in res["what_changed"]

    assert len(calls["upsert"]) == 1
    assert calls["upsert"][0]["hash"] == new_hash
    assert len(calls["images"]) == 1
    assert len(calls["log"]) == 1
    assert calls["log"][0]["outcome"] == "updated"


def test_404_marks_inactive_and_logs_gone(monkeypatch):
    prev_raw = _load_raw()
    prev_hash = hashing.content_hash(prev_raw)
    prev = {"id": 7, "content_hash": prev_hash, "raw_json": prev_raw}
    calls = _patch_db(monkeypatch, prev)

    resp = requests.Response()
    resp.status_code = 404
    exc = requests.HTTPError("404 Not Found", response=resp)
    client = _StubClient(exc=exc)
    conn = _FakeConn()
    res = freshness.freshness_check(conn, client, sreality_id=2836292428)

    assert res["outcome"] == "gone"
    assert res["snapshot_id"] is None
    assert res["new_hash"] is None
    assert calls["upsert"] == []
    assert calls["images"] == []
    assert any("UPDATE listings" in sql for sql, _ in conn.executions)
    assert any("is_active = false" in sql for sql, _ in conn.executions)
    # the flip stamps the delisting moment (migration 175)
    assert any("inactive_at = now()" in sql for sql, _ in conn.executions)
    assert calls["log"][0]["outcome"] == "gone"


def test_410_also_treated_as_gone(monkeypatch):
    prev = None
    calls = _patch_db(monkeypatch, prev)

    resp = requests.Response()
    resp.status_code = 410
    exc = requests.HTTPError("410 Gone", response=resp)
    client = _StubClient(exc=exc)
    conn = _FakeConn()
    res = freshness.freshness_check(conn, client, sreality_id=2836292428)

    assert res["outcome"] == "gone"
    assert calls["log"][0]["outcome"] == "gone"


def test_listing_gone_error_treated_as_gone(monkeypatch):
    """Production path: get_detail raises ListingGoneError (a wrapped
    404/410 or sreality's 'page does not exist' body). Must flip inactive
    and log gone, not record a fetch error."""
    from scraper.sreality_client import ListingGoneError

    calls = _patch_db(monkeypatch, prev=None)
    client = _StubClient(
        exc=ListingGoneError("https://www.sreality.cz/api/.../estates/1", 200)
    )
    conn = _FakeConn()
    res = freshness.freshness_check(conn, client, sreality_id=2836292428)

    assert res["outcome"] == "gone"
    assert any("is_active = false" in sql for sql, _ in conn.executions)
    assert calls["log"][0]["outcome"] == "gone"


def test_500_treated_as_fetch_error(monkeypatch):
    prev_raw = _load_raw()
    prev_hash = hashing.content_hash(prev_raw)
    prev = {"id": 7, "content_hash": prev_hash, "raw_json": prev_raw}
    calls = _patch_db(monkeypatch, prev)

    resp = requests.Response()
    resp.status_code = 500
    exc = requests.HTTPError("500 Internal Server Error", response=resp)
    client = _StubClient(exc=exc)
    conn = _FakeConn()
    res = freshness.freshness_check(conn, client, sreality_id=2836292428)

    assert res["outcome"] == "fetch_error"
    assert res["error_message"] is not None
    assert "500" in res["error_message"]
    assert calls["upsert"] == []
    # No UPDATE listings — a 500 is not evidence the listing is gone.
    assert all("UPDATE listings" not in sql for sql, _ in conn.executions)


def test_generic_exception_is_fetch_error(monkeypatch):
    calls = _patch_db(monkeypatch, prev=None)

    client = _StubClient(exc=ConnectionError("dns failure"))
    conn = _FakeConn()
    res = freshness.freshness_check(conn, client, sreality_id=2836292428)

    assert res["outcome"] == "fetch_error"
    assert "dns failure" in res["error_message"]
    assert calls["upsert"] == []
    assert all("UPDATE listings" not in sql for sql, _ in conn.executions)


def test_db_write_failure_is_fetch_error(monkeypatch):
    prev_raw = _load_raw()
    new_raw = copy.deepcopy(prev_raw)
    new_raw["price_czk"] = 22500
    new_raw["price_summary_czk"] = 22500

    prev = {
        "id": 7,
        "content_hash": hashing.content_hash(prev_raw),
        "raw_json": prev_raw,
    }
    calls = _patch_db(monkeypatch, prev)

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(freshness.db, "upsert_listing", boom)

    client = _StubClient(raw=new_raw)
    conn = _FakeConn()
    res = freshness.freshness_check(conn, client, sreality_id=2836292428)

    assert res["outcome"] == "fetch_error"
    assert "db down" in res["error_message"]
    assert calls["log"][0]["outcome"] == "fetch_error"


def test_no_prior_snapshot_treats_as_updated(monkeypatch):
    raw = _load_raw()
    new_hash = hashing.content_hash(raw)
    calls = _patch_db(monkeypatch, prev=None, new_snap_id=1)

    client = _StubClient(raw=raw)
    conn = _FakeConn()
    res = freshness.freshness_check(conn, client, sreality_id=2836292428)

    assert res["outcome"] == "updated"
    assert res["snapshot_id"] == 1
    assert res["prev_hash"] is None
    assert res["new_hash"] == new_hash
    assert res["what_changed"] == []  # no prev to diff against
    assert len(calls["upsert"]) == 1


def test_image_changes_appear_in_what_changed(monkeypatch):
    prev_raw = _load_raw()
    new_raw = copy.deepcopy(prev_raw)
    images = new_raw.get("advert_images") or []
    if not images:
        pytest.skip("fixture has no images to mutate")
    added = copy.deepcopy(images[0])
    added["id"] = 999999999
    added["url"] = "//d18-a.sdn.cz/d_18/c_img_qB_D/newUpload/abcd.jpeg"
    added["order"] = len(images) + 1
    images.append(added)

    prev = {
        "id": 7,
        "content_hash": hashing.content_hash(prev_raw),
        "raw_json": prev_raw,
    }
    _patch_db(monkeypatch, prev)

    client = _StubClient(raw=new_raw)
    conn = _FakeConn()
    res = freshness.freshness_check(conn, client, sreality_id=2836292428)

    assert res["outcome"] == "updated"
    assert "images" in res["what_changed"]


def test_resigned_image_url_is_unchanged(monkeypatch):
    # a re-signed sdn.cz image URL (same image id) is CDN churn, not content
    prev_raw = _load_raw()
    new_raw = copy.deepcopy(prev_raw)
    images = new_raw.get("advert_images") or []
    if not images:
        pytest.skip("fixture has no images to mutate")
    images[0]["url"] = images[0]["url"] + "?changed"

    prev = {
        "id": 7,
        "content_hash": hashing.content_hash(prev_raw),
        "raw_json": prev_raw,
    }
    _patch_db(monkeypatch, prev)

    client = _StubClient(raw=new_raw)
    conn = _FakeConn()
    res = freshness.freshness_check(conn, client, sreality_id=2836292428)

    assert res["outcome"] == "unchanged"
