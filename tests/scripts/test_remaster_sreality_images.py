"""Hermetic tests for the sreality image re-master lane.

No network, no Postgres, no R2: a fake connection records every statement it is
handed, a fake bucket records every put, and the CDN download + detail fetch are
monkeypatched. What is pinned here is the lane's decision table — which outcome
each failure produces, and what is (and is NOT) written for each.
"""

from __future__ import annotations

import pathlib
import re
import time
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
        images: list[tuple[int, int, int | None, str, str | None, bool]] | None = None,
    ) -> None:
        self.statements: list[tuple[str, Any]] = []
        self.result: list[tuple[Any, ...]] = []
        self._listings = listings or []
        self._images = images or []
        self._listing_pages = 0

    def _still_convertible(self) -> list[tuple[Any, ...]]:
        """What the rail-free completion probe would see: unretired, serve-shaped keys."""
        retired = {
            params[-1]
            for sql, params in self.statements
            if sql.startswith("UPDATE images SET rendition")
        }
        return [
            (1,)
            for row in self._images
            if row[0] not in retired and remaster._KEY_RE.match(row[4] or "")
        ][:1]

    def answer(self, sql: str, params: Any) -> list[tuple[Any, ...]]:
        if "JOIN listings" in sql:
            return self._still_convertible()
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


