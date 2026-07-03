---
name: toolkit-api
description: Use when writing or changing analytical toolkit functions (toolkit/) or the FastAPI service (api/) — the facts-not-opinions rule, the standard tool return envelope, the read-only-with-write-exceptions rule, bearer-token auth, the versioned estimation trace, provider pluggability, or the full env-var/secrets reference (Postgres, R2 images, LLM+maps keys, API service, notification delivery, scraper orchestration, frontend/extension build-time). Triggers on: new toolkit tool, /admin route, API_TOKEN, write exception, estimation_runs.trace, llm_calls, provider, env var, secret, R2/ANTHROPIC/MAPY/RESEND/TELEGRAM keys, CORS.
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
8. **API auth gated by `API_TOKEN`.** When the env var is set, every endpoint except
   `/health` requires `Authorization: Bearer <token>`. When unset (local development) the
   gate is a no-op. `/health` stays open so Railway healthchecks keep working. `/admin/*`
   (Settings-page surface: skills, `app_settings`, agent tool inventory) is **bearer-gated
   like every other write surface** — its router carries `Depends(require_token)`. (It was
   historically exempt on the theory that the private Railway URL was the perimeter, but that
   URL ships inside the public SPA bundle, so the exemption gave no real protection; the SPA's
   Settings page already sends the token on every `/admin` call, so gating it is transparent.)
   Every route except `/health` requires the token; *no* write path bypasses the FastAPI
   service. The token is shared with every caller (including the Chrome extension and any
   ClickUp caller); no per-user identity layer.
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
    tag.

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
  (`SUPABASE_SERVICE_ROLE_KEY` is the 2025 `sb_secret_...` token, **not** a JWT.)

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

Notification delivery (Sprint N — `channel_sends` ledger + `api/transports/` + the outbox loop,
rule #16; all OPTIONAL, dark until set):
- `RESEND_API_KEY` + `EMAIL_FROM` — the Resend email transport (`api/transports/email_resend.py`).
  Both required for `is_configured()`; transactional/self-notification scope only (Resend AUP
  forbids cold outreach — outreach gets a separate EU vendor). Railway API env.
- `TELEGRAM_BOT_TOKEN` — the Telegram Bot API transport (`api/transports/telegram.py`). Railway
  API env. The recipient `chat_id` lives in `app_settings.notification_telegram_chat_id`.
- `SPA_BASE_URL` — SPA origin for notification deep links (`{SPA_BASE_URL}/listing/{id}`).
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
the browser), and `VITE_API_BASE_URL` / `VITE_API_TOKEN` (and the extension's
`EXT_API_BASE_URL` / `EXT_API_TOKEN` that map to them). These follow the Path 1 posture: the
token is embedded in a build shipped only to trusted operators.

