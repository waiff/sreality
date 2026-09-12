"""The ONE place predicate, API side (`api.location_filter`).

W3 S3 replaced five chip predicates — obec/okres/region equality, a `locality`
pair of obec-equality AND a `place_search_text` ILIKE, and a legacy name
fallback ILIKE-ing across four text columns AND-ed with a `context` narrow —
with `<level>_id = any(codes)`. These tests pin the compiled shape, the sentinel
that makes an unresolvable chip fail CLOSED, and the read-time name upgrade that
keeps a pre-code saved filter working without rewriting its stored blob.

The cross-surface table (this predicate compiles identically in the SPA, the API
and the two RPC bodies) lives in `tests/test_one_place_predicate.py`.
"""

from __future__ import annotations

import pytest

from api.location_filter import (
    NO_MATCH_CODE,
    DistrictChip,
    district_code_plan,
    district_where,
    needs_upgrade,
    parse_district_chips_csv,
    upgrade_district_chips,
)


def test_no_chips_is_a_no_op() -> None:
    where, params = district_where(None, alias="l")
    assert where == []
    assert params == {}
    where, params = district_where([], alias="l")
    assert where == []
    assert params == {}


def test_one_resolved_chip_is_one_equality_on_its_level() -> None:
    where, params = district_where(
        [DistrictChip(name="Jihlava", level="obec", id=586846)], alias="l",
    )
    assert where == ["(l.obec_id = ANY(%(district_codes_obec)s))"]
    assert params == {"district_codes_obec": [586846]}


def test_chips_at_one_level_collapse_into_one_array() -> None:
    where, params = district_where(
        [
            DistrictChip(name="Jihlava", level="obec", id=586846),
            DistrictChip(name="Brno", level="obec", id=582786),
        ],
        alias="l",
    )
    assert where == ["(l.obec_id = ANY(%(district_codes_obec)s))"]
    assert params == {"district_codes_obec": [586846, 582786]}


def test_levels_or_together_coarsest_first() -> None:
    where, params = district_where(
        [
            DistrictChip(name="Žižkov", level="cast_obce", id=490067),
            DistrictChip(name="Jihlava", level="okres", id=3707),
            DistrictChip(name="Vysočina", level="kraj", id=108),
        ],
        alias="l",
    )
    assert where == [
        "(l.region_id = ANY(%(district_codes_kraj)s) "
        "OR l.okres_id = ANY(%(district_codes_okres)s) "
        "OR l.cast_obce_id = ANY(%(district_codes_cast_obce)s))"
    ]
    assert params == {
        "district_codes_kraj": [108],
        "district_codes_okres": [3707],
        "district_codes_cast_obce": [490067],
    }


def test_a_locality_chip_filters_at_the_obec_level() -> None:
    """A street / POI / address pick carries its CONTAINING obec code. The
    `place_search_text ILIKE` half of the old locality predicate is gone: a
    street chip means its municipality until a street-grain code exists."""
    where, params = district_where(
        [DistrictChip(name="Pezinská", level="locality", id=535419)], alias="l",
    )
    assert where == ["(l.obec_id = ANY(%(district_codes_obec)s))"]
    assert params == {"district_codes_obec": [535419]}
    for clause in where:
        assert "ILIKE" not in clause.upper()
        assert "place_search_text" not in clause


def test_a_chip_with_no_code_matches_nothing() -> None:
    """Fail CLOSED. An include chip we cannot resolve must not widen the cohort
    — a Watchdog whose place filter silently becomes "the whole country" mails
    the operator about the whole country."""
    where, params = district_where([DistrictChip(name="Brno")], alias="l")
    assert where == ["(l.obec_id = ANY(%(district_codes_obec)s))"]
    assert params == {"district_codes_obec": [NO_MATCH_CODE]}
    assert NO_MATCH_CODE < 0, "RÚIAN codes are positive; the sentinel must not be one"


def test_excluded_chips_are_negated_separately() -> None:
    where, params = district_where(
        [
            DistrictChip(name="Praha", level="obec", id=554782),
            DistrictChip(name="Modřany", level="cast_obce", id=490017, excluded=True),
        ],
        alias="l",
    )
    assert where == [
        "(l.obec_id = ANY(%(district_codes_obec)s))",
        "NOT (l.cast_obce_id = ANY(%(district_codes_excl_cast_obce)s))",
    ]
    assert params == {
        "district_codes_obec": [554782],
        "district_codes_excl_cast_obce": [490017],
    }


def test_exclude_only_filter_keeps_the_whole_cohort_minus_the_chips() -> None:
    where, _params = district_where(
        [DistrictChip(name="Praha", level="obec", id=554782, excluded=True)], alias="l",
    )
    assert where == ["NOT (l.obec_id = ANY(%(district_codes_excl_obec)s))"]


