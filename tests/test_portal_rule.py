"""THE portal rule (MS19, migration 590), executed and pinned to the shared fixture.

`tests/fixtures/portal_rule.json` is read by both languages. Here: every case through
`public.portal_status_matches`, through both aggregate RPCs (Stats over `browse_list`, the
map cells over a parked `properties_map_mv`) and, for the `any` rows, through the Watchdog's
compiled clause (`toolkit.filter_compiler.PROPERTIES_GRAIN`); every broker case seeds its
ads and runs the broker lookup (`toolkit.brokers.broker_property_ids`). The SPA half is
`applyPortalRule` in queries.test.ts. Also live: the rebuild's one partial index per portal
and the three relations' one shape. Offline: the fixture covers every arm.

Live rails run in CI's migrations lane (`TEST_DATABASE_URL`, `DB_RAILS_REQUIRED=1`), each
inside a transaction that always rolls back.
"""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from typing import Any

import pytest

from toolkit.filter_registry import PORTAL_OPTIONS

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_REQUIRED = os.environ.get("DB_RAILS_REQUIRED") == "1"
live = pytest.mark.skipif(
    not _DB_URL and not _REQUIRED,
    reason="TEST_DATABASE_URL not set — this rail runs in CI's migrations lane",
)

_TABLE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "portal_rule.json").read_text(encoding="utf-8"))
CASES = _TABLE["cases"]
BROKER_CASES = _TABLE["broker_cases"]
_STATUS_ARGS = {"any": (False, False), "active": (True, False), "inactive": (False, True)}
_PORTALS = [o.value for o in PORTAL_OPTIONS]
_IDS = itertools.count(970_000_001)


def _ids(cases: list[dict[str, Any]]) -> list[str]:
    return [c["name"] for c in cases]


# --- offline: the table itself ---------------------------------------------------------


def test_the_table_covers_every_arm_both_ways() -> None:
    seen = {(min(len(c["portals"]), 2), c["status"], c["expected"]) for c in CASES}
    for width, status, expected in itertools.product((0, 1, 2), _STATUS_ARGS, (True, False)):
        if (width, status) == (0, "any") and not expected:
            continue  # no portal and no status is no constraint
        assert (width, status, expected) in seen, (width, status, expected)


def test_every_case_names_known_portals_and_consistent_lists() -> None:
    for c in [*CASES, *BROKER_CASES]:
        assert set(c["portals"]) <= set(_PORTALS), c["name"]
    for c in CASES:
        assert set(c["active_sources"]) <= set(c["all_sources"]), c["name"]
        assert c["is_active"] == bool(c["active_sources"]) or not c["all_sources"], c["name"]


# --- live ------------------------------------------------------------------------------


@pytest.fixture()
def conn():
    if not _DB_URL:
        pytest.fail(
            "DB_RAILS_REQUIRED=1 but TEST_DATABASE_URL is not set — the migrations lane "
            "is misconfigured and this rail would otherwise have skipped green."
        )
    import psycopg

    with psycopg.connect(_DB_URL, autocommit=False) as c:
        yield c
        c.rollback()


@live
@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_the_function_answers_the_table(conn, case: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "select public.portal_status_matches(%s, %s::text[], %s::text[], %s::text[], %s)",
            (case["is_active"], case["all_sources"], case["active_sources"],
             case["portals"], case["status"]))
        assert cur.fetchone()[0] is case["expected"]


@live
@pytest.mark.parametrize("case", [c for c in CASES if c["status"] == "any"],
                         ids=_ids([c for c in CASES if c["status"] == "any"]))
def test_the_watchdogs_clause_is_the_any_arm(conn, case: dict[str, Any]) -> None:
    from toolkit.filter_compiler import PROPERTIES_GRAIN, compile_filter_where

    where, params = compile_filter_where({"portals": case["portals"]}, PROPERTIES_GRAIN)
    with conn.cursor() as cur:
        cur.execute(
            "select count(*) from (values (%(all)s::text[], %(active)s::text[])) "
            f"as l(all_sources, active_sources) where {' and '.join(where) or 'true'}",
            {**params, "all": case["all_sources"], "active": case["active_sources"]})
        assert cur.fetchone()[0] == int(case["expected"])


def _rpc_args(case: dict[str, Any]) -> dict[str, Any]:
    active_only, inactive_only = _STATUS_ARGS[case["status"]]
    return {"portal": case["portals"] or None, "active_only": active_only,
            "inactive_only": inactive_only}


@live
@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_stats_counts_the_table(conn, case: dict[str, Any]) -> None:
    """browse_stats_properties reads browse_list (590's rebuild made it a real table)."""
    with conn.cursor() as cur:
        cur.execute(
            "insert into browse_list (property_id, is_active, all_sources, active_sources) "
            "values (%s, %s, %s, %s)",
            (next(_IDS), case["is_active"], case["all_sources"], case["active_sources"]))
        cur.execute(
            "select public.browse_stats_properties(portal_filter => %(portal)s::text[], "
            "active_only_filter => %(active_only)s, inactive_only_filter => %(inactive_only)s)",
            _rpc_args(case))
        assert cur.fetchone()[0]["total"] == int(case["expected"])


