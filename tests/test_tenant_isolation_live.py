"""Live tenant-isolation checks against the replayed schema (Phase 1 tenancy).

Gated on TEST_DATABASE_URL exactly like tests/test_sql_schema_prepare.py: with
no database configured (normal local `pytest`) the whole module skips; CI's
schema-replay job sets it and runs this against the freshly-rebuilt schema.

The TEST_DATABASE_URL login is the table OWNER (RLS never applies to it), so
every isolation assertion runs under an explicit `SET LOCAL ROLE authenticated`
— the same switch api.tenant_pool.tenant_conn issues per request — which is
what makes RLS bind, both here and in production.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — live tenant-isolation checks run only in the CI DB job",
)

# SPA-facing views (migrations 022/025/202/203/205/211/278) the frontend reads
# directly via supabase-js — must be `security_invoker` (migration 316) or they
# run as their postgres owner and BYPASS every RLS policy below, regardless of
# the caller's role. The base-table tests above don't exercise this: the SPA
# never queries base tables directly, only these views.
_TENANT_VIEWS: list[str] = [
    "collection_properties_public",
    "collections_public",
    "pipeline_stages_public",
    "property_estimates_public",
    "property_notes_public",
    "property_pipeline_public",
    "property_tags_public",
    "tags_public",
]

# The 19 user-state tables migrations 290-294 (+ entitlements, 298) scope per account.
_TENANT_TABLES: list[str] = [
    "collections",
    "tags",
    "property_notes",
    "filter_presets",
    "notification_subscriptions",
    "manual_rental_estimates",
    "collection_properties",
    "property_tags",
    "notification_dispatches",
    "estimation_cohort_entries",
    "estimation_trace_payloads",
    "estimation_feedback",
    "building_run_attachments",
    "estimation_runs",
    "building_runs",
    "property_pipeline",
    "pipeline_stages",
    "property_pipeline_events",
    "entitlements",
]

# Amendment A6 (Phase 0): the broker-directory PII surfaces stay dark to BOTH
# browser roles until Wave 4 ships masked columns. These are SECURITY DEFINER
# views/matview (the broker base tables are already RLS-on-no-policy), so the
# effective gate is the absence of a browser-role SELECT grant on each.
_BROKER_PII_RELATIONS: list[str] = [
    "brokers_public",
    "broker_firm_memberships_public",
    "broker_listings_public",
    "listing_broker_public",
    "broker_geo_options",
    "broker_resolution_runs_public",
    "broker_region_type_stats",
]


@pytest.fixture(scope="module")
def svc() -> "Iterator[Any]":
    import psycopg

    conn = psycopg.connect(_DB_URL, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="module")
def tenants(svc: Any) -> "Iterator[dict[str, uuid.UUID]]":
    """Two accounts + two auth users + memberships, via direct service-role
    inserts. The on-signup trigger (migration 294) would mint its own accounts
    and race for the legacy-backfill claim, so it is disabled around the
    auth.users inserts to keep the fixture deterministic."""
    a_user, b_user = uuid.uuid4(), uuid.uuid4()
    with svc.cursor() as cur:
        cur.execute(
            "INSERT INTO accounts (kind, name) VALUES ('personal', %s) RETURNING id",
            (f"iso-a-{a_user.hex[:8]}",),
        )
        a_acc = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO accounts (kind, name) VALUES ('personal', %s) RETURNING id",
            (f"iso-b-{b_user.hex[:8]}",),
        )
        b_acc = cur.fetchone()[0]
        cur.execute("ALTER TABLE auth.users DISABLE TRIGGER USER")
        try:
            cur.execute(
                "INSERT INTO auth.users (id, email) VALUES (%s, %s), (%s, %s)",
                (
                    a_user, f"iso-a-{a_user.hex[:8]}@test.local",
                    b_user, f"iso-b-{b_user.hex[:8]}@test.local",
                ),
            )
        finally:
            cur.execute("ALTER TABLE auth.users ENABLE TRIGGER USER")
        cur.execute(
            "INSERT INTO account_members (account_id, user_id, role) "
            "VALUES (%s, %s, 'owner'), (%s, %s, 'owner')",
            (a_acc, a_user, b_acc, b_user),
        )
    try:
        yield {"a_user": a_user, "b_user": b_user, "a_acc": a_acc, "b_acc": b_acc}
    finally:
        with svc.cursor() as cur:
            cur.execute(
                "DELETE FROM legacy_backfill_claim WHERE account_id IN (%s, %s)",
                (a_acc, b_acc),
            )
            cur.execute("DELETE FROM accounts WHERE id IN (%s, %s)", (a_acc, b_acc))
            cur.execute("DELETE FROM auth.users WHERE id IN (%s, %s)", (a_user, b_user))


@contextlib.contextmanager
def _scoped(sub: uuid.UUID) -> "Iterator[Any]":
    """One transaction scoped the way tenant_conn scopes: SET LOCAL ROLE
    authenticated + the caller's JWT claims, evaporating at transaction end."""
    import psycopg

    conn = psycopg.connect(_DB_URL, autocommit=False)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SET LOCAL ROLE authenticated")
                cur.execute(
                    "SELECT set_config('request.jwt.claims', %s, true)",
                    (json.dumps({"sub": str(sub), "role": "authenticated"}),),
                )
            yield conn
    finally:
        conn.close()


