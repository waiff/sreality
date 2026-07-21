# Public-release remediation — post-ship review of migrations 316–319

**Date:** 2026-07-20. **Status: COMPLETE — R1, R2, R2b, R3, R4 all shipped and
live-verified (migrations 329, 330, 331, 332). The one deliberate deferral is the
health-matview repoint; rationale in R2.**

A 14-finding code review of the deployed 316–319 hardening batch was adversarially
re-verified by a 6-agent workflow (repo + **live DB**, project `erlvtprrmrylhznfyaih`)
before this plan was written. Verdicts: **11 confirmed** (3 of them live P0 production
regressions), **1 refuted** (the per-row-gate perf claim), **2 partial** (core confirmed,
premise corrected). The verification also surfaced **two exposures the review missed**
(`listing_natural_key_public` anon-readable; all 13 matviews carrying full pre-299
`authenticated` ACLs) and **one fix-path landmine** (a `current_user`-keyed fallback in
`is_platform_admin()` would open the gate to everyone — see R1).

This doc is the execution spec. Work = **4 PRs (R1–R4), in order**. R1 is urgent (three
things are broken in production right now). R2 blocks public release. R3/R4 are quality.

## Verdict table

| # | Finding (file) | Verdict | Disposition |
| --- | --- | --- | --- |
| F1 | `property_estimates_public` broken by `security_invoker` (mig 316) | CONFIRMED — worse: also breaks `browse_stats_properties` `with_estimates`; anon path hard-errors 42501 | R1 |
| F2 | `is_platform_admin()` GUC dependency breaks golden-set script | CONFIRMED live (`dedup_label_events` = 0 rows on a raw connection; feeders 584/28/16) | R1 |
| F3 | Same breaks pg_cron `refresh_health_matviews()` → Health poisoned | CONFIRMED live (matview payloads: 0 failures / 0 queued / null parses; base: 1485 / 895 / 11) | R1 |
| F4 | Stale anon SELECT grants on 5 gated views + `property_estimates_public` | PARTIAL — confirmed **+ 7th drifted view `listing_natural_key_public`** (anon can dump the full natural-key map); `property_estimates_public` is UNGATED so anon reads real rows, not an error | R2 |
| F5 | Live tenant-view test covers 1 of 8 views | CONFIRMED (and would have caught F1 on the 316 PR itself) | R3 |
| F6 | Bare gate = per-row anti-pattern (mig 275 class) | **REFUTED** — live EXPLAIN on 6 objects shows a **One-Time Filter** (standalone pseudoconstant qual, O(1); non-admins skip the whole subtree). 275's per-row case only occurs when the gate is OR/AND-ed with a column Var | no action (optional hardening note) |
| F7 | Raw `_mv` matviews bypass the 318 gate for `authenticated` | CONFIRMED — broader: **all 13** public matviews have authenticated SELECT; 12/13 carry full pre-299 DML+MAINTAIN ACLs | R2 |
| F8 | Broker `.catch()` swallows every error | CONFIRMED (A6 denial is a hard SQLSTATE **42501** via `PostgrestError.code`, not an HTTP status — the catch is live code, and everything else funnels into it silently) | R4 |
| F9 | Static gate test = substring check only | CONFIRMED | R3 |
| F10 | No structural gate for future admin view #27 | CONFIRMED (incl. the roadmap's "suite now can catch this on its own" being overstated) | R3 |
| F11 | Redundant `size === 0` ternary | CONFIRMED (callee short-circuits) | R4 |
| F12 | `docs/architecture.md:1008` stale on broker degrade path | CONFIRMED | R4 |
| F13 | Multi-paragraph docstring | CONFIRMED (blank line 307, added by `bab3a3bf`) | R3 |
| F14 | Hand-copied view bodies vs "existing DDL-loop convention" | PARTIAL — premise wrong (291/294/299 loops do GRANT/REVOKE, never view bodies; hand-writing IS the convention). Transcription risk bounded by `CREATE OR REPLACE` column checks | no action |

## Ground rules for the executing session

- Load the `database` skill before R1/R2. Migrations are **append-only**, applied via the
  Supabase MCP; everything below is additive → autonomous per the DB gate (no pg_dump needed).
