"""Boundary pack: layer mapping, the 8-vs-14 assertion, and the three-geometry contract."""

from __future__ import annotations

import pytest

from location_data import ruian_boundaries as rb


def _feature(level: str, code: int) -> rb.BoundaryFeature:
    return rb.BoundaryFeature(level=level, code=code, name=f"unit {code}", wkb=b"")


def test_region_and_vusc_are_different_layers_and_both_are_asserted():
    region = next(x for x in rb.LAYERS if x.token == "REGION_P")
    vusc = next(x for x in rb.LAYERS if x.token == "VUSC_P")
    assert (region.level, region.expected_features) == ("region_soudrznosti", 8)
    assert (vusc.level, vusc.expected_features) == ("kraj", 14)


def test_feature_count_assertion_catches_the_region_vusc_mixup():
    vusc = next(x for x in rb.LAYERS if x.token == "VUSC_P")
    rb.assert_feature_counts(vusc, [_feature("kraj", i) for i in range(14)])
    with pytest.raises(rb.BoundarySchemaError):
        rb.assert_feature_counts(vusc, [_feature("kraj", i) for i in range(8)])


def test_layers_without_an_expected_count_are_not_asserted():
    obce = next(x for x in rb.LAYERS if x.token == "OBCE_P")
    assert obce.expected_features is None
    rb.assert_feature_counts(obce, [])


def test_default_levels_are_all_real_layers():
    known = {layer.level for layer in rb.LAYERS}
    assert set(rb.DEFAULT_LAYERS) <= known
    # ZSJ (138 MB) is off by default; it is loadable via --levels.
    assert "zsj" not in rb.DEFAULT_LAYERS
    assert "zsj" in known


def test_render_tolerances_are_finer_for_smaller_units():
    by_level = {layer.level: layer.render_tolerance_deg for layer in rb.LAYERS}
    assert by_level["kraj"] > by_level["okres"] > by_level["obec"]
    assert by_level["katastralni_uzemi"] < by_level["obec"]


def test_recorded_tolerance_is_metres_not_degrees():
    obec = next(x for x in rb.LAYERS if x.token == "OBCE_P")
    metres = round(obec.render_tolerance_deg * rb.DEG_TO_M, 3)
    assert 50 <= metres <= 60  # the ~55 m the current production loader uses


def test_authoritative_geometry_is_never_simplified():
    assert "'authoritative', 0, 'none'" in rb._INSERT_AUTHORITATIVE
    assert "Simplify" not in rb._INSERT_AUTHORITATIVE


def test_pip_geometry_is_subdivided_from_the_authoritative_row_only():
    assert "ST_Subdivide" in rb._INSERT_PIP
    assert "purpose = 'authoritative'" in rb._INSERT_PIP
    assert rb.SUBDIVIDE_MAX_VERTICES == 256


def test_render_geometry_records_its_tolerance_and_algorithm():
    assert "ST_SimplifyPreserveTopology" in rb._INSERT_RENDER
    assert "%(tolerance_m)s" in rb._INSERT_RENDER


def test_representative_point_is_paired_with_a_containment_radius():
    """An inscribed-circle CENTRE with the max centre-to-boundary distance — never the
    inscribed radius, which understates uncertainty on elongated units."""
    sql = rb._INSERT_AUTHORITATIVE
    assert "ST_MaximumInscribedCircle" in sql
    assert "ST_MaxDistance(c.center, ST_Boundary(c.geom5514))" in sql


def test_pick_field_is_case_insensitive_and_ordered():
    assert rb._pick_field(["kod", "nazev"], rb._CODE_FIELDS) == "kod"
    assert rb._pick_field(["KOD_KU_", "NAZ_KU"], rb._NAME_FIELDS) == "NAZ_KU"
    assert rb._pick_field(["other"], rb._CODE_FIELDS) is None
