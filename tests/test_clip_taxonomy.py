"""Guard data/clip_taxonomy.json — the entire CLIP zero-shot definition.

No torch needed (pure JSON + the label contract), so it runs in normal CI and
catches a malformed taxonomy before any dispatch burns a runner.
"""

import json
from pathlib import Path

from toolkit.image_classification import ROOM_TYPES

_TAX = json.loads(
    (Path(__file__).resolve().parent.parent / "data" / "clip_taxonomy.json").read_text()
)


def test_model_pinned():
    assert _TAX.get("model")


def test_every_collapse_key_is_a_prompt():
    assert set(_TAX["collapse"]) == set(_TAX["prompts"])


def test_collapse_targets_are_logical_labels():
    valid = set(ROOM_TYPES)
    for fine, logical in _TAX["collapse"].items():
        assert logical in valid, f"collapse {fine!r}->{logical!r} not a ROOM_TYPE"


def test_prompts_nonempty():
    assert _TAX["prompts"]
    assert all(isinstance(v, str) and v for v in _TAX["prompts"].values())
