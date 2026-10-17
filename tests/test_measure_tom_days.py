"""One time-on-market, one definition (migration 617).

Offline: the effective body of each view that publishes `tom_days` calls the function and
spells no arithmetic of its own, and no Python module renders the arithmetic either.
Replayed schema (CI's migrations job, `TEST_DATABASE_URL`): the function answers the
documented cases, and the one relation a bare listing reaches (`listings_public`) and the
SQL the Python emitters render all return exactly what the function returns for the same row.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from tests._live_property import REQUIRED_DB, cur  # noqa: F401 -- `cur` is the fixture
from toolkit.measures import tom_days_sql

_ROOT = Path(__file__).resolve().parents[1]
_VIEWS = ("properties_public", "listings_public", "listing_feed_public", "browse_projection")


def _effective_definition(view: str) -> str:
    pattern = re.compile(
        rf"create\s+(or\s+replace\s+)?view\s+(public\.)?{view}\b(.*?);\n", re.I | re.S,
    )
    last = ""
    for path in sorted((_ROOT / "migrations").glob("[0-9]*.sql")):
        for m in pattern.finditer(path.read_text()):
            last = m.group(0)
    assert last, f"no migration defines {view}"
    return re.sub(r"--[^\n]*", "", last)


@pytest.mark.parametrize("view", _VIEWS)
def test_a_view_publishes_tom_days_by_calling_the_measure(view: str) -> None:
    body = _effective_definition(view)
    assert re.search(r"measure_tom_days\(\s*\w+\.is_active|measure_tom_days\(\s*is_active", body), view
    assert "86400" not in body, f"{view} spells time on market inline again"


_SQL_EPOCH_SPELLING = re.compile(r"epoch\s+from[\s\S]{0,120}?first_seen_at[\s\S]{0,60}?86400", re.I)
_PYTHON_DAYS_SPELLING = re.compile(r"\bfirst_seen\w*\s*\)?\s*\.days\b")


def test_no_python_module_spells_the_tom_arithmetic() -> None:
    """Two spellings the old copies used: SQL `epoch ... first_seen_at ... 86400` and
    `(now - first_seen).days`. A third (e.g. a bare timedelta subtraction) is invisible to a
    pattern, which is why the known consumers are also pinned positively below."""
    offenders = []
    for tree in ("scraper", "toolkit", "api", "scripts"):
        for path in (_ROOT / tree).rglob("*.py"):
            if path.name == "measures.py":
                continue
            text = path.read_text()
            if _SQL_EPOCH_SPELLING.search(text) or _PYTHON_DAYS_SPELLING.search(text):
                offenders.append(str(path.relative_to(_ROOT)))
    assert offenders == [], f"time on market is re-spelled in {offenders}; call tom_days_sql"


@pytest.mark.parametrize("module", ["filter_compiler", "velocity", "snapshots"])
def test_the_known_consumers_call_the_one_face(module: str) -> None:
    assert "tom_days_sql(" in (_ROOT / "toolkit" / f"{module}.py").read_text()


@REQUIRED_DB
@pytest.mark.parametrize(
    ("active", "first", "last", "want"),
    [
        (True, "10 days 12 hours", "0 seconds", 10),      # open run: age so far, floored
        (True, "0 seconds", "0 seconds", 0),
        (False, "30 days", "20 days", 10),                # closed run: first -> last sighting
        (False, "30 days", "29 days 1 hour", 0),          # under a day floors to zero
        (False, "20 days", "30 days", 0),                 # last before first clamps, never negative
        (None, "30 days", "20 days", 10),                 # unknown status reads as closed
    ],
)
def test_the_measure_answers_the_documented_cases(
    cur: Any, active: bool | None, first: str, last: str, want: int,
) -> None:
    cur.execute(
        "SELECT measure_tom_days(%s, now() - %s::interval, now() - %s::interval)",
        (active, first, last),
    )
    assert cur.fetchone()[0] == want


@REQUIRED_DB
def test_a_missing_first_sighting_is_a_gap_not_a_zero(cur: Any) -> None:
    cur.execute("SELECT measure_tom_days(false, NULL, now())")
    assert cur.fetchone()[0] is None


@REQUIRED_DB
@pytest.mark.parametrize("active", [True, False])
def test_the_view_and_the_python_emitter_agree_with_the_function(cur: Any, active: bool) -> None:
    from tests._live_property import _advert, _property

    pid = _property(cur)
    lid = _advert(cur, pid, source="sreality", active=active)
    cur.execute(
        "UPDATE listings SET first_seen_at = now() - interval '12 days 5 hours', "
        "last_seen_at = now() - interval '4 days' WHERE id = %s",
        (lid,),
    )
    cur.execute(
        "SELECT measure_tom_days(is_active, first_seen_at, last_seen_at) FROM listings WHERE id = %s",
        (lid,),
    )
    want = cur.fetchone()[0]
    assert want == (12 if active else 8)

    cur.execute("SELECT tom_days FROM listings_public WHERE id = %s", (lid,))
    assert cur.fetchone()[0] == want

    cur.execute(f"SELECT {tom_days_sql('l')} FROM listings l WHERE l.id = %s", (lid,))
    assert cur.fetchone()[0] == want


@REQUIRED_DB
def test_the_measure_inlines(cur: Any) -> None:
    cur.execute(
        "EXPLAIN (VERBOSE, COSTS OFF) "
        "SELECT measure_tom_days(is_active, first_seen_at, last_seen_at) FROM listings"
    )
    plan = "\n".join(r[0] for r in cur.fetchall())
    assert "measure_tom_days" not in plan, plan