def test_cross_tenant_denial(svc: Any, tenants: dict[str, uuid.UUID]) -> None:
    name = f"iso-{uuid.uuid4().hex}"
    with svc.cursor() as cur:
        cur.execute(
            "INSERT INTO collections (account_id, name) VALUES (%s, %s) RETURNING id",
            (tenants["a_acc"], name),
        )
        coll_id = cur.fetchone()[0]
    try:
        with _scoped(tenants["b_user"]) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM collections WHERE name = %s", (name,))
                assert cur.fetchall() == [], "tenant B must not see tenant A's collection"
                cur.execute(
                    "UPDATE collections SET description = 'stolen' WHERE id = %s",
                    (coll_id,),
                )
                assert cur.rowcount == 0, "tenant B must not update tenant A's collection"
                cur.execute("DELETE FROM collections WHERE id = %s", (coll_id,))
                assert cur.rowcount == 0, "tenant B must not delete tenant A's collection"
        with _scoped(tenants["a_user"]) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM collections WHERE name = %s", (name,))
                assert len(cur.fetchall()) == 1, "tenant A must see their own collection"
    finally:
        with svc.cursor() as cur:
            cur.execute("DELETE FROM collections WHERE id = %s", (coll_id,))


def test_tenant_views_are_security_invoker(svc: Any) -> None:
    """Migration 316: every SPA-facing tenant view must run as the querying
    role, not its postgres owner, or RLS never binds through it at all."""
    with svc.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'v' "
            "AND c.relname = ANY(%s) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM pg_options_to_table(c.reloptions) o "
            "  WHERE o.option_name = 'security_invoker' AND o.option_value = 'true'"
            ")",
            (_TENANT_VIEWS,),
        )
        not_invoker = sorted(r[0] for r in cur.fetchall())
    assert not not_invoker, (
        f"view(s) not security_invoker — they run as their postgres owner and "
        f"BYPASS every RLS policy on the underlying table, leaking every "
        f"account's rows to every authenticated caller: {not_invoker}"
    )


def test_cross_tenant_denial_through_public_view(
    svc: Any, tenants: dict[str, uuid.UUID],
) -> None:
    """The base-table test above (test_cross_tenant_denial) doesn't reproduce
    what the SPA actually does: it never queries `collections` directly, only
    `collections_public`. A security-definer-ish view (no security_invoker)
    would pass the base-table test while still leaking every row through the
    view — exactly the live bug migration 316 fixed (found 2026-07-20)."""
    name = f"iso-view-{uuid.uuid4().hex}"
    with svc.cursor() as cur:
        cur.execute(
            "INSERT INTO collections (account_id, name) VALUES (%s, %s) RETURNING id",
            (tenants["a_acc"], name),
        )
        coll_id = cur.fetchone()[0]
    try:
        with _scoped(tenants["b_user"]) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM collections_public WHERE name = %s", (name,),
                )
                assert cur.fetchall() == [], (
                    "tenant B must not see tenant A's collection through "
                    "collections_public"
                )
        with _scoped(tenants["a_user"]) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM collections_public WHERE name = %s", (name,),
                )
                assert len(cur.fetchall()) == 1, (
                    "tenant A must see their own collection through "
                    "collections_public"
                )
    finally:
        with svc.cursor() as cur:
            cur.execute("DELETE FROM collections WHERE id = %s", (coll_id,))


