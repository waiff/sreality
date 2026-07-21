"""Hermetic tests for the image-download phase in scraper/main.py.

Three concerns:
  1. `_classify_image_failure` correctly buckets 404/410 → freshness
     check → ('taken_down' | 'transient'), and other errors → transient.
     The per-run gone/alive caches prevent repeat freshness calls.
  2. `_suspicious_stop` only fires when the window is full AND the
     transient ratio exceeds the threshold.
  3. `pending_image_downloads` SQL respects the new
     `unavailable_reason IS NULL` exclusion and the active_only
     ordering knob.
"""

from __future__ import annotations

import threading
from collections import deque
from contextlib import contextmanager
from typing import Any

import pytest
import requests

from scraper import db as scraper_db
from scraper import main as scraper_main


# ---- _classify_image_failure -------------------------------------------


def _http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    err = requests.HTTPError(response=resp)
    return err


def test_classify_non_404_is_transient(monkeypatch):
    """5xx, timeout, R2 failures, connection resets — never trigger a
    freshness check; the listing might still be perfectly alive."""
    called = []
    monkeypatch.setattr(
        scraper_main, "client_freshness_check",
        lambda *_a, **_kw: (called.append(1), "alive_should_not_be_called")[1],
    )
    gone: set[int] = set()
    alive: set[int] = set()

    kind = scraper_main._classify_image_failure(
        conn=None, client=None, sreality_id=42,
        error=_http_error(500),
        gone_listings=gone, alive_listings=alive,
    )
    assert kind == "transient"
    assert called == []  # no freshness check fired


def test_classify_401_is_source_unavailable(monkeypatch):
    """A 401 is the bare-URL/rotated-path signature — a dead URL, not a block.
    Classified source_unavailable directly, with no freshness check (we know the
    URL is the problem), so a pocket of 401s never trips the suspicious-stop."""
    called = []
    monkeypatch.setattr(
        scraper_main, "client_freshness_check",
        lambda *_a, **_kw: (called.append(1), "should_not_be_called")[1],
    )
    gone: set[int] = set()
    alive: set[int] = set()

    kind = scraper_main._classify_image_failure(
        conn=None, client=None, sreality_id=42,
        error=_http_error(401),
        gone_listings=gone, alive_listings=alive,
    )
    assert kind == "source_unavailable"
    assert called == []  # no freshness check fired
    assert gone == set() and alive == set()  # not a listing-liveness verdict


@pytest.mark.parametrize("status", [400, 415])
def test_classify_400_and_415_are_source_unavailable(monkeypatch, status):
    """400 (malformed prefix transform chain, e.g. '?fl=rot,180,0|') and 415
    are URL-level rejections — permanently dead, never transient. Classifying
    them transient is what red-looped the image workflows on the breaker."""
    called = []
    monkeypatch.setattr(
        scraper_main, "client_freshness_check",
        lambda *_a, **_kw: (called.append(1), "should_not_be_called")[1],
    )
    gone: set[int] = set()
    alive: set[int] = set()

    kind = scraper_main._classify_image_failure(
        conn=None, client=None, sreality_id=42,
        error=_http_error(status),
        gone_listings=gone, alive_listings=alive,
    )
    assert kind == "source_unavailable"
    assert called == []  # no freshness check fired
    assert gone == set() and alive == set()  # not a listing-liveness verdict


def test_classify_403_stays_transient(monkeypatch):
    """403 is sreality throttling us, not a dead URL. It MUST stay transient:
    a row parked on an inactive listing is never un-parked (record_images only
    resets unavailable_reason on a parent detail refetch)."""
    called = []
    monkeypatch.setattr(
        scraper_main, "client_freshness_check",
        lambda *_a, **_kw: (called.append(1), "should_not_be_called")[1],
    )
    kind = scraper_main._classify_image_failure(
        conn=None, client=None, sreality_id=42,
        error=_http_error(403),
        gone_listings=set(), alive_listings=set(),
    )
    assert kind == "transient"
    assert called == []  # no freshness check fired


def test_suspicious_stop_source_unavailable_does_not_count():
    """A window of all-401 (source_unavailable) outcomes must not fire the
    breaker — dead URLs are not sreality blocking us."""
    window: deque[str] = deque(maxlen=scraper_main.SUSPICIOUS_STOP_WINDOW)
    for _ in range(scraper_main.SUSPICIOUS_STOP_WINDOW):
        window.append("source_unavailable")
    assert scraper_main._suspicious_stop(window) is False


