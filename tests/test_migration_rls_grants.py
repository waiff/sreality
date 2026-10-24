"""CI gate: keep Phase-0's anon/authenticated posture from silently drifting as
the append-only migration cadence adds new files.

The grant/RLS posture is UNTESTABLE on the `_FakeConn` DB tests (they can't see
GRANTs, default ACLs or RLS), so — exactly like tests/test_sql_placeholders.py —
this is a fast, offline, regex gate over the migration SQL text. It enforces four
rules for every migration numbered >= MIN_ENFORCED (history is fixed-forward,
architecture rule #1, so older migrations are never rewritten):

  1. No migration grants a WRITE privilege (INSERT/UPDATE/DELETE/TRUNCATE/ALL) to
     `anon` — anon is dark after Phase 0 (migration 299).
  2. No migration grants a WRITE privilege to `authenticated` EXCEPT on the
     user-state TENANT tables, whose per-account DML is deliberate and RLS-scoped.
     The list is tests/_tenant_registry.py, shared with the live lane — add a new
     tenant table there.
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

from tests._admin_gate_shape import gate_is_sound
from tests._tenant_registry import RENAMED_TENANT_TABLES, TENANT_TABLES

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
# Phase 0 (299) is the baseline: it establishes the posture this gate protects.
# Migrations 286-298 legitimately grant `authenticated` DML on tenant tables and
# predate the gate, so enforcement starts at 299.
MIN_ENFORCED = 299

_WRITE_PRIVS = {"insert", "update", "delete", "truncate", "all", "all privileges"}
# `public` is included because anon/authenticated INHERIT every grant made to PUBLIC
# (the exact vector Phase 0 closed on functions) — a `grant insert ... to public`
# would hand anon write access while naming neither browser role.
_ROLES = ("anon", "authenticated", "public")

# Amendment A6: these broker-directory PII surfaces stay dark to browser roles;
# the SPA reads them only through the PII-masking /brokers API. A migration must
# not re-grant them.
#
# MIRRORS tests/test_tenant_isolation_live.py::_BROKER_PII_RELATIONS -- add a new
# broker surface to BOTH. This gate scans migration SQL text, so it sees only
# literal `grant` statements; the live one probes the effective privilege and is
# what catches default-ACL drift or a blanket `grant ... on all tables`.
_BROKER_A6_SURFACES = frozenset({
    "brokers_public", "broker_leaderboard", "broker_firm_memberships_public",
    "broker_listings_public", "listing_broker_public", "broker_geo_options",
    "broker_resolution_runs_public", "broker_region_type_stats",
    # 299's PART F missed this one; its `authenticated` SELECT came from the
    # default ACL at CREATE time, not from any grant statement, so it survived the
    # sweep and stayed live until migration 395 revoked it.
    "firms_public",
})


_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _strip_comments(sql: str) -> str:
    """Remove -- and /* */ comments that are NOT inside a string or dollar-quote.

    The previous regex form ran over the raw text, so a literal containing `/*` and a
    later literal containing `*/` made the non-greedy match swallow everything between
    them — including whole CREATE statements, which then became invisible to every rule
    in this file. Copy string and $tag$ bodies verbatim instead.
    """
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


def _migration_number(path: Path) -> int | None:
    m = re.match(r"^(\d+)_", path.name)
    return int(m.group(1)) if m else None


def _statements(sql: str) -> list[str]:
    """Split on `;` OUTSIDE strings and dollar-quotes, so a function body or DO block
    stays ONE statement instead of shattering at every internal semicolon."""
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
    # A column list belongs to its privilege: split on its commas, `update (a, b)` read as
    # the two non-privileges `update (a` and `b)`, so a column-level write grant passed.
    bare = re.sub(r"\s*\([^)]*\)", "", m.group(1))
    privs = {p.strip() for p in bare.split(",") if p.strip()}
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
            if not objs or any(o not in TENANT_TABLES and o not in RENAMED_TENANT_TABLES
                               for o in objs):
                out.append(f"WRITE to authenticated on non-tenant table: {collapsed}")
    return out


def _revokes(stmt: str, obj: str, role: str) -> bool:
    """True for a `revoke <privs> on <obj> from <... role ...>` naming both."""
    low = re.sub(r"\s+", " ", stmt.lower()).strip()
    m = re.match(r"revoke (?:grant option for )?(?:.+?) on (.+?) from (.+)$", low)
    if not m:
        return False
    raw = re.sub(r"^(table|sequence|function)\s+", "", m.group(1).strip())
    objs = {o.strip().strip('"').replace("public.", "") for o in raw.split(",")}
    roles = {r.strip() for r in re.split(r"[,\s]+", m.group(2)) if r.strip()}
    return obj in objs and role in roles


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
        m = re.match(
            r'create (?:unlogged )?table (?:if not exists )?(?:"?([a-z0-9_]+)"?\.)?"?([a-z0-9_]+)"?',
            low,
        )
        if m:
            names.append(_qualify(m.group(1), m.group(2)))
    return names


def _qualify(schema: str | None, name: str) -> str:
    """Bare name for `public`/unqualified tables (preserves every existing
    exemption-list entry, which was written before non-public schemas
    existed); `schema.table` for anything else, e.g. `dedup_sim.settings` —
    so a create/ALTER pair in a non-public schema still correlates."""
    return name if schema in (None, "public") else f"{schema}.{name}"


def _rls_enabled_tables(sql: str) -> set[str]:
    return {
        _qualify(m.group(1), m.group(2)) for m in re.finditer(
            r'alter table (?:if exists )?(?:"?([a-z0-9_]+)"?\.)?"?([a-z0-9_]+)"?\s+enable row level security',
            _strip_comments(sql).lower(),
        )
    }


def _rls_exempt_tables(sql: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"--\s*ci-allow-no-rls:\s*([a-z0-9_]+)", sql.lower())}


# Admin-only operational relations. MIRRORS tests/test_tenant_isolation_live.py::
# _ADMIN_ONLY_RELATIONS — add a new admin table/matview to BOTH. listings/
# properties/images are deliberately absent (shared-market data behind many
# legitimately open views); see that file for the full reasoning.
#
# tag_taxonomy + image_tag_labels (migration 442) replace image_training_examples
# here: they are the tag ground truth now, and — like dedup_sim.taxonomy_labels /
# label_proposals before them, and unlike migration 309 — they are backend-only,
# with NO `_public` view. image_tag_annotations / phash_pair_notes came out with
# image_training_examples: no app code reads or writes any of the three since the
# tag-annotation-matrix cutover (docs/design/tag-annotation-matrix.md); the tables
# themselves are dropped by that design's Wave C.
_ADMIN_ONLY_RELATIONS = frozenset({
    "dedup_engine_runs", "dedup_scan_state", "dedup_vision_bakeoff_results",
    "dedup_decision_feedback", "property_identity_candidates", "property_merge_events",
    # The carry record (migration 589): read only by the merge route, the brake's dry run
    # and the split, all service-role; no `_public` view.
    "property_merge_carries",
    "listing_detail_queue", "listing_fetch_failures", "detail_queue_completions",
    "llm_calls", "pipeline_check_results",
    "image_border_cases", "tag_taxonomy", "image_tag_labels",
    # image_tag_label_events (migration 446) is the append-only decision history
    # behind image_tag_labels — same backend-only posture, no `_public` view.
    "image_tag_label_events",
    # tag_candidates (450) is the per-tag review queue; tag_exam_cohorts /
    # tag_exam_members (458) are the sealed exam roster. All three are
    # backend-only with NO `_public` view — and publishing the exam roster would
    # hand anyone the answer key's index.
    "tag_candidates", "tag_exam_cohorts", "tag_exam_members", "tag_exam_sets",
    # tag_exam_screens (459) + tag_exam_suggestions (461): the screener's guesses
    # and the sitting pre-answers — machine output about the answer key, backend-
    # only, no `_public` view.
    "tag_exam_screens", "tag_exam_suggestions",
    "workflow_failures", "workflow_run_health",
    "scrape_runs", "worker_heartbeats",
    "health_summary_mv", "portal_health_mv", "scraper_health_checks_mv",
    "category_trends_mv", "image_storage_overview_mv", "snapshot_churn_24h_mv",
    "dedup_funnel_resolutions_archive",
    "images_failure_overview_mv",
    # The tagging bake-off store (migration 489, schema dedup_sim): experiment
    # evidence — runs, encoder arms, their vectors, per-image scores and the
    # per-head metrics. Backend-only, NO `_public` view; the bake-off page reads
    # them through the admin-gated API (/new-dedup/tagging-bakeoff/*).
    "dedup_sim.tag_head_bakeoff_runs",
    "dedup_sim.tag_head_bakeoff_arms",
    "dedup_sim.tag_head_bakeoff_vectors",
    "dedup_sim.tag_head_bakeoff_scores",
    "dedup_sim.tag_head_bakeoff_metrics",
    # The versioned tag model (migration 490, PUBLIC schema — it must survive
    # Wave 8's drop of dedup_sim): the model registry, its per-tag artifacts, and
    # the per-image winner store. Locked to the service role; the admin pages
    # read them through the admin-gated API (/new-dedup/tags/*). One deliberate
    # exception: images_public.tag_head_scores (migration 591, ci-allow-ungated)
    # publishes the ACTIVE model's per-photo scores for the Browse card cover.
    "tag_head_models", "tag_head_model_heads", "image_tag_scores",
    # The Level-0 candidate store (migration 492, schema dedup_sim): parameter
    # sets, generation runs and the listing-pair evidence rows. Backend-only, NO
    # `_public` view; the Candidate audit page reads them through the admin-gated
    # API (/new-dedup/candidates/*).
    "dedup_sim.candidate_inputs",
    "dedup_sim.candidate_generations",
    "dedup_sim.candidate_pairs",
    # The autonomous dedup engine's own store (migration 528, schema autodedup):
    # fingerprints, the blocking substrate, pair/cluster grain, the judge queue and the
    # operator verdicts, the learning tables, and the operator-facing iteration log.
    # Backend-only, NO `_public` view — the browser client is pinned to schema `public`,
    # so the progress page and the validation UI read them through the admin-gated API.
    # `autodedup.merges` is the reserved, EMPTY write ledger (the trial is shadow mode).
    "autodedup.settings",
    "autodedup.runs",
    "autodedup.iterations",
    "autodedup.listing_fp",
    "autodedup.image_band",
    "autodedup.phash_pop",
    "autodedup.exploded_blocks",
    "autodedup.pairs",
    "autodedup.judgements",
    "autodedup.must_not_link",
    "autodedup.clusters",
    "autodedup.cluster_members",
    "autodedup.cluster_conflicts",
    "autodedup.merges",
    "autodedup.verdicts",
    "autodedup.labels",
    "autodedup.models",
    "autodedup.eval_samples",
    "autodedup.resolve_queue",
    "autodedup.scan_cursor",
    "autodedup.judge_queue",
    # A1's apply ledger (migration 558): what the engine merged in production, dry runs and
    # refusals included. Backend-only like the rest of the schema.
    "autodedup.applied_merges",
    # The operator's own Browse merges, copied once into the engine's schema (migration 564,
    # E299) so nothing reads the production ledger at evaluation time. Backend-only.
    "autodedup.operator_merges",
    # The sold-transaction fetch LEDGER (migration 542). The sales themselves are market
    # data behind `sold_transactions_public`; the ledger is not. A cell is fetched only
    # where some account holds a live deal-pipeline card, so its obec set is a projection
    # of tenant state — publishing it would hand every signed-in account the map and the
    # cadence of every other account's deal sourcing. NO `_public` view; the ONE reader
    # is migration 545's `sold_coverage`, a SECURITY DEFINER point lookup that answers
    # about a single municipality and is annotated `ci-allow-ungated` there.
    "sold_transaction_fetches",
    # Auth identities (migration 610): emails are PII. The ONE reader is account_email(),
    # EXECUTE revoked from every browser role and annotated `ci-allow-ungated` there; the
    # browser reaches a label only through share_party_emails(), counterparties only.
    "auth.users",
    # The health check registry and the incident table (migrations 619 / 462): the browser
    # reads both only through the admin-gated pipeline_checks_public.
    "health_checks",
    "ops_incidents",
    # The push device registry and its delivery ledger (migration 654): service role only, NO
    # `_public` view; the /push routes and the worker's push lane are their one writer each.
    "push_subscriptions",
    "notification_deliveries",
})

_CREATE_GATED_OBJ = re.compile(
    r"create (?:or replace )?(?:materialized )?(?:view|function) "
    r'(?:if not exists )?(?:public\.)?"?([a-z0-9_]+)"?',
)


_DYNAMIC_DDL = re.compile(
    r"execute\s+(?:format\s*\()?\s*['$].{0,400}?create\s+(?:or\s+replace\s+)?"
    r"(?:materialized\s+)?(?:view|function|table)",
    re.IGNORECASE | re.DOTALL,
)
# Migration 299 legitimately builds browse_list_next / properties_map_mv_next through
# dynamic EXECUTE; it predates this rule and is fixed-forward (architecture rule #1).
_DYNAMIC_DDL_HISTORICAL = frozenset({"299_phase0_anon_hardening.sql"})

# "<file>:<object>" created ungated before migration 318 and re-created WITH the gate
# BY migration 318 — verified sound in production today. Migrations are append-only, so
# the exemption lives here rather than in a rewritten file.
_HISTORICAL_UNGATED = frozenset({
    "300_dedup_golden_set_foundation.sql:dedup_label_events",
    "303_dedup_vision_bakeoff_results.sql:dedup_vision_bakeoff_results_public",
    "307_dedup_recency_instrumentation.sql:dedup_recency_backlog",
    "308_clip_phash_audit_tooling.sql:image_tag_annotations_public",
    "308_clip_phash_audit_tooling.sql:phash_pair_notes_public",
    "309_clip_training_examples.sql:image_training_examples_public",
    "310_image_border_cases.sql:image_border_cases_public",
    "311_source_trust_rank_and_sign_check.sql:dedup_label_events",
})


def _ungated_admin_objects(sql: str) -> list[tuple[str, str]]:
    """(name, 'reads a, b') for each view/function created over admin-only data whose
    is_platform_admin() gate is absent OR unsound — moved out of a boolean position,
    OR'd into a predicate, or turned into a tautology."""
    exempt = {m.group(1) for m in re.finditer(r"--\s*ci-allow-ungated:\s*([a-z0-9_]+)", sql.lower())}
    out: list[tuple[str, str]] = []
    for stmt in _statements(sql):
        low = re.sub(r"\s+", " ", stmt.lower()).strip()
        m = _CREATE_GATED_OBJ.match(low)
        if not m or m.group(1) in exempt:
            continue
        kind = "function" if re.match(
            r"create (?:or replace )?(?:materialized )?function", low) else "view"
        if gate_is_sound(low, kind):
            continue
        reads = sorted(
            t for t in _ADMIN_ONLY_RELATIONS
            if re.search(rf"\b{re.escape(t)}\b", low)
        )
        if reads:
            out.append((m.group(1), ", ".join(reads)))
    return out


