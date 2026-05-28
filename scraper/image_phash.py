"""Perceptual image hashing for cross-source dedup (multi-portal PR5).

dHash (difference hash): resize to 9x8 grayscale, compare horizontally adjacent
pixels -> 64 bits. Pillow-only (no numpy/scipy). Robust to scale / compression /
minor colour shifts — the near-duplicate case when the same listing's photos are
reused across portals. The hash is stored in `images.phash` as a signed bigint;
Hamming distance is `bit_count(a # b)` in Postgres.
"""

from __future__ import annotations

import io

from PIL import Image

_W = 9
_H = 8
_MASK64 = (1 << 64) - 1


def compute_dhash(image_bytes: bytes) -> int:
    """64-bit unsigned dHash of one image."""
    img = Image.open(io.BytesIO(image_bytes)).convert("L").resize(
        (_W, _H), Image.Resampling.LANCZOS,
    )
    px = list(img.getdata())
    bits = 0
    for row in range(_H):
        base = row * _W
        for col in range(_W - 1):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return bits


def to_signed64(value: int) -> int:
    """Map a 64-bit unsigned hash into Postgres bigint's signed range.

    The bit pattern is preserved, so `bit_count(a # b)` over two stored values
    still returns the Hamming distance regardless of sign.
    """
    value &= _MASK64
    return value - (1 << 64) if value >= (1 << 63) else value


def hamming(a: int, b: int) -> int:
    """Hamming distance between two 64-bit hashes (sign-agnostic)."""
    return bin((a ^ b) & _MASK64).count("1")
