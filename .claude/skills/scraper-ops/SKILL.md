---
name: scraper-ops
description: Use when running, debugging, or extending the scrapers — triggering the per-portal index-walk/detail-drain workflows, adding a new scraper field without breaking data, refreshing per-source HTML fixtures, reading the pipeline logs (INDEX/ENQUEUE/INACTIVE/DRAIN/IMAGES line shapes), the always-on real-time worker (probe/drain/images/count-probe/property-maintenance/estimation/location-resolve/location-intake-fast/sold-comps/text-extract lanes), the visual-signal producer jobs (image pHash, CLIP tagging/retag, DINOv3 corpus embedding on RunPod), or the pipeline verification/alerting harness. Also covers condition-scoring (currently unscheduled) and image-download workflow cadence. Triggers on: index_walk, detail_drain, gh workflow run, mark_inactive, scrape_runs, fixtures, RUN done, a new listings column, onboarding a portal, reading a scrape log, realtime_worker, sold_comps, clip_tag, dinov3_embed_backfill, compute_image_phash, verify_pipeline, llm_burn_rate.
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
3. Add it to `scraper/db.py` `LISTING_COLUMNS` + `_LISTING_COLUMN_PGTYPE` (covers BOTH write paths) and,
   for crawler portals, `scraped_listing._LISTING_FIELDS`. Whether a parser NULL erases is decided per
   (source, column) by the producer from step 2 (`text`/`none` preserve, `structured`/`derived` clear);
   `_PRESERVE_IF_NULL_COLUMNS` is the GLOBAL identity pair (`published_at`, `source_url`) and must not grow.
4. Backfill old rows from NARROW typed columns via a `scripts/backfill_*.py` dispatch job (never a
   `raw_json` pass — ~62 KB/row detoast); NULL is acceptable for a nullable column. The new cell reaches
   `field_fill_matrix` only once the OPERATOR re-blesses (`--bless` needs the prod DB).

## Refreshing per-source HTML fixtures

The LLM-driven parsers (`scraper/source_parsers/`) are tested against saved listing HTML in
`tests/fixtures/source_html/`. Real listings get taken down or change layout, so every few
months the fixtures need a refresh. Don't fetch live in tests — that would burn LLM credit and
break offline runs.

Refresh (CLI, fastest): `gh workflow run fetch-fixtures.yml --ref <branch>` (add `-f` inputs to
override URLs), or Actions → **Fetch + anonymize source HTML fixtures** → **Run workflow**. It
fetches each URL, scrubs via `scripts/fetch_and_anonymize_fixtures.py` and commits the
`*_sample.html` files back to the branch; the skipif tests in
`tests/scraper/test_source_parsers/test_real_fixtures.py` light up once they exist.

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
`tests/fixtures/location_w2a_refetch/`), the blanket sweep above is the wrong tool — masking
*every* 9-digit run rewrites `data-gps-lat="50.069672777778"`, JSON-escaped photo ids the URL
mask never sees, and Tailwind custom properties. Use the contact-scoped mode instead:
`python scripts/fetch_and_anonymize_fixtures.py --scrub-contacts <files> --name "<agent name>"`.
It seeds phones only from markup that says "phone" (`tel:`, schema.org `telephone`, a rendered
`+420` group, a whole-text-node number, reveal-on-click attributes, **and a JSON `phone`/`mobile`
key** — plain, entity- or backslash-escaped, which is how a portal whose payload is an embedded
JSON prop spells it: mmreality's agent number arrives as `&quot;phone&quot;:&quot;731404040&quot;`
with no `+420`, no grouping and no `tel:` href). It replaces e-mails, **re-encodes Cloudflare's
obfuscated e-mail payloads** (`data-cfemail`, `/cdn-cgi/l/email-protection#…` — an XOR against
their own leading byte, so a committed one publishes the address while matching no plaintext
rule; the placeholder is re-encoded under the page's OWN key, which preserves the per-response
key that is itself measured churn on Cloudflare-fronted portals), and takes each hand-supplied
name in plain, JSON-escaped **and** slugged form (the profile-URL slug is the one that gets
forgotten). Pass `--name` once per name, **longest first** — replacing "Radomír Kočí" before
"Bc. Radomír Kočí, DiS." leaves the longer form half-rewritten.
Same placeholders, so a fixture set stays consistent either way; it is idempotent, so re-running
it on a committed fixture proves the fixture is clean. Two tests in
`tests/location_data/test_payload_norm_measured.py` fail if a committed fixture carries contact
details in plaintext **or** in Cloudflare's hex.

