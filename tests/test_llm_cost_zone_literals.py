"""The /costs day boundary is spelled in exactly four places, and they must agree.

Migration 437 moved "which day is this call on" into the database. After it, the Prague day
is named in exactly three database objects — `llm_cost_daily_public`'s day expression,
`llm_cost_hour_rollup_prague_day_idx`, and `llm_cost_today_usd()`'s body — plus one place in
the SPA (`frontend/src/lib/llmCosts.ts`, which labels and buckets what the page renders).
The hour grain, `llm_cost_hour_union`, names UTC and nothing else.

WHY THIS NEEDS A RAIL AT ALL. Change `'Europe/Prague'` to `'Europe/Berlin'` in any ONE of
them and **every number on the page stays identical** — the two zones have shared offsets
since 1980 — while the expression index silently stops matching the view it was built for.
Lowercase it to `'europe/prague'` and the same thing happens for the opposite reason: the
zone still resolves, and the index expression no longer matches character for character.
There is no test anywhere else that can see either mutation, and no user-visible symptom.

The Python half is the mirror image: `api.llm_client.DAILY_COST_TODAY_SQL` must carry NO
date arithmetic and NO zone of its own. It used to spell its own UTC day, so the spend
guard's "today" and the page's "today" were free to drift apart — again with every number
individually correct.

RED by: any of the three catalog literals differing from the others in bytes; the union
gaining a Prague literal (the hour grain drifting off UTC); a `::date` or `now()` creeping
back into `DAILY_COST_TODAY_SQL`; or `llmCosts.ts` naming the zone twice (the second copy is
how the tile and the chart start disagreeing).

Lane: migrations. The catalog reads need the replayed schema, and the two text assertions
ride along with them so all four spellings are checked in one place.

KNOWN GAP, stated rather than hidden: that lane triggers on `**/*.py` and `migrations/**`, so
a frontend-ONLY pull request touching `llmCosts.ts` does not run this file. Adding
`frontend/src/lib/llmCosts.ts` to the lane's path filter closes it, at the cost of
regenerating `frontend/public/workflow-docs.json` (CI checks that codegen for drift).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_REQUIRED = os.environ.get("DB_RAILS_REQUIRED") == "1"

pytestmark = pytest.mark.skipif(
    not _DB_URL and not _REQUIRED,
    reason="TEST_DATABASE_URL not set — this rail runs in CI's migrations lane",
)

# Case-insensitive on the keywords, CASE-SENSITIVE on the captured literal — that asymmetry
# is the point: Postgres deparses the keywords in its own case, but the zone string is the
# thing that has to match byte for byte across the three objects.
_ZONE_RE = re.compile(r"(?i:at\s+time\s+zone)\s*'([^']*)'")

_ROOT = Path(__file__).resolve().parent.parent
_SPA_ZONE_FILE = _ROOT / "frontend" / "src" / "lib" / "llmCosts.ts"

_PRAGUE_ARTIFACTS = ("daily view", "prague-day index", "llm_cost_today_usd")


@pytest.fixture(scope="module")
def conn():
    if not _DB_URL:
        pytest.fail(
            "DB_RAILS_REQUIRED=1 but TEST_DATABASE_URL is not set — the migrations lane "
            "is misconfigured and this rail would otherwise have skipped green."
        )
    import psycopg

    with psycopg.connect(_DB_URL, autocommit=True) as c:
        yield c


def _zones(text: str) -> set[str]:
    return set(_ZONE_RE.findall(text))


@pytest.fixture(scope="module")
def definitions(conn) -> dict[str, str]:
    """The four catalog texts, read as the database itself renders them."""
    out: dict[str, str] = {}
    with conn.cursor() as cur:
        cur.execute("select pg_get_viewdef('public.llm_cost_daily_public'::regclass, true)")
        out["daily view"] = cur.fetchone()[0]

        cur.execute(
            "select indexdef from pg_indexes where schemaname='public' and indexname=%s",
            ("llm_cost_hour_rollup_prague_day_idx",),
        )
        row = cur.fetchone()
        assert row, (
            "llm_cost_hour_rollup_prague_day_idx is missing — the daily view's day filter "
            "falls back to a seq scan of the rollup"
        )
        out["prague-day index"] = row[0]

        cur.execute("select pg_get_functiondef(%s::regprocedure)", ("public.llm_cost_today_usd()",))
        out["llm_cost_today_usd"] = cur.fetchone()[0]

        cur.execute("select pg_get_viewdef('public.llm_cost_hour_union'::regclass, true)")
        out["hour union"] = cur.fetchone()[0]
    return out


@pytest.mark.parametrize("artifact", _PRAGUE_ARTIFACTS)
def test_each_prague_artifact_names_exactly_one_zone(definitions, artifact):
    zones = _zones(definitions[artifact])
    assert zones == {"Europe/Prague"}, (
        f"{artifact} names {sorted(zones) or 'no zone'} — the Prague day is spelled in "
        f"three places and every one of them must say exactly 'Europe/Prague':\n"
        f"{definitions[artifact]}"
    )


def test_the_three_prague_spellings_are_byte_identical(definitions):
    """Set equality, not `'prague' in text.lower()`.

    A substring hunt would accept 'europe/prague', which resolves at runtime and still stops
    the expression index from matching the view.
    """
    sets = {a: _zones(definitions[a]) for a in _PRAGUE_ARTIFACTS}
    distinct = {frozenset(s) for s in sets.values()}
    assert len(distinct) == 1, (
        "the three Prague-day spellings have diverged (the numbers stay identical when they "
        f"do; only the index stops matching): { {a: sorted(s) for a, s in sets.items()} }"
    )


def test_the_hour_union_names_utc_and_nothing_else(definitions):
    """The hour grain must never acquire a display zone.

    `llm_cost_hour_union` is a view rather than inlined text precisely so this assertion can
    be a clean set equality: `pg_get_viewdef` does not expand a referenced view, so the
    daily view above shows only Prague and this one shows only UTC.
    """
    zones = _zones(definitions["hour union"])
    assert zones == {"UTC"}, (
        f"llm_cost_hour_union names {sorted(zones) or 'no zone'} — its bucket boundaries "
        "must stay UTC, or the stored hour grain shifts under the rollup"
    )


def test_the_spend_guard_statement_carries_no_date_arithmetic():
    """`DAILY_COST_TODAY_SQL` must delegate "today" entirely to the database.

    Any of these tokens reappearing means the guard has grown its own opinion about the day
    boundary — which is how it silently drifted from the page's before 437.
    """
    from api.llm_client import DAILY_COST_TODAY_SQL

    sql = DAILY_COST_TODAY_SQL.lower()
    for token in ("at time zone", "::date", "current_date", "now()", "interval"):
        assert token not in sql, (
            f"DAILY_COST_TODAY_SQL spells its own day boundary again ({token!r}): "
            f"{DAILY_COST_TODAY_SQL!r}"
        )
    assert "llm_cost_today_usd" in sql, (
        f"DAILY_COST_TODAY_SQL no longer calls the one function that owns the guard's "
        f"'today': {DAILY_COST_TODAY_SQL!r}"
    )


def test_the_spa_names_the_zone_exactly_once():
    """One literal in `llmCosts.ts`, shared by the day key, the tile and the axis.

    Read as text on purpose — this is a Python test tree and importing anything from the
    frontend would be a territory violation, not just inconvenient.
    """
    assert _SPA_ZONE_FILE.exists(), f"{_SPA_ZONE_FILE} is missing"
    occurrences = _SPA_ZONE_FILE.read_text(encoding="utf-8").count("Europe/Prague")
    assert occurrences == 1, (
        f"{_SPA_ZONE_FILE.name} names 'Europe/Prague' {occurrences} time(s); it must name it "
        "exactly once and share that constant — a second copy is how the KPI tile and the "
        "chart axis start disagreeing for the two hours a day the zones differ"
    )
