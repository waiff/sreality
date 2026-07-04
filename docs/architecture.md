# Architecture — deep rationale

`CLAUDE.md` holds the hard rules (one to three lines each). This file holds the WHY —
the full as-built rationale, edge cases, and incident history behind them. Read the
relevant section here before modifying any code an architectural rule touches.

- Operational how-tos live in the on-demand skills under `.claude/skills/` — `database`,
  `toolkit-api`, `llm-pipelines`, `scraper-ops`.
- Design-time specs live in `docs/design/` (multi-portal-dedup, dedup-byt-precision,
  clip-visual-embeddings, notifications-unified, price-stats-datasets, street-coverage-ruian).
- Sequencing lives in `ROADMAP.md` + `roadmap/`.

## Data sources — per-portal narratives

How each portal is ingested: API/HTML shape, parser strategy, coordinate source,
completeness posture (`supports_complete_walk`), and quirks. The **operational** side —
which workflows run each portal, their crons, dispatch inputs, and log lines — is in the
`scraper-ops` skill; the cross-source dedup design is in `docs/design/multi-portal-dedup.md`.

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
cron `50 */6` — **every request rides the residential proxy** (`SCRAPER_PROXY_URL`,
`USE_PROXY=True`): Cloudflare hard-403s datacenter IPs (the first 101 direct scheduled runs
ingested zero listings), so the proxy is mandatory from ANY datacenter egress, GitHub or
Railway alike; the cron is offset from ceskereality's `25 */6` so the two proxied portals
don't hammer the shared proxy at the same minute) tagged `source='mmreality'`. M&M Reality is server-rendered HTML
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

**Data source (ceskereality.cz).** A scheduled scraper (`scraper/ceskereality_client.py`,
`ceskereality_parser.py`, `ceskereality_main.py`) tagged `source='ceskereality'`. It is large
(~49k listings), so — like sreality/idnes — it is **cadence-split**: `ceskereality_index_walk.yml`
(every 6h, full complete-walk + mark_inactive + enqueue) feeds the hourly bounded
`ceskereality_detail_drain.yml` (`--max-seconds` budget). ceskereality is a STRUCTURED HTML portal
like idnes: each detail page carries a `schema.org` `individualProduct` JSON-LD block (clean price +
broker), an `i-info` spec list, **precise per-listing coordinates** in `data-coord-lat/lng` (and a
Google-Maps `?q=` link) so there is **no geocoding step**, and an `img.ceskereality.cz/foto/` gallery.
Typed fields are normalised to the SAME canonical labels sreality emits (verified against the live
sreality vocabulary: `Zděná→cihla`, `Bezvadný→velmi_dobry`, `K rekonstrukci→pred_rekonstrukci`,
`soukromé→osobni`). **Street** is taken from the JSON-LD `streetAddress` when present, else mined from
the SEO detail-URL slug (`…-{street}-{id}.html`) — the broker's `offeredby.address` (the agency office)
is deliberately never used; both route through the shared `scraper/street.py` guard. **Broker** carries
a stable identity — the `/realitni-makleri/{slug}-{id}/` profile id — stored idnes-shaped in
`raw["broker"]`, so ceskereality is in `BROKER_ATTRIBUTED_SOURCES` and `resolve_brokers` has a
per-source attribution block (phone-only; no email → no firm). Per-category search pages carry a result
total ("Máme tady N…") with no deep-pagination cap, so a per-category walk is provable-complete
(`supports_complete_walk=true`; the runner marks delistings inactive under the completeness guard,
source-scoped). The detail URL carries the category, so the drain derives each listing's category from
its own URL — one config (the `portals` row, migration 249) walks all 12 (cm × offer-type) descriptors.
The client uses an honest identifying `User-Agent` at a polite rate (the site disallows generic bots in
robots.txt — an operator-owned posture). NOTE: ceskereality ALSO has an on-demand URL parser
(`scraper/source_parsers/ceskereality.py`, LLM, `source_kind='ceskereality'`) used by the estimation
preview — a separate entry point unchanged by the scheduled scraper.

## Territories — deep rationale

