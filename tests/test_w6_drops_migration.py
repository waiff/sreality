"""Migration 615's rails (MERGE SPRINT W6, PROGRAM.md §6): the one destructive window, offline.

What only the text can show: the §6 objects are dropped and nothing else, with no CASCADE and no
EXECUTE; the three concurrent index drops are top-level statements, the feed's before the column
it reads and the keyset pair last; each section refuses before its first drop, including what a
column's removal would take unasked (an index, a constraint over a column that stays) and a
function that reads the note; section 0's production half, which the replay never reaches, names
what the worker and the sweep name; the map function is not this file's (migration 597 took
`listing_ids_filter` out, so section 5 is gone) and only section 8's smoke call names it; the board
view is 590's verbatim and the card view 377's less the note; the receipt probes the two restated
views only. The live md5 pins (section 0, section 8) are checked where a server formats them: CI's
replay applies this file (migrations.yml), and production refuses at apply time
(docs/design/merge-sprint/W6_RUNBOOK.md).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.migration_objects import _strip_noise, parse_objects
from tests.migration_defs import latest_definition
from tests.test_migration_rls_grants import _dynamic_ddl

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
SQL = (MIGRATIONS / "615_merge_sprint_w6_drops.sql").read_text(encoding="utf-8")
CODE = re.sub(r"--[^\n]*", "", SQL)
S590 = (MIGRATIONS / "590_read_model_portal_rule.sql").read_text(encoding="utf-8")
S377 = (MIGRATIONS / "377_pipeline_stage_code.sql").read_text(encoding="utf-8")
FEED_DIC = "drop index concurrently if exists public.listings_portal_feed_idx;"
KEYSET_DICS = ("drop index concurrently if exists public.properties_cat_last_seen_keyset_idx;",
               "drop index concurrently if exists public.properties_last_seen_keyset_idx;")


def _block(tag: str) -> str:
    at = CODE.index(f"do ${tag}$")
    return CODE[at: CODE.index(f"${tag}$;", at)]


def _map_params(text: str) -> list[str]:
    """browse_map_cells' parameter names in a migration that creates it, in order."""
    at = text.index("create or replace function public.browse_map_cells(")
    args = re.sub(r"--[^\n]*", "", text[text.index("(", at) + 1: text.index(")\nreturns", at)])
    return re.findall(r"(?:^|,)\s*([a-z][a-z0-9_]*)\s+[a-z]", args)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _guard(tag: str) -> str:
    """A section's text before its first drop, lock or alter: its checks."""
    block = _flat(_block(tag))
    first = min(m.start() for m in re.finditer(r"\b(?:drop|alter table|lock table)\b", block))
    return block[:first]


def test_the_section_6_objects_go_and_nothing_else_nothing_cascades() -> None:
    drops = sorted(_flat(m) for m in re.findall(
        r"\b(?:drop|truncate|delete\s+from)\b[^;,(]*", _strip_noise(SQL), re.I))
    assert drops == sorted([
        "drop column if exists asset_id", "drop column if exists discovery_seq",
        "drop column if exists discovery_seq", "drop column if exists distinct_site_count",
        "drop column if exists price_per_m2_source_listing_id", "drop column note",
        "drop function if exists public.listing_feed_visible",
        "drop function if exists public.log_property_status_event",
        "drop function if exists public.price_per_m2_source_id",
        "drop index concurrently if exists public.listings_portal_feed_idx",
        "drop index concurrently if exists public.properties_cat_last_seen_keyset_idx",
        "drop index concurrently if exists public.properties_last_seen_keyset_idx",
        "drop sequence if exists public.listing_discovery_seq",
        "drop table if exists public.asset_membership_events",
        "drop table if exists public.assets", "drop table if exists public.property_status_events",
        "drop trigger if exists properties_log_status_event on public.properties",
        "drop view if exists public.listing_feed_public",
        "drop view if exists public.property_status_events_public",
        "drop view public.pipeline_board_public", "drop view public.property_pipeline_public",
    ])
    assert "cascade" not in CODE.lower()
    assert not re.search(r"--\s*ci-allow-dynamic:", SQL) and not _dynamic_ddl(SQL)


