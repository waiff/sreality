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

from scraper import freshness
from scraper.hashing import digest, sreality_hash_doc
from scraper.listing_write import SnapshotRef, WriteOutcome


def content_hash(raw: dict[str, Any]) -> str:
    return digest(sreality_hash_doc(raw))


def _snap(raw: dict[str, Any], snapshot_id: int = 7) -> SnapshotRef:
    return SnapshotRef(id=snapshot_id, content_hash=content_hash(raw), raw_json=raw)


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


_NOMINATED = ("sreality", [("2836292428", None, None, freshness.db.QUEUE_PRIORITY_VERIFY)])


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
    prev: SnapshotRef | None,
    new_snap_id: int = 99,
) -> dict[str, list]:
    """Stub all DB helpers. Returns a dict of recorded calls."""
    calls: dict[str, list] = {"log": [], "upsert": [], "nominate": []}
    monkeypatch.setattr(
        freshness.listing_write, "latest_snapshot", lambda c, source, native: prev
    )
    monkeypatch.setattr(
        freshness.db, "enqueue_detail",
        lambda _c, source, entries: calls["nominate"].append((source, list(entries))) or 1,
    )

    def _write_listings(_c: Any, writes: list[Any]) -> list[WriteOutcome]:
        calls["upsert"].extend(writes)
        return [WriteOutcome("sreality", w.source_id_native, 1, "updated", new_snap_id,
                             w.content_hash, 0) for w in writes]

    monkeypatch.setattr(freshness.listing_write, "write_listings", _write_listings)
    monkeypatch.setattr(
        freshness, "_insert_log",
        lambda c, sid, o, prev_hash, new_hash, error: calls["log"].append(
            {"sreality_id": sid, "outcome": o, "prev_hash": prev_hash,
             "new_hash": new_hash, "error": error}
        ),
    )
    return calls


def test_unchanged_writes_log_no_listings_writes(monkeypatch):
    raw = _load_raw()
    h = content_hash(raw)
    calls = _patch_db(monkeypatch, _snap(raw))

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
    assert len(calls["log"]) == 1
    assert calls["log"][0]["outcome"] == "unchanged"
    # No raw SQL hit our fake conn either (helpers monkeypatched).
    assert conn.executions == []


def test_updated_writes_snapshot_and_reports_diff(monkeypatch):
    prev_raw = _load_raw()
    prev = _snap(prev_raw)
    prev_hash = prev.content_hash

    new_raw = copy.deepcopy(prev_raw)
    new_raw["price_czk"] = 22500
    new_raw["price_summary_czk"] = 22500
    new_hash = content_hash(new_raw)
    assert new_hash != prev_hash

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
    [w] = calls["upsert"]
    assert (w.source, w.source_id_native, w.content_hash) == (
        "sreality", str(new_raw["hash_id"]), new_hash)
    assert len(w.images) == len(new_raw.get("advert_images") or [])
    assert len(calls["log"]) == 1
    assert calls["log"][0]["outcome"] == "updated"


def test_404_marks_inactive_and_logs_gone(monkeypatch):
    calls = _patch_db(monkeypatch, _snap(_load_raw()))

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
    # never flipped here: the observation nominates a page check and the drain's one
    # decider (rule #3 hysteresis) rules on it
    assert calls["nominate"] == [_NOMINATED]
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
    assert calls["nominate"] == [_NOMINATED]
    assert calls["log"][0]["outcome"] == "gone"


def test_listing_gone_error_treated_as_gone(monkeypatch):
    """Production path: get_detail raises ListingGoneError (a wrapped 404/410).
    Must nominate the page check and log gone, not record a fetch error."""
    from scraper.sreality_client import ListingGoneError

    calls = _patch_db(monkeypatch, prev=None)
    client = _StubClient(
        exc=ListingGoneError("https://www.sreality.cz/api/.../estates/1", 200)
    )
    conn = _FakeConn()
    res = freshness.freshness_check(conn, client, sreality_id=2836292428)

    assert res["outcome"] == "gone"
    assert calls["nominate"] == [_NOMINATED]
    assert calls["log"][0]["outcome"] == "gone"


def test_500_treated_as_fetch_error(monkeypatch):
    calls = _patch_db(monkeypatch, _snap(_load_raw()))

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

    calls = _patch_db(monkeypatch, _snap(prev_raw))

    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(freshness.listing_write, "write_listings", boom)

    client = _StubClient(raw=new_raw)
    conn = _FakeConn()
    res = freshness.freshness_check(conn, client, sreality_id=2836292428)

    assert res["outcome"] == "fetch_error"
    assert "db down" in res["error_message"]
    assert calls["log"][0]["outcome"] == "fetch_error"


def test_no_prior_snapshot_treats_as_updated(monkeypatch):
    raw = _load_raw()
    new_hash = content_hash(raw)
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

    _patch_db(monkeypatch, _snap(prev_raw))

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

    _patch_db(monkeypatch, _snap(prev_raw))

    client = _StubClient(raw=new_raw)
    conn = _FakeConn()
    res = freshness.freshness_check(conn, client, sreality_id=2836292428)

    assert res["outcome"] == "unchanged"