def _dynamic_ddl(sql: str) -> bool:
    """True if the migration builds DDL through EXECUTE, which the statement scanner
    cannot see into — the CREATE lives inside a string argument."""
    if re.search(r"--\s*ci-allow-dynamic:", sql, re.IGNORECASE):
        return False
    return bool(_DYNAMIC_DDL.search(_strip_comments(sql)))


def test_new_admin_objects_embed_the_gate():
    offenders = [
        f"  {p.name}: {name} reads {reads}"
        for p in _enforced_migrations()
        for name, reads in _ungated_admin_objects(p.read_text(encoding="utf-8"))
        if f"{p.name}:{name}" not in _HISTORICAL_UNGATED
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
        "through the bearer-gated API, tenant writes are RLS-scoped on the "
        f"{len(TENANT_TABLES)} tables of tests/_tenant_registry.py:\n"
        + "\n".join(offenders)
    )


def test_a_column_list_never_hides_a_write_grant():
    assert _parse_grant("grant update (a, b) on public.t to anon")[0] == {"update"}
    assert _parse_grant("grant select (id), insert (a, b) on t to anon")[0] == {"select", "insert"}
    assert _offending_write_grants("grant update (seen_at, note) on public.listings to anon;")
    assert _offending_write_grants("grant update (a) on public.listings to authenticated;")
    assert not _offending_write_grants("grant select (id, name) on public.listings to anon;")
    # a column-level UPDATE on a tenant table stays allowed (migrations 536, 599, 619)
    assert not _offending_write_grants(
        "grant update (seen_at, estimation_run_id) on public.notification_dispatches "
        "to authenticated;")


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


