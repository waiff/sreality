"""E929: tags are never facts. The CLIP room tag separates nothing in any mode (arm E1) and no
longer refuses the `photos:N` waiver of a missing reading (arm E2); a floor-plan drawing that
differs stays a distinguishing fact (operator case 11), exactly as in w31."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from autodedup.d43 import ClusterRelation, relation_for
from autodedup.dataset import Listing, Location
from autodedup.decide import Decision, apply_d43_rule, demonstration_refusal
from autodedup.demonstrate import strong_corroboration
from autodedup.incremental_lane import named_config, pass_config
from autodedup.indistinguishable import (
    CLUSTER,
    FACT_NAMES,
    GATE,
    MODES,
    PROMOTE,
    distinguishing_facts,
    promotion_warrant,
    unit_grade_warrant,
)
from autodedup.settings import Settings

SETTINGS_DIR = Path(__file__).resolve().parents[2] / "autodedup" / "settings"
W31 = Settings.from_json(SETTINGS_DIR / "w31.json")
# w31.json as shipped by 49f72ef5 (S15b) and seeded live on 09-27: E929 edits no row.
W31_SHA256 = "5ba98be7e443caef4bd2ec7da0570a2973ee2e2d480c4fe756809b7f5830927a"
# The two dials that narrowed the deleted tag fact, both ways: they must change nothing now.
INTERIOR_DIALS_OFF = Settings(d43_interior_requires_no_tight_photo=False,
                              d43_interior_sequential_repost=False)
INTERIOR_DIALS_ON = Settings(d43_interior_requires_no_tight_photo=True,
                             d43_interior_sequential_repost=True)
ROWS = (Settings(), W31, INTERIOR_DIALS_OFF, INTERIOR_DIALS_ON)

FLOORPLAN = {"floorplan_conflict": (1.0, True), "tag_room_clip_min2": (0.5, True)}
FLOORPLAN_CODE = {**FLOORPLAN, "ref_code_shared": (1.0, True)}


def _tag_only(clip: float) -> dict[str, tuple[float, bool]]:
    return {"floorplan_conflict": (0.0, True), "tag_room_clip_min2": (clip, True)}


TAG_ONLY = _tag_only(0.5)
TAG_ONLY_CODE = {**TAG_ONLY, "ref_code_shared": (1.0, True)}


def _listing(listing_id: int, source: str = "sreality") -> Listing:
    return Listing(id=listing_id, block="b", source=source, category_main="byt",
                   category_type="prodej", disposition="2+kk", area_m2=70.0, floor=3,
                   total_floors=6, price=5_000_000.0,
                   location=Location(obec_kod=1, granularity="street", granularity_rank=60))


def _facts(feats: object, settings: Settings, mode: str) -> list[str]:
    found = distinguishing_facts(_listing(1), _listing(2, "idnes"), feats, settings, mode)  # type: ignore[arg-type]
    return [fact.name for fact in found]


# --- the tag separates nothing ---------------------------------------------------------------


def test_interior_is_no_fact_name_any_more() -> None:
    assert "interior" not in FACT_NAMES
    assert "floorplan" in FACT_NAMES


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("clip", [0.0, 0.5, 0.879, 0.899])
def test_a_room_tag_difference_alone_separates_nothing(mode: str, clip: float) -> None:
    for settings in ROWS:
        assert _facts(_tag_only(clip), settings, mode) == [], (mode, clip)


@pytest.mark.parametrize("mode", MODES)
def test_a_tag_with_a_shared_photograph_or_a_re_post_changes_nothing(mode: str) -> None:
    # E243 / E289 narrowed the tag fact; with it gone neither dial moves any answer.
    with_photo = {**TAG_ONLY, "phash_tight_matches": (7.0, True)}
    for feats in (TAG_ONLY, with_photo, FLOORPLAN):
        assert _facts(feats, INTERIOR_DIALS_ON, mode) == _facts(feats, INTERIOR_DIALS_OFF, mode)


# --- the floor plan still separates -----------------------------------------------------------


@pytest.mark.parametrize("mode", [GATE, CLUSTER, PROMOTE])
def test_a_floor_plan_conflict_still_separates_in_every_mode(mode: str) -> None:
    for settings in (Settings(), INTERIOR_DIALS_OFF, INTERIOR_DIALS_ON):
        assert _facts(FLOORPLAN, settings, mode) == ["floorplan"], mode


def test_w31_reads_the_floor_plan_exactly_where_it_always_did() -> None:
    assert _facts(FLOORPLAN, W31, GATE) == []            # its own dial: d43_gate_image_facts false
    assert _facts(FLOORPLAN, W31, CLUSTER) == ["floorplan"]
    assert _facts(FLOORPLAN, W31, PROMOTE) == ["floorplan"]


def test_the_floor_plan_still_needs_the_weak_room_it_was_measured_with() -> None:
    strong = {"floorplan_conflict": (1.0, True), "tag_room_clip_min2": (0.95, True)}
    assert all(_facts(strong, Settings(), mode) == [] for mode in MODES)


# --- every reader: the gate, the promotion warrant, both cluster relations --------------------


def test_promotion_and_both_cluster_relations_pass_a_tag_and_hold_a_floor_plan() -> None:
    a, b = _listing(1), _listing(2, "idnes")
    assert promotion_warrant(a, b, TAG_ONLY, W31) is not None
    assert promotion_warrant(a, b, FLOORPLAN, W31) is None
    listings = {1: a, 2: b}
    for feats, joined in ((TAG_ONLY, True), (FLOORPLAN, False)):
        assert ClusterRelation(listings, {(1, 2): feats}, W31).strict().ok(1, 2) is joined
        relation = relation_for(W31, listings, {(1, 2): feats})
        assert relation is not None and relation.ok(1, 2) is joined
        assert relation.strict().ok(1, 2) is joined


def test_the_gate_demotes_on_a_floor_plan_and_never_on_a_tag() -> None:
    a, b = _listing(1), _listing(2, "idnes")
    merge = Decision(1, 2, "merge", 0.99, {"IMG"}, None, None, "model")
    gated = Settings(d43_gate=True, d43_gate_image_facts=True)
    demoted = apply_d43_rule(merge, a, b, FLOORPLAN, gated)
    assert demoted.zone == "band" and demoted.reason == "model:d43_gate:floorplan"
    assert apply_d43_rule(merge, a, b, TAG_ONLY, gated) is merge
    assert apply_d43_rule(merge, a, b, TAG_ONLY, W31) is merge


def test_a_band_pair_only_a_tag_separated_is_promoted_and_a_floor_plan_is_not() -> None:
    a, b = _listing(1), _listing(2, "idnes")
    band = Decision(1, 2, "band", 0.6, {"IMG"}, None, None, "model")
    promoted = apply_d43_rule(band, a, b, TAG_ONLY_CODE, W31)
    assert promoted.zone == "merge" and promoted.reason.startswith("d43_promote:")
    assert apply_d43_rule(band, a, b, FLOORPLAN_CODE, W31) is band


# --- E2: the photos:N waiver does not ask the tag -----------------------------------------------
BODY = "Prodej bytu 2+kk v cihlovém domě po celkové rekonstrukci, sklep a balkon. " * 4


def _bodied(listing_id: int, source: str = "sreality") -> Listing:
    advert = _listing(listing_id, source)
    advert.description = BODY
    return advert


@pytest.mark.parametrize("clip", [None, 0.0, 0.5, 0.899, 0.95])
def test_the_photos_waiver_no_longer_reads_the_tag(clip: float | None) -> None:
    a, b = _bodied(1), _bodied(2, "idnes")
    feats: dict[str, tuple[float, bool]] = {"phash_tight_matches": (4.0, True)}
    if clip is not None:
        feats["tag_room_clip_min2"] = (clip, True)
    assert strong_corroboration(a, b, feats, W31) == "photos:4"
    assert unit_grade_warrant(a, b, feats, W31) == "photos:4"
    feats["phash_tight_matches"] = (2.0, True)
    assert strong_corroboration(a, b, feats, W31) is None


def test_a_weak_tag_no_longer_keeps_a_missing_reading_out_of_the_merge_zone() -> None:
    a, b = _bodied(1), _bodied(2, "idnes")
    b.price = None
    feats = {"phash_tight_matches": (4.0, True), "tag_room_clip_min2": (0.5, True),
             "floorplan_conflict": (0.0, True)}
    assert demonstration_refusal(a, b, feats, W31) is None
    band = Decision(1, 2, "band", 0.6, {"IMG"}, None, None, "model")
    promoted = apply_d43_rule(band, a, b, feats, W31)
    assert promoted.zone == "merge" and promoted.reason.startswith("d43_promote:")
    # The floor plan still refuses the same pair: a drawing that differs is a fact.
    held = {**feats, "floorplan_conflict": (1.0, True)}
    assert apply_d43_rule(band, a, b, held, W31) is band


# --- the live seed still loads (N1: no key added, none deleted) --------------------------------


def test_w31_is_byte_for_byte_the_shipped_row() -> None:
    assert hashlib.sha256((SETTINGS_DIR / "w31.json").read_bytes()).hexdigest() == W31_SHA256


def test_the_live_seed_row_loads() -> None:
    stored = json.loads(json.dumps(W31.to_dict(), sort_keys=True))
    assert stored["d43_interior_requires_no_tight_photo"] is True
    assert stored["d43_interior_sequential_repost"] is True
    assert Settings.from_dict(stored) == W31
    settings, _ = pass_config(stored, "w6_gold", "rt")
    assert settings == W31
    settings, _, name, model = named_config({"settings": "w31", "model": "w6_gold"},
                                            what="rt_seed")
    assert (name, model) == ("w31", "w6_gold") and settings == W31


def test_every_settings_row_still_loads() -> None:
    rows = sorted(p for p in SETTINGS_DIR.glob("*.json") if not p.stem.endswith("_strata"))
    assert len(rows) > 30
    for path in rows:
        Settings.from_json(path)
