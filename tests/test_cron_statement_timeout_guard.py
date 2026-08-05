"""CI gate: a pg_cron job that calls a function declaring `set statement_timeout`
in its own signature must re-arm that timeout in the CRON COMMAND itself.

Why this exists: PostgreSQL arms statement_timeout exactly once, at the moment a
top-level statement begins, using whatever value is active in the session at that
instant. A `SET`/`SET LOCAL statement_timeout` issued from *inside* a function --
whether in the function body or via its own proconfig `SET` option -- can never
raise the budget for that function's own execution, in any calling context (direct
call, cron, or nested) -- this is an explicit, documented exception to how SET
normally works inside a function. A function's proconfig `SET statement_timeout`
therefore only ever documents intent; it never enforces it.

pg_cron executes a scheduled command via the simple query protocol, which DOES run
a `;`-separated multi-statement command as a sequence of independent top-level
statements sharing one session -- so a `SET statement_timeout='Xs';` issued as its
OWN statement immediately before the function call, in the SAME cron command, is
the one place that actually works.

This bug shipped TWICE independently in this codebase (migration 284 for
refresh_health_matviews on 2026-07-09, migration 277 for rebuild_browse_list /
rebuild_properties_map_mv) before migration 371 fixed all three -- see that
migration's header for the live-verified failure data. This gate makes a third
occurrence structurally impossible, offline, with no database needed -- pg_cron's
`cron` schema does not exist in the CI replay Postgres (see
tests/test_sql_schema_prepare.py's allowlist), so this cannot be a live
introspection query.

Scope limits (a linter, not a formal verifier):
  - Only `cron.schedule(...)` call sites are understood -- the only scheduling call
    this codebase uses (no `cron.alter_job` usage exists as of this writing).
    cron.schedule upserts by job name, so only the LATEST call site per job name
    reflects live behavior; earlier (superseded) call sites for the same name are
    not checked, since migrations are append-only and an old file can never be
    fixed retroactively.
  - Only direct calls (`function_name(`) inside the cron command are detected -- a
    wrapper function that itself calls the timed function is not followed.
"""
from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _strip_comments(sql: str) -> str:
    """Remove `--` and `/* */` comments outside strings/dollar-quotes. Copied from
    test_migration_rls_grants.py's proven implementation -- this repo's migration
    gates are each self-contained (see test_migration_numbers.py for the same
    non-sharing convention), so duplicating a small, stable helper beats a new
    cross-file dependency."""
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
    """Split on `;` outside strings/dollar-quotes, so a function body or DO block
    stays ONE statement. Copied alongside _strip_comments for the same reason."""
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
_BODY_START = re.compile(r"\bas\s*(\$[a-zA-Z_]*\$|')", re.IGNORECASE)
_TIMEOUT_OVERRIDE = re.compile(r"\bset\s+statement_timeout\b", re.IGNORECASE)


def _function_timeout_overrides() -> dict[str, bool]:
    """{function_name: declares `set statement_timeout` in its own header}. The
    last CREATE (OR REPLACE) FUNCTION across all migrations wins -- migrations
    replay in order and this must reflect current LIVE proconfig, not history."""
    overrides: dict[str, bool] = {}
    for path in _migration_files():
        for stmt in _statements(path.read_text(encoding="utf-8")):
            m = _FUNC_START.match(stmt)
            if not m:
                continue
            name = m.group(1)
            body_m = _BODY_START.search(stmt, m.end())
            header = stmt[m.end():body_m.start()] if body_m else stmt[m.end():]
            overrides[name] = bool(_TIMEOUT_OVERRIDE.search(header))
    return overrides


_CRON_SCHEDULE = re.compile(
    r"cron\.schedule\(\s*'([^']+)'\s*,\s*'[^']*'\s*,\s*\$\$(.*?)\$\$\s*\)",
    re.IGNORECASE | re.DOTALL,
)


def _latest_cron_commands() -> dict[str, tuple[str, str]]:
    """{job_name: (command_text, origin_filename)}. cron.schedule upserts by name,
    so the LAST call site across migrations (file order) is what's live."""
    commands: dict[str, tuple[str, str]] = {}
    for path in _migration_files():
        stripped = _strip_comments(path.read_text(encoding="utf-8"))
        for m in _CRON_SCHEDULE.finditer(stripped):
            commands[m.group(1)] = (m.group(2), path.name)
    return commands


_SET_TIMEOUT_STMT = re.compile(r"^set\s+(?:local\s+)?statement_timeout\b", re.IGNORECASE)


def _offenders_for(
    overrides: dict[str, bool], commands: dict[str, tuple[str, str]]
) -> list[str]:
    offenders: list[str] = []
    for job_name, (command, origin) in commands.items():
        seen_set = False
        for stmt in _statements(command):
            if _SET_TIMEOUT_STMT.match(stmt.strip()):
                seen_set = True
                continue
            if seen_set:
                continue
            for func_name, has_override in overrides.items():
                if has_override and re.search(rf"\b{re.escape(func_name)}\s*\(", stmt, re.IGNORECASE):
                    offenders.append(
                        f"  {origin}: job '{job_name}' calls {func_name}() -- which "
                        "declares `set statement_timeout` in its own signature -- "
                        "without a `set statement_timeout` statement earlier in the "
                        "SAME cron command. PostgreSQL arms the timeout once, at the "
                        "start of the top-level statement pg_cron issues; a SET made "
                        "from inside the function never re-arms it, so this job runs "
                        "capped at the database default regardless of the function's "
                        "own declared budget. Fix: prefix the cron command with "
                        "`set statement_timeout='<budget>';` as its own statement."
                    )
    return offenders


def _inert_timeout_offenders() -> list[str]:
    return _offenders_for(_function_timeout_overrides(), _latest_cron_commands())


def test_cron_jobs_reset_a_real_statement_timeout_budget():
    offenders = _inert_timeout_offenders()
    assert not offenders, (
        "pg_cron job(s) call a function whose own `set statement_timeout` can "
        "never take effect for that call (see this file's docstring) -- the "
        "budget must be set in the cron command itself:\n" + "\n".join(offenders)
    )


def test_gate_actually_scans_migrations():
    assert MIGRATIONS_DIR.is_dir(), f"migrations dir not found: {MIGRATIONS_DIR}"
    assert _migration_files(), "no migrations found"


def test_gate_recognizes_the_known_pattern():
    """Sanity-check the parser against a synthetic fixture shaped exactly like the
    real bug (migrations 277/284 before the fix in 371), so a future change to the
    regexes above that silently breaks detection fails loudly here -- the live
    corpus alone can't catch this once migration 371 removes the only real
    examples of the broken pattern."""
    overrides = {"broken_fn": True}

    broken = _offenders_for(
        overrides, {"broken-job": ("select public.broken_fn();", "synthetic.sql")}
    )
    assert broken, "parser failed to flag the known-bad shape -- detection logic is broken"

    fixed = _offenders_for(
        overrides,
        {"broken-job": ("set statement_timeout='600s'; select public.broken_fn();", "synthetic.sql")},
    )
    assert not fixed, "parser false-positived on the correctly-fixed shape"
