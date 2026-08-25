---
name: scraper-ops
description: Use when running, debugging, or extending the scrapers — triggering the per-portal index-walk/detail-drain workflows, adding a new scraper field without breaking data, refreshing per-source HTML fixtures, reading the pipeline logs (INDEX/ENQUEUE/INACTIVE/DRAIN/IMAGES line shapes), the always-on real-time worker (probe/drain/images/count-probe/property-maintenance/estimation lanes), the visual-signal producer jobs (image pHash, CLIP tagging/retag), or the pipeline verification/alerting harness. Also covers condition-scoring (currently unscheduled) and image-download workflow cadence. Triggers on: index_walk, detail_drain, gh workflow run, mark_inactive, scrape_runs, fixtures, RUN done, a new listings column, onboarding a portal, reading a scrape log, realtime_worker, clip_tag, compute_image_phash, verify_pipeline, llm_burn_rate.
---

# Scraper operations

Operating the scrapers: adding a field, refreshing fixtures, triggering the workflows, and
reading the logs. The per-portal ingest architecture (what each portal is, parser
strategy, completeness posture) lives in `docs/architecture.md` § Data sources. Default
test / log helpers: `scripts/test-summary.sh` and `scripts/logs.sh <run-id> [pattern]`.

## Adding a new scraper field without breaking existing data

1. Add the column with a new numbered migration (`alter table listings add column ...`). Never
   touch `001_initial.sql`.
2. Update the parser in `scraper/parser.py` to extract the field.
3. Update the upsert in `scraper/db.py` to include the new column.
4. Backfill old rows: either leave them NULL (acceptable if the column is nullable) or run a
   one-off SQL update from the `raw_json` column, which already contains the full source
   record.

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