- Migration numbers mean "next free numbers" — re-check `ls migrations/ | tail -3` before
  writing; renumber if something landed first. (This already happened: the plan said 320,
  but the listing-identity track had taken 320–328, so R1 shipped as **329 + 330** and R2's
  numbers move accordingly.)
- One branch + draft PR per R-item, off fresh `main`; CI green before merge; update
  `roadmap/public-release-track.md` (this track only) in each PR.
- After applying each migration live, run the stated verification queries **before** pushing,
  and keep repo/live in sync (the mig file on the branch must match what was applied).
- The live suite `tests/test_tenant_isolation_live.py` is the safety net here — run it
  (gated `TEST_DATABASE_URL` lane) after every migration.

---

## R1 — P0 hotfix: un-break Browse estimates, golden-set freeze, Health matviews

**✅ SHIPPED 2026-07-20** — migrations **329 + 330** (the plan's "320" was taken; the
listing-identity track had landed 320–328 by execution time). Applied live and verified;
see the live-verification results at the end of this section.

**Branch:** `fix/admin-gate-p0-hotfix`. **Two migrations + test updates.**
All three regressions share the 316–318 deployment as root cause.

### 329 part A — revert `security_invoker` on `property_estimates_public` (F1)

The view is a **market-wide aggregate** (`estimation_runs JOIN listings`, mig 311:200-220),
not tenant data — mig 316 mis-grouped it with the 7 genuine tenant views. Under invoker
rights the join hits `listings` (RLS enabled, **zero policies** = deny-all) → 0 rows for
`authenticated`, SQLSTATE 42501 for `anon`. That empties Browse's "with estimates" prefilter
(`frontend/src/lib/queries.ts:518-530` → `pre.empty` → zero results on Map/Table/Cards/count)
**and** zeroes the Stats RPC (`browse_stats_properties` is SECURITY INVOKER and does
`EXISTS (SELECT 1 FROM property_estimates_public …)`). Verified live: 58 rows as a
privileged role, 0 rows under `SET ROLE authenticated` + JWT claims.

```sql
alter view public.property_estimates_public set (security_invoker = false);
```

No frontend or function change needed — reverting the view heals both surfaces. The other
7 mig-316 views were individually re-verified as genuinely tenant-scoped (each base table
carries `account_id` + an RLS policy) — **do not touch them**.

Do NOT "fix" this instead by granting browser roles SELECT on `estimation_runs`/`listings`
base tables — that would leak tenant estimation rows and unwind Phase 0.

### 329 part B — claims-absent fallback in `is_platform_admin()` (F2 + F3)

`is_platform_admin()` (mig 286:71-84, SQL STABLE SECURITY DEFINER) keys solely on
`request.jwt.claims`, which is NULL on every non-PostgREST connection. Confirmed live:
raw connection → `is_platform_admin() = false` → `dedup_label_events` returns 0 rows
(base feeders: 584 merge events + 28 dismissals + 16 feedback rows), so
`scripts/build_dedup_golden_set.py` freezes nothing and exits 0; and the `*/10` pg_cron
`refresh_health_matviews()` (runs as `postgres`, no claims) has **already poisoned**
`scraper_health_checks_mv` / `health_summary_mv` / `portal_health_mv` — every source shows
"0 active" failures and "0 queued" while base tables hold 1485 fetch-failure rows and 895
queue rows. This kills exactly the failure-tracking signal CLAUDE.md rule #5 depends on.

**⚠ Fix-path landmine (do not deviate):** inside a SECURITY DEFINER body, `current_user`
is the **definer** (`postgres`), not the caller — any fallback keyed on `current_user` (or
`rolbypassrls(current_user)`) evaluates against `postgres` and returns true for **every**
caller including anon = full admin-gate bypass. The only unmasked discriminator is
**`session_user`**. Live facts: pg_cron → `postgres`; `SUPABASE_DB_URL` scripts → `postgres`;
PostgREST → `authenticator`; tenant pool → `tenant_pool` (rolbypassrls **false**, and
`api/tenant_pool.py:75-78` always sets claims before any query, so the fallback never fires
for tenant traffic and fails closed even if claims were somehow left NULL).

