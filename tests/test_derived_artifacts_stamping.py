"""The registry declares nothing it cannot observe (migration 441).

Migration 440 finished the DECLARATION half of Corollary E — 14 rows, each naming a
producer, a cadence and a staleness budget. It did not finish the OBSERVATION half: only
three producers stamped, so 11 of the 14 rows carried `last_succeeded_at IS NULL` and a
Health panel built on them would have rendered eleven permanent "never" rows, which is
worse than no panel because it teaches the operator to stop looking.

Migration 441 adds one helper, `public.stamp_derived_artifact(name, rows, duration_ms)`,
and calls it from the five producers that were silent. The helper's UPDATE is deliberately
allowed to match nothing — a producer must never fail because a metadata row is missing —
and that choice buys exactly one hole: a MISTYPED name stamps nothing forever and reads on
the panel as a dead artifact, i.e. indistinguishable from the condition the registry
exists to detect. These rails are what close it, offline, before the typo ships.

Every test below states the specific mutation that makes it RED.

Lane: migrations, with `DB_RAILS_REQUIRED=1` so a lane that loses its `TEST_DATABASE_URL`
goes RED instead of reporting a green skip (the `tests/test_location_drain_index_plan.py`
idiom). The `conn` fixture is `autocommit=True`, so any GUC these tests ever need must be
a plain `SET` — `SET LOCAL` outside a transaction is a silent no-op.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_REQUIRED = os.environ.get("DB_RAILS_REQUIRED") == "1"

pytestmark = pytest.mark.skipif(
    not _DB_URL and not _REQUIRED,
    reason="TEST_DATABASE_URL not set — this rail runs in CI's migrations lane",
)

_REPO = Path(__file__).resolve().parent.parent

# The production trees only. `tests/` is deliberately excluded: this file itself spells
# `stamp_derived_artifact(` inside regexes and prose, and a scanner that read its own
# source would flag phantom call sites.
_SOURCE_TREES = ("api", "scraper", "toolkit", "scripts", "location_data", "migrations")

# Call sites whose artifact name is NOT a literal, so no static scan can check it. Each
# needs a reason and a compensating check, because the whole value of these rails is that
# every name is verified somewhere. Keep this at zero entries if you possibly can.
_DYNAMIC_STAMP_SITES = {
    "scripts/refresh_image_stats.py": (
        "loops over the module-level _MVS tuple and stamps `mv`; every member of that "
        "tuple is checked by test_the_image_stat_matview_tuple_is_registered below"
    ),
}

# Python producers all pass the connection first, so the artifact name is argument two.
# `def stamp_derived_artifact(conn: psycopg.Connection, name: str, ...)` does not match —
# the annotation's `:` sits where this pattern requires a `,` — so the definition in
# scraper/db.py is correctly not read as a call site.
_PY_CALL = re.compile(r"stamp_derived_artifact\s*\(\s*conn\s*,\s*(?P<arg>[^,)]+)")

# SQL call sites, in migration text and in `pg_proc.prosrc` alike. Anchored on `perform`
# or `select` so that the CREATE FUNCTION signature, the COMMENT ON and the REVOKE in
# migration 441 — which all spell `stamp_derived_artifact(text, bigint, integer)` — are
# not mistaken for calls.
_SQL_CALL = re.compile(
    r"(?:perform|select)\s+(?:public\.)?stamp_derived_artifact\s*\(\s*(?P<arg>'[^']*'|[^,)]+)",
    re.IGNORECASE,
)

# The pre-441 stamp shape, still used by the three producers that carry their own inline
# UPDATE (both blue-green rebuilds, plus refresh_llm_cost_rollups, which cannot use the
# helper because it must pass its own watermark as complete_through). Scoped to the
# UPDATE's own statement so an unrelated `where name = '...'` elsewhere in the same
# function body cannot be mistaken for a stamp and mask a missing one.
_INLINE_STAMP = re.compile(
    r"update\s+(?:public\.)?derived_artifacts\b[^;]*?where\s+name\s*=\s*'(?P<name>[a-z0-9_]+)'",
    re.IGNORECASE | re.DOTALL,
)

_REFRESH_OR_STAMP = re.compile(
    r"refresh\s+materialized\s+view\s+concurrently\s+(?P<refresh>[a-z0-9_]+)"
    r"|stamp_derived_artifact\s*\(\s*'(?P<stamp>[a-z0-9_]+)'",
    re.IGNORECASE,
)


@pytest.fixture(scope="module")
def conn():
    if not _DB_URL:
        pytest.fail(
            "DB_RAILS_REQUIRED=1 but TEST_DATABASE_URL is not set — the migrations lane "
            "is misconfigured and this rail would otherwise have skipped green."
        )
    import psycopg

    with psycopg.connect(_DB_URL, autocommit=True) as c:
        yield c


@pytest.fixture(scope="module")
def registry(conn) -> dict[str, str]:
    """{artifact name: declared producer}."""
    with conn.cursor() as cur:
        cur.execute("select name, producer from public.derived_artifacts")
        return {r[0]: r[1] for r in cur.fetchall()}


@pytest.fixture(scope="module")
def function_bodies(conn) -> dict[str, str]:
    """{proname: prosrc} for every public function that touches the registry."""
    with conn.cursor() as cur:
        cur.execute(
            # No bound parameters, so psycopg does no placeholder substitution and a
            # single `%` is literal here — doubling it would search for `%%`.
            "select p.proname, p.prosrc from pg_proc p "
            "  join pg_namespace n on n.oid = p.pronamespace "
            " where n.nspname = 'public' and p.prosrc like '%derived_artifact%'"
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def _source_files() -> list[Path]:
    out: list[Path] = []
    for tree in _SOURCE_TREES:
        root = _REPO / tree
        if not root.is_dir():
            continue
        out += sorted(root.rglob("*.py")) + sorted(root.rglob("*.sql"))
    return out


def _drop_sql_comment_lines(sql: str) -> str:
    """Blank out whole-line `--` comments.

    Migration headers in this repo are long prose that quotes the very call shapes this
    file scans for — 441's own header contains the sentence "six `perform
    public.stamp_derived_artifact(...)` calls" — and a scanner that reads them reports a
    phantom call site. Line-level only, deliberately: a `--` at the end of a code line is
    left alone (conservative), and no code in this repo puts a stamp call on a commented
    line. Python is not stripped; `#` cannot produce a match for `_PY_CALL`'s shape.
    """
    return "\n".join(
        "" if line.lstrip().startswith("--") else line for line in sql.splitlines()
    )


def _repo_call_sites() -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    """({relpath: literal names}, {relpath: non-literal argument texts})."""
    literal: dict[str, set[str]] = {}
    dynamic: dict[str, list[str]] = {}
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        if "stamp_derived_artifact" not in text:
            continue
        rel = path.relative_to(_REPO).as_posix()
        if path.suffix == ".sql":
            text = _drop_sql_comment_lines(text)
        pattern = _SQL_CALL if path.suffix == ".sql" else _PY_CALL
        for m in pattern.finditer(text):
            arg = m.group("arg").strip()
            if arg[:1] in {"'", '"'}:
                literal.setdefault(rel, set()).add(arg.strip("'\""))
            else:
                dynamic.setdefault(rel, []).append(arg)
    return literal, dynamic


def _catalog_call_names(bodies: dict[str, str]) -> dict[str, set[str]]:
    """{proname: literal artifact names it stamps through the helper}."""
    out: dict[str, set[str]] = {}
    for name, body in bodies.items():
        found = {
            m.group("arg").strip("'")
            for m in _SQL_CALL.finditer(body)
            if m.group("arg").startswith("'")
        }
        if found:
            out[name] = found
    return out


def _image_stat_mvs() -> list[str]:
    """`_MVS` out of scripts/refresh_image_stats.py, read with ast (no import)."""
    tree = ast.parse((_REPO / "scripts" / "refresh_image_stats.py").read_text("utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_MVS" for t in node.targets
        ):
            return [
                e.value for e in node.value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
    return []


def test_the_scanner_finds_the_call_sites_it_is_supposed_to_check():
    """Guards every scan below against passing vacuously.

    RED by: breaking `_PY_CALL` or `_SQL_CALL` — e.g. renaming the helper's first
    parameter away from `conn`, or wrapping the calls so the name is no longer the
    second argument. Without this, a regex that matches nothing makes every
    "all names are registered" assertion below trivially true.
    """
    literal, dynamic = _repo_call_sites()
    assert literal, (
        "the repo scanner found no literal stamp_derived_artifact call site at all — "
        "either every producer's stamp was deleted, or _PY_CALL/_SQL_CALL no longer "
        "match the call shape"
    )
    assert len(literal) >= 2, (
        f"only one file in the repo stamps the registry: {sorted(literal)}. Migration "
        "441 instrumented five producers."
    )
    assert _image_stat_mvs(), (
        "scripts/refresh_image_stats.py::_MVS could not be read — the compensating "
        "check for that file's dynamic stamp call has silently stopped checking anything"
    )


def test_every_stamp_call_in_the_repo_names_a_registered_artifact(registry):
    """RED by: mistyping one name, e.g. `stamp_derived_artifact(conn, "price_stat_chloropleth")`.

    The helper's UPDATE matches nothing on a bad name and raises nothing, so a typo is
    invisible at runtime and reads on the Health panel exactly like a producer that never
    ran. This is the only place it can be caught.
    """
    literal, _ = _repo_call_sites()
    unknown = sorted(
        (rel, name)
        for rel, names in literal.items()
        for name in names
        if name not in registry
    )
    assert not unknown, (
        f"stamp_derived_artifact call(s) naming an artifact with no derived_artifacts "
        f"row: {unknown}. Registered names are {sorted(registry)}."
    )


def test_every_stamp_call_inside_the_catalog_names_a_registered_artifact(
    registry, function_bodies
):
    """RED by: mistyping one of the six names inside `refresh_health_matviews`'s body.

    The repo scan above cannot see a function body that was replaced directly against the
    database outside any migration file; this reads `pg_proc.prosrc`, so it can.
    """
    per_function = _catalog_call_names(function_bodies)
    assert per_function, (
        "no function in the catalog stamps the registry through stamp_derived_artifact — "
        "migration 441's cutover of refresh_health_matviews is missing"
    )
    unknown = sorted(
        (fn, name)
        for fn, names in per_function.items()
        for name in names
        if name not in registry
    )
    assert not unknown, (
        f"function body/bodies stamp an unregistered artifact name: {unknown}. The "
        "UPDATE matches nothing, forever, and the panel shows a dead artifact."
    )


def test_the_only_dynamic_stamp_call_sites_are_the_documented_ones():
    """RED by: adding `db.stamp_derived_artifact(conn, some_variable)` in a new producer.

    A computed name is unreachable for every check in this file, so a new one has to be
    declared here together with the compensating check that covers it — otherwise the
    rails quietly stop covering the thing they exist to cover.
    """
    _, dynamic = _repo_call_sites()
    assert set(dynamic) == set(_DYNAMIC_STAMP_SITES), (
        f"dynamic stamp_derived_artifact call sites changed.\n"
        f"  found:    { {k: v for k, v in sorted(dynamic.items())} }\n"
        f"  declared: {sorted(_DYNAMIC_STAMP_SITES)}\n"
        "Prefer a literal artifact name. If it genuinely cannot be one, add the file to "
        "_DYNAMIC_STAMP_SITES with the check that covers it instead."
    )


def test_the_image_stat_matview_tuple_is_registered(registry):
    """The compensating check for scripts/refresh_image_stats.py's dynamic stamp.

    RED by: adding a matview to `_MVS` without a `derived_artifacts` seed row — that
    matview would then be refreshed every two hours and stamp nothing at all.
    """
    mvs = _image_stat_mvs()
    missing = sorted(mv for mv in mvs if mv not in registry)
    assert not missing, (
        f"scripts/refresh_image_stats.py refreshes and stamps {missing}, which has no "
        "derived_artifacts row — the stamp is a silent no-op for it"
    )


def test_every_registry_row_is_stamped_by_something(registry, function_bodies):
    """Corollary E, whole: a declared artifact whose freshness nothing writes is exactly
    the permanently-red row this wave exists to delete.

    RED by: deleting any producer's stamp — the helper call in one of the four Python
    producers, one of the six inside `refresh_health_matviews`, or one of the three
    pre-441 inline `update derived_artifacts ... where name = '...'` blocks.

    Three stamp shapes count, because three genuinely exist: the helper with a literal
    name, `scripts/refresh_image_stats.py`'s loop over `_MVS`, and the inline UPDATE that
    `refresh_llm_cost_rollups` and both blue-green rebuilds keep (the rollup cannot use
    the helper — it must pass its own watermark as `complete_through`).
    """
    literal, _ = _repo_call_sites()
    stamped: set[str] = set(_image_stat_mvs())
    for names in literal.values():
        stamped |= names
    for names in _catalog_call_names(function_bodies).values():
        stamped |= names
    for body in function_bodies.values():
        stamped |= {m.group("name") for m in _INLINE_STAMP.finditer(body)}

    unobserved = sorted(set(registry) - stamped)
    assert not unobserved, (
        f"derived_artifacts row(s) that nothing ever stamps: {unobserved}. Each one "
        "publishes 'never succeeded' forever, which is the state migration 441 exists to "
        "end — a panel with permanently-red rows trains the operator to ignore it."
    )


def test_every_registry_row_s_producer_exists(conn, registry):
    """RED by: renaming `scripts/refresh_image_stats.py` (or any producer function)
    without updating its `derived_artifacts.producer`.

    `producer` is the only pointer from a stale artifact back to the thing that should
    have refreshed it, so a producer naming something that no longer exists turns the
    panel's most actionable column into a dead end. Two spellings are legal and both are
    live today: a function in `pg_proc`, and a repo path on disk — including the one
    composite, `api/rent_map.py + fetch_rent_map.yml`, which is honest about an artifact
    refreshed inside a request handler with a monthly workflow as its floor.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
            " where n.nspname = 'public'"
        )
        functions = {r[0] for r in cur.fetchall()}

    broken: list[str] = []
    for name, producer in sorted(registry.items()):
        for token in (t.strip() for t in producer.split("+")):
            if not token:
                continue
            if token.endswith((".yml", ".yaml")) and "/" not in token:
                ok = (_REPO / ".github" / "workflows" / token).is_file()
            elif "/" in token or token.endswith(".py"):
                ok = (_REPO / token).is_file()
            else:
                ok = token in functions
            if not ok:
                broken.append(f"{name}: producer token {token!r} resolves to nothing")
    assert not broken, (
        "derived_artifacts.producer names something that does not exist:\n  "
        + "\n  ".join(broken)
    )


