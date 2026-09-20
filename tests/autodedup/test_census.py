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


# --- probe layer -------------------------------------------------------------------------


def test_block_layer_counts_source_url_and_the_detail_layer_the_text_signals() -> None:
    """Description-based counters detoast every row, so they live in the per-block detail pass
    (a corpus-wide pass timed out at 540 s the first time they rode in the block layer)."""
    for sql in census.BLOCK_SQL_BY_GRAIN.values():
        assert "AS with_source_url" in sql
        assert "with_desc200" not in sql and "no_signal_at_all" not in sql
    for alias in ("with_desc200", "no_signal_at_all"):
        assert f"AS {alias}" in census.CENSUS_DETAIL_SQL


def test_no_signal_at_all_is_the_conjunction_of_every_missing_probe() -> None:
    assert (
        "(coalesce(l.area_m2, 0) <= 0 AND l.disposition IS NULL\n"
        "         AND l.broker_identity_id IS NULL\n"
        "         AND length(coalesce(substr(l.description, 1, 200), '')) < 200)"
    ) in census.CENSUS_DETAIL_SQL


def test_zero_area_counts_as_absent_everywhere_in_the_block_layer() -> None:
    """`with_area` filters area_m2 > 0, so the recall ceiling must not read 0 as a signal."""
    for sql in census.BLOCK_SQL_BY_GRAIN.values():
        assert "count(*) FILTER (WHERE area_m2 > 0)" in sql
        assert "l.area_m2 IS NULL AND l.disposition IS NULL" not in sql


def test_description_length_tests_are_slice_friendly() -> None:
    """length(substr(...)) lets Postgres fetch one TOAST chunk; length(whole column) cannot."""
    for sql in census.BLOCK_SQL_BY_GRAIN.values():
        assert "description" not in sql
    assert "length(coalesce(l.description, ''))" not in census.CENSUS_DETAIL_SQL
    assert census.CENSUS_DETAIL_SQL.count("length(coalesce(substr(l.description, 1, 200), ''))") == 2


def test_every_probe_has_a_unique_name_and_statement() -> None:
    probes = census.BLOCK_PROBES + census.CORPUS_PROBES
    assert len({p.name for p in probes}) == len(probes)
    assert len({p.sql for p in probes}) == len(probes)


def test_block_probe_placeholders_are_supplied_by_probe_params() -> None:
    supplied = set(census.probe_params(grain="town", block_code=1, hash_share_min=5))
    assert supplied == {"obec_kod", "cast_obce_kod", "hash_share_min"}
    for probe in census.BLOCK_PROBES:
        names = census.sql_placeholders(probe.sql)
        assert names <= supplied, probe.name
        assert {"obec_kod", "cast_obce_kod"} <= names, probe.name


def test_block_probes_type_both_nullable_grain_codes() -> None:
    for probe in census.BLOCK_PROBES:
        for name in ("obec_kod", "cast_obce_kod"):
            assert f"%({name})s IS NULL" not in probe.sql, probe.name
            assert f"%({name})s::bigint IS NULL" in probe.sql, probe.name


def test_repost_probe_requires_category_equality() -> None:
    """sale != rent and flat != house are hard merge rules, so a cross-type relist is not a
    re-post; it is counted, but on its own line."""
    sql = census.CENSUS_PROBE_REPOSTS_SQL
    assert "b.category_main IS NOT DISTINCT FROM a.category_main" in sql
    assert "b.category_type IS NOT DISTINCT FROM a.category_type" in sql
    assert "AS repost_pairs," in sql and "AS repost_pairs_cross_type," in sql
    assert "WHERE same_category" in sql and "WHERE NOT same_category" in sql


def test_the_repost_probe_ends_an_advert_at_its_last_sighting() -> None:
    """E62: `inactive_at` is the DETECTION stamp — up to 70 days after the advert went — so a
    co-live window measured from it invents overlap the crawler never saw."""
    sql = census.CENSUS_PROBE_REPOSTS_SQL
    assert "coalesce(l.last_seen_at, l.inactive_at, now()) AS ended_at" in sql
    assert "coalesce(l.inactive_at, now())" not in sql


def test_repost_area_tolerance_does_not_depend_on_row_order() -> None:
    assert "0.02 * least(a.area_m2, b.area_m2)" in census.CENSUS_PROBE_REPOSTS_SQL
    assert "0.02 * a.area_m2" not in census.CENSUS_PROBE_REPOSTS_SQL


