"""The admin gate must be hoisted, not called per row, in every RLS policy (mig 431).

`is_platform_admin()` is STABLE and argument-less, so one evaluation per statement is
exactly what STABLE promises. But in all 10 tenancy policies the gate sits inside an OR
with column references, which destroys the pseudoconstancy Postgres would otherwise
exploit — so the executor calls a SECURITY DEFINER function once per candidate row,
including on `llm_calls` at 293,551 rows. Wrapping it as `(select is_platform_admin())`
turns it into an InitPlan, evaluated once. Verified live:

    before:  Filter: (... OR ((account_id = '000...0') AND is_platform_admin()))
    after:   InitPlan 2 -> Result
             Filter: (... OR ((account_id = '000...0') AND (InitPlan 2).col1))

Semantics are bit-identical and the function still reads live, so revocation stays
instantaneous.

These are OFFLINE text assertions over the migration files — no DB. The live half (that
the replayed catalog actually carries the wrapped form) is
`tests/test_admin_gate_policies_live.py`, and the behavioural authority remains the
existing tenancy suite, which must pass UNCHANGED.

NOT covered here on purpose: `tests/_admin_gate_shape.py` is deliberately NOT extended to
`pg_policy`. Its `_GATE_OR_EVASION` rule rejects any `or ... is_platform_admin` — which is
the exact shape all 10 legitimate tenancy policies have, because a tenancy policy is an OR
of "my rows" and "platform rows". A regex cannot express the difference between a tenancy
OR and an evasion OR; pointing that guard at policies would force it to be weakened to
pass, and the module's own docstring records two earlier regex generations that were
weakened and then accepted gate-defeating forms. Behavioural assertions are the right
guard there.
"""

from __future__ import annotations

import re
from pathlib import Path

# Reused rather than re-derived: both are dollar-quote aware, so a DO block stays one
# statement and a `/*` inside a string literal does not swallow the file. Re-deriving
# them is how this rail first went wrong — a naive scan matched the prose "ALTER POLICY
# cannot lose TO authenticated" inside the migration's own header comment.
from tests.test_migration_rls_grants import _statements, _strip_comments

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS = _ROOT / "migrations"

_HOIST_MIGRATION = _MIGRATIONS / "431_admin_gate_initplan_hoist_policies.sql"
_REVERT = _MIGRATIONS / "reverts" / "431_revert_admin_gate_initplan_hoist_policies.sql"

# Every migration numbered ABOVE this must not introduce a bare per-row gate into a
# policy. 431 is the migration that made the wrapped form the standard.
MIN_WRAPPED = 431

# `(select is_platform_admin())`, in migration-source spelling or catalog-deparsed
# spelling (the deparser adds an ` AS is_platform_admin` alias).
_WRAPPED = re.compile(
    r"\(\s*select\s+is_platform_admin\s*\(\s*\)(?:\s+as\s+is_platform_admin)?\s*\)",
    re.IGNORECASE | re.DOTALL,
)

_POLICIES = (
    ("building_run_attachments", "building_run_attachments_tenant_rw"),
    ("building_runs", "building_runs_tenant_read"),
    ("estimation_cohort_entries", "estimation_cohort_entries_tenant_rw"),
    ("estimation_feedback", "estimation_feedback_tenant_rw"),
    ("estimation_runs", "estimation_runs_tenant_read"),
    ("estimation_trace_payloads", "estimation_trace_payloads_tenant_rw"),
    ("llm_calls", "llm_calls_tenant_read"),
    ("manual_rental_estimates", "manual_rental_estimates_admin_insert"),
    ("manual_rental_estimates", "manual_rental_estimates_admin_update"),
    ("notification_dispatches", "notification_dispatches_tenant_read"),
)

_NUM_RE = re.compile(r"^(\d+)_.*\.sql$")
_IS_POLICY_STMT = re.compile(r"^\s*(?:create|alter)\s+policy\b", re.IGNORECASE)


def _policy_statements(sql: str) -> list[str]:
    """Executable CREATE/ALTER POLICY statements only — comments stripped first."""
    return [s for s in _statements(sql) if _IS_POLICY_STMT.match(s)]