def test_the_six_health_matviews_are_stamped_individually(registry, function_bodies):
    """RED by: collapsing the six stamps into one at the end of the fan-out (and by
    dropping any single one of them).

    Each matview is its own registry row with its own freshness and its own
    `last_duration_ms`, so each gets its own stamp, immediately after its own REFRESH.
    Today that buys six real per-matview durations — `clock_timestamp()` advances inside a
    transaction, unlike `now()` — which is the only published answer to "which of the six
    is the slow one", and migration 440 measured that this job's trailing-7d p95 gap is
    77.7 minutes against a 90d p95 of 10.0.

    Asserted as an exact event sequence rather than a set, because a set cannot tell
    `refresh a; stamp a; refresh b; stamp b` from `refresh a; refresh b; stamp a; stamp b`
    — and the second is the shape this test exists to reject.

    NOT asserted, and deliberately not built: per-matview error handling. All six
    REFRESHes and all six stamps share ONE transaction (a plpgsql function with no
    BEGIN..EXCEPTION block opens no subtransaction), so a failure part-way rolls back the
    refreshes together with their stamps. Stamping individually is the correct shape and
    becomes load-bearing for partial-failure reporting only if the fan-out is ever split;
    that is a separate, filed change.
    """
    body = function_bodies.get("refresh_health_matviews")
    assert body, "refresh_health_matviews is missing from the catalog entirely"

    events = [
        ("refresh", m.group("refresh")) if m.group("refresh") else ("stamp", m.group("stamp"))
        for m in _REFRESH_OR_STAMP.finditer(body)
    ]
    refreshed = [name for kind, name in events if kind == "refresh"]

    declared = sorted(n for n, p in registry.items() if p == "refresh_health_matviews")
    assert sorted(refreshed) == declared, (
        f"refresh_health_matviews refreshes {sorted(refreshed)} but the registry declares "
        f"it as the producer of {declared} — the two must not drift apart"
    )

    expected = [pair for mv in refreshed for pair in (("refresh", mv), ("stamp", mv))]
    assert events == expected, (
        "the health fan-out no longer stamps each matview immediately after its own "
        f"REFRESH.\n  expected: {expected}\n  found:    {events}"
    )


