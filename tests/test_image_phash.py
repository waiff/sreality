"""Tests for perceptual image hashing (scraper.image_phash)."""

from __future__ import annotations

import io

from PIL import Image

from scraper.image_phash import compute_dhash, hamming, to_signed64

_MASK64 = (1 << 64) - 1


def _gradient(w: int = 64, h: int = 64, *, flip: bool = False) -> bytes:
    im = Image.new("L", (w, h))
    data = [
        ((w - 1 - x) if flip else x) * 255 // (w - 1)
        for _ in range(h)
        for x in range(w)
    ]
    im.putdata(data)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_dhash_is_64_bit_and_deterministic():
    g = _gradient()
    h1 = compute_dhash(g)
    assert 0 <= h1 <= _MASK64
    assert compute_dhash(g) == h1


def test_identical_images_zero_hamming():
    g = _gradient()
    assert hamming(compute_dhash(g), compute_dhash(g)) == 0


def test_mirror_image_has_large_hamming():
    a = compute_dhash(_gradient(flip=False))
    b = compute_dhash(_gradient(flip=True))
    assert hamming(a, b) > 10


def test_to_signed64_preserves_bit_pattern():
    v = (1 << 63) | 0x1234  # top bit set -> must map into signed range
    s = to_signed64(v)
    assert s < 0
    assert (s & _MASK64) == v
    # XOR/Hamming is sign-agnostic, so a stored (signed) hash compares equal
    # to its unsigned form — the invariant the dedup SQL's bit_count relies on.
    assert hamming(s, v) == 0


def test_to_signed64_noop_below_sign_bit():
    v = 0x1234
    assert to_signed64(v) == v
