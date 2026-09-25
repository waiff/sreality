"""The photo fingerprint is taken at download time, from the bytes in hand.

End-to-end over the REAL pieces (real Pillow, real `_phash_or_none`, real
`_fetch_one_image`, the real `mark_image_stored` SQL and the real backstop
selection), with only the network, R2 and Postgres faked:

  1. the download-time hash is bit-identical to what the hourly backstop
     (`scripts/compute_image_phash.py`) would compute for the same bytes;
  2. it lands in the SAME `UPDATE images` that records `storage_path`;
  3. undecodable bytes still store — phash NULL, counted, warned once;
  4. the backstop's selection skips a row hashed at download and picks up a miss.

Only `images` is written; nothing here touches `listings` (rule #2).
"""

from __future__ import annotations

import io
import logging
import sqlite3
from contextlib import contextmanager
from typing import Any

import pytest

PIL_Image = pytest.importorskip("PIL.Image")

from scraper import db as scraper_db  # noqa: E402
from scraper import main as scraper_main  # noqa: E402
from scraper.image_phash import compute_dhash, to_signed64  # noqa: E402
from scripts.compute_image_phash import _SELECT_SQL, _hash_one  # noqa: E402

URL = "https://www.bazos.cz/img/1/1/1.jpg"
# Passes the magic-number gate as a JPEG but no decoder can open it.
UNDECODABLE = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _jpeg_bytes(shade: int = 0) -> bytes:
    img = PIL_Image.new("RGB", (48, 36))
    img.putdata([((x * 5 + shade) % 256, (y * 7) % 256, (x + y) % 256)
                 for y in range(36) for x in range(48)])
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


