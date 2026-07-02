"""Tests for the content_hash function (v1 estate shape)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scraper.hashing import content_hash

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample() -> dict[str, Any]:
    return json.loads((FIXTURES / "sample_listing.json").read_text("utf-8"))


def test_hash_is_stable(sample):
    assert content_hash(sample) == content_hash(sample)


def test_hash_unchanged_when_view_counter_changes(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["stats"] = {"views": 999, "anything": True}
    assert content_hash(mutated) == base


def test_hash_unchanged_when_session_state_changes(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["note"] = "a personal note"
    mutated["rus"] = {"anything": True}
    mutated["rus_reply"] = "x"
    assert content_hash(mutated) == base


def test_hash_unchanged_when_poi_labels_change(sample):
    # labels / labels_extended are sreality's own nearby-POI enrichment,
    # recomputed per request — 56% of churned snapshot pairs differed only here.
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["labels"] = [{"name": "different_poi", "distance": 1.0}]
    mutated["labels_extended"] = {"doctors": {"data": []}}
    assert content_hash(mutated) == base


def test_hash_unchanged_when_user_avatar_url_changes(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["user"]["image"] = "//d18-a.sdn.cz/d_18/c_img_ob_F/resigned/other.jpeg"
    assert content_hash(mutated) == base


def test_hash_changes_when_broker_name_changes(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["user"]["user_name"] = "Jiná Makléřka"
    assert content_hash(mutated) != base


def test_hash_changes_when_params_change(sample):
    # legacy camelCase raw_json shape: non-volatile keys under `params` count
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["params"] = {"floor": {"value": 3}}
    assert content_hash(mutated) != base


def test_hash_changes_when_price_changes(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["price_czk"] = 18000
    mutated["price_summary_czk"] = 18000
    assert content_hash(mutated) != base


def test_hash_changes_when_a_real_attribute_changes(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["building_condition"] = {"name": "Po rekonstrukci", "value": 4}
    assert content_hash(mutated) != base


def test_hash_unchanged_when_image_kind_flaps(sample):
    # verified in prod: `kind` oscillates 2<->4 portal-side on unchanged photos
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    for img in mutated["advert_images"]:
        img["kind"] = 4 if img.get("kind") == 2 else 2
    assert content_hash(mutated) == base


def test_hash_unchanged_when_image_url_is_resigned(sample):
    # same image id, re-signed sdn.cz token path (the whole path rotates)
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    for img in mutated["advert_images"]:
        img["url"] = "//d18-a.sdn.cz/d_18/c_img_qC_E/nO2CysqjGrC2szhYTgHCHN4u/ff86.jpeg"
    assert content_hash(mutated) == base


def test_hash_unchanged_when_image_dimensions_flap(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["advert_images"][0]["width"] = 1281
    mutated["advert_images"][0]["height"] = 854
    assert content_hash(mutated) == base


def test_hash_changes_when_image_added(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["advert_images"].append(
        {"id": 999999999, "alt": "", "kind": 2, "order": 99,
         "url": "//d18-a.sdn.cz/d_18/c_img_qB_D/newTokenXYZ/abcd.jpeg",
         "width": 1280, "height": 853}
    )
    assert content_hash(mutated) != base


def test_hash_changes_when_image_removed(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["advert_images"].pop()
    assert content_hash(mutated) != base


def test_hash_changes_when_images_reordered(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    imgs = mutated["advert_images"]
    imgs[0], imgs[1] = imgs[1], imgs[0]
    imgs[0]["order"], imgs[1]["order"] = imgs[1]["order"], imgs[0]["order"]
    assert content_hash(mutated) != base


def test_hash_changes_when_image_caption_changes(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["advert_images"][0]["alt"] = "nová kuchyň"
    assert content_hash(mutated) != base


def test_image_without_id_falls_back_to_url_path(sample):
    base_doc = copy.deepcopy(sample)
    del base_doc["advert_images"][0]["id"]
    base = content_hash(base_doc)
    mutated = copy.deepcopy(base_doc)
    mutated["advert_images"][0]["url"] = "//d18-a.sdn.cz/d_18/other/path.jpeg"
    assert content_hash(mutated) != base
    requeried = copy.deepcopy(base_doc)
    requeried["advert_images"][0]["url"] += "?sig=abc123"
    assert content_hash(requeried) == base


def test_hash_unchanged_when_edited_date_bumps(sample):
    # a bare re-save/re-promotion — the v1 form of the legacy 'Aktualizace' item
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["edited"] = "2026-07-01"
    assert content_hash(mutated) == base


def test_hash_unchanged_when_attachment_url_is_resigned(sample):
    signed = copy.deepcopy(sample)
    signed["sdn_energy_performance_attachment_url"] = (
        "//d18-a.sdn.cz/d_18/c_attachment_qC_A/kQMlcCt5zmD87zEJ9lHAoR5r/5ad9.pdf"
    )
    resigned = copy.deepcopy(sample)
    resigned["sdn_energy_performance_attachment_url"] = (
        "//d18-a.sdn.cz/d_18/c_attachment_qC_A/nO2CysqjGrScbvO8bHApm9y/88a9.pdf"
    )
    assert content_hash(signed) == content_hash(resigned)


def test_hash_changes_when_attachment_appears_or_disappears(sample):
    absent = copy.deepcopy(sample)
    absent["sdn_energy_performance_attachment_url"] = ""
    present = copy.deepcopy(sample)
    present["sdn_energy_performance_attachment_url"] = (
        "//d18-a.sdn.cz/d_18/c_attachment_qC_A/kQMlcCt5zmD87zEJ9lHAoR5r/5ad9.pdf"
    )
    assert content_hash(absent) != content_hash(present)


def test_hash_unchanged_when_premise_review_counters_flap(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["premise"]["review_count"] = 4242
    mutated["premise"]["review_score"] = 4.9
    mutated["premise"]["logo"] = "//d18-a.sdn.cz/d_18/c_img_ob_F/resigned/logo.png"
    mutated["premise"]["premise_paid_firmy"] = 1
    mutated["premise"]["company"]["sos_custom_advert_card"] = True
    assert content_hash(mutated) == base


def test_hash_changes_when_premise_agency_changes(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["premise"]["id"] = 99999
    mutated["premise"]["name"] = "Jiná realitka s.r.o."
    assert content_hash(mutated) != base


def test_volatile_flap_plus_real_change_still_differs(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["advert_images"][0]["kind"] = 4
    mutated["edited"] = "2026-07-01"
    mutated["price_czk"] = 123456789
    assert content_hash(mutated) != base
