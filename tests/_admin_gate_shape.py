"""Shared gate-shape validation for the two admin-gate CI lanes.

Both the offline migration-text scan (test_migration_rls_grants.py) and the live
schema scan (test_tenant_isolation_live.py) must decide whether an
`is_platform_admin()` call actually GATES a relation, not merely appears somewhere in
its definition. The exit-gate audit of migrations 329-332 showed the old checks — a
bare `"is_platform_admin()" in definition` substring, and a
`where[^;]*is_platform_admin\\(\\)` regex — both accept forms that gate nothing:

    where account_id = x or is_platform_admin()   -- non-admin still sees every row
    where is_platform_admin() or true             -- tautology

so a migration adding either would merge green. The offline lane has no behavioural
backstop at all, which makes this structural check load-bearing there.
"""

from __future__ import annotations

import re

# Migration 318's wrapper tail, allowing the `(select is_platform_admin())` variant.
_VIEW_GATE_TAIL = re.compile(
    r"\)\s*__admin_gate\s+where\s+(?:\(\s*select\s+)?is_platform_admin\(\)\s*\)?\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)

# A WHERE clause containing the gate (standalone or AND-chained): set-returning
# functions, and any view whose gate is a plain final qual rather than the wrapper.
_WHERE_GATE = re.compile(r"\bwhere\b[^;]*?\bis_platform_admin\(\)", re.IGNORECASE | re.DOTALL)

# Migration 332's scalar-RPC head: `case when is_platform_admin() then <payload> end`.
_CASE_GATE = re.compile(
    r"\bcase\s+when\s+(?:\(\s*select\s+)?is_platform_admin\(\)\s*\)?\s+then\b",
    re.IGNORECASE | re.DOTALL,
)

# An OR adjacent to the gate on either side neutralises it (this also catches the
# `is_platform_admin() or true` tautology).
_GATE_OR_EVASION = re.compile(
    r"\bor\s+\(?\s*(?:select\s+)?is_platform_admin"
    r"|is_platform_admin\(\)\s*\)?\s*or\b",
    re.IGNORECASE | re.DOTALL,
)


def gate_is_sound(definition: str, kind: str) -> bool:
    """True iff `definition` gates on is_platform_admin() in a boolean-restricting
    position with no OR/tautology escape. `kind` is 'view' or 'function'."""
    if _GATE_OR_EVASION.search(definition):
        return False
    if kind == "view":
        return bool(_VIEW_GATE_TAIL.search(definition) or _WHERE_GATE.search(definition))
    return bool(_WHERE_GATE.search(definition) or _CASE_GATE.search(definition))