**The nine SCRAPER portal parsers are a separate fixture set** in `tests/fixtures/portal_html/`
(`tests/scraper/test_portal_media_fixtures.py`), distinct from the LLM `source_parsers` set
above. They exist because a hand-authored fixture can only assert back the strings the test
itself planted, so it is structurally blind to upstream drift — that blindness is what let a
19-day realitymix gallery blackout and a ~63% idnes photo loss both ship with CI green. Never
hand-edit a URL in one; regenerate the page instead, or the fixture degrades into the tautology
it replaced.

## Recovering a field a parser silently stopped extracting

**Every typed `listings` column goes through the ONE re-parse seam**, `scripts/reparse.py` / `reparse.yml`: it replays the portal's OWN `parse_detail` / `parse_advert` / `parse_listing` over a substrate declared once per portal — `portal_raw_pages.html` on the seven HTML portals, `listings.raw_json` on sreality + bezrealitky, which stage no body — so a heal cannot disagree with the live scraper. No re-fetch, and it repairs **inactive** listings too, which a re-fetch structurally cannot. `--fields` is REQUIRED (it writes exactly those and reads the rest only to leave them alone); dry-run is the default. It never writes a `listing_snapshots` row, never blanks a value the re-derive could not produce, never touches `last_seen_at`, enqueues `dirty_properties` in the same statement, and writes a row only while it still holds what the pass read.

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
mmreality's parcel is `parcelArea` (`landArea`/`plotArea` never filled; `totalArea` is DERIVED per category — never read it). `usable_area` = the "užitná plocha" label ONLY. Plot area for a READER is the `plot_area_m2` MEASURE (mig 534) — a COLUMN on browse_list/map_mv/listing_feed_public (mig 535), never `estate_area`.
Heal via `reparse.yml` (`--source <portal> --fields area_m2,estate_area,usable_area,garden_area --allow-snapshot-deferral`), which replays `parse_detail` over the stored detail page: dispatch-only, dry-run default, one source per run, no R2. It NEVER blanks a stored value.

## How to manually trigger the scrapers

