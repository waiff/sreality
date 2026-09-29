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
    ("garáž", "hallway"),
    ("technické zařízení / místnost", "hallway"),
])
def test_every_v1_head_has_a_room(label, room):
    assert ht.head_room(label) == room


def test_every_head_room_is_the_room_clip_gives_its_photographs():
    from autodedup.features import PRIVATE_ROOM_TAGS
    from autodedup.fingerprint import dominant_family
    from toolkit.room_taxonomy import ROOM_FAMILIES

    assert set(ht.HEAD_ROOMS.values()) <= set(ROOM_FAMILIES)
    assert dominant_family(Image(listing_id=1, image_id=1, tags=[("plan_3d", 0.9)])) == "plan"
    # garage / technical photographs are CLIP's interior catch-all, never a room a unit owns
    for label in ("garáž", "technické zařízení / místnost"):
        assert ht.head_room(label) == ht.SUBFLOOR_CATCH_ALL
        assert ht.head_room(label) not in PRIVATE_ROOM_TAGS
    assert ht.INTERIOR_HEAD_ROOMS == {"kitchen", "bathroom", "living_room", "hallway"}


def test_a_sub_floor_garage_winner_keeps_the_interior_family():
    """c18 219887 x 318445, a bazos shop re-posted with the same photographs: CLIP tags its interior
    shots `hallway`; the heads' winners there are garage / technical below the floor."""
    row = {"label": "garáž", "winner_score": 0.14, "scores": json.dumps({"1": 0.14, "2": 0.1})}
    assert ht.head_tag_pairs([row]) == [["hallway", 0.14]]
    technical = dict(row, label="technické zařízení / místnost", winner_score=0.28)
    assert ht.head_tag_pairs([technical]) == [["hallway", 0.28]]
    assert ht.head_tag_pairs([row], catch_all=None) == []


def test_an_unknown_head_is_refused_not_guessed():
    with pytest.raises(ht.UnknownHeadLabel):
        ht.head_room("interier - ložnice")


def test_the_floor_and_the_margin_decide_routing():
    row = {"label": "interier - kuchyně", "winner_score": 0.62,
           "scores": json.dumps({"25": 0.62, "22": 0.55, "3": 0.1})}
    assert ht.head_tag_pairs([row]) == [["kitchen", 0.62]]
    assert ht.head_tag_pairs([row], floor=0.7, catch_all=None) == []
    assert ht.head_tag_pairs([row], margin=0.1, catch_all=None) == []
    # by default a sub-floor INTERIOR winner keeps its family through the catch-all
    assert ht.head_tag_pairs([row], floor=0.7) == [["hallway", 0.62]]
    facade = dict(row, label="exterier - fasáda")
    assert ht.head_tag_pairs([facade], floor=0.7) == []
    assert ht.head_record(row) == ["kitchen", 0.62, 0.55]


def test_the_harness_arm_swaps_clip_tags_for_the_head_winner():
    kitchen = Image(listing_id=1, image_id=1, tags=[("hallway", 0.9)], head=["kitchen", 0.8, 0.1])
    weak = Image(listing_id=1, image_id=2, tags=[("bedroom", 0.9)], head=["living_room", 0.4, 0.3])
    unscored = Image(listing_id=1, image_id=3, tags=[("kitchen", 0.9)], head=None)
    counts = ht.apply_head_tags([kitchen, weak, unscored], catch_all=None)
    assert kitchen.tags == [("kitchen", 0.8)] and weak.tags == [] and unscored.tags == []
    assert counts == {"tagged": 1, "untagged": 1, "unscored": 1}
    assert ht.apply_head_tags([weak]) == {"tagged": 1, "untagged": 0, "unscored": 0}
    assert weak.tags == [("hallway", 0.4)]


def test_the_export_emits_head_only_when_asked():
    row = {"listing_id": 1, "image_id": 2, "sequence": 1, "storage_path": "a", "phash": 5}
    assert "head" not in build_image_record(row, clip=None)
    assert build_image_record(row, clip=None, head=None)["head"] is None
    assert build_image_record(row, clip=None, head=["kitchen", 0.8, 0.1])["head"] == ["kitchen", 0.8, 0.1]
    assert Image.from_json({"listing_id": 1, "image_id": 2, "head": ["kitchen", 0.8, 0.1]}).head == [
        "kitchen", 0.8, 0.1]
    assert parse_export_args({})["head_tags"] == 0
    assert parse_export_args({"head_tags": "1"})["head_tags"] == 1


def test_a_dump_attaches_the_real_winners_by_image_id(tmp_path):
    import gzip

    path = tmp_path / "tag_dump.jsonl.gz"
    with gzip.open(path, "wt") as handle:
        handle.write(json.dumps({"image_id": 1, "room": "kitchen", "winner": 0.8,
                                 "runner_up": 0.1}) + "\n")
    dump = ht.load_head_dump(str(path))
    first, second = Image(listing_id=1, image_id=1), Image(listing_id=1, image_id=2)
    assert ht.attach_heads([first, second], dump) == 1
    assert first.head == ["kitchen", 0.8, 0.1] and second.head is None


