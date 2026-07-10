# Phase 0 — Emergency anon-hardening (design + runbook)

**Status:** ready to apply, not yet applied. Analysis/planning artifact — no code changed, nothing in the live `migrations/` sequence yet. Authored 2026-07-10 as the first step of the public-release program (see the feasibility dossier / memory `public-release-feasibility-2026-07`).

**What this closes (all live today, exploitable by any visitor holding the anon key in the SPA bundle):**
1. 25 base tables with RLS off + anon write grants (anon can read/write `address_points` 1.56M, `browse_list` 452k, `dedup_pair_audit`, …).
2. ~30 auto-updatable `*_public` views that let anon write base tables **bypassing RLS** (incl. `listing_snapshots`).
3. 8 anonymously-executable `SECURITY DEFINER` functions (`rebuild_browse_list`, `refresh_health_matviews` → DoS; `emit_verification_stale_alert` → a definer write).
4. The **root cause**: Supabase's public-schema default ACL grants every privilege to `anon`+`authenticated` on every *future* table too — invisible to the 317 migrations.
Plus two independent API hardening items (fail-closed auth, hide `/docs`).

**Why it is safe on the live single-operator system:** the SPA does **zero** `supabase-js` writes (every write goes through the bearer API), so revoking anon *write* grants (keeping `SELECT`) breaks no read path. The backend/worker/cron all connect as `postgres` (BYPASSRLS) — provable: `listings` has carried RLS-with-no-policy since migration 001 yet is UPSERTed hourly, so enabling RLS on more tables cannot block a backend writer.

**This package was adversarially reviewed.** Four defects from that review are folded in below and marked `[REVIEW-FIX]`. Two hard prerequisites before applying: (a) the Supabase MCP is disconnected this session, so the **live pre-flight SELECTs in §Runbook are mandatory** — do not apply on unverified assumptions; (b) apply on a fresh branch `fix/phase0-anon-hardening`, **not** the current dedup branch.

---

## Apply-safety ranking (safest first)

| # | Step | Safety | Note |
|---|------|--------|------|
| 1 | Part 3 — revoke anon EXECUTE on 5 DoS/definer-write funcs | Safest | SPA never calls them; cron runs as postgres |
| 2 | Part 1 — revoke anon/auth WRITE on all tables+views | Safe | closes criticals #1 + #2; `SELECT` untouched |
| 3 | Part 4a — RLS-enable the 13 named internal tables | Safe | none anon-read; backend bypasses |
| 4 | Part 2 — `ALTER DEFAULT PRIVILEGES` root-cause fix | **Verify first** | `[REVIEW-FIX A]` scope/role must match live `pg_default_acl` or it silently no-ops |
| 5 | Part 4b — durable `browse_list` write-lock in the rebuild fn | Verify first | `[REVIEW-FIX B]` replaces the inert "enable RLS on browse_list" |
| 6 | Part 5 — DROP 6 dead backup tables | **Separate migration 286** | `[REVIEW-FIX C]` destructive; pg_dump first |
| — | API: fail-closed auth + hide `/docs` | Safe, independent | ship alongside; does not close the DB criticals |

---

## Migration 285 — `285_phase0_anon_hardening.sql` (Parts 1–4)

> Lift this into `migrations/285_phase0_anon_hardening.sql` on the hardening branch **after** running the §Runbook pre-flight (some values below are confirmed live first). Wrapped in one transaction so a mid-apply failure rolls back cleanly.

