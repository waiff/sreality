"""Guardrail against the market-wide Browse timeout (migrations 273 -> 275).

The two anon-hot Browse read surfaces — the `properties_public` view (card list,
table, exact counts, watchdog/collection matchers) and the `properties_map_mv`
materialized view (the map) — both filter on the dedup publication gate. That
gate is `publication_gate_enabled()`, a SECURITY DEFINER SQL function, which the
planner CANNOT inline. Called BARE in a WHERE it runs ONCE PER ROW (~87k times
for the byt+pronájem cohort: 172k shared buffers, times out cold under the anon
3s budget); wrapped in a scalar subquery `(SELECT publication_gate_enabled())`
it runs ONCE as an InitPlan. Migration 273 shipped it bare and broke Browse;
migration 275 + refresh_map_mv.py wrap it.

This test pins the invariant so it can never regress: the EFFECTIVE definition
of each surface must wrap the gate call, never call it bare. It reads the LATEST
migration that defines `properties_public` (so an append-only re-inline in a
future migration is caught — that migration becomes the latest and is checked),
and the single source of truth for the matview shape, `scripts/refresh_map_mv.py`.
Deterministic and offline — no database required.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO / "migrations"
GATE_CALL = re.compile(r"publication_gate_enabled\s*\(\s*\)", re.IGNORECASE)
# Strip line comments so a comment that merely *describes* the bad pattern
# ("...a BARE call: publication_gate_enabled()...") isn't flagged. `--` covers
# SQL, `#` covers the Python source that embeds the matview build SQL.
LINE_COMMENT = re.compile(r"(--|#).*$", re.MULTILINE)


def _strip_comments(src: str) -> str:
    return LINE_COMMENT.sub("", src)


def _assert_gate_wrapped(raw: str, where: str) -> None:
    """Every `publication_gate_enabled()` call in executable `raw` (comments
    stripped) must be immediately preceded (ignoring whitespace) by `select` —
    i.e. `(select ...)`."""
    sql = _strip_comments(raw)
    for m in GATE_CALL.finditer(sql):
        preceding = sql[: m.start()].rstrip().lower()
        assert preceding.endswith("select"), (
            f"{where}: publication_gate_enabled() is called BARE — it must be "
            f"wrapped as `(select publication_gate_enabled())` so the planner "
            f"evaluates it ONCE (InitPlan), not once per row. Context: "
            f"...{sql[max(0, m.start() - 40):m.end() + 5]!r}"
        )


def _latest_migration_defining(view: str) -> Path:
    pat = re.compile(
        rf"create\s+(or\s+replace\s+)?(materialized\s+)?view\s+{re.escape(view)}\b",
        re.IGNORECASE,
    )
    hits = [p for p in MIGRATIONS.glob("*.sql") if pat.search(p.read_text())]
    assert hits, f"no migration defines {view}"
    # Numeric prefix orders them; the highest-numbered is the effective one.
    return max(hits, key=lambda p: int(p.name.split("_", 1)[0]))


def test_properties_public_wraps_the_publication_gate() -> None:
    src = _latest_migration_defining("properties_public")
    _assert_gate_wrapped(src.read_text(), src.name)


def test_map_matview_refresh_wraps_the_publication_gate() -> None:
    script = REPO / "scripts" / "refresh_map_mv.py"
    _assert_gate_wrapped(script.read_text(), script.name)


def test_guardrail_detects_a_bare_call() -> None:
    """The check itself must fail on a bare call (so it can't silently pass)."""
    import pytest

    with pytest.raises(AssertionError):
        _assert_gate_wrapped(
            "create view v as select 1 where not publication_gate_enabled();",
            "synthetic",
        )
    # ...and accept the wrapped form.
    _assert_gate_wrapped(
        "create view v as select 1 where not (select publication_gate_enabled());",
        "synthetic",
    )
