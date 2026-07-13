"""Tests for the shared district/location filter builder
(`api.location_filter.district_where`): the single-alias case is pinned
byte-identical to Watchdog's pre-extraction implementation (see
`tests/api/test_notifications.py`); these tests cover the generalisation to
multiple aliases the dedup Decision history + Queue filters need (a pair
matches if EITHER side touches the picked place).
"""

from __future__ import annotations

from api.location_filter import DistrictChip, district_where, parse_district_chips_csv


def test_no_chips_is_a_no_op() -> None:
    where, params = district_where(None, aliases=["l"])
    assert where == []
    assert params == {}
    where, params = district_where([], aliases=["l"])
    assert where == []
    assert params == {}


def test_single_alias_matches_legacy_param_names() -> None:
    where, params = district_where(
        [DistrictChip(name="Jihlava", level="obec", id=586846)], aliases=["l"],
    )
    assert where == ["(l.obec_id = %(district_id_0)s)"]
    assert params == {"district_id_0": 586846}


def test_two_aliases_or_the_chip_across_both_sides() -> None:
    where, params = district_where(
        [DistrictChip(name="Jihlava", level="obec", id=586846)], aliases=["l", "r"],
    )
    assert len(where) == 1
    # Double-wrapped: the per-chip alias-OR group, then the include-group OR
    # (of just this one chip) — harmless, same shape the single-alias locality
    # branch has always produced.
    assert where[0] == (
        "((l.obec_id = %(district_id_l_0)s OR r.obec_id = %(district_id_r_0)s))"
    )
    assert params == {"district_id_l_0": 586846, "district_id_r_0": 586846}


def test_two_alias_legacy_chip_name_match_both_sides() -> None:
    where, params = district_where(
        [DistrictChip(name="Brno", context=None)], aliases=["l", "r"],
    )
    assert len(where) == 1
    assert "l.district ILIKE %(district_name_l_0)s" in where[0]
    assert "r.district ILIKE %(district_name_r_0)s" in where[0]
    assert " OR " in where[0]
    assert params["district_name_l_0"] == "%Brno%"
    assert params["district_name_r_0"] == "%Brno%"


def test_excluded_chip_negated_across_both_aliases() -> None:
    where, params = district_where(
        [DistrictChip(name="Praha", excluded=True)], aliases=["l", "r"],
    )
    assert len(where) == 1
    assert where[0].startswith("NOT (")
    assert "l.district ILIKE %(district_name_l_0)s" in where[0]
    assert "r.district ILIKE %(district_name_r_0)s" in where[0]


def test_mixed_include_exclude_two_aliases() -> None:
    where, params = district_where(
        [
            DistrictChip(name="Praha"),
            DistrictChip(name="Modřany", excluded=True),
        ],
        aliases=["l", "r"],
    )
    assert len(where) == 2
    inc = next(w for w in where if "district_name_l_0" in w)
    exc = next(w for w in where if "district_name_l_1" in w)
    assert not inc.startswith("NOT (")
    assert exc.startswith("NOT (")
    assert params["district_name_l_0"] == "%Praha%"
    assert params["district_name_r_0"] == "%Praha%"
    assert params["district_name_l_1"] == "%Modřany%"
    assert params["district_name_r_1"] == "%Modřany%"


def test_no_aliases_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        district_where([DistrictChip(name="Praha")], aliases=[])


def test_parse_csv_absent_names_is_none() -> None:
    assert parse_district_chips_csv(None) is None
    assert parse_district_chips_csv("") is None


def test_parse_csv_legacy_names_only() -> None:
    chips = parse_district_chips_csv("Praha,Brno")
    assert chips == [
        DistrictChip(name="Praha"),
        DistrictChip(name="Brno"),
    ]


def test_parse_csv_full_shape_round_trips() -> None:
    chips = parse_district_chips_csv(
        names_raw="Jihlava,Modřany",
        ctx_raw=",Praha",
        excl_raw="0,1",
        lvl_raw="obec,",
        id_raw="586846,",
    )
    assert chips == [
        DistrictChip(name="Jihlava", context=None, level="obec", id=586846),
        DistrictChip(name="Modřany", context="Praha", excluded=True),
    ]


def test_parse_csv_unresolved_level_falls_back_to_legacy() -> None:
    # A blank / unknown `districts_lvl` entry drops that chip's level/id
    # entirely, so it takes the legacy ILIKE-by-name path rather than
    # crashing on a bogus admin level.
    chips = parse_district_chips_csv(
        names_raw="Praha", lvl_raw="bogus", id_raw="123",
    )
    assert chips == [DistrictChip(name="Praha")]
