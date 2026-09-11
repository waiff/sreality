"""The SPA's sreality transform must equal the scraper's, byte for byte.

Two independent copies of the CDN template exist because the SPA falls back to
the portal CDN for a photo whose bytes R2 does not hold yet. sreality's CDN is an
exact-template ALLOWLIST — a chain that drifts from the one we download through
either 400s or serves a DIFFERENT rendition than the stored bytes, which is
invisible in tests that only exercise one side.
"""

from __future__ import annotations

import re
from pathlib import Path

from scraper import image_storage

_IMAGE_URL_TS = Path(__file__).resolve().parents[1] / "frontend/src/lib/imageUrl.ts"


def _const(name: str) -> str | None:
    source = _IMAGE_URL_TS.read_text(encoding="utf-8")
    match = re.search(rf"^const {name} = '(.*)';$", source, re.M)
    return match.group(1) if match else None


def test_spa_transform_ops_match_the_scraper():
    ops = _const("SREALITY_TRANSFORM_OPS")
    assert ops is not None, "did the constant move? imageUrl.ts no longer declares it"
    assert ops == image_storage.IMAGE_TRANSFORM_OPS


def test_spa_sreality_host_matches_the_scraper():
    host = _const("SREALITY_IMG_HOST")
    assert host is not None, "did the constant move? imageUrl.ts no longer declares it"
    assert host == image_storage._SREALITY_IMAGE_HOST
