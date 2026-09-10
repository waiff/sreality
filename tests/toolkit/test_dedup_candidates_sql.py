"""The path C SQL, offline: the three forms of each rung statement share ONE select body,
every placeholder a statement names is one the lane supplies, the area band never separates
two areas the tolerance accepts, the location read stays on the projection (rule 24), and the
column order matches migration 492. CI's schema-replay job PREPAREs the statements
themselves; the lane's `verify` mode holds them to the oracle on real towns."""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

from toolkit import dedup_candidates_sql as sql
from toolkit import dedup_candidates as dc
from toolkit import dedup_sim_settings as dss

_ROOT = Path(__file__).resolve().parent.parent.parent
_PLACEHOLDER = re.compile(r"%\((\w+)\)s")
_MARK = " SELECT %(inputs_id)s::bigint"  # where every rung's pair select begins

INPUTS = dc.path_inputs("C", dss.effective_settings(None))


def _placeholders(statement: str) -> set[str]:
    return set(_PLACEHOLDER.findall(statement))


def _lane_args() -> dict[str, object]:
    return {**sql.rung_params(INPUTS), "block_key": "554782", "id_from": 0, "id_to": 1 << 62,
            "inputs_id": 1, "generation_id": 1}


def test_each_rung_has_insert_count_and_rows_over_one_select_body() -> None:
    for rung, stmts in sql.RUNG_SQL.items():
        body = _MARK + stmts["rows"].split(_MARK, 1)[1]  # the pair select: list + joins + where
        assert body in stmts["insert"], rung
        assert body in stmts["count"], rung
        assert stmts["insert"].startswith("WITH base AS (")
        assert " INSERT INTO dedup_sim.candidate_pairs (" in stmts["insert"]
        assert stmts["insert"].rstrip().endswith("last_seen_at = now()")
        assert stmts["count"].startswith("WITH base AS (") and " SELECT count(*) FROM (" in stmts["count"]
        assert f"'{rung}'" in body


def test_every_placeholder_a_rung_statement_names_is_one_the_lane_supplies() -> None:
    supplied = set(_lane_args())
    for rung, stmts in sql.RUNG_SQL.items():
        for form, statement in stmts.items():
            missing = _placeholders(statement) - supplied
            assert not missing, f"{rung}/{form} names {missing}"


def test_placeholders_are_all_named_and_cast_where_their_type_is_not_implied() -> None:
    corpus = [s for stmts in sql.RUNG_SQL.values() for s in stmts.values()] + [
        sql.BLOCKS_SQL, sql.BLOCK_IDS_SQL, sql.STALE_SWEEP_SQL, sql.FUNNEL_SQL, sql.TOP_BUCKETS_SQL,
        sql.PAIR_MATRIX_SQL, sql.LISTINGS_WITH_CANDIDATES_SQL, sql.PAIRS_PER_BLOCK_SQL,
        sql.BLOCK_NAMES_SQL, sql.BLOCK_ATTRS_SQL, sql.TOWN_ASSIGNMENT_SQL,
    ]
    for statement in corpus:
        assert "%s" not in statement.replace("%(", ""), "positional placeholder"
        # a bare parameter in a SELECT list has no type for PREPARE; every one is cast
        for m in re.finditer(r"%\((\w+)\)s(?!::)", statement):
            name = m.group(1)
            before = statement[max(0, m.start() - 40):m.start()]
            assert re.search(r"(=|>=|<=|<>|<|>|IN \(|ANY\()\s*$", before), (
                f"{name} is neither cast nor compared: …{before!r}")


def test_the_location_read_stays_on_the_projection_never_the_legacy_columns() -> None:
    corpus = "\n".join(
        [s for stmts in sql.RUNG_SQL.values() for s in stmts.values()]
        + [sql.BLOCKS_SQL, sql.BLOCK_IDS_SQL, sql.FUNNEL_SQL, sql.TOP_BUCKETS_SQL, sql.BLOCK_ATTRS_SQL,
           sql.TOWN_ASSIGNMENT_SQL, sql.BLOCK_NAMES_SQL]
    ).lower()
    for legacy in ("x.geom", "x.obec_id", "x.okres_id", "x.region_id", "x.ku_id", "x.obec ", "x.street",
                   "street_name_key", "geocode_cache", "browse_list", "locality_district_id"):
        assert legacy not in corpus, legacy
    assert "listing_location_current" in corpus
    # the granularity floor is applied by RANK, never by enum order
    assert "location_granularity_rank" in corpus
    assert "granularity >=" not in corpus and "granularity > " not in corpus


def test_c1_needs_both_dispositions_and_c3_needs_one_missing() -> None:
    c1 = sql.RUNG_SQL["C1"]["rows"]
    assert "JOIN base b ON b.disposition = a.disposition" in c1
    assert "WHERE a.disposition IS NOT NULL" in c1
    c3 = sql.RUNG_SQL["C3"]["rows"]
    assert "WHERE (a.disposition IS NULL OR b.disposition IS NULL)" in c3
    assert "FROM band a" in c3 and "JOIN (VALUES (-1), (0), (1)) d(off) ON TRUE" in c3
    assert "b.band = a.band + d.off" in c3