The three-territory summary is in `CLAUDE.md`; the full per-territory rules and rationale
follow.

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
  bazos, bezrealitky, idnes, maxima, remax, mmreality, ceskereality, realitymix) — widen
  BOTH `manifest.json` `content_scripts.matches` (so the script INJECTS there — a registry
  entry without a match is dead) AND the registry in `src/portals.ts` (host→portal +
  detail-URL→native-id) as new portals come online; `host_permissions` stays broad
  `https://*/*` for the background fetch. Match patterns are exact-host, so an apex-canonical
  portal (e.g. `realitymix.cz`) needs its apex pattern, not just `www.`. **Detail pages** get a floating
  panel (closed shadow root). For ANY listing we have it shows a **"Přidat do pipeline"**
  deal-pipeline control (bookmark; once in, change stage via a native `<select>` + remove)
  + a monitoring/collection toggle (rule #18) + **operator notes** (list existing + add a new
  one via `GET`/`POST /properties/{id}/notes`, property-grain, the viewed advert recorded as
  the note's `origin_listing_id`) + an "Otevřít v aplikaci" deep-link to the SPA page
  (`{VITE_APP_BASE_URL}/listing/{sreality_id}` — the app-wide identity every SPA surface
  uses, negative for non-sreality portals) + subject facts; for sale apartments it ALSO
  shows the precomputed `mf_reference_rent_czk` + `mf_gross_yield_pct` ("Výnos MF") with
  the comparables estimation as the deeper tool/fallback (MF + estimation gated to
  byt+prodej, the bookmark + link + facts are not). The estimation's editable **net-yield
  calculator** (rent / fond oprav+SVJ / cena / **rekonstrukce**, with the renovation joining
  the price as the acquisition-cost denominator — migration 213) mirrors the SPA's `YieldBlock`
  by value: the yield % is **computed-on-read client-side in BOTH** `computeYield` (extension)
  and `YieldBlock` (SPA) — there is no server-side yield (the scenario inputs are the single
  stored truth, `estimation_runs.scenario` + `ScenarioUpdateIn`). The two clients are separate
  build territories that can't share a runtime module, so the formula is duplicated by value
  (like `normalizeBaseUrl` / `<FunnelIcon>`): **a yield-formula change must touch both
  `computeYield` and `YieldBlock` in the same PR** (the field hints — fond/měs + the acquisition
  denominator — are mirrored too). The bookmark is property-grain
  (rule #22): `POST /listings/lookup` returns the listing's `property_id` + pipeline
  membership, and the toggle writes through the SAME bearer-gated
  `POST/DELETE /pipeline/cards` the SPA's `PipelineToggle` uses — one write path, one
  `<FunnelIcon>` glyph everywhere. Reachable from index/search pages too: the per-card
  badge opens this same panel. The panel can be **minimized** (a `−` in the header) to a
  tiny one-line bar showing only the two yield figures (MF + comparables estimate); the
  preference persists across listings via `chrome.storage.local` (`panelMinimized`, the
  "storage" permission) so it stays tucked away while browsing, and `openPanel` awaits it
  before first paint (no flash).
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

## Architectural rules — full rationale

Each entry is the full as-built text behind the one-line hard rule of the same number in
`CLAUDE.md`. **Rule numbers are stable and cited by code/tests/design-docs — never
renumber.** Navigate by area:

- **Data model & history:** #2 #3 #4 #5 #8 #9
- **Migrations & schema:** #1 (the additive-vs-destructive flow lives in the `database` skill)
- **Images & storage (R2):** #6
- **Dependencies:** #7
- **OSM mirrors:** #10 #11
- **Estimation & building runs:** #12 #13
- **Condition scoring:** #14
- **Dedup + canonical properties:** #15 (design context: `docs/design/multi-portal-dedup.md`, `dedup-byt-precision.md`, `clip-visual-embeddings.md`)
- **Notifications / city-quality / operator state / pipeline:** #16 #17 #18 #22
- **Scraper framework & cadence:** #19 #20 #21

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
    so it stays matchable; gating the scan on `is_active` would orphan that history. **(B) RETIRED
    (2026-06): exact address (street + house_number + disposition + floor) is no longer an
    auto-merge.** It was the ONLY merge path that produced false merges — 6.7% of `address_exact`
    merges were later unmerged (two DIFFERENT units at the same address+floor) vs **0%** for pHash
    (0/23.6k) and visual (0/753), because address alone is not unit-conclusive. `classify_pair` now
    returns an exact-address pair as a normal rule-C **candidate** (the `address_exact` reason is kept
    for provenance), so it flows through the pHash fast-path → forensic visual → floor-plan gate (the
    0%-reversal paths) like any street+disposition pair. The rare same-address-different-photos-no-
    matching-room pair queues for the operator instead of auto-merging. **(C)** same street + disposition → visual candidate unless
    an **area-gap** / house-number / **floor-gap-≥2** contradiction rejects it; nothing is ever compared
    that doesn't share street + disposition, AND no **same-development guard** fires. The area-gap
    reject is **unified at 10%** for every category (`MatchProfile.candidate_area_max_pct`). It is
    the "Rezidence Na Bradle" / "Budovatelů" fix — units one area-band apart (73→87 = 16%, 87→99 =
    12%; or 59/62/74 m² bridged by a NULL-floor listing) used to each slip under the old 20% gate and
    then chain-merge via transitivity once pHash matched their shared renders; the 10% reject now
    hard-stops them *before* the pHash fast-path. (Floor is a
    SOFT cross-portal signal — idnes counts the ground floor as 0 (patro), sreality as 1 (NP), so the
    same flat reads one floor apart on the two portals, and sreality is itself lister-inconsistent;
    a gap of exactly 1 is convention noise that falls through to the visual layer, only a gap of 2+
    is a hard reject. Since rule B's retirement no path
    auto-merges on address+floor alone, so an off-by-one never auto-merges without photo
    confirmation.)
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
    ≥2 near-identical image pairs (`PHASH_MIN_IDENTICAL_PAIRS`, Hamming ≤6) → auto-merge
    with NO LLM. Identical-photo re-posts (same- OR cross-source) merge for free here, skipping
    classify AND compare. The **count** is the
    safety bar (a development sharing one stock facade/plan gives 1 match; an actual re-post shares
    many) — validated: only 0.34% of operator-dismissed pairs reach ≥2. **A single near-identical
    DISTINCTIVE-room match overrides the count** (`_phash_distinctive_match` →
    `decide_phash_fastpath(count, distinctive)`): one Hamming-≤6 kitchen/bathroom pair
    (`DISTINCTIVE_ROOMS`, CLIP-tagged) is enough, since wet rooms are unit-specific not shared
    marketing (operator policy; the distinctive query only runs when the generic count fell short).
    **For byt the count excludes
    KNOWN-exterior / shared-marketing images** (`phash_excluded_tags_for` → `NON_INTERIOR_TAGS`:
    exterior_facade / balcony_terrace / garden / site_plan / floor_plan, sourced from CLIP
    `image_clip_tags`) — a development reuses the same facade/plan/render across its units, so those
    images carry no unit identity and must not feed the byt fast-path; other categories count any
    image (exterior IS a house/plot's identity). Untagged images still count, so byt recall holds for
    the not-yet-CLIP-tagged majority and tightens toward interior-only as coverage fills in (the count
    bar + the 10% area reject above are the other two rails). To preserve the site-plan development
    guard (which is post-classify), the fast-path **defers** (falls through to the visual
    stage) when both listings already carry a classified `site_plan` (`_both_have_site_plan`). NOTE: pHash only catches
    listings that SHARE photos — most cross-source dups have DIFFERENT photos (different portals), so
    pHash resolves a minority; the forensic compare below is still needed for the rest. (pHash
    coverage on the `images` table must keep up — `compute_image_phash.yml` — or the fast-path
    under-fires.)
    **Cross-source gate — REMOVED in Wave 3 (recall).** It previously ran the paid visual layer (D)
    only on CROSS-source pairs, skipping same-portal non-exact pairs (73/74 historical visual
    auto-merges were cross-source) — which cut ~36% of pairs off the LLM stage but cost ~1.4% recall
    (a same-portal relist with changed photos, or two cross-posts on one portal, were dropped). Now
    ALL rule-C candidates reach the visual stage; the forensic **High** verdict + the floor/site-plan
    gates remain the precision guards, so recall rises without false merges (pHash still auto-merges
    identical-photo same-source relists for free, above). The trade-off is more pairs at the (paid)
    visual stage — the per-lane `--compare-budget` (pay-at-decision-time, cosine-routed) bounds the cost
    (the batch warmer that used to pre-buy this is retired — see the vision cost model below).
    **(D)** forensic visual confirmation (the pair reached here only because pHash did
    NOT resolve it): classify both listings, run the site-plan development guard, then a room-aware
    forensic comparison (operator prompt, `app_settings.llm_visual_match_prompt`) on like rooms in
    priority order (`rooms_in_priority(common, category_main)` → `room_priority_for`), stop at the
    first **High** verdict → auto-merge. The compare order is **per-family** (`room_priority_for`):
    **byt** compares INTERIOR rooms only (`BYT_ROOM_PRIORITY`: kitchen, bathroom, toilet, living_room,
    bedroom, hallway) — exterior_facade / balcony_terrace / garden are dropped (a shared facade render
    can't produce the auto-merging High verdict), and the CLIP cosine tier below is interior-only too;
    **dum / komercni / ostatni** lead with the **FACADE** (`HOUSE_PRIORITY` — the building's identity)
    then interiors; **pozemek** leads with the **SITE PLAN** (`LAND_PRIORITY` — the plot's identity).
    The byt-only **distinctive single-match override** (one near-identical kitchen/bathroom pHash =
    merge, `distinctive_rooms_for`) is empty for non-byt: a facade/site-plan is development-shared, so
    they always need the ≥2-match count. These per-family orders are **operator-editable** (Stage 2):
    `default_priority_for_family` is the coded default + the valid tag set, and the Settings page's
    "Dedup comparison priority" draggable lists reorder them per family into
    `app_settings.dedup_tag_priorities` (JSON). `toolkit/dedup_priorities` loads + validates the blob
    (`normalize_priority` completes any omission from the default, so a list never silently drops a
    room); the engine threads it via `_RunContext.tag_overrides` → `rooms_in_priority`, and the batch
    warmer (`submit_dedup_batch`) loads the same overrides so both lanes order rooms identically.
    Absent / partial → the coded default, so a fresh deploy is unchanged. **(E)** everything else queues
    on the operator's `/dedup` review page.
    **Floor-plan validation gate (migration 234).** Whenever the engine WOULD merge a pair — via the
    pHash fast-path OR a visual High — `_floor_plan_gate` runs a Sonnet floor-plan check (the
    `DOCUMENT_MAX_EDGE=1568` tier; pHash conflates line-art plans and CLIP cosine can't read layout,
    so vision is the only tool). It ONLY adds conservatism: BOTH sides carry a floor plan (a CLIP tag
    **at or above `FLOOR_PLAN_MIN_CONFIDENCE = 0.50`** OR an LLM classifier room_type — the floor is
    CLIP-only because only `image_clip_tags.confidence` is numeric; the LLM `image_room_classifications`
    confidence is a coarse high/medium/low enum, left unfiltered. A low-confidence CLIP floor_plan tag
    is a likely false positive, e.g. an idnes location map mis-tagged at 0.36, and 95% of real CLIP plan
    tags score ≥ 0.52, so the floor drops the phantom-plan "one-sided" read while keeping genuine plans)
    → `compare_listing_floor_plans` (operator prompt
    `app_settings.llm_floor_plan_match_prompt`, cache `listing_floor_plan_matches`, write-allowed rule
    #5; verdict same_layout / different_layout / inconclusive / no_2d_plan (migration 260) + per-plan
    OCR in `extracted`, used plan-to-plan only never to overwrite listing data) → `different_layout`
    is the **only new auto-dismiss** (the visual model stays the sole thing that can dismiss);
    same_layout / no_2d_plan → the merge proceeds. **N×N over multiple plans (migration 243):** a listing can carry several
    floor/site plans (a multi-unit building, a multi-floor home); the one vision call sends EVERY
    labelled plan of both listings and the prompt matches the cross-product — `same_layout` if ANY
    A-plan matches ANY B-plan, `different_layout` only if NONE do (and `compare_listing_site_plans`
    the same: `same_unit` if any pair shares a unit). So a matching plan among several is never missed
    into a wrong dismiss. No schema / cost change — one call, the model reasons over all pairs; the
    payload labels each plan ("Listing A plan 2") so the rationale can cite the matching pair. The
    prompt update is `updated_by`-guarded so an operator-customised prompt is never clobbered.
    **2D-plan-aware dismiss (migration 245).** The gate was wrongly dismissing legit same-property
    pairs whose "floor plans" are 3D perspective RENDERS (a 3+1 flat misread as a "two-level duplex").
    `render_score` can't separate a 2D plan from a 3D render (its anchors are about *interiors*, so a
    drawing's score is noise — empirically a flat 0..1 spread), so the distinction is made by the
    **vision model that sees the images**: the prompt judges layout ONLY from flat 2D floor plans and
    treats 3D renders as unreliable. Migration 245 also SWEPT the cache (deleted every `different_layout`
    verdict, ~242) so the stale pre-N×N + the 3D-render misreads re-evaluate under the 2D-aware prompt.
    **Contradiction-veto + the `no_2d_plan` verdict (migration 260).** Migration 245 returned
    `inconclusive` for "no usable 2D plan (only 3D renders)" and routed it to the manual queue — which
    VETOED ~600 obvious pHash/visual merges (cross-portal re-posts whose "plans" are 3D renders) over an
    un-readable image. The gate is now a pure **contradiction veto**: the ONLY things it may do beyond
    letting the merge proceed are DISMISS on a proven `different_layout`, or QUEUE the one genuinely-human
    case. To separate them the compare gained a 4th verdict: **`no_2d_plan`** = ≥1 side has no usable 2D
    plan (only 3D renders / illegible) → a 2D compare is impossible → **merge** (trust the primary pHash/
    visual signal); **`inconclusive`** now means BOTH sides HAVE usable 2D plans but the model still can't
    decide → **queue** (operator-gated by `dedup_floor_plan_inconclusive_to_review`, default on — the
    operator's carve-out: a real both-2D ambiguity is a human call). Migration 260 rewrote the prompt to
    emit the split (`updated_by`-guarded) and SWEPT the stale `inconclusive` cache so old render-verdicts
    re-run and reclassify. The gate distinguishes **"a human must decide" (queue: both-2D inconclusive)**
    from **"validate it later" (defer)**: a both-plan pair whose Sonnet verdict isn't available this run
    (budget exhausted / cache-miss) → **`defer`** — re-try next run, never the manual queue (automatable);
    and **exactly ONE side / neither side has a plan → `merge`** (no plan-to-plan compare possible → the
    gate can't contradict, so the primary signal stands — no more one-sided queue). It applies
    to pHash + visual merges, NOT rule-B exact-address. **The floor-plan check runs autonomously on the
    SCHEDULED free run (the operator-chosen posture, Option C):** even though the free run skips the
    expensive all-rooms classify/compare, it gets the LIVE `_build_floor_plan_fn` with a bounded budget
    `app_settings.dedup_floor_plan_budget` (registry default 10000; `--floor-plan-budget` overrides for an
    ad-hoc run) — the ONE paid call on a free run, firing only on the SMALL set of would-merge both-plan
    pairs, so they auto-confirm / auto-dismiss inline instead of piling onto the manual queue. (The budget
    is the count of PAID calls — "free" is the run MODE, not the cost.) An **`inconclusive`** floor-plan
    verdict routes to manual review when `dedup_floor_plan_inconclusive_to_review` (default on); off →
    treat as `same_layout` and merge. Beyond the budget, pairs DEFER to the next run; budget 0 is a $0
    escape hatch (`_build_cache_only_floor_plan_fn` — consume only warmed verdicts, defer the rest). The
    cap is wired through the pure `_effective_vision_cap` (free → the floor-plan budget; cache-only →
    unthrottled; else → `max_vision_calls`). The cache-only fn AND the live fn resolve the model via the
    SAME `LLMClient.resolve_model("llm_floor_plan_match_model")` the batch warm-up uses, so the model-keyed
    verdict cache never silently misses. A raised `floor_plan_budget` on a `free=true` dispatch is a
    **compare-free floor-plan sweep** (floor-plan checks only, no all-rooms compare spend) — how the
    initial 379-pair backlog was cleared. The per-run `dedup_engine_runs.floor_plan_deferred` counter
    (migration 241, on the `/dedup` dashboard's stat grid) is the silent-stall guard: it should trend to
    ~0; a persistently high value means the free run's floor-plan budget is too small for the inflow. With
    the batch warmer retired (W2), the free run ALWAYS pays its floor-plan checks inline within its own
    budget (dirty 25, candidates/full via `dedup_floor_plan_budget`) rather than consuming warm verdicts —
    `dedup_batches.yml` is dispatch-only; budget 0 would defer forever, which is why the dirty lane pays 25.
    **Self-hosted CLIP tier (v2, migrations 225/226 — settings-gated, default OFF).** A free
    zero-shot CLIP model (`scraper/clip_tagger.py`, ViT-B/32, run on GitHub Actions by
    `clip_tag.yml`/`scripts/clip_tag_backfill.py`) tags every image — room/plot type into
    `image_clip_tags.logical_tag` (the same `ROOM_TYPES` space the LLM classifier emits, via a
    coherent-anchor→collapse taxonomy in `data/clip_taxonomy.json`) + a 512-d vector into
    `image_clip_embeddings` (active-listing images only; pgvector, NO ANN index — dedup does exact
    pairwise cosine). It does TWO jobs the LLM classifier can't afford at full-inventory scale: (1)
    `dedup_prefer_clip_tags` makes the engine source like-room pairing from CLIP tags for FREE
    (replacing the paid Haiku classify on the hot path) — and, decisively, it is the FIRST tagger for
    `dum`/`pozemek`/`komercni` (which had zero classified images), unblocking their visual dedup; (2)
    `dedup_clip_cosine_enabled` adds a cosine recall tier (`toolkit/clip_dedup.room_pair_cosine`) that
    routes each room's forensic compare to a model by the same-room cosine band
    (`toolkit.dedup_engine.route_by_cosine`/`CosineBands`: ≥`haiku_min`→Haiku, ≥`sonnet_min`→Sonnet,
    below→skip the LLM for that room). The cosine tier NEVER auto-merges or auto-dismisses on cosine
    alone — a too-low room is skipped (the pair still queues, protecting same-property reshoots whose
    photos differ), and the forensic **High** verdict remains the only auto-merge gate. Validated
    (PRs around the trial): pozemek 77% → plot/site family, coarse room agreement 87%, same-property
    tag consistency 86%, cosine AUC 0.80. Both knobs ship OFF; flip via `app_settings` after a
    `--shadow` merge-diff confirms merges hold. Run counters: `dedup_engine_runs.clip_classified` /
    `clip_cosine_calls` / `routed_haiku` / `routed_sonnet`. (3) **Tagging-readiness gate (2026-06,
    DEFAULT whenever CLIP is the tagger).** A pair is DEFERRED — before pHash, the floor-plan gate, or
    visual — if EITHER listing has any stored image still pending the tagger
    (`resolve_pair._clip_incomplete`: a `storage_path` image with `clip_tagged_at IS NULL`; a
    processed-but-untaggable image is terminal so it never blocks forever). Reason: an
    incompletely-tagged listing's floor-plan / room images may still be in the tag queue, so the
    floor-plan gate would mis-read a pending plan as ABSENT (the false `floor_plan_review` "one-sided"
    queue — 77% of the old review backlog) and the visual flow would under-pair rooms. The engine never
    decides on partial tag data: it DEFERS and waits — no re-queue (a pending image already has
    `clip_tagged_at IS NULL`, so `clip_tag.yml` will tag it; re-queuing would only cycle a
    terminally-undecodable image — the `_trigger_clip_tagging` call was removed for that reason). The
    **trigger half** of the same invariant: `scraper.db.mark_properties_dedup_dirty_for_images` (called
    by `clip_tag.yml` after each tag batch) enqueues a property into `dedup_dirty_properties` ONLY when
    the just-tagged listing is now FULLY tagged (`NOT EXISTS` a pending image) — NOT on a partial batch
    (the old bug that shoved a 1-of-N-tagged listing into the `--dirty` drain). So the hourly `--dirty`
    drain re-decides a pair only once BOTH sides are complete → a real two-sided floor-plan compare
    merges on MATCHING plans (the correct, transparent path). The readiness gate is **always on** when
    CLIP is the tagger (the old `dedup_clip_only` opt-in setting + its dead plumbing were REMOVED — every
    pair reaching the visual stage is fully CLIP-tagged, so the Haiku fallback is never needed).
    `clip_deferred` counts deferrals per run.
    **Render detection (migration 239).** The CLIP tagger ALSO scores an orthogonal
    render-vs-photo axis per image — `image_clip_tags.render_score` (0..1), softmax over the
    `render_anchors` / `photo_anchors` in `data/clip_taxonomy.json` (a render IS a kitchen-render,
    so it is NOT part of the room argmax). **The axis is only meaningful for ROOM/photo images** —
    its anchors are about *interiors*, so it scores a DRAWING (floor/site plan) or DOCUMENT arbitrarily
    (a flat 0..1 spread). So `render_score` is **left NULL for the plan/document logical tags**
    (`floor_plan` / `site_plan` / `property_document` — `clip_tagger._DRAWING_LOGICAL_TAGS`, kept ==
    the `plan` family; the backfill skips them; migration 246 NULLed the ~445k existing). The UI render
    badge self-hides on a NULL score, so "RENDER" no longer appears on a `půdorys`. The new
    **`property_document`** logical tag (energy certificates, contracts, spec tables) is added to the
    taxonomy + `ROOM_TYPES` + the room-classifier CHECK (migration 246). Two **`staircase_interior` /
    `staircase_exterior`** tags (migration 247) sit in a new **`common`** family — a shared building
    stairwell is the same for every unit, so like the exterior/plan families it's excluded from the byt
    unit-match signal (`NON_INTERIOR_TAGS` = exterior + common + plan). The `toilet` (WC) anchor was
    sharpened to exclude shower/bathtub so CLIP stops confusing it with `bathroom`. **Applying a taxonomy
    change to the back catalogue** is `scripts/retag_from_embeddings` + `clip_retag.yml`: it re-runs the
    zero-shot over each image's STORED embedding (no R2 download / re-inference — text-anchor dot
    products), driven by `app_settings.clip_taxonomy_retag_after` (set it to `now()` to start a campaign;
    re-tagged rows stamp `tagged_at=now()` and self-drain; once caught up the scheduled run pre-checks and
    no-ops). New / not-yet-tagged images go through `clip_tag.yml`, which loads the live taxonomy. For **byt**, an image scoring >=
    `app_settings.dedup_render_exclude_min` (registry default **0.95**; `RENDER_SCORE_EXCLUDE_MIN` is the
    code fallback) is a shared development RENDER and is dropped from the pHash
    count, the distinctive single-match override, AND the forensic room compare
    (`phash_render_exclude_for` / `_render_exclusion_predicate` / `_high_render_image_ids`) — closing
    the same-area dev-unit case area + room-type couldn't (Na Bradle's two 99 m² units share a kitchen
    render). The "vizualizace" caption is NOT used (verified absent on those units) — the IMAGE is the
    signal. Validated (`scripts/validate_render_detection.py`): Na Bradle renders 0.55-0.99 vs a bazos
    amateur-photo control 0.05-0.20. Exposed on `images_public.clip_render_score`; the listing-detail
    gallery + carousels show a Render/Foto badge with the score (`ImageRenderBadge`) so the operator
    can eyeball the detector. Untagged/not-yet-scored images are never excluded (recall holds as the
    CLIP backfill ramps). **One-shot `render_score` backfill (migration 240 + `backfill_render_score.yml`):**
    `clip_tag_backfill` SKIPS already-tagged images (`clip_tagged_at IS NOT NULL`), so every image tagged
    BEFORE the render axis shipped has `render_score` NULL — the badge stays hidden and the byt exclusion
    is inert on it. `scripts/backfill_render_score.py` re-scores the render axis from each image's STORED
    CLIP embedding (`image_clip_embeddings` — NO R2 download, NO re-inference; just the
    `Tagger.render_scores_from_emb` text-anchor dot product), so it is fast and resumable (a partial
    index on `render_score IS NULL`, migration 240, self-empties as it completes). Dispatch-only, sharded
    4× (`image_id %% 4`), `SUPABASE_DB_URL` only.
    **Self-healing queue (migration 198):** the engine doesn't only ADD to the review queue — each
    run it RESOLVES stale proposed candidates so they don't pile up. Recall-neutral dismissals: a
    pair the current rules now hard-reject, one the cross-source gate skips, or a candidate pointing
    to a merged-away property (`_reconcile_stale_candidates`) is auto-dismissed; the now-mergeable
    (e.g. exact-address pairs queued while the toggle was off) auto-merge. The one calibration-gated
    dismissal: a confident visual **"different"** — `decide_visual_dismiss` auto-dismisses when NO
    room reached High and a DISTINCTIVE room (kitchen/bathroom) is Low (operator toggle
    `app_settings.dedup_forensics_autodismiss_enabled`, default on; `--no-autodismiss` /
    `--shadow` CLI overrides). Calibrated safe: the verdict is ~binary (High/Low), the High OR-gate
    already rescues any same-property pair with one matching room, and 0/273 operator-merged pairs
    carried a Low. Per-run counts land in `dedup_engine_runs.auto_dismissed`. The visual layer's cached
    LLM tools — `classify_listing_images` (migration 128), `compare_listings_visually`
    (migration 129), and `compare_listing_site_plans` (migration 171,
    `listing_site_plan_matches`) — are write-allowed exceptions (toolkit rule #5). A
    `dedup_engine_runs` row (migration 130) per run powers the `/dedup` automation dashboard.
    **Decision feedback + auditability (migration 248).** Every decision is FULLY auditable from
    the `/dedup` Decision-history feed AND the Needs-review queue, and the operator can FLAG a
    wrong one: `dedup_decision_feedback` is a **PROPERTY-pair-keyed** ("this merge/dismissal was
    wrong" + `expected_outcome` should_merge/should_dismiss/unsure + free note) operator-state
    table — keyed on the canonical `(left_property_id < right_property_id)` pair, NOT an audit-row id
    and NOT the listing pair, so ONE flag attaches to whichever surface shows that pair and persists
    across the pair's lifecycle (a queued candidate flagged "should dismiss" stays flagged once it
    becomes a terminal decision — the merge/dismiss audit row carries the SAME two property_ids).
    **Property-grain, not the listing (sreality) pair, is deliberate:** a property's representative
    listing (`repr_listing_id`) DRIFTS when `recompute_property_stats` re-picks it, so a listing-pair
    key would silently orphan the flag off the Needs-review card after a recompute; the audit row
    SNAPSHOTS its `left/right_property_id` at decision time (immutable) and a candidate's property
    pair is stable while pending, so the property pair is the stable identity on BOTH surfaces.
    It is a labelled corpus for improving the engine; the feed filters to flagged-only. Writes via
    the bearer-gated `POST/DELETE /dedup/feedback`; anon never reads it. **Auditability is computed,
    not stored:** `toolkit/dedup_audit.build_audit_breakdown(detail)` is a PURE function turning a
    decision's stored factor `detail` into rungs (each signal — pHash / cosine / forensic verdict /
    floor-plan / address — with its measured value vs the bar it was judged on, met/unmet/info, and
    the app_settings key(s) that govern it), so it renders identically on the history feed
    (`list_pair_audit`) and the queue (`list_candidates`) and works on every historical row. The
    rungs deep-link to the exact Settings knob via `settingAnchorId` (the Settings rows carry stable
    `id="setting-<key>"` anchors + a hash-scroll/force-open). The SPECIFIC pictures a decision turned
    on are resolved at READ time by `decision_evidence` (the pHash near-identical PAIRS recomputed
    from stored phashes with the engine's category exclusions, the compared plans, or the deciding
    room) — no decision-time `detail` bloat, faithful for any old row.
    **Vision cost model — pay-at-decision-time, NO batch warmer (operator decision 2026-07-04, W2).**
    The dedup flow is **pHash → CLIP cosine → vision forensics only on the images that need it**,
    paid at decision time. The scheduled `--free` lanes buy their forensic compares LIVE, capped
    per lane by `--compare-budget` (dirty 40 / candidates 100 / full 300 PAID calls; cache hits are
    free, cosine-routed via `CosineBands`), with the floor-plan validation gate on its OWN separate
    budget (`--floor-plan-budget`; dirty 25, candidates/full via `dedup_floor_plan_budget`). Before
    W2 the `--free` lanes built `compare_fn=None`, so different-photo cross-portal apartments — the
    ones pHash can't catch — never auto-merged on any scheduled run (`auto_visual=0` for days); the
    compare budget closes that gap. The old **batch warmer** (`dedup_batches.yml`, migration 197 —
    `dedup_batches` / `dedup_batch_requests` / `scripts/submit_dedup_batch.py` +
    `ingest_dedup_batch.py`) pre-bought all-rooms classify/compare/site_plan vision through the
    Anthropic Message Batches API at 50% off; it is **retired** — an all-rooms pre-buy is
    structurally wasteful for a stop-at-first-High flow. `dedup_batch_warmer_enabled` is `false`
    (the registry default) and the workflow is **dispatch-only** (both crons removed; scripts kept
    for a one-off warm/ingest). Any warm verdicts already in the caches are still consumed for free
    by the live compare fn (a cache hit costs $0 and doesn't count against the budget). Merging stays
    the engine's job; the batch lane never merged.
    **Category compatibility** is enforced at every classify site AND the `merge_properties`
    chokepoint via the single `room_taxonomy.category_main_compatible` helper: a sale ≠ a rental
    (`category_type`), and a flat ≠ a house — **except** the ONE sanctioned cross-type **dum ↔
    komercni** (the same building listed as a house on one portal, commercial on another, is one
    real-world property — irrespective of sub-type). A cross-type pair takes the FIRST listing's
    `MatchProfile` / priority order (no special-case logic). **The geo strong-signal auto-merge gates
    on BOTH families** (`profile.geo_auto_merge_allowed and profile_for(b).geo_auto_merge_allowed`), so
    a cross-type pair never geo-auto-merges on a weak proximity signal alone (komercni isn't
    geo-auto-merge-validated) — it queues, symmetrically regardless of order, and still merges via the
    exact-address / pHash / visual paths or operator review. This is distinct from the **asset-link**
    grain (migration 224), which links genuinely *different* units in one building (a `byt` + its
    ground-floor `komercni`, a `dum` + its `pozemek`) WITHOUT collapsing them.
    **Geo path (single-dwelling: house / land / commercial), default OFF.** Apartments key on
    street + disposition; houses/land/commercial have no usable disposition, so they are matched by
    **geo-proximity** instead — but through the EXACT SAME `resolve_pair` brain (pHash → CLIP cosine
    → forensic compare → floor/site-plan gate), not a separate deterministic path. `run_engine(geo=True)`
    swaps only: the loader (`_load_geo_eligible`), the candidate FILTER (`classify_geo_pair`, keyed on
    `geo_cell_key` = obec + rounded coord + category bucket + offering; `geo_category_bucket` collapses
    dum+komercni into one cell so the cross-type co-locates), the area tolerance
    (`dedup_geo_area_max_pct`, default ±20% — wider than the street 10% because the visual flow still
    confirms), and the queue tier (`'geo'`). The geo classify maps its deterministic `auto_merge` →
    `candidate`, so a geo signal NEVER merges on its own — the free-first visual flow (with FACADE /
    SITE-PLAN priority via `room_priority_for`, rule #15 PR-1) is the sole merge gate. The geo path
    also does NOT apply the **cross-source gate** (`_RunContext.cross_source_only=False`): that gate is
    justified only where rule B auto-merges same-source exact-address relists for free (the street
    path), and geo has no rule B — so a same-portal house re-post still reaches the visual stage.
    **Geo is its OWN scheduled run** (`dedup_engine.yml` cron `0 3,9,15,21`, `--geo-only`), gated by the
    `dedup_geo_enabled` master switch. It is **NOT** bolted onto the street full-scan / candidate-drain
    anymore: doing so produced ZERO geo candidates/merges because it (a) ran AFTER the street pass on the
    shared `--max-seconds` (deadline-starved by the ~100K-eligible street scan) and (b) on the candidate
    drain inherited the street pass's APARTMENT `restrict` (`_load_geo_eligible(restrict=apartment
    candidates)` → no single-dwelling rows). The dedicated geo run is **PAID, not `--free`** (bounded by
    `--max-vision-calls`): single-dwelling cross-portal pairs have DIFFERENT photos (pHash can't), so the
    forensic FACADE compare is the only thing that resolves them — it auto-merges the confident ones and
    enqueues the ambiguous (`tier='geo'`) for review. **Geo ALWAYS enqueues its unresolved pairs** (rule
    #15 (E): a geo signal never auto-merges on proximity alone, so everything else queues) — the geo pass
    overrides `--free`'s general enqueue suppression (that suppression is a STREET optimization; geo has no
    warmer and cross-portal houses share no photos, so the queue is geo's only surfacing mechanism). So the
    scheduled PAID run auto-merges the confident ones + queues the rest, and an ad-hoc `--geo-only --free`
    still surfaces the co-located candidates (just without the auto-merge) instead of silently dropping them.
    `--geo` forces it onto any non-dirty run ad-hoc (ignores the setting); the real-time DIRTY drain never
    runs geo. The geo pass writes NO separate `dedup_engine_runs` row (the dashboard reads the latest single
    row); its decisions land in `dedup_pair_audit` + the `tier='geo'` candidate queue. (Follow-up: a geo
    candidate-drain mode; the cross-portal coord-divergence cell-miss — a same house geocoded ~270m apart on
    two portals falls in different geo cells. NB the batch warmer is retired — the whole engine now pays
    vision at decision time (pHash → cosine → bounded live forensics), so geo already matches street's cost
    model without a warmer.)
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
    helper), so the two surfaces can never disagree on what a filter means.
    `notification_dispatches` is the **unified notification event table** (migration 206 —
    physical name kept; conceptually "notifications"): one source-generic, **property-grain**,
    append-only event row per `(source_kind ∈ {watchdog, collection_monitor}, subject, change_kind)`,
    deduped by a single per-event **`dedupe_key`** (`wd:{sub}:new:{property_id}` once-ever;
    `wd:{sub}:price_drop:{snapshot_id}` **per-snapshot**, so a property that keeps dropping fires
    once per real cut — and so does the collection-monitor producer). Each row carries provenance
    (`trigger_price_czk` / `prev_price_czk` / `trigger_snapshot_id`) and producer-stamped
    `target_channels` (the delivery-layer contract, see `docs/design/notifications-unified.md`).
    Rows are re-pointed onto the survivor on a property merge by the operator-state reconciler
    (rule #18, `toolkit/operator_state.py`, collapse key `(subscription_id, collection_id,
    change_kind, trigger_snapshot_id)`, NULL-safe) so they never orphan onto a `merged_away`
    property. **Delivery and detection are SEPARATE:** in-app delivery is the event row itself
    (`channel='in_app'`); external channels (email/Telegram, Sprint N) deliver via a dedicated
    `channel_sends` ledger draining `target_channels` — NOT a `channel`-column widen. (The old
    migration-057 comment claiming a new channel was "a one-line ALTER" was **false**: migration
    096 dropped `channel` from the dedup key, so the grain could never carry a second channel —
    which is why delivery gets its own ledger.) **A SECOND producer is live (Sprint C):
    `match_monitored_collections_once` (api/notifications.py, own daily cadence
    `notifications_monitor_interval_seconds` + window `notifications_monitor_window_days`) emits
    `source_kind='collection_monitor'` dispatches for every property in a `monitoring_enabled`
    collection — `price_drop`/`price_rise` (per-snapshot), `inactive`/`reactivated` (lifecycle;
    `reactivated` reads the prior `inactive` dispatch as the durable "was dead" marker since
    `listings.inactive_at` is cleared on reactivation), and `new_source` (a sibling listing on a
    new portal). It is set-based (one `INSERT…SELECT` per kind across all monitored collections),
    collection-scoped dedupe (`cm:{collection}:{kind}:{discriminator}`), `target_channels` stamped
    from the collection's `notify_channels`. Every detector is **anchored on `monitor_since`**
    (= `greatest(collection_properties.added_at, collections.monitoring_enabled_at)`, migration 230)
    so it fires only for changes observed AFTER the operator started watching that property — a
    price drop / delisting / new source that PREDATES membership never notifies (the false-positive
    the anchor closes). `monitoring_enabled_at` is stamped by a trigger on every false→true
    monitoring transition, so the anchor is correct across all write paths.
    `broker_change` is in the `change_kind` CHECK
    (migration 209) but NOT yet emitted — `listing_broker_public` is current-state-only with no
    change signal; the kind is reserved for when one exists. The unified in-app **Notifications**
    page (`/notifications`) reads BOTH producers off one LEFT-join feed (the watchdog-only INNER
    join became a LEFT join + a `collections` join so monitor rows aren't dropped), and a red nav
    unread badge polls `GET /notifications/unread-count`; `POST /notifications/mark-all-seen`
    clears it.)
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
    browser never writes directly. **Collections carry monitoring (Sprint C, migration 211):
    `monitoring_enabled` opts a collection into change alerts (the collection-monitor producer,
    rule #16) and `notify_channels` is its delivery-channel pick (folded into the dispatch's
    `target_channels`); a protected default "monitoring" collection (`is_system=true`, can't be
    renamed or deleted) ships monitoring on. The "add to collection" affordance lives on the
    Browse card (a layers control ADJACENT to the pipeline funnel — rule #22 keeps the funnel the
    sole pipeline affordance), the listing-detail `CurationBlock`, and the Chrome-extension panel.**
    **Adding notes is reachable from the Chrome-extension panel too** — it lists the property's
    existing notes + an add box, writing through the SAME `POST /properties/{id}/notes` the
    `CurationBlock` uses (the viewed advert's `sreality_id` as `origin_listing_id`); notes are
    NOT batched into `POST /listings/lookup` (too heavy per index card) — the panel fetches them
    lazily via `GET /properties/{id}/notes` on open. Tags are the one curation surface the
    extension does not yet expose.
    Same no-hard-delete spirit as the rest of the data model.
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
    `GET /pipeline/stages`). **The "Přidat do pipeline" affordance is the shared `<FunnelIcon>`
    (a funnel with three arrows, filled body = in-pipeline) used on EVERY pipeline
    surface — the listing-detail header (`PipelineToggle`, in the top action bar next to "New
    estimation", NOT buried in CurationBlock), every Browse card (`BookmarkButton`), the
    stage-manager's entry-stage indicator (`is_entry` — filled = the entry stage), AND the
    Chrome-extension panel (the glyph reproduced by value in vanilla TS — separate territory,
    no React import) — so the "into the pipeline" concept reads as one icon everywhere.** The
    extension bookmarks AND changes stage property-grain like every other surface: it reads
    `property_id` + membership (incl. `stage_id`) off the batched `POST /listings/lookup` (and
    `GET /pipeline/stages` for the select options) and writes through these same
    `POST/DELETE /pipeline/cards` (bookmark/remove) + `PATCH /pipeline/cards/{id}` (move) routes —
    no extension-specific write path, no second secret. The `/pipeline` kanban board reads
    `property_pipeline_public` + `pipeline_stages_public` hydrated against `properties_public`
    (street + `mf_gross_yield_pct` from the view; one thumbnail per card via the shared
    `fetchImagesByListingIds` + `imageSrc()` Browse helpers; the **canonical broker** per card via
    two batched anon reads — `fetchListingBrokersByIds` (`listing_broker_public`) + `fetchBrokersByIds`
    (`brokers_public` contact), NOT the raw drift-prone `properties_public.broker_*` — the name links
    to `/brokers/{id}`, contact in a native-title hover). The board offers basic **property-type
    filtering** — multi-select `category_main` chips (Byty / Domy / Komerční / …) whose labels come
    from the SAME generated filter registry as Browse's TYPE tabs (`FILTER_REGISTRY`, never a parallel
    hardcode); only the types actually present in the pipeline get a chip, and the filter is
    client-side (the board is small). **On the kanban board** stage moves are
    **drag-and-drop ONLY** (`@dnd-kit`, `Pipeline.tsx`: each column a `useDroppable`, each card a
    `useDraggable` with a grip handle; one optimistic move mutation; keyboard moves via the
    `KeyboardSensor`). The drag→move resolution is the pure, unit-tested `planMove(activeId,
    overId, cards)` (same column / dropped-outside / unknown card → no-op). The per-card stage
    `<select>` was **removed** there (the card instead carries a trash → inline two-step confirm →
    optimistic remove-from-pipeline, the app's destructive-action pattern). `<DragOverlay
    dropAnimation={null}>` so the released card doesn't fly back to origin before the optimistic
    move lands it in the target column. **On the listing-detail header** (a record page, no board
    to drag onto) `PipelineToggle` changes the stage with a native `<select>` (the app's
    single-choice control) tinted the stage colour + a remove `✕`, and the not-yet-in-pipeline
    state is the funnel "Přidat do pipeline". The **Chrome-extension panel** mirrors this exactly
    (a native `<select>` + remove `✕` in a soft-tinted pill, vanilla TS). All three surfaces
    (kanban drag, listing-detail select, extension select) call the SAME `movePipelineCard` PATCH
    (stamps `entered_stage_at`, logs the `moved` event) with the same optimistic-update shape — one
    audited write, never a second-grade path. `PipelineCard` (`property_pipeline_public`) exposes
    `stage_id` for the select's value.
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


## Cross-reference map

| Topic | Operational how-to |
| --- | --- |
| Database, migrations, schema, connection modes, Supabase MCP | `.claude/skills/database` |
| Toolkit tools, FastAPI, auth, versioned trace, env-vars & secrets | `.claude/skills/toolkit-api` |
| LLM URL parsing, cached analysis tools, vision tiers, MF rent map | `.claude/skills/llm-pipelines` |
| Running/debugging scrapers, adding a field, fixtures, reading logs | `.claude/skills/scraper-ops` |
| Roadmap / sequencing | `ROADMAP.md` + `roadmap/<track>.md` |

**Two "skills" namespaces (don't conflate):** repo-root `skills/` holds **agent** skills
seeded into the `skills` DB table (architectural rule #10). `.claude/skills/` holds the
Claude Code reference skills above — never seed those into the DB table.
