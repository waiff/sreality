---
name: toolkit-api
description: Use when writing or changing analytical toolkit functions (toolkit/) or the FastAPI service (api/) — the facts-not-opinions rule, the standard tool return envelope, the read-only-with-write-exceptions rule, the two-gate auth split (require_token shared-secret vs. require_admin/verify_jwt real Supabase JWT / login / admin gating / identity), the billing/entitlements skeleton (Stripe webhook, plans, agenda gating), the versioned estimation trace, provider pluggability (Anthropic + Gemini), or the full env-var/secrets reference (Postgres, tenant pool, R2 images, LLM+maps keys, API service, notification delivery, scraper orchestration, frontend/extension build-time). Triggers on: new toolkit tool, /admin route, API_TOKEN, login, admin gating, identity, account menu, billing, Stripe, entitlement, plan, agenda gating, write exception, estimation_runs.trace, llm_calls, provider, env var, secret, R2/ANTHROPIC/GEMINI/MAPY/RESEND/TELEGRAM/STRIPE keys, CORS.
---

# Toolkit & API

Rules for the analytical toolkit (`toolkit/`) and the FastAPI service that exposes it
(`api/`), plus the complete secrets / env-var reference. These do not apply to the
scraper. Rule numbers below are cited by code — never renumber.

## Toolkit and API rules

These rules govern the analytical toolkit (`toolkit/`) and the FastAPI service that exposes
it (`api/`). They do not apply to the scraper.

1. **Tools return facts, not opinions.** No "recommended price", no "this looks like a good
   deal." Tools return data + provenance. Reasoning happens at the agent layer.
2. **Standard envelope on every tool's return value:**
   ```python
   {
     "data": ...,
     "metadata": {
       "tool": "tool_name",
       "filters_used": {...},      # echo of actual params after defaults applied
       "result_count": int,
       "queried_at": iso8601,
       "data_freshness": iso8601,  # max(last_seen_at) of considered listings, or null
     }
   }
   ```
3. **Every tool excludes `given_up = true` listings** from `listing_fetch_failures` by
   default. An `include_unreliable: bool = False` parameter overrides.
4. **"Active" filter is `is_active = true AND last_seen_at > now() - interval 'X days'`
   (default 7).** Don't trust `is_active` alone — a listing not seen for 30 days is
   functionally inactive.
5. **No writes from the toolkit, with ten explicit exceptions.** Read-only by default. The
   exceptions are:
   - `verify_listing_freshness` (and `scraper.freshness.freshness_check` that it wraps), so
     an agent can confirm a comparable is still valid before relying on it. Every call logs
     to `listing_freshness_checks` and may also write a new `listing_snapshots` row, flip
     `listings.is_active`, or both.
   - `find_anchor_amenities`, which writes the OSM-mirror tables `amenities` /
     `amenity_fetches` on a cache miss.
   - `find_comparables_along_axis`, which writes the OSM-mirror tables `transit_lines` /
     `transit_line_fetches` (migration 028) on a cache miss.
   - `summarize_listing`, which writes a structured Claude summary to `listing_summaries`
     (keyed on `(sreality_id, snapshot_id)`) on cache miss.
   - `compare_listing_images`, which writes the pairwise visual comparison to
     `listing_image_comparisons` (canonical-ordered pair) on cache miss.
   - `extract_building_units`, which writes the structural unit decomposition to
     `building_unit_extractions` (keyed on `(sreality_id, snapshot_id)`) on cache miss —
     the vision extractor behind the building-paste flow.
   - `read_floor_plan`, which writes a structured Claude-vision analysis of one
     operator-supplied attachment to `building_attachment_analyses` (keyed on
     `(attachment_id, model)`) on cache miss. Only callable inside the building flow; the
     agent handler in `api/agent.py` enforces that the `attachment_id` belongs to the run's
     `building_run_id`.
   - `discover_condition_markers`, which writes a structured list of Czech "condition
     markers" to `listing_marker_extractions` (keyed on `(sreality_id, snapshot_id)`) on
     cache miss — feeds the condition-scoring marker dictionary.
   - `score_listing_condition`, which writes per-listing building/apartment condition levels
     (1..5) + matched marker_ids + per-axis confidence to `listing_condition_scores` (keyed
     on `(sreality_id, snapshot_id)`) AND updates the two derived columns on `listings`
     (`building_condition_level`, `apartment_condition_level`) in one transaction, guarded by
     a latest-wins subquery.
   - `summarize_region_dispositions`, which writes the per-disposition box-plot annotations
     for a Browse > Stats cohort to `region_disposition_annotations` (migration 104, keyed on
     `(region_hash, day)`) on cache miss. Unlike the snapshot-keyed caches above this one
     invalidates by **calendar day**: a region's annotations are generated once per day so
     repeat browser sessions don't re-bill. `region_hash` is the sha256 of the caller's
     deterministic serialization of the active filter set.
   Every write-allowed exception caches an expensive external/LLM fact locally and
   auto-invalidates (a new snapshot, a model bump, or the calendar day rolling over yields a
   fresh key); the LLM/OSM source is the truth, the table is a mirror. No other toolkit
   function may write. The API service should still connect with a read-only role if Postgres
   permits; these ten paths then need a separately-elevated route. For now we ship with one
   role and discipline.
   **Broker merge-review writes are durable decisions, not just status flips** (`api/broker_review.py`,
   migration 401). The nightly sweep re-derives every merge from scratch — name-gated and
   portal-agnostic since 2026-08-20 — so an unmerge used to be re-applied the same night.
   `unmerge_group` derives, inside its own transaction and BEFORE the re-point, every cross-owner
   identity pair it pulled apart (SAME-PORTAL PAIRS INCLUDED: within-source merging is allowed now,
   and the biggest duplicate fans are same-portal) and records it in `broker_merge_suppressions`. Both derivations read a **cohort**, never a remembered id: the
   unmerge anchors on where the restored identities live NOW (the survivor on the event rows may itself
   have been merged away since — that read returns nothing and the unmerge silently writes zero rows),
   and a dismissal anchors on the candidate's BROKER pair, suppressing every cross-owner pair between
   them (the card is keyed `contactbridge:{lo}:{hi}` and the evidence is last-write-wins, so sibling
   identity pairs would otherwise stay live). Pairs already under one broker are skipped — an active
   suppression over co-located identities is an instant `verify_pipeline` violation. A `name_firm`
   candidate has no identity evidence: dismiss only. `merge_brokers` LIFTS (never deletes) every active
   suppression whose two identities sit under DIFFERENT brokers being merged — an explicit operator
   merge always beats the rail, which gates the AUTO path only; `GET /broker-review/suppressions` +
   `POST /broker-review/suppressions/{id}/lift` (409 on a re-lift) are the ledger and the manual
   override. Every mutating `/broker-review/*` route binds `require_admin`'s claims and threads
   `claims.get("email") or claims.get("sub")` into `undone_by` / `resolved_by` / `created_by` /
   `lifted_by`.
