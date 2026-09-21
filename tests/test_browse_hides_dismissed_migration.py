"""Source rails for migration 537 — Browse hides the caller's dismissed properties.

The three dismissal-aware sources only perform because the planner INLINES them:
PostgREST's filters, ORDER BY and LIMIT then reach `browse_list`'s index exactly as
before (measured: 48.7 ms worst-case card page at 20k dismissals). Inlining silently
stops if a function becomes SECURITY DEFINER, gains a SET clause, or stops being a
single-statement SQL body — and a DEFINER would also read the dismissals as the
owner, not the caller. Returning the RELATION's row type would pin the blue-green
DROP in `rebuild_browse_list` / `rebuild_properties_map_mv` and wedge every tick.

Offline; the live behaviour is verified on the replayed schema by the DB lane.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MIGRATION = (
    Path(__file__).resolve().parents[1] / "migrations" / "537_browse_hides_dismissed.sql"
)
_SQL = MIGRATION.read_text(encoding="utf-8")
_PREDICATE = "select 1 from public.property_dismissals_public d where d.property_id ="

_SOURCES = {
    "browse_list_visible": ("public.browse_projection", "public.browse_list"),
    "properties_map_visible": ("public.browse_projection", "public.properties_map_mv"),
    "listing_feed_visible": ("public.listing_feed_public", "public.listing_feed_public"),
}


def _definition(fn: str) -> str:
    at = _SQL.index(f"create or replace function public.{fn}()")
    return _SQL[at: _SQL.index("$$;", at)]


@pytest.mark.parametrize("fn", sorted(_SOURCES))
def test_each_source_is_an_inlinable_invoker_sql_function(fn: str) -> None:
    body = " ".join(_definition(fn).split())
    returns, relation = _SOURCES[fn]
    assert f"returns setof {returns} language sql stable as $$" in body
    assert f"from {relation} " in body
    assert _PREDICATE in body
    for defeat in ("security definer", " set ", "volatile", "strict", "begin"):
        assert defeat not in body.lower(), f"{fn}: `{defeat.strip()}` stops inlining"


@pytest.mark.parametrize("fn", sorted(_SOURCES))
def test_each_source_is_executable_by_signed_in_callers_only(fn: str) -> None:
    assert f"revoke execute on function public.{fn}() from public, anon;" in _SQL
    assert f"grant execute on function public.{fn}() to authenticated, service_role;" in _SQL


def test_no_source_returns_a_blue_green_relations_own_row_type() -> None:
    assert not re.search(r"returns setof (public\.)?(browse_list|properties_map_mv)\b", _SQL)


@pytest.mark.parametrize("fn", ["browse_stats_properties", "browse_map_cells"])
def test_each_aggregate_rpc_takes_the_flag_once_and_restores_its_acl(fn: str) -> None:
    at = re.search(rf"create or replace function public\.{fn}\s*\(", _SQL, re.I).start()
    stmt = _SQL[at: _SQL.index("$function$;", at)]
    assert re.search(r"hide_dismissed boolean default false", stmt, re.I)
    assert stmt.count("not hide_dismissed or not exists") == 1
    assert "select 1 from property_dismissals_public d where d.property_id = l.property_id" in stmt
    assert f"drop function if exists public.{fn}(" in _SQL
    assert re.search(rf"revoke execute on function public\.{fn}\([^)]*\) from public, anon;", _SQL)
    assert re.search(
        rf"grant execute on function public\.{fn}\([^)]*\) to authenticated, service_role;", _SQL,
    )


def test_the_whole_swap_is_one_transaction() -> None:
    statements = [s.strip() for s in re.sub(r"--[^\n]*", "", _SQL).split(";") if s.strip()]
    assert statements[0] == "begin"
    assert statements[-1] == "commit"
