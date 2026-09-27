"""autodedup/head_tags.py: the active tag model's winner as the engine's room tag (G4)."""

from __future__ import annotations

import json

import pytest

from autodedup import head_tags as ht
from autodedup.dataset import Image
from autodedup.export import build_image_record, parse_export_args


@pytest.mark.parametrize("label, room", [
    ("interier - kuchyně", "kitchen"),
    ("interier - koupelna", "bathroom"),
    ("Obývací pokoj", "living_room"),
    ("exterier - fasáda", "exterior_facade"),
    ("podklad - půdorys", "floor_plan"),
    ("podklad - 3d plán", "plan_3d"),
    ("podklad - katastrální mapa", "site_plan"),
    ("podklad - letecký snímek s ohraničením subjektu", "site_plan"),
    ("property list", "property_document"),
    ("garáž", "garage"),
    ("technické zařízení / místnost", "technical"),
])
def test_every_v1_head_has_a_room(label, room):
    assert ht.head_room(label) == room


def test_an_unknown_head_is_refused_not_guessed():
    with pytest.raises(ht.UnknownHeadLabel):
        ht.head_room("interier - ložnice")


def test_the_floor_and_the_margin_decide_routing():
    row = {"label": "interier - kuchyně", "winner_score": 0.62,
           "scores": json.dumps({"25": 0.62, "22": 0.55, "3": 0.1})}
    assert ht.head_tag_pairs([row]) == [["kitchen", 0.62]]
    assert ht.head_tag_pairs([row], floor=0.7) == []
    assert ht.head_tag_pairs([row], margin=0.1) == []
    assert ht.head_record(row) == ["kitchen", 0.62, 0.55]


def test_the_harness_arm_swaps_clip_tags_for_the_head_winner():
    kitchen = Image(listing_id=1, image_id=1, tags=[("hallway", 0.9)], head=["kitchen", 0.8, 0.1])
    weak = Image(listing_id=1, image_id=2, tags=[("bedroom", 0.9)], head=["living_room", 0.4, 0.3])
    unscored = Image(listing_id=1, image_id=3, tags=[("kitchen", 0.9)], head=None)
    counts = ht.apply_head_tags([kitchen, weak, unscored])
    assert kitchen.tags == [("kitchen", 0.8)] and weak.tags == [] and unscored.tags == []
    assert counts == {"tagged": 1, "untagged": 1, "unscored": 1}


def test_the_export_emits_head_only_when_asked():
    row = {"listing_id": 1, "image_id": 2, "sequence": 1, "storage_path": "a", "phash": 5}
    assert "head" not in build_image_record(row, clip=None)
    assert build_image_record(row, clip=None, head=None)["head"] is None
    assert build_image_record(row, clip=None, head=["kitchen", 0.8, 0.1])["head"] == ["kitchen", 0.8, 0.1]
    assert Image.from_json({"listing_id": 1, "image_id": 2, "head": ["kitchen", 0.8, 0.1]}).head == [
        "kitchen", 0.8, 0.1]
    assert parse_export_args({})["head_tags"] == 0
    assert parse_export_args({"head_tags": "1"})["head_tags"] == 1