`scripts/reextract.py --source <portal> --field <media|description|broker> [--since YYYY-MM-DD] --dry-run` replays the
CURRENT parser over already-stored `portal_raw_pages` HTML — no re-fetch, and it repairs
**inactive** listings too, which a re-fetch structurally cannot. Snapshot-safe by construction:
it writes only child media rows, never a `listings` content column, so the content hash cannot
change (rule #2). Dispatch via the `reextract.yml` workflow; resumable by keyset cursor, so
re-dispatch until it reports `recovered≈0`.

For `--field media` it only repairs listings with **zero** image rows. `record_images` upserts on
`(listing_id, sequence)` = gallery position and refreshes the URL only `WHERE storage_path IS
NULL`, so re-parsing a listing that already holds photos and now yields more of them shifts
every later photo's position — downloaded rows keep an old URL at a sequence the new parse means
for a different photo. Partial-loss recovery therefore needs a stable media identity, not a
positional one, and is deliberately not attempted here.

**Hashed vs unhashed fields.** The `_FIELDS` registry declares, per field, whether it sits in
`_HASH_FIELDS`, and the module raises at import if that ever disagrees with
`scraper.scraped_listing`. An unhashed field (`media`) writes only child rows → zero snapshots.
A hashed field (`description`) genuinely changes the content hash, so **one snapshot per listing
is appended on that listing's next natural detail scrape** — deferred, never skipped, and spread
over the normal cadence instead of landing all at once. `--allow-snapshot-deferral` is required
so that is a deliberate choice. Hashed fields are written with a targeted single-column UPDATE,
never by replaying a whole `ScrapedListing`, which would rewrite every other column from a
possibly-stale stored page and could regress a price the portal has since changed.

## The location-data gates riding the ingest path

All are OFF by default, cached ~60 s per process (a flip reaches the always-on worker within a minute, a
cron run instantly), and wrapped so any failure warns and returns — an instrument or an archive must never
break the scrape it rides in. **None may be enabled before the operator's churn + storage sign-off**
(02 §2.3.2's gate; `python -m scripts.location_payload_churn_report`).

- **`app_settings.location_payload_shadow_hash`** (W2a-0) — the *instrument*. Counts fetches and
  raw-vs-normalised changes into `portal_payload_churn`, one row per `(source, source_id_native,
  page_kind, normalizer_version)`, **no body ever stored**.
- **`PortalLimits.payload_dual_write`** (W2a-2) — the *archive*, and the OUTER gate every body passes:
  everything `upsert_portal_raw_page` stages (7 HTML detail writers + 3 index archivers) plus sreality's
  estate JSON and bezrealitky's advert-with-query from their own `append_payload_if_enabled` call sites. A
  per-portal **operational limit**, not an app_settings flag, so no migration. Alone: only `detail`.
- **`PortalLimits.payload_index_archive`** (W2a-6) — the *surface-grain second gate*, ANDed on top for
  **every `page_kind` except `detail`**. The split is about GRAIN, not the word "index": ceskereality's map
  and bezrealitky's gazetteer declare `archive: true`, so a gate naming only `'index'` would have let both
  archive every walk. It only narrows dual-write, never widens it.
- **A measured page weight** (W2a-7) — the *third gate, not an operator switch*. The chokepoint refuses any
  `(source, page_kind)` missing from `location_data.payload_budget.PORTAL_STORAGE` (today: every portal's
  `detail`), since archiving an uncosted surface silently invalidates the signed ceiling.
  **`payload_index_archive` does nothing until that surface is measured** in —
  `python -m scripts.location_payload_storage_ceiling` re-derives it; logs `payload archive refuses
  unmeasured surface source=… page_kind=…`.

**Bodies live in R2, so `payload_dual_write` needs the R2 env vars** (the image lane's four) — on all 14
page-fetching lanes since #1074, held by `tests/test_scrape_lane_r2_env.py`; MISSING when the flag was first
called ready, and INVISIBLE: the append warns `payload archive needs R2 for …` and archives nothing while the
scrape stays green. Railway's worker: own dashboard. Over `LOCATION_PAYLOAD_R2_THRESHOLD_BYTES` (2048, TOAST)
compressed spills to `body_r2_key` — every portal but bezrealitky, whose small bodies archive inline.
Knobs: `LOCATION_PAYLOAD_VERSION_CAP` (2), `LOCATION_PAYLOAD_MIN_APPEND_INTERVAL_DAYS` (7, per-listing time
floor; 0 disables), `LOCATION_PAYLOAD_STATS_EVERY` (200).

```sql
-- one portal; swap the key for "payload_index_archive" to add its non-detail surfaces
update portals set operational_limits = coalesce(operational_limits, '{}'::jsonb)
  || '{"payload_dual_write": true}'::jsonb where source = 'idnes';
insert into app_settings (key, value) values                  -- or the global underlay
  ('scraper_limits_global', '{"payload_dual_write": true}'::jsonb)
  on conflict (key) do update set value = app_settings.value || excluded.value;
```

Verify with `select source, page_kind, count(*), max(version_seq) from portal_raw_payloads group by 1,2;`
— append-on-CHANGE, so an unchanged refetch must add no row. Failures read `payload archive append failed
source=… key=…` / `payload archive limit read failed source=…` in the walk or drain log and are never fatal
— so **a broken archive looks like a healthy scrape**: `portal_raw_pages` keeps filling while
`portal_raw_payloads` silently stops. The audit below catches that (it calls it STALLED).

**Before flipping `payload_index_archive`, run
`python -m scripts.location_index_archive_audit`** (`--skip-db` for the code/contract half
alone, which also degrades to that half on its own if the DB read times out). It reports each
portal on three axes — what the contract asks, whether the code's archive call site is
`wired`/`gated`/`absent`, and whether staging AND payload rows are accumulating. Today all three
call sites (sreality, remax, ceskereality) are **gated**: their client-side freshness skip returns
before the archive call, so enabling the flag archives an index body at most once per
`INDEX_ARCHIVE_REFRESH_HOURS` (22 h) per page position and drops every intra-window change. A
KNOWN GAP, commented at each call site; reworking the skip is an open P2 question — the audit
measures it, it does not fix it. Classification is by **reachability**, not by the guard merely
being present, so when P2 hoists the append above the guard the audit reads `wired`.

## How to manually trigger the scrapers

The sreality pipeline is **split by cadence (Phase 2)**: `index_walk.yml` ("Scraping: Sreality
index walk", cron `*/15`) feeds `detail_drain.yml` ("Scraping: Sreality detail drain", cron
`*/15`). `scrape.yml` ("Scraping: Sreality combined walk") is the **dispatch-only fallback** —
the proven combined index+detail `_run_full`, kept for instant revert (re-add its `schedule:`
cron, disable the two new ones) and ad-hoc full walks. The bazos crawl is **cadence-split**
like sreality (bazos walks 14 nationwide scopes, ~1500 index pages — a combined run starves the
drain): `bazos_index_walk.yml` ("Scraping: Bazos index walk", cron `0 */6`, full walk +
mark_inactive + enqueue) feeds `bazos_detail_drain.yml` ("Scraping: Bazos detail drain", cron
`45 * * * *`, bounded `--max-seconds`); a third job, `bazos_description_enrichment.yml`, backfills
free-text description enrichment every 3h (PR #733) — bazos's ad text needs a separate enrichment
pass the other portals' structured pages don't. Its tool (`toolkit/bazos_enrichment.py`) was
slimmed to the 8 fields it actually consumes with the LLM call's `tool_choice` FORCED (PR #768) —
the prior full-schema tool let ~27% of calls return prose instead of a tool call, which wrote no
cache row and re-billed forever; a `no_extraction` result now also caches, and the driving script
aborts (exit 1, red workflow) after 5 consecutive provider errors instead of finishing green on a
dead API key. The bezrealitky scrape is
`scrape_bezrealitky.yml` ("Scraping: Bezrealitky scraper (pilot)", every 6h + dispatch; runs
both index walk + detail drain in one job via `bezrealitky_main`). The maxima scrape is
`scrape_maxima.yml` ("Scraping: Maxima Reality scraper (pilot)", every 6h + dispatch; the
~220-listing catalogue fits both phases in one job via `maxima_main`). The mmreality scrape is
`scrape_mmreality.yml` ("Scraping: M&M Reality scraper (pilot)", cron `50 */6` + dispatch —
every request via the residential `SCRAPER_PROXY_URL` (Cloudflare 403-blocks datacenter IPs);
runs both phases in one job via `mmreality_main`, bounded by `--max-pages`/`--max-detail`). The remax
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
locally. The properties track adds
`property_maintenance.yml` (**dirty-set incremental, cron `*/5`** — attaches new stragglers as
singletons + recomputes only changed properties; rule #20) and
`recompute_property_stats.yml` (the **daily full-sweep reconcile** at 04:15 — recomputes every
property + clears the dirty queue, within a `--max-seconds` budget: on exhaustion it clean-stops
at a batch boundary, clears only the swept id range, exits RED, and leaves the completion stamp
unwritten so the `property_maintenance` check alarms). The visual-signal producers run alongside:
`compute_image_phash.yml` (hourly pHash backfill, active-listing images first),
`clip_tag.yml` (`scripts/clip_tag_backfill.py` — zero-shot CLIP room/plot tags into
`image_clip_tags` + a 512-d vector into `image_clip_embeddings`), `clip_retag.yml`
(`scripts/retag_from_embeddings.py` — re-runs the zero-shot over each image's STORED embedding
when the taxonomy changes, driven by `app_settings.clip_taxonomy_retag_after`; no R2 download,
no re-inference) and `backfill_render_score.yml` (one-shot render-vs-photo axis backfill from
stored embeddings).

**There is NO scheduled dedup job any more.** The automatic decision layer — the engine, its
queues, its batch warmer, its geo/byt-geo runs, the model-compare and vision A/B harnesses, and
the publication gate — was removed wholesale in the 2026-08 NEW DEDUP cutoff (architectural rule
#15; `docs/design/new-dedup/CUTOFF.md`). Nothing auto-merges; merges are operator-ordered through
`POST /properties/merge`. The tagging/pHash/embedding lanes above are kept running precisely
because the rebuilt engine (`docs/design/new-dedup/PROGRAM.md`) consumes them, so treat a stalled
`clip_tag.yml` or `compute_image_phash.yml` as a real problem even though nothing reads their
output for decisions today. Do not resurrect the removed workflows or scripts.

**CLIP tagging persists an embedding for every TAGGED image, not just active-listing ones**
(PR #748) — it closed a ~19% coverage gap; a spare-capacity repair phase (PR #751) backfilled the
pre-existing tagged-but-vectorless backlog.

A unified `CoordResolver` (`scraper/location.py`, migration 288, PR #749) now backs
idnes/realitymix/maxima/remax/mmreality/ceskereality — four of those had no geocode path at
all before. See the `database` skill's "Location/geocode lifecycle" and "Street lifecycle"
entries for the caching/provenance detail; this is the portal-wiring side of the same change.

Monitor/alerting workflows watch the rest: `monitor_workflow_failures.yml` ("Monitoring: workflow
failures", cron `*/30` — records failed / timed-out / startup-failed runs into `workflow_failures`
so the Health page can list them; GitHub only emails about failed *scheduled* runs; it now
distinguishes a never-started supersession cancel from a genuine failure so cancelled-by-newer-run
doesn't inflate the failure count, and captures the run's cursor + whether it was killed by
timeout, PR #767/#738) and `llm_health.yml` ("Monitoring: acute health", hourly — runs
verify_pipeline's acute lane: `llm_errors`, `llm_liveness`, `llm_burn_rate`, `db_saturation`,
`worker_liveness`, `property_maintenance`, `broker_resolution_freshness`, with
`--exit-nonzero-on-fail` so any `fail` goes red
and emails; it replaced the standalone `check_llm_health.py` in the WS4 alerting rebuild). A
credit-balance error alarms immediately; the LLM failure probe is INDEPENDENT of pending work — it closes
the blind spot where a credit-exhausted account stayed green for ~8h because condition scoring
happened to be quiet. `LLMClient` records the failure row on every provider exception; the check
needs no Anthropic key of its own). Two more alerting layers were added on top: `llm_burn_rate`
(PR #739, warn threshold operator-tuned via `pipeline_check_thresholds`, currently 130 — PR #766)
watches daily LLM spend for the recurring credit-depletion pattern (see the
`llm-credit-outage-health-gap` memory if you need the incident history) — its rows land in the
same `pipeline_check_results` table the verification harness below writes to; and a broader
edge-triggered-alerts / blind-spot-detector rework (PR #732, WS4 tracks A/B/C) consolidates related
LLM alerts instead of firing one per symptom. Run any directly:
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
  grouping is out-of-band and operator-ordered, rule #15). A gone fetch flips that listing inactive +
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
- **Tightened delisting rails for sreality** (PR #697) — sreality's completeness gate moved
  1.0→0.995 and its unseen-staleness window to 3h (vs 12h on the 6h-cadence portals), matching
  rule #3's two-rail design to sreality's faster real cadence.
- **Property-maintenance lane**, every 2 min (PR #716) — runs `run_incremental_pass` against
  `dirty_properties` (rule #20) far more often than the 5-min GH Actions cron. Its first cut
  serialized against the GH cron + daily sweep with a SESSION advisory lock, which is unsound
  over the transaction pooler and stranded within minutes of deploy (PR #717 fixed it with the
  lease-row CAS pattern — see the `database` skill's connection-modes section; don't reintroduce
  a session advisory lock on any pooled connection).
- **Estimation job lane** (migration 349, Wave 1 W1-3 / Phase 1 Amendment A10) — moves agent +
  deterministic rent-estimate EXECUTION off the FastAPI request threadpool (a 240 s agent run
  used to pin a Starlette token; a deploy SIGTERM killed paid runs mid-flight). Claims one
  `pending` `estimation_runs` row per pass via `FOR UPDATE SKIP LOCKED`, flips it `running` +
  stamps `claimed_at`/`worker`, runs the SAME `execute_pending_run` path from a `{body,
  resolution}` snapshot the submit route stored in `job_payload` (the run row stays the job — no
  new table), then clears the payload. Each pass first runs the periodic stuck-run sweep (keyed
  off `coalesce(claimed_at, created_at)`) so a run orphaned by a crash frees its slot. Ships
  **DARK**: idle until `estimation_job_lane_enabled` is set — the SAME flag makes
  `POST /estimations` route rows to the lane instead of an in-process BackgroundTask, so the
  cutover (and rollback) is one setting, no deploy.

## Pipeline verification (migration 274)

**No publication gate any more.** Migration 273 used to hide a new property from Browse, the
map, Stats, the agent and Watchdog until something stamped `properties.published_at` — and the
only stamper for ordinary properties was the dedup engine, so the gate died with it in the
2026-08 cutoff (rule #15). It was flipped inert first (`dedup_publication_gate_enabled=false`),
then removed in code and views; `published_at` / `publish_reason` are frozen as a historical
record. Watchdog's "new property" cursor is anchored on `listings.first_seen_at` again. Keep the
one durable lesson: a `SECURITY DEFINER` function referenced from a view's `WHERE` must be
wrapped in a scalar subquery, not called bare — see the `database` skill's InitPlan gotcha
(migration 275 fixed exactly that on `properties_public` after it broke Browse market-wide).

**Pipeline verification harness** (`scripts/verify_pipeline.py`, migration 274, PR #703) — a
scheduled job that writes one `pipeline_check_results` row per health metric (`ok`/`warn`/`fail`)
and is the origin of the notification system's third producer, `system_health` (see
`docs/architecture.md` rule #16) — a `fail` rings the same in-app bell the SPA nav badge polls,
on state TRANSITIONS only. Born from the 2026-07 two-day silent stall (Anthropic credit
exhaustion, 38k+ failed LLM calls) whose only alarm was a cron the operator happened to miss.
Two lanes: `llm_health.yml` hourly (the acute checks, `--only ... --exit-nonzero-on-fail`, so a
`fail` also reds the run and emails) and `verify_pipeline.yml` 6-hourly (everything). Live checks:
`llm_errors`, `llm_liveness`, `llm_burn_rate`, `db_saturation`, `worker_liveness`,
`dual_write_parity`, `property_maintenance`, `broker_resolution_freshness`,
`broker_merge_suppression`, and two 6-hourly-only groups — from migration 437,
`long_open_transaction` (warn-only: the llm-cost rollup's 3h trailing re-scan stops
self-healing once a transaction outlives it), and from the per-m² measure program's W9, the four
plausibility checks `ppm2_median_shift`, `ppm2_basis_floor_share`, `area_vs_usable_divergence` and
`ppm2_measure_coverage` over `measure_plausibility_by_source` (migration 427), which watch what a
value IS where `data_quality_by_source` only tests that it exists — the fourth watching whether
there is anything to measure at all, since the other three are ratios that skip a cell with no
inputs and would read clean on a corpus gone dark. Thresholds live in
`app_settings.pipeline_check_thresholds` over code defaults in `DEFAULT_THRESHOLDS`.
**Per-check rationale, incident history and threshold sizing:
`.claude/skills/scraper-ops/references/pipeline-verification.md`.**

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

