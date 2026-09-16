"""Pure-function tests for the region census: scoring, rendering, arg coercion, SQL params."""

from __future__ import annotations

import pytest

from autodedup import census


def _block(**over):
    base = {
        "grain": "town",
        "block_code": 1,
        "block_name": "Blok",
        "n_total": 3000,
        "n_active": 1000,
        "n_sources": 9,
        "cat_byt": 900,
        "cat_dum": 900,
        "cat_pozemek": 600,
        "cat_komercni": 600,
        "cat_ostatni": 0,
        "type_prodej": 2000,
        "type_pronajem": 1000,
        "n_listings_with_images": 2700,
        "shared_pin_listings": 0,
        "raw_inblock_pairs": 1234,
    }
    base.update(over)
    return base


def test_perfect_block_scores_top() -> None:
    assert census.score_block(_block()).total == pytest.approx(1.0)


def test_missing_categories_and_sources_rank_lower() -> None:
    perfect = census.score_block(_block()).total
    thin_sources = census.score_block(_block(n_sources=2)).total
    thin_categories = census.score_block(_block(cat_komercni=5, cat_pozemek=5)).total
    assert thin_sources < perfect
    assert thin_categories < perfect


def test_single_deal_type_loses_the_whole_type_component() -> None:
    both = census.score_block(_block())
    sale_only = census.score_block(_block(type_pronajem=0))
    assert both.types == 1.0 and sale_only.types == 0.0
    assert both.total - sale_only.total == pytest.approx(census.WEIGHTS["types"])


def test_image_share_below_target_is_proportional() -> None:
    score = census.score_block(_block(n_listings_with_images=1050))  # 0.35 share of 0.7 target
    assert score.images == pytest.approx(0.5)


def test_size_band_penalises_both_directions() -> None:
    inside = census.score_block(_block(n_total=3000)).size
    small = census.score_block(_block(n_total=750)).size
    big = census.score_block(_block(n_total=10000)).size
    assert inside == 1.0
    assert small == pytest.approx(0.5)
    assert big == pytest.approx(0.5)


def test_shared_pin_share_penalises() -> None:
    clean = census.score_block(_block())
    dirty = census.score_block(_block(shared_pin_listings=3000))
    assert dirty.pin_penalty == 1.0
    assert dirty.total < clean.total


def test_missing_detail_scores_zero_images_not_a_crash() -> None:
    block = _block()
    del block["n_listings_with_images"]
    del block["shared_pin_listings"]
    assert census.score_block(block).images == 0.0


def test_ranking_order_is_by_total_score() -> None:
    blocks = [_block(block_code=1), _block(block_code=2, n_sources=1, type_pronajem=0)]
    ranked = sorted(blocks, key=lambda b: -census.score_block(b).total)
    assert [b["block_code"] for b in ranked] == [1, 2]


def test_render_table_is_one_line_per_block_plus_header() -> None:
    rows = [dict(_block(), score=0.9), dict(_block(block_code=2), score=0.4)]
    out = census.render_table(rows)
    lines = out.splitlines()
    assert len(lines) == 4
    assert lines[0].startswith("grain")
    assert "0.900" in lines[2]


def test_render_table_truncates_long_names() -> None:
    out = census.render_table([dict(_block(block_name="A" * 60), score=0.1)])
    assert "…" in out


def test_parse_census_args_defaults_and_coercion() -> None:
    assert census.parse_census_args({}) == census.ARG_DEFAULTS
    parsed = census.parse_census_args({"min_n": "10", "top": "3"})
    assert parsed["min_n"] == 10 and parsed["top"] == 3
    assert parsed["max_n"] == census.ARG_DEFAULTS["max_n"]


@pytest.mark.parametrize(
    "args",
    [
        {"nope": "1"},
        {"min_n": "abc"},
        {"min_n": "9000", "max_n": "10"},
        {"top": "0"},
        {"timeout_s": "-1"},
    ],
)
def test_parse_census_args_rejects_bad_input(args: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        census.parse_census_args(args)


def test_detail_params_binds_one_grain_and_nulls_the_other() -> None:
    town = census.detail_params(grain="town", block_code=554782, pin_share_min=10)
    quarter = census.detail_params(grain="quarter", block_code=490067, pin_share_min=10)
    assert town == {"obec_kod": 554782, "cast_obce_kod": None, "pin_share_min": 10}
    assert quarter == {"obec_kod": None, "cast_obce_kod": 490067, "pin_share_min": 10}
    with pytest.raises(ValueError):
        census.detail_params(grain="obec", block_code=1, pin_share_min=10)


def test_block_sql_exists_for_every_grain() -> None:
    assert set(census.BLOCK_SQL_BY_GRAIN) == set(census.GRAINS)


def test_sql_placeholders_match_the_params_the_callers_build() -> None:
    supplied = set(census.block_params(min_n=1, max_n=2, top=3))
    for grain in census.GRAINS:
        assert census.sql_placeholders(census.BLOCK_SQL_BY_GRAIN[grain]) == supplied
    detail_supplied = set(census.detail_params(grain="town", block_code=1, pin_share_min=10))
    assert census.sql_placeholders(census.CENSUS_DETAIL_SQL) == detail_supplied


def test_both_grain_statements_project_the_same_columns() -> None:
    def aliases(sql: str) -> list[str]:
        return [line.split(" AS ")[-1].strip().rstrip(",") for line in sql.splitlines() if " AS " in line]

    assert aliases(census.CENSUS_TOWNS_SQL) == aliases(census.CENSUS_QUARTERS_SQL)


def test_every_portal_and_category_has_its_own_counter() -> None:
    for sql in census.BLOCK_SQL_BY_GRAIN.values():
        for source in census.SOURCES:
            assert f"AS src_{source}" in sql
        for cat in census.CATEGORY_MAIN:
            assert f"AS cat_{cat}" in sql
        for deal in census.CATEGORY_TYPE:
            assert f"AS type_{deal}" in sql


def _served_clause() -> str:
    """The geom/country_status test from the ONE pinned definition, minus its EXISTS wrapper."""
    from location_data.claims_common import served_location_predicate

    rendered = served_location_predicate("l.id", alias="ll")
    _, sep, tail = rendered.partition(" AND ")
    assert sep, rendered
    return tail[:-1]


def test_served_clause_matches_the_one_pinned_definition() -> None:
    clause = _served_clause()
    assert clause == "(ll.geom IS NOT NULL OR ll.country_status = 'foreign')"
    for sql in census.BLOCK_SQL_BY_GRAIN.values():
        assert clause in sql
    assert clause in census.CENSUS_COVERAGE_SQL


def test_coverage_statement_binds_no_parameters() -> None:
    assert census.sql_placeholders(census.CENSUS_COVERAGE_SQL) == set()


def test_block_statements_carry_no_distinct_aggregate() -> None:
    for sql in census.BLOCK_SQL_BY_GRAIN.values():
        assert "count(distinct" not in sql.lower()
        assert "AS n_sources" in sql


def test_detail_statement_types_both_nullable_grain_codes() -> None:
    sql = census.CENSUS_DETAIL_SQL
    for name in ("obec_kod", "cast_obce_kod"):
        assert f"%({name})s IS NULL" not in sql
        assert f"%({name})s::bigint IS NULL" in sql


def test_timeout_is_clamped_to_the_documented_ceiling() -> None:
    assert census.parse_census_args({"timeout_s": str(census.TIMEOUT_S_MAX)})["timeout_s"] == (
        census.TIMEOUT_S_MAX
    )
    with pytest.raises(ValueError):
        census.parse_census_args({"timeout_s": str(census.TIMEOUT_S_MAX + 1)})