```sql
-- 285_phase0_anon_hardening.sql
-- PHASE 0 EMERGENCY HARDENING — close the 3 live anon-exploitable criticals
-- WITHOUT breaking the operator's anon-key SPA reads. Root cause: Supabase's
-- public-schema DEFAULT ACL grants every privilege to anon+authenticated on every
-- existing AND future table (pg_default_acl, platform state, invisible to migrations).
-- Safe because the SPA does ZERO supabase-js writes and the backend connects as a
-- BYPASSRLS role. See docs/design/phase-0-emergency-hardening.md.

begin;

-- ==== PART 1 — revoke anon/authenticated WRITE on every table+view (keep SELECT) ====
-- Closes critical #1 (RLS-off tables) AND #2 (write-through the auto-updatable *_public
-- views: ON ALL TABLES covers views, so anon loses INSERT/UPDATE/DELETE on them too).
-- SELECT untouched -> browse_list + every _public view keep working. Matviews aren't
-- DML-able so no write vector there. Idempotent.
revoke insert, update, delete, truncate, references, trigger
  on all tables in schema public
  from anon, authenticated;

-- ==== PART 2 — ROOT CAUSE: stop the default ACL re-granting future objects ====
-- [REVIEW-FIX A] The statements below assume the default grant is scoped
-- (defaclrole=postgres, defaclnamespace=public). Supabase OFTEN installs it at GLOBAL
-- scope (defaclnamespace=0, no IN SCHEMA) and sometimes under a different role. If so,
-- these REVOKE a non-existent ACL — NO ERROR — and the hole stays open (every future
-- table, incl. the browse_list rebuilt every 5 min, keeps granting anon write).
-- >>> MANDATORY pre-flight (Runbook step P2): run
--     select defaclrole::regrole, defaclnamespace, defaclobjtype, defaclacl from pg_default_acl;
--     and reissue these to MATCH each row's scope+role: drop `in schema public` when
--     defaclnamespace=0; repeat `for role <r>` for every role that appears; if the grant
--     is to PUBLIC (not anon explicitly), revoke from public.
alter default privileges for role postgres in schema public
  revoke insert, update, delete, truncate, references, trigger on tables
  from anon, authenticated;
alter default privileges for role postgres in schema public
  revoke execute on functions from anon, authenticated;

-- ==== PART 3 — revoke anon/authenticated EXECUTE on the 5 dangerous DEFINER funcs ====
-- SPA-aware: these 5 are NOT called by the SPA (verified against frontend/src). The 3
-- definer funcs the Health page DOES call (images_failure_overview, recent_workflow_failures,
-- workflow_failure_summary) are DEFERRED to Phase 1 admin-gating — revoking them now breaks
-- the SPA. Part 2 already stops any NEW function from being anon-executable.
do $$
declare r record;
begin
  for r in
    select p.oid::regprocedure as sig
    from pg_proc p join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.proname in ('rebuild_browse_list','rebuild_properties_map_mv',
                        'refresh_health_matviews','emit_verification_stale_alert',
                        'publication_gate_enabled')
  loop
    execute format('revoke execute on function %s from anon, authenticated', r.sig);
    raise notice 'EXECUTE revoked from anon/authenticated: %', r.sig;
  end loop;
end $$;

-- ==== PART 4a — enable RLS (deny-all) on the internal RLS-off tables ====
-- [REVIEW-FIX] EXPLICIT list only (the draft's blind "flip every relrowsecurity=false"
-- loop is removed — it flipped ~5 un-audited tables with no rollback record). RLS-on +
-- no policy = deny-all to anon/authenticated; postgres + service_role bypass. Metadata-only
-- (instant even on address_points 1.56M). If the Runbook step P4 pre-flight enumerates an
-- RLS-off base table NOT in this list, add it here explicitly ONLY after confirming (query
-- in step P4) that no anon-EXECUTE function reads it; otherwise leave it and note it.
alter table public.address_points                       enable row level security;
alter table public.dedup_pair_audit                     enable row level security;
alter table public.data_quality_snapshots               enable row level security;
alter table public.dedup_golden_pairs                   enable row level security;
alter table public.listing_description_enrichments      enable row level security;
alter table public.property_identity_candidates_archive enable row level security;
alter table public.broker_merge_candidates              enable row level security;
alter table public.workflow_failures                    enable row level security;
alter table public.workflow_run_health                  enable row level security;
alter table public.filter_visibility                    enable row level security;
alter table public.outreach_campaigns                   enable row level security;
alter table public.outreach_messages                    enable row level security;
alter table public.broker_outreach_suppression          enable row level security;

revoke all on
  public.address_points, public.dedup_pair_audit, public.data_quality_snapshots,
  public.dedup_golden_pairs, public.listing_description_enrichments,
  public.property_identity_candidates_archive, public.broker_merge_candidates,
  public.workflow_failures, public.workflow_run_health, public.filter_visibility,
  public.outreach_campaigns, public.outreach_messages, public.broker_outreach_suppression
  from anon, authenticated;

-- ==== PART 4b — DURABLE browse_list write-lock ====
-- [REVIEW-FIX B] Enabling RLS on browse_list is INERT: rebuild_browse_list() is blue-green
-- (DROP browse_list; RENAME browse_list_next -> browse_list) every 5 min, and the new table
-- is created RLS-off with anon write re-granted by the default ACL. Part 1's revoke is wiped
-- within one cycle. The durable fix is to re-assert the write-revoke INSIDE the rebuild
-- function, so every cycle re-locks writes regardless of Part 2.
--
-- DO NOT hand-retype the function. Take migrations/283_browse_list_district_price_covering.sql's
-- `create or replace function rebuild_browse_list()` body VERBATIM and insert ONE statement
-- immediately AFTER its existing line:
--     execute 'grant select on browse_list to anon, authenticated';
-- namely:
--     execute 'revoke insert, update, delete, truncate on browse_list from anon, authenticated';
-- Keep security definer + the advisory-lock structure unchanged. (rebuild_properties_map_mv
-- needs no equivalent — a matview can't be DML'd by anon.)
--
-- >>> Runbook step P4b verifies rebuild_browse_list is owned by a BYPASSRLS role so its own
--     TRUNCATE/INSERT still runs. If that can't be confirmed, ship Part 4b as the function
--     edit anyway (revoke is harmless to the definer) but skip any RLS-enable on browse_list.

-- (paste the corrected rebuild_browse_list() definition here, per the note above)

-- ==== PART 4c — [REVIEW-FIX] embedded post-conditions: fail the whole migration if wrong ====
do $$
begin
  assert not has_table_privilege('anon','public.address_points','INSERT'),
         'anon still has INSERT on address_points — Part 1 did not take';
  assert has_table_privilege('anon','public.browse_list','SELECT'),
         'anon LOST SELECT on browse_list — Browse would break, aborting';
  assert not has_function_privilege('anon',
         'public.refresh_health_matviews()','EXECUTE'),
         'anon still has EXECUTE on refresh_health_matviews — Part 3 did not take';
end $$;

commit;
```

