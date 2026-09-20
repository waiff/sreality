"""W2 Task A — the rule floor: E2-E5 pair vetoes, E5's area relation, E34 cluster invariants."""

from __future__ import annotations

import json
from typing import Any

import pytest

from autodedup import features
from autodedup.dataset import Listing, Location
from autodedup.fingerprint import Fingerprint, build_fingerprint
from autodedup.guards import (
    area_relation,
    area_rel_diff,
    cluster_invariants_ok,
    floor_relation,
    pair_veto,
    unit_designator_conflict,
)
from autodedup.settings import Settings

SETTINGS = Settings()


def fp(listing_id: int, **kwargs: Any) -> Fingerprint:
    location = Location(**kwargs.pop("location", {}))
    listing = Listing(id=listing_id, block=kwargs.pop("block", "turnov"),
                      location=location, **kwargs)
    return build_fingerprint(listing, [], SETTINGS)


def test_category_type_mismatch_is_a_veto() -> None:
    assert pair_veto(fp(1, category_type="prodej"), fp(2, category_type="pronajem")) == "category_type"


def test_an_unknown_category_type_is_never_a_mismatch() -> None:
    assert pair_veto(fp(1, category_type="prodej"), fp(2)) is None


def test_byt_never_pairs_with_dum() -> None:
    assert pair_veto(fp(1, category_main="byt"), fp(2, category_main="dum")) == "category_main"


def test_dum_and_komercni_are_the_one_sanctioned_cross_type() -> None:
    assert pair_veto(fp(1, category_main="dum"), fp(2, category_main="komercni")) is None


def test_areas_more_than_eight_percent_apart_are_a_veto() -> None:
    assert pair_veto(fp(1, area_m2=100.0), fp(2, area_m2=91.0)) == "area"
    assert pair_veto(fp(1, area_m2=100.0), fp(2, area_m2=93.0)) is None


def test_a_missing_area_is_never_a_mismatch() -> None:
    assert pair_veto(fp(1, area_m2=100.0), fp(2)) is None


def test_unequal_dispositions_are_a_veto_off_land() -> None:
    a = fp(1, category_main="byt", disposition="3+kk")
    b = fp(2, category_main="byt", disposition="2+kk")
    assert pair_veto(a, b) == "disposition"


def test_land_is_exempt_from_the_disposition_guard() -> None:
    a = fp(1, category_main="pozemek", disposition="3+kk")
    b = fp(2, category_main="pozemek", disposition="2+kk")
    assert pair_veto(a, b) is None


def test_flat_floors_two_apart_are_a_veto_but_one_apart_is_not() -> None:
    far = pair_veto(fp(1, category_main="byt", floor=1), fp(2, category_main="byt", floor=3))
    near = pair_veto(fp(1, category_main="byt", floor=1), fp(2, category_main="byt", floor=2))
    assert far == "floor"
    assert near is None


def test_the_floor_guard_applies_to_flats_only() -> None:
    a = fp(1, category_main="dum", floor=1)
    b = fp(2, category_main="dum", floor=5)
    assert pair_veto(a, b) is None


def test_floor_relation_grades_the_delta() -> None:
    flat = {"category_main": "byt"}
    assert floor_relation(fp(1, floor=2, **flat), fp(2, floor=2, **flat)) == "support"
    assert floor_relation(fp(1, floor=2, **flat), fp(2, floor=3, **flat)) == "band"
    assert floor_relation(fp(1, floor=2, **flat), fp(2, floor=4, **flat)) == "reject"
    assert floor_relation(fp(1, **flat), fp(2, floor=4, **flat)) == "unknown"


@pytest.mark.parametrize(
    "a,b,expected",
    [
        (100.0, 100.0, "support"),
        (100.0, 97.5, "support"),
        (100.0, 95.0, "band"),
        (100.0, 91.0, "reject"),
        (None, 95.0, "unknown"),
        (100.0, None, "unknown"),
        (0.0, 100.0, "unknown"),
    ],
)
def test_area_relation_table(a: float | None, b: float | None, expected: str) -> None:
    assert area_relation(a, b) == expected


def test_area_rel_diff_is_symmetric_against_the_larger_side() -> None:
    assert area_rel_diff(80.0, 100.0) == pytest.approx(0.2)
    assert area_rel_diff(100.0, 80.0) == pytest.approx(0.2)
    assert area_rel_diff(100.0, None) is None


