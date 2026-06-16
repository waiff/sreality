"""Downscale + base64-encode listing images for Claude vision payloads.

Portal originals are full-resolution (~4000x3000), which blows the 200k-token
prompt limit when several are packed into one classify / compare call (the dedup
engine's classification of image-heavy listings failed with HTTP 400 "prompt is
too long"). Room classification and forensic room comparison need nothing near
full-res, so we thumbnail to a bounded long edge before encoding. Pillow is
already a dependency (scraper/image_phash.py).
"""

from __future__ import annotations

import base64
import io
from typing import Any

# Anthropic resizes vision inputs to <=1568px long edge / ~1.15 MP and counts
# ~1600 tokens at that size; bounding the long edge here keeps a 12-image classify
# call near ~19k tokens instead of ~210k.
DEFAULT_MAX_EDGE = 1568


def downscale_jpeg(data: bytes, max_edge: int = DEFAULT_MAX_EDGE) -> bytes:
    """Shrink an image so its long edge <= max_edge, re-encode JPEG. Never upsizes.

    Returns the original bytes if Pillow can't decode them, so a bad image
    degrades to "send as-is" rather than dropping the listing from the run.
    """
    try:
        from PIL import Image
    except Exception:
        return data
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((max_edge, max_edge))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=80)
            return buf.getvalue()
    except Exception:
        return data


def image_block(r2: Any, storage_path: str, max_edge: int = DEFAULT_MAX_EDGE) -> dict[str, Any]:
    """Download one R2 image, downscale, and wrap it as an Anthropic image block."""
    data = downscale_jpeg(r2.download_bytes(storage_path), max_edge)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }
