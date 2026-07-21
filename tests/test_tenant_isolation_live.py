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
import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from tests._admin_gate_shape import gate_is_sound

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — live tenant-isolation checks run only in the CI DB job",
)

# SPA-facing PER-ACCOUNT views (migrations 022/025/202/203/205/211/278) the
# frontend reads directly via supabase-js — must be `security_invoker` (migration
# 316) or they run as their postgres owner and BYPASS every RLS policy below,
# regardless of the caller's role. The base-table tests above don't exercise this:
# the SPA never queries base tables directly, only these views.
#
# `property_estimates_public` is deliberately NOT here. Migration 316 included it,
# but it is a MARKET-WIDE aggregate that joins `listings` (RLS-enabled with zero
# policies, so deny-all to every non-bypassrls role) — under invoker rights it
# returned zero rows to everyone and silently emptied Browse's "with estimates"
# filter. Migration 329 reverted it; `test_market_view_not_security_invoker`
# below pins that, and it must stay out of this list.
_TENANT_VIEWS: list[str] = [
    "collection_properties_public",
    "collections_public",
    "pipeline_stages_public",
    "property_notes_public",
    "property_pipeline_public",
    "property_tags_public",
    "tags_public",
]

# Views that must NOT be security_invoker: they join a shared table carrying
# RLS-enabled-with-zero-policies (`listings`/`properties`/`images`), which is
# deny-all under invoker rights, so flipping them silently returns zero rows to
# every caller instead of scoping anything.
#
# Owner rights does NOT mean unscoped. property_estimates_public reads
# `estimation_runs`, whose RLS cannot bind inside an owner-rights view, so
# migration 341 mirrors that read policy as an in-body predicate instead --
# scoping lives in the view body, not in invoker RLS
# (test_estimates_view_scopes_per_account).
_MARKET_VIEWS: list[str] = ["property_estimates_public"]

# Base relations + matviews that hold admin-only operational data. Any view (or
# function an authenticated caller can EXECUTE) that reads one of these must embed
# is_platform_admin() — enforced generically by
# test_no_ungated_relation_reads_admin_only_data, so a NEW admin surface is caught
# without anyone remembering to enumerate it.
#
# Deliberately EXCLUDED: listings / properties / images. Those are shared-market
# data read by many legitimately open views (listings_public, properties_public,
# browse_*, price_stat_*), so listing them would force a large, churny allowlist
# and train people to add entries reflexively. The residual blind spot — an
# admin-only aggregate over ONLY those tables, e.g. data_quality_by_source or
# publication_gate_health_public — stays covered by the enumerated lists below.
# Tenant tables are excluded too: they are scoped by RLS + security_invoker, a
# different mechanism with its own tests.
_ADMIN_ONLY_RELATIONS: list[str] = [
    "dedup_engine_runs", "dedup_scan_state", "dedup_vision_bakeoff_results",
    "dedup_decision_feedback", "property_identity_candidates", "property_merge_events",
    "listing_detail_queue", "listing_fetch_failures", "detail_queue_completions",
    "llm_calls", "parsed_url_cache", "phash_pair_notes", "pipeline_check_results",
    "image_border_cases", "image_tag_annotations", "image_training_examples",
    "workflow_failures", "workflow_run_health",
    # Added by migration 340: the audit found scrape_runs_public/recent_scrape_runs()
    # ungated and browser-readable because this list was seeded from migration 318's
    # objects rather than a first-principles table inventory.
    "scrape_runs", "worker_heartbeats",
    "health_summary_mv", "portal_health_mv", "scraper_health_checks_mv",
    "category_trends_mv", "image_storage_overview_mv", "snapshot_churn_24h_mv",
    "dedup_funnel_resolutions_mv", "dedup_llm_cost_by_category_mv",
    "images_failure_overview_mv",
]