## Migration 286 — `286_drop_dead_backup_tables.sql` (Part 5, DESTRUCTIVE)

> `[REVIEW-FIX C]` Split out of 285. Irreversible → gated on operator OK + an off-box `pg_dump` (Runbook step P1). Ship 285 first and urgently; 286 can follow whenever.

```sql
-- 286_drop_dead_backup_tables.sql  -- DESTRUCTIVE. Requires the pg_dump from Runbook step P1.
begin;
drop table if exists public._backup_estimation_subject_summary_20260602;
drop table if exists public._bazos_mistagged_20260602;
drop table if exists public.dq_p0_backfill_backup;
drop table if exists public.images_backfill_backup_20260609;
drop table if exists public.notification_dispatches_pre204_backup;
drop table if exists public.placeholder_backfill_backup_20260612;
commit;
```

---

## API code changes (independent of the SQL; ship in the same PR)

**(a) Fail CLOSED when `API_TOKEN` is unset — `api/dependencies.py:120-126`.** Today the gate is a no-op when the env var is missing (fail-open). Invert it; keep local-dev ergonomics behind an explicit opt-out so a *forgotten* prod secret can never silently disable auth. Also make the compare timing-safe.

```python
# add near the stdlib imports (top of api/dependencies.py)
import hmac

def require_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer gate. Fails CLOSED: if API_TOKEN is unset the API refuses every request
    (503) unless the operator explicitly opts out for local dev with API_AUTH_OPTIONAL=1."""
    expected = os.environ.get("API_TOKEN")
    if not expected:
        if os.environ.get("API_AUTH_OPTIONAL") == "1":
            return
        raise HTTPException(status_code=503,
            detail="API auth is not configured (set API_TOKEN, or API_AUTH_OPTIONAL=1 for local dev)")
    if not authorization or not hmac.compare_digest(authorization, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="Invalid or missing token")
```
CI/test note: any test that hits gated routes must set `API_AUTH_OPTIONAL=1` (or `API_TOKEN`) — grep tests for `require_token`/`TestClient` before merging so none flips to 503.