def test_classify_404_gone_marks_taken_down(monkeypatch):
    monkeypatch.setattr(
        scraper_main, "client_freshness_check",
        lambda *_a, **_kw: "gone",
    )
    gone: set[int] = set()
    alive: set[int] = set()

    kind = scraper_main._classify_image_failure(
        conn=None, client=None, sreality_id=42,
        error=_http_error(404),
        gone_listings=gone, alive_listings=alive,
    )
    assert kind == "taken_down"
    assert gone == {42}
    assert alive == set()


def test_classify_404_alive_is_source_unavailable(monkeypatch):
    """Image URL 404s but the listing's detail still returns 200 — that one
    CDN URL has expired (permanently dead), not a taken-down listing and not
    a transient failure. Mark the image unavailable; don't retry it forever."""
    monkeypatch.setattr(
        scraper_main, "client_freshness_check",
        lambda *_a, **_kw: "unchanged",
    )
    gone: set[int] = set()
    alive: set[int] = set()

    kind = scraper_main._classify_image_failure(
        conn=None, client=None, sreality_id=42,
        error=_http_error(404),
        gone_listings=gone, alive_listings=alive,
    )
    assert kind == "source_unavailable"
    assert gone == set()
    assert alive == {42}


def test_classify_404_alive_cached_is_source_unavailable(monkeypatch):
    """A listing already cached alive: a 404 image is source_unavailable
    (dead URL), but a non-404 error stays transient."""
    called = []
    monkeypatch.setattr(
        scraper_main, "client_freshness_check",
        lambda *_a, **_kw: (called.append(1), "unchanged")[1],
    )
    gone: set[int] = set()
    alive: set[int] = {42}

    assert scraper_main._classify_image_failure(
        conn=None, client=None, sreality_id=42, error=_http_error(404),
        gone_listings=gone, alive_listings=alive,
    ) == "source_unavailable"
    assert scraper_main._classify_image_failure(
        conn=None, client=None, sreality_id=42, error=_http_error(500),
        gone_listings=gone, alive_listings=alive,
    ) == "transient"
    assert called == []  # cache short-circuits; no freshness call


def test_classify_per_run_cache_short_circuits(monkeypatch):
    """Once a listing is known gone OR alive, no further freshness
    calls fire for that listing within this run."""
    call_count = [0]

    def _spy(*_a, **_kw):
        call_count[0] += 1
        return "gone"

    monkeypatch.setattr(scraper_main, "client_freshness_check", _spy)
    gone = {42}  # already cached
    alive: set[int] = set()

    kind = scraper_main._classify_image_failure(
        conn=None, client=None, sreality_id=42,
        error=_http_error(404),
        gone_listings=gone, alive_listings=alive,
    )
    assert kind == "taken_down"
    assert call_count[0] == 0  # never called


def test_classify_freshness_failure_falls_back_to_transient(monkeypatch):
    """If the freshness check itself raises (network blip, etc.) we
    treat the image failure as transient — never blanket-classify a
    listing as taken-down on incomplete evidence."""
    def _boom(*_a, **_kw):
        raise RuntimeError("freshness check crashed")

    monkeypatch.setattr(scraper_main, "client_freshness_check", _boom)
    gone: set[int] = set()
    alive: set[int] = set()

    kind = scraper_main._classify_image_failure(
        conn=None, client=None, sreality_id=42,
        error=_http_error(404),
        gone_listings=gone, alive_listings=alive,
    )
    assert kind == "transient"
    assert gone == set()
    assert alive == set()  # not cached — we couldn't determine


# ---- _suspicious_stop -------------------------------------------------------


def test_suspicious_stop_requires_full_window():
    """A handful of transient failures at run-start must not fire the
    stop — only a sustained pattern across a full window."""
    window: deque[str] = deque(
        ["transient"] * 50, maxlen=scraper_main.SUSPICIOUS_STOP_WINDOW
    )
    assert scraper_main._suspicious_stop(window) is False


def test_suspicious_stop_below_threshold():
    """29% transient over a full window — under threshold, no stop."""
    window: deque[str] = deque(maxlen=scraper_main.SUSPICIOUS_STOP_WINDOW)
    for _ in range(29):
        window.append("transient")
    for _ in range(scraper_main.SUSPICIOUS_STOP_WINDOW - 29):
        window.append("ok")
    assert scraper_main._suspicious_stop(window) is False


