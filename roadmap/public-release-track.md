# Public release — accounts, multi-tenancy, billing

Taking the single-operator platform public: user accounts, per-account private state,
email+Google login, Stripe billing, admin-gated internals, shared scraped market data
common to all. Full plan, sequencing, and gates: `docs/design/public-release-program.md`
(index) → `phase-0-emergency-hardening.md`, `phase-1-multitenancy-foundations.md`
(Amendments A1–A10), `waves-1-4-public-features.md`.

## Status

- **Phase 0 (emergency hardening)** — **fully shipped 2026-07-20.** DB hardening applied live
  2026-07-13 (migration 299 via Supabase MCP): anon revoked to ~nothing, authenticated loses
  write on shared tables, 25 internal tables RLS-enabled, 8 dangerous DEFINER funcs locked,
  broker-PII surfaces (A6) dark to `authenticated` too. **PR #775 merged 2026-07-20** (after
  7 days open, rebased cleanly onto main with zero conflicts) — `require_token` is now
  fail-CLOSED (verified behavior-preserving first: the live API already 401s unauthenticated
  requests, so `API_TOKEN` was already set on Railway), `/docs`/`/redoc`/`/openapi.json` are
  hidden, and `tests/test_migration_rls_grants.py` (the anon-write-grant + RLS-on-new-tables
  CI gate) is now enforced on every push to `main`. That merge surfaced one more gap the gate
  itself caught: `dedup_model_compare_sets` (migration 304) had no RLS — closed same-day via
  migration 317 (same pattern as 301), applied live and verified before pushing.
  `migrations/299_*.sql` + `301_rls_dedup_golden_sets.sql` are now on `main` too, closing the
  repo/live drift where CI's schema-replay was testing a laxer schema than production.
