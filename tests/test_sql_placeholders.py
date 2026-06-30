"""App-wide guard: every SQL string constant must have valid %-placeholders.

psycopg scans the ENTIRE query string for `%` placeholders on every
parameterized `execute(sql, params)` — comments and string literals included.
A bare `%` that is not part of a valid placeholder (`%s`, `%b`, `%t`,
`%(name)s`, or an escaped `%%`) raises `ProgrammingError: incomplete
placeholder` at runtime. CI's fake DB connections never run this parser, so the
bug ships green — exactly how a prose `~2%` in a SQL comment took down property
maintenance + the dedup engine (PR #653). This test closes that gap for the
whole repo.

It is AST-based on purpose: it never imports the modules (no import-time side
effects, no optional-dep fragility), it auto-discovers constants (zero per-file
upkeep), and it validates with psycopg's own placeholder tokenizer (the exact
runtime check). Constants derived via `.replace()` (e.g. `_RECOMPUTE_ONE_SQL`)
are not separately evaluable statically, but they share the body of the base
`*_SQL` constant they are derived from, so validating the base covers the family.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_EXCLUDED_DIRS = {
    "tests", ".claude", ".git", "node_modules", "__pycache__",
    ".venv", "venv", "build", "dist", "frontend",
}


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


def _iter_sql_constants():
    """Yield (relpath, lineno, name, sql) for every `*_SQL` / `*_QUERY` string."""
    for path in sorted(_ROOT.rglob("*.py")):
        rel = path.relative_to(_ROOT)
        if any(part in _EXCLUDED_DIRS for part in rel.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(rel))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            for target in node.targets:
                name = getattr(target, "id", "")
                if name.endswith("_SQL") or name.endswith("_QUERY"):
                    yield rel, node.lineno, name, value.value


def test_sql_constants_have_valid_placeholders():
    constants = list(_iter_sql_constants())
    # Tripwire: a guard that silently scans nothing is worthless. The repo has
    # dozens of `*_SQL` constants; if this drops to ~0 the scan/exclusions broke.
    assert len(constants) >= 20, (
        f"only {len(constants)} SQL constants discovered — the scan is likely broken"
    )
    offenders = [
        f"  {rel}:{lineno} {name}: {err}"
        for rel, lineno, name, sql in constants
        if (err := _invalid_placeholder(sql)) is not None
    ]
    assert not offenders, (
        "SQL string constants with an invalid `%` placeholder (psycopg will raise "
        "`incomplete placeholder` at execute() time — double a literal `%` as `%%` "
        "or keep prose `%` out of the query string):\n" + "\n".join(offenders)
    )
