# CLAUDE.md

The project brain: standing context for any Claude Code session that touches this
repo. Read it before changing anything. When a rule here keeps getting broken, the
fix is to update this file — not to repeat the correction by hand.

## What this project is

A **market-wide real-estate intelligence platform** for the Czech market. It began
as an hourly sreality.cz scraper and has grown into a system that collects, enriches,
and reasons over property data from multiple portals. The store of record is a
Postgres database (Supabase, Frankfurt region, PostGIS enabled) with full listing
history.

It works with several kinds of data, layered together:
- **Scraped listings** from a growing set of portals. sreality.cz (public JSON v1 API)
  is the steady hourly ingest; bazos.cz (HTML crawler) is a manual pilot, with more
  portals rolling out per `docs/design/multi-portal-dedup.md`.
- **Geo data** — coordinates, districts, ČÚZK/RÚIAN admin boundaries, transit-route
  geometry, OSM amenities.
- **Operator-supplied data** — curated city-quality indexes, collections, building
  unit decompositions, estimation inputs.
- **Derived / computed data** — condition scores, time-on-market velocity,
  statistics, and LLM-produced summaries, image comparisons, and value estimates.

Two goals sit on top of that data:
1. **Robust, polite scraping** that preserves history — latest-wins current state plus
   append-only snapshots; nothing is ever deleted.
2. **Letting the user *and* autonomous AI agents work with the data many ways** —
   filter through a large filter set or on a map, layer different views on the map,
   see statistics for regions / properties / property types, estimate sale and rental
   values, browse, run watchdog alerts on any saved filter, and more. The list keeps
   developing — ROADMAP.md is the sequencing source of truth for what's next.

Surfaces over this data: an analytical **toolkit + FastAPI service** (Railway), a
**React browser UI** (Railway, separate service — reads public data directly and
routes every write through the API), and a **Chrome extension** that overlays
estimates on portal listing pages. Multi-portal rows land behind a thin `properties`
parent (migration 091) so the same real-world property seen on several portals can be
grouped.

**Data source (sreality v1 API).** In 2026 sreality rebuilt their site on Next.js and
removed the old `/api/cs/v2/estates` API the scraper was born on. The scraper now
reads the public JSON v1 API: `GET /api/v1/estates/search` (filters `category_main_cb`
/ `category_type_cb` / `locality_country_id=112`, **offset/limit** paging,
`pagination.total` for completeness) for the index, and `GET /api/v1/estates/{id}` for
detail (a `{categoryMainCb, locality, params{…}, images, price…}` object; `params`
holds the typed attributes). No cookies needed. The deep-pagination cap still applies
(HTTP 422 past the window), so large categories are walked per-district
(`SPLIT_THRESHOLD` / `DISTRICT_IDS`). `parser.parse_listing` maps that object to the
row contract; `scraper/hashing.py` strips the volatile fields (`params.stats` view
counter, `note`/`rus`/`rusReply`).

**Data source (bazos.cz).** A separate HTML crawler (`scraper/bazos_client.py`,
`bazos_parser.py`, `bazos_main.py`, workflow `scrape_bazos.yml` — a manual pilot, not yet
scheduled) lands bazos listings into the same `listings`/`listing_snapshots` contract,
tagged `source='bazos'`. Raw HTML is staged in `portal_raw_pages` (migration 099) before
parsing.

## Territories

The repo is split into **three** top-level territories with deliberately different
rules. Identify which one a task belongs to before you start.

**Backend territory** (`scraper/`, `toolkit/`, `api/`, `migrations/`, `tests/`,
`.github/workflows/`):
- Python 3.12, stdlib-first, `psycopg` direct to Postgres.
- Service-role database access. Reads and writes anything.
- Runs in GitHub Actions (scrapers + scheduled jobs) or Railway (FastAPI).
- All rules below apply: append-only migrations, snapshot-on-change, no deletes, no
  `supabase-py`, etc.

**Frontend territory** (`frontend/`):
- Browser code. Vite + React 18 + TypeScript + Tailwind v4 SPA, served by Caddy from a
  two-stage Docker build (see `frontend/Dockerfile`). Deployed to Railway as a separate
  service alongside the API.
- The current page set lives in `frontend/src/routes.tsx` (consult it rather than
  trusting a list here, which rots). Today it spans **Browse** (filters → Map / Table /
  Stats), **Listing Detail** (with the snapshot-timeline strip — the product's
  signature visual element), **Region**, **Health** (operator dashboard),
  **Estimations** + **Estimation Detail**, **Building Detail**, **Collections** +
  **Collection Detail**, **Watchdog** (in-app notification feed) + its manage/edit
  routes, and **Settings**. The `Timeline` component dispatches on `step.kind` so it
  renders today's deterministic traces and the agent's longer traces without rework.
  Extend this SPA; do not fork a separate frontend tree.
- Connects with the **publishable (`anon`) key only**. Never embed the service-role
  key, the `SUPABASE_DB_URL`, or any other secret in browser-shipped code.
- Reads exclusively from the `*_public` views and the page-specific RPCs (e.g.
  `listings_public` / `properties_public`, `browse_stats`, `region_stats`,
  `health_summary`, `listings_with_city_quality`). All RPCs are `SECURITY INVOKER` and
  rely on anon's existing SELECT grant on the public views — they don't escalate. New
  public-data RPCs follow the same pattern; new private RPCs go through the FastAPI
  service.
