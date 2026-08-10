"""Gazetteer normalization + alias generation. Normalization lives in the loader because
`unaccent()` is STABLE, so it can never be a generated column or an index expression."""

from __future__ import annotations

import pytest

from location_data import name_index


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Praha", "praha"),
        ("Brno-střed", "brno stred"),
        ("Krásný Les", "krasny les"),
        ("Ústí nad Labem", "usti nad labem"),
        ("  Bílovec  ", "bilovec"),
        ("Hrad I. nádvoří", "hrad i nadvori"),
    ],
)
def test_normalize_name(raw, expected):
    assert name_index.normalize_name(raw) == expected


def test_street_word_is_stripped_but_the_type_word_is_not():
    assert name_index.normalize_street_name("ulice Zkušební") == "zkusebni"
    assert name_index.normalize_street_name("ul. Zkušební") == "zkusebni"
    # 'náměstí Míru' is not 'Míru' — folding it would collide with a real street.
    assert name_index.normalize_street_name("náměstí Míru") == "namesti miru"


def test_split_qualifier():
    assert name_index.split_qualifier("Krásný Les u Frýdlantu") == ("Krásný Les", "u Frýdlantu")
    assert name_index.split_qualifier("Ústí nad Labem") == ("Ústí", "nad Labem")
    assert name_index.split_qualifier("Praha") == ("Praha", None)


def test_build_rows_emits_official_deaccented_and_qualifier_stripped():
    rows = name_index.build_rows(
        "obec", 42, "Krásný Les u Frýdlantu",
        is_street=False, parent_obec_unit_id=42, parent_okres_unit_id=7,
        psc_set=["46401", "46401", ""],
    )
    kinds = {r.name_kind: r for r in rows}
    assert set(kinds) == {"official", "deaccented", "qualifier_stripped"}
    assert kinds["official"].name_norm == "krasny les u frydlantu"
    assert kinds["deaccented"].name == "Krasny Les u Frydlantu"
    assert kinds["qualifier_stripped"].name_norm == "krasny les"
    assert all(r.qualifier == "u Frýdlantu" for r in rows)
    assert kinds["official"].psc_set == ["46401"]


def test_build_rows_without_diacritics_has_no_duplicate_alias():
    rows = name_index.build_rows(
        "obec", 1, "Testov", is_street=False,
        parent_obec_unit_id=1, parent_okres_unit_id=None,
    )
    assert [r.name_kind for r in rows] == ["official"]


def test_homonym_count_is_per_kind_and_normalized_name():
    rows = (
        name_index.build_rows("obec", 1, "Krásný Les", is_street=False,
                              parent_obec_unit_id=1, parent_okres_unit_id=None)
        + name_index.build_rows("obec", 2, "Krasny Les", is_street=False,
                                parent_obec_unit_id=2, parent_okres_unit_id=None)
        + name_index.build_rows("ulice", 3, "Krásný Les", is_street=True,
                                parent_obec_unit_id=1, parent_okres_unit_id=None)
    )
    counts = name_index.count_homonyms(rows)
    assert counts[("obec", "krasny les")] == 2
    assert counts[("ulice", "krasny les")] == 1


def test_deaccented_alias_is_what_makes_a_cross_portal_join_possible():
    """ceskereality stores streets ~98 % de-accented and realitymix 27 %."""
    rows = name_index.build_rows(
        "ulice", 9, "Bílovecká", is_street=True,
        parent_obec_unit_id=1, parent_okres_unit_id=None,
    )
    assert {r.name_norm for r in rows} == {"bilovecka"}
    assert {r.name for r in rows} == {"Bílovecká", "Bilovecka"}