def test_a_clean_cluster_has_no_violated_invariant() -> None:
    members = [
        fp(1, category_main="byt", category_type="prodej", area_m2=68.0,
           disposition="3+kk", floor=4),
        fp(2, category_main="byt", category_type="prodej", area_m2=69.0,
           disposition="3+kk", floor=4),
    ]
    assert cluster_invariants_ok(members, SETTINGS) is None


def test_cluster_invariants_catch_each_violation() -> None:
    flat = {"category_main": "byt", "category_type": "prodej", "area_m2": 68.0}
    base = fp(1, disposition="3+kk", floor=4, **flat)

    oversize = [fp(index, **flat) for index in range(1, SETTINGS.max_cluster_size + 2)]
    assert cluster_invariants_ok(oversize, SETTINGS) == "size"

    mixed_type = [base, fp(2, category_main="byt", category_type="pronajem", area_m2=68.0)]
    assert cluster_invariants_ok(mixed_type, SETTINGS) == "category_type"

    mixed_class = [fp(1, category_main="byt"), fp(2, category_main="pozemek")]
    assert cluster_invariants_ok(mixed_class, SETTINGS) == "compat_class"

    spread = [base, fp(2, category_main="byt", category_type="prodej", area_m2=80.0,
                       disposition="3+kk", floor=4)]
    assert cluster_invariants_ok(spread, SETTINGS) == "area_spread"

    dispositions = [base, fp(2, disposition="2+kk", floor=4, **flat)]
    assert cluster_invariants_ok(dispositions, SETTINGS) == "disposition"

    floors = [base, fp(2, disposition="3+kk", floor=5, **flat)]
    assert cluster_invariants_ok(floors, SETTINGS) == "floor_spread"


def test_land_members_are_exempt_from_the_disposition_invariant() -> None:
    members = [
        fp(1, category_main="pozemek", disposition="3+kk", area_m2=500.0),
        fp(2, category_main="pozemek", disposition="2+kk", area_m2=505.0),
    ]
    assert cluster_invariants_ok(members, SETTINGS) is None


def test_a_must_not_link_pair_refuses_the_union_in_either_order() -> None:
    members = [fp(7, category_main="byt"), fp(3, category_main="byt")]
    assert cluster_invariants_ok(members, SETTINGS, {(3, 7)}) == "must_not_link"
    assert cluster_invariants_ok(members, SETTINGS) is None


@pytest.mark.parametrize(
    "overrides,field",
    [
        ({"area_band_tol": 0.0}, "area_band_tol"),
        ({"area_band_pct": 0.5}, "area_band_pct"),
        ({"t_lo": 0.99}, "t_lo"),
        ({"store_floor": 0.9}, "store_floor"),
        ({"anchor_images": 0}, "anchor_images"),
        ({"max_candidates_per_listing": 0}, "max_candidates_per_listing"),
        ({"simhash_bands": 5}, "64 bits"),
        ({"phash_tight": 20}, "phash_tight"),
        ({"max_cluster_size": 1}, "max_cluster_size"),
        ({"clip_sample": 0}, "clip_sample"),
        ({"phash_sample": 0}, "phash_sample"),
        ({"vocabulary_attr_keys": ("price_unit", "nonsense")}, "nonsense"),
        ({"numeral_conflict_units": ("floor", "storeys")}, "storeys"),
        ({"unit_interior_min": 1.5}, "unit_interior_min"),
        ({"unit_containment_min": -0.1}, "unit_containment_min"),
        ({"developer_catalog_ratio_min": 2.0}, "developer_catalog_ratio_min"),
        ({"unit_rare_tokens_min": -1.0}, "unit_rare_tokens_min"),
        ({"unit_rare_support_interior_min": 1.4}, "unit_rare_support_interior_min"),
        ({"colive_overlap_days": -1.0}, "colive_overlap_days"),
        ({"max_attr_contradictions": 0.0}, "max_attr_contradictions"),
    ],
)
def test_settings_reject_an_incoherent_sweep_row(overrides: dict[str, Any], field: str) -> None:
    """A sweep file must fail at load, naming the field — not mid-run, deep in a probe."""
    with pytest.raises(ValueError) as error:
        Settings(**overrides)
    assert field in str(error.value)


