"""Migration 584's rails: the order that keeps every read path one shape, and md5 guards that
match the bodies they guard. The append-only prefix rail is tests/test_location_w3_projection.py."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
SQL = (MIGRATIONS / "584_serve_street_code.sql").read_text(encoding="utf-8")
CODE = re.sub(r"--[^\n]*", "", SQL)


def _bodies(fn: str, text: str) -> list[str]:
    return re.findall(rf"create or replace function public\.{fn}\(\).*?as \$\$(.*?)\$\$", text, re.S)


def test_the_table_widens_before_the_view_and_the_map_source_is_bridged_until_the_rebuild():
    at = CODE.index
    assert at("pg_advisory_lock(hashtext('rebuild_browse_list'))") < at("begin;")
    assert at("alter table public.browse_list add column if not exists ulice_id bigint;") < at(
        "create or replace view browse_projection as")
    bridge = _bodies("properties_map_visible", CODE)[0]
    assert "null::bigint as ulice_id" in bridge
    assert at("null::bigint as ulice_id") < at("select public.rebuild_properties_map_mv();") < at(
        "select m.* from public.properties_map_mv m") < at("select public.rebuild_browse_list();")
    assert at("select public.rebuild_browse_list();") < at(
        "pg_advisory_unlock(hashtext('rebuild_browse_list'))")


def test_the_lock_wait_outlasts_a_cron_rebuild_and_the_ddl_then_fails_fast():
    at = CODE.index
    wait = CODE[at("set statement_timeout = '1900s';"): at("begin;")]
    assert wait.index("set lock_timeout = 0;") < wait.index(
        "pg_advisory_lock(hashtext('rebuild_browse_list'))") < wait.index(
        "pg_advisory_lock(hashtext('rebuild_properties_map_mv'))") < wait.index(
        "set lock_timeout = '5s';") < wait.index("set statement_timeout = '900s';")


def test_the_md5_guards_are_the_md5s_of_the_bodies_they_name():
    original = (MIGRATIONS / "561_one_property_view.sql").read_text(encoding="utf-8")
    bridge, final = _bodies("properties_map_visible", SQL)
    assert final == _bodies("properties_map_visible", original)[0]
    for body in (bridge, final, _bodies("browse_list_visible", original)[0]):
        assert hashlib.md5(body.encode()).hexdigest() in SQL
    rebuilds = (MIGRATIONS / "522_location_w13_rebuild_budgets.sql").read_text(encoding="utf-8")
    for fn in ("rebuild_browse_list", "rebuild_properties_map_mv"):
        body = re.findall(
            rf"create or replace function public\.{fn}\(\).*?as \$function\$(.*?)\$function\$",
            rebuilds, re.S)[0]
        assert hashlib.md5(body.encode()).hexdigest() in CODE


def test_the_post_conditions_date_the_rebuild_by_its_own_start_stamp_and_never_fail_on_a_retired_street():
    assert "select last_succeeded_at into started from public.derived_artifacts" in CODE
    assert "last_duration_ms" not in CODE
    post = " ".join(CODE[CODE.index("do $post$"):].split())
    assert "not exists ( select 1 from public.ruian_streets s where s.code = b.ulice_id))" in post
    assert "if n_unknown > 0 then raise exception" in post
    assert "if n_retired > n_unknown then raise notice" in post
    for fn in ("browse_list_visible", "properties_map_visible", "listing_feed_visible"):
        assert f"perform * from public.{fn}() limit 1;" in post