**(b) Hide `/docs`, `/redoc`, `/openapi.json` in prod — `api/main.py:172`** (`os` already imported). Setting `openapi_url=None` also disables Swagger/ReDoc, so the 169-route inventory isn't publicly enumerable.

```python
_docs_enabled = os.environ.get("API_DOCS_ENABLED") == "1"
app = FastAPI(
    title="sreality toolkit API", version="0.3.0", lifespan=_lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)
```

---

## CI gate — `tests/test_migration_rls_grants.py`

A fast **offline regex** gate (the anon-write posture is untestable on the `_FakeConn` DB tests, like `tests/test_sql_placeholders.py`). It fails any migration **≥ 285** that (a) grants INSERT/UPDATE/DELETE/TRUNCATE/ALL to `anon`/`authenticated`, or (b) creates a base table without enabling RLS. Escape hatch: a `-- ci-allow-no-rls: <table> <reason>` marker. This stops the append-only cadence from silently re-opening the hole on every new table.

```python
"""CI gate: no migration may re-open anon/authenticated WRITE access, and every
new base table must ship with RLS enabled. The anon-write posture is UNTESTABLE on
the _FakeConn DB tests (it can't see GRANTs/default ACLs/RLS), so — like
tests/test_sql_placeholders.py — this is a fast, offline, regex gate over the SQL
text. Enforced only for migrations >= MIN_ENFORCED (history is fixed-forward, rule #1).
Escape hatch: `-- ci-allow-no-rls: <table> <reason>` in the same migration."""
from __future__ import annotations
import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
MIN_ENFORCED = 285
_WRITE_PRIVS = ("insert", "update", "delete", "truncate", "all", "all privileges")
_ROLES = ("anon", "authenticated")

def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", sql)

def _migration_number(path: Path) -> int | None:
    m = re.match(r"^(\d+)_", path.name)
    return int(m.group(1)) if m else None

def _statements(sql: str) -> list[str]:
    return [s.strip() for s in _strip_comments(sql).split(";") if s.strip()]

def _offending_grants(sql: str) -> list[str]:
    out: list[str] = []
    for stmt in _statements(sql):
        low = re.sub(r"\s+", " ", stmt.lower())
        if not low.startswith("grant "):
            continue
        m = re.match(r"grant (.+?) on .+? to (.+)$", low)
        if not m:
            continue
        privs, roles = m.group(1), m.group(2)
        if not any(r in re.split(r"[,\s]+", roles) for r in _ROLES):
            continue
        if any(p in _WRITE_PRIVS for p in re.split(r"[,\s]+", privs)):
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
    return {m.group(1) for m in re.finditer(
        r'alter table (?:if exists )?(?:public\.)?"?([a-z0-9_]+)"?\s+enable row level security',
        _strip_comments(sql).lower())}

def _rls_exempt_tables(sql: str) -> set[str]:
    return {m.group(1) for m in re.finditer(r"--\s*ci-allow-no-rls:\s*([a-z0-9_]+)", sql.lower())}

def _enforced_migrations() -> list[Path]:
    return [p for p in sorted(MIGRATIONS_DIR.glob("*.sql"))
            if (n := _migration_number(p)) is not None and n >= MIN_ENFORCED]

def test_no_anon_write_grants_in_new_migrations():
    offenders = [f"  {p.name}: {stmt}" for p in _enforced_migrations()
                 for stmt in _offending_grants(p.read_text(encoding="utf-8"))]
    assert not offenders, (
        "Migration(s) grant INSERT/UPDATE/DELETE/TRUNCATE/ALL to anon or authenticated — "
        "browser roles get SELECT only; writes go through the bearer-gated API:\n" + "\n".join(offenders))

def test_new_base_tables_enable_rls():
    offenders: list[str] = []
    for p in _enforced_migrations():
        sql = p.read_text(encoding="utf-8")
        rls_on, exempt = _rls_enabled_tables(sql), _rls_exempt_tables(sql)
        for tbl in _created_base_tables(sql):
            if tbl not in rls_on and tbl not in exempt:
                offenders.append(f"  {p.name}: table '{tbl}' created without RLS")
    assert not offenders, (
        "New base table(s) created without `enable row level security` in the same migration "
        "(Supabase's default ACL makes an RLS-off public table anon-writable). Enable RLS + an "
        "anon SELECT policy if the SPA reads it, or add `-- ci-allow-no-rls: <table> <reason>`:\n"
        + "\n".join(offenders))

def test_gate_actually_scans_migrations():
    assert MIGRATIONS_DIR.is_dir(), f"migrations dir not found: {MIGRATIONS_DIR}"
    assert len(_enforced_migrations()) >= 1, f"no migrations at/after {MIN_ENFORCED}"
```