def test_suspicious_stop_above_threshold():
    """31% transient over a full window — fires."""
    window: deque[str] = deque(maxlen=scraper_main.SUSPICIOUS_STOP_WINDOW)
    for _ in range(31):
        window.append("transient")
    for _ in range(scraper_main.SUSPICIOUS_STOP_WINDOW - 31):
        window.append("ok")
    assert scraper_main._suspicious_stop(window) is True


def test_suspicious_stop_taken_down_does_not_count():
    """A run that uncovers many taken-down listings (expected on the
    first backfill pass) must not look like sreality is blocking us."""
    window: deque[str] = deque(maxlen=scraper_main.SUSPICIOUS_STOP_WINDOW)
    # 80 taken_down, 20 ok — 0 transient, must not fire.
    for _ in range(80):
        window.append("taken_down")
    for _ in range(20):
        window.append("ok")
    assert scraper_main._suspicious_stop(window) is False


# ---- pending_image_downloads SQL shape -------------------------------------


class _ScriptedCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.executed: list[tuple[str, Any]] = []
        self._rows = rows

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def __enter__(self) -> "_ScriptedCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _ScriptedConn:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.cursor_obj = _ScriptedCursor(rows)

    def cursor(self) -> _ScriptedCursor:
        return self.cursor_obj


def test_pending_image_downloads_excludes_unavailable_and_given_up():
    conn = _ScriptedConn([])
    scraper_db.pending_image_downloads(conn, limit=500)
    sql, params = conn.cursor_obj.executed[0]
    assert "storage_path IS NULL" in sql
    assert "unavailable_reason IS NULL" in sql
    assert "download_attempts < %s" in sql
    assert params == (5, 500)


def test_pending_image_downloads_active_only_filters_and_orders():
    conn = _ScriptedConn([])
    scraper_db.pending_image_downloads(conn, limit=500, active_only=True)
    sql, _params = conn.cursor_obj.executed[0]
    assert "l.is_active = true" in sql
    # active_only path: pure newest-first within the active slice.
    assert "ORDER BY i.id DESC" in sql


def test_pending_image_downloads_default_orders_active_first():
    conn = _ScriptedConn([])
    scraper_db.pending_image_downloads(conn, limit=500)
    sql, _params = conn.cursor_obj.executed[0]
    # Default path tier-orders active before inactive, newest first.
    assert "(l.is_active IS TRUE) DESC NULLS LAST, i.id DESC" in sql


def test_pending_image_downloads_shard_appends_modulo():
    conn = _ScriptedConn([])
    scraper_db.pending_image_downloads(conn, limit=500, shard=(2, 4))
    sql, params = conn.cursor_obj.executed[0]
    assert "(hashint8(i.listing_id) & 2147483647) %% %s = %s" in sql
    # max_attempts, then shard (n, k), then limit — modulus N before remainder K.
    assert params == (5, 4, 2, 500)


def test_pending_image_downloads_sources_filter():
    conn = _ScriptedConn([])
    scraper_db.pending_image_downloads(conn, limit=500, sources=("idnes", "bazos"))
    sql, params = conn.cursor_obj.executed[0]
    assert "l.source = ANY(%s)" in sql
    assert params == (5, ["idnes", "bazos"], 500)


def test_pending_image_downloads_shard_and_sources_and_active():
    conn = _ScriptedConn([])
    scraper_db.pending_image_downloads(
        conn, limit=500, active_only=True, shard=(0, 3), sources=("idnes",)
    )
    sql, params = conn.cursor_obj.executed[0]
    assert "l.is_active = true" in sql
    assert "l.source = ANY(%s)" in sql
    assert "(hashint8(i.listing_id) & 2147483647) %% %s = %s" in sql
    # Order of bound params: max_attempts, sources, shard(n, k), limit.
    assert params == (5, ["idnes"], 3, 0, 500)


# ---- _run_image_downloads gates --------------------------------------------


def test_run_image_downloads_no_op_when_r2_unset(monkeypatch, caplog):
    monkeypatch.setattr(scraper_main.image_storage, "is_configured", lambda: False)
    with caplog.at_level("INFO"):
        out = scraper_main._run_image_downloads(max_downloads=1000, workers=8)
    assert out == {"images_stored": 0, "by_category": {}, "stopped_suspicious": False}
    assert any("IMAGES skipped" in m for m in caplog.messages)