- **No write path from the browser.** Any UI action that needs a write goes through the
  bearer-token-gated FastAPI service, not direct Postgres. The toolkit's write-allowed
  exceptions (see Toolkit rule #5) are reachable only via the API.
- **`Mapy.cz`-powered location search.** The Region/Browse pages call `GET /maps/suggest`
  and `POST /maps/resolve` on the FastAPI service for autocomplete + admin-unit
  resolution. The `MAPY_CZ_API_KEY` is server-side only — never inlined into the browser
  bundle. When the API returns 503 (key unset), the search box renders a graceful
  fallback hint and auto-opens the Advanced disclosure with the legacy district / radius
  pickers.
- Frontend conventions live in `frontend/README.md`. Design tokens are in
  `frontend/src/styles/globals.css` under a single `@theme` block; **never tweak these
  tokens without operator approval** — they encode the agreed visual direction
  (civic-archive feel, oxidised-copper accent, borders-only depth, tabular numerals,
  Czech locale formatting). Add new tokens only at the bottom of the file with a clear
  domain-name.
- Backend rules below (psycopg, no `supabase-py`, stdlib-first, etc.) do not apply
  inside `frontend/`.

**Chrome-extension territory** (`chrome-extension/`):
- Manifest v3 browser extension that overlays a yield/estimate panel on portal listing
  pages. The content script today matches `sreality.cz/detail/*`; the **intent is to
  cover every portal we scrape**, so the panel pops up on any listing page where the
  extension has a workable feature — surfacing an estimate we've already produced, or
  letting the operator trigger an on-demand run from the page. (`host_permissions` is
  broad `https://*/*` for the background fetch; widen the content-script `matches` as
  new portals come online.) Two-entry Vite build (`content.js` + `background.js`) plus a
  copied-over `manifest.json` and `icon-128.png`; output lands in
  `chrome-extension/dist/`.
- **Vanilla TypeScript only — no React, no Tailwind.** The panel lives inside a closed
  shadow root with its own scoped CSS in `src/styles.css?inline`. Palette mirrors the
  SPA's civic-archive tokens by hand-coded values (no `@theme` import). Keep the bundle
  small.
- Every network call goes through the background service worker via
  `chrome.runtime.sendMessage` so `host_permissions` covers the API origin and the fetch
  isn't subject to the portal's CORS posture. The content script never calls `fetch`
  directly.
- Build-time secrets: `VITE_API_BASE_URL` + `VITE_API_TOKEN` are inlined into `dist/` —
  same Path 1 security posture as the SPA. Ship `dist/` only to trusted operators; never
  upload to the public Chrome Web Store. `chrome-extension/README.md` documents Path 3
  (no embedded token, writes bounced through the SPA) for when a publicly-shareable build
  is needed.
- The extension's origin (`chrome-extension://<id>`) must be added to the FastAPI
  service's `CORS_ALLOW_ORIGINS` env var. Install unpacked first, copy the assigned ID
  from `chrome://extensions`, then update the Railway env var.
- Backend rules (psycopg, stdlib-first, etc.) and SPA conventions (React, Tailwind,
  design tokens) do NOT apply inside `chrome-extension/`.

When in doubt about which territory a task belongs to, ask. Don't import frontend deps
into the Python tree or vice versa.

## Working with the operator

The owner of this repo works locally in **VS Code on WSL2 Ubuntu**, connected to GitHub.
They have a full terminal, local Git, local Python, and the GitHub CLI (`gh`, already
authenticated). So you can — and should — suggest and run local commands: run tests
locally, run Git, drive workflows with `gh`, debug interactively.

- **Production execution still runs in the cloud**: scrapers + scheduled jobs in GitHub
  Actions, the FastAPI service + frontend on Railway. Local execution is for development,
  testing, and debugging — not a replacement for the deployed runtime.
- The operator is **non-technical by training but learns fast**. Explain the *why*, not
  just the *what*, and define jargon the first time it appears ("upsert," "JWT," "RLS,"
  "draft PR," etc.). Teaching is welcome — a one-line "here's what this command does and
  why" beats silent execution.
- For tasks that genuinely need a browser (Supabase SQL editor, GitHub Settings pages),
  give click-by-click instructions: which page, which menu, which button.

## Git workflow and pull requests

Work on short-lived branches and merge via pull request. **Never push directly to `main`.**
Railway auto-deploys from `main`, so a merged PR *is* the deploy — the PR + branch
protection + CI is the gate that protects production.

- **Branch naming:** `feature/<short-name>` (new code), `fix/<short-name>` (bug fixes),
  `cleanup/<short-name>` or `roadmap/<short-name>` (repo hygiene & docs).
- **Start a piece of work** with: `git checkout main && git pull && git checkout -b <branch>`.
- **One PR = one purpose.** Don't mix a feature with an unrelated docs/ROADMAP rewrite —
  split them so reviews stay simple and conflicts stay small. (Exception: the small
  ROADMAP phase-entry bookkeeping that records the work you just shipped rides in that
  same PR; a *large* ROADMAP restructure is its own PR.)
- **End** by pushing the branch and opening a PR. Return the PR URL.

## Autonomy and the safety net

The operator wants to describe features and have you run with them. Default to **full
autopilot**: at the start of a task, create the branch, push early, open a **draft PR**
(so the operator can watch without interrupting), and work to completion.

- **Stop and surface** — don't paper over — a merge conflict, a failing test, or genuine
  ambiguity. Report what you found before continuing.
- **The safety net that makes autonomy safe:** CI (`.github/workflows/test.yml`) runs on
  every push, and branch protection guards `main`, so broken code can't reach production.
  Lean on it; keep tests green.
- **Mode guidance:** use plan mode for large or unfamiliar work; default mode for routine
  work; reach for autopilot on patterns already validated together.
- **Database changes** have their own gate — see "Database access" below (additive
  migrations are autonomous; destructive ones pause for confirmation).

## Fetching live state (fetch, don't ask)

Dynamic state lives outside Git, not in tracked files. Don't ask the operator for context
you can fetch in one command — just fetch it:
- Recent activity → `git log --oneline -10`
- Current branch / working tree → `git branch --show-current`, `git status`
- Migrations on disk → `ls migrations/ | tail -5`
- Database counts, freshness, schema → the Supabase MCP tools
- GitHub Actions runs → `gh run list --limit 10`

## Database access

We connect directly to Supabase Postgres with `psycopg` v3 (not the Supabase REST
client), for two reasons:
- **PostGIS support:** inserting `geography(point, 4326)` is one line of SQL with
  `ST_SetSRID(ST_MakePoint(lon, lat), 4326)`. The PostgREST equivalent needs a stored
  procedure or fragile GeoJSON casting.
- **Atomic transactions:** writing `listings`, `listing_snapshots`, and `images` for one
  listing happens inside a single transaction. The REST client cannot span tables
  atomically.

Do not introduce `supabase-py` without an explicit reason and a discussion.

**Two connection modes.** `scraper/db.py` exposes two factories:
- `connect()` — the **default for everything** (scrape_run bookkeeping, bazos, images,
  recompute, API, scripts). Points at `SUPABASE_DB_URL` (the **Transaction-mode pooler**,
  port 6543) with `prepare_threshold=None`. Disabling auto-prepare is **required** there:
  PgBouncer rebinds connections between queries, so a cached prepared statement would trip
  `DuplicatePreparedStatement`.
- `connect_session()` — **only** for the scraper's hot detail-write loop (the long-lived
  connection in `scraper/main.py:_run_full`). Points at `SUPABASE_DB_SESSION_URL` (the
  **Session-mode pooler**, port 5432) and leaves `prepare_threshold` at psycopg3's default,
  so the repeated upsert + spatial SQL gets server-side **prepared once and reused** across
  every listing in the run (the plan isn't re-derived per call). The session pooler gives
  each client a dedicated backend, so prepared statements are safe there. If
  `SUPABASE_DB_SESSION_URL` is unset, `connect_session()` **falls back to `connect()`**, so
  nothing breaks where the secret isn't configured.

**Supabase MCP.** Claude Code has direct read/write access to the production Supabase
project via the MCP integration. Use it for: inspecting the live schema, running SELECT
queries to verify data state, applying migrations, running backfill UPDATEs, and
confirming changes succeeded. The MCP connection points at **production** — there is no
separate dev/staging database. Treat every operation accordingly.

**`migrations/` is the source of truth for schema.** MCP is the *execution* mechanism,
not a replacement for tracked migrations. Applying a schema change without committing the
corresponding migration file silently breaks the codebase — future sessions or fresh
rebuilds will be missing the change. "Append-only" means **never rewrite migration
history** (never edit an existing numbered file); it does **not** trap us into keeping
dead schema — prune an unused table/column by writing a *new* forward migration that
drops it (a destructive change — see the policy below).

**Migration safety policy (under autopilot):**
- **Additive migrations** (new tables / columns / indexes / RPCs) — write the new
  numbered file, commit it, apply via MCP, verify with a SELECT, and report. No approval
  gate; CI + the tracked file are the net.
- **Destructive migrations** (`DROP TABLE`/`COLUMN`, type-changing `ALTER`, `DELETE`
  without `WHERE`, `TRUNCATE`) — **pause for explicit operator OK** ("yes, apply it") and
  take a `pg_dump` backup of the affected tables *first*. There's no staging DB, so these
  are largely irreversible.
- Read-only inspection (counts, sample rows, schema introspection, verifying backfills)
  needs no confirmation — just do it and report.

Correct flow for any schema change: (1) write the new numbered migration file in
`migrations/`; (2) for destructive changes, get explicit approval + back up first;
(3) apply via MCP (`apply_migration`), verify with a SELECT; (4) commit the migration
file in the same change; (5) report what was applied and verified.

## Roadmap maintenance

`ROADMAP.md` is the manual sequencing source of truth — narrative phase entries, no
generated content. (Live status is fetched on demand per "Fetching live state"; nothing
dynamic is committed into tracked files, which is why the old auto-status block was
removed — it caused repeated cross-session merge conflicts.)

After shipping meaningful work (a merged PR that completes a phase bullet, a new
migration, a new toolkit function, a new UI page), update the relevant phase entry in the
same PR as the work: move bullets from `## Next` to `## Done`, add new "next" items if
scope changed, update the map / scraper / UI tracks. Don't defer roadmap updates to a
follow-up commit. (A large ROADMAP restructure is its own PR — see the Git workflow.)

## Architectural rules (do not violate without asking)

1. **The schema in `migrations/` is append-only.** Never modify an existing migration.
   Schema changes go in a new numbered file (`002_*.sql`, `003_*.sql`...) and are applied
   via the Supabase MCP. See "Database access" for the full flow and the
   additive-vs-destructive policy.
2. **Snapshots on content change only.** Never insert into `listings` without computing
   the content hash and inserting into `listing_snapshots` if it differs from the most
   recent snapshot for that listing.
3. **Never delete listings.** Listings that disappear get `is_active=false`. History is
   sacred. The `is_active=false` inference is only valid after a **complete index walk** —
   a partial walk (`--limit N`, `--detail-only`) cannot determine which listings are gone.
   The scraper enforces this: `mark_inactive` is skipped when `--limit` is set, and
   `--detail-only` never reaches the index phase.
4. **`last_seen_at` is driven by index sightings and successful detail fetches; failed
   fetches never touch it.** Every existing listing whose id appears in the run's index
   gets its `last_seen_at` bumped before any detail fetches happen. A successful detail
   fetch (cron or on-demand via `freshness_check`) also bumps `last_seen_at` as a side
   effect of `db.upsert_listing` — that's real evidence the listing is alive. A *failed*
   detail fetch must not affect `last_seen_at`, otherwise repeated failures would falsely
   flip a still-live listing to `is_active=false`. The `unchanged` path of
   `freshness_check` deliberately does NOT bump `last_seen_at` either — for that case the
   "I confirmed it" signal lives in `listing_freshness_checks.checked_at` instead. See
   architectural rule #9.
5. **Failed detail fetches are tracked, not silently dropped.** When a detail fetch (HTTP,
   parse, or DB write) fails, we record it in `listing_fetch_failures(sreality_id,
   attempts, last_error, given_up)`. Next run, listings with an active failure row jump to
   the front of `to_refetch` so the per-run cap can't keep deferring them. After 5 attempts
   a row's `given_up` flips to true and it falls out of the active retry queue (manual SQL
   un-flip required to retry). On successful fetch the failure row is deleted. Inspect with
   `SELECT * FROM listing_fetch_failures ORDER BY attempts DESC`.
