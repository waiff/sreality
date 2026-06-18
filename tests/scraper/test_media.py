"""Tests for scraper.media (image/video URL + byte classification) and the
db.record_media routing that splits a portal's gallery into images vs videos."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from scraper import db as scraper_db
from scraper import media


def test_is_image_url_rejects_video_only():
    # Rejected: video extensions + the /video/ path token (the iDNES case).
    assert media.is_image_url("https://sta-reality2.1gr.cz/compile/thumbs/video/x.mp4") is False
    assert media.is_image_url("https://cdn/a/clip.MP4") is False
    assert media.is_image_url("https://cdn/a/tour.webm?x=1") is False
    assert media.is_image_url("https://cdn/video/x.jpg") is False  # path token wins
    assert media.is_image_url("") is False
    # Accepted: real photos, including extensionless API URLs and .mpo.
    assert media.is_image_url("https://cdn/a/1.jpg") is True
    assert media.is_image_url("https://sdn.cz/abc/no63kR.mpo") is True  # multi-pic JPEG
    assert media.is_image_url("https://api.bezrealitky.cz/img/12345") is True  # no ext
    assert media.is_image_url("https://cdn/a/2.png?fl=res,749") is True


def test_split_media_rows_preserves_original_sequence():
    urls = [
        "https://cdn/video/clip.mp4",  # 0 -> video
        "https://cdn/a/1.jpg",         # 1 -> image
        "https://cdn/a/1.jpg",         # dup -> skipped
        "",                            # empty -> skipped
        "https://cdn/a/2.jpg",         # 4 -> image
    ]
    images, videos = media.split_media_rows(urls)
    # Photos keep their ORIGINAL gallery index (gap at 0 where the video was).
    assert images == [
        {"url": "https://cdn/a/1.jpg", "sequence": 1},
        {"url": "https://cdn/a/2.jpg", "sequence": 4},
    ]
    assert videos == [{"url": "https://cdn/video/clip.mp4", "sequence": 0}]


def test_is_image_bytes_recognises_formats():
    assert media.is_image_bytes(b"\xff\xd8\xff" + b"\x00" * 12) == "image/jpeg"
    assert media.is_image_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) == "image/png"
    assert media.is_image_bytes(b"GIF89a" + b"\x00" * 8) == "image/gif"
    assert media.is_image_bytes(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4) == "image/webp"
    # An MP4 (ISO-BMFF: size box then 'ftyp') is not an image.
    assert media.is_image_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8) is None
    assert media.is_image_bytes(b"<!doctype html><html>") is None
    assert media.is_image_bytes(b"short") is None  # < 12 bytes


class _Cur:
    def __init__(self, calls: list[tuple[str, Any]]):
        self._calls = calls
        self._rows = 1

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any) -> None:
        self._calls.append((sql, params))
        # The INSERTs flatten to (sreality_id, url, sequence) triples; echo one
        # "inserted" row per triple so record_images' RETURNING count is honest.
        self._rows = len(params) // 3 if params else 1

    def fetchall(self) -> list[tuple[bool]]:
        return [(True,)] * self._rows


class _Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self.calls)

    def transaction(self):  # type: ignore[no-untyped-def]
        return nullcontext()


def test_record_media_routes_images_and_videos():
    conn = _Conn()
    urls = [
        "https://cdn/video/clip.mp4",  # 0 -> listing_videos
        "https://cdn/a/1.jpg",         # 1 -> images
        "https://cdn/a/2.jpg",         # 2 -> images
    ]
    new_images = scraper_db.record_media(conn, 42, urls)
    assert new_images == 2  # only the two photos counted as new images

    image_call = next(c for c in conn.calls if "INTO images" in c[0])
    video_call = next(c for c in conn.calls if "INTO listing_videos" in c[0])

    iparams = image_call[1]
    assert "https://cdn/a/1.jpg" in iparams and "https://cdn/a/2.jpg" in iparams
    assert "https://cdn/video/clip.mp4" not in iparams
    assert iparams[2::3] == [1, 2]  # photos keep their original sequence (gap at 0)

    vparams = video_call[1]
    assert "https://cdn/video/clip.mp4" in vparams
    assert vparams[2::3] == [0]