# Relations that read the above but are legitimately reachable without the gate.
# EVERY entry needs a comment justifying it — an unexplained name here is how this
# class of bug comes back.
_ADMIN_GATE_ALLOWLIST: list[str] = [
    # The pg_cron refresher itself: SECURITY DEFINER, not executable by a browser
    # role, and it must run without a JWT context to rebuild the health matviews.
    "refresh_health_matviews",
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


def test_market_view_not_security_invoker(svc: Any) -> None:
    """Migration 329: the mirror of the test above. A market-wide view joining a
    zero-policy RLS table must run as its owner — invoker rights make that join
    deny-all and it returns zero rows to EVERY caller (the live regression
    migration 316 shipped for `property_estimates_public`, which emptied Browse's
    "with estimates" filter and the browse_stats_properties EXISTS test)."""
    with svc.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'v' "
            "AND c.relname = ANY(%s) "
            "AND EXISTS ("
            "  SELECT 1 FROM pg_options_to_table(c.reloptions) o "
            "  WHERE o.option_name = 'security_invoker' AND o.option_value = 'true'"
            ")",
            (_MARKET_VIEWS,),
        )
        wrongly_invoker = sorted(r[0] for r in cur.fetchall())
    assert not wrongly_invoker, (
        f"market-wide view(s) flipped to security_invoker — they join a shared "
        f"table whose RLS is enabled with zero policies, so they now return zero "
        f"rows to every caller instead of scoping anything: {wrongly_invoker}"
    )


@pytest.fixture(scope="module")
def seeded_estimate_rows(
    svc: Any, tenants: dict[str, uuid.UUID],
) -> "Iterator[dict[str, int]]":
    """One successful estimation_run per account (A, B) plus one on the shared SYSTEM
    account, each on its own property via a seeded sreality listing, so
    property_estimates_public has rows to scope. Seeded as svc (owner, RLS-exempt)."""
    a_acc, b_acc = tenants["a_acc"], tenants["b_acc"]
    system = uuid.UUID("00000000-0000-0000-0000-000000000000")
    base = 900_000_000 + int(uuid.uuid4().int % 50_000_000)
    plan = {"a": (a_acc, base + 1), "b": (b_acc, base + 2), "system": (system, base + 3)}
    props: dict[str, int] = {}
    with svc.cursor() as cur:
        for key, (acc, srid) in plan.items():
            cur.execute("INSERT INTO properties DEFAULT VALUES RETURNING id")
            props[key] = cur.fetchone()[0]
            # source='sreality' requires sreality_id > 0 (listings_sreality_id_sign_check)
            # and source_id_native NOT NULL (listings_source_id_native_present).
            cur.execute(
                "INSERT INTO listings (sreality_id, source, source_id_native, raw_json, "
                "property_id) VALUES (%s, 'sreality', %s, '{}'::jsonb, %s)",
                (srid, f"iso-est-{srid}", props[key]),
            )
            cur.execute(
                "INSERT INTO estimation_runs (account_id, source, mode, status, "
                "input_spec, input_sreality_id) "
                "VALUES (%s, 'api', 'deterministic', 'success', '{}'::jsonb, %s)",
                (acc, srid),
            )
    try:
        yield props
    finally:
        srids = [srid for _, srid in plan.values()]
        with svc.cursor() as cur:
            cur.execute("DELETE FROM estimation_runs WHERE input_sreality_id = ANY(%s)", (srids,))
            cur.execute("DELETE FROM listing_snapshots WHERE sreality_id = ANY(%s)", (srids,))
            cur.execute("DELETE FROM listings WHERE sreality_id = ANY(%s)", (srids,))
            cur.execute("DELETE FROM properties WHERE id = ANY(%s)", (list(props.values()),))


def test_estimates_view_scopes_per_account(
    tenants: dict[str, uuid.UUID], seeded_estimate_rows: dict[str, int],
) -> None:
    """Migration 341: property_estimates_public stays owner-rights (its join to
    zero-policy `listings` is deny-all under invoker rights) but mirrors
    estimation_runs' read policy in its body, so a tenant sees their own estimates
    and the shared SYSTEM account's, never another tenant's private estimation
    activity. The SYSTEM arm is load-bearing: without it every current run becomes
    invisible and Browse's "with estimates" filter empties -- the migration-316
    regression this whole batch started from."""
    pa, pb, ps = (seeded_estimate_rows[k] for k in ("a", "b", "system"))

    def visible(sub: uuid.UUID) -> set[int]:
        with _scoped(sub) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT property_id FROM property_estimates_public "
                    "WHERE property_id = ANY(%s)",
                    ([pa, pb, ps],),
                )
                return {r[0] for r in cur.fetchall()}

    seen_a, seen_b = visible(tenants["a_user"]), visible(tenants["b_user"])
    assert pa in seen_a, "tenant A cannot see their OWN estimate (view over-scoped)"
    assert pb not in seen_a, "tenant A sees tenant B's private estimate (cross-tenant leak)"
    assert ps in seen_a, "tenant A cannot see a shared SYSTEM-account estimate"
    assert pb in seen_b, "tenant B cannot see their OWN estimate"
    assert pa not in seen_b, "tenant B sees tenant A's private estimate (cross-tenant leak)"
    assert ps in seen_b, "tenant B cannot see a shared SYSTEM-account estimate"


