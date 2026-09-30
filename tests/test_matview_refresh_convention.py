"""One publication path for served matviews (migration 578, Broker Unify W1).

The 2026-09-30 Brokers outage was a plain (ACCESS EXCLUSIVE) REFRESH of
broker_region_type_stats blocking every reader for the whole rebuild, defended by a
comment that falsely claimed CONCURRENTLY cannot run inside a transaction. Migration
578 made the safe form the only form: every Python refresh goes through
scraper.db.refresh_matview -> public.refresh_matview, which refuses unregistered
names, refreshes CONCURRENTLY (plain only on first populate), skips an in-flight
duplicate, and stamps derived_artifacts. These rails keep the class unrepresentable,
offline, before a bypass ships.

Pre-578 SQL producers (refresh_health_matviews, refresh_location_pin_audit_mv, both
blue-green rebuilds, refresh_llm_cost_rollups) keep their inline concurrent-refresh +
stamp shapes — they are pinned by tests/test_derived_artifacts_stamping.py and were
never the blocking class. New SQL producers should call the chokepoint.

Every test states the mutation that makes it RED.
"""

from __future__ import annotations

import re
from pathlib import Path

# Dollar-quote-aware comment stripping, reused like test_broker_leaderboard_contract
# reuses the rls-grants helpers: migration headers quote refresh statements in prose.
from tests.test_derived_artifacts_stamping import _drop_sql_comment_lines

_REPO = Path(__file__).resolve().parent.parent
_MIGRATIONS = _REPO / "migrations"
_SOURCE_TREES = ("api", "scraper", "toolkit", "scripts", "location_data")
_CHOKEPOINT_MIGRATION = 578

_REFRESH = re.compile(r"refresh\s+materialized\s+view", re.IGNORECASE)
_PLAIN_REFRESH = re.compile(
    r"refresh\s+materialized\s+view\s+(?!concurrently\b)", re.IGNORECASE
)
_DEFINES_CHOKEPOINT = re.compile(
    r"create\s+or\s+replace\s+function\s+public\.refresh_matview\s*\(", re.IGNORECASE
)


def _mig_number(path: Path) -> int:
    return int(re.match(r"(\d+)", path.name).group(1))


def _stripped(path: Path) -> str:
    return _drop_sql_comment_lines(path.read_text(encoding="utf-8"))


def test_no_python_code_refreshes_a_matview_directly():
    """RED by: writing `cur.execute("refresh materialized view ...")` in any production
    tree. That statement's plain form takes ACCESS EXCLUSIVE and blocks every reader —
    the exact 2026-09-30 outage — and even the concurrent form bypasses the registry
    check and the stamp. scraper.db.refresh_matview is the one Python path."""
    offenders = []
    for tree in _SOURCE_TREES:
        root = _REPO / tree
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if _REFRESH.search(path.read_text(encoding="utf-8")):
                offenders.append(path.relative_to(_REPO).as_posix())
    assert not offenders, (
        f"Python file(s) refresh a matview directly instead of through "
        f"scraper.db.refresh_matview (migration 578): {offenders}"
    )


def test_no_new_migration_ships_a_plain_refresh_outside_the_chokepoint():
    """RED by: a migration numbered above 578 running a plain (non-CONCURRENT) REFRESH
    anywhere but inside a redefinition of public.refresh_matview itself. A brand-new
    matview populates via CREATE ... WITH DATA; a served matview republishes through
    the chokepoint. Older migrations are history and stay unedited (rule #1)."""
    offenders = []
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        if _mig_number(path) <= _CHOKEPOINT_MIGRATION:
            continue
        text = _stripped(path)
        if _DEFINES_CHOKEPOINT.search(text):
            continue  # the chokepoint's own first-populate branch lives here
        if _PLAIN_REFRESH.search(text):
            offenders.append(path.name)
    assert not offenders, (
        f"migration(s) after {_CHOKEPOINT_MIGRATION} ship a plain (blocking) REFRESH "
        f"outside public.refresh_matview: {offenders}. Publish through the chokepoint."
    )


def test_the_chokepoint_guards_before_it_refreshes():
    """RED by: a redefinition of public.refresh_matview losing the registration RAISE,
    the undefined_table errcode on a missing matview, the concurrent branch, the
    in-flight skip, the stamp, or one of the three EXECUTE revokes. The latest defining
    migration is checked, so a future redefinition stays honest without edits here."""
    defining = [p for p in _MIGRATIONS.glob("*.sql") if _DEFINES_CHOKEPOINT.search(_stripped(p))]
    assert defining, "no migration defines public.refresh_matview"
    latest = max(defining, key=_mig_number)
    flat = " ".join(_stripped(latest).lower().split())

    assert "from derived_artifacts" in flat and "raise exception" in flat, (
        f"{latest.name}: the registration guard (RAISE on a name with no "
        "derived_artifacts row) is gone — the silent-no-op stamp hole reopens"
    )
    assert "errcode = 'undefined_table'" in flat, (
        f"{latest.name}: a missing matview must raise undefined_table so deploy-race "
        "tolerant callers (scripts/refresh_image_stats.py) can keep catching it"
    )
    assert "refresh materialized view concurrently %i" in flat, (
        f"{latest.name}: the CONCURRENTLY branch is gone — readers would block again"
    )
    assert "pg_try_advisory_xact_lock" in flat, (
        f"{latest.name}: the in-flight-refresh skip is gone — overlapping producers "
        "would queue duplicate multi-minute builds"
    )
    assert "stamp_derived_artifact" in flat, (
        f"{latest.name}: the chokepoint no longer stamps the registry"
    )
    revokes = flat.count("revoke execute on function public.refresh_matview")
    assert revokes >= 3, (
        f"{latest.name}: expected EXECUTE revokes for public, anon and authenticated "
        f"(migration 287's default-ACL lesson), found {revokes}"
    )