def test_category_guard_and_floor_rule_are_the_same_predicates_on_both_rungs() -> None:
    for rung in ("C1", "C3"):
        s = sql.RUNG_SQL[rung]["rows"]
        assert "a.category_type IS NULL OR b.category_type IS NULL OR a.category_type = b.category_type" in s
        assert "(a.category_main = 'dum' AND b.category_main = 'komercni')" in s
        assert "(a.category_main = 'komercni' AND b.category_main = 'dum')" in s
        assert "ABS(a.floor - b.floor) <= %(floor_tolerance)s::int" in s
        assert "b.listing_id > a.listing_id" in s


def test_area_is_the_plot_for_land_and_zero_means_missing() -> None:
    assert "CASE WHEN x.category_main = 'pozemek'" in sql.RUNG_SQL["C3"]["rows"]
    assert "CASE WHEN x.estate_area > 0 THEN x.estate_area END" in sql.RUNG_SQL["C3"]["rows"]
    assert "CASE WHEN x.usable_area > 0 THEN x.usable_area END" in sql.RUNG_SQL["C3"]["rows"]
    assert "'pozemek' IN (a.category_main, b.category_main)" in sql.RUNG_SQL["C3"]["rows"]


@pytest.mark.parametrize("general,pozemek", [(5, 2), (2, 5), (10, 10), (0.5, 0.1), (50, 2)])
def test_band_never_separates_two_areas_within_the_widest_tolerance(general: float, pozemek: float) -> None:
    w = sql.band_width(general, pozemek)
    t = max(general, pozemek) / 100.0
    for a in [7.0, 10.0, 33.3, 55.5, 60.0, 99.99, 100.0, 100.01, 148.5, 1000.0, 12345.6, 99999.0]:
        for b in (a * (1 - t), a / (1 - t), a * (1 - t / 2), a, a * (1 - t) + 1e-9):
            # b within t of a as a percent of the larger side
            assert abs(a - b) / max(a, b) <= t + 1e-12
            band_a, band_b = math.floor(math.log(a) / w), math.floor(math.log(b) / w)
            assert abs(band_a - band_b) <= 1, (a, b, band_a, band_b)


def test_floor_guard_is_two_valued_when_a_category_is_unknown() -> None:
    # category_main is nullable; `NULL = 'byt'` would make the whole guard NULL and either
    # drop the pair or write NULL into a NOT NULL column. The oracle treats None as "not byt".
    for rung in ("C1", "C3"):
        s = sql.RUNG_SQL[rung]["rows"]
        assert "COALESCE(a.category_main, '') = 'byt' AND COALESCE(b.category_main, '') = 'byt'" in s
        assert "(a.category_main = 'byt' AND b.category_main = 'byt'" not in s


def test_band_is_a_bigint_and_a_zero_tolerance_stays_in_range() -> None:
    # tolerance 0 is a registry-legal value ("areas must match exactly"); the sentinel
    # width makes ln(area)/w ~1e10 for a 100 m2 flat — far outside int4, inside bigint.
    assert "::bigint AS band" in sql.RUNG_SQL["C3"]["rows"]
    w = sql.band_width(0, 0)
    for area in (1.0, 8.6, 100.0, 12345.0, 1e9):
        band = math.floor(math.log(area) / w)
        assert -(2**63) < band < 2**63 - 1
    assert math.floor(math.log(100.0) / w) == math.floor(math.log(100.0) / w)
    assert math.floor(math.log(100.0) / w) != math.floor(math.log(100.01) / w)


def test_band_width_edges() -> None:
    assert sql.band_width(5, 2) == pytest.approx(-math.log(0.95))
    assert sql.band_width(100, 2) == 1e9
    assert sql.band_width(0, 0) == 1e-9


def test_rung_params_come_from_the_inputs() -> None:
    p = sql.rung_params(INPUTS)
    assert p == {
        "floor_tolerance": 2, "area_pct_general": 5.0, "area_pct_pozemek": 2.0,
        "band_width": pytest.approx(-math.log(0.95)), "active_only": False,
    }
    assert sql.rung_params({**INPUTS, "l0_candidate_scope": "active"})["active_only"] is True


def test_pair_column_order_matches_migration_492() -> None:
    migration = (_ROOT / "migrations" / "492_new_dedup_candidate_store.sql").read_text(encoding="utf-8")
    body = migration.lower().split("create table if not exists dedup_sim.candidate_pairs (")[1].split(");")[0]
    columns = [line.split()[0] for line in body.splitlines() if line.strip() and not line.strip().startswith(("--", "primary", "check"))]
    for name in sql.PAIR_COLUMN_NAMES:
        assert name in columns, name
    assert len(sql.PAIR_COLUMN_NAMES) == 16
    # the select lists have exactly as many expressions as the insert names (count the
    # top-level commas of the select list of each rung)
    for rung, stmts in sql.RUNG_SQL.items():
        select_list = stmts["rows"].split(_MARK, 1)[1].split(" FROM ", 1)[0]
        depth, commas = 0, 0
        for ch in select_list:
            depth += ch == "("
            depth -= ch == ")"
            commas += ch == "," and depth == 0
        assert commas + 1 == len(sql.PAIR_COLUMN_NAMES), rung


def test_stale_sweep_is_scoped_to_one_parameter_set() -> None:
    assert "WHERE inputs_id = %(inputs_id)s::bigint AND generation_id <> %(generation_id)s::bigint" in sql.STALE_SWEEP_SQL


def test_listings_with_candidates_groups_rather_than_distincts() -> None:
    assert "DISTINCT" not in sql.LISTINGS_WITH_CANDIDATES_SQL.upper()
    assert "GROUP BY id, category_main" in sql.LISTINGS_WITH_CANDIDATES_SQL
