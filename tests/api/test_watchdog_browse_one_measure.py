"""Rule 16, enforced: the Watchdog and Browse share ONE per-m² definition.

Until W5 this was unenforced — `grep -c price_per_m2 tests/api/test_notifications.py`
was 0, and the matcher carried its own hand-typed `price_czk / NULLIF(area_m2, 0)`
for months while Browse filtered on a different number entirely.

WHAT "ONE DEFINITION" LOOKS LIKE IN SQL DEPENDS ON THE RELATION, and that is why
this file does not assert the two clauses are byte-identical — they cannot be:

  * the Watchdog matcher runs against `properties_public`, which PUBLISHES
    `price_per_m2` as `measure_price_per_m2(...)` (migration 425). Reading the
    published column IS reading the measure.
  * `toolkit.comparables._shared_filter_where` runs against the `listings`
    TABLE (its only three FROM clauses are comparables, velocity and the transit
    corridor). `listings` has NO price_per_m2 column, so that spelling does not
    fail review — it fails at PREPARE. The four-argument call is the only legal
    way to name the same measure there.

So the invariant is: NEITHER site derives the formula itself, and BOTH resolve
to `measure_price_per_m2`. That is what these tests pin.
"""

from __future__ import annotations

import re
from pathlib import Path

from api.notifications import WatchdogFilterSpec, _build_match_clauses
from toolkit.comparables import (
    ComparableFilters,
    TargetSpec,
    _shared_filter_where,
    build_query,
)
from toolkit.measures import per_m2_sql

_ROOT = Path(__file__).resolve().parents[2]

# A price-over-area division in SQL, in any of the spellings the repo has used.
_HAND_TYPED_DIVISION = re.compile(
    r"price_czk\s*(::numeric)?\s*/\s*(NULLIF\s*\()?\s*\w*\.?area_m2", re.I
)


def _watchdog_ppm2_clauses() -> list[str]:
    where, _ = _build_match_clauses(
        WatchdogFilterSpec(min_price_per_m2=50_000, max_price_per_m2=120_000)
    )
    return [c for c in where if "price_per_m2" in c]


def _comparables_ppm2_clauses() -> list[str]:
    where, _ = _shared_filter_where(
        TargetSpec(lat=50.0, lng=14.0),
        ComparableFilters(min_price_per_m2=50_000, max_price_per_m2=120_000),
    )
    return [c for c in where if "price_per_m2" in c or "measure_price_per_m2" in c]


def test_watchdog_reads_the_published_measure_column():
    clauses = _watchdog_ppm2_clauses()
    assert clauses == [
        "l.price_per_m2 >= %(min_price_per_m2)s",
        "l.price_per_m2 <= %(max_price_per_m2)s",
    ]


def test_comparables_calls_the_named_measure_over_the_listings_table():
    clauses = _comparables_ppm2_clauses()
    assert clauses == [
        f"{per_m2_sql('l')} >= %(min_price_per_m2)s",
        f"{per_m2_sql('l')} <= %(max_price_per_m2)s",
    ]
    # `listings` has no price_per_m2 column: the published-column spelling would
    # PREPARE-fail here, which is precisely why the two sites differ textually.
    assert all("l.price_per_m2 " not in c for c in clauses)


def test_per_m2_sql_names_the_one_measure():
    assert per_m2_sql("l").startswith("measure_price_per_m2(")
    assert "l.category_main" in per_m2_sql("l")
    assert "l.category_type" in per_m2_sql("l")


def test_neither_site_derives_the_formula_itself():
    """Both bounds resolve to the measure; neither re-spells price / area."""
    for clause in _watchdog_ppm2_clauses() + _comparables_ppm2_clauses():
        assert not _HAND_TYPED_DIVISION.search(clause), clause


def test_no_hand_typed_per_m2_division_survives_in_either_module():
    """The source-text guard: a fifth copy cannot be reintroduced quietly."""
    for rel in ("api/notifications.py", "toolkit/comparables.py",
                "toolkit/transit_axis.py", "toolkit/neighborhoods.py"):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        # Strip comment lines: the WHY of the collapse names the old spelling.
        code = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        )
        assert not _HAND_TYPED_DIVISION.search(code), (
            f"{rel} still derives price-per-m² itself; call the measure instead"
        )


def test_the_sql_corpus_still_cannot_see_these_statements():
    """Pins the premise behind tests/test_measure_sql_prepare.py.

    The automatic SQL gate discovers module-level `*_SQL` constants and inline
    `.execute()` literals; all four per-m² builders assemble their statement in
    a local variable, so discovery reaches none of them. Written as an assertion
    rather than a comment: the day the corpus grows an in-function resolver,
    this fails loudly instead of leaving a duplicate gate running forever.
    """
    from tests.sql_corpus import discover
    from toolkit.neighborhoods import build_query as neighborhood_query
    from toolkit.transit_axis import build_corridor_query
    from toolkit.velocity import build_market_velocity_query

    def norm(sql: str) -> str:
        return " ".join(sql.split())

    target = TargetSpec(lat=50.08, lng=14.42)
    filters = ComparableFilters(min_price_per_m2=50_000)
    rendered = [
        norm(build_query(target, filters)[0]),
        norm(build_market_velocity_query(target, filters, "all")[0]),
        norm(build_corridor_query(target, filters, ["tram"], 800, 300)[0]),
        norm(neighborhood_query(50.08, 14.42, 1000, 30, "byt", "prodej")[0]),
    ]
    discovered = {norm(item.sql) for item in discover(include_inline=True)}
    assert not [sql for sql in rendered if sql in discovered], (
        "sql_corpus now discovers the per-m² statements — fold "
        "tests/test_measure_sql_prepare.py into tests/test_sql_schema_prepare.py"
    )
