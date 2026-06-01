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

from collections import deque
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
    assert "(i.id %% %s) = %s" in sql
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
    assert "(i.id %% %s) = %s" in sql
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
    seqs = captured["params"][2::3]  # every 3rd flat value is the sequence
    assert seqs.count(1) == 1   # deduped
    assert seqs.count(2) == 1
    assert seqs.count(None) == 2  # nulls preserved
    assert "//a/1.jpg" in captured["params"]       # first of the dup kept
    assert "//a/1b.jpg" not in captured["params"]  # second dropped
