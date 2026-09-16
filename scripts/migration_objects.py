"""What a migration file CREATES, extracted from its SQL text.

Feeds the `migration_drift` check: a migration that is merged but never applied
leaves every object it declares absent from the live catalog, and nothing else
in the system notices. Migration 438 merged 2026-08-25 17:12 and was applied
2026-08-26 22:06; for 29 hours every write on six portals failed a CHECK
constraint that the code assumed existed, and `scrape_runs.errors` read 0.

WHY NOT THE LEDGER. `supabase_migrations.schema_migrations` looks like the
obvious oracle and is not one. Its `name` is whatever the applier passed, so it
matches the repo filename sometimes (`444_listings_discovered_at`) and not others
(migration 441 is recorded as `stamp_derived_artifact`, unnumbered), and it also
carries ad-hoc migrations with no repo file at all. Name-matching that produces
false alarms, and an alarm that cries wolf gets muted. The catalog cannot lie:
either the column is there or it is not.

DELIBERATELY INCOMPLETE, AND SAYS SO. This parser handles the DDL shapes this
repo actually uses; anything else yields no objects and the migration is reported
`unverifiable` rather than passing silently. That count is surfaced by the check,
because a guard whose blind spot is invisible is worse than no guard — the same
failure this sprint already hit once, with a live test that was wired up but never
executed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Comments and string literals are stripped BEFORE matching. Every migration in
# this repo opens with a long prose header, and that prose routinely quotes the
# very DDL the file runs ("ADD COLUMN IF NOT EXISTS discovered_at ..."), so a
# parser that reads raw text reports objects the file never creates.
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_]\w*)?\$")

_IDENT = r'(?:"[^"]+"|[a-zA-Z_][a-zA-Z0-9_$]*)'
_QUALIFIED = rf"(?:{_IDENT}\s*\.\s*)?{_IDENT}"

_RE_TABLE = re.compile(
    rf"\bcreate\s+(?:unlogged\s+|temp\s+|temporary\s+)?table\s+(?:if\s+not\s+exists\s+)?({_QUALIFIED})",
    re.IGNORECASE,
)
_RE_VIEW = re.compile(
    rf"\bcreate\s+(?:or\s+replace\s+)?(?:materialized\s+)?view\s+(?:if\s+not\s+exists\s+)?({_QUALIFIED})",
    re.IGNORECASE,
)
_RE_INDEX = re.compile(
    rf"\bcreate\s+(?:unique\s+)?index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?({_QUALIFIED})\s+on\s+(?:only\s+)?({_QUALIFIED})",
    re.IGNORECASE,
)
_RE_FUNCTION = re.compile(
    rf"\bcreate\s+(?:or\s+replace\s+)?function\s+({_QUALIFIED})\s*\(",
    re.IGNORECASE,
)
_RE_ADD_CONSTRAINT = re.compile(
    rf"\balter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?({_QUALIFIED})\s+add\s+constraint\s+({_IDENT})",
    re.IGNORECASE,
)
_RE_POLICY = re.compile(
    rf"\bcreate\s+policy\s+({_IDENT})\s+on\s+({_QUALIFIED})",
    re.IGNORECASE,
)
_RE_ADD_COLUMN = re.compile(
    rf"\balter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?({_QUALIFIED})\s+add\s+column\s+(?:if\s+not\s+exists\s+)?({_IDENT})",
    re.IGNORECASE,
)

# 'relation' covers tables, views, matviews and indexes — to_regclass resolves
# all four, so they need no separate probe. 'constraint' earns its place because
# migration 438 — the outage this check exists for — creates no object at all: it
# swaps a CHECK constraint, and a parser that only looks for CREATE would have
# been blind to precisely the incident that motivated it.
Kind = str  # "relation" | "function" | "column" | "constraint" | "policy"


@dataclass(frozen=True)
class MigrationObject:
    kind: Kind
    ident: str  # relation/function: [schema.]name  |  column: table.column

    def __str__(self) -> str:
        return f"{self.kind}:{self.ident}"


_DO_INTRO = re.compile(r"\bdo\s*(?:language\s+[a-zA-Z_]+\s*)?$", re.IGNORECASE)


def _strip_noise(sql: str, *, inside_apply_time: bool = False) -> str:
    """Remove what is not executed DDL, keeping what is.

    ONE PASS, LEFT TO RIGHT, because these three lexical forms nest inside each
    other and any fixed order of separate regex passes gets one of them wrong.
    Stripping comments first deletes the closing quote of a literal that itself
    contains `--` (`raise exception 'lost -- re-run'`, three times in the W3
    migrations) and the rest of the file dissolves into one giant string;
    stripping literals first opens a bogus one on the apostrophe in a prose
    comment, which every header in this repo has. The scanner reads whichever
    form OPENS first and skips to its own terminator.

    Dollar-quoted regions are NOT uniformly noise. A CREATE FUNCTION body is:
    its statements run when the function is called, not when the migration is
    applied. A `do $$ ... $$` block is the opposite — its DDL executes right
    there, and this repo uses exactly that shape for lock-race-retrying DDL.
    Migration 438, the outage this whole check exists for, does its
    ALTER TABLE ... ADD CONSTRAINT inside a do-block; a parser that strips both
    alike is blind to the one migration it most needs to see. A kept block is
    re-scanned (so prose inside it declares nothing) and everything nested in it
    is kept with it: migrations 480 and 489 run their CREATE TABLE through
    `execute $sql$ ... $sql$` inside the do-block, which is DDL that executes at
    apply time exactly like the block around it.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            else:
                j = n
            out.append("' '")
            i = j
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            out.append(" ")
            i = j
        else:
            m = _DOLLAR_TAG.match(sql, i)
            if not m:
                out.append(ch)
                i += 1
                continue
            tag = m.group(0)
            close = sql.find(tag, m.end())
            end = n if close < 0 else close + len(tag)
            inner = sql[m.end() : close] if close >= 0 else sql[m.end() :]
            keep = inside_apply_time or bool(_DO_INTRO.search("".join(out)))
            out.append(_strip_noise(inner, inside_apply_time=True) if keep else " ")
            i = end
    return "".join(out)