def test_hoist_migration_exists():
    assert _HOIST_MIGRATION.is_file(), f"{_HOIST_MIGRATION.name} is missing"


def test_every_gate_site_in_the_migration_is_wrapped():
    """Strip every wrapped form; nothing named is_platform_admin may survive.

    RED by: removing one `(select ...)` wrapper from any of the 11 sites.
    """
    body = _HOIST_MIGRATION.read_text()
    # The header comment quotes the BEFORE spelling to explain the defect, and the rail's
    # own DO block matches on the bare name. Only executable ALTER POLICY statements are
    # in scope here.
    statements = " ".join(_policy_statements(body))
    assert statements, "no ALTER POLICY statements found — did the migration change shape?"
    stripped = _WRAPPED.sub("", statements)
    assert "is_platform_admin" not in stripped.lower(), (
        "a bare per-row is_platform_admin() call survives in migration 431:\n" + stripped
    )


def test_migration_covers_every_policy():
    """RED by: deleting one ALTER POLICY line."""
    body = " ".join(_policy_statements(_HOIST_MIGRATION.read_text())).lower()
    missing = [f"{t}.{p}" for t, p in _POLICIES if p.lower() not in body]
    assert not missing, f"migration 431 does not name: {missing}"


def test_the_four_all_policies_do_not_gain_an_admin_arm():
    """Their with_check has no admin arm and must not acquire one.

    A scripted "wrap both expressions" pass would hallucinate a gate into these, which is
    a privilege change, not a performance change. The migration must specify USING alone.

    RED by: adding `with check (... (select is_platform_admin()))` to any of the four.
    """
    body = _HOIST_MIGRATION.read_text()
    for table, policy in _POLICIES:
        if not policy.endswith("_tenant_rw"):
            continue
        stmts = [s for s in _policy_statements(body) if policy.lower() in s.lower()]
        assert stmts, f"{policy} not found in migration 431"
        assert "with check" not in " ".join(stmts).lower(), (
            f"{policy} specifies WITH CHECK — its with_check predicate has no admin arm "
            "and must be left untouched"
        )


def test_revert_exists_and_is_outside_the_forward_chain():
    """The rollback is a written, reviewed file — and must not be replayed.

    `migrations/reverts/` is deliberate: the CI replay applies `ls migrations/*.sql`,
    which is not recursive, and tests/test_migration_numbers.py forbids a duplicate
    number above 304. A revert sitting beside its forward migration would be applied
    right after it and silently undo the change in every replayed environment.
    """
    assert _REVERT.is_file(), "the 431 revert migration is missing"
    assert not (_MIGRATIONS / _REVERT.name).exists(), (
        "the revert is in migrations/ — the schema replay would apply it and undo 431"
    )
    body = " ".join(_policy_statements(_REVERT.read_text()))
    for _, policy in _POLICIES:
        assert policy.lower() in body.lower(), f"revert does not restore {policy}"
    # The revert's whole point is restoring the BARE form.
    assert not _WRAPPED.search(body), "the revert still carries wrapped forms"


def test_no_new_migration_reintroduces_a_bare_gate_in_a_policy():
    """The durable anti-drift rail.

    Scoped precisely to the defect class: a POLICY predicate is where the gate gets
    OR'd with column references and therefore evaluated per row. Views and functions
    that call the gate standing alone are already one-evaluation and are out of scope.

    RED by: a future migration writing `create policy ... using (... or is_platform_admin())`.
    """
    offenders: list[str] = []
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        match = _NUM_RE.match(path.name)
        if not match or int(match.group(1)) < MIN_WRAPPED:
            continue
        for stmt in _policy_statements(path.read_text()):
            if "is_platform_admin" in _WRAPPED.sub("", stmt).lower():
                offenders.append(f"{path.name}: {' '.join(stmt.split())[:160]}")
    assert not offenders, (
        "migration(s) at or above "
        f"{MIN_WRAPPED} put a bare per-row is_platform_admin() inside a policy — wrap it "
        "as (select is_platform_admin()):\n" + "\n".join(offenders)
    )
