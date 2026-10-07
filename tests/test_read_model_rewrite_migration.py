"""Migration 590's rails (MERGE SPRINT W5, MS19): the one read-model rewrite, offline.

What only the text can show: the preconditions refuse any body this file was not written
against and skip in CI's replay container; the locks come before any DDL; TX1 is guarded;
every restated body is the live one it was derived from plus exactly its named edits; no
re-created object loses its grants; nothing cascades or builds DDL through EXECUTE; the
post-conditions check what landed. The view columns are tests/test_location_w3_projection.py's
(the W5 block); the rule's behaviour is tests/test_portal_rule.py's (CI's migrations lane).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from tests.test_location_w3_projection import _columns
from tests.test_location_w3_projection import _sql as _view_sql
from tests.test_migration_rls_grants import _dynamic_ddl
from toolkit.filter_registry import PORTAL_OPTIONS

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
NAME = "590_read_model_portal_rule.sql"
SQL = (MIGRATIONS / NAME).read_text(encoding="utf-8")
CODE = re.sub(r"--[^\n]*", "", SQL)
PORTALS = [o.value for o in PORTAL_OPTIONS]
# browse_projection's appended tail: the two portal lists, the ad count, one date per portal.
TAIL = 3 + len(PORTALS)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _read(name: str) -> str:
    return (MIGRATIONS / name).read_text(encoding="utf-8")


def _block(tag: str) -> str:
    at = SQL.index(f"do ${tag}$")
    return SQL[at: SQL.index(f"${tag}$;", at)]


def _function_bodies(text: str) -> list[str]:
    return re.findall(r"\$function\$(.*?)\$function\$", text, re.S)


def _sql_body(text: str, fn: str, tag: str = "$$") -> str:
    at = re.search(rf"create (?:or replace )?function public\.{fn}\(\)", text).start()
    start = text.index(f"as {tag}", at) + len(f"as {tag}")
    return text[start: text.index(tag, start)]


_STATUS_LINES = ("          (not active_only_filter   or l.is_active = true)\n"
                 "      and (not inactive_only_filter or l.is_active = false)\n")
_RULE_CALL = ("          public.portal_status_matches(l.is_active, l.all_sources, l.active_sources, "
              "portal_filter,\n            case when active_only_filter then 'active' "
              "when inactive_only_filter then 'inactive' else 'any' end)\n")
_PORTAL_LINE = ("      and (portal_filter is null or array_length(portal_filter, 1) is null "
                "or l.source = any(portal_filter))\n")


def _rpc_header(text: str, fn: str) -> str:
    at = re.search(rf"create or replace function public\.{fn}\(", text, re.I).start()
    return text[at: text.index("$function$", at)]


# --- 0. preconditions --------------------------------------------------------------------


def test_the_replay_container_skips_the_preconditions_before_any_check() -> None:
    pre = _block("pre")
    assert pre.index("select count(*) = 100000 into populated") < pre.index("return;") < pre.index(
        "to_regprocedure")


def test_the_preconditions_name_the_live_bodies_and_this_files_own() -> None:
    """Each md5 is computed here from the migration that wrote the body, so a guard that
    no longer matches what it guards fails offline instead of refusing at apply time."""
    pre = _block("pre")
    rebuilds = _function_bodies(_read("522_location_w13_rebuild_budgets.sql"))
    one_property_view = _read("561_one_property_view.sql")
    rpcs_549 = _function_bodies(_read("549_browse_aggregates_know_ownership_jine.sql"))
    new_rebuild, new_stats, new_map = _function_bodies(SQL)
    bridge = re.findall(r"\$fn\$(.*?)\$fn\$", SQL, re.S)[1]
    live = {
        "rebuild_browse_list": rebuilds[0], "rebuild_properties_map_mv": rebuilds[1],
        "browse_list_visible": _sql_body(one_property_view, "browse_list_visible"),
        "properties_map_visible": _sql_body(one_property_view, "properties_map_visible"),
        "browse_stats_properties": rpcs_549[0], "browse_map_cells": rpcs_549[1],
    }
    for fn, body in live.items():
        assert _md5(body) in pre, f"{fn}: the guard does not name 549/561/522's body"
    for body in (new_rebuild, bridge, new_stats, new_map):
        assert _md5(body) in pre, "a re-run must accept this file's own bodies"
    assert "is distinct from 'e18ea5161c8af9ceab5b07a62b6dc37d'" in pre
    assert "is distinct from '47291211d34d2d14ca403dd3bbcdf176'" in pre


def test_the_preconditions_wait_for_a_cycle_begun_after_w2a_and_no_running_sweep() -> None:
    pre = " ".join(_block("pre").split())
    assert ("select (value->>'cycle_started_at')::timestamptz into v_cycle from "
            "public.app_settings where key = 'property_sweep_last_complete';") in pre
    assert "if v_cycle is null or v_cycle <= timestamptz '2026-10-06 19:57:46+00' then" in pre
    assert "where holder like 'full:%' and expires_at > now()" in pre
    assert "if n_checked = 0 or n_wrong > 0 then raise exception" in pre


# --- 1-2. locks, then one guarded shape transaction --------------------------------------


def test_the_locks_queue_then_fail_fast_before_any_ddl() -> None:
    """Queued behind an in-flight tick (a list rebuild may hold its key 1800 s), then every
    pg_cron tick self-skips and each statement fails fast."""
    at = CODE.index
    locks = CODE[at("$pre$;"): at("create or replace function public.portal_status_matches(")]
    on = locks.index
    assert (on("set statement_timeout = '1900s';") < on("set lock_timeout = 0;")
            < on("select pg_advisory_lock(hashtext('rebuild_browse_list'));")
            < on("select pg_advisory_lock(hashtext('rebuild_properties_map_mv'));")
            < on("set lock_timeout = '5s';") < on("set statement_timeout = '900s';"))
    assert at("create or replace function public.portal_status_matches(") < at("do $tx1$")
    assert (at("$post$;") < at("select pg_advisory_unlock(hashtext('rebuild_browse_list'));")
            < at("select pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));")
            < at("select pg_notify('pgrst', 'reload schema');"))


def test_tx1_is_one_guarded_block_holding_every_shape() -> None:
    tx1 = re.sub(r"--[^\n]*", "", _block("tx1"))
    guard = tx1.index("raise notice '590: TX1 already applied")
    assert tx1.index("attname = 'newest_ad_at_realitymix' and not attisdropped") < guard < tx1.index(
        "return;") < tx1.index("alter view public.browse_projection rename to browse_projection_legacy;")
    for stmt in ("create view browse_projection as", "alter table public.browse_list",
                 "drop function public.browse_list_visible();",
                 "create function public.browse_list_visible()",
                 "drop function public.properties_map_visible();",
                 "create function public.properties_map_visible()",
                 "drop view public.pipeline_board_public;", "drop view public.properties_public;",
                 "create view properties_public as", "create view pipeline_board_public"):
        assert stmt in tx1, stmt
    assert tx1.index("drop view public.pipeline_board_public;") < tx1.index(
        "drop view public.properties_public;") < tx1.index("create view properties_public as") < (
        tx1.index("create view pipeline_board_public"))


def test_the_cache_table_takes_the_projections_shape_first_in_the_same_transaction() -> None:
    """`sync_browse_list` inserts by POSITION and locks the table before the view: browse_list
    loses asset_id and gains the twelve exactly as the projection appends them, BEFORE the
    projection is touched, or a patch in flight deadlocks TX1."""
    tx1 = _block("tx1")
    found = re.search(r"alter table public\.browse_list\s+(.*?);", tx1, re.S)
    assert found.start() < tx1.index("alter view public.browse_projection rename")
    alter = found.group(1)
    clauses = [" ".join(c.split()) for c in alter.split(",")]
    assert clauses[0] == "drop column if exists asset_id"
    added = [re.fullmatch(r"add column if not exists (\w+) (\S+)", c).groups() for c in clauses[1:]]
    appended = _columns(_view_sql(NAME), "browse_projection")[-TAIL:]
    assert [name for name, _ in added] == appended
    assert [kind for _, kind in added] == ["text[]"] * 2 + ["integer"] + ["timestamptz"] * len(PORTALS)


def test_the_bridge_names_the_new_shape_and_yields_to_561s_body() -> None:
    """Valid over the old matview AND a new one (a crash between TX1 and TX2); written as
    `create function` so the `create or replace` finders resolve TX2's restored body."""
    bridge = re.findall(r"\$fn\$(.*?)\$fn\$", SQL, re.S)[1]
    survivors = [c for c in _columns(_view_sql(NAME), "browse_projection")[:-TAIL]]
    named = re.findall(r"\bm\.([a-z0-9_]+)", bridge.split("from public.properties_map_mv")[0])
    assert named == survivors
    nulls = re.findall(r"null::(\w+(?:\[\])?)", bridge)
    assert nulls == ["text[]"] * 2 + ["integer"] + ["timestamptz"] * len(PORTALS)
    final = _sql_body(SQL[SQL.index("begin;"):], "properties_map_visible")
    assert final == _sql_body(_read("561_one_property_view.sql"), "properties_map_visible")