def test_admin_gate_opens_for_service_but_not_role_switch(svc: Any) -> None:
    """Migrations 329/330: `is_platform_admin()` gates 26 admin-ops objects, but
    it reads the request.jwt.claims GUC, which only PostgREST and the tenant pool
    set — so on a claims-less connection it was false and those objects returned
    zero rows, silently no-op'ing build_dedup_golden_set.py and poisoning the
    pg_cron-refreshed health matviews. The fallback must open for a genuine
    service connection and stay shut for a claims-less role switch."""
    with svc.cursor() as cur:
        cur.execute("SELECT is_platform_admin()")
        assert cur.fetchone()[0] is True, (
            "claims-less service connection is not admin — pg_cron's health "
            "matview refresh and the golden-set freeze would read zero rows"
        )
        cur.execute("SELECT count(*) FROM listing_fetch_failures_public")
        assert cur.fetchone()[0] is not None

    import psycopg

    conn = psycopg.connect(_DB_URL, autocommit=False)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SET LOCAL ROLE authenticated")
                cur.execute("SELECT is_platform_admin()")
                assert cur.fetchone()[0] is False, (
                    "a claims-less SET ROLE reported admin — session_user alone "
                    "is not enough on an owner login (migration 330)"
                )
    finally:
        conn.close()


@pytest.fixture(scope="module")
def seeded_tenant_rows(
    svc: Any, tenants: dict[str, uuid.UUID],
) -> "Iterator[dict[str, tuple[str, tuple[Any, ...]]]]":
    """One row per tenant view, all owned by account A, each findable by a filter
    the view actually exposes. Seeded as `svc` (the owner, so RLS never blocks
    setup); torn down children-first."""
    nonce = uuid.uuid4().hex[:12]
    a_acc = tenants["a_acc"]
    where: dict[str, tuple[str, tuple[Any, ...]]] = {}
    cleanup: list[tuple[str, tuple[Any, ...]]] = []
    with svc.cursor() as cur:
        # property_pipeline holds at most one card per property, so it gets its own.
        cur.execute("INSERT INTO properties DEFAULT VALUES RETURNING id")
        prop = cur.fetchone()[0]
        cur.execute("INSERT INTO properties DEFAULT VALUES RETURNING id")
        prop_pipe = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO collections (account_id, name) VALUES (%s, %s) RETURNING id",
            (a_acc, nonce),
        )
        coll = cur.fetchone()[0]
        cleanup.append(("DELETE FROM collections WHERE id = %s", (coll,)))
        where["collections_public"] = ("name = %s", (nonce,))

        cur.execute(
            # `color` is a named-palette CHECK (copper/sage/brick/ochre/slate/
            # plum/teal/sand), not a hex string.
            "INSERT INTO tags (account_id, name, color) VALUES (%s, %s, 'slate') RETURNING id",
            (a_acc, nonce),
        )
        tag = cur.fetchone()[0]
        cleanup.append(("DELETE FROM tags WHERE id = %s", (tag,)))
        where["tags_public"] = ("name = %s", (nonce,))

        cur.execute(
            "INSERT INTO property_notes (account_id, property_id, body) VALUES (%s, %s, %s)",
            (a_acc, prop, nonce),
        )
        cleanup.insert(0, ("DELETE FROM property_notes WHERE body = %s", (nonce,)))
        where["property_notes_public"] = ("body = %s", (nonce,))

        # No text column on this view — filter on the id pair instead.
        cur.execute(
            "INSERT INTO property_tags (account_id, property_id, tag_id) VALUES (%s, %s, %s)",
            (a_acc, prop, tag),
        )
        cleanup.insert(0, ("DELETE FROM property_tags WHERE tag_id = %s", (tag,)))
        where["property_tags_public"] = ("property_id = %s AND tag_id = %s", (prop, tag))

        cur.execute(
            "INSERT INTO collection_properties (account_id, collection_id, property_id) "
            "VALUES (%s, %s, %s)",
            (a_acc, coll, prop),
        )
        cleanup.insert(0, ("DELETE FROM collection_properties WHERE collection_id = %s", (coll,)))
        where["collection_properties_public"] = ("collection_id = %s", (coll,))

        # A fresh account has no stages; a lone non-entry/non-terminal stage satisfies
        # both column CHECKs (the entry/terminal invariants are API-enforced, not DB).
        # The view filters archived_at IS NULL, so leave it NULL.
        cur.execute(
            "INSERT INTO pipeline_stages (account_id, key, label, position) "
            "VALUES (%s, %s, 'iso', 1) RETURNING id",
            (a_acc, nonce),
        )
        stage = cur.fetchone()[0]
        cleanup.append(("DELETE FROM pipeline_stages WHERE id = %s", (stage,)))
        where["pipeline_stages_public"] = ("key = %s", (nonce,))

        cur.execute(
            "INSERT INTO property_pipeline (account_id, property_id, stage_id) VALUES (%s, %s, %s)",
            (a_acc, prop_pipe, stage),
        )
        cleanup.insert(0, ("DELETE FROM property_pipeline WHERE property_id = %s", (prop_pipe,)))
        where["property_pipeline_public"] = ("property_id = %s", (prop_pipe,))
    try:
        yield where
    finally:
        with svc.cursor() as cur:
            for sql, params in cleanup:
                cur.execute(sql, params)
            cur.execute("DELETE FROM properties WHERE id IN (%s, %s)", (prop, prop_pipe))


