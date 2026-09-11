# Portal listing URLs — one contract, nine portals

**Status (2026-09-11):** W0 in PR #1400; W1 stacked on it. Operator rulings: the Supabase MCP is
the DB access path; inactive rows DO get URLs; canonical string or nothing (never a placeholder);
the weekly HEAD probe is approved; CLAUDE.md gets a rule-21 clause (not a new rule). Investigation
+ a five-lens adversarial verification + a three-angle design panel (Opus 5) sit behind every
claim; the evidence trail is in the Appendix.

## 0. The report, and what it actually is

The operator reported that the listing page for sreality `1915215948` links to
`https://www.sreality.cz/detail/prodej/dum/rodinny-dum/x/1915215948` (HTTP 404) while the real
page is `https://www.sreality.cz/detail/prodej/dum/rodinny/praha-michle-pod-sychrovem-i/1915215948`.

The 404 is a symptom. The defect is **one asymmetry**: sreality is the only portal whose listing
URL is not a stored fact. Every other consequence follows from it mechanically:

| # | Asymmetry | Consequence today |
|---|---|---|
| A1 | `scraper/parser.py:parse_listing` emits no `source_url`; all 8 crawler parsers do | `listings.source_url` is NULL on every sreality row (~232k) |
| A2 | The SPA *reconstructs* the URL from a **display-label** table (`frontend/src/lib/portals.ts` slugifies `CATEGORY_SUB_LABELS`) | Wrong sub-category slug for ≥14 of sreality's 48 codes (37 "Rodinný dům" → `rodinny-dum`; sreality says `rodinny`), both auction types 404 (`drazba`/`podil` vs sreality's `drazby`/`podily`), and **no link at all** for any land (`pozemek`) or "other" (`ostatni`) listing because the label table has no such codes |
| A3 | `listings_public` carries no `source_url`, so the SPA's `ListingPublic` type has none | The comparable modal passes a literal `null` → **no external link for any of the nine portals** there |
| A4 | `priceHistory.ts` computes ONE category triple from the *parent* listing and applies it to every sibling row | On a merged property, a sibling sreality row with a different sub-category 404s even with a perfect slug table. Per-row reconstruction is **structurally impossible** client-side (`PropertySource` carries no category fields) |
| A5 | `source_url` for the 8 crawlers is written by a bespoke post-insert `UPDATE` (`scraper/db.py:893-896`), outside the `LISTING_COLUMNS` registry | The one listing column with its own write statement, exempt from the shared preserve/batch rails |
| A6 | A green test pins the 404 (`frontend/src/lib/portals.test.ts:70-78` asserts `rodinny-dum`) | The bug survived review because the test agreed with it |
| A7 | `BrokerDetail.tsx` renders the Portál cell as plain text; `LocationQuality.tsx` renders sreality members inert | Operator stranded on two more surfaces |

A1–A7 are one defect and one sprint. A separate finding (four *inbound* URL→portal registries,
one already drifted) is filed in §8 and deliberately kept out.

## 1. North star

> **A listing's URL on its own portal is a FACT its own parser captures once at ingest and
> stores per row in `listings.source_url`, for all nine portals. Every surface READS that
> column. Nothing, anywhere, reconstructs a URL from other fields.**

Decision rule for every wave: *if a change makes any surface compute a URL from category or
locality fields, or creates a second place that knows sreality's URL vocabulary, it is out.*

Corollaries:

1. **One assembly site per portal.** The 8 crawlers already have theirs (`<portal>_client.detail_url`).
   sreality gets one: `scraper/sreality_url.py`.
2. **No fallback.** A fallback is a second source of truth. When the fact is absent the UI shows the
   in-app view and the absence is **counted**, never patched around.
3. **Unknown → NULL + a counter, never a guess.** sreality's sub-category slugs are a *closed
   codebook sourced from sreality* (its sitemap), not a slugified label. An unmapped code yields no
   URL and increments a per-code histogram.
4. **History is filled from stored typed columns, never from `raw_json`.** (§4.1 — the payload
   route is ~14 GB of detoast and does not fit the job budget.)
5. **sreality's stored URL is DISPLAY-ONLY.** Its server-rendered page 302s into a `login.seznam.cz`
   autologin chain and an infinite redirect loop (`contracts/portals/sreality.yaml` header). No fetch
   path may ever key on it; the scraper reaches sreality by id through the JSON API.
6. **Ingest can fill; only an explicit reconciliation can clear.** Preserve-if-null at ingest plus a
   named reconciler that may write NULL (§4.3) — the same rail the resolver street trio uses.

