"""PREPARE the six per-m² statements the automatic SQL corpus cannot reach.

THE GAP THIS CLOSES, verified rather than assumed. Five of the six are assembled
by in-function concatenation into a local variable, which the AST layer cannot
evaluate and the import-resolve layer never sees (it reads module-level `*_SQL`
constants only): `toolkit/comparables.py` contributes 0 corpus items,
`toolkit/neighborhoods.py` 0, and `toolkit/transit_axis.py`'s 4 items are its
cache statements, not the corridor CTE.

The SIXTH, `api/portal_lookup._MARKET_SQL`, fails the gate a different way and
is the reason this docstring names the mechanism rather than the file shape: it
IS a module-level constant and the sweep DOES discover it, but
`tests/test_sql_schema_prepare._is_format_template` then skips it, because its
`WITH req(source, source_id) AS (VALUES {values})` slot leaves it unrunnable as
written. Discovered and skipped is indistinguishable from covered unless you
look, so it is rendered here and PREPAREd like the rest. A `*_SQL` constant is
NOT evidence of coverage — an unquoted `{slot}` anywhere in it means the sweep
walks past.

So W5's six call sites — the exact statements that moved onto
`measure_price_per_m2` — were covered by NOTHING. `l.price_per_m2` against the
`listings` TABLE (which has no such column) is a 42703 that no fake connection
can raise and no offline assertion can see, and it would have reached production
green — for `_MARKET_SQL`, 500ing `POST /listings/lookup`, the Chrome
extension's only market-data route. A plan that leans on automatic coverage
leans on nothing.

Each build_* function below is pure, so this file reaches them directly and asks
Postgres to PREPARE the result: a full parse, name-resolve and type-check against
the replayed schema, without running the query or touching a row. Same gate,
same DB job, same skip-without-TEST_DATABASE_URL posture as
`tests/test_sql_schema_prepare.py`.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from tests.sql_corpus import to_prepare_form
from tests.test_sql_schema_prepare import _is_param_type_artifact

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — runs only in the CI schema-replay job",
)


def _statements() -> list[tuple[str, str]]:
    """(name, sql) for every statement W5 moved onto the named measure."""
    from api.notifications import WatchdogFilterSpec, _build_match_clauses
    from toolkit.comparables import ComparableFilters, TargetSpec, build_query
    from toolkit.neighborhoods import build_query as neighborhood_query
    from toolkit.transit_axis import build_corridor_query
    from toolkit.velocity import build_market_velocity_query

    target = TargetSpec(lat=50.08, lng=14.42, area_m2=65.0, disposition="2+kk")
    # Every ppm²-bearing branch ON, so the rendered statement is the widest one
    # a caller can produce rather than the narrowest.
    filters = ComparableFilters(
        radius_m=1000,
        min_price_per_m2=50_000,
        max_price_per_m2=200_000,
        min_price_czk=1_000_000,
        max_price_czk=20_000_000,
        category_main="byt",
        category_type="prodej",
        lifecycle="active",
        max_age_days=30,
    )

    out: list[tuple[str, str]] = [
        ("comparables.build_query", build_query(target, filters)[0]),
        (
            "velocity.build_market_velocity_query",
            build_market_velocity_query(target, filters, "all")[0],
        ),
        (
            "transit_axis.build_corridor_query",
            build_corridor_query(target, filters, ["tram"], 800, 300)[0],
        ),
        (
            "neighborhoods.build_query",
            neighborhood_query(50.08, 14.42, 1000, 30, "byt", "prodej")[0],
        ),
    ]

    # The Watchdog matcher renders WHERE fragments, not a statement. Wrap them
    # over the relation the matcher actually runs against (`properties_public`,
    # aliased `l` — api/notifications.py:1311) so the ppm² bound is resolved
    # against the same view in the test as in production.
    where, _ = _build_match_clauses(
        WatchdogFilterSpec(min_price_per_m2=50_000, max_price_per_m2=200_000)
    )
    out.append((
        "notifications._build_match_clauses",
        "SELECT l.property_id FROM properties_public l WHERE "
        + " AND ".join(where),
    ))

    # The extension's market panel. Rendered with a one-row VALUES list, cast
    # exactly as api/portal_lookup.py binds it, so the LEFT JOIN's
    # `l.source = req.source` type-resolves the way it does in production.
    from api.portal_lookup import _MARKET_SQL

    out.append((
        "portal_lookup._MARKET_SQL",
        _MARKET_SQL.format(values="(%s::text, %s::text)"),
    ))
    return out


@pytest.fixture(scope="module")
def _conn() -> Any:
    import psycopg

    conn = psycopg.connect(_DB_URL, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def test_every_per_m2_statement_prepares_against_the_schema(_conn):
    import psycopg

    statements = _statements()
    assert len(statements) == 6, "a per-m² call site lost its PREPARE coverage"

    failures: list[str] = []
    indeterminate: list[str] = []
    for i, (name, sql) in enumerate(statements):
        # The whole reason this file exists: prove the measure resolves.
        if name != "notifications._build_match_clauses":
            assert "measure_price_per_m2" in sql, (
                f"{name} no longer names the measure"
            )
        stmt_name = f"_ppm2check_{i}"
        try:
            with _conn.cursor() as cur:
                cur.execute(f"PREPARE {stmt_name} AS {to_prepare_form(sql)}")
                cur.execute(f"DEALLOCATE {stmt_name}")
        except psycopg.Error as exc:
            line = (
                f"  [{exc.sqlstate or '?????'}] {name}\n"
                f"      -> {str(exc).strip().splitlines()[0]}"
            )
            (indeterminate if _is_param_type_artifact(exc) else failures).append(line)

    if indeterminate:
        print("\nIndeterminate parameter type (not failures):")
        print("\n".join(indeterminate))
    assert not failures, (
        "per-m² statements failed to PREPARE against the replayed schema:\n"
        + "\n".join(failures)
    )