# Migration 318: admin-only operational views/functions the SPA reads directly
# (dedup engine internals, scraper health, LLM cost, image training/labeling
# state, workflow health) that CANNOT use migration 316's security_invoker +
# base-table RLS policy technique, because several of them read the shared
# `listings`/`properties`/`images` tables (which must stay universally
# readable to every authenticated user for Browse). Instead each one embeds
# `is_platform_admin()` directly as a query filter -- evaluated per-request,
# independent of RLS/security_invoker/ownership.
_ADMIN_GATED_VIEWS: list[str] = [
    "data_quality_by_source", "dedup_engine_flow_public", "dedup_engine_runs_public",
    "dedup_funnel_resolutions_public", "dedup_label_events",
    "dedup_llm_cost_by_category_public", "dedup_queue_snapshot_public",
    "dedup_recency_backlog", "dedup_scan_state_public",
    "dedup_vision_bakeoff_results_public", "detail_latency_recent",
    "image_border_cases_public", "image_tag_annotations_public",
    "image_training_examples_public", "listing_detail_queue_public",
    "listing_fetch_failures_public", "llm_cost_daily_public", "llm_cost_hourly_public",
    "parsed_url_activity", "phash_pair_notes_public", "pipeline_check_history_public",
    "pipeline_checks_public", "publication_gate_health_public",
]
_ADMIN_GATED_FUNCTIONS: list[str] = [
    "images_failure_overview", "recent_workflow_failures", "workflow_failure_summary",
]


def test_admin_ops_views_embed_is_platform_admin(svc: Any) -> None:
    with svc.cursor() as cur:
        missing = []
        for name in _ADMIN_GATED_VIEWS:
            cur.execute("SELECT pg_get_viewdef(%s::regclass, true)", (f"public.{name}",))
            if "is_platform_admin()" not in cur.fetchone()[0]:
                missing.append(name)
        for name in _ADMIN_GATED_FUNCTIONS:
            cur.execute(
                "SELECT pg_get_functiondef(oid) FROM pg_proc "
                "WHERE proname = %s AND pronamespace = 'public'::regnamespace",
                (name,),
            )
            if "is_platform_admin()" not in cur.fetchone()[0]:
                missing.append(name)
    assert not missing, (
        f"admin-ops view/function(s) lost their is_platform_admin() gate -- any "
        f"authenticated caller (not just the admin) can read them again: {missing}"
    )


def test_admin_ops_views_deny_non_admin_allow_admin(
    svc: Any, tenants: dict[str, uuid.UUID],
) -> None:
    """Live proof the gate actually binds: tenant A (an ordinary account, not
    admin) sees zero rows through every admin-gated view/function; promoting
    that same user to `admins` makes them see through it (may still be zero
    rows if the table itself is empty in this schema-replay DB -- the point is
    no permission/relation error, not a specific count).

    Three objects (dedup_funnel_resolutions_public, dedup_llm_cost_by_category_public,
    images_failure_overview()) wrap a materialized view that a fresh schema-replay
    never refreshes (production refreshes all three on a cron) -- querying an
    unrefreshed matview errors regardless of caller, so refresh them here to match
    production instead of asserting on that unrelated failure mode."""
    with svc.cursor() as cur:
        cur.execute("REFRESH MATERIALIZED VIEW dedup_funnel_resolutions_mv")
        cur.execute("REFRESH MATERIALIZED VIEW dedup_llm_cost_by_category_mv")
        cur.execute("REFRESH MATERIALIZED VIEW images_failure_overview_mv")
    with _scoped(tenants["a_user"]) as conn:
        with conn.cursor() as cur:
            for name in _ADMIN_GATED_VIEWS:
                cur.execute(f"SELECT count(*) FROM {name}")
                assert cur.fetchone()[0] == 0, (
                    f"non-admin authenticated saw rows through {name}"
                )
            for name in _ADMIN_GATED_FUNCTIONS:
                cur.execute(f"SELECT count(*) FROM {name}()")
                assert cur.fetchone()[0] == 0, (
                    f"non-admin authenticated saw rows through {name}()"
                )
    with svc.cursor() as cur:
        cur.execute(
            "INSERT INTO admins (user_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (tenants["a_user"],),
        )
    try:
        with _scoped(tenants["a_user"]) as conn:
            with conn.cursor() as cur:
                for name in _ADMIN_GATED_VIEWS:
                    cur.execute(f"SELECT count(*) FROM {name}")  # must not raise
                for name in _ADMIN_GATED_FUNCTIONS:
                    cur.execute(f"SELECT count(*) FROM {name}()")  # must not raise
    finally:
        with svc.cursor() as cur:
            cur.execute("DELETE FROM admins WHERE user_id = %s", (tenants["a_user"],))


