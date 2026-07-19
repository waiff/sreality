"""Schema-aware SQL guard: every executed SQL statement must PREPARE against the
real prod schema.

This is the layer the fake DB connections structurally cannot be: it loads the
actual schema (CI replays every migration from zero into a throwaway Postgres +
PostGIS + pgvector — see .github/workflows/migrations.yml) and asks Postgres to
`PREPARE` each discovered statement. `PREPARE` fully parses, name-resolves, and
type-checks the query against the live catalog **without running it or touching a
row**, so it catches the whole class the fakes miss and that shipped to prod:

  * a missing / renamed column or table   -> 42703 / 42P01
  * an illegal construct, e.g.             -> 0A000
    `count(DISTINCT x) OVER (...)`            (this exact bug ran green on every
                                              build and broke the notifications
                                              producer in prod)
  * a syntax error                         -> 42601
  * a bad function signature               -> 42883
  * a placeholder-arity mismatch           -> (also caught offline by
                                              test_sql_placeholders.py)

Gated on TEST_DATABASE_URL: with no database configured (normal local `pytest`)
the whole module skips, so the offline suite stays fast and hermetic. CI's
schema-replay job sets it and runs this against the freshly-rebuilt schema.

The fake-conn unit tests stay — they assert control flow. This complements them
with the one thing they can't answer: does this SQL actually compile against our
schema?
"""

from __future__ import annotations

import os
import re

import pytest

from tests.sql_corpus import discover, first_keyword, to_prepare_form

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — schema-aware SQL sweep runs only in the CI DB job",
)

# Statement types PREPARE accepts. Everything else (DDL, SET, REFRESH, TRUNCATE,
# COPY, and the GraphQL *_QUERY constants whose first word is `query`) is reported
# as skipped, not failed — PREPARE only plans a single DML/SELECT statement.
_PREPARABLE = {"SELECT", "INSERT", "UPDATE", "DELETE", "WITH", "VALUES", "TABLE"}

# Statements that legitimately cannot PREPARE in isolation because they depend on
# session-local state, or are not Postgres SQL at all. Keyed by an
# (origin-substring, sql-substring) pair with a documented reason. Keep tiny.
_ALLOWLIST: list[tuple[str, str, str]] = [
    (
        "load_obec_population",
        "_obec_pop",
        "references a session-local TEMP TABLE created earlier in the same script",
    ),
    (
        "backfill_listing_surrogate_id.py",
        "listing_id_backfill_map",
        "a runtime-only mapping table the script CREATEs at execution time to "
        "freeze the chronological id assignment (migration 312 backfill); it is "
        "not in migrations, so the replayed schema can't see it",
    ),
    (
        "fetch_population_wikidata",
        "wdt:",
        "a SPARQL query to the Wikidata endpoint, not Postgres SQL",
    ),
    (
        "verify_pipeline.py",
        "cron.job_run_details",
        "pg_cron's run-history lives in the extension-managed `cron` schema, not "
        "in migrations, so the replayed schema can't see it; check_db_saturation "
        "reads it defensively and warns if unreadable",
    ),
]


def _is_param_type_artifact(exc) -> bool:
    """A PREPARE error caused by this sweep's type-less binding, not a real defect.

    The sweep PREPAREs without param VALUES, so Postgres cannot always infer a
    parameter's type (e.g. a param used as both `IS NULL` and `= ANY(...)`); at
    execute() time psycopg sends the type from the Python value and the query
    works. Postgres surfaces this as an indeterminate/ambiguous parameter, or as
    an ambiguous operator/function whose operand is the `unknown` pseudo-type of
    an unbound param. All are inconclusive, never failures. A genuine missing
    column / bad function is a different code (42703 / 42883) and still fails.
    """
    state = (exc.sqlstate or "").upper()
    msg = str(exc).lower()
    return (
        state in {"42P18", "42P08"}
        or "could not determine data type of parameter" in msg
        or ("is not unique" in msg and "unknown" in msg)
    )


def _allowlisted(item) -> str | None:
    for origin_sub, sql_sub, reason in _ALLOWLIST:
        if origin_sub in item.origin and sql_sub in item.sql:
            return reason
    return None


# An UNQUOTED `.format()` slot — `{name}` not preceded by a quote. Some constants
# (e.g. dedup_engine `_ELIGIBLE_SQL` -> `{filter}`, portal_lookup -> `{values}`)
# hold a template filled per-call; resolve-imports surfaces the raw template,
# which is not runnable SQL. A QUOTED `'{...}'` (a jsonb path / array literal) is
# deliberately NOT matched — it is valid SQL and PREPAREs fine.
_FORMAT_SLOT = re.compile(r"(?<!')\{[A-Za-z_]\w*\}")


def _is_format_template(sql: str) -> bool:
    """True if `sql` carries an unquoted `str.format()` slot (not runnable as-is).

    Skipped rather than false-failed: the concrete `.format()` result is assembled
    at the call site and is part of the documented dynamic-SQL residue the static
    sweep cannot reach.
    """
    return bool(_FORMAT_SLOT.search(sql))


@pytest.fixture(scope="module")
def _conn():
    import psycopg

    conn = psycopg.connect(_DB_URL, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def test_every_sql_statement_prepares_against_the_schema(_conn):
    import psycopg

    corpus = discover(include_inline=True, resolve_imports=True)
    assert len(corpus) >= 200, (
        f"only {len(corpus)} SQL statements discovered — the corpus scan is broken"
    )

    failures: list[str] = []
    indeterminate: list[str] = []
    skipped_non_prepare = 0
    skipped_template = 0
    skipped_allowlist = 0
    prepared = 0

    for i, item in enumerate(corpus):
        if _is_format_template(item.sql):
            skipped_template += 1
            continue
        if first_keyword(item.sql) not in _PREPARABLE:
            skipped_non_prepare += 1
            continue
        reason = _allowlisted(item)
        if reason:
            skipped_allowlist += 1
            continue

        name = f"_sqlcheck_{i}"
        stmt = to_prepare_form(item.sql)
        try:
            with _conn.cursor() as cur:
                cur.execute(f"PREPARE {name} AS {stmt}")  # parse + plan, never run
                cur.execute(f"DEALLOCATE {name}")
            prepared += 1
        except psycopg.Error as exc:
            state = exc.sqlstate or "?????"
            snippet = " ".join(item.sql.split())[:120]
            line = f"  [{state}] {item.origin}\n      {snippet}\n      -> {str(exc).strip().splitlines()[0]}"
            if _is_param_type_artifact(exc):
                indeterminate.append(line)
            else:
                failures.append(line)

    summary = (
        f"SQL schema sweep: {prepared} PREPAREd OK, {len(failures)} failed, "
        f"{len(indeterminate)} indeterminate-param, {skipped_non_prepare} non-PREPARE-able, "
        f"{skipped_template} format-template, {skipped_allowlist} allowlisted "
        f"(of {len(corpus)} discovered)."
    )
    print("\n" + summary)
    if indeterminate:
        print("Indeterminate parameter type (not failures — see _INDETERMINATE_PARAM):")
        print("\n".join(indeterminate))

    assert not failures, (
        "SQL statements that do not compile against the real schema — Postgres would "
        "raise these at execute() time; the fake-conn tests can't see them:\n"
        + "\n".join(failures)
        + f"\n\n{summary}"
    )
