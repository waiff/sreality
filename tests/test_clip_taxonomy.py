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


def test_staircase_tags_wired():
    assert _TAX["collapse"]["staircase_interior"] == "staircase_interior"
    assert _TAX["collapse"]["staircase_exterior"] == "staircase_exterior"
    assert {"staircase_interior", "staircase_exterior"} <= set(ROOM_TYPES)


def test_toilet_anchor_disambiguates_from_bathroom():
    # The sharpened WC anchor must exclude shower/bathtub so CLIP stops confusing WC <-> koupelna.
    assert "no shower" in _TAX["prompts"]["toilet"].lower()


def test_property_document_wired():
    assert _TAX["collapse"]["energy_certificate"] == "property_document"
    assert _TAX["collapse"]["document_text"] == "property_document"
    assert "property_document" in set(ROOM_TYPES)


def test_drawing_tags_match_plan_family():
    # The tagger NULLs render_score for DRAWING/DOCUMENT logical tags (the render-vs-photo
    # axis is noise on a drawing). That set must stay == the room_taxonomy 'plan' family, so a
    # new plan/doc tag automatically gets no render score.
    from scraper.clip_tagger import _DRAWING_LOGICAL_TAGS
    from toolkit.room_taxonomy import ROOM_FAMILIES
    plan_family = {t for t, f in ROOM_FAMILIES.items() if f == "plan"}
    assert _DRAWING_LOGICAL_TAGS == plan_family


def test_render_photo_anchors_present_and_nonempty():
    # The orthogonal render-vs-photo axis (migration 239): both sides must exist + be
    # non-empty, else the tagger silently scores every image render_score 0.
    for key in ("render_anchors", "photo_anchors"):
        assert _TAX.get(key), f"{key} missing/empty"
        assert all(isinstance(v, str) and v for v in _TAX[key])