## 2. Verified facts the design rests on

All verified 2026-09-10/11 against the live API/site and the code at `origin/main` f716441e.

- **sreality's canonical URL is** `/detail/{type_slug}/{main_slug}/{sub_slug}/{locality_slug}/{hash_id}`.
  sreality validates `type/main/sub` **strictly** (404 on any mismatch) and is lenient **only** on the
  locality segment (any value, including literal `x`, 301-redirects to the canonical). So the codebook
  is 404-critical; the locality rule is about canonicality, not reachability.
- **Type slugs** come from `category_type_cb.value`: 1 `prodej`, 2 `pronajem`, 3 `drazby`, 4 `podily`.
  Our stored `listings.category_type` says `drazba`/`podil` (parser vocabulary); both 404 in a URL.
  The URL vocabulary is therefore a separate, explicit map — never `listings.category_type` verbatim.
- **Main slugs** equal our stored `category_main` (`byt dum pozemek komercni ostatni`).
- **Sub slugs are a closed codebook of 48 `(category_main, cb)` pairs**, discovered from sreality's own
  sitemap (107,766 detail URLs, 140 type/main/sub triples) → one id per pair → the API's
  `category_sub_cb.value`. Every code outside the 48 is rejected by sreality's own search API (HTTP 422),
  so the live feed cannot produce an unknown code; only historic rows can. Codes are disjoint across
  mains by sreality's numbering. Full table in Appendix A.
- **Locality slug rule**: `{city_seo_name}-{citypart_seo_name or city_seo_name}-{street_seo_name or ''}`.
  sreality's own canonical keeps a **trailing hyphen** when there is no street (`prestavlky-prestavlky-`)
  and **repeats the city** when citypart is null (`hlubocky-hlubocky-`), even when they are equal
  (`karlovy-vary-karlovy-vary-`). Byte-identical to sreality's 301 target on 110/110 live listings.
- **Both the v1 detail and index payloads carry** `category_*_cb{name,value}` and
  `locality.*_seo_name`. `hash_id` is the id. The `+` in flat dispositions must be stored **literally**
  (`2+1`; `%2B` also resolves, `%20` 404s).
- **`listings.raw_json` payloads are 52–72 KB** (mean ~62 KB). Legacy v2 rows (pre-2026-05-26) have a
  different shape (`seo.locality`, `_embedded`, camelCase); their count is unmeasured.
- **Live counts (planner estimates):** ~231,941 sreality rows, ~593,081 other-portal rows.
  Every non-sreality portal stores a non-null page URL (live sample, 8/8).
- **Both sreality write paths** (`db.upsert_listing` and the batched `db.write_detail_batch`) take
  columns only from `LISTING_COLUMNS`; the realtime worker's sreality lane runs through the same
  `portal_runner` → `write_detail_batch`. `scraper/freshness.py` and `scraper/url_parser.py` (pasted-URL
  persist) also go through `parse_listing` → `upsert_listing`.
