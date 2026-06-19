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
  is the steady hourly ingest; bazos.cz (HTML crawler) and bezrealitky.cz (public GraphQL
  API) are scheduled 6-hourly pilots, with more portals rolling out per
  `docs/design/multi-portal-dedup.md`.
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
`bazos_parser.py`, `bazos_main.py`) lands bazos listings into the same
`listings`/`listing_snapshots` contract, tagged `source='bazos'`. It walks 14 nationwide
scopes (byt/dum/chata/restaurace/kancelar/prostory/sklad × prodam/pronajmu), so — like
sreality/idnes (rule #19) — it is **cadence-split**: `bazos_index_walk.yml` (every 6h, full
walk + mark_inactive + enqueue) feeds the bounded `bazos_detail_drain.yml` (hourly,
`--max-seconds` budget). A combined run can't do both inside one job (~1500 index pages ≈
50 min eats the window, starving the drain); narrow ad-hoc runs go through the split
workflows' dispatch inputs (`-f sale_type=… -f category=…`, or locality + radius) or
`scraper.bazos_main` locally. **Detail-page** raw HTML is staged in `portal_raw_pages`
(migration 099) before parsing (the parsed-state ledger + reparse-without-refetch capability); INDEX/search-page
HTML is NOT staged — it was write-only dead weight (nothing reads `page_kind='index'`) and the per-page TOAST
write was the dominant cost on slow HTML index walks, so all HTML portals (bazos/idnes/mmreality/remax/maxima)
skip it. Coordinates come from the detail page's embedded Google-Maps/Mapy.cz link
(page-wide, CZ-bbox-guarded); they are what lets cross-source dedup match bazos against
sreality.

**Data source (bezrealitky.cz).** A scheduled scraper (`scraper/bezrealitky_client.py`,
`bezrealitky_parser.py`, `bezrealitky_main.py`, workflow `scrape_bezrealitky.yml` — pilot,
every 6h) tagged `source='bezrealitky'`. Bezrealitky is a JSON-API portal like sreality
(not an HTML crawler): it reads the public GraphQL API at `api.bezrealitky.cz/graphql/`
(`listAdverts` for the index — offset/limit paging, `totalCount` for completeness,
`includeImports:false` to scope to bezrealitky's OWN private-seller inventory — and
`advert(id)` for detail). The API requires browser-like `Origin`/`Referer` headers; no
cookies. `bezrealitky_parser.parse_advert` maps the advert object onto the shared
`ScrapedListing` contract, translating bezrealitky's enums into the SAME canonical label
strings sreality stores (`po_rekonstrukci`, `cihla`, `celkem`/`měsíc`, `2+kk`, …) so
cross-source filtering/dedup/condition-scoring see one vocabulary. Coordinates come from
the API's `gps` field (precise, per-listing — no geocoding step). Because the detail JSON
carries `offerType`/`estateType`, the drain derives each listing's category from the
response, so one config walks many categories (no per-category queue encoding).
`listAdverts` has a `totalCount` and no deep-pagination cap, so a per-category walk is
provable-complete: unlike bazos, bezrealitky is complete-walk capable and the runner marks
delisted listings inactive under the completeness guard (source-scoped). NOTE: bezrealitky
also has an on-demand URL parser (`scraper/source_parsers/bezrealitky.py`, LLM) used by the
estimation preview — a separate entry point that is unchanged by the scheduled scraper.

