"""Migration 656's rails, offline (notifications push W6, PROGRAM.md § 5 "Migration E"): what only the text can show.
The one destructive window: a $pre$ that refuses before any lock, then one transaction in the program's order, then a
$post$ that proves the 15 columns. Its pre- and post-conditions run in CI's schema replay, which applies every file."""

from __future__ import annotations

import difflib
import re

from tests.migration_defs import MIGRATIONS

SQL = (MIGRATIONS / "656_notifications_w6_prune.sql").read_text(encoding="utf-8")
CODE = " ".join(re.sub(r"--[^\n]*", "", SQL).split())
_SETTINGS = ("notification_email_to", "notification_telegram_chat_id", "system_health_channels",
             "notifications_matcher_interval_seconds", "notifications_match_window_listings",
             "notifications_monitor_interval_seconds", "notifications_monitor_window_days",
             "notifications_outbox_interval_seconds", "notifications_watermark_first_seen_at")
_TABLES = ("channel_sends", "notification_suppression", "resend_webhook_events", "notification_matcher_lease")
_COLUMNS = ("dedupe_key", "trigger_snapshot_id", "channel", "status", "error_message", "target_channels",
            "sreality_id", "estimation_run_id", "message")


def test_the_header_marks_it_destructive_and_names_the_gate() -> None:
    head = SQL[:SQL.index("set lock_timeout")]
    assert "DESTRUCTIVE." in head and '"yes, apply it"' in head
    assert "backup_before_drop.yml" in head and "label notifications-w6" in head
    assert "NEVER through the MCP" in head and "apply_migration.yml only" in head
    assert "W2b (#1951" in head and "Forward only (rule 1)" in head
    # The code that stops reading `message` ships in the same PR, so the file applies after its merge.
    assert "this file's own PR (W6) are MERGED AND DEPLOYED" in head and "applies AFTER its merge" in head


def test_pre_refuses_before_the_transaction_and_post_proves_after_it() -> None:
    pre, begin, commit = CODE.index("do $pre$"), CODE.index("begin;"), CODE.index("commit;")
    collections, post = CODE.index("alter table public.collections drop column if exists notify_channels;"), \
        CODE.index("do $post$")
    assert pre < begin < commit < collections < post
    assert CODE.count("begin;") == 1 and CODE.count("commit;") == 1
    guard = CODE[pre:begin]
    for refusal in ("details ? 'notifications'", "expires_at > now() - interval '5 minutes'",
                    "notification_dispatches_identity_uq", "notification_dispatches_account_feed_idx",
                    "to_regclass('public.notification_deliveries') is null",
                    "where sreality_id is not null and listing_id is null", "p.prosrc ~", "pg_depend d",
                    "d.deptype = 'n'", "where source_kind <> 'system_health'"):
        assert refusal in guard, refusal
    assert guard.count("raise exception '656 refused:") == 9


def test_the_transaction_runs_in_the_programs_order() -> None:
    txn = CODE[CODE.index("begin;"):CODE.index("commit;")]
    steps = [
        "lock table public.notification_dispatches, public.notification_subscriptions in access exclusive mode;",
        "delete from public.notification_dispatches where source_kind = 'system_health';",
        "create or replace function public.sync_notification_dispatches_account_id()",
        "add constraint notification_dispatches_source_ck check",
        "add constraint notification_dispatches_change_kind_ck check",
        "drop column if exists dedupe_key",
        "drop index if exists public.notification_dispatches_source_kind_idx;",
        "alter table public.notification_subscriptions drop column if exists channels,",
        "drop table if exists public.channel_sends;",
        "drop function if exists public.emit_verification_stale_alert();",
        "insert into public.app_settings_history",
        "delete from public.app_settings",
    ]
    at = [txn.index(step) for step in steps]
    assert at == sorted(at), steps
    # The delete is the one row deletion, behind a delivery count; a NOTICE carries how many went.
    system = txn[txn.index("do $system$"):txn.index("$system$;")]
    assert "from public.notification_deliveries x" in system and "raise notice '656: %" in system
    assert CODE.count("delete from public.notification_dispatches") == 1


def test_every_drop_is_idempotent_and_none_cascades() -> None:
    assert not re.search(r"\bcascade;", CODE.lower())
    assert "execute " not in CODE.lower() and "-- ci-allow-dynamic:" not in SQL
    for stmt in re.findall(r"\bdrop (?:table|index|function|column|constraint) (\S+)", CODE):
        assert stmt == "if", stmt
    for table in _TABLES:
        assert f"drop table if exists public.{table};" in CODE
    for column in _COLUMNS:
        assert f"drop column if exists {column}" in CODE
    for column in ("channels", "last_matched_first_seen_at", "notify_channels"):
        assert f"drop column if exists {column}" in CODE


def test_the_nine_settings_are_named_alike_four_times_and_copied_before_they_go() -> None:
    lists = re.findall(r"key in \(([^)]*)\)", CODE) + re.findall(r"c_settings constant text\[\] := array\[([^\]]*)\]",
                                                                 CODE)
    assert len(lists) == 4
    for found in lists:
        assert tuple(re.findall(r"'(\w+)'", found)) == _SETTINGS
    assert CODE.index("insert into public.app_settings_history") < CODE.index("delete from public.app_settings")


def test_the_trigger_is_641s_body_but_for_its_system_branch() -> None:
    def body(sql: str) -> list[str]:
        head = re.search(r"create or replace function (?:public\.)?sync_notification_dispatches_account_id\(\)", sql)
        assert head, "no trigger body"
        start = sql.index("$$", head.end()) + 2
        return [line.strip() for line in sql[start:sql.index("$$;", start)].splitlines() if line.strip()]

    before = body((MIGRATIONS / "641_pipeline_shares_and_acting.sql").read_text(encoding="utf-8"))
    after = body(SQL)
    ops = [op for op in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes() if op[0] != "equal"]
    assert [op[0] for op in ops] == ["replace"]
    _tag, i1, i2, j1, j2 = ops[0]
    assert before[i1:i2] == ["new.account_id := '00000000-0000-0000-0000-000000000000';"]
    assert after[j1:j2] == ["raise exception 'notification_dispatches has no producer %', new.source_kind;"]


def test_the_checks_name_three_producers_and_nine_kinds() -> None:
    source = CODE.split("add constraint notification_dispatches_source_ck check (", 1)[1].split(");", 1)[0]
    assert set(re.findall(r"source_kind = '(\w+)'", source)) == {"watchdog", "collection_monitor", "share"}
    kinds = CODE.split("add constraint notification_dispatches_change_kind_ck check (", 1)[1].split(");", 1)[0]
    assert set(re.findall(r"'(\w+)'", kinds)) == {
        "new", "price_drop", "price_rise", "inactive", "reactivated", "new_source",
        "shared_collection", "shared_dashboard", "shared_pipeline"}


def test_post_pins_the_fifteen_columns() -> None:
    post = CODE[CODE.index("do $post$"):]
    cols = re.search(r"v_cols is distinct from array\[([^\]]*)\]", post)
    assert cols
    assert re.findall(r"'(\w+)'", cols.group(1)) == sorted([
        "id", "subscription_id", "collection_id", "source_kind", "property_id", "listing_id", "change_kind",
        "trigger_price_czk", "prev_price_czk", "dispatched_at", "seen_at", "account_id", "event_ref",
        "actor_account_id", "duplicate_of"])
    for proof in ("i.indisvalid", "i.indnullsnotdistinct", "emit_verification_stale_alert()",
                  "source_kind = 'system_health'", "notification_dispatches_source_kind_idx"):
        assert proof in post, proof