def test_the_default_settings_row_validates() -> None:
    assert Settings() == Settings.from_dict(Settings().to_dict())


def test_the_vocabulary_default_has_ONE_definition() -> None:
    """`features.attribute_conflicts` is also called without a settings row (the judge digest),
    so its fallback and the swept field must be the same tuple, not two that can drift."""
    assert Settings().vocabulary_attr_keys == features.DEFAULT_VOCABULARY_ATTR_KEYS


def test_the_w4c_rules_are_sweepable_and_survive_a_json_round_trip() -> None:
    """E45/E46 and the K-A demotion are settings, not constants: the evaluation prices each one
    by naming it in a sweep file, and JSON's lists must come back as tuples."""
    swept = Settings.from_dict({
        **json.loads(json.dumps(Settings().to_dict())),
        "certificate_ka_enabled": True,
        "unit_evidence_required": False,
        "developer_signature_same_broker_only": True,
        "developer_colive_guard": False,
        "unit_rare_requires_support": True,
        "colive_overlap_days": 7.0,
        "vocabulary_attr_keys": ["price_unit"],
    })
    assert swept.certificate_ka_enabled is True
    assert swept.unit_evidence_required is False
    assert swept.developer_signature_same_broker_only is True
    assert (swept.developer_colive_guard, swept.colive_overlap_days) == (False, 7.0)
    assert swept.unit_rare_requires_support is True
    assert swept.vocabulary_attr_keys == ("price_unit",)
    assert Settings() == Settings.from_dict(json.loads(json.dumps(Settings().to_dict())))


def test_the_image_sample_caps_are_sweepable() -> None:
    """features.py reads these two off the settings row, so a sweep must be able to name them."""
    swept = Settings.from_dict({**Settings().to_dict(), "clip_sample": 12, "phash_sample": 40})
    assert (swept.clip_sample, swept.phash_sample) == (12, 40)


# --- E61: a conflicting unit designator inside one address block ----------------------------


def _unit_listing(listing_id: int, text: str, ruian: int | None = 21778370) -> Listing:
    return Listing(id=listing_id, block="jablonec", description=text,
                   location=Location(ruian_adm_kod=ruian, obec_kod=1))


def test_two_different_units_of_one_building_are_a_veto() -> None:
    """The Zizkov shape (ruian:21778370): one developer, one building, units 1/3/5, each with
    its own price. Nothing about the adverts differs except the number they print."""
    a = _unit_listing(1, "Prodej bytu (č.3) v novostavbě, cena 8 018 187 Kč.")
    b = _unit_listing(2, "Prodej bytu (č.5) v novostavbě, cena 8 076 376 Kč.")
    assert unit_designator_conflict(a, b) == ("3", "5")


def test_the_same_unit_named_twice_is_not_a_conflict() -> None:
    a = _unit_listing(1, "Prodej bytu (č.3) v novostavbě.")
    b = _unit_listing(2, "Nabízíme byt č. 3 v novostavbě.")
    assert unit_designator_conflict(a, b) is None


def test_two_buildings_number_their_flats_independently() -> None:
    a = _unit_listing(1, "Prodej bytu (č.3).")
    b = _unit_listing(2, "Prodej bytu (č.5).", ruian=99999999)
    assert unit_designator_conflict(a, b) is None


def test_a_price_list_of_several_units_is_not_this_advert_s_identity() -> None:
    a = _unit_listing(1, "Volné jednotky č. 3, jednotka č. 5 a jednotka č. 7.")
    b = _unit_listing(2, "Prodej bytu (č.5).")
    assert unit_designator_conflict(a, b) is None


def test_a_side_that_names_no_unit_ends_the_rule() -> None:
    a = _unit_listing(1, "Prodej bytu (č.3).")
    b = _unit_listing(2, "Prodej bytu v novostavbě.")
    assert unit_designator_conflict(a, b) is None


def test_the_veto_is_switchable() -> None:
    a = _unit_listing(1, "Prodej bytu (č.3).")
    b = _unit_listing(2, "Prodej bytu (č.5).")
    from dataclasses import replace

    assert unit_designator_conflict(a, b, SETTINGS) == ("3", "5")
    assert unit_designator_conflict(a, b, replace(SETTINGS, unit_designator_veto=False)) is None
