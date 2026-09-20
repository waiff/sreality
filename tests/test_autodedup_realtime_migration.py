"""Shape gate for migration 539 — the real-time shadow lane's state (PROGRAM.md E65/E66/E67).

Offline, no DB. The generic RLS/grant rails see every statement here; this file checks what a
generic rail cannot know: that the lane's own tables never reach outside schema `autodedup`
(ruling D4, and D8's ban on DDL over a shared hot table), that the posting table is keyed by
GENERATION so two calibrations can never share one probe key, and that E66's two direction
bits and E64's stored census are columns rather than something re-derived at read time.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION = _ROOT / "migrations" / "539_autodedup_realtime_lane.sql"


def _sql() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def _code() -> str:
    body = "\n".join(line.split("--")[0] for line in _sql().lower().splitlines())
    return re.sub(r"\s+", " ", body)


def test_the_migration_exists_and_touches_only_schema_autodedup() -> None:
    assert _MIGRATION.is_file()
    code = _code()
    for table in re.findall(r"create table if not exists (\S+)", code):
        assert table.startswith("autodedup."), table
    for table in re.findall(r"alter table (\S+)", code):
        assert table.startswith("autodedup."), table
    for table in re.findall(r"update (\S+)", code):
        assert table.startswith("autodedup."), table
    for table in re.findall(r"delete from (\S+)", code):
        assert table.startswith("autodedup."), table


def test_it_sets_a_plain_lock_timeout_and_resets_it() -> None:
    code = _code()
    assert "set lock_timeout = '5s';" in code
    assert "set local" not in code
    assert "reset lock_timeout;" in code


def test_it_is_additive_and_idempotent() -> None:
    code = _code()
    assert "drop table" not in code
    assert "drop column" not in code
    for statement in re.findall(r"create table (\S+)", code):
        assert statement == "if", code
    assert code.count("add column if not exists") >= 7


def test_the_posting_table_is_keyed_by_generation() -> None:
    """E65: a probe key is a function of the frozen calibration (K3's price decile), so two
    generations must not be able to read each other's postings."""
    code = _code()
    assert "create table if not exists autodedup.fp_key" in code
    assert "primary key (generation, probe, key_token, listing_id)" in code
    assert "autodedup_fp_key_listing_idx on autodedup.fp_key (generation, listing_id)" in code


def test_the_pair_grain_carries_both_directions_the_census_and_the_certificate() -> None:
    """E66 keeps a pair while EITHER side retrieves the other; E64 replays the census a
    promotion was taken under, never today's; and E33 orders a component's edges
    certificate-first, which a lane clustering from STORED rows can only do from a column."""
    code = _code()
    for column in ("from_lo", "from_hi", "evidence", "context", "calibration_digest",
                   "certificate", "fp_lo", "fp_hi"):
        assert f"add column if not exists {column}" in code, column


def test_the_fingerprint_row_is_generation_scoped_and_sweepable() -> None:
    """Not migration 528's `listing_fp`: that one is keyed on `listing_id` alone, so it could
    not hold two generations, and no lane has ever written it."""
    code = _code()
    assert "create table if not exists autodedup.rt_fp" in code
    assert "primary key (generation, listing_id)" in code
    # The revive sweep's slice — the only feed that can see a `touch_listings` revival.
    assert ("autodedup_rt_fp_inactive_idx on autodedup.rt_fp (generation, listing_id) "
            "where is_active = false") in code


def test_the_lane_uses_a_lease_row_and_not_an_advisory_lock() -> None:
    code = _code()
    assert "autodedup.rt_lease" in code
    assert "pg_advisory" not in code


def test_every_new_table_enables_rls_and_revokes_the_browser_roles() -> None:
    code = _code()
    created = set(re.findall(r"create table if not exists (autodedup\.\w+)", code))
    for table in created:
        assert f"alter table {table} enable row level security" in code, table
        assert f"revoke all on {table} from anon, authenticated" in code, table
    assert "create policy" not in code


def test_it_adds_no_index_to_a_shared_hot_table() -> None:
    """D8. The six probes key on DERIVED values no index on public.listings could serve, which
    is why the postings are a side table this lane owns rather than an index over there."""
    code = _code()
    for index in re.findall(r"create index if not exists \S+ on (\S+)", code):
        assert index.startswith("autodedup."), index
    assert "public." not in code