**Data source (reality.idnes.cz).** A scheduled scraper (`scraper/idnes_client.py`,
`idnes_parser.py`, `idnes_main.py` — **cadence-split** like sreality/bazos:
`idnes_index_walk.yml` every 6h feeds the hourly bounded `idnes_detail_drain.yml`) tagged
`source='idnes'`. iDNES is an HTML portal (like bazos, not a JSON API) but a STRUCTURED one:
`idnes_parser` reads the `<dl>` spec table, a clean price element, and **precise per-listing
coordinates from the page's embedded map config** (`"center":[lon,lat]`), so there is no
geocoding step. Typed fields are normalised to the SAME canonical labels sreality stores
(`panelová→panel`, `velmi dobrý stav→velmi_dobry`, `osobní→osobni`) for one cross-source
vocabulary. Search pages carry a result total and have **no deep-pagination cap**, so a
per-category walk is provable-complete: unlike bazos, idnes is **complete-walk capable**
(`supports_complete_walk=true`) and the runner marks delisted listings inactive under the
completeness guard, source-scoped (rules #3/#15). The detail URL carries the category
(`/detail/{sale}/{cat}/…`), so the drain derives each listing's category from its own URL —
one config (the `portals` row, migrations 110/111) walks many categories (byty + domy ×
prodej + pronájem today). Image-URL rows are recorded by the drain; the shared `images.yml`
job downloads the bytes to R2 (source-agnostic). NOTE: iDNES also has an on-demand URL parser
(`scraper/source_parsers/idnes_reality.py`, LLM, `source_kind='idnes_reality'`) used by the
estimation preview — a separate entry point unchanged by the scheduled scraper, which is why
the Health dashboard's iDNES card shows BOTH a scraper and an on-demand-parser badge.

**Data source (mmreality.cz).** A crawler (`scraper/mmreality_client.py`,
`mmreality_parser.py`, `mmreality_main.py`, workflow `scrape_mmreality.yml` — pilot,
**dispatch-only**: Cloudflare 403-blocks GitHub-hosted runner IPs, so the cron was removed
while every scheduled run produced zero listings; re-enable only with non-datacenter
egress) tagged `source='mmreality'`. M&M Reality is server-rendered HTML
but **every detail page embeds a COMPLETE structured estate object** as a Vue
`:property` prop (HTML-entity-encoded JSON), so `mmreality_parser.parse_detail` decodes
that JSON rather than scraping markup: precise per-listing coordinates (`point`), typed
condition/construction/ownership/energy, area, floors, and images all from one object —
no `<dl>` table, no geocoding step. Typed fields are normalised to the SAME canonical
labels sreality/idnes emit (`smíšená→smisena`, `velmi dobrý→velmi_dobry`,
`Družstevní→druzstevni`, `2+1`). The index is a SINGLE MIXED-category feed
(`/nemovitosti/?page=N`, no per-category slice); each listing's category is read from
its own detail JSON, so one config descriptor walks everything. Because a single mixed
walk can't be gated per-(category_main, category_type) the way the source-scoped
`mark_inactive` requires, mmreality is `supports_complete_walk=false` (the bazos posture,
rule #21): the runner never flips its listings inactive from index absence (rule #3) —
delistings surface via a gone detail fetch (immediate per-listing flip via
`mark_listing_inactive_native`) + the toolkit's "active = seen within 7 days" rule.
Registered as a scraper portal (migration 117, sort 35).

**Data source (remax-czech.cz).** A scheduled scraper (`scraper/remax_client.py`,
`remax_parser.py`, `remax_main.py`, workflow `scrape_remax.yml` — pilot, every 6h +
dispatch) tagged `source='remax'`. RE/MAX is a national franchise catalogue (~7,900
listings) served as STRUCTURED server-rendered HTML (no JSON API), so
`remax_parser` is deterministic: the search cards are `<div class="pl-items__item"
data-url=… data-price=… data-gps=… data-title=…>` (price, coordinates and title
straight off the card), and the detail page is a `pd-detail-info__row` →
`__label`/`__value` spec block + a clean integer `data-advert-price` + per-listing
coordinates in `data-gps` (DMS, e.g. `50°05'26.1"N,14°29'33.4"E` — parsed to
decimal, CZ-bbox-guarded, no geocoding step) + a `mlsf.remax-czech.cz/data//zs/{id}/`
gallery (the `_th350` thumbnail strips to the full-resolution original). Typed
fields are normalised to the SAME canonical labels sreality/idnes emit
(`Cihlová→cihla`, `Velmi dobrý→velmi_dobry`, `Osobní→osobni`, `2+kk`). Like maxima,
the index is TWO mixed indexes — sale (`?sale=1` prodej) and rent (`?sale=2`
pronájem), `?stranka=N` paging (21/page) — with no per-category URL; each config
descriptor pairs a category with its offer-type flag and `walk_category` walks (or
reuses, via the agenda cache) that agenda once and keeps the title-derived slice for
its category (giving the runner real (cm, ct) Health-reconciliation labels). The
drain re-derives each listing's category from the detail page ("Typ nemovitosti" +
title verb). A PILOT: `supports_complete_walk=false` (remax reports a per-AGENDA
total and the per-category slice is title-derived — not a portal-reported per-(cm,ct)
total — so a safe per-category completeness check isn't available; the runner never
flips listings inactive from index absence, rule #3); a gone detail (404/410 or a
redirect off the detail path) still flips that one listing inactive. Registered as a
scraper portal by CONVERTING the existing on-demand-parser row (migration 135). NOTE:
remax ALSO has an on-demand URL parser (`scraper/source_parsers/remax.py`, LLM,
`source_kind='remax'`) used by the estimation preview — a separate entry point
unchanged by the scheduled scraper, routed by domain in `source_dispatcher`
independent of the `portals` row's `kind`.

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
- Manifest v3 browser extension that overlays MF rent/yield + an estimate panel on portal
  listing pages. The content script matches **every scraped portal's host** (sreality,
  bazos, bezrealitky, idnes, maxima, remax, mmreality, ceskereality) — widen `matches`
  (and the registry in `src/portals.ts`) as new portals come online; `host_permissions`
  stays broad `https://*/*` for the background fetch. **Detail pages** get a floating
  panel (closed shadow root). For ANY listing we have it shows a **"Přidat do pipeline"**
  deal-pipeline bookmark toggle + an "Otevřít v aplikaci" deep-link to the SPA page
  (`{VITE_APP_BASE_URL}/listing/{sreality_id}` — the app-wide identity every SPA surface
  uses, negative for non-sreality portals) + subject facts; for sale apartments it ALSO
  shows the precomputed `mf_reference_rent_czk` + `mf_gross_yield_pct` ("Výnos MF") with
  the comparables estimation as the deeper tool/fallback (MF + estimation gated to
  byt+prodej, the bookmark + link + facts are not). The bookmark is property-grain
  (rule #22): `POST /listings/lookup` returns the listing's `property_id` + pipeline
  membership, and the toggle writes through the SAME bearer-gated
  `POST/DELETE /pipeline/cards` the SPA's `PipelineToggle` uses — one write path, one
  `<FilterIcon>` glyph everywhere. Reachable from index/search pages too: the per-card
  badge opens this same panel.
  **Index/search pages** get per-card badges via anchor-href scanning (no per-portal card
  selectors — robust to markup changes). The default display is a **read** through
  `POST /listings/lookup`, which maps a card's on-page `(source, native id)` to our row +
  MF figures + `sreality_id` (the public views don't expose `source_id_native`, so the
  browser can't resolve non-sreality listings directly). `src/portals.ts` is the single
  source of truth for host→portal + detail-URL→native-id. Two-entry Vite build (`content.js` +
  `background.js`, with `index_overlay.ts` bundled into `content.js`) plus a copied-over
  `manifest.json` and `icon-128.png`; output lands in `chrome-extension/dist/`.
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
   sacred. The `is_active=false` inference is only valid after a **~complete index walk** —
   a partial walk (`--limit N`, `--detail-only`, `--max-pages`) cannot determine which
   listings are gone. The scraper enforces this: `mark_inactive` is skipped when `--limit`
   is set, and `--detail-only` never reaches the index phase. "Complete" is ≥99.5%
   (`INDEX_MIN_COMPLETENESS = 0.995`) for the framework portals, NOT 100% — portal counts
   jitter mid-walk, and a strict 1.0 gate proved statistically unreachable for large bazos
   categories (delistings then accumulated for 11 days). The second rail: framework sweeps
   only flip rows additionally unseen for 24h+ (`min_unseen_hours` on `db.mark_inactive` /
   `mark_inactive_native`), so a tolerated walk-miss can never flip a freshly-seen listing,
   and a false flip self-heals on the next index sighting (`touch_listings` reactivates).
   Every flip stamps `listings.inactive_at` (cleared on reactivation) — the delisting-latency
   health check reads it.
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
    `first/last_seen_at`), maintained by an **async property-maintenance job**, never inline in
    the scrape (see rule #20 for the dirty-set incremental cadence). `is_active` /
    `last_seen_at` are **per-source** on the `listings` row; the property-level rollup is
    derived, not authoritative per source. `db.mark_inactive` / `db.active_count` are
    **source-scoped** to enforce this — a portal's index walk only flips its own rows.
    (Originally `mark_inactive` scoped by `(category_main, category_type)` alone, so every
    sreality walk swept bazos rows — same canon categories, never in sreality's `seen_ids` —
    to `is_active=false`; migration 109 era fixed it.) **New listings get a singleton property
    at insert time — there is no insert-time matching.** All grouping is done out-of-band by the
    **street + disposition dedup engine** (see below), so neither `scraper/db.py` nor the
    maintenance job's straggler-attach does any spatial/geo probe anymore.
    Today sreality + bazos ingest; further portals follow the design in
    `docs/design/multi-portal-dedup.md`. Frontend Browse reads `properties_public`.
    **Dedup engine (street + disposition keyed).** `toolkit/dedup_engine.py` (pure rules) +
    `scripts/dedup_engine.py` (orchestrator, `dedup_engine.yml`, daily) replaced the old
    geo-proximity matcher. Rules: **(A)** only listings with BOTH a `street` and a `disposition`
    are eligible (computed inline; a partial index backs the scan, migration 127); the rest are
    `location_unclear` / `disposition_unclear` and never matched. **Inactive listings are
    eligible too** (the scan does NOT gate on `is_active`): a property's price/lifecycle history
    is only complete if a listing taken down on one portal — or delisted then relisted under a
    new id — can still merge into the surviving group. The merge chokepoint gates on the
    *property* `status='active'` and an inactive listing keeps its own active singleton property,
    so it stays matchable; gating the scan on `is_active` would orphan that history. **(B)** same street + house
    number + disposition + floor → auto-merge, with a 5% area guard that demotes
    mismatched-area pairs to visual. **(C)** same street + disposition → visual candidate unless
    a >20%-area / house-number / **floor-gap-≥2** contradiction rejects it; nothing is ever compared
    that doesn't share street + disposition, AND no **same-development guard** fires. (Floor is a
    SOFT cross-portal signal — idnes counts the ground floor as 0 (patro), sreality as 1 (NP), so the
    same flat reads one floor apart on the two portals, and sreality is itself lister-inconsistent;
    a gap of exactly 1 is convention noise that falls through to the visual layer, only a gap of 2+
    is a hard reject. Rule B's exact auto-merge still requires floor *equality*, so an off-by-one
    never auto-merges without photo confirmation.)
    Two development guards keep near-identical units of one project from auto-merging:
    a TEXT one (rule C `unit_marker_contradiction` — the descriptions name the same
    keyword with different unit tokens: `pozemek č.3` vs `č.4`, `dům 3A` vs `5C`,
    `byt 42` vs `45`, and container/letter labels `budova/blok/vchod/sekce/etapa/objekt`
    `A` vs `B`; letter labels matched case-sensitively so the Czech conjunction "a" isn't
    one) → hard reject; and a VISUAL one (the `site_plan` image category, migration 171):
    when both listings carry a site/situation plan, `compare_listing_site_plans` checks
    whether they highlight the SAME unit — a `different_unit` verdict **queues** the pair
    for the operator (never auto-merges, never auto-rejects — the conservative choice).
    **pHash fast-path (FREE, runs FIRST — before classify, all sources).** `_phash_identical_pairs`:
    ≥2 near-identical image pairs (`PHASH_MIN_IDENTICAL_PAIRS`, any image, Hamming ≤6) → auto-merge
    with NO LLM. Runs before the cross-source gate, so identical-photo re-posts merge for free —
    including SAME-source ones the gate would otherwise drop (a price-history/recall win) — and
    cross-posted cross-source pairs skip classify AND compare. The raw any-image **count** is the
    safety bar (a development sharing one stock facade/plan gives 1 match; an actual re-post shares
    many) — validated: only 0.34% of operator-dismissed pairs reach ≥2. To preserve the site-plan
    development guard (which is post-classify), the fast-path **defers** (falls through to the visual
    stage) when both listings already carry a classified `site_plan` (`_both_have_site_plan`). This
    REPLACES the old interior-gated fast-path that needed classify first. NOTE: pHash only catches
    listings that SHARE photos — most cross-source dups have DIFFERENT photos (different portals), so
    pHash resolves a minority; the forensic compare below is still needed for the rest. (pHash
    coverage on the `images` table must keep up — `compute_image_phash.yml` — or the fast-path
    under-fires.)
    **Cross-source gate (cost):** the paid visual layer (D) runs only on CROSS-source pairs —
    same-portal pairs that pHash didn't resolve are skipped (no classify, no compare, no queue),
    since dedup's payoff is matching one portal against another (73/74 historical visual auto-merges
    were cross-source). Rule B above still auto-merges exact same-source relists for free. This
    cut ~36% of candidate pairs off the LLM stage at ~1.4% recall cost.
    **(D)** forensic visual confirmation (cross-source, the pair reached here only because pHash did
    NOT resolve it): classify both listings, run the site-plan development guard, then a room-aware
    forensic comparison (operator prompt, `app_settings.llm_visual_match_prompt`) on like rooms in
    priority order, stop at the first **High** verdict → auto-merge. **(E)**
    everything else queues on the operator's `/dedup` review page.
    **Self-healing queue (migration 198):** the engine doesn't only ADD to the review queue — each
    run it RESOLVES stale proposed candidates so they don't pile up. Recall-neutral dismissals: a
    pair the current rules now hard-reject, one the cross-source gate skips, or a candidate pointing
    to a merged-away property (`_reconcile_stale_candidates`) is auto-dismissed; the now-mergeable
    (e.g. exact-address pairs queued while the toggle was off) auto-merge. The one calibration-gated
    dismissal: a confident visual **"different"** — `decide_visual_dismiss` auto-dismisses when NO
    room reached High and a DISTINCTIVE room (kitchen/bathroom) is Low (operator toggle
    `app_settings.dedup_visual_autodismiss_enabled`, default on; `--no-autodismiss` /
    `--shadow` CLI overrides). Calibrated safe: the verdict is ~binary (High/Low), the High OR-gate
    already rescues any same-property pair with one matching room, and 0/273 operator-merged pairs
    carried a Low. Per-run counts land in `dedup_engine_runs.auto_dismissed`. The visual layer's cached
    LLM tools — `classify_listing_images` (migration 128), `compare_listings_visually`
    (migration 129), and `compare_listing_site_plans` (migration 171,
    `listing_site_plan_matches`) — are write-allowed exceptions (toolkit rule #5). A
    `dedup_engine_runs` row (migration 130) per run powers the `/dedup` automation dashboard.
    **Vision is batch-pre-warmed (cost):** `dedup_batches.yml` (migration 197 — `dedup_batches`
    / `dedup_batch_requests`) runs the engine's FREE funnel and submits the surviving cross-source
    pairs' classify/compare/site_plan vision through the Anthropic Message Batches API (50% off,
    recall-identical), writing the SAME caches the sync tools write
    (`scripts/submit_dedup_batch.py` + `ingest_dedup_batch.py`). The daily engine run then REPLAYS
    unchanged over the warm caches → identical merges for free (a cache miss falls back to a sync
    call). The lane NEVER merges; merging stays the engine's job.
    Merges are **reversible**:
    `toolkit/property_identity.py` re-points `listings.property_id` onto the survivor + soft-retires
    the loser (`properties.status='merged_away'`) and logs `property_merge_events` so
    `unmerge_group` is a deterministic replay. Because matching keys on street, a listing needs a
    parsed street to participate (sreality detail rows carry one; other portals as their parsers
    improve). Region stats also read the property grain (migration 103).