**Caveat the reviewer flagged:** `scripts/ci_db_bootstrap.sql` creates the roles but does **not** replicate Supabase's default ACL, so Part 1/Part 2 are no-ops in the CI replay — this gate is a *regression* guard, not a live-state assertion. The real cross-tenant/grant verification lands in Phase 1's `TEST_DATABASE_URL` lane.

---

## Operator runbook

Run in the Supabase SQL editor (connects as `postgres`, bypasses RLS, so every verify works). Nothing here rewrites a large table — enabling RLS is instant metadata — so no long lock; still pick a quiet few minutes.

### Pre-flight (MANDATORY — MCP is disconnected, so confirm live before applying)

- **P1 — back up the 6 tables 286 will DROP** (irreversible without this). Locally: `pg_dump "$SUPABASE_DB_URL" -t public._backup_estimation_subject_summary_20260602 -t public._bazos_mistagged_20260602 -t public.dq_p0_backfill_backup -t public.images_backfill_backup_20260609 -t public.notification_dispatches_pre204_backup -t public.placeholder_backfill_backup_20260612 > backups/phase0_backups_20260710.sql`. Confirm non-empty, store off-box. **Do not run 286 until this exists.**
- **P2 — default-ACL scope (the single most important check, `[REVIEW-FIX A]`):** `select defaclrole::regrole, defaclnamespace, defaclobjtype, defaclacl from pg_default_acl;` → rewrite Part 2 to match every row's role + scope (drop `in schema public` if `defaclnamespace=0`; repeat `for role <r>`; revoke from `public` if granted via PUBLIC).
- **P3 — pooler role bypasses RLS:** confirm the login role behind `SUPABASE_DB_URL` has `rolbypassrls` (so the backend/worker keep writing after RLS-enable). `select rolname, rolbypassrls, rolsuper from pg_roles where rolname = current_user;` while connected via that DSN.
- **P4 — enumerate the exact RLS-off base tables** the 4a step covers, and confirm none is read by an anon function: run the two SELECTs in the draft (`safe_to_denyall` block) — `relkind='r' and not relrowsecurity`, and the `pg_get_functiondef` scan of the 12 anon INVOKER RPCs for any of those table names (expect **0 rows**). Add any newly-found RLS-off table to Part 4a explicitly only if it passes.
- **P4b — `browse_list` rebuild owner bypasses RLS:** `select proname, pg_get_userbyid(proowner) owner, prosecdef, (select rolbypassrls from pg_roles where rolname = pg_get_userbyid(proowner)) owner_bypass from pg_proc p join pg_namespace n on n.oid=pronamespace where nspname='public' and proname='rebuild_browse_list';` → need `prosecdef=true` and `owner_bypass=true`.
- **P5 — snapshot the before-picture** (rollback reference): the RLS-off list, and `has_table_privilege('anon', ..., 'INSERT'/'SELECT')` for all base tables; the anon-executable function list.