def test_the_list_source_is_561s_body_verbatim() -> None:
    assert _sql_body(SQL, "browse_list_visible", "$fn$") == _sql_body(
        _read("561_one_property_view.sql"), "browse_list_visible")


# --- the rebuild -------------------------------------------------------------------------


def test_the_rebuild_is_522s_plus_one_static_index_and_rename_per_portal() -> None:
    s522 = _read("522_location_w13_rebuild_budgets.sql")
    old = _function_bodies(s522)[0]
    new = _function_bodies(SQL)[0]
    header = "create or replace function public.rebuild_browse_list()"
    assert SQL[SQL.index(header): SQL.index("$function$", SQL.index(header))] == (
        s522[s522.index(header): s522.index("$function$", s522.index(header))])
    creates = [
        f"  execute 'create index browse_list_next_newest_ad_at_{p}_idx on browse_list_next "
        f"(category_main, category_type, newest_ad_at_{p} desc, property_id desc) "
        f"where newest_ad_at_{p} is not null';\n" for p in PORTALS]
    renames = [
        f"  execute 'alter index browse_list_next_newest_ad_at_{p}_idx rename to "
        f"browse_list_newest_ad_at_{p}_idx';\n" for p in PORTALS]
    for line in (*creates, *renames):
        assert new.count(line) == 1, line
    stripped = new
    for line in (*creates, *renames):
        stripped = stripped.replace(line, "")
    assert stripped == old
    assert new.index(creates[0]) < new.index("analyze browse_list_next") < new.index(renames[0])
    assert "format(" not in new and "foreach" not in new and " loop" not in new


