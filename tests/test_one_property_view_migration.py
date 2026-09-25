"""Migration 561's rails: an inlinable order function, and the two never-written columns out of
the Browse read model with the physical columns left for W8. Executed: test_merge_safety_live."""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
SQL = (MIGRATIONS / "561_one_property_view.sql").read_text(encoding="utf-8")
CODE = " ".join(re.sub(r"--[^\n]*", "", SQL).split())


def _body(fn: str, text: str) -> str:
    at = text.index(f"create or replace function public.{fn}(")
    return " ".join(text[at: text.index("$$;", at)].split())


def test_the_order_function_is_an_inlinable_invoker_sql_function():
    body = _body("property_canonical_listings", SQL)
    assert "language sql stable as $$" in body
    assert not re.search(r"security definer| set |volatile|strict", body.lower())
    assert ("revoke execute on function public.property_canonical_listings(bigint) "
            "from public, anon, authenticated;") in CODE


def test_the_read_model_loses_the_two_columns_in_one_swap_and_properties_keeps_them():
    view = CODE[CODE.index("create view browse_projection as"):]
    assert not re.search(r"all_sources|active_sources", view[: view.index(";")])
    swap = CODE[CODE.index("begin;"): CODE.index("commit;")]
    assert "drop view if exists public.browse_projection;" in swap
    assert ("alter table public.browse_list drop column if exists all_sources, "
            "drop column if exists active_sources;") in swap
    assert not re.search(r"alter table (public\.)?properties\b", CODE), "the physical drop is W8's"


def test_both_dismissal_aware_sources_come_back_verbatim_with_their_acl():
    original = (MIGRATIONS / "537_browse_hides_dismissed.sql").read_text(encoding="utf-8")
    for fn in ("browse_list_visible", "properties_map_visible"):
        assert _body(fn, SQL) == _body(fn, original)
        assert f"revoke execute on function public.{fn}() from public, anon;" in CODE
        assert f"grant execute on function public.{fn}() to authenticated, service_role;" in CODE
    assert CODE.index("select public.rebuild_properties_map_mv();") < CODE.index(
        "create or replace function public.properties_map_visible()")
