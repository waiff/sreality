"""Migration 655's rails, offline (notifications push W2b, PROGRAM.md § 5 "Migration B"): what only the text can show.
A function replace and nothing else: health_liveness() is 633's body with the "api" arm deleted and not one line added,
so every other watcher arm is unchanged. Its post-condition runs in tests/test_health_liveness_live.py."""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from tests.migration_defs import MIGRATIONS

SQL = (MIGRATIONS / "655_health_liveness_without_api_cell.sql").read_text(encoding="utf-8")
CODE = " ".join(re.sub(r"--[^\n]*", "", SQL).split())
_ROOT = Path(__file__).resolve().parents[1]
# What the deleted arm and the deleted API matcher read; W6 drops the table and the six rows (PROGRAM.md § 5 W6).
_UNREAD = ("notification_matcher_lease", "notifications_matcher_interval_seconds",
           "notifications_match_window_listings", "notifications_monitor_interval_seconds",
           "notifications_monitor_window_days", "notifications_outbox_interval_seconds",
           "notifications_watermark_first_seen_at", "notifications_change_match_interval_seconds")


def _function(sql: str) -> list[str]:
    head = sql.index("create or replace function public.health_liveness()")
    return sql[head:sql.index("$fn$;", head)].splitlines()


def test_it_replaces_one_function_and_nothing_else() -> None:
    assert re.findall(r"create or replace function ([\w.]+)", CODE) == ["public.health_liveness"]
    # Outside the function (its body upserts its own registry row and writes its result, as 633's did).
    outside = CODE[:CODE.index("create or replace function")] + CODE[CODE.index("$fn$;"):]
    for verb in ("drop ", "alter ", "delete from", "update ", "insert into", "grant ", "create table",
                 "create index"):
        assert verb not in outside, verb
    assert "revoke all on function public.health_liveness() from public, anon, authenticated;" in CODE
    assert "-- ci-allow-ungated: health_liveness " in SQL


def test_the_body_is_633s_with_the_api_arm_deleted_and_nothing_added() -> None:
    before = _function((MIGRATIONS / "633_health_explicit_cells.sql").read_text(encoding="utf-8"))
    after = _function(SQL)
    ops = [op for op in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes() if op[0] != "equal"]
    assert ops and {op[0] for op in ops} == {"delete"}
    deleted = "\n".join(line for _tag, i1, i2, _j1, _j2 in ops for line in before[i1:i2])
    for gone in ("notification_matcher_lease", "notifications_matcher_interval_seconds", "'subject', 'api'",
                 "c_lease_passes", "v_lease timestamptz"):
        assert gone in deleted, gone
    body = "\n".join(after)
    assert "lease" not in body and "matcher" not in body and "API" not in body


def test_the_post_condition_refuses_a_lease_reader_and_keeps_the_other_arms() -> None:
    post = SQL[SQL.index("do $post$"):]
    assert "strpos(v_def, 'notification_matcher_lease') > 0" in post
    assert "raise exception '655: health_liveness() still judges the API''s matcher lease'" in post
    assert "perform public.health_liveness();" in post
    for arm in ("details -> ''health''", "'''unalerted'''", "'''stale:'''", "'cell:liveness:'"):
        assert arm in post, arm


def test_no_runtime_code_reads_the_lease_or_the_matchers_settings() -> None:
    """After W2b nothing but history names them: the table and six app_settings rows wait for W6's drop."""
    for tree in ("api", "scraper", "scripts", "toolkit", "autodedup", "location_data"):
        for path in (_ROOT / tree).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for name in _UNREAD:
                assert name not in text, f"{path.relative_to(_ROOT)} names {name}"
