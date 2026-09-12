"""ONE place predicate, in four places, provably the same one (W3 S3).

Browse's list, Browse's Stats, the map and the Watchdog each used to carry their
own copy of the chip predicate — six copies of five predicates. They are now one
rule, `<level>_id = any(codes)`, compiled by:

  * `api/location_filter.py`            — the API + the Watchdog matcher
  * `frontend/src/lib/districtCodes.ts` — the SPA (PostgREST string + the
                                          in-memory row predicate)
  * `browse_stats_properties`           — migration 504, the Stats cohort
  * `browse_map_cells`                  — migration 504, the map cohort

This file holds the API half of the shared table (`tests/fixtures/
district_chip_plan.json`, read by `districtCodes.test.ts` for the SPA half) plus
the source-text rails over the two SQL bodies and over the deletion ledger.

Offline; the shipped SQL's behaviour is the DB lane's subject.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from api.location_filter import (
    LEVEL_COLUMN,
    LEVEL_ORDER,
    NO_MATCH_CODE,
    DistrictChip,
    district_code_plan,
    district_where,
)

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "migrations"
FIXTURE = REPO / "tests" / "fixtures" / "district_chip_plan.json"
TS_MODULE = REPO / "frontend" / "src" / "lib" / "districtCodes.ts"

_TABLE = json.loads(FIXTURE.read_text(encoding="utf-8"))
_CASES = _TABLE["cases"]


def _chips(case: dict) -> list[DistrictChip]:
    return [DistrictChip(**c) for c in case["chips"]]


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_the_api_compiles_the_table(case: dict) -> None:
    """The plan and the SQL rendering, from the SAME chip sets the SPA is tested
    against. RED by: either side drifting from the table."""
    plan = district_code_plan(_chips(case))
    assert plan.include == {k: list(v) for k, v in case["plan"]["include"].items()}
    assert plan.exclude == {k: list(v) for k, v in case["plan"]["exclude"].items()}
    assert plan.unresolved == case["plan"]["unresolved"]

    where, params = district_where(_chips(case), alias="l")
    assert where == case["sql_where"]
    assert params == {k: list(v) for k, v in case["sql_params"].items()}


def test_the_sentinel_agrees_across_the_table() -> None:
    assert NO_MATCH_CODE == _TABLE["no_match_code"]
    assert NO_MATCH_CODE < 0, "RÚIAN codes are positive; the sentinel must not be one"


def test_the_ts_module_declares_the_same_levels_and_columns() -> None:
    """The level → column map is the predicate. If the SPA spells one column
    differently the two cohorts diverge with no error anywhere — PostgREST would
    happily filter on a column that exists."""
    ts = TS_MODULE.read_text(encoding="utf-8")
    block = ts[ts.index("export const DISTRICT_LEVEL_COLUMN"):]
    block = block[: block.index("} as const;")]
    pairs = dict(re.findall(r"(\w+):\s*'([a-z_]+)'", block))
    assert pairs == LEVEL_COLUMN

    order_block = ts[ts.index("export const DISTRICT_LEVEL_ORDER"):]
    order_block = order_block[: order_block.index("];")]
    assert tuple(re.findall(r"'([a-z_]+)'", order_block)) == LEVEL_ORDER

    assert f"export const NO_MATCH_CODE = {NO_MATCH_CODE};" in ts


# --- the two RPC bodies -----------------------------------------------------

_W3_S3 = "504_location_w3_one_code_predicate.sql"


def _latest_definition(func: str) -> Path:
    pat = re.compile(rf"create or replace function (?:public\.)?{func}\s*\(", re.IGNORECASE)
    hits = [p for p in MIGRATIONS.glob("*.sql") if pat.search(p.read_text(encoding="utf-8"))]
    assert hits, f"no migration defines {func}"
    return max(hits, key=lambda p: int(p.name.split("_", 1)[0]))


def _function_body(func: str) -> str:
    """Just this function's statement — 504 defines two, so a whole-file scan
    would silently mix their CASE blocks together."""
    sql = _latest_definition(func).read_text(encoding="utf-8")
    at = re.search(
        rf"create or replace function (?:public\.)?{func}\s*\(", sql, re.IGNORECASE
    ).start()
    return sql[at: sql.index("$function$;", at)]


def _chip_case_arms(func: str) -> list[dict[str, str]]:
    """Every `when lvl = '<level>' then l.<column> = admin_id` arm of the chip
    CASE, one dict per CASE block (there are two: include and exclude)."""
    blocks = re.findall(
        r"and case\b(.*?)\bend\b", _function_body(func), re.IGNORECASE | re.DOTALL,
    )
    chip_blocks = [b for b in blocks if "lvl =" in b]
    assert len(chip_blocks) == 2, (
        f"{func}: expected exactly two chip CASE blocks (include + exclude), "
        f"found {len(chip_blocks)}"
    )
    return [
        dict(re.findall(r"when lvl = '([a-z_]+)'\s*then l\.([a-z_]+)\s*=", b))
        for b in chip_blocks
    ]


@pytest.mark.parametrize("func", ["browse_stats_properties", "browse_map_cells"])
def test_the_rpc_bodies_compile_the_same_level_map(func: str) -> None:
    """RED by: an RPC arm pointed at a different column than the API/SPA use, or
    a level served in one RPC and not the other — the Stats tab and the map
    would then answer for different cohorts under the same chips."""
    assert _latest_definition(func).name == _W3_S3
    for arms in _chip_case_arms(func):
        # `locality` is compiled, not stored: a street pick filters at its obec.
        assert arms == {**LEVEL_COLUMN, "locality": LEVEL_COLUMN["obec"]}


@pytest.mark.parametrize("func", ["browse_stats_properties", "browse_map_cells"])
def test_the_rpc_bodies_read_no_text_column_for_a_chip(func: str) -> None:
    """The five predicates are gone: no ILIKE, no place_search_text, no context
    narrow. RED by: reintroducing any of them in either body."""
    body = "\n".join(
        line for line in _function_body(func).splitlines()
        if not line.strip().startswith("--")
    )
    for block in re.findall(r"and case\b(.*?)\bend\b", body, re.IGNORECASE | re.DOTALL):
        if "lvl =" not in block:
            continue
        low = block.lower()
        assert "ilike" not in low
        assert "place_search_text" not in low
        assert "ctx" not in low
        assert "needle" not in low


@pytest.mark.parametrize("func", ["browse_stats_properties", "browse_map_cells"])
def test_a_chip_without_a_code_matches_nothing_in_sql_too(func: str) -> None:
    """The null guard and the `else` are the SQL half of the sentinel rule: an
    include chip we could not resolve contributes nothing rather than widening
    the cohort to the whole country."""
    for block in re.findall(
        r"and case\b(.*?)\bend\b", _function_body(func), re.IGNORECASE | re.DOTALL
    ):
        if "lvl =" not in block:
            continue
        assert re.search(r"when admin_id is null\s+then false", block, re.IGNORECASE)
        assert re.search(r"else false", block, re.IGNORECASE)


def test_the_rpc_signatures_did_not_change() -> None:
    """`create or replace function` cannot change a parameter list, and the SPA
    builds ONE argument object for both RPCs (tests/test_browse_map_read_
    contract.py). The chip parameters therefore stay exactly as they were — the
    now-inert `districts_context_filter` included."""
    for func, previous in (
        ("browse_stats_properties", "436_city_quality_obec_key.sql"),
        ("browse_map_cells", "439_browse_map_cells.sql"),
    ):
        new = _params_of(_latest_definition(func).read_text(encoding="utf-8"), func)
        old = _params_of((MIGRATIONS / previous).read_text(encoding="utf-8"), func)
        assert new == old, f"{func}: parameter list changed"
        for chip_param in (
            "districts_filter", "districts_levels", "districts_ids",
            "districts_excluded_filter", "districts_context_filter",
        ):
            assert chip_param in new


def _params_of(sql: str, func: str) -> list[str]:
    at = re.search(
        rf"create or replace function (?:public\.)?{func}\s*\(", sql, re.IGNORECASE
    ).end()
    depth, i = 1, at
    while depth:
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
        i += 1
    args = re.sub(r"--[^\n]*", "", sql[at: i - 1])
    return re.findall(r"(?:^|,)\s*([a-z][a-z0-9_]*)\s+[a-z]", args)


def test_the_fourth_level_exists_on_every_relation_the_predicate_runs_on() -> None:
    """`cast_obce_id` is only a filter if the relation publishes it: browse_list
    and properties_map_mv inherit it from browse_projection (migration 503),
    properties_public (the Watchdog) and pipeline_board_public (the board's
    in-memory copy) get it in 504."""
    s503 = (MIGRATIONS / "503_location_w3_serving_views.sql").read_text(encoding="utf-8")
    assert "ll.cast_obce_kod as cast_obce_id" in s503  # browse_projection
    s504 = (MIGRATIONS / _W3_S3).read_text(encoding="utf-8")
    assert "ll.cast_obce_kod as cast_obce_id" in s504  # properties_public
    assert "p.cast_obce_id" in s504  # pipeline_board_public


# --- the deletion ledger ----------------------------------------------------

_DELETED = ("locality_district_id", "locality_region_id")


def test_the_two_sreality_only_filters_are_gone_from_the_registry() -> None:
    """They were SREALITY's own portal ids — one portal out of nine — and place
    is one thing now: a RÚIAN code at a level."""
    from toolkit.filter_registry import REGISTRY

    for gone in _DELETED:
        assert gone not in REGISTRY
    assert not [d for d in REGISTRY.values() if d.pg_column in _DELETED]


def test_the_two_filters_are_gone_from_the_generated_spa_registry() -> None:
    gen = (REPO / "frontend" / "src" / "lib" / "filterRegistry.generated.ts").read_text(
        encoding="utf-8"
    )
    for gone in _DELETED:
        assert f'"id": "{gone}"' not in gen


def test_the_two_filters_are_gone_from_the_api_schemas_and_predicates() -> None:
    import dataclasses

    from api import schemas as s
    from api.notifications import WatchdogFilterSpec
    from toolkit.comparables import ComparableFilters

    comparable_fields = {f.name for f in dataclasses.fields(ComparableFilters)}
    for gone in _DELETED:
        assert gone not in comparable_fields
        assert gone not in WatchdogFilterSpec.model_fields
        for model in (s.FindComparablesIn, s.CreateEstimationIn):
            assert gone not in model.model_fields
    for rel in ("toolkit/comparables.py", "api/notifications.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        for gone in _DELETED:
            assert f"l.{gone} = %" not in text, f"{rel} still filters on {gone}"


def test_the_two_filters_are_gone_from_the_spa_filter_types() -> None:
    """The listing-row COLUMNS survive (they are `listings` columns W4 deletes,
    and an immutable estimation trace still renders them); what is gone is every
    place they were a FILTER."""
    types_ts = (REPO / "frontend" / "src" / "lib" / "types.ts").read_text(encoding="utf-8")
    for block in ("export interface WatchdogFilterSpec", "export interface EstimationFilters"):
        at = types_ts.index(block)
        body = types_ts[at: types_ts.index("\n}", at)]
        for gone in _DELETED:
            assert gone not in body
    default = types_ts[types_ts.index("export const DEFAULT_WATCHDOG_FILTER_SPEC"):]
    default = default[: default.index("\n};")]
    for gone in _DELETED:
        assert gone not in default
