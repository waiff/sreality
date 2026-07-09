"""Offline guard: every SQL string constant has valid `%`-placeholders.

psycopg scans the ENTIRE query string for `%` placeholders on every parameterized
`execute(sql, params)` — comments and string literals included. A bare `%` that
is not a valid placeholder (`%s`, `%b`, `%t`, `%(name)s`, or an escaped `%%`)
raises `ProgrammingError: incomplete placeholder` at runtime. CI's fake DB
connections never run this parser, so the bug ships green — exactly how a prose
`~2%` in a SQL comment took down property maintenance + the dedup engine (PR
#653).

This is the FAST, OFFLINE floor: no database, no imports, so it runs in the
normal `pytest` job and gives instant feedback. Discovery is shared with the
schema-aware sweep via `tests/sql_corpus.py` (this test takes only module-level
constants — the parameterized-query convention — to stay free of false positives
on param-less literal `%`, e.g. an inline `LIKE '%x%'`). The schema-aware
counterpart (`test_sql_schema_prepare.py`) then PREPAREs the wider corpus against
the real schema in CI.
"""

from __future__ import annotations

import re

from tests.sql_corpus import discover


def _invalid_placeholder(sql: str) -> str | None:
    """psycopg's error if `sql` has an invalid `%` placeholder, else None.

    Prefers psycopg's own tokenizer (authoritative — the exact code path that
    raises at execute() time); falls back to a self-contained model of the same
    rule if psycopg restructures its internals, so the guard never goes dark.
    """
    try:
        from psycopg._queries import _split_query  # type: ignore[attr-defined]
    except Exception:
        _split_query = None
    if _split_query is not None:
        try:
            _split_query(sql.encode())
            return None
        except Exception as exc:  # psycopg.ProgrammingError on a bad placeholder
            return str(exc)
    # Fallback: strip every valid placeholder token; any leftover `%` is invalid.
    leftover = re.sub(r"%%|%\([^)]*\)s|%[sbt]", "", sql)
    return "bare '%' is not a valid placeholder" if "%" in leftover else None


def test_sql_constants_have_valid_placeholders():
    constants = discover(include_inline=False, resolve_imports=False)
    # Tripwire: a guard that silently scans nothing is worthless. The repo has
    # dozens of `*_SQL` constants; if this drops to ~0 the scan/exclusions broke.
    assert len(constants) >= 20, (
        f"only {len(constants)} SQL constants discovered — the scan is likely broken"
    )
    offenders = [
        f"  {item.origin} {item.name}: {err}"
        for item in constants
        if (err := _invalid_placeholder(item.sql)) is not None
    ]
    assert not offenders, (
        "SQL string constants with an invalid `%` placeholder (psycopg will raise "
        "`incomplete placeholder` at execute() time — double a literal `%` as `%%` "
        "or keep prose `%` out of the query string):\n" + "\n".join(offenders)
    )
