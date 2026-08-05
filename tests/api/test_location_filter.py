"""Tests for the shared district/location filter builder
(`api.location_filter.district_where`): the clauses/param-names are pinned
byte-identical to Watchdog's pre-extraction implementation (see
`tests/api/test_notifications.py`).
"""

from __future__ import annotations

from api.location_filter import DistrictChip, district_where, parse_district_chips_csv


def test_no_chips_is_a_no_op() -> None:
    where, params = district_where(None, alias="l")
    assert where == []
    assert params == {}
    where, params = district_where([], alias="l")
    assert where == []
    assert params == {}


def test_resolved_chip_matches_legacy_param_names() -> None:
    where, params = district_where(
        [DistrictChip(name="Jihlava", level="obec", id=586846)], alias="l",
    )
    assert where == ["(l.obec_id = %(district_id_0)s)"]
    assert params == {"district_id_0": 586846}


def test_legacy_chip_name_matches_across_name_columns() -> None:
    where, params = district_where([DistrictChip(name="Brno", context=None)], alias="l")
    assert len(where) == 1
    assert "l.district ILIKE %(district_name_0)s" in where[0]
    assert "l.place_search_text ILIKE %(district_name_0)s" in where[0]
    assert params["district_name_0"] == "%Brno%"


def test_excluded_chip_negated() -> None:
    where, _params = district_where([DistrictChip(name="Praha", excluded=True)], alias="l")
    assert len(where) == 1
    assert where[0].startswith("NOT (")
    assert "l.district ILIKE %(district_name_0)s" in where[0]


def test_mixed_include_exclude() -> None:
    where, params = district_where(
        [
            DistrictChip(name="Praha"),
            DistrictChip(name="Modřany", excluded=True),
        ],
        alias="l",
    )
    assert len(where) == 2
    inc = next(w for w in where if "district_name_0" in w)
    exc = next(w for w in where if "district_name_1" in w)
    assert not inc.startswith("NOT (")
    assert exc.startswith("NOT (")
    assert params["district_name_0"] == "%Praha%"
    assert params["district_name_1"] == "%Modřany%"


def test_empty_alias_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        district_where([DistrictChip(name="Praha")], alias="")


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
