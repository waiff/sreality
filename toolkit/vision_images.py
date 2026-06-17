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
import logging
from typing import Any

LOG = logging.getLogger("vision_images")

# Anthropic resizes every vision input down to <=1568px long edge AND <=~1.15 MP,
# then bills tokens on the RESIZED size (~1.6k tokens at the cap). Two consequences
# drive the two constants below:
#   - Sending anything ABOVE the cap costs the SAME tokens (only wasted upload) and
#     risks the 200k prompt-assembly limit. So document/diagram reads use 1568px:
#     the model sees the same pixels it would have anyway, with no upload waste.
#   - The token bill only DROPS once the image is BELOW the cap. 768px (~0.4 MP) is
#     ~1/3 the vision tokens of the cap, which is the real cost saving — used for
#     photo comparison/classification, where sub-megapixel detail is ample.
COMPARISON_MAX_EDGE = 768   # photo comparison / room classification — the cost lever
DOCUMENT_MAX_EDGE = 1568    # site plans / condition / diagrams — Anthropic's own cap, quality-neutral

# The default favours the cheap path; document readers pass DOCUMENT_MAX_EDGE explicitly.
DEFAULT_MAX_EDGE = COMPARISON_MAX_EDGE


def downscale_jpeg(data: bytes, max_edge: int = DEFAULT_MAX_EDGE) -> bytes:
    """Shrink an image so its long edge <= max_edge, re-encode JPEG. Never upsizes.

    Returns the original bytes if Pillow can't decode them, so a bad image
    degrades to "send as-is" rather than dropping the listing from the run.
    """
    try:
        from PIL import Image
    except Exception as exc:
        LOG.warning("downscale: PIL import failed (sending full-res): %r", exc)
        return data
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((max_edge, max_edge))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=80)
            return buf.getvalue()
    except Exception as exc:
        LOG.warning("downscale: resize failed (%d bytes; sending full-res): %r", len(data), exc)
        return data


def image_block(r2: Any, storage_path: str, max_edge: int = DEFAULT_MAX_EDGE) -> dict[str, Any]:
    """Download one R2 image, downscale, and wrap it as an Anthropic image block."""
    raw = r2.download_bytes(storage_path)
    data = downscale_jpeg(raw, max_edge)
    LOG.info("image_block %s raw=%dB out=%dB max_edge=%d", storage_path, len(raw), len(data), max_edge)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }
