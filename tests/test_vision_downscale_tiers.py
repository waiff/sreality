"""Every image->LLM comparison/analysis site routes through the shared downscaler
at the right semantic tier: photo comparison at COMPARISON_MAX_EDGE (the cost lever),
document/condition reads at DOCUMENT_MAX_EDGE (Anthropic's own cap, quality-neutral).

These pin the per-site tier without a DB/R2/LLM by recording the max_edge each
block-builder hands to image_block. They are the regression guard against a future
edit silently sending full-res (cost) or dropping a document read to 768 (quality).
"""

from __future__ import annotations

from typing import Any

import pytest

from toolkit import (
    building_extraction,
    condition_markers,
    condition_scoring,
    image_similarity,
)
from toolkit.vision_images import COMPARISON_MAX_EDGE, DOCUMENT_MAX_EDGE


class _FakeR2:
    def download_bytes(self, key: str) -> bytes:  # pragma: no cover - recorder ignores bytes
        return b"x"


def _recorder(monkeypatch: Any, module: Any) -> list[int]:
    """Replace module.image_block with a recorder of the max_edge it's called with."""
    calls: list[int] = []

    def fake_image_block(r2: Any, key: str, max_edge: int) -> dict[str, Any]:
        calls.append(max_edge)
        return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": ""}}

    monkeypatch.setattr(module, "image_block", fake_image_block)
    return calls


def _stub_r2_keys(monkeypatch: Any, module: Any, keys: list[str]) -> None:
    monkeypatch.setattr(module.image_storage, "is_configured", lambda: True)
    monkeypatch.setattr(module.image_storage.R2Client, "from_env", staticmethod(lambda: _FakeR2()))
    monkeypatch.setattr(module, "_fetch_image_keys", lambda *a, **k: list(keys))


def test_image_similarity_uses_comparison_tier(monkeypatch: Any) -> None:
    calls = _recorder(monkeypatch, image_similarity)
    image_similarity._build_image_blocks(_FakeR2(), ["a", "b"])
    assert calls == [COMPARISON_MAX_EDGE, COMPARISON_MAX_EDGE]


def test_condition_scoring_uses_document_tier(monkeypatch: Any) -> None:
    calls = _recorder(monkeypatch, condition_scoring)
    _stub_r2_keys(monkeypatch, condition_scoring, ["a", "b"])
    condition_scoring._build_image_blocks_if_available(None, 1, 4)
    assert calls == [DOCUMENT_MAX_EDGE, DOCUMENT_MAX_EDGE]


def test_condition_markers_uses_document_tier(monkeypatch: Any) -> None:
    calls = _recorder(monkeypatch, condition_markers)
    _stub_r2_keys(monkeypatch, condition_markers, ["a", "b"])
    condition_markers._build_image_blocks_if_available(None, 1, 4)
    assert calls == [DOCUMENT_MAX_EDGE, DOCUMENT_MAX_EDGE]


def test_building_extraction_listing_photos_use_document_tier(monkeypatch: Any) -> None:
    calls = _recorder(monkeypatch, building_extraction)
    _stub_r2_keys(monkeypatch, building_extraction, ["a", "b"])
    blocks, n, warning = building_extraction._build_image_blocks(None, 1, 4)
    assert calls == [DOCUMENT_MAX_EDGE, DOCUMENT_MAX_EDGE]
    assert n == 2 and warning is None
