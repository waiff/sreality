"""Hermetic tests for the sreality image re-master lane.

No network, no Postgres, no R2: a fake connection records every statement it is
handed, a fake bucket records every put, and the CDN download + detail fetch are
monkeypatched. What is pinned here is the lane's decision table — which outcome
each failure produces, and what is (and is NOT) written for each.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import requests
import yaml

from scraper import db, image_storage
from scripts import remaster_sreality_images as remaster

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "sreality_image_remaster.yml"

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class _Cursor:
    rowcount = 0

    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.statements.append((" ".join(sql.split()), params))
        self._conn.result = self._conn.answer(sql, params)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._conn.result)


class _Txn:
    def __enter__(self) -> "_Txn":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    """Records SQL; answers the two SELECTs from canned pages."""

    def __init__(
        self,
        listings: list[tuple[int, int | None, bool]] | None = None,
        images: list[tuple[int, int, int | None, str, str | None]] | None = None,
    ) -> None:
        self.statements: list[tuple[str, Any]] = []
        self.result: list[tuple[Any, ...]] = []
        self._listings = listings or []
        self._images = images or []
        self._listing_pages = 0

    def answer(self, sql: str, params: Any) -> list[tuple[Any, ...]]:
        if "FROM listings" in sql:
            # One page per (active, inactive) loop, then exhausted.
            if params and params.get("active") is False:
                return [r for r in self._listings if not r[2]]
            if params and "active" in params:
                return [r for r in self._listings if r[2]]
            return list(self._listings)
        if "FROM images" in sql:
            ids = set(params["ids"])
            return [r for r in self._images if r[1] in ids]
        return []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Txn:
        return _Txn()


class _R2:
    def __init__(self, fail: bool = False) -> None:
        self.puts: list[tuple[str, bytes, str]] = []
        self.fail = fail

    def upload_bytes(self, key: str, data: bytes, content_type: str = "image/jpeg") -> None:
        if self.fail:
            raise RuntimeError("r2 down")
        self.puts.append((key, data, content_type))


def _http_error(status: int) -> requests.HTTPError:
    """A real requests.HTTPError, so the shared classifiers see what they expect."""
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"HTTP {status}", response=response)


class _Client:
    """Stands in for SrealityClient.get_detail."""

    def __init__(self, payloads: dict[int, Any]) -> None:
        self.payloads = payloads
        self.calls: list[int] = []

    def get_detail(self, sreality_id: int) -> dict[str, Any]:
        self.calls.append(sreality_id)
        payload = self.payloads[sreality_id]
        if isinstance(payload, Exception):
            raise payload
        return payload


def _boom(status: int) -> Any:
    def _download(url: str) -> bytes:
        raise _http_error(status)

    return _download


@pytest.fixture(autouse=True)
def _no_pillow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dimensions and pHash come from bytes we never really encode."""
    monkeypatch.setattr(image_storage, "image_dimensions", lambda data: (1800, 1200))
    monkeypatch.setattr(remaster, "_phash_or_none", lambda data: 4242)


def _one_image_conn(url: str = "https://d18-a.sdn.cz/x/1.jpg") -> _Conn:
    return _Conn(
        listings=[(101, 55501, True)],
        images=[(9001, 101, 3, url, "101/0003.jpg")],
    )


def _run(conn: _Conn, r2: Any, resolver: Any = None, **kwargs: Any) -> remaster._Stats:
    return remaster.run_remaster(conn, r2, resolver, **kwargs)


def _updates(conn: _Conn) -> list[tuple[str, Any]]:
    return [(sql, params) for sql, params in conn.statements if sql.startswith("UPDATE")]


def test_remastered_stamps_master_and_puts_to_the_rows_exact_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _one_image_conn()
    r2 = _R2()
    monkeypatch.setattr(remaster, "_download_bytes", lambda url: JPEG)

    stats = _run(conn, r2)

    assert stats.remastered == 1 and stats.scanned == 1
    # The row's stored key, verbatim — never image_key(listing_id, sequence).
    assert [key for key, _, _ in r2.puts] == ["101/0003.jpg"]
    stamps = [p for sql, p in _updates(conn) if "rendition = %s" in sql]
    assert stamps == [(image_storage.RENDITION_SREALITY_MASTER, 1800, 1200, 4242, None, 9001)]
    # ... and the derived signals were re-armed in the same call.
    assert any("phash = NULL" in sql for sql, _ in conn.statements)


def test_stale_url_is_re_resolved_from_the_current_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _one_image_conn()
    r2 = _R2()
    fresh = "https://d18-a.sdn.cz/fresh/9.jpg"
    client = _Client({55501: {"advert_images": [{"url": fresh, "order": 3}]}})

    def _download(url: str) -> bytes:
        if fresh in url:
            return JPEG
        raise _http_error(404)

    monkeypatch.setattr(remaster, "_download_bytes", _download)
    stats = _run(conn, r2, remaster._DetailResolver(client))

    assert stats.remastered == 1 and stats.detail_fetches == 1
    assert client.calls == [55501]
    stamp = [p for sql, p in _updates(conn) if "rendition = %s" in sql][0]
    # The fresh URL is persisted with the bytes it produced.
    assert stamp[4] == fresh
    assert r2.puts[0][0] == "101/0003.jpg"