16. **Watchdog and Browse share one definition of "matches."** Saved watchdog filters live
    in `notification_subscriptions` (migration 056); the background matcher in
    `api/notifications.py` builds its WHERE clauses from the **same** logic Browse uses
    (`toolkit/comparables._shared_filter_where` + the shared `_city_quality_clauses`
    helper), so the two surfaces can never disagree on what a filter means. Dispatches are
    **property-grain** and append-only, deduped by `UNIQUE(subscription_id, property_id,
    change_kind)`, and are re-pointed onto the survivor on a property merge by the
    operator-state reconciler (rule #18, `toolkit/operator_state.py`) so they never orphan
    onto a `merged_away` property. Delivery is **in-app only today** (`channel='in_app'`
    CHECK); a free email channel is planned (extend via ALTER, not a rewrite).
17. **City-quality indexes are a normalized, operator-curated time series.** `curated_cities`
    + `city_index_revisions` + `city_index_values` + `city_index_definitions` +
    `city_population` (migration 078 onward) store per-city indexes long-form, so a new index
    on next upload needs no migration; each upload appends a `source_revision` and the latest
    is the default query target. Filtering goes through the shared `_city_quality_clauses`
    helper and the `listings_with_city_quality` RPC, and the filters are **agenda-gated to
    BROWSE + WATCHDOG only** (`toolkit/filter_registry.py`) — the estimation agent
    deliberately never sees them, preserving deterministic estimate semantics.
18. **Operator curation is PROPERTY-grain and dedup-stable** (`collections` +
    `collection_properties(collection_id, property_id)`, `tags` + `property_tags(property_id,
    tag_id)`, `property_notes(property_id, body, origin_listing_id)`, migration 202 — was
    listing-grain on `sreality_id` pre-202). A tag, collection membership, or note is a fact
    about the real-world property, not one portal's advert, so it is keyed on `property_id`
    and **follows the property across merge/unmerge/split**. `toolkit/operator_state.py`
    (`carry_operator_state_on_merge` + `OPERATOR_STATE_TABLES`, the single registry of every
    property-anchored operator-state table — collections, tags, notes, AND `notification_dispatches`)
    re-points that state onto the survivor inside the `merge_properties` transaction (SET tables
    union with collision-collapse; APPEND tables move every row), so no operator-state row can
    ever orphan onto a `merged_away` property — the invariant holds by construction. Adding a
    new property-anchored operator-state table = one registry line. Unmerge/split are deliberately
    **best-effort**: state stays on the surviving/anchor property and the reactivated/detached
    side starts clean (the operator re-curates — nothing is destroyed, it is on the survivor).
    Notes carry `origin_listing_id` as display provenance only ("written while viewing this
    advert"), never as a grouping key. The Browse tag filter resolves through
    `properties_with_tags(tag_ids)` at property grain — a property matches if ANY of its
    listings' property carries the tags, fixing the pre-202 bug where only the representative
    listing's tags were matched. Writes flow through the FastAPI service (property-grain routes
    `/collections/{id}/properties`, `/properties/{id}/tags`, `/properties/{id}/notes`); the
    browser never writes directly. Same no-hard-delete spirit as the rest of the data model.
19. **The sreality scrape is split by cadence (Phase 2): a fast index-walk feeds an async
    batched detail-drain through `listing_detail_queue` (migration 105).** `index_walk.yml`
    (`scraper.main --index-only`, `run_type='index'`) walks the full index, `touch_listings` +
    `mark_inactive` (under the completeness guard, rule #3), and **enqueues** new/price-changed
    ids with a priority. `detail_drain.yml` (`--drain-only`, `run_type='detail'`) claims a
    bounded slice (`FOR UPDATE SKIP LOCKED`), fetches, and writes **batched** via
    `db.write_detail_batch` (set-based `jsonb_to_recordset`; one transaction per ~100 listings;
    snapshot-on-change preserved via an `IS DISTINCT FROM` anti-join). The index-walk uses the
    transaction pooler; the drain uses the session pooler (`connect_session()`) for prepared
    statements. The **Tier-1 property matcher is deferred off the hot write path** — the drain
    inserts with `property_id` NULL and `recompute_property_stats`'s straggler-attach runs the
    same spatial match set-based (rule #15 still governs the grouping). `scrape.yml`'s combined
    `_run_full` is retained as the **dispatch-only revert fallback** (re-add its cron to roll
    back, no code change). The queue is the needs-detail signal; `listing_fetch_failures` stays
    the Health-visible give-up ledger. As of Phase 4 both phases run through the **shared
    `portal_runner`** (rule #21) and the queue is **source-generic** (`(source, native_id)`,
    migration 108), so this same split is how every portal scrapes — sreality is just one
    `Portal`.
20. **Property maintenance is dirty-set incremental (Phase 3), not a full-table recompute.**
    The writers that change a property's children — `write_detail_batch` (a content change →
    new snapshot), `mark_inactive` / `mark_listing_inactive` (delisting), `touch_listings`
    (re-sighting reactivation) — enqueue the affected `property_id` into `dirty_properties`
    (migration 106) with a cheap set-based `INSERT ... ON CONFLICT DO UPDATE SET marked_at`.
    `property_maintenance.yml` (`recompute_property_stats --incremental`, cron `*/5`) attaches
    new stragglers (singletons only — the old geo Tier-1 matcher was removed; grouping is the
    dedup engine's job, rule #15) and recomputes **only the queued properties** (the full
    recompute SQL scoped to
    `id = ANY(...)`), so a new/edited/delisted listing reaches `properties` + Browse within ~5
    min and the job is **O(changes)**, not O(all properties). The drain is race-free +
    terminating: it claims rows dirtied at/before a run cutoff and deletes only those untouched
    since (a mid-run re-dirty bumps `marked_at` past the cutoff → survives to the next pass).
    New listings (`property_id` NULL) are resolved by straggler-attach, not the queue. The
    **daily full sweep** (`recompute_property_stats.yml`, no `--incremental`, 04:15 UTC) is the
    reconcile backstop — it recomputes every property and clears the queue, so a missed enqueue
    self-heals within 24h. The street+disposition dedup engine (`dedup_engine.py`, daily) runs
    separately (rule #15). Both
    maintenance jobs share the `sreality-property-maintenance` concurrency group so they never
    mutate `properties` concurrently. Inline merge/unmerge still call `recompute_one` directly
    (they keep the survivor current without waiting for the cron). One accepted lag: a
    byte-identical reactivation (a delisted listing reappears with no content change) produces
    no snapshot, so it waits for the daily sweep — rare, documented.
21. **Every portal runs through ONE shared framework (Phase 4); per-portal code is a fetcher +
    a parser + a config row — no per-portal branches in shared code.** The pieces:
    `scraper/portal_base.py` (`BasePortalClient` — the shared HTTP session/headers, `RateLimiter`
    pacing + 429/403 penalize, retry/backoff, `ListingGoneError` on 404/410); `scraper/portal.py`
    (`PortalConfig` + `load_portal_config`, backed by the operational columns on the `portals`
    registry — `supports_complete_walk`, `categories`, `split_threshold` — migration 107); and
    `scraper/portal_runner.py` (the one `run_index_walk` + `run_detail_drain`, parameterized by a
    `Portal` object). sreality (`SrealityPortal` in `scraper/main.py`), bazos (`BazosPortal` in
    `scraper/bazos_main.py`), and bezrealitky (`BezrealitkyPortal` in `scraper/bezrealitky_main.py`)
    all implement the `Portal` protocol; `_run_index_walk` / `_run_detail_drain`, `bazos_main.main`,
    and `bezrealitky_main.main` are thin delegators to the runner. The **only**
    per-portal code is the fetcher (a `BasePortalClient` subclass — its `_request` does GET for
    sreality/bazos and POST for bezrealitky's GraphQL), the parser strategy, and the
    config — everything else (queue claim/complete/fail, the fetch pool, batched writes,
    completeness-gated `mark_inactive`, `scrape_runs`) is shared. A genuine per-portal need is an
    explicit method on the `Portal` protocol, justified in review — **sreality's district-split
    (the deep-pagination-cap workaround) inside its `walk_category` is the one sanctioned hook**.
    The needs-detail queue is **source-generic** (`listing_detail_queue` keyed on
    `(source, native_id)` + `detail_ref`, migration 108) so every portal shares the one queue and
    the one drain. A portal that cannot prove a near-complete walk sets
    `supports_complete_walk=false` and the runner never marks its listings inactive (rule #3) —
    bazos (partial single-category walks) is such a portal; bezrealitky is NOT (its GraphQL
    `totalCount` + uncapped paging make a per-category walk provable-complete, so it sets
    `supports_complete_walk=true` and marks delistings inactive, source-scoped).
22. **The deal pipeline is single-valued, property-grain operator state (migration 205).**
    `property_pipeline` holds at most ONE card per property (PK on `property_id`) at one
    `stage_id` (`pipeline_stages`, a TABLE not an enum so the operator can rename/reorder/add
    columns with no migration — the curated-index precedent). **A "bookmark / interested" is
    just the entry stage** (`pipeline_stages.is_entry`), not a separate flag: presence of a
    `property_pipeline` row == the property is in the pipeline. Single-valued-ness is why it
    can't live at advert grain (unlike the m2m curation of rule #18) — so it gets its OWN
    merge reconciler, `toolkit/pipeline_identity.reconcile_pipeline_on_merge`, called in the
    `merge_properties` transaction alongside the curation carry: it snapshots BOTH sides'
    pre-merge cards to the append-only `property_pipeline_events` ledger, then keeps the
    most-advanced stage on the survivor — **TERMINAL-AWARE**: a live (non-terminal) stage
    always beats a closed/terminal one, so a merge never buries a live deal under `lost`/`won`;
    within the same terminality the higher `position` wins (tie → later `updated_at`).
    `reconcile_pipeline_on_unmerge` restores the reactivated retired property's card from that
    snapshot (**lossless**: the reactivated property gets its pre-merge stage back, and in the
    move-if-empty case the survivor's absorbed card is dropped so it isn't duplicated); the
    survivor's own stage is left as-is — a chained-merge-safe best-effort, so a survivor that
    absorbed the retired's stage keeps it until the operator adjusts. Split stays best-effort
    (the card rides the anchor property). Writes go through the bearer-gated API (`POST/DELETE /pipeline/cards` to
    bookmark/un-bookmark, `PATCH /pipeline/cards/{id}` to move stage — a stage change stamps
    `entered_stage_at` and logs a `moved` event, a pure within-stage reorder logs nothing;
    `GET /pipeline/stages`). **The "Přidat do pipeline" affordance is the shared `<FilterIcon>`
    (a horizontal filter / sliders glyph, filled knobs = in-pipeline) used on EVERY pipeline
    surface — the listing-detail header (`PipelineToggle`, in the top action bar next to "New
    estimation", NOT buried in CurationBlock), every Browse card (`BookmarkButton`), the
    stage-manager's entry-stage indicator (`is_entry` — filled = the entry stage), AND the
    Chrome-extension panel (the glyph reproduced by value in vanilla TS — separate territory,
    no React import) — so the "into the pipeline" concept reads as one icon everywhere.** The
    extension bookmarks property-grain like every other surface: it reads `property_id` +
    membership off the batched `POST /listings/lookup` and writes through these same
    `POST/DELETE /pipeline/cards` routes (no extension-specific write path, no second secret). The `/pipeline` kanban board reads
    `property_pipeline_public` + `pipeline_stages_public` hydrated against `properties_public`
    (street + `mf_gross_yield_pct` from the view; one thumbnail per card via the shared
    `fetchImagesByListingIds` + `imageSrc()` Browse helpers; the **canonical broker** per card via
    two batched anon reads — `fetchListingBrokersByIds` (`listing_broker_public`) + `fetchBrokersByIds`
    (`brokers_public` contact), NOT the raw drift-prone `properties_public.broker_*` — the name links
    to `/brokers/{id}`, contact in a native-title hover). Stage moves are
    **drag-and-drop ONLY** (`@dnd-kit`, `Pipeline.tsx`: each column a `useDroppable`, each card a
    `useDraggable` with a grip handle; one optimistic move mutation; keyboard moves via the
    `KeyboardSensor`). The drag→move resolution is the pure, unit-tested `planMove(activeId,
    overId, cards)` (same column / dropped-outside / unknown card → no-op). The per-card stage
    `<select>` was **removed** (the card instead carries a trash → inline two-step confirm →
    optimistic remove-from-pipeline, the app's destructive-action pattern). `<DragOverlay
    dropAnimation={null}>` so the released card doesn't fly back to origin before the optimistic
    move lands it in the target column.
    **Stages are operator-curated from the board's "Spravovat fáze" panel** (`POST
    /pipeline/stages` create — the `key` slug is derived server-side from the label; `PATCH
    /pipeline/stages/{id}` rename/recolor/retag/crown-entry; `POST /pipeline/stages/reorder`
    rewrite left-to-right order; `DELETE /pipeline/stages/{id}` soft-archive via `archived_at`).
    Two invariants the API enforces (not just the DB): a stage can't be **both** the entry and
    terminal, and `is_entry` may only be **set** (you re-home the single-entry crown by crowning
    another stage, never by un-crowning the only one — the partial unique index needs exactly one).
    Archive is refused (409) for the entry stage or any stage still holding cards — the FK is
    `ON DELETE RESTRICT`, so cards must be moved off a stage before it retires; archived stages
    drop out of `pipeline_stages_public` but their `property_pipeline_events` history survives.
    Stage colour uses the shared **`<TagColorPicker>`** swatch grid (the one component behind the
    filter-preset save modal, the tag pickers, and this stage editor — the single colour-picking
    control app-wide; don't re-inline a swatch grid), and the entry-star / "konec" (terminal)
    controls carry `<InfoIcon>` (i) hints (native `title=`, the codebase's tooltip convention).

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
- `summarize_region_dispositions` (`toolkit/region_annotations.py`, migration 104, cache
  `region_disposition_annotations`) — a one-to-two-sentence factual annotation per
  per-disposition Kč/m² box plot in Browse > Stats, from the same `ppm2_box` payload that
  drives the chart. Cached per `(region_hash, day)` — invalidates by calendar day, not by
  snapshot. Powers the `summarize-1` annotated-charts feature; FACTS not opinions (toolkit
  rule #1) — it describes the distribution, never recommends a price.

**Vision image downscaling is unified in `toolkit/vision_images.py` — one helper, two
tiers.** Every image→LLM call routes R2 bytes through `image_block(r2, key, max_edge)`
(download → Pillow downscale → base64) rather than hand-rolling base64 per call. Two
semantic constants pick the tier: `COMPARISON_MAX_EDGE = 768` for photo comparison /
classification (`classify_listing_images`, `compare_listings_visually`,
`compare_listing_images`) — sub-megapixel is ample and, crucially, *below* Anthropic's
~1.15 MP resize cap, so it actually cuts vision tokens to ~⅓ (the cost lever); and
`DOCUMENT_MAX_EDGE = 1568` for reads where fine text/markers matter (site-plan compare,
condition scoring/markers, building-extraction listing photos) — that *is* Anthropic's own
cap, so the model sees the same pixels it would have anyway (quality-neutral; just less
upload + no 200k prompt-assembly blowups). Anthropic bills tokens on the post-resize size,
so anything ≥ the cap costs the *same* tokens — the saving only appears below it. **Operator
attachments (`read_floor_plan`, building-extraction custom attachments) are deliberately
NOT routed through this** — they carry arbitrary mime (PDF/PNG line-art) where the JPEG
re-encode would corrupt PDFs and degrade crisp text; they keep their full-fidelity base64
path. The forensic `compare_listings_visually` is the one call whose verdict auto-merges, so
its tier is gated: `scripts/validate_vision_models.py` (workflow
`validate_vision_models.yml`) A/Bs a candidate `(model, max_edge)` against every historical
`High` verdict and only a green run authorizes flipping its model to Haiku / its edge to 768.

## Secondary rent reference (MF Cenová mapa nájemného)

Every **rental** estimate carries a second, independent reference figure from the Czech
Ministry of Finance's quarterly *Cenová mapa nájemného* (a hedonic-model reference rent per
territory), shown ALONGSIDE the comparables-based primary estimate — it never overrides it.
Stored on `estimation_runs.reference_rent jsonb` (migration 131; NULL = sale run / territory
miss / no revision ingested yet). Surfaced on Estimation Detail, the Chrome-extension panel,
the `/estimations` + `/estimate_yield` API payloads, and as a Browse map choropleth layer
(VK1–VK4 selectable, optional Kraje overlay — reproduces the official MF map).

- **Source store (migration 132, history-tracked):** `rent_map_revisions` (one row per ingested
  XLSX; `file_sha256` UNIQUE so re-fetching an unchanged file no-ops) + long-form
  `rent_map_values` (per RÚIAN territory × VK1–4, standard + novostavba rent) +
  `rent_map_adjustments` (per-VK amenity Kč/m², older + novostavba tables). The `*_public` views
  are latest-revision-wins (the curated-cities pattern, rule #17). The Browse map reads the
  materialized `rent_map_choropleth` (polygons + the four VK rents, REFRESHed on each ingest) so
  the anon read is a precomputed scan under the 3 s statement timeout.
- **The join:** the spreadsheet's `Kód obce` IS the ČÚZK/RÚIAN code = `admin_boundaries.id`
  (verified: all 7,630 codes match — 1,582 `ku` + 6,048 `obec` — with zero id-space collision).
  The calc resolves the subject's lat/lng to its containing `ku`/`obec` polygon (PIP, same
  pattern as `toolkit/comparables`) and looks up the rent by that code.
- **The calc:** `toolkit.rent_map.compute_reference_rent` is **READ-ONLY — NOT a new toolkit
  write exception (rule #5)**: base reference rent (VK from the disposition's leading room count:
  1→VK1 … ≥4→VK4) + per-amenity adjustments (balkón/terasa/vybavenost/garáž/výtah, + *jiný
  konstrukční materiál* for new builds), × area. New builds (`condition='novostavba'`) use the
  novostavba reference column + novostavba adjustment table; everything else uses the older-flat
  column + older adjustments. Best-effort: any miss → NULL, never fails an estimation run. It
  reproduces the MF sheet's own worked example exactly (Litoměřice older 3+1, 68 m², +výtah
  +balkon +garáž → 291 Kč/m² → 19 788 Kč).
- **Ingest (write path, out of the read-only toolkit):** `api.rent_map.ingest_bytes` →
  `insert_revision` (parse → revision INSERT → COPY values/adjustments → REFRESH the matview).
  Refreshed two ways: the monthly `fetch_rent_map.yml` workflow (`scripts.fetch_rent_map`, scrapes
  the current XLSX off the MF *infografika* page — MF updates 4×/year) AND a manual `.xlsx` upload
  / "Fetch latest now" from the Settings page (`POST /admin/rent-map/*`). The XLSX is parsed with
  stdlib `zipfile`+`xml.etree` (no `openpyxl`). No new secrets — uses `SUPABASE_DB_URL`.
- **MF gross yield Browse filter (migration 133).** Every **sale apartment** carries a derived
  `listings.mf_gross_yield_pct` (= MF reference monthly rent × 12 / asking price × 100) +
  `mf_reference_rent_czk`, computed set-based by the `recompute_mf_gross_yields()` SQL function
  (PIP-resolve territory → rent-map join → ÷ price). NULL where not computable (non-apartment,
  rental, no territory) **and** where the asking price is implausible for a sale (`< 100 000` CZK —
  excludes "cena v RK"/placeholder + rent-magnitude prices mis-tagged `prodej`, which would
  otherwise yield absurd %; genuine high-yield deals are preserved). The function runs **hourly**
  (`recompute_mf_yields.yml` → `scripts.recompute_mf_yields`) and **after each rent-map ingest**
  (inside `scripts.fetch_rent_map`); cheap + idempotent (`is distinct from` guard). Exposed on
  `listings_public` / `properties_public` and filterable in Browse **and** Watchdog via the
  `min/max_mf_gross_yield_pct` registry filter (`_UI_AGENDAS`, float range slider) — Map/Table
  auto-dispatch `.gte/.lte` on `properties_public`, the Stats RPC `browse_stats_properties` gained
  two params, and the Watchdog matcher + `_shared_filter_where`/`ComparableFilters` carry it for
  saved alerts. Real-data distribution sanity: median ~3.5%, p99 ~10%. The same recompute pass also
  stores the full formula **breakdown** as `listings.mf_reference_rent jsonb` (migration 134: territory,
  VK, novostavba flag, `base_per_m2`, per-amenity `adjustments[]`, `total_per_m2`, area,
  `monthly_rent_czk`) — exposed on `listings_public` and rendered on the sale **listing-detail header**
  so the operator sees the exact numbers behind the stored rent/yield (always consistent — one pass
  writes all three columns).

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

The sreality pipeline is **split by cadence (Phase 2)**: `index_walk.yml` ("Scraping: Sreality
index walk", cron `*/15`) feeds `detail_drain.yml` ("Scraping: Sreality detail drain", cron
`*/15`). `scrape.yml` ("Scraping: Sreality combined walk") is the **dispatch-only fallback** —
the proven combined index+detail `_run_full`, kept for instant revert (re-add its `schedule:`
cron, disable the two new ones) and ad-hoc full walks. The bazos crawl is **cadence-split**
like sreality (bazos walks 14 nationwide scopes, ~1500 index pages — a combined run starves the
drain): `bazos_index_walk.yml` ("Scraping: Bazos index walk", cron `0 */6`, full walk +
mark_inactive + enqueue) feeds `bazos_detail_drain.yml` ("Scraping: Bazos detail drain", cron
`45 * * * *`, bounded `--max-seconds`). The bezrealitky scrape is
`scrape_bezrealitky.yml` ("Scraping: Bezrealitky scraper (pilot)", every 6h + dispatch; runs
both index walk + detail drain in one job via `bezrealitky_main`). The maxima scrape is
`scrape_maxima.yml` ("Scraping: Maxima Reality scraper (pilot)", every 6h + dispatch; the
~220-listing catalogue fits both phases in one job via `maxima_main`). The mmreality scrape is
`scrape_mmreality.yml` ("Scraping: M&M Reality scraper (pilot)", **dispatch-only** — Cloudflare
403-blocks GitHub-hosted runner IPs, so its cron was removed; runs both phases in one job via
`mmreality_main`, bounded by `--max-pages`/`--max-detail`). The remax
scrape is `scrape_remax.yml` ("Scraping: RE/MAX scraper (pilot)", every 6h + dispatch; runs both
phases in one job via `remax_main`, bounded by `--max-detail` + a `--max-seconds` budget so the
~7,900-listing backlog drains over several ticks). The idnes scrape is
**cadence-split** like sreality (iDNES is large — ~2400 index pages, ~60k listings — so a
combined run's full index starves the drain): `idnes_index_walk.yml` ("Scraping: iDNES Reality
index walk", `idnes_main --index-only`, cron `15 */6`, full complete-walk + mark_inactive +
enqueue) feeds `idnes_detail_drain.yml` ("Scraping: iDNES Reality detail drain", `--drain-only`,
hourly cron `30 * * * *`, bounded by a `--max-seconds` wall-clock budget; with
`SCRAPE_CHAIN_TOKEN` it re-dispatches itself while the queue has work, for near-continuous
backlog drains). There is no combined bazos/idnes fallback workflow anymore — sreality's
`scrape.yml` is the only retained combined fallback (its `_run_full` is the instant revert for
the split); for the other portals an ad-hoc combined run is `python -m scraper.<portal>_main`
locally. The dedup/properties track adds
`property_maintenance.yml` (**dirty-set incremental, cron `*/5`** — attaches new stragglers as
singletons + recomputes only changed properties; rule #20),
`recompute_property_stats.yml` (the **daily full-sweep reconcile** at 04:15 — recomputes every
property + clears the dirty queue), `dedup_engine.yml` (daily street+disposition dedup engine +
auto-merge; rule #15), `dedup_batches.yml` ("Dedup engine (vision batch warm-up)", submit every
6h + ingest hourly — pre-warms the engine's vision caches via the Anthropic Batches API at 50%
off so the daily engine run merges over warm cache for free; rule #15), and
`compute_image_phash.yml` (hourly pHash backfill, active-listing images first). Two monitor
workflows watch the rest: `monitor_workflow_failures.yml` ("Monitoring: workflow failures", cron
`*/30` — records failed / timed-out / startup-failed runs into `workflow_failures` so the Health
page can list them; GitHub only emails about failed *scheduled* runs) and `llm_health.yml`
("Monitoring: LLM pipeline liveness", hourly — goes red when `llm_calls` has been idle for hours
while condition-scoring work is pending, catching the silent dead-key/no-credit mode). Run any
directly:
- CLI: `gh workflow run index_walk.yml --ref <branch>` (or `detail_drain.yml`, `-f` for flags).
  Watch with `gh run list --workflow=index_walk.yml` then `gh run watch`.
- Browser: GitHub repo → **Actions** → the workflow → **Run workflow** → pick branch + optional
  flags → **Run workflow**. (All sreality scraping workflows are prefixed `Scraping:`.)

**Each scrape workflow self-declares its portal with a `# portal: <source>` tag.** A one-line
comment near the top of a portal's index/drain/combined workflow (`<source>` = the
`portals.source` key, e.g. `# portal: idnes`) is parsed by `scripts/generate_workflow_docs.py`
into `WorkflowDoc.portal`, which is what the Health dashboard's per-portal "Pipeline schedule"
panel groups on — so a new portal's cron lines surface there automatically, with **no hardcoded
frontend map to keep in sync**. Tag only the actual ingest workflows (index walk / detail drain /
combined fallback); shared, source-agnostic jobs (`images.yml`, `condition_scores.yml`,
`recompute_property_stats.yml`, `dedup_engine.yml`, …) stay **untagged** (`portal: null`) and
appear in the full Settings → Workflows list rather than any single portal's schedule. As with any
workflow edit, regenerate `frontend/src/lib/workflowDocs.generated.ts` in the same commit
(`python scripts/generate_workflow_docs.py`; CI's `--check` guards drift).

**The split (architectural rule #19).** The cheap "which ads still exist" check is decoupled
from the slow "download each ad" write:
- **`index_walk.yml` (fast, frequent).** Walks the **entire** index of every category pair (no
  `--limit`), `touch_listings` bumps `last_seen_at` on still-listed ids, `mark_inactive` flips
  delisted ones (under the completeness guard), and new + price-changed ids are **enqueued** into
  `listing_detail_queue` with a priority (failure-retry > price-changed > new). No detail fetch,
  so delistings surface within minutes. Records `run_type='index'`, `index_pages>0` (what Health
  liveness keys off). Uses the **transaction pooler** (`connect()`) — bulk set-based statements,
  no per-listing loop.
- **`detail_drain.yml` (slow, async, bounded).** Claims a bounded slice of the queue
  (`--max-detail-refetches`, the workflow passes 12000), fetches details on a rate-limited pool, and writes
  them **batched** via `db.write_detail_batch` (set-based `jsonb_to_recordset`, one transaction
  per ~100 listings, ~0.1–0.2 s/listing). Uses the **session pooler** (`connect_session()`) for
  prepared statements. New listings land with `property_id` NULL and become **singletons** via
  `recompute_property_stats`'s straggler-attach (the hot write path carries no matching at all;
  grouping is the dedup engine's job, rule #15). A gone fetch flips that listing inactive +
  dequeues it; a transient error bumps
  the queue row's `attempts` (given up after 5) and stays queued. Records `run_type='detail'`,
  `index_pages=0`. The queue persists across runs, so a bounded run never loses work; a
  SIGKILLed claim is recovered by the next run's `reclaim_stale_claims`.

`mark_inactive` runs every index walk. Two safety rails make the flip safe (architectural rule
#3): (1) each per-category flip is gated on **walk completeness** — `_walk_complete` compares the
collected count against the API's `result_size` and skips the flip (logging `INACTIVE skipped`)
when the walk looks truncated; (2) a gone detail fetch (HTTP 404/410 or sreality's "tato stránka
neexistuje" body, `ListingGoneError`) flips that single listing immediately. The drain's
failure-priority replaces the old per-walk priority retry: a failed fetch keeps its queue row at
elevated priority.

**Condition scoring** stays decoupled and is **batch-driven**: `condition_score_batches.yml`
is the scheduled steady-state driver (Anthropic Message Batches API, 50% cost) — `submit`
every 3h (`5 */3 * * *`) puts the next slice of unscored listings in a batch, `ingest` hourly
(`35 * * * *`) polls + persists; one workflow, mode chosen by `github.event.schedule`. The
synchronous `condition_scores.yml` is now a **dispatch-only fallback** (its `30 * * * *` cron
was removed) — don't schedule both, they select the same pending listings and the sync scorer
doesn't skip in-flight batch rows. The scoring model is `app_settings.llm_condition_model`
(Haiku today), so batch+Haiku ≈ 25% of the original Sonnet-sync cost. Both scrape workflows
still pass `--no-condition-scoring`. Scoring is **kraj-scoped and reuse-first** (migration 174):
the selector targets only listings whose geo-derived `region_id` is in
`app_settings.condition_scoring_enabled_region_ids` (operator-edited via the Settings page
"Hodnocení stavu — kraje" toggles; empty = paused; `region_id` NULL = parked), and
`propagate_condition_levels` copies a property's genuine score to its cross-portal siblings
(`listings.condition_levels_propagated_from` records provenance) before every submit/backfill,
so a duplicate never re-bills the LLM. `check_llm_health` mirrors the same scope.

**Images** stay decoupled across three workflows (both halves of the scrape split pass
`--no-image-downloads`; the drain's write phase only records image-URL rows — bytes land in R2
via these jobs):
- `images.yml` ("Scraping: image backlog drain (sharded)", 2-hourly) — THE deep backlog drain
  across ALL portals, horizontally **sharded into 4 parallel jobs** (each owns the
  `image_id mod 4 == shard` slice via `--image-shard k/4`), each with its own per-shard cap,
  suspicious-stop circuit-breaker, and runner IP.
- `images_fresh.yml` ("Scraping: fresh-listing image fast lane", cron `*/15` + self-chaining via
  `SCRAPE_CHAIN_TOKEN` while work remains) — drains the newest ACTIVE listings' photos first so
  a freshly-scraped card renders an image within minutes instead of waiting for the 2-hourly
  drain.
- `refresh_stale_images.yml` ("Jobs: refresh stale image URLs", every 6h) — re-enqueues active
  listings whose un-downloaded image URLs have rotated/gone stale into `listing_detail_queue`
  (low priority) so the detail drain repoints the URLs and the backfill can then store the
  bytes.

**Cadence:** `*/15` for each half, deliberately — frequent index walks surface delistings fast,
while the bounded drain keeps a steady, polite fetch volume. GitHub throttles scheduled
workflows, so effective cadence is slower; Health liveness/freshness thresholds are **per-portal
cadence-aware** (`portals.scrape_cadence_minutes`, migration 114): `scraper_health_checks` scales
liveness warn at 1.5× / fail at 3× the portal's cadence, and freshness warn at 1× / fail at 3×.
sreality's cadence (60 min, ~hourly real cadence) reproduces the original 90/180 + 60/180; the 6h
pilots (bazos/bezrealitky/idnes, cadence 360) get proportional thresholds so they aren't falsely
red between runs. Concurrency: each workflow has its own group with `cancel-in-progress: false` — a long
run is never killed mid-batch; the next tick queues behind it. Per-category mark_inactive commits
immediately after each category's walk, so even a timed-out index walk leaves a consistent
partial result.

The detail-drain writes `scrape_runs` rows too (`run_type='detail'`), but only the **index
walk** sets `index_pages>0` — so "last scrape", the liveness check, and reconciliation track
the index walk specifically, while the 24h new/updated/error counters sum across the drain's
`index_pages=0` rows too (see `scraper_health_checks()`, migration 105). The image backfill
(`--images-only`) deliberately writes NO `scrape_runs` row — recording it once polluted
liveness/reconciliation with `index_pages=0` noise.

## Reading the logs

The scheduled pipeline logs in two halves; the shared `portal_runner` emits the same line
shapes for every portal (with its own `source=`), so this reads the same for bazos/idnes/etc.

**Index walk** (`index_walk.yml` and the per-portal walks):
- `CATEGORY start cm=... ct=...` per category pair
- `INDEX offset=N estates=M total=K` per search page (offset/limit paging; sreality)
- `SPLIT cm=... ct=... result_size=N > T: walking D districts` when a sreality category exceeds
  the deep-pagination window and is walked per-district
- `PLAN unchanged=N refetch=M` per category walk (per district when split) after diffing index
  prices against the DB; `PLAN priority_retry=N` if any listings have prior failure rows
  (sreality — the other portals go straight to ENQUEUE)
- `ENQUEUE enqueued=N new=... changed=... priority=...` per category — the ids handed to the
  drain via `listing_detail_queue`
- `INACTIVE cm=... ct=... marked=N collected=M result_size=K` per category after a
  completeness-checked mark_inactive
- `INACTIVE skipped cm=... ct=...` per category whose walk looked truncated (flip suppressed)
- `RECONCILE cm=... ct=... sreality=... collected=... active=...` — portal-reported total vs
  collected vs our active DB count (drift feeds the Health page)
- `INDEX total=N pages=M enqueued=K` once at end of the walk
- `RUN done pages=N enqueued=M inactive=K errors=E`

**Detail drain** (`detail_drain.yml` and the per-portal drains):
- `DRAIN reclaimed stale claims=N` when a prior SIGKILLed run left claims behind
- `DRAIN starting source=... max_claims=... workers=W batch=B budget=Ss` once
- `DETAIL id=... gone (is_active=false)` / `DETAIL id=... error: ...` per non-ok listing
- `DRAIN flush size=N new=... updated=... unchanged=... images=...` per batched write
  (one transaction per ~100 listings)
- `DRAIN progress claimed=N new=... updated=... unchanged=... gone=... errors=... buffered=...`
  per claim chunk
- `DRAIN time budget Ss reached at claimed=N; finalizing cleanly` when `--max-seconds` stops
  the run before the job timeout
- `RATE penalize status=429|403 url=...` when the portal throttles us and the limiter widens its
  interval (auto-recovers on subsequent healthy fetches)
- `RUN done pages=0 new=... updated=... unchanged=... gone=... errors=... claimed=...`

**Image workflows** (`images.yml` / `images_fresh.yml`, `--images-only`):
- `IMAGES start cap=... workers=... active_only=... shard=... sources=...` once
- `IMAGES progress=N downloaded=... errors=... taken_down=... source_unavailable=...` every 50
- `IMAGE listing_taken_down sid=... marked=N` / `IMAGE source_unavailable id=...` per classified
  failure (an inline freshness check flips a taken-down listing inactive + bulk-marks its images)
- `IMAGES STOP suspicious ...` when the transient-failure circuit-breaker trips (exits 75; the
  next cron tick retries)
- `IMAGES done downloaded=... errors=... taken_down=... source_unavailable=... attempted=...`

The dispatch-only `scrape.yml` fallback additionally emits the legacy coupled-path lines
(`PLAN cap=N deferred=M`, `DETAIL starting refetch=N workers=W`, `DETAIL progress=N/M ...`,
`DETAIL id=... new|updated|unchanged`, `IMAGE id=... inserted=N`).

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
- The Czech admin hierarchy on a listing is **derived from `geom`, not parsed from the address**
  (migration 140). `listings.obec` / `okres` / `region` (municipality / district / kraj) are set
  by a BEFORE INSERT/UPDATE-OF-geom trigger (`listings_set_admin_geo`) that PIPs the coordinate
  into `admin_boundaries` and walks `parent_id` — so they're populated **instantly at scrape time**
  and **uniform across every source** (only ~5% of listings, foreign points, lack a CZ match). The
  trustworthy anchor is the coordinate (~95% coverage, straight from each portal's map/GPS data);
  the free-text `locality` is portal-specific display text and unreliable for grouping. The legacy
  display `district` text column is filled from okres (or obec for Prague) only when NULL, so
  sreality's richer "City - Quarter" labels are preserved. Don't re-derive hierarchy from `locality`;
  read the normalized columns.
- `listings.street` is **portal-uniform via one shared extractor, `scraper/street.py`** (migration
  122 added the column). sreality + bezrealitky read a structured street (bezrealitky also fills
  `house_number` / `zip`); the HTML portals mine it from a free-text locality (`street_from_locality`:
  first segment for idnes/remax, last for maxima) or clean a regex capture (`clean_street` for bazos).
  The ONE don't-fabricate guard (`reject_as_town`) lives here so it isn't reimplemented per portal —
  it rejects foreign coords/countries, "Town - Quarter" forms, "okres X" qualifiers, and any candidate
  equal to the row's own geo-derived obec/okres/region; a wrong street is worse than NULL (it poisons
  the dedup street-key and Browse). Stored values are bare/human-readable for display; the SEPARATE
  match-time grouping key is `toolkit.dedup_engine.street_group_keys` (don't confuse the two): a row
  dual-keys into `id:<street_id>` (sreality/bezrealitky) AND `name:<obec_id>:<_street_name_key>`. The
  NAME key is **obec-scoped** — a common name like "Žižkova" has 100+ active listings across dozens of
  towns; one nationwide group blows `MAX_GROUP_SIZE=40` and gets the whole group SKIPPED, so the
  cross-portal pairs there (HTML portals have no street_id → name group is the only place they meet a
  sreality row) were never compared. obec-scoping keeps each town's street its own small group AND
  blocks cross-town false merges (classify_pair has no geo check). `street` /
  `house_number` / `zip` are OUT of the content hash, so backfilling them never churns snapshots
  (`scripts/backfill_portal_streets.py` re-derives from already-stored data — no re-fetch). Browse
  street picks ILIKE `properties_public.place_search_text`, which is a **group-best** street
  (`coalesce(p.street, l.street)`, migration 183) denormalized onto `properties` by
  `recompute_property_stats` — so a multi-portal property matches a street even when its representative
  listing lacks one. (The old expand-normalizer `toolkit/addresses.py` that turned `ul.`→`ulice` was
  dead code and was removed.)
