"""Migration 588: an inlinable order function with 561's ACL; nine dates in one catalog-only ALTER."""

from __future__ import annotations

import re
from pathlib import Path

from toolkit.filter_registry import PORTAL_OPTIONS

SQL = (Path(__file__).resolve().parents[1] / "migrations"
       / "588_canonical_order_and_portal_dates.sql").read_text(encoding="utf-8")
CODE = " ".join(re.sub(r"--[^\n]*", "", SQL).split())


def test_the_order_function_stays_an_inlinable_invoker_sql_function_with_its_acl():
    at = CODE.index("create or replace function public.property_canonical_listings(")
    body = CODE[at: CODE.index("$$;", at)]
    assert "returns table (listing_id bigint, canonical_rank integer) language sql stable as $$" in body
    assert not re.search(r"security definer| set |volatile|strict", body.lower())
    assert "left join public.listing_location ll on ll.listing_id = l.id" in body
    assert "last_seen_at desc nulls last" not in body, "561's moving key is back"
    assert ("revoke execute on function public.property_canonical_listings(bigint) "
            "from public, anon, authenticated;") in CODE
    assert "grant execute on function public.property_canonical_listings(bigint) to service_role;" in CODE


def test_the_nine_dates_arrive_in_one_catalog_only_alter_behind_a_lock_limit():
    alters = re.findall(r"alter table public\.properties (.*?);", CODE)
    assert len(alters) == 1
    columns = re.findall(r"add column if not exists newest_ad_at_(\w+) timestamptz,?", alters[0])
    assert columns == [o.value for o in PORTAL_OPTIONS]
    assert not re.search(r"\b(default|not null|rename|index)\b", alters[0])
    assert CODE.index("set lock_timeout = '6s';") < CODE.index("alter table public.properties")
    assert not re.search(r"\bdrop\b", CODE.lower()), "588 is additive"


def test_the_trust_comment_names_no_python_mirror():
    comment = CODE[CODE.index("comment on function public.source_trust_rank(text) is"):]
    assert "source_trust.py" not in comment[: comment.index(";")]
    assert not (Path(__file__).resolve().parents[1] / "toolkit" / "source_trust.py").exists()