@live
@pytest.mark.parametrize("case", CASES, ids=_ids(CASES))
def test_the_map_cells_count_the_table(conn, case: dict[str, Any]) -> None:
    """The matview is parked for a table of its shape (tests/test_browse_map_cells_live.py)."""
    with conn.cursor() as cur:
        cur.execute("alter materialized view public.properties_map_mv rename to pmm_parked_for_rule;"
                    "create table public.properties_map_mv (like public.pmm_parked_for_rule);")
        cur.execute(
            "insert into properties_map_mv (property_id, listing_id, lat, lng, is_active, "
            "all_sources, active_sources) values (%s, %s, 50.08, 14.42, %s, %s, %s)",
            (next(_IDS), next(_IDS), case["is_active"], case["all_sources"],
             case["active_sources"]))
        cur.execute(
            "select public.browse_map_cells(portal_filter => %(portal)s::text[], "
            "active_only_filter => %(active_only)s, inactive_only_filter => %(inactive_only)s, "
            "point_budget => 2000)",
            _rpc_args(case))
        assert cur.fetchone()[0]["total"] == int(case["expected"])


def _seed_broker_case(cur: Any, case: dict[str, Any]) -> tuple[dict[str, int], int]:
    brokers: dict[str, int] = {}
    for label in sorted({ad["broker"] for ad in case["ads"]}):
        cur.execute("insert into brokers (display_name) values (%s) returning id",
                    (f"rule-{label}",))
        broker_id = cur.fetchone()[0]
        cur.execute(
            "insert into broker_identities (source, source_broker_id_native, broker_id) "
            "values ('sreality', %s, %s) returning id", (f"rule-{next(_IDS)}", broker_id))
        brokers[label] = cur.fetchone()[0]
    cur.execute("insert into properties default values returning id")
    property_id = cur.fetchone()[0]
    for ad in case["ads"]:
        native = next(_IDS)
        cur.execute(
            "insert into listings (source, source_id_native, sreality_id, raw_json, property_id, "
            "broker_identity_id, is_active) values (%s, %s, %s, '{}', %s, %s, %s)",
            (ad["source"], str(native), native if ad["source"] == "sreality" else None,
             property_id, brokers[ad["broker"]], ad["is_active"]))
    cur.execute("select id, broker_id from broker_identities where id = any(%s)",
                (list(brokers.values()),))
    by_identity = dict(cur.fetchall())
    return {label: by_identity[i] for label, i in brokers.items()}, property_id


@live
@pytest.mark.parametrize("case", BROKER_CASES, ids=_ids(BROKER_CASES))
def test_the_broker_lookup_judges_the_brokers_own_ads(conn, case: dict[str, Any]) -> None:
    from toolkit.brokers import broker_property_ids

    with conn.cursor() as cur:
        brokers, property_id = _seed_broker_case(cur, case)
    out = broker_property_ids(conn, brokers[case["broker"]], status=case["status"],
                              portals=case["portals"])
    assert (property_id in out["data"]) is case["expected"]


@live
def test_the_rebuild_builds_one_partial_index_per_portal(conn) -> None:
    """The one-portal "Newest first" page and count read these (migration 590). A tenth
    portal is offered only with its line here (tests/test_recompute_property_stats.py)."""
    with conn.cursor() as cur:
        cur.execute("select indexname, indexdef from pg_indexes where schemaname = 'public' "
                    "and tablename = 'browse_list' and indexname like 'browse_list_newest_ad_at_%'")
        found = dict(cur.fetchall())
    assert found == {
        f"browse_list_newest_ad_at_{p}_idx": (
            f"CREATE INDEX browse_list_newest_ad_at_{p}_idx ON public.browse_list USING btree "
            f"(category_main, category_type, newest_ad_at_{p} DESC, property_id DESC) "
            f"WHERE (newest_ad_at_{p} IS NOT NULL)")
        for p in _PORTALS
    }


@live
def test_the_projection_and_both_read_models_have_one_shape(conn) -> None:
    """`sync_browse_list` inserts by POSITION, and both sources return the projection's
    row type: three relations, one column list, the twelve appended last."""
    shapes = {}
    with conn.cursor() as cur:
        for rel in ("browse_projection", "browse_list", "properties_map_mv"):
            cur.execute(
                "select attname, format_type(atttypid, atttypmod) from pg_attribute "
                "where attrelid = %s::regclass and attnum > 0 and not attisdropped "
                "order by attnum", (f"public.{rel}",))
            shapes[rel] = cur.fetchall()
    assert shapes["browse_list"] == shapes["browse_projection"] == shapes["properties_map_mv"]
    names = [n for n, _ in shapes["browse_projection"]]
    assert "asset_id" not in names
    assert names[-12:] == ["all_sources", "active_sources", "source_count",
                           *(f"newest_ad_at_{p}" for p in _PORTALS)]
