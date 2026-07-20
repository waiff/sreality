"""Hermetic tests for the Location Audit endpoint's pure helpers:
the coordinate/street acquisition-method inference and the WHERE builder's
presence-filter allowlist (injection safety + the deliberate exclusion of
raw_json-derived predicates). No DB — the SQL itself is validated live.
"""

from __future__ import annotations

from api.routes.location_audit import (
    _DEDUP_REACHABLE_SQL,
    _MATRIX_SQL,
    _PATH_SQL,
    _PRESENCE_SQL,
    _build_where,
    _geom_method,
    _path_meta,
    _street_method,
)
from toolkit.publication import (
    BYT_GEO_ELIGIBLE_PREDICATE,
    GEO_ELIGIBLE_PREDICATE,
    GEO_FAMILIES,
    STREET_ELIGIBLE_PREDICATE,
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
    where, params = _build_where("remax", "byt", "active", [], [], None)
    assert "l.source = %(source)s" in where
    assert "l.category_main = %(category_main)s" in where
    assert "l.is_active = true" in where
    assert params == {"source": "remax", "category_main": "byt"}


def test_build_where_inactive() -> None:
    where, _ = _build_where(None, None, "inactive", [], [], None)
    assert "l.is_active = false" in where


def test_build_where_presence_has_and_missing() -> None:
    where, _ = _build_where(None, None, None, ["street"], ["geom"], None)
    assert _PRESENCE_SQL["street"] in where
    assert f"NOT {_PRESENCE_SQL['geom']}" in where


def test_build_where_unknown_key_is_ignored_not_interpolated() -> None:
    # Injection safety: a key not in the allowlist never reaches the SQL.
    where, params = _build_where(None, None, None, ["street; drop table listings"], [], None)
    assert "drop table" not in where.lower()
    assert params == {}
    assert where == ""  # the bogus key produced no clause


def test_build_where_empty_is_no_where() -> None:
    where, params = _build_where(None, None, None, [], [], None)
    assert where == ""
    assert params == {}


def test_build_where_dedup_reachable_appends_engine_predicate() -> None:
    where, _ = _build_where(None, None, None, [], [], "reachable")
    assert f"({_DEDUP_REACHABLE_SQL})" in where
    assert "NOT (" not in where


def test_build_where_dedup_filters_are_null_safe() -> None:
    """The reachability predicate is THREE-valued: a NULL category_main makes both
    category-gated arms NULL, so a street-ineligible row with no category yields NULL,
    and a bare `NOT (...)` is NULL too — matching nothing. Such rows (197 active in
    production when this was found) fell out of BOTH filters. IS TRUE / IS NOT TRUE
    partitions them, the same null-safe form _publish_sweep already uses."""
    reachable, _ = _build_where(None, None, None, [], [], "reachable")
    unreachable, _ = _build_where(None, None, None, [], [], "unreachable")
    assert reachable.endswith("IS TRUE")
    assert unreachable.endswith("IS NOT TRUE")
    # The two halves must partition, never both-drop or double-count a row.
    assert f"NOT ({_DEDUP_REACHABLE_SQL})" not in unreachable


def test_dedup_predicate_is_column_only_no_raw_json() -> None:
    # Reachability filter must be safe in a WHERE (column-only), unlike raw_json signals.
    assert "raw_json" not in _DEDUP_REACHABLE_SQL
    # It is the three-pass union: street+disposition, geo family, byt-geo.
    assert "disposition" in _DEDUP_REACHABLE_SQL
    assert "geom" in _DEDUP_REACHABLE_SQL
    assert "area_m2" in _DEDUP_REACHABLE_SQL


def test_presence_allowlist_excludes_raw_json_predicates() -> None:
    # raw_json-derived signals are display-only: a jsonb-key test in the WHERE
    # detoasts every scanned row (migration-234 incident class).
    for key in ("coords_source", "inaccuracy_type", "accurate"):
        assert key not in _PRESENCE_SQL
    for pred in _PRESENCE_SQL.values():
        assert "raw_json" not in pred


# --- path scoping (the eligibility matrix's drill-down) --------------------

def test_path_domain_only_without_state() -> None:
    """`path` alone narrows to the pass's DOMAIN — the listings it is supposed to
    cover — without judging any of them eligible or not."""
    where, _ = _build_where(None, None, None, [], [], None, "geo", None)
    families = ", ".join(f"'{f}'" for f in GEO_FAMILIES)
    assert f"l.category_main IN ({families})" in where
    assert "IS TRUE" not in where


def test_path_state_splits_domain_null_safely() -> None:
    elig, _ = _build_where(None, None, None, [], [], None, "geo", "eligible")
    inelig, _ = _build_where(None, None, None, [], [], None, "geo", "ineligible")
    assert f"({GEO_ELIGIBLE_PREDICATE}) IS TRUE" in elig
    assert f"({GEO_ELIGIBLE_PREDICATE}) IS NOT TRUE" in inelig


def test_street_path_has_no_category_gate() -> None:
    """The engine's street pass loads EVERY category carrying street+disposition — that
    it is de-facto byt-only is a data fact (single-dwelling families are ~0% disposition),
    not a rule. Inventing a category gate here would misreport that."""
    where, _ = _build_where(None, None, None, [], [], None, "street", "ineligible")
    assert "category_main" not in where
    assert f"({STREET_ELIGIBLE_PREDICATE}) IS NOT TRUE" in where


def test_byt_geo_path_domain() -> None:
    where, _ = _build_where(None, None, None, [], [], None, "byt_geo", "ineligible")
    assert "l.category_main = 'byt'" in where
    assert f"({BYT_GEO_ELIGIBLE_PREDICATE}) IS NOT TRUE" in where


def test_unknown_path_is_ignored_not_interpolated() -> None:
    where, params = _build_where(
        None, None, None, [], [], None, "geo; drop table listings", "ineligible"
    )
    assert "drop table" not in where.lower()
    assert where == "" and params == {}


def test_path_state_without_path_is_inert() -> None:
    where, _ = _build_where(None, None, None, [], [], None, None, "ineligible")
    assert where == ""


# --- the two non-location eligibility inputs -------------------------------

def test_new_presence_keys_match_the_canonical_predicates_verbatim() -> None:
    """A matrix cell counts 'missing disposition' / 'missing area' using the engine's
    predicate; clicking it must land on the SAME rows. That only holds if the presence
    SQL is spelled exactly as the predicate spells it — `disposition IS NOT NULL` with
    no `<> ''`, and the same area coalesce order."""
    assert _PRESENCE_SQL["disposition"] in STREET_ELIGIBLE_PREDICATE
    assert _PRESENCE_SQL["disposition"] in BYT_GEO_ELIGIBLE_PREDICATE
    assert _PRESENCE_SQL["area"] in GEO_ELIGIBLE_PREDICATE
    assert _PRESENCE_SQL["area"] in BYT_GEO_ELIGIBLE_PREDICATE


def test_geom_and_obec_presence_keys_match_geo_predicate() -> None:
    # The geo passes' other two inputs, likewise drillable from a cell.
    assert _PRESENCE_SQL["geom"] in GEO_ELIGIBLE_PREDICATE
    assert _PRESENCE_SQL["obec_id"] in GEO_ELIGIBLE_PREDICATE


# --- the eligibility matrix ------------------------------------------------

def test_matrix_selects_the_engines_own_verdicts() -> None:
    """The client derives only the REASON; eligibility itself is read off the engine's
    predicates, so the matrix can never disagree with the row view beside it."""
    for pred in (
        STREET_ELIGIBLE_PREDICATE,
        GEO_ELIGIBLE_PREDICATE,
        BYT_GEO_ELIGIBLE_PREDICATE,
    ):
        assert pred in _MATRIX_SQL


def test_matrix_groups_every_selected_key() -> None:
    """11 grouped expressions + count(*). A missing ordinal would silently collapse
    buckets — the counts would still add up, so nothing else would catch it."""
    assert "GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11" in _MATRIX_SQL
    # 3 plain columns carry no alias; the 8 derived ones do, plus count(*) AS n.
    for alias in (
        "has_street",
        "has_disposition",
        "has_geom",
        "has_obec",
        "has_area",
        "elig_street",
        "elig_geo",
        "elig_byt_geo",
    ):
        assert f"AS {alias}" in _MATRIX_SQL
    assert _MATRIX_SQL.count(" AS ") == 9
    for col in ("l.source,", "l.category_main,", "l.is_active,"):
        assert col in _MATRIX_SQL


def test_matrix_is_column_only_and_placeholder_free() -> None:
    # No raw_json (detoast) and no literal % (psycopg would read it as a placeholder).
    assert "raw_json" not in _MATRIX_SQL
    assert "%" not in _MATRIX_SQL


def test_matrix_input_booleans_mirror_the_presence_predicates() -> None:
    """Each bucket key is the same test the presence filter uses — that identity is what
    makes a cell's count and its drill-down agree."""
    for key in ("street", "disposition", "geom", "obec_id", "area"):
        assert _PRESENCE_SQL[key].strip("()") in _MATRIX_SQL


def test_path_meta_is_derived_not_hardcoded() -> None:
    meta = {m["key"]: m for m in _path_meta()}
    assert meta["street"]["domain_categories"] is None  # no category gate
    assert meta["geo"]["domain_categories"] == list(GEO_FAMILIES)
    assert meta["byt_geo"]["domain_categories"] == ["byt"]
    # Only the geo passes gate on activity; the street pass keeps inactive rows so a
    # delisted listing can still merge and complete a property's price history.
    assert meta["street"]["active_only"] is False
    assert meta["geo"]["active_only"] is True
    assert meta["byt_geo"]["active_only"] is True


def test_path_sql_covers_exactly_the_three_engine_passes() -> None:
    assert set(_PATH_SQL) == {"street", "geo", "byt_geo"}
    assert [elig for _, elig in _PATH_SQL.values()] == [
        STREET_ELIGIBLE_PREDICATE,
        GEO_ELIGIBLE_PREDICATE,
        BYT_GEO_ELIGIBLE_PREDICATE,
    ]