The sreality pipeline is **split by cadence (Phase 2)**: `index_walk.yml` ("Scraping: Sreality
index walk", cron `*/15`) feeds `detail_drain.yml` ("Scraping: Sreality detail drain", cron
`*/15`). `scrape.yml` ("Scraping: Sreality combined walk") is the **dispatch-only fallback** —
the proven combined index+detail `_run_full`, kept for instant revert (re-add its `schedule:`
cron, disable the two new ones) and ad-hoc full walks. The bazos crawl is **cadence-split**
like sreality (bazos walks 14 nationwide scopes, ~1500 index pages — a combined run starves the
drain): `bazos_index_walk.yml` ("Scraping: Bazos index walk", cron `0 */6`, full walk +
mark_inactive + enqueue) feeds `bazos_detail_drain.yml` ("Scraping: Bazos detail drain", cron
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
`PROXY_REQUIRED=False`, being throttled rather than blocked);
runs both phases in one job via `mmreality_main`, bounded by `--max-pages`/`--max-detail`). The remax
scrape is `scrape_remax.yml` ("Scraping: RE/MAX scraper (pilot)", every 6h + dispatch; runs both
phases in one job via `remax_main`, bounded by `--max-detail` + a `--max-seconds` budget so the
~7,900-listing backlog drains over several ticks). The idnes scrape is
**cadence-split** like sreality (iDNES is large — 4,235 index pages, ~110k listings):
`idnes_index_walk.yml` ("Scraping: iDNES Reality index walk", `idnes_main --index-only`, cron
`15 */6`) feeds `idnes_detail_drain.yml` ("Scraping: iDNES Reality detail drain", `--drain-only`,
hourly `30 * * * *`, bounded by `--max-seconds`; `SCRAPE_CHAIN_TOKEN` re-dispatches it while the
queue has work). **idnes delisting is PARKED (migration 453)**; its walk is sliced into the 14
kraje + abroad with each slice's outcome in `portal_index_slices` (454), and `coverage_gate.yml`
("Ops: index coverage gate", cron `15 3,9,15,21`) un-parks it on evidence, unattended. **Parked
flags, the slice ledger and the gate: `references/coverage-and-delisting.md`.**
There is no combined bazos/idnes fallback workflow anymore — sreality's
`scrape.yml` is the only retained combined fallback (its `_run_full` is the instant revert for
the split); for the other portals an ad-hoc combined run is `python -m scraper.<portal>_main`
locally. The properties track adds
`property_maintenance.yml` (**dirty-set incremental, cron `*/5`** — attaches new stragglers as
singletons + recomputes only changed properties; rule #20) and
`recompute_property_stats.yml` (the **daily full-sweep reconcile** at 04:15 — recomputes every
property + clears the dirty queue, within a `--max-seconds` budget: on exhaustion it clean-stops
at a batch boundary, clears only the swept id range, exits RED, and leaves the completion stamp
unwritten so the `property_maintenance` check alarms). The visual-signal producers run alongside:
`compute_image_phash.yml` (hourly pHash backfill, active-listing images first), `clip_tag.yml`
(`scripts/clip_tag_backfill.py` — zero-shot CLIP room/plot tags into `image_clip_tags` + a 512-d
vector into `image_clip_embeddings`), `clip_retag.yml` (re-runs the zero-shot over each image's
STORED embedding when the taxonomy changes, per `app_settings.clip_taxonomy_retag_after`; no R2
download, no re-inference) and `backfill_render_score.yml` (render-vs-photo axis). Newest and
**dispatch-only**: `dinov3_embed_backfill.yml` (GPU, inert until the encoder config is complete),
`tagging_bakeoff.yml` (GPU, the encoder EXPERIMENT, `dedup_sim` only), `tag_model.yml` (CPU, ONE
versioned tag model, mig 490 — the tag is the argmax head) and `new_dedup_candidates.yml` (CPU, Level 0
path C = same town + disposition/area; `estimate`/`verify` read only). **Read the matching `references/*-lane.md` before touching.**

**There is NO scheduled dedup job any more** — the whole automatic decision layer (the engine, its queues,
its batch warmer, its geo/byt-geo runs, the model-compare and vision A/B harnesses, the publication gate) was
removed wholesale in the 2026-08 NEW DEDUP cutoff (architectural rule #15; `docs/design/new-dedup/CUTOFF.md`)
and its workflows/scripts must never be resurrected; nothing auto-merges (merges are operator-ordered through
`POST /properties/merge`). The tagging/pHash/embedding lanes above ARE kept running because the rebuilt engine
(`docs/design/new-dedup/PROGRAM.md`) consumes them, so treat a stalled `clip_tag.yml` or
`compute_image_phash.yml` as a real problem even though nothing reads their output for decisions today.

**CLIP tagging persists an embedding for every TAGGED image** (PR #748, ~19% coverage gap; PR #751 backfilled the tagged-but-vectorless backlog).
**Any job that REPLACES an image's bytes under its existing `storage_path`** (the sreality re-master lane — `images.rendition` / `stored_width` /
`stored_height`, migration 496) **must go through `db.invalidate_derived_signals`**: the one chokepoint that re-arms these producers by nulling their
OWN predicates (`phash`, `clip_tagged_at`). It deletes no label/review/CLIP-cache row; DINOv3 vectors go only on `drop_dinov3=True` (hand-dispatched GPU refill).

**No drain geocodes, and no place columns.** W4-c dropped every location column from `listings`
and `properties`, so a parser's coordinate/locality/street reading is evidence for the resolver,
not a write; a listing's place is `listing_location` (join on `listing_id`).
`scraper/geocoding.py` survives for `/maps/*` and the on-demand URL parse only.

Monitor/alerting workflows watch the rest: `monitor_workflow_failures.yml` ("Monitoring: workflow
failures", cron `*/30` — records failed / timed-out / startup-failed runs into `workflow_failures`
so the Health page can list them, since GitHub only emails about failed *scheduled* runs; a
never-started supersession cancel is distinguished from a genuine failure, and the run's cursor +
whether a timeout killed it are captured) and `llm_health.yml` ("Monitoring: acute health", hourly
— verify_pipeline's acute lane: `llm_errors`, `llm_burn_rate`, `db_saturation`,
`worker_liveness`, `property_maintenance`, `broker_resolution_freshness`, with
`--exit-nonzero-on-fail` so any `fail` goes red and emails). A credit-balance error alarms
immediately, and the LLM failure probe is INDEPENDENT of pending work — that blind spot kept a
credit-exhausted account green for ~8h while condition scoring happened to be quiet; `LLMClient`
records the failure row on every provider exception, so the check needs no key of its own.
`llm_burn_rate` watches daily LLM spend for the recurring credit-depletion pattern (warn threshold
operator-tuned via `pipeline_check_thresholds`, currently 130; incident history in the
`llm-credit-outage-health-gap` memory) and lands its rows in the same `pipeline_check_results`
table the verification harness below writes to. Run any directly:
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
`recompute_property_stats.yml`, `clip_tag.yml`, …) stay **untagged** (`portal: null`) and
appear in the full Settings → Workflows list rather than any single portal's schedule. As with any
workflow edit, regenerate `frontend/public/workflow-docs.json` in the same commit (a FETCHED asset,
not a bundled module — see `docs/architecture.md`); CI's `--check` guards drift.

**The split (architectural rule #19).** The cheap "which ads still exist" check is decoupled
from the slow "download each ad" write:
- **`index_walk.yml` (fast, frequent).** Walks the **entire** index of every category pair (no
  `--limit`), `touch_listings` bumps `last_seen_at` on still-listed ids, unseen ids are nominated
  for a page check (rule #3, 2026-09-07), and new + price-changed ids. The walk carries a
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
  reserve backfills to refresh. Do NOT reintroduce a single ordering across both. No detail fetch,
  so delistings surface within minutes. Records `run_type='index'`, `index_pages>0` (what Health
  liveness keys off). Uses the **transaction pooler** (`connect()`) — bulk set-based statements,
  no per-listing loop.
- **`detail_drain.yml` (slow, async, bounded).** Claims a bounded slice of the queue
  (`--max-detail-refetches`, the workflow passes 12000), fetches details on a rate-limited pool, and writes
  them **batched** via `db.write_detail_batch` (set-based `jsonb_to_recordset`, one transaction
  per ~100 listings, ~0.1–0.2 s/listing). Uses the **session pooler** (`connect_session()`) for
  prepared statements. New listings land with `property_id` NULL and become **singletons** via
  `recompute_property_stats`'s straggler-attach (the hot write path carries no matching at all;
  grouping is out-of-band and operator-ordered, rule #15). A gone fetch flips that listing inactive +
  dequeues it; a transient error bumps the queue row's `attempts` (given up after 5) and stays queued. Records `run_type='detail'`,
  `index_pages=0`. The queue persists across runs, so a bounded run never loses work; a SIGKILLed
  claim is recovered by the next run's `reclaim_stale_claims`.

**Delisting is presence-verified (rule #3, 2026-09-07); the gate is STRUCTURAL (2026-09-08).** A
walk that REACHED THE PORTAL'S END (`portal.walk_reached_end`: every unit walked, each page loop out
on a portal terminator, no stop of ours) nominates every active row it did not see (`VERIFY cm=…
candidates=… queued=… deferred=…`); the drain fetches each page and only a positive gone signal
(404/410, a redirect off the listing, the portal's "no longer active" text → `ListingGoneError`)
flips it; a live page refreshes it. The COUNT never vetoes — a short walk that reached the end
nominates and logs `COVERAGE`. No absence sweep, no staleness rail; `delist_flip_cap` throttles per
walk ONLY above its 2,000-active-row floor (`VERIFY DEFERRED`, in `delist_flip_refusals`); the page must be the row's OWN (a `detail_ref`
naming another id is dropped). Stop-reason vocabulary: `references/coverage-and-delisting.md`.

**Condition scoring is currently UNSCHEDULED — an intentional pause, not a bug** (PR #730,
confirmed operator-intentional 2026-07-09; ~56k byt rows unscored is accepted). Don't
re-enable or backfill without explicit direction. The machinery is otherwise unchanged and
**batch-driven** when it does run: `condition_score_batches.yml` is the driver (Anthropic
Message Batches API, 50% cost) — `submit` (previously every 3h) puts the next slice of
unscored listings in a batch, `ingest` hourly (`35 * * * *`, still live for any in-flight
batch) polls + persists; one workflow, mode chosen by `github.event.schedule`. The
synchronous `condition_scores.yml` is a **dispatch-only fallback** — don't schedule both,
they select the same pending listings and the sync scorer doesn't skip in-flight batch rows.
The scoring model is `app_settings.llm_condition_model` (Haiku today), so batch+Haiku ≈ 25%
of the original Sonnet-sync cost. Both scrape workflows still pass `--no-condition-scoring`.
Scoring is **kraj-scoped and reuse-first** (migration 174):
the selector targets only listings whose resolved kraj (`listing_location.kraj_kod`) is in
`app_settings.condition_scoring_enabled_region_ids` (operator-edited via the Settings page
"Hodnocení stavu — kraje" toggles; empty = paused; an unresolved listing is parked), and
`propagate_condition_levels` copies a property's genuine score to its cross-portal siblings
(`listings.condition_levels_propagated_from` records provenance) before every submit/backfill,
so a duplicate never re-bills the LLM. `check_llm_health` mirrors the same scope.

**Images** stay decoupled across four workflows (both halves of the scrape split pass `--no-image-downloads`; the drain only
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
  OVERWRITING each R2 object under its stored key; dead URLs re-resolve from live detail. Final: `REMASTER done …`.

**Cadence:** `*/15` for each half, deliberately — frequent index walks surface delistings fast, while
the bounded drain keeps a steady, polite fetch volume. GitHub throttles scheduled workflows, so effective
cadence is slower; Health liveness/freshness thresholds are **per-portal cadence-aware**
(`portals.scrape_cadence_minutes`, migration 114): `scraper_health_checks` scales liveness warn at 1.5× /
fail at 3× the portal's cadence, freshness warn at 1× / fail at 3×. sreality (cadence 60, ~hourly real)
reproduces the original 90/180 + 60/180; the 6h pilots (bazos/bezrealitky/idnes, cadence 360) get
proportional thresholds so they aren't falsely red between runs. Concurrency: each workflow has its own
group with `cancel-in-progress: false` — a long run is never killed mid-batch, the next tick queues behind
it. Per-category nominations are queued immediately after each category's walk, so even a timed-out index
walk leaves a consistent partial result.

The detail-drain writes `scrape_runs` rows too (`run_type='detail'`), but only the **index walk** sets `index_pages>0` — so "last scrape", the liveness check, and reconciliation track the index walk specifically, while the 24h new/updated/error counters sum across the drain's `index_pages=0` rows too (see `scraper_health_checks()`, migration 105). The image backfill (`--images-only`) deliberately writes NO `scrape_runs` row — recording it once polluted liveness/reconciliation with `index_pages=0` noise.
**The lifecycle around both phases lives in ONE place, `portal_runner.run_phase`** (rule #21; never re-add a per-portal copy): `ended_at` means the phase COMPLETED, so a phase that raises bumps `errors` and deliberately leaves `ended_at` NULL, lighting up both the `stuck` and `err_pct` health arms instead of neither. That same crash path is W3's failure-signature producer (`ops_incidents`, migration 462). **The crash contract, the signature grammar and the log-tail backstop: `references/pipeline-verification.md`.**

## The real-time worker (`scraper/realtime_worker.py`)

A dark-by-default, always-on Railway service (a 2nd process from the SAME image, gated by
`REALTIME_WORKER_ENABLED`) that replaces cron quantization for the latency-critical parts of
the pipeline — the GH Actions crons above are still the throughput/completeness backbone; the
worker is the latency layer on top. Design + shipped waves: `docs/design/realtime-scrapers.md`.
Lanes shipped so far:
- **Per-source drain-disable knob** (`realtime_drain_disabled_sources`, PR #694) — the bounded
  detail drain skips sources listed here, letting a portal be pulled from the real-time lane
  without touching its GH Actions cadence.
- **sreality count-probe lane** (migration 270, PR #696) — a lightweight per-`(category_main,
  category_type)` count check that detects a market-wide count swing faster than a full index
  walk would, feeding the completeness/delisting rails.
- **Property-maintenance lane**, every 2 min (PR #716) — runs `run_incremental_pass` against
  `dirty_properties` (rule #20) far more often than the 5-min GH Actions cron. It serializes
  against the GH cron + daily sweep with the lease-row CAS pattern (PR #717): **never a session
  advisory lock on a pooled connection** — the first cut stranded within minutes of deploy.
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
  (`claims_intake.run`, `mode="incremental"`; JSON half first, then a bodies pass on the
  remainder — cap `LOCATION_INTAKE_FAST_BODIES_CAP` 300, R2 width 8, ONE 2-wide `ExtractionPool`
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
- **Text-extract lane** (field-capture W7, `toolkit/description_extraction.run_pass`) — the
  post-publication read of the facts a prose-only advert states in its text and nowhere else. Ships
  **LIVE** on a CONSTANT 300 s interval: no flag, no setting, no env var (the estimation lane above
  is why — a lane nobody enabled is a lane no monitor can see). Scope = the attribute contract's
  `text` cells carrying a `gate`, so none declared is one indexed query a tick. 250 newest-first
  every pass + 750 OLDEST-first HOURLY — the DESC-only order is why the deleted lane's 50k backlog
  was unreachable, and the ASC arm cannot stop at its LIMIT once nothing is eligible (a measured
  ~10 s), so it is not a per-pass cost; 8 threads through `toolkit.vision_batch.run_batch`;
  one in-process pass lock, because an abandoned pass keeps billing. Heartbeat
  `details.text_extract.last` = `{claimed, extracted, written, by_column, dropped, errors,
  spent_usd, model, backlog, slice_full}`; needs `OPENAI_API_KEY` on the Railway service; rail =
  `text_extraction_lag` below. **Contract, cache key and write gate: `llm-pipelines` skill.**

## Pipeline verification (migration 274)

**No publication gate any more.** Migration 273's `properties.published_at` gate died with the
dedup engine in the 2026-08 cutoff (rule #15); the columns are frozen history and Watchdog's "new
property" cursor is back on `listings.first_seen_at`. Durable lesson: a `SECURITY DEFINER` function
in a view's `WHERE` needs a scalar subquery, never a bare call — `database` skill, InitPlan gotcha.

**Pipeline verification harness** (`scripts/verify_pipeline.py`, migration 274, PR #703) — a
scheduled job that writes one `pipeline_check_results` row per health metric (`ok`/`warn`/`fail`) and
is the origin of the notification system's third producer, `system_health` (`docs/architecture.md`
rule #16) — a `fail` rings the same in-app bell the SPA nav badge polls, once per INCIDENT: onset,
then 6h/24h/72h/weekly while red, then one recovery (W3.4's ladder + flap cooldown in
`toolkit/system_alerts`, inherited by every check). Born from the 2026-07 two-day silent stall
(Anthropic credit exhaustion, 38k+ failed LLM calls) whose only alarm was a cron the operator missed.
Two lanes: `llm_health.yml` hourly (the acute checks, `--only ... --exit-nonzero-on-fail`, so a
`fail` also reds the run and emails) and `verify_pipeline.yml` 6-hourly (everything). Live checks:
`llm_errors`, `llm_burn_rate`, `db_saturation`, `worker_liveness`,
`dual_write_parity`, `property_maintenance`, `broker_resolution_freshness`,
`broker_merge_suppression`, and two 6-hourly-only groups — migration 437's
`long_open_transaction` (warn-only: the llm-cost rollup's 3h trailing re-scan stops self-healing
once a transaction outlives it), and the per-m² program's four plausibility checks over
`measure_plausibility_by_source` (migration 427) — `ppm2_median_shift`, `ppm2_basis_floor_share`,
`area_vs_usable_divergence` and the denominator arm `ppm2_measure_coverage`, whose three siblings
are ratios that skip a cell with no inputs and would read clean on a corpus gone dark. **`acquisition_lag` + `walk_coverage`
(2026-08-27)** close the ingestion blind spot: until then every scraper health signal compared our
data to our own data and rendered as a dot on a page, so sreality ingested ZERO new listings for
nine days without anything leaving the database. `acquisition_lag` reads the oldest unclaimed never-fetched `listing_detail_queue` row per portal — deliberately the QUEUE and not `listings.first_seen_at`, because a "no new rows in N hours" check needs a baseline that the outage itself erodes (nine days of zeros makes zero the expected value). **`text_extraction_lag`** (field capture W7) is its twin one layer later — oldest ELIGIBLE-unextracted advert + waiting count per portal, plus a wedge arm (rows eligible, lane claiming none, or the lane absent from the heartbeat) — and it is computed from the text lane's OWN selector predicate, because the outage it replaces was a lane and its three monitors disagreeing about who was eligible. `walk_coverage` is the only
comparison against EXTERNAL truth: collected vs the portal's advertised total from the latest
COMPLETED index run's `by_category`, plus a truncation arm (categories walked vs that portal's own
7-day best) because a budget-stopped walk leaves no entry for the categories it never reached and
so makes the gap look BETTER. remax and maxima derive their total as `len(seen)` and mmreality
reports none — all three are reported `verifiable: false` rather than 100%. **`worker_lane_stall`** closes the gap `worker_liveness` structurally cannot see — a worker that is ALIVE with a wedged lane. The realtime worker beat every 30 s for nine hours while its drain lane completed ONE pass and its images lane completed 486; a pass was recorded only on COMPLETION, so a hung lane and an idle lane published byte-identical state. The worker now stamps when a pass BEGINS and the heartbeat resolves it to `in_flight_s`, and `_lane_loop` bounds every pass with `LANE_PASS_TIMEOUT_SECONDS` (1800) — containment, not a diagnosis: it stops one hang costing every later pass, and repeated timeouts on one lane are themselves the diagnosis. Caveat worth knowing: a pass blocked inside `asyncio.to_thread` keeps running after cancellation (Python cannot kill a thread), so the lane is freed but the thread is not. **`migration_drift`** closes a different silent gap: it probes the live catalog for the objects the newest 25 migrations declare, so a migration merged but never applied is caught in one tick instead of the 29 h it took on 2026-08-25 (see the `database` skill). **`workflow_poller_liveness`** (W0.1, registered in the 6h lane only for now — promote it into `llm_health.yml`'s `--only` list after a soak) keys on the AGE of `app_settings.workflow_failures_cursor`: `record_workflow_failures.py` excludes its own runs from `workflow_failures`, so a dead poller cannot appear in the table it feeds — it just stops adding rows, which is byte-identical to a quiet week. **Three rules the harness now enforces on itself** (W0 of `docs/design/reliability-program.md`; evidence in the reference below): **silence is not recovery** — a failure is superseded only by a newer SUCCESS, never by elapsed time, so never reintroduce a recency window into a state check; **a zero is ambiguous, so name the arm** — `llm_burn_rate` carries `details.arm` (`starved`/`idle`/`runaway`/`ok`), evaluated per `called_for`; and **results are persisted AND alerted per check as each completes**, under a per-check budget and a lane budget — the acute lane's 120s `_LANE_BUDGET_S` out of its 300s job, the full lane's `full_lane_budget_s` (one per-check budget per registered check, CI-bounded by the 30-min job) (an overrun is `warn` "timed out", an unreached check `warn` "not run" — neither is ever `ok`). **`field_fill_matrix`** (field capture W1) reads the VALUES, not just presence — the half
`data_quality_by_source` structurally cannot see: per (source, field) over EVERY active row — a
sampled cohort cannot be compared with itself a week later, the newest-1,000 slice rotated 30+ pp on
untouched parsers — it rings when fill collapses against the blessed baseline in
`data/field_capture/`, when the off-canon share against `toolkit/filter_registry` rises, or when a
blessed cell stops being measured at all; today's zeros are blessed KNOWN and named on every run, and
a census older than 30 d warns (a CI test would red `main` on a date, not on a defect). Re-blessing is
the OPERATOR's (step 4 above), both goldens' tests are subset assertions so an unblessed new field or
portal reds nothing, and it owns no thresholds (sized on that cohort's measured drift) — every other
check's live in `app_settings.pipeline_check_thresholds` over code defaults in `DEFAULT_THRESHOLDS`.
**`floor_convention`** (field capture W8) asks what no fill or validity measure can — whether a
populated, plausible integer is on the RIGHT SCALE: `mean(portal floor − idnes floor)` over active
`byt` rows sharing a (price, area, disposition) key, warn 0.35 / fail 0.50 / min 40 pairs. It is the
lane's costliest check (11-35 s, registered last among the DB checks) and the ONE that ships RED on
purpose — the six ground = 1 portals stay +0.8..+1.1 until the operator has run the six
`scripts/reparse.py --fields floor` heal passes in the W8 hand-over § 7.
**Per-check rationale, incidents and threshold sizing: `references/pipeline-verification.md`.**

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
- `VERIFY cm=... ct=... subtype=... candidates=N queued=M deferred=K active=A` — rows nominated for
  a page check (rule #3); `VERIFY skipped ...: the walk did not reach the portal's end (our stop:
  ...)` when it may not, and `COVERAGE cm=... ct=...` WARNS when it nominates while short
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