def test_record_images_dedupes_duplicate_sequence():
    """sreality occasionally returns two images sharing one `order`. With
    ON CONFLICT DO UPDATE, a single INSERT proposing the same (sreality_id,
    sequence) twice raises CardinalityViolation. record_images must de-dupe
    non-null sequences in the batch first, while keeping NULL sequences (which
    don't conflict — NULLs are distinct in the unique index)."""
    captured: dict[str, Any] = {}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql: str, params: Any) -> None:
            captured["params"] = params
        def fetchall(self): return [(True,)]

    class _Conn:
        def cursor(self): return _Cur()
        def transaction(self):
            from contextlib import nullcontext
            return nullcontext()

    imgs = [
        {"url": "//a/1.jpg", "sequence": 1},
        {"url": "//a/1b.jpg", "sequence": 1},   # duplicate non-null sequence
        {"url": "//a/2.jpg", "sequence": 2},
        {"url": "//a/n.jpg", "sequence": None},
        {"url": "//a/n2.jpg", "sequence": None},  # two nulls: both kept
    ]
    scraper_db.record_images(_Conn(), 999, imgs)
    # 4 flat values per row: (sreality_id, sreality_id-for-listing_id, url, sequence)
    seqs = captured["params"][3::4]
    assert seqs.count(1) == 1   # deduped
    assert seqs.count(2) == 1
    assert seqs.count(None) == 2  # nulls preserved
    assert "//a/1.jpg" in captured["params"]       # first of the dup kept
    assert "//a/1b.jpg" not in captured["params"]  # second dropped


# ---- _image_host -----------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://img.ceskereality.cz/a/b.jpg", "img.ceskereality.cz"),
        ("https://IMG.CESKEREALITY.CZ/X", "img.ceskereality.cz"),  # lowercased
        ("//d18-a.sdn.cz/foo.jpg", "d18-a.sdn.cz"),                # protocol-relative
        ("https://host:8443/x.jpg", "host"),                      # port stripped
        ("not a url", ""),                                         # unparseable
        ("", ""),
    ],
)
def test_image_host_parses_cdn_hostname(url, expected):
    assert scraper_main._image_host(url) == expected


# ---- _fetch_one_image per-host semaphore -----------------------------------


def test_fetch_one_image_success_with_semaphore(monkeypatch):
    """A download wrapped in a per-host semaphore stores normally and releases
    the slot afterwards — and the inline pHash of the downloaded bytes rides
    the return value."""
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(
        scraper_main.image_storage, "download_image", lambda url, **kw: b"\xff\xd8\xff"
    )
    monkeypatch.setattr(scraper_main.media, "is_image_bytes", lambda data: "image/jpeg")
    monkeypatch.setattr(scraper_main, "_phash_or_none", lambda data: 42)

    class _R2:
        def upload_bytes(self, key, data, content_type="image/jpeg"):
            return None

    key, phash, err = scraper_main._fetch_one_image(7, 0, "https://h/x.jpg", _R2(), sem)
    assert err is None
    assert key == scraper_main.image_storage.image_key(7, 0)
    assert phash == 42
    assert sem.acquire(blocking=False)  # slot was released
    sem.release()


def test_fetch_one_image_releases_semaphore_on_error(monkeypatch):
    """A failed download must still release the per-host slot (the `with`
    contract) so one bad image can't permanently leak host capacity."""
    sem = threading.BoundedSemaphore(2)

    def _boom(url, **kw):
        raise requests.ConnectionError("read timed out")

    monkeypatch.setattr(scraper_main.image_storage, "download_image", _boom)
    key, phash, err = scraper_main._fetch_one_image(7, 0, "https://h/x.jpg", object(), sem)
    assert err is not None
    assert phash is None
    # Both slots are free again (none leaked).
    assert sem.acquire(blocking=False) and sem.acquire(blocking=False)
    sem.release()
    sem.release()


def test_fetch_one_image_works_without_semaphore(monkeypatch):
    """semaphore=None (cap disabled) is a valid path — no wrapping, still stores."""
    monkeypatch.setattr(
        scraper_main.image_storage, "download_image", lambda url, **kw: b"\xff\xd8\xff"
    )
    monkeypatch.setattr(scraper_main.media, "is_image_bytes", lambda data: "image/jpeg")
    monkeypatch.setattr(scraper_main, "_phash_or_none", lambda data: None)

    class _R2:
        def upload_bytes(self, key, data, content_type="image/jpeg"):
            return None

    key, phash, err = scraper_main._fetch_one_image(7, 0, "https://h/x.jpg", _R2(), None)
    assert err is None


