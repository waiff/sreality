# Architecture — deep rationale

`CLAUDE.md` holds the hard rules (one to three lines each). This file holds the WHY —
the full as-built rationale, edge cases, and incident history behind them. Read the
relevant section here before modifying any code an architectural rule touches.

- Operational how-tos live in the on-demand skills under `.claude/skills/` — `database`,
  `toolkit-api`, `llm-pipelines`, `scraper-ops`.
- Design-time specs live in `docs/design/` (new-dedup/PROGRAM + CUTOFF, notifications-unified,
  price-stats-datasets, street-coverage-ruian, realtime-scrapers).
- Sequencing lives in `ROADMAP.md` + `roadmap/`.

## Data sources — per-portal narratives

How each portal is ingested: API/HTML shape, parser strategy, coordinate source,
completeness posture (`supports_complete_walk`), and quirks. The **operational** side —
which workflows run each portal, their crons, dispatch inputs, and log lines — is in the
`scraper-ops` skill; cross-source grouping is rule #15 (and, for the rebuild in progress,
`docs/design/new-dedup/PROGRAM.md`).

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
staging was removed repo-wide in June 2026 (per-page TOAST writes dominated slow HTML walks) and is
selectively RE-ENABLED (location-data W0 item 0n) for the three portals whose index pages carry
signals no other surface has — sreality (geohash, POI distances, `locality.geometry`; raw JSON body),
remax (`data-display-address`, the only house-number-bearing remax surface), ceskereality (map
markers). Index keys are **week-stamped** (`db.index_archive_week`) so the archive ACCUMULATES for
delisted listings — a position-only key would be a rolling snapshot preserving nothing — and the
write cost is held down twice: a client-side fresh-key skip set (`db.fresh_index_page_keys`,
preloaded per walk, so multi-MB bodies aren't even uploaded when fresh) plus the server-side
`refresh_after_hours` guard on `upsert_portal_raw_page` (max one refresh per key per
`db.INDEX_ARCHIVE_REFRESH_HOURS` as the racing-writer backstop). remax's page-capped realtime
probe never archives (a transient probe fetch must not claim a page's daily slot); the slow
portals with no index-only signals (bazos/idnes/mmreality/maxima) still skip index staging.
The W2a payload store supersedes this scheme, and the path is BUILT (location-data W2a-2):
`upsert_portal_raw_page` dual-writes every body it stages into `portal_raw_payloads` via
`location_data.payloads.append_payload` — one chokepoint edit covering all seven HTML detail
writers and all three index archivers with no per-portal branch (rule #21) — plus a call site
each in `scraper/main.py` (`_record_detail_fetch`: the unwrapped, untrimmed estate JSON) and
`scraper/bezrealitky_main.py` (the advert **plus the exact GraphQL query text and its sha256**,
design 02 §2.3.2 P3), the two portals that stage no body. That store is content-addressed on
the NORMALISED body, so a refetch that changed nothing appends nothing and a replayed drain
batch collides instead of duplicating; retention (version cap + pins) runs in the append's own
transaction. **The BODY lives in R2 and Postgres holds the metadata row** (identity, both
hashes, sizes, version, pin state, the content-addressed key): W2a-7 measured one body per
listing-ever at 9.56 GB against a ~4 GB Postgres allowance — the location subsystem's 20 GB
envelope less the ~16 GB the RÚIAN mirror and claim spine already occupy — so no retention
setting made a database-resident archive fit, while a metadata row is 713 B and object storage
is ~1/100th the price. Everything above Postgres's own TOAST threshold
(`LOCATION_PAYLOAD_R2_THRESHOLD_BYTES`, 2 KB) spills. This is not a latency trade: nothing on a
user-facing path reads a body — the readers (W2 re-mine, backfills, the round-trip verifier)
are all batch — and Postgres-resident bodies would tax the shared buffer cache this platform
has been burned by twice. A hot-window hybrid was rejected because "processed" is undefinable
when the archive exists to be re-mined by extractors not yet written. An UNCONFIGURED bucket
REFUSES the payload write rather than falling back inline (which would rebuild the
database-resident archive invisibly); the refusal is caught by `append_payload_if_enabled`, so
the walk and the drain are unaffected, and the upload runs inside the write transaction so a
failed PUT rolls the row back. A surface whose page weight is not in
`location_data.payload_budget.PORTAL_STORAGE` is refused outright — archiving an uncosted
surface would silently invalidate the storage ceiling the operator signed. It is gated per portal by `PortalLimits.payload_dual_write` (baked default
**False**, overridable via `app_settings.scraper_limits_global` / `portals.operational_limits`
— no migration), cached ~60 s per source, and every failure warns rather than touching the
scrape. **Every `page_kind` except `detail`** passes a SECOND gate on top of it,
`PortalLimits.payload_index_archive` (W2a-6, same precedence, also baked **False**). The split is
by GRAIN: a `detail` body is one listing fetched when that listing is enqueued, while index, map,
gazetteer, snapshot and archive bodies are whole-SURFACE artefacts refetched on the walk cadence
(sreality's index 24×/day; ceskereality's `/mapa/` and bezrealitky's `Region.boundaryGeoJson`
already declare `archive: true`), which is the churn 02 §2.3.2 P2 gates — so a gate naming only
`'index'` would have let map and gazetteer bodies archive on every walk. It only ever narrows what
`payload_dual_write` allows. **OFF everywhere until the operator signs off the churn measurement
+ storage projection** (02 §2.3.2's gate; the numbers come from
`scripts/location_payload_churn_report.py`). All three index archivers are additionally **gated
by their own client-side freshness skip**, which returns before `upsert_portal_raw_page` and so
suppresses the dual-write for an index page that genuinely changed inside the 22 h window — a
KNOWN GAP commented at each call site, measured by `scripts/location_index_archive_audit.py`
(W2a-6) and left for P2 to fix.
**`portal_raw_pages` is preservation substrate, never pruned** (location-data
program, W0 item 0o): migration 099's "safe to delete once parsed" header is superseded —
the archive is the only surviving copy of delisted pages' location signal (portals don't
serve delisted pages again), an off-database copy lives in R2 under
`backups/portal-raw-pages/` (`scripts/export_portal_raw_pages_archive.py`, workflow
`export_raw_pages_archive.yml`), and `tests/test_portal_raw_pages_guard.py` fails CI on
any `DELETE`/`TRUNCATE`/`DROP` against the table. Coordinates come from the detail page's embedded Google-Maps/Mapy.cz link
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
vocabulary. Search pages carry a result total and have **no deep-pagination cap** — page 1,052
of `prodej/byty` serves the declared tail exactly and 1,060 404s — so the catalogue is fully
*reachable*. It has not been fully *reached*: **`supports_complete_walk` was parked to `false`
in migration 453** because a portal cannot prove it saw everything if we have not. We hold
109,908 active idnes rows (more than sreality) and 64% of them had gone unseen for over a week
while the flag still authorised delisting; eleven of the last fourteen walks were killed by the
job clock, and the one that finished covered 2 of 10 categories and 13% of the biggest.
**The cause is a silent soft-throttle on our datacenter egress**, not portal size: pages arrive
in ~2.3s each for exactly 20 requests, then one stalls for ~390 SECONDS and returns 200 — no
429, no error, no retry, so every rail we own stays quiet. Twenty-four such stalls consumed 143
of one 160-minute run. A residential IP shows no stall over 26 consecutive requests (0.62
pages/s vs 0.047), so idnes now sets `USE_PROXY` like the two Cloudflare portals — but with
`PROXY_REQUIRED = False`, because idnes only *degrades* without the proxy where they hard-403,
and skipping a slow portal trades degraded data for none.
**The walk is SLICED, and the slices are REMEMBERED.** Each category is walked as the 14
`CZ_KRAJ_SLUGS` plus the abroad bucket (`?s-l=STAT-XX` — a query parameter, not a path segment;
every `/zahranici/` spelling 404s). That last slice is not a nicety: the kraj slices sum to
15,319 of the 27,372 flats for sale, so a slice set built from the region nav alone would report
56% of the portal as 100% of it. Proven a true row-level partition by ID enumeration (755 rows,
14 slices, zero overlap and zero gap), and `kraj_sum + abroad` equals the national declared total
on all 10 categories. Slicing here is **not** a workaround for a pagination cap — idnes has none
— it is what makes coverage *provable* (15 declared totals to check instead of one) and
*resumable*: every slice's outcome lands in `portal_index_slices` (migration 454), and both the
category order and the slice order are **least-recently-walked first**, with a never-walked slice
sorting ahead of everything. That is the fix for the real defect, which was amnesia rather than
speed — a walk that runs out of budget used to restart at the first category's first page, so the
same head was re-walked while 8 of 10 categories were never touched at all. A category reads
complete only when **every** slice was walked *and* returned `exhausted` *and* the union satisfies
the national declared total; one `deadline`, `error`, `degraded` or `ceiling` holds the whole
category open. An empty slice publishes no count, so `total` is `None` — identical to a degraded
page — and is only accepted when idnes *says* it is empty ("momentálně tu není žádný inzerát",
`IndexPage.empty_confirmed`); ceskereality, which publishes no such string, has to confirm a zero
by reading the page twice instead.
**A slice that paging cannot finish DESCENDS, and the parent walk is kept.** idnes's result
ordering is not stable between requests, so pages of one query overlap and the loss compounds with
page count: `stredocesky-kraj` (67 pages) returned its declared 1,675 exactly, `praha` (154 pages)
returned 2,948 of 3,839 — 27% of slots were repeats. Two axes, tried in order: **place** (the
site's own hierarchy — a kraj links its okresy, Prague its ten obvody, abroad one `s-l` per
country, and those 38 countries sum *exactly* to the abroad total), then **price bands** for a
place with no sub-places at all (Spain: 8,613 flats, 345 pages, no regions advertised). Neither
axis is a partition — 60 Prague listings are too vaguely addressed for any obvod, and 6 Spanish
ones have no price — which is precisely why the parent's own rows are merged with its children's
rather than replaced: the unfiltered walk is what holds each axis's remainder. Measured:
parent alone 76.8%, children alone 98.4%, **union 99.74%**; verified through the real code path at
99.61%. The child list is scraped rather than declared, which is safe only because the arithmetic
checks it — a missing child leaves the union short, and a spurious one can only add rows of the
same category. Descent runs only on `incomplete` (paged to the end and came up short), never on an
`error` or a `degraded` page, which would relabel a fetch problem as a coverage one. The page-capped realtime probe keeps the flat national walk,
since slicing would scatter the newest-first head it exists to read.
**Un-parking is a scheduled decision, not a human one** (`coverage_gate.yml` → `scripts/
coverage_gate.py`, cron `15 3,9,15,21`, three hours after each walk cycle). Both parked portals
were parked for the same reason — the flag was a standing claim someone typed once, and the walks
stopped matching it — so flipping it back by hand would recreate exactly that. The gate instead
re-asks two questions from data every cycle: **covered** (every slice of every *declared* category
finished inside 30h — the declared count is the denominator, or a portal could pass by walking a
subset perfectly, which is precisely idnes's failure) and **stable** (that held on three
consecutive evaluations with the delist-candidate count steady between them — a walk that reaches
every slice but enumerates a different population each time is sampling, not covering, and that
difference is invisible in a coverage percentage). Both pass → `supports_complete_walk` returns to
true; either fails → it stays down. Every evaluation is appended to `portal_coverage_gate`
(migration 455), holds included. **It is safe unattended not because the gate is certain to be
right, but because a wrong verdict cannot execute**: un-parking only makes a sweep *eligible*, and
the flip cap (rule #3's third rail) still refuses anything over 10% of a category, latches and
alarms — idnes's backlog is ~37% of its rows, so the one failure this gate could cause is exactly
the one the layer beneath it is built to catch.
The detail URL carries the category
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
`ceskereality_detail_drain.yml` (`--max-seconds` budget). The index walk partitions each
category on the **14 declared kraj slugs** (`KRAJ_SLUGS`, a proven row-level partition —
never the rendered facet block, which is a top-10-by-popularity list), pages each slice to
its own declared tail (a *filtered* search URL caps at 99 pages / 1,980 rows; the famous
12-page cap belongs only to UNFILTERED category URLs), and descends onto a declared subtype
axis for the one kraj slice that exceeds that ceiling. A 200 carrying zero cards is the
portal's real degraded response and is only ever read as a finished slice when the page's
H1 proves it is the empty slice we asked for. ceskereality is a STRUCTURED HTML portal
like idnes: each detail page carries a `schema.org` `individualProduct` JSON-LD block (clean price +
broker), an `i-info` spec list, **precise per-listing coordinates** in `data-coord-lat/lng` (and a
Google-Maps `?q=` link) so there is **no geocoding step**, and an `img.ceskereality.cz/foto/` gallery.
Typed fields are normalised to the SAME canonical labels sreality emits (verified against the live
sreality vocabulary: `Zděná→cihla`, `Bezvadný→velmi_dobry`, `K rekonstrukci→pred_rekonstrukci`,
`soukromé→osobni`). **Street** is taken from the JSON-LD `streetAddress` when present, else mined from
the SEO detail-URL slug (`…-{street}-{id}.html`) — the broker's `offeredby.address` (the agency office)
is deliberately never used; both route through the shared `scraper/street.py` guard. **Broker** carries
a stable identity — the `/realitni-makleri/{slug}-{id}/` profile id — stored idnes-shaped in
`raw["broker"]`, so ceskereality is in `BROKER_ATTRIBUTED_SOURCES` and has a `toolkit/broker_sources.py`
registry row (phone-only; no email → no firm). Per-category search pages carry a result
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
- **Every PostgREST read is one of three shapes** (the 2026-08 cap-drift audit): a
  *keyed* read (`.eq` on a key, cardinality bounded by the domain — snapshots per
  listing, images per listing); an *exhaustive* read whose whole meaning is "the
  complete set" (membership maps, prefilter id-lists, choropleths, curated
  registries) — these MUST go through `frontend/src/lib/fetchAllRows.ts`
  (complete-or-throw paging, correct under any `db-max-rows`; ESLint bans `.range()`
  everywhere else); or a *bounded* read with an explicit `.limit()` and, where "more
  exists" matters, a communicated flag (the `MAP_CAP` + `capped` pattern).
  PostgREST's server clamp is itself VERSIONED config — migration 394 pins
  `pgrst.db_max_rows = 50000` (= `MAP_CAP`) on the `authenticator` role, after the
  unversioned dashboard value shipped two silent-truncation bugs at 1,000 and was then
  lifted out-of-band; keep the dashboard "Max Rows" field agreeing with the migration.
  **A bounded read needs an `ORDER BY` to be a contract, and W6b (migration 439) is why
  that is a rule and not a preference.** The Browse map's `.limit(MAP_CAP)` had none, so
  "the first 50,000" meant "the 50,000 southernmost", and 52 percent of the default
  cohort — everything north of ~lat 50.025 — was silently absent: *a `LIMIT` without an
  `ORDER BY` is a sample, and a sample chosen by the planner is not a contract*
  (Corollary F). The fix was not a bigger cap but a fourth shape, an *aggregate* read
  (`browse_map_cells`): the server answers with a result whose SIZE IS BOUNDED BY
  CONSTRUCTION — a 20 x 13 integer-division grid over `properties_map_mv`, no `LIMIT`
  anywhere — and reports the cohort's exact total alongside it. Reach for that shape
  whenever a surface renders a summary of many rows rather than the rows themselves;
  reach for `.limit()` + `capped` only when the rows themselves are the point AND the
  read is ordered. Two lanes still read the map unbounded on purpose: the portal mirror
  (`listing_feed_public` has no matview twin) and the `?map=legacy` bisect hatch.
- **All code-splitting goes through `frontend/src/lib/lazyChunk.ts`**, never React's bare
  `lazy` (ESLint bans it outside that file — the SPA's second such chokepoint after
  `fetchAllRows`). Every deploy rotates every hashed chunk filename (measured: 30 of 30
  on a one-character source change, because each lazy chunk hard-references the entry
  chunk and Rollup's hash cascade reaches all of them), and `Caddyfile`'s
  `handle_path /assets/*` has no SPA fallback, so any tab open across a deploy hard-404s
  the next chunk it loads. That is normal for a hashed-asset SPA; what was NOT normal was
  the old recovery path. `main.tsx` used to listen for `vite:preloadError` and call
  `event.preventDefault()` — Vite's "I handled it" signal, after which its helper
  (`baseModule().catch(handlePreloadError)`) makes the `import()` RESOLVE to `undefined`.
  React's `lazy` initializer then read `.default` off `undefined` and threw a TypeError
  into the route boundary: a full-page crash screen held in front of the reload that
  handler had itself scheduled (the 2026-08-19 pipeline→listing incident). A window-level
  listener structurally cannot do better — it holds no reference to the pending import,
  so it cannot keep React suspended. `lazyChunk` owns the failure where the import lives:
  a rejected load returns a **never-settling promise** so React holds the `Suspense`
  fallback with nothing to dereference, then reloads. Rails: a 60-second sessionStorage
  rate limit (not a one-shot flag — the old one was cleared by a `load` listener on the
  very reload it triggered, so it bounded nothing across documents), `navigator.onLine`
  treated as its own case (reloading an offline tab replaces a working app with the
  browser's offline page), and every storage access wrapped so a throwing
  `sessionStorage` degrades to "allow the reload" rather than to "no recovery".
- **Generated data the SPA only displays is FETCHED, not bundled.**
  `scripts/generate_workflow_docs.py` emits `frontend/public/workflow-docs.json` (~180 KB)
  and the two admin pages that render it read it through `frontend/src/lib/workflowDocs.ts`.
  It used to be a `.ts` module under `src/lib/`, which put every workflow-YAML edit — a
  pure-backend concern — inside the SPA's module graph, rotating every chunk hash and
  breaking every open tab. Measured after the move: a workflow-docs change rotates 0 of 35
  chunks (it was ~30 of 30). Any future generated blob with the same shape (large, changed
  by backend work, read by one or two pages) belongs in `public/` for the same reason.
  Note the reader MUST check the response content type, not just `res.ok`: Caddy's SPA
  fallback answers an unmatched path with `index.html` and HTTP 200, so a wrong path
  returns HTML rather than a 404. `filterRegistry.generated.ts` deliberately stays a
  module — it is consumed synchronously at import time by the filter/query core.
- **Version skew is offered, never imposed** (`frontend/src/lib/buildSkew.ts` +
  `useBuildSkew`, mounted once in `Shell`). On tab focus, throttled to once per five
  minutes, the app compares its own entry-script URL — read straight out of the DOM, since
  Vite rewrites `<script src="/src/main.tsx">` to the hashed `/assets/index-<hash>.js`, so
  build identity needs no `define:` block, no Dockerfile ARG and no version.json that could
  drift — against the one a freshly fetched `index.html` names. A difference means a newer
  build is deployed, and the app shows a sticky toast with a Reload button. It never
  navigates on the user's behalf: an involuntary reload costs unsaved filter state or a
  half-typed note, and the failure it would pre-empt (a chunk 404) is already handled
  invisibly by `lazyChunk`. Every uncertainty — failed probe, non-OK response, unhashed dev
  entry — reads as "no news", because a false positive here is a toast telling the operator
  to reload a tab that is already current.
- **`UserFacingError` (`frontend/src/lib/errors.ts`) marks the errors a person should
  read.** `ErrorBoundary` renders its `userMessage` + `recovery` as the headline and
  folds the technical text (the `cause`, when there is one) under a collapsed "Technical
  details". Everything else keeps the generic crash wording. Raw diagnostic strings in
  front of the operator are how a TypeError came to read like data corruption.
- **No write path from the browser.** Any UI action that needs a write goes through the
  bearer-token-gated FastAPI service, not direct Postgres. The toolkit's write-allowed
  exceptions (see Toolkit rule #5) are reachable only via the API.
- **Two auth shapes to the FastAPI service** (`frontend/src/lib/api.ts`), matching the
  backend gate each route actually uses. `require_admin`/`verify_jwt`/`tenant_conn` routes
  (Settings, Outreach, broker-review, skill-refinements, Collections
  list, Pipeline, Watchdog subscriptions, `/estimations` create/read) get `jwt: true` on
  their `request()` call and receive the caller's real Supabase session `access_token`
  (`supabase.auth.getSession()`) — `api/dependencies.py:verify_jwt` no longer accepts
  anything else there (the legacy static-token branch that used to grant a synthetic
  `is_admin: True` identity was removed 2026-08-04; see
  `docs/design/api-token-rotation-and-spa-jwt-migration.md`). Routes still gated by the
  simpler `require_token` (a shared-secret check, no identity) keep sending the static
  `VITE_API_TOKEN` — extractable from the bundle via devtools by design, since that gate
  only proves "past the password gate," never an admin or per-account claim. Adding a new
  `require_admin`/`verify_jwt` route means adding `jwt: true` to its frontend call in the
  same change, or it 401s.
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
  bazos, bezrealitky, idnes, maxima, remax, mmreality, ceskereality, realitymix). `src/portals.ts`
  (host→portal + detail-URL→native-id) is the single source of truth for the host list —
  `manifest.json`'s checked-in `content_scripts.matches` is a template only; `vite.config.ts`'s
  `closeBundle` hook overwrites it at build time from `PORTALS[].hosts` (same pattern already
  used there for `name` + `host_permissions`), so onboarding a new portal only means adding it
  to `src/portals.ts` — no second hand-maintained match list to keep in sync. `host_permissions`
  is narrowed at build time to just the two live API/auth origins the background worker fetches
  (`VITE_API_BASE_URL` + `VITE_SUPABASE_URL`), not a broad wildcard. Several portals (sreality's
  Next.js frontend confirmed live) navigate between listings via client-side routing (History
  API) rather than a full page load, which MV3's manifest-declared content script does NOT
  re-inject for. `background.ts` listens for `chrome.webNavigation.onHistoryStateUpdated`
  (`webNavigation` permission, filtered to the same `PORTALS` host list) and relays the new URL
  to the tab's already-injected content script (`route_changed` message), which re-runs its
  page-type decision (`renderForUrl` in `content.ts`) without needing a real reload — this
  fixed the "panel only appears after F5" bug. `renderForUrl` keys on the **listing identity**
  (`source:sourceId`), not the raw href, so a gallery/tracking query-param rewrite doesn't tear
  down the panel and discard the operator's note draft + calculator edits. Because the panel's
  state is a single module global that `openPanel` replaces wholesale, **every apply that resumes
  after an `await` is epoch-guarded** (`renderEpoch` / `setStateIf`) — without it, listing A's
  lookup or its ~6-minute estimation poll paints into listing B's panel. That epoch ("is this
  still the same panel instance?") sits alongside the older property-id guards
  (`applyMembershipIf` / `loadNotes`, "is the panel still showing this property?"), which survive
  a re-open of the same listing. Index-card badges mark the card with the **listing id** they were
  drawn for (not a boolean), so a card DOM node recycled by the portal's router is re-badged
  rather than left showing the previous listing's yield.
  **Distribution + auto-update:** an unpacked install has no update channel at all, so the
  everyday install belongs on the Chrome Web Store (Unlisted pre-launch → a visibility flip to go
  public, same ID + update channel). Chrome only auto-updates on a strictly-greater version, so
  `vite.config.ts` stamps the patch component from `GITHUB_RUN_NUMBER` (monotonic, never resets);
  the committed `MAJOR.MINOR` is the hand-owned release marker. Publishing to the store reassigns
  the extension ID, which is baked into the Supabase redirect allowlist, the Google OAuth client,
  and `CORS_ALLOW_ORIGINS` — add the new ID alongside the old one before cutover
  (`chrome-extension/README.md`, "Keeping it up to date"). Match patterns are exact-host, so an apex-canonical
  portal (e.g. `realitymix.cz`) needs its apex pattern, not just `www.`. **Detail pages** get a floating
  panel (closed shadow root). For ANY listing we have it shows a **"Přidat do pipeline"**
  deal-pipeline control (bookmark; once in, change stage via a native `<select>`, and remove
  behind the panel's two-step confirm — rule #22: no surface removes a card on one click)
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
- **Own Supabase session (Wave 1, shipped 2026-07-21) — no bearer token in the bundle
  anymore.** Hand-rolled PKCE (`src/auth.ts`) against GoTrue via
  `chrome.identity.launchWebAuthFlow` (no `supabase-js`): `VITE_SUPABASE_URL` +
  `VITE_SUPABASE_ANON_KEY` are the build-time vars (mirroring the SPA's, both are public
  client config — the anon key is not a secret). The old `VITE_API_TOKEN` / `EXT_API_TOKEN`
  static bearer is retired for the extension; `verify_jwt`'s legacy-token branch
  (`api/dependencies.py`) has since been removed entirely (2026-08-04) — see the Frontend
  territory entry below and `docs/design/api-token-rotation-and-spa-jwt-migration.md`.
  `manifest.json` pins a stable extension ID via a generated RSA keypair's public
  half in the `key` field (needed because the GoTrue PKCE redirect URL,
  `https://<id>.chromiumapp.org/`, must be pre-registered with Supabase + Google, and
  "Load unpacked" would otherwise assign a different ID per machine/download path).
  `host_permissions` is computed at build time in `vite.config.ts` from the same two origins,
  replacing a checked-in `https://*/*` wildcard. The extension is now safe to distribute
  broadly (no embedded secret); Chrome Web Store submission still needs the non-code
  readiness items in `docs/design/waves-1-4-public-features.md` (privacy policy,
  single-purpose statement, staged rollout).
- The extension's origin (`chrome-extension://<id>`) must be added to the FastAPI
  service's `CORS_ALLOW_ORIGINS` env var — the id is fixed by the pinned `key` above, so
  this is a one-time step per deployment, not per-install.
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
- **Dedup + canonical properties:** #15 (design context: `docs/design/new-dedup/PROGRAM.md` + `CUTOFF.md`)
- **Notifications / city-quality / operator state / pipeline:** #16 #17 #18 #22
- **Scraper framework & cadence:** #19 #20 #21
- **Measures & labels:** #23 (program charter: `docs/design/ppm2-measure-unification.md`)

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
   is set, and `--detail-only` never reaches the index phase. **The verdict is
   `scraper.portal.walk_coverage`, the ONE definition for all nine portals, and it has three
   outcomes, not two: `complete` / `incomplete` / `unknown`. Only `complete` authorises a
   sweep.** It replaced eight byte-identical private copies that FAILED OPEN — `if not total:
   return True`, i.e. "I could not measure, so assume complete". ceskereality's nationwide
   probe swallows its own exception and returns None, so a walk that reached a fraction of a
   category reported itself complete and became eligible to delist everything it never saw.
   An unmeasurable walk is now `unknown`; "I don't know" is not a proof. Note the care around
   zero: `None` means *could not measure* and fails closed, while a declared `0` is a real
   measurement, so an empty district IS complete (sreality's 77-district split depends on it)
   — conflating them via `if not total` is precisely how a failed probe came to look like an
   empty category. The gate is also TWO-SIDED: collecting more than `INDEX_MAX_OVERCOLLECTION`
   (1.02x) of the declared total means the slices overlap or foreign stock leaked in, so the
   denominator is wrong — contamination must not read as completeness. Measured 2026-08-27
   across 7 days and all nine portals, the worst real ratio is 1.0029, so the ceiling
   suppresses nothing that works today. "Complete" is ≥99.5%
   (`INDEX_MIN_COMPLETENESS = 0.995`) for the framework portals, NOT 100% — portal counts
   jitter mid-walk, and a strict 1.0 gate proved statistically unreachable for large bazos
   categories (delistings then accumulated for 11 days). The second rail: framework sweeps
   only flip rows additionally unseen for 24h+ (`min_unseen_hours` on `db.mark_inactive` /
   `mark_inactive_native`), so a tolerated walk-miss can never flip a freshly-seen listing,
   and a false flip self-heals on the next index sighting (`touch_listings` reactivates).
   Every flip stamps `listings.inactive_at` (cleared on reactivation) — the delisting-latency
   health check reads it. **A non-sreality portal sweeps on its own native id**
   (`db.mark_inactive_native` / `mark_inactive_agenda`, keyed `source_id_native`), never on a
   PK set resolved back out of the DB: under the listing-identity refactor's Gate 2 a
   non-sreality row carries `sreality_id = NULL`, and SQL three-valued logic makes ONE NULL
   inside the sweep's `<> ALL(...)` predicate evaluate NULL for EVERY row — the sweep would
   silently become a permanent no-op for the whole portal. `db.mark_inactive` (keyed
   `sreality_id`) is therefore sreality-only. All three sweeps drop NULL ids from the bound
   array and bail out rather than sweep with what's left of an all-NULL seen-set, since an
   EMPTY array flips the predicate the other way and would delist the entire scope.
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

    **What immutability covers.** The RESULT: the estimate, the trace, the cost, the
    comparables frozen at run time. Two things on the row are deliberately mutable and
    always have been. (a) The operator's yield `scenario` (`PATCH /estimations/{id}/scenario`)
    — a what-if overlay, not a computed output. (b) Subject IDENTITY. Since the surrogate
    cutover (migration 411, PR #1095, completed for `property_estimates_public` by migration
    412) every read path that answers "what estimates exist for this listing" FILTERS on
    `estimation_runs.input_listing_id` — `listings.id` — and on nothing else. (Display-side
    JOINs are a separate matter: `_LIST_FROM` still carries a guarded legacy arm to resolve a
    locality label for a run whose surrogate is not yet bound. That arm decides what a row
    shows, never which rows come back.) The legacy
    `input_sreality_id` is NULL for every post-Gate-2 non-sreality subject (migration 311's
    sign check), so keying on it silently dropped those subjects; and an empty id set once
    collapsed the predicate entirely, returning the whole table.

    One surface still uses the legacy id as a KEY SPACE, and it is not a defect:
    `latest_rent_estimations_by_listing` returns a map keyed by `sreality_id` for Browse's
    on-card estimate chip. Its JOIN already prefers the surrogate; only the map's keys are
    legacy, and the chip is sreality-only end to end — the affordance is gated on
    `sreality_id != null` and `createEstimation` submits a `sreality_id`, so no estimate can
    exist for a card the legacy key space would mis-answer. Re-keying the read alone would
    change no behaviour while risking a silent mis-key across six stateful call sites that
    the compiler cannot check (both ids are `number`). Browse gains non-sreality estimates
    only when `POST /estimations` accepts a `listing_id` — read and write together, in one
    PR, with a component test.

    An estimation submitted for a URL the scraper has not reached yet legitimately lands with
    `input_listing_id` NULL — the insert's `COALESCE` subquery finds no listing — so it
    belongs to no listing page until the listing appears. `_bind_pending_estimation_listing_ids`
    (in `scripts/recompute_property_stats.py`, on the property-maintenance tick that already
    attaches stragglers) stamps it exactly once. That is identity RESOLUTION, not result
    mutation: it only ever fills a NULL (the `IS NULL` guard is repeated in the UPDATE's own
    WHERE, so it is one-way and idempotent), it matches only the unique `sreality_id`, and it
    refuses fuzzy or multi-match URL recovery — a wrong attribution silently credits a paid
    estimate to the wrong flat, which is worse than leaving it unattached.

    This is also why there is NO `CHECK (input_sreality_id IS NULL OR input_listing_id IS NOT
    NULL)`. It holds for every row written so far, but it would reject exactly the
    not-yet-scraped case above and turn a working degraded path into a 500 on submit.
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
    at insert time — there is no insert-time matching.** All grouping is out-of-band, so
    neither `scraper/db.py` nor the maintenance job's straggler-attach does any spatial/geo
    probe. Frontend Browse reads `properties_public`; region stats read the property grain
    (migration 103).
    **What changed: the NEW DEDUP cutoff (2026-08).** The whole *automatic decision layer* that
    used to order merges was removed wholesale — a deliberate teardown, not a regression. It had
    grown into a many-rung machine (street+disposition and geo-proximity candidate paths, a
    pHash fast-path, a CLIP cosine routing tier, paid forensic vision compares, floor/site-plan
    gates, a batch warmer, self-healing review queues, a publication gate) whose behaviour the
    operator could no longer reason about end-to-end, and which merged on thresholds nobody had
    signed off. Rather than patch it further, the decision layer is being **rebuilt from
    scratch, simulation-first**: every level computes merge/dismiss outcomes "as if", into a
    separate droppable schema, over the whole corpus, and nothing writes a real merge until the
    full stack is approved. The program, its waves and its operator gates live in
    `docs/design/new-dedup/PROGRAM.md`; the surgical removal spec (every cut point, what was
    kept, what was frozen, what is dropped) is `docs/design/new-dedup/CUTOFF.md`.
    The cut was made at two seams. **Upstream:** scrapers, enrichment, image download, pHash
    computation and CLIP tagging/embedding all stay — every hand-off where they *fed work to*
    or *were gated by* the decision engine was severed (the tag job's dedup-dirty enqueue, the
    maintenance job's imageless-candidate enqueue, the real-time worker's dedup lane, the
    scheduled decision workflows, the eligibility predicates in `toolkit/publication.py`, and
    the dedup-specific checks in `scripts/verify_pipeline.py`). **Downstream:** everything from
    the moment a merge is *ordered* stays; every code path that *decided whether* to order one
    is gone. Deleted outright: `toolkit/dedup_engine.py`, `clip_dedup.py`, `dedup_audit.py`,
    `dedup_priorities.py`, `dedup_model_overrides.py`, `dedup_settings.py`,
    `dedup_batch_defer.py`, `visual_match.py`, `image_classification.py`, `publication.py`;
    the `scripts/` orchestrators, batch submit/ingest, golden-set builders and vision-model
    harnesses; `api/model_compare.py`; the `/dedup` API surface and its workflows.
    `toolkit/room_taxonomy.py` was reduced to vocabulary plus the merge-chokepoint category
    guard, and `toolkit/property_identity.py` lost its candidate-table stamps.
    **Consequences to hold in mind.** Nothing auto-merges any more, so cross-portal duplicates
    accumulate in Browse until the new engine ships — that build-up was accepted explicitly.
    The **publication gate is gone**: since migration 273 a new property stayed invisible in
    Browse/map/stats/watchdogs until something stamped `published_at`, and the only stamper for
    ordinary properties was the old engine, so leaving the gate up would have hidden the entire
    market. The gate was flipped inert first (`dedup_publication_gate_enabled=false`), then its
    code removed; the watchdog matcher's "new property" cursor is re-anchored on arrival
    (`listings.first_seen_at`), which is what it keyed on before migration 273.
    `properties.published_at` / `publish_reason` are kept frozen as a historical record. The
    legacy decision ledger (`dedup_pair_audit`), the manual-feedback and golden-pair tables, and
    the paid LLM verdict caches are **frozen, not dropped**: no code writes them and the new
    design never reads them. The engine's queue/state tables are dropped in their own migration
    PR, gated on operator confirmation and a `pg_dump`; `property_merge_events.generation` will
    stamp `'legacy'` on every pre-cutoff row so the future engine's merges (`'v2'`) are
    distinguishable. The blocking keys `listings.street_name_key` and `geo_cell_key` (+ their
    trigger) **stay** — the new Level 0 reuses them.
    **Standing rule: the removed code, its comments and its design docs are never consulted
    again for any purpose.** They survive in git history and on branch
    `backup/pre-new-dedup-2026-08` for forensic recovery only. The operator owns all
    merge/no-merge logic in the rebuild; thresholds, weights and rules are not to be invented.
    **The link mechanics (live, unchanged).** `toolkit/property_identity.py` is the single
    chokepoint through which any grouping change passes. `merge_properties` row-locks both
    properties `FOR UPDATE`, gates on `status='active'`, re-points `listings.property_id` onto
    the survivor, writes one `property_merge_events` row per moved child, soft-retires the loser
    (`status='merged_away'`, `merged_into`), and — inside the same transaction — carries
    operator state onto the survivor (`toolkit/operator_state.py`, rule #18), reconciles the
    deal pipeline (`reconcile_pipeline_on_merge`, rule #22) and re-syncs the browse read model
    (`sync_browse_list`, so Browse reads its own writes). `unmerge_group` replays the event
    ledger deterministically and `split_property_to_singletons` breaks a group apart; both
    reconcile the same operator state. Concurrent callers serialize per-property on the row
    locks, and a redundant re-merge is an `already_merged` no-op.
    **Category compatibility is enforced at the chokepoint** via the single
    `room_taxonomy.category_main_compatible` helper: a sale ≠ a rental (`category_type`), and a
    flat ≠ a house — **except** the ONE sanctioned cross-type **dum ↔ komercni** (the same
    building listed as a house on one portal and commercial on another is one real-world
    property, irrespective of sub-type). This guard is deliberately *at the merge*, not in the
    caller, so no future decision layer can route around it. It is distinct from the
    **asset-link** grain (migration 224), which links genuinely *different* units in one
    building (a `byt` and its ground-floor `komercni`, a `dum` and its `pozemek`) WITHOUT
    collapsing them into one property.
    **Who orders a merge today.** Only the operator: Browse's `mergeMode` (checkbox
    multi-select → merge) posts to `POST /properties/merge`, with the ledger and reversal under
    `GET /properties/merges`, `POST /properties/merges/{group}/unmerge` and
    `GET /properties/merged` (`api/property_merge.py`). Labeling / annotation CRUD that the old
    dedup page carried — training examples, border cases, image annotations, pHash pair notes —
    first re-homed under `/labeling/*` (`api/labeling.py`), then (docs/design/tag-annotation-matrix.md,
    2026-08) superseded: the confirmed-training-set half moved to a permanent, per-(image, tag)
    tri-state ground truth (`tag_taxonomy` + `image_tag_labels`, migration 442, managed under
    `/new-dedup/labeling/*`) that every independent per-tag classifier head will train from, and
    the old ClipAudit page (its only other consumer) was retired outright. `/labeling/*` now
    carries only border-case flagging (`image_border_cases`) — `image_tag_annotations` and
    `phash_pair_notes` had zero live callers even before the cutover. `image_training_examples`
    itself is superseded but not yet dropped (a separately-gated destructive migration).
    Interim caveat: the unmerge *button* lived on the deleted Dedup page, so until the rebuild
    gives it a home, unmerge is API-only.
    **Signal producers keep running** — they are the substrate the new engine will consume, and
    stopping them would leave a cold start: image pHash (`compute_image_phash.yml`), the
    self-hosted CLIP tagger and its embeddings (`clip_tag.yml` / `clip_retag.yml`, writing
    `image_clip_tags` + `image_clip_embeddings`), and the operator's labeling corpus
    (`tag_taxonomy`, `image_tag_labels`, `image_border_cases`). `listing_image_comparisons`
    (the agent-facing `compare_listing_images` tool) is unrelated to dedup and unaffected.
16. **Watchdog and Browse share one definition of "matches."** Saved watchdog filters live
    in `notification_subscriptions` (migration 056); the background matcher in
    `api/notifications.py` builds its WHERE clauses from the **same** logic Browse uses
    (`toolkit/comparables._shared_filter_where` + the shared `_city_quality_clauses`
    helper), so the two surfaces can never disagree on what a filter means.
    `notification_dispatches` is the **unified notification event table** (migration 206 —
    physical name kept; conceptually "notifications"): one source-generic, **property-grain**,
    append-only event row per `(source_kind ∈ {watchdog, collection_monitor, system_health},
    subject, change_kind)`,
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
    change signal; the kind is reserved for when one exists. **A THIRD producer is live
    (migration 274, `scripts/verify_pipeline.py`):** an hourly pipeline-verification job writes
    `pipeline_check_results` rows (`ok`/`warn`/`fail` per health metric, read via the anon
    `pipeline_checks_public` / `pipeline_check_history_public` views) and a `fail` status emits
    a `source_kind='system_health'` dispatch — `subscription_id` and `collection_id` both NULL
    (widened `notification_dispatches_source_ck`), `sreality_id` now nullable since the alert
    isn't about any one listing, verbatim text in a new `message` column, `change_kind =
    'system_alert'`. It rings the same in-app bell the SPA nav badge already polls; a
    SECURITY DEFINER dead-man-switch function (the migration-136 exception-guarded pg_cron
    pattern) fires if the hourly job itself stops running. The unified in-app **Notifications**
    page (`/notifications`) reads all THREE producers off one LEFT-join feed (the watchdog-only
    INNER join became a LEFT join + a `collections` join so monitor and system_health rows
    aren't dropped), and a red nav unread badge polls `GET /notifications/unread-count`;
    `POST /notifications/mark-all-seen` clears it.)
17. **City-quality indexes are a normalized, operator-curated time series.** `curated_cities`
    + `city_index_revisions` + `city_index_values` + `city_index_definitions` +
    `city_population` (migration 078 onward) store per-city indexes long-form, so a new index
    on next upload needs no migration; each upload appends a `source_revision` and the latest
    is the default query target. Filtering goes through the shared `_city_quality_clauses`
    helper and the `listings_with_city_quality` RPC, and the filters are **agenda-gated to
    BROWSE + WATCHDOG only** (`toolkit/filter_registry.py`) — the estimation agent
    deliberately never sees them, preserving deterministic estimate semantics. **Curated-city
    *membership* (which city, if any, a property's coordinate falls in) is precomputed onto
    `properties.home_city_id`** (migration 375, `recompute_home_city()`, hourly incremental job
    mirroring migration 142's `home_obec_pop`/`near_*` pattern) rather than evaluated live: a
    per-request `ST_Covers(admin boundary)` / `ST_DWithin(centroid, radius)` scan against all
    curated cities is only cheap at Watchdog's small per-subscription scale, not at Browse's
    full-table scale (a live EXPLAIN on the Browse-grain form priced in the billions). All three
    consumers — `listings_with_city_quality`, `browse_stats_properties`, and
    `_city_quality_clauses` — join the same `home_city_id` column, so they can no longer drift
    on the containment test the way `browse_stats_properties` previously had (radius-only,
    silently disagreeing with the other two's boundary-aware version on edge-of-city listings).
    The one exception is `near_city_proximity` (an operator-chosen radius search, not curated-city
    membership) — not precomputable the same way, and confirmed dead in the SPA UI today (no
    widget wires it), so left as a live, unoptimized, unexercised code path.
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
    **Every curation route runs on the tenant pool (`tenant_conn` + `verify_jwt`), with no
    exceptions** (hydration sprint W-1c). Until then the collection CRUD (`POST /collections`,
    `GET`/`PATCH`/`DELETE /collections/{id}`) and every `/tags` route were still on the
    `get_db_conn` + `require_token` pair the rest of the module had already left: that pair is
    service-role (RLS off) behind the static `API_TOKEN` that ships inside the public SPA bundle,
    and `api/curation.py`'s statements carry no account predicate of their own — they lean
    entirely on RLS — so any holder of the token could read, rename or delete another account's
    collections and tags (live data at the time: five collections across five distinct accounts).
    Two consequences worth keeping in mind when touching this module: (a) `collections` and `tags`
    are top-level tables with no owning parent, so unlike `collection_properties` /
    `property_tags` (whose BEFORE triggers copy `account_id` off the parent, migration 292) their
    INSERTs must stamp `account_id` explicitly or fail the WITH CHECK closed — `create_collection`
    / `create_tag` / `create_note` all take it as a parameter, resolved route-side by
    `tenant_pool.resolve_account_id`; and (b) the frontend wrappers for these routes must pass
    `jwt: true` (`frontend/src/lib/api.ts`), because `verify_jwt` no longer accepts the static
    token as a synthetic identity. `tests/api/test_auth.py::_jwt_gated_calls` is the test-side
    twin of this rule — a route moving back to `_gated_calls` is the regression to catch.
19. **The sreality scrape is split by cadence (Phase 2): a fast index-walk feeds an async
    batched detail-drain through `listing_detail_queue` (migration 105).** `index_walk.yml`
    (`scraper.main --index-only`, `run_type='index'`) walks the full index, `touch_listings` +
    `mark_inactive` (under the completeness guard, rule #3), and **enqueues** new/price-changed
    ids — classified by the ONE shared verdict rule, `portal.classify_index_sighting`, which
    every portal including sreality routes through (a rail in `tests/scraper/test_portal.py`
    fails any `*_main.py` that calls `price_changed` directly). Its load-bearing clause: **an
    index card with no price is missing evidence, not evidence of change, and reads
    `unchanged`.** Six portals used to fall through to `changed` there, re-enqueueing every
    "Cena na dotaz" listing on every walk forever — 85% of sreality's refresh queue, 91% of
    ceskereality's. That made refresh volume a function of WALK FREQUENCY rather than market
    activity, so doubling the walk rate on 2026-08-15 doubled the busywork and starved
    acquisition. bazos and remax already had the correct rule and were measurably the fleet's
    healthiest portals. Known gap, deliberate: a price-on-request listing whose detail price
    moves behind an unchanged index card is not detected — the same contract every listing has
    always had, not a regression; closing it needs a last-detail-fetch timestamp the schema
    does not carry. Enqueues
    ids into one of two service classes — ACQUISITION (`QUEUE_PRIORITY_NEW`, never fetched) or
    REFRESH (everything else, ranked failure-retry > price-changed > refetch-cohort).
    `detail_drain.yml` (`--drain-only`, `run_type='detail'`) claims a bounded slice
    (`FOR UPDATE SKIP LOCKED`) **composed from both classes**: `claim_detail_batch` reserves
    `QUEUE_ACQUISITION_RESERVE` (half) of each batch for acquisition and gives refresh the rest,
    with unused reserve backfilling to refresh inside the SQL. This is load-bearing, not a tuning
    knob: refresh inflow is unbounded (a walk re-enqueues on every pass) while acquisition is
    bounded by the market, so **any strict ordering that puts refresh first eventually stops
    ingesting new listings altogether** — it did, on 2026-08-17, for nine days and 15,064
    listings, with no alarm. `tests/test_detail_queue_fairness_live.py` holds the invariant.
    **Every walk carries a wall-clock deadline, checked PER PAGE, and a walk that stops on it
    MUST report `complete=False`.** `run_index_walk` builds the deadline from `--max-seconds`
    and passes it into `walk_category` (the Portal protocol declares it); portals check it with
    the one shared `portal.deadline_reached` — never an inline comparison, which is trivially
    invertible into a walk that runs to the job timeout while looking correct. Checking only
    BETWEEN categories is not enough and was the idnes failure: one idnes category is ~1,050
    pages, so the outer check never came round and GitHub SIGKILLed the job at page 599 in 9 of
    12 runs, each recording zero categories. Equally load-bearing is that the budget is actually
    WIRED: when this shipped, seven of nine portals called `run_index_walk` without
    `max_seconds` and sreality had no `--max-seconds` flag at all, so the checks were dead code
    on those portals. `tests/scraper/test_walk_deadline_wiring.py` is a census over the real
    modules that fails if any link in that chain — flag, forward, per-page check, protocol
    parameter — is missing on any portal.
    It fetches, and writes **batched** via
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
    `Portal`. **This split does NOT preserve portal-native listing order** — priority-bucketed
    claiming, concurrent thread-pool fetch, batch-constant `now()`, and (for 7/9 portals) two
    independent drain processes racing the same queue all reorder a listing between discovery and
    write (full analysis: `docs/design/portal-order-fidelity.md`). `listing_detail_queue.discovery_seq`
    / `listings.discovery_seq` (migration 368) is a dedicated sequence assigned once at true
    enqueue time — immune to all of the above because it's fixed before any of it happens — carried
    through `claim_detail_batch` → `write_detail_batch` / `ingest_scraped_listing` and written
    **once**, never on a later re-fetch (`COALESCE(listings.discovery_seq, EXCLUDED.discovery_seq)`,
    the same shape as `source_id_native`'s preserve-if-set rail). It is the true relative-discovery-order
    signal; `first_seen_at` (this rule's write-time stamp) is display-only going forward.
    **`listings.discovered_at` (migration 444) is its companion in TIME** — the same claimed row's
    `enqueued_at`, carried on the same path, written once by the same COALESCE. `discovery_seq`
    answers "in what order did we discover this", `discovered_at` answers "when". The pair exists
    because `first_seen_at` has always meant *when the drain wrote the row*, and that was
    indistinguishable from discovery only while the queue was healthy: during the 2026-08-17
    starvation the gap opened to **nine days**, so days-on-market, listing velocity, the price-drop
    baseline and the watchdog's `:new:` event were all silently reading queue latency as market
    behaviour. **`discovered_at` is deliberately NOT backfilled from `first_seen_at`** — that would
    restate the queue delay as a market fact, which is precisely the false statement that hid the
    outage. NULL is the truthful value for a row whose discovery time was not retained; only the
    tail seeded from `detail_queue_completions` is populated for history.
    **THIRD DELISTING RAIL — the flip cap (migration 451).** `mark_inactive` had no ceiling: it
    flipped every unseen active row of a category in one statement, however many that was. That
    was survivable only because the completeness gate kept the dangerous cases from running — a
    coincidence, not a safety property, and it ends every time a portal's walk is repaired,
    because **fixing coverage is the same event as authorising the mass flip it unblocks**.
    ceskereality's rebuilt walk moved byt/prodej from 85.7% to 99.8% in one deploy and made
    ~29,400 rows eligible; idnes has identical exposure the first time its walk ever completes.
    All three sweeps (`mark_inactive`, `_native`, `_agenda`) now count their scope BEFORE
    flipping and refuse anything above `app_settings.delist_flip_cap`. A refusal is RECORDED in
    `delist_flip_refusals` and alarmed by `verify_pipeline`'s `delist_flip_refused` — an Actions
    log expires, and a signal nothing can query is a signal nobody receives. Refusing is safe in
    the direction that matters: an unswept stale row is visible and self-heals on next sighting,
    a wrongly-delisted live listing is not.
    **The threshold is 10% with a 2,000-row category floor, and it is MEASURED (migration 452).**
    Across 60 days and 11,763 flipping sweeps the per-sweep share of a category is p95 = 1.8%,
    p99 = 3.4%, and then the tail jumps straight to 86% — routine churn and genuine incidents are
    two populations with a wide empty gap, and the ceiling belongs in the gap. The first cut (2%,
    floor 500) sat *inside* the churn population: it would have tripped 446 times in 60 days on
    ordinary sreality and idnes rental churn, and because the cap latches, it would have stalled
    delisting on our two largest sources permanently. At 10% it trips on exactly four real events
    (realitymix `dum/prodej` 86.3%, ceskereality `komercni/prodej` 30.1% and 13.7%, sreality
    `pozemek/podil` 18.7%). The floor is on category SIZE, not on the ceiling, because the small
    categories are the churny ones — sreality `pozemek/drazba` legitimately turns over 6–39% of
    its ~600 rows per sweep, since auctions end on a date. **Calibrate a breaker against the
    measured distribution or it becomes the outage it was meant to prevent.**
    **The cap LATCHES on purpose, so it needs a reset.** A refusal does not clear itself: the
    unswept rows keep aging, the next sweep proposes more, and it is refused again. That is
    correct breaker behaviour — an auto-reclosing breaker defeats the purpose — but the only
    reset migration 451 offered was raising the global ceiling, which disarms the guard for every
    portal at once. `delist_flip_cap.overrides` is the per-scope release valve: each entry is
    SCOPED (names its `source`; `category_main` / `category_type` / `subtype` omitted or null
    mean "any"), BOUNDED (`max_rows` is a hard row count, so even a wildcard entry cannot
    authorise an unbounded flip), and EXPIRING (`until` is required and must still be in the
    future). Anything missing, unparseable or already expired is ignored — the valve fails shut,
    exactly like the cap it releases, and one malformed entry never blocks a later valid one.
    It lives in the SAME setting as the cap so there is one knob to read and one to audit.
20. **Property maintenance is dirty-set incremental (Phase 3), not a full-table recompute.**
    The writers that change a property's children — `write_detail_batch` (a content change →
    new snapshot), `mark_inactive` / `mark_listing_inactive` (delisting), `touch_listings`
    (re-sighting reactivation) — enqueue the affected `property_id` into `dirty_properties`
    (migration 106) with a cheap set-based `INSERT ... ON CONFLICT DO UPDATE SET marked_at`.
    `property_maintenance.yml` (`recompute_property_stats --incremental`, cron `*/5`) attaches
    new stragglers (singletons only — the old geo Tier-1 matcher was removed; grouping is
    out-of-band, rule #15) and recomputes **only the queued properties** (the full
    recompute SQL scoped to
    `id = ANY(...)`), so a new/edited/delisted listing reaches `properties` + Browse within ~5
    min and the job is **O(changes)**, not O(all properties). The drain is race-free +
    terminating: it claims rows dirtied at/before a run cutoff and deletes only those untouched
    since (a mid-run re-dirty bumps `marked_at` past the cutoff → survives to the next pass).
    New listings (`property_id` NULL) are resolved by straggler-attach, not the queue. The
    **daily full sweep** (`recompute_property_stats.yml`, no `--incremental`, 04:15 UTC) is the
    reconcile backstop — it recomputes every property and clears the queue, so a missed enqueue
    self-heals within 24h *provided the sweep completes*: since the 2026-08-06 incident it runs
    under a `--max-seconds` budget (default 6000s, clamped to the same ceiling the workflow's
    `timeout-minutes: 130` is sized for) and on exhaustion clean-stops at a batch
    boundary, clears only the swept id range, exits RED, and does NOT write the
    `property_sweep_last_complete` stamp — so chronic exhaustion surfaces as a red run daily plus
    the `property_maintenance` health check failing on stamp age, and the unswept id tail keeps
    its pre-sweep windowed stats until a sweep finishes (is_active flips still heal incrementally
    — every delist path enqueues `dirty_properties`). The maintenance lease is one 15-minute TTL
    heartbeat-renewed every batch/slice, so a killed job freezes maintenance for minutes, not
    hours. (There is no scheduled dedup job any more — the automatic decision
    layer was removed in the 2026-08 cutoff, rule #15.) Both
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
    explicit method on the `Portal` protocol, justified in review. Sanctioned hooks so far:
    **sreality's district-split** (the deep-pagination-cap workaround) inside its `walk_category`;
    **ceskereality's and sreality's bespoke `probe_category`** (both lack a sort param their
    index accepts, so each implements its own per-page early-stop discovery probe instead of the
    generic capped-walk-then-diff fallback `run_index_probe` otherwise uses — sreality's version,
    added Phase 4 of the portal-order-fidelity program, is deliberately UNSPLIT: the
    deep-pagination 422 is offset-triggered, not size-triggered, so a shallow probe never needs
    the district-split; full rationale `docs/design/portal-order-fidelity.md`).
    The needs-detail queue is **source-generic** (`listing_detail_queue` keyed on
    `(source, native_id)` + `detail_ref`, migration 108) so every portal shares the one queue and
    the one drain. A portal that cannot prove a near-complete walk sets
    `supports_complete_walk=false` and the runner never marks its listings inactive (rule #3) —
    bazos (partial single-category walks) is such a portal; bezrealitky is NOT (its GraphQL
    `totalCount` + uncapped paging make a per-category walk provable-complete, so it sets
    `supports_complete_walk=true` and marks delistings inactive, source-scoped).
21b. **`category_type` is NULLABLE on Browse — NULL means "no deal-type constraint"** (the
    "Vše" pill). This is not new semantics: `toolkit/comparables.py`, the watchdog matcher in
    `api/notifications.py` and `browse_stats_properties` have always guarded the clause with an
    `is not null` check, and `applyRegistryFilters` skips null values — the only thing missing
    was a frontend type and a control that could reach the state. It is declared once, as
    `FilterDef.nullable` in the registry, which is what makes the shared `PillRow` render a
    leading "Vše" pill; any future nullable enum gets the control for free. Deliberately NOT an
    extra `any` enum member: `category_type` is on EVERY agenda, so an `any` token would be a
    legal input to the estimation agent, where "any deal type" silently mixes rent and sale
    comparables into one valuation. URL: `?deal=any`; an ABSENT `deal` still means the
    `pronajem` default, so bare `/browse` links are unchanged. Before this, clicking the
    selected deal-type pill (which `PillRow` has always offered) wrote `deal=null`, which
    `enumOr` silently snapped back to `pronajem` — the control lied about what it could do.

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
    `GET /pipeline/stages`). **The "Přidat do pipeline" affordance is the shared `<PipelineMark>`
    (`<FunnelIcon>` — a funnel with three arrows, filled body = in-pipeline — plus the stage
    badge) used on EVERY pipeline surface — the listing-detail header (`PipelineToggle`, in the
    top action bar next to "New estimation", NOT buried in CurationBlock), every Browse card AND
    every Browse **table row** (`PipelineFunnelButton`, a leading unsortable column), the
    stage-manager's entry-stage indicator (`is_entry` — filled = the entry stage), the Pipeline
    scope chip + its sidebar stage picker, AND the Chrome-extension panel (the glyph reproduced
    by value in vanilla TS — separate territory, no React import) — so the "into the pipeline"
    concept reads as one icon everywhere.**
    **And it MEANS the same thing everywhere.** Out of the pipeline, a click adds at the entry
    stage — cheap, reversible, one keystroke in the middle of triage. Already in it, a click opens
    the shared `<PipelineStageMenu>`: every live stage (badged, current one checked, terminal stages
    under their own divider) plus a two-step "Odebrat z pipeline". The funnel is **never** a remove
    toggle. It was one on Browse, and that was the bug: `remove_card` DELETEs the `property_pipeline`
    row, there is no restore path in the UI or the API, and re-adding stamps a fresh `added_at` — so
    a stray click in a 60-card grid, undone the obvious way, silently reset "in pipeline since" and
    every time-in-stage figure the board sorts on. (The transition trail survives in
    `property_pipeline_events`; nothing reads it back. Operator notes are property-grain — rule #18 —
    and were never at risk: `property_pipeline.note` is a separate column with no writer in any
    surface.) The confirm therefore names that consequence and points at the terminal stages, which
    close a deal while KEEPING its record — the data-preserving alternative to deletion. The menu
    also replaced the listing header's own `<select>` + bare ✕, and the Chrome-extension panel gained
    the same confirm on its ✕ (`buildConfirmRow` — one destructive-confirm shape for the whole panel,
    not one per action). The kanban is the sanctioned exception, on both counts: moves are
    drag-and-drop and its trash already carried the confirm.
    **Menus hang off `<AnchoredPopover>`, which portals to `<body>`.** Not a style choice: the funnel
    sits inside an `overflow-hidden` card AND inside that card's `<Link>`, so the app's other
    popovers (`absolute` inside their own container) would be clipped to the photo and every click
    inside one would navigate. Fixed coordinates off the anchor rect, flip up when the panel would
    overflow the viewport, reposition on scroll, close when the anchor scrolls out of sight.
    **Every pipeline write shares one cache policy** (`lib/pipelineCache`): TWO caches hold "where
    is this property" — `members` (the account's whole card set, keyed by `property_id`; read by
    the Browse funnels, the table rows, the listing header and the pipeline scope alike) and
    `board` (the kanban's own ordered array) — and each surface used to patch only the one it
    could see, so a kanban drag left every Browse funnel badging the pre-drag stage. It was three
    until W3: a per-property `card(id)` cache duplicated a single row of `members`, so every
    write had a third shape to patch and every listing header paid its own read; collapsing it
    into `members` made the chokepoint smaller, which is the only sanctioned direction for it. Optimistic patch in `onMutate`, rollback +
    revalidate in `onSettled` — deliberately NOT `onError`, because the global `MutationCache.onError`
    (`main.tsx`) stays silent for any mutation that defines its own, which is why a failed board drag
    used to snap back with no explanation.
    **The board's read is STRUCTURAL ONLY; decorations load through `lib/hydration`** (hydration
    sprint W1). `fetchPipelineBoard` used to await six serialized cross-origin round trips inside one
    promise — pipeline rows, a guaranteed-empty pagination tail, properties, every image of every
    card (830 rows to render 44 thumbnails, each row paying a per-image CLIP lateral), then two
    `/brokers` calls, the last of which existed only to fill a hover tooltip — and `Pipeline.tsx`
    rendered a bare "Načítání…" until all of it settled. It now reads ONE relation and returns —
    `pipeline_board_public` (migration 417, `security_invoker = true`) joins the account's
    pipeline rows to their properties server-side, so even the two structural reads W1 left
    behind became one; the cover photo and the broker line are independent React Query reads
    keyed on the surrogate `listing_id`, delivered to cards through `CardHydrationProvider`, and
    the cover comes from `listing_cover_public` (migration 416), which reduces to one row per
    listing BEFORE the CLIP-tag lateral instead of after. Three rules hold this
    in place. (1) **Decoration keys live in their own top-level `['hydration', …]` namespace** — never
    under `['pipeline']` — because `revalidatePipeline` invalidates `['pipeline','board']` after every
    card write and the stage editor sweeps `['pipeline']` wholesale, so a nested decoration key would
    refetch every thumbnail and broker on the board on every drag, making the split slower than the
    chain it replaced (`lib/hydration/hydration.test.ts` pins the disjointness). (2) **Decorations
    reach `CardFace` by context, not props**, because it renders twice — in-column and inside the
    `DragOverlay` — and props would let those two mount points drift. (3) **Enrichment isolation is
    now structural**: a failed broker read cannot affect the board because it is not on the board's
    promise, so the old hand-written `.catch(() => new Map())` swallow is gone and a failure is a
    real, visible error again instead of a permanent silent "no broker". `pipelineCache` is untouched
    by all of this — it only ever needed `property_id` plus a mutable `stage_id` on the board array.
    The rule going forward: if the board cannot filter, sort or place a card without a field, it is a
    decoration and does not belong in that queryFn.
    **The badge is `pipeline_stages.code` (migration 377), not an ordinal and never a parse of
    the label.** The live board numbers its stages inside the display text ("1. For Review" …
    "9. Passed", "9. Bought", "9. Lost"), so the number is operator data: three stages
    deliberately share "9" while their `position` runs 5/6/7, and a badge derived from ordering
    would contradict the labels the operator reads. `code` is nullable and NOT unique; when it is
    NULL the funnel falls back to the stage's 1-based ordinal among the live stages
    (`lib/pipelineStage.ts:stageBadge`) — computed where it renders, so nothing guessed is ever
    written back. `stageAccent` in the same module is the one answer to "what colour is this
    stage" (the operator's `color`, copper when unset — the board used to fall back to grey while
    the listing header fell back to copper). The stage editor exposes `code` as a 4-char box whose
    placeholder IS the ordinal, so "empty = automatic" is visible. Writes (add/remove/move) go
    through one hook, `lib/usePipelineCard.ts`, which owns the cache-invalidation policy for every
    surface.
    **Browse can be SCOPED to the pipeline** (`ListingFilters.pipeline`, `?pipeline=any` or
    `?pipeline=<stage ids>`, registry id `pipeline`, BROWSE agenda only): a property-grain id
    allowlist resolved from `property_pipeline_public` by `resolvePipelinePrefilter` and AND'd
    onto the cohort exactly like tags / with-estimates, plus `browse_stats_properties`'
    `property_ids_filter` (migration 378) so the Stats tab can never disagree with the list. The
    scope reads its membership through the SAME `fetchPipelineMembers` the funnels render from —
    one definition of "in my pipeline". It is surfaced as a chip in the preset row, and it behaves like the
    other chips in that row: clicking it **loads a VIEW** — the scope over a NEUTRAL cohort
    (`pipelineViewFilters`: no category, no deal type, no price/area/location), with any active
    preset deselected, in ONE atomic write (`browseState.loadPipelineView`; two writes against
    one searchParams snapshot clobber each other, and a preset left active over replaced filters
    reads as dirty and pops "Update preset"). It shipped as a modifier that AND'd itself onto
    whatever was set, which was wrong: Browse's default cohort is `byt` + `pronajem`, so "show me
    my pipeline" showed 1 of 45 deals — every sale flat, house and commercial unit hidden by a
    default the operator never chose for that purpose. `status` deliberately stays `any` so a
    deal whose listing was delisted mid-negotiation does not vanish; Back undoes the load
    (the write is pushed, not replaced). Turning the chip OFF is a plain filter edit that keeps
    the cohort the operator has since built, and the MODIFIER semantics still exist in the
    sidebar's Curation → Pipeline control (compose with current filters, pick stages). It is
    still **not a preset**: `PRESET_EXCLUDED_KEYS` keeps it out of what a preset stores and out
    of the dirty comparison, so it never offers "Update preset" and it survives loading one. A
    watchdog can't use it (`UNSUPPORTED_LABELS`) — the operator's own state can't be a trigger. The
    extension bookmarks AND changes stage property-grain like every other surface: it reads
    `property_id` + membership (incl. `stage_id`) off the batched `POST /listings/lookup` (and
    `GET /pipeline/stages` for the select options) and writes through these same
    `POST/DELETE /pipeline/cards` (bookmark/remove) + `PATCH /pipeline/cards/{id}` (move) routes —
    no extension-specific write path, no second secret. The `/pipeline` kanban board reads
    `property_pipeline_public` + `pipeline_stages_public` hydrated against `properties_public`
    (street + `mf_gross_yield_pct` from the view; one thumbnail per card via `listing_cover_public`
    (migration 416, W4) through the shared `lib/hydration` layer's `useListingCovers` — a
    server-side `DISTINCT ON`, deliberately a different QUERY from the multi-image
    `useListingPhotos` that Browse cards and the estimation comparables share (W7a): asking the
    multi-image read for one photo per card is the fetch-everything-then-discard W4 measured at
    901 rows / 3,995 buffers for 44 cards; the **canonical broker** per card via
    ONE batched read — `fetchListingBrokersByIds` (`POST /brokers/by-listings`), NOT the raw
    drift-prone `properties_public.broker_*` — the name links to `/brokers/{id}`, contact in a
    native-title hover. **Migration 419 (hydration sprint W6)** put `primary_email` /
    `primary_phone` on `listing_broker_public`, so the chained `fetchBrokersByIds`
    (`GET /brokers?ids=`) that used to follow it is deleted from the SPA — on the board and on
    listing detail alike. It was pure duplication: the contact pair sits on the same `brokers`
    row that view already joins (and already filters to `status='active'`), so the second
    statement re-read heap pages the first had in hand (measured on the 48 live board ids: 518
    buffers for step 1, then 207 execution + 436 planning buffers for step 2) and paid a second
    Railway round trip's ~270–410 ms floor to do it, serialized behind the first because its
    `broker_id`s came out of that response. The route itself stays for non-SPA consumers.
    Widening it is not a PII widening: `listing_broker_public` is API-only under A6 (below) and
    `toolkit.brokers.apply_pii_policy` masks on the column NAME, so both columns are swapped for
    `has_email` / `has_phone` for every non-admin caller with no route change. **Migration 398 settles that for good:**
    `listings_public`/`properties_public` still carry `broker_email`/`broker_phone` as columns
    (so PostgREST answers `?select=` with nulls instead of a 400, and the five matviews depending
    on `listings_public` survive) but project them as `null::text` — they were owner-rights views
    with a live `authenticated` SELECT grant, i.e. a bulk contact-PII read for any logged-in
    session. The masked `/brokers` API is now the only broker-contact path; `broker_name` stays
    (a label, not a contact). **Both went through PostgREST until
    2026-08-12 and were dark the whole time:** Phase 0's A6 revoked `listing_broker_public` +
    `brokers_public` from `anon` AND `authenticated`, so every read returned SQLSTATE `42501` and
    every card degraded to "no broker shown". `frontend/src/lib/brokers.ts` is now repointed wholesale
    onto the identity-gated API (every call `jwt: true`; the routes reject the static bundle token),
    so a logged-in caller gets HTTP 200 with either the values or `has_email`/`has_phone` — there is
    no longer an *expected* failure, and the 42501 special case is gone. The read stays isolated
    from the board (broker data is an enrichment; a failure must not take stages/cards/images down)
    but every failure is now `console.error`'d, never silently expected (pinned by
    `frontend/src/lib/pipelineBoard.test.ts`). A masked card keeps its broker name + firm and its
    hover box says the contact is admin-only rather than omitting it. The helper chunks its id
    list below the route's `MAX_BATCH` (1000) cap, which is a 422 on the whole batch — unchunked,
    a board past that size would lose EVERY card's broker rather than the overflow — and rejects a
    200 that carries no envelope (an SPA-fallback HTML page), a guard inherited from the deleted
    `fetchBrokersByIds` twin and now covering the entire broker line rather than half of it.
    The board offers basic **property-type
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
    audited write, never a second-grade path. The stage a surface renders comes from the shared
    `members` map (`PipelineMembership`); the per-property `PipelineCard` type and its
    `card(id)` query were deleted in W3 — every surface now selects its row out of the one
    account-wide read instead of issuing its own.
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
23. **One measure, one definition, one label.** Every per-m² figure the platform computes or
    renders — in SQL, in Python, in the SPA, in the Chrome extension — resolves from ONE named
    measure carrying its own numerator, denominator, unit and validity bounds. No consumer
    re-derives the formula; no surface renders the number without its basis label.

    **The measure** is `public.measure_price_per_m2(price, area, category_main, category_type)`
    and its label `public.measure_price_per_m2_basis(category_main, category_type)` (migration
    425), both `IMMUTABLE PARALLEL SAFE` single-expression SQL with no `SET search_path`, so the
    planner inlines them and a predicate over the measure is not a full scan. Numerator: the
    asking price in CZK, monthly on the rent basis and capital otherwise, exactly as the portal
    published it. Denominator: `area_m2`, **polymorphic by design** — floor area for byt / dum /
    komercni, PLOT area for pozemek (the "Option A" fork; `listings.area_basis`, migration 423,
    records which). Bounds: a NULL price, a NULL or non-positive area, an undecidable basis, or a
    price below its per-basis floor (sale 100 000 CZK, rent 1 000 CZK, land deliberately
    unfloored) all yield NULL — a visible gap, never a guess. Rounded to 2dp so all six
    publishing relations return byte-identical figures.

    **The basis is resolved from `(category_main, category_type)`, rent-first, and NEVER from
    `listings.price_unit`** — that column is four legacy spellings of two concepts across nine
    portals, a duplicate of `category_type`, not a per-area unit. The three tokens
    (`sale_capital_czk_m2`, `rent_monthly_czk_m2`, `land_capital_czk_m2`) are published as
    `price_per_m2_basis` on all six read relations, so a render surface READS the label rather
    than recomputing it. Two states a *cohort* can be in are not bases and get no unit at all:
    `mixed` (rule #22 makes a sale+rent cohort one click away — sale medians run ~91 535 Kč/m²
    against rent's ~319 Kč/m²/měs, a 300x category error if they share an axis or a suffix) and
    `unknown` (client-supplied rows carry no basis; the honest answer is not a default of sale).

    **The faces.** `toolkit/measures.py` renders the SQL (`per_m2_sql(alias)`), mirrors the
    resolution order for rows that never touched Postgres, and owns the vocabulary, the floors and
    the unit strings; `frontend/src/lib/measure.ts` is its SPA twin and reads the server-published
    token wherever a column exists; the Chrome extension, which can import neither, copies the one
    unit string VERBATIM. `api/estimate_yield._scale` may not multiply a per-m² percentile by an
    area without `require_scalable_basis` agreeing that the product may be CALLED what the caller
    intends — the arithmetic is identical for a monthly and a capital rate, which is exactly why
    an unlabelled one is dangerous rather than merely untidy.

    **Why a rail and not a rule.** The program that unified this found **64 live call sites** —
    nine SQL definitions, five Python-emitted statements bypassing every view, six client-side
    re-derivations, twelve render surfaces, and `region_stats`, whose signature had no category
    arguments at all, pooling sale flats, monthly rentals, houses and land into one distribution.
    They were not written by careless people; they were written one at a time, each locally
    reasonable. So W8 installed three interlocking mechanisms rather than a paragraph:
    (a) **required-argument signatures** — `per_m2_sql(alias)` has no zero-arg fallback and
    `fmtMeasuredPricePerM2(value, basis)` makes the old two-number call a TypeScript error under
    the already-blocking `tsc --noEmit` (pinned by `@ts-expect-error` cases in `format.test.ts`,
    which fail the build if the unsafe call ever starts compiling);
    (b) **the census** — `tests/test_measure_registry_census.py` scans six source trees AND
    `migrations/` (both the effective — highest-numbered, undropped — definition of each database
    object AND, unconditionally, every statement that is not one of the five tracked `create`
    forms: generated columns, DML backfills, index expressions, `comment on`, `grant`, `do`
    blocks, none of which anything supersedes), and fails unless every occurrence is declared in
    `toolkit.measures.REGISTERED_SITES`. **Three arms**, because each is provably insufficient
    alone: `division` (a price-ish expression over an area-ish one, both operands resolved by a
    bracket-balanced walk so aggregates, subscripts and wrapped operands land, not just bare
    identifiers) cannot see `scraper/price_stats_metrics.py`'s `12.0 * rent_per_m2_month /
    sale_per_m2`, which names no area; `unit` (a per-m² literal) catches that and every render
    surface, but goes silent exactly where this rule succeeds, because a well-behaved surface
    IMPORTS the label instead of spelling it; so `vocab` registers every file that reads
    `PPM2_UNIT` / `PPM2_UNIT_CS` / `PPM2_VALUE_LABEL` / `PPM2_BASIS_TOKEN`, one per file, making
    "labels correctly, computes the number itself" a census event too. Comments are stripped,
    string literals and docstrings are not — a comment is prose about the code, a string is
    something the program can emit; a prose match is registered as `kind="prose"`, never reworded
    away. Two value-comparing tests sit beside the three arms, because the census counts
    occurrences and is otherwise blind to what they SAY: they pin the SPA's `PPM2_UNIT` and the
    extension's copied monthly suffix against `PPM2_UNIT_CS` basis-for-basis. (That is how W8
    found the land basis carrying the sale suffix in the SPA — one measure with two labels.)
    (c) **`FilterDef.basis`** beside `FilterDef.unit`, because `CZK/m²` alone is two labels 300x
    apart and the registry reaches agents that never see the cohort; every numeric filter must
    declare a unit or be named in `UNITLESS_NUMERIC_FILTERS`, so silence is not a legal answer.

    **What the census does NOT see**, stated because a rail that oversells itself is worse than
    none — the next session reads the guarantee as proof. Both value arms are closed-vocabulary
    spelling filters: `price_czk / sqm`, `amount / area_m2`, a unit assembled at runtime
    (`'Kč' + '/m²'`) and a division routed through a helper (`np.divide(price, area)`) all pass.
    `ruian_*` / `area_km2` / `area_ha` are exempt by name on the denominator. And the SQL half is
    a census of `migrations/` **on disk, not of the database**: an object created by dynamic DDL
    inside plpgsql (migrations 283 / 299 / 371 / 376) or one that drifted into production with no
    numbered create statement is unregisterable and unseen — `property_sources_mv` was that
    example until migration 432 dropped it, and the blind spot it demonstrates remains. The full list
    is in the test module's own docstring, and it is the first thing to extend when a new shape
    gets through. A registered site that is NOT legitimate is marked `kind="debt"` and must name
    an owner and a blocker — and `debt` may not mean *reachable*: migration 083's `browse_stats`
    was registered as inert while still EXECUTE-granted to `authenticated`, so migration 428
    revokes that grant (additive) and the drop itself stays with the operator. The whole program
    is written up in `docs/design/ppm2-measure-unification.md`.


## Broker identity merges — auto-merge and the suppression rail

Unlike property merges (rule #15, operator-only), broker identities DO auto-merge. The nightly
sweep (`scripts/resolve_brokers.py::_auto_merge`, cron 04:35 UTC) hands the WHOLE identity +
contact corpus to `toolkit.broker_resolver.decide_merges`, which since 2026-08-20 is
**portal-agnostic and name-gated** — one rule, no per-portal exceptions:

> MERGE two identities when their **names match** AND (**A** they share a *discriminating* contact
> OR **B** they share a firm and that name appears at only ONE firm corpus-wide).

- **Names match** = `name_key` equality: diacritics folded, token order ignored, Czech academic
  titles stripped (`Bc. Ondřej Kadlec` ≡ `Kadlec Ondřej`; a title-only string keys to NULL, which
  matches nothing). No name, no edge — ever.
- **Discriminating contact** = a `(kind, value)` whose carriers ACROSS THE WHOLE CORPUS — every
  source, every identity, including identities of already merged-away brokers — all carry that one
  name. It replaced the frequency==1 "personal contact" guard, which duplication defeated: six
  copies of one agent made his own personal e-mail look shared (n=6), so the guard discarded the
  one fact that proved they were one person. Under the discrimination test duplication REINFORCES
  the signal; role inboxes (`info@…` under 353 names) and switchboards still fail it — by carrying
  many NAMES, not many rows.
- **Path R (name rarity)** is the presumption flip the operator directed on 2026-08-24: a
  same-name cohort is ONE PERSON unless the contacts disagree, provided the name is rare — it
  appears at no more than one firm in the whole corpus. No co-location evidence is required at
  all. Two earlier revisions each demanded some: requiring a shared firm row structurally
  orphaned every ceskereality record (no e-mail → no firm, so the identity abstained from the
  firm path forever), and requiring a shared contact value still missed pairs whose ledgers held
  only different desk lines, plus records with no contacts at all (realitymix is identity-only).
  Rarity is the entire warrant — a name confined to one firm and absent from the rest of a
  nine-portal market is overwhelmingly one person — and the contradiction veto is the brake.
  Common names (`Jan Novák`, at dozens of firms) fail rarity and merge only on path A. Guards:
  the firm spread is measured over the **identity's own** firm (`firm_identities.firm_id`, its
  e-mail domain), never `brokers.primary_firm_id` (a recency rollup that made the test
  self-weakening and non-deterministic); an identity with no firm abstains from the spread; the
  rule does not consult `firms.is_franchise` (a flag exclusion once parked 92% of the name_firm
  queue — 1,481 of 1,615 cards — behind firm-display metadata; the veto separates franchise
  offices by their disagreeing personal contacts instead).
- **Path F (shared firm)** drops the rarity requirement inside a firm: a same-name cohort at one
  firm is one person unless personal contacts disagree, however many other firms carry the name.
  Rarity guards the cross-firm question — is the record at ANOTHER firm the same human? — but a
  within-firm cohort never asks it; holding six "Václav Kučera" records at one agency hostage to
  a namesake elsewhere answered nothing a reviewer could judge either (2026-08-24: the entire
  post-rarity `name_firm` residue was this shape). F never crosses a firm boundary, so common
  labels at different firms still never fuse; the veto still refuses disagreeing personal
  contacts. Accepted residual: two same-named colleagues at one agency reachable only through
  office contacts pool into one broker — same-name, same-firm, mild, reversible.
- **The contradiction veto** refuses a cohort whose members carry **disconfirming** contacts —
  each a discriminating one of the same kind, no value in common. It is what catches a display
  name that IS the firm's name ("PREXIMA nemovitosti s.r.o." on five agents, each with a personal
  mailbox: unique to its firm by construction, so rarity alone can never see it) and what
  separates two same-named agents at two offices of a franchise brand (`re-max.cz` is one firm
  row over ~95 independent offices): their personal contacts disagree. Refusals land in the
  `name_firm` operator tab.
- **The contradiction veto reads only PERSONAL contacts** (`ROLE_EMAIL_LOCALPARTS` in
  `toolkit/broker_resolver.py`): an e-mail whose local part is a department word (`info@`,
  `prodej@`, `garaze@`, …) identifies a desk, and a phone published by an identity whose every
  e-mail is such an address is presumed the desk's line — one broker running five department
  mailboxes on his own domain is otherwise indistinguishable from five colleagues. Phone-only
  identities keep their phones in the veto, and a department mailbox still works as an A bridge
  (it can prove sameness, never difference).
- **Every review card explains why it was not auto-merged** (`evidence.hold`, rendered as one
  line under the card header): `multi_firm` (the firms the name spans), `contradicted` (the
  disagreeing personal values the veto read), `oversized`, `suppressed`, `firm_evidence_gap`.
  Pair cards get the hold from the engine itself (`MergeDecision.pair_holds`); `name_firm` cards
  from the generator by elimination — post-F those four codes are the only ways a group survives.
- **`name_cross_firm` cards** (gk `crossfirm:{nk}`, third review tab) surface the one population
  every automatic path refuses by design: a name whose identities span EXACTLY two firms (rarity
  fails, F never crosses a firm) — previously visible in no queue at all unless the two sides
  shared a contact. The evidence carries both domains and each side's activity window; disjoint
  tenures are the mover's signature, concurrent ones the namesake's — the fact the operator's
  one-click decision actually turns on. Dismissing one writes the same standing suppression a
  contact-bridge dismissal does (the operator judged the broker pair, and the pair must not
  auto-merge later when its evidence strengthens).
- The candidate generator deletes **stale PROPOSED cards of both its reasons** (a `group_key` the
  sweep no longer generates — e.g. keys minted before `name_key` stripped titles, which left
  duplicate cards for one cohort); merged/dismissed rows are operator ledger and are never touched.
- The paths are OR'd, and the firm-spread test guards **B only** — it is B's substitute for contact
  evidence, not an extra bar on A. A shared discriminating contact merges a common-named pair even
  if that name exists at fifty firms.

**Within-portal merging is allowed** (repealed 2026-08-20 — it was policy, never schema:
`broker_identities` only requires `UNIQUE(source, source_broker_id_native)`, and the biggest
duplicate fans are same-portal). Gone with it: the `broker_auto_merge_sources` allowlist, the
≥2-distinct-sources requirement, the ≥2-bridge corroboration bar and the per-source frequency
guard. `app_settings.broker_auto_merge_enabled` is the one switch left — **absent means ON**, an
explicit `false` skips the step with a log line (no migration; the engine change needed none).
Merges stay reversible via `broker_merge_events`, whose `reason` now records the evidence path
(`contact_name` | `name_firm` | `contact_name+name_firm`) and whose `bridge_kind`/`bridge_value`
are stamped only for a group formed by ONE contact edge carrying ONE value.

What the rule cannot prove goes to the operator, not the bin. A same-name pair at DIFFERENT firms
sharing a non-discriminating contact becomes a `contact_bridge_review` card; the same-firm shape is
already the `name_firm` tab, so it is deliberately not carded twice. A cross-name pair produces
nothing at all — the engine never proposes one, so there is no question to ask. #1096's
auto-dismissal (`status='dismissed', resolved_by='auto:name_conflict'`, deliberately NOT routed
through `api.broker_review.dismiss_candidate`, which writes `broker_merge_suppressions` — a
standing NO meant to record a HUMAN judgement) stays wired as the retirement path for the cards the
old corroboration guard queued before the name gate existed. And a proposal whose brokers no longer
both survive is retired the moment the merges land (`resolved_by='auto:sweep'`) rather than
lingering as a card that can only ever answer 409.

Components cap at `MAX_AUTO_MERGE_COMPONENT`, raised 6 → **20**: every edge is name-gated and
`name_key` equality is transitive, so a component of the pure layer is single-named BY CONSTRUCTION
and the cross-name chain fusion the old cap guarded against (one recycled phone chaining distinct
people) cannot form there. What the cap stops is a role-account mega-pool — one switchboard under
one generic label, a live example carrying 464 records — while clearing the largest observed genuine
duplicate fan (7). An over-cap component is downgraded WHOLE, and queued as its real **edges**
(n-1 same-name shared-contact pairs) rather than its n(n-1)/2 transitive closure: expanding that
464-record pool pairwise was 107,416 cards a night, and the review writer's "must share an actual
contact" filter thins none of them, because the one switchboard that chained the pool is on every
pair. A suppression anywhere inside a component downgrades the component the same way, and the
component is computed over EVERY edge including the suppressed one — dropping the edge first let a
suppression that landed on a chain's hub strand the detached identity in neither queue, with the
outcome turning on which id sorted first. `_apply_merges` then merges at BROKER grain, which
deliberately widens what one run fuses (two capped groups chain through a broker holding an identity
in each — the one place differently-named groups can meet, since `name_key` transitivity rules it
out inside `decide_merges`); that chain is bounded by the SAME cap at broker grain, and a wider
component is skipped whole and counted rather than merged with only a warning.

The 7,689 live auto-merges predate this rule and were decided under the old bar — those with
conflicting names are NOT retroactively undone.

**The rail (migration 401).** The sweep re-derives its whole candidate set every night from
`broker_identity_contacts` and consulted no decision record, so an unmerge came straight back and a
dismissed pair auto-merged the moment its evidence strengthened (the shared contact losing its
other names, or two display names converging on one key). `broker_merge_suppressions` records the
operator's NO keyed on the **identity** pair — durable (`broker_identities.id` is never deleted), unlike a broker
pair, which stops describing the same cohort after any later merge. Written by `unmerge_group` (every
cross-owner pair it pulled apart — same-portal pairs now included, since those merge too) and by
dismissing a `contact_bridge_review` candidate;
read once per sweep and enforced twice — in `decide_merges` (a suppressed pair reaches neither
auto-merge nor review, and a suppression anywhere INSIDE a component downgrades that whole component
to review, the oversized-component expansion included) and as an apply-time backstop that
drops any component which would newly co-locate a suppressed pair, catching the transitive chain the
pure layer cannot see. The backstop re-reads the active set inside its own write transaction, because
the merge step runs ~8.4 min and an operator NO landing inside that window must still bind.

Both writers derive from a **cohort read, never a remembered id**. The unmerge anchors on where the
restored identities live NOW: survivor = `min(id)`, so a later merge can retire the survivor of an
earlier one, and deriving ownership from the id stamped on the event rows then returns nothing —
every identity reads as one owner, zero suppressions are written and the sweep re-applies the merge
that night (the exact failure the table exists for). A dismissal anchors on the candidate's BROKER
pair and suppresses every cross-source pair between them: the card is keyed `contactbridge:{lo}:{hi}`
and `_queue_review_pairs` last-write-wins its evidence, so `evidence.identity_ids` is a sample of the
decision, not its extent. Pairs already sharing a broker are skipped by construction — a proposed
candidate outlives its brokers being merged, and an active suppression over co-located identities is
an instant, permanent invariant violation.

Lifting never deletes: an explicit operator merge stamps `lifted_at`/`lifted_by`/`lift_reason` and
wins outright — but only for pairs it actually brings together (`lo.broker_id <> hi.broker_id`);
lifting an already-co-located pair would silently clear the evidence of a bypass rather than overrule
a decision. `GET /broker-review/suppressions` (active first) and `POST
/broker-review/suppressions/{id}/lift` make the ledger readable and clearable through the product.
`verify_pipeline`'s `broker_merge_suppression` check — in the hourly emailing lane, O(1) — fails on
any active suppression whose identities share a broker. It matters MORE now that no portal
allowlist gates the engine: remax contacts are email-only and ceskereality's phone-only, so one
shared contact plus a name match is the entire case for those merges.

## Location data (W1) — the greenfield location SSOT

W1 (migrations 380–389, PRs #1008–#1013 + fixes) shipped a **parallel** truth model for where a
listing is — not a change to the existing one. It is **shadow-only**: `listings.geom`, the geo-derived
admin columns and `scraper/street.py` still back Browse, the map, the watchdog and dedup, and **no
consumer flips before W6**. The authoritative plan is operator-held outside this repo —
`~/location-data-architecture-2026-08-10/design/final/MASTER.md`, `00-shared-contracts.md` the
tie-breaker (the `00 §…` / `03 §…` citations in the code are that corpus); shipped state and
sequencing live in `roadmap/location-data.md`.

**Three layers, one direction.** `location_claims` is **append-only evidence** — what a payload
asserted, with an extractor id, a surface, a licence class and a `claim_fingerprint` (migration 386's
IMMUTABLE `location_claim_fingerprint()`, computed in SQL so W2's re-mine and W3's backfill reuse the
definition instead of re-transcribing it). Nothing is corrected in place: a wrong claim is retracted
and a new one inserted. `location_resolutions` + `location_resolution_candidates` are the output of a
**pure function** — S1–S9 in `location_data/resolver/`, no wall clock (`as_of = max(observed_at)`),
no network, no randomness, enforced by an AST scan — so a resolution replays byte-identically from
its inputs and the five version ids stamped on it. `listing_location_current` +
`property_location_current` are **rebuildable caches**, never a store of record: the
`dirty_locations` drain rebuilds a row from its resolution, the full sweep anything built at a stale
version tuple.

**Four precision axes, mandatory next to every coordinate** (D3): `granularity` (ordinal enum,
country → … → address_point), `position_source` (admin_centroid → carried_forward →
portal_pin_blurred → portal_pin → registry_point), `match_confidence`, and `uncertainty_radius_m`
**paired with** `radius_semantics` — NOT NULL on the resolution, the candidate and both projections
(at claim grain only a nullable `declared_radius_m`; most portals declare nothing). `blur_evidence`
and `position_licence_class` ride alongside as separate NOT NULL facts: W6's consumers must know how
much to trust a pin, not just where it is. Radii ship uncalibrated by design (never `r95_empirical`);
calibrating writes a new `policy_version`, it never edits a row.

**Licence enforcement is structural.** `licence_class` is the program's single licence vocabulary and
`ephemeral_display_only` (Mapy.cz-class) its poison value. Three CHECKs — `loc_res_licence` on
`location_resolutions`, `llc_licence`/`plc_licence` on the projections — make such a position
impossible to mint or to land in a store of record, and a partial index on `location_claims` keeps
the remediation set one indexed predicate away. The claim lane's blocking gate is `claims JOIN
mapy_affected WHERE claim_type='coordinate'` = 0, and it refuses to start unless the Mapy affected-set
inventory (migration 385 — five arms, identity and reason codes, **never** a coordinate, and
trigger-immutable: 42501 on UPDATE/DELETE/TRUNCATE) is TERMINAL *and* COMPLETE. Half-built is worse
than none, because every listing past its high-water mark reads as absent — the verdict that admits a
Mapy coordinate as first-party.

**The RÚIAN mirror is versioned, not mutated.** `ruian_*` (migration 381) holds ČÚZK's address points,
streets, parcels, building objects, admin units and a typo-tolerant gazetteer. Every load stamps one
`registry_versions` row (`ruian:YYYY-MM-DD`) and publishes by **pointer swap** behind blocking
assertions, so it never half-changes the world underneath a resolution that pinned a version. Křovák
S-JTSK → WGS84 goes through ONE audited conversion on an explicitly chosen 1 m PROJ pipeline
(`location_data/krovak.py`; the 6 m one is never used), guarded by a golden-point test; boundary packs
carry three geometries per unit (authoritative, subdivided pip, render). Freshness is the monthly
baseline — the VFR daily-delta lane ships as chain-verification only and fails loudly until the
`ST_ZZSZ` element schema is pinned down.

**Portal contracts are data; git stays the store of record.** `contracts/portals/<portal>.yaml` × 9
declares every extractor (permanent id, surface, licence class, caps, priors, exclusion zones) and
`location_data/contracts.py` projects them into `portal_contracts`/`portal_contract_entries`,
idempotent per `contract_version`, refusing a changed body under a loaded version. An unknown
top-level YAML key is a refusal, not a shrug — every key in this format fails OPEN when misspelled.
**Entries are immutable** — a fix is a version bump, never an edit, so a claim's `extractor_id` always
names the rule that produced it. Hence no per-portal branch in the intake: a new signal is a YAML
entry, not code.

The header carries **two mutable columns and no more**: `is_active` (which version the extractor runs)
and `shadow` (whether what it mined is admissible to the resolver yet, migration 404). **Shadow is the
W2 gate**: a contract that cannot meet its frozen-sample precision floors merges dark — claims mined,
stored and auditable, but excluded from `location_claims_live` — so a failing gate has somewhere to
land that is not "revert the branch". Three relations, one predicate each: `location_claims_unretracted`
states the retraction predicate once, and `location_claims_live` (resolver) and `location_claims_shadow`
(scorer) partition it. The scorecard reads the shadow relation
(`toolkit/location_labels.score_shadow_claims`, `/location/sample/{source}/score-shadow`) — without a
read path the un-shadow gate would be unsatisfiable. Flipping the flag (`--shadow` / `--unshadow`)
**enqueues the contract's listings into `dirty_locations`** in the same transaction: the view flips
instantly but `listing_location_current` is rebuilt only by the drain, and neither the intake (new
claims only) nor the daily sweep (version-tuple predicate) would ever re-queue them. The enqueue is
unconditional, like the operator-correction lane, so a re-run after a failed drain is never a dead
button. `shadow` is excluded from `contract_sha256` — it is operational state, so editing it in git is
not a `contract_version` bump (and a bump would re-shadow the contract, discarding the passed sample).

**Ops rules the incidents wrote.** All four heavy lanes — registry load, claim intake, Mapy inventory,
resolve — share the OUTER `location-batch` concurrency group so **at most one runs at a time** (each
keeps its own inner group at job level); a new heavy lane joins it. On 2026-08-10 four concurrent
lanes dropped backends across the fleet, degraded the live Browse rebuild to multi-minute
DataFileReads and wedged two lanes with no error at all. **No batch statement runs without a ceiling**:
`statement_timeout = 0` is for genuine bulk phases (COPY, index build, whole-table rebuild) and
nothing else; per-unit and per-batch work arms `SET LOCAL statement_timeout` in its own transaction
(budgets env-overridable, `LOCATION_*_TIMEOUT_S`; gate
`tests/location_data/test_location_batch_hardening.py`). And the drain's cost is **round trips, not
work**: from Actions it is network-RTT-bound at ~5–17 listings/s (~75 ms per GitHub↔`eu-west-1` trip
against 0.02–0.5 ms of server-side work), so the slice-batching, memoization and prefetching that got
it there compound if the lane ever moves onto the always-on Railway worker (~1–2 ms RTT).

## Cross-reference map

| Topic | Operational how-to |
| --- | --- |
| Database, migrations, schema, connection modes, Supabase MCP | `.claude/skills/database` |
| Toolkit tools, FastAPI, auth, versioned trace, env-vars & secrets | `.claude/skills/toolkit-api` |
| LLM URL parsing, cached analysis tools, vision tiers, MF rent map | `.claude/skills/llm-pipelines` |
| Running/debugging scrapers, adding a field, fixtures, reading logs | `.claude/skills/scraper-ops` |
| Location-data program (claim spine, RÚIAN mirror, contracts) | `roadmap/location-data.md` + the operator-held design corpus |
| Roadmap / sequencing | `ROADMAP.md` + `roadmap/<track>.md` |

**Two "skills" namespaces (don't conflate):** repo-root `skills/` holds **agent** skills
seeded into the `skills` DB table (architectural rule #10). `.claude/skills/` holds the
Claude Code reference skills above — never seed those into the DB table.