def test_the_stamp_helper_is_unreachable_from_every_browser_role(conn):
    """RED by: dropping `public` from migration 441's REVOKE (leaving only anon and
    authenticated), or by granting EXECUTE back to either role.

    PostgreSQL grants EXECUTE on a NEW function to PUBLIC by default — a built-in, not an
    ACL entry — so revoking only the two named roles leaves the function callable by
    everyone, PostgREST RPC included. That is the trap migration 287 documented for
    SECURITY DEFINER functions on this project, and this helper is SECURITY DEFINER
    precisely so it can write a table `authenticated` cannot.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select p.prosecdef, p.proconfig, "
            "       has_function_privilege('anon', p.oid, 'EXECUTE'), "
            "       has_function_privilege('authenticated', p.oid, 'EXECUTE'), "
            "       coalesce(array_to_string(p.proacl, ','), '') "
            "  from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
            " where n.nspname = 'public' and p.proname = 'stamp_derived_artifact'"
        )
        row = cur.fetchone()

    assert row is not None, "public.stamp_derived_artifact() is missing from the catalog"
    secdef, proconfig, anon_exec, auth_exec, acl = row

    assert secdef is True, (
        "stamp_derived_artifact stopped being SECURITY DEFINER. Its UPDATE is allowed to "
        "match nothing, so a caller that loses write access on derived_artifacts turns "
        "every stamp into a silent no-op that looks exactly like 'never ran'."
    )
    assert proconfig and any(c.startswith("search_path=") for c in proconfig), (
        "stamp_derived_artifact lost its `set search_path` — mandatory on any SECURITY "
        "DEFINER function, and what makes the unqualified table reference unspoofable"
    )
    assert anon_exec is False, "anon can EXECUTE stamp_derived_artifact"
    assert auth_exec is False, "authenticated can EXECUTE stamp_derived_artifact"
    assert not any(entry.startswith("=") for entry in acl.split(",") if entry), (
        f"stamp_derived_artifact still carries a PUBLIC execute grant (proacl={acl!r}) — "
        "the REVOKE must name `public` as well as anon and authenticated"
    )
