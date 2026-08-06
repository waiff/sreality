"""CI gate: `browse_list` / `properties_map_mv` must never be re-grantable to
`anon` by any function's dynamic DDL, ever again.

Why this exists as its OWN file, not a rule bolted onto
tests/test_migration_rls_grants.py: that file's `_offending_write_grants`
scanner deliberately treats every dollar-quoted function body as ONE opaque
statement (see its `test_dynamic_ddl_is_annotated` — dynamic DDL built via
`EXECUTE 'grant ...'` inside a `$fn$ ... $fn$` body is an acknowledged,
annotated blind spot there, not a bug). It also only tracks WRITE privileges
(INSERT/UPDATE/DELETE/TRUNCATE/ALL); SELECT was never in scope because most
`_public` views are SUPPOSED to grant anon SELECT.

`browse_list` and `properties_map_mv` are different: they are blue-green
DROP+CREATE'd by `rebuild_browse_list()` / `rebuild_properties_map_mv()`
every 5 / 30 minutes via pg_cron (migrations 276/277), and migration 299
(Phase 0) deliberately narrowed their grant to `authenticated` only. Migration
371 silently reverted that (re-added `grant select on browse_list to anon,
authenticated` and the properties_map_mv equivalent, INSIDE an `EXECUTE`
string) while its own commit message claimed "no behavior change" -- live
verified 2026-08-05/06: anon held SELECT on both, unauthenticated-readable
via the public anon key embedded in the frontend bundle, for an unknown
period since 371 shipped. Migration 331's post-condition assertion (`assert
not has_table_privilege('anon', ...)`) is a ONE-SHOT check that runs once, at
migration-apply time -- structurally unable to catch a SCHEDULED FUNCTION
regressing on some LATER, unrelated edit, since nothing ever re-runs it.

This gate reaches INTO the dollar-quoted body (unlike
test_migration_rls_grants.py's scanner) specifically for these two
protected relations, and -- like test_cron_statement_timeout_guard.py --
tracks only the LATEST `create or replace function` per name across all
migrations (migrations replay in order; CREATE OR REPLACE is cumulative, so
only the last definition reflects live behavior). Complements, not
replaces, migration 376's runtime self-check (an
`if has_table_privilege('anon', ...) then raise exception` inside both
functions, so a regression is also caught on the very next pg_cron tick even
if it somehow bypassed this offline gate, e.g. a grant issued directly
against prod outside any migration file).
"""
from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")

# The two relations blue-green rebuilt by a scheduled SECURITY DEFINER
# function and deliberately narrowed to `authenticated` by migration 299.
_PROTECTED_RELATIONS = ("browse_list", "properties_map_mv")


