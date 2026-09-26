# Architecture — deep rationale

`CLAUDE.md` holds the hard rules (one to three lines each). This file holds the WHY —
the full as-built rationale, edge cases, and incident history behind them. Read the
relevant section here before modifying any code an architectural rule touches.

- Operational how-tos live in the on-demand skills under `.claude/skills/` — `database`,
  `toolkit-api`, `llm-pipelines`, `scraper-ops`.
- Design-time specs live in `docs/design/` (new-dedup/PROGRAM + CUTOFF, notifications-unified,
  price-stats-datasets, realtime-scrapers).
- Sequencing lives in `ROADMAP.md` + `roadmap/`.

## Data sources — per-portal narratives

How each portal is ingested: API/HTML shape, parser strategy, coordinate source,
completeness posture (`supports_complete_walk`), and quirks. The **operational** side —
which workflows run each portal, their crons, dispatch inputs, and log lines — is in the
`scraper-ops` skill; cross-source grouping is rule #15 (and, for the rebuild in progress,
`docs/design/new-dedup/PROGRAM.md`).

**Where a typed attribute comes from is DECLARED, not discovered (field-capture W2).** Every
narrative below says its typed fields are "normalised to the same canonical labels sreality
emits". That is now one table and one module rather than nine copies of each:

- **`scraper/attribute_contract.py`** — the ATTRIBUTE contract: one cell per (portal, typed
  column), all 9 × 26 declared, carrying the producer (`structured` = named payload keys,
  `text` = mined from the ad prose, `derived` = the URL/breadcrumb/title, `none`), the source-key
  PRECEDENCE (replacing the inline `params.get(a) or params.get(b)` chains), what a MISSING key
  means (`false` on bezrealitky's real booleans, `unknown` everywhere else), the default
  sentinels, and — for a cell nothing fills — the census key a later wave will wire.
  Not to be confused with the LOCATION contract in `contracts/portals/*.yaml`: that one is
  governed by a hash tied to a re-minable claim corpus, and typed attributes are deliberately
  NOT a seventh top-level key there (`location_data/contracts.py` `_TOP_LEVEL_KEYS`).
- **`scraper/vocabulary.py`** — the producer side of the vocabulary: one diacritic fold, one
  `(field, portal label) → canonical` registry, one disposition grammar, one PENB grammar and
  the boolean readings. The CANON stays in `toolkit/filter_registry.py`
  (`COLUMN_CANONICAL_VALUES`, keyed by `listings` column — NOT scanned off the filters, which
  made the `building_material` bucket name `ostatni` a canonical construction and left
  `price_unit` with no canon at all) and is imported, never restated; the LLM tool schema's
  enums are generated from `CANON`, which since W5 IS the whole value space — `disposition`
  and `price_unit` included, so the on-demand URL parser cannot emit an `8+7` or a fifth
  spelling of "monthly". A label no entry names is NULL **plus a counted event** in the run
  summary (`RUN done … unmapped=N`), never a passthrough; the
  counter is drained per drain pass, because the always-on worker runs every source's drain
  in one long-lived process.
- The evidence both answer to is the checked-in per-portal key census in
  `data/field_capture/census/`, through gates A1 (no dead read), A2 (no unread emission ≥ 5%
  that is neither mapped nor ignored with a reason) and A3 (no unmapped live value), plus the
  characterisation goldens in `tests/fixtures/field_capture/golden/` — recorded from the
  parsers as they stood before the module existed, so a value that moves is visible.

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
counter, `note`/`rus`/`rusReply`). The parser also emits the listing's **page URL**
(`listings.source_url`) like every crawler portal does: `scraper/sreality_url.py` assembles
`/detail/{type}/{main}/{sub}/{locality}/{hash_id}` from the payload's `category_*_cb` codes and
`locality.*_seo_name` values through a **closed, sreality-sourced codebook** (its sitemap; the sub
slug is NOT a slugified label — 37 "Rodinný dům" is `rodinny`; auction types are `drazby`/`podily`,
not the stored `drazba`/`podil`). sreality validates type/main/sub strictly (404) and only the
locality leniently (301 to canonical). An unknown code yields NULL + a counted reason, never a
guess; the column is preserve-if-null at the write and **display-only** (the SSR page is a
login-redirect loop — `db.detail_ref` keeps it off every fetch queue). No surface reconstructs
a URL; design + waves: `docs/design/portal-listing-url.md`.
**Photos** come off their CDN (`*.sdn.cz`) through a render-transform chain, and that CDN is an
**exact-template allowlist**, not a transform language: only templates they publish return bytes,
everything else 400s (and a bare URL 401s). We download through their `SQUARE_1800_JPG` template,
`res,1800,1800,1|shr,,20|jpg,80` — the **whole frame** (mode 1 = fit, no crop) at up to 1800px, no
watermark. It replaced `res,749,562,3|shr,,20|jpg,90`, whose mode 3 CROPPED to 4:3 (~85% of sreality
photos lost their edges; a floor plan lost a whole floor) at a fraction of the master's resolution.
`image_storage.with_transform` is the one definition: a stored `sreality_url` may be bare, carry the
legacy chain, or carry a prefix chain (`?fl=rot,180,0|`), and all three are **normalised** onto the
current template. What survives from a stored chain is itself an **allowlist**, `_PRESERVED_OP_HEADS`
= `{rot}`: `rot,<deg>,0` is a per-photo fact the template can't carry (without it the CDN returns 200
and stores the photo unrotated) and is the one prefix verified accepted in front of the template.
Everything else is dropped — the serving ops (`res`/`shr`/`jpg`/…) because they're ours to choose, an
unrecognised op because carrying it through would build a chain off the allowlist, and a 400 is
classified `source_unavailable`: terminal, out of the queue, never retried. Dropping costs at worst a
cosmetic difference on one photo; keeping could park a cohort. The chain is split by hand and never
URL-encoded: the allowlist matches literally, so `%2C`/`%7C` are rejected.
`frontend/src/lib/imageUrl.ts` mirrors this for the not-yet-downloaded fallback, and the mirror is
pinned on THREE things by `tests/test_image_transform_parity.py` + `imageUrl.test.ts` — the template
string, the kept-op set, and shared probe vectors (`tests/fixtures/sreality_transform_probes.json`)
that both suites run through their own normaliser, so a drift in the logic reds a suite too.

**Data source (bazos.cz).** A separate HTML crawler (`scraper/bazos_client.py`,
`bazos_parser.py`, `bazos_main.py`) lands bazos listings into the same
`listings`/`listing_snapshots` contract, tagged `source='bazos'`. It walks 22 nationwide
scopes (byt/dum/chata/restaurace/kancelar/prostory/sklad/pozemek/zahrada/garaz/ostatni ×
prodam/pronajmu). The last four closed a **silent** four-year coverage gap: migration 160
deferred pozemek/garaz/ostatni ("left out for now") and never named zahrada at all, so ads
filed in those sections were never enqueued — no error, no failed fetch, nothing in
`scrape_runs`, because a section the walk never requests cannot fail. Migration 488 added
them; `tests/scraper/test_portal_category_coverage.py` now fails CI whenever a parser's
`CATEGORY_MAIN` knows a slug the walk doesn't ask for. Still excluded on purpose: `projekty`
("Nové projekty" — developer marketing that also appears under byt/dum, and no other portal
in the fleet carries such a category) and the rent-only `podnajem`/`ubytovani` (roommate
search and short-term lodging classifieds, not the sale or rental of a property). So — like
sreality/idnes (rule #19) — it is **cadence-split**: `bazos_index_walk.yml` (every 6h, full
walk + presence nomination + enqueue) feeds the bounded `bazos_detail_drain.yml` (hourly,
`--max-seconds` budget). A combined run can't do both inside one job (the full 22-scope walk is
~2400 index pages ≈ 85-90 min at the portal's 0.5 req/s index rate since migration 488 added
pozemek/zahrada/garaz/ostatni — a 130-minute job timeout with a 110-minute budget — which eats
the window, starving the drain); narrow ad-hoc runs go through the split
workflows' dispatch inputs (`-f sale_type=… -f category=…`, or locality + radius) or
`scraper.bazos_main` locally. **A REMOVED ad answers HTTP 200 with the CATEGORY INDEX page** —
bazos lands `/inzerat/<id>/…` on `/inzeraty/<slug>/`, titled `… inzerce - Reality | Bazoš.cz`,
carrying an "Inzerát byl vymazán" banner — so, like mmreality/ceskereality/remax,
`bazos_client.fetch_detail` reads all three as `ListingGoneError` (2026-09-12; it previously
matched only an unused "smazán" spelling, so 4,374 removed ads stayed active — rule #3's page
check can only decide gone if the fetcher says so). Detail-path only: an INDEX fetch returns
that same page by design, and a gone verdict there would truncate the walk. **Detail-page** raw HTML is staged in `portal_raw_pages`
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
failed PUT rolls the row back. **A `detail` body is ALWAYS archived and every other `page_kind` NEVER is** — one page-kind
comparison in `scraper.db._payload_archive_enabled`, no flag, no per-portal limit, no
measurement corpus. Three gates used to stand here (`PortalLimits.payload_dual_write`, a second
`payload_index_archive` for non-detail surfaces, and a "has this surface been weighed" check
against `location_data.payload_budget.PORTAL_STORAGE`); rule 25 removed all three with the
modules that read them, because the stored detail body is now the claim lane's SECOND SUBSTRATE
rather than an opt-in experiment. What survives is the reason the split existed, as an
invariant about GRAIN: a `detail` body is ONE listing fetched when that listing is enqueued and
is mined for that listing's claims, while index, map, gazetteer, snapshot and archive bodies
are whole-SURFACE artefacts refetched on the walk cadence (sreality's index 24x/day) that no
listing's claim can be mined from. All three index archivers still stage through their own
client-side freshness skip; with index bodies out of the payload archive entirely, that skip
now only guards a latest-wins `portal_raw_pages` row.
**`portal_raw_pages` is preservation substrate, never pruned** (location-data
program, W0 item 0o): migration 099's "safe to delete once parsed" header is superseded —
the archive is the only surviving copy of delisted pages' location signal (portals don't
serve delisted pages again), an off-database copy lives in R2 under
`backups/portal-raw-pages/` (`scripts/export_portal_raw_pages_archive.py`, workflow
`export_raw_pages_archive.yml`), and `tests/test_portal_raw_pages_guard.py` fails CI on
any `DELETE`/`TRUNCATE`/`DROP` against the table. Coordinates come from the detail page's embedded Google-Maps/Mapy.cz link
(page-wide, CZ-bbox-guarded); they are what lets cross-source dedup match bazos against
sreality. **bazos' STREET is text, and since W18 it is claimed** (`bazos@7`,
`bzs.det.street_cue`): it was the only portal of the nine with no street claim at all —
129,871 located rows, every one obec-grain — while the ad states a street on a third of them.
The claim reads ONE surface: `raw_json.title`, which is
`h1.nadpisdetail` verbatim (`scraper/bazos_parser.py:485`) and therefore the SELLER'S OWN
HEADLINE for that ad. Of 20,909 cued titles, 18,226 anchor to an obec and 14,659 (80.1 %)
bind exactly; ~4,300 more cue-less titles carry a comma segment that folds to a register
street. bazos hard-caps a title at 60 characters — 21,930 sit exactly on the cap, cut
mid-word — and a truncated stem never binds. Three other surfaces were measured and left out:
`raw_json.coords.street` is NOT subject-scoped (`extract_street` scans the title *and the
description* and takes the first cue match, so 29,697 of its 50,529 values never appear in
the title — boilerplate like "Energetická třída" and proximity prose that binds to real
streets, which on a portal whose every pin is blurred would silently MOVE the point); the
`<head>` title is the same capped string plus the okres and " | Bazoš.cz"; and the
description is populated on 93 % of rows and carries a cue on 48 %, but its FIRST cue is
usually prose and binds exactly only 19 % of the time — W19's lever, with the number already
taken. Nothing about the text is trusted on its own: the claim keeps
whatever the portal wrote (only the generic `ulice`/`ul.` wrapper comes off, via
`street_token`) and the RESOLVER decides, so `Nový` — hallucinated out of "Nový 2 pokojový
byt" and once geocoded 130 km away — is claimed and never published, while the numeric-leading
`28. října` that the old morphology guard refused binds.