def _clean(ident: str) -> str:
    return re.sub(r"\s*\.\s*", ".", ident.strip()).replace('"', "")


def parse_objects(sql: str) -> list[MigrationObject]:
    """Every object this SQL creates, in file order, de-duplicated.

    A dollar-quoted function body is stripped whole: it is plpgsql, not DDL the
    migration itself runs, and it frequently contains CREATE TEMP TABLE and
    similar that exists only for the duration of a call.
    """
    body = _strip_noise(sql)
    found: list[MigrationObject] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: Kind, ident: str) -> None:
        key = (kind, ident.lower())
        if key not in seen:
            seen.add(key)
            found.append(MigrationObject(kind, ident))

    for rx in (_RE_TABLE, _RE_VIEW):
        for m in rx.finditer(body):
            add("relation", _clean(m.group(1)))
    # An index always lives in its table's schema and CREATE INDEX cannot qualify
    # the index name, so a bare name on a schema-qualified table is probed as
    # `<schema>.<index>` — otherwise every index outside `public` reads ABSENT.
    for m in _RE_INDEX.finditer(body):
        name = _clean(m.group(1))
        target = _clean(m.group(2))
        schema = target.rsplit(".", 1)[0] if "." in target else "public"
        if "." not in name and schema != "public":
            name = f"{schema}.{name}"
        add("relation", name)
    for m in _RE_FUNCTION.finditer(body):
        add("function", _clean(m.group(1)))
    for m in _RE_ADD_COLUMN.finditer(body):
        qualified = _clean(m.group(1))
        schema, _, table = qualified.rpartition(".")
        # information_schema.columns is probed per schema, so a column on a table
        # outside `public` keeps its schema: `<schema>.<table>.<column>`.
        prefix = f"{schema}.{table}" if schema and schema != "public" else table
        add("column", f"{prefix}.{_clean(m.group(2))}")
    for m in _RE_ADD_CONSTRAINT.finditer(body):
        table = _clean(m.group(1)).split(".")[-1]
        add("constraint", f"{table}.{_clean(m.group(2))}")
    for m in _RE_POLICY.finditer(body):
        table = _clean(m.group(2)).split(".")[-1]
        add("policy", f"{table}.{_clean(m.group(1))}")
    return found


_MIGRATION_NAME = re.compile(r"^(\d+)_(.+)\.sql$")


@dataclass(frozen=True)
class Migration:
    number: int
    filename: str
    objects: list[MigrationObject]


def load_migrations(migrations_dir: Path, newest: int = 25) -> list[Migration]:
    """The `newest` numbered migrations, parsed. Files under `reverts/` and any
    non-numbered file are ignored — a revert is expected NOT to be present."""
    out: list[Migration] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        m = _MIGRATION_NAME.match(path.name)
        if not m:
            continue
        out.append(
            Migration(int(m.group(1)), path.name, parse_objects(path.read_text(encoding="utf-8")))
        )
    out.sort(key=lambda mig: mig.number)
    return out[-newest:] if newest > 0 else out
