"""The filter compile's old and new predicates select the same rows (C4, rule 16).

The golden (`tests/fixtures/filter_compile_golden.json`) pins the TEXT the hand-coded
adapters rendered before `toolkit/filter_compiler.py` existed. Text equality is necessary
but says nothing about a fragment Postgres reads differently than it looks (a param type,
a NULL arm, a measure call), so this executes every golden case twice over seeded rows —
once with the golden's fragments, once with what the current adapter renders for the same
input — and the two id sets must be equal. Executing them also type-checks every template
and hook against the real column types, which is stronger than PREPARE.

A comparison of two empty sets proves nothing, so every regular single-filter case must
also select a strict, non-empty subset of its grain's invariant cohort. A case that cannot
be made to discriminate goes in `_EXEMPT_NON_VACUITY` with a written reason; the assertion
is never loosened.

Runs in CI's migrations lane (`TEST_DATABASE_URL`, `DB_RAILS_REQUIRED=1`). The seeded
relations are TEMP tables that shadow `public` (pg_temp is searched first, and
`curated_cities_matching()` is `language sql` with no SET search_path, so its body reads
the shadows too), inside one transaction that is always rolled back.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

import toolkit.filter_registry as fr
from tests.toolkit import test_filter_compiler_golden as gold
from toolkit.measures import per_m2_sql, plot_area_sql

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_REQUIRED = os.environ.get("DB_RAILS_REQUIRED") == "1"

pytestmark = pytest.mark.skipif(
    not _DB_URL and not _REQUIRED,
    reason="TEST_DATABASE_URL not set — this rail runs in CI's migrations lane",
)

_SHADOWED = (
    "listings", "listing_location", "properties", "listing_fetch_failures",
    "properties_public", "curated_cities_public", "city_index_values_public",
)

_STATEMENT = {
    "listings": "SELECT l.id FROM listings l "
                "JOIN listing_location ll ON ll.listing_id = l.id WHERE {where}",
    "watchdog": "SELECT l.property_id FROM properties_public l WHERE {where}",
}

# case name -> why it cannot select a strict, non-empty subset. Empty on purpose.
_EXEMPT_NON_VACUITY: dict[str, str] = {}

# Column-backed filters whose SQL is not a plain `<column> <op> value`.
_HOOKED = frozenset({"building_material", "min_price_czk", "max_price_czk"})

_CASES = [
    c for c in (gold.golden() if gold.GOLDEN_PATH.exists() else [])
    if c["grain"] in _STATEMENT and "raises" not in c
]


@pytest.fixture(scope="module")
def conn():
    if not _DB_URL:
        pytest.fail(
            "DB_RAILS_REQUIRED=1 but TEST_DATABASE_URL is not set — the migrations lane "
            "is misconfigured and this rail would otherwise have skipped green."
        )
    import psycopg

    c = psycopg.connect(
        _DB_URL,
        options="-c statement_timeout=20000 -c lock_timeout=5000"
        " -c idle_in_transaction_session_timeout=120000",
    )
    try:
        with c.cursor() as cur:
            _seed(cur)
        yield c
    finally:
        c.rollback()
        c.close()


def _seed(cur: Any) -> None:
    data = gold.rows()
    for rel in _SHADOWED:
        cur.execute(f"CREATE TEMP TABLE {rel} AS SELECT * FROM public.{rel} WHERE false")
    for rel in _SHADOWED:
        cur.execute(f"SELECT * FROM {rel} LIMIT 0")
        columns = {d.name for d in cur.description}
        for row in data[rel]:
            _insert(cur, rel, columns, row)


def _insert(cur: Any, rel: str, columns: set[str], row: dict[str, Any]) -> None:
    cols: list[str] = []
    exprs: list[str] = []
    params: list[Any] = []
    for key, value in row.items():
        if rel == "listing_location" and key in ("lat", "lng"):
            continue
        col, expr = key, "%s"
        if key.endswith("__days_ago"):
            col, expr = key[: -len("__days_ago")], "now() - make_interval(days => %s)"
        if col not in columns:
            continue  # an annotation (e.g. the measure's expected value), not a column
        cols.append(col)
        exprs.append(expr)
        params.append(value)
    if rel == "listing_location":
        cols.append("geom")
        exprs.append(
            "CASE WHEN %s::float8 IS NULL THEN NULL "
            "ELSE ST_SetSRID(ST_MakePoint(%s::float8, %s::float8), 4326) END"
        )
        params.extend([row["lat"], row["lng"], row["lat"]])
    cur.execute(
        f"INSERT INTO {rel} ({', '.join(cols)}) VALUES ({', '.join(exprs)})", params,
    )


def _ids(conn: Any, grain: str, where: list[str], params: dict[str, Any]) -> set[int]:
    sql = _STATEMENT[grain].format(where=" AND ".join(where) if where else "true")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(sql, params)
        return {int(r[0]) for r in cur.fetchall()}


def _golden_ids(conn: Any, case: dict[str, Any]) -> set[int]:
    return _ids(conn, case["grain"], case["where_sorted"], gold.decode(case["params"]))


@pytest.fixture(scope="module")
def cohort(conn) -> dict[str, set[int]]:
    """Each grain's invariant cohort: the ids its empty input selects."""
    by_name = {c["name"]: c for c in _CASES}
    return {g: _golden_ids(conn, by_name[f"{g}:empty"]) for g in _STATEMENT}


def _regular_single(case: dict[str, Any]) -> bool:
    f = fr.REGISTRY.get(case.get("filter", ""))
    if f is None or f.pg_column is None or f.id in _HOOKED:
        return False
    value = case["input"].get(f.id)
    return value is not None and value != []


def test_the_seeded_cohorts_are_not_trivial(cohort) -> None:
    for grain, ids in cohort.items():
        assert len(ids) >= 20, f"{grain}: only {len(ids)} rows survive the invariants"


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["name"])
def test_old_and_new_select_the_same_rows(conn, cohort, case: dict[str, Any]) -> None:
    old = _golden_ids(conn, case)
    where, params = gold.render(case)
    new = _ids(conn, case["grain"], where, params)
    assert new == old, (
        f"{case['name']}: only old {sorted(old - new)}, only new {sorted(new - old)}"
    )
    if _regular_single(case) and case["name"] not in _EXEMPT_NON_VACUITY:
        base = cohort[case["grain"]]
        assert new and new < base, (
            f"{case['name']} selects {len(new)} of the {len(base)}-row cohort; seed rows "
            "in tests/fixtures/filter_compile_rows.json so it selects a strict subset"
        )


def test_the_city_rule_reads_the_seeded_cities(conn, cohort) -> None:
    """`curated_cities_matching()` must resolve to the shadows, not the empty base."""
    case = next(c for c in _CASES if c.get("filter") == "city_index_rules"
                and c["input"]["city_index_rules"])
    ids = _golden_ids(conn, case)
    assert ids and ids < cohort["watchdog"], f"{case['name']} selected {len(ids)} rows"


def test_the_fixture_annotations_are_the_measures(conn) -> None:
    """The golden's per-m² and plot medians come from these annotations."""
    expected = {r["id"]: (r["price_per_m2"], r["plot_area_m2"]) for r in gold.rows()["listings"]}
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"SELECT l.id, {per_m2_sql('l')}, {plot_area_sql('l')} FROM listings l")
        actual = {
            int(i): (None if p is None else float(p), None if a is None else float(a))
            for i, p, a in cur.fetchall()
        }
    assert actual == expected
