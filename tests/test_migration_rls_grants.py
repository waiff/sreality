"""CI gate: keep Phase-0's anon/authenticated posture from silently drifting as
the append-only migration cadence adds new files.

The grant/RLS posture is UNTESTABLE on the `_FakeConn` DB tests (they can't see
GRANTs, default ACLs or RLS), so — exactly like tests/test_sql_placeholders.py —
this is a fast, offline, regex gate over the migration SQL text. It enforces four
rules for every migration numbered >= MIN_ENFORCED (history is fixed-forward,
architecture rule #1, so older migrations are never rewritten):

  1. No migration grants a WRITE privilege (INSERT/UPDATE/DELETE/TRUNCATE/ALL) to
     `anon` — anon is dark after Phase 0 (migration 299).
  2. No migration grants a WRITE privilege to `authenticated` EXCEPT on the 19
     user-state TENANT tables, whose per-account DML is deliberate and RLS-scoped
     (migrations 290-294,298). This registry MIRRORS tests/test_tenant_isolation_
     live.py::_TENANT_TABLES — add a new tenant table to BOTH.
  3. Every new base table ships with `enable row level security` in the same
     migration (Supabase's default ACL would otherwise make an RLS-off public
     table reachable). Escape hatch: `-- ci-allow-no-rls: <table> <reason>`.
  4. No migration re-grants the Amendment-A6 broker-directory PII surfaces to a
     browser role before Wave 4 masks them (they were revoked in migration 299).
  5. No migration creates a view/function that reads admin-only operational data
     without embedding `is_platform_admin()` (migrations 318/332). Enforced from
     MIN_VIEW_GATE. Escape hatch: `-- ci-allow-ungated: <name> <reason>`.

Live cross-tenant / effective-grant verification lands in the TEST_DATABASE_URL
lane (tests/test_tenant_isolation_live.py), which this composes with.
"""
from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
# Phase 0 (299) is the baseline: it establishes the posture this gate protects.
# Migrations 286-298 legitimately grant `authenticated` DML on tenant tables and
# predate the gate, so enforcement starts at 299.
MIN_ENFORCED = 299
# Rule 5 starts after the remediation batch that established the posture
# (329-332); 318's own 26 objects predate it and are covered by the enumerated
# lists in the live lane.
MIN_VIEW_GATE = 333

_WRITE_PRIVS = {"insert", "update", "delete", "truncate", "all", "all privileges"}
# `public` is included because anon/authenticated INHERIT every grant made to PUBLIC
# (the exact vector Phase 0 closed on functions) — a `grant insert ... to public`
# would hand anon write access while naming neither browser role.
_ROLES = ("anon", "authenticated", "public")

# Mirrors tests/test_tenant_isolation_live.py::_TENANT_TABLES — the user-state
# tables whose per-account `authenticated` DML is intentional and RLS-guarded.
_TENANT_TABLES = frozenset({
    "collections", "tags", "property_notes", "filter_presets",
    "notification_subscriptions", "manual_rental_estimates", "collection_properties",
    "property_tags", "notification_dispatches", "estimation_cohort_entries",
    "estimation_trace_payloads", "estimation_feedback", "building_run_attachments",
    "estimation_runs", "building_runs", "property_pipeline", "pipeline_stages",
    "property_pipeline_events", "entitlements",
})

# Amendment A6: these broker-directory PII surfaces stay dark to browser roles
# until Wave 4 ships masked columns. A migration must not re-grant them.
_BROKER_A6_SURFACES = frozenset({
    "brokers_public", "broker_leaderboard", "broker_firm_memberships_public",
    "broker_listings_public", "listing_broker_public", "broker_geo_options",
    "broker_resolution_runs_public", "broker_region_type_stats",
})


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", sql)


def _migration_number(path: Path) -> int | None:
    m = re.match(r"^(\d+)_", path.name)
    return int(m.group(1)) if m else None


def _statements(sql: str) -> list[str]:
    return [s.strip() for s in _strip_comments(sql).split(";") if s.strip()]


def _enforced_migrations() -> list[Path]:
    return [
        p for p in sorted(MIGRATIONS_DIR.glob("*.sql"))
        if (n := _migration_number(p)) is not None and n >= MIN_ENFORCED
    ]