def _one_image_conn(
    url: str = "https://d18-a.sdn.cz/x/1.jpg", *, retried: bool = False
) -> _Conn:
    """`retried` is the row's own `last_download_attempt_at`: a second sighting."""
    return _Conn(
        listings=[(101, 55501, True)],
        images=[(9001, 101, 3, url, "101/0003.jpg", retried)],
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


def test_detail_gone_defers_the_first_time_and_retires_the_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scraper.portal_base import ListingGoneError

    monkeypatch.setattr(remaster, "_download_bytes", _boom(404))

    # First sighting: one 404 is not proof, so the row only gets the attempt clock
    # and comes back in 20 hours. Retiring claims the rendition FOREVER.
    first = _one_image_conn()
    stats = _run(first, _R2(), remaster._DetailResolver(_Client({55501: ListingGoneError("u", 404)})))
    assert stats.deferred == 1 and stats.terminal == 0
    assert [sql for sql, _ in _updates(first)] == [
        " ".join(db._MARK_IMAGE_REMASTER_DEFERRED_SQL.split())
    ]

    second = _one_image_conn(retried=True)
    r2 = _R2()
    stats = _run(second, r2, remaster._DetailResolver(_Client({55501: ListingGoneError("u", 404)})))
    assert stats.terminal == 1 and stats.remastered == 0 and not r2.puts
    assert [sql for sql, _ in _updates(second)] == [
        " ".join(db._MARK_IMAGE_REMASTER_TERMINAL_SQL.split())
    ]


def test_missing_sequence_in_detail_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _one_image_conn(retried=True)
    client = _Client({55501: {"advert_images": [{"url": "https://x.sdn.cz/a.jpg", "order": 1}]}})
    monkeypatch.setattr(remaster, "_download_bytes", _boom(410))

    stats = _run(conn, _R2(), remaster._DetailResolver(client))

    assert stats.terminal == 1


def test_fresh_url_also_gone_is_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _one_image_conn(retried=True)
    fresh = "https://d18-a.sdn.cz/fresh/9.jpg"
    client = _Client({55501: {"advert_images": [{"url": fresh, "order": 3}]}})
    monkeypatch.setattr(remaster, "_download_bytes", _boom(404))

    stats = _run(conn, _R2(), remaster._DetailResolver(client))

    assert stats.terminal == 1 and stats.detail_fetches == 1


def test_an_unedited_listing_is_never_probed_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The detail hands back the URL that just died — that is one URL, not two."""
    stored = "https://d18-a.sdn.cz/x/1.jpg"
    conn = _one_image_conn(stored, retried=True)
    client = _Client({55501: {"advert_images": [{"url": stored, "order": 3}]}})
    attempts: list[str] = []

    def _download(url: str) -> bytes:
        attempts.append(url)
        raise _http_error(404)

    monkeypatch.setattr(remaster, "_download_bytes", _download)
    stats = _run(conn, _R2(), remaster._DetailResolver(client))

    assert stats.terminal == 1
    assert len(attempts) == 1


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
    assert not r2.puts
    # Nothing is uploaded and no rendition is claimed — but the attempt clock IS
    # stamped, or the row is re-downloaded on every tick for ever.
    assert [sql for sql, _ in _updates(conn)] == [
        " ".join(db._MARK_IMAGE_REMASTER_DEFERRED_SQL.split())
    ]


def test_a_repeat_anomaly_is_retired_instead_of_re_downloaded_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _one_image_conn(retried=True)
    r2 = _R2()
    monkeypatch.setattr(remaster, "_download_bytes", lambda url: JPEG)
    monkeypatch.setattr(image_storage, "image_dimensions", lambda data: (749, 562))

    stats = _run(conn, r2)

    assert stats.anomalies == 1 and not r2.puts
    assert [sql for sql, _ in _updates(conn)] == [
        " ".join(db._MARK_IMAGE_REMASTER_TERMINAL_SQL.split())
    ]
    # ... and being retired is what lets the shard ever report itself complete.
    assert stats.complete is True


def test_an_oversize_body_is_an_anomaly_not_an_endless_deferral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`NotAnImageError` documents itself as terminal, not transient."""
    conn = _one_image_conn()

    def _download(url: str) -> bytes:
        raise image_storage.NotAnImageError("body exceeds the cap")

    monkeypatch.setattr(remaster, "_download_bytes", _download)
    stats = _run(conn, _R2(), remaster._DetailResolver(_Client({})))

    assert stats.anomalies == 1 and stats.deferred == 0
    # No detail fetch either: the URL resolved, its payload is simply unusable.
    assert [sql for sql, _ in _updates(conn)] == [
        " ".join(db._MARK_IMAGE_REMASTER_DEFERRED_SQL.split())
    ]


def test_non_jpeg_bytes_are_an_anomaly(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _one_image_conn()
    r2 = _R2()
    monkeypatch.setattr(remaster, "_download_bytes", lambda url: b"<html>" + b"\x00" * 32)

    stats = _run(conn, r2)

    assert stats.anomalies == 1 and not r2.puts


def _anomalous_conn(count: int) -> _Conn:
    images = [
        (9000 + n, 101, n, f"https://d18-a.sdn.cz/x/{n}.jpg", f"101/{n:04d}.jpg", False)
        for n in range(1, count + 1)
    ]
    return _Conn(listings=[(101, 55501, True)], images=images)


def test_a_wall_of_anomalies_aborts_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _anomalous_conn(40)
    monkeypatch.setattr(remaster, "_download_bytes", lambda url: JPEG)
    monkeypatch.setattr(image_storage, "image_dimensions", lambda data: (749, 562))

    stats = _run(conn, _R2(), workers=2)

    assert stats.stopped == "anomalies"
    assert stats.anomalies == remaster._ANOMALY_ABORT
    assert stats.remastered == 0


def test_a_sparse_scatter_of_anomalies_does_not_red_the_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor alone would mean 20 permanently odd objects stop every future run."""
    conn = _anomalous_conn(400)
    odd = {f"https://d18-a.sdn.cz/x/{n}.jpg" for n in range(1, 26)}
    monkeypatch.setattr(
        remaster,
        "_download_bytes",
        lambda url: b"<html>" + b"\x00" * 32 if url.split("?")[0] in odd else JPEG,
    )

    stats = _run(conn, _R2(), workers=2)

    assert stats.anomalies == 25 and stats.stopped == ""
    assert stats.remastered == 375


def test_bad_storage_path_is_never_uploaded_and_never_claims_a_rendition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _Conn(
        listings=[(101, 55501, True)],
        images=[(9001, 101, 3, "https://d18-a.sdn.cz/x/1.jpg", "sreality/101/3.jpeg", False)],
    )
    r2 = _R2()
    monkeypatch.setattr(remaster, "_download_bytes", lambda url: JPEG)

    stats = _run(conn, r2)

    assert stats.bad_key == 1 and not r2.puts
    # A key shape this lane refuses is not a fact about the bytes: no rendition
    # claim, so widening `_KEY_RE` later can still recover the row.
    assert [sql for sql, _ in _updates(conn)] == [
        " ".join(db._MARK_IMAGE_REMASTER_DEFERRED_SQL.split())
    ]
    # ... and it must not hold the completion stamp hostage either.
    assert stats.complete is True


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
        (9000 + n, 101, n, f"https://d18-a.sdn.cz/x/{n}.jpg", f"101/{n:04d}.jpg", False)
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


def test_completion_is_a_confirmed_empty_queue_not_an_idle_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = _Conn(listings=[(101, 55501, True)], images=[])
    assert _run(empty, _R2()).complete is True

    # A pass that converted the last row IS complete — `scanned == 0` alone would
    # have denied it, and then claimed it two hours later out of pure idleness.
    busy = _one_image_conn()
    monkeypatch.setattr(remaster, "_download_bytes", lambda url: JPEG)
    assert _run(busy, _R2()).complete is True

    # The row that a throttle deferred is invisible to the pass's own 20-hour
    # rail but NOT to the probe: work is outstanding, so nothing is stamped.
    throttled = _one_image_conn()
    monkeypatch.setattr(remaster, "_download_bytes", _boom(403))
    assert _run(throttled, _R2(), remaster._DetailResolver(_Client({}))).complete is False


def test_a_bounded_stop_never_reports_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _anomalous_conn(5)
    monkeypatch.setattr(remaster, "_download_bytes", lambda url: JPEG)
    monkeypatch.setattr(remaster, "_IMAGE_BATCH", 2)

    stats = _run(conn, _R2(), max_images=3)

    assert stats.stopped == "max_images" and stats.complete is False


def test_a_spent_deadline_stops_the_listing_walk_itself() -> None:
    """The budget has to bite on the page loop: a late pass walks millions of
    listings that carry no pending row at all, and never enters the batch loop."""
    conn = _Conn(listings=[(101, 55501, True)], images=[])

    stats = _run(conn, _R2(), deadline=time.monotonic() - 10_000)

    assert stats.stopped == "deadline" and stats.complete is False
    assert len([sql for sql, _ in conn.statements if "FROM listings" in sql]) == 1


def test_the_completion_probe_ignores_the_rail_and_the_keys_we_refuse() -> None:
    probe = remaster._PENDING_REMAINS_SQL
    # "the pass saw nothing" is also what a shard looks like two hours after it
    # deferred its last rows — so the probe must not carry the 20-hour rail.
    assert "last_download_attempt_at" not in probe
    pattern = re.search(r"storage_path ~ '([^']+)'", probe)
    assert pattern is not None
    compiled = re.compile(pattern.group(1))
    for key in ("101/0003.jpg", "-9/0001.jpg", "sreality/101/3.jpeg", "101/3.jpg", "101/0003.png"):
        assert bool(compiled.match(key)) is bool(remaster._KEY_RE.match(key))


def test_each_shard_stamps_its_own_sub_key_under_one_row() -> None:
    conn = _Conn()
    remaster.stamp_complete(conn, remaster._Stats(shard="3/6", elapsed_s=12.5))

    sql, params = conn.statements[-1]
    assert params == {"shard": "3/6", "elapsed_s": 12.5}
    # Merged, never replaced: six shards finish at different times and the first
    # one home must not read as "the lane is done".
    assert "COALESCE(app_settings.value, '{}'::jsonb) || excluded.value" in sql
    assert "jsonb_build_object( %(shard)s::text," in sql


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
    # No shard-denominator input: an N that disagreed with the static matrix would
    # either red the surplus jobs (argparse) or silently leave the tail unwalked.
    assert "shards" not in inputs


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


def test_the_shard_denominator_is_the_matrix_length() -> None:
    job = _workflow()["jobs"]["remaster"]
    step = next(s for s in job["steps"] if "--shard" in s.get("run", ""))
    shards = len(job["strategy"]["matrix"]["shard"])

    assert "--shard ${SHARD}/%d " % shards in step["run"]
    # Spaces separate listing ids, they do not vanish: deleting them would splice
    # "101 102" into one listing that does not exist and report a clean run.
    assert "${INPUT_LISTING_IDS// /,}" in step["run"]