class _R2:
    """Upload records the bytes; download serves them back (the backstop's read)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def upload_bytes(self, key: str, data: bytes, content_type: str = "image/jpeg") -> None:
        self.objects[key] = data

    def download_bytes(self, key: str) -> bytes:
        return self.objects[key]


class _RecordingConn:
    """Fake psycopg conn: every executed statement is kept, nothing is run."""

    class _Ctx:
        def __enter__(self) -> "_RecordingConn._Ctx":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    class _Cur(_Ctx):
        def __init__(self, conn: "_RecordingConn") -> None:
            self._conn = conn

        def execute(self, sql: str, params: Any = None) -> None:
            self._conn.executed.append((" ".join(sql.split()), params))

    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def transaction(self) -> "_RecordingConn._Ctx":
        return self._Ctx()

    def cursor(self) -> "_RecordingConn._Cur":
        return self._Cur(self)


def _drive(monkeypatch: pytest.MonkeyPatch, payloads: list[bytes]) -> tuple[
    dict[str, Any], _RecordingConn, _R2
]:
    """One image run over `payloads`, with the real fetch/hash/store path."""
    conn = _RecordingConn()
    r2 = _R2()

    @contextmanager
    def _connect():
        yield conn

    urls = [f"https://www.bazos.cz/img/{i}/1.jpg" for i in range(len(payloads))]
    rows = [(i + 1, 500 + i, 0, url, "byt", "prodej", None) for i, url in enumerate(urls)]
    body = dict(zip(urls, payloads))
    script = [rows]

    monkeypatch.setattr(scraper_main.image_storage, "is_configured", lambda: True)
    monkeypatch.setattr(scraper_main.image_storage.R2Client, "from_env", lambda **kw: r2)
    monkeypatch.setattr(scraper_main.image_storage, "download_image", lambda u, **kw: body[u])
    monkeypatch.setattr(scraper_main.db, "connect", _connect)
    monkeypatch.setattr(
        scraper_main.db, "pending_image_downloads",
        lambda c, **kw: script.pop(0) if script else [],
    )
    out = scraper_main._run_image_downloads(max_downloads=0, workers=1)
    return out, conn, r2


def _stores(conn: _RecordingConn) -> list[tuple[str, Any]]:
    return [(sql, p) for sql, p in conn.executed if sql.startswith("UPDATE images SET storage_path")]


def test_download_time_hash_is_the_backstop_hash(monkeypatch) -> None:
    """Same function, same bytes, same number: a row hashed at download is
    indistinguishable from one the hourly job would have hashed."""
    data = _jpeg_bytes()
    r2 = _R2()
    monkeypatch.setattr(scraper_main.image_storage, "download_image", lambda u, **kw: data)

    key, phash, _rendition, dims, err = scraper_main._fetch_one_image(7, 0, URL, r2)

    assert err is None and dims == (48, 36)
    assert r2.objects[key] == data
    backstop_id, backstop_hash, backstop_err = _hash_one(r2, 99, key)
    assert (backstop_id, backstop_err) == (99, None)
    assert phash is not None
    assert phash == backstop_hash == to_signed64(compute_dhash(data))


def test_download_writes_the_hash_in_the_storage_path_update(monkeypatch) -> None:
    data = [_jpeg_bytes(0), _jpeg_bytes(90)]
    out, conn, r2 = _drive(monkeypatch, data)

    stores = _stores(conn)
    assert len(stores) == 2
    for sql, params in stores:
        assert "phash = COALESCE(%s, phash)" in sql
        storage_path, phash = params[0], params[1]
        assert r2.objects[storage_path] in data
        assert phash == to_signed64(compute_dhash(r2.objects[storage_path]))
    # Only `images` was written — no listings statement on this path (rule #2).
    assert all(sql.startswith("UPDATE images") for sql, _ in conn.executed)
    assert out["images_stored"] == 2
    assert out["images_phash_missed"] == 0


def test_undecodable_bytes_still_store_with_null_hash(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO, logger="scraper")
    out, conn, r2 = _drive(monkeypatch, [UNDECODABLE, _jpeg_bytes(), UNDECODABLE])

    stores = _stores(conn)
    assert len(stores) == 3  # a hashing failure never fails the download
    assert len(r2.objects) == 3
    phashes = sorted((p[1] is None) for _sql, p in stores)
    assert phashes == [False, True, True]
    assert out["images_stored"] == 3
    assert out["images_phash_missed"] == 2

    warned = [r for r in caplog.records if "phash_inline_failed" in r.getMessage()]
    assert len(warned) == 1  # once per exception kind per run, not once per image
    assert warned[0].levelno == logging.WARNING
    done = [r.getMessage() for r in caplog.records if r.getMessage().startswith("IMAGES done")]
    assert done and done[-1].endswith("phash_missed=2")


def test_each_run_names_the_failure_again(monkeypatch, caplog) -> None:
    """The once-per-kind latch resets per run, so every Actions run and every
    worker pass that hits a systemic failure says so."""
    caplog.set_level(logging.WARNING, logger="scraper")
    _drive(monkeypatch, [UNDECODABLE])
    _drive(monkeypatch, [UNDECODABLE])
    warned = [r for r in caplog.records if "phash_inline_failed" in r.getMessage()]
    assert len(warned) == 2


# ---- the backstop treats a download-time hash as done ----------------------


def _sqlite_images() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.create_function("now", 0, lambda: "2026-09-24T12:00:00Z")
    conn.execute("CREATE TABLE listings (sreality_id INTEGER PRIMARY KEY, is_active BOOLEAN)")
    conn.execute(
        "CREATE TABLE images (id INTEGER PRIMARY KEY, sreality_id INTEGER, "
        "storage_path TEXT, phash INTEGER, last_download_attempt_at TEXT, "
        "download_attempts INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("INSERT INTO listings VALUES (1, TRUE)")
    conn.execute(
        "INSERT INTO images (id, sreality_id) VALUES (10, 1), (11, 1), (12, 1)"
    )
    return conn


def _store(conn: sqlite3.Connection, image_id: int, key: str, phash: int | None) -> None:
    """Run the REAL mark_image_stored statement (psycopg placeholders -> sqlite)."""
    sql = scraper_db._MARK_IMAGE_STORED_SQL.format(extra="").replace("%s", "?")
    conn.execute(sql, (key, phash, image_id))


def _backstop_pending(conn: sqlite3.Connection) -> list[int]:
    sql = _SELECT_SQL.replace("%(limit)s", ":limit")
    return [row[0] for row in conn.execute(sql, {"limit": 100})]


def test_backstop_skips_rows_hashed_at_download() -> None:
    conn = _sqlite_images()
    _store(conn, 10, "500/0000.jpg", to_signed64(compute_dhash(_jpeg_bytes())))
    _store(conn, 11, "501/0000.jpg", None)  # inline miss -> the backstop's work
    # 12 is not downloaded yet: nothing to hash, not the backstop's either.
    assert _backstop_pending(conn) == [11]


def test_a_null_inline_hash_never_erases_an_existing_one() -> None:
    conn = _sqlite_images()
    conn.execute("UPDATE images SET phash = -7 WHERE id = 10")
    _store(conn, 10, "500/0000.jpg", None)
    assert conn.execute("SELECT phash, download_attempts FROM images WHERE id = 10").fetchone() == (-7, 1)
    assert 10 not in _backstop_pending(conn)
