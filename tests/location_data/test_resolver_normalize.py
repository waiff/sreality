"""S1 — normalization (03 §3.3), including its named regression cases.

The gazetteer key produced here must be the SAME key the loader writes into
`ruian_name_index.name_norm`, or the resolver matches against a vocabulary that does not
exist. That is asserted against the loader's own function when the RÚIAN-loader branch is
present, rather than by importing it — a resolver replay must not depend on a loader
deploy.
"""

from __future__ import annotations

import pytest

from location_data.resolver import normalize
from tests.location_data import mini_mirror as mm


def test_the_match_key_agrees_with_the_gazetteer_loader():
    loader = pytest.importorskip(
        "location_data.name_index", reason="the RÚIAN loader lands in PR-B"
    )
    for value in [
        "Krásný Les u Frýdlantu", "Nad Bořislavkou", "28. října", "Ústí nad Labem",
        "Brno-střed", "Malá Hraštice", "Praha 6 - Vokovice",
    ]:
        assert normalize.normalize_match_key(value) == loader.normalize_name(value)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Krásný Les", "krasny les"),
        ("Nad Bořislavkou", "nad borislavkou"),
        ("Bilovec", "bilovec"),
        ("Brno-střed", "brno stred"),
        ("28. října", "28 rijna"),
    ],
)
def test_match_keys_are_deaccented_and_punctuation_folded(value, expected):
    assert normalize.normalize_match_key(value) == expected


def test_a_numeric_leading_street_keeps_its_ordinal():
    """bazos's street regex fails on numeric-leading names — `28. října` is stated 3× in
    the corpus and still stored NULL. Named regression test."""
    street, slots = normalize.split_street_and_number("28. října 15")
    assert street == "28. října"
    assert slots["cislo_domovni"] == "15"


def test_a_bare_numeric_leading_street_takes_no_house_number():
    street, slots = normalize.split_street_and_number("17. listopadu")
    assert street == "17. listopadu"
    assert slots == {}


def test_the_street_type_word_is_split_out_but_a_real_type_word_is_kept():
    assert normalize.split_street_type("ul. Slunečná") == ("Slunečná", "ul")
    assert normalize.split_street_type("náměstí Míru") == ("Míru", "náměstí")


def test_glue_words_are_split():
    """'MasarykovaNabízíme' was a real capture."""
    assert normalize.split_glue("MasarykovaNabízíme") == "Masarykova Nabízíme"


def test_the_house_number_is_three_typed_slots_never_one_column():
    assert normalize.normalize_house_number("487/40a") == {
        "cislo_domovni": "487", "cislo_orientacni": "40", "znak_orientacniho": "a",
    }
    assert normalize.normalize_house_number("ev. č. 12") == {"evidencni": "12"}


@pytest.mark.parametrize(
    "raw, psc, hint, rejection",
    [
        ("130 00", "13000", None, None),
        ("37001", "37001", None, None),
        ("-1", None, None, "psc_sentinel"),
        ("987 65", None, "SK", "psc_portal_bucket"),
        # `XX` = the Zahraničí bucket: foreign, country unknown. It never reaches the
        # projection as a code — S2 carries it as status='foreign' with a NULL code.
        ("987 66", None, "XX", "psc_portal_bucket"),
        ("1234", None, None, "psc_malformed"),
    ],
)
def test_psc_normalization_and_the_two_portal_buckets(raw, psc, hint, rejection):
    """31 046 of 84 120 non-null sreality zips are the literal string `-1`; bazos's
    `987 65` / `987 66` are country buckets, not postcodes."""
    assert normalize.normalize_psc(raw) == (psc, hint, rejection)


def test_a_town_written_as_a_street_is_rejected_at_s1_not_at_s3():
    """'a town name written as a street ("Brno") … poisons both the display and the match
    key worse than a NULL would'."""
    claim = mm.claim(1, "street_name", value_text="Praha")
    mirror = mm.default_mirror()
    result = normalize.normalize_claim(
        claim,
        is_place_name=lambda key: bool(mirror.admin_units_by_name(key, levels=("obec",))),
        street_exists=lambda key: False,
    )
    assert result.rejections == ("town_as_street",)


def test_a_real_street_that_shares_a_town_name_survives_when_it_exists_in_the_obec():
    claim = mm.claim(1, "street_name", value_text="Praha")
    result = normalize.normalize_claim(
        claim, is_place_name=lambda key: True, street_exists=lambda key: True
    )
    assert result.rejections == ()


def test_rejections_are_kept_as_outcomes_not_dropped():
    claim = mm.claim(1, "psc", value_text="-1")
    result = normalize.normalize_claim(claim)
    assert result.rejections == ("psc_sentinel",)
    assert result.value_verbatim == "-1"
