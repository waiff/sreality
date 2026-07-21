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
- **Phase 1 exit gate — CLOSED 2026-07-21.** The external re-audit ran against PR #856 and
  its 13 findings were remediated in full (round 2, PRs A–G, migrations 340–342); the RLS
  lane and the 2-account pen-test are green, and both standing gate lanes (offline over
  migration SQL, live over `pg_views` + authenticated-callable functions) are now
  adversarially validated rather than merely asserted.
- **Waves 1–4 (public features)** — **Wave 1 is now unblocked** and is the next body of
  work; Waves 2–4 follow its ordering constraints (`docs/design/waves-1-4-public-features.md`).

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
5. ~~**R4**~~ — **SHIPPED 2026-07-20 (PR #845).** Both Pipeline broker fetches now degrade
   silently only on the A6 signature (SQLSTATE `42501` on `PostgrestError.code` — verified
   live to be a revoked grant, not an RLS-empty result) and `console.error` before degrading
   on anything else, so a genuine regression is no longer indistinguishable from the
   permanent mask. Dropped the redundant empty-Map ternary, and corrected the
   `docs/architecture.md` paragraph that still described the broker box as working.
6. ~~Phase 1 exit gate: external re-audit~~ — **RAN 2026-07-21** against the review-only
   PR #856 (the whole 329-332 batch as one diff). 13 findings; all re-verified live before
   planning (11 confirmed, 1 refuted, 1 partial, one escalation the audit missed: the
   MAINTAIN revoke had already drifted back via the postgres default ACL on the
   `properties_map_mv` blue-green rebuild). One live exposure (`scrape_runs_public` +
   `recent_scrape_runs()` readable by any signed-in user), the rest latent/lane-hardening.
   **Round-2 fix plan: `docs/design/public-release-remediation-round2.md`** — 7 PRs
   (A: scrape_runs gate; B: per-account estimates scoping; C: durable MAINTAIN revoke;
   D: gate-lane honesty — seeds, OR-evasion, parser hardening, coverage floor 299;
   E: API require_admin route-coverage test; F: docs/skill corrections; G: CI replay
   PG15→17). **All seven shipped 2026-07-21** (migrations 340/341/342) — details below.
7. ~~**Round-2 remediation (PRs A–G)**~~ — **COMPLETE 2026-07-21.** Migrations 340–342.
   - **A** (mig 340, #863) — the audit's one live exposure: `scrape_runs_public`
     7 945 → 0 rows and `recent_scrape_runs()` 2 166 → 0 for a non-admin.
   - **B** (mig 341, #864) — per-account scoping on `property_estimates_public`. The
     naive "own accounts OR admin" predicate returns **zero** rows live (every run sits on
     the shared SYSTEM account `0000…0000`, also the column DEFAULT); the view mirrors
     **all three** RLS arms or Browse's "with estimates" filter empties again (mig-316 déjà vu).
   - **C** (mig 342, #865) — MAINTAIN revoked at the **postgres DEFAULT ACL**, not
     per-relation: mig 331's one-time revoke had been undone within a day by the 30-min
     `properties_map_mv` blue-green rebuild. 85 holders → 0.
   - **D** (#868) — made the standing gates provable: `gate_is_sound` rejects OR'd and
     tautology gates (35/35 live accepted, 8/8 adversarial rejected), a string/dollar-quote-aware
     SQL scanner (the old regex let a literal containing `/*` swallow whole CREATE statements),
     coverage floor 333→299 with an 8-entry historical exemption set. The deny test was
     **vacuous** on the empty CI DB — it now seeds 17 views and asserts reachability before denial.
   - **E** (#869) — standing test that every admin-prefixed API route carries `require_admin`.
     FastAPI ≥0.13x `include_router` appends one `_IncludedRouter` wrapper instead of splicing,
     so route-table introspection must recurse via `original_router.routes`. Live app = 190
     route-method pairs (88 admin / 89 token / 10 tenant / 3 public); **no ungated admin route**.
   - **F** (#871) — the `database` skill's per-row-gate rule split into its three real cases,
     plus a lock-timeout rule for GRANT/REVOKE on cron-refreshed relations.
   - **G** (#872) — CI schema replay PG15 → PG17 to match prod 17.6; the version gap is
     exactly what let the MAINTAIN drift go unnoticed. Passed first try.
8. Wave 1 (extension + agent estimations: quotas, async job lane, Stripe checkout + metering).
   - ~~**W1-1 (IDOR fix: route scoping + account stamping)**~~ — **SHIPPED, migration 347.**
     Verified against `origin/main` first (`docs/design/waves-1-4-public-features.md`'s
     "Phase 1 supplies every primitive" premise was wrong — `usage_ledger`/`check_budget`
     don't exist, corrected same-day, PR #882). Moved the extension-touched estimation +
     curation routes onto `verify_jwt`/the tenant pool: `GET/PATCH /estimations/{id}`
     (+ `/scenario`), `GET /collections`, `POST/DELETE /collections/{id}/properties`,
     `GET/POST /properties/{id}/notes`. `POST /estimations` stays on the service-role
     connection (moving execution off the request process is W1-3) but now resolves +
     stamps `account_id` on every INSERT via `tenant_pool.resolve_account_id` — closing the
     gap where every run silently landed on the SYSTEM account regardless of caller.
     `estimation_runs.source` gained `'extension'` (CHECK + Pydantic lockstep); a new
     `llm_calls_tenant_read` RLS policy scopes `GET /estimations/{id}`'s cost subselect
     through the owning run's account (the table carried RLS-enabled-with-zero-policies
     like every migration-299 table, so it would have silently shown cost_usd_total=0 to
     every real per-account caller otherwise). **Deliberately deferred, not fixed here:**
     `listings`/`properties` still have zero `authenticated` read policies on the base
     tables (Amendment A5) — verified live they carry `broker_email`/`broker_phone`/
     `raw_json` directly, so a blanket policy would leak broker PII over Supabase's
     auto-REST; needs a column-safe approach (redacted view or narrow function), not a
     table-wide policy. `list_estimation_runs`'s locality-display JOIN and
     `POST /listings/lookup` degrade gracefully (NULL/unscoped) for real per-account JWTs
     until that lands — cosmetic today since 100% of traffic is still the legacy
     static-token bridge. Behavior-preserving for every current caller (verified against
     live account/backfill-claim state before writing code); only becomes a live boundary
     once the extension's own `chrome.identity` session (still ahead) sends real user JWTs.
   - ~~**Extension login session**~~ — **SHIPPED 2026-07-21.** The extension now runs its
     own Supabase session — the live boundary W1-1 set up for. Hand-rolled PKCE (no
     supabase-js in the vanilla-TS bundle): `chrome.identity.launchWebAuthFlow` opens
     Google's consent screen against GoTrue's `/authorize`, the background worker exchanges
     the returned code for `{access_token, refresh_token, expires_at}` at `/token?grant_type=pkce`
     and stores it in `chrome.storage.local`; refresh is lazy-in-the-fetch-wrapper (near
     expiry) plus a `chrome.alarms` ~30 min periodic (MV3 evicts any in-memory timer),
     single-flighted so the two paths can't race Supabase's refresh-token reuse-detection.
     `VITE_API_TOKEN` deleted from the bundle entirely — every extension-touched route
     already ran on `verify_jwt` (W1-1), so this was pure auth-plumbing with **zero backend
     changes**. `manifest.json` gained a `key` (a generated RSA keypair's public half) so
     "Load unpacked" gives the same extension ID everywhere, needed because the GoTrue PKCE
     redirect URL (`https://<id>.chromiumapp.org/`) must be pre-registered with both Supabase
     and Google; `host_permissions` narrowed from `https://*/*` to just the API + Supabase
     origins, computed at build time in `vite.config.ts` from the same env vars that inline
     into the bundle. The panel shows a "Přihlásit se přes Google" prompt when signed out
     and a compact email + "Odhlásit" line when signed in — no separate popup surface.
     **Operator follow-up needed** (dashboard access only, can't be done from a session):
     register the pinned redirect URL in Supabase's Additional Redirect URLs + the Google
     OAuth client's Authorized redirect URIs, and add the `EXT_SUPABASE_URL` /
     `EXT_SUPABASE_ANON_KEY` GitHub Actions secrets (replacing the retired `EXT_API_TOKEN`)
     — exact steps in `chrome-extension/README.md` § Sign-in. The private half of the
     generated keypair was handed to the operator out-of-band (not committed, only the
     public half lives in `manifest.json`). **Verified live end-to-end 2026-07-21** (real
     Google sign-in → panel loads real data).
   - **Latent P0 the first real JWT exposed:** Railway's `TENANT_POOL_DB_URL` carried a bare
     `tenant_pool` username, but the Supabase **shared** pooler routes by a project-ref
     suffix (`tenant_pool.<ref>`) and rejects anything else with `FATAL: (ENOIDENTIFIER) no
     tenant identifier provided`. Every tenant-pool route 500'd the moment a real user JWT
     arrived. Proven with a raw Postgres startup-packet probe (bare → the exact error;
     suffixed → proceeds to auth), fixed by the operator in the Railway dashboard.
     **It had been dead since the tenant pool shipped (increment 3, migration 293)** and was
     unobservable because `tenant_conn`'s legacy branch sends static-`API_TOKEN` callers to
     the service-role connection — so no production request had ever run that code path.
     Consequences for what's left: Wave 2's pipeline writes and Wave 3's notification routes
     were queued up behind the same dead config, and the launch-gate two-account pen-test
     **must run against real user JWTs** — a green RLS lane says nothing about a DSN that
     production never dials.
   - Remaining: the metering substrate (`usage_ledger`/`check_budget` — needs a product
     decision on quota shape: free-tier run count vs USD budget, daily vs monthly window),
     the atomic submit-time gates (entitlement/budget/concurrency/idempotency), moving agent
     execution off the request process onto a job lane on the realtime worker, a periodic
     zombie-run sweep, and Chrome Web Store submission readiness (privacy policy,
     single-purpose statement, staged rollout, the platform-wide `API_TOKEN` rotation
     cutover — see `chrome-extension/README.md` § Chrome Web Store submission).

**Housekeeping done 2026-07-20:** operator enabled Supabase Auth's leaked-password-protection
toggle (Authentication → Sign In / Providers → Email → "Prevent use of leaked passwords").
