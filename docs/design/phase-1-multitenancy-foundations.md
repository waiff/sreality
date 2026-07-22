# Phase 1 — Multi-tenant foundations (design)

**Status:** design/planning. The one large lift between Phase 0 (which closed the live holes but added no tenancy) and the first public wave. Turns the single-operator system into a multi-tenant SaaS: real per-user auth, per-account private state enforced by the **database**, admin-gated internals, a billing skeleton, and the abuse controls metered compute needs. Nothing public ships until this lands and passes the exit gate.

**Design principles (the operator's stated goals — unification, low maintenance, no patchwork, no tech debt):**
- One ownership rule, enforced in the DB via RLS, referenced through **one SQL helper** → "teams later" is a one-function change, not a 19-policy migration.
- **Fail-closed by construction** — the API user-write path runs as a non-superuser role, so a forgotten scope is denied by RLS, never written as superuser.
- Additive, reversible migrations; the operator **keeps working daily** through a dual-auth window (no big-bang cutover).
- Shared market data stays common to all; only user state becomes private.

**Prerequisite:** Phase 0 applied (anon writes revoked, default ACL fixed, RLS-off tables closed). This design assumes that baseline.

---

## 1. Identity & accounts model

A billing account is **not** an auth user (a paid account may have several logins; the operator's 63 workflows have no auth user at all). Model the indirection now so teams/seats never force a second migration of the same blast radius.

| Table | Shape | Purpose |
|---|---|---|
| `accounts` | `id uuid pk, kind text check (personal\|team\|system), created_at, stripe_customer_id` | the tenant |
| `account_members` | `account_id fk, user_id uuid fk→auth.users, role (owner\|admin\|member), pk(account_id,user_id)` | who belongs where |
| `admins` | `user_id uuid pk→auth.users` | platform-admin allowlist (source of truth for the `is_admin` claim) |

- **One fixed `system` account** (hard-coded UUID) owns every platform/system-written row, so `account_id` can be **`NOT NULL` everywhere** — no NULL-owner ambiguity, and a bypassed write fails loud instead of writing a globally-visible orphan.
- **On signup:** a handler (Supabase Auth "on user created" hook, or a trigger on `auth.users`) creates a `personal` account + an `owner` membership. The founding operator becomes **account #1**; all existing user-state backfills to it (§11 step 2).
- **`current_account_ids()`** — the linchpin. A `STABLE SECURITY DEFINER` SQL function returning the `account_id`s the JWT's `sub` belongs to:
  ```sql
  create function current_account_ids() returns setof uuid
  language sql stable security definer set search_path = public as $$
    select account_id from account_members
    where user_id = (current_setting('request.jwt.claims', true)::jsonb ->> 'sub')::uuid
  $$;
  ```
  **Every RLS policy references this one function.** Today it returns the caller's single personal account; teams later = it returns the membership set — zero policy changes.

## 2. Authentication

- **Provider: Supabase Auth (GoTrue)** — email + Google. `supabase-js` (already installed) bundles it, so the SPA gets login with **no new frontend dependency**.
- **Custom SMTP via Resend** (`api/transports/email_resend.py` exists) — Supabase's built-in mailer caps at a few emails/hour. Dedicated auth subdomain with SPF/DKIM/DMARC, **separate** from the notification stream so a notification-spam complaint can't break password resets.
- **FastAPI JWT verification** — the one justified new backend dependency (`PyJWT`). Verify against Supabase's **asymmetric JWKS** (ES256 signing keys, not the legacy shared HS256 secret — the modern, rotation-friendly path). A `verify_jwt` dependency yields the validated claims; it slots in exactly where `require_token` sits today (`api/dependencies.py`).
- **Dual-auth window (the safety net):** during migration the API accepts **both** the legacy static bearer (the operator's current SPA/extension keep working) **and** a Supabase JWT. The legacy bearer is retired only at the very end (§11 step 5), after the last old extension build ages out.
- **Admin role:** an `admins` table is the source of truth; a **Custom Access Token Hook** stamps `is_admin` into `app_metadata` so it rides in the JWT (fast, no per-request lookup). A `require_admin` dependency checks the claim.
- **Signup abuse controls Supabase supports:** Turnstile/hCaptcha on signup, **mandatory** email verification, disposable-domain blocking.

## 3. Tenancy enforcement — Option C, hardened (two DB roles, two pools)

The API connects as superuser (`postgres`) today, which **bypasses RLS**. Keeping that for user-writes means a forgotten scope is a silent superuser write. Fix: **two connection pools**.

| Pool | DB role | RLS | Used for |
|---|---|---|---|
| **service** | `postgres` / service-role (BYPASSRLS) | bypassed by design | scraper, realtime worker, 63 workflows, admin routes, analytics/dedup reads, the multi-minute agent loops |
| **tenant** | a new **non-superuser** login role (no BYPASSRLS), granted only what `authenticated` needs | **enforced** | end-user reads/writes; each request does `set local request.jwt.claims = '<verified>'` inside its transaction |

- A user-write endpoint that forgets to use the tenant pool **fails closed** (RLS denies) — the failure mode inverts from "silent leak" to "loud error".
- **Browser path:** `supabase-js` as `authenticated` reads user-state through `security_invoker` views; RLS + `current_account_ids()` return only the caller's rows — zero app code.
- **`account_id uuid NOT NULL`** on every user-state table; default resolves from the claims under the tenant role; system/service writes set it to the system account explicitly.
- **Policy shape (identical across all user-state tables):**
  ```sql
  alter table <t> enable row level security;
  create policy <t>_tenant on <t> for all to authenticated
    using (account_id in (select current_account_ids()))
    with check (account_id in (select current_account_ids()));
  ```

## 4. The user-state work list (what changes per table)

**Add `account_id` + RLS + re-scope uniques (the definitions):** `collections` (unique `lower(name)` → `(account_id, lower(name))`; per-account system "monitoring" collection seed), `tags` (same), `property_notes`, `filter_presets` (column only), `notification_subscriptions` (column only), `manual_rental_estimates` (product decision — per-account private comps vs admin ground-truth; see §Open), `estimation_feedback`.

**Add `account_id` for RLS, grain inherited via parent:** `collection_properties`, `property_tags`, `notification_dispatches`, `channel_sends`, `estimation_cohort_entries`, `estimation_trace_payloads`, `building_run_attachments`.

**Nullable-then-system-account:** `estimation_runs`, `building_runs` (platform/system runs → system account; user runs → caller). Per-run cost auto-attributes via `llm_calls.estimation_run_id` — no `account_id` needed on `llm_calls` itself.

**The one hard schema break — the deal pipeline:**
- `property_pipeline` PK `property_id` → **`(account_id, property_id)`**.
- `pipeline_stages`: per-account boards — `is_entry` unique becomes `(account_id) where is_entry`; `key` unique becomes `(account_id, lower(key))`; **copy the 5-stage default board on signup** (stage ids stay FK-safe per account).
- `property_pipeline_events` → `+account_id` (so unmerge restores the right user's snapshot).
- `reconcile_pipeline_on_merge` (`toolkit/pipeline_identity.py`) and the "single-entry crown" logic (`api/pipeline.py:176`) rewritten to fan out per account.

**Stays GLOBAL platform content (NO `account_id`, curation becomes admin-only):** `curated_cities`, `city_index_*`, `city_population`, `rent_map_*`, `skills`/`skill_refinements` (user feedback stays a per-account signal, but applying refinements to the global skill prompt becomes an admin step), `filter_visibility`, `region_disposition_annotations`, `assets`/`properties.asset_id`, all `broker*` identity/review tables, **`broker_outreach_suppression` (global by GDPR design)**, the `dedup_*` review corpus. New per-user **preferences** (notification email/telegram endpoints, currently masquerading as global `app_settings` keys) go in a **new `account_settings(account_id, key, value)`** table — not a widen of `app_settings` (whose history + admin UI assume global keys).

## 5. System writers must become account-aware

These write user-state tables as **service-role (no JWT)**, so they must set `account_id` **explicitly** — the `DEFAULT` can't reach them, and a NULL/orphan owner would be a cross-tenant bug:
- `carry_operator_state_on_merge` + `reconcile_pipeline_on_merge` — derive `account_id` from the owning collection/subscription/card; **the set-dedup collapse keys gain `account_id`**, else a merge over a **shared** property collapses two tenants' private rows together (cross-tenant *corruption*, not just a leak).
- watchdog + collection-monitor producers — stamp `account_id` from the owning subscription/collection.
- **The metered revenue path:** `estimation_runs` is persisted in a **post-response `BackgroundTask`** (service-role, no JWT), so `account_id` must be **hand-threaded** from the request's verified claims into the task. This is the one place app-level threading is unavoidable — flag it explicitly in the estimation code.

## 6. Admin gating

- `require_admin` (the `is_admin` claim) re-gates the **70 admin routes** (admin/dedup/broker-review/outreach/price-stats-dispatch/skills).
- SPA: **code-split** the 10 admin pages out of the default bundle; lazy-load behind the admin claim (admin code + API shapes stop shipping to end users).
- **Read-model bifurcation** (deliberate): admin data reads via the **service-role API** (an admin's own `auth.uid()` can't see other tenants under RLS); user + shared-market data via `supabase-js` direct. Admin never touches the tenant read path.

## 7. Billing skeleton (Stripe)

- `accounts.stripe_customer_id`; a new `entitlements(account_id, plan, features jsonb, status, current_period_end)` table.
- **Webhook = a new auth class**, distinct from `require_token`: signature-verified (`stripe-signature`), token-exempt, **idempotent** (dedupe on event id), out-of-order tolerant. Its own FastAPI dependency.
- **Entitlement gating:** a `require_entitlement` dependency — plan can be claim-stamped (fast, stale ≤ token TTL) backed by the `entitlements` table (truth).
- **Metered agent estimations:** the usage ledger (§8) feeds Stripe usage records. Deterministic estimates are free; agent estimates (~$0.34, cap $2) are the metered unit.
- **EU VAT:** operator decision (merchant-of-record recommended for the first paid wave; see the dossier).

## 8. Anti-abuse & cost controls (RLS scopes data, not spend)

- A **`check_budget` dependency** runs **before** the agent and Mapy paths (both currently unbounded behind the shared token).
- **`usage_ledger(account_id, action, cost_usd, ts)`** — appended per metered action; a rolling-window aggregate enforces the plan quota. Estimation cost rolls up for free via the `llm_calls → estimation_runs` join once `account_id` exists.
- Convert `_check_daily_cost` (`api/llm_client.py`) from **warn-only to an enforcing global kill-switch** (429 past the ceiling) — the backstop behind per-account quotas.
- Per-account **concurrency cap** + an **async job model** (submit → poll) for agent runs, replacing the synchronous 240-second HTTP request (detailed in Wave 1, designed here).

## 9. The RLS test lane (the correctness this codebase can't currently verify)

The DB tests run against a `_FakeConn` that can't see policies, so the most security-critical logic would ship untested. Add a **real-Postgres CI lane** (gated on `TEST_DATABASE_URL`, like the existing schema-prepare gate) that, on the replayed schema + Supabase roles:
1. creates two accounts, inserts rows for each;
2. connects as `authenticated` with each account's claims and asserts cross-tenant `SELECT/UPDATE/DELETE` return/affect **zero** of the other's rows;
3. asserts **no** table grants `anon`/`authenticated` DML;
4. asserts **every** user-state table has `account_id` + a policy.
This is the launch-gate guard. It composes with Phase 0's offline grant-drift gate.

## 10. GDPR user lifecycle

- **Account deletion:** a defined cascade from `auth.users` through all owned state. Where rule 12 forbids deleting rows (`estimation_runs` immutable), **anonymize** instead — repoint `account_id` to a `deleted` sentinel and scrub PII — rather than delete. Decide anonymize-vs-delete per table.
- **Data export** (Art. 20), **consent/ToS acceptance records**, privacy policy + cookie/telemetry consent for the SPA and extension.

## 11. Rollout choreography — the operator keeps working daily

The trap (from the adversarial critique): flipping user-state views to `security_invoker` blanks the operator's anon SPA until auth ships. So sequence **within Phase 1** so every step is additive and the operator never loses their data:

1. **Auth + accounts land first.** Supabase Auth on; SPA gains a login screen; **dual-auth window** (API accepts legacy bearer + JWT). Operator can log in; nothing else changes.
2. **Add `account_id` + backfill** the operator as account #1 (`nullable` → populate → `NOT NULL`). Purely additive; no read path changes.
3. **Add RLS policies** `TO authenticated` while the `*_public` views are **still definer** — so the anon SPA still reads globally and the operator sees everything exactly as before. No visible change.
4. **Move the SPA's user-state reads** from `anon`+definer-views to `authenticated`+invoker-views **page by page**, behind the login. As each page moves, flip **that** view to `security_invoker`. The operator (logged in as account #1, owner of all backfilled rows) sees the same data — because they own it.
5. **Re-grant the shared-market views from `anon` to `authenticated`** and point the SPA's market reads at the `authenticated` role. Now every logged-in user reads shared data; user state stays RLS-scoped.
6. **Revoke `anon` to ~nothing and retire the legacy static bearer** once the SPA is fully logged-in on JWT and the last old extension build has aged out. The only remaining anonymous surface is the static marketing page (which ships no Supabase key) and the deliberately-public image redirect.

The **pipeline PK rewrite** rides with step 2's `account_id` backfill (trivial row volume). Every migration is additive/reversible; there is no big-bang.

## 12. Ops & monitoring (multi-tenant baseline)

Error tracking (Sentry — none today); an **audit log** of admin/user actions (attributed, replacing free-text `created_by`); verified **PITR + a tested restore**; security-event alerting (failed-login floods, anomalous access).

---

## Workstream sequencing & exit gate

Rough dependency order (several parallelizable):

```
A. Auth provider + custom SMTP + JWT verify + dual-auth        ─┐
B. accounts / account_members / system account / signup hook    ─┼─ foundation
C. current_account_ids() helper + tenant DB role + 2nd pool     ─┘
D. account_id + backfill + RLS policies (19 tables)   ── depends on B,C
E. pipeline PK/stage/reconciler rewrite               ── with D
F. system-writer account-awareness + merge dedup keys ── with D
G. SPA read migration + view invoker-flip (page by page) ── depends on D
H. admin gating (is_admin, re-gate 70 routes, code-split)── depends on A
I. Stripe skeleton + entitlements + webhook           ── depends on B
J. usage ledger + check_budget + kill-switch          ── depends on B,D
K. RLS test lane + GDPR lifecycle + Sentry/audit/PITR  ── continuous
L. retire legacy bearer                               ── last, depends on G,H
```

**Exit gate (before any public wave):** the RLS test lane green + an external re-audit (`/code-review ultra`) + a manual cross-tenant pen-test with two real accounts.

## Settled decisions (operator, 2026-07-10)

- **Fully login-gated.** Only a static marketing page is anonymous; **all data requires an account.** Consequences baked into this design: (a) `anon` is revoked to ~nothing in Phase 1 — the transitional "keep anon SELECT" from Phase 0 is undone here; (b) the shared-market views (`listings_public`, `browse_list`, `brokers_public`, `price_stat_*`, etc.) are **re-granted from `anon` to `authenticated`** — any logged-in user reads shared market data, RLS-scoped user state returns only their own rows; (c) the marketing/landing page is a separate static asset that ships no Supabase key; (d) no PostgREST edge rate-limiting for `anon` is needed (there is no anon surface left). This is the simplest, safest posture.
- **Accounts + members from day one.** `current_account_ids()` reads `account_members` as designed in §1 — teams/seats are additive later with zero policy changes. A personal account is auto-created per user at signup; the operator is account #1.

**Read-model summary under these decisions:** static marketing (anon, no data) → login → `authenticated` reads shared-market views (common to all) + own user-state via `security_invoker` views (RLS) ; admin reads via the service-role API behind the `is_admin` claim.

Deferred (do not block Phase 1): wave-4 broker-contact masking, and the EU VAT approach.

---

## Amendments (surfaced by the Wave 1–4 design reviews, 2026-07-10)

Designing the four public waves exposed eight places where this Phase 1 spec was under-specified or would break a wave. These are **corrections to Phase 1 itself** — fold them in before building, because every wave sits on them.

**A1 — The tenant pool needs an explicit transaction contract (or every user write 500s).** The "`SET LOCAL request.jwt.claims` per request" line collides with this codebase's reality: `scraper/db.py` connects `autocommit=True` over the transaction-mode pooler, and route handlers (e.g. `api/pipeline.py`) run *several* transactions per request and issue reads *outside* the write transaction (a post-commit `_fetch_card`). A `SET LOCAL` evaporates when its implicit transaction ends, so the read-back runs claim-less and RLS hides the row just written. **Resolution:** the tenant pool runs **one explicit transaction per request** (autocommit off for that pool), does `SET LOCAL request.jwt.claims` first, and tenant routes are refactored so their read-backs execute inside that same transaction. The session-mode-pooler alternative (session-level `SET` + `RESET` on release) is rejected — it re-introduces the scarce backend ceiling the stack deliberately avoids. Add a real-Postgres test that a write followed by its read-back succeeds under the tenant role.

**A2 — Merge reconcilers run `BYPASSRLS`; RLS can never scope them.** `reconcile_pipeline_on_merge`/`unmerge` run inside `merge_properties` as the service role, driven by the dedup engine on every pass. Once `account_id` is in the pipeline PK there are N cards per property (one per tenant that bookmarked it), and the current single-card SQL cross-joins tenant A's survivor with tenant B's retired card → **routine cross-tenant corruption and card loss**, not an edge case. The Phase 1 "pipeline rewrite" is therefore an account-*partitioned* SQL rewrite (add `s.account_id = r.account_id` throughout; snapshot/restore per `(account_id, property_id)`), and **`property_pipeline_events` must gain `account_id`** (it has none today). Same rule for the `carry_operator_state_on_merge` set-dedup keys.

**A3 — Re-key BOTH global uniques on `pipeline_stages`, not just the entry one.** `pipeline_stages_key_ci` (`unique lower(key)`) is global; `seed_default_pipeline` inserts the same five keys per account, so the **second signup fails** unless it becomes `unique (account_id, lower(key))`. The spec named only the `one_entry` partial-unique.

**A4 — `estimation_runs.account_id` is NULLABLE, stamped synchronously.** It is not a standard tenant table: agent and system runs execute as service-role with **no JWT** (the post-response `BackgroundTask`), so `account_id` can't come from a claims `DEFAULT` and can't be RLS-forced `NOT NULL`. Add it **nullable**, stamp it at the *synchronous* kickoff INSERT before the task is scheduled, and scope its read policy `account_id IN (current_account_ids()) OR account_id = SYSTEM`. This is load-bearing for Waves 1, 2, and 3.

**A5 — The tenant role needs explicit read policies on shared-market base tables.** Phase 0 made every RLS-off base table RLS-on deny-all. Tenant-pool endpoints that read base tables directly (e.g. `portal_lookup.py` reads `listings`/`properties`; the matcher reads `properties`) return **nothing** under the non-`BYPASSRLS` tenant role, and a `GRANT` does not defeat RLS. Phase 1 must add explicit `FOR SELECT TO authenticated USING (true)` policies on the shared-market base tables the tenant path touches (listings, properties, admin_boundaries, …) — the "re-grant views to authenticated" line does not cover direct base-table reads.

> **Correction, 2026-07-21 (verified live while shipping Wave 1 W1-1).** A blanket `USING (true)` policy directly on `listings` is unsafe as written — the base table carries `broker_email`/`broker_phone`/`broker_name`/`raw_json` columns inline (confirmed live), and RLS filters *rows*, not *columns*. Table-level `SELECT` grants for `authenticated` already exist (Phase 0 left them in place for the owner-rights `*_public` views), so the moment a row-level policy makes `listings` actually readable, Supabase's auto-REST (PostgREST) exposes those columns to **every signed-in tenant** directly — the exact class of leak A6 walled off for the `_public` views, reopened one layer down. `listings_public`/`properties_public` are deliberately **not** `security_invoker` today specifically so they can keep serving shared-market reads without touching base-table RLS at all (mig 290's own comment: "the anon SPA reads via the (still definer) `*_public` views, so no current behavior changes") — that's the working pattern, not a gap to close. A5's fix must be column-safe: either route tenant-pool reads through the existing DEFINER `*_public` views (accepting the column-shape mismatch vs. hand-written SQL), or add a narrow `SECURITY DEFINER` function that returns only the safe columns a given caller needs. Do **not** add `FOR SELECT TO authenticated USING (true)` directly on `listings`. W1-1 left `list_estimation_runs`'s locality-display JOIN and `POST /listings/lookup` degraded (NULL / already-unscoped-via-legacy-bridge) rather than take this shortcut.

> **SHIPPED, 2026-07-22 (migration 349).** A5's column-safe fix split cleanly along the two tables' *actual* PII shape, which the correction above conflated:
> - **`properties` — a plain `FOR SELECT TO authenticated USING (true)` policy IS safe here.** Verified against every `create/alter table properties` migration: the base table carries only market rollups + geo + subtype/street — **no** broker/contact/`raw_json` column has ever been added. The broker contact visible on `properties_public` is *joined in from `listings`* by the view, not stored on `properties`. So a tenant reading base `properties` sees strictly **less** than the `properties_public` view they can already read. This unblocks `toolkit.property_identity.resolve_active_property_ids` (the recursive merge-survivor walk) on the tenant conn — which was live-breaking add-note, add-to-collection, and pipeline-bookmark for every signed-in extension user (all three 404'd on the resolver returning zero rows under deny-all).
> - **`listings` — stays deny-all; its one tenant-conn reader is rerouted, not policied.** The only tenant-conn read of `listings` was `create_note`'s `id ↔ sreality_id` map; it now resolves through the existing PII-free `listing_natural_key_public` (`sreality_id, source, source_id_native, id` — owner-bypass, already `authenticated`-granted, physically incapable of leaking broker columns). This is the "route through the DEFINER view" option, purpose-fit.
> - **`portal_lookup`** was already fixed the same way in Wave 1 (market facts on a service-role conn, per-account joins on the tenant conn) and needs nothing here. **`list_estimation_runs`** turned out **not** to be degraded — its `GET /estimations` route runs on `deps.get_db_conn` (service role), not the tenant pool, so its `l.district` locality JOIN works; leave it. **`admin_boundaries`** (PII-free) was NOT policied — no tenant-conn consumer reads it yet; add a `USING (true)` policy when one does (e.g. Wave 3's matcher). Regression: `test_a5_properties_readable_listings_still_deny_all` asserts all three invariants (properties readable, base `listings` still deny-all incl. broker_email, identity view round-trips) live.

**A6 — Exclude broker-PII surfaces from the blanket anon→authenticated re-grant (critical; changes a "settled" decision).** The login-gated decision re-grants shared-market views to `authenticated`. Applied blindly, that exposes `brokers_public` **with `primary_email`/`primary_phone`** (~17.8k emails, ~23.4k phones) to every tenant the moment Phase 1 ships — *before* Wave 4 masks anything. Phase 1 must **exclude** the broker-PII/re-identifying surfaces (`brokers_public`, `broker_leaderboard`, `broker_firm_memberships_public`, `broker_listings_public`, `listing_broker_public`, and the directly-granted `broker_region_type_stats` matview) from the re-grant — they stay dark to tenants until **Wave 4** flips them in masked form. Encode the exclusion as a CI assertion (no broker email/phone column is reachable by `authenticated` before Wave 4).

**A7 — `channel_sends` stays a service-role ledger — do NOT put `account_id` + RLS on it.** It is a shared delivery ledger across four consumers (watchdog, collection_monitor, **outreach** [GDPR-global broker path], system_health). RLS'ing it would hide operator/broker delivery rows and collide with the global outreach path. Derive per-account delivery status by joining through `notification_dispatches.account_id` when the SPA needs it.

**A8 — Don't couple all detection to the dark-by-default worker.** Wave 3 moves the watchdog matcher to the always-on realtime worker behind a lease. But hard-deleting the API-lifespan matcher couples *all* detection to `REALTIME_WORKER_ENABLED` (which ships dark). Keep a leased matcher pass **co-hostable in the API** (the lease makes co-running safe), *or* make `REALTIME_WORKER_ENABLED` an explicit launch gate with a `system_health` alarm on a stale matcher heartbeat.

**A9 — Every quota/concurrency/idempotency gate must be atomic, not check-then-act.** `SELECT count(*) … < limit` then `INSERT` is TOCTOU — parallel submits each read "under limit" and all insert, bypassing the cap; the same race defeats a select-then-insert idempotency short-circuit (two clicks → two paid runs). Session advisory locks are **unsound over the transaction pooler** (the mig-279 lesson). The sanctioned patterns are a **`UNIQUE` partial index + `ON CONFLICT`** (idempotency + single-in-flight in one atomic write) and an atomic **`INSERT … SELECT WHERE (window count) < limit`** for rolling-window budgets. `check_budget` and the concurrency cap must be built this way from the start — this is the enforcement spine behind the metered surfaces (Wave 1, Wave 3 estimate-from-dispatch).

**A10 — Long/paid work must drain off the request process, not run in a post-response `BackgroundTask`.** The agent estimation is a sync run that blocks one of Starlette's ~40 threadpool tokens for up to 240 s, and every merge-to-`main` deploy SIGTERMs the container mid-run (killing paid work with no resume, no ledger row). So the first *paid, concurrent, long* workload must **drain off a job lane on the always-on realtime worker** (consuming `pending` `estimation_runs` — the run row stays the job; only the executor moves), with a **periodic** (not startup-only) stuck-run sweep so orphaned `running` rows don't permanently consume a concurrency slot and poison idempotency. This also covers Wave 3's estimate-from-dispatch. (The DB pooler is *not* the binding constraint — short autocommit transactions between LLM calls don't pin a backend — the threadpool and the deploy lifecycle are.)
