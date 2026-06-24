"""The dedup settings registry — validation + the single-source-of-truth guard."""

import pytest

from toolkit.dedup_engine import CosineBands
from toolkit.dedup_settings import REGISTRY, REGISTRY_BY_KEY, coerce, default_for


def test_keys_unique():
    keys = [s.key for s in REGISTRY]
    assert len(keys) == len(set(keys))


def test_coerce_bool():
    s = REGISTRY_BY_KEY["dedup_prefer_clip_tags"]
    assert coerce(s, "true") is True
    assert coerce(s, "off") is False
    assert coerce(s, True) is True


def test_coerce_float_clamps_to_range():
    s = REGISTRY_BY_KEY["dedup_cosine_haiku_min"]
    assert coerce(s, 0.85) == 0.85
    assert coerce(s, 2.0) == 1.0     # clamped to max
    assert coerce(s, -1.0) == 0.0    # clamped to min


def test_coerce_model_rejects_empty():
    s = REGISTRY_BY_KEY["llm_visual_match_model"]
    with pytest.raises(ValueError):
        coerce(s, "   ")


def test_cosine_defaults_are_the_engine_bands():
    # The registry is THE source of truth; the engine's CosineBands defaults must
    # equal the registered defaults so the UI and the engine never disagree.
    assert default_for("dedup_cosine_haiku_min") == CosineBands().haiku_min
    assert default_for("dedup_cosine_sonnet_min") == CosineBands().sonnet_min


def test_safe_toggles_default_off():
    # The CLIP tiers ship OFF (flip is operator-gated after shadow validation).
    assert default_for("dedup_prefer_clip_tags") is False
    assert default_for("dedup_clip_cosine_enabled") is False
