# Public release — accounts, multi-tenancy, billing

Taking the single-operator platform public: user accounts, per-account private state,
email+Google login, Stripe billing, admin-gated internals, shared scraped market data
common to all. Full plan, sequencing, and gates: `docs/design/public-release-program.md`
(index) → `phase-0-emergency-hardening.md`, `phase-1-multitenancy-foundations.md`
(Amendments A1–A10), `waves-1-4-public-features.md`.

## Status

- **Phase 0 (emergency hardening)** — **DB hardening applied live** 2026-07-13 (migration 299
  via Supabase MCP): anon revoked to ~nothing, authenticated loses write on shared tables,
  25 internal tables RLS-enabled, 8 dangerous DEFINER funcs locked, broker-PII surfaces (A6)
  dark to `authenticated` too. **API hardening + the CI grant-gate are still NOT merged** —
  PR #775 (`fix/phase0-anon-hardening`) has sat open since 2026-07-13, CI was green as of
  that run but the branch is 7 days stale against main and needs a mergeability re-check.
  Consequence: `require_token` on `main` today is still **fail-OPEN** (silently disables auth
  if `API_TOKEN` is ever unset on Railway), `/docs` is still publicly enumerable, and `main`'s
  `migrations/` directory is **missing files 299 + 301** that are already applied live (only
  present on the PR branch) — the CI schema-replay lane is testing a laxer schema than
  production actually has. **Top priority: get #775 rebased + merged** (confirm `API_TOKEN`
  is set on Railway first, per the PR's own checklist).
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
supabase-js read. Related: 3 SPA-called `SECURITY DEFINER` functions
(`images_failure_overview`, `recent_workflow_failures`, `workflow_failure_summary`) execute
for any `authenticated` caller with no `is_admin` check inside — the Phase 0 doc's "deferred
to Phase 1 admin-gating" was never actually closed at the function level, only at the
route/API level. **Not yet triaged or fixed** — needs a per-view pass (does it need
`security_invoker` + an admin-only RLS policy, or should the SPA read it through the gated
API instead?), unlike the 8 tenant views this is genuinely more work, not a one-line flip.

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

## Next

1. **Rebase + merge PR #775** (Phase 0 API hardening + CI grant-gate) — confirm `API_TOKEN` is
   set on Railway first (its own checklist item), since the fail-closed change 503s the API if
   it's missing. This also lands `migrations/299_*.sql` + `301_*.sql` on `main`, closing the
   repo/live migrations-directory drift.
2. **Triage the remaining ~26 non-`security_invoker` admin-ops views** + the 3 gap-in-function
   DEFINER functions — decide per-surface: add `security_invoker` + an `is_platform_admin()`
   RLS policy, or move the SPA read behind the gated API. Use migration 316 + its regression
   tests as the template.
3. Phase 1 exit gate: external re-audit (`/code-review ultra`) — point it explicitly at the
   `_public` view / `security_invoker` class of bug found today, since the existing pen-test
   suite structurally couldn't have caught it. Then the 2-account pen-test can be considered
   to actually cover the SPA's real read path (item 2 landed the missing view-path test).
4. Wave 1 (extension + agent estimations: quotas, async job lane, Stripe checkout + metering).
5. Housekeeping: deny-all RLS migration for `dedup_model_compare_sets`; enable Supabase Auth's
   leaked-password-protection toggle before public signup.