def test_address_density_probe_carries_its_denominator() -> None:
    sql = census.CENSUS_PROBE_ADDRESS_DENSITY_SQL
    assert "AS n_block_listings" in sql and "AS n_addressed_listings" in sql


def test_shared_hash_probe_returns_the_groups_not_only_a_count() -> None:
    """Exact-dHash groups include document collapse, so the operator must be able to look."""
    sql = census.CENSUS_PROBE_SHARED_HASH_SQL
    assert "AS top_hash_groups" in sql
    for key in ("'phash'", "'n_listings'", "'n_sources'", "'sample_listing_id'"):
        assert key in sql


def test_median_images_is_a_json_number_not_a_decimal() -> None:
    assert "::numeric(10, 2)" not in census.CENSUS_PROBE_PORTAL_SIGNAL_SQL
    assert "::float8\n" in census.CENSUS_PROBE_PORTAL_SIGNAL_SQL


def test_shared_hash_probe_types_its_threshold() -> None:
    assert "%(hash_share_min)s::int" in census.CENSUS_PROBE_SHARED_HASH_SQL
    assert "%(hash_share_min)s\n" not in census.CENSUS_PROBE_SHARED_HASH_SQL


def test_corpus_probe_params_bind_only_what_the_statement_names() -> None:
    by_name = {p.name: p for p in census.CORPUS_PROBES}
    assert set(by_name) == {"source_location", "ingest_rate"}
    assert census.corpus_probe_params(by_name["source_location"], source="remax") == {
        "source": "remax"
    }
    assert census.corpus_probe_params(by_name["ingest_rate"], source="remax") == {}
    assert census.sql_placeholders(census.CENSUS_PROBE_INGEST_SQL) == set()
    assert "%(source)s::text" in census.CENSUS_PROBE_SOURCE_LOCATION_SQL


def test_probe_params_rejects_an_unknown_grain() -> None:
    with pytest.raises(ValueError):
        census.probe_params(grain="okres", block_code=1, hash_share_min=5)


def test_parse_census_args_carries_the_probe_switches() -> None:
    assert census.ARG_DEFAULTS["probes"] == 1
    assert census.ARG_DEFAULTS["hash_share_min"] == 5
    assert census.parse_census_args({"probes": "0"})["probes"] == 0
    for bad in ({"probes": "2"}, {"hash_share_min": "1"}):
        with pytest.raises(ValueError):
            census.parse_census_args(bad)


def test_parse_probes_args_defaults_and_validation() -> None:
    assert census.parse_probes_args({}) == {"timeout_s": 540, "source": "remax"}
    assert census.parse_probes_args({"source": "bazos"})["source"] == "bazos"
    for bad in ({"nope": "1"}, {"source": "nosuchportal"}, {"timeout_s": "x"}, {"timeout_s": "0"}):
        with pytest.raises(ValueError):
            census.parse_probes_args(bad)


def test_probe_row_flattens_every_section_and_survives_a_failed_probe() -> None:
    block = {
        "grain": "town", "block_code": 1, "block_name": "B", "n_total": 3000,
        "with_desc200": 2500, "with_source_url": 3000, "no_signal_at_all": 40,
        "probes": {
            "portal_signal": [{"source": "sreality"}, {"source": "bazos"}],
            "reposts": {
                "repost_pairs": 61, "repost_pairs_cross_type": 4,
                "repost_listings": 98, "repost_candidates": 400,
            },
            "address_density": {
                "n_addr_3plus_multi_floor": 12, "n_addr_dense_listings": 80,
                "n_addressed_listings": 900,
            },
            "shared_hash": {"error": "RuntimeError: timeout"},
        },
    }
    row = census.probe_row(block)
    assert row["n_portals"] == 2
    assert row["repost_pairs"] == 61 and row["repost_listings"] == 98
    assert row["repost_pairs_cross_type"] == 4
    assert row["n_addressed_listings"] == 900
    assert row["n_addr_3plus_multi_floor"] == 12
    assert row["n_shared_hashes"] is None
    table = census.render_table([row], census.PROBE_TABLE_COLUMNS)
    assert table.splitlines()[0].startswith("grain")
    assert len(table.splitlines()) == 3


