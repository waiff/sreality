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
