"""The replayed catalog must carry the HOISTED admin gate on every policy (mig 431).

The offline half (`tests/test_admin_gate_hoist.py`) asserts what migration 431's text
says. This asserts what the database actually ends up with, which is not the same claim:
a later migration can silently restore the per-row form, and `ALTER POLICY` leaves no
trace in the file that created the policy.

The defect being guarded: `is_platform_admin()` is SECURITY DEFINER, and in all 10 tenancy
policies it sits inside an OR with column references, so it is not pseudoconstant and the
executor calls it once per candidate row — on `llm_calls` at 293,551 rows. Nothing else
fails when this regresses. The rows returned are identical; only the cost changes.

Runs in CI's migrations lane against the replayed schema.

Skip behaviour: the lane sets `DB_RAILS_REQUIRED=1`, so a lane that loses its
`TEST_DATABASE_URL` goes RED instead of reporting a green skip.
"""

from __future__ import annotations

import os

import pytest

_DB_URL = os.environ.get("TEST_DATABASE_URL")
_REQUIRED = os.environ.get("DB_RAILS_REQUIRED") == "1"

pytestmark = pytest.mark.skipif(
    not _DB_URL and not _REQUIRED,
    reason="TEST_DATABASE_URL not set — this rail runs in CI's migrations lane",
)

# The deparser renders the wrapper with an added ` AS is_platform_admin` alias, so the
# catalog spelling differs from the migration-source spelling. Match both.
_UNWRAP = (
    r"\(\s*SELECT\s+is_platform_admin\(\)(\s+AS\s+is_platform_admin)?\s*\)"
)

_EXPECTED_POLICIES = 10
_EXPECTED_SITES = 11

# Their with_check has NO admin arm and must never acquire one — that would be a
# privilege change wearing a performance change's clothes.
_ALL_POLICIES_WITHOUT_ADMIN_WITH_CHECK = (
    "building_run_attachments_tenant_rw",
    "estimation_cohort_entries_tenant_rw",
    "estimation_feedback_tenant_rw",
    "estimation_trace_payloads_tenant_rw",
)

_GATED_POLICIES_SQL = """
select c.relname                                        as tbl,
       pol.polname                                      as pol,
       coalesce(pg_get_expr(pol.polqual, pol.polrelid, true), '')      as qual,
       coalesce(pg_get_expr(pol.polwithcheck, pol.polrelid, true), '') as withcheck,
       pol.polpermissive                                as permissive,
       pol.polroles::regrole[]::text[]                  as roles
  from pg_policy pol
  join pg_class c on c.oid = pol.polrelid
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public'
   and (pg_get_expr(pol.polqual, pol.polrelid, true) like '%is_platform_admin%'
     or pg_get_expr(pol.polwithcheck, pol.polrelid, true) like '%is_platform_admin%')
 order by c.relname, pol.polname
"""


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
def policies(conn):
    with conn.cursor() as cur:
        cur.execute(_GATED_POLICIES_SQL)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _strip_wrapped(conn, text: str) -> str:
    with conn.cursor() as cur:
        cur.execute("select regexp_replace(%s, %s, '', 'gi')", (text, _UNWRAP))
        return cur.fetchone()[0]


def test_no_policy_calls_the_gate_per_row(conn, policies):
    """RED by: applying migrations/reverts/431_revert_*.sql to the test DB."""
    offenders = [
        f"{p['tbl']}.{p['pol']}"
        for p in policies
        if "is_platform_admin" in _strip_wrapped(conn, p["qual"] + " " + p["withcheck"])
    ]
    assert not offenders, (
        "these policies still call is_platform_admin() once per candidate row: "
        f"{offenders}"
    )


def test_coverage_is_exactly_what_the_migration_claims(conn, policies):
    """A policy that quietly loses its admin arm is a tenancy change, not a perf one.

    RED by: dropping the admin arm from any policy.
    """
    # Count the WRAPPER, not the name. The deparser renders the wrapped form as
    # `( SELECT is_platform_admin() AS is_platform_admin)`, so the name appears TWICE per
    # site — once as the call, once as the alias — and counting the name reports 22 for 11.
    sites = sum(
        (p["qual"] + " " + p["withcheck"]).lower().count("select is_platform_admin()")
        for p in policies
    )
    assert (len(policies), sites) == (_EXPECTED_POLICIES, _EXPECTED_SITES), (
        f"expected {_EXPECTED_POLICIES} gated policies / {_EXPECTED_SITES} sites, got "
        f"{len(policies)} / {sites}: {[(p['tbl'], p['pol']) for p in policies]}"
    )


def test_roles_and_permissive_flag_survived_the_replay(policies):
    """`ALTER POLICY` cannot lose these, but a future DROP+CREATE silently can.

    A CREATE POLICY that omits `TO authenticated` defaults to PUBLIC — privilege
    escalation that no performance test would notice. RED by: recreating any of these
    policies without its role clause.
    """
    drifted = [
        f"{p['tbl']}.{p['pol']} roles={p['roles']} permissive={p['permissive']}"
        for p in policies
        if not p["permissive"] or p["roles"] != ["authenticated"]
    ]
    assert not drifted, f"policy attributes drifted: {drifted}"


def test_all_policies_did_not_gain_an_admin_with_check_arm(policies):
    """RED by: adding an admin arm to any *_tenant_rw with_check."""
    offenders = [
        f"{p['tbl']}.{p['pol']}"
        for p in policies
        if p["pol"] in _ALL_POLICIES_WITHOUT_ADMIN_WITH_CHECK
        and "is_platform_admin" in p["withcheck"]
    ]
    assert not offenders, (
        "these ALL policies gained an admin arm in with_check — a privilege change: "
        f"{offenders}"
    )


def test_the_gate_still_reads_live(conn):
    """The whole point of refusing a cache: revocation must stay instantaneous.

    `(select f())` is an InitPlan — evaluated once per STATEMENT, not once per session,
    not memoized across statements. This asserts the function is still STABLE (not
    IMMUTABLE, which would let the planner fold it at plan time and survive an admins
    change), still SECURITY DEFINER, and still argument-less.

    RED by: marking is_platform_admin() IMMUTABLE, or dropping SECURITY DEFINER.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select provolatile, prosecdef, pronargs "
            "from pg_proc where oid = 'public.is_platform_admin()'::regprocedure"
        )
        provolatile, prosecdef, pronargs = cur.fetchone()

    assert provolatile == "s", (
        f"is_platform_admin() volatility is {provolatile!r}, expected 's' (STABLE). "
        "IMMUTABLE would let the planner fold it and break instantaneous revocation."
    )
    assert prosecdef is True, "is_platform_admin() lost SECURITY DEFINER"
    assert pronargs == 0, (
        "is_platform_admin() gained an argument — it would no longer be pseudoconstant "
        "and the (select ...) hoist would stop being valid"
    )
