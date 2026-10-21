---
name: scraper-ops
description: Use when running, debugging, or extending the scrapers — triggering the per-portal index-walk/detail-drain workflows, adding a new scraper field without breaking data, refreshing portal HTML fixtures, reading the pipeline logs (INDEX/ENQUEUE/VERIFY/DRAIN/IMAGES line shapes), the always-on real-time worker (probe/drain/images/count-probe/property-maintenance/broker-maintenance/estimation/location-resolve/location-intake-fast/location-refetch/sold-comps/text-extract/health/autodedup/notifications/push/heartbeat lanes), the visual-signal producer jobs (image pHash, CLIP tagging/retag, DINOv3 corpus embedding on RunPod), or the pipeline verification/alerting harness. Also covers condition-scoring (parked) and image-download workflow cadence. Triggers on: index_walk, detail_drain, gh workflow run, delist_policy, presence check, scrape_runs, fixtures, RUN done, a new listings column, onboarding a portal, reading a scrape log, realtime_worker, sold_comps, clip_tag, dinov3_embed_backfill, compute_image_phash, verify_pipeline, health_canary, health_liveness, health lane, job cell, missing lane, match_once, reason line.
---

# Scraper operations

Operating the scrapers: adding a field, refreshing fixtures, triggering the workflows, and
reading the logs. The per-portal ingest architecture (what each portal is, parser
strategy, completeness posture) lives in `docs/architecture.md` § Data sources. Default
test / log helpers: `scripts/test-summary.sh` and `scripts/logs.sh <run-id> [pattern]`.

## Adding a new scraper field without breaking existing data

1. Add the column with a new numbered migration (`alter table listings add column ...`). Never
   touch `001_initial.sql`.
2. Declare the cell in `scraper/attribute_contract.py` for EVERY portal (producer, source-key
   precedence, absence semantics, sentinels) and read it in the parser through `source_value` /
   `source_label`; a label→value mapping belongs in `scraper/vocabulary.py`, never in the parser.
   Gates A1–A3 (`tests/scraper/test_attribute_contract.py`) read the checked-in key census.
3. Add it to `scraper/db.py` `LISTING_COLUMNS` + `_LISTING_COLUMN_PGTYPE` (read by the ONE writer, `scraper/listing_write.py`) and,
   for crawler portals, `scraped_listing._LISTING_FIELDS`. Whether a parser NULL erases is decided per
   (source, column) by the producer from step 2 (`text`/`none` preserve, `structured`/`derived` clear);
   `_PRESERVE_IF_NULL_COLUMNS` is the GLOBAL identity pair (`published_at`, `source_url`) and must not grow.
4. Backfill old rows from NARROW typed columns via a `scripts/backfill_*.py` dispatch job (never a
   `raw_json` pass — ~62 KB/row detoast); NULL is acceptable for a nullable column. The new cell reaches
   the `parsing` alarm's fill arm only once the OPERATOR re-blesses (`--bless` needs the prod DB).

## Refreshing portal HTML fixtures

Portal media fixtures (`tests/scraper/test_portal_media_fixtures.py`) are saved detail pages scrubbed by
`scripts/fetch_and_anonymize_fixtures.py --scrub-contacts`; never fetch live in tests.

Anonymization scope: phones → `+420 XXX XXX XXX`, emails → `agent@example.cz`, street numbers
(`123/45`) → `XXX/YY`. Listing prices and the surrounding HTML structure are preserved — public
data the parsers need. Agent names are too varied to scrub by regex; if a fixture leaks one,
hand-edit the file. **`http(s)` URLs are masked during scrubbing and restored after** — without
that the phone regex (any 9 consecutive digits) rewrote realitymix photo ids to
`nab_+420 XXX XXX XXX.jpg` and the street regex rewrote idnes CDN shard paths `/thumbs/1/6/e/`
to `/thumbs/XXX/YY/e/`, silently corrupting the only data a media test can assert on.

**Never commit an unscrubbed portal page — this repo is PUBLIC.** A live detail page carries the
broker's mobile, work e-mail and name, and merging one publishes them permanently. For a fixture
kept for its **bytes** rather than its visible text (the payload-normaliser set,
`tests/fixtures/location_w2a_refetch/`), the blanket sweep is the wrong tool — masking *every*
9-digit run rewrites `data-gps-lat`, JSON-escaped photo ids, Tailwind custom properties. Use:
`python scripts/fetch_and_anonymize_fixtures.py --scrub-contacts <files> --name "<agent name>"`.
It seeds phones only from markup that says "phone" (`tel:`, schema.org `telephone`, a rendered
`+420` group, a whole-text-node number, reveal-on-click attributes, **and a JSON `phone`/`mobile`
key** — plain, entity- or backslash-escaped, how an embedded-JSON portal spells it: mmreality's
agent number arrives as `&quot;phone&quot;:&quot;731404040&quot;`, no `+420`, no `tel:` href). It replaces e-mails, **re-encodes Cloudflare's
obfuscated e-mail payloads** (`data-cfemail`, `/cdn-cgi/l/email-protection#…` — an XOR against
their own leading byte, so a committed one publishes the address while matching no plaintext
rule; the placeholder is re-encoded under the page's OWN key, which preserves the per-response
key that is itself measured churn on Cloudflare-fronted portals), and takes each hand-supplied
name in plain, JSON-escaped **and** slugged form (the profile-URL slug is the one that gets
forgotten). Pass `--name` once per name, **longest first** — replacing "Radomír Kočí" before
"Bc. Radomír Kočí, DiS." leaves the longer form half-rewritten.
Same placeholders either way; idempotent, so re-running it on a committed fixture proves it clean.
Two tests in `tests/location_data/test_payload_norm_measured.py` fail if a committed fixture
carries contact details in plaintext **or** in Cloudflare's hex.

**The nine SCRAPER portal parsers are a separate fixture set** in `tests/fixtures/portal_html/`
(`tests/scraper/test_portal_media_fixtures.py`)
above. They exist because a hand-authored fixture can only assert back the strings the test
itself planted, so it is structurally blind to upstream drift — that blindness is what let a
19-day realitymix gallery blackout and a ~63% idnes photo loss both ship with CI green. Never
hand-edit a URL in one; regenerate the page instead, or the fixture degrades into the tautology
it replaced.

## Recovering a field a parser silently stopped extracting

**Every typed `listings` column goes through the ONE re-parse seam**, `scripts/reparse.py` / `reparse.yml`: it replays the portal's OWN `parse_detail` / `parse_advert` / `parse_listing` over a substrate declared once per portal — `portal_raw_pages.html` on the seven HTML portals, `listings.raw_json` on sreality + bezrealitky, which stage no body — so a heal cannot disagree with the live scraper. No re-fetch, and it repairs **inactive** listings too, which a re-fetch structurally cannot. `--fields` is REQUIRED (it writes exactly those and reads the rest only to leave them alone); dry-run is the default. It never writes a `listing_snapshots` row, never blanks a value the re-derive could not produce, never touches `last_seen_at`, enqueues `dirty_properties` in the same statement, and writes a row only while it still holds what the pass read. No snapshot means no autodedup feed claims the healed adverts: `rt` keeps its decisions until each advert is re-claimed (the E960 freshness rule does so before any merge resting on it applies) or the scope is re-seeded.

**Hashed columns.** `--allow-snapshot-deferral` is required for a column in `_HASH_FIELDS`: on the eight portals hashing the PARSED fields the one genuine snapshot is DEFERRED to that row's next detail scrape (an inactive row never gets one). On **sreality**, which hashes the RAW payload, NO snapshot is ever appended and the column diverges from its history for good; its oldest rows also hold the pre-unwrap payload (`hash_id`/`id` absent) and cannot be parsed at all — the run WARNs with the count rather than exiting clean.