```sql
create or replace function public.is_platform_admin()
returns boolean
language sql stable security definer
set search_path = public
as $$
  select case
    when nullif(current_setting('request.jwt.claims', true), '') is null then
      -- No JWT context: direct connection (psycopg scripts, pg_cron), never a
      -- PostgREST/tenant-pool request. Trust only bypassrls login roles.
      -- session_user is NOT masked by SECURITY DEFINER; current_user IS.
      coalesce((select rolbypassrls from pg_roles where rolname = session_user), false)
    else exists (
      select 1 from public.admins a
      where a.user_id =
        nullif(current_setting('request.jwt.claims', true)::jsonb ->> 'sub', '')::uuid
    )
  end
$$;
```

Before writing the migration, pull the live def (`select pg_get_functiondef('public.is_platform_admin'::regproc)`)
and preserve everything except the added CASE (attributes, search_path). `rolbypassrls(session_user)`
is TRUE only for `postgres`/`service_role`/`supabase_admin`, FALSE for
`tenant_pool`/`authenticator`/`anon`/`authenticated` — verified live. Blast radius: the
function also backs RLS policies (migs 290–292) and all 26 gated objects; the fallback only
fires when claims are NULL, which never happens on the PostgREST or tenant-pool paths, and
the CI live tests always `set_config` claims, so their assertions are unaffected.