# ---- inline pHash (Wave C-4) ------------------------------------------------


def _fake_phash_module(compute=None, signer=None):
    """Stand-in for scraper.image_phash: the real module imports Pillow at the
    top, which this sandbox lacks — CI covers the real-import path."""
    import types

    mod = types.ModuleType("scraper.image_phash")
    mod.compute_dhash = compute or (lambda data: 7)
    mod.to_signed64 = signer or (lambda value: value)
    return mod


def test_phash_or_none_threads_bytes_through_dhash_and_signer(monkeypatch):
    import sys

    seen: dict[str, object] = {}

    def compute(data):
        seen["data"] = data
        return 99

    def signer(value):
        seen["signed"] = value
        return -99

    monkeypatch.setitem(
        sys.modules, "scraper.image_phash", _fake_phash_module(compute, signer)
    )
    assert scraper_main._phash_or_none(b"bytes-in-hand") == -99
    assert seen == {"data": b"bytes-in-hand", "signed": 99}


def test_phash_or_none_swallows_dhash_failure(monkeypatch):
    import sys

    def compute(data):
        raise ValueError("cannot identify image file")

    monkeypatch.setitem(
        sys.modules, "scraper.image_phash", _fake_phash_module(compute)
    )
    assert scraper_main._phash_or_none(b"\x00garbage") is None


def test_phash_failure_never_fails_the_store(monkeypatch):
    """Undecodable bytes store with phash NULL — the fetch still returns the
    key with no error, and the hourly backfill remains the backstop."""
    import sys

    def compute(data):
        raise OSError("truncated image")

    monkeypatch.setitem(
        sys.modules, "scraper.image_phash", _fake_phash_module(compute)
    )
    monkeypatch.setattr(
        scraper_main.image_storage, "download_image", lambda url, **kw: b"\xff\xd8\xff"
    )
    monkeypatch.setattr(scraper_main.media, "is_image_bytes", lambda data: "image/jpeg")

    class _R2:
        def upload_bytes(self, key, data, content_type="image/jpeg"):
            return None

    key, phash, err = scraper_main._fetch_one_image(7, 0, "https://h/x.jpg", _R2(), None)
    assert err is None
    assert phash is None
    assert key == scraper_main.image_storage.image_key(7, 0)


class _RecordingConn:
    """Fake psycopg conn capturing executed SQL (the test_db_inactive_at shape)."""

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    class _Cur:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def execute(self, sql, params=None):
            self._conn.executed.append((" ".join(sql.split()), params))

    def __init__(self):
        self.executed = []

    def transaction(self):
        return self._Ctx()

    def cursor(self):
        return self._Cur(self)


def test_mark_image_stored_writes_phash_in_the_same_statement():
    conn = _RecordingConn()
    scraper_db.mark_image_stored(conn, 5, "7/0000.jpg", phash=-123)
    sql, params = conn.executed[0]
    assert "SET storage_path = %s" in sql
    assert "phash = COALESCE(%s, phash)" in sql
    assert params == ("7/0000.jpg", -123, 5)
    assert len(conn.executed) == 1  # one statement, not a follow-up UPDATE


def test_mark_image_stored_null_phash_preserves_existing():
    conn = _RecordingConn()
    scraper_db.mark_image_stored(conn, 5, "7/0000.jpg")
    sql, params = conn.executed[0]
    assert "phash = COALESCE(%s, phash)" in sql  # NULL input keeps a prior hash
    assert params == ("7/0000.jpg", None, 5)


# ---- _run_image_downloads per-host quarantine (loop integration) -----------


@contextmanager
def _fake_connect():
    yield object()