def test_the_concurrent_drops_are_top_level_the_feeds_first_the_keysets_last() -> None:
    at = CODE.index
    assert at("$pre$;") < at(FEED_DIC) < at("do $status_assets$") < at("do $feed$")
    assert at("$note$;") < at(KEYSET_DICS[0]) < at(KEYSET_DICS[1]) < at("do $post$")
    inside = [m.group(0) for m in re.finditer(r"do \$(\w+)\$.*?\$\1\$;", CODE, re.S)]
    inside += re.findall(r"^begin;$.*?^commit;$", CODE, re.M | re.S)
    for statement in (FEED_DIC, *KEYSET_DICS):
        assert re.search(rf"^{re.escape(statement)}$", CODE, re.M)
        assert not [block for block in inside if statement in block], statement


@pytest.mark.parametrize("tag", ["status_assets", "feed", "queue_seq", "note"])
def test_each_section_returns_when_applied_and_refuses_before_its_first_drop(tag: str) -> None:
    block = _block(tag)
    first = min(m.start() for m in re.finditer(r"\b(?:drop|alter table|lock table)\b", block))
    assert block.index("already applied") < block.index("raise exception '615 refused") < first


_GUARDED = {"status_assets": ("properties", "any (v_cols)"), "feed": ("listings", "v_col"),
            "queue_seq": ("listing_detail_queue", "v_col"), "note": ("property_pipeline", "v_note")}


@pytest.mark.parametrize("tag", sorted(_GUARDED))
def test_each_column_drop_first_refuses_what_it_would_take_unasked(tag: str) -> None:
    """A column's removal takes an index or a constraint on it along without a word: each section
    refuses an index on its column (but asset_id's own) and a constraint that also covers a
    column that stays (a single-column one goes with its column) before its first drop."""
    table, column = _GUARDED[tag]
    guard = _guard(tag)
    assert (f"d.refclassid = 'pg_class'::regclass and d.refobjid = 'public.{table}'::regclass "
            f"and d.refobjsubid = {column}") in guard
    assert re.search(rf"from pg_constraint k where k\.conrelid = 'public\.{table}'::regclass "
                     r"and k\.conkey && (\S+) and not \(k\.conkey <@ \1\)", guard)


def test_the_note_waits_for_a_function_or_a_trigger_that_reads_it() -> None:
    """pg_depend does not see a function body: the note is read by name, from its table or as
    NEW / OLD in a trigger function on it, and either refuses section 6."""
    guard = _guard("note")
    assert ("or (p.prosrc ~* '\\mnote\\M' and (p.prosrc ~* '\\mproperty_pipeline\\M' or p.oid in "
            "(select t.tgfoid from pg_trigger t "
            "where t.tgrelid = 'public.property_pipeline'::regclass))))") in guard


def test_section_0s_production_half_names_what_its_owners_name() -> None:
    """The replay returns before these checks (< 100k properties), so only the text pins them:
    a mistyped brake key would read as braked (an absent row is 0, as the worker reads it), a
    lost check would pass. Each names the worker's brake, the lane's lease and the full sweep's
    holder as their owners write them, and raises its refusal."""
    from autodedup.incremental_sql import RT_LEASE_READ_SQL, RT_LEASE_TAKE_SQL
    from scraper.realtime_worker import AUTODEDUP_INTERVAL_SETTING
    from scripts.recompute_property_stats import _TRY_LEASE_SQL, _new_holder

    pre = _flat(_block("pre"))
    production = pre[pre.index("if not v_populated then"):]
    refuse = " then raise exception '615 refused"
    for check in (
        "if coalesce((select value #>> '{}' from public.app_settings where key = "
        f"'{AUTODEDUP_INTERVAL_SETTING}'), '0') <> '0'",
        "if exists (select 1 from autodedup.rt_lease where expires_at > now())",
        "if exists (select 1 from public.property_maintenance_lease where holder like 'full:%' "
        "and expires_at > now())",
        "if to_regclass('public.listings_source_id_idx') is null",
        "if exists (select 1 from pg_stat_user_indexes where indexrelname in "
        "('properties_cat_last_seen_keyset_idx', 'properties_last_seen_keyset_idx') "
        "and idx_scan > 0)",
    ):
        assert check + refuse in production, check
    assert "insert into autodedup.rt_lease" in RT_LEASE_TAKE_SQL
    assert "l.expires_at > now() as live" in RT_LEASE_READ_SQL
    assert "UPDATE property_maintenance_lease" in _TRY_LEASE_SQL
    assert "expires_at < now()" in _TRY_LEASE_SQL
    assert _new_holder("full").startswith("full:")