- **Content hashes ignore identity.** sreality hashes `raw_json`; crawlers hash `_HASH_FIELDS`, which
  deliberately omits `source_url`. Writing or backfilling the column appends **zero** `listing_snapshots`
  rows (rule #2 holds). No trigger on `listings` fires on it (both triggers are `UPDATE OF geom …`).
- **`parse_listing`'s output is diffed in two places** — `scraper/freshness.py:33` and
  `toolkit/snapshots.py:26` (`_DIFF_SKIP_KEYS = {sreality_id, lon, lat}`) — so a new key becomes a
  user-visible "field changed" event unless it is added to both skip sets. Both sites also run
  `parse_listing` over *historic* `raw_json` (including the legacy shape) inside a broad `except` that
  blanks the listing's timeline on a raise. The deriver must be total: decline, never raise.
- **The newest `listings_public` body is migration 425** (not 420 as first assumed). A view append must
  be authored from 425's body or it errors on production and reds `tests/test_measure_registry_census.py`.
- **`listings.source_url` has no index.** It is matched by `api/estimation_runs.py` (`_match_listing_by_url`),
  migration 311/341/412's `join listings l on l.source_url = er.input_url`, and `api/portal_lookup.py`.
  Today none of these can match a sreality row (NULL). After the backfill they can.
- **The delivery surfaces already read the column.** `property_sources_public` (listing header chips,
  price history), `api/notifications.py` `_LISTING_PROJECTION` (Watchdog + Notifications), `portal_lookup`
  (extension) and `broker_listings_public` all select `source_url`. Only `listings_public` lacks it.

## 3. Architecture

```
                 sreality v1 API payload              8 crawler HTML/JSON pages
                          │                                     │
              scraper/sreality_url.from_payload()      <portal>_client.detail_url()
                          │  (codebook + locality rule)          │
                          ▼                                     ▼
            parse_listing() row["source_url"]      ScrapedListing.source_url → to_row()
                          └──────────────┬──────────────────────┘
                                         ▼
                 LISTING_COLUMNS (source_url: text, preserve-if-null)
                 upsert_listing ─┬─ write_detail_batch     ← ONE write contract
                                 ▼
                        listings.source_url  ◄──── scripts/reconcile_source_url.py
                                 │                  (fill history from typed columns;
                                 │                   weekly dry-run = parity check)
        ┌────────────┬───────────┼──────────────┬──────────────┐
        ▼            ▼           ▼              ▼              ▼
property_sources_public  listings_public(+494)  api/notifications  portal_lookup  broker_listings_public
        │            │           │              │              │
   ListingDetail  Comparable  Watchdog/     Chrome ext.    BrokerDetail
   priceHistory   Modal       Notifications                LocationQuality
                       (every surface READS; nothing builds)
```

## 4. Waves

### W0 — the contract (backend, one PR, no migration)

**Purpose:** make `source_url` an ordinary listing column for all nine portals, and make sreality
emit it. From this deploy on, every freshly drained sreality row is correct on the listing header
chips, the Watchdog Portál chip and the estimation prefill — those surfaces already prefer the stored
URL — **before any migration or SPA change**.

1. **`scraper/sreality_url.py` (new, ~120 lines).** One codebook, one assembly rule, two thin input
   adapters (ingest has sreality's own `*_seo_name`; the reconciler has display columns — they must not
   become two derivations).
   - `TYPE_SLUG: {1: prodej, 2: pronajem, 3: drazby, 4: podily}`, `MAIN_SLUG`, `SUB_SLUG` (48 pairs,
     Appendix A), and `STORED_TYPE_SLUG` (`drazba→drazby`, `podil→podily`) derived from the two maps.
   - `canonical_url(*, type_slug, main_slug, sub_cb, city_slug, citypart_slug, street_slug, listing_id)
     -> (url | None, Declined | None)`. Takes already-slugged parts. Never slugifies the sub segment,
     never guesses, never raises. `Declined` is a closed enum of counted reasons
     (`id_missing type_null type_unmapped main_null sub_cb_null sub_cb_unknown locality_null`).
   - `from_payload(raw)` — ingest adapter, passes `*_seo_name` through unslugged.
   - `from_columns(category_type, category_main, category_sub_cb, locality, street, street_source,
     sreality_id)` — reconciler adapter: `slugify(city)`, `slugify(citypart)`, and `slugify(street)`
     **only when `street_source = 'parser'`** (a resolver-sourced RÚIAN street was never on sreality's
     page; omitting it yields sreality's trailing-hyphen form, i.e. at worst a 301, never a 404).
     `split_locality` handles both stored shapes: `"Olomouc - Slavonín"` and the legacy
     `"Jižní, Olomouc - Slavonín"`. A locality that slugifies to empty (non-Latin script) declines.
   - Module docstring carries the DISPLAY-ONLY warning.
   - Deliberate non-choice: the codebook does **not** live in `contracts/portals/sreality.yaml`. Its
     `url_template` rows are read by no code, and the contract files sit under a governed hash whose
     accidental change once killed intake for three days. Executable vocabulary lives in Python; the
     contract file is not touched at all (a comment line would change its governed hash).
2. **`scraper/parser.py`** — `parse_listing` returns one more key: `"source_url": sreality_url.from_payload(raw)[0]`.
   Both sreality write paths, freshness, and the pasted-URL persist path pick it up unchanged.
3. **`scraper/db.py`** — `LISTING_COLUMNS += ("source_url",)`, `_LISTING_COLUMN_PGTYPE["source_url"] = "text"`,
   `_PRESERVE_IF_NULL_COLUMNS += {"source_url"}`. **Delete** the post-insert `UPDATE listings SET
   source_url` block (`:892-896`) and fix the comment at `:874`. Add `db.detail_ref(source, source_url)
   -> str | None` (returns None for sreality, always) and route the two existing string comparisons
   (`db.py:1546`, `scripts/refresh_stale_image_urls.py:63-66`) through it — the one place the
   "never fetch sreality's page" decision lives.
   - Preserve-if-null is **load-bearing**: without it one detail-drain cycle would clobber the W1
     backfill back to NULL on any row whose derivation transiently declines. It is also
     behaviour-preserving for the 8 crawlers today (every parser passes a non-null string, so
     `COALESCE(EXCLUDED, stored)` always takes the new value — byte-identical to the old unconditional
     UPDATE). It **changes** semantics only for a hypothetical parser that emits NULL, which the
     contract guard below now forbids.
4. **`scraper/scraped_listing.py`** — `_LISTING_FIELDS += ("source_url",)`; `_HASH_FIELDS` unchanged;
   a `__post_init__` that raises on an empty `source_url` (the annotation `source_url: str` was never
   enforced; the raise fails one drain item, it does not wedge a lane).
5. **`scraper/freshness.py` + `toolkit/snapshots.py`** — add `"source_url"` to both `_DIFF_SKIP_KEYS`.
   The URL is identity, not content; without this every listing crossing the 2026-05-26 payload
   rebuild would show a spurious "source_url changed" event in its timeline.
6. **Tests.** `tests/test_sreality_url.py` (closed codebook: exactly 48 entries, `^[a-z0-9+-]+$`;
   vocabulary lockstep against `parser.CATEGORY_TYPE/CATEGORY_MAIN/SUBTYPE`; literal `+`; the three
   locality shapes incl. legacy; every `Declined` reason; adapter agreement on one fixture; the
   regression case `1915215948 → …/prodej/dum/rodinny/praha-michle-pod-sychrovem-i/1915215948`;
   legacy-shaped and index-shaped payloads decline without raising). `tests/test_db_source_url.py`
   mirroring `test_db_published_at.py`. **Retarget** `tests/test_db_property.py:179-181` (it pins the
   deleted statement) to assert the URL in the upsert row. `test_scraped_listing.py`: empty URL raises.
   Freshness/snapshots: the new key is skipped.
7. **Docs, same PR.** `docs/architecture.md` § Data sources (sreality paragraph) + one clause on rule 21
   ("…a fetcher + a parser **that emits the row's `source_url`** + a config row"); `.claude/skills/scraper-ops`
   "Adding a new scraper field" gains one line and loses one in the same section (it is at the 500-line
   cap); `roadmap/scraper-track.md` gets the sprint entry.

### W1 — the reconciler (backend, one PR + operator dispatches)

**Purpose:** fill the ~232k historic sreality rows, and become the standing drift detector.

1. **`scripts/backfill_support.py` (new, extraction).** Lift `wait_for_rebuild_gap`,
   `_REBUILD_ACTIVE_SQL`, `_REBUILD_LIKE`, `_REBUILD_POLL_SECONDS`, `_STATEMENT_TIMEOUT_SQL` out of
   `scripts/backfill_area_basis.py` (their only home today) and re-import there. Second user → share
   the rail now, before a third copy exists.
2. **`scripts/reconcile_source_url.py` (new).** Same rails as `backfill_area_basis`: dry-run default,
   `--write`, keyset `--after` resume, `--limit`, `--batch-size`, `--max-seconds`, rebuild-gap wait,
   batched `UPDATE … FROM unnest(...)` with `WHERE l.source_url IS DISTINCT FROM v.url`, idempotent
   without a marker. **Narrow projection only** (`id, source, sreality_id, category_type, category_main,
   category_sub_cb, locality, street, street_source, source_url, is_active, last_seen_at`) — no
   `raw_json` anywhere (~14 GB of detoast for the payload route vs ~3 minutes for a primary-key walk
   of the whole table). **No `source`/`source_url` predicate inside the LIMITed keyset query** (the
   sparse-predicate trap); filter in Python. Per-batch deadlock/timeout retry — the hourly MF-yield
   recompute holds row locks on `listings` for ~90 s twice an hour.
   - Modes: fill (default), `--clear` (explicit eraser: derivation declines AND the stored URL is a
     sreality one → NULL; off by default so the first live pass can never reduce coverage),
     `--conformance N` (one-shot acceptance gate: HEAD-probe N active rows at ≤2 req/s; pass = 200 or a
     301 whose target equals the derived URL; any 404 blocks the write), `--report-check` (write one
     `pipeline_check_results` row, `outbound_url_parity`, see W3).
   - **Counters that carry decisions:** `examined sreality written cleared unchanged`;
     `declined_by_reason{…}`; **a histogram of unknown `category_sub_cb` values with `(cb,
     category_main, max(last_seen_at))`** — the codebook's only drift alarm and the retired-code-reuse
     detector (an unknown code on a row seen in the last 7 days **blocks** the write pass);
     `locality_format{structured, legacy_comma}`; `written_split{active, inactive}`;
     `written_street_source{parser, resolver, null}`; `distinct_urls == written` (a mismatch means the
     id dropped out of the URL — abort, because an unindexed, non-unique `source_url` is what the
     estimation URL-matcher binds a subject on).
   - `locality_null` is the **delta metric**: rows that render a (working) link today and would lose it
     under canonical-only rules. Today's builder emits `x` for locality; the reconciler declines. Read
     this number before deciding anything about placeholders (§7).
3. **`.github/workflows/reconcile_source_url.yml`** — a copy of `backfill_area_basis.yml` (dispatch-only,
   its own concurrency group, `write` defaulting to false, the flags above as inputs). Regenerate
   `frontend/src/lib/workflowDocs.generated.ts`.
4. **Tests.** Table-driven over every decline reason; non-sreality rows always skipped; an already-canonical
   row never rewritten; `drazba → drazby`; unknown code counted not guessed; the SQL constants ride the
   existing PREPARE census.
5. **PR body must state** the two downstream effects: (a) the pasted-URL estimation entry
   (`_match_listing_by_url`) and `property_estimates_public` arm 2 can match sreality rows for the first
   time — account scoping is intact, the arms stay disjoint, but `run_count`/`last_run_at` shift upward
   for sreality properties with no migration, and the Chrome extension may show an estimation badge on
   sreality pages where it showed none; (b) the column goes from 232k NULLs to 232k ~90-char strings on
   an unindexed seq scan (measure-then-index, §8).

**Operator run order (W1):** merge → sizing counts (§7) → `write=false` → read the counters together →
`conformance=40` → `write=true` (clear off) → later, `--clear` once `would_clear` is understood.

### W2 — the read surfaces + the deletion (one migration + one SPA PR)

1. **Migration `494_listings_public_source_url.sql`** — authored from **migration 425's body**, appending
   `source_url` LAST; `set local lock_timeout = '5s'`; no rebuild inside the transaction (five matviews
   depend on the view); re-assert `revoke all … from anon; grant select … to authenticated`;
   `comment on column listings.source_url` carrying the display-only warning;
   `select pg_notify('pgrst', 'reload schema')`. **Apply before the SPA PR merges** (the
   merged-is-not-applied lesson) — a listing-detail read that selects an unknown column 400s.
   Widens `source_url` from `property_sources_public`'s `where property_id is not null` scope to every
   row: same `authenticated` audience, same non-PII data class.
2. **SPA PR.**
   - `queries.ts`: append `source_url` to `DETAIL_COLS` (every `listings_public` read uses an explicit
     column list — without this the migration is invisible). `types.ts`: `ListingPublic.source_url:
     string | null` with the never-reconstruct comment.
   - `portals.ts`: **delete** `srealitySubSlug`, `srealityListingUrl`, `SrealityCategory`, and
     `portalListingUrl` (once reconstruction is gone it returns its argument — a wrapper that invites the
     branch back). Keep the label helpers. `CATEGORY_SUB_LABELS` stays for display only.
   - Rewire: `priceHistory.ts` (drop the parent-derived triple; `url: s.source_url`, and
     `listing.source_url` in the pre-attach fallback — this is where A4 dies); `ListingDetail.tsx`
     prefill (`currentSource?.source_url ?? listing.source_url`); `ComparableModal.tsx`
     (`listing.source_url`); `Watchdog.tsx` (`dispatch.source_url`); `BrokerDetail.tsx` (wrap the Portál
     cell in an `<a>` when non-null; fix the false comment); `LocationQuality.tsx` needs no change.
     `NewEstimationModal.tsx:291-292` are input placeholders, not links — left alone deliberately.
   - **Delete `frontend/src/lib/portals.test.ts`** (both describes cover deleted functions; one asserts
     the 404). Replace with a **no-reconstruction census test**: a vitest case walking `src/lib`,
     `src/pages`, `src/components` that fails if any file outside an explicit allowlist
     (`NewEstimationModal.tsx`) contains `sreality.cz/detail`. Same idiom as the repo's SQL-placeholder
     and measure-registry census tests.
   - Acceptance case for the whole sprint: a property merged from two sreality rows of different
     sub-category — both header chips resolve. Read-only browser check on production after deploy.
   - `roadmap/ui-track.md` entry.

### W3 — the rails (two small PRs + one migration), only after the W1 write pass

1. **Coverage** — both of these, one line each, deliberately: migration `495` appends
   `('source_url', l.source_url IS NOT NULL)` to `data_quality_by_source` (authored from migration 318,
   its only definition; the existing 6-hourly `capture-data-quality` pg_cron job then snapshots a
   per-portal series for free), **and** `check_outbound_url_coverage` in `scripts/verify_pipeline.py`
   thresholded on the **absolute** per-source count of active rows with NULL `source_url` (warn 50 /
   fail 500 via `app_settings.pipeline_check_thresholds`) — a new sreality code silently producing NULLs
   would never move a percentage against ~800k rows, but it moves an absolute count within a day.
2. **Parity** (`outbound_url_parity`) — the reconciler in `--dry-run --report-check`, weekly, in its own
   workflow step: rows whose stored URL disagrees with today's derivation, plus the unknown-code
   histogram. ~3 minutes, zero network, and the **only** detector that reaches the inactive archive.
3. **Conformance** (`outbound_url_conformance`) — `scripts/verify_outbound_urls.py` + a weekly workflow
   (never inside `verify_pipeline`'s wall-clock budget). HEAD only, no redirects followed, no body read,
   ≤2 req/s, **active rows only** (a delisted sreality page 404s at any URL). Stratified **rotating**
   sample keyed on the ISO week over the sorted codebook (~12 codes × 3 rows + 1 row per crawler portal
   ≈ 44 requests, full codebook every 4 weeks). Threshold on **concentration**, not rate: fail when any
   `(category_main, cb)` cell with ≥3 samples is ≥2/3 non-conforming (a slug defect fails a whole cell
   together; a scattered 404 is a delisting rule #3 has not caught up with) — warn above 5% overall.
   Labels in `frontend/src/lib/pipelineChecks.ts`; alerting rides `emit_transition_alerts`.
4. **Separate 4-line PR (one purpose):** `frontend/src/lib/api.ts` `SUPPORTED_SOURCES` + `types.ts`
   `SourceKind` gain `ceskereality` and `realitymix` to match `scraper/source_dispatcher._KIND_SUFFIXES`
   (§8 — a live wrong-copy bug, not the unification).

## 5. Rollout order and gates

| Step | Action | Gate |
|---|---|---|
| 0 | Three cheap catalog facts the operator runs (§7 first bullet) | before any SQL is written |
| 1 | Merge **W0** | CI green; Railway commit status green for API + realtime-worker |
| 2 | After one drain cycle: HEAD-probe ~10 freshly drained sreality rows; confirm a listing's snapshot timeline shows no `source_url` field-change | live evidence, not CI alone |
| 3 | Merge **W1**; dispatch `write=false`; read counters together | `unknown_cb_with_recent_last_seen == 0`; `distinct_urls == written`; conformance sample 200/301 only |
| 4 | Dispatch `write=true` in a window clear of the hourly MF recompute | counters match the dry run |
| 5 | **Apply migration 494**, verify in the catalog | applied ≠ merged — before step 6 |
| 6 | Merge **W2** (SPA); read-only production check on the merged-property acceptance case | chips, comparable modal, Watchdog, BrokerDetail |
| 7 | Merge **W3**; apply 495; first weekly conformance run green; merge the drift PR | — |

## 6. Decisions ledger (the panel's disagreements, resolved)

| Question | Decision | Why |
|---|---|---|
| Preserve-if-null vs always-write on `source_url` | **Preserve-if-null**, with the parser emitting on every fetch | It is the repo's existing rail for "an incoming NULL never erases"; a NULL here can only mean "derivation failed", never "URL removed"; refetches still upgrade a backfilled value because non-null wins; the eraser is the explicit reconciler |
| Locality in the backfill: derive from columns vs store an `x` placeholder | **Derive; canonical string or nothing** | A placeholder is a second shape of the same fact; inactive rows are never refetched so it would be permanent for most of the cohort; the only cost is a counted `locality_null` cohort |
| Is `raw_json` read anywhere | **No** | Every 404-critical segment is a typed column; the payload route is ~14 GB and shape-dependent |
| Coverage metric location | **Both** the view row and the pipeline check | The view is a free trend; the check is the alarm, and only an absolute count can see a new code |
| Does `portalListingUrl()` survive | **Deleted** | The census test is the real rail; a pass-through function invites the fallback back |
| Weekly live HEAD probe | **Yes, own workflow** | ~44 HEAD requests a week against a portal we call thousands of times an hour; concentration thresholds avoid false reds; an unexpected 3xx-to-login is itself a finding |
| Clearing default | **Off**; explicit `--clear` after the number is read | The first live pass must never reduce coverage |
| Reconciler cadence | Fill once on dispatch; weekly `--dry-run --report-check` | The eraser never fires on a schedule; only the detector does |
| Where the sreality vocabulary lives | `scraper/sreality_url.py` only | The contract YAML is under a governed hash and read by no code |
| Index on `source_url` | **Measure, then decide** (own PR) | `EXPLAIN (ANALYZE, BUFFERS)` on `_match_listing_by_url` after the write pass; index only above ~200 ms, `CREATE INDEX CONCURRENTLY … WHERE source_url IS NOT NULL`, which cannot run inside a transaction block |
| CLAUDE.md | One clause on rule #21 (recommended); a new #23 is the operator's alternative | The file is at 293 of a hard 300-line cap |

## 7. Decisions that are genuinely the operator's

1. **Three catalog facts to read before W0's PR is opened** (none touch `raw_json`; MCP session):
   `select pg_get_viewdef('listings_public')` (confirm it matches migration 425);
   `select tgname from pg_trigger where tgrelid = 'listings'::regclass and not tgisinternal`;
   and the sizing/shape counts:
   ```sql
   select count(*) filter (where is_active) as active, count(*) as total,
          count(*) filter (where category_sub_cb is null) as no_sub_cb,
          count(*) filter (where category_type is null or category_main is null) as no_category,
          count(*) filter (where locality is null) as no_locality,
          count(*) filter (where locality like '%, %') as legacy_locality,
          count(*) filter (where source_url is not null) as already_urled
   from listings where source = 'sreality';
   select count(*) filter (where input_url like '%sreality.cz/detail/%/x/%') as x_form,
          count(*) filter (where input_url like '%sreality.cz%?%') as with_query,
          count(*) filter (where input_url like '%sreality.cz%') as sreality_total
   from estimation_runs;
   ```
2. **Do inactive sreality rows get URLs?** Recommendation: **yes** — the URL is the record of where the
   listing lived; sreality's own 404 page tells the operator it is gone, and the chip already renders
   the inactive dot. It changes the run size and what "coverage" means, so it is your call.
3. **May a non-canonical placeholder ever enter `listings.source_url`?** Recommendation: **never**
   (canonical string or NULL). Revisit only if the `locality_null` count from the dry run is material.
4. **A recurring outbound HEAD probe against sreality.cz, weekly, ~44 requests.** Recommendation: yes.
5. **The pasted-URL estimation path may change branch** for sreality URLs once rows are matchable
   (reuse-scraper-attributes instead of the API parse). Recommendation: accept — it is the branch the
   other eight portals already take; the build session states in the W1 PR body which branch wins.
6. **CLAUDE.md:** rule-21 clause vs a new rule #23.
7. **The write-pass window** (clear of the hourly MF recompute) and whether `--clear` may ever run
   unattended (recommendation: dispatch-only, after the `would_clear` number is read).
8. **Ship the `api.ts` SourceKind drift fix now** as its own PR (recommendation: yes, same week).

## 8. Explicitly out of scope (and why)

- **Inbound URL→portal classification** has four registries (`chrome-extension/src/portals.ts`,
  `scraper/source_dispatcher._KIND_SUFFIXES`, `scraper/url_parser.py`, `frontend/src/lib/api.ts`) and the
  fourth has already drifted (missing `ceskereality`, `realitymix`). That is a different contract
  (parse, not build). The **drift** ships as a 4-line PR (W3.4); the **unification** is filed, not started.
- **The 8 crawler `detail_url()` builders and 7 extension id-extractors** stay where rule #21 puts them.
- **`canonical_url()` normalisation of pasted URLs** in the estimation entry, and the two-things-named-
  `source_url` in the pasted-URL envelope (`estimation_runs.input_url` = what was pasted;
  `listings.source_url` = the fact). Documented, not changed.
- **No new columns on `listings`** (no `source_url_derived_at`, no rule version): a pure derived value's
  provenance is reproducible from stored columns in ~3 minutes; an `ALTER TABLE` on this table has a
  documented history of lost lock races.
- **No `raw_json` re-parse, no snapshot-grain URL history, no legacy-fixture restoration** (nothing reads
  the legacy shape after this design; `git show 1ea1838a^:tests/fixtures/sample_listing.json` remains the
  only copy if that ever changes).
- **bezrealitky's `detail_url(uri)` can produce `…/None`** — a pre-existing, separate bug; the new
  non-empty contract guard does not catch it and is not meant to.
- **`contracts/portals/*.yaml`** is not touched: its `url_template` rows are prose read by no code, and the files sit under a governed hash.

## 9. Documentation that changes with the code (same PR, CI warns otherwise)

| PR | Doc |
|---|---|
| W0 | `docs/architecture.md` § Data sources (sreality) + rule 21 clause; `CLAUDE.md` rule 21 clause; `.claude/skills/scraper-ops` (same section, net zero lines); `roadmap/scraper-track.md` |
| W1 | `frontend/src/lib/workflowDocs.generated.ts` (codegen); `.claude/skills/scraper-ops` only if the reconciler earns a dispatch line (trim to fit) |
| W2 | `roadmap/ui-track.md`; `.claude/skills/database` only if the view-append pattern gains a line (at cap — trim) |
| W3 | `frontend/src/lib/pipelineChecks.ts` labels; `roadmap/reliability-track.md` or `scraper-track.md` (one file) |
| any | `CLAUDE.md` per the operator's ruling (§7.6) |

## Appendix A — the sreality sub-category codebook (48 pairs, sreality-sourced)

| main | cb → slug |
|---|---|
| byt | 2 `1+kk` · 3 `1+1` · 4 `2+kk` · 5 `2+1` · 6 `3+kk` · 7 `3+1` · 8 `4+kk` · 9 `4+1` · 10 `5+kk` · 11 `5+1` · 12 `6-a-vice` · 16 `atypicky` · 47 `pokoj` |
| dum | 33 `chata` · 35 `pamatka` · 37 `rodinny` · 39 `vila` · 40 `na-klic` · 43 `chalupa` · 44 `zemedelska-usedlost` · 54 `vicegeneracni-dum` |
| komercni | 25 `kancelare` · 26 `sklad` · 27 `vyrobni-prostor` · 28 `obchodni-prostor` · 29 `ubytovani` · 30 `restaurace` · 31 `zemedelsky` · 32 `ostatni-komercni-prostory` · 38 `cinzovni-dum` · 49 `virtualni-kancelar` · 56 `ordinace` · 57 `apartman` |
| ostatni | 34 `garaz` · 36 `jine-nemovitosti` · 50 `vinny-sklep` · 51 `pudni-prostor` · 52 `garazove-stani` · 53 `mobilni-domek` |
| pozemek | 18 `komercni` · 19 `bydleni` · 20 `pole` · 21 `les` · 22 `louka` · 23 `zahrada` · 24 `ostatni-pozemky` · 46 `rybnik` · 48 `sady-vinice` |

Type: 1 `prodej` · 2 `pronajem` · 3 `drazby` · 4 `podily`. Main: 1 `byt` · 2 `dum` · 3 `pozemek` ·
4 `komercni` · 5 `ostatni`. Codes 1, 13, 14, 15, 17, 41, 42, 45, 55 are rejected by sreality's API today
(HTTP 422) — a historic row carrying one declines and is counted; a code whose `category_main` disagrees
with this table is a reused code and blocks the write.

## Appendix B — how this was established

- Sitemap: `https://www.sreality.cz/sitemap.xml` → 12 gzipped parts; 107,766 detail URLs; 140 distinct
  `(type, main, sub)` triples; one id per `(main, sub)` → `/api/v1/estates/{id}` → `category_sub_cb.value`.
- Locality rule: 60 + 110 live listings across all mains/types; derived URL HEAD-probed; exact HTTP 200
  on every row whose sub slug was in the codebook; the placeholder `x` 301s to canonical on 110/110.
- Slug-vs-label mismatches were found by probing the label-derived URL for each code (404) and the
  sitemap slug (301/200).
- Legacy payload shape: `git show 1ea1838a^:tests/fixtures/sample_listing.json` (`seo.locality`,
  `seo.category_*_cb`, `_links.self.href = /cs/v2/estates/{id}`).
- Write-path claim: the exact W0 edit was applied to a scratch copy and the full offline suite run —
  1 failed (the test that pins the deleted statement), 7,472 passed.
- Live counts through the SPA's own public read surface with the smoke-test reader
  (`Prefer: count=planned`): sreality ≈ 231,941 rows; other portals ≈ 593,081.