@pytest.mark.parametrize("view", _TENANT_VIEWS)
def test_tenant_view_scopes_both_ways(
    tenants: dict[str, uuid.UUID],
    seeded_tenant_rows: dict[str, tuple[str, tuple[Any, ...]]],
    view: str,
) -> None:
    """Every SPA-facing tenant view, both directions: tenant B must not see tenant A's
    row, and tenant A MUST see it. The positive half is the half that was missing.
    `test_tenant_views_are_security_invoker` checks the reloption but cannot tell a
    correctly-scoped view from one that returns zero rows to everybody — which is
    exactly what migration 316 shipped for `property_estimates_public`. A
    parameterized read-your-own-row assertion would have failed CI on that PR instead
    of reaching production."""
    clause, params = seeded_tenant_rows[view]
    sql = f"SELECT * FROM {view} WHERE {clause}"  # noqa: S608 - fixed view list
    with _scoped(tenants["b_user"]) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            assert cur.fetchall() == [], f"tenant B saw tenant A's row through {view}"
    with _scoped(tenants["a_user"]) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            assert len(cur.fetchall()) == 1, (
                f"tenant A cannot see their OWN row through {view} — the view is "
                f"not scoped, it is broken (this is the migration 316 failure mode)"
            )


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
    "scrape_runs_public",
]
_ADMIN_GATED_FUNCTIONS: list[str] = [
    "images_failure_overview", "recent_workflow_failures", "workflow_failure_summary",
    "recent_scrape_runs",
]

# Migration 332: the five health/ops RPCs the Health dashboard calls. Unlike the
# set-returning functions above these return a scalar jsonb, so the gate shows up as
# NULL rather than zero rows — `count(*) FROM fn()` is 1 either way and would not
# catch a regression. Migration 318 missed them because its triage worked from the
# `security_definer_view` advisor list and these were plain SECURITY INVOKER SQL
# functions; SPA route-gating (<AdminPage>) is a client affordance, not a boundary.
_ADMIN_GATED_SCALAR_RPCS: list[str] = [
    "health_summary()",
    "portal_health_summary()",
    "scraper_health_checks('sreality')",
    "category_trends('sreality')",
    "image_storage_overview()",
]


def test_admin_ops_views_embed_is_platform_admin(svc: Any) -> None:
    """Cheap structural guard against the gate being dropped or moved out of the
    WHERE clause. It is NOT authoritative — a definition can satisfy this regex and
    still not gate rows. `test_admin_ops_views_deny_non_admin_allow_admin` is the
    test that actually proves the binding; keep both."""
    with svc.cursor() as cur:
        missing = []
        for name in _ADMIN_GATED_VIEWS:
            cur.execute("SELECT pg_get_viewdef(%s::regclass, true)", (f"public.{name}",))
            if not gate_is_sound(cur.fetchone()[0], "view"):
                missing.append(name)
        for name in _ADMIN_GATED_FUNCTIONS:
            cur.execute(
                "SELECT pg_get_functiondef(oid) FROM pg_proc "
                "WHERE proname = %s AND pronamespace = 'public'::regnamespace",
                (name,),
            )
            if not gate_is_sound(cur.fetchone()[0], "function"):
                missing.append(name)
    assert not missing, (
        f"admin-ops view/function(s) have no is_platform_admin() call in a WHERE "
        f"position -- any authenticated caller (not just the admin) may be able to "
        f"read them again: {missing}"
    )