**No manual matview refresh needed:** the next `*/10` cron tick repopulates all three
health matviews once the function is fixed. (Known flake: one cron cycle timed out
refreshing `scraper_health_checks_mv` on 2026-07-20 17:00 — pre-existing, unrelated; R2's
matview decoupling doesn't change refresh cost.)

### R1 test updates (same PR)

1. Remove `'property_estimates_public'` from `_TENANT_VIEWS` (tests/test_tenant_isolation_live.py:39)
   — `test_tenant_views_are_security_invoker` then asserts the 7 genuine tenant views only.
2. New liveness test: under `SET LOCAL ROLE authenticated` + JWT claims,
   `SELECT count(*) FROM property_estimates_public` must equal the privileged-baseline count
   (>0) — this is the assertion that would have failed CI on the 316 PR.
3. New raw-connection tests: without claims, `is_platform_admin()` is true as a bypassrls
   session AND `SELECT count(*) FROM dedup_label_events` > 0 (seed one operator merge event
   if the replay DB is empty); with `SET ROLE authenticated` + foreign claims it stays false
   (existing deny tests re-run unchanged).

### R1 live verification — RESULTS (2026-07-20, post-apply)

| Check | Before | After |
| --- | --- | --- |
| `dedup_label_events` (golden-set source) | 0 rows | **809 rows** |
| `listing_fetch_failures_public` | 0 | **1485** |
| `listing_detail_queue_public` | 0 | **680** |
| `parsed_url_activity` | 0 | **4** |
| `property_estimates_public` reloption | `security_invoker=true` | **owner-rights**, 58 rows |
| `scraper_health_checks_mv` fetch-failure check (sreality) | `0 active` | **`38 active`** = base table exactly |
| Foreign JWT → `is_platform_admin()` | false | **false** (deny direction intact) |
| Claims-less `SET ROLE authenticated` | n/a | **false** (migration 330) |

The health matviews healed through the **real pg_cron path** (the 18:10 tick), not a manual
refresh — so the cron→matview→dashboard chain is verified end to end. Both migrations carry
their own `DO` post-condition blocks that raise rather than leave a half-fixed gate live.

Still worth an operator click-check (not blocking): the Browse "with estimates" toggle in the SPA.

### R1 addendum — why there are two migrations

Migration 329's fallback keyed on `session_user` alone. Live-testing it immediately after
apply showed that guard is looser than intended: `SET ROLE` does **not** change
`session_user`, so a connection logging in as an owner/bypassrls role and then simulating
`SET LOCAL ROLE authenticated` **without** claims still reported admin. Production was never
exposed (PostgREST logs in as `authenticator`, the API pool as `tenant_pool` — neither is
bypassrls, so both already failed closed), but the **CI schema-replay DB logs in as the table
owner**, exactly where a role-switch simulation would have been silently over-privileged and
could have masked a real gate regression.

Migration 330 adds the second condition: the `role` GUC must read `'none'`. That GUC is **not**
masked by SECURITY DEFINER (unlike `current_user`), so it stays visible inside the function
body — `'none'` for a genuine service connection, the switched-to role name after any
`SET ROLE`. Both conditions must hold. pg_cron and the service-role scripts are unaffected
(SECURITY DEFINER does not set the role GUC, and neither issues a `SET ROLE`), which 330's own
post-condition asserts in both directions before committing.

**Generalizable lesson:** when a SECURITY DEFINER function needs to know *who is calling*,
`current_user` is the definer and useless; `session_user` identifies the login but survives
`SET ROLE`; only `session_user` **plus** the `role` GUC distinguishes "a service connection"
from "a service connection pretending to be a browser role."

---

## R2 — grant hardening + decouple cron matviews from request state

**✅ SHIPPED 2026-07-20 (grant hardening) — migration 331.** The matview repoint is
**deliberately deferred** — see "Deferred: the matview repoint" below.

**Branch:** `fix/anon-matview-grant-hardening`. Latent today (single operator) but a hard
public-release blocker.

### 331 — revoke the drifted grants (F4 + F7)

Live-verified full anon inventory = **exactly 7 views, all drift** (granted by migs
303/308/309/310/311/315 *after* 299's blanket revoke; the settled posture is anon-dark, and
the SPA reads all 7 as `authenticated` behind RequireAuth — grep-verified, so nothing breaks):

```sql
revoke select on
  public.dedup_vision_bakeoff_results_public,
  public.image_border_cases_public,
  public.image_tag_annotations_public,
  public.image_training_examples_public,
  public.phash_pair_notes_public,
  public.property_estimates_public,     -- leaks real rows to anon today (58)
  public.listing_natural_key_public     -- leaks the full ~555k natural-key map to anon today
from anon;
```

Note: the first 5 are is_platform_admin-gated, so anon currently gets a raw
`permission denied for function is_platform_admin` error (implementation leak); the last 2
are **ungated real-data reads**. All 7 revokes are behavior-neutral for the SPA.

Matviews (mig 299 PART B2 only covered `relkind in ('r','p','v')` — matviews slipped through
with their full pre-299 default ACLs):

```sql
-- Close the 318-gate bypass: gated _public views/function keep working via
-- owner/definer rights; no non-admin surface reads these raw.
revoke all on
  public.dedup_funnel_resolutions_mv,
  public.dedup_llm_cost_by_category_mv,
  public.images_failure_overview_mv
from anon, authenticated;

-- Strip write + MAINTAIN (REFRESH) from authenticated on every matview; keep SELECT
-- (Health/map/choropleth matviews are read directly by the admin SPA as authenticated).
do $$ declare r record; begin
  for r in select c.relname from pg_class c
           join pg_namespace n on n.oid = c.relnamespace
           where n.nspname = 'public' and c.relkind = 'm' loop
    execute format(
      'revoke insert, update, delete, truncate, references, trigger on public.%I from anon, authenticated',
      r.relname);
  end loop;
end $$;
```

`MAINTAIN` is a PG17 privilege — check `show server_version` first; include it in the revoke
list only if ≥17 (live ACLs show it present, so the server is likely 17 — verify, don't assume).
Do NOT revoke authenticated SELECT on the other 10 matviews without first grepping
`frontend/src/` for each name — `properties_map_mv` (SPA map) and the health/choropleth
matviews are legitimately read by the logged-in app.

### Repoint the 3 health matviews at base tables (F3 defense-in-depth) — NOT SHIPPED

A matview refreshed out-of-band by pg_cron must never depend on `request.jwt.claims`. Even
with R1's fallback, leave no request-scoped state in cron paths:

- `scraper_health_checks_mv` (mig 214): `listing_fetch_failures_public` → `listing_fetch_failures`
  (214:94) and `listing_detail_queue_public` → `listing_detail_queue` (214:104/114).
- `health_summary_mv` (mig 216): `listing_fetch_failures_public` → `listing_fetch_failures` (216:109/113/123).
- `portal_health_mv` (mig 219): replace `left join parsed_url_activity` (219:76) with the
  same aggregate inlined over `parsed_url_cache` (body is at 318:514-521).

Matview bodies can't be ALTERed: DROP + CREATE each, **preserving the unique index**
(required for `REFRESH … CONCURRENTLY`) **and the existing grants** (e.g. 214:351), then a
one-time plain `REFRESH MATERIALIZED VIEW` per matview at the end of the migration so the
dashboard heals immediately. These matviews expose only aggregates — repointing to base
tables leaks no row-level data. Watch the known `scraper_health_checks_mv` refresh-timeout
flake when applying (plain refresh, not CONCURRENTLY, inside the migration is fine).

### R2 standing tests (same PR; must use `pg_class` + `aclexplode` — `information_schema.role_table_grants` omits matviews)

```python
def test_anon_holds_no_relation_grants(svc):
    """Settled posture: anon reads NOTHING. Allowlist is empty; assert equality, never <=."""
    # pg_class + aclexplode over relkind in ('r','v','m','p'), grantee = anon → []

_ADMIN_GATED_MATVIEWS = ["dedup_funnel_resolutions_mv",
                         "dedup_llm_cost_by_category_mv",
                         "images_failure_overview_mv"]
def test_admin_ops_matviews_dark_to_authenticated(svc):
    # has_table_privilege('authenticated', mv, 'SELECT') is False for each
```

Also update `docs/design/phase-0-emergency-hardening.md` in this PR: record (a) the mig-299
PART B2 matview relkind gap, (b) the drift vector "grants added by migrations after the
one-time revoke sweep" — the standing anon test is the durable fix for (b).

### Deferred: the matview repoint (never assigned a number)

(The number 332 went to the health-RPC admin gate that R2's audit turned up — see
"New finding while executing R2" below. This repoint was never assigned a number.)

The plan called for DROP+CREATE'ing `scraper_health_checks_mv` / `health_summary_mv` /
`portal_health_mv` so they read base tables instead of the gated wrapper views — the rule
being that a pg_cron-refreshed matview must never depend on request-scoped state. The swap
was confirmed semantically exact: `listing_fetch_failures_public` and
`listing_detail_queue_public` are pure gated passthroughs (identical column list over the
base table, wrapped `WHERE is_platform_admin()`), so pointing at the base table changes
nothing but the gate.

**Not shipped, on purpose.** The failure mode is already covered from two directions:
migration 330's own post-condition asserts the claims-less service path stays admin, and
`test_admin_gate_opens_for_service_but_not_role_switch` pins it in the live lane — so a
regression in the fallback fails CI instead of silently poisoning Health. Against that, the
repoint means DROP+CREATE on the operator's live monitoring surface, preserving each
matview's unique index (required for `REFRESH … CONCURRENTLY`) plus its grants, with an
in-migration repopulate — on a matview (`scraper_health_checks_mv`) whose refresh *already*
timed out on the 17:00 cron cycle. Trading a monitoring outage for belt-and-braces on an
already-covered failure mode is the wrong side of the risk.

**Revisit** when a health-matview change is needed anyway. Operator can overrule if they
want the coupling gone regardless.

### R2 live verification — RESULTS

- Full anon inventory (`has_table_privilege` over relkind r/v/m/p — catches matviews, which
  `information_schema.role_table_grants` omits entirely, and PUBLIC-inherited grants):
  **7 views before → 0 after**.
- The 3 gate-backing matviews: `authenticated` SELECT **revoked**. Their owner-rights wrapper
  views and the SECURITY DEFINER `images_failure_overview()` still reach them via the owner,
  so the admin path is unchanged.
- Every matview: DML (+ `MAINTAIN` on PG17) stripped from both browser roles; SELECT preserved
  on the 10 that have real readers.
- All three checks run as post-conditions **inside** migration 331, so a partial apply rolls
  back rather than leaving a half-open ACL.

### New finding while executing R2 — ungated health RPCs (own PR)

`health_summary()` and `portal_health_summary()` are `SECURITY INVOKER` with **no**
`is_platform_admin()` gate and `EXECUTE` granted to `authenticated` — the same
admin-only-data-reachable-by-any-tenant class migration 318 was written to close, but these
two were not in its 26. They are why matview SELECT could not simply be revoked on
`health_summary_mv` / `portal_health_mv` (an invoker function reads them as the caller).
Fixing them needs three coupled changes (convert to `SECURITY DEFINER`, embed the gate,
then revoke the matview SELECT), so it ships as its own PR rather than riding along here.

---

## R3 — test-lane hardening + the standing CI gate

**✅ SHIPPED 2026-07-20.**

**Branch:** `fix/tenant-test-lane`. **No migration.** This is what makes bug-class #27
structurally impossible to ship silently, replacing the roadmap's "defer to one-time
external re-audit" posture.

1. **Parameterize** `test_cross_tenant_denial_through_public_view` over the 7 tenant views
   (`@pytest.mark.parametrize`), each with: seed as `svc` (postgres) with a unique nonce →
   negative read as tenant B (`[]`) → **positive read as tenant A** (row present — the
   assertion class that would have caught F1). Seed specifics (all FK prereqs seeded once in
   the fixture, cleaned in `finally`, children before parents):
   - `properties`: `INSERT … DEFAULT VALUES RETURNING id` works (all NOT NULLs have defaults).
   - `tags` needs `color` (NOT NULL, no default); `property_tags` filters on the id pair (no
     text column); `collection_properties` via a seeded collection; `pipeline_stages` — a lone
     non-entry/non-terminal stage passes the column-level CHECKs (entry/terminal invariants
     are API-enforced, not DB); `property_pipeline` — composite FK `(account_id, stage_id)`,
     single card per property → use a dedicated property id.
2. **Liveness test for the reclassified market view** (rides R1 if convenient, else here):
   authenticated non-admin sees `property_estimates_public` rows; if seeding is needed,
   respect `listings_sreality_id_sign_check` (mig 311: `source='sreality'` ⇒ positive id).
3. **Strengthen the static gate test** (F9): require the gate in a WHERE position —
   `re.search(r"\)\s+__admin_gate\s+where\s+is_platform_admin\(\)", viewdef, re.I|re.S)` for
   views (functions: gate inside WHERE, not the SELECT list) — and add the comment that the
   live deny/allow test is authoritative; the static one only guards accidental clause removal.
4. **Standing CI gate** (F10), in `tests/test_migration_rls_grants.py` style:
   - *Static:* for every migration ≥ 331 (`MIN_VIEW_GATE`), any `CREATE [OR REPLACE]
     [MATERIALIZED] VIEW`/`FUNCTION` whose body references a `SENSITIVE_TABLES` token
     (word-boundary) must contain `is_platform_admin()` or be named in an explicit,
     comment-justified `PUBLIC_ALLOWLIST`. `SENSITIVE_TABLES` = the admin base relations the
     26 objects read: dedup_engine_runs, dedup_scan_state, dedup_vision_bakeoff_results,
     dedup_decision_feedback, property_identity_candidates, property_merge_events,
     listing_detail_queue, listing_fetch_failures, detail_queue_completions, llm_calls,
     parsed_url_cache, phash_pair_notes, pipeline_check_results, image_border_cases,
     image_tag_annotations, image_training_examples, workflow_failures, workflow_run_health,
     dedup_funnel_resolutions_mv, dedup_llm_cost_by_category_mv, images_failure_overview_mv.
     **Exclude** `listings`/`properties`/`images` (shared-market — including them would
     false-flag every public view) and the 19 tenant tables (covered by the RLS/invoker lane).
   - *Live* (auto-generalizes to view #27 regardless of migration): sweep `pg_views` +
     SECURITY DEFINER functions whose definition references a sensitive table; each must
     embed the gate or be allowlisted.
   - **Document the residual blind spot** in the test file: admin aggregates over
     `listings`/`properties` only (e.g. `data_quality_by_source`,
     `publication_gate_health_public`) cannot be auto-flagged — they stay covered by the
     human-maintained `_ADMIN_GATED_VIEWS` live list; the external re-audit checks for
     new ones.
5. F13: collapse the `test_admin_ops_views_deny_non_admin_allow_admin` docstring to a single
   paragraph (delete blank line 307 / merge).

Optional (skip unless cheap): a plan-shape guardrail asserting EXPLAIN on one heavy gated
view contains `One-Time Filter`/`InitPlan` and not a per-row `Filter: is_platform_admin()` —
this is the real residual risk behind refuted F6 (a future edit AND-ing the gate with a
column predicate would silently go per-row).

---

## R4 — Pipeline broker fetch: narrow the catch, fix the doc

**✅ SHIPPED 2026-07-20 (PR #845).**

**Branch:** `fix/pipeline-broker-catch`. **No migration.** Frontend + docs in ONE PR
(the same-PR-doc rule is exactly what F12 flagged).

1. `frontend/src/lib/queries.ts:2158-2171` — replace both catches; degrade silently **only**
   on the observed A6 signature (`PostgrestError.code === '42501'` — it is a hard grant
   revoke, live-verified, not an RLS-empty result), `console.error` + degrade on anything
   else (the SPA has zero telemetry — grep confirms no `console.*` in queries.ts today);
   drop the redundant `listingBrokers.size === 0` ternary (callee short-circuits,
   `brokers.ts:205` — keep the two coupled with a comment):

```ts
const brokerMaskExpected = (err: unknown): boolean =>
  (err as { code?: string } | null)?.code === '42501';
const listingBrokers = await fetchListingBrokersByIds(srealityIds).catch(
  (err): Map<number, ListingBroker> => {
    if (!brokerMaskExpected(err))
      console.error('fetchPipelineBoard: listing_broker_public read failed', err);
    return new Map();
  },
);
const brokerContacts = await fetchBrokersByIds([
  ...new Set([...listingBrokers.values()].map((b) => b.broker_id)),
]).catch((err): Map<number, BrokerPublic> => {
  if (!brokerMaskExpected(err))
    console.error('fetchPipelineBoard: brokers_public read failed', err);
  return new Map();
});
```

2. `docs/architecture.md:1007-1011` — rewrite the broker-per-card clause to state the truth:
   both broker views are dark (42501) to anon+authenticated under A6 until Wave 4, both
   fetches degrade to "no broker shown" on that signature (log-then-degrade on anything
   else), the board loads without the broker box until the mask lifts.

---

## Explicit non-actions

- **F6:** no gate rewrite. The bare standalone `WHERE is_platform_admin()` is already a
  One-Time Filter (live EXPLAIN, 6 objects incl. both heavy views). A `(SELECT …)` wrap
  would change nothing measured; do not cargo-cult it. The 318:18-22 comment's "exactly
  like properties_public" parity claim is imprecise (that clause is per-row-bound to
  `published_at`) — worth a one-line correction only if a migration touches these views anyway.
- **F14:** no mechanized re-emit of the 23 view bodies. No prior migration emits view bodies
  via loops; `CREATE OR REPLACE VIEW` rejects column drift, bounding transcription risk. If a
  future triage round regates in bulk, a `pg_get_viewdef` + exactly-one-gate-occurrence
  replace loop is the pattern (sketch preserved in the review-verification transcript).

## Recorded product decision (default chosen; operator can override)

`property_estimates_public` returns to **market-wide** visibility (its designed intent, migs
173/311: estimate-existence per property — id, run_count, last_run_at; never values/inputs).
Once multiple accounts exist this discloses *that* some account estimated a property. If the
operator wants it private later, the alternative is an account-scoped rewrite resolving
`property_id` via `browse_list` instead of `listings` under invoker rights (Option B in the
verification transcript) — revisit at Wave 1, do not build now.

## Feed into the Phase-1 exit re-audit

When `/code-review ultra` runs as the exit gate, point it at: (1) the mig-316 class
(`security_invoker` on views joining zero-policy shared tables — the F1 anchor case);
(2) admin aggregates over `listings`/`properties` only (the CI gate's documented blind spot);
(3) request-GUC dependencies (`request.jwt.claims`) reachable from cron/scripts (the F2/F3
class); (4) grant drift added by migrations after a one-time revoke sweep (now caught by the
standing anon test, but worth an independent look at `authenticated` too).