def _parse_grant(stmt: str) -> tuple[set[str], list[str], set[str]] | None:
    """(-> privs, objects, roles) for a `grant <privs> on <objs> to <roles>`
    statement, else None. Object names are lowercased + stripped of the leading
    `table`/`public.`/quotes; a `sequence`/`function`/`all …` object is returned
    verbatim so callers can ignore it."""
    low = re.sub(r"\s+", " ", stmt.lower()).strip()
    m = re.match(r"grant (.+?) on (.+?) to (.+)$", low)
    if not m:
        return None
    privs = {p.strip() for p in m.group(1).split(",") if p.strip()}
    roles = {r.strip() for r in re.split(r"[,\s]+", m.group(3)) if r.strip()}
    raw_objs = m.group(2).strip()
    objs: list[str] = []
    for obj in raw_objs.split(","):
        obj = obj.strip()
        obj = re.sub(r'^(table|sequence|function)\s+', "", obj)
        obj = obj.replace("public.", "").strip().strip('"')
        objs.append(obj)
    return privs, objs, roles


def _offending_write_grants(sql: str) -> list[str]:
    out: list[str] = []
    for stmt in _statements(sql):
        parsed = _parse_grant(stmt)
        if not parsed:
            continue
        privs, objs, roles = parsed
        if not (privs & _WRITE_PRIVS):
            continue
        collapsed = re.sub(r"\s+", " ", stmt.strip())
        if "anon" in roles:
            out.append(f"WRITE to anon: {collapsed}")
        if "public" in roles:
            out.append(f"WRITE to public (anon/authenticated inherit it): {collapsed}")
        if "authenticated" in roles:
            # allowed only on the tenant registry (and only when EVERY target is
            # a registry table — a mixed grant is rejected to force an explicit split)
            if not objs or any(o not in _TENANT_TABLES for o in objs):
                out.append(f"WRITE to authenticated on non-tenant table: {collapsed}")
    return out


def _broker_regrants(sql: str) -> list[str]:
    out: list[str] = []
    for stmt in _statements(sql):
        parsed = _parse_grant(stmt)
        if not parsed:
            continue
        _privs, objs, roles = parsed
        if not (roles & set(_ROLES)):
            continue
        # a function object keeps its `(args)` — compare on the base name
        base_names = {o.split("(", 1)[0].strip() for o in objs}
        if base_names & _BROKER_A6_SURFACES:
            out.append(re.sub(r"\s+", " ", stmt.strip()))
    return out


def _created_base_tables(sql: str) -> list[str]:
    names: list[str] = []
    for stmt in _statements(sql):
        low = re.sub(r"\s+", " ", stmt.lower())
        if not low.startswith("create "):
            continue
        if re.match(r"create (or replace )?(materialized view|view|temp|temporary|foreign)", low):
            continue
        m = re.match(r'create (?:unlogged )?table (?:if not exists )?(?:public\.)?"?([a-z0-9_]+)"?', low)
        if m:
            names.append(m.group(1))
    return names


def _rls_enabled_tables(sql: str) -> set[str]:
    return {
        m.group(1) for m in re.finditer(
            r'alter table (?:if exists )?(?:public\.)?"?([a-z0-9_]+)"?\s+enable row level security',
            _strip_comments(sql).lower(),
        )
    }


def _rls_exempt_tables(sql: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"--\s*ci-allow-no-rls:\s*([a-z0-9_]+)", sql.lower())}


# Admin-only operational relations. MIRRORS tests/test_tenant_isolation_live.py::
# _ADMIN_ONLY_RELATIONS — add a new admin table/matview to BOTH. listings/
# properties/images are deliberately absent (shared-market data behind many
# legitimately open views); see that file for the full reasoning.
_ADMIN_ONLY_RELATIONS = frozenset({
    "dedup_engine_runs", "dedup_scan_state", "dedup_vision_bakeoff_results",
    "dedup_decision_feedback", "property_identity_candidates", "property_merge_events",
    "listing_detail_queue", "listing_fetch_failures", "detail_queue_completions",
    "llm_calls", "parsed_url_cache", "phash_pair_notes", "pipeline_check_results",
    "image_border_cases", "image_tag_annotations", "image_training_examples",
    "workflow_failures", "workflow_run_health",
    "health_summary_mv", "portal_health_mv", "scraper_health_checks_mv",
    "category_trends_mv", "image_storage_overview_mv", "snapshot_churn_24h_mv",
    "dedup_funnel_resolutions_mv", "dedup_llm_cost_by_category_mv",
    "images_failure_overview_mv",
})

