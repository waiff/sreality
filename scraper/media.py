"""Classify scraped media URLs (image vs video) and validate downloaded bytes.

One shared home for the image/video distinction so every portal's ingest funnels
through the same rule — no per-parser video filters to keep in sync — and so the
image-download path can reject anything that isn't actually an image. Pure +
stdlib-only: no DB, no network, no third-party deps.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

# Reject-list, NOT accept-list. Portals serve photos at extensionless URLs
# (sreality / bezrealitky / mmreality JSON galleries) and at non-jpg extensions
# (sreality `.mpo` multi-picture JPEG), so allow-listing image extensions would
# silently drop legitimate photos. We only deny what is provably non-photographic;
# the byte-level `is_image_bytes` sniff backs this up for anything that slips by.
_VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".mov", ".webm", ".avi", ".m4v", ".mkv", ".m3u8"}
)
_VIDEO_PATH_TOKENS: tuple[str, ...] = ("/video/",)

# Cap on a single downloaded image. Legit listing photos are well under ~10 MB
# even at the largest portal transform; the iDNES video tours that motivated this
# were 24-119 MB. Anything larger is treated as not-an-image.
MAX_IMAGE_BYTES: int = 15 * 1024 * 1024


def is_image_url(url: str) -> bool:
    """True unless the URL is recognisably a video (or other non-image media)."""
    if not url:
        return False
    clean = url.split("?", 1)[0].split("#", 1)[0].lower()
    if any(token in clean for token in _VIDEO_PATH_TOKENS):
        return False
    _, ext = os.path.splitext(clean)
    return ext not in _VIDEO_EXTENSIONS


def split_media_rows(urls: Iterable[str]) -> tuple[list[dict], list[dict]]:
    """Partition a portal's ordered media URLs into image rows and video rows.

    Sequence = the URL's ORIGINAL position in the gallery, preserved across the
    split — so dropping a leading video leaves a gap (sequence 0 absent) rather
    than renumbering the surviving photos. That keeps a re-scrape idempotent
    against already-stored rows (a photo keeps the same sequence run after run, so
    the `storage_path IS NULL` upsert guard is a no-op instead of inserting a
    duplicate). Empty and duplicate URLs are skipped.
    """
    image_rows: list[dict] = []
    video_rows: list[dict] = []
    seen: set[str] = set()
    for seq, url in enumerate(urls):
        if not url or url in seen:
            continue
        seen.add(url)
        target = image_rows if is_image_url(url) else video_rows
        target.append({"url": url, "sequence": seq})
    return image_rows, video_rows


def is_image_bytes(data: bytes) -> str | None:
    """Return the image content-type if `data` starts with a known image magic
    number, else None.

    stdlib-only (imghdr was removed in Python 3.13). Covers the four formats the
    portals actually serve; MP4/HTML/PDF/etc. match none and return None, so the
    caller rejects them before they are stored as a photo.
    """
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"  # also matches .mpo multi-picture JPEG
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None