**Data source (bezrealitky.cz).** A scheduled scraper (`scraper/bezrealitky_client.py`,
`bezrealitky_parser.py`, `bezrealitky_main.py`, workflow `scrape_bezrealitky.yml` — pilot,
every 6h) tagged `source='bezrealitky'`. Bezrealitky is a JSON-API portal like sreality
(not an HTML crawler): it reads the public GraphQL API at `api.bezrealitky.cz/graphql/`
(`listAdverts` for the index — offset/limit paging, `totalCount` for completeness,
`includeImports:false` to scope to bezrealitky's OWN private-seller inventory — and
`advert(id)` for detail). The API requires browser-like `Origin`/`Referer` headers; no
cookies. `bezrealitky_parser.parse_advert` maps the advert object onto the shared
`ScrapedListing` contract, translating bezrealitky's enums into the SAME canonical label
strings sreality stores (`po_rekonstrukci`, `cihla`, `za nemovitost`/`za mesic`, `2+kk`, …) so
cross-source filtering/dedup/condition-scoring see one vocabulary. Coordinates come from
the API's `gps` field (precise, per-listing — no geocoding step). Because the detail JSON
carries `offerType`/`estateType`, the drain derives each listing's category from the
response, so one config walks many categories (no per-category queue encoding).
`listAdverts` has a `totalCount` and no deep-pagination cap, so a per-category walk can page
to the API's own last page; the runner nominates the rows such a finished walk did not see for
a page check (rule #3, source-scoped) and the page decides. NOTE: bezrealitky
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
same head was re-walked while 8 of 10 categories were never touched at all. A category NOMINATES only when **every**
slice was walked *and* each one ended on idnes's own last page; one `deadline`, `error` or
`ceiling` — a stop of ours — holds the whole category open. Since 2026-09-08 the union no longer
has to satisfy the national declared total (rule #3): that arithmetic is the ledger's `outcome`
and the runner's `COVERAGE` alarm, not the gate. A slice one row short of its declared count has
still reached the end of the index. An empty slice publishes no count, so `total` is `None` — identical to a degraded
page — and is only accepted when idnes *says* it is empty ("momentálně tu není žádný inzerát",
`IndexPage.empty_confirmed`); ceskereality, which publishes no such string, has to confirm a zero
by reading the page twice instead.
**A slice that paging cannot finish DESCENDS, and the parent walk is kept.** idnes's result
ordering is not stable between requests, so pages of one query overlap and the loss compounds with
page count: `stredocesky-kraj` (67 pages) returned its declared 1,675 exactly, `praha` (154 pages)
returned 2,948 of 3,839 — 27% of slots were repeats. **Place** — the site's own
hierarchy — is the ONE axis: a kraj links its okresy, Prague its ten obvody, abroad one `s-l` per
country, and those 38 countries sum *exactly* to the abroad total. (A price-band fallback for a
place with no sub-places at all, e.g. Spain's 8,613 flats over 345 pages, was tried and REMOVED —
idnes's price filter cannot paginate, so it burned hundreds of requests for nothing;
`tests/test_idnes_main.py::test_place_is_the_only_descent_axis` pins its absence.) Place is not a
partition — 60 Prague listings are too vaguely addressed for any obvod — which is precisely why
the parent's own rows are merged with its children's rather than replaced: the unfiltered walk is
what holds the remainder. Measured:
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
right, but because a wrong verdict cannot execute**. Since 2026-09-07 the flag no longer gates
anything: a complete walk nominates its unseen rows for a page check and the drain's fetch
decides (rule #3), so the gate is a posture signal for Health and the ledger is its evidence;
`delist_flip_cap` throttles how many checks one walk may queue rather than refusing a sweep.
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
`Družstevní→druzstevni`, `2+1`). The index is TEN per-(sale type, property
type) feeds (`/nemovitosti/{prodej|pronajem}/{byty|domy|pozemky|komercni-objekty|ostatni}/`,
`?page=N`, 12 cards a page), each declaring its own result count in the page's Vue SSR
state (`metadata.count`); the five prodej counts sum to the prodej total, so the ten
partition the portal exactly. Until 2026-09 the walk paged the bare `/nemovitosti/` feed
as "a single mixed index with no total" — live-verified false on both counts: that feed's
own count IS the prodej total (rentals never appeared in it, so 1,518 were never scraped)
and every per-type page declares one. Each category's ledger row (one per category,
`slice_key='national'`) still records the `walk_is_complete` arithmetic against its declared
count, but since 2026-09-08 what authorises NOMINATION is the structural verdict — the feed
paged to its own last page with no stop of ours — and the source- and category-scoped page check
decides each row (rule #3; the 12 h staleness rail went with absence-based delisting). The
coverage gate (migration 455) flips `supports_complete_walk` from that evidence
(migration 481 set the ten descriptors). A gone detail fetch still flips a single
listing immediately (`mark_listing_inactive_native`).
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
(`Cihlová→cihla`, `Velmi dobrý→velmi_dobry`, `Osobní→osobni`, `2+kk`). **remax spells the
parcel "Plocha parcely"** — `areas_from_params` read `plocha pozemku`, the label the other
portals use and that NO remax page carries (0 of 13,806 stored rows, against 4,339 under
the real one), so `estate_area` was NULL portal-wide and land rows took their headline from
the title fallback instead of their own measure. Fixed in the scraper track's W20
(2026-09-17): one key, no dead fallback, and the plot reaches `derive_headline_area(plot=)`
as on every other portal — a dwelling keeps its own headline with the parcel beside it in
`estate_area`, land stamps `area_basis='plot'`. Like maxima,
the index is TWO mixed indexes — sale (`?sale=1` prodej) and rent (`?sale=2`
pronájem), `?stranka=N` paging (21/page) — with no per-category URL; each config
descriptor pairs a category with its offer-type flag and `walk_category` walks (or
reuses, via the agenda cache) that agenda once and keeps the title-derived slice for
its category (giving the runner real (cm, ct) Health-reconciliation labels). The
drain re-derives each listing's category from the detail page ("Typ nemovitosti" +
title verb). `supports_complete_walk=true` (`scraper/portal.py`) at AGENDA grain: remax reports a
per-AGENDA total and the per-category slice is title-derived — not a portal-reported per-(cm,ct)
total — so the per-AGENDA walk nominates the whole agenda's unseen rows for a page check once,
scoped by category_type (rule #3). Because that per-category `declared_total` is `len(seen)` by
construction, the runner's `COVERAGE` warning is blind here and `remax_main.presence_candidates`
logs its own agenda-grain `COVERAGE agenda …` line instead; a gone detail (404/410 or a
redirect off the detail path) flips that one listing inactive. Registered as a
scraper portal by CONVERTING the existing on-demand-parser row (migration 135). NOTE:
remax ALSO has an on-demand URL parser (`scraper/source_parsers/remax.py`, LLM,
`source_kind='remax'`) used by the estimation preview — a separate entry point
unchanged by the scheduled scraper, routed by domain in `source_dispatcher`
independent of the `portals` row's `kind`.

**Data source (ceskereality.cz).** A scheduled scraper (`scraper/ceskereality_client.py`,
`ceskereality_parser.py`, `ceskereality_main.py`) tagged `source='ceskereality'`. It is large
(~49k listings), so — like sreality/idnes — it is **cadence-split**: `ceskereality_index_walk.yml`
(every 6h, full walk + presence nomination + enqueue) feeds the hourly bounded
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
total ("Máme tady N…") with no deep-pagination cap, so every kraj slice can be paged to its own
declared tail; a category all of whose slices reached that end nominates its unseen rows for a
page check (rule #3, source-scoped) even when the declared counts add up short — which is the
whole point of the 2026-09-08 structural gate, since one 87-row Karlovarský slice collecting 86
had been vetoing a 20,964-row category. The detail URL carries the category, so the drain derives each listing's category from
its own URL — one config (the `portals` row, migration 249) walks all 12 (cm × offer-type) descriptors.
The client uses an honest identifying `User-Agent` at a polite rate (the site disallows generic bots in
robots.txt — an operator-owned posture). NOTE: ceskereality ALSO has an on-demand URL parser
(`scraper/source_parsers/ceskereality.py`, LLM, `source_kind='ceskereality'`) used by the estimation
preview — a separate entry point unchanged by the scheduled scraper.

**Data source (reas.cz sold transactions) — NOT a portal.** The tenth source is not a tenth
portal: it is a feed of **registered sales**, and a sale is an account-less external FACT, not a
listing. Its north star: one row per sale under the sale's own cadastral identity, fetched per
municipality cell and only where the deal pipeline has a live card, read through ONE SQL
definition — never a `listings` row, never linked to a `property`, never mixed with asking
prices, never adjusted, never shown without saying where it came from and when we last looked.
It therefore has its OWN store (`sold_transactions` + the append-only `sold_transaction_fetches`
ledger, migration 542) and touches none of the listings contract: no `listing_snapshots`, no
`is_active`, no `listing_detail_queue`, no `portals` row, no `images` rows (photos are hot-linked
from the source's public URLs), no `property_id` — rules #2/#3/#15/#19 are about listings and do
not reach here. What it DOES share is the vocabulary: the attribute columns carry the same names
and types as their `listings` twins, so `measure_price_per_m2` / `plot_area_m2` and
`scraper/area.derive_headline_area` apply with zero new code (rules 21/23).
Ingest shape: the SSR HTML of `https://www.reas.cz/prodane/...` carries the whole page in
`<script id="__NEXT_DATA__">` → `props.pageProps.adsListResult`; `scraper/reas_parser.py` is a pure
payload→rows function (no I/O, no `requests`). The natural key is `mapPointerId`
(`<transferId>_unit_<buildingId>-<čp>-<unitNo>` or `<transferId>_building_<buildingId>`) — the
cadastre transfer id, which survives the source re-creating its own ad record. Three refusals, each
a measured silent-failure class: the `_next/data/<buildId>/prodane/…` JSON route answers **200 with
the ACTIVE catalogue** and no sold price on any record (`adsListParams.linkedToTransfer` is THE
discriminator and is checked before a row is read); a record whose transferId is neither a number
nor an ObjectId has no cadastral identity; a `type` outside flat|building is a contract change (the
sold catalogue is byty + domy only, proven by the sold sitemap, by zero parcels in 308 records
despite the query asking for them, and by the source's own four-member filter enum). Records whose
transferId is a Mongo ObjectId are the source's **self-reported** ~1%, so they are dropped and
counted, not stored and not raised on. The envelope is read as the source's own two numbers:
`count`, the cell inside the query's date window (what a completed walk takes, so it can never
measure what we miss), and `possibleCount`, the same cell without it — the second is what the
ledger's `source_total` records, so the table can always say how much it is NOT seeing (Olomouc 89
of 625, Praha 1,082 of 7,545).
**Deliberately absent, and each for a reason that has been measured:** `displayArea` (the source's
own headline, `min(utility, floor)` on a flat against our usable-first precedence — a 30% area and
43% per-m² gap on 3% of flats, in one direction), `histogramPrice` (`soldPrice` indexed to today:
identity within 12 months, ×1.10–1.28 beyond), `originalPrice` (corrupt — one record carries 1 Kč),
and seller/broker identity (dropped at parse time by key SHAPE —
`seller|company|agent|broker|contact|phone|email|owner|user` — not by a list of today's names,
because `raw` keeps every other key the source invents and would otherwise quietly start storing
the next one). None of the four is in `raw` either, so a future session is never one mapping away
from the defect. Two more: there is no `price_kind` column (asking
vs realized is PROVENANCE, and the table identity is the discriminator) and no widened
`DISPOSITION_OPTIONS` — the source's `larger` and `atypic` become NULL rather than push two
sold-only values into Browse, the watchdog matcher and the comparables agent for 0.68% of one
source. `mapPointerPublishedAt` (= `soldAt` + 27–31 days) is the only correct crawl watermark: a
sale is a state change on an arbitrarily old ad record, so the freshest sale the feed can show is
~30 days old.
**The fetch unit is a municipality CELL, and the deal pipeline is what makes a cell worth fetching.**
The box is the obec's `admin_boundaries` envelope widened by `sold_db.MAX_READ_RADIUS_M` (5,000 m —
the read surface's largest radius, so any subject inside the obec is covered out to its full radius
by construction, with no second fetch). `admin_boundaries.id` IS the ČÚZK/RÚIAN code of the unit
(migration 017; `sreality_id` is the separate bridge into sreality's own id space), so it joins
directly to `listing_location.obec_kod` and to the source's `municipalityId` — three spellings of one
number. The work-list (`sold_db.sold_comp_cells`) is a QUERY, not a queue: the obec cells of
properties carrying ANY account's pipeline card at a non-terminal, non-archived stage, active,
`byt`/`dum`, with a resolved point, **and an `admin_boundaries` polygon** — DISTINCT over accounts,
minus the cells whose NEWEST `sold_transaction_fetches` row is `ok` within 35 days or `failed` within
6 hours. That last join is load-bearing: a cell with no polygon cannot be boxed, so a fetch can only
skip it, a skip writes no ledger row, and `ORDER BY fetched_at NULLS FIRST` would then re-offer it
first on every pass for ever. Terminal stages leave the REFRESH, never the READ: stopping the
re-fetch is the politeness lever, and a closed deal is exactly where the stored comps must survive.
`sold_fetch.fetch_cell` never raises — every cell attempt ends in the ledger, because a cell that
failed silently is indistinguishable from a cell that holds no sales — a page the parser REFUSES
fails the whole cell rather than storing 90% of it (a half-truth in a fact table that nothing could
later audit), and an `ok` row whose walk was cut short by the page cap or a non-advancing `nextPage`
says so in `error` (`truncated: took N of M in P pages`), so "we looked" never silently means "we
looked at part". Politeness: one request per five seconds on the shared `portal_rate_state` ledger,
an identifying User-Agent, `listPerPage=100`, ONE retry (403/429 are retryable in `portal_base`, and
a 35-day-TTL fact feed does not knock four times), and a walk that follows `nextPage` alone under a
25-page runaway cap — Praha, the one cell measured to paginate at all, is 11 pages of its BARE
envelope, while a cell sends that envelope +5 km, which is why truncation is recorded rather than
assumed impossible. The scheduler is the `sold_comps` worker lane, dark behind one integer
(`realtime_sold_comps_interval_seconds`, migration 544, seeded 0) that is cadence AND kill switch: no
boolean flag, no env var, no workflow YAML.

**Read surface (migration 545).** ONE SQL definition reaches the browser. The SALES get a
definer-style view, `sold_transactions_public` (the base table is RLS-deny-all, so the single
`grant select … to authenticated` on it IS the dissemination switch), and over it a SECURITY INVOKER
`language sql stable` single-SELECT function with NO `SET` clause — so the planner INLINES it and
PostgREST's filters / ORDER BY / LIMIT reach the `(geom::geography)` GiST index (migration 537's
contract; migration 109 is the anti-pattern — never an optional filter parameter here).
`sold_comparables(p_lat, p_lng, p_radius_m)` returns the sale's columns plus `distance_m`,
`sold_age_days` (which is what makes the date filter a plain integer `.lte`) and migration 425's
`price_per_m2` + `price_per_m2_basis`. The fetch LEDGER gets no view at all: a cell is fetched only
where some account holds a live deal-pipeline card, so the set of fetched cells is a projection of
tenant state, not market data — `sold_transaction_fetches` is registered in
`tests/test_migration_rls_grants.py::_ADMIN_ONLY_RELATIONS`. Its one reader is
`sold_coverage(p_lat, p_lng)`, SECURITY DEFINER and scoped to the MUNICIPALITY containing the point,
resolved through the same `admin_boundaries` obec polygon W2 builds the cell from: the cell is that
polygon's envelope expanded by 5 km, so overlapping boxes would otherwise answer with whichever town
happened to be walked last. It returns that obec's name, its newest successful fetch (`fetched_at`,
`record_count`, `source_total`) and its newest attempt of ANY status, so the surface can separate
four answers — never looked, tried and FAILED (our outage, not operator inaction), looked and found
nothing, looked and hold N. A radius control, a filter panel and a "no registered sale matches these
filters" line all say WE LOOKED, so over a town nobody has fetched they contradict the coverage
sentence directly above them and are withheld — unless the cohort itself came back holding sales,
which is the store answering for itself. The cohort READ is never gated on coverage: `sold_coverage`
answers about the one obec containing the point while `sold_comparables` is a radius query over
every sale we hold, and a fetched cell is that obec's envelope plus 5 km, so the store routinely
holds sales around neighbouring towns whose own coverage row is still NULL. That sentence advises a
pipeline card only where the fetcher would act on one: the work-list takes live stages only (`NOT
ps.is_terminal`), so a card closed into a terminal stage is told to move it rather than that its
town is on the list — and where no obec resolves at all, nothing about the work-list is knowable and
no advice is given. The filter vocabulary is `Agenda.SOLD` (existing area / category / disposition defs
re-tagged, plus `max_sold_age_days`, bounded 60–730 by the source's own ~30-day publication lag and
24-month window), dispatched to PostgREST by the shared `applyAgendaFilters` with no hand-coded
escape. `subtype` is deliberately NOT in it, and Type offers `byt` / `dum` only: reas publishes
flats and houses and the parser refuses the rest, so every other option is a cohort that can only
ever be empty — the block narrows the registry's OWN option list through FilterForm's existing
per-filter widget override rather than growing per-agenda option machinery. The SPA reads it in
`frontend/src/components/listing-detail/SoldCompsBlock.tsx` — the listing page's only REALIZED
prices. `record_count` and `source_total` render as what they are, two populations (the source's
24-month window against all-time) and never as a shortfall; the ~30-day lag and reas's minority
match of the register are on screen; and the headline median holds out the 0–30 m² band, whose
Kč/m² is a denominator defect rather than a market fact. The reas.cz and Cenová-mapa outbound chips both stay beside it: the
table holds only reas.cz's anonymous 24-month window. Waves and sequencing: `roadmap/sold-comps.md`.

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
  `listings_public` / `properties_public`, `browse_stats_properties`,
  `health_summary`). All RPCs are `SECURITY INVOKER` and
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
  + a save-to-collection control (rule #18 — the SPA header's "Uložit do kolekce": a checklist of
  every collection, monitored first) + **operator notes** (list existing + add a new
  one via `GET`/`POST /properties/{id}/notes`, property-grain, the viewed advert recorded as
  the note's `origin_listing_id`) + an "Otevřít v aplikaci" deep-link to the SPA
  (`{VITE_APP_BASE_URL}/listing/{source}/{native}` — an advert alias that lands on the
  property page with that advert's row open) + subject facts; for sale apartments it ALSO
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
  browser can't resolve non-sreality listings directly). Badges come only from a successful
  lookup; a failed one raises ONE page-level notice (bottom-LEFT corner — the panel owns the
  right one — in a closed shadow root, shown only while cards the URL doesn't rule out as sale
  apartments wait on it): a sign-in button when signed out (that 401 used to leave search pages
  silent, read as a broken extension — 2026-09-24), "Obnovit stránku" when the extension was
  reloaded / updated / disabled / removed under the open tab (the overlay then stops scanning),
  the error + "Zkusit znovu" otherwise (no button for a build without an API URL). × lasts for
  the page (a soft-nav round trip through a listing keeps it hidden). Automatic re-lookups back off 60 s, `visibilitychange` re-asks at once (a sign-in made in
  the panel or another tab heals the page — the content script never receives the session; the
  sign-in button asks `get_auth_state` first and skips the Google round trip if one exists); one
  lookup is out per generation (a sign-in or the retry starts a new one, older answers are
  dropped, an older `ok` still badges); a lookup unanswered after 20 s shows as failed but a late
  `ok` still badges;
  `stop()` removes the notice on route change. `src/portals.ts` is the single source of truth for
  host→portal + detail-URL→native-id. One entry per Vite pass since #1524 (`npm run build` runs
  `vite build --mode content` then `--mode background`: `content.js` a self-contained IIFE
  classic script with `index_overlay.ts` bundled in, `background.js` ESM) plus a copied-over
  `manifest.json` and `icon-{16,48,128}.png`; output lands in `chrome-extension/dist/`.
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
- **Location:** #25 (#24 is folded into it; full as-built: § Location data)

1. **The schema in `migrations/` is append-only.** Never modify an existing migration.
   Schema changes go in a new numbered file (`002_*.sql`, `003_*.sql`...) and are applied
   via the Supabase MCP. See "Database access" for the full flow and the
   additive-vs-destructive policy.
2. **Snapshots on content change only.** Never insert into `listings` without computing
   the content hash and inserting into `listing_snapshots` if it differs from the most
   recent snapshot for that listing.
3. **Never delete listings.** Listings that disappear get `is_active=false`. History is
   sacred. **Since 2026-09-07 the flip is PRESENCE-VERIFIED, not inferred from absence.** A
   complete category walk (`portal_runner.run_index_walk` → `_queue_presence_checks`) queues
   every active row it did not see into `listing_detail_queue` at `QUEUE_PRIORITY_VERIFY`
   (served after new and changed listings); the drain fetches the page and only a POSITIVE gone
   signal — 404/410, a redirect off the listing, the portal's own "no longer active" text,
   raised as `ListingGoneError` — flips it (`mark_gone` → `mark_listing_inactive_native`);
   a live page refreshes it, an error leaves it for the next pass. Why: absence-based sweeps
   needed a staleness rail, a national cross-check and a latching cap to be safe, and even so
   parked two portals for weeks (ceskereality's rentals could never reach the national count
   because listings filed under no region are in no regional list). A nomination cannot be
   wrong — it costs one fetch — so the rail and the cross-check are gone, `supports_complete_walk`
   no longer gates anything, and `delist_flip_cap` throttles how many checks one walk may
   queue (oldest-unseen first, the rest deferred and recorded in `delist_flip_refusals`).
   Nomination is the shared default (`db.presence_candidates`, keyed on `source_id_native` or,
   via `seen_key`, on `sreality_id`); a portal whose index sections do not map 1:1 onto a
   category overrides `presence_candidates` (bazos: subtype scope; ceskereality/realitymix:
   sibling-slice union; remax/maxima: agenda grain). What a finished walk still means: a
   partial walk (`--limit N`, `--max-pages`, a deadline) cannot know which rows it never
   reached, so it nominates nothing.

   **2026-09-08 — the nomination gate became STRUCTURAL; the count stopped voting.** Until this
   date the 5th element of `walk_category` was arithmetic: `walk_is_complete(collected,
   declared_total)`, ANDed across every slice a category is split into. On a category split into
   many units that AND can never pass, because it asks every unit to reconcile simultaneously
   against a live, jittering index. ceskereality's houses-for-sale is 20,964 rows across 14
   regions; the Karlovarský region declares 87 and the walk consistently collected 86, so
   `0.9885 < 0.995` failed that one slice and the whole 20,964-row category nominated NOTHING —
   for as long as one small region stayed one row short. (The category-level arithmetic would
   have passed: 20,963/20,964 = 0.99995. It was the per-slice AND that bit.) The fix is to stop
   asking "how much did we collect?" and to ask **"did the walk reach the portal's end?"**:
   `reached_end` := every unit the category is defined over was walked, AND each unit's page loop
   exited on a PORTAL terminator, AND no stop of OURS fired anywhere in the category. The
   vocabulary is shared (`scraper.portal.StopReason` + `PORTAL_ENDS` / `OUR_STOPS` /
   `stop_is_portal_end`) and the conjunction is spelled once (`walk_reached_end(portal_end=,
   our_stop=)`), so the flag means one thing on all nine portals. PORTAL terminators: `pager_end`
   (a page with items and no next page), `declared_total_reached` (the portal's OWN count, never
   our ratio), `short_page`, `empty_confirmed`, `clamp_repeat` (a past-the-end request the portal
   clamps back onto its last page — the weakest signal, and one no live probe has yet observed:
   the 2026-09-08 probe found remax / mmreality / maxima / bezrealitky / sreality answer past the
   end with 200 + an empty list and realitymix 404s, while idnes — the one portal that DOES clamp
   — does not use it. So it survives only on **maxima** and **realitymix**, corroborated by
   position (at or past the page the declared count implies) and a strict-subset id check at
   page >= 2; where the premise was unproven the same shape is now `pager_stalled`, a stop of
   ours: remax and mmreality break their loops on it but nominate nothing).
   OUR stops, any one of which vetoes the whole category: `deadline`, `page_cap`,
   `limit`, `slice_subset`, `slice_unreached`, `error`, `pager_stalled`, `cap_wall` (sreality's
   HTTP 422 deep-pagination refusal, previously swallowed by the client and now exposed as
   `cap_hit`), `barren`. Two disciplines make the flag honest, and both are mandatory on every
   portal. **ITEMS-FIRST**: a loop that breaks on `not items or next is None` must test `not
   items` FIRST, because a blocked or throttled HTTP 200 also carries no pager and would
   otherwise exit through the same branch as the genuine last page — that conflation is exactly
   what the numeric gate was silently defending against, so a structural flag that skips the
   split is a safety regression. **THE BARREN RULE**: a page with zero items is `barren` (OURS)
   until proven otherwise — re-fetch the same URL once through the limiter, and only if it is
   still empty AND at or past the position the declared total implies (or no total was ever
   readable and an earlier page of this unit carried items) does it become `empty_confirmed`. A
   confirmation that cannot be obtained is not a confirmation, and a unit whose FIRST page is
   barren with no declared total is never confirmed. What this trades away, deliberately: the
   count no longer vetoes, so a walk that ends on a forged or misrendered terminator will
   nominate its whole unseen remainder. That costs FETCHES, never rows — every nominated listing
   is decided by its own page, and `delist_flip_cap` throttles per walk (but mind its FLOOR: the
   cap only engages once a scope holds `min_rows` = 2,000 active rows, so a small scope — maxima's
   ~220-row agendas, sreality `pozemek/drazba`, a bazos subtype scope — is unthrottled and one
   walk can nominate all of it; the "~10% per walk" bound describes the big scopes only).
   So a terminator has to be genuinely HARD TO FORGE, and that is a per-portal obligation, settled
   against the live per-portal probe of the last page and the page past it (2026-09-08) — the
   suspect page never supplies its own corroboration (a count is latched only from a page that
   carried items, and only upward within a walk); `next is None` is never read as an end where it
   also means "no pager rendered" (ceskereality reads its own `--disabled --next` arrow, maxima its
   rendered pager, bazos corroborates a full page with one fetch of the next offset, mmreality —
   whose last page still over-advertises `<link rel="next">` — walks past it to the items-less page
   that IS its terminator); an offset API needs a new-id progress guard (bezrealitky); a clamped
   page SIZE is not a `short_page` (sreality adopts the `pagination.limit` it was served); and
   `clamp_repeat` survives only where clamping is plausible (maxima, realitymix), everywhere else
   a repeated page is `pager_stalled`, ours. Per-portal detail: the `scraper-ops` skill's
   `references/coverage-and-delisting.md`. The detectors that
   replace the veto are the runner's `COVERAGE` warning (logged whenever a walk nominates with
   `walk_coverage != "complete"`), the two facts now recorded per category in
   `scrape_runs.by_category` (`walk_reached_end` + `walk_coverage` — JSONB, no migration), and
   `scripts/verify_pipeline.py`'s coverage check. Those must not be silenced.

   The numeric verdict is unchanged and still runs every walk — it just no longer gates. It
   drives ceskereality's descent trigger, idnes's resample trigger, sreality's national-fallback
   trigger and verify_pipeline's health check: **`scraper.portal.walk_coverage`, the ONE
   definition for all nine portals, and it has three outcomes, not two: `complete` / `incomplete`
   / `unknown`.** It replaced eight byte-identical private copies that FAILED OPEN — `if not total:
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
   categories (delistings then accumulated for 11 days). The old second rail — flipping only
   rows additionally unseen for 24h+ (`min_unseen_hours` on `db.mark_inactive` /
   `mark_inactive_native`) — was retired on 2026-09-07 together with absence-based delisting: a
   page check needs no staleness window, because it does not infer. A false flip still self-heals
   on the next index sighting (`touch_listings` reactivates).
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
   never breaks the scrape. sreality photos are fetched through their `SQUARE_1800_JPG` template
   (whole frame, ≤1800px, no watermark) and a legacy chain on a stored URL is NORMALISED onto it at
   download, never passed through — see § Data source (sreality) for the allowlist mechanics.
   Migration 496 adds provenance, and **every stored row is stamped with what was actually stored**:
   the download path passes `rendition` + the decoded `stored_width`/`stored_height` into
   `db.mark_image_stored`. `rendition` = WHAT bytes the object
   holds (NULL pre-provenance — for sreality the legacy 749x562 mode-3 crop, else the portal's
   native file; `sreality-1800-fit` the uncropped master; `sreality-749-crop` assessed/source
   gone/crop retained, TERMINAL; `native` a non-sreality file), plus `stored_width`/`stored_height`
   measured at upload. A job that REPLACES an object's bytes under its existing `storage_path`
   must go through `db.invalidate_derived_signals` — the one chokepoint that re-arms the phash +
   CLIP lanes by nulling their own predicates (`phash`, `clip_tagged_at`) and deletes no label,
   review or CLIP-cache row. **Outside the download path itself, the ONLY job that replaces stored
   bytes is the re-master lane** (`scripts/remaster_sreality_images.py` / `sreality_image_remaster.yml`):
   it overwrites the object under the row's existing `storage_path` and stamps `rendition` +
   dimensions through `db.mark_image_remastered`, which runs that invalidation in the same
   transaction — so no other job may rewrite an object without going through the same pair.
   Two properties of that lane matter outside it. *Every* outcome stamps `last_download_attempt_at`
   (the pending set's only anti-thrash rail), and RETIREMENT — claiming `sreality-749-crop`, which
   is irreversible for the lane — always takes two sightings 20h apart. And because a stored URL that
   404s is re-resolved from the live detail **by image `order`**, the lane inherits the platform's
   image identity: a row is `(listing_id, sequence)`, a POSITION, so a reordered gallery re-points it
   onto whatever photo now sits at that order — exactly as the `(listing_id, sequence)` images upsert,
   the detail drain and `refresh_stale_images` already do. Labels keyed on `images.id` follow the
   position, not the photograph; that is a property of the upsert key, not of this lane.
   **Two consequences of the template switch, both live until the re-master lane finishes.**
   (a) *The visual-signal corpus is MIXED-RENDITION.* dHash and CLIP are framing-sensitive, so the
   same photo stored as the old 4:3 crop and as the whole frame yields materially different signals:
   **phash/CLIP are comparable only WITHIN a rendition**, and any cross-row query (the duplicate
   census, the new-dedup rebuild, a Hamming join) must group or filter on `images.rendition` — NULL
   and `sreality-749-crop` are the crop cohort, `sreality-1800-fit` the master cohort. Newly
   downloaded rows widen the split without passing through `invalidate_derived_signals`, which only
   covers rows a re-master rewrites. `scripts/tagging_bakeoff_arms.py` calibrated its resolutions
   against the old 749px stored width — re-read that comment against the rendition mix before
   trusting a cross-era comparison. (b) *Stored objects get several times larger and there is no
   thumbnail rendition yet*, so every surface (Browse cards, lightbox, comparables, the extension)
   serves a master into a thumbnail-sized `<img>`: more R2 egress and page weight, the accepted
   price of not having lost the photo's edges. A derived small rendition is sequenced on the scraper
   track. Downstream, the image phase's bound is wall-clock (`--image-max-seconds`), not the count
   cap — per-image cost moved, and a count cap calibrated against the old rate overruns the CI job
   timeout into a SIGKILL that skips finalize.
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
    real-world property across portals. `properties` holds the canonical advert's display row
    plus derived rollups (`source_count`, price-change aggregates, lifecycle `is_active` /
    `first/last_seen_at`), written by ONE recompute (`scripts/recompute_property_stats.py`): the
    property-maintenance job (rule #20 for the dirty-set cadence) and, per listing, the ingest
    path's `_ensure_property`. `is_active` /
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
    **One property, one voice (W4, migration 561, decision 18).** A property speaks with its
    CANONICAL advert, rank 1 of `property_canonical_listings(property_id)`: active first, then
    `source_trust_rank`, then the most recently seen, then the lowest id. ONE RULE PER FIELD:
    every advert field (price and ITS OWN `listing_price_steps` history, area with no fallback,
    layout, category, subtype, source, condition with both derived levels -- rule #14 --,
    furnished) is the canonical advert's, and `repr_listing_ref_id` names it for every read model
    (`properties_public.listing_id` IS it); every physical fact (building type, ownership,
    energy rating, amenities, estate/usable/garden area, parking) is the first non-empty value
    in the same order. A property is born one way, `scraper.db.NEW_SINGLETONS_SQL` (a bare row
    linked in the same statement) then that recompute: on ingest (`_ensure_property`) and in the
    straggler-attach alike. A re-scrape of a linked advert keeps the singleton mirror
    (`_cheap_property_rollup`: counts and lifecycle, the one advert's fields while a singleton)
    until the full recompute is measured no slower there (the W4 latency gate).
    `all_sources` / `active_sources` (never written) left the read model; the physical columns
    are W8's destructive drop (the SPA never read them).
    **The SPA shows it one way (decision 11): ONE property page, `/property/:propertyId`**
    (`frontend/src/pages/PropertyDetail.tsx`). Its header is the `properties_public` row
    (`PROPERTY_COLS`, pinned to the view by `tests/test_property_page_read_contract.py`) -- the
    facts the Browse card shows -- with the canonical advert's photos, broker, own price series
    and freshness checks; the price-change figures are the row's, as Browse filters on them. The
    merged-adverts section is the ONLY list of adverts (a singleton's one advert included), each
    advert's own facts and stored portal link in its row. Every property-grain surface links
    `propertyPath(property_id)` (`lib/listingUrl`). Old advert addresses -- `/listing/{source}/
    {native}` (emails, the extension), `/listing/{sreality_id}`, `/listing?property=` -- are
    aliases (`AdvertRedirect`) that resolve the advert's property and open its row
    (`?advert=`; ignored for the canonical advert), `?run=` and the hash preserved; a merged-away
    property id follows its survivor through `GET /properties/{id}/origins`. Gone: the per-portal
    chips, the "current active listing" jump, the history block's URL list, the price-mismatch
    note and the repr-resolving `?property=` redirect.
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
    accumulate in Browse until the new engine ships — that build-up was accepted explicitly
    (the one engine path since, AUTODEDUP's worker lane, is dark; see "Who orders a merge").
    The **publication gate is gone**: since migration 273 a new property stayed invisible in
    Browse/map/stats/watchdogs until something stamped `published_at`, and the only stamper for
    ordinary properties was the old engine, so leaving the gate up would have hidden the entire
    market. The gate was flipped inert first (`dedup_publication_gate_enabled=false`), then its
    code removed; the watchdog matcher's "new property" cursor is re-anchored on arrival
    (`listings.first_seen_at`), which is what it keyed on before migration 273. **Migration 475
    finished it:** the predicate is out of `properties_public` / `browse_projection` /
    `listing_feed_public`, and `publication_gate_enabled()` + `publication_gate_health_public`
    are dropped.
    `properties.published_at` / `publish_reason` are kept frozen as a historical record. The
    legacy decision ledger (`dedup_pair_audit`), the manual-feedback and golden-pair tables, and
    the paid LLM verdict caches are **frozen, not dropped**: no code writes them and the new
    design never reads them. The engine's queue/state tables went in that **same migration 475**,
    after operator confirmation and a `pg_dump` to R2: `property_identity_candidates` + archive,
    `dedup_dirty_properties`, `dedup_scan_state`, `dedup_batches`, `dedup_batch_requests`,
    `dedup_engine_runs`, their six admin views, and the unused migration-127 eligibility index.
    `property_merge_events.generation`
    stamps `'legacy'` on every pre-cutoff row so the future engine's merges (`'v2'`) are
    distinguishable. The blocking keys `listings.street_name_key` and `geo_cell_key` (+ their
    triggers) were dropped by W4-c (mig 508) with the rest of the legacy location store; the
    rebuilt engine blocks on `listing_location.obec_kod` instead (Path C).
    **Standing rule: the removed code, its comments and its design docs are never consulted
    again for any purpose.** They survive in git history and on branch
    `backup/pre-new-dedup-2026-08` for forensic recovery only. The operator owns all
    merge/no-merge logic in the rebuild; thresholds, weights and rules are not to be invented.
    **The link mechanics: one merge, one undo.** `toolkit/property_identity.py` is the single
    chokepoint. `merge_property_set` is the ONE merge (operator and engine alike): it refuses a
    non-active property, a category clash between ANY two members, two DIFFERENT asset links,
    or (the engine only) two linked units of one asset (`AssetLinkConflict`), keeps the OLDEST
    record (`first_seen_at`, then the lowest id — decision 17, `survivor_of`), merges the rest
    through `merge_properties` under ONE `merge_group_id` in one transaction, and recomputes the
    survivor and patches Browse (`sync_browse_list`) once. `merge_properties` row-locks both,
    gates on `status='active'`, re-points `listings.property_id`, writes one
    `property_merge_events` row per moved advert, CARRIES the one asset link onto the survivor,
    carries operator state (rule #18), the pipeline (rule #22) and dismissals, and soft-retires
    the loser (`merged_away`). `detach_listing` is the ONE split and the ONE undo, per advert:
    back to its ORIGIN (the `prev_property_id` of its oldest live ledger row), reactivating that
    property with its pipeline card and carried asset link if merged away INTO that merge's
    survivor (else the advert stays: `origin_moved_on`, read under the lock), stamping its ledger
    rows `undone_at`/`undone_by` (never deleted), recomputing both once. An advert NO standing
    merge moved (an ingest-time grouping, ~15.9k `native_multi` properties) is, while ANOTHER
    such own advert stays, a BIRTH through the one birth path (`split_native`, the operator's
    only: any other source answers `propose_only`, decision 9): the property locked first and
    the plan re-read under the lock, then the advert unlinked and born by
    `scraper.db.create_singleton_properties` and both recomputed; ONE ledger row records it in
    the existing shape — the ingest grouping as the merge it amounts to (`survivor` = the
    property left, `retired` = `prev` = the new record) written already undone by the split (no
    migration) — so the new record IS the advert's origin, and a later merge of the two
    (operator or engine, `merge_property_set` as ever) comes apart by the same detach. A
    property's LAST own advert stays (`last_native`): the merged ones go home instead, so no
    detach, `unapply` loop included, can leave an active property with no advert. Operator
    state, the pipeline card and the asset link stay on the property left (rules 18, 22).
    Idempotent (`not_merged` = alone on its property; a group-scoped detach never births). One
    undo covers both kinds. A group comes apart as a
    loop of detaches scoped to it (`merge_group_id=`: only while that merge is the newest to
    move the advert, else a conflict left in place) — `unmerge_group`,
    `split_property_to_singletons` and their fix-up scripts are gone. Merge-then-detach gives
    back every original property and asset link in any order (one asset held twice in a chained
    operator merge excepted; tests/test_detach_listing.py, executed in
    tests/test_merge_safety_live.py). Callers serialize per-property on the row locks.
    **A merge writes no status event (migration 559).** The status-history trigger
    (migration 392) skips the retirement (`is_active = false` set with `merged_away`), and
    `property_status_events` is NOT carried onto the survivor: each property keeps its own
    activity log, so the survivor never charts two series as one. A detach restores
    `is_active` in the statement that clears `merged_away`, and the trigger logs that only
    where the property's own last row disagrees (a pre-559 absorbed property ends on the old
    merge's false 'inactive' and gets its 'active' back). **Apply 559 before its code merges**
    — the rollup and both notification producers read `listing_price_steps` with no fallback.
    **Category compatibility is enforced at the chokepoint** via the single
    `room_taxonomy.category_main_compatible` helper: a sale ≠ a rental (`category_type`), and a
    flat ≠ a house — **except** the ONE sanctioned cross-type **dum ↔ komercni** (the same
    building listed as a house on one portal and commercial on another is one real-world
    property, irrespective of sub-type). This guard is deliberately *at the merge*, not in the
    caller, so no future decision layer can route around it. It is distinct from the
    **asset-link** grain (migration 224), which links genuinely *different* units in one
    building (a `byt` and its ground-floor `komercni`, a `dum` and its `pozemek`) WITHOUT
    collapsing them into one property.
    **Who orders a merge today.** The operator — and, only inside the area its scope row
    names, the AUTODEDUP apply path below. The operator's path: Browse's `mergeMode` (checkbox
    multi-select → merge) posts to `POST /properties/merge`; `POST /properties/{id}/detach`
    (`{listing_id, reason?}` → `{listing_id, detached, outcome, survivor_property_id,
    restored_property_id, rulings_written}`; an advert no longer on it answers `detached: false`)
    sends one advert back; `GET /properties/{id}/origins` names each advert's origin; the ledger
    is `GET /properties/merges` (`api/property_merge.py`). **Every operator merge and detach is a
    ruling (decision 8)** (`toolkit.property_identity.record_rulings`, same transaction, only
    for `source='operator'`: an engine merge or `unapply` never is): the merge rules every cross
    pair of the ticked properties' CANONICAL adverts (`repr_listing_ref_id` — never a child the
    removed engine or ingest grouped there) `same`; the detach rules the advert `different` from
    every advert that stays, with the optional `reason` (max 500) — both into the review pages'
    store (`autodedup.verdicts` + operator `must_not_link`, `decided_by` = the admin's email).
    **Migration 560** copied the operator's live pre-ruling merges (362 groups) into `same`
    rulings — pairs that sat on different properties of a group (a side is an advert's origin)
    and share one now — `decided_by='operator'`, dated at the merge, never over an existing
    ruling or veto, so the engine can never undo them. Labeling / annotation CRUD that the old
    dedup page carried — training examples, border cases, image annotations, pHash pair notes —
    first re-homed under `/labeling/*` (`api/labeling.py`), then (docs/design/tag-annotation-matrix.md,
    2026-08) superseded: the confirmed-training-set half moved to a permanent, per-(image, tag)
    tri-state ground truth (`tag_taxonomy` + `image_tag_labels`, migration 442, managed under
    `/new-dedup/labeling/*`) that every independent per-tag classifier head will train from, and
    the old ClipAudit page (its only other consumer) was retired outright. `/labeling/*` now
    carries only border-case flagging (`image_border_cases`) — `image_tag_annotations` and
    `phash_pair_notes` had zero live callers even before the cutover. `image_training_examples`
    itself is superseded but not yet dropped (a separately-gated destructive migration).
    The unmerge *button* lived on the deleted Dedup page; its new home is the property page's
    **Sloučené inzeráty** section (`frontend/src/components/listing-detail/MergedAdvertsSection.tsx`:
    the page's only advert list, one expandable row per child advert — photos and the stored
    portal link collapsed, description / full gallery / broker expanded). For an admin
    session each expanded row also names its origin (`GET /properties/{id}/origins`: the property
    a detach returns it to, and the source and date of the merge that took it from there), and
    every row WITH an origin carries a two-step **Rozdělit** that calls
    `POST /properties/{id}/detach` for exactly that advert — any property size, a merge of any
    origin (operator, legacy `auto`, `autodedup`), the optional free-text `reason` kept on the
    "different" ruling — then re-reads the property page (keyed on the property) and refreshes
    Browse (`lib/mergedAdverts.refreshAfterDetach`). The property's own advert (null origin) has none.
    The page's former guess at which merge group a row came in with (a ledger scan plus a
    two-advert-only rule) and the group-grain unmerge it called are gone.
    **AUTODEDUP one lane (W5, dark: interval 0).** The engine's ONE production path is the
    always-on worker's `autodedup` lane (`scraper/realtime_worker.py`; its interval
    `realtime_autodedup_interval_seconds`, 0 = stop, is the only switch): one pass of
    `autodedup.incremental_lane.run_incremental` decides and groups the live `rt` generation —
    under the batch pass's D43 relation and the operator's rulings, a `same` pair ruling being a
    must-link and a `different` one a must-not-link — commits, and then RECONCILES production
    under its lease (`autodedup/reconcile.py`, PROGRAM.md E908–E915): the groups it re-clustered
    go through the apply path below (`apply.plan_groups` / `apply.apply_group`, the same
    refusals, the same chokepoint, `source='autodedup'`, ledger rows `generation='rt'`,
    `run_id='rt:<holder>'`), only inside `autodedup_apply_scope` and never as a split — a
    grouping the stream no longer supports is a proposal. The brake is the interval (0), then
    `mode=unapply`. Its calibration is cut from the database (`rt_seed`, and a re-cut when the
    pHash population drifts); a pass past its own deadline rolls back and halves its rate. The
    batch `apply` mode stays until the lane has run three live days and checkpoint C2 passes;
    `legacy_retire` until W8.
    **AUTODEDUP apply path (dark).** Merges may now ALSO be ordered by the AUTODEDUP engine
    (`docs/design/autodedup/PROGRAM.md` E900–E906) — through the same chokepoint, never around
    it, and only inside `app_settings.autodedup_apply_scope`, the ONE rollout control: a scope
    naming no deal types or no area merges nothing (migration 558 seeds it with no area), and
    it is re-read before every group, so emptying its area on /settings stops a running apply
    between two groups. `autodedup/apply.py` (lane modes `apply` / `unapply` in
    `.github/workflows/autodedup.yml`) reads one stored generation's groups, names the survivor
    by the one rule (`survivor_of`: the oldest record), and calls `merge_property_set`
    with `source='autodedup'` (migration 558 widened `property_merge_events.source`; that is
    the whole record of who merged), ONE `merge_group_id` per engine group inside one
    transaction, so each group is undoable as a unit (`mode=unapply`, newest-first, by
    generation, run or time window: a loop of `detach_listing` over the adverts the group's
    merge moved, from the placement its ledger row recorded; the dry run reads each detach's
    answer from `detach_outcomes`). A dry run is the default and writes only
    its own ledger, `autodedup.applied_merges`; a live run refuses — recording why — any group
    whose merge would unite, across EVERY listing it moves (both properties' full sets, not
    just the members), an operator
    negative (a pair or must-not-link with both sides inside, a group verdict with its whole set
    inside — any superset, under any key, the newest ruling per operator winning), mixed
    categories, a listing outside the scope — inside = LOCATED in a scope block by its live
    `listing_location` obec_kod / cast_obce_kod, never the engine's blocking key; out-of-scope
    groups are counted per reason — (and the merge's own refusal of two **asset-linked**
    properties is recorded as `asset_linked_units`), a non-active property, a
    property the engine split across two groups, or a listing no group holds (unless this
    engine's own live merge already put it
    there with a member). Inside each group's transaction the properties are locked `FOR UPDATE`
    and their listings `FOR SHARE`, and every one of those checks runs again over the locked
    rows before it merges; the live `rt` generation is refused here (the lane reconciles it,
    below), and a live run holds the lane's lease `autodedup.rt_lease` (one writer). An engine
    merge the operator took
    apart stays apart: its separated LISTINGS are never re-united by a later generation, even
    once the restored property has been merged into another one, and an `unapply` that finds
    the merge already partly taken apart records its undo as the operator's. `unapply` skips a
    group a later engine merge still builds on (more listings merged onto its survivor by a
    merge whose own undo is not refused for moving nothing back, or a retirement of its
    survivor that still stands) and names the merge to undo first — and only then: a group
    taken apart outside the engine, or with nothing left to move back (from the placement each
    ledger row records over its locked rows as it merges), is skipped naming nothing; a group
    someone else already undid (a retired property no longer merged into the survivor by its
    merge — told apart from a later hand re-merge of the same pair by `properties.merged_at`
    against the ledger's `applied_at`, both now() of the group's one transaction) is noted
    undone as theirs wherever its survivor went since; and the dry run reports each group as
    the live run would treat it; an undone group may merge again on a later apply (undo is a
    brake, not a ruling). **Splits are propose-only (decision 9):** `GET
    /autodedup/proposed-splits` (+ `/{property_id}`; `autodedup/proposed_splits.py`, read-only)
    lists each live multi-advert property a generation touches (the live `rt` stream unless
    one is named) with a pair STATED apart: grouped
    apart AND scored reject/veto/band or named by a conflict (in the live stream, a pair the
    lane holds apart with no stored row reads `below band`; in a batch pass a pair never scored is not spoken
    for, like an unseen advert), or carrying a stored negative; a pair whose newest ruling is
    `same` is never proposed (decision 8). Each pair carries its reason (conflict, else the
    pair's decision, else must-not-link, else `no stated fact` for a negative ruling alone) and
    the operator's ruling; the batch split is the detach per advert, each advert carrying its
    `detach_outcome` (what `detach_outcomes` answers now) and `splittable` (that moves it: back
    to its origin, or a native advert to a new record); `GET /properties/{id}/origins` carries
    both for the property page, whose rows that would not move say why (and link where a
    retired origin went). Its page is `/autodedup/proposed-splits` (AUTODEDUP menu,
    "Návrhy rozdělení", `frontend/src/pages/AutodedupProposedSplits.tsx`): a card per proposal
    with each group's adverts side by side (`MemberGrid`), the reason and ruling per pair, a
    checkbox, and a two-step "Rozdělit vybrané" (`splitPlan`): the group holding the property's
    own adverts stays (else the canonical advert's), and an advert leaves only when it is alone
    in its group (a detach rules it different from every advert left behind), is `splittable`
    and is stated apart from the staying group; the optional shared reason rides each ruling,
    with progress and a per-advert outcome. Group size is the engine's own cap alone. A group already on one
    property that the operator has since ruled different is reported, never acted on. It reads
    nothing from `property_merge_events`. Undo restores listings and pipeline cards;
    collections, tags and notes stay on the survivor (rule #18: a detach is best-effort).
    **Signal producers keep running** — they are the substrate the new engine will consume, and
    stopping them would leave a cold start: image pHash (`compute_image_phash.yml`), the
    self-hosted CLIP tagger and its embeddings (`clip_tag.yml` / `clip_retag.yml`, writing
    `image_clip_tags` + `image_clip_embeddings`), and the operator's labeling corpus
    (`tag_taxonomy`, `image_tag_labels`, `image_border_cases`). `listing_image_comparisons`
    (the agent-facing `compare_listing_images` tool) is unrelated to dedup and unaffected.
    A fourth producer now exists as infrastructure: the **DINOv3 embedding lane**
    (`dinov3_embed_backfill.yml` → `scripts/dinov3_embed_dispatch.py` launching a RunPod GPU
    pod that runs `scripts/dinov3_embed_backfill.py`, writing `image_dinov3_embeddings`) — the
    encoder the operator accepted on 2026-09-05 for the tag heads, Level-3 similarity and
    candidate path B (`docs/design/new-dedup/ENCODER-DECISION.md`). It is **dispatch-only and
    has never been run against real data**: manual `workflow_dispatch`, no schedule, and inert
    until the bake-off fills in `data/dinov3_config.json` — the loader refuses to run while any
    of the six facts that identify a vector (model, revision, library, pooling, resolution,
    preprocessing, dtype) is null. Those six are the target table's primary key, because any
    one of them changing means a **new population, not a new value**. The CLIP lane keeps
    running in parallel for comparison; nothing has been retired.
    A fifth producer is the **versioned tag model** (migration 490, `toolkit/tag_models.py` →
    `scripts/tag_model.py` / `tag_model.yml`): a promoted bake-off cell — one encoder, one
    training mode, one frozen head set — stored under a version name, at most one of which is
    `active`, whose per-image output lands in `image_tag_scores` as **every head's probability
    plus the ARGMAX winner** (ties toward the lower tag id, no threshold and no per-head yes/no
    stored — a consumer applies its own floor to `winner_score`; the operator's 2026-09-09
    ruling, ask 1 of five — the full list is in `docs/design/new-dedup/PROGRAM.md`'s
    `2026-09-09 (b)` ledger entry).
    Adding heads is a new version, never an edit, and `activate` is a separate step from `score`
    so no consumer ever reads a half-scored version. Nothing has been promoted or scored yet.
16. **Watchdog and Browse share one definition of "matches."** Saved watchdog filters live
    in `notification_subscriptions` (migration 056); the background matcher in
    `api/notifications.py` builds its WHERE clauses from the **same** logic Browse uses
    (`toolkit/comparables._shared_filter_where` + the shared `_city_quality_clauses`
    helper), so the two surfaces can never disagree on what a filter means.
    **Every surface reads the same canonical advert (migration 561).** Both watchdog producers
    and the collection monitor alert only on the canonical advert's own steps scraped after it
    became canonical (`properties.repr_since`, stamped by the rollup when the canonical advert
    changes: a merge, detach or delisting that hands the slot over replays nothing), and
    `_shared_filter_where` admits an advert only as its property's canonical advert and drops
    every advert of the subject's property (`exclude_listing_ids`, the one exclusion; decision
    13), so comparables, velocity and the corridor count each property once.
    **PLACE is the same rule (W3 S3, migration 504): ONE code predicate,
    `<level>_id = any(codes)`, plain equality per level.** A location chip is a LEVEL plus a
    RÚIAN CODE at four levels — `region_id` / `okres_id` / `obec_id` / `cast_obce_id` — and
    `api/location_filter.district_where` (the Watchdog), `frontend/src/lib/districtCodes.ts`
    (Browse's PostgREST string and the pipeline board's in-memory twin) and the two RPC bodies
    (`browse_stats_properties`, `browse_map_cells`) all compile the same plan; the shared table
    `tests/fixtures/district_chip_plan.json` is read by BOTH `tests/test_one_place_predicate.py`
    and `districtCodes.test.ts`, so a divergence is a red test rather than a support ticket.
    What this replaced was **five** predicates in **six** copies: obec/okres/region equality, a
    `locality` pair (obec equality AND `place_search_text ILIKE`), and a legacy name fallback
    ILIKE-ing across `district` / `place_search_text` / `okres` / `region` AND-ed with an
    optional parent-`context` narrow. Equality is enough because `listing_location`
    (migration 501) answers every listing with RÚIAN codes and `browse_list.obec_id` /
    `okres_id` / `region_id` already WERE those codes (`admin_boundaries.id` IS the RÚIAN code),
    so the swap is value-identical for a resolved row. Two consequences worth knowing: a
    street / POI / address chip now filters at its CONTAINING OBEC (there is no street-grain
    code to narrow with, and no text column left to ILIKE), and **a chip that carries no code
    matches NOTHING** — `NO_MATCH_CODE` (-1), which no positive RÚIAN code can equal. That is
    deliberate and fail-CLOSED: an include chip we cannot resolve must not widen a saved
    watchdog to the whole country. Chips saved before codes existed are resolved ONCE at read
    time against the RÚIAN name index (`api/maps.resolve_names`, shared by the matcher in
    process and by the SPA over `POST /maps/resolve-names`) — the stored blob is never
    rewritten, because a preset stores the operator's own full blob. The chip producer
    `/maps/resolve` PIPs the RÚIAN mirror for obec/okres/kraj; `cast_obce` has no polygon at
    any registry version, so the point places the obec and the NAME places the part inside it.
    Deleted with the ILIKE arms: the sreality-only `locality_district_id` /
    `locality_region_id` filters (registry, comparables, watchdog spec, API schemas, SPA filter
    types) — one portal out of nine could answer them, and `districts` at the obec / okres level
    says the same thing for all nine. **A stored spec that still carries one now WIDENS**: the
    request models ignore unknown fields (Pydantic's default), so an old watchdog filtering on
    `locality_district_id` silently loses that narrowing rather than erroring — deliberate (a 422
    on every save of a pre-W3 watchdog is worse), and the reason `districts` is the one place
    filter left to re-pick. Two smaller asymmetries are recorded rather than fixed here: the
    Watchdog reads its four codes off `properties_public` (trigger-289-sourced) while Browse reads
    them off `browse_list` (`listing_location`-sourced) — identical values by construction, but
    **W4 owes a row-level parity measurement** before it re-sources the first; and an EXCLUDE chip
    keeps rows whose code at that level is NULL (`NOT COALESCE(…, false)` in SQL,
    `or(col.is.null,col.not.in.(…))` in PostgREST), because an exclude subtracts what it MATCHES
    and the bare spelling made "not Prague" quietly mean "not Prague AND located".
    `notification_dispatches` is the **unified notification event table** (migration 206 —
    physical name kept; conceptually "notifications"): one source-generic, **property-grain**,
    append-only event row per `(source_kind ∈ {watchdog, collection_monitor, system_health},
    subject, change_kind)`,
    deduped by a single per-event **`dedupe_key`** (`wd:{sub}:new:{property_id}` once-ever;
    `wd:{sub}:price_drop:{snapshot_id}` **per-snapshot**, so a property that keeps dropping fires
    once per real cut — and so does the collection-monitor producer). **A price step is one
    advert's change against its OWN previous priced snapshot** — `listing_price_steps`
    (migration 559) is the one definition, read by the watchdog, the collection monitor and
    the property rollup alike; the two notification producers used to window every advert of
    a property into one series, so two portals quoting 5.0M and 5.2M fired a drop and a rise
    on every scrape and a merge alone sent false alerts. Each row carries provenance
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
    pattern) fires if the hourly job itself stops running. **The `system_health` arm is the one
    producer that is NOT property-grain** — it is about the pipeline, not a listing, so
    `listing_id` / `sreality_id` / `subscription_id` / `collection_id` are all NULL, the
    operator-state merge reconciler has nothing to re-point on it, and `compose_message()`
    renders its stored `message` verbatim instead of building a listing subject. Since the
    reliability program's W3 (migration 462) it has a **second emitter beside verify_pipeline:
    `ops_incidents`** — one open row per failure SIGNATURE (`scripts/failure_signature.py`, a key
    derived from the error TEXT and never from `workflow_path`, so N workflows failing for one
    reason are one incident), fed by `scraper.portal_runner`'s crash path in-process and by
    `scripts/record_workflow_failures.py`'s job-log backstop. Crossing
    `ops_incident_min_failures` writes exactly ONE dispatch here; there is deliberately no
    second alert table, no `ops_alert_email`, and no parallel bell namespace. Incidents close
    when every member workflow posts a newer `workflow_run_health.last_success_at`, on
    `ops_incident_max_age_hours`, or manually (`toolkit.ops_incidents.resolve_incident`).
    Delivery detail: `.claude/skills/scraper-ops/references/pipeline-verification.md`.
    The unified in-app **Notifications**
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
    *membership* (which city, if any, a property falls in) resolves through ONE SQL function,
    `curated_cities_matching()` (migration 436), to an `obec_id` allowlist** — `curated_cities
    .admin_boundary_id` already IS the obec's RÚIAN code, so membership is equality on a code
    every consumer already carries, and `browse_stats_properties`, `_city_quality_clauses` and
    the SPA prefilter cannot drift on the containment test the way they once did (one evaluated
    radius-only and silently disagreed with the other two's boundary-aware version on
    edge-of-city listings). It was previously PRECOMPUTED onto `properties.home_city_id`
    (migration 375, `recompute_home_city()`, a daily job measured at 680 MB of buffer traffic
    per call) because a per-request `ST_Covers` / `ST_DWithin` scan against every curated city
    is only cheap at Watchdog's per-subscription scale, not at Browse's; 436 removed the need
    for the scan rather than the cost of it. **W3 S4 (migration 506) deleted the column,
    `home_city_computed_at`, `recompute_home_city()`, the job — and its one remaining reader,
    `listings_with_city_quality()`**, the pre-W5 path that joined `curated_cities_public` on
    `browse_list.home_city_id` and was reachable only through the `?cityQualityLegacy=1` bisect
    hatch the same PR removed. A column and the function that joins on it leave together:
    Postgres does not track column dependencies through a function body, so dropping one without
    the other leaves a function that compiles and fails on its first call. What it does NOT delete is the obec-keyed pair this pattern was
    modelled on: `home_obec_pop` and the eight `near_*` columns (migration 142) stay, because
    they are population/proximity FACTS about the obec, not a membership cache.
    The one exception is `near_city_proximity` (an operator-chosen radius search, not curated-city
    membership) — not precomputable the same way, and confirmed dead in the SPA UI today (no
    widget wires it), so left as a live, unoptimized, unexercised code path.
18. **Operator curation is PROPERTY-grain and dedup-stable** (`collections` +
    `collection_properties(collection_id, property_id)`, `tags` + `property_tags(property_id,
    tag_id)`, `property_notes(property_id, body, origin_listing_id)`, migration 202 — was
    listing-grain on `sreality_id` pre-202). A tag, collection membership, or note is a fact
    about the real-world property, not one portal's advert, so it is keyed on `property_id`
    and **follows the property across a merge** (a detach leaves it where it is, best-effort). `toolkit/operator_state.py`
    (`carry_operator_state_on_merge` + `OPERATOR_STATE_TABLES`, the single registry of every
    property-anchored operator-state table — collections, tags, notes, AND `notification_dispatches`)
    re-points that state onto the survivor inside the `merge_properties` transaction (SET tables
    union with collision-collapse; APPEND tables move every row), so no operator-state row can
    ever orphan onto a `merged_away` property — the invariant holds by construction. **That
    invariant has a second half, on the WRITE side: a caller-supplied `property_id` is resolved to
    the active survivor (`toolkit.property_identity.resolve_active_property_id`) before EVERY
    property-anchored write — the remove and edit halves included, not just the INSERT.** 426fa575
    hardened only the INSERTs while claiming "every property-anchored write entry", and the five
    UPDATE/DELETE twins kept the bug for fourteen months: the add half resolved and landed a row on
    the survivor, the remove half kept the raw id, matched nothing, and answered a success-shaped
    `{"removed": false}` (or a 404 "note not found" for a note alive and well on the survivor) — so
    one cached id could create a membership it could then never remove. The two halves of one
    affordance must resolve alike. A write that needs a real target 4xx's on an unresolvable id; a
    remove falls through to the raw id and stays idempotent, because no caller reads the boolean.
    Resolution is wrong in exactly two places, both enumerated in the rail: the merge route itself
    (it CREATES survivors) and `properties.asset_id` (a column on the property row: the merge carries it
    onto the survivor, but a link or an unlink names one row). The rail is `tests/api/test_property_anchored_write_census.py` — an enumeration in a
    commit message is not one. Adding a
    new property-anchored operator-state table = one registry line. Unmerge/split are deliberately
    **best-effort**: state stays on the surviving/anchor property and the reactivated/detached
    side starts clean (the operator re-curates — nothing is destroyed, it is on the survivor).
    Notes carry `origin_listing_id` as display provenance only ("written while viewing this
    advert"), never as a grouping key. Writes flow through the FastAPI service (property-grain
    routes `/collections/{id}/properties`, `/properties/{id}/tags`, `/properties/{id}/notes`); the
    browser never writes directly. **Collections carry monitoring (Sprint C, migration 211):
    `monitoring_enabled` opts a collection into change alerts (the collection-monitor producer,
    rule #16) and `notify_channels` is its delivery-channel pick (folded into the dispatch's
    `target_channels`); a protected default "monitoring" collection (`is_system=true`, can't be
    renamed or deleted) ships monitoring on. The "add to collection" affordance lives on the
    Browse card (a layers control ADJACENT to the pipeline funnel — rule #22 keeps the funnel the
    sole pipeline affordance), the listing-detail `CurationBlock`, and the Chrome-extension panel.**
    The panel's control is the SPA header's `CollectionSaveToggle` + `CollectionSaveMenu` reproduced
    by value (a bookmark button opening a checklist of EVERY collection, monitored ones first and
    bell-marked); it replaced a one-click "Sledovat" bell that could only reach the single
    monitoring collection, so the panel and the app now offer the same verb over the same set.
    In the SPA those affordances share ONE membership read (`fetchPropertyCollectionMemberSet`
    under `curationKeys.propertyCollectionMembers`; one property's ids are `members.get(id)`) and
    ONE revalidation — `lib/collectionCache.ts`, called by every writer: the menu, the
    `CurationBlock` row, the collection page's row-remove, BOTH collection DELETEs (migration 202
    cascades memberships away) and the merge (operator state re-points `collection_properties`, so
    the map's keys change). The hand-typed key lists it replaced had drifted — every one but the
    menu forgot the shared map — the same failure `lib/browseInvalidation.ts` records for Browse. The
    extension holds no such cache, so this is an SPA-scoped claim.
    **`collections` is also a Browse COHORT FILTER** (`ListingFilters.collections`,
    `?collections=<ids>`, registry id `collections`, BROWSE agenda only). Semantics are **OR** —
    a property matches if it is in ANY selected collection — stated once, in the registry
    description, and deliberately the opposite of `tags` (AND): collections read as folders,
    so two of them mean "either folder". BROWSE-only for
    `pipeline`'s reasons (rule #22): a watchdog scoped to the operator's own groupings would
    fire on their own clicks, and the estimation agent must never see their taste. Like the
    other lenses it sits OUTSIDE preset identity (`PRESET_EXCLUDED_KEYS` + `_PARAMS`), so
    toggling it never dirties a loaded preset. `lib/collectionScope.ts` holds the ONE definition
    of what a selection means, rendered for whichever surface asks — today a property-id
    allowlist for Browse; the pipeline board's in-memory predicate joins it there rather than
    growing a second answer. The allowlist resolves from the SAME member map
    the glyphs render from, and "nothing selected" (no constraint, `null`) stays distinguishable
    from "a selection nothing is in" (zero rows, `[]`) all the way to the query.
    **Both curated-set prefilters now share one shape** — membership rows reduced to a
    property-id allowlist, AND for tags, OR for collections — and `tags` left
    `properties_with_tags(tag_ids)` for `property_tags_public` to get there: the RPC body
    carries `limit 5000` (migration 202) under a client comment asserting exhaustiveness, and a
    truncated allowlist silently bleeds listings the operator asked to exclude back into the
    cohort. The membership read is complete-or-throw (`fetchAllRows`); the RPC stays in the
    database until the SPA deploy has rolled out. `fetchBrowseStats` was the one Browse fetcher
    that named its prefilters by hand; it now resolves through `resolveBrowsePrefilters` like
    every other lane (with `brokerId` cleared — Stats is deliberately not broker-scoped and has
    no listing-grain parameter, while the broker resolver throws without a session), so a new
    property-grain filter cannot narrow the list and leave the panel above it counting the whole
    market. A membership write invalidates the Browse reads only when membership IS the cohort
    (`revalidateCollections`' `cohortScoped`, passed by the Browse card alone — the mirror of
    `revalidatePipeline`'s knob).
    **Adding notes is reachable from the Chrome-extension panel too** — it lists the property's
    existing notes + an add box, writing through the SAME `POST /properties/{id}/notes` the
    `CurationBlock` uses (the viewed advert's `sreality_id` as `origin_listing_id`); notes are
    NOT batched into `POST /listings/lookup` (too heavy per index card) — the panel fetches them
    lazily via `GET /properties/{id}/notes` on open. Tags are the one curation surface the
    extension does not yet expose. **Every property-grain operator write (curation here, the
    pipeline in rule #22) carries exactly ONE account, resolved once at the route edge; reads take
    none and are scoped by RLS. The doctrine and its standing gates are stated once — rule #22's
    tenancy note below.**
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
    **Dismissal (migration 536) is the fourth curation fact: "I reviewed this property and never
    want to see it again."** It is NOT a special collection — collections are many-to-many
    groupings, a dismissal is one boolean per property, and a pseudo-collection would need a
    magic-id lookup in every consumer plus hiding from every collection picker.
    `property_dismissals` follows migration 401's suppression shape: ACTIVE = `lifted_at IS
    NULL` (a partial unique index keeps one active row per property per account), undo LIFTS
    the row (`lift_reason` `operator` | `pipeline` | `merge`, so an undo rate stays measurable),
    and nothing deletes one — the table is its own history. `property_dismissals_public`
    (security_invoker, active rows only) is the ONE read definition of "dismissed for the
    caller"; it is plural under RLS, so `DELETE /dismissals/{property_id}` is RLS-only and lifts
    every row the caller can see, while `POST /dismissals` names its one account. **A LIVE deal
    and a dismissal never coexist** — "live" meaning a card at a non-terminal stage.
    `POST /dismissals` answers 409 for a live deal; every pipeline write that can leave a card
    live (`add_card`, a `move_card` stage change, `update_stage` re-opening a terminal stage)
    then calls `api.dismissals.lift_dismissals_of_live_deals`, which lifts (`pipeline`) only
    where the data shows a live card — so the rule is decided in one statement from the data,
    not re-derived by each caller. A deal closed into a terminal stage is history, not pursuit,
    and keeps its dismissal: until 2026-09-21 ANY card blocked dismissing, so a deal the
    operator had "Passed" on stayed in Browse forever with no way to hide it (29 "Passed" + 15
    "Lost" cards at the time), while the 409's own advice — "close the deal there instead" —
    hid nothing. The row is deliberately absent from `OPERATOR_STATE_TABLES` — a SET collision
    there DELETEs, which would destroy history — so `toolkit/dismissal_identity.py` carries it
    across a merge after the pipeline reconciler: a colliding active row is lifted (`merge`),
    every row re-points, and a survivor holding that account's LIVE card lifts the dismissal
    (`pipeline`). Unmerge is best-effort, as for the registry tables.
    **Browse hides dismissed properties by default, server-side (migration 537).** Every other
    Browse prefilter is an id ALLOWLIST sent as `.in(...)` in the GET URL; a dismissed set is an
    exclusion that grows without bound, so it never leaves the database. Each Browse relation has
    a dismissal-aware twin — `browse_list_visible()`, `properties_map_visible()`,
    `listing_feed_visible()` — an inlinable SECURITY INVOKER SQL function (`NOT EXISTS` against
    `property_dismissals_public`); functions rather than views because `browse_list` and
    `properties_map_mv` are blue-green rebuilt (a view or policy on them would block the DROP or
    vanish with it), and they return `browse_projection`'s row type, never the rebuilt
    relation's. `browse_stats_properties` / `browse_map_cells` take one trailing
    `hide_dismissed` flag (default false). The SPA's `queries.ts:readSource` is the one seam:
    every cohort read starts from the twin unless `?dismissed=show` reveals them — a lens outside
    preset identity, like the pipeline scope — and the sidebar says how many the cohort hides
    (the difference of two cached totals, fetched only once something is dismissed). Merge mode
    reads the same source: to merge a dismissed duplicate, reveal it first. Measured on
    production with 20,000 dismissals stacked on the newest rows: card page 49 ms, exact count
    115 ms (31 ms baseline), Stats no slower, map clusters +95 ms.
    **Notifications: a dismissal hides, it never un-detects.** The watchdog matcher, change
    detector and collection monitor are untouched — `match_once` advances its cursor whether or
    not it inserts, `:new:` dedupe keys are once-ever, and the `reactivated` detector keys off a
    prior `inactive` dispatch, so suppressing an INSERT would lose the event for good on undo.
    Instead every in-app read of `notification_dispatches` — the feed and its total, the unread
    badge, mark-all-seen, each watchdog's dispatch count — carries one `_NOT_DISMISSED` predicate
    (RLS-scoped, via the view), so the badge and the list cannot disagree; a dispatch stays
    reachable by id. The outbox drain (service-role) skips a dispatch whose own account dismissed
    its property, on both the new and the retry pass: it gets no `channel_sends` row and ages out
    of the 7-day window unless the dismissal is lifted first.
    **One control.** `<DismissButton>` (`overlay` on the Browse card, `inline` in the Table's
    action cell beside the funnel, `header` on the listing page) over one hook,
    `lib/useDismissal`. A click hides with no confirm — nothing is destroyed — and a single
    sticky "Vrátit" toast (each new dismissal replaces the last) restores; it keeps working after
    the dismissed card has unmounted. The dismissal leaves every cached Browse list that hides
    dismissed properties at once and those lists are NOT refetched (a triage run must not re-read
    every loaded page per click); counts, Stats and the map re-read, and a restore re-reads the
    lists. State is one query per property filled by `fetchIsDismissed`, which answers every call
    made in the same task with ONE read of the view (≤ 200 ids per URL) — never a whole-set read,
    since the dismissed set only grows. The control is absent while the property is in the
    caller's pipeline, and a pipeline write re-reads dismissal state (`add_card` lifts it). The
    cancel-snapshot-restore step is `lib/optimisticCache.holdQueries`, shared with
    `lib/pipelineCache`. The Chrome extension's panel carries the same verb ("Skrýt" / "Skryto",
    ink not copper), its state riding on `POST /listings/lookup` as `dismissed` (RLS-only, like
    the pipeline and collection state beside it) and its writes on the same `/dismissals` routes.
19. **The sreality scrape is split by cadence (Phase 2): a fast index-walk feeds an async
    batched detail-drain through `listing_detail_queue` (migration 105).** `index_walk.yml`
    (`scraper.main --index-only`, `run_type='index'`) walks the full index, `touch_listings` +
    nominates unseen rows for a page check (rule #3, 2026-09-07), and **enqueues** new/price-changed
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
    `id = ANY(...)`), so a new/edited/delisted listing reaches `properties` within ~5 min (~2 on
    the worker's maintenance lane) and the job is **O(changes)**, not O(all properties). Reaching
    **Browse** is a second step, because `browse_projection` reads `properties` and Browse reads
    the `browse_list` snapshot: since field-capture W6 the drain patches `browse_list` for exactly
    the ids it recomputed (`sync_browse_list`), so a change usually no longer waits for the `*/15`
    wholesale rebuild — which was a measured mean of 11.7 min, worst 36.6 (94 rebuilds / 24 h,
    2026-09-21). A fast path, not a guarantee: the rebuild snapshots `browse_projection` at its
    start and renames the new table in at its end, so a patch committed inside that window is
    superseded silently, and a rebuild is in flight ~26 % of wall-clock (283 runs / 72 h, mean
    237 s against a 900 s cadence). The drain is race-free +
    terminating: it claims rows dirtied at/before a run cutoff and deletes only those untouched
    since (a mid-run re-dirty bumps `marked_at` past the cutoff → survives to the next pass).
    New listings (`property_id` NULL) are born + recomputed by straggler-attach, not the queue. The
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
    mutate `properties` concurrently. Inline merge/detach still call `recompute_one` directly
    (they keep the survivor current without waiting for the cron). One accepted lag: a
    byte-identical reactivation (a delisted listing reappears with no content change) produces
    no snapshot, so it waits for the daily sweep — rare, documented.
21. **Every portal runs through ONE shared framework (Phase 4); per-portal code is a fetcher +
    a parser + a config row — no per-portal branches in shared code.** The parser's outputs
    include the row's `source_url` (its page on the portal): a stored fact every surface READS and
    none reconstructs — sreality's assembler is `scraper/sreality_url.py`, the 8 crawlers' are their
    `<portal>_client.detail_url`; the column rides `LISTING_COLUMNS` preserve-if-null on every write
    path (`docs/design/portal-listing-url.md`). Preserve-if-null is per (source, column) since
    field-capture W6 — `scraper/db._listing_update_set_sql(source)` asks
    `scraper/attribute_contract.py` which cells a parser NULL may clear (`structured`/`derived`
    still clear, `text`/`none` preserve) — and that is a config row of the same kind as
    `PortalConfig`, not a per-portal branch: shared code reads one table and the nine portals add
    no code to it. The pieces:
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
    end-gated presence nomination (`_queue_presence_checks`), `scrape_runs`) is shared. A genuine per-portal need is an
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
    the one drain. Since 2026-09-07 every portal delists the same way (rule #3): a complete
    category walk nominates its unseen rows, the drain's page fetch decides; a portal whose
    walk cannot be proven complete (no declared total → `unknown`) simply nominates nothing and
    relies on gone detail fetches. `supports_complete_walk` is a posture signal, not a gate.
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
    `reconcile_pipeline_on_detach` restores a retired property's card from that snapshot when a
    detach reactivates it (**lossless**: the reactivated property gets its pre-merge stage back,
    and in the move-if-empty case the survivor's absorbed card is dropped so it isn't
    duplicated); the survivor's own stage is left as-is — a chained-merge-safe best-effort, so a
    survivor that absorbed the retired's stage keeps it until the operator adjusts. Writes go through the bearer-gated API (`POST/DELETE /pipeline/cards` to
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
    **TENANCY NOTE — stated here once, for rule #18 as well.** Pipeline MEMBERSHIP has exactly ONE
    definition: `current_account_ids()`, the database's own membership function, on every surface —
    the extension's `POST /listings/lookup` included, which takes no account argument and whose SQL
    carries no account predicate, so its answer IS the SPA's answer by construction. That is what
    makes the "MEANS one thing" claim below true rather than aspirational (a second,
    explicitly-bound definition is precisely what made the extension disagree with the SPA for
    seven weeks, 2026-07-23 → 09-11). The same doctrine governs every account-scoped route,
    curation included: reads on a tenant connection scoped by RLS alone, writes carrying ONE
    account resolved at the route edge, an explicit `account_id = %s` predicate only where RLS is
    off and it is the sole gate. **It is written out in full exactly once** — with the fourth
    shape (`deps.account_scope`), the post-mortem's two test rules and the standing censuses —
    in `.claude/skills/database/references/tenancy.md`. Don't restate it here or under rule #18.
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
    (a label, not a contact). **W6-c/migration 517 dropped all three from `listings_public`** — a
    NULL placeholder with no reader is not a compatibility surface; `properties_public` still nulls
    the contact pair. **Both went through PostgREST until
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
    The board's **filter bar is built from the app's shared primitives**
    (`Field` + `Segmented` + `MultiselectChips`, the horizontal grammar `Brokers.tsx` already uses):
    **Stav** (any/active/inactive), **Typ** (`category_main`), **Lokalita** (the shared
    `LocationTypeahead`) and **Kolekce** (collection membership, OR — rule #18). Labels come from
    the SAME generated filter registry as Browse's own controls (`FILTER_REGISTRY`, never a parallel
    hardcode) and the URL spellings are Browse's (`status` / `cat` / the `districts` family /
    `collections`). Every row applies **client-side** over ONE board read plus the shared
    member map (`curationKeys.propertyCollectionMembers`) — no per-filter read, no widened view —
    and is offered only when it could change the view, or while it already constrains it: Stav
    needs a delisted card, Typ ≥2 present types, Kolekce a RESOLVED member map plus either a
    selection in the URL (a live constraint is always visible, so it is always liftable — before
    the list arrives its chip reads `#<id>`) or a board collection that could partition the board.
    Clearing is ONE header **Reset** gated on a derived `filtersActive`, never a per-row
    clear. **Fail-open contract** (pinned by `Pipeline.test.tsx`): an unresolved member map
    (loading or errored) means no constraint, no Kolekce row and nothing counted — a `?collections=`
    link must never empty a board that cannot see membership — while a RESOLVED selection matching
    nothing is zero cards, never everything. Stav's default stays `any`, so a delisted member of a
    collection stays in the cohort. **On the kanban board** stage moves are
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

    **The headline area has ONE rule, and every portal feeds it the same way** (W17,
    2026-09-15). `scraper/area.derive_headline_area(category_main, usable, floor, total, plot,
    fallback)` picks `area_m2` and stamps `area_basis`; the land arm is
    `plot -> total -> usable -> floor -> fallback`, always stamped `plot`, and the dwelling arm
    never reads `plot` at all (a house's parcel sits beside its floor area in `estate_area`).
    All nine parsers now hand over their "plocha pozemku" / `surfaceLand` / `estate_area` value
    — including the six whose land pages already reached the headline under another label, so
    the rule is fed identically everywhere (rule 21) rather than by nine arrangements of which
    argument a parcel happens to arrive in. Before W17 three parsers passed no plot at all:
    **52,183 land rows** (sreality 44,237 of 44,237, idnes 5,292 of 45,500, bezrealitky 2,654 of
    2,667; ~32.7k active) stored a parcel in `estate_area` and carried `area_m2` NULL — no per-m²
    price and no area for any consumer reading the headline. `scripts/backfill_land_headline_area.py`
    (+ its dispatch-only workflow) healed exactly that population, active or not, by moving the
    stored value into the column the one rule would put it in today; it wrote **no snapshot**
    (the sanctioned rule-2 exception: our own mis-parse of the SAME stored page, the
    `backfill_idnes_areas` precedent) and was idempotent because the write emptied its own
    selection. **Both scripts were deleted by the field-capture program's W3** — the ONE
    re-parse seam (`scripts/reparse.py` / `reparse.yml`) is the heal path now, replaying the
    portal's own parse entry point over its declared substrate under the same rules. What
    follows a heal differs by portal: idnes and bezrealitky hash the PARSED
    fields, so W17's parser change — not the heal — makes each live row's next detail fetch
    append exactly ONE genuine snapshot; **sreality hashes the RAW payload**
    (`scraper.hashing.content_hash`), which did not change, so its 44,237 rows get no snapshot
    ever — the heal is the only write they receive and later refetches re-derive the same value
    and rewrite the column silently. Two populations are deliberately left with no headline:
    3,016 land rows carry no area from their portal at all, and 20 hold a parcel beyond
    `area_m2`'s `numeric(7,1)` ceiling (largest 16,809,800 m²) — `MAX_AREA_M2` in the rule
    declines the measure rather than stamping a basis for a value the row cannot store, which
    is also what keeps the heal's first batch from aborting on a 22003.

    **The area NUMBER has one grammar too** (the scraper track's W19, 2026-09-17 — not
    the location program's). Picking the right measure is only half the rule; reading the
    figure is the other half, and it was **five private copies** of a regex until now — four
    of them the naive form that matched the first bare digit run before an `m²`. On the
    Czech spaced-thousands format every portal renders ("5 870 m²", "1 063 m²" with a
    no-break space) that match starts INSIDE the number, so 5870 stored as 870. idnes had
    already been fixed after its own 2026-08 incident (8k+ rows); this wave moved that proven
    grammar into `scraper/area.parse_area_text`, **widened** it to the separators idnes never
    had to handle (narrow NBSP, thin space), and deleted every copy — ceskereality,
    realitymix, remax, maxima and bazos now call it, so the grammar can no longer diverge per
    portal (rule 21). The negative lookbehind keeps a disposition from being swallowed
    ("3+1 174 m²" stays 174, never 1174) and a per-m² price from reading as an area. Measured
    on production 2026-09-17: **ceskereality 17,207 rows** of 98,126 carried the truncation
    fingerprint (stored area = title area mod 1000) and not one of its 19,088 land rows had an
    `area_m2` of 1000 or more; **realitymix 13,164** of 83,051 (its spec cells are unspaced —
    only the title fallback truncates); **bazos ~12,400** (8.4 % of its newest 8,000 rows);
    remax (13,797 rows) and maxima (540, of which 87 land rows max out at 987 m²) could not be
    fingerprinted from titles, but remax's spec cells render "Plocha parcely: 1 063 m²" with
    an NBSP.

    **The key precedence is one rule too, one level up.** A portal decides WHICH of its spec
    cells is the usable measure and which is the parcel, in what order — and a second copy of
    that order is the same defect as a second copy of the grammar. So each parser exposes
    `areas_from_params(params, title=, category_main=)` (bazos, which has no spec table:
    `areas_from_text`) returning `scraper.area.PortalAreas`, and its own `parse_detail` calls
    it. That is what makes the heal possible without a second implementation. Since W2 the
    KEYS in that order are the attribute contract's: the five HTML-table portals unpack
    `source_values(SOURCE, "area_m2", params)` in the slot order (usable, floor, total, plot)
    the `area_m2` cell declares, so the gates can prove every one of them is a key the portal
    emits — the restatement it replaced named seven keys no parser reads (which let the
    portals go on publishing them unread) and omitted thirteen the parsers did read, every
    one of the thirteen dead.

    `scripts/backfill_area_spaced_thousands.py` (+ its dispatch-only workflow) healed the
    stored rows **from `listings.raw_json` — the parser's own latest reading of the live
    page**. Each of these parsers stores the detail page's spec cells verbatim under
    `raw_json['params']` plus `raw_json['title']`; bazos keeps its ad body in
    `listings.description`. Those are exactly the strings the naive regex mis-read, so
    re-reading them with the fixed grammar IS the heal — no fetch, no object store, no R2.
    **That substrate was chosen the hard way.** The first cut re-parsed the ARCHIVED body out
    of `portal_raw_payloads` + the bucket and, because the payload writer holds a 7-day
    per-listing floor, the archive lags the live row; a gate that refused to apply a stale
    body skipped **89 % of the population** on the first production dry run (examined=3000,
    would change=85, body_stale=2672), and the heal was very nearly a no-op. `raw_json`
    cannot lag: it is rewritten by the same transaction that writes the areas, so the
    staleness question does not arise. **That script is GONE since the field-capture
    program's W3** — the same heal is now `reparse.yml --source <portal> --fields
    area_m2,estate_area,usable_area,garden_area`, which replays the portal's whole
    `parse_detail` over `portal_raw_pages.html`. That substrate is staged in the SAME drain
    transaction as the listings row, so it cannot lag either, and it carries the page rather
    than the parser's spec-cell projection of it; `portal_raw_payloads` stays out of the seam
    for exactly the 89 %-stale reason recorded above. Same rule-2 posture as the heals above,
    and it
    **subsumes the W17 land heal on these portals**: that one copied `estate_area` into
    `area_m2`, and on these portals `estate_area` was itself truncated, so the re-derive fixes
    both columns from the same fields in one statement (which also enqueues
    `dirty_properties` in the same CTE, so the mark cannot survive a rolled-back write).
    Three further properties are load-bearing and each has a test that fails without it.
    (1) **It never writes NULL over a stored area** — a column the re-derive cannot produce
    is not a change and is not written, because a shape drift is indistinguishable from a
    genuinely absent measure; one consequence is deliberate, in that a parcel beyond
    `area_m2`'s ceiling leaves the row's existing headline where it was while `estate_area`
    is healed beside it. The live simulation over 6,000 rows agrees: every change grows or
    fills a value, none shrinks one. (2) **Every value is rounded to its column's scale**
    (all five area columns are `numeric(*,1)`) before being compared AND before being
    written, HALF-UP like Postgres's own numeric rounding, or a cell reading "86,19 m²"
    rewrites the stored 86.2 on every pass for ever. (3) **The selection carries no
    `raw_json` predicate** — a truncation always leaves a value under 1000, or a "000" tail
    the write boundary NULLed, so the four narrow numeric columns answer it completely; the
    page fields are PROJECTED per page (500 rows) and never predicated on, since a predicate
    over `raw_json` detoasts every candidate row. That makes it a CAN-BE-WRONG set matching
    ~99 % of these portals' rows while a minority actually move, and the per-source report
    says both. The paging SELECT measured 40.5 s and the count 13.6 s against the cluster's
    120 s default, so the run arms `SET statement_timeout = '600s'` on connect and walks the
    sources ONE AT A TIME with the keyset on `id`. Expect the heal to SURFACE seller-error
    outliers the truncation masked: a 2+kk flat advertised as "3 060 m²" stops reading 60
    and starts reading 3060 — the portal's number, faithfully.

    **The SIDE columns get one rule each too, and one of them was a SUM** (the scraper
    track's W21, 2026-09-17). `usable_area`, `estate_area` and `garden_area` sit beside the
    headline, and the wave above only ever governed the headline — so three defects lived on
    underneath it, each invisible to every health check the platform had.

    *mmreality's parcel is `parcelArea` ("Plocha parcely").* The parser read
    `landArea or plotArea or totalArea`. `landArea` and `plotArea` are keys the page declares
    and **never fills** — a value on ZERO of 14,417 stored rows — so `estate_area` was always
    `totalArea`, and `totalArea` is not a measure at all: the page DERIVES it, and **what it sums depends on
    the category** — `parcelArea + usableArea` on a dum (4,133 stored rows carry all three and
    agree; `parcelArea` is itself `builtUpArea + gardenArea`), `== usableArea` on
    komerční / ostatní, which state no parcel, and `== parcelArea` on a pozemek, which has no
    interior to add. The arithmetic differs by category; the conclusion does not. Live before
    the fix: **1,178 active houses** carried a plot 44–50 % too large, **1,515 more** still
    carried that same derived figure as their HEADLINE (the pre-W1 shape, never re-derived
    because nothing refetched them), and `parcelArea` was unmapped on all **3,653 active land
    rows** and 755 komerční. The fix is one
    `mmreality.areas_from_params(obj, category_main=)` — usable ← `usableArea`, plot and
    `estate_area` ← `parcelArea`, garden ← `gardenArea` — and `totalArea` is offered to the
    resolver in NO slot: a derived figure stamped `'total'` would be a confident wrong label,
    which is worse than the missing value it replaces. That costs a headline on exactly **19
    rows corpus-wide** (5 byt, 2 komerční, 11 dum and one inactive pozemek) whose page states
    neither input, and nothing on land, where `parcelArea` equals `totalArea` on every one of
    4,568 rows. mmreality is a JSON-object portal, so its function takes the estate
    object (`raw_json` IS that object) rather than a `params` map and no title; the
    re-parse seam replays `parse_detail` over the stored page, so no second copy of that key
    list exists anywhere.

    *`usable_area` is the "užitná plocha" label and nothing else.* idnes
    (`užitná or podlahová or plocha`) and ceskereality (`plocha užitná or užitná plocha or
    plocha`) both ended their chain on a broader label, which is the same pre-collapse the
    headline resolver exists to prevent, one column over: a page stating only a floor or total
    area wrote that number into the field every consumer reads as the interior measure. Those
    labels still reach the headline through their own typed slots under their own basis; they
    no longer impersonate a third label. **This is a contract change with a 1-row live
    footprint** — both portals state the užitná label whenever they state any area (measured
    2026-09-17: one idnes row corpus-wide carries a `usable_area` its own `area_basis` says came
    from the fallback) — and the heal cannot undo even that one, because it never blanks a
    stored value (see (1) above). Fixed forward, residue named.

    *A side column's validity bound has to sit before the content hash.* `usable_area` /
    `estate_area` / `garden_area` are `numeric(9,1)`; a value at or beyond that ceiling, or a
    0 m² form placeholder, was HASHED as itself and then NULLed at the write boundary by
    `scraper.db.sane_listing_numerics` — leaving `listings` permanently disagreeing with its own
    newest snapshot (rules 2/8), the hazard `derive_headline_area` already closed for `area_m2`
    alone. `PortalAreas.__post_init__` now bounds all three at `MAX_SIDE_AREA_M2`, pinned to
    `scraper.db._NUMERIC_ABS_MAX` by test; `sane_listing_numerics` stays the last guard for the
    two parsers that build no `PortalAreas` (sreality, bezrealitky). bezrealitky's `_num` also
    refuses a `bool` before `float` — `float(True)` is 1.0, and its advert carries boolean flags
    beside its measures, so one key flipping from a size to a flag would have written a 1.0 m²
    garden. (`frontGarden` there is a FRONT YARD, the strip in front of a ground-floor flat.)

    idnes joined the shared shape in the same wave: `idnes.areas_from_params` replaces its
    private `_AREA_M2_MAX` / `_AREA_LARGE_MAX` / `_clamp` with the shared bounds, and both it
    and mmreality joined `backfill_area_spaced_thousands`'s dispatch (deleted in W3; the seam
    is the dispatch now) — which is why W21 needed no heal of its own. mmreality's arm of that
    heal walked the portal WHOLE rather than by the
    truncation fingerprint: its numbers are typed JSON that never met a regex, and a land row
    carrying the sum as its headline with NULL in every other area column satisfies neither
    fingerprint arm (3,443 of 14,417 rows). It is also FIRST in the default set — the only
    portal with rows that are wrong today, and the smallest corpus, so a run that spends its
    `--max-seconds` budget still finishes it. **idnes is wired but NOT walked by default:** the
    užitná narrowing is forward-only (the heal never blanks a stored value, and there is one
    row corpus-wide to retract), so a default idnes walk would read ~206k `raw_json` blobs to
    change ~nothing while starving the portals that do move; `--sources idnes` still runs it.
    `scripts/backfill_mmreality_areas.py` and its workflow are DELETED — they re-spelled the
    phantom chain, so keeping them meant keeping a second, wrong copy of the key order.

    **"Plot area" is a MEASURE, not a column** (migration 534). `plot_area_m2(category_main,
    area_m2, estate_area)` = `area_m2` for `pozemek`, `estate_area` otherwise — the same
    declaration style as `measure_price_per_m2` (single expression, `IMMUTABLE PARALLEL SAFE`,
    no `SET search_path` so the planner inlines it), with a Python face in `toolkit.measures`
    (`plot_area_sql` / `plot_area_m2`). It exists because the polymorphism above cuts both ways:
    every reader of "the plot area" spelled it `estate_area`, which is right for a house and
    silently wrong for land, where the plot IS the headline. Measured 2026-09-17 over active
    `pozemek`: **32,626 of 101,021 rows (32.3 %) carry no `estate_area`** — bazos 15,846,
    realitymix 11,470, mmreality 3,653, remax 1,649 — and 31,613 of them carry the parcel in
    `area_m2`. So `min_estate_area = 500` dropped a third of the country's land inventory
    without a word. The fix is the name, **not** a writer change. **The writer rule, stated once:** a
    parser fills `estate_area` ONLY from a parcel cell the page itself LABELS — sreality,
    bezrealitky, idnes, ceskereality, maxima and (from W21) mmreality all do, land rows
    included, and that is faithful reporting rather than duplication. What no parser may do is
    SYNTHESISE the column from `area_m2` for the four portals whose land pages carry no parcel
    label: that would copy the headline into a second column on 32k rows, leave every future
    portal to remember the rule, and make the data lie in order to spare the reader a function
    call. Live readers moved:
    `toolkit.comparables._shared_filter_where` (comparables + velocity + the transit corridor)
    and the watchdog matcher `api/notifications._build_match_clauses` — rule 16's two sites,
    and there the SQL is textually identical, because neither relation publishes a plot column.

    **On the SPA the same measure has to BE a column, and getting that wrong is a silent
    no-op** (migration 535, caught in review). `registryQueryBuilder.applyRegistryFilters` is
    how a browse-agenda filter becomes a PostgREST predicate, and it SKIPS any filter whose
    `pg_column` is null — so the first cut, which set `pg_column = None` because no `listings`
    column answers these filters, turned the Browse "Lot area" inputs from "applies a
    predicate that drops land" into "applies nothing at all": the UI still offered them, the
    query no longer used them, and CI stayed green because the drift test skipped null
    `pg_column` too. It also broke rule 16 the other way round — the watchdog matching on the
    measure while Browse matched on nothing. So `plot_area_m2` is now a COLUMN of the read
    model, computed by the same function inside `browse_projection` (→ `browse_list`,
    `properties_map_mv`) and `listing_feed_public` — the three relations Browse filters
    against — and `pg_column` points at it. One definition, two spellings: a function call
    over `listings`, a published column over the read model.
    `test_frontend_read_contract_subset_of_projection` already demands the projection publish
    every browse filter's `pg_column`, so the two ends cannot drift apart again, and a null
    `pg_column` on a browse filter is now itself a test failure unless it is declared in
    `HAND_CODED_BROWSE_FILTERS` (something applies it by hand) or in the new
    `BROWSE_FILTERS_NOT_ON_THE_LIST_QUERY` (nothing does, and the reason is recorded). That
    second set exists because the hardened test found `tom_days_min/max` already in that
    state: they reach the Stats RPC and no list predicate at all, so Browse's Stats panel and
    the list beneath it can disagree about the cohort — a pre-existing gap this wave found,
    named, and deliberately did not fix.

    **Known residue, named.** Browse's table still SELECTS and SORTS the raw `estate_area`
    column while it now FILTERS the measure, so a bazos/realitymix/mmreality/remax land row can
    match "plots ≥ 5,000 m²" and render an EMPTY Lot-area cell — the fix is three lines but
    `estate_area` is a `SortField`, so a saved `?sort=estate_area` would silently fall back to
    the default sort, which makes it a Browse-cohort decision rather than an area one. And
    `browse_stats_properties`, `browse_list`'s own RPCs and
    `browse_map_cells` still compare `l.estate_area` in SQL. They are inert from the SPA today
    (`frontend/src/lib/queries.ts` sends them no `estate_area_min_filter`), and moving them
    means re-creating three large `SECURITY DEFINER` bodies — its own wave, with its own drift
    hazard (migrations 371/376).

    **The plausibility view gained the arm that would have caught the sum** (migration 534
    appends two columns to `measure_plausibility_by_source`). `estate_sum_share` is the share of
    rows carrying all three areas whose `estate_area` equals `area_m2 + usable_area` within 1 %
    — a side column holding a figure the page COMPUTED rather than measured. Neither existing
    detector can see that class: `data_quality_by_source` tests presence and the column was
    populated on every row, and `area_vs_usable_divergence` watches the HEADLINE, which on
    mmreality was already the interior. Read the calibration honestly — the relation is a WEAK
    fingerprint of mmreality's live shape (its `estate_area` was `parcelArea + usableArea` while
    its headline was `usableArea`, so the two coincide only where a parcel happens to equal its
    interior): mmreality is the top TWO cells at 5.5 % of 55 rows and 2.6 % of 1,123 over a
    background where nothing exceeds 1.4 %, so warn 5 % / fail 10 % is a forward guard sized on
    a real ranking rather than a smoking gun. The same wave RE-MEASURED
    `area_vs_usable_divergence`, whose thresholds were sized on pre-W1 numbers: its only
    non-zero cell today is mmreality dum/prodej at **57.1 % of 2,638 pairs** with a **0.0 % 7d
    arm** — the live parser has been right since W1 and what remains is the 1,515 legacy rows
    the heal clears — while realitymix byt, the 10.5 % cell the 20 % warn tier was chosen to
    spare, now reads 0.0 %. The tiers are deliberately NOT tightened onto that floor while the
    one real offender is still pending a heal: a threshold moved to fit a corpus mid-repair
    calibrates on the repair.

    **Still open, deliberately.** Six portals publish a **built-up area** ("zastavěná plocha" /
    mmreality's `builtUpArea`) with no column to put it in — a schema decision for the operator,
    not a parser fix. bazos has no labelled parcel in its free text, so it gains no side columns.

    **`area_m2` is what every consumer reads — the dedup rule included.** NEW DEDUP path C used
    to spell its own choice (`estate_area` for pozemek, else `usable_area`), a second answer to
    "which area is this listing's area" that disagreed with the headline on every one of those
    52,183 rows and on every dwelling measured only by a floor/total label. W17 deleted it:
    `toolkit/dedup_candidates_sql.py` reads `area_m2 > 0` for every category, and the per-category
    IDENTITY ATTRIBUTES are one vocabulary (`dedup_candidates.IDENTITY_ATTRS` — `pozemek`: area
    only, everything else disposition + area) rendered into both the pairing SQL and the Python
    oracle, so C1 never joins two parcels on a room count. That changed what the generator PAIRS,
    so `GENERATOR_VERSION` moved `c3 -> c4` (a new `inputs_id`; the pilot's pair rows are
    orphaned deliberately) and the SHA-256 ledger of the pairing statements in
    `tests/toolkit/test_dedup_candidates_sql.py` was re-pinned in the same commit.

    **The basis is resolved from `(category_main, category_type)`, rent-first, and NEVER from
    `listings.price_unit`** — that column is two values (`za nemovitost` / `za mesic`; W5
    collapsed the four spellings), a duplicate of `category_type`, not a per-area unit. The three tokens
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

24. **Folded into 25.** The number is kept because rules are cited by number and never renumbered;
    the two-paths-until-W6 rule it used to state ended when W4-c dropped the legacy store.

25. **Location: one store, one lane, one label, one code predicate; every location PR deletes at
    least as much as it adds.** Written 2026-09-11 from the full-programme audit ("Where the Town
    Lives"). The programme had built a completeness-first engine wave after wave — 62 tables, 81
    projection columns, 40 claim types, 5 claim-producing lanes, 19 workflows, 5 policy tables — and
    never flipped a consumer, so nothing exercised it end to end and nothing was ever deleted; 733
    verified findings came out of that shape, not out of any one bug. The corrective, and the
    as-built state: ONE answer table (`listing_location`, 26 columns) written by ONE four-step
    resolver (bind → fill → grade → check); ONE hourly lane over the stored payload and the stored
    page body; ELEVEN claim types, at most one contract entry per type, the town entry mandatory and
    naming a reader; ONE label function and ONE four-level code predicate for every place display
    and filter; no serving flags, no granularity floors, no second store — `listings` and
    `properties` carry no place column, so a reader joins `ll on ll.listing_id = l.id` and casts
    `ll.geom::geography` for anything measured in metres. The reason the answer is graded rather
    than a bare point: a `listings.geom` carried no statement of how precisely or trustworthily it
    was known, and a 75 m dedup circle around a town-centroid pin is exactly the false-merge class
    the axes prevent — remax disagreement is predicted on 54.3 % of raw addresses, bazos ran 5.56
    listings per pin. **The invariant**: every served listing has a row and every active Czech
    listing has a town (`check_location_town_coverage` is red until both are zero), and foreign is a
    determination — a country field, a foreign section, a pin outside the country — never the
    default for "no town found". **Speed**: Browse and the map read `browse_list`, which copies the
    fields at rebuild, so no consumer query joins the store; a field is added only after a measured
    slowdown and only there. Full as-built detail — the store's 26 columns by role, the lane's two
    halves and their cursors, the contract rails, the resolver's four stages, the served-listing
    predicate, what deliberately stays outside the store, and the incident lessons — is
    `docs/architecture.md` § Location data.

## Broker identity merges — auto-merge and the suppression rail

Unlike property merges (rule #15: operator-ordered, plus the dark AUTODEDUP worker lane that
merges only inside the area `autodedup_apply_scope` names), broker identities DO auto-merge. The nightly
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

## Location data — one store, one lane, one label

A listing's location is a fact the platform DERIVES — portal claims → a resolver → one graded answer
table — not a column a scraper writes. This section is the as-built END STATE; the wave-by-wave
ledger of how it got here is `roadmap/location-data.md`, and the plan corpus is operator-held
(`~/location-data-architecture-2026-08-10/design/final/MASTER.md`, `00-shared-contracts.md` the
tie-breaker — the `00 §…` / `03 §…` citations in the code cite that corpus). Why this shape and not
a `listings.geom` column: a raw point carries no statement of how precisely or how trustworthily it
is known, and the corpus made that fatal — remax disagreement is predicted on 54.3 % of raw
addresses, bazos ran 5.56 listings per pin with 51.5 % in clusters of 20+, and a 75 m dedup circle
around a town-centroid pin is exactly the false-merge class the grade axes exist to prevent.

**ONE STORE.** `listing_location` (migration 501) is the only place a listing's location is stored:
**26 columns** in five roles — the listing (`listing_id`); one position (`geom`,
`geometry(Point,4326)`); nine names (`country_code`, kraj, okres, obec, část obce, street, čp, čo,
psč); six RÚIAN codes (kraj, okres, obec, část obce, ulice, `ruian_adm_kod`); three grade columns,
all NOT NULL (`match_confidence`, `granularity`, `uncertainty_radius_m`); two status columns
(`country_status` NOT NULL, `disputed`); and four housekeeping (`resolver_version`, `resolved_at`,
`claim_set_hash`, `registry_version`). `location_data/resolver/projection.py`'s column tuple IS that
contract. `listings` and `properties` carry no place column at all (migration 508), so a place read
joins `listing_location ll on ll.listing_id = l.id`, and a property's place is its representative
listing's (`repr_listing_ref_id`) — one child for place, price and area alike. The table is a
**rebuildable cache, never a store of record**: truncating it is always legal, the `dirty_locations`
drain is its only writer, and each row is a pure function of the claims and the registry at the three
versions stamped on it. NOT NULL on the grade axes is load-bearing — a NULL reads as "no gate" and
fails open (a NULL radius makes both branches of the three-valued containment test evaluate NULL, so
the row drops out of `certain` AND `possible`). The radii are geometric bounds, uncalibrated by
design, never `r95_empirical`; calibrating them is a `RESOLVER_VERSION` bump that re-resolves the
corpus through the ordinary lane. One trap comes with the store's own type: `ll.geom` is `geometry`
where `listings.geom` was `geography`, so every metre-based `ST_DWithin` / `ST_Distance` casts
`ll.geom::geography` (index `listing_location_geog_gist`, migration 507) or it compiles and silently
measures DEGREES — `tests/test_one_place_predicate.py` pins both halves.

**ONE EVIDENCE TABLE.** `location_claims` is append-only evidence — what a payload asserted, with a
surface, an extraction method, a licence class and a `claim_fingerprint` (migration 386's IMMUTABLE
`location_claim_fingerprint()`, computed in SQL so no second transcription of the definition can
drift; it still takes all 23 inputs, of which 19 are stored). **19 columns**: identity, the contract
entry, five typed value slots, the declared-precision trio, the fingerprint. Nothing is corrected in
place — a wrong VALUE is superseded by a newer claim, a wrong CONTRACT is retracted:
`python -m location_data.contracts --retract <portal>@<version> [--extractor-id X]` resolves the
target first (no matching entry is an ERROR, not `deleted=0`), DELETEs the claims in bounded batches
each atomic with its own `dirty_locations` enqueue, then stands the header down. Batched because
"the contract's claims" is every listing a portal has ever had (~5 M on sreality), and one atomic
DELETE of that size spends its `statement_timeout` and rolls back, making no progress ever.

**ONE LANE, TWO HALVES.** `location_data/claims_intake.py` (hourly, `35 * * * *`) is the only writer
of `location_claims`, and it reads the two substrates we hold: `listings.raw_json`, and the LATEST
stored detail body in `portal_raw_payloads`, joined on `(source, source_id_native)` (`.listing_id`
is nullable and nothing ever populated it), fetched from R2 and scoped by the contract's exclusion
zones. ONE registry — `claims_intake.READERS`, 21 entries keyed by substrate, a name outside it a
hard refusal — over seven payload readers and fourteen page readers
(`location_data/page_readers.py`; the vocabulary both halves share is
`location_data/claims_common.py`). A `listings` COLUMN is never a substrate: a column the scraper
writes is not evidence a portal published.

* *The payload half* walks `listing_snapshots.id`. A snapshot row is appended exactly when a
  listing's content hash moves (rule 2), and every write path appends one for a brand-new row too, so
  "snapshots above my cursor" IS "the payloads whose claims can have changed" — where selecting on
  `last_seen_at` was a scan of the live corpus (~180 000 listings in 51 minutes), because the index
  walks re-sight everything within hours. The window is a keyset slice deduped to one row per
  listing, with `--source` INSIDE it (outside, a source-scoped run whose window held no row for that
  portal would read as "the log is exhausted" and stamp `ok` with its cursor stuck), and it **stands
  15 minutes behind the wall clock**: `listing_snapshots.id` is a bigserial allocated at INSERT and
  visible at COMMIT, written N at a time inside one transaction across concurrent drains, so a row
  whose id is below an advanced cursor can become visible after that cursor moved — and a keyset
  never looks back.
* *The page half* runs AHEAD of that scan and is **hash-gated**: a body is mined only while
  `portal_raw_payloads.contract_version IS DISTINCT FROM` the portal's active version, and a batch
  stamps the bodies it mined in the same transaction as their claims — so a body is fetched once per
  contract version, steady-state cost is bounded by page CHURN (~50–80 new bodies an hour fleet-wide)
  rather than by corpus size, and a bump re-mines every latest body over the runs that follow. Each
  batch is **two statements in one transaction**: a fenced WINDOW (`ORDER BY p.id LIMIT cap` over
  payload columns ALONE, carrying the whole eligibility gate) and then the `listings` join and
  latest-body anti-join over the ids it named. The cursor is the WINDOW's max id, never the surviving
  rows' (a third of a window survives the joins), and the pass ends when the window comes back short.
  **That cursor CONTINUES across passes** (W6-b, migration 516): "pass complete" means caught up, not
  restart, so the next run walks only the ids above it — seconds, not minutes. It is stamped together
  with the active contract-version set it was taken under (`bazos@5,bezrealitky@2,…`) and honoured
  only while that set holds, because a bump is the one event that makes rows BELOW it eligible again;
  an unstampable body is re-fetched once per version set instead of once per pass.
  Bodies are extracted **across PROCESSES** (`page_readers.extract_pages`, `os.cpu_count()` wide) —
  the parse is pure CPU and threads cannot share it, one core held a 1 500-body batch at 143–313 s
  against ~48 s to fetch it — with `forkserver`, never `fork`, because the lane holds an open psycopg
  connection inside the batch transaction and a forked child finalizing its copy of that socket would
  terminate the parent's session. The pool is an accelerator only: one outcome per body IN ORDER, so
  a content-triggered refusal still costs one listing's page entries and a pool the OOM killer takes
  finishes its batch on the main thread.

**A GONE PAGE IS NOT A BODY (W10).** "Latest body" means the latest body that is an AD. Four portals
answer HTTP 200 for a listing they have removed — bazos serves the CATEGORY INDEX page, which carries
no ad-level location at all — and until #1451 the bazos client did not recognise that, so the page was
archived like any other detail body and the pass mined it in place of the ad's last live page. The
fix needs no new column and no flag, because all three payload predicates (`_BODY_JOIN`,
`_UNMINED_WINDOW_WHERE`, `_LATEST_BODY_ONLY`) already ask for a body whose fetch SUCCEEDED: migration
521 (see below — 519 and 520 are its failed cuts) corrects the stored gone pages' `http_status` to **410**, the truthful status of a removed ad, and
the pass falls through to the previous version on its own (`payloads._PRUNE_SQL` also ranks non-2xx
last, so a stamped gone page is the first thing the version cap evicts). Nothing is deleted — the
body and its R2 object stay. A stored body is proven gone only when the archived HTML in
`portal_raw_pages` hashes to that payload row's own `body_sha256` (both are written in one
transaction by `upsert_portal_raw_page`, so their timestamps agree too) AND both of #1451's signals
fire on it; on 14 000 sampled bazos pages those two signals never disagreed. Going FORWARD nothing
new is stamped, because every HTML portal decides "gone" inside its client's `fetch_detail` and
raises `ListingGoneError` before the body reaches the caller that stages it — the guard is structural,
there is no body for the writer to refuse. TWO SQL traps are worth carrying, because both cost a
900 s statement timeout on prod before 521 landed. (1) 519 tested the title with
`ilike '%<title>%…%'`: a LIKE pattern with TWO internal wildcards is quadratic over a 100 KB
document (>180 ms a page against ~2.2 ms), and the anchor bought nothing — both forms selected
the same 598 of 3,000 pages. (2) 519 and 520 both bounded a batch by an id RANGE, but
`portal_raw_pages` interleaves nine portals in ONE id sequence — 142,506 bazos detail pages over
ids 3..6,302,167, **44 ids apiece** — and the planner puts the `html ilike` tests ahead of
`source = 'bazos'` in that scan's filter, so every batch detoasted ~44x the pages it wanted, most
of them larger idnes ones. 521 hands the UPDATE the batch's ids as a `bigint[]`
(`r.id = ANY(v_ids)`): 9.7 s per 1,000 pages against >900 s for the same work by range.
`test_location_w10_gone_bodies.py` pins both — one wildcard pair per pattern, and no id range.

If R2 is unconfigured the page half is skipped with ONE warning per run and the payload half runs
unchanged — the hourly ingest for nine portals must never go dark because a credential rotated. The
lane writes `location_claims`, `dirty_locations` and its own `location_claim_batches` ledger and
nothing else: a refusal (a withheld coordinate, an oversized value, a subject miss) is a COUNTER and
one log line per reason per batch.

**ONE LANE, TWO SCHEDULES (W7-a).** The module stays one lane; what it gained is a second
*schedule* for the payload half. Under W5 the consumers serve only RESOLVED locations, so an hourly
producer standing 15 minutes behind the clock meant a listing written just after a tick was invisible
in Browse for up to ~75 minutes (measured 2026-09-13: two sreality listings first seen at 18:19:50Z,
45 s into the run, had no claims and no verdict 27 minutes later). So the always-on worker
(`scraper/realtime_worker.py`, lane `location_intake_fast`) runs the SAME `claims_intake.run()` every
~60 s — `mode="incremental"`, `skip_bodies=True` (no bodies pass, no R2, no forkserver pool), a 45 s
budget over 2 000-row batches, and a **2-minute** lag instead of 15. Its knobs are Railway env vars
(`LOCATION_INTAKE_FAST_{ENABLED,INTERVAL_S,LAG_S,BUDGET_S}`), it ships LIVE, and `ENABLED=0` idles it.
**The short lag is safe only because the two schedules do not share a cursor.** The lag guards the
bigserial race above, so at 2 minutes the fast schedule will occasionally skip a late-committing id —
and `location_claim_batches` resumes on `(lane, source, scan_mode)`, so the hourly run, still 15
minutes back on its own `location_claims_intake` cursor, re-reads exactly that slice within the hour.
Mining a listing twice costs nothing: claim fingerprints are `ON CONFLICT DO NOTHING` and the resolve
enqueue is a bump. **It mines new page BODIES too (W7-a2)** — six of the nine portals put a listing's
location only in the stored body, so a JSON-only fast lane left their listings waiting the hour out:
the tick runs the JSON half first and gives the bodies pass the remainder of its budget, capped at
`LOCATION_INTAKE_FAST_BODIES_CAP` (300) bodies checked BETWEEN batches, over its own lane-scoped
W6-b cursor — so the corpus is walked once, on the first tick after a deploy, and every tick after
that returns only new bodies. The extraction is the same forkserver path, two workers wide, in ONE
`page_readers.ExtractionPool` reused across ticks (rebuilt when the contract data moves or a worker
dies; `EXTRACTION_TIMEOUT_S` is what keeps a pathological page from ever holding a tick). The fast lane also never inherits a stopped full walk (`_full_walk_handoff` looks
that cursor up by lane and the fast lane never runs `--mode full`), and it projects the portal
contracts from the image once at startup — a failure there is a warning, because
`location_claims_intake.yml` is the authoritative projector. **Expected latency, detail write to
Browse:** ≤2 min lag → ≤1 min tick → the resolver drain's own worker lane (~15 s) → `browse_list`'s
pg_cron rebuild, `*/15`. So a claim and a verdict land within ~3–4 minutes, and the read model — not
the claim lane — is what the operator now waits on. **A batch that loses a LOCK runs again (W12):**
55P03 and 40P01 roll the transaction back without moving the keyset cursor and the batch is
idempotent, so both loops retry THE SAME batch (2 s doubling to 30 s, counters rolled back with the
transaction, `lock_retries` in the summary) and only five consecutive losses — or any other error,
a lost connection above all — stamp the run `failed`; the ceiling is `INTAKE_LOCK_TIMEOUT_S`, 20 s
rather than 5, because waiting behind one of the drain's four concurrent slices is now the normal
case. **And the minute lane yields while a GitHub run holds the lane:** one bounded read for an
`outcome='running'` row of the hourly lane younger than 65 minutes (older is a stale stamp from a
killed run) skips the tick with a `yielded_to=<batch_id>` heartbeat note — nothing is lost, because
the hourly run re-reads its own 15-minute-lag slice, and only new-listing latency degrades to that
run while a full walk is in progress.

**A PAGE THAT CARRIED NO LOCATION GETS A SECOND LOOK (W8).** ceskereality and realitymix
geocode an ad AFTER publishing it. Measured 2026-09-14, of the audit page's active "no data" rows
(`location_pin_audit_mv`, `state='unresolved'` + `quality='active_no_claims'`) 56 of 63 ceskereality
and 56 of 112 realitymix listings had been fetched within TWO MINUTES of first sighting and never
fetched again: our one detail fetch read the page while its location fields were still empty, the
index card never changed afterwards, so nothing ever re-enqueued them and the listing had no claims
for ever. Checked live the same day, ceskereality 3876635 now carries "Zlín, ulice Mlýnská" and
coordinates — so the fix is a re-fetch, not a contract change. The `location_refetch` lane in
`scraper/realtime_worker.py` runs once a day (`LOCATION_REFETCH_INTERVAL_S`, first tick ~5 minutes
after start, `LOCATION_REFETCH_ENABLED=0` idles it), asks the audit relation for those rows — joined
to `listings` by primary key, so the URL it aims at and the row's liveness are current and not an
hour-old snapshot — keeps the ones whose newest stored page or payload is older than
`LOCATION_REFETCH_MIN_AGE_S` (6 h, so a listing discovered today gets its second look on tomorrow's
tick), and enqueues at most `LOCATION_REFETCH_CAP_PER_SOURCE` (500) per portal, longest-unfetched
first, into `listing_detail_queue` at `QUEUE_PRIORITY_VERIFY`. It enqueues and nothing else: the
drain re-fetches and rewrites the page, the fast intake lane mines the new body within a minute, the
resolver answers. `enqueue_location_refetch` is its own writer rather than `enqueue_detail` because
the two directions differ — `LEAST` on priority (a second look must never promote a row above the
new listings the drain is fetching) and `given_up=false, attempts=0`, since the rows it most wants
are exactly the ones the drain gave up on and no claim can see (rule #5). The bound is what makes it
polite: bazos's share of that bucket is dead ads whose page answers with the category listing, which
raises `ListingGoneError` and delists the row, so a portal carrying thousands of them drains over
days instead of flooding one drain pass. A daily lane never reads as a dead one —
`check_worker_lane_stall` alarms on `in_flight_s` (a pass running too long), never on the gap
between passes.

**The cursor is the lane's only memory, and every run has a budget.**
`location_claim_batches.cursor_after_id` holds a `listings.id` in full mode and a
`listing_snapshots.id` in incremental mode. Full mode resumes only from a budget-`stopped`
predecessor — `ok` there means the table was walked, and the next full pass is the contract-bump
re-walk from 0; incremental resumes from ANY terminal outcome, because its cursor is a position in an
append-only log rather than a coverage claim, and it only ever advances past a batch whose
transaction closed. `--max-seconds` defaults to 2400 **in the CLI**, not only in the workflow; a
batch does not START unless the previous batch's measured duration fits what is left, and the run's
backlog readout runs AFTER the terminal stamp, so it cannot push the job past the 55-minute ceiling
and lose the cursor of a run that had otherwise finished cleanly — and it is counted **from that
run's cursor** under a 60 s ceiling of its own (W6-b2), so "backlog remaining" means what the next
pass would mine from there and a count that overruns reports `?` instead of a 600 s tail. **The lane self-chains while it has
a backlog**: GitHub fires an hourly cron ~7 times a day, so a contract bump's 250 000-body backlog
would drain at ~6 000 bodies a fired tick. A run dispatches ONE successor with its own budget when it
reports an unfinished half THAT IT MOVED (`bodies_pass_complete=false` with `bodies_mined>0`, or
`reached_end=false` with `listings>0`) — the progress term is the loop breaker — and it **yields**
first: if any member of the `location-batch` group is already waiting the chain ends, because the
group holds one pending slot and GitHub supersedes the OLDER entry.

**ELEVEN CLAIM TYPES, AND THE CONTRACT RAILS.** `contracts/portals/<portal>.yaml` × 9 declares every
extractor (permanent id, surface, licence class, caps, priors, exclusion zones) and
`location_data/contracts.py` projects them into `portal_contracts` / `portal_contract_entries`,
idempotent per `contract_version`, refusing a changed body under a loaded version — the YAML is data,
git stays the store of record. The vocabulary is eleven types (`coordinate`, `precision_declaration`,
`country`, the four admin names, `street_name`, `house_number_cp` / `_co`, `psc`); the Postgres enums
keep their retired labels (an enum cannot shrink in place), so the loader's vocabulary is a strict
subset of the enum's. The loader enforces the SHAPE: six legal top-level keys (`portal`,
`contract_version`, `persistence`, `exclusion_zones`, `regressions`, `extractions`) and an unknown
one is a refusal, not a shrug — every key in this format fails OPEN when misspelled; **at most one
entry per claim type**; an `obec_name` entry **mandatory and naming a reader**, because a contract
that cannot state the town cannot satisfy the invariant the store exists for; and EVERY entry naming
a reader — "declared ahead for a later wave" is how a fleet grows 47 entries that extract nothing.
Nine contracts, **67 entries**, 4 to 11 apiece; what a portal does not publish is an omission
recorded in its report, never a placeholder entry. **Entries are immutable**: a fix is a version
bump, never an edit, so a claim's `extractor_id` always names the rule that produced it — hence no
per-portal branch in the intake, a new signal is a YAML entry. A locator may name an ordered `fallback` list of alternative paths, each with the transforms ITS
shape needs, and the reader takes the first that answers — that, not a second entry, is how a portal
whose payload changed shape keeps reading the older one (sreality@3: 30,265 delisted rows were frozen
on the pre-cutover JSON, whose whole address is one line in `/locality/value` with the pin in `/map`,
and every rail the primary locator has applies to each alternative). Two further rails: `ReaderContract`
records each reader's whole `locator` appetite (the keys it requires plus the ones it merely reads),
derived back out of the reader bodies by an AST scan over each reader and the helpers it delegates
its locator to, and a locator key outside that union is refused — a declared key no reader consults
is a rail that looks enforced and is not (bazos' pin entry named a `pattern` its reader ignored, so
the portal had no coordinate while the contract read as though it published one). And a page-reader
entry may only be declared for `page_kind: detail`: `_BODY_JOIN` selects that kind and nothing else,
so any other kind is unreachable by construction. `fetch_config` is still refreshed in place on a
re-load, because `persistence` is outside `contract_sha256` — an archive-config edit is deliberately
not a version bump, and a bump would re-stamp the claims corpus.

**`idnes@4` (2026-09-13) is what a bump looks like under that shape:** the portal's one `country`
entry moved off the address tail onto the `viewDetail` dataLayer's own `listing_localityState`
alpha-2, because idnes sells 38 countries and `address_part_country`'s closed name table spells 18
of them — 3,263 active foreign listings sat `undetermined` with a Croatian town as their only claim.
The new `foreign_country_code` transform drops the `CZ` that field carries on every domestic row:
foreign is a determination, never a default.

**A contract BUMP does not supersede the old version's rows** — `location_claims` is append-only and
its fingerprint hashes `extractor_version`, so a bump inserts new rows beside the old ones, and the
superseded row has the LOWER id, which wins every "first admissible claim of this type" tie. So the
resolver's claim projection (`_claims_sql`) admits a claim only when its `contract_entry_id`
belongs to a contract version that is a listing's NEWEST EVIDENCE **for that claim type**, plus
operator claims, which carry
no entry by construction (`contract_entry_id IS NULL` + `licence_class = 'operator'` — named explicitly, so a
portal claim that lost its entry id is NOT let through). Filtering at READ is what made deleting the
superseded rows a cleanup rather than a correctness step, and **W6-a took it**: a claim under a
retired contract version is now DELETED — 9.5 M of 13.2 M rows, the large majority of the table —
by `.github/workflows/location_claims_retire.yml`, which `\copy`s the doomed rows to a gzipped CSV
artifact (rule 1's backup, 90-day retention) and then runs `scripts/location_claims_retire.py` in
20,000-row id-keyset batches, bounded, paused and resumable. It enqueues NOTHING: the resolver never
read these rows, so no verdict can move — which is the whole difference from `contracts.py
--retract`, the mechanism that withdraws a version's evidence BECAUSE it was wrong and must
re-resolve every listing it touched. Re-dispatch the workflow after any future retirement.

**A CONTRACT BUMP NEVER BLACKS OUT (W11, 2026-09-14), AND A PARTIAL RE-MINE NEVER BLANKS A LISTING
(W18-b, 2026-09-17).** W1-c spelled that rail `pc.is_active`, and the resolver then read the ACTIVE
version's claims or nothing — so in the 6–8 h a re-mine takes, a bumped portal's listings were judged
with NO evidence at all. On 2026-09-14 a W9 bump of eight contracts at 05:58Z met a full-resolve
sweep at 06:42Z and 595,816 rows came out `unknown/undetermined/low`; under W5 Browse fell from
~350,000 active rows to 45,810. W11 made the rail read **a listing's newest evidence** — the highest
contract version present that is `<= the active version` — but partitioned per (listing, PORTAL), and
**a portal's entries are not re-mined in one hop**. W18 bumped bazos 6 → 7 for ONE payload entry
(`street_name` off `raw_json` /title): the payload lane mined it across all 147k listings in a single
hop while the same run's BODIES pass (the four PAGE entries — `obec_name`, `psc`,
`precision_declaration`, `coordinate`) is bounded and reached 56,905 of ~155k pages before the chain
yielded. For ~90k listings the newest version present was therefore 7 and carried the street ALONE:
the town, the PSČ and the pin sat unread at version 6, and 29,545 of 50,598 live bazos listings went
`undetermined` with no geom (61,396 rows at granularity `unknown`).

So the rail is **PER CLAIM TYPE**: for each claim type the portal's ACTIVE contract declares an entry
for, a listing's claims come from the newest version `<= active` that carries a claim OF THAT TYPE —
`max(pc.version) OVER (PARTITION BY listing_id, source, claim_type)`, so each type keeps its best
evidence and a re-mine upgrades them one at a time, while the supersession the rail exists for is
unchanged (a re-mined type never reads the old version's answer beside the new one). A claim type the
active contract **no longer declares is not read at all** (an `EXISTS` over the active contract's
entries — not a join, because the same type may be declared on several surfaces), so a deliberately
dropped entry stops being evidence when it is dropped rather than lingering forever at its last
version, and `location_claims_retire.py` stays the only thing that DELETES those rows. Measured on
prod over a 250-listing slice the W11 shape was 11 ms / 1.2k buffers, index-driven; the per-type
partition adds a semi-join against one contract's entries. `claim_set_hash` fingerprints the CONSUMED
set including claim ids, so the re-mine's new rows change the hash and the listing re-resolves
normally. **Retirement waits for the re-mine**: those older rows are live data until the
page has been re-mined, so `location_claims_retire.py` refuses (exit 3) while any portal still has a
SERVED listing carrying no claim under its ACTIVE contract, and names the number. There is no
`--force` — the answer is to wait for the intake lanes. **Licence
enforcement rides the same predicate**: `licence_class` is the program's single licence vocabulary
and `ephemeral_display_only` (Mapy.cz-class) its poison value; `listing_location` carries no
`position_licence_class` column because `licence_class IN ('portal','operator')` is part of
`_CLAIMS_SELECT` — such a coordinate is not refused at the winner, it is never READ (a partial index
keeps the remediation set one indexed predicate away). Nothing mints one any more: the Mapy geocoder
and the `mapy_affected` inventory that policed it are gone, so a coordinate claim comes only from a
portal's payload or its own page, and every other stamp is class E outright.

**THE RESOLVER IS FOUR STEPS** (v5, `location_data/resolver/`), and one answer row:

* **BIND** (`bind.py`) picks the finest RÚIAN entity the claims justify — a portal registry key;
  obec + street + čp/čo; a street inside the constraining obec; an obec / část obce by name; a PSČ
  set; the pin's containing obec; the nearest obec within the 250 m sliver tolerance; last the okres
  or kraj alone — resolving homonyms locally inside the constraining parent (PSČ, okres/kraj,
  cadastral territory, qualifier, and only then the coordinate as a tie-break). The tail of that
  chain is what keeps a border pin or a region-only listing from having no town at all, which rule 25
  does not allow: each answers at `low` confidence, and a sliver is NOT a dispute, because a polygon
  edge is not a disagreement. **The pin BIND reverse-geocodes from is the pin the row publishes** —
  elected by DECLARED QUALITY and only then by claim id, so a listing carrying a blurred pin and a
  precise one takes its town from one and its `geom` from the other. The four rungs whose entity was
  INFERRED rather than named (a PSČ lookup, a reverse geocode, the sliver, a bare region) contribute
  no agreeing field, so they cannot grade above `low`. **A COMPOSITE LOCALITY NAME IS BOUND AGAINST
  THE REGISTER** (`composite.py`, W9, operator ruling 2026-09-14): when the line names no obec, the
  whole string is matched first at obec / část obce / MOMC / správní obvod — so "Frýdek-Místek" stays
  a town and "Praha-Řeporyje" binds the městská část and resolves up to Praha — and only if nothing
  matches whole is it split on the portals' separators, a token naming an obec (or a uniquely named
  part) anchoring the rest INSIDE that town, which is what makes "Praha 4 - Podolí" publish Praha and
  Praha's own Podolí rather than the village of that name in okres Brno-venkov. It fails CLOSED: an
  ambiguous name with nothing to anchor it, or a line that matches an okres or a kraj ("Brno-venkov"),
  binds nothing and the row stays unresolved. The readers no longer interpret a locality at all — the
  `statutory_city_obec` regex over eight hand-typed city names and its `address_part_cast_obce` mirror
  are deleted, the claim carries the portal's line verbatim, and both names on the answer row are the
  register's own spelling. **A STREET STATED INSIDE A LINE IS BOUND THE SAME WAY** (`composite.resolve_street`,
  W18, operator ruling 2026-09-16): a portal states a street inside a line as readily as it states a
  quarter inside one — bazos' headline is "Prodej bytu 3+1, ul. Jiráskova, Mladá Bolesl" and its
  parser's own reading is "Kladno - Dubí, Ke Křížku" — so EVERY street claim is split on the portals' own
  separators — a value carrying none is simply one segment — and each segment is matched EXACTLY
  against `ruian_streets` inside the anchoring obec. ONE binder and one answer: whether a claim reached
  the exact matcher used to turn on whether the portal happened to write a comma, so a comma-less
  headline fell through to the trigram rung and bound a street out of prose. Three rules the locality binder does not
  need: **both keys** (the register keeps `náměstí`/`třída`/`nábřeží` in `name_norm` while S1 parses
  them off, so claim and register are each folded both ways and matched on the pair — worth 215
  titles that bind only with the generic word kept); **no trigram unless the CONTRACT calls the claim an
  address field** (R3 is for one claimed name with a typo in it; over a headline it binds a street the
  ad never named — "Byt Slunečná" scores 1.0 against Slunečná while "Prodej domu Slunečná" scores 0.429
  and binds nothing, i.e. coverage decided by title length. The bazos title entry declares
  `claim_confidence: low`, meaning *a headline, not an address field*, and the resolver obeys the
  declaration — no rule names a portal. It is also why a title cut at bazos' 60-character cap, 21,930
  of them, simply fails: there is no prefix matching anywhere in this lane. And a TIE is not a typo —
  where the exact matcher fails closed on two register rows, R3 does not run either); and **not a place** (a segment naming the anchoring obec or
  a část obce inside it is never a street candidate — 76 register streets across 20 obce are spelled
  exactly like a část obce of their own town). It fails CLOSED on two distinct street codes across
  the segments, and a bound segment carrying a house number reaches R1 rather than stopping at R2.
* **FILL** (`fill.py`) joins the hierarchy off the bound ids: ONE `admin_chain` read returning the
  unit itself ahead of its ancestors. Administrative names and codes are ALWAYS the registry's own
  spelling; **the street is the REGISTER's or it is nothing** (W18) — an unbound claim text is no
  longer copied through preserve-if-null, because a `street_name` with `ulice_kod` NULL cannot be
  joined, filtered, compared across portals or de-duplicated on, and the 1,864 production rows in
  that state included the hallucination class the rule exists to stop; čp / čo / psč still fall back
  to a claim, preserve-if-null, and only an operator correction outranks the registry. It also fills
  **the position: the portal pin when admissible, else the finest bound unit's point on surface** — the boundary's stored
  inscribed-circle centre, inside the polygon where `ST_Centroid` need not be, read off the same
  chain rather than as a tenth registry question. It WALKS that chain, because RÚIAN draws no polygon
  for a část obce or a městský obvod and `ruian_streets` carries no geometry at all, so the finest
  bound unit is often precisely the one with no point of its own; it refuses `stat` and
  `region soudržnosti`, whose point would place a listing at the centre of the country. Grade,
  confidence and radius are untouched — the LEVEL is what says how coarse a position is — and
  `disputed` is never set by this path. Foreign and undetermined rows bind no Czech unit, so they
  keep `geom NULL` by construction.
* **GRADE** (`grade.py`) is two tables: `match_confidence` from how many INDEPENDENT fields agreed
  with the bound entity (`exact` an address point the pin corroborates, `high` ≥ 2 fields, `medium`
  one, `low` a tie-break or nothing), and `uncertainty_radius_m` from a per-level constant dict
  carrying migration 383's own v1 numbers — floored by the STREET's extent when a street placed the
  row (W18), so a radius can never understate the thing the position came off. A portal's declared
  precision reaches the confidence and never the grain (see the position ladder below).
* **CHECK** (`check.py`) decides the country and whether the row disagrees with itself. `disputed` is
  ONE nullable text column whose value IS the reason: `pin_outside_obec` (the pin is kept, the
  granularity drops to the admin level; asked only when the town came from a CLAIM, since on BIND's
  pin-derived rungs the comparison is circular), `pin_outside_cz`, `country_conflict`, and
  `pin_off_street` (W18 — an EXACT pin overridden by the street it cannot be on; the granularity does
  NOT drop, because the address identity is the half that bound to the register and the coordinate is
  the half that lost, and GRADE caps such a row at `medium`). A Czech admin
  unit that BOUND at any level is itself a country determination, because the gazetteer it came out
  of is the Czech one. **Foreign is a determination, never a default** — nothing bound and no foreign
  signal is `undetermined`.

**WHERE THE ROW IS PLACED IS ONE LADDER** (`bind.place`, W18), and every rung of it is the register's
before it is a portal's:

    registry ADDRESS POINT  >  bound STREET point  >  portal pin  >  FILL's unit point

`ruian_streets` carries no geometry, so a bound street's point is DERIVED — the centroid of its valid
address points, with an EXTENT that reaches the FARTHEST of them (all but 4 of the 83,451 live streets
have at least one live address point; the other four simply have no point, which the code answers with
`None` rather than a guess). Half the bounding-box diagonal was the first cut of that extent and it
excluded a real address point on 65 % of streets — a door of the street reading as off it. The street beats the pin in
exactly three states: there is no pin; the pin is DECLARED blurred or approximate (bazos stamps every
one of its pins "Přibližná lokalita", so a street the ad NAMES is strictly better evidence than a
coordinate the portal itself calls fuzzy); or the pin lies farther than `max(REGISTRY_PIN_CONFLICT_M,
the street's extent)` from the centroid. **A pin that loses the position is not thrown away as
evidence**: CHECK's containment tests read the elected coordinate CLAIM, so `pin_outside_obec` and
`pin_outside_cz` survive a register-placed row — without that, bazos (every pin declared blurred) would
have lost the flag on 3,421 rows the moment a street bound, exactly where it says the obec or the bind
is wrong. The extent is in that threshold because a street is not a
point — Jiráskova in Mladá Boleslav spans 1,727 m, and a flat 300 m rule would call half of its pins a
disagreement. An EXACT pin that loses is the one case that IS a disagreement and is stamped
`pin_off_street`; an exact pin that agrees keeps the position, as the finer of two true answers.

**THE GRANULARITY IS THE BIND'S, AND A PORTAL'S DECLARED PRECISION REACHES ONLY THE CONFIDENCE.** A
portal declaring "Přibližná lokalita" is saying its COORDINATE is fuzzy; it is not saying the ad named
no street, and it cannot un-say what RÚIAN holds about the street the ad named. W18 deleted the
`DECLARED_CAP` ladder outright rather than narrowing it: keying the cap on the elected POSITION was
wrong twice over (it published a bazos row with `street_name` + `ulice_kod`, the position on the
street's centroid and `granularity='obec'` at 1 km, and it INVERTED the two labels that are capped but
not blurred — idnes' `no_exact_address`, 66,165 listings, and sreality's `not_address`, 13,176 — because
a pin AGREEING with the bound street stayed the position and was capped while one CONTRADICTING it lost
the position and was not), and keying it on the PIN-DERIVED rungs instead was correct and INERT: R7/R8
already grade `obec` and every cap value is at or coarser than that, so the table could not change an
answer on any input. What a declaration still does is rank the pin against a blurred sibling and cap the
confidence at `medium` — a blurred pin is a weak witness however good the bind is.

It is a **pure function**: no wall clock, no network, no randomness, enforced by an AST scan, so a
row replays byte-identically from its inputs and the three version ids stamped on it
(`claim_set_hash`, `resolver_version`, `registry_version`).

**THE SWEEP IS THE INVARIANT'S BACKSTOP, AND THE QUEUE IS RE-ENTRANT.** `_SWEEP_SQL` — ONE statement,
driving off `listings` in id windows — enqueues every listing whose row is missing or carries a stale
`resolver_version` / `registry_version`, which is also how a version bump reaches the corpus, plus a
fourth arm: `obec_kod IS NULL AND country_status <> 'foreign'`, so every Czech listing without a town
is re-resolved nightly (`location_resolve.yml`, 03:17 UTC, `mode=full-resolve`) until it has one or
is determined foreign — the version arms could not express that, because a townless row is stamped at
the CURRENT version tuple. Every evidence-producing enqueue (`claim_insert`, a retraction batch, an
operator edit) is `ON CONFLICT (listing_id) DO UPDATE SET enqueued_at = now(), attempts = 0,
next_eligible_at = now()`, and every statement that FINISHES a queue row (both deletes and the
failure stamp) is bounded by the `enqueued_at` the slice claimed, so evidence arriving mid-slice
leaves the row queued instead of deleted-unresolved; the sweep alone uses `NOT EXISTS` + `DO NOTHING`
, because it carries no evidence and a bump there would reset a poisoned row's backoff and push the
queue's oldest row to the back. A listing with no live claim gets an `undetermined` row (granularity
`unknown`, no position) instead of no row, so coverage is `count(listing_location) = count(listings)`
by construction.

**THERE IS NO COHORT: THE LANE COVERS EVERY LISTING** (W15, operator ruling 2026-09-14). W1 scoped
the walks and the sweep to a "served set" — `l.is_active OR EXISTS (properties pr WHERE
pr.repr_listing_ref_id = l.id AND pr.status = 'active')`, the constant `SERVED_LISTING_PREDICATE` —
to save the payload half of every hop on delisted rows. It was a cost shortcut, and it became a
SECOND definition of "what counts" next to the consumer rule: the 87,756 listings it excluded are
legitimate listings, they must be walked, mined and re-resolved like any other, and while their
location cannot be determined they must be VISIBLE on the audit page. So the constant is deleted and
every walk, sweep, rail and audit surface drives off `listings` unfiltered (migration 505's partial
index on `properties (repr_listing_ref_id)` stays — append-only, and every read model that joins
`listings` on that column still uses it). The one rule left in the programme is the CONSUMER rule
`SERVED_LOCATION_PREDICATE`, below, and it decides what is *shown*, never what is *worked*. **The red
line**: `check_location_town_coverage` (`scripts/verify_pipeline.py`) reports, per portal and in
absolute counts, listings with no row and non-foreign listings with no `obec_kod` over the portal's
WHOLE corpus, and is RED until both are zero — its per-portal series changed meaning on 2026-09-14
and numbers either side of that date are not comparable. That is the invariant the whole shape exists
for; the mandatory town entry, BIND's tail rungs and the sweep's fourth arm are all rails that serve
it.

**ONE LABEL, ONE CODE PREDICATE.** Every surface renders `location_display_label(...)` (migration
503, one IMMUTABLE SQL function over seven columns): foreign country code, else street + čp/čo +
obec, else část obce + obec, else obec, else NULL. It is published by `browse_projection`,
`listing_feed_public`, `listings_public`, `broker_listings_public` and `properties_public`
(`pipeline_board_public` reads it back off `properties_public`, because it is `security_invoker`
while `listing_location` is revoked from `authenticated`), and the API's raw-SQL surfaces call the
same function with the same columns — where eleven sites used to compose a place string out of five
legacy columns in five different assemblies, the SPA and the extension falling back in opposite
orders, so one listing could be labelled two ways at once. Every place FILTER is
`<level>_id = any(codes)`, plain equality at four levels (`region_id` / `okres_id` / `obec_id` /
`cast_obce_id`), compiled in exactly two places (`api/location_filter.py` and
`frontend/src/lib/districtCodes.ts`) plus two SQL bodies (`browse_stats_properties`,
`browse_map_cells`), all four tested against ONE table, `tests/fixtures/district_chip_plan.json`.
A street / POI / address chip filters at its CONTAINING OBEC; a chip with no code matches NOTHING
(`NO_MATCH_CODE = -1`, fail closed); chips saved before codes existed are resolved once at read time
against `ruian_name_index` — in-process for the watchdog matcher, over `POST /maps/resolve-names` for
the SPA (`frontend/src/lib/useLegacyChipUpgrade.ts`, still called by Browse and the pipeline board) —
without ever rewriting the stored blob. Because RÚIAN draws no polygon for `cast_obce` or `momc`, a
POINT resolves only to obec / okres / kraj and the quarter is placed BY NAME inside the PIP'd obec.

`browse_projection` re-sources `obec_id` / `okres_id` / `region_id` from
`ll.obec_kod` / `okres_kod` / `kraj_kod` and `lat` / `lng` from `ST_Y/ST_X(ll.geom)`, and appends
`cast_obce_id`, `uncertainty_radius_m` and `granularity_rank`. The last two are what the map DRAWS: a
pin the resolver placed **below building level** (rank < 90, `location_granularity_rank`) is an open
ring and one at or above it a solid dot, so "middle of the village" and "this front door" stop looking
identical; clicking a pin draws its true-metre circle of `uncertainty_radius_m` for as long as its
popup is open, and the popup names the rung and the radius. (W3-3 first drew that circle under every
such pin at once — with ~87 % of active pins below building level it buried the map, 2026-09-22.)
Clusters and server-side grid cells carry no per-pin radius, so all of this exists only in point mode.
**Appending is the only legal edit here** — `browse_list` and `properties_map_mv` materialize
`select * from browse_projection` and `toolkit/browse_read_model.sync_browse_list` re-inserts
POSITIONALLY, so anything computed outside the view, or any reordering, writes NULLs into the wrong
columns silently. An unresolved row's re-sourced codes and pin are NULL, and a NULL `lat` drops the
row out of `properties_map_mv` — the intended posture (no pin the resolver would not stand behind),
and the reason the red line gates everything downstream. Browse and the map read `browse_list`, which
COPIES those fields at rebuild, so no consumer query joins the store; a field is added only after a
measured slowdown, and only there.

**THE CONSUMER RULE** (operator ruling 2026-09-13, W5). A listing is SERVED to consumers only when
`listing_location` has an ANSWER for it — a point, or the determination `country_status = 'foreign'`;
everything else stays invisible until the lane resolves it. It is not a flag and not a column but a
predicate over the store itself — `location_data.claims_common.SERVED_LOCATION_PREDICATE`, ONE text
rendered onto each surface's own listing-id expression — so it covers the 36,981-row migration-510
audit set, every future listing whose page carries no location, and nothing else; a row returns the
moment it has a geom, with no backfill and nothing to re-stamp. It is carried by the two LIST
surfaces — `browse_projection` (keyed on the property's display listing, so `browse_list`,
`properties_map_mv`, `browse_stats_properties` and `browse_map_cells` all inherit it) and
`listing_feed_public` — plus, in code, the watchdog matcher (`api/notifications._build_match_clauses`,
which reads `properties_public` and so cannot inherit) and path C candidate generation
(`toolkit/dedup_candidates_sql`); `toolkit/comparables._shared_filter_where` needs no clause because
its `ll.geom IS NOT NULL` is the same rule minus the foreign arm. **DETAIL STAYS REACHABLE**:
`listings_public`, `properties_public` and `pipeline_board_public` are read by id, so a direct link,
the extension, the audit page's own links and an operator's pipeline card (rule 22 operator state)
all keep working on an unresolved listing. Measured at the ruling: 44,702 of 711,600 Browse rows,
2,953 of them still-live ads. `check_location_town_coverage` reports the count per portal as
`hidden_n` — a workload number that never moves the check's status. The operator watches the same
set on `/new-dedup/pin-audit`, whose relation `location_pin_audit_mv` (migration 524) IS that
definition — every listing that fails `SERVED_LOCATION_PREDICATE` and nothing else, refreshed hourly
by pg_cron, leading the nav with its count — so the page and the rule can never describe different
sets. W15 widened that cohort from ~44 k to ~80 k by retiring the served set: a delisted listing with
no decided location is a finding like any other, and live-vs-delisted is one of the page's `quality`
filters rather than a reason to hide the row. It carries no map: the whole subject is rows with no point to draw. Migration 510's
property-and-legacy-pin version of that relation stays on disk as history (append-only, rule 1); it
was retired by 513 because it read five `properties` place columns 508 drops, and 514 re-creates the
name on inputs that cannot expire the same way.

**THE REBUILD BUDGET SCALES WITH THE CORPUS** (W13, migration 522, after the 2026-09-14 freeze).
`browse_list` and `properties_map_mv` are whole-relation rebuilds of `browse_projection` on pg_cron
(`*/15` and `7,37`), so their cost tracks the corpus and their `statement_timeout` must too. It did
not: as the location store recovered to 712,932 rows with a town the list rebuild grew 173 s → 257 s
→ 405 s → past its fixed 600 s budget, timed out 11 times in 6 h, and froze Browse at 315,827 active
rows for two and a half hours while each tick threw away ten minutes of work. Two lessons landed
with it. **The cost:** W5 appended the consumer rule to `browse_projection` as an EXISTS against a
SECOND alias of `listing_location` — but the projection already LEFT JOINs that table on the SAME
key for the label and the chip codes, and `listing_location_pkey` is UNIQUE on `listing_id`, so the
answer was already in hand and the EXISTS bought a second full pass over an 821k-row table (join
skeleton 283,226 → 229,197). Both spellings are sanctioned and
`tests/test_browse_read_path_guardrail.py` accepts either, provided the rule still names
`listing_location` and still carries BOTH arms. **The lock:** both rebuilds took a SESSION advisory
lock released in an `exception when others` handler, and PL/pgSQL's OTHERS does not match
QUERY_CANCELED — so every one of the 11 cancellations skipped the release. Nothing wedged only
because pg_cron opens a fresh connection per run; from a pooled session the first cancel would have
left the key held and every later tick would have returned "skipping tick" forever while
`cron.job_run_details` reported success. Both now use `pg_try_advisory_xact_lock`, which Postgres
releases on commit, rollback AND cancel. **And the blast radius:** the failing `*/15` job kept the
box in `DataFileRead` for 10 minutes in every 15, the pg_cron scheduler lagged past the minute
boundary, and pg_cron does not catch up a MISSED slot — so `browse-map-rebuild`, whose whole
schedule is two single minutes an hour (:07 and :37, both inside a rebuild window), stopped firing
entirely for 3.5 h with no run row and no log line. A starved job looks exactly like a wedged one;
`pg_locks`, an orphaned `_next` relation and `pg_stat_activity` are what tell them apart.

**WHAT REMAINS OUTSIDE THE STORE, AND WHY.**

* `admin_boundaries` — price stats, the rent map and city proximity still read its geometry and
  population. Its LOCATION role died with trigger 289; re-keying those three onto
  `ruian_admin_unit_geometries` is a later wave. `curated_cities.admin_boundary_id` is an FK to it,
  and already the RÚIAN obec code.
* `portal_raw_pages` / `portal_raw_payloads` — the preservation substrate, and the intake's second
  source; not a location path (`tests/test_portal_raw_pages_guard.py` fails CI on any DROP naming
  it). With it `listings.raw_json`, the content-hash substrate (rule 2) and the resolver's evidence,
  so the legacy place keys live there as history forever.
* `listings_public` is **44 columns — exactly its readers** (W6-c, migration 517): the SPA's
  `DETAIL_COLS` listing-detail select, of which `api/notifications.py` reads 15, `api/curation.py` 2
  and the five dependent matviews 8. W4-c had left it 61 wide because a matview's dependency is on
  the VIEW, not on its columns, so `create or replace` (append-only) was the only shape available;
  517 takes the width **blue-green** — rename the wide view aside, create the narrow one under the
  original name, rebuild each matview beside itself and swap it in under a 5s `lock_timeout` — so
  neither the detail read nor the Health dashboard is ever without a relation. The 17 that went had
  no reader anywhere: four typed-NULL placeholders, the ten legacy place columns `display_label`
  replaced, `broker_name`, and the two condition levels Browse reads off `browse_list`.
  `portal_listing_counts` stays 8 wide and that is the census result, not a deferral: all eight are
  read by `portal_health_mv`, so it is already minimal.
* `ScrapedListing` keeps `locality`, `district`, `street`, `house_number`, `zip`, `lat`, `lon` —
  `locality` and `district` are content-hash inputs, so removing them would churn a snapshot for
  every listing in the corpus (rule 2), and all seven are the parser's reading of the page, which is
  the CLAIM the resolver arbitrates. They are simply no longer columns.
* `CoordinateRule`'s `"geom_column"` substrate literal is a **historical name kept deliberately**: it
  is pinned by six portal contracts, and renaming it would bump every one of them and re-run the
  blackout. For those six portals (bazos, idnes, ceskereality, realitymix, maxima, remax) the live
  coordinate once existed only in `listings.geom`; the resolver's point is the value now, the
  archived page body is the re-derivation path, and migration 508's `pg_dump` is the backstop.
* `location_granularity_rank`; `properties.home_obec_pop` + the eight `near_*` columns (Browse
  filters read them); and `scraper/street.py`, whose extraction is a CLAIM now, not a column.

**THE RÚIAN MIRROR IS VERSIONED, NOT MUTATED.** `ruian_*` (migration 381) holds ČÚZK's address
points, streets, parcels, building objects, admin units and a typo-tolerant gazetteer. Every load
stamps one `registry_versions` row (`ruian:YYYY-MM-DD`) and publishes by **pointer swap** behind
blocking assertions, so it never half-changes the world underneath a resolution that pinned a
version. Křovák S-JTSK → WGS84 goes through ONE audited conversion on an explicitly chosen 1 m PROJ
pipeline (`location_data/krovak.py`; the 6 m one is never used), guarded by a golden-point test;
boundary packs carry three geometries per unit (authoritative, subdivided pip, render). Freshness is
the monthly baseline — the VFR daily-delta lane ships as chain-verification only and fails loudly
until the `ST_ZZSZ` element schema is pinned down.

**OPS RULES THE INCIDENTS WROTE.** The heavy lanes — registry load and claim intake — share the OUTER
`location-batch` concurrency group so **at most one runs at a time** (each keeps its own inner group
at job level); a new heavy lane joins it. On 2026-08-10 four concurrent lanes dropped backends across
the fleet, degraded the live Browse rebuild to multi-minute DataFileReads and wedged two lanes with
no error at all. **The resolve DRAIN is outside the group** (operator decision, 2026-09-10): it is
the one member that is latency-bound rather than instance-bound — a handful of small indexed reads
and one answer-row write per listing, no COPY, no corpus scan, no detoast — and it READS the claim
spine the intake WRITES, so it never carried a must-never-overlap constraint, while at 0.7 listings/s
and a queue above 100k a self-chaining group member starved it to zero ticks in three hours. Its
guards are the job-level `location-resolve` group plus the `location_jobs` lease CAS, which is also
what keeps it exclusive against the always-on Railway worker's resolve lane. **No batch statement
runs without a ceiling**: `statement_timeout = 0` is for genuine bulk phases (COPY, index build,
whole-table rebuild) and nothing else; per-unit and per-batch work arms `SET LOCAL statement_timeout`
in its own transaction (budgets env-overridable, `LOCATION_*_TIMEOUT_S`; gate
`tests/location_data/test_location_batch_hardening.py`). The drain's cost is **round trips, not
work** — from Actions it is network-RTT-bound at ~5–17 listings/s (~75 ms per GitHub↔`eu-west-1` trip
against 0.02–0.5 ms of server-side work) — which is why it belongs on the Railway worker (~1–2 ms
RTT), where a pass drains `LOCATION_RESOLVE_WORKERS` slices CONCURRENTLY (env var, default 4; one
thread and one session-mode connection each, disjoint by `FOR UPDATE SKIP LOCKED`, sharing one
lock-guarded `RunCache` and one lease on the caller's connection), safe only because the resolver has
no cross-listing write left. **A failed BATCH costs one slice, never the worker**: the transaction
rolls back, its rows stay queued exactly as claimed, the loop backs off (2 s doubling to 30 s) and
claims again; only five consecutive failures — or a lost connection, told apart by SQLSTATE because
`QueryCanceled` is an `OperationalError` subclass, and reconnected once — stop a worker. The prefetch
runs on its own 90 s ceiling, because a 250-listing bulk claims read legitimately outruns the 30 s a
per-listing statement gets, and cancelling it threw the whole batch away.

**LESSONS A FUTURE SESSION NEEDS** (2026-09-12 unless noted), one line each:

* **The read-model rebuild budget must scale with the corpus, and a cancelled rebuild must not
  leave a lock** (2026-09-14) — a fixed `statement_timeout` on a whole-relation rebuild is a freeze
  waiting for the corpus to grow into it, and `exception when others` never runs on a cancel.
* **A keyset in the same statement's WHERE is not a fence** — Postgres planned the bodies pass from
  `listings` and applied `p.id > after` as a POST-FILTER, so every batch paid the whole corpus and
  died on the 600 s ceiling with 186,546 bodies queued; a `LIMIT` subquery planned alone stops at
  `cap` rows.
* **A `failed` run must not reset the walk** — full mode restarts at 0 unless its predecessor stopped
  on budget, incremental resumes from any terminal outcome, and `cursor_after_ts IS NULL` guards
  against reading an old-epoch cursor back as a new-epoch one.
* **A yield must count the right runs** — the self-chain's "is anything waiting?" check counted the
  resolve lane's `*/15` drain ticks, which run in a different group; it stopped the chain on a routine
  07:58 tick and handed a 212 000-body backlog back to a cron GitHub fires ~7 times a day.
* **A cursor that restarts on "done" is a full scan on a timer** (W6-b, 2026-09-13) — the bodies
  keyset went back to 0 whenever it caught up, so each idle hourly hop re-walked all 744k payload
  rows (~500k of them unstampable) to stamp nothing: `bodies=416s`. Keep the position and reset it on
  the ONE event that makes the rows below it eligible again — here, a contract bump.
* **A 5 s lock ceiling is not resilience** (W12, 2026-09-14) — two `mode=full` walks died ~80 s
  in (runs 34817669095, 34824922631) because a queue-row bump waited behind a drain slice; a batch
  that lost a lock must RETRY ITSELF (idempotent, cursor unmoved) rather than skip its rows, and of
  two schedules writing the same rows one has to yield.
* **A contract bump must never outrun its re-mine** (W11, 2026-09-14) — reading only the ACTIVE
  version's claims meant the 6–8 h between a bump and the re-mine judged 595,816 listings with no
  evidence and emptied Browse to 45,810 rows; the resolver reads a listing's NEWEST evidence instead.
* **…and "newest evidence" is PER CLAIM TYPE** (W18-b, 2026-09-17) — a bump's entries are not
  re-mined in one hop (bazos 6 → 7 mined the one payload entry across 147k listings while the bounded
  bodies pass reached 56,905 of ~155k pages), so a per-PORTAL partition served the new version's
  street alone and hid the town, PSČ and pin still sitting at the old one: 29,545 of 50,598 live
  bazos listings `undetermined` with no geom. Per type, a partial re-mine costs a listing nothing.
* **An enqueue that no-ops loses the listing** — the 07:13Z contract bump re-mined ~60k listings
  already queued from a ~540k sweep; every enqueue no-opped, the drain resolved them from the OLD
  claims and deleted the rows, and 384,500 answer rows with 135 towns had nothing that could
  re-enqueue them. Hence the re-entrant `DO UPDATE` and the `enqueued_at` fence.
* **A fallback reading a column nobody writes is silent** — BIND's admin-centroid fallback read
  `ruian_admin_units.definition_point`, which the loader has never written: 29 % of towned rows
  (8,706 of 29,892) shipped with no position, which the map would have dropped.
* **A slim of a live table is TWO migrations** — a merge deploys in minutes while a migration is
  applied by hand, so RELAX (drop the CHECKs/NOT NULLs/FKs the new write cannot satisfy) before the
  merge and DROP after the deploy is green; in that window `--retract` must not be run.
* **`create or replace view` can only append** — a view that must LOSE a column is DROP + CREATE and
  is applied AFTER the deploy (a bundle selecting a column the view just lost gets a PostgREST 400,
  while a view carrying a column nobody selects is inert); an additive re-source is applied BEFORE it.
* **A column drop under a live pg_cron schedule does not raise** — it breaks the job silently on a
  later tick, so the schedule is unscheduled IN the migration, ahead of its tables.

**Pin-loss audit page — RETIRED 2026-09-13 (migration 513), kept here for the record.** The ruling
it was built for landed (503 now allows zero new pin loss, exempting the audited residue), and its
matview depended on the five `properties` place columns W4-c drops, so 508 could not run while it
existed. 513 drops the matview, both functions and the hourly cron job; the page is unrouted. The
CI replay skips 510 for the same reason — see `.github/workflows/migrations.yml`. What it was:
migration **510** materialised the exact set migration 503's pin-collapse guard would cost the map:
every active property that still carries legacy coordinates and for which `listing_location` holds
no Czech `geom` (a determined `country_status='foreign'` is excluded — that is a correct answer, not
a loss). Measured 2026-09-13: **36,970 rows — 2,413 live ads** (almost all bazos dead ads, which
#1451 delists over the coming days) **and 34,557 delisted display listings**.
`location_pin_audit_mv` is one row per property at its representative listing, carrying the portal,
the type, the listing's own `source_url`, the legacy pin, the resolver's verdict and four audit
columns. `has_claims` is what the SAVED verdict consumed (`claim_set_hash` is NOT NULL, so an empty
consumption is the digest of the empty list, `sha256('[]')`, never a NULL). `claims_now` is evidence
under an **ACTIVE contract** (`portal_contract_entries` → `portal_contracts.is_active`) — 1,351 rows,
**exactly the `has_claims` set**, which is the finding: the lane has already consumed everything a
live contract offers, so there is no backlog to wait for. The first cut of this view counted ANY
`location_claims` row and concluded the evidence had been mined after the verdict; that was wrong.
Of the newest 366 rows, 361 carry claims and none sits under an active contract — they are bazos
@1/@3/@4 `surface=legacy_column, extraction_method=legacy_column` copies of the legacy `listings`
columns (the Mapy-era pin, the legacy PSČ/locality fields W1-b dropped), plus `archived_html` /
`url_slug_parse` claims under superseded versions. (`old_evidence` named that superseded material so
"no live evidence" was never read as "nothing was ever there" — `legacy` 4.6k, `archived` 32.2k,
`none` 172. **W6-a deleted the rows behind it and migration 515 dropped the column**: a column that
can only say "žádná" reads as a finding. `claims_now` survives as one EXISTS instead of a
three-aggregate lateral — 75 % off the hourly refresh's planned cost.) `sibling_has_pin` is the cheap recovery the operator can take without any resolver
change: **1,163 rows (~3 %)** where another listing of the same property already has a `geom`.
**W7-b (migration 518) puts a `state` above all of it**: `pending` — no `listing_location` row yet,
or the listing sits in `dirty_locations`, or its newest `listing_snapshots.scraped_at` is past the
verdict's `resolved_at` — is the lane still working and NOT a finding; `unresolved` (a verdict
exists, nothing queued, no newer evidence, still no location) is the issue, and the four `quality`
buckets refine that half alone. The page toggles between the two and defaults to `unresolved`; the
nav badge counts `unresolved` only, because a badge that climbed whenever the scrapers ran would
teach the operator to ignore it. Measured at the split: 11 pending, 44,370 unresolved.
**W14 (migration 523) makes the page read against the whole database, and W15 (migration 524)
simplifies the chain to the one rule.** The operator's complaint was that the audit set was a number
with nothing to measure it by. `location_audit_waterfall` is the chain — **9 rows** since W15,
rewritten hourly by the SAME `refresh_location_pin_audit_mv()` run, after the matview so the last
step's split reads the relation the page lists: every listing in the database (841,428) → **judged**
(the lane has a stored verdict for it; the loss is the listings it has never reached) → **located**
(a point, or the determination that it is abroad — split into with-an-obec / foreign / a Czech point
with no obec) → **hidden**, the complement of `located` over the WHOLE database, split into
`unresolved` and `pending`, which is the set this page lists and is read as a share of 841 k rather
than 100 % of itself. W14 shipped it as 14 rows with a served / not-served fork; retiring the served
set removed the fork, and the hidden set grew from 12,068 to ~80 k because the ~68 k delisted rows it
used to exclude are now findings. The one cut is `SERVED_LOCATION_PREDICATE`, rendered verbatim
(pinned by `tests/test_location_w14_audit_waterfall.py`, the W5 rail's shape) — one statement, one
snapshot, no new column on `listings`, and the client renders `n` / `lost` / `share_pct` without
recomputing any of them. Rows are `chain` (the funnel, `lost` = the previous step's count minus its
own), `deduction` (a set carved out: the hidden set) or `split` (sub-rows that partition their
parent); the migration proves the arithmetic laws at apply time. Cost: 19.3 s on top of the hourly
refresh's 900 s budget.
**W16 (migration 526) makes it ONE vocabulary with the NEW DEDUP candidates funnel.** The two
readouts asked the same first questions in different words, and at one step with genuinely different
rules: the funnel's "Known to the location engine" read as *answered* while it was byte-for-byte this
chain's `with_verdict` (*judged*), and its town step was cut with NO consumer rule — so its single
"lost 99,889" silently added three unlike things together (≈53,374 judged-but-not-located, 45,619
**abroad — an ANSWER, which this chain has always booked as a split INSIDE `located`**, and ≈895 a
Czech point with no town). The four booleans are now rendered from
`location_data/location_steps.py`, which also declares the step keys, their order and each key's
shape for BOTH chains: `located_town` is a **split** of `located` here (this page's subject is the
hidden set) and a **chain step** on a candidate run (that page's subject is pairing) — same key, same
predicate, same wording, the role declared in one place. Two consequences. `has_town` gained dedup's
obec RANK floor, so this page's town number IS the number dedup can block on (measured 2026-09-14: 0
rows of movement), and `located_no_town` stays the plain complement so the three splits keep
partitioning `located` by construction. And `label_cs` was **dropped**: wording moved to
`frontend/src/lib/locationSteps.ts`, one file holding Czech and English for every key, so a better
sentence costs no migration and a step cannot be renamed on one page only — and, one level down,
the hidden set's two states now carry ONE Czech name across the waterfall split, the filter pill,
the header total and the note. Two operational constraints ride with the drop, both in 526's header:
the column is made NULLABLE **before** the producer is replaced and dropped only **after**, so the
:25 cron cannot land between two statements and fail; and the migration is applied only once the
merge commit's Railway `vite` rollout is green, because the bundle on `main` still selects
`label_cs` and PostgREST answers 400 for a column the table no longer publishes. A candidate run stamps the
same keys into `candidate_generations.stats.waterfall` with the losses and shares already computed by
the lane, so the two readouts can now differ only by SCOPE (a run may be narrowed to active listings)
and by TIME (a run is frozen; this relation refreshes hourly) — and the candidates page prints both
next to its chain. The cross-surface rail is `tests/test_location_steps_vocabulary.py`.
`quality` buckets the set on active/delisted × `has_claims` — and since W15 `listings.is_active` is
the page's ONLY live/delisted notion; the SPA page `/new-dedup/pin-audit` filters on those four axes
plus the sibling flag and reads its overview matrix from `location_pin_audit_summary()` so the
matrix, the header split and the list cannot disagree. The cohort is ONE arm — `listings` LEFT JOINed
to `listing_location`, `WHERE NOT <the consumer rule>` — instead of 514's union of two candidate
sets, and the cohort CTE is MATERIALIZED so the evidence and snapshot laterals run once per cohort
row (~80 k) and not once per listing. Refreshed hourly by pg_cron
(`refresh-location-pin-audit`, guarded so the replay container skips it) with the budget armed **in
the cron command** — migration 371's rule, or the refresh would silently die at the 120 s database
default. Rule 25's deletion parity did not apply: it was an operator-requested review surface with
a stated end. While it was live it led the TOP-LEVEL nav as `!AUDIT POLOH` (admin-only, first entry,
badged with the unfiltered row count), not buried in the NEW DEDUP dropdown, because it was a
decision waiting on the operator.
**The map's container carries an inline `position/inset/size` style and that is NOT redundant with
Tailwind's `absolute inset-0`**: `globals.css` imports `maplibre-gl.css` AFTER `tailwindcss`, so the
`.maplibregl-map` class MapLibre stamps onto the container at init wins the cascade with its own
`position: relative` at equal specificity, `inset-0` then sizes nothing, and the container collapses
to 0 height — no tiles, no points, an empty panel. Every map in the app (DetailMap, ListingMap,
ComparablesMap) carries the same override for the same reason; the audit map shipped without it and
rendered blank in production.

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