6. **Images are downloaded to Cloudflare R2.** v1 only stored URLs; v1.5 downloads the
   bytes to an R2 bucket (S3-compatible) so the data survives the CDN expiring listing
   photos. The `images` table tracks per-image download state via `storage_path`,
   `download_attempts`, and `last_download_attempt_at`. Image-download is a separate phase
   after the scrape phase; it's a no-op if R2 env vars are missing, so a partial deploy
   never breaks the scrape.
7. **No new dependencies without justification.** Each entry in `pyproject.toml` should
   have a clear reason. Prefer the stdlib.
8. **Latest-wins data model with snapshot history.** The `listings` table always reflects
   the most recent state. Every meaningful change appends a row to `listing_snapshots`.
   Analytical queries default to current state for relevance. Estimates that need
   retrospective auditability record the `snapshot_id` of each comparable they used — that
   resolves to the exact JSON the estimate relied on, even if the listing has since been
   updated or marked inactive. Avoid building "as-of" semantics into live queries; capture
   snapshot IDs in the estimate response instead.
9. **`listing_freshness_checks` is append-only and ephemeral.** Rows older than 30 days are
   safe to delete. No automated pruning is built; manual SQL when the table gets large. The
   table records every on-demand verification triggered by `verify_listing_freshness` — its
   primary purpose is observability and per-listing throttling, not history. The primary
   history table is `listing_snapshots`.