def test_no_ungated_relation_reads_admin_only_data(svc: Any) -> None:
    """The standing gate: any view, or any function an authenticated caller can
    EXECUTE, that reads an admin-only relation must carry a SOUND is_platform_admin()
    gate or be explicitly allowlisted. Unlike the enumerated lists above this
    generalises -- admin surface #27 is caught without anyone remembering to register
    it, which is exactly how scrape_runs_public slipped through (migration 340).
    Soundness is judged in Python by gate_is_sound rather than by a SQL substring
    match: `where x = y or is_platform_admin()` contains the call but gates nothing.
    listings/properties/images are deliberately absent from _ADMIN_ONLY_RELATIONS --
    shared-market data behind many legitimately open views; see that list's comment."""
    with svc.cursor() as cur:
        cur.execute(
            "SELECT c.relname, 'view', pg_get_viewdef(c.oid, true) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'v' "
            "  AND EXISTS (SELECT 1 FROM unnest(%s::text[]) t "
            "              WHERE pg_get_viewdef(c.oid) ~ ('\\m' || t || '\\M')) "
            "UNION ALL "
            "SELECT p.proname, 'function', pg_get_functiondef(p.oid) FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' "
            "  AND has_function_privilege('authenticated', p.oid, 'EXECUTE') "
            "  AND EXISTS (SELECT 1 FROM unnest(%s::text[]) t "
            "              WHERE p.prosrc ~ ('\\m' || t || '\\M')) "
            "ORDER BY 1",
            (_ADMIN_ONLY_RELATIONS, _ADMIN_ONLY_RELATIONS),
        )
        candidates = cur.fetchall()
    ungated = [
        f"{kind} {name}"
        for name, kind, definition in candidates
        if name not in _ADMIN_GATE_ALLOWLIST and not gate_is_sound(definition, kind)
    ]
    assert ungated == [], (
        f"relation(s) read admin-only data without a sound is_platform_admin() gate, "
        f"so any signed-in non-admin can read them over supabase-js (SPA route-gating "
        f"is not a security boundary). Add the gate, or add an entry to "
        f"_ADMIN_GATE_ALLOWLIST with a comment justifying why it is safe: {ungated}"
    )


# Views whose gate the seed fixture below can prove BEHAVIOURALLY (a service-role
# read returns >= 1 row, so a non-admin's 0 is the gate rather than an empty table).
# Established by seeding production inside a rolled-back transaction and diffing each
# view's count. Deliberately absent: dedup_recency_backlog and detail_latency_recent
# (aggregates whose row count does not move for one extra base row), plus the two
# matview-backed views and the set-returning functions, which need a refresh chain the
# replay never runs -- those keep a structural-only deny assertion.
_SEEDED_ADMIN_VIEWS: frozenset[str] = frozenset({
    "dedup_engine_runs_public", "dedup_label_events", "dedup_queue_snapshot_public",
    "dedup_scan_state_public", "dedup_vision_bakeoff_results_public",
    "image_border_cases_public", "image_tag_annotations_public",
    "image_training_examples_public", "listing_detail_queue_public",
    "listing_fetch_failures_public", "llm_cost_daily_public", "llm_cost_hourly_public",
    "parsed_url_activity", "phash_pair_notes_public", "pipeline_check_history_public",
    "pipeline_checks_public", "scrape_runs_public",
})