# --- 4. TX2 and the two RPCs -------------------------------------------------------------


def test_tx2_switches_the_rpcs_after_both_read_models_carry_the_rule() -> None:
    """The rebuild is replaced after TX1, never inside or before it (the comment at 2b)."""
    at = CODE.index
    assert (at("$tx1$;") < at("create or replace function public.rebuild_browse_list()")
            < at("set lock_timeout = 0;\nset statement_timeout = '3600s';")
            < at("select public.rebuild_browse_list();") < at("begin;")
            < at("select public.rebuild_properties_map_mv();")
            < at("create or replace function public.properties_map_visible()")
            < at("drop view if exists public.browse_projection_legacy;")
            < at("CREATE OR REPLACE FUNCTION public.browse_stats_properties(")
            < at("create or replace function public.browse_map_cells(") < at("commit;")
            < at("do $post$"))


@pytest.mark.parametrize("fn", ["browse_stats_properties", "browse_map_cells"])
def test_each_rpc_is_549s_body_after_the_two_edits(fn: str) -> None:
    s549 = _read("549_browse_aggregates_know_ownership_jine.sql")
    old = dict(zip(("browse_stats_properties", "browse_map_cells"), _function_bodies(s549)))[fn]
    new = dict(zip(("browse_stats_properties", "browse_map_cells"), _function_bodies(SQL)[1:]))[fn]
    assert old.count(_STATUS_LINES) == 1 and old.count(_PORTAL_LINE) == 1
    assert new == old.replace(_STATUS_LINES, _RULE_CALL).replace(_PORTAL_LINE, "")
    assert _rpc_header(SQL, fn) == _rpc_header(s549, fn), "same identity args: the ACL survives"
    assert _md5(new) in _block("post")


def test_the_portal_rule_is_one_inlinable_immutable_function() -> None:
    at = CODE.index("create or replace function public.portal_status_matches(")
    stmt = " ".join(CODE[at: CODE.index("$$;", at)].split())
    assert ("portal_status_matches( p_is_active boolean, p_all_sources text[], "
            "p_active_sources text[], p_portals text[], p_status text) returns boolean "
            "language sql immutable parallel safe as $$") in stmt
    for defeat in ("security definer", " set ", "strict", "volatile"):
        assert defeat not in stmt.lower()
    for arm in ("when coalesce(cardinality(p_portals), 0) = 0 then case p_status "
                "when 'active' then p_is_active when 'inactive' then not p_is_active else true end",
                "when p_status = 'active' then p_active_sources && p_portals",
                "when p_status = 'inactive' then p_all_sources && p_portals "
                "and not (p_active_sources && p_portals)",
                "else p_all_sources && p_portals"):
        assert arm in stmt, arm


