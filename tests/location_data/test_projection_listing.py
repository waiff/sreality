"""The answer row: 27 columns, and the list is the contract.

`listing_location` (migration 501) replaces `listing_location_current`'s 81. The test that
matters is the one below: the builder's keys and the migration's columns are the SAME list,
so a column added to one and not the other fails here rather than at the first INSERT.

The row is a CACHE, never truth — truncating it is always legal and the drain is its only
writer.
"""

from __future__ import annotations

import re
from pathlib import Path

from location_data.resolver import core, projection
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm

MIGRATION = (
    Path(__file__).resolve().parents[2] / "migrations"
    / "501_location_w2a_listing_location.sql"
)

# The 27, spelled out. Transcribing them is the point: this list and the DDL are two
# independent statements of the same contract, and they are compared below.
EXPECTED_COLUMNS = (
    "listing_id",
    "geom",
    "country_code", "kraj_name", "okres_name", "obec_name", "cast_obce_name",
    "street_name", "house_number_cp", "house_number_co", "psc",
    "kraj_kod", "okres_kod", "obec_kod", "cast_obce_kod", "ulice_kod", "ruian_adm_kod",
    "match_confidence", "granularity", "uncertainty_radius_m",
    "country_status", "disputed", "pin_shared_by_n",
    "resolver_version", "resolved_at", "claim_set_hash", "registry_version",
)


def _ddl_columns() -> list[str]:
    sql = MIGRATION.read_text(encoding="utf-8")
    sql = re.sub(r"--[^\n]*", "", sql)
    body = sql.split("create table listing_location (", 1)[1]
    depth, out, current = 0, [], []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    out.append("".join(current).strip())
    return [c.split()[0] for c in out if c.strip()]


def _row(claims):
    resolution = core.resolve(
        claims, mm.context(), resolver_version=RESOLVER_VERSION,
        registry_version="ruian:2026-07-31",
    )
    return projection.build_listing_row(resolution)


def _address_claims():
    return [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
        mm.claim(3, "coordinate", lat=50.10102, lon=14.34804, declared_precision_label="gps"),
        mm.claim(4, "psc", value_text="160 00"),
    ]


def test_the_table_is_exactly_these_twenty_seven_columns():
    assert _ddl_columns() == list(EXPECTED_COLUMNS)
    assert len(EXPECTED_COLUMNS) == 27
    assert list(projection.LISTING_LOCATION_COLUMNS) == list(EXPECTED_COLUMNS)


def test_the_builder_binds_every_column_the_statement_does_not_default():
    """`geom` is built in SQL from the lat/lon pair and `resolved_at` is the statement's own
    `now()`; everything else travels as a named parameter."""
    row = _row(_address_claims())
    expected = (set(EXPECTED_COLUMNS) - {"geom", "resolved_at"}) | {"lat", "lon"}
    assert set(row) == expected
    assert set(projection.ROW_PARAMS) == expected


def test_no_column_the_wave_deleted_came_back():
    """The 54 that went. Four classes: provably NULL on every row, reachable through a join,
    derivable at read, or the output of an engine this wave deletes."""
    gone = {
        "source", "property_id", "resolution_id", "policy_version", "registry_version_id",
        "is_cz", "display_label", "display_path", "place_search_text", "admin_path",
        "position_source", "blur_evidence", "radius_semantics", "position_licence_class",
        "match_components", "field_provenance", "geom_claim_id", "street_claim_id",
        "pin_cluster_id", "pin_collision_class", "cluster_heterogeneity_ok",
        "pin_shared_by_n_25m", "pin_shared_by_n_100m", "collision_epoch_id",
        "position_quality_class", "render_as", "renderable_as_point", "is_low_precision",
        "geo_blockable", "location_disputed", "distance_to_nearest_boundary_m",
        "history_completeness", "addr_block_key", "building_block_key", "street_block_key",
        "geo_cell_key", "h3_r10", "momc_kod", "ku_kod", "pou_kod", "orp_kod",
        "obec_unit_id", "cast_obce_unit_id", "okres_unit_id", "kraj_unit_id",
        "admin_assignment_method", "admin_position_source", "admin_sliver_distance_m",
        "evidencni", "postal_town", "development_name", "country_method",
        "country_confidence", "country_driving_claim_ids", "stavebni_objekt_kod",
        "parcela_id", "built_at",
    }
    assert gone & set(EXPECTED_COLUMNS) == set()
    assert gone & set(projection.ROW_PARAMS) == set()


def test_the_row_carries_its_three_version_inputs():
    """`claim_set_hash` says the claims moved, `resolver_version` says a rule moved,
    `registry_version` says the mirror moved. Those three are what the sweep compares."""
    row = _row(_address_claims())
    assert row["resolver_version"] == RESOLVER_VERSION
    assert row["registry_version"] == "ruian:2026-07-31"
    assert len(row["claim_set_hash"]) == 64  # sha256, hex, decoded to bytea by the statement


def test_the_grade_columns_are_never_null():
    """A NULL axis reads as "no gate" and fails open — a NULL radius makes both branches of
    the three-valued containment test evaluate NULL, so the row silently drops out of
    `certain` AND `possible`. The DDL says NOT NULL; the builder must never try."""
    for claims in (_address_claims(), []):
        row = _row(claims)
        for column in ("granularity", "match_confidence", "uncertainty_radius_m",
                       "country_status", "pin_shared_by_n"):
            assert row[column] is not None, column


def test_the_position_travels_as_a_lat_lon_pair_and_may_be_absent():
    row = _row(_address_claims())
    assert (row["lat"], row["lon"]) == (50.10100, 14.34800)
    empty = _row([mm.claim(1, "obec_name", value_text="Neexistující")])
    assert (empty["lat"], empty["lon"]) == (None, None)


def test_a_foreign_row_carries_the_country_and_nothing_else():
    row = _row([
        mm.claim(1, "address_line_verbatim", value_text="Benahavís, Španělsko", source="idnes"),
    ])
    assert (row["country_status"], row["country_code"]) == ("foreign", "ES")
    for column in ("obec_kod", "obec_name", "street_name", "psc", "ruian_adm_kod"):
        assert row[column] is None, column