@pytest.fixture(scope="module")
def seeded_admin_rows(svc: Any) -> "Iterator[None]":
    """One service-role row behind each view in _SEEDED_ADMIN_VIEWS. Without this the
    deny assertion below is vacuous: the CI schema replay holds no business data, so
    `count(*) == 0` for a non-admin passes whether or not the gate works. Seeded as
    svc (owner, RLS-exempt, and is_platform_admin() true via the claims-less
    bypassrls fallback); torn down children-first."""
    n = f"iso-{uuid.uuid4().hex[:10]}"
    rid = 900_000_000 + int(uuid.uuid4().int % 40_000_000)
    rid2 = rid + 1
    with svc.cursor() as cur:
        cur.execute("INSERT INTO properties DEFAULT VALUES RETURNING id")
        pa = cur.fetchone()[0]
        cur.execute("INSERT INTO properties DEFAULT VALUES RETURNING id")
        pb = cur.fetchone()[0]
        lo, hi = min(pa, pb), max(pa, pb)  # CHECK left_property_id < right_property_id
        cur.execute("INSERT INTO images (sreality_url) VALUES (%s) RETURNING id", (f"https://iso.test/{n}a.jpg",))
        ia = cur.fetchone()[0]
        cur.execute("INSERT INTO images (sreality_url) VALUES (%s) RETURNING id", (f"https://iso.test/{n}b.jpg",))
        ib = cur.fetchone()[0]
        ilo, ihi = min(ia, ib), max(ia, ib)  # CHECK image_id_a < image_id_b

        cur.execute("INSERT INTO dedup_engine_runs DEFAULT VALUES RETURNING id")
        run_id = cur.fetchone()[0]
        cur.execute("INSERT INTO dedup_scan_state (lane) VALUES (%s)", (n,))
        cur.execute("INSERT INTO listing_detail_queue (source, native_id) VALUES ('sreality', %s)", (n,))
        cur.execute("INSERT INTO listing_fetch_failures (sreality_id) VALUES (%s)", (rid2,))
        cur.execute(
            "INSERT INTO llm_calls (called_for, provider, model, input_tokens, "
            "output_tokens, cost_usd) VALUES ('parse_url', 'anthropic', %s, 1, 1, 0.000001)",
            (n,),
        )
        cur.execute(
            "INSERT INTO parsed_url_cache (url_hash, source_url, source_kind, parse_result) "
            "VALUES (%s, 'https://iso.test/x', 'sreality', '{}'::jsonb)",
            (n,),
        )
        cur.execute("INSERT INTO pipeline_check_results (run_at, check_key, status) VALUES (now(), %s, 'ok')", (n,))
        cur.execute("INSERT INTO phash_pair_notes (image_id_a, image_id_b) VALUES (%s, %s)", (ilo, ihi))
        cur.execute("INSERT INTO image_border_cases (image_id) VALUES (%s)", (ilo,))
        cur.execute("INSERT INTO image_tag_annotations (image_id) VALUES (%s)", (ilo,))
        cur.execute("INSERT INTO image_training_examples (image_id, label) VALUES (%s, 'iso')", (ilo,))
        cur.execute(
            "INSERT INTO property_identity_candidates (left_property_id, right_property_id, "
            "tier, status) VALUES (%s, %s, 'street_disposition', 'proposed')",
            (lo, hi),
        )
        # `id` is identity-generated here — do not supply it.
        cur.execute(
            "INSERT INTO dedup_decision_feedback (left_property_id, right_property_id, "
            "is_incorrect, expected_outcome) VALUES (%s, %s, true, 'should_merge')",
            (lo, hi),
        )
        cur.execute(
            "INSERT INTO dedup_vision_bakeoff_results (run_label, set_name, check_type, lane, "
            "model, sreality_id_a, sreality_id_b, danger_verdict, candidate_verdict, is_dangerous) "
            "VALUES (%s, 'iso', 'review', 'compare', 'iso-model', %s, %s, 'different', 'different', false)",
            (n, rid, rid2),
        )
        cur.execute("INSERT INTO scrape_runs (run_type, source) VALUES ('index', 'sreality') RETURNING id")
        scrape_id = cur.fetchone()[0]
    try:
        yield None
    finally:
        with svc.cursor() as cur:  # children before parents
            cur.execute("DELETE FROM image_border_cases WHERE image_id = %s", (ilo,))
            cur.execute("DELETE FROM image_tag_annotations WHERE image_id = %s", (ilo,))
            cur.execute("DELETE FROM image_training_examples WHERE image_id = %s", (ilo,))
            cur.execute("DELETE FROM phash_pair_notes WHERE image_id_a = %s AND image_id_b = %s", (ilo, ihi))
            cur.execute("DELETE FROM dedup_decision_feedback WHERE left_property_id = %s AND right_property_id = %s", (lo, hi))
            cur.execute("DELETE FROM property_identity_candidates WHERE left_property_id = %s AND right_property_id = %s", (lo, hi))
            cur.execute("DELETE FROM dedup_vision_bakeoff_results WHERE run_label = %s", (n,))
            cur.execute("DELETE FROM pipeline_check_results WHERE check_key = %s", (n,))
            cur.execute("DELETE FROM parsed_url_cache WHERE url_hash = %s", (n,))
            cur.execute("DELETE FROM llm_calls WHERE model = %s", (n,))
            cur.execute("DELETE FROM listing_fetch_failures WHERE sreality_id = %s", (rid2,))
            cur.execute("DELETE FROM listing_detail_queue WHERE native_id = %s", (n,))
            cur.execute("DELETE FROM dedup_scan_state WHERE lane = %s", (n,))
            cur.execute("DELETE FROM dedup_engine_runs WHERE id = %s", (run_id,))
            cur.execute("DELETE FROM scrape_runs WHERE id = %s", (scrape_id,))
            cur.execute("DELETE FROM images WHERE id IN (%s, %s)", (ia, ib))
            cur.execute("DELETE FROM properties WHERE id IN (%s, %s)", (pa, pb))


