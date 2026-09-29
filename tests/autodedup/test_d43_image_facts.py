"""Release R1: `d43_image_facts` — photo labels are facts in no mode.

`d43_gate_image_facts` and `d43_cluster_image_facts` switch the image block off for GATE and
CLUSTER only, so PROMOTE (`promotion_warrant`, E193's strict cluster relation) still refused
on `floorplan` and `interior`, a tag-derived similarity nobody states. `d43_image_facts =
False` returns before that block in every mode; `w31r1c.json` is w31 + that one dial (R1c,
G4 day 3). The live lane reads the settings its seed STORED, so a row stored before the
field existed must load with the default that keeps today's behaviour.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autodedup.d43 import ClusterRelation, relation_for
from autodedup.dataset import Listing, Location
from autodedup.decide import Decision, apply_d43_rule
from autodedup.incremental_lane import named_config, pass_config
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
W31R1C = Settings.from_json(SETTINGS_DIR / "w31r1c.json")
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
# The same pair with the seller's order code in common, so promotion's corroboration holds and
# only the image facts stand between the band and the merge zone.
FEATS_CODE = {**FEATS, "ref_code_shared": (1.0, True)}


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
    # The gap the field closes: both limbs off, and PROMOTE still reads the tags.
    cfg = Settings(d43_gate_image_facts=False, d43_cluster_image_facts=False)
    assert _image_facts(cfg, GATE) == set() and _image_facts(cfg, CLUSTER) == set()
    assert _image_facts(cfg, PROMOTE) == IMAGE_FACTS


def test_off_leaves_every_stated_fact_standing() -> None:
    a, b = _listing(1), _listing(2, "idnes")
    b.floor = 5
    for mode in MODES:
        facts = {f.name for f in distinguishing_facts(a, b, FEATS, W31R1C, mode)}
        assert "floor" in facts and not facts & IMAGE_FACTS, mode


# --- w31 is unchanged; w31r1c is w31 plus one dial -------------------------------------------


def test_w31_is_byte_for_byte_the_shipped_row() -> None:
    raw = (SETTINGS_DIR / "w31.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == W31_SHA256
    assert b"d43_image_facts" not in raw


def test_w31_still_reads_the_image_block_where_it_always_did() -> None:
    assert W31.d43_image_facts is True
    assert _image_facts(W31, GATE) == set()              # d43_gate_image_facts: false
    assert _image_facts(W31, CLUSTER) == IMAGE_FACTS
    assert _image_facts(W31, PROMOTE) == IMAGE_FACTS


def test_w31r1c_is_w31_plus_image_facts_off_and_nothing_else() -> None:
    w31, r1c = W31.to_dict(), W31R1C.to_dict()
    assert {key: r1c[key] for key in r1c if w31[key] != r1c[key]} == {"d43_image_facts": False}
    raw_w31 = json.loads((SETTINGS_DIR / "w31.json").read_text(encoding="utf-8"))
    raw_r1c = json.loads((SETTINGS_DIR / "w31r1c.json").read_text(encoding="utf-8"))
    assert raw_r1c == {**raw_w31, "d43_image_facts": False}


@pytest.mark.parametrize("mode", [GATE, CLUSTER, PROMOTE])
def test_w31r1c_reads_no_image_fact_in_any_mode(mode: str) -> None:
    assert _image_facts(W31R1C, mode) == set()


# --- every reader of the block: the gate, the promotion warrant, both cluster relations ------


def test_promotion_and_both_cluster_relations_pass_a_tag_only_difference_under_w31r1c() -> None:
    a, b = _listing(1), _listing(2, "idnes")
    assert promotion_warrant(a, b, FEATS, W31) is None
    assert promotion_warrant(a, b, FEATS, W31R1C) is not None
    listings, feats = {1: a, 2: b}, {(1, 2): FEATS}
    # E193: a cut re-offered its join reads PROMOTE's bar, and under w31 a tag still split it.
    assert ClusterRelation(listings, feats, W31).strict().ok(1, 2) is False
    assert ClusterRelation(listings, feats, W31R1C).strict().ok(1, 2) is True
    # The group step's own relation, built the way the lane and `harness run` build it.
    for settings, joined in ((W31, False), (W31R1C, True)):
        relation = relation_for(settings, listings, feats)
        assert relation is not None and relation.ok(1, 2) is joined
        assert relation.strict().ok(1, 2) is joined


def test_the_gate_demotes_on_a_tag_only_when_the_field_is_on() -> None:
    a, b = _listing(1), _listing(2, "idnes")
    merge = Decision(1, 2, "merge", 0.99, {"IMG"}, None, None, "model")
    gated = Settings(d43_gate=True, d43_gate_image_facts=True)
    demoted = apply_d43_rule(merge, a, b, FEATS, gated)
    assert demoted.zone == "band" and demoted.reason == "model:d43_gate:floorplan"
    kept = apply_d43_rule(merge, a, b, FEATS, Settings(d43_gate=True, d43_gate_image_facts=True,
                                                        d43_image_facts=False))
    assert kept is merge
    # w31 never gated on tags (its limb is off), so the release changes nothing at the gate.
    assert apply_d43_rule(merge, a, b, FEATS, W31) is merge
    assert apply_d43_rule(merge, a, b, FEATS, W31R1C) is merge


def test_a_band_pair_only_a_tag_separated_is_promoted_under_w31r1c_and_not_under_w31() -> None:
    a, b = _listing(1), _listing(2, "idnes")
    band = Decision(1, 2, "band", 0.6, {"IMG"}, None, None, "model")
    assert apply_d43_rule(band, a, b, FEATS_CODE, W31) is band
    promoted = apply_d43_rule(band, a, b, FEATS_CODE, W31R1C)
    assert promoted.zone == "merge" and promoted.reason.startswith("d43_promote:")


# --- the live lane reads the settings its seed STORED ----------------------------------------


def _stored_before_the_field(settings: Settings) -> dict[str, object]:
    """The calibration row a seed wrote on a build without the field (`json.dumps(to_dict())`)."""
    row = json.loads(json.dumps(settings.to_dict(), sort_keys=True))
    del row["d43_image_facts"]
    return row


def test_a_seed_stored_without_the_field_loads_as_today() -> None:
    settings, _ = pass_config(_stored_before_the_field(W31), "w6_gold", "rt")
    assert settings == W31 and settings.d43_image_facts is True
    assert _image_facts(settings, CLUSTER) == IMAGE_FACTS
    assert _image_facts(settings, PROMOTE) == IMAGE_FACTS


def test_a_seed_of_the_release_row_reads_the_field_back() -> None:
    stored = json.loads(json.dumps(W31R1C.to_dict(), sort_keys=True))
    settings, _ = pass_config(stored, "w6_gold", "rt")
    assert settings == W31R1C and settings.d43_image_facts is False


def test_rt_seed_finds_the_release_row_by_name() -> None:
    settings, _, name, model = named_config({"settings": "w31r1c", "model": "w6_gold"},
                                            what="rt_seed")
    assert (name, model) == ("w31r1c", "w6_gold") and settings == W31R1C