def _drive_image_loop(monkeypatch, batches, fetch_result):
    """Run _run_image_downloads with a scripted pending queue + fake fetcher.

    `batches` is a list of row-lists (each row: image_id, listing_id, seq, url,
    cm, ct, sreality_id)
    returned by successive `pending_image_downloads` calls; an exhausted script
    returns [] (empty queue → terminate). `fetch_result(url)` returns the
    Exception (transient) or None (success) the fake fetcher yields for that URL.
    Returns (aggregate_dict, list_of_stored_image_ids).
    """
    monkeypatch.setattr(scraper_main.image_storage, "is_configured", lambda: True)
    monkeypatch.setattr(
        scraper_main.image_storage.R2Client, "from_env", lambda **kw: object()
    )
    monkeypatch.setattr(scraper_main.db, "connect", _fake_connect)

    script = list(batches)

    def _fake_pending(conn, **kw):
        return script.pop(0) if script else []

    stored: list[int] = []
    stored_phashes: list[int | None] = []
    monkeypatch.setattr(scraper_main.db, "pending_image_downloads", _fake_pending)
    monkeypatch.setattr(
        scraper_main.db, "mark_image_stored",
        lambda conn, iid, key, phash=None: (
            stored.append(iid), stored_phashes.append(phash),
        ),
    )
    monkeypatch.setattr(
        scraper_main.db, "mark_image_attempt", lambda conn, iid, error=None: None
    )
    monkeypatch.setattr(
        scraper_main.db, "mark_image_unavailable", lambda *a, **k: None
    )
    monkeypatch.setattr(
        scraper_main.db, "mark_image_listing_taken_down", lambda conn, sid: 0
    )

    def _fake_fetch(sid, seq, url, r2, semaphore=None):
        err = fetch_result(url)
        phash = None if err is not None else 777
        return (scraper_main.image_storage.image_key(sid, seq), phash, err)

    monkeypatch.setattr(scraper_main, "_fetch_one_image", _fake_fetch)

    out = scraper_main._run_image_downloads(max_downloads=0, workers=4)
    out["_stored_phashes"] = stored_phashes
    return out, stored


def _rows(url, start_id, n, sid_base):
    """Row shape mirrors pending_image_downloads: (image_id, listing_id, seq,
    url, category_main, category_type, sreality_id) — BOTH ids, since the drain
    keys R2/shard on the surrogate but classifies taken-down on the legacy id."""
    return [
        (start_id + i, sid_base + i, 0, url, "byt", "prodej", sid_base + i)
        for i in range(n)
    ]


def _timeout_for_bad(url):
    return RuntimeError("read timed out") if "bad.cz" in url else None


def test_quarantine_localizes_one_host_run_stays_green(monkeypatch):
    """A host that crosses the transient bar is quarantined, but the healthy
    host keeps draining to the end of the queue and the run is NOT a failure
    (stopped_suspicious False) — this is the multi-portal isolation guarantee."""
    monkeypatch.setattr(scraper_main, "SUSPICIOUS_STOP_WINDOW", 4)
    bad = "https://img.bad.cz/x.jpg"
    good = "https://img.good.cz/x.jpg"
    # Batch 1: 4 bad (all time out → quarantine bad.cz) + 4 good (stored).
    # Batch 2: 4 more good (healthy work remains → keeps draining).
    batch1 = _rows(bad, 0, 4, 1000) + _rows(good, 100, 4, 2000)
    batch2 = _rows(good, 200, 4, 3000)
    out, stored = _drive_image_loop(monkeypatch, [batch1, batch2], _timeout_for_bad)

    assert out["images_stored"] == 8
    assert len(stored) == 8  # every good image stored; no bad image stored
    assert all(iid >= 100 for iid in stored)  # the bad-host ids (0..3) never stored
    assert out["stopped_suspicious"] is False  # green — one bad host doesn't fail the run
    # The inline pHash computed by the fetch worker is forwarded into the same
    # store call — no separate re-download hop.
    assert out["_stored_phashes"] == [777] * 8


def test_run_stops_suspicious_when_only_quarantined_host_remains(monkeypatch):
    """When the ONLY work left in the queue is a quarantined host, the run stops
    and flags stopped_suspicious (gates the self-chain) — and it TERMINATES
    rather than re-querying the same pending rows forever."""
    monkeypatch.setattr(scraper_main, "SUSPICIOUS_STOP_WINDOW", 4)
    bad = "https://img.bad.cz/x.jpg"
    good = "https://img.good.cz/x.jpg"
    # Batch 1: 4 bad (→ quarantine) + 2 good (stored).
    # Batch 2: the 4 bad rows re-appear (still pending) → nothing fetchable → stop.
    batch1 = _rows(bad, 0, 4, 1000) + _rows(good, 100, 2, 2000)
    batch2 = _rows(bad, 0, 4, 1000)
    out, stored = _drive_image_loop(monkeypatch, [batch1, batch2], _timeout_for_bad)

    assert len(stored) == 2  # only the good images
    assert out["stopped_suspicious"] is True