def test_the_two_restated_views_are_pinned_before_the_production_gate() -> None:
    """Section 6 restates the board and the card from text written on 2026-10-08. Section 0
    refuses any other live body before its production gate, so CI's replay checks the pins too,
    and section 8 checks what section 6 wrote.

    RED by: moving the pin below `if not v_populated` (the replay would stop checking it), or
    dropping either view's pin from section 0 or section 8.
    """
    pre = _flat(_block("pre"))
    pin = pre.index("if md5(pg_get_viewdef('public.pipeline_board_public'::regclass)) <> "
                    "'06c950df3e18e0f58c4be6b06973600e' or md5(pg_get_viewdef("
                    "'public.property_pipeline_public'::regclass)) not in "
                    "('54671707e2d2c12d1a47971325e9a784', '88093f6916bb54a0a9e4333fbf0cd544') "
                    "then raise exception '615 refused")
    assert pin < pre.index("if not v_populated then")
    post = _flat(_block("post"))
    for view, md5 in (("property_pipeline_public", "88093f6916bb54a0a9e4333fbf0cd544"),
                      ("pipeline_board_public", "06c950df3e18e0f58c4be6b06973600e")):
        assert (f"md5(pg_get_viewdef('public.{view}'::regclass)) is distinct from '{md5}'"
                in post), view


def test_the_map_function_is_597s_and_this_file_only_calls_it() -> None:
    """Migration 597 (portal exclusion, applied 2026-10-13) re-created browse_map_cells without
    `listing_ids_filter`, so 615 no longer drops, creates, grants or pins it.

    RED by: restating the map here again: it would undo the later definitions (597's portal
    exclusion and after), live and on the replay, which since the renumbering runs this file after
    them. The one call left (section 8's smoke read) names only arguments that 590's signature
    and the latest one both take.
    """
    assert "listing_ids_filter" not in CODE
    assert len(re.findall(r"browse_map_cells", CODE)) == 1
    call = re.search(r"perform public\.browse_map_cells\((.*?)\);", _block("post"), re.S)
    named = re.findall(r"(\w+)\s*=>", call.group(1))
    assert named == ["category_main_filter", "category_type_filter", "bbox_west", "bbox_south",
                     "bbox_east", "bbox_north", "point_budget"]
    latest = latest_definition("browse_map_cells")
    assert int(latest.name.split("_", 1)[0]) >= 597
    for text in (S590, latest.read_text(encoding="utf-8")):
        params = _map_params(text)
        assert len(params) > 60 and not set(named) - set(params)
    assert "listing_ids_filter" not in _map_params(latest.read_text(encoding="utf-8"))


def test_the_board_is_590s_verbatim_and_the_card_is_377s_less_the_note() -> None:
    note = _block("note")
    board = re.search(r"create view pipeline_board_public\n.*?;", note, re.S).group(0)
    assert _flat(board) in _flat(S590)
    card = _flat(re.search(r"create view property_pipeline_public\n.*?;", note, re.S).group(0))
    old = _flat(re.search(r"create or replace view property_pipeline_public as(.*?);", S377,
                          re.S).group(1))
    assert card == ("create view property_pipeline_public with (security_invoker = true) as "
                    + old.replace("pp.note, ", "") + ";")
    on = _flat(note).index
    for view in ("property_pipeline_public", "pipeline_board_public"):
        assert on(f"revoke all on public.{view} from public, anon, authenticated;") < on(
            f"grant select on public.{view} to authenticated;")
    assert (on("drop view public.pipeline_board_public;")
            < on("drop view public.property_pipeline_public;")
            < on("create view property_pipeline_public") < on("create view pipeline_board_public")
            < on("alter table public.property_pipeline drop column note;"))


def test_the_receipt_probes_the_two_restated_views_only() -> None:
    assert [str(o) for o in parse_objects(SQL)] == [
        "relation:property_pipeline_public", "relation:pipeline_board_public"]
