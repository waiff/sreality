"""W7-b: the audit relation separates "pending" from "unresolved".

The operator's words, 2026-09-13: "make sure listings like that (that are in a
queue) have their own category on the audit poloh page, as that is a very
different set of listings (not really an issue) from the ones that were run and
not resolved properly (an issue...)".

`state` is computed once an hour inside `location_pin_audit_mv` (migration 518)
and everything the operator sees hangs off it — the page's toggle, its matrix,
and the nav badge, which now counts the issue alone. A migration is not
importable at runtime, so this file is the rail that keeps the THREE ways the
lane can still owe a listing an answer in that CASE:

  1. no row in `listing_location` at all — the resolver never ran;
  2. a row in `dirty_locations` — queued for the drain;
  3. a snapshot newer than the verdict — the ad changed after the resolver looked.

Drop one of them and its rows quietly move into the column the operator reads as
a finding, which is exactly the confusion W7-b exists to end.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
W7B = REPO / "migrations" / "518_location_w7b_audit_state.sql"

_LINE_COMMENT = re.compile(r"--.*$", re.MULTILINE)


def _body() -> str:
    """The file with its prose stripped, so a match is CODE and never a comment."""
    return _LINE_COMMENT.sub("", W7B.read_text(encoding="utf-8"))


def _state_case() -> str:
    """The CASE that ends in `as state`, normalised to single spaces."""
    sql = _body()
    end = sql.index("as state")
    start = sql.rindex("case", 0, end)
    return " ".join(sql[start:end].split())


def test_state_case_carries_all_three_pending_arms() -> None:
    case = _state_case()
    # 1. never resolved.
    assert "when not c.has_row then 'pending'" in case
    # 2. queued for the drain — the one queue, migration 384.
    assert "dirty_locations d" in case
    assert "d.listing_id = c.listing_id" in case
    # 3. the ad changed after the verdict was formed.
    assert "snap.scraped_at > c.resolved_at" in case
    assert case.count("'pending'") == 3
    # And the fall-through is the issue, spelled once.
    assert case.rstrip().endswith("else 'unresolved' end")


def test_the_newest_snapshot_is_one_index_descent_not_an_aggregate() -> None:
    """Arm 3 runs ~44k times per refresh; it must ride the (listing_id,
    scraped_at DESC) btree and stop at the first row."""
    sql = " ".join(_body().split())
    assert "from listing_snapshots s where s.listing_id = c.listing_id" in sql
    assert "order by s.scraped_at desc limit 1" in sql
    assert "max(s.scraped_at)" not in sql


def test_state_is_a_column_of_the_relation_and_is_indexed() -> None:
    sql = _body()
    assert "drop materialized view if exists location_pin_audit_mv cascade;" in sql
    assert "create materialized view location_pin_audit_mv as" in sql
    # REFRESH CONCURRENTLY needs the unique index back after the DROP.
    assert "create unique index if not exists location_pin_audit_mv_pk" in sql
    assert "location_pin_audit_mv_state" in sql
    assert "on location_pin_audit_mv (state)" in " ".join(sql.split())


def test_the_summary_function_groups_by_state() -> None:
    """The page reads ONE payload: if the summary did not carry `state`, the two
    header counts would have to come from a second read that could disagree."""
    sql = " ".join(_body().split())
    assert "drop function if exists location_pin_audit_summary();" in sql
    assert "returns table ( state text," in sql
    assert "select a.state, a.source, a.category_main, a.quality" in sql
    assert (
        "group by a.state, a.source, a.category_main, a.quality, a.sibling_has_pin"
        in sql
    )


def test_the_four_quality_buckets_survive_untouched() -> None:
    """`state` is a new top-level cut, not a replacement: the buckets that
    explain a verdict still refine the unresolved half."""
    sql = _body()
    for bucket in (
        "active_no_claims",
        "active_unresolved",
        "delisted_no_claims",
        "delisted_unresolved",
    ):
        assert f"'{bucket}'" in sql


def test_the_refresh_stays_scheduled_and_the_cron_guard_is_kept() -> None:
    """A DROP + CREATE that forgets the cron leaves the page frozen at the last
    refresh with nothing saying so."""
    sql = _body()
    assert "'refresh-location-pin-audit'" in sql
    assert "raise notice 'pg_cron unavailable" in sql
    # The budget is armed in the COMMAND (migration 371's lesson), not in the
    # function's proconfig.
    assert "set statement_timeout='900s'; select public.refresh_location_pin_audit_mv();" in sql
