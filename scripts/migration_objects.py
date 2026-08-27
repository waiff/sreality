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
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_LITERAL = re.compile(r"'(?:[^']|'')*'", re.DOTALL)
_DOLLAR_QUOTED = re.compile(r"\$(\w*)\$.*?\$\1\$", re.DOTALL)

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
    rf"\bcreate\s+(?:unique\s+)?index\s+(?:concurrently\s+)?(?:if\s+not\s+exists\s+)?({_QUALIFIED})\s+on\b",
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


def _strip_noise(sql: str) -> str:
    """Remove what is not executed DDL, keeping what is.

    Dollar-quoted regions are NOT uniformly noise. A CREATE FUNCTION body is:
    its statements run when the function is called, not when the migration is
    applied. A `do $$ ... $$` block is the opposite — its DDL executes right
    there, and this repo uses exactly that shape for lock-race-retrying DDL.
    Migration 438, the outage this whole check exists for, does its
    ALTER TABLE ... ADD CONSTRAINT inside a do-block; a parser that strips both
    alike is blind to the one migration it most needs to see.
    """
    kept: list[str] = []
    pos = 0
    for m in _DOLLAR_QUOTED.finditer(sql):
        kept.append(sql[pos : m.start()])
        kept.append(m.group(0) if _DO_INTRO.search(sql[: m.start()]) else " ")
        pos = m.end()
    kept.append(sql[pos:])
    sql = "".join(kept)
    sql = _BLOCK_COMMENT.sub(" ", sql)
    sql = _LINE_COMMENT.sub(" ", sql)
    return _STRING_LITERAL.sub("' '", sql)


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

    for rx in (_RE_TABLE, _RE_VIEW, _RE_INDEX):
        for m in rx.finditer(body):
            add("relation", _clean(m.group(1)))
    for m in _RE_FUNCTION.finditer(body):
        add("function", _clean(m.group(1)))
    for m in _RE_ADD_COLUMN.finditer(body):
        table = _clean(m.group(1)).split(".")[-1]
        add("column", f"{table}.{_clean(m.group(2))}")
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