def test_no_anon_write_grants(svc: Any) -> None:
    with svc.cursor() as cur:
        cur.execute(
            "SELECT table_name, privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE grantee = 'anon' AND table_schema = 'public' "
            "AND table_name = ANY(%s)",
            (_TENANT_TABLES,),
        )
        anon_grants = cur.fetchall()
        cur.execute(
            "SELECT table_name, privilege_type "
            "FROM information_schema.role_table_grants "
            "WHERE grantee = 'authenticated' AND table_schema = 'public' "
            "AND privilege_type IN ('TRUNCATE', 'REFERENCES', 'TRIGGER') "
            "AND table_name = ANY(%s)",
            (_TENANT_TABLES,),
        )
        auth_extra = cur.fetchall()
    assert anon_grants == [], f"anon must hold NO privileges on user-state tables: {anon_grants}"
    assert auth_extra == [], (
        f"authenticated must never hold TRUNCATE/REFERENCES/TRIGGER: {auth_extra}"
    )


def test_broker_pii_dark_to_browser_roles(svc: Any) -> None:
    """Amendment A6: neither anon nor authenticated may read the broker-directory
    PII surfaces (or execute the broker_leaderboard RPC) before Wave 4 masking."""
    with svc.cursor() as cur:
        leaks: list[str] = []
        for rel in _BROKER_PII_RELATIONS:
            cur.execute(
                "SELECT has_table_privilege('anon', %s, 'SELECT'), "
                "       has_table_privilege('authenticated', %s, 'SELECT')",
                (f"public.{rel}", f"public.{rel}"),
            )
            anon_sel, auth_sel = cur.fetchone()
            if anon_sel:
                leaks.append(f"anon can SELECT {rel}")
            if auth_sel:
                leaks.append(f"authenticated can SELECT {rel}")
        # broker_leaderboard RPC — look the oid up so we don't hard-code the args
        cur.execute(
            "SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = 'broker_leaderboard'"
        )
        for (oid,) in cur.fetchall():
            cur.execute(
                "SELECT has_function_privilege('anon', %s, 'EXECUTE'), "
                "       has_function_privilege('authenticated', %s, 'EXECUTE')",
                (oid, oid),
            )
            anon_x, auth_x = cur.fetchone()
            if anon_x:
                leaks.append("anon can EXECUTE broker_leaderboard")
            if auth_x:
                leaks.append("authenticated can EXECUTE broker_leaderboard")
    assert not leaks, f"broker PII reachable by a browser role (A6 violated): {leaks}"


def test_user_state_tables_have_account_id_and_policy(svc: Any) -> None:
    with svc.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'account_id' "
            "AND table_name = ANY(%s)",
            (_TENANT_TABLES,),
        )
        with_column = {r[0] for r in cur.fetchall()}
        cur.execute(
            "SELECT DISTINCT tablename FROM pg_policies "
            "WHERE schemaname = 'public' AND tablename = ANY(%s)",
            (_TENANT_TABLES,),
        )
        with_policy = {r[0] for r in cur.fetchall()}
    missing_column = sorted(set(_TENANT_TABLES) - with_column)
    missing_policy = sorted(set(_TENANT_TABLES) - with_policy)
    assert not missing_column, f"tables missing account_id: {missing_column}"
    assert not missing_policy, f"tables missing an RLS policy: {missing_policy}"


def test_write_then_read_back_one_transaction(
    svc: Any, tenants: dict[str, uuid.UUID], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves tenant_conn's ONE-transaction contract (write + read-back share the
    SET LOCAL scope) — RLS binds via its role switch, not via the owner login."""
    from api import tenant_pool

    monkeypatch.setenv("TENANT_POOL_DB_URL", _DB_URL)
    name = f"iso-rw-{uuid.uuid4().hex}"
    claims = {"sub": str(tenants["a_user"]), "role": "authenticated"}
    try:
        gen = tenant_pool.tenant_conn(claims)
        conn = next(gen)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO collections (account_id, name) VALUES (%s, %s)",
                (tenants["a_acc"], name),
            )
            cur.execute("SELECT count(*) FROM collections WHERE name = %s", (name,))
            assert cur.fetchone()[0] == 1, "row must be visible INSIDE the same transaction"
        next(gen, None)  # resume past the yield -> clean commit + close

        with _scoped(tenants["a_user"]) as conn_a:
            with conn_a.cursor() as cur:
                cur.execute("SELECT count(*) FROM collections WHERE name = %s", (name,))
                assert cur.fetchone()[0] == 1, "committed row must be visible to tenant A"
        with _scoped(tenants["b_user"]) as conn_b:
            with conn_b.cursor() as cur:
                cur.execute("SELECT count(*) FROM collections WHERE name = %s", (name,))
                assert cur.fetchone()[0] == 0, "committed row must be invisible to tenant B"
    finally:
        with svc.cursor() as cur:
            cur.execute("DELETE FROM collections WHERE name = %s", (name,))