10. **`amenities` + `amenity_fetches` are a local OSM mirror, not a history table.**
    Populated by `find_anchor_amenities` on cache miss via Overpass. Cache key is
    `(category, radius_m, exact center, fetched_at within TTL)`. POIs accumulate; no
    automated deletion of POIs that have disappeared from OSM (out of scope). Manual SQL
    pruning when the audit table gets large. Categories are determined by the *query* that
    fetched a POI, not the OSM tags themselves — `ON CONFLICT (source, source_id)`
    overwrites on subsequent fetches under different categories. The canonical category
    taxonomy lives in `toolkit/amenities.CATEGORY_TAGS`; add new categories there.
11. **`transit_lines` + `transit_line_fetches` are a parallel OSM mirror for route geometry
    (migration 028).** Populated by `find_comparables_along_axis` on cache miss via
    Overpass. One row per (relation, member way) pair — `source_id` is
    `"relation/R/way/W"` — so a single relation produces N rows of clean polylines and a
    way shared by two relations occupies two rows. Avoids the merge ambiguity that bites
    when a route has branches or loops. Cache key is sha256 of the canonicalised
    `(bbox, transport_types)` pair; bbox values are rounded inside `_bbox_around` so
    identical anchor + radius callers share the same cache row. TTL default 30 days,
    matching the amenity TTL. Same accumulate-and-prune discipline as amenities; allowed
    transport types are tram / subway / bus.
12. **`estimation_runs` is the single source of truth for every estimation.** Every
    UI/API/ClickUp/agent invocation lands here. Synchronous deterministic mode INSERTs once
    with a terminal `status` (`'success'` or `'failed'`); the schema reserves
    `'pending'`/`'running'` for the async agent without forcing today's code to write twice.
    Failed runs still persist a row — the row IS the audit trail; the endpoint returns HTTP
    200 with `status='failed'` and `error_message` set. Re-runs INSERT a new row with
    `parent_run_id` set; the original is immutable. Legal `source` values today: `'ui'`,
    `'api'`, `'clickup'` (CHECK constraint, not enum — adding more is a single ALTER).