6. **Spatial queries use `geography(point, 4326)`.** Always `ST_DWithin(geom, target_geom,
   radius_m)`. Never compute distance in Python.
7. **psycopg directly, not supabase-py.** Same reasoning as the scraper.
   `prepare_threshold=None` for pgbouncer-mode pooler.
8. **Two auth gates coexist by design: `require_token` (shared secret) and
   `require_admin`/`verify_jwt` (real identity, JWT-only since 2026-08-04).** Baseline:
   every endpoint except `/health` requires `Authorization: Bearer <token>` when
   `API_TOKEN` is set (no-op when unset, for local dev); `/health` stays open for Railway
   healthchecks. `/admin/*` (Settings-page surface: skills, `app_settings`, agent tool
   inventory) is bearer-gated like every other write surface — it was historically exempt
   on the theory that the private Railway URL was the perimeter, but that URL ships
   inside the public SPA bundle, so the exemption gave no real protection.
   **Phase 1 (increments 1–4, #747/#753/#763/#765) layered identity on top**, not instead
   of the token: `/admin/*`, `/properties/merge*`, `/properties/assets/*`, `/labeling/*`,
   `/outreach/*`, `/broker-review/*`,
   `/skill-refinements/*`, `/location-audit/*`, and dataset-write/dispatch routes on
   price-stats use `require_admin` (JWT-gated, see below) instead of plain `require_token`;
   `/pipeline/*`, `/collections` (GET), `/estimations` create/detail/scenario, notes,
   `/listings/lookup`, and `/brokers/*` use `verify_jwt`/`tenant_conn` for per-account
   identity without the admin claim; every other route is still `require_token`-only (a
   shared secret, no identity — `POST /collections`, tags, buildings, manual estimates,
   filter-presets). `/brokers/*` moved off `require_token` on 2026-08-12 (D1/D2 of the
   broker E2E review): the leaderboard returned up to 2000 brokers' unmasked email +
   phone behind the bundle-extractable token. Every `/brokers/*` envelope now runs
   through `toolkit.brokers.apply_pii_policy`, which swaps any contact column for
   `has_email` / `has_phone` (matched on the column NAME, so a widened view can't leak)
   and stamps `metadata.pii_masked`, unless the caller is admin; `GET
   /brokers/{id}/contacts` is `require_admin` outright — there is no masked variant.
   Under the name rule sits a **shape rule**: a string value that is an email, or that is
   wholly a phone number, is redacted whichever column it arrives in (live brokers whose
   `display_name` IS their email address). The shape rule also reaches the PREDICATE, not
   just the projection: `GET /brokers/search` passes the caller's `include_pii` into
   `toolkit.brokers.search`, which for a non-admin keeps only rows still matching once
   `_redact_shaped` has been applied — an ILIKE over the raw column was a guess-confirming
   oracle that recovered a redacted address one character at a time. The bound term is also
   LIKE-escaped with backslash, LIKE's default escape (`%` / `_` in a bound value are still
   wildcards, so `@_` walked under the two-character minimum). Firm identifiers (`firm_name`, `firm_domain`) are deliberately
   NOT masked — `pii_masked` promises that contact VALUES are masked, not that the row is
   unattributable. `has_email` / `has_phone` mean "a current
   primary contact is on file" (the `brokers` rollup's `primary_email`/`primary_phone`),
   not "every address ever seen" — that fuller set is `broker_identity_contacts`, admin-only
   via `/contacts`. Both batch reads bound their input at `toolkit.brokers.MAX_BATCH`
   (1000) at the HTTP layer, so an over-cap batch is a 422 rather than a silently
   truncated 200, and `?geo_level=` accepts only `region`/`okres` (`GEO_LEVELS`).
   `tests/api/test_broker_routes.py` holds two standing gates: no `/brokers` route may
   ride `require_token`, and every non-admin one must stamp `pii_masked` — a new route
   must be added to that table. The SPA is the main consumer since 2026-08-12
   (`frontend/src/lib/brokers.ts`, every call `jwt: true`); it previously read the
   underlying views straight off PostgREST and had been dark since migration 299 revoked
   them. Migration 395 revoked the last one A6 missed (`firms_public`, default-ACL drift).
   Because the cap is a 422 on the WHOLE batch, `fetchListingBrokersByIds` chunks
   client-side (supabase-js `.in()` had no such cap, so an oversized board would otherwise
   lose every card's broker, not just the overflow). Its `fetchBrokersByIds` twin is
   **deleted** (hydration sprint W6): migration 419 put `primary_email`/`primary_phone` on
   `listing_broker_public`, so `POST /brokers/by-listings` answers identity AND contact in
   one row and the SPA no longer chains a second, serialized `GET /brokers?ids=` to fetch
   what the first read's join had already touched. `apply_pii_policy` covers the two new
   columns automatically — it masks on the column NAME, which is exactly why widening a view
   behind these routes needs no route change. `GET /brokers?ids=` itself stays, for the agent
   and other non-SPA consumers; and
   the client treats 404 as an answer only when the body carries this module's own
   `broker not found` / `listing has no attributed broker` detail — any other 404 (edge,
   stale base URL, renamed route) must surface as an error, not as "no broker".
   **The old coexistence window is gone for `require_admin`/`verify_jwt`**: the static
   `API_TOKEN`, extractable from the shipped SPA bundle via devtools, used to also satisfy
   `verify_jwt` as a synthetic `is_admin: True` identity — a live CRITICAL finding closed
   2026-08-04 (`docs/design/api-token-rotation-and-spa-jwt-migration.md`). It never
   authenticates through `verify_jwt` now; `require_token`-only routes are unaffected (a
   fully separate, simpler check that was never the vulnerable path).
   See "Identity, login, and admin gating" for the JWT mechanics.
9. **Trace format on `estimation_runs.trace` is versioned.** `TRACE_SCHEMA_VERSION` lives in
   `api/estimation_runs.py`; every row's `trace.version` matches that constant at write time.
   Shape: `{version, summary, steps: [{n, kind, started_at, duration_ms, output_summary,
   ...}]}`. Step `kind` ∈ `'tool_call' | 'computation' | 'reasoning'`. The reasoning kind is
   emitted per LLM turn by the agent loop. Steps NEVER store full tool outputs — only
   `output_summary`; the full data lives in dedicated columns (`comparables_used` for the
   cohort, etc.). This caps row size at single-digit kilobytes regardless of cohort size.
   Bumping the version is a deliberate change; future readers must handle older versions.
   Full per-step tool outputs that the operator may want to drill into later live in a
   separate side-table `estimation_trace_payloads` (migration 043), keyed on
   `(estimation_run_id, step_n)`. Populated only for `tool_call` steps that opt in via
   `StepHandle.set_full_output(...)`. Reachable through
   `GET /estimations/{id}/trace/{n}/payload`. Same retention discipline as
   `listing_freshness_checks`: rows older than 30 days are safe to delete; no automated
   pruner.
10. **Agent skills live in the `skills` table; the on-disk `skills/<name>/SKILL.md` file is
    the canonical seed.** Each skill is a bundle of (system prompt + allowed tool whitelist +
    per-provider preferred model + loop limits). Migration 029's seed `INSERT` is the importer
    of the markdown file's content; at runtime the DB row is the source of truth. Operators
    edit via `/settings` (UI) or `PUT /admin/skills/{name}` (API). Every update writes a
    `skills_history` row via trigger — same pattern as `app_settings_history` (migration 020).
    When adding a new skill: commit a new `skills/<name>/SKILL.md`, write the corresponding
    seed `INSERT` in a new migration, apply.
11. **LLM provider is pluggable; `llm_calls.provider` records which backend served each call.**
    `api/providers/` defines a `CompletionProvider` Protocol with neutral message / tool /
    completion types; today `anthropic` and `gemini` are wired up (default `anthropic`).
    Adding a third provider is a new file implementing the same Protocol, registered in
    `api/dependencies.py:_build_providers`. `LLMClient` is the audit orchestrator — every call
    writes one row to `llm_calls` with provider, model, tokens, USD cost, and a `called_for`
    tag. An unmapped model id records `cost_usd=0` rather than raising — silent, not loud;
    check `api/providers/gemini.py`'s `_PRICES` table after any Gemini model bump.
    **Gemini quirks** (`api/providers/gemini.py`): (1) pricing table needs live maintenance
    across generations — 2.5 closed to new projects, PR #760 moved the default price entries
    to the 3.x generation (`gemini-3.1-pro-preview`, `gemini-3.5-flash`); (2) our
    Anthropic-shaped tool schemas set `additionalProperties: false` and sometimes carry
    `$schema` — Gemini's function-calling API 400s on both, so they're recursively stripped
    before every call (`_GEMINI_UNSUPPORTED_SCHEMA_KEYS`, PR #755) — a new tool schema key
    Gemini rejects needs adding to that frozenset, not a per-call workaround. The
    `CompletionProvider` Protocol also gained `tool_choice` (force-tool-by-name — Anthropic's
    `{"type": "tool", "name": ...}`, Gemini's `FunctionCallingConfig mode=ANY`, PR #768) so a
    caller that needs a guaranteed structured response (no prose fallback) can force it; pass
    it through `LLMClient.call(..., tool_choice=...)` — omitted, providers/fakes without the
    param keep working.
12. **Never spell `price / area`. `toolkit/measures.py` is the only per-m² definition.**
    (Numbered last on purpose — the rules above are cited by code comments; never renumber.)
    `per_m2_sql(alias)` / `per_m2_basis_sql(alias)` render calls to `measure_price_per_m2` /
    `measure_price_per_m2_basis` (migration 425); `alias` is required, so a unit-blind call
    cannot be written. Against the `listings` TABLE the four-argument call is the ONLY legal
    spelling — `listings` has no `price_per_m2` column, so the published-column form fails at
    PREPARE; against `properties_public` / `browse_list`, read the published column, which IS
    the measure. Vocabulary: four values (`sale_capital_czk_m2`, `rent_monthly_czk_m2`,
    `land_capital_czk_m2`, NULL) plus two non-basis COHORT states, `mixed` and `unknown`.
    Floors are on the PRICE (rent < 1000, sale non-land < 100000, land unfloored) and a NULL
    is a visible gap, never a guess. Every envelope carrying a per-m² number carries `basis`
    beside it, in `data` and in `filters_used`; a tool handed caller-supplied rows degrades to
    `'unknown'` rather than defaulting to sale. `require_scalable_basis` gates any
    multiplication of a per-m² percentile by an area (`estimate_yield._scale`), raising
    `MeasureBasisError` → 422. The five statements assembled by in-function concatenation
    (comparables, velocity, the transit corridor, neighborhoods, the watchdog matcher) are
    INVISIBLE to `tests/sql_corpus.discover()`; `tests/test_measure_sql_prepare.py` is their
    PREPARE gate and must gain a line when a sixth appears.

## Identity, login, and admin gating (Phase 1, `api/dependencies.py`)

Four auth primitives now coexist in `api/dependencies.py`:
- `require_token` — the original bearer-token gate (rule #8's baseline), unchanged.
- `account_scope` — an EITHER gate that also returns a READ SCOPE, for routes that must serve
  both a browser session and a non-browser caller over the one `Authorization` header: the
  static token resolves to `[SYSTEM]` (it ships in the SPA bundle, so it is not an identity),
  a verified JWT to `[that account, SYSTEM]` — mirroring the `estimation_runs_tenant_read`
  policy (migration 291) rather than defining tenancy a second time. Its unset/wrong-credential
  contract matches `require_token` exactly (503 / 401). Used by `GET /estimations` and
  `GET /estimations/latest-by-listing`, which stay on the SERVICE-ROLE connection on purpose:
  their query LEFT JOINs `listings` + `parsed_url_cache`, both RLS-on-with-zero-policies, so a
  tenant connection would silently NULL `locality_display` on every row. Callers pass the
  resulting `account_ids` to the read helpers, where it is a REQUIRED kwarg with no default —
  omitting it is a `TypeError`, never a silent unscoped read.
- `verify_jwt` — verifies a Supabase user JWT and returns its claims. Preferred path:
  asymmetric JWKS (`SUPABASE_URL` → `/auth/v1/.well-known/jwks.json`, ES256/RS256, cached
  via `PyJWKClient`, no shared secret). Falls back to a shared HS256 secret
  (`SUPABASE_JWT_SECRET`) if that's all that's configured. Fails closed with `503` if
  neither JWKS nor the HS256 secret is configured (an unconfigured auth backend must
  never authenticate anyone). **The legacy dual-auth branch is gone (removed 2026-08-04):**
  it used to check the static `API_TOKEN` bearer FIRST and, if it matched, return a
  synthetic claims dict `{"sub": None, "role": "operator", "is_admin": True, "legacy":
  True}` — so any route behind `verify_jwt`/`require_admin` accepted the SPA-bundle-
  embedded token as a god-credential. Presenting that token now just fails normal JWT
  decoding (401) like any other garbage bearer value. See
  `docs/design/api-token-rotation-and-spa-jwt-migration.md` for the incident + fix.
- `require_admin` (`Depends(verify_jwt)`) — gates on `claims["is_admin"]` or
  `claims["app_metadata"]["is_admin"]`; `403` otherwise. Only reachable now via a real
  Supabase JWT whose `app_metadata.is_admin` was stamped `true` (the `admins` table is the
  provisioning allowlist, but the live claim is a plain `auth.users.raw_app_meta_data`
  attribute — Supabase includes `app_metadata` in every issued JWT by default, no Custom
  Access Token Hook needed or configured).

`SYSTEM_ACCOUNT_ID = "00000000-0000-0000-0000-000000000000"` mirrors migration 286's
fixed system account — the fallback owner for a run/write whose caller has no resolvable
account (service-role/background writers that never had a JWT `sub` to begin with; no
longer describes a "legacy caller" path since `verify_jwt` has none).

For routes that need per-account **data isolation** (not just an admin/non-admin split),
use `api/tenant_pool.py`'s `tenant_conn` dependency instead of the service-role
`get_db_conn` — it opens an RLS-scoped transaction under the `tenant_pool` role. See the
`database` skill's connection-modes + Multi-tenancy sections for the mechanics;
`verify_jwt` is authentication, `tenant_conn` (via RLS) is authorization. Its
`resolve_account_id(conn, claims)` helper picks the caller's own account; both this
helper and `tenant_conn` still carry an internal `if claims.get("legacy")` branch (routes
to the unscoped service-role connection / the legacy-backfill claim) that is now
unreachable dead code, since `verify_jwt` can no longer produce a `legacy` claim — left in
place rather than refactored in the same change that closed the `verify_jwt` gap, to keep
that fix narrowly scoped; safe to remove in a follow-up.

**Billing skeleton** (`api/routes/billing.py`, migration 298, PR #769 — Phase 1 increment
5) adds a **fourth** auth class alongside the three above: `POST /billing/webhook` verifies
the `Stripe-Signature` header as an HMAC over the raw request body using the stdlib (no
Stripe SDK), rejects payloads outside a 300s replay window, and fails closed with no
`STRIPE_WEBHOOK_SECRET` configured — it does NOT use `require_token`/`verify_jwt` at all.
One DB transaction covers both the `stripe_webhook_events` idempotency INSERT (`ON CONFLICT
DO NOTHING` on the Stripe event id — atomic already-processed check, never check-then-act)
and the event handler, so a mid-handler crash lets Stripe's own retry reprocess safely.
`checkout.session.completed` anchors the Stripe customer id to an account (never re-points
an already-bound one); `customer.subscription.*` upserts plan/status/period guarded by
`last_event_created` (Stripe doesn't guarantee delivery order). `GET /billing/me` rides
`tenant_conn` (RLS) and returns the caller's plan + agenda visibility.
`require_entitlement(agenda)` is a dependency **factory** (not a single dependency like
`require_admin`) — call it as `Depends(require_entitlement("watchdogs"))` to 403 unless the
caller's plan has that agenda's visibility flag on; its bypass check is `claims.get("legacy")
or is_admin` (the operator is never billing-gated) — the `legacy` half is now dead code
(`verify_jwt` can't produce it, see "Identity, login, and admin gating" above), left as-is
since it's harmless and this file wasn't touched by the 2026-08-04 `verify_jwt` fix. Wired
to no *router* yet — the first real enforcement is **inline in `create_estimation_run`**
(below), not via the dependency.

**Agent-estimation metering** (Wave 1, migration 355) is the first metered path. The paid
`mode:'agent'` submit is gated **inside `api/estimation_runs.py:create_estimation_run`**, at
the single choke point *before the URL parse* (`_prepare_metered_submit`) so a rejected submit
spends zero LLM cost. Meter = **per successful agent run, monthly** (operator decision, not
USD): free plan `plans.agent_estimations_monthly_quota` = 3, `trial_*` = 10 (used while
`entitlements.status='trialing'` + unexpired). Only a real, non-admin tenant sending
`mode:'agent'` is metered — admin/SYSTEM and all deterministic runs bypass, mirroring
`require_entitlement` (`_is_privileged`'s `claims.get("legacy")` disjunct is dead code
today, same note as `require_entitlement` above). ClickUp is named in the comments here as
a bypass beneficiary via `claims is None` (an internal/direct-Python call path, not the
`POST /estimations` HTTP route — that route's `Depends(deps.verify_jwt)` always yields a
dict, never `None`), but ClickUp has zero historical rows in `estimation_runs`/
`building_runs` (verified live 2026-08-04) — it has never actually called the HTTP API with
the static token. If it ever does, that call now 401s at `verify_jwt` like any other; giving
it a real credential is deferred until the integration is actually activated (operator
decision 2026-08-04). The enforcement is **atomic** (A9 — never check-then-act over
the tx pooler): the INSERT is `INSERT … SELECT WHERE (monthly non-failed count) < quota AND
(in-flight count) < cap ON CONFLICT (account_id, idempotency_key) DO NOTHING` — budget +
per-account concurrency + idempotency in one write, arbiter index `estimation_runs_inflight_idem`.
The budget counts `estimation_runs` (non-failed this month) directly, not `usage_ledger`;
`usage_ledger` is the append-only billing/margin record (one row per metered success, cost =
the run's `llm_calls` sum), written at the agent terminal, RLS-scoped like `entitlements`.
Flags (app_settings, read on the service-role conn): `estimation_budget_enabled` (absent ⇒
**enforced** — fail-closed; the emergency off), `agent_estimation_concurrency_cap` (default 3).
Deferred: granting the trial at signup, and the extension sending `mode:'agent'`.

## Auth and secrets

All secrets are GitHub Actions secrets and/or Railway env vars in production. Backend code
references them by name; never write a value into a committed file (`.env` is gitignored).
API keys are **backend-only** — never `VITE_*`-prefix a backend secret; the `frontend/` build
must not see them.

Database:
- `SUPABASE_DB_URL` — Postgres connection string (Supabase → Database → Connection string →
  Transaction pooler, port 6543; password embedded). **The one the scraper / API / scripts
  actually use.** Required.
- `SUPABASE_DB_SESSION_URL` — Session-mode pooler connection string (Supabase → Database →
  Connection string → Session pooler, port 5432; same host/user as `SUPABASE_DB_URL`, just
  port 5432 not 6543). **Optional**; used only by the scraper's hot detail-write loop
  (`connect_session()`, i.e. the Phase-2 detail-drain's batched writes) so its repeated SQL
  gets prepared statements. Unset → falls back to `SUPABASE_DB_URL`. Set it as an Actions
  secret on `detail_drain.yml` (and the Railway env var only if the API ever calls
  `connect_session()`).
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` — set as Actions secrets for forward
  compatibility; the v1 scraper connects to Postgres directly and does not need them.
  (`SUPABASE_SERVICE_ROLE_KEY` is the 2025 `sb_secret_...` token, **not** a JWT.) **On the
  Railway API service, `SUPABASE_URL` is now load-bearing**, not just forward-compatible:
  `verify_jwt` builds the JWKS URL from it to verify Supabase user JWTs.
- `SUPABASE_JWT_SECRET` (Railway API, optional) — HS256 fallback for `verify_jwt` when
  JWKS/`SUPABASE_URL` isn't set. Prefer JWKS; this exists for environments without it.
- `TENANT_POOL_DB_URL` (Railway API only) — connection string for the `tenant_pool` role
  (migration 293), used by `api/tenant_pool.py`'s `tenant_conn` for RLS-scoped per-account
  writes. Distinct from `SUPABASE_DB_URL` (service-role, unscoped, bypasses RLS).
  **The username MUST carry the project-ref suffix — `tenant_pool.<project-ref>`, not bare
  `tenant_pool`** — because this points at Supabase's *shared* pooler host
  (`aws-0-<region>.pooler.supabase.com:6543`), which routes by that suffix; without it the
  pooler rejects the connection with `FATAL: (ENOIDENTIFIER) no tenant identifier provided`.
  A direct-to-database DSN needs no suffix, which is why `SUPABASE_DB_URL` looks different.
  This was mis-set from migration 293 until 2026-07-21 and stayed invisible the whole time:
  `tenant_conn`'s legacy branch routed static-`API_TOKEN` callers to the service-role
  connection, so until the Chrome extension's own JWT arrived, **no production request had
  ever executed the tenant-pool path**. (That branch is dead code as of 2026-08-04 — see
  "Identity, login, and admin gating" above — but the lesson stands.) When moving any
  further route onto `tenant_conn`, exercise it with a real user JWT — a green RLS test
  lane proves nothing about a DSN.

Image storage (Cloudflare R2, S3-compatible):
- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` (usually
  `sreality-images`).
- **TWO runtimes need these, set them on BOTH:** (1) the **scraper** (GitHub Actions secrets)
  to *download* image bytes — optional there, a missing var just logs a skip and exits zero;
  (2) the **FastAPI service** (Railway env vars) to *serve* them, since `GET /images/{key}`
  presigns R2 (the frontend's image path since PR #255). If the **API** service is missing
  them, every listing photo 503s and the UI looks imageless even though the DB reports the
  bytes "stored" — the API logs a boot WARNING and `GET /health` reports
  `image_storage: "unconfigured"` in that case.
- `IMAGE_PRESIGN_ANCHOR_SECONDS` (optional, API service, default `86400`) — the width of the
  bucket `GET /images/{key}` pins its SigV4 signing time to, so the same key presigns to a
  byte-identical URL all day. The browser's HTTP cache keys on the whole URL including the
  signature; without this the hourly redirect re-mint handed back a fresh signature and
  every photo re-downloaded hourly despite R2's own 30-day `max-age`. Read per request, so
  it takes effect without a redeploy: set it to `0` to fall back to per-request signing
  (the kill switch if R2 ever rejects a backdated signature). Values are capped at half the
  7-day presign TTL — the last URL minted in a bucket was signed at the bucket's *start*,
  so anchor and TTL meeting would serve an already-expired URL.

LLM + maps (FastAPI service + scoring jobs):
- `ANTHROPIC_API_KEY` — required for the URL parser, summarize/vision tools, condition
  scoring, and the agent under `provider='anthropic'`.
- `GEMINI_API_KEY` — Google AI Studio key; required for the agent under `provider='gemini'`.
  A request selecting an unconfigured provider returns 502; missing at boot is not fatal.
- `MAPY_GEOCODE_ENABLED` — **the W0 Mapy kill switch (location-data program, remediation
  step R1), default OFF.** Mapy.com's terms prohibit storing/caching API results and every
  geocode path persisted them, so `scraper.geocoding.geocode()` raises and
  `location.build_geocoder()` returns `None` unless this is explicitly `1`/`true`/`yes`.
  Applies to every geocode caller (portal drains, bazos in-parser, realtime worker, URL
  parser, seed/backfill scripts). Display-only Mapy use (`/maps/suggest`, tiles) is NOT
  gated — the prohibition is on persistence, not display. Do not enable without an
  operator decision recorded against the Mapy remediation plan.
- `MAPY_CZ_API_KEY` — Mapy.cz REST key; geocodes locality strings and powers `/maps/*`.
- `MAPY2_CZ_API_KEY` (optional backup) — a second Mapy.cz key. `scraper.geocoding` and the
  `/maps/suggest` proxy fail over to it automatically **only** when the primary is rejected
  (401/403) or rate-limited (429); a Mapy outage (5xx) does not trigger failover. Set it in
  **both runtimes** that geocode — the GitHub Actions secret (already injected into the bazos /
  idnes detail drains + the seed/backfill jobs) and the **Railway API service env var** (powers
  `/maps/suggest` + URL-parse geocoding). Unset → no-op, primary behaviour unchanged.
- `LLM_DAILY_COST_WARN_USD` (optional, default `5.0`) — soft cross-provider warning
  threshold; `LLMClient` logs one WARNING per day when the `llm_calls.cost_usd` sum first
  crosses it. Each provider's own console spend cap is the hard guard.

API service:
- `API_TOKEN` — bearer-token gate (no-op when unset, for local dev). See Toolkit rule #8.
- `CORS_ALLOW_ORIGINS` — CSV of allowed origins; must include the Chrome extension's
  `chrome-extension://<id>` origin and the SPA origin.
- `STUCK_ROW_SWEEP_DISABLED`, `NOTIFICATIONS_MATCHER_DISABLED` (optional flags) — disable the
  startup sweep of stuck estimation/building runs, and the background watchdog matcher loop,
  respectively. Default: both enabled.
- `STRIPE_WEBHOOK_SECRET` (Railway API) — HMAC secret verifying `Stripe-Signature` on
  `POST /billing/webhook`. Unset → the webhook fails closed (rejects every request), not a
  silent no-op — billing writes never happen without explicit signature verification.

Notification delivery (Sprint N — `channel_sends` ledger + `api/transports/` + the outbox loop,
rule #16; all OPTIONAL, dark until set):
- `RESEND_API_KEY` + `EMAIL_FROM` — the Resend email transport (`api/transports/email_resend.py`).
  Both required for `is_configured()`; transactional/self-notification scope only (Resend AUP
  forbids cold outreach — outreach gets a separate EU vendor). Railway API env.
- `TELEGRAM_BOT_TOKEN` — the Telegram Bot API transport (`api/transports/telegram.py`). Railway
  API env. The recipient `chat_id` lives in `app_settings.notification_telegram_chat_id`.
- `SPA_BASE_URL` — SPA origin for notification deep links (`{SPA_BASE_URL}/listing/{id}`).
- `RESEND_WEBHOOK_SECRET` (Wave 3, migration 367) — Svix signing secret for `POST /webhooks/resend`.
  Railway API env. Unset = the webhook 503s (fail closed); the handler verifies the Svix HMAC over
  the raw body with the stdlib (no dependency, same auth class as the Stripe webhook), dedups by
  `svix-id` (`resend_webhook_events`), advances `channel_sends.status`
  (`delivered`/`bounced`/`complained`), and inserts a GLOBAL, address-level `notification_suppression`
  row (survives tenant deletion) on bounce/complaint — the outbox hard-skips suppressed addresses.
- `NOTIFICATION_UNSUB_SECRET` + `API_PUBLIC_URL` (Wave 3) — one-click unsubscribe (RFC 8058).
  `make_unsub_token`/`verify_unsub_token` (`api/unsubscribe.py`) HMAC-sign `channel:address` with
  the secret; the Resend transport adds `List-Unsubscribe`/`List-Unsubscribe-Post` headers pointing
  at `{API_PUBLIC_URL}/u/{token}` (the unauthenticated `GET`/`POST /u/{token}` route, HMAC = auth,
  renders for logged-out users, POST inserts a `source='unsubscribe'` suppression). BOTH env vars
  optional: unset → the header is omitted (email still sends) and the token can authenticate no one.
- `STRIPE_WEBHOOK_SECRET` — Stripe webhook signing secret (Dashboard → Developers →
  Webhooks). Railway API env. Unset = `POST /billing/webhook` 503s (fail closed); the
  handler verifies the `Stripe-Signature` HMAC with the stdlib (no stripe SDK).
- `OUTBOX_DRAIN_DISABLED` (flag) — force-off the delivery outbox loop. The loop ALSO only starts
  when ≥1 transport `is_configured()`, so it's a true no-op until a key above is set + redeploy.
- Operator destinations are `app_settings` rows (operator-editable, history-tracked):
  `notification_email_to`, `notification_telegram_chat_id` (empty = that channel skipped);
  `notifications_outbox_interval_seconds` paces the loop. A watchdog opts in via
  `notification_subscriptions.channels`, a collection via `collections.notify_channels`.

Scraper orchestration:
- `SREALITY_COUNTRY_ID` (optional, default `112` = Czech Republic).
- `SCRAPE_CHAIN_TOKEN` (optional fine-grained PAT: this repo, Actions read+write) — lets the
  scrape workflow re-dispatch itself for tighter-than-cron cadence; no-op without it.
- `GITHUB_ACTOR` — CI context, used for curated-cities upload attribution.

NEW DEDUP (docs/design/new-dedup/PROGRAM.md, Wave 1):
- `RUNPOD_API_KEY` — GitHub Actions secret only (no Railway/frontend use). Auths
  `scripts/runpod_client.py`'s REST (`rest.runpod.io/v1`) and GraphQL (`api.runpod.io/graphql`)
  calls to launch/poll/terminate on-demand GPU pods for DINOv2 embedding batches (Wave 5) —
  today exercised only by the `new_dedup_runpod_smoke_test.yml` workflow_dispatch. Every job
  goes through `RunPodClient.run_job`, which terminates the pod in a `finally` regardless of
  how the job ends — the actual cost-safety guarantee, since RunPod's pod API has no
  documented "run once and stop" flag.

Frontend / extension (build-time only, inlined into the browser bundle — *not* backend
runtime): `VITE_SUPABASE_URL` + `VITE_SUPABASE_ANON_KEY` (the publishable anon key, safe in
the browser), and `VITE_API_BASE_URL` / `VITE_API_TOKEN` for the SPA (Path 1 posture: the
static token is embedded in a build shipped only to trusted operators, until the
platform-wide rotation cutover). The **extension** (`EXT_API_BASE_URL` /
`EXT_SUPABASE_URL` / `EXT_SUPABASE_ANON_KEY` repo secrets → `VITE_API_BASE_URL` /
`VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` at build) no longer carries `VITE_API_TOKEN` /
`EXT_API_TOKEN` at all (Wave 1, 2026-07-21) — it runs its own Supabase session via a
hand-rolled PKCE flow (`chrome-extension/src/auth.ts`), so no bearer secret ships in the
bundle and it's safe to distribute broadly.

