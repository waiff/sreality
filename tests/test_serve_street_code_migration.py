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


def test_the_md5_guards_are_the_md5s_of_the_bodies_they_name():
    original = (MIGRATIONS / "561_one_property_view.sql").read_text(encoding="utf-8")
    bridge, final = _bodies("properties_map_visible", SQL)
    assert final == _bodies("properties_map_visible", original)[0]
    for body in (bridge, final, _bodies("browse_list_visible", original)[0]):
        assert hashlib.md5(body.encode()).hexdigest() in SQL