- **Phase 1 (multi-tenant foundations)** — in progress.
  - Increment 1 ✅ — accounts/account_members/admins, `current_account_ids()` /
    `is_platform_admin()`, the on-signup handler, JWT verify (JWKS/ES256) (migrations
    286+287, PR #747). Google OAuth + Railway `SUPABASE_URL` configured by the operator.
  - Increment 2 ✅ — login made visible: account menu (sign-in / signed-in-as / logout)
    in the app Shell. Purely additive, no route gated yet.
  - Increment 3 ✅ — the tenant DB pool + `account_id`/RLS across the 18 user-state
    tables + the pipeline PK rewrite (property_pipeline → `(account_id, property_id)`) +
    the real-Postgres CI isolation lane (migrations 290–295, PR #763). Verified live:
    two-account denial, lossless operator backfill, fail-closed cross-account writes.
  - Increment 4 ✅ — login gate (logged-out → /login), admin-gated nav + 10 admin pages
    code-split behind the is_admin claim, require_admin on the admin-class API routes
    (PR #765).
  - Increment 5 ✅ — billing skeleton: plans/entitlements/webhook-idempotency tables
    (migration 298), signature-verified Stripe webhook (stdlib HMAC, no SDK),
    `require_entitlement` gate, admin Tiers & agenda-visibility screen in Settings,
    plan-driven tenant nav. Stripe products/checkout flow still to come with Wave 1
    metering; `STRIPE_WEBHOOK_SECRET` on the API service arms the webhook.
- **Waves 1–4 (public features)** — not started; gated on Phase 1's exit (RLS lane green +
  external re-audit + 2-account pen-test).

**CRITICAL finding + fix, 2026-07-20:** the 2-account pen-test
(`tests/test_tenant_isolation_live.py`) only ever asserted RLS on **base tables**
(`SET LOCAL ROLE authenticated; SELECT * FROM collections`) — but the SPA never reads base
tables, only the `*_public` views. None of those views (`collections_public`,
`property_pipeline_public`, `pipeline_stages_public`, `property_notes_public`,
`property_tags_public`, `tags_public`, `collection_properties_public`,
`property_estimates_public`) were ever created with `security_invoker = true` (checked every
migration back to 022/025/202/203/205/211/278 — it was never built, not a regression). A
Postgres view without that option runs as its **owner** (`postgres`, `rolbypassrls = true`),
so it bypasses RLS entirely regardless of who queries it — every `authenticated` session was
reading **every account's** collections/tags/notes/pipeline/estimates, not just its own.
Invisible until now only because exactly one account exists. **Fixed live**: migration 316
(`ALTER VIEW ... SET (security_invoker = true)` on all 8), verified with a foreign JWT
(0 rows) vs the real account (unchanged row counts) — no grant/permission fallout, since
`authenticated` already needed base-table grants for the tenant-pool API writes to work at
all. Regression coverage added: `test_tenant_views_are_security_invoker` (static, all 8) +
`test_cross_tenant_denial_through_public_view` (live, reproduces the exact bug). **This must
be the anchor case in the exit-gate external re-audit** — the class of bug (`_public` view
missing `security_invoker`) is the thing to search for exhaustively, not just these 8.
Supabase's advisor (`security_definer_view`) flags **53 views total**; most are legitimately
open shared-market data (no RLS to bypass), but a same-day pass found ~26 more that read
**admin-only operational tables** (dedup tooling, health, price-stat run internals, LLM cost,
pipeline-checks) through the identical bypass — currently harmless (the one authenticated
user IS the admin) but a real gap the moment Wave 1 signs up a non-admin tenant, since
frontend route-gating (`RequireAdmin`) and API `require_admin` don't apply to a direct
supabase-js read.

**Triaged + fixed the same day (migration 318):** a 29-agent live audit (Opus, one agent per
flagged view/function) classified all 26 views + the 3 gap `SECURITY DEFINER` functions
(`images_failure_overview`, `recent_workflow_failures`, `workflow_failure_summary`, which
execute for any `authenticated` caller with no `is_admin` check inside — the Phase 0 doc's
"deferred to Phase 1 admin-gating" was never actually closed at the function level). 23 views
+ all 3 functions are genuinely admin-only operational data with no legitimate non-admin SPA
reader (confirmed per-object: base tables, RLS state, grants, and every frontend call site).
3 views were correctly left alone: `browse_read_model_state_public` / `portal_listing_counts`
(non-sensitive aggregate metadata) and `listing_freshness_checks_public` (a genuine non-admin
feature — the Listing Detail "verify freshness" button reads it for any signed-in user).

The fix deliberately does **NOT** reuse migration 316's technique (`security_invoker` + a
base-table RLS policy): 7 of the flagged views read the shared `listings`/`properties`/
`images` tables directly, which have carried RLS-enabled-with-**zero**-policies since early
migrations specifically so their owner-bypass views (`listings_public`, `properties_public`,
...) can keep serving shared-market reads to every authenticated user — adding a restrictive
policy directly to those tables would risk every other reader of them for a narrow, low-value
fix. Instead each of the 26 objects is redefined to embed `is_platform_admin()` as a plain
query filter (the same technique `properties_public` already uses for
`publication_gate_enabled()`) — evaluated per-request, independent of RLS/security_invoker/
ownership, so it needs **zero** changes to `listings`/`properties`/`images` or any of the 15
other base tables' grants or policies. Applied live and verified both directions before
committing: a foreign JWT sees 0 rows across all 26 objects; the real admin JWT sees unchanged
data; `listings_public`/`properties_public` reads confirmed completely unaffected. Regression
coverage: `test_admin_ops_views_embed_is_platform_admin` (static) +
`test_admin_ops_views_deny_non_admin_allow_admin` (live, promotes a test user into `admins`
mid-test to prove the gate opens correctly, not just closes).

**Also found the same day (lower severity, worth a cleanup pass):** `dedup_model_compare_sets`
(migration 304) shipped without RLS — same pattern mig 300/301 already hit, currently
protected only by the mig-299 default-ACL fix granting it zero browser access, deserves its
own deny-all migration like 301 did. Supabase Auth's leaked-password-protection toggle is
still off (dashboard setting, not a migration) — cheap to flip before Wave 1 public signup.

**Also fixed the same day:** the Pipeline board (`/pipeline`) was failing to load
("Nepodařilo se načíst pipeline.") — `fetchPipelineBoard` (`frontend/src/lib/queries.ts`)
enriches each card with a canonical broker (shipped 2026-06-19, PR #519) by querying
`listing_broker_public` + `brokers_public`, both of which the A6 decision (2026-07-13)
correctly darkened for `authenticated` — but that pre-existing Pipeline dependency wasn't on
anyone's radar when A6 shipped, and the fetch had no error isolation, so the 403 on broker
data failed the *entire* board query. Fixed: both broker fetches now degrade to "no broker
shown" on error instead of failing the board — consistent with A6's intent (broker data stays
dark until Wave 4), stages/cards/properties/images all still load. No other caller of these
two fetch helpers exists in the app (grep-verified) — this was the only broken surface.

**Post-ship review verified, 2026-07-20 (evening):** a 14-finding code review of the deployed
316–319 batch was adversarially re-verified against the live DB (6-agent workflow): 11
confirmed, 1 refuted (the per-row-gate perf claim — live EXPLAIN shows a One-Time Filter),
2 partial. **Three findings are live P0 regressions shipped by 316/318:** (1) `security_invoker`
on `property_estimates_public` (a market-wide view mis-grouped with the 7 tenant views) empties
Browse's "with estimates" filter + the Stats RPC for every user; (2) `is_platform_admin()`
returns false on any connection without `request.jwt.claims` — the golden-set freeze script is
a silent no-op; (3) same cause: the pg_cron health-matview refresh has poisoned the Health
dashboard to all-zeros while 1485 fetch-failures / 895 queue rows exist. Verification also
found two exposures the review missed (anon can read `listing_natural_key_public` +
`property_estimates_public` — 7 drifted anon-readable views total; all 13 matviews still carry
full pre-299 `authenticated` ACLs, bypassing the 318 gate) and one fix-path landmine (a
`current_user`-keyed fallback would open the admin gate to everyone; only `session_user`
works under SECURITY DEFINER). Note: the claim above that the suite "now can" catch the 316
class on its own was overstated — only `collections_public` had a live through-view test;
remediation R3 closes that. Full spec: `docs/design/public-release-remediation-2026-07.md`.

## Next

1. ~~**R1 (P0 hotfix)**~~ — **SHIPPED 2026-07-20, migrations 329 + 330** (320-328 were
   taken by the listing-identity track). Reverted `security_invoker` on the market-wide
   `property_estimates_public` + gave `is_platform_admin()` a claims-absent fallback keyed on
   `session_user` **and** the `role` GUC. Live-verified: `dedup_label_events` 0 → 809 rows
   (golden-set freeze works again), fetch-failures 0 → 1485, detail-queue 0 → 680, and the
   Health matviews healed through the real pg_cron tick (sreality now reports `38 active`
   fetch failures, matching the base table exactly). Deny direction intact: foreign JWT and
   claims-less `SET ROLE` both stay non-admin. The two-migration split is explained in the
   spec's R1 addendum — 330 closes a CI-replay fidelity gap 329 left open.
2. ~~**R2 (public-release blocker)**~~ — **SHIPPED 2026-07-20, migration 331.** Revoked the
   7 drifted anon view grants (anon now reads **nothing**: 7 → 0, including
   `listing_natural_key_public`, which was dumping every listing's natural key, and
   `property_estimates_public`), took `authenticated` SELECT off the 3 matviews that bypassed
   migration 318's admin gate, and stripped DML/`MAINTAIN` off all 13 — closing migration
   299's `relkind in ('r','p','v')` gap that skipped matviews entirely. Three standing tests
   added. The health-matview repoint is deliberately deferred (rationale in the spec).
3. ~~**R2b (new finding)**~~ — **SHIPPED 2026-07-20, migration 332.** The live audit for R2
   turned up **five** (not two) ungated admin-ops RPCs — `health_summary`,
   `portal_health_summary`, `scraper_health_checks`, `category_trends`,
   `image_storage_overview` — all SECURITY INVOKER with EXECUTE to `authenticated`, all
   feeding only the admin Health dashboard. Migration 318 missed them because its triage
   worked from the `security_definer_view` advisor list and these are plain SQL functions;
   SPA route-gating is a client affordance, not a boundary. They were also *why* 331 could
   not revoke matview SELECT — an INVOKER function reads as the caller — so 332 does both
   halves together: DEFINER + gate, then revoke `authenticated` on the 7 ops matviews.
   Live-verified both directions (admin reads 16 sreality checks; foreign JWT gets NULL from
   all five and cannot read the raw matviews).
4. ~~**R3**~~ — **SHIPPED 2026-07-20.** The cross-tenant live test is now
   parameterized over all 7 tenant views with a **read-your-own-row** assertion per
   view — the half that was missing, and the assertion that would have failed CI on the
   migration-316 PR instead of shipping the Browse regression to production. The static
   gate test now requires `is_platform_admin()` in a WHERE position (a bare substring
   check passed `true OR is_platform_admin()`), and a **standing gate** in both lanes
   flags any view or authenticated-callable function reading admin-only data without the
   gate — generalizing to admin surface #27 without anyone remembering to register it,
   which is what the deferred-to-re-audit posture could not do. Verified live: both gates
   return zero today, and the offline gate was tested against a synthetic offender.
5. **R4** — Pipeline broker fetch: degrade only on the A6 42501 signature, log anything else;
   update the stale `docs/architecture.md` broker paragraph in the same PR. § R4.
6. Phase 1 exit gate: external re-audit (`/code-review ultra`) — anchor cases + blind spots
   listed in the remediation doc's final section.
7. Wave 1 (extension + agent estimations: quotas, async job lane, Stripe checkout + metering).

**Housekeeping done 2026-07-20:** operator enabled Supabase Auth's leaked-password-protection
toggle (Authentication → Sign In / Providers → Email → "Prevent use of leaked passwords").
