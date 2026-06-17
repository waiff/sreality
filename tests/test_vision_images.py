"""Tests for the vision image downscaler (toolkit.vision_images)."""

from __future__ import annotations

import base64
import io

import pytest

from toolkit.vision_images import (
    COMPARISON_MAX_EDGE,
    DEFAULT_MAX_EDGE,
    DOCUMENT_MAX_EDGE,
    downscale_jpeg,
    image_block,
)

Image = pytest.importorskip("PIL.Image")


def test_semantic_tiers() -> None:
    # The cost lever (sub-megapixel) vs Anthropic's own resize cap (quality-neutral).
    assert COMPARISON_MAX_EDGE == 768
    assert DOCUMENT_MAX_EDGE == 1568
    assert COMPARISON_MAX_EDGE < DOCUMENT_MAX_EDGE
    # The default favours the cheap path, so a forgetful caller errs cheap, not costly.
    assert DEFAULT_MAX_EDGE == COMPARISON_MAX_EDGE


def _jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (123, 200, 80)).save(buf, format="JPEG")
    return buf.getvalue()


def _dims(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as im:
        return im.size


def test_downscale_shrinks_oversized_image() -> None:
    big = _jpeg(4000, 3000)
    out = downscale_jpeg(big, max_edge=1024)
    w, h = _dims(out)
    assert max(w, h) <= 1024
    assert (w, h) == (1024, 768)          # aspect ratio preserved
    assert len(out) < len(big)            # genuinely smaller payload


def test_downscale_never_upsizes_small_image() -> None:
    small = _jpeg(500, 400)
    w, h = _dims(downscale_jpeg(small, max_edge=1024))
    assert (w, h) == (500, 400)           # thumbnail only shrinks


def test_downscale_returns_original_on_undecodable_bytes() -> None:
    junk = b"this is not an image"
    assert downscale_jpeg(junk) == junk   # graceful fallback, never raises


def test_image_block_downscales_and_encodes() -> None:
    big = _jpeg(4000, 3000)

    class _FakeR2:
        def download_bytes(self, key: str) -> bytes:
            return big

    block = image_block(_FakeR2(), "some/key.jpg", max_edge=1024)
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/jpeg"
    decoded = base64.standard_b64decode(block["source"]["data"])
    assert max(_dims(decoded)) <= 1024


def test_image_block_default_is_comparison_tier() -> None:
    big = _jpeg(4000, 3000)

    class _FakeR2:
        def download_bytes(self, key: str) -> bytes:
            return big

    decoded = base64.standard_b64decode(
        image_block(_FakeR2(), "k.jpg")["source"]["data"]
    )
    assert max(_dims(decoded)) <= COMPARISON_MAX_EDGE