`scripts/reextract.py --source <portal> --field <media|broker> [--since YYYY-MM-DD] --dry-run` keeps only the two NON-column recoveries the seam cannot express: `images` child rows and the `raw_json.broker` block. Neither is in `_HASH_FIELDS`, so both are snapshot-free (rule #2), and the module raises at import if either ever joins it. Dispatch via `reextract.yml`; resumable by keyset cursor, so re-dispatch until it reports `recovered≈0`.

For `--field media` it only repairs listings with **zero** image rows. `record_images` upserts on
`(listing_id, sequence)` = gallery position and refreshes the URL only `WHERE storage_path IS
NULL`, so re-parsing a listing that already holds photos and now yields more of them shifts
every later photo's position — downloaded rows keep an old URL at a sequence the new parse means
for a different photo. Partial-loss recovery therefore needs a stable media identity, not a
positional one, and is deliberately not attempted here.

## The payload archive riding the ingest path

**A `detail` body is ALWAYS archived; every other `page_kind` NEVER is.** One page-kind comparison
(`scraper.db._payload_archive_enabled`) — no flag, no per-portal limit, no measurement corpus: rule
25 makes the stored detail body the hourly claim lane's SECOND SUBSTRATE, not an opt-in experiment.
The rule is GRAIN — a `detail` body is ONE listing's page and is mined for that listing's claims;
index/map/gazetteer/snapshot bodies are whole-SURFACE artefacts refetched on the walk cadence that
no listing's claim can come from.

Everything `upsert_portal_raw_page` stages (7 HTML detail writers) passes through it, plus sreality's
estate JSON and bezrealitky's advert-with-query from their own `append_payload_if_enabled` call
sites. Every failure warns and returns — an archive must never break the scrape it rides in.

**Bodies live in R2, so the archive needs the R2 env vars** (the image lane's four) — on all 14
page-fetching lanes since #1074 AND on `location_claims_intake.yml` (which now READS them back),
held by `tests/test_scrape_lane_r2_env.py`; MISSING when the path was first called ready, and
INVISIBLE: the append warns `payload archive needs R2 for …` and archives nothing while the scrape
stays green. Railway's worker: own dashboard. Over `LOCATION_PAYLOAD_R2_THRESHOLD_BYTES` (2048,
TOAST) compressed spills to `body_r2_key` — every portal but bezrealitky, whose small bodies archive
inline. Knobs: `LOCATION_PAYLOAD_VERSION_CAP` (2), `LOCATION_PAYLOAD_MIN_APPEND_INTERVAL_DAYS` (7,
per-listing time floor; 0 disables), `LOCATION_PAYLOAD_STATS_EVERY` (200).

Verify with `select source, page_kind, count(*), max(version_seq) from portal_raw_payloads group by 1,2;`
— append-on-CHANGE, so an unchanged refetch must add no row; `detail` is the only kind you will see.
Failures read `payload archive append failed source=… key=…` in the walk or drain log and are never
fatal — so **a broken archive looks like a healthy scrape**: `portal_raw_pages` keeps filling while
`portal_raw_payloads` silently stops, and the hourly claim lane's page half quietly mines nothing.
`select source, count(*) filter (where contract_version is null) from portal_raw_payloads
where page_kind = 'detail' group by 1;` is the backlog the lane's hash gate is working through.

**One area rule per column (W19/W21).** `scraper.area.parse_area_text` is the ONLY area regex; ONE `areas_from_params` per portal (bazos `areas_from_text`; mmreality takes the estate OBJECT) that only `parse_detail` calls — never a second copy of a key order. The KEYS are `attribute_contract`'s: the HTML portals unpack `source_values(SOURCE, "area_m2", params)` in the slot order (usable, floor, total, plot) the cell declares.
mmreality's parcel is `parcelArea` (`landArea`/`plotArea` never filled; `totalArea` is DERIVED per category — never read it). `usable_area` = the "užitná plocha" label ONLY. Plot area for a READER is the `plot_area_m2` MEASURE (mig 534) — a COLUMN on `browse_list` (mig 535), never `estate_area`.
Heal via `reparse.yml` (`--source <portal> --fields area_m2,estate_area,usable_area,garden_area --allow-snapshot-deferral`), which replays `parse_detail` over the stored detail page: dispatch-only, dry-run default, one source per run, no R2. It NEVER blanks a stored value. A heal the seam cannot make — a value to BLANK, a column the parser never derived, or a substrate the parser cannot read — is a DATA migration under the seam's rules (no snapshot, no `last_seen_at`, `dirty_properties` in the same statement; a `backup_NNN` schema first since 642): 554 (sreality price_unit leftovers on an unparseable substrate), 642 (bazos off-canon condition spellings + dispositions outside the grammar; the stale values survived because a `text` cell is preserve-if-null, `db._preserved_columns`).

## How to manually trigger the scrapers

The sreality pipeline is **split by cadence (Phase 2)**: `index_walk.yml` ("Scraping: Sreality
index walk", cron `*/15`) feeds `detail_drain.yml` ("Scraping: Sreality detail drain", cron
`*/15`). The combined `scrape.yml` fallback (`scraper.main`'s `_run_full`) was deleted in C2-2
(2026-10); an ad-hoc combined sreality run is `python -m scraper.sreality_main` with neither
phase flag (walk, then drain), locally. The bazos crawl is **cadence-split**
like sreality (bazos walks 22 nationwide scopes (migration 488), ~1500 index pages — a combined run starves the
drain): `bazos_index_walk.yml` ("Scraping: Bazos index walk", cron `0 */6`, full walk +
nominate unseen + enqueue) feeds `bazos_detail_drain.yml` ("Scraping: Bazos detail drain", cron
`45 * * * *`, bounded `--max-seconds`). Bazos's ad text needs a post-publication pass the other
portals' structured pages don't: field-capture W7 runs it as the worker's `text_extract` lane
(`toolkit/description_extraction.py`), never in the scrape — the LLM is behind publication, never
in front of it.
The bezrealitky scrape is
`scrape_bezrealitky.yml` ("Scraping: Bezrealitky scraper (pilot)", every 6h + dispatch; runs
both index walk + detail drain in one job via `bezrealitky_main`). The maxima scrape is
`scrape_maxima.yml` ("Scraping: Maxima Reality scraper (pilot)", every 6h + dispatch; the
~220-listing catalogue fits both phases in one job via `maxima_main`). The mmreality scrape is
`scrape_mmreality.yml` ("Scraping: M&M Reality scraper (pilot)", cron `50 */6` + dispatch —
every request via the residential `SCRAPER_PROXY_URL` (CF 403s datacenter IPs; `USE_PROXY` +
`PROXY_REQUIRED=True` so the worker skips it when unset — idnes is proxied too but sets
`PROXY_REQUIRED=False`, being throttled rather than blocked; ceskereality (own nginx, not CF) is the one
portal with `CLIENT_HINTS=True`: since 09-29 it 403s a Chrome UA without sec-ch-ua hints, no proxy fixes);
runs both phases in one job via `mmreality_main`, bounded by `--max-pages`/`--max-detail`). The remax
scrape is `scrape_remax.yml` ("Scraping: RE/MAX scraper (pilot)", every 6h + dispatch; runs both
phases in one job via `remax_main`, bounded by `--max-detail` + a `--max-seconds` budget so the
~7,900-listing backlog drains over several ticks). The idnes scrape is
**cadence-split** like sreality (iDNES is large — 4,235 index pages, ~110k listings):
`idnes_index_walk.yml` ("Scraping: iDNES Reality index walk", `idnes_main --index-only`, cron
`15 */6`) feeds `idnes_detail_drain.yml` ("Scraping: iDNES Reality detail drain", `--drain-only`,
hourly `30 * * * *`, bounded by `--max-seconds`; `SCRAPE_CHAIN_TOKEN` re-dispatches it while the
queue has work). idnes's `supports_complete_walk` was parked (migration 453) — posture only since
2026-09-07, it gates no delisting; the walk is sliced into the 14 kraje + abroad, each slice's outcome
in `portal_index_slices` (454), read back to walk the stalest slice first (the gate that re-earned the flag retired in wave 4).
**Parked flags, the slice ledger and the retired gate: `references/coverage-and-delisting.md`.**
There is no combined fallback workflow for a split portal anymore (sreality's `scrape.yml`
went in C2-2); an ad-hoc combined run of any portal is `python -m scraper.<portal>_main`
with neither `--index-only` nor `--drain-only`, run locally. The properties track adds
`property_maintenance.yml` (**dirty-set incremental, cron `*/5`** — attaches new stragglers as
singletons + recomputes only changed properties; rule #20) and
`recompute_property_stats.yml` (the **daily full-sweep reconcile** at 04:15 — recomputes every
property + clears the dirty queue under a `--max-seconds` budget; a budget stop clears only the range it
swept and saves `app_settings.property_sweep_cursor`, where the next run resumes the cycle (if <36h old);
a resumed run that stops again exits RED; only a whole cycle stamps the `property_sweep` artifact, judged by `jobs`). The visual-signal producers run alongside:
`compute_image_phash.yml` (hourly pHash BACKSTOP only — the image drain hashes the bytes in hand and writes `phash` in the
`storage_path` UPDATE, so this job takes just `phash IS NULL` rows: `IMAGES done phash_missed` + re-master re-arms), `clip_tag.yml`
(`scripts/clip_tag_backfill.py` — zero-shot CLIP room/plot tags into `image_clip_tags` + a 512-d vector into `image_clip_embeddings`),
`clip_retag.yml` (re-runs the zero-shot over each image's STORED embedding when the taxonomy changes, per
`app_settings.clip_taxonomy_retag_after`; no R2 download, no re-inference) and `backfill_render_score.yml` (render-vs-photo axis). Newest and
**dispatch-only**: `dinov3_embed_backfill.yml` (GPU, inert until the encoder config is complete),
`tagging_bakeoff.yml` (GPU, the encoder EXPERIMENT, `dedup_sim` only), `tag_model.yml` (CPU, ONE
versioned tag model, mig 490 — the tag is the argmax head) and `new_dedup_candidates.yml` (CPU, Level 0
path C = same town + disposition/area; `estimate`/`verify` read only). **Read the matching `references/*-lane.md` before touching.**

**There is NO scheduled dedup job any more** — the whole automatic decision layer (the engine, its queues,
its batch warmer, its geo/byt-geo runs, the model-compare and vision A/B harnesses, the publication gate) was
removed wholesale in the 2026-08 NEW DEDUP cutoff (architectural rule #15; `docs/design/new-dedup/CUTOFF.md`)
and its workflows/scripts must never be resurrected; nothing auto-merges (merges are operator-ordered through
`POST /properties/merge`; AUTODEDUP's `mode=apply` is dark until `app_settings.autodedup_apply_scope` names an area). The
tagging/pHash/embedding lanes above ARE kept running because the rebuilt engine (`docs/design/new-dedup/PROGRAM.md`)
consumes them, so treat a stalled `clip_tag.yml` or `compute_image_phash.yml` as a real problem.

**CLIP tagging persists an embedding for every TAGGED image** (PR #748, ~19% coverage gap; PR #751 backfilled the tagged-but-vectorless backlog).
**Any job that REPLACES an image's bytes under its existing `storage_path`** (the sreality re-master lane — `images.rendition` / `stored_width` /
`stored_height`, migration 496) **must go through `db.invalidate_derived_signals`**: the one chokepoint that re-arms these producers by nulling their
OWN predicates (`phash`, `clip_tagged_at`). It deletes no label/review/CLIP-cache row; DINOv3 vectors go only on `drop_dinov3=True` (hand-dispatched GPU refill).

**No drain geocodes, and no place columns.** W4-c dropped every location column from `listings`
and `properties`, so a parser's coordinate/locality/street reading is evidence for the resolver,
not a write; a listing's place is `listing_location` (join on `listing_id`).
`scraper/geocoding.py` survives for `/maps/*` and the curated-city seed only.

One monitoring workflow watches the rest: the canary, `verify_pipeline.yml` ("Monitoring: health canary (hourly)"; GitHub
delivers ~5 runs a day). Step 1, the poller `scripts/record_workflow_failures.py`, records PRODUCTION runs only (event
`schedule`, or `push` on `main`; a branch dispatch writes nothing): each red into `workflow_failures` with its reason line,
`event` and `head_branch`, and per workflow `workflow_run_health` (`last_success_at`, `state`, `on_main`, `recent_starts` of scheduled runs only),
which the worker's `jobs` check judges as one `gh/<stem>` cell each, since GitHub only emails about failed *scheduled* runs
(a never-started supersession cancel is not a failure; cursor + timeout-kill captured);
step 2 measures the `github-actions` checks DUE by registry cadence; step 3, `scripts/health_canary.py`, is the step
meant to red the run and so email (a failing check never does; a crash of the poller or the harness, or the harness step
overrunning its 6 minutes, reds it too; see Pipeline verification below).
The worker's `llm` check alarms PER PROVIDER (each its own account: a credit error newer than that provider's own newest success in 72 h is its `<provider>/credit` Problem, so one provider's success never clears another's; the cell quotes the error, where 2026-10-02..10 the old alert named every paid job and no provider), INDEPENDENT of pending work (a quiet-queue blind spot
once kept a credit-exhausted account green ~8h); `LLMClient` records the failure row on every
provider exception, so the check needs no key of its own. Its `fleet/spend` cell judges 24 h spend (Watch > $130,
Problem > $150, in `THRESHOLDS`) and lands in the same `pipeline_check_results` table the harness below writes to. Run any directly:
- CLI: `gh workflow run index_walk.yml --ref <branch>` (`-f` for flags); watch with
  `gh run list --workflow=index_walk.yml` then `gh run watch`.
- Browser: repo → **Actions** → the workflow → **Run workflow** → branch + flags. (All sreality
  scraping workflows are prefixed `Scraping:`.)

**The workflow catalogue is Settings › GitHub Actions** (Admin → General Settings): every
`.github/workflows/*.yml` with its triggers and cron, dispatch inputs, secrets, run settings and links,
generated by `scripts/generate_workflow_docs.py` from the YAML and the first paragraph of each file's
leading `#` comment into `frontend/public/workflow-docs.json` (a FETCHED asset, not a bundled module —
see `docs/architecture.md`). Regenerate it in the same commit as any workflow edit; CI's `--check` fails
on drift. Health shows no declared schedule: the `jobs` check judges each workflow's production runs as its `gh/<stem>` cell.

**The split (architectural rule #19).** The cheap "which ads still exist" check is decoupled
from the slow "download each ad" write:
- **`index_walk.yml` (fast, frequent).** Walks the **entire** index of every category pair (no
  `--limit`), `reconcile_sightings` bumps `last_seen_at` on every still-listed id we hold, unseen ids are nominated
  for a page check (rule #3, 2026-09-07), new + price-changed ids are queued. The walk carries a
  **wall-clock deadline checked per PAGE** (`--max-seconds` → `run_index_walk` →
  `walk_category` → `portal.deadline_reached`); a deadline is a stop of OURS, so such a
  walk reports `reached_end=False`, nominates nothing, and keeps everything it collected.
  Never hand-roll it, and never add a portal without wiring the flag through —
  `tests/scraper/test_walk_deadline_wiring.py` fails the build if you do. Ids are classified by the
  one shared `portal.classify_index_sighting` (**an index card with no price reads `unchanged`,
  never `changed`**) — and are **enqueued** into
  `listing_detail_queue` in one of two service classes — ACQUISITION (never fetched) or REFRESH
  (failure-retry > price-changed > the location refetch lane). The drain reserves half of every
  claim for acquisition, so an unbounded refresh backlog can no longer starve new listings; unused
  reserve backfills to refresh. Do NOT reintroduce a single ordering across both. No detail fetch:
  the drain's page check decides each nominated row. Records `run_type='index'`, `index_pages>0` (what Health
  liveness keys off). Uses the **transaction pooler** (`connect()`) — bulk set-based statements,
  no per-listing loop.
- **`detail_drain.yml` (slow, async, bounded).** Claims a bounded slice of the queue
  (`--max-detail`, default the per-portal `max_detail_per_run`), fetches details on a rate-limited pool, and writes
  each ~100-item flush through **`listing_write.write_listings`** on all nine portals (set-based, ONE transaction:
  upsert, images/videos, failure clear, snapshot-on-change, dirty marks). sreality uses the **session pooler**
  (`connect_session()`, prepared statements), the crawlers `connect()`. New listings land `property_id` NULL and become
  **singletons** via `recompute_property_stats`'s bounded straggler-attach (the write carries no matching at all;
  grouping is out-of-band and operator-ordered, rule #15). A gone fetch records its verdict + dequeues it (flips only
  under rule #3's hysteresis); a transient error bumps the queue row's `attempts` (given up after 5) and stays queued. Records `run_type='detail'`,
  `index_pages=0`. The queue persists across runs, so a bounded run never loses work; a SIGKILLed
  claim is recovered by the next run's `reclaim_stale_claims`.

**Delisting is presence-verified (rule #3, 2026-09-07); the gate is STRUCTURAL (2026-09-08).** A
walk that REACHED THE PORTAL'S END (`portal.walk_reached_end`: every unit walked, each page loop out
on a portal terminator, no stop of ours) nominates every active row it did not see (`VERIFY cm=…
candidates=… queued=… deferred=…`); the drain fetches each page; a positive gone signal (404/410, redirect off the listing,
"no longer active" text → `ListingGoneError`) is recorded (`detail_queue_completions` `gone`) and flips it (`flipped`) only when
an earlier verdict ≥ `GONE_DWELL` (12 h) stands unrefuted (2026-10-09, portals blink; RUN `pending=`; Health: the `delisting` check) — effectively 24 h, as a walk re-nominates no ad checked within `RECHECK_THROTTLE` (24 h); a live page refreshes it. The COUNT never vetoes — a short walk that reached the end
nominates and logs `COVERAGE`. No absence sweep, no staleness rail; `delist_flip_cap` throttles per
walk ONLY above its 2,000-active-row floor (`VERIFY DEFERRED`, in `delist_flip_refusals`); the page must be the row's OWN (a `detail_ref`
naming another id is dropped). Stop-reason vocabulary: `references/coverage-and-delisting.md`.

**Condition scoring is PARKED — an intentional pause, not a bug** (crons off since PR #730,
2026-07-09; last level written 2026-06-18; operator ruling 2026-10-20: "keep the scoring engine
for now, but make sure it does not impact anything"). Don't re-enable, schedule or backfill
without explicit direction. Code, tables, the `listings.*_condition_level` columns and their data
stay; NO surface reads the levels (no filter, agent fact or estimate — rule #14; the four level
filters are `filter_compiler.RETIRED_FILTERS`). All six condition workflows are dispatch-only:
`condition_score_batches.yml` (Anthropic Message Batches API, 50% cost; `submit` / `ingest` modes,
both crons commented out) and the synchronous `condition_scores.yml` fallback — never run both at
once, they select the same pending listings and the sync scorer doesn't skip in-flight batch rows.
The scoring model is `app_settings.llm_condition_model` (Haiku). The scrape has no scoring phase
(its runner left `scraper.main` in C2-2). Scoring is **kraj-scoped and reuse-first** (migration
174): the selector targets only listings whose resolved kraj (`listing_location.kraj_kod`) is in
`app_settings.condition_scoring_enabled_region_ids` (the Settings page "Hodnocení stavu — kraje"
toggles; empty = none; a toggle alone runs nothing), and `propagate_condition_levels` copies a
property's genuine score to its cross-portal siblings (`listings.condition_levels_propagated_from`
records provenance) before every submit/backfill. Resume blockers: `roadmap/scraper-track.md`.

**Images** stay decoupled across four workflows (the scrape split has no image phase; the drain only
records image-URL rows — bytes land in R2 via these jobs). sreality comes through their `SQUARE_1800_JPG` template
(`res,1800,1800,1|shr,,20|jpg,80`: whole frame, ≤1800px), an exact-template allowlist, so a stored legacy chain is NORMALISED
onto it (only `rot` survives) and every row is stamped `rendition` + `stored_width`/`stored_height` (mig 496): **phash/CLIP
compare only WITHIN a rendition**. Timeout guard = `--image-max-seconds`, NOT the count cap (~11k/hr basis predates it).
- `images.yml` (2-hourly) — THE deep backlog drain across ALL portals, **sharded into 4 parallel jobs**
  (`--image-shard k/4` = the `image_id mod 4` slice), each with its own cap, breaker and runner IP.
- `images_fresh.yml` (`*/15` + self-chaining via `SCRAPE_CHAIN_TOKEN` while work remains) — newest
  ACTIVE listings' photos first, so a fresh card renders an image in minutes, not 2 hours.
- `refresh_stale_images.yml` (every 6h) — re-enqueues active listings whose un-downloaded image URLs
  rotated/went stale into `listing_detail_queue` (low priority) so the detail drain repoints them.
- `sreality_image_remaster.yml` (`15 */2`, 6 shards; scheduled runs gated on the `SREALITY_REMASTER_CRON` repo
  variable — the kill-switch, dispatch always runs) — re-downloads `rendition IS NULL` rows at the master template,
  OVERWRITING each R2 object under its stored key, then deleting its `thumb/w1080/…webp` phone copy (best-effort:
  a failure logs `REMASTER copy_delete_failed`); dead URLs re-resolve from live detail. Final: `REMASTER done …`.

**Cadence:** `*/15` for each half, deliberately — frequent index walks surface delistings fast, while
the bounded drain keeps a steady, polite fetch volume. GitHub throttles scheduled workflows, so effective
cadence is slower, and Health judges the gap a portal keeps, not a declared one: the `scraping` check's `<portal>/walks/<unit>`
cells age a unit's newest end-reached walk against G, the median start gap of the portal's finished walks over 14 d (under 10
gaps, the slowest portal's G): Watch past 3 G, Problem past 6 G (crawlers ~43 h, sreality 26.5 h) or with no end-reached walk
in 14 d; `<portal>/walks` is a Problem when no walk finished in 30 d (`THRESHOLDS` in `scripts/verify_pipeline.py`). No surface
has read `scraper_health_checks()` since Health wave 8, so `portals.scrape_cadence_minutes` (migration 114), which it scales
by, judges nothing; wave 9 drops both. Concurrency: each workflow has its own group with `cancel-in-progress: false` — a long
run is never killed mid-batch, the next tick queues behind it. Per-category nominations are queued immediately after each
category's walk, so even a timed-out index walk leaves a consistent partial result.

The detail-drain writes `scrape_runs` rows too (`run_type='detail'`), but only the **index walk** sets `index_pages>0`, and the portal checks read `run_type='index'` rows alone; the worker writes none (`run_id=None`), so the arms that must see its work read the queue, the completions ledger and listings. The image backfill (`--images-only`) deliberately writes NO `scrape_runs` row — recording it once polluted the old liveness and reconciliation arms with `index_pages=0` noise.
**The lifecycle around both phases lives in ONE place, `portal_runner.run_phase`** (rule #21; never re-add a per-portal copy): `ended_at` means the phase COMPLETED, so a phase that raises bumps `errors` and deliberately leaves `ended_at` NULL with `by_category` still `[]`: a crashed walk is never a finished walk, nor an end-reached one, even once the API's boot sweep stamps its `ended_at`, so its units' `<portal>/walks/<unit>` cells keep aging against G (the delisting check's `<portal>/stuck` cell is another thing: active ads whose gone verdict waits past its limit). The crash path logs and writes no incident (the signature engine retired in Health wave 3, migration 621): a red workflow run is the `jobs` check's `gh/<stem>` cell with its reason line, a portal raising inside the worker its `lane/<lane>/<portal>` cell. **The crash contract and the jobs check: `references/pipeline-verification.md`.**

## The real-time worker (`scraper/realtime_worker.py`)

A dark-by-default, always-on Railway service (a 2nd process from the SAME image, gated by
`REALTIME_WORKER_ENABLED`) running every lane registered in `_amain`. Its probe/drain/images/count-probe
core replaces cron quantization for the latency-critical path (the GH Actions crons above stay the
throughput/completeness backbone); the other lanes host background jobs, `health` measures the worker-hosted checks
every 60 s and `heartbeat` beats `worker_heartbeats`. Design + shipped waves: `docs/design/realtime-scrapers.md`. **Liveness (2026-09-30, after a silent 7 h 40 min freeze):** every lane, the heartbeat included, runs on one pinned default executor (32 threads), so the heartbeat is its canary; a daemon **watchdog** dumps every thread's stack and `os._exit(1)`s once no heartbeat pass has FINISHED for `LIVENESS_BOUND_SECONDS` (300 s; a beat that fails still finishes, so a refused write cannot crash-loop the worker), and Railway restarts it — `WATCHDOG no heartbeat pass finished` in the log is that exit, `WATCHDOG pass lock of <lane> held over 3600s` the same exit for a lane wedged behind its lock (`PASS_LOCK_BOUND_SECONDS`, 2 x the pass timeout). The dump writes to a non-blocking stderr (a full pipe cuts it short, never holds the exit) and a `faulthandler` exit timer armed first `_exit(1)`s it if it is still alive 15 s later (a C thread, no GIL; not a signal, which the kernel drops for PID 1). All eleven lanes with a pass lock (`_PassLock`: probe, drain, broker_maintenance, location_resolve, location_intake_fast, sold_comps, text_extract, health, autodedup, notifications, push) run each pass in ONE thread, so each leaks at most one; a skipped pass is `last.previous_pass_running`, and while an abandoned pass still holds the lock the lane's `in_flight_s` keeps counting from that pass's start, so the `jobs` check's `lane/<lane>` cell sees the wedge (Watch past 1,200 s, Problem past 1,800 s). The worker's probe, drain and sold_comps lanes bound each wait in the shared rate ledger (`scraper/rate_ledger.py`) at `LEDGER_MAX_WAIT_SECONDS` (120 s), and the drain's limiter also refuses every acquire past its `DRAIN_MAX_SECONDS` deadline: a farther window is refused without moving the frontier and that portal is given up for the pass (`budget_refused` in the heartbeat, drain claims handed back untouched, the sold_comps tick stopped with no ledger row for the refused cell; `RATE budget refused` in the log); a drain whose time budget runs out mid-chunk hands back the same way but counts `deadline_stopped` (`DRAIN time budget reached mid-chunk`), not a block. The Actions walks and drains and the `reas_main` CLI pass no bound and wait as the ledger says. The retry after a 403 leases one slot (that debt capped at one lease batch), and a drain's lease is sized to its claims.
Per-lane notes:
- **Per-source kill switches** `realtime_probe_disabled_sources` / `realtime_drain_disabled_sources` (JSON lists, read every pass, no redeploy; PR #694) — a portal leaves that lane, its GH Actions cadence untouched.
- **sreality count-probe lane** (migration 270, PR #696) — a per-`(category_main, category_type)` count
  check that sees a market-wide count swing faster than a full index walk; opted in, it dispatches `index_walk.yml` early.
- **Property-maintenance lane**, every 30 s (PR #716) — `run_incremental_pass` against `dirty_properties`
  (rule #20), far more often than the 5-min GH cron; serialized with it + the daily sweep by the lease-row
  CAS (PR #717): **never a session advisory lock on a pooled connection** — the first cut stranded. Each drain
  slice = ONE `properties_changed` txn (recompute, Browse patch, `dirty_broker_listings` mirror), as merge/detach run inline.
- **Broker-maintenance lane** (Broker Unify W3) — same cadence; `scripts.resolve_brokers.
  run_incremental_pass` (`broker_resolution.yml` runs the SAME driver as backstop): drain until empty,
  recompute affected brokers (one merged statement), republish `broker_region_type_stats` via the
  mig-578 chokepoint when its registry stamp is >~1 h old; `broker_resolution_lock` serializes.
- **Estimation job lane** (migration 349, Wave 1 W1-3 / Phase 1 Amendment A10) — moves agent +
  deterministic rent-estimate EXECUTION off the FastAPI request threadpool (a 240 s agent run used
  to pin a Starlette token; a deploy SIGTERM killed paid runs mid-flight). Claims one `pending`
  `estimation_runs` row per pass via `FOR UPDATE SKIP LOCKED`, runs the SAME `execute_pending_run`
  path from the `job_payload` snapshot (the run row stays the job — no new table). Each pass first
  sweeps stuck runs (keyed off `coalesce(claimed_at, created_at)`) so a crash-orphaned run frees
  its slot. Ships **DARK**: idle until `estimation_job_lane_enabled` is set — the SAME flag makes
  `POST /estimations` route rows to the lane instead of an in-process BackgroundTask, so the
  cutover (and rollback) is one setting, no deploy.
- **Location-resolve lane** (Decision 8b) — THE resolver drain from here, not only from the
  `location_resolve.yml` cron (round-trip-bound: 0.7 listings/s from a US runner; W2-a cut the
  per-listing registry reads from ~11 to ~4 and the writes from 7 to 1). Ships DARK; enable via
  `realtime_location_resolve_enabled`. Live tuning:
  `realtime_location_resolve_{interval_seconds,max_seconds,batch_size}` (15/240/250), interval `0`
  idles. Since W2-a5 a pass drains **`LOCATION_RESOLVE_WORKERS`** slices concurrently (env on the
  Railway service, default 4, clamped 1–8; one thread + one session connection each, disjoint by
  SKIP LOCKED) — the heartbeat's `workers`/`failed_batches` say what actually ran; the GH lane
  stays single-connection. Per-slice failure/backoff, the 90 s prefetch ceiling, exclusion,
  budgets and lease/lock: `docs/design/realtime-scrapers.md`. (The `epoch_job` it had to be idled
  before is gone with the pin-collision engine, W2-a.)
- **Location-intake-fast lane** (W7-a) — THE claim lane's change-driven listing scan
  (`claims_intake.run`, `mode="incremental"`; JSON half first, then bodies + stored readings on the
  remainder — body cap `LOCATION_INTAKE_FAST_BODIES_CAP` 300, R2 width 8, ONE 2-wide `ExtractionPool`
  reused across ticks) every ~60 s with a **2-minute** snapshot lag and a 45 s budget, under its OWN
  `claims_intake.FAST_LANE` cursor so it never moves the hourly run's 15-minute one (that run
  re-reads whatever the short lag skipped). Ships **LIVE**; env knobs on the Railway service:
  `LOCATION_INTAKE_FAST_{ENABLED,INTERVAL_S,LAG_S,BUDGET_S}` (`1`/60/120/45), `ENABLED=0` idles.
  Projects the portal contracts from the image once at lane start (warn, never fail); no `R2_*` =
  warn once, JSON only. Heartbeat `details.location_intake_fast.last` = `{listings,
  claims_inserted, enqueued, bodies_mined, bodies_complete, seconds, cursor, bodies_cursor}`.
- **Location-refetch lane** (W8) — once a day (`LOCATION_REFETCH_INTERVAL_S` 86400, first tick 5 min
  after start; `LOCATION_REFETCH_ENABLED=0` idles) queue the audit page's active "no data" rows
  (`location_pin_audit_mv` `state='unresolved'`+`quality='active_no_claims'`, joined to `listings` by
  PK) whose newest stored page/payload is older than `LOCATION_REFETCH_MIN_AGE_S` (6 h) into
  `listing_detail_queue` at VERIFY, ≤`LOCATION_REFETCH_CAP_PER_SOURCE` (500)/portal/tick,
  oldest-fetched first. ceskereality/realitymix geocode AFTER publishing; the drain + intake lane do
  the rest, and a bazos dead ad delists on the same fetch. Heartbeat
  `details.location_refetch.last` = `{candidates, queued, sources:{src:{candidates,queued,backlog}},
  min_age_s, cap, seconds}`; no audit view = skip + one warning.
- **Sold-comps lane** (sold-comps W2, migration 544) — registered sales from reas.cz (an external FACT
  feed, NOT a tenth portal) for ≤5 obec cells a pass: towns where the deal pipeline holds a live card AND
  the obec has an `admin_boundaries` polygon (an unboxable cell writes no ledger row, so it would sit at
  the queue's head for ever), stalest first, minus cells whose newest `sold_transaction_fetches` row is
  `ok` within 35 d or `failed` within 6 h. Ships **DARK** behind ONE integer,
  `realtime_sold_comps_interval_seconds` (seeded 0, editable on /settings) — cadence AND kill switch, no
  `*_enabled` flag, no env var, no workflow. Box = that polygon's envelope +5 km; `fetch_cell` never
  raises, so every attempt with a box ends in the ledger, and an `ok` row whose walk the 25-page cap (or a
  non-advancing `nextPage`) cut short carries `truncated: …` in `error`; 1 req/5 s + ONE retry on the
  shared rate ledger; one in-process pass lock. Heartbeat `details.sold_comps.last` = `{ran, cells,
  records, new, failed, skipped, seconds}`; no store = `ran: false` + one warning.
- **Text-extract lane** (field-capture W7, `description_extraction.run_pass`): facts only an advert's text states.
  CONSTANT 300 s, no flag/setting/env var (an unset flag hides a lane); scope = open-R7-gate `text` cells ∪ portals
  declaring `llm_text`. W4: then `claims_intake.mine_readings` mines that pass's readings (30 s, batch lane
  `location_readings_fast`, log `INTAKE readings fast ids=…`, heartbeat `last.readings_fast`, off:
  `LOCATION_READINGS_FAST_ENABLED=0`; the hourly claim run is the backstop).
  Needs `OPENAI_API_KEY`; a pass the provider stopped (`last.fatal`: credit, quota, key) is a FAILED pass (`failed_passes`, `failing_since`, `last_error`, same interval; `aborted` with no `fatal` = its own $/time ceiling, not a failure); rails = the `text_extraction` gauge (new ads' flow, the backlog's ETA), the `jobs` check's `lane/text_extract` cell and, for a provider stop, the `llm` credit cell. Sizing, cache key, write gate: `llm-pipelines`.
- **Notifications lane** (notifications push W2a/W2b, `docs/design/notifications-push/PROGRAM.md` § 3.3) — the three producers of `api/notifications` on
  their ONE clock (W2b deleted the API's `matcher_loop` and lease): CONSTANT 300 s, no flag/setting/env var (the brake is a deploy). Steps
  `watchdog_new` (`match_once`: a live watchdog's matches first seen in the last 24 h and after its `active_since` (create, switch-on, filter edit),
  images-first gated; no cursor), `price` (`match_changes_once`: one canonical-ad step fragment, 7 days, watchdogs + monitored collections) and
  `monitor` (inactive after 24 h gone, reactivated, new_source), each on its own connection, committing per statement (one watchdog's INSERT, one
  detector; `SET LOCAL statement_timeout` 120 s; the notification-events lock SHARED first, so a merge waits one statement at most and never deadlocks,
  architecture § rule 16). A step that raises or has a failed statement is its own `portal_errors` entry, `lane/notifications/<step>` red alone while
  the others write; a stored spec the matcher refuses is `last.*.subscriptions_invalid`, never red (the write routes 422 it), even when every watchdog
  is. A worker without the lane is the jobs check's missing-lane cell; `NOTIFICATIONS` log line only on a write or a failure.
- **Push lane** (notifications push W4, PROGRAM.md § 3.3–3.4; `api/push_delivery.deliver_once`) — CONSTANT 15 s; the ONE brake is
  `VAPID_PRIVATE_KEY` (unset: `last.unconfigured`, no connection; unreadable: the `lane/push/key` cell). A pass: fan-out (VISIBLE unseen
  events ≤ 24 h × the account's live devices registered before each; a share past one per sharer a day = `skipped/capped`), a claim
  (≤ 10 devices, 2-min lease = `next_attempt_at`, SKIP LOCKED, < 5 attempts), re-check (seen | hidden | stale | device_ended), ONE
  push per device (coalesced: "N nových upozornění", `/notifications?d=`), finalized right after its POST (sent | gone: the device
  ends | 429: the device is held, `hold_until`, the rows keep their attempt | retry 2ⁿ min | failed). No lock but the lease; steps `fan_out`/`claim`/`send` are
  `portal_errors` cells by exception CLASS only — an endpoint is a secret, never in the heartbeat or the log (`PUSH` line on a send).
- **Autodedup re-cut** (E944) — pHash coverage < 0.85 marks `rt_recut:<gen>` due; the NEXT tick re-cuts instead of passing (same lease, 1,050 s, reads stop 120 s early); never in a build or while the scope (max of the last cut's count and the store's `rt_fp`) holds > `RECUT_MAX_LISTINGS` 50k ads (row says `bootstrap`/`unfit`: Prague keeps its seed's cut); ≤ 1 attempt / 6 h, stamped before the cut, so a death mid-cut waits and does not halve the claim cap (2 deaths under one cut = `died` until a seed); failed = `errors: 1` + `recut_failed`; shutdown = taken back while its stamp holds.
  Heartbeat `last.recut` + the lane's own `recut` (lost on restart; durable: `rt_recut:<gen>.last`), `calibration_built_age_days`. The parsed calibration (~16 MB at 39.5k) outlives its pass, read once a digest — E949's one deliberate exception.
- **Autodedup lane** (AUTODEDUP §7.3/E914, mig 557) — THE engine's one real-time pass (`run_incremental`); one integer
  `realtime_autodedup_interval_seconds` (seeded 0 = stopped; 60 running) is cadence AND brake, claim = engine rate × half a 1050 s deadline (a build's three quarters, E951), rate
  halved BEFORE every pass, restored the moment it commits (or the pair budget refuses it, E970), then overwritten by its measurement (E941), at least doubled instead when the clock cut the claim, the feeds filled it (the re-claims ride on top and fill none, E971) and the pass took under half the deadline (E951; `last.pass_budget_s`, `rate_doubled` when 2× beat the blend); one pair budget, build and live: 150k pairs to DECIDE once the reject ledger has answered (E943: `autodedup.rt_reject_ledger`, mig 596, a below-floor reject skipped while both decision digests, the calibration, the feature version and the engine digest — decision sources, model, settings — match; a deploy touching them or a re-cut costs one full-price pass; a CLIP re-embed that leaves `clip_tagged_at` alone must empty it), each refusal re-claiming a quarter of the claim made (E952; `last.attempts`/`wanted_pairs`/`wanted_by_attempt`/`pairs_to_decide`/`pairs_to_decide_by_attempt`/`aborted`/`ledger_skipped`/`ledger_written`); a pass reads facts only for its claim and the sides of the pairs it decides (E960, mig 604 `rt_fp.dd`; `last.dirty`/`reads` by reason; `dirty` is a neighbourhood, never a backlog: the build's backlog, rate and decision lag are the Health `dedup` check's), so an advert it does not claim is seen as its row stored it — a reparse wave, a location correction, sightings or a re-master reach `rt` only when a feed re-claims the advert, the freshness rule re-claims it (a merge applies only when its moving members were refreshed within 12 h under this engine, else `last.reconcile_stale`; waiting groups are re-claimed whole — Praha, the scope's `town:` towns, the rest (E974), then flats for sale, flats for rent, houses + commercial for sale, the rest (E972, E974), oldest first within each, active and inactive groups together (E974: the operator's ruling of 2026-10-21 retired E973's active-first level); the hand-on list's 5,000-advert cap cuts the same order — 200 adverts a pass (a group that does not fit is skipped for the next, the first taken whole, E973) on top of the feeds' limit (E971: the worker's memory holds a pass to ~300 adverts), never more than `rt_claim_cap`, on a pass's first attempt only, each group tried at most once in 6 h (stamped as the pass ends on its own terms, never by a shutdown, a lost lease or a kill, E973), paused once and halved after a pass that died with them (`rt_redecide_budget`; watch `stale_claimed` beside `deadline_exceeded`), never while the apply scope is closed or the reject ledger is inert, so a W8 undo or a go-live backlog drains at ~80–110 groups a pass; arrival merges never wait; `last.reconcile_handed_on`/`_dropped`/`stale_claimed`; escape: brake 0 → delete `autodedup.settings` `rt_redecide:<gen>` → brake 60) or the scope is re-seeded; a killed pass keeps the half; `SystemExit`/trip = a failed pass. Decides + groups `rt`, then RECONCILES production: re-clustered groups merge via `merge_property_set` inside `app_settings.autodedup_apply_scope`, never a split (`mode=unapply` undoes). `rt_seed`/`apply`/`unapply` share `autodedup.rt_lease`; every lane transaction (pass, each reconcile group, re-cut) ends with `rt_lease.hold` (`lease_lost`). SIGTERM marks the lane stopping: the pass stops at its next checkpoint (`PassStopped`), rolls back and releases; the handler also releases the holder's lease on a fresh connection (2 attempts; `drainingSeconds` 30); a pass whose connections a database restart took retries the release on new ones, every 20 s for up to 180 s, before falling back to the TTL. A restart in place (same hostname + pid) releases, right before the take, the live lease its dead predecessor left (holder `<host>:<pid>:<second>` older than this boot; E948; the engine logs it) and halves `rt_claim_cap:<gen>` in the same commit: the feeds' limit = min(500, cap, rate × budget) (each feed a fifth of it, the revived one too since E970) and the re-claims ≤ cap (E971 review), floor 25, only a seed, or a container limit (read each pass) grown ≥ 25 % over the row's `limit_mb`, raises it (E948b: `limit_mb` = the biggest limit a death since the last seed/reset died under; a row without one takes the pass's limit, but one E948 halved (no key) is measured from the 8 GB `E948_LIMIT_MB` 7,629.4; a boot seeing the growth AND a dead predecessor releases it without halving); at the floor the lease waits out its TTL (`predecessor_kept`). `last.peak_rss_mb`/`rss_mb`/`memory_limit_mb`/`claim_cap`/`predecessor_released`; `MAX_SCHEMA_MB` 1,500 (20 KiB an ad with the ledger); no GH schedule. The decided pairs are scored 5,000 at a time and each chunk's rows are written before the next, so the rows follow the chunk; a side's CLIP vectors are held from its first decided pair to its last, which in a dense pass is most sides (E950; `last.chunks`/`chunk_rss_mb`/`chunk_vectored`). No body or token memo outlives its pass (E949): each pass counts and empties them (`last.memo_entries`), and freed heap goes back to the kernel (`gc.collect` + glibc `malloc_trim(0)`) as every pass begins and ends, before its lock is released (`last.rss_at_start_mb` / `rss_after_trim_mb`), a raised pass's when the next one begins; the worker starts as `env MALLOC_ARENA_MAX=2 python -m scraper.realtime_worker` (two arenas; Railway's exec form needs `env`).

- **A pasted link enters the same queue.** `POST /listings/ingest {url}` (admin) resolves the URL with the one
  identity canon (`scraper/portal_identity.resolve_url`) and, for an ad we do not hold (or hold inactive), calls
  `db.enqueue_detail` at NEW (a `given_up` row is re-armed); `GET /listings/ingest/{source}/{native_id}` reports
  `refused | held | queued | gone | failed`. The API never fetches a page: the worker's drain does, so the ad is written,
  born (the maintenance lane, 30 s) and located exactly like a walked one (`docs/design/new-property-from-link.md`).

## Pipeline verification (migration 274)

**Health check harness** (`scripts/verify_pipeline.py`; plan: `docs/design/health-dashboard.md`) — ONE registry, `CHECKS`:
each `Check` declares key, class (`alarm`/`gauge`), host (`realtime-worker` for `jobs`, `capacity` and `llm` since wave 3,
`scraping`, `publishing`, `delisting` and `coverage` since wave 4, `dedup` since Health dashboard sprint wave 5, `parsing`, `data_quality`, `location_town_coverage` and `text_extraction` since wave 6, `github-actions` only for `migration_drift` and the URL probe), `cadence_s`, budget, label, unit and owner page; `project_registry` mirrors the host's keys into `health_checks`
(migration 619). FOUR statuses, `ok`/`warn`/`fail`/`unknown` = OK/Watch/Problem/Not measured: a timeout, an error or a
check's own could-not-measure path is `unknown`, reason in `details` (`timed_out`/`errored`/`skipped`), never `ok`. `run_due` (the canary for GitHub's keys; the
worker's `health` lane, every 60 s under a 500 s pass budget that defers a gauge whose budget no longer fits, for its own) measures a host's DUE checks (newest row since `declared_at` older than `cadence_s` less
min(cadence / 10, 15 min)), alarms before gauges, each under a real wall clock: every statement's `SET LOCAL statement_timeout` is the
check's time left, so an overrun is `unknown` (`timed_out`, `budget_seconds`, `elapsed_seconds`), and a check the run
never reached writes no row and stays due; the weekly outbound-URL probe is a cadence, not a workflow. A row keeps `details.message`, `elapsed_ms` and its `cells` (one verdict per portal/job/lane/file; every measured result has them since wave 6).
Measuring writes no system alert and reds no run: the ONE ladder, `toolkit/system_alerts.reconcile`, runs in the API every 60 s (its only loop; off under `HEALTH_TELLER_DISABLED`) —
one `ops_incidents` row per failing cell (`cell:<key>:<subject>`), one grouped onset per check, reminders until
acknowledged on Health (Acknowledge on a check's row covers all its open incidents) — alarm
6 h / 24 h / 72 h then weekly, gauge daily — and one recovery once an OK is measured across two cadences less the due slack, never under
30 min; `unknown`, or a result past its staleness budget, is inert.
**Platform checks (wave 3, `scripts/checks/platform.py`, on the worker):** `jobs`, one job cell per production GitHub
workflow (`gh/<stem>`, with its reason line), the poller (`gh/poller`), pg_cron job (`cron/<job>`), worker lane
(`lane/<lane>`, `lane/<lane>/<portal>`) and derived artifact (`artifact/<name>`); `capacity`, pg_cron durations against a
sticky reference, check timeouts per host, the oldest open transaction, connections and the worker's memory; `llm`, credit per
provider, error share and starvation per (provider, `called_for`), 24 h spend. Thresholds: `THRESHOLDS`, no override.
**Push check (notifications push W4, `scripts/checks/notifications.py`, on the worker; key `notifications`, 5 s budget):**
`push/stall` (a delivery due > 10 min, its device not held), `push/recall` (a VISIBLE unseen event < 24 h, an older live device, no ledger
row after 5 min; neither judged while braked), `push/failures` (≥ 3 terminal in 24 h), `push/unconfigured` (Watch: braked while a device waits).
**Portal checks (wave 4, `scripts/checks/{scraping,publishing,delisting}.py`, on the worker; cells `<portal>/<arm>`):**
`scraping` (walks: hours since a unit's newest end-reached walk against the portal's own walk gap; inflow; fetch errors;
missing), `publishing` (queue age, Browse visibility of new ads and of the stock, photos, `fleet/dirty`), `delisting`
(outflow, stuck verdicts, verify backlog, deferrals, precision), the `coverage` gauge (residual, unnominated ads).
**Engine check (Health dashboard sprint wave 5, `scripts/checks/dedup.py`, on the worker; key `dedup`, label "Engine"):** the engine's state
(off/building/merging/idle: the `state` cell, always OK, and a fact on every cell); `stream`, `build`, `merge_pause`, `decision_lag`, `lease`,
`rulings/ads`, `rulings/brokers` (the former `broker_merge_suppression`) and `splits`, from the live engine's stores and `autodedup`/`operator` merges only (rule 15).
**Quality checks (Health dashboard sprint wave 6, `scripts/checks/quality.py`, on the worker):** the `parsing` alarm (fill and vocabulary against the blessed baseline, new ads' per-m² measures, stock
medians, location payload shape via `claims_common.PAYLOAD_SHAPE_PREDICATES`, re-snapshot churn) and the gauges `data_quality` (basis floor, area vs usable, no portal link), `location_town_coverage` (active ads only) and `text_extraction` (flow, backlog ETA), each gauge cell with a 30-day `trend` and `worse_7d`.
**Watchers (migration 620):** pg_cron's `health_liveness()` (job `health-liveness`, every 5 min) writes one `liveness` row
(host `pg_cron`) whose cells judge what must not judge itself: `worker` (heartbeat over 10 min old), `health_lane` (no completed pass, a pass in flight or failing for 10 min), `stale:<key>` (an alarm past its staleness budget) and `unalerted` (a
measured Problem with no incident; the matcher-lease `api` cell went in W2b, mig 655); the ladder tells them, and they replace `worker_liveness` and the 24 h dead-man job.
The canary's last step, `scripts/health_canary.py`, exits 2 when the DB does not answer and 1 on any `CANARY FAIL`: the
newest `liveness` row is 30 min old, a failing liveness cell has no incident after 15 min, or an open, unacknowledged alarm-class
incident's latest rung (`ops_incidents.alerted_at`: onset, 6 h, 24 h, 72 h, weekly) is 15 min to 12 h old (never a gauge's). System alerts
reach no bell (ruling R2b, 2026-10-21), so that red run's e-mail is the one external path. pg_cron watches the worker and every alarm's freshness (the
canary's keys too); the canary watches pg_cron and the ladder. **Rationale, watchers, sizing: `references/pipeline-verification.md`.**

## Reading the logs

The scheduled pipeline logs in two halves. The shared `portal_runner` emits CATEGORY, ENQUEUE, VERIFY, RECONCILE,
RUN, COVERAGE, the closing `INDEX total=…` and every DRAIN line; per-page INDEX / SPLIT / SLICE lines come from each portal's `walk_category` (sreality's per-page INDEX from its client).

**Index walk** (`index_walk.yml` and the per-portal walks):
- `CATEGORY start cm=... ct=...` per category pair
- `INDEX offset=N estates=M total=K` per search page (offset/limit paging; sreality)
- `SPLIT cm=... ct=... result_size=N > T: walking D districts` when a sreality category exceeds
  the deep-pagination window and is walked per-district
- `PLAN priority_retry=N` (sreality only) — repriced re-sighted listings that still hold a
  `listing_fetch_failures` row; they enqueue at FAILURE priority, ahead of CHANGED
- `ENQUEUE source=<portal>[ cm=... ct=...] new=N changed=M unchanged=U enqueued=E` — ONE shape from
  `portal_runner.reconcile_sightings`: per category (per district on sreality), per page in the bespoke
  probes (`offset=` sreality, `page=` ceskereality; mmreality labels `sale= cat=`) — ids handed to the drain
- `VERIFY cm=... ct=... subtype=... candidates=N queued=M deferred=K active=A` — rows nominated for
  a page check (rule #3); `VERIFY skipped ...: the walk did not reach the portal's end (our stop:
  ...)` when it may not, and `COVERAGE cm=... ct=...` WARNS when it nominates while short; `VERIFY DEFERRED`/`OVERRIDE` from `scraper.db`, `VERIFY failed` on an exception, `VERIFY dropped N NULL id(s)` = a parser bug
- `RECONCILE cm=... ct=... sreality=... collected=... active=...` — portal-reported total vs
  collected vs our active DB count (drift feeds the Health page)
- `INDEX total=N pages=M enqueued=K` once at end of the walk
- `RUN done pages=N enqueued=M to_verify=V deferred=D errors=E` (E = failed categories)

**Detail drain** (`detail_drain.yml` and the per-portal drains):
- `DRAIN reclaimed stale claims=N` when a prior SIGKILLed run left claims behind
- `DRAIN starting source=... max_claims=... workers=W batch=B budget=Ss` once
- `DETAIL id=... gone (is_active=false)` / `DETAIL id=... error: ...` per non-ok listing; `DRAIN gone-rate breaker: ...` once when >50% of ≥20 ingest fetches read gone (later gones become failures); `gone flip matched no listing` when a gone key matches no row (INFO for a never-fetched NEW id); `could not mark id=... inactive` = a failed flip, retried (counted in errors)
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
- `IMAGES STOP suspicious ...` when the transient-failure circuit-breaker trips (exits 75; the next cron tick retries)
- `IMAGES done downloaded=... errors=... taken_down=... source_unavailable=... attempted=... phash_missed=...` (`phash_missed` =
  stored but inline-unhashed, 0 in steady state; its cause is one `IMAGE phash_inline_failed` WARNING per kind per run)

A run ending with `errors > 0` is not necessarily a failure (single-listing fetch errors are
tolerated). A run that did not emit a `RUN done` line is a real failure — check the GitHub
Actions log for a stack trace.

