"""Shared discovery of the repo's executed-SQL corpus for the SQL guards.

ONE place that knows how to find every SQL statement the runtime code executes,
so the offline placeholder guard (`test_sql_placeholders.py`) and the schema-aware
PREPARE sweep (`test_sql_schema_prepare.py`) validate the SAME corpus instead of
each re-implementing discovery.

Two layers:
  * AST (import-free, always safe): module-level `*_SQL`/`*_QUERY` string
    constants, plus string literals passed straight to `.execute()`/
    `.executemany()`. No import side effects, no optional-dep fragility.
  * import-resolve (opt-in): imports the handful of modules that build a
    `*_SQL`/`*_QUERY` constant by `.replace()`/f-string/concatenation — which AST
    cannot evaluate statically (e.g. `recompute_property_stats._RECOMPUTE_ONE_SQL`)
    — and reads its RESOLVED value. The test suite already imports these modules,
    so this is safe under CI; a module that fails to import is reported by the
    caller, never silently dropped.

Also exposes two helpers the schema sweep needs: `first_keyword` (statement type,
comment-skipping) and `to_prepare_form` (rewrite psycopg `%s`/`%(name)s`
placeholders to Postgres `$1..$n` so a statement can be PREPAREd without binding
values; a param-less literal `%` — e.g. `LIKE '%x%'` — is left untouched).
"""

from __future__ import annotations

import ast
import importlib
import re
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# Every executed-SQL constant and execute() site lives under these (verified);
# migrations/ is *.sql applied by CI, tests/ are fakes, frontend/ is TS.
RUNTIME_DIRS = ("scraper", "toolkit", "api", "scripts")


@dataclass(frozen=True)
class SqlItem:
    origin: str          # "scraper/db.py:341"  or  "scraper/db.py::_BATCH_UPSERT_SQL"
    name: str | None     # constant name, or None for an inline literal
    sql: str
    kind: str            # "const" | "inline" | "resolved"


def _norm(sql: str) -> str:
    return " ".join(sql.split())


def _source_files():
    for d in RUNTIME_DIRS:
        for path in sorted((_ROOT / d).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def _is_sql_const_name(name: str) -> bool:
    return name.endswith("_SQL") or name.endswith("_QUERY")


def discover(*, include_inline: bool = True, resolve_imports: bool = False) -> list[SqlItem]:
    """Return the deduped SQL corpus (by normalized text)."""
    items: list[SqlItem] = []
    seen: set[str] = set()
    composed_modules: set[Path] = set()

    def add(item: SqlItem) -> None:
        key = _norm(item.sql)
        if key and key not in seen:
            seen.add(key)
            items.append(item)

    for path in _source_files():
        rel = path.relative_to(_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), rel)
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                names = [getattr(t, "id", "") for t in node.targets]
                if any(_is_sql_const_name(n) for n in names):
                    v = node.value
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        nm = next(n for n in names if _is_sql_const_name(n))
                        add(SqlItem(f"{rel}:{node.lineno}", nm, v.value, "const"))
                    else:
                        composed_modules.add(path)  # non-literal -> needs import-resolve
            if include_inline and isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr in ("execute", "executemany")
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    add(SqlItem(f"{rel}:{node.lineno}", None, node.args[0].value, "inline"))

    if resolve_imports:
        for path in sorted(composed_modules):
            rel = path.relative_to(_ROOT).as_posix()
            mod_name = rel[:-3].replace("/", ".")
            try:
                mod = importlib.import_module(mod_name)
            except Exception:  # noqa: BLE001 — caller reports import failures
                continue
            for attr in dir(mod):
                if _is_sql_const_name(attr) and isinstance(getattr(mod, attr, None), str):
                    add(SqlItem(f"{rel}::{attr}", attr, getattr(mod, attr), "resolved"))

    return items


_LEADING_NOISE = re.compile(r"\s*(--[^\n]*\n|/\*.*?\*/|\s)+", re.DOTALL)


def first_keyword(sql: str) -> str:
    """First SQL word, upper-cased, skipping leading whitespace + comments."""
    m = _LEADING_NOISE.match(sql)
    rest = sql[m.end():] if m else sql
    tok = rest.split(None, 1)[0] if rest.split(None, 1) else ""
    return re.sub(r"[^A-Za-z].*$", "", tok).upper()


def to_prepare_form(sql: str) -> str:
    """Rewrite psycopg placeholders (`%s` / `%(name)s`) to Postgres `$1..$n`.

    Returns the statement unchanged when it carries no valid psycopg placeholders
    — a param-less literal `%` (e.g. `LIKE '%x%'`, run without params) is a real
    literal to Postgres and must pass through so it PREPAREs as-is.
    """
    try:
        from psycopg._queries import _split_query  # type: ignore[attr-defined]
        parts = _split_query(sql.encode())
    except Exception:  # noqa: BLE001 — no/invalid placeholders: PREPARE the raw text
        return sql
    out: list[str] = []
    named: dict[str, int] = {}
    counter = 0
    # N placeholders -> N+1 parts; the final part is the tail (no placeholder).
    for i, part in enumerate(parts):
        out.append(part.pre.decode())
        if i < len(parts) - 1:
            item = part.item
            if isinstance(item, str):  # %(name)s — dedupe repeated names
                if item not in named:
                    counter += 1
                    named[item] = counter
                out.append(f"${named[item]}")
            else:  # %s — positional
                counter += 1
                out.append(f"${counter}")
    return "".join(out)