def _strip_comments(sql: str) -> str:
    """Remove -- and /* */ comments outside strings/dollar-quotes. Copied
    from test_cron_statement_timeout_guard.py's proven implementation --
    this repo's migration gates are each self-contained, so duplicating a
    small, stable helper beats a new cross-file dependency."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        two = sql[i:i + 2]
        if two == "--":
            j = sql.find("\n", i)
            out.append(" ")
            i = n if j == -1 else j
            continue
        if two == "/*":
            depth, i = 1, i + 2
            while i < n and depth:
                t = sql[i:i + 2]
                if t == "/*":
                    depth, i = depth + 1, i + 2
                elif t == "*/":
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            out.append(" ")
            continue
        if sql[i] == "'":
            out.append("'")
            i += 1
            while i < n:
                if sql[i:i + 2] == "''":
                    out.append("''")
                    i += 2
                    continue
                out.append(sql[i])
                if sql[i] == "'":
                    i += 1
                    break
                i += 1
            continue
        if sql[i] == "$":
            m = _DOLLAR.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, m.end())
                stop = (end + len(tag)) if end != -1 else n
                out.append(sql[i:stop])
                i = stop
                continue
        out.append(sql[i])
        i += 1
    return "".join(out)


def _statements(sql: str) -> list[str]:
    """Split on `;` outside strings/dollar-quotes, so a function body or DO
    block stays ONE statement. Copied alongside _strip_comments for the same
    reason (see that helper's docstring)."""
    s = _strip_comments(sql)
    stmts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "'":
            buf.append(ch)
            i += 1
            while i < n:
                if s[i:i + 2] == "''":
                    buf.append("''")
                    i += 2
                    continue
                buf.append(s[i])
                if s[i] == "'":
                    i += 1
                    break
                i += 1
            continue
        if ch == "$":
            m = _DOLLAR.match(s, i)
            if m:
                tag = m.group(0)
                end = s.find(tag, m.end())
                stop = (end + len(tag)) if end != -1 else n
                buf.append(s[i:stop])
                i = stop
                continue
        if ch == ";":
            frag = "".join(buf).strip()
            if frag:
                stmts.append(frag)
            buf, i = [], i + 1
            continue
        buf.append(ch)
        i += 1
    frag = "".join(buf).strip()
    if frag:
        stmts.append(frag)
    return stmts


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


_FUNC_START = re.compile(
    r"create\s+(?:or\s+replace\s+)?function\s+(?:public\.)?\"?([a-z0-9_]+)\"?\s*\(",
    re.IGNORECASE,
)


def _latest_function_bodies() -> dict[str, tuple[str, str]]:
    """{function_name: (full_statement_text, origin_filename)}. CREATE OR
    REPLACE is cumulative and migrations replay in order, so the LAST
    definition per name across all migrations is what's actually live --
    mirrors test_cron_statement_timeout_guard.py's _latest_cron_commands."""
    bodies: dict[str, tuple[str, str]] = {}
    for path in _migration_files():
        for stmt in _statements(path.read_text(encoding="utf-8")):
            m = _FUNC_START.match(stmt)
            if not m:
                continue
            bodies[m.group(1)] = (stmt, path.name)
    return bodies


# Matches `grant <privs> on <relation> to <roles...>` INSIDE a single-quoted
# EXECUTE string (so it can be embedded in a dollar-quoted plpgsql body) --
# roles run up to the closing quote or a semicolon, whichever comes first.
_GRANT_IN_STRING = re.compile(
    r"grant\s+([a-z, ]+?)\s+on\s+([a-z0-9_]+)\s+to\s+([a-z0-9_, ]+?)\s*['\";]",
    re.IGNORECASE,
)


def _protected_relation_anon_grants(body: str) -> list[str]:
    """Every `grant ... on <protected relation> to <roles incl. anon>`
    fragment found anywhere in the (comment-stripped) body text -- including
    inside a dollar-quoted EXECUTE string, which the offline scanner in
    test_migration_rls_grants.py cannot see into by design."""
    offenders: list[str] = []
    stripped = _strip_comments(body)
    for m in _GRANT_IN_STRING.finditer(stripped):
        privs, relation, roles = m.group(1), m.group(2).lower(), m.group(3)
        if relation not in _PROTECTED_RELATIONS:
            continue
        role_tokens = {r.strip() for r in roles.split(",") if r.strip()}
        if "anon" in role_tokens:
            offenders.append(
                f"grant {privs.strip()} on {relation} to {roles.strip()}"
            )
    return offenders


def test_browse_list_and_map_mv_never_regrant_anon():
    offenders = [
        f"  {origin} ({func_name}): {grant}"
        for func_name, (body, origin) in _latest_function_bodies().items()
        for grant in _protected_relation_anon_grants(body)
    ]
    assert not offenders, (
        "The LATEST definition of some function grants anon SELECT on "
        "browse_list or properties_map_mv -- migration 299 (Phase 0) "
        "deliberately narrowed both to `authenticated` only, and migration "
        "371 already shipped exactly this regression once (silently, "
        "re-asserted every 5 minutes by pg_cron, because the DDL is built "
        "through EXECUTE and this class of gate did not exist yet). Both "
        "objects are anon-key-readable the moment this grant lands -- the "
        "React Router login gate is not a security boundary, the anon key is "
        "unavoidably public in the shipped JS bundle. Remove `anon` from the "
        "grant's role list:\n" + "\n".join(offenders)
    )


def test_gate_actually_scans_migrations():
    assert MIGRATIONS_DIR.is_dir(), f"migrations dir not found: {MIGRATIONS_DIR}"
    assert _migration_files(), "no migrations found"
    assert "rebuild_browse_list" in _latest_function_bodies(), (
        "rebuild_browse_list() was not found by the scanner -- parser likely broken"
    )


def test_gate_recognizes_the_known_pattern():
    """Sanity-check the parser against a synthetic fixture shaped exactly
    like the real bug (migration 371's regression), so a future change to
    the regexes above that silently breaks detection fails loudly here --
    the live corpus alone can't catch this once the bug is fixed and no real
    example remains on disk."""
    broken_body = (
        "create or replace function rebuild_browse_list() returns void "
        "language plpgsql as $fn$ begin "
        "execute 'grant select on browse_list to anon, authenticated'; "
        "end $fn$;"
    )
    offenders = _protected_relation_anon_grants(broken_body)
    assert offenders, "parser failed to flag the known-bad shape -- detection logic is broken"

    fixed_body = (
        "create or replace function rebuild_browse_list() returns void "
        "language plpgsql as $fn$ begin "
        "execute 'grant select on browse_list to authenticated'; "
        "end $fn$;"
    )
    assert not _protected_relation_anon_grants(fixed_body), (
        "parser false-positived on the correctly-fixed shape"
    )

    unrelated_body = (
        "create or replace function some_public_view_helper() returns void "
        "language plpgsql as $fn$ begin "
        "execute 'grant select on curated_cities_public to anon, authenticated'; "
        "end $fn$;"
    )
    assert not _protected_relation_anon_grants(unrelated_body), (
        "parser false-positived on a non-protected relation's anon grant "
        "(most _public views are SUPPOSED to be anon-readable)"
    )
