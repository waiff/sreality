"""N:N plan-gate payload: each plan is labelled so the cross-product compare can cite it."""

from __future__ import annotations

from typing import Any

import toolkit.visual_match as vm


def test_labelled_plan_blocks_interleaves_one_label_per_plan(monkeypatch: Any) -> None:
    monkeypatch.setattr(vm, "image_block", lambda r2, key, edge: {"type": "image", "key": key})
    out = vm._labelled_plan_blocks(object(), ["k1", "k2", "k3"], "Listing A", 1568)
    assert out == [
        {"type": "text", "text": "Listing A plan 1:"},
        {"type": "image", "key": "k1"},
        {"type": "text", "text": "Listing A plan 2:"},
        {"type": "image", "key": "k2"},
        {"type": "text", "text": "Listing A plan 3:"},
        {"type": "image", "key": "k3"},
    ]


def test_labelled_plan_blocks_empty_is_empty(monkeypatch: Any) -> None:
    monkeypatch.setattr(vm, "image_block", lambda *a: (_ for _ in ()).throw(AssertionError("unused")))
    assert vm._labelled_plan_blocks(object(), [], "Listing B", 1568) == []
