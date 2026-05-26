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
    mutated["params"]["stats"] = (mutated["params"].get("stats") or 0) + 123
    assert content_hash(mutated) == base


def test_hash_unchanged_when_session_state_changes(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["note"] = "a personal note"
    mutated["rus"] = {"anything": True}
    mutated["rusReply"] = "x"
    assert content_hash(mutated) == base


def test_hash_changes_when_price_changes(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["priceCzk"] = 18000
    mutated["priceSummaryCzk"] = 18000
    assert content_hash(mutated) != base


def test_hash_changes_when_a_real_attribute_changes(sample):
    base = content_hash(sample)
    mutated = copy.deepcopy(sample)
    mutated["params"]["buildingCondition"] = {"name": "Po rekonstrukci", "value": 4}
    assert content_hash(mutated) != base