def test_detail_gone_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from scraper.portal_base import ListingGoneError

    conn = _one_image_conn()
    r2 = _R2()
    client = _Client({55501: ListingGoneError("u", 404)})
    monkeypatch.setattr(remaster, "_download_bytes", _boom(404))

    stats = _run(conn, r2, remaster._DetailResolver(client))

    assert stats.terminal == 1 and stats.remastered == 0 and not r2.puts
    assert [sql for sql, _ in _updates(conn)] == [
        " ".join(db._MARK_IMAGE_REMASTER_TERMINAL_SQL.split())
    ]


def test_missing_sequence_in_detail_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _one_image_conn()
    client = _Client({55501: {"advert_images": [{"url": "https://x.sdn.cz/a.jpg", "order": 1}]}})
    monkeypatch.setattr(remaster, "_download_bytes", _boom(410))

    stats = _run(conn, _R2(), remaster._DetailResolver(client))

    assert stats.terminal == 1


def test_fresh_url_also_gone_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _one_image_conn()
    fresh = "https://d18-a.sdn.cz/fresh/9.jpg"
    client = _Client({55501: {"advert_images": [{"url": fresh, "order": 3}]}})
    monkeypatch.setattr(remaster, "_download_bytes", _boom(404))

    stats = _run(conn, _R2(), remaster._DetailResolver(client))

    assert stats.terminal == 1 and stats.detail_fetches == 1


def test_throttle_403_defers_and_writes_only_the_attempt_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _one_image_conn()
    r2 = _R2()
    client = _Client({})
    monkeypatch.setattr(remaster, "_download_bytes", _boom(403))

    stats = _run(conn, r2, remaster._DetailResolver(client))

    assert stats.deferred == 1 and stats.terminal == 0 and not r2.puts
    # 403 is a throttle, never a dead URL: no detail fetch, no rendition claim.
    assert client.calls == []
    assert [sql for sql, _ in _updates(conn)] == [
        " ".join(db._MARK_IMAGE_REMASTER_DEFERRED_SQL.split())
    ]


def test_r2_failure_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _one_image_conn()
    monkeypatch.setattr(remaster, "_download_bytes", lambda url: JPEG)

    stats = _run(conn, _R2(fail=True))

    assert stats.deferred == 1 and stats.remastered == 0


