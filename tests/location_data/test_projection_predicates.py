"""The two honesty predicates in their CANONICAL combined form (00 §7.3/§7.4).

`pin_collision_class` is carried verbatim from `pin_clusters.classification` and is NEVER
NULL — an unclustered listing is `'normal'`. The retired `pin_collision_class IS NULL` form
is a never-true test (the vocabulary has no NULL member) that additionally excluded the two
classes that ARE fine; 01 §A.2 check 8 forbids it anywhere in the tree, and this file scans
`location_data/` for it because the schema branch's own scan covers only
scraper/toolkit/api/scripts/migrations.

The measured evidence in both directions:

* bezrealitky's 1:many collapse is genuinely real-world (Rezidence Veletržní 42, River
  Garden, Signature Prague; max cluster 12) and that portal is both the 60.7 %
  address-point-tier outlier and the ground-truth anchor for calibrating every other
  portal's R95 — under the old gate it was permanently ineligible for geometric blocking;
* the idnes cluster at 50.12413, 14.12853 carries 58 rows that are ALL
  `Unhošťská, Kladno - Kročehlavy` WITH a street — homogeneous, and correctly blockable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from location_data.resolver import derived
from location_data.resolver.types import GranularityRank

RANK = GranularityRank()
_PACKAGE = Path(__file__).resolve().parents[2] / "location_data"


def test_the_forbidden_pin_collision_class_is_null_form_appears_nowhere():
    pattern = re.compile(r"pin_collision_class\s+is\s+(not\s+)?null", re.IGNORECASE)
    offenders = [
        f"{path.relative_to(_PACKAGE.parent)}: {match.group(0)}"
        for path in sorted(_PACKAGE.rglob("*.py"))
        for match in pattern.finditer(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "01 §A.2 check 8: use the class-aware predicate "
        "pin_collision_class IN ('normal','building_1_to_many') — " + ", ".join(offenders)
    )


@pytest.mark.parametrize(
    "classification, expected",
    [
        ("normal", True),
        ("building_1_to_many", True),
        ("legitimate_multiunit", False),
        ("town_centroid_suspect", False),
        ("parser_collapse_suspect", False),
        ("foreign_resort_centroid", False),
    ],
)
def test_pin_collision_ok_is_class_aware(classification, expected):
    assert (
        derived.pin_collision_ok(
            pin_collision_class=classification,
            cluster_heterogeneity_ok=True,
            pin_shared_by_n=1,
            threshold_n=4,
        )
        is expected
    )


def test_an_unclustered_row_is_normal_and_therefore_ok():
    assert derived.pin_collision_ok(
        pin_collision_class="normal", cluster_heterogeneity_ok=True,
        pin_shared_by_n=1, threshold_n=4,
    )


def test_the_threshold_is_policy_and_is_read_per_source():
    """bezrealitky's threshold is 12; a 5-listing homogeneous building passes there and
    fails under the global 4."""
    args = dict(
        pin_collision_class="building_1_to_many", cluster_heterogeneity_ok=True,
        pin_shared_by_n=5,
    )
    assert derived.pin_collision_ok(threshold_n=12, **args)
    assert not derived.pin_collision_ok(threshold_n=4, **args)


def test_heterogeneity_alone_can_fail_the_gate():
    assert not derived.pin_collision_ok(
        pin_collision_class="normal", cluster_heterogeneity_ok=False,
        pin_shared_by_n=2, threshold_n=4,
    )


@pytest.mark.parametrize(
    "granularity, position_source, expected",
    [
        ("address_point", "registry_point", True),
        ("building", "portal_pin", True),
        ("street_segment", "portal_pin", True),
        ("street", "portal_pin", False),           # street < street_segment
        ("address_point", "admin_centroid", False),
        ("address_point", "portal_pin_blurred", False),  # 02 §2.2.9's exclusion
        ("address_point", "carried_forward", False),
        ("address_point", "none", False),
    ],
)
def test_geo_blockable_matches_the_canonical_predicate(granularity, position_source, expected):
    assert (
        derived.geo_blockable(
            granularity=granularity, position_source=position_source,
            collision_ok=True, rank=RANK,
        )
        is expected
    )


@pytest.mark.parametrize(
    "granularity, position_source, disputed, expected",
    [
        ("address_point", "registry_point", False, True),
        ("building", "portal_pin", False, True),
        ("street_segment", "portal_pin", False, False),  # street_segment < building
        ("building", "carried_forward", False, False),
        ("building", "portal_pin", True, False),
    ],
)
def test_renderable_as_point_matches_the_canonical_predicate(
    granularity, position_source, disputed, expected
):
    assert (
        derived.renderable_as_point(
            granularity=granularity, position_source=position_source, collision_ok=True,
            location_disputed=disputed, rank=RANK,
        )
        is expected
    )


def test_the_non_complement_row_that_review_m4_rejected_renders_a_circle():
    """granularity='building', position_source='carried_forward', a 40-listing shared pin
    and location_disputed=true evaluated the retired `render_as_circle=false` and rendered
    as a confident pin."""
    collision_ok = derived.pin_collision_ok(
        pin_collision_class="town_centroid_suspect", cluster_heterogeneity_ok=False,
        pin_shared_by_n=40, threshold_n=4,
    )
    renderable = derived.renderable_as_point(
        granularity="building", position_source="carried_forward", collision_ok=collision_ok,
        location_disputed=True, rank=RANK,
    )
    assert renderable is False
    assert derived.render_as(
        renderable=renderable, granularity="building", position_source="carried_forward",
        has_geom=True, rank=RANK,
    ) == "circle"


def test_render_as_and_renderable_as_point_can_never_disagree():
    """`CONSTRAINT llc_render CHECK (renderable_as_point = (render_as = 'point'))`."""
    for granularity in ("unknown", "obec", "street", "street_segment", "building", "address_point"):
        for position_source in ("none", "admin_centroid", "portal_pin", "registry_point"):
            for disputed in (False, True):
                for has_geom in (False, True):
                    renderable = derived.renderable_as_point(
                        granularity=granularity, position_source=position_source,
                        collision_ok=True, location_disputed=disputed, rank=RANK,
                    ) and has_geom
                    rendering = derived.render_as(
                        renderable=renderable, granularity=granularity,
                        position_source=position_source, has_geom=has_geom, rank=RANK,
                    )
                    assert renderable == (rendering == "point")


def test_is_low_precision_is_a_different_question_from_renderability():
    """A coarse-but-honest row is low precision AND still renderable as an area; it must
    never be used as a render gate."""
    assert derived.is_low_precision(granularity="obec", rank=RANK)
    assert not derived.is_low_precision(granularity="street", rank=RANK)
    assert not derived.is_low_precision(granularity="address_point", rank=RANK)


def test_rank_comparisons_never_use_the_enum_ordinality():
    source = (_PACKAGE / "resolver" / "derived.py").read_text(encoding="utf-8")
    assert "rank.at_least" in source
    for forbidden in ('granularity >= "', "granularity >= '", "granularity > '"):
        assert forbidden not in source