### Apply (285), verify after each part
- **A — Part 1:** re-check anon INSERT = false everywhere; anon SELECT on `browse_list` still true. This alone is the non-breaking core fix.
- **B — Part 2:** re-run the `pg_default_acl` query; the anon/auth write + function-EXECUTE rows are gone for the role(s) you edited.
- **C — Part 3:** the 5 funcs → anon EXECUTE false; the 15 SPA RPCs (incl. the 3 deferred Health ones) → still true.
- **D — Part 4a:** open the SPA — Browse, a listing, broker pages, Health all still load (they read via `*_public` views / definer funcs).
- **E — Part 4b:** wait one 5-minute rebuild cycle, then `set role anon; select count(*) from public.browse_list; reset role;` returns a number, **and** `has_table_privilege('anon','public.browse_list','INSERT')` = **false** (proves the in-function revoke re-locked the freshly-rebuilt table). Load Browse in the SPA.
- **G — API:** confirm `API_TOKEN` is set on Railway before deploy (else the fail-closed gate 503s everything). After: authed request 200, unauthed 401, `/docs` 404.
- **H — rotate the shared token** (the old one is in every shipped bundle): `openssl rand -hex 32` → set `API_TOKEN` (Railway API), `VITE_API_TOKEN` (Railway frontend, rebuild), `EXT_API_TOKEN` (repo secret, rebuild extension dist). Do the three close together. (It lives in the browser bundle so it isn't truly secret — rotation just kills the leaked value; per-user JWT replaces it in Phase 1.)

### Rollback (per step; the migration's own begin/commit + Part 4c asserts cover mid-apply failures)
- **Part 1:** surgical `grant insert,update,delete on public.<table> to anon;` (blunt all-tables re-grant = emergency only, re-opens the hole).
- **Part 2:** `alter default privileges for role <r> [in schema public] grant insert,update,delete,truncate,references,trigger on tables to anon, authenticated;` (+ `grant execute on functions`). Revert, not a recommendation.
- **Part 3:** `grant execute on function public.<fn>(<args>) to anon;` (signature from the P5 snapshot).
- **Part 4a:** `alter table public.<table> disable row level security;` (re-`grant select` only if a read genuinely needs it — prefer routing via a `*_public` view).
- **Part 4b:** if Browse goes blank or the rebuild stops writing, `create or replace` `rebuild_browse_list` back to migration 283's version (without the added revoke); `browse_list` keeps anon SELECT from mig 277:75 so reads return immediately.
- **286 (dropped backups):** `psql "$SUPABASE_DB_URL" < backups/phase0_backups_20260710.sql`.
- **API:** env-only revert — set `API_TOKEN` / `API_AUTH_OPTIONAL=1`; `API_DOCS_ENABLED=1`. Full revert = redeploy previous image.

---

## What Phase 0 deliberately does NOT do (→ Phase 1)

Closes the *live* holes only. It does **not** add per-user auth, tenancy/`account_id`, RLS *policies* (these tables become deny-all, not per-tenant), admin role-gating of the 70 admin routes, the dedicated non-superuser API role, rate limits, or a hard LLM spend cap. The token rotation buys time but the shared token remains a public-bundle secret until Phase 1's per-user JWT replaces it. `browse_list` will still show the Supabase `rls_disabled_in_public` advisor (cosmetic — writes are locked by Part 4b every cycle); clearing it durably would mean baking RLS into the rebuild, deferred as non-urgent.
```