def test_legacy_crop_dimensions_are_an_anomaly_not_a_master(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _one_image_conn()
    r2 = _R2()
    monkeypatch.setattr(remaster, "_download_bytes", lambda url: JPEG)
    monkeypatch.setattr(image_storage, "image_dimensions", lambda data: (749, 562))

    stats = _run(conn, r2)

    assert stats.anomalies == 1 and stats.remastered == 0
    assert not r2.puts and not _updates(conn)


def test_non_jpeg_bytes_are_an_anomaly(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _one_image_conn()
    r2 = _R2()
    monkeypatch.setattr(remaster, "_download_bytes", lambda url: b"<html>" + b"\x00" * 32)

    stats = _run(conn, r2)

    assert stats.anomalies == 1 and not r2.puts


def test_twenty_anomalies_abort_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    images = [
        (9000 + n, 101, n, f"https://d18-a.sdn.cz/x/{n}.jpg", f"101/{n:04d}.jpg")
        for n in range(1, 41)
    ]
    conn = _Conn(listings=[(101, 55501, True)], images=images)
    monkeypatch.setattr(remaster, "_download_bytes", lambda url: JPEG)
    monkeypatch.setattr(image_storage, "image_dimensions", lambda data: (749, 562))

    stats = _run(conn, _R2(), workers=2)

    assert stats.stopped == "anomalies"
    assert stats.anomalies == remaster._ANOMALY_ABORT
    assert stats.remastered == 0


def test_bad_storage_path_is_never_written(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _Conn(
        listings=[(101, 55501, True)],
        images=[(9001, 101, 3, "https://d18-a.sdn.cz/x/1.jpg", "sreality/101/3.jpeg")],
    )
    r2 = _R2()
    monkeypatch.setattr(remaster, "_download_bytes", lambda url: JPEG)

    stats = _run(conn, r2)

    assert stats.bad_key == 1 and not r2.puts and not _updates(conn)


def test_dry_run_touches_neither_network_nor_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _one_image_conn()
    r2 = _R2()

    def _never(url: str) -> bytes:
        raise AssertionError("dry-run must not download")

    monkeypatch.setattr(remaster, "_download_bytes", _never)
    stats = _run(conn, r2, dry_run=True)

    assert stats.scanned == 1 and stats.remastered == 0
    assert not r2.puts and not _updates(conn)


def test_max_images_caps_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    images = [
        (9000 + n, 101, n, f"https://d18-a.sdn.cz/x/{n}.jpg", f"101/{n:04d}.jpg")
        for n in range(1, 6)
    ]
    conn = _Conn(listings=[(101, 55501, True)], images=images)
    monkeypatch.setattr(remaster, "_download_bytes", lambda url: JPEG)
    monkeypatch.setattr(remaster, "_IMAGE_BATCH", 2)

    stats = _run(conn, _R2(), max_images=3)

    assert stats.scanned == 3 and stats.remastered == 3


def test_listing_ids_scope_ignores_the_shard(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _one_image_conn()
    monkeypatch.setattr(remaster, "_download_bytes", lambda url: JPEG)

    _run(conn, _R2(), shard=(2, 6), listing_ids=[101])

    selects = [(sql, params) for sql, params in conn.statements if "FROM listings" in sql]
    assert len(selects) == 1
    assert "hashint8" not in selects[0][0]
    assert selects[0][1] == {"ids": [101]}


def test_shard_arithmetic_is_zero_based_and_sign_safe() -> None:
    conn = _Conn()
    list(remaster._iter_listing_pages(conn, shard=(1, 1), listing_ids=None))

    params = [p for sql, p in conn.statements if "FROM listings" in sql]
    assert [p["k"] for p in params] == [0, 0]        # CLI shard 1 -> residue 0
    assert [p["n"] for p in params] == [1, 1]
    assert [p["active"] for p in params] == [True, False]   # ACTIVE listings first
    # A signed hashint8 would make half the corpus unreachable at any shard index.
    assert "abs(hashint8(id)::bigint)" in remaster._LISTING_PAGE_SQL


def test_completion_stamp_only_when_a_whole_pass_found_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = _Conn(listings=[(101, 55501, True)], images=[])
    stats = _run(empty, _R2())
    assert stats.complete is True

    busy = _one_image_conn()
    monkeypatch.setattr(remaster, "_download_bytes", lambda url: JPEG)
    assert _run(busy, _R2()).complete is False


def test_key_regex_matches_the_serve_path_shape() -> None:
    pytest.importorskip("fastapi")
    from api.routes.images import _KEY_RE as serve_key_re

    assert remaster._KEY_RE.pattern == serve_key_re.pattern


def test_db_helpers_write_exactly_what_they_promise() -> None:
    terminal = " ".join(db._MARK_IMAGE_REMASTER_TERMINAL_SQL.split())
    deferred = " ".join(db._MARK_IMAGE_REMASTER_DEFERRED_SQL.split())

    assert terminal == (
        "UPDATE images SET rendition = 'sreality-749-crop', "
        "last_download_attempt_at = now() WHERE id = %s"
    )
    assert deferred == "UPDATE images SET last_download_attempt_at = now() WHERE id = %s"
    # The literal is the rendition vocabulary's own term, not a lookalike.
    assert image_storage.RENDITION_SREALITY_LEGACY_CROP in terminal
    for sql in (terminal, deferred):
        assert "storage_path" not in sql and "download_attempts" not in sql
    assert "phash" not in terminal and "phash" not in deferred


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_is_dispatchable_and_scheduled() -> None:
    doc = _workflow()
    triggers = doc[True] if True in doc else doc["on"]
    assert triggers["schedule"] == [{"cron": "15 */2 * * *"}]
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["max_images"]["default"] == "16000"
    assert inputs["listing_ids"]["default"] == ""
    assert inputs["dry_run"]["type"] == "boolean" and inputs["dry_run"]["default"] is False
    assert inputs["shards"]["default"] == "6"


def test_workflow_cron_is_kill_switchable_without_a_code_change() -> None:
    job = _workflow()["jobs"]["remaster"]
    assert job["if"] == (
        "${{ github.event_name != 'schedule' || vars.SREALITY_REMASTER_CRON == 'on' }}"
    )
    assert job["strategy"]["matrix"]["shard"] == [1, 2, 3, 4, 5, 6]
    assert job["strategy"]["max-parallel"] == 6
    assert job["concurrency"]["cancel-in-progress"] is False
    assert job["timeout-minutes"] == 110


def test_workflow_carries_the_five_secrets_and_no_input_interpolation() -> None:
    job = _workflow()["jobs"]["remaster"]
    step = next(s for s in job["steps"] if "python -m scripts.remaster_sreality_images" in
                s.get("run", ""))
    for var in (
        "SUPABASE_DB_URL", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME",
    ):
        assert f"secrets.{var}" in step["env"][var]
    # Dispatch inputs reach the shell through env only — never spliced into it.
    assert "${{ inputs." not in step["run"]
    assert "--max-seconds 5700" in step["run"]
