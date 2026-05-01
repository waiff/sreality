"""Tests for the content_hash function."""

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


def test_hash_unchanged_when_only_topped_flips(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["is_topped"] = not mutated["is_topped"]
    mutated["is_topped_today"] = not mutated["is_topped_today"]
    assert content_hash(mutated) == base


def test_hash_unchanged_when_aktualizace_value_changes(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    for item in mutated["items"]:
        if item.get("name") == "Aktualizace":
            item["value"] = "Před týdnem"
    assert content_hash(mutated) == base


def test_hash_unchanged_when_user_session_changes(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["_embedded"]["favourite"]["is_favourite"] = True
    mutated["logged_in"] = False
    assert content_hash(mutated) == base


def test_hash_changes_when_price_changes(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["price_czk"]["value_raw"] = 18000
    assert content_hash(mutated) != base


def test_hash_changes_when_a_real_attribute_changes(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    for item in mutated["items"]:
        if item.get("name") == "Stav objektu":
            item["value"] = "Po rekonstrukci"
    assert content_hash(mutated) != base
