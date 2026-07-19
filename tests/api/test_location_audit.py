"""Hermetic tests for the Location Audit endpoint's pure helpers:
the coordinate/street acquisition-method inference and the WHERE builder's
presence-filter allowlist (injection safety + the deliberate exclusion of
raw_json-derived predicates). No DB — the SQL itself is validated live.
"""

from __future__ import annotations

from api.routes.location_audit import (
    _PRESENCE_SQL,
    _build_where,
    _geom_method,
    _street_method,
)


# --- _geom_method ----------------------------------------------------------

def test_geom_method_absent_is_none() -> None:
    assert _geom_method("sreality", False, None) is None


def test_geom_method_native_portal_unstamped_is_page_native() -> None:
    # Fully-native portals never stamp coords.source; a present geom is native.
    assert _geom_method("sreality", True, None) == "page_native"
    assert _geom_method("mmreality", True, None) == "page_native"


def test_geom_method_coordresolver_tags() -> None:
    assert _geom_method("idnes", True, "page") == "page_native"
    assert _geom_method("idnes", True, "carry_forward") == "carry_forward"
    assert _geom_method("realitymix", True, "geocode") == "geocoded"


def test_geom_method_bazos_three_tier() -> None:
    assert _geom_method("bazos", True, "street") == "geocoded_street"
    assert _geom_method("bazos", True, "link") == "map_link_pin"
    assert _geom_method("bazos", True, "locality") == "geocoded_town"
    # bazos with no tag still resolves to a shown value, not a crash.
    assert _geom_method("bazos", True, None) == "page_native"


def test_geom_method_unknown_tag_passes_through() -> None:
    # A future tag surfaces raw rather than being hidden or mislabelled.
    assert _geom_method("idnes", True, "satellite") == "satellite"


# --- _street_method --------------------------------------------------------

def test_street_method_absent_is_none() -> None:
    assert _street_method("remax", False, "parser", False) is None


def test_street_method_resolver_and_llm() -> None:
    assert _street_method("idnes", True, "resolver", False) == "ruian_resolver"
    assert _street_method("bazos", True, "llm", False) == "llm"


def test_street_method_structured_id_wins() -> None:
    # A numeric street_id means the portal handed us a structured id-keyed street.
    assert _street_method("sreality", True, "parser", True) == "structured_id"


def test_street_method_structured_text_for_passthrough_portals() -> None:
    assert _street_method("mmreality", True, "parser", False) == "structured_text"
    assert _street_method("bezrealitky", True, None, False) == "structured_text"


def test_street_method_free_text_for_mining_portals() -> None:
    assert _street_method("remax", True, "parser", False) == "free_text"
    assert _street_method("idnes", True, "parser", False) == "free_text"


# --- _build_where ----------------------------------------------------------

def test_build_where_scalar_filters_bind_params() -> None:
    where, params = _build_where("remax", "byt", "active", [], [])
    assert "l.source = %(source)s" in where
    assert "l.category_main = %(category_main)s" in where
    assert "l.is_active = true" in where
    assert params == {"source": "remax", "category_main": "byt"}


def test_build_where_inactive() -> None:
    where, _ = _build_where(None, None, "inactive", [], [])
    assert "l.is_active = false" in where


def test_build_where_presence_has_and_missing() -> None:
    where, _ = _build_where(None, None, None, ["street"], ["geom"])
    assert _PRESENCE_SQL["street"] in where
    assert f"NOT {_PRESENCE_SQL['geom']}" in where


def test_build_where_unknown_key_is_ignored_not_interpolated() -> None:
    # Injection safety: a key not in the allowlist never reaches the SQL.
    where, params = _build_where(None, None, None, ["street; drop table listings"], [])
    assert "drop table" not in where.lower()
    assert params == {}
    assert where == ""  # the bogus key produced no clause


def test_build_where_empty_is_no_where() -> None:
    where, params = _build_where(None, None, None, [], [])
    assert where == ""
    assert params == {}


def test_presence_allowlist_excludes_raw_json_predicates() -> None:
    # raw_json-derived signals are display-only: a jsonb-key test in the WHERE
    # detoasts every scanned row (migration-234 incident class).
    for key in ("coords_source", "inaccuracy_type", "accurate"):
        assert key not in _PRESENCE_SQL
    for pred in _PRESENCE_SQL.values():
        assert "raw_json" not in pred
