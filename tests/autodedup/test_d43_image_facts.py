"""B-n (R1): `d43_image_facts` — tags are facts in no mode.

`d43_gate_image_facts` and `d43_cluster_image_facts` switch the image block off for GATE and
CLUSTER only, so PROMOTE (`promotion_warrant`, E193's strict cluster relation) still refused
on `floorplan` and `interior`, a tag-derived similarity nobody states. `d43_image_facts =
False` returns before that block in every mode; `w31r1.json` is w31 + the heads + that dial.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from autodedup.d43 import ClusterRelation
from autodedup.dataset import Listing, Location
from autodedup.indistinguishable import (
    CLUSTER,
    GATE,
    MODES,
    PROMOTE,
    distinguishing_facts,
    promotion_warrant,
)
from autodedup.settings import Settings

SETTINGS_DIR = Path(__file__).resolve().parents[2] / "autodedup" / "settings"
W31 = Settings.from_json(SETTINGS_DIR / "w31.json")
W31R1 = Settings.from_json(SETTINGS_DIR / "w31r1.json")
IMAGE_FACTS = {"floorplan", "interior"}
# w31.json as shipped by 49f72ef5 (S15b), byte for byte: R1 adds a row, it edits none.
W31_SHA256 = "5ba98be7e443caef4bd2ec7da0570a2973ee2e2d480c4fe756809b7f5830927a"


def _listing(listing_id: int, source: str = "sreality") -> Listing:
    return Listing(id=listing_id, block="b", source=source, category_main="byt",
                   category_type="prodej", disposition="2+kk", area_m2=70.0, floor=3,
                   total_floors=6, price=5_000_000.0,
                   location=Location(obec_kod=1, granularity="street", granularity_rank=60))


# A floor-plan conflict with a weak second room: both image facts fire when the block is read.
FEATS = {"floorplan_conflict": (1.0, True), "tag_room_clip_min2": (0.5, True)}


def _image_facts(settings: Settings, mode: str) -> set[str]:
    facts = distinguishing_facts(_listing(1), _listing(2, "idnes"), FEATS, settings, mode)
    return {fact.name for fact in facts} & IMAGE_FACTS


def test_the_field_defaults_to_reading_the_image_block() -> None:
    assert Settings().d43_image_facts is True
    for mode in MODES:
        assert _image_facts(Settings(), mode) == IMAGE_FACTS, mode


@pytest.mark.parametrize("mode", [GATE, CLUSTER, PROMOTE])
def test_off_drops_both_image_facts_in_every_mode(mode: str) -> None:
    assert _image_facts(Settings(d43_image_facts=False), mode) == set()


def test_off_wins_over_the_per_mode_limbs() -> None:
    cfg = Settings(d43_image_facts=False, d43_gate_image_facts=True, d43_cluster_image_facts=True)
    assert all(_image_facts(cfg, mode) == set() for mode in MODES)


def test_the_per_mode_limbs_never_reached_promote() -> None:
    # The gap B-n closes: both limbs off, and PROMOTE still reads the tags.
    cfg = Settings(d43_gate_image_facts=False, d43_cluster_image_facts=False)
    assert _image_facts(cfg, GATE) == set() and _image_facts(cfg, CLUSTER) == set()
    assert _image_facts(cfg, PROMOTE) == IMAGE_FACTS


def test_off_leaves_every_stated_fact_standing() -> None:
    a, b = _listing(1), _listing(2, "idnes")
    b.floor = 5
    for mode in MODES:
        facts = {f.name for f in distinguishing_facts(a, b, FEATS, W31R1, mode)}
        assert "floor" in facts and not facts & IMAGE_FACTS, mode


# --- w31 is unchanged; w31r1 is w31 plus two dials -------------------------------------------


def test_w31_is_byte_for_byte_the_shipped_row() -> None:
    raw = (SETTINGS_DIR / "w31.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == W31_SHA256
    assert b"d43_image_facts" not in raw and b"image_tags" not in raw


def test_w31_still_reads_the_image_block_where_it_always_did() -> None:
    assert W31.d43_image_facts is True and W31.image_tags == "clip"
    assert _image_facts(W31, GATE) == set()              # d43_gate_image_facts: false
    assert _image_facts(W31, CLUSTER) == IMAGE_FACTS
    assert _image_facts(W31, PROMOTE) == IMAGE_FACTS


def test_w31r1_is_w31_plus_the_heads_and_image_facts_off() -> None:
    w31, r1 = W31.to_dict(), W31R1.to_dict()
    assert {key: r1[key] for key in r1 if w31[key] != r1[key]} == {
        "image_tags": "heads", "d43_image_facts": False}


@pytest.mark.parametrize("mode", [GATE, CLUSTER, PROMOTE])
def test_w31r1_reads_no_image_fact_in_any_mode(mode: str) -> None:
    assert _image_facts(W31R1, mode) == set()


def test_w31r1_lets_promotion_and_the_strict_relation_past_a_tag_difference() -> None:
    a, b = _listing(1), _listing(2, "idnes")
    assert promotion_warrant(a, b, FEATS, W31) is None
    assert promotion_warrant(a, b, FEATS, W31R1) is not None
    listings, feats = {1: a, 2: b}, {(1, 2): FEATS}
    # E193: a cut re-offered its join reads PROMOTE's bar, and under w31 a tag still split it.
    assert ClusterRelation(listings, feats, W31).strict().ok(1, 2) is False
    assert ClusterRelation(listings, feats, W31R1).strict().ok(1, 2) is True
    assert ClusterRelation(listings, feats, W31).ok(1, 2) is False
    assert ClusterRelation(listings, feats, W31R1).ok(1, 2) is True