def test_the_receipt_probes_only_what_outlives_the_apply() -> None:
    """apply_migration.yml's receipt and verify_pipeline's drift check probe what the file
    declares; a rebuild defined inside the DO block declared its scratch table too, which is
    renamed away at the end of every rebuild, so a clean apply read as a missing object."""
    from scripts.migration_objects import parse_objects

    assert [str(o) for o in parse_objects(SQL)] == [
        "relation:browse_projection", "relation:properties_public", "relation:pipeline_board_public",
        "function:public.portal_status_matches", "function:public.browse_list_visible",
        "function:public.properties_map_visible", "function:public.rebuild_browse_list",
        "function:public.browse_stats_properties", "function:public.browse_map_cells"]


# --- grants, cascade, dynamic DDL, post-conditions -----------------------------------------


def test_every_recreated_object_restates_its_acl() -> None:
    code = " ".join(CODE.split())
    for view in ("browse_projection", "properties_public"):
        assert f"revoke all on public.{view} from anon, authenticated;" in code
        assert f"grant select on public.{view} to authenticated;" in code
    assert "revoke all on public.pipeline_board_public from public, anon, authenticated;" in code
    assert "grant select on public.pipeline_board_public to authenticated;" in code
    for fn in ("browse_list_visible", "properties_map_visible"):
        assert code.count(f"revoke execute on function public.{fn}() from public, anon;") == (
            1 if fn == "browse_list_visible" else 2)
        assert f"grant execute on function public.{fn}() to authenticated, service_role;" in code
    assert ("revoke execute on function public.rebuild_browse_list() "
            "from public, anon, authenticated;") in code
    assert "grant execute on function public.rebuild_browse_list() to service_role;" in code
    signature = "public.portal_status_matches(boolean, text[], text[], text[], text)"
    assert f"revoke execute on function {signature} from public, anon;" in code
    assert f"grant execute on function {signature} to authenticated, service_role;" in code
    assert "with (security_invoker = true)" in code


def test_nothing_cascades_and_nothing_but_shapes_is_dropped() -> None:
    """Views hold no data and the two functions return the projection's row type, so
    each is re-created in the same transaction; the one column is a cache's (browse_list
    is rebuilt from the projection every 15 minutes). Every data object waits for W6."""
    assert "cascade" not in CODE.lower()
    assert not re.search(r"--\s*ci-allow-dynamic:", SQL)
    assert not _dynamic_ddl(SQL)
    outside = re.sub(r"\$function\$.*?\$function\$", "", CODE, flags=re.S)
    drops = sorted(" ".join(m.split()) for m in re.findall(
        r"\bdrop\s+(?:view|function|table|materialized\s+view|index|column|sequence)\b[^;,]*",
        outside, re.I))
    assert drops == [
        "drop column if exists asset_id", "drop function public.browse_list_visible()",
        "drop function public.properties_map_visible()",
        "drop view if exists public.browse_projection_legacy",
        "drop view public.pipeline_board_public", "drop view public.properties_public",
    ]


def test_the_post_conditions_check_what_landed() -> None:
    post = " ".join(_block("post").split())
    for frag in (
        "foreach rel in array array['browse_projection', 'browse_list', 'properties_map_mv'] loop",
        "if last_col is distinct from 'newest_ad_at_realitymix' then",
        "attname = 'asset_id' and not attisdropped",
        "attname in ('asset_id', 'distinct_site_count', 'published_at')",
        "if to_regclass('public.browse_projection_legacy') is not null then",
        "indexname like 'browse_list_newest_ad_at_%'", f"if n_idx <> {len(PORTALS)} then",
        "perform * from public.browse_list_visible() limit 1;",
        "perform * from public.properties_map_visible() limit 1;",
        "perform * from public.pipeline_board_public limit 1;",
        "select last_succeeded_at into started from public.derived_artifacts where name = 'browse_list';",
        "or b.source_count is distinct from p.source_count",
        "p.stats_computed_at < started - interval '15 minutes'",
    ):
        assert frag in post, frag
    assert post.index("perform * from public.pipeline_board_public") < post.index(
        "data post-conditions skipped") < post.index("into n_checked, n_wrong")