13. **`building_runs` is the parent grouping for the paste-a-building workflow.** One row
    per pasted house listing (typically `category_main='dum'`). Children are normal
    `estimation_runs` rows linked back via `building_run_id` (FK, `ON DELETE SET NULL` so
    child estimations survive parent cleanup) + `building_unit_id` (stable string ID
    matching an entry in the parent's `units` JSONB). The unit list lives as JSONB on the
    parent — operator-curated, ~5-10 entries, not an analytical object. Status flow:
    `pending` → `extracting` → `awaiting_input` → `estimating` → `success` | `failed`. The
    `awaiting_input` pause is the human-in-the-loop gate where the operator confirms / edits
    the agent's tentative unit decomposition before per-unit estimates fan out — the
    explicit departure from the `estimation_runs` single-shot flow. `units_proposal` (agent
    output, append-only after extraction) and `units` (operator-confirmed) are kept separate
    so the extractor's original guess is auditable. The business-case overlay lives in
    `business_case jsonb` on this same row.
14. **Condition scoring is two-axis (building + apartment).** `listings.condition` (the raw
    sreality "Stav objektu" enum, ~11 Czech text values) stays as the source field — it's
    what `listings_public` exposes and what the legacy filter binds against. The two derived
    columns `listings.building_condition_level` and `apartment_condition_level` (integers
    1..5, NULL if not yet scored) live alongside it, computed by
    `toolkit.condition_scoring.score_listing_condition`. The score cache lives in
    `listing_condition_scores`, keyed on `(sreality_id, snapshot_id)` — same
    auto-invalidation pattern as `listing_summaries` / `listing_marker_extractions`. The
    scorer writes the cache row AND updates the two `listings` columns in one transaction
    with a latest-wins guard so a stale-snapshot scorer can't overwrite a fresher score. The
    coarse `condition_assessment` produced by `summarize_listing` is for cohort skimming,
    not authoritative filtering — use the new columns for that. The 5-level rubric lives in
    `data/condition_rubric_v1.json` (committed) and is loaded into
    `app_settings.llm_condition_rubric` by `scripts/seed_condition_settings.py`; the curated
    marker dictionary follows the same pattern via `data/condition_markers_curated.json` →
    `app_settings.llm_condition_marker_dictionary`.
15. **Multi-portal listings sit behind a thin `properties` parent (migration 091).** Each
    `listings` row carries `(source, source_id_native)` (unique together) plus `source_url`,
    and an FK `property_id` to a `properties` row that groups observations of the same
    real-world property across portals. `properties` holds a representative display row plus
    derived rollups (`source_count`, price-change aggregates, lifecycle `is_active` /
    `first/last_seen_at`), maintained by an **async recompute job** (`recompute_property_stats.yml`),
    never inline in the scrape. `is_active` / `last_seen_at` are **per-source** on the
    `listings` row; the property-level rollup is derived, not authoritative per source. An
    insert-time Tier-1 matcher (geo + price + area proximity) seeds candidate groupings.
    Today sreality + bazos ingest; further portals follow the design in
    `docs/design/multi-portal-dedup.md`. Frontend Browse reads `properties_public`.
16. **Watchdog and Browse share one definition of "matches."** Saved watchdog filters live
    in `notification_subscriptions` (migration 056); the background matcher in
    `api/notifications.py` builds its WHERE clauses from the **same** logic Browse uses
    (`toolkit/comparables._shared_filter_where` + the shared `_city_quality_clauses`
    helper), so the two surfaces can never disagree on what a filter means. Dispatches are
    **property-grain** and append-only, deduped by `UNIQUE(subscription_id, property_id,
    change_kind)`. Delivery is **in-app only today** (`channel='in_app'` CHECK); a free
    email channel is planned (extend via ALTER, not a rewrite).
17. **City-quality indexes are a normalized, operator-curated time series.** `curated_cities`
    + `city_index_revisions` + `city_index_values` + `city_index_definitions` +
    `city_population` (migration 078 onward) store per-city indexes long-form, so a new index
    on next upload needs no migration; each upload appends a `source_revision` and the latest
    is the default query target. Filtering goes through the shared `_city_quality_clauses`
    helper and the `listings_with_city_quality` RPC, and the filters are **agenda-gated to
    BROWSE + WATCHDOG only** (`toolkit/filter_registry.py`) — the estimation agent
    deliberately never sees them, preserving deterministic estimate semantics.
18. **Collections are operator-curated many-to-many groupings of listings** (`collections` +
    `collection_listings(collection_id, sreality_id)`, migration 022). Writes flow through
    the FastAPI service; the browser never writes directly. Same no-hard-delete spirit as the
    rest of the data model.

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
     for a Browse > Stats cohort to `region_disposition_annotations` (migration 102, keyed on
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
   `/health` and `/admin/*` requires `Authorization: Bearer <token>`. When unset (local
   development) the gate is a no-op. `/health` stays open so Railway healthchecks keep
   working. `/admin/*` (Settings-page surface: skills, `app_settings`, agent tool inventory)
   is also exempt: the operator chose to skip per-page auth, so the private Railway URL is
   the security perimeter for that prefix. Every other route still requires the token; *no*
   write path bypasses the FastAPI service. The token is shared with every caller (including
   the Chrome extension and any ClickUp caller); no per-user identity layer.
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
  Connect → Session pooler, port 5432). **Optional**; used only by the scraper's hot
  detail-write loop (`connect_session()`) so its repeated SQL gets prepared statements.
  Unset → falls back to `SUPABASE_DB_URL`. Set it as an Actions secret on the scrape
  workflow (and the Railway env var only if the API ever calls `connect_session()`).
- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` — set as Actions secrets for forward
  compatibility; the v1 scraper connects to Postgres directly and does not need them.
  (`SUPABASE_SERVICE_ROLE_KEY` is the 2025 `sb_secret_...` token, **not** a JWT.)

Image storage (Cloudflare R2, S3-compatible) — all optional; if any is missing the
image-download phase logs a skip and exits zero:
- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` (usually
  `sreality-images`).

LLM + maps (FastAPI service + scoring jobs):
- `ANTHROPIC_API_KEY` — required for the URL parser, summarize/vision tools, condition
  scoring, and the agent under `provider='anthropic'`.
- `GEMINI_API_KEY` — Google AI Studio key; required for the agent under `provider='gemini'`.
  A request selecting an unconfigured provider returns 502; missing at boot is not fatal.
- `MAPY_CZ_API_KEY` — Mapy.cz REST key; geocodes locality strings and powers `/maps/*`.
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

## LLM-backed parsing

`scraper.source_dispatcher.parse_listing_url` is the single entry point for any listing URL
(sreality or otherwise). It classifies the URL by domain and routes to either the
deterministic sreality flow (`scraper.url_parser`, unchanged) or an LLM-driven per-source
parser under `scraper/source_parsers/`. Today's allowlist is `bezrealitky`, `idnes_reality`
(reality.idnes.cz), and `remax` (remax-czech.cz); everything else falls through to a
best-effort generic parser that always reports `parse_confidence='best_effort'`. (Note: bazos
is ingested by its own crawler into `listings`, not through this on-demand URL parser.)

The LLM path:
1. Cache check against `parsed_url_cache`. Key is sha256 of the canonicalised URL (lowercase
   scheme/host, no query, no trailing slash). Hit → return cached spec, no LLM, no cost.
2. Fetch HTML, send to Claude with the system prompt from `app_settings.llm_parse_system_prompt`
   and the per-source user prompt from `scraper.source_parsers.<source>`. The model is
   `app_settings.llm_parse_model` (default `claude-sonnet-4-5`, from `api/llm_client.py`).
3. The LLM is required to invoke `record_listing` exactly once with every field in a
   `{value, confidence}` envelope. Any deviation raises `ParseError` and surfaces as a 502
   from `/estimations/preview` or a `failed` row from `POST /estimations`.
4. If the page didn't yield lat/lng, geocode the locality string via Mapy.cz
   (`scraper.geocoding`). The geocode confidence rolls into
   `parse_confidence_per_field['lat'/'lng']`.
5. Store the full extraction + spec + warnings in `parsed_url_cache` with a 7-day TTL.

Operator-tunable parser behaviour lives in `app_settings` (system prompt, model name) — edits
take effect on the next preview/estimation, no deploy. Every prior value is preserved in
`app_settings_history` (migration 020 trigger). Every call is recorded in `llm_calls` with
token counts (incl. cache-read/write splits), USD cost, duration, and the optional
`estimation_run_id`; `called_for='parse_url'`.

## LLM-backed analysis

Several analytical toolkit functions reach for Claude. Each caches its result locally and
auto-invalidates, logs to `llm_calls` under a distinct `called_for`, and is a write-allowed
exception per Toolkit rule #5. System prompts and model IDs are operator-tunable via
`app_settings` (model defaults `claude-sonnet-4-5`).

- `summarize_listing` (`toolkit/summaries.py`, migration 027, cache `listing_summaries`) —
  structured Czech summary of one snapshot: `headline`, `key_highlights`, `concerns`,
  `condition_assessment`, `target_audience`, plus location/building/apartment summaries.
- `compare_listing_images` (`toolkit/image_similarity.py`, migration 027, cache
  `listing_image_comparisons`) — Claude-vision pairwise comparison across six fixed dimensions
  (`exterior`, `kitchen`, `windows_and_light`, `floor_finish`, `lighting`, `styling`) plus an
  `overall_similarity`. Image bytes pulled from R2 server-side via boto3 and base64-encoded.
  Vision is materially more expensive than text (~$0.05/pair) — the cache matters most here.
- `extract_building_units` (`toolkit/building_extraction.py`, migration 036, cache
  `building_unit_extractions`) — structural decomposition of a multi-unit building into a unit
  proposal; the vision extractor behind the building-paste flow.
- `read_floor_plan` (`toolkit/floor_plan.py`, migration 044, cache
  `building_attachment_analyses`, keyed on `(attachment_id, model)`) — vision analysis of one
  operator-supplied attachment (floor plan, drawing, photo).
- `discover_condition_markers` (`toolkit/condition_markers.py`, migration 064, cache
  `listing_marker_extractions`) — mines Czech technical-state phrases ("zateplená budova", "po
  kompletní rekonstrukci") to feed the condition-scoring marker dictionary.
- `score_listing_condition` (`toolkit/condition_scoring.py`, migration 072, cache
  `listing_condition_scores`) — two-axis building/apartment condition levels (1..5) from the
  curated rubric + marker dictionary. See architectural rule #14.
- `summarize_region_dispositions` (`toolkit/region_annotations.py`, migration 102, cache
  `region_disposition_annotations`) — a one-to-two-sentence factual annotation per
  per-disposition Kč/m² box plot in Browse > Stats, from the same `ppm2_box` payload that
  drives the chart. Cached per `(region_hash, day)` — invalidates by calendar day, not by
  snapshot. Powers the `summarize-1` annotated-charts feature; FACTS not opinions (toolkit
  rule #1) — it describes the distribution, never recommends a price.

## Coding conventions

- Python 3.12. Type hints on every function signature.
- Prefer the stdlib. Reach for a dependency only when stdlib is awkward.
- No comments unless the WHY is non-obvious. Don't narrate WHAT the code does.
- No multi-paragraph docstrings. One-line docstrings are fine for module heads.
- `requests` for HTTP, `psycopg` for DB. Don't add `httpx`, `aiohttp`, `sqlalchemy`, or
  `supabase-py` without a strong reason.
- Keep files small and single-purpose: `sreality_client.py` is HTTP only, `parser.py` is
  JSON-to-row mapping only, `db.py` is database I/O only.

## Adding a new scraper field without breaking existing data

1. Add the column with a new numbered migration (`alter table listings add column ...`). Never
   touch `001_initial.sql`.
2. Update the parser in `scraper/parser.py` to extract the field.
3. Update the upsert in `scraper/db.py` to include the new column.
4. Backfill old rows: either leave them NULL (acceptable if the column is nullable) or run a
   one-off SQL update from the `raw_json` column, which already contains the full source
   record.

## How to test changes

- **Locally:** one-time setup `pip install -e ".[dev,api,geo]"`, then `pytest -q` (or
  `pytest tests/path -q` for a subset). The interpreter is `python3` (there's no bare
  `python` on PATH). This mirrors exactly what CI runs.
- **In CI:** every push runs `.github/workflows/test.yml` (`gh run watch` to tail it). CI +
  branch protection on `main` is the gate that makes autopilot safe — it's the reliable
  test signal if local Python deps aren't installed.
- End-to-end without polluting the DB: `--dry-run` (logs what would be written, writes
  nothing).
- Single listing: `--detail-only <sreality_id>`. Small live run: `--limit 10`.

## Refreshing per-source HTML fixtures

The LLM-driven parsers (`scraper/source_parsers/`) are tested against saved listing HTML in
`tests/fixtures/source_html/`. Real listings get taken down or change layout, so every few
months the fixtures need a refresh. Don't fetch live in tests — that would burn LLM credit and
break offline runs.

Refresh (CLI, fastest): `gh workflow run fetch-fixtures.yml --ref <branch>` (add `-f`
inputs to override URLs). Or via the browser: GitHub repo → **Actions** → **Fetch + anonymize
source HTML fixtures** → **Run workflow** → pick branch / optional URLs → **Run workflow**. It
fetches each URL, runs the anonymization in `scripts/fetch_and_anonymize_fixtures.py`, and
commits the resulting `*_sample.html` files back to the same branch. The skipif tests in
`tests/scraper/test_source_parsers/test_real_fixtures.py` light up automatically once the files
exist.

Anonymization scope: phones → `+420 XXX XXX XXX`, emails → `agent@example.cz`, street numbers
(`123/45`) → `XXX/YY`. Listing prices and the surrounding HTML structure are preserved — public
data the parsers need. Agent names are too varied to scrub by regex; if a fixture leaks one,
hand-edit the file.

## How to manually trigger the scrapers

The sreality scrape (`scrape.yml`, "Scraping: Sreality hourly run") and the bazos crawl
(`scrape_bazos.yml`, "Scraping: Bazos crawler (manual pilot)" — dispatch-only, not yet
scheduled) can both be run from the terminal or the browser. You (or Claude) can run them
directly:
- CLI: `gh workflow run scrape.yml --ref <branch>` (add `-f` for optional flags). Watch with
  `gh run list --workflow=scrape.yml` then `gh run watch`.
- Browser: GitHub repo → **Actions** → the workflow → **Run workflow** → pick branch + optional
  flags → **Run workflow**. (All sreality scraping workflows are prefixed `Scraping:` so they
  group together.)

There is **one sreality scrape pipeline**, not a two-tier split. `scrape.yml` (cron `0 * * * *`,
hourly) walks the **entire** index of every category pair (no `--limit`) every run, so
newly-listed properties surface AND delistings flip to `is_active=false`. Because the walk is
complete it runs `mark_inactive` every run. It records `run_type='full'` (auto-derived, since
there's no `--limit`).

- **Detail refetches** are capped per run (`--max-detail-refetches 4000`,
  `--max-detail-refetches-per-category 1200`) — generous caps comfortably above hourly churn so
  details don't perpetually lag, while still bounding worst-case run time. Deferred work drains
  on the next run (failure-priority retry). Fetches run on a thread pool paced by a shared rate
  limiter (`--detail-workers 8` / `--detail-rate 6.0`; the 429/403 auto-backoff is the safety
  net).
- **Images:** the run drains ACTIVE-listing images newest-first to empty
  (`--images-active-only`, no cap). The 2-hourly `images.yml` backfill is the deeper drain that
  also reaches the INACTIVE/historical backlog.
- **Condition scoring is NOT part of the scrape.** It lives in its own decoupled workflow
  `condition_scores.yml` (cron `30 * * * *`) so the LLM phase can never slow the walk; the hourly
  scrape always passes `--no-condition-scoring`. The same workflow's `workflow_dispatch` is the
  manual backfill. An optional cheaper async backend runs scoring through the Anthropic Message
  Batches API (`condition_score_batches.yml`, dispatch-only).

**Cadence:** hourly, deliberately — each run is a complete walk, and hourly keeps a steady,
polite request volume (a too-aggressive schedule is a plausible abuse-flag trigger). GitHub also
throttles scheduled workflows, so effective cadence can be slightly slower; the Health liveness
check is tuned to this (warn >90 min, fail >180 min). Setting `SCRAPE_CHAIN_TOKEN` enables the
workflow's "Chain next run" step (re-dispatches itself on success, since `GITHUB_TOKEN` can't);
the hourly cron remains the safety net.

`mark_inactive` runs every walk. Two safety rails make the every-run flip safe (architectural
rule #3): (1) each per-category flip is gated on **walk completeness** — `_walk_complete`
compares the collected count against the API's `result_size` and skips the flip (logging
`INACTIVE skipped`) when the walk looks truncated; (2) a gone detail fetch (HTTP 404/410 or
sreality's "tato stránka neexistuje" body, surfaced as `ListingGoneError`) flips that single
listing immediately and clears any `listing_fetch_failures` row. The `--limit` guard
short-circuits `mark_inactive` for ad-hoc partial runs (a `--limit` run records
`run_type='delta'`).

The image backfill (`images.yml`, `--images-only`) is NOT a scrape run and does **not** write a
`scrape_runs` row — only index walks do — so "last scrape", the liveness check, and
reconciliation track real walks. Scrape concurrency is `cancel-in-progress: false` — a long tick
is never killed mid-walk; the next cron tick queues behind it. Per-category marking commits
immediately after each category's walk, so even a timed-out tick leaves a consistent partial
result.

## Reading the logs

The scraper emits structured progress lines:

- `INDEX offset=N estates=M total=K` per search page (offset/limit paging)
- `INDEX total=N pages=M` once at end of index walk
- `PLAN unchanged=N refetch=M` once after deciding what to fetch
- `PLAN priority_retry=N` once if any listings have prior failure rows
- `PLAN cap=N deferred=M` once if the per-run refetch cap kicks in
- `DETAIL starting refetch=N workers=W` once before the refetch loop (detail fetches run on a
  `W`-thread pool paced by a shared rate limiter; DB writes stay serial on the main thread)
- `DETAIL progress=N/M new=... updated=... gone=... errors=...` every 50 refetches
- `RATE penalize status=429|403 url=...` when the portal throttles us and the limiter widens its
  interval (auto-recovers on subsequent healthy fetches)
- `DETAIL id=... new|updated|unchanged` per refetched listing
- `IMAGE id=... inserted=N` per listing with new image rows recorded
- `DETAIL id=... gone (is_active=false)` per listing whose detail fetch reported it delisted
- `INACTIVE cm=... ct=... marked=N collected=M result_size=K` per category after a
  completeness-checked mark_inactive
- `INACTIVE skipped cm=... ct=...` per category whose walk looked truncated (flip suppressed)
- `RUN done pages=... new=... updated=... unchanged=... gone=... errors=...`
- `IMAGES pending=N cap=N workers=N` once before the image-download phase
- `IMAGES progress=N/M ...` every 50 images during the phase
- `IMAGES done downloaded=... errors=... attempted=...` after image phase

A run ending with `errors > 0` is not necessarily a failure (single-listing fetch errors are
tolerated). A run that did not emit a `RUN done` line is a real failure — check the GitHub
Actions log for a stack trace.

## What is explicitly out of scope right now

- **Authentication / user management.** Single-operator platform; one shared API token, no
  per-user identity.
- **A public read API.** The bearer-gated FastAPI service is private (the Railway URL is the
  perimeter); we don't expose a documented public API for third parties.

(ClickUp is *not* out of scope — it's a supported API consumer: ClickUp can call the FastAPI
service for a rental-price estimate, and `'clickup'` is a reserved `estimation_runs.source`
value. A free email notification channel is planned — tracked in ROADMAP.md, not here.)

Do not start anything still out of scope without explicit user direction in a new session.

## Follow-ups (deferred)

Deferred and sequenced work lives in **ROADMAP.md** (the sequencing source of truth) — consult
it rather than duplicating a list here.

## Schema conventions

- Sreality enum codes that we promote to typed columns are stored as Czech text labels without
  diacritics, mirroring the existing treatment of `category_main` / `category_type`. Source maps
  live next to the parser: `parser.CATEGORY_MAIN`, `parser.CATEGORY_TYPE`, `parser.FURNISHED`,
  `parser.OWNERSHIP`. Unknown source codes (including sreality's `0` "not specified") return
  `None`, never raise — same forgiving pattern that lets the parser tolerate sreality adding a
  new code (as it did for `category_type_cb=4` / `'podil'`).
- `has_balcony` / `has_parking` are LEGACY combined booleans. They conflate
  balcony+terrace+loggia and parking+garage respectively. The granular columns added in
  migration 022 (`terrace`, `garage`, `parking_lots`) are the correct fields for new analytical
  work. The legacy columns stay populated for backward compatibility with existing queries /
  RPCs.