def test_probe_row_of_a_block_without_probes_is_all_none() -> None:
    row = census.probe_row({"grain": "town", "block_code": 1})
    assert row["repost_pairs"] is None and row["n_portals"] is None


def test_render_corpus_probes_prints_both_small_tables() -> None:
    out = census.render_corpus_probes(
        {
            "ingest_rate": [{"source": "sreality", "n_1d": 10, "n_7d": 70}],
            "source_location": [{"granularity": "address_point", "n_listings": 5}],
        },
        source="remax",
    )
    assert "ingest rate" in out and "remax" in out
    assert "sreality" in out and "address_point" in out


class _Noop:
    def __enter__(self) -> "_Noop":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _fake_conn(execute, seen: list[str] | None = None):
    """A connection that answers each statement from `execute` and records what it was asked."""

    class Cur:
        def __init__(self) -> None:
            self.rows: list[dict[str, object]] = []

        def execute(self, sql: str, params: object = None) -> None:
            if "statement_timeout" in sql:
                self.rows = []
                return
            if seen is not None:
                seen.append(sql)
            self.rows = execute(sql)

        @property
        def description(self):
            return [(k,) for k in (self.rows[0] if self.rows else {})]

        def fetchall(self):
            return [tuple(r.values()) for r in self.rows]

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    class Conn:
        def cursor(self) -> Cur:
            return Cur()

        def transaction(self) -> _Noop:
            return _Noop()

        def close(self) -> None:
            return None

    return Conn


def _census_execute(sql: str) -> list[dict[str, object]]:
    if sql is census.CENSUS_COVERAGE_SQL:
        return [{"n_listings": 10}]
    if sql is census.CENSUS_DETAIL_SQL:
        return [{"n_block_listings": 3000, "n_listings_with_images": 2700}]
    if sql is census.CENSUS_PROBE_PORTAL_SIGNAL_SQL:
        return [{"source": "sreality", "n_listings": 100}]
    if sql is census.CENSUS_PROBE_REPOSTS_SQL:
        return [{
            "repost_pairs": 61, "repost_pairs_cross_type": 4,
            "repost_listings": 98, "repost_candidates": 400,
        }]
    if sql is census.CENSUS_PROBE_ADDRESS_DENSITY_SQL:
        return [{
            "n_addr_3plus_multi_floor": 12, "n_addr_dense_listings": 80,
            "n_addressed_listings": 900,
        }]
    if sql is census.CENSUS_PROBE_SHARED_HASH_SQL:
        return [{"n_shared_hashes": 3, "n_shared_hash_listings": 20, "largest_hash_group": 9}]
    return [{
        "block_code": 1, "block_name": "B", "n_total": 3000, "n_sources": 9,
        "with_desc200": 2500, "with_source_url": 3000, "no_signal_at_all": 40,
    }]