def test_a_dump_row_without_a_room_is_refused(tmp_path):
    path = tmp_path / "tag_dump.jsonl"
    path.write_text(json.dumps({"image_id": 1, "room": None, "winner": 0.8, "runner_up": 0.1}) + "\n")
    with pytest.raises(ht.UnknownHeadLabel):
        ht.load_head_dump(str(path))


def test_the_settings_switch_is_clip_or_heads():
    from autodedup.settings import Settings

    assert Settings().image_tags == "clip"
    assert Settings(image_tags="heads").image_tags == "heads"
    with pytest.raises(ValueError):
        Settings(image_tags="dinov3")


def test_the_harness_applies_heads_only_when_the_settings_say_so():
    from autodedup.dataset import Dataset, Meta
    from autodedup.harness import use_head_tags
    from autodedup.settings import Settings

    image = Image(listing_id=1, image_id=1, tags=[("hallway", 0.9)], head=["kitchen", 0.8, 0.1])
    dataset = Dataset(meta=Meta(), listings={}, images_by_listing={1: [image]})
    assert use_head_tags(dataset, Settings()) == {}
    assert image.tags == [("hallway", 0.9)]
    assert use_head_tags(dataset, Settings(image_tags="heads"))["tagged"] == 1
    assert image.tags == [("kitchen", 0.8)]


def test_the_harness_refuses_heads_on_an_export_without_them():
    from autodedup.dataset import Dataset, Meta
    from autodedup.harness import use_head_tags
    from autodedup.settings import Settings

    dataset = Dataset(meta=Meta(), listings={},
                      images_by_listing={1: [Image(listing_id=1, image_id=1, tags=[("hallway", 0.9)])]})
    with pytest.raises(ValueError, match="head_tags=1"):
        use_head_tags(dataset, Settings(image_tags="heads"))


def test_a_photo_scored_under_the_floor_is_complete_evidence():
    from autodedup.incremental import evidence_of

    below = Image(listing_id=1, image_id=1, phash=1, clip="x", tags=[], head=["hallway_like", 0.3, 0.2])
    unscored = Image(listing_id=1, image_id=2, phash=2, clip="x", tags=[], head=None)
    assert evidence_of([below]).complete
    assert not evidence_of([below, unscored]).complete


def test_the_lane_reads_head_winners_under_heads(monkeypatch):
    from autodedup import export_sql as E
    from autodedup.incremental_lane import SqlFacts

    rows = {
        E.COHORT_IMAGES_SQL: [
            {"listing_id": 7, "image_id": 1, "sequence": 1, "storage_path": "a", "phash": 11},
            {"listing_id": 7, "image_id": 2, "sequence": 2, "storage_path": "b", "phash": 12},
            {"listing_id": 7, "image_id": 3, "sequence": 3, "storage_path": "c", "phash": 13}],
        E.COHORT_CLIP_SQL: [],
        ht.COHORT_HEAD_TAGS_SQL: [
            {"image_id": 1, "winner_score": 0.9, "label": "interier - kuchyně",
             "scores": {"25": 0.9, "22": 0.1}},
            {"image_id": 2, "winner_score": 0.4, "label": "interier - koupelna",
             "scores": {"22": 0.4, "25": 0.3}}],
    }
    asked: list[str] = []

    def fake_dicts(self, sql, params):
        asked.append(sql)
        return rows[sql]

    monkeypatch.setattr(SqlFacts, "_dicts", fake_dicts)
    facts = SqlFacts(object(), population={11: 1, 12: 1, 13: 1}, image_tags="heads")
    gallery = {img.image_id: img for img in facts._galleries([7])[7]}
    assert E.COHORT_CLIP_TAGS_SQL not in asked
    assert gallery[1].tags == [("kitchen", 0.9)] and gallery[1].head == ["kitchen", 0.9, 0.1]
    assert gallery[2].tags == [("hallway", 0.4)] and gallery[2].head == ["bathroom", 0.4, 0.3]
    assert gallery[3].tags == [] and gallery[3].head is None


def test_the_catch_all_keeps_a_subfloor_interior_photo_in_its_family():
    assert ht.route("kitchen", 0.8, 0.1) == "kitchen"
    assert ht.route("kitchen", 0.4, 0.3, catch_all=None) is None
    assert ht.route("kitchen", 0.4, 0.3, catch_all="hallway") == "hallway"
    assert ht.route("exterior_facade", 0.4, 0.3, catch_all="hallway") is None
    image = Image(listing_id=1, image_id=1, tags=[], head=["bathroom", 0.35, 0.3])
    assert ht.apply_head_tags([image], catch_all="hallway") == {"tagged": 1, "untagged": 0, "unscored": 0}
    assert image.tags == [("hallway", 0.35)]