def test_firms_public_is_revoked_and_registered():
    """firms_public is the A6 surface migration 299 missed.

    Its `authenticated` SELECT came from Supabase's default ACL at CREATE time
    (migration 187) rather than from a grant statement, so PART F's relation list
    never covered it and it stayed browser-readable while every sibling broker
    surface went dark. Two halves, both required: a migration that actually revokes
    it, and its presence in the registry above so `test_no_broker_a6_regrants`
    blocks a future re-grant.
    """
    assert "firms_public" in _BROKER_A6_SURFACES

    revoking = [
        p.name
        for p in sorted(MIGRATIONS_DIR.glob("*.sql"))
        for stmt in _statements(p.read_text(encoding="utf-8"))
        if _revokes(stmt, "firms_public", "authenticated")
    ]
    assert revoking, (
        "No migration revokes firms_public from `authenticated`. The grant is live "
        "in production (default-ACL drift, verified 2026-08-12) — a migration must "
        "revoke it, or a fresh rebuild silently republishes the firm rollup."
    )


def test_gate_actually_scans_migrations():
    assert MIGRATIONS_DIR.is_dir(), f"migrations dir not found: {MIGRATIONS_DIR}"
    assert len(_enforced_migrations()) >= 1, f"no migrations at/after {MIN_ENFORCED}"