_CREATE_GATED_OBJ = re.compile(
    r"create (?:or replace )?(?:materialized )?(?:view|function) "
    r'(?:if not exists )?(?:public\.)?"?([a-z0-9_]+)"?',
)


def _ungated_admin_objects(sql: str) -> list[str]:
    """Views/functions created over admin-only data with no is_platform_admin()."""
    exempt = {m.group(1) for m in re.finditer(r"--\s*ci-allow-ungated:\s*([a-z0-9_]+)", sql.lower())}
    out: list[str] = []
    for stmt in _statements(_strip_comments(sql)):
        low = re.sub(r"\s+", " ", stmt.lower()).strip()
        m = _CREATE_GATED_OBJ.match(low)
        if not m or m.group(1) in exempt:
            continue
        if "is_platform_admin()" in low:
            continue
        reads = sorted(
            t for t in _ADMIN_ONLY_RELATIONS
            if re.search(rf"\b{re.escape(t)}\b", low)
        )
        if reads:
            out.append(f"{m.group(1)} reads {', '.join(reads)}")
    return out


def test_new_admin_objects_embed_the_gate():
    offenders = [
        f"  {p.name}: {o}"
        for p in _enforced_migrations()
        if (n := _migration_number(p)) is not None and n >= MIN_VIEW_GATE
        for o in _ungated_admin_objects(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "view/function(s) read admin-only operational data with no "
        "is_platform_admin() gate — any signed-in non-admin could read them over "
        "supabase-js (SPA route-gating is not a security boundary). Add the gate, "
        "or annotate `-- ci-allow-ungated: <name> <why>`:\n" + "\n".join(offenders)
    )


def test_no_write_grants_to_browser_roles():
    offenders = [
        f"  {p.name}: {o}"
        for p in _enforced_migrations()
        for o in _offending_write_grants(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "Migration(s) grant a WRITE privilege to anon, or to authenticated on a "
        "non-tenant table — browser roles get SELECT only; shared-market writes go "
        "through the bearer-gated API, tenant writes are RLS-scoped on the 19 "
        "registry tables (mirror tests/test_tenant_isolation_live.py):\n"
        + "\n".join(offenders)
    )


def test_new_base_tables_enable_rls():
    # Cross-file (not per-file): a table must be RLS-enabled by SOME enforced
    # migration, not necessarily the one that creates it. Concurrent lanes routinely
    # split create (migration N) from a hardening ALTER (N+1) — a per-file rule would
    # false-flag N forever since N is append-only. Still catches a table that is
    # never RLS-enabled anywhere in the enforced range. Which migration each table was
    # created in is tracked for the error message.
    created: dict[str, str] = {}
    rls_on: set[str] = set()
    exempt: set[str] = set()
    for p in _enforced_migrations():
        sql = p.read_text(encoding="utf-8")
        for tbl in _created_base_tables(sql):
            created.setdefault(tbl, p.name)
        rls_on |= _rls_enabled_tables(sql)
        exempt |= _rls_exempt_tables(sql)
    offenders = sorted(
        f"  {origin}: table '{tbl}' never gets `enable row level security`"
        for tbl, origin in created.items()
        if tbl not in rls_on and tbl not in exempt
    )
    assert not offenders, (
        "New base table(s) created without `enable row level security` in any enforced "
        "migration (Supabase's default ACL makes an RLS-off public table reachable if it "
        "is ever granted). Add `alter table <t> enable row level security;` in this or a "
        "follow-up migration, or annotate `-- ci-allow-no-rls: <table> <reason>`:\n"
        + "\n".join(offenders)
    )


def test_no_broker_a6_regrants():
    offenders = [
        f"  {p.name}: {o}"
        for p in _enforced_migrations()
        for o in _broker_regrants(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "Migration(s) grant a broker-directory PII surface to anon/authenticated. "
        "These stay dark to browser roles until Wave 4 ships masked columns "
        "(Amendment A6). Remove the grant or gate it behind the Wave-4 masking:\n"
        + "\n".join(offenders)
    )


def test_gate_actually_scans_migrations():
    assert MIGRATIONS_DIR.is_dir(), f"migrations dir not found: {MIGRATIONS_DIR}"
    assert len(_enforced_migrations()) >= 1, f"no migrations at/after {MIN_ENFORCED}"
