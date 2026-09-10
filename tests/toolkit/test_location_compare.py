"""Hermetic guards for the old-vs-new location comparison (location W6).

No DB and no HTTP: a fake connection records the SQL, so what is pinned here is
the CONTRACT — the verdict CASE covers every `admin_assignment_method` label in
migration 380, the level allowlist rejects junk before it reaches Postgres, the
`*_SQL` constants stay parameterized (the schema-aware PREPARE sweep in CI is
the other half of that guard), and the legacy radius bbox is byte-for-byte the
arithmetic Browse's `centerRadiusBbox` sends.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import pytest

from toolkit import location_compare as lc

_ROOT = Path(__file__).resolve().parents[2]


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((sql, params))

    def fetchall(self) -> list[dict[str, Any]]:
        return self._conn.rows_for(self._conn.executed[-1][0])

    def fetchone(self) -> dict[str, Any] | None:
        rows = self.fetchall()
        return rows[0] if rows else None


class _Txn:
    def __enter__(self) -> "_Txn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    autocommit = True

    def __init__(self, canned: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.canned = canned or {}

    def rows_for(self, sql: str) -> list[dict[str, Any]]:
        for needle, rows in self.canned.items():
            if needle in sql:
                return rows
        return []

    def cursor(self, row_factory: Any = None) -> _Cur:
        return _Cur(self)

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def transaction(self) -> _Txn:
        return _Txn()


def _sql_constants() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(lc).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }


# --------------------------------------------------------------------------
# The verdict definition (05 5.3.3 A).
# --------------------------------------------------------------------------

_MIGRATION_380 = _ROOT / "migrations" / "380_location_w1_enums_and_config.sql"


def _admin_assignment_method_labels() -> list[str]:
    text = _MIGRATION_380.read_text(encoding="utf-8")
    block = re.search(
        r"create type admin_assignment_method as enum \((.*?)\);", text, re.DOTALL
    )
    assert block, "migration 380 no longer declares admin_assignment_method"
    return re.findall(r"'([a-z_]+)'", block.group(1))


def test_every_assignment_method_label_is_decided_by_the_verdict_case():
    labels = _admin_assignment_method_labels()
    assert set(labels) == {
        "registry",
        "pip_containment",
        "pip_nearest_within_n_m",
        "unresolved_sliver",
        "outside_country",
        "claimed",
        "unresolved",
    }
    verdict = lc._admin_verdict("s")
    # registry/claimed -> certain, pip_containment -> the scalar comparison,
    # pip_nearest_within_n_m -> possible; the other three fall to the ELSE 'no'.
    for label in ("registry", "claimed", "pip_containment", "pip_nearest_within_n_m"):
        assert f"'{label}'" in verdict, label
    assert "ELSE 'no'" in verdict
    for label in ("unresolved_sliver", "outside_country", "unresolved"):
        assert f"'{label}'" not in verdict, f"{label} must fall through to ELSE 'no'"


def test_pip_containment_compares_the_two_stored_scalars_not_geometry():
    verdict = lc._admin_verdict("s")
    assert "s.uncertainty_radius_m" in verdict
    assert "<= s.distance_to_nearest_boundary_m" in verdict
    assert "ST_" not in verdict


def test_cast_obce_is_certain_only_for_registry_or_claimed():
    branch = lc._VERDICT_CTE.split("'cast_obce' THEN")[1].split("ELSE")[0]
    assert "IN ('registry', 'claimed')\n                     THEN 'certain'" in branch
    assert "IN ('pip_containment', 'pip_nearest_within_n_m')" in branch


def test_street_ranks_granularity_through_the_rank_table_never_enum_order():
    assert "FROM location_granularity_rank" in lc._VERDICT_CTE
    assert "s.granularity_rank >=" in lc._VERDICT_CTE
    assert "IN ('medium', 'high', 'exact')" in lc._VERDICT_CTE
    # An enum-ordinal comparison would look like `granularity >= 'street'`.
    assert "granularity >= 'street'" not in lc._VERDICT_CTE


def test_street_is_possible_only_on_low_confidence_never_a_catch_all():
    """05 5.3.3 (A): 'possible when ulice_kod matches but confidence is low; no
    otherwise' - an ELSE 'possible' would badge a sub-street-rank match as a
    street hit."""
    branch = lc._VERDICT_CTE.split("'street' THEN")[1].split("WHEN %(level)s")[0]
    assert "WHEN s.match_confidence = 'low' THEN 'possible'" in branch
    assert "ELSE 'no' END" in branch
    assert "ELSE 'possible'" not in branch


def test_every_documented_reason_string_is_reachable():
    for reason in (
        "no_projection_row",
        "assigned_elsewhere",
        "unresolved",
        "outside_country",
        "possible_only",
        "moved_in",
    ):
        assert f"'{reason}'" in lc._REASON, reason


def test_render_state_is_read_off_the_row_never_re_derived():
    assert "box.render_as" in lc._MAP_ROWS_SQL
    assert "box.renderable_as_point" in lc._MAP_ROWS_SQL
    assert "position_source IN" not in lc._MAP_ROWS_SQL
    assert "is_low_precision" not in lc._MAP_ROWS_SQL


def test_radius_uses_the_dwithin_prefilter_and_never_st_buffer():
    for sql in (lc._RADIUS_COUNTS_SQL, lc._RADIUS_ROWS_SQL):
        assert "ST_Buffer" not in sql
    assert "ST_DWithin" in lc._RADIUS_COUNTS_SQL
    assert "r.dist_m + coalesce(r.uncertainty_radius_m, 0) <=" in lc._RADIUS_CTE
    assert "r.dist_m - coalesce(r.uncertainty_radius_m, 0) <=" in lc._RADIUS_CTE


def test_the_cohort_is_the_union_of_both_sides():
    assert "b.region_id = ANY(%(kraje)s::bigint[])" in lc._COHORT_CTE
    assert "OR p.kraj_kod = ANY(%(kraje)s::bigint[])" in lc._COHORT_CTE
    assert "w.listing_id = p.winner_listing_id" in lc._COHORT_CTE
    assert "b.is_active" in lc._COHORT_CTE


# --------------------------------------------------------------------------
# SQL shape.
# --------------------------------------------------------------------------


def test_sql_constants_are_named_parameterized_and_template_free():
    constants = _sql_constants()
    assert len(constants) >= 14, f"only {len(constants)} discovered — the scan broke"
    for name, sql in constants.items():
        assert "{" not in sql, f"{name} carries a format/f-string slot"
        assert "}" not in sql, f"{name} carries a format/f-string slot"
        # A bare %s would be a positional placeholder; only %(name)s is allowed.
        assert not re.search(r"(?<!%)%[sbt]\b", sql), f"{name} uses a positional %s"
        assert re.search(r"%\([a-z_]+\)s", sql), f"{name} has no named placeholder"


def test_no_route_of_this_module_writes():
    for name, sql in _sql_constants().items():
        upper = sql.upper()
        for verb in ("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ", "CREATE ", "DROP "):
            assert verb not in upper, f"{name} is not read-only"


def test_the_level_is_a_bound_parameter_not_interpolated_sql():
    assert "%(level)s::text" in lc._SCOPED_CTE
    for name in ("kraj", "okres", "obec", "cast_obce", "street"):
        assert f"WHEN '{name}'" in lc._SCOPED_CTE, name


# --------------------------------------------------------------------------
# Input validation.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("junk", ["region", "ulice", "obec; drop table listings", ""])
def test_level_allowlist_rejects_junk(junk):
    with pytest.raises(lc.CompareInputError):
        lc.check_level(junk)


def test_unit_list_levels_are_narrower_than_the_detail_levels():
    for level in ("obec", "cast_obce"):
        assert lc.check_level(level, allowed=lc.UNIT_LIST_LEVELS) == level
    for level in ("kraj", "okres", "street"):
        with pytest.raises(lc.CompareInputError):
            lc.check_level(level, allowed=lc.UNIT_LIST_LEVELS)


@pytest.mark.parametrize(
    "raw,expected",
    [(None, [19, 27]), ("", [19, 27]), ("19", [19]), ("19,27, 31", [19, 27, 31])],
)
def test_parse_kraje_accepts_int_lists_and_defaults_to_praha_stredocesky(raw, expected):
    assert lc.parse_kraje(raw) == expected


@pytest.mark.parametrize("raw", ["nineteen", "19,abc", "19;27", "1.5"])
def test_parse_kraje_rejects_non_integers(raw):
    with pytest.raises(lc.CompareInputError):
        lc.parse_kraje(raw)


# The seeded location_constants.cz_bbox row (migration 380), as the envelope
# query returns it — the ONE canonical box, not a narrower module-level copy.
_CZ_BBOX_ROWS = {
    "FROM location_constants": [
        {"west": 12.0, "south": 48.0, "east": 19.0, "north": 51.5}
    ]
}


@pytest.mark.parametrize("radius_m", [49, 0, -1, 50_001, 1_000_000])
def test_radius_rejects_an_out_of_range_radius(radius_m):
    conn = _FakeConn(dict(_CZ_BBOX_ROWS))
    with pytest.raises(lc.CompareInputError):
        lc.radius(conn, lat=50.0, lng=14.4, radius_m=radius_m, kraje=[19], limit=10)
    assert conn.executed == []


@pytest.mark.parametrize("lat,lng", [(0.0, 0.0), (52.0, 14.4), (50.0, 25.0), (47.9, 14.4)])
def test_radius_rejects_a_point_outside_the_cz_bbox(lat, lng):
    with pytest.raises(lc.CompareInputError):
        lc.radius(
            _FakeConn(dict(_CZ_BBOX_ROWS)),
            lat=lat, lng=lng, radius_m=1000, kraje=[19], limit=10,
        )


def test_radius_reads_the_cz_bbox_from_location_constants_not_a_literal():
    """Migration 380 owns the envelope; 48.2 N is inside it and must be served."""
    assert not hasattr(lc, "CZ_BBOX")
    conn = _FakeConn({
        **_CZ_BBOX_ROWS,
        "AS max_u": [{"max_u": 0.0}],
        "AS old_bbox_count": [
            {"old_bbox_count": 0, "new_certain": 0, "new_possible": 0}
        ],
    })
    out = lc.radius(conn, lat=48.2, lng=18.95, radius_m=1000, kraje=[19], limit=5)
    assert out["centre"] == {"lat": 48.2, "lng": 18.95, "radius_m": 1000}


# --------------------------------------------------------------------------
# The legacy radius: a BBOX, not a circle.
# --------------------------------------------------------------------------


def _reference_center_radius_bbox(lat: float, lng: float, radius_m: float) -> dict:
    """Independent transcription of frontend/src/lib/queries.ts centerRadiusBbox."""
    earth = 6_371_000
    d_lat = (radius_m / earth) * (180 / math.pi)
    d_lng = (radius_m / (earth * math.cos((lat * math.pi) / 180))) * (180 / math.pi)
    return {
        "south": lat - d_lat,
        "north": lat + d_lat,
        "west": lng - d_lng,
        "east": lng + d_lng,
    }


@pytest.mark.parametrize(
    "lat,lng,radius_m",
    [(50.0755, 14.4378, 1000.0), (49.1951, 16.6068, 5000.0), (50.5, 14.0, 250.0)],
)
def test_center_radius_bbox_reproduces_browse_exactly(lat, lng, radius_m):
    assert lc.center_radius_bbox(lat, lng, radius_m) == _reference_center_radius_bbox(
        lat, lng, radius_m
    )


def test_center_radius_bbox_known_value_prague_1km():
    box = lc.center_radius_bbox(50.0755, 14.4378, 1000.0)
    # 1 km of latitude on a 6 371 km sphere is 0.008992... degrees.
    one_km_deg = math.degrees(1000.0 / 6_371_000)
    assert box["north"] - box["south"] == pytest.approx(2 * one_km_deg, rel=1e-12)
    # Longitude degrees are stretched by 1/cos(lat); at 50.0755 N that is ~1.56x.
    assert (box["east"] - box["west"]) / (box["north"] - box["south"]) == pytest.approx(
        1 / math.cos(math.radians(50.0755)), rel=1e-12
    )


def test_radius_passes_the_browse_bbox_and_the_max_uncertainty_prefilter():
    conn = _FakeConn(
        {
            **_CZ_BBOX_ROWS,
            "AS max_u": [{"max_u": 250.0}],
            "AS old_bbox_count": [
                {"old_bbox_count": 4, "new_certain": 3, "new_possible": 1}
            ],
            "'only_old' AS side": [],
        }
    )
    lc.radius(conn, lat=50.0755, lng=14.4378, radius_m=1000.0, kraje=[19], limit=5)
    params = [p for sql, p in conn.executed if p and "prefilter_m" in p][0]
    expected = lc.center_radius_bbox(50.0755, 14.4378, 1000.0)
    for key, value in expected.items():
        assert params[key] == value
    assert params["prefilter_m"] == 1250.0


# --------------------------------------------------------------------------
# Call plumbing.
# --------------------------------------------------------------------------


def test_scope_runs_four_reads_and_publishes_the_denominators():
    conn = _FakeConn(
        {
            "k AS (SELECT unnest": [
                {"kraj_kod": 19, "name": "Praha", "n_old": 200, "n_agree": 150,
                 "n_new_certain": 140, "n_new_possible": 20, "n_new_no": 30,
                 "n_no_row": 20, "n_only_old": 50, "n_only_new": 10}
            ]
        }
    )
    out = lc.scope(conn, [19, 27])
    assert out["kraje"] == [19, 27]
    assert "generated_at" in out
    row = out["kraje_rows"][0]
    assert row["agreement_pct"] == 75.0
    assert "n_agree" not in row
    for key in ("n_old", "n_new_certain", "n_new_possible", "n_new_no", "n_no_row",
                "n_only_old", "n_only_new"):
        assert key in row
    assert len([p for _, p in conn.executed if p is not None]) == 4


def test_units_picks_the_obec_query_for_obec_and_the_parts_query_for_cast_obce():
    conn = _FakeConn()
    lc.units(conn, level="obec", parent_kod=3100, kraje=[19])
    assert any("codes AS (" in sql for sql, _ in conn.executed)
    conn = _FakeConn()
    lc.units(conn, level="cast_obce", parent_kod=554782, kraje=[19])
    assert any("parts AS (" in sql for sql, _ in conn.executed)


def test_units_rejects_a_detail_only_level():
    with pytest.raises(lc.CompareInputError):
        lc.units(_FakeConn(), level="street", parent_kod=1, kraje=[19])


def test_unit_detail_resolves_a_street_to_its_obec_and_ilike_pattern():
    conn = _FakeConn(
        {
            "FROM ruian_streets s": [
                {"ulice_kod": 1, "name": "Kaprova", "obec_kod": 554782}
            ],
            "AS n_old": [
                {"n_old": 3, "n_new_certain": 2, "n_new_possible": 1, "n_new_no": 0,
                 "n_no_row": 0, "n_only_old": 1, "n_only_new": 0, "n_claimed": 0,
                 "by_method": [], "by_source": []}
            ],
        }
    )
    out = lc.unit_detail(conn, level="street", code=1, kraje=[19], limit=10)
    assert out["name"] == "Kaprova"
    params = [p for _, p in conn.executed if p and "name_pat" in p][0]
    assert params["name_pat"] == "%Kaprova%"
    assert params["parent_obec_kod"] == 554782
    assert params["level"] == "street"
    assert set(out["counts"]) == {
        "n_old", "n_new_certain", "n_new_possible", "n_new_no",
        "n_no_row", "n_only_old", "n_only_new", "n_claimed",
    }


def test_unit_detail_splits_the_two_row_lists_and_drops_the_side_column():
    conn = _FakeConn(
        {
            "AS n_old": [
                {"n_old": 0, "n_new_certain": 0, "n_new_possible": 0, "n_new_no": 0,
                 "n_no_row": 0, "n_only_old": 0, "n_only_new": 0, "n_claimed": 0,
                 "by_method": [], "by_source": []}
            ],
            "'only_old' AS side": [
                {"side": "only_old", "property_id": 1, "reason": "assigned_elsewhere"},
                {"side": "only_new", "property_id": 2, "reason": "moved_in"},
            ],
        }
    )
    out = lc.unit_detail(conn, level="obec", code=554782, kraje=[19], limit=10)
    assert [r["property_id"] for r in out["only_old"]] == [1]
    assert [r["property_id"] for r in out["only_new"]] == [2]
    assert all("side" not in r for r in out["only_old"] + out["only_new"])


def test_unit_detail_rejects_a_name_only_level_whose_code_resolves_to_nothing():
    """No registry row means the old side would be all-false: a 400, not n_old = 0."""
    for level in ("street", "cast_obce"):
        with pytest.raises(lc.CompareInputError):
            lc.unit_detail(_FakeConn(), level=level, code=999_999, kraje=[19], limit=10)


def test_cast_obce_units_never_pairs_every_part_with_every_cohort_row():
    sql = lc._UNITS_CAST_OBCE_SQL
    assert "JOIN local m ON true" not in sql
    assert "GROUP BY 1, 2, 3, 4" in sql  # the old side is grouped before the ILIKE


def test_unit_detail_rejects_junk_levels_before_touching_the_db():
    conn = _FakeConn()
    with pytest.raises(lc.CompareInputError):
        lc.unit_detail(conn, level="ulice", code=1, kraje=[19], limit=10)
    assert conn.executed == []


def test_no_geom_either_is_counted_over_the_cohort_not_the_visible_box():
    """A row with no position on either side can never satisfy the bbox test."""
    head = lc._MAP_COUNTS_SQL.split("AS no_geom_either")[0]
    subquery = head[head.rindex("(SELECT") :]
    assert "FROM cohort" in subquery
    assert "FROM box" not in subquery


def test_map_rows_reports_truncation_against_the_full_box_count():
    conn = _FakeConn(
        {
            "AS n_total": [
                {"n_total": 3, "both": 2, "only_old_geom": 1, "only_new_geom": 0,
                 "moved_gt_100m": 1, "demoted_to_circle": 0, "no_geom_either": 0}
            ],
            "ST_Y(box.new_geom)": [{"property_id": 1}, {"property_id": 2}],
        }
    )
    out = lc.map_rows(
        conn, west=14.0, south=50.0, east=14.5, north=50.5, kraje=[19], limit=2
    )
    assert out["truncated"] is True
    assert "n_total" not in out["counts"]
    assert set(out["counts"]) == {
        "both", "only_old_geom", "only_new_geom",
        "moved_gt_100m", "demoted_to_circle", "no_geom_either",
    }
