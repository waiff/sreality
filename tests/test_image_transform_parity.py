"""The SPA's sreality transform must equal the scraper's, byte for byte.

Two independent copies of the CDN normaliser exist because the SPA falls back to
the portal CDN for a photo whose bytes R2 does not hold yet. sreality's CDN is an
exact-template ALLOWLIST — a chain that drifts from the one we download through
either 400s or serves a DIFFERENT rendition than the stored bytes, which is
invisible in tests that only exercise one side.

Three rails, because pinning the template string alone would not have caught a
drift in the op-head allowlist or in the query rebuild:
  * both string constants (template, host),
  * the set of op heads a stored chain may keep,
  * the shared probe vectors in `fixtures/sreality_transform_probes.json`, which
    `frontend/src/lib/imageUrl.test.ts` runs against the TS copy of this logic.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from scraper import image_storage

_REPO = Path(__file__).resolve().parents[1]
_IMAGE_URL_TS = _REPO / "frontend/src/lib/imageUrl.ts"
PROBES = _REPO / "tests/fixtures/sreality_transform_probes.json"


def _const(name: str) -> str | None:
    source = _IMAGE_URL_TS.read_text(encoding="utf-8")
    match = re.search(rf"^const {name} = '(.*)';$", source, re.M)
    return match.group(1) if match else None


def _string_set(name: str) -> set[str] | None:
    """Read a `const NAME = new Set(['a', 'b']);` literal out of the TS module."""
    source = _IMAGE_URL_TS.read_text(encoding="utf-8")
    match = re.search(rf"^const {name} = new Set\(\[(.*?)\]\);$", source, re.M | re.S)
    if match is None:
        return None
    return set(re.findall(r"'([^']*)'", match.group(1)))


def test_spa_transform_ops_match_the_scraper():
    ops = _const("SREALITY_TRANSFORM_OPS")
    assert ops is not None, "did the constant move? imageUrl.ts no longer declares it"
    assert ops == image_storage.IMAGE_TRANSFORM_OPS


def test_spa_sreality_host_matches_the_scraper():
    host = _const("SREALITY_IMG_HOST")
    assert host is not None, "did the constant move? imageUrl.ts no longer declares it"
    assert host == image_storage._SREALITY_IMAGE_HOST


def test_spa_preserved_op_heads_match_the_scraper():
    heads = _string_set("PRESERVED_OP_HEADS")
    assert heads is not None, "did the set move? imageUrl.ts no longer declares it"
    assert heads == set(image_storage._PRESERVED_OP_HEADS)


def test_scraper_matches_every_shared_probe_vector():
    probes = json.loads(PROBES.read_text(encoding="utf-8"))
    assert probes["ops"] == image_storage.IMAGE_TRANSFORM_OPS
    assert probes["probes"], "the shared contract must not be empty"
    for probe in probes["probes"]:
        assert image_storage.with_transform(probe["input"]) == probe["expected"], (
            probe["why"]
        )