def test_admin_ops_views_deny_non_admin_allow_admin(
    svc: Any, tenants: dict[str, uuid.UUID], seeded_admin_rows: None,
) -> None:
    """Live proof the gate actually binds: tenant A (an ordinary account, not admin)
   sees zero rows through every admin-gated view/function; promoting that same user
   to `admins` makes them see through it (may still be zero rows if the table itself
   is empty in this schema-replay DB -- the point is no permission/relation error,
   not a specific count). Three objects (dedup_funnel_resolutions_public,
   dedup_llm_cost_by_category_public, images_failure_overview()) wrap a materialized
   view that a fresh schema-replay never refreshes (production refreshes all three
   on a cron) -- querying an unrefreshed matview errors regardless of caller, so
   refresh them here to match production instead of asserting on that unrelated
   failure mode."""
    with svc.cursor() as cur:
        cur.execute("REFRESH MATERIALIZED VIEW dedup_funnel_resolutions_mv")
        cur.execute("REFRESH MATERIALIZED VIEW dedup_llm_cost_by_category_mv")
        cur.execute("REFRESH MATERIALIZED VIEW images_failure_overview_mv")
        # Positive control: without a seeded row behind each view, the non-admin
        # `count(*) == 0` below passes on an empty schema replay whether or not the
        # gate works. Prove the row is reachable before asserting it is hidden.
        unreachable = []
        for name in sorted(_SEEDED_ADMIN_VIEWS):
            cur.execute(f"SELECT count(*) FROM {name}")  # noqa: S608 - fixed list
            if cur.fetchone()[0] == 0:
                unreachable.append(name)
        assert unreachable == [], (
            f"seed did not reach {unreachable} — the deny assertion for those views "
            f"would be vacuous, so fix the fixture rather than trusting the zero"
        )
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
            for call in _ADMIN_GATED_SCALAR_RPCS:
                cur.execute(f"SELECT {call}")  # noqa: S608 - fixed list
                assert cur.fetchone()[0] is None, (
                    f"non-admin authenticated got data back from {call}"
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
                for call in _ADMIN_GATED_SCALAR_RPCS:
                    # Must not raise. Not asserted non-NULL for every call: three of
                    # the five read a matview the replay never refreshes. The two that
                    # are unconditional (below) ARE asserted, so the admin direction
                    # is a real positive control rather than a smoke test.
                    cur.execute(f"SELECT {call}")  # noqa: S608 - fixed list
                for call in ("image_storage_overview()", "category_trends('sreality')"):
                    # These build their payload from a bare aggregate / coalesce
                    # default, so they are non-NULL even on empty data — meaning a
                    # dropped gate would leak a real value here, and NULL means the
                    # gate wrongly stayed shut for an admin.
                    cur.execute(f"SELECT {call}")  # noqa: S608 - fixed list
                    assert cur.fetchone()[0] is not None, (
                        f"admin got NULL from {call} — the gate did not open"
                    )
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


# A materialized view can carry neither RLS nor an embedded gate, so `authenticated`
# holding SELECT on one reads exactly what its gated wrapper hides. The first three
# back migration 318's gated views/function (closed by 331); the rest back the five
# health/ops RPCs (closed by 332, which had to convert those RPCs to SECURITY
# DEFINER first — while they were INVOKER they read these as the CALLER, so revoking
# would have broken the operator's own Health dashboard).
#
# Deliberately absent: properties_map_mv, price_stat_choropleth, rent_map_choropleth
# — the SPA reads those three directly as shared-market data.
_ADMIN_GATED_MATVIEWS: list[str] = [
    "dedup_funnel_resolutions_mv",
    "dedup_llm_cost_by_category_mv",
    "images_failure_overview_mv",
    "health_summary_mv",
    "health_mv_refresh_stamp",
    "portal_health_mv",
    "scraper_health_checks_mv",
    "snapshot_churn_24h_mv",
    "category_trends_mv",
    "image_storage_overview_mv",
]


def test_anon_holds_no_relation_grants(svc: Any) -> None:
    """Migration 331: the settled Phase 0 posture is that anon reads NOTHING, so
    the allowlist is empty and this asserts equality, never a subset. Migration 299
    swept anon once, but a one-time sweep cannot cover grants added by LATER
    migrations — 303/308/309/310/311/315 each re-opened a view to anon, two of them
    leaking real rows. `has_table_privilege` is deliberate: it covers materialized
    views (which information_schema.role_table_grants omits entirely) and privileges
    inherited from a grant to PUBLIC."""
    with svc.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r','v','m','p') "
            "AND has_table_privilege('anon', c.oid, 'SELECT') "
            # PostGIS lands geometry_columns/spatial_ref_sys in `public` on the CI
            # image and grants them to PUBLIC; they are extension-owned, not ours.
            "AND NOT EXISTS (SELECT 1 FROM pg_depend d "
            "  WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid "
            "  AND d.deptype = 'e') "
            "ORDER BY c.relname",
        )
        readable = [r[0] for r in cur.fetchall()]
    assert readable == [], (
        f"anon must hold no SELECT on any relation — the SPA is fully login-gated "
        f"and reads as authenticated. Drift (usually a `grant ... to anon` in a "
        f"migration added after 299's sweep): {readable}"
    )


