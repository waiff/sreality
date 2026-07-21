---
name: toolkit-api
description: Use when writing or changing analytical toolkit functions (toolkit/) or the FastAPI service (api/) — the facts-not-opinions rule, the standard tool return envelope, the read-only-with-write-exceptions rule, dual-mode auth (legacy bearer token + Supabase JWT / login / admin gating / identity), the billing/entitlements skeleton (Stripe webhook, plans, agenda gating), the versioned estimation trace, provider pluggability (Anthropic + Gemini), or the full env-var/secrets reference (Postgres, tenant pool, R2 images, LLM+maps keys, API service, notification delivery, scraper orchestration, frontend/extension build-time). Triggers on: new toolkit tool, /admin route, API_TOKEN, login, admin gating, identity, account menu, billing, Stripe, entitlement, plan, agenda gating, write exception, estimation_runs.trace, llm_calls, provider, env var, secret, R2/ANTHROPIC/GEMINI/MAPY/RESEND/TELEGRAM/STRIPE keys, CORS.
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
6. **Spatial queries use `geography(point, 4326)`.** Always `ST_DWithin(geom, target_geom,
   radius_m)`. Never compute distance in Python.
7. **psycopg directly, not supabase-py.** Same reasoning as the scraper.
   `prepare_threshold=None` for pgbouncer-mode pooler.
8. **Dual-auth window: a legacy bearer token AND Supabase-JWT auth coexist.** Baseline:
   every endpoint except `/health` requires `Authorization: Bearer <token>` when
   `API_TOKEN` is set (no-op when unset, for local dev); `/health` stays open for Railway
   healthchecks. `/admin/*` (Settings-page surface: skills, `app_settings`, agent tool
   inventory) is bearer-gated like every other write surface — it was historically exempt
   on the theory that the private Railway URL was the perimeter, but that URL ships
   inside the public SPA bundle, so the exemption gave no real protection.
   **Phase 1 (increments 1–4, #747/#753/#763/#765) layered identity on top**, not instead
   of the token: `/admin/*`, `/dedup/*`, `/outreach/*`, `/broker-review/*`,
   `/skill-refinements/*`, and dataset-write/dispatch routes on price-stats now use
   `require_admin` (JWT-gated, see below) instead of plain `require_token`; every other
   route is still bearer-only. The legacy static token still passes `require_admin` too
   (see below) — this is a coexistence window, not a hard cutover, and no route currently
   requires a JWT with no token fallback.
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

## Identity, login, and admin gating (Phase 1, `api/dependencies.py`)

Three auth primitives now coexist in `api/dependencies.py`:
- `require_token` — the original bearer-token gate (rule #8's baseline), unchanged.
- `verify_jwt` — verifies a Supabase user JWT and returns its claims. Preferred path:
  asymmetric JWKS (`SUPABASE_URL` → `/auth/v1/.well-known/jwks.json`, ES256/RS256, cached
  via `PyJWKClient`, no shared secret). Falls back to a shared HS256 secret
  (`SUPABASE_JWT_SECRET`) if that's all that's configured. **Dual-auth branch:** the
  legacy static `API_TOKEN` bearer is checked FIRST and, if it matches, returns a
  synthetic claims dict `{"sub": None, "role": "operator", "is_admin": True,
  "legacy": True}` — so a route behind `verify_jwt`/`require_admin` still accepts the
  operator's existing token. Fails closed with `503` if neither JWKS nor the HS256
  secret is configured (an unconfigured auth backend must never authenticate anyone).
- `require_admin` (`Depends(verify_jwt)`) — gates on `claims["is_admin"]` or
  `claims["app_metadata"]["is_admin"]`; `403` otherwise. The legacy synthetic claims
  dict always has `is_admin: True`, so the operator token passes this too.

`SYSTEM_ACCOUNT_ID = "00000000-0000-0000-0000-000000000000"` mirrors migration 286's
fixed system account — legacy callers (no Supabase `sub`) resolve to it until they
re-auth with a real JWT.

For routes that need per-account **data isolation** (not just an admin/non-admin split),
use `api/tenant_pool.py`'s `tenant_conn` dependency instead of the service-role
`get_db_conn` — it opens an RLS-scoped transaction under the `tenant_pool` role. See the
`database` skill's connection-modes + Multi-tenancy sections for the mechanics;
`verify_jwt` is authentication, `tenant_conn` (via RLS) is authorization. Its
`resolve_account_id(conn, claims)` helper picks the caller's own account, or — for the
legacy operator — whichever account claimed the legacy backfill (`None` until that
happens).

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
caller's plan has that agenda's visibility flag on; admin + legacy claims always pass (the
operator is never billing-gated). **Not wired to any route yet** — a future wave attaches it
per-agenda; don't assume any endpoint is currently billing-gated.

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

LLM + maps (FastAPI service + scoring jobs):
- `ANTHROPIC_API_KEY` — required for the URL parser, summarize/vision tools, condition
  scoring, and the agent under `provider='anthropic'`.
- `GEMINI_API_KEY` — Google AI Studio key; required for the agent under `provider='gemini'`.
  A request selecting an unconfigured provider returns 502; missing at boot is not fatal.
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

