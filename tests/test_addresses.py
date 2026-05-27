"""Tests for Czech address normalization + similarity (toolkit.addresses).

Pure, hermetic — no DB, no network.
"""

from __future__ import annotations

from toolkit.addresses import address_similarity, extract_psc, normalize_address


def test_normalize_strips_diacritics_and_lowercases():
    assert normalize_address("Náměstí Míru") == "namesti miru"
    assert normalize_address("Žižkov", "Praha 3") == "zizkov praha 3"


def test_normalize_expands_abbreviations():
    assert normalize_address("nám. Republiky") == "namesti republiky"
    assert normalize_address("ul. Dlouhá") == "ulice dlouha"


def test_normalize_skips_empty_parts():
    assert normalize_address("Praha", None, "") == "praha"
    assert normalize_address(None) == ""


def test_extract_psc():
    assert extract_psc("Praha 110 00") == "11000"
    assert extract_psc("11000 Praha") == "11000"
    assert extract_psc("no postal code here") is None
    assert extract_psc(None) is None


def test_address_similarity_identical_is_one():
    assert address_similarity("Praha 2 Vinohrady", "Praha 2 Vinohrady") == 1.0


def test_address_similarity_disjoint_is_zero():
    assert address_similarity("Praha", "Brno") == 0.0


def test_address_similarity_partial_overlap():
    # {praha, 2} vs {praha, 5} -> inter 1 / union 3
    assert abs(address_similarity("Praha 2", "Praha 5") - (1 / 3)) < 1e-9


def test_address_similarity_empty_is_zero():
    assert address_similarity("", "Praha") == 0.0
    assert address_similarity("Praha", None) == 0.0