def _read(path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def test_probes_are_skipped_when_probes_is_zero(tmp_path) -> None:
    seen: list[str] = []
    census.run_census(
        _fake_conn(_census_execute, seen),
        {"top": "1", "detail_top": "1", "probes": "0"},
        tmp_path,
    )
    assert not ({p.sql for p in census.BLOCK_PROBES} & set(seen))
    report = _read(tmp_path / "census.json")
    assert "probes" not in report["grains"]["town"][0]
    assert report["probe_table"] == ""


def test_probes_run_by_default_and_land_on_the_block(tmp_path) -> None:
    seen: list[str] = []
    result = census.run_census(
        _fake_conn(_census_execute, seen), {"top": "1", "detail_top": "1"}, tmp_path
    )
    assert {p.sql for p in census.BLOCK_PROBES} <= set(seen)
    report = _read(tmp_path / "census.json")
    probes = report["grains"]["town"][0]["probes"]
    assert set(probes) == {p.name for p in census.BLOCK_PROBES}
    assert probes["reposts"]["repost_pairs"] == 61
    assert probes["portal_signal"] == [{"source": "sreality", "n_listings": 100}]
    assert "repair" in report["probe_table"]
    assert result["probed"] == 2


def test_a_failed_block_probe_is_recorded_not_raised(tmp_path) -> None:
    def execute(sql: str) -> list[dict[str, object]]:
        if sql is census.CENSUS_PROBE_REPOSTS_SQL:
            raise RuntimeError("canceling statement due to statement timeout")
        return _census_execute(sql)

    census.run_census(_fake_conn(execute), {"top": "1", "detail_top": "1"}, tmp_path)
    report = _read(tmp_path / "census.json")
    block = report["grains"]["town"][0]
    assert "statement timeout" in block["probes"]["reposts"]["error"]
    assert block["probes"]["shared_hash"]["n_shared_hashes"] == 3
    assert any("probe reposts" in err for err in report["errors"])


def _probes_execute(sql: str) -> list[dict[str, object]]:
    if sql is census.CENSUS_PROBE_INGEST_SQL:
        return [
            {"source": "sreality", "n_1d": 100, "n_7d": 700},
            {"source": "bazos", "n_1d": 50, "n_7d": 300},
        ]
    if sql is census.CENSUS_PROBE_SOURCE_LOCATION_SQL:
        return [{
            "granularity": "address_point", "n_listings": 11819, "n_with_geom": 11819,
            "n_active": 9000, "n_active_with_geom": 9000,
        }]
    raise AssertionError(f"unexpected statement: {sql[:60]}")


def test_a_failed_detail_still_runs_that_blocks_probes(tmp_path) -> None:
    """The probes do not read the detail result, and the detail statement is the one most
    likely to time out — losing both would lose the numbers the region is chosen on."""

    def execute(sql: str) -> list[dict[str, object]]:
        if sql is census.CENSUS_DETAIL_SQL:
            raise RuntimeError("canceling statement due to statement timeout")
        return _census_execute(sql)

    result = census.run_census(_fake_conn(execute), {"top": "1", "detail_top": "1"}, tmp_path)
    report = _read(tmp_path / "census.json")
    block = report["grains"]["town"][0]
    assert "statement timeout" in block["detail_error"]
    assert block["probes"]["reposts"]["repost_pairs"] == 61
    assert result["detailed"] == 0 and result["probed"] == 2
    assert "repair" in report["probe_table"]


def test_census_json_lands_after_every_block(tmp_path, monkeypatch) -> None:
    import copy

    def execute(sql: str) -> list[dict[str, object]]:
        if sql in census.BLOCK_SQL_BY_GRAIN.values():
            return [
                {"block_code": 1, "block_name": "A", "n_total": 3000, "n_sources": 9},
                {"block_code": 2, "block_name": "B", "n_total": 2000, "n_sources": 8},
            ]
        return _census_execute(sql)

    snapshots: list[dict] = []
    real = census.write_json

    def spy(path, payload) -> None:
        snapshots.append(copy.deepcopy(payload))
        real(path, payload)

    monkeypatch.setattr(census, "write_json", spy)
    census.run_census(_fake_conn(execute), {"top": "2", "detail_top": "2"}, tmp_path)
    mid = [
        snap
        for snap in snapshots
        if len(snap["grains"].get("town", [])) == 2
        and "probes" in snap["grains"]["town"][0]
        and "probes" not in snap["grains"]["town"][1]
    ]
    assert mid, "census.json never landed between two blocks"


def test_probes_mode_writes_probes_json(tmp_path) -> None:
    result = census.run_probes(_fake_conn(_probes_execute), {"source": "remax"}, tmp_path)
    payload = _read(tmp_path / "probes.json")
    assert result["ingest_1d"] == 150 and result["ingest_7d"] == 1000
    assert result["granularities"] == 1 and result["errors"] == []
    assert payload["parameters"]["source"] == "remax"
    assert payload["probes"]["ingest_totals"] == {"n_1d": 150, "n_7d": 1000}
    assert "address_point" in payload["table"] and "remax" in payload["table"]
    assert set(payload["timings"]) == {"source_location_s", "ingest_rate_s"}


def test_probes_mode_records_a_failed_probe_and_still_writes_the_report(tmp_path) -> None:
    def execute(sql: str) -> list[dict[str, object]]:
        if sql is census.CENSUS_PROBE_SOURCE_LOCATION_SQL:
            raise RuntimeError("canceling statement due to statement timeout")
        return _probes_execute(sql)

    result = census.run_probes(_fake_conn(execute), {}, tmp_path)
    payload = _read(tmp_path / "probes.json")
    assert result["ingest_7d"] == 1000
    assert any("source_location" in err for err in payload["errors"])
    assert "source_location" not in payload["probes"]


def test_probes_mode_is_registered_on_the_lane() -> None:
    from autodedup import lane

    assert lane.MODES["probes"] is census.run_probes