def test_admin_gated_matviews_dark_to_authenticated(svc: Any) -> None:
    """A matview cannot embed migration 318's is_platform_admin() filter, so raw
    SELECT on it bypasses the gate its wrapper view enforces. Legitimate readers go
    through the owner-rights view or the SECURITY DEFINER function, which keep
    access via the owner — no non-admin surface needs these directly."""
    with svc.cursor() as cur:
        leaks: list[str] = []
        for mv in _ADMIN_GATED_MATVIEWS:
            cur.execute("SELECT to_regclass(%s)", (f"public.{mv}",))
            if cur.fetchone()[0] is None:
                continue
            cur.execute(
                "SELECT has_table_privilege('authenticated', %s, 'SELECT')",
                (f"public.{mv}",),
            )
            if cur.fetchone()[0]:
                leaks.append(mv)
    assert leaks == [], (
        f"authenticated can SELECT the raw matview(s) behind migration 318's admin "
        f"gate, bypassing it entirely: {leaks}"
    )


def test_matviews_not_writable_by_browser_roles(svc: Any) -> None:
    """Migration 299's authenticated-write revoke scoped itself to relkind in
    ('r','p','v'), silently skipping materialized views, which kept the pre-299 default
    ACL including DML and (on PG17) MAINTAIN; migration 331 closed it and 342 made the
    MAINTAIN half durable at the default ACL. Both browser roles are checked for all
    three DML verbs -- the original asymmetry tested anon for INSERT only."""
    with svc.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'm' "
            "AND (has_table_privilege('authenticated', c.oid, 'INSERT') "
            "  OR has_table_privilege('authenticated', c.oid, 'UPDATE') "
            "  OR has_table_privilege('authenticated', c.oid, 'DELETE') "
            "  OR has_table_privilege('anon', c.oid, 'INSERT') "
            "  OR has_table_privilege('anon', c.oid, 'UPDATE') "
            "  OR has_table_privilege('anon', c.oid, 'DELETE')) "
            "ORDER BY c.relname",
        )
        offenders = [r[0] for r in cur.fetchall()]
        # MAINTAIN is PG17+ (it permits REFRESH/VACUUM/ANALYZE/CLUSTER/REINDEX).
        # has_table_privilege only accepts the token on >=17 and the CI schema replay
        # is 15, so probe it conditionally: this is a no-op until the replay is bumped.
        cur.execute("SELECT current_setting('server_version_num')::int >= 170000")
        if cur.fetchone()[0]:
            cur.execute(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r','m','p') "
                "AND (has_table_privilege('authenticated', c.oid, 'MAINTAIN') "
                "  OR has_table_privilege('anon', c.oid, 'MAINTAIN')) "
                "ORDER BY c.relname",
            )
            offenders += [f"{r[0]} (MAINTAIN)" for r in cur.fetchall()]
    assert offenders == [], (
        f"relation(s) still writable (or MAINTAIN-able) by a browser role: {offenders}"
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