def test_dynamic_ddl_is_annotated():
    """DDL built through `EXECUTE format('create ...')` is invisible to the statement
    scanner above, so every rule in this file silently skips it. A migration that needs
    dynamic DDL must say so with `-- ci-allow-dynamic: <name> <why>`, which makes the
    blind spot reviewer-visible instead of accidental."""
    offenders = [
        f"  {p.name}"
        for p in _enforced_migrations()
        if p.name not in _DYNAMIC_DDL_HISTORICAL
        and _dynamic_ddl(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "migration(s) build DDL via EXECUTE, which the offline scanner cannot inspect. "
        "Annotate with `-- ci-allow-dynamic: <name> <why>`:\n" + "\n".join(offenders)
    )


# --- the sharing policies (migrations 610, 641, 647), pinned ----------------
# A collection share widens READS only, through exactly these five permissive policies, and a
# dashboard share (647) through its one; a pipeline share widens nothing (its recipient ACTS AS
# the owner, below). Every party policy hides itself while acting. An `alter policy` that swaps only
# USING / WITH CHECK keeps the pinned shape for the PARTY policies alone (641 adds `not acting()`
# that way, and test_the_share_rows_are_invisible_while_acting checks their final USING); any alter
# of a `*_shared_read` still fails here.
# 610's own assert block checks them once, when 610 applies; this census re-reads EVERY
# migration so a later one that re-creates a `*_shared_read` as FOR ALL (WITH CHECK defaults
# to USING: a recipient could write) or drops `to authenticated` (anon reaches it), or that
# alters or drops one, fails here instead of shipping green.
_SHARE_POLICY_PIN: dict[str, tuple[str, str, frozenset[str]]] = {
    "collection_shares_party": ("collection_shares", "all", frozenset({"authenticated"})),
    "pipeline_shares_party": ("pipeline_shares", "all", frozenset({"authenticated"})),
    "stats_dashboard_shares_party": ("stats_dashboard_shares", "all", frozenset({"authenticated"})),
    "stats_dashboards_shared_read": ("stats_dashboards", "select", frozenset({"authenticated"})),
    "collections_shared_read": ("collections", "select", frozenset({"authenticated"})),
    "collection_properties_shared_read": (
        "collection_properties", "select", frozenset({"authenticated"})),
    "property_notes_shared_read": ("property_notes", "select", frozenset({"authenticated"})),
    "property_attachments_shared_read": (
        "property_attachments", "select", frozenset({"authenticated"})),
}


def _policy_shape(rest: str) -> tuple[str, frozenset[str], bool]:
    head = re.split(r"\b(?:using|with check)\b", rest, maxsplit=1)[0]
    cmd = re.search(r"\bfor (all|select|insert|update|delete)\b", head)
    roles = re.search(r"\bto (.+)$", head.strip())
    return (
        cmd.group(1) if cmd else "all",
        frozenset(r.strip() for r in roles.group(1).split(",")) if roles else frozenset({"public"}),
        "as restrictive" in head,
    )


def _only_swaps_expressions(rest: str) -> bool:
    """`on <table> using (...) [with check (...)]`: no rename, no role change."""
    return bool(re.match(r'\s*on (?:public\.)?"?\w+"? (?:using|with check)\b', rest)) and not re.search(
        r"\brename to\b", rest)


def _final_share_policies() -> dict[str, object]:
    state: dict[str, object] = {}
    paths = sorted(
        (p for p in MIGRATIONS_DIR.glob("*.sql") if _migration_number(p) is not None),
        key=lambda p: _migration_number(p) or 0,
    )
    for p in paths:
        for stmt in _statements(p.read_text(encoding="utf-8")):
            low = re.sub(r"\s+", " ", stmt.lower()).strip()
            m = re.match(r'create policy "?(\w+)"? on (?:public\.)?"?(\w+)"?(.*)$', low)
            if m and m.group(1) in _SHARE_POLICY_PIN:
                cmd, roles, restrictive = _policy_shape(m.group(3))
                state[m.group(1)] = (m.group(2), cmd, roles, restrictive, p.name)
                continue
            m = re.match(r'(drop|alter) policy (?:if exists )?"?(\w+)"?(.*)$', low)
            if m and m.group(2) in _SHARE_POLICY_PIN:
                if (m.group(1) == "alter" and m.group(2) in _HIDDEN_WHILE_ACTING
                        and _only_swaps_expressions(m.group(3))):
                    continue
                state[m.group(2)] = f"{m.group(1)} policy in {p.name}"
    return state


def test_the_sharing_policies_stay_select_only_and_authenticated_only():
    state = _final_share_policies()
    assert set(state) == set(_SHARE_POLICY_PIN), "the census lost sight of migration 610"
    drifted = []
    for name, (table, cmd, roles) in _SHARE_POLICY_PIN.items():
        got = state[name]
        if not isinstance(got, tuple):
            drifted.append(f"  {name}: {got}")
        elif got[:4] != (table, cmd, roles, False):
            drifted.append(f"  {name} ({got[4]}): on {got[0]} for {got[1]} to {sorted(got[2])}"
                           f"{' restrictive' if got[3] else ''}; pinned {table}/{cmd}/{sorted(roles)}")
    assert not drifted, (
        "a sharing policy drifted from its pin (docs/design/collection-sharing.md §3.1, §7): a "
        "share must widen reads only, to signed-in callers only. If the change is deliberate, "
        "update _SHARE_POLICY_PIN in the same PR:\n" + "\n".join(drifted)
    )


# --- acting as the owner (migration 641), pinned ------------------------------
# A pipeline share lets its recipient ACT AS the owner on exactly the eleven curation tables:
# their FOR ALL policies read acting_account_ids() (who the caller acts FOR) in USING and WITH
# CHECK; the three pipeline tables pass `true`, the only ones a READ share reaches (ruling 2). Every other policy keeps current_account_ids() (who the caller IS): billing, estimations,
# watchdog, presets, notifications and the share tables never act (docs/design/pipeline-sharing.md
# §3.7). A later migration that moves a twelfth policy onto the acting function, or moves one of
# the eleven back, fails here instead of shipping green. Both share-party policies hide while acting.
_ACTING_POLICIES: dict[str, str] = {
    "collections_tenant_rw": "collections",
    "tags_tenant_rw": "tags",
    "property_notes_tenant_rw": "property_notes",
    "collection_properties_tenant_rw": "collection_properties",
    "property_tags_tenant_rw": "property_tags",
    "property_attachments_tenant_rw": "property_attachments",
    "property_artifact_links_tenant_rw": "property_artifact_links",
    "property_dismissals_tenant_rw": "property_dismissals",
    "property_pipeline_tenant_rw": "property_pipeline",
    "pipeline_stages_tenant_rw": "pipeline_stages",
    "property_pipeline_events_tenant_rw": "property_pipeline_events",
}
_BOARD_POLICIES = frozenset({
    "property_pipeline_tenant_rw", "pipeline_stages_tenant_rw", "property_pipeline_events_tenant_rw",
})
_HIDDEN_WHILE_ACTING = ("collection_shares_party", "pipeline_shares_party", "stats_dashboard_shares_party")


def _clause(rest: str, keyword: str) -> str | None:
    """The parenthesised expression after `using` / `with check` in a policy tail, else None."""
    m = re.search(rf"\b{keyword}\s*\(", rest)
    if not m:
        return None
    depth, i = 1, m.end()
    while i < len(rest) and depth:
        depth += {"(": 1, ")": -1}.get(rest[i], 0)
        i += 1
    return rest[m.end(): i - 1]


def _final_policy_expressions() -> dict[str, tuple[str, str | None, str | None]]:
    """name -> (table, USING, WITH CHECK) as the migrations leave them, in numeric order."""
    state: dict[str, tuple[str, str | None, str | None]] = {}
    paths = sorted(
        (p for p in MIGRATIONS_DIR.glob("*.sql") if _migration_number(p) is not None),
        key=lambda p: _migration_number(p) or 0,
    )
    for p in paths:
        for stmt in _statements(p.read_text(encoding="utf-8")):
            low = re.sub(r"\s+", " ", stmt.lower()).strip()
            m = re.match(r'create policy "?(\w+)"? on (?:public\.)?"?(\w+)"?(.*)$', low)
            if m:
                state[m.group(1)] = (m.group(2), _clause(m.group(3), "using"),
                                     _clause(m.group(3), "with check"))
                continue
            m = re.match(r'alter policy "?(\w+)"? on (?:public\.)?"?(\w+)"?(.*)$', low)
            if m and m.group(1) in state:
                _, using, check = state[m.group(1)]
                state[m.group(1)] = (m.group(2), _clause(m.group(3), "using") or using,
                                     _clause(m.group(3), "with check") or check)
                continue
            m = re.match(r'drop policy (?:if exists )?"?(\w+)"?', low)
            if m:
                state.pop(m.group(1), None)
    return state


def test_exactly_the_eleven_curation_policies_act():
    state = _final_policy_expressions()
    wrong = []
    for name, table in _ACTING_POLICIES.items():
        got = state.get(name)
        if got is None or got[0] != table:
            wrong.append(f"  {name}: missing on {table}")
            continue
        call = "acting_account_ids(true)" if name in _BOARD_POLICIES else "acting_account_ids()"
        for side, expr in (("USING", got[1]), ("WITH CHECK", got[2])):
            if not expr or call not in expr or "current_account_ids" in expr:
                wrong.append(f"  {name}: {side} does not read {call} alone: {expr}")
    strays = sorted(
        name for name, (_, using, check) in state.items()
        if name not in _ACTING_POLICIES and "acting_account_ids" in f"{using} {check}"
    )
    wrong += [f"  {name}: reads acting_account_ids() but is not a curation policy" for name in strays]
    assert not wrong, (
        "the acting identity drifted (docs/design/pipeline-sharing.md §3.1, §3.7): the eleven "
        "curation policies read acting_account_ids(), every other policy current_account_ids():\n"
        + "\n".join(wrong)
    )


def test_the_share_rows_are_invisible_while_acting():
    state = _final_policy_expressions()
    for name in _HIDDEN_WHILE_ACTING:
        assert name in state, f"{name} is gone"
        using = state[name][1] or ""
        assert "not acting()" in using and "current_account_ids()" in using, (
            f"{name} must stay the caller's own consent (current_account_ids) and hide while "
            f"acting (`and not acting()`): {using}"
        )


def test_the_eleven_curation_tables_carry_no_other_policy():
    """Replace, never union: a twelfth permissive policy on a curation table that reads
    current_account_ids() would union the delegate's own rows into the acting view. The only
    others allowed are the four pinned `*_shared_read` arms, whose share rows hide while acting."""
    tables = set(_ACTING_POLICIES.values())
    allowed = set(_ACTING_POLICIES) | {
        name for name, (table, _cmd, _roles) in _SHARE_POLICY_PIN.items() if table in tables
    }
    extra = sorted(
        f"  {name} on {table}" for name, (table, _u, _c) in _final_policy_expressions().items()
        if table in tables and name not in allowed
    )
    assert not extra, (
        "a curation table gained a policy outside its acting `*_tenant_rw` and the pinned "
        "`*_shared_read` arms (docs/design/pipeline-sharing.md §3.1, replace semantics):\n"
        + "\n".join(extra)
    )