def test_no_text_column_is_read_any_more() -> None:
    chips = [
        DistrictChip(name="Praha", level="obec", id=554782),
        DistrictChip(name="Brno", context="Jihomoravský kraj"),
        DistrictChip(name="Ostrava", level="okres", id=3811, excluded=True),
    ]
    where, _ = district_where(chips, alias="l")
    blob = " ".join(where)
    for gone in ("l.district", "place_search_text", "l.okres ", "l.region ", "ILIKE"):
        assert gone not in blob, f"{gone!r} is back in the place predicate"


def test_empty_alias_raises() -> None:
    with pytest.raises(ValueError):
        district_where([DistrictChip(name="Praha")], alias="")


# --- the read-time compatibility reader ------------------------------------


def test_needs_upgrade_only_for_chips_without_a_code() -> None:
    assert needs_upgrade([DistrictChip(name="Brno")])
    assert needs_upgrade([DistrictChip(name="Brno", level="obec")])
    assert not needs_upgrade([DistrictChip(name="Brno", level="obec", id=582786)])
    assert not needs_upgrade([DistrictChip(name="X", level="locality", id=1)])
    assert not needs_upgrade([])


def test_a_name_only_chip_is_resolved_once_at_read_time() -> None:
    """The stored blob is NOT migrated (a preset stores the operator's full
    blob); it is resolved on the way into the predicate."""
    stored = [DistrictChip(name="Jihlava", context=None, excluded=False)]

    def resolve(pairs):
        assert list(pairs) == [("Jihlava", None)]
        return {("Jihlava", None): [("obec", 586846), ("okres", 3707)]}

    upgraded = upgrade_district_chips(stored, resolve)
    # One name legitimately answers at two levels — the ILIKE this replaces
    # matched both, so both are kept.
    assert [(c.level, c.id) for c in upgraded] == [("obec", 586846), ("okres", 3707)]
    where, params = district_where(upgraded, alias="l")
    assert params == {"district_codes_okres": [3707], "district_codes_obec": [586846]}
    assert stored[0].level is None, "the stored chip must not be mutated"


def test_an_upgraded_exclude_chip_stays_an_exclude() -> None:
    upgraded = upgrade_district_chips(
        [DistrictChip(name="Modřany", context="Praha", excluded=True)],
        lambda _pairs: {("Modřany", "Praha"): [("cast_obce", 490017)]},
    )
    assert [(c.level, c.id, c.excluded) for c in upgraded] == [
        ("cast_obce", 490017, True)
    ]


def test_an_unresolvable_chip_is_kept_and_matches_nothing() -> None:
    upgraded = upgrade_district_chips(
        [DistrictChip(name="U Kulaťáku")], lambda _pairs: {},
    )
    assert [(c.name, c.level, c.id) for c in upgraded] == [("U Kulaťáku", None, None)]
    _where, params = district_where(upgraded, alias="l")
    assert params == {"district_codes_obec": [NO_MATCH_CODE]}


def test_a_failing_resolver_never_drops_the_filter() -> None:
    def boom(_pairs):
        raise RuntimeError("name index unavailable")

    upgraded = upgrade_district_chips([DistrictChip(name="Brno")], boom)
    assert [(c.name, c.id) for c in upgraded] == [("Brno", None)]
    _where, params = district_where(upgraded, alias="l")
    assert params == {"district_codes_obec": [NO_MATCH_CODE]}


def test_already_resolved_chips_skip_the_resolver_entirely() -> None:
    def never(_pairs):
        raise AssertionError("resolver called for an already-coded chip")

    chips = [DistrictChip(name="Brno", level="obec", id=582786)]
    assert upgrade_district_chips(chips, never) == chips


# --- the wire format --------------------------------------------------------


def test_parse_csv_absent_names_is_none() -> None:
    assert parse_district_chips_csv(None) is None
    assert parse_district_chips_csv("") is None


def test_parse_csv_legacy_names_only() -> None:
    chips = parse_district_chips_csv("Praha,Brno")
    assert chips == [DistrictChip(name="Praha"), DistrictChip(name="Brno")]


def test_parse_csv_full_shape_round_trips() -> None:
    chips = parse_district_chips_csv(
        names_raw="Jihlava,Modřany",
        ctx_raw=",Praha",
        excl_raw="0,1",
        lvl_raw="obec,cast_obce",
        id_raw="586846,490017",
    )
    assert chips == [
        DistrictChip(name="Jihlava", context=None, level="obec", id=586846),
        DistrictChip(
            name="Modřany", context="Praha", excluded=True,
            level="cast_obce", id=490017,
        ),
    ]


def test_parse_csv_unresolved_level_falls_back_to_no_code() -> None:
    chips = parse_district_chips_csv(names_raw="Praha", lvl_raw="bogus", id_raw="123")
    assert chips == [DistrictChip(name="Praha")]
    assert district_code_plan(chips).unresolved == ["Praha"]
