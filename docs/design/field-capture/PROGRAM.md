# FIELD CAPTURE — every typed listing fact, captured properly, on all nine portals

**Program document. This file is the program's source of truth.**
Status: plan approved by the operator 2026-09-21; W0 in flight.
Rulings R1–R12 below are **binding** — they supersede any design text, code comment or docstring that differs.

---

## 0. North star

> **Every typed listing fact has ONE declared producer per portal, ONE canonical vocabulary, and a MEASURED fill
> rate and accuracy — stated facts parsed at ingest, prose-only facts extracted after publication — and nothing
> ever sits between a sighting and the row being visible.**

Every wave is tested against this sentence. Work that does not serve it is cut (§6), not folded in.

**Subtraction is the deliverable.** Every change removes at least as much as it adds — fewer fields, variants,
special cases, paths. No flags, no new settings, no new env vars. Program estimate: ≈ −7,260 / +2,860 LOC, −30 files,
−2 tables, −8 workflows, 0 new columns. The two waves that do not subtract lines on their own (W1 measurement, W2
vocabulary) subtract concepts and are the instruments every other wave is judged by.

## 1. Why this program exists (verified 2026-09-21)

The trigger was a false sentence: *"Bazos has no area on any advert, and the engine does not punish it."* Its origin
is `docs/design/autodedup/PROGRAM.md` (E12, :241, :1046 — "zero area on all 138,997 rows"). Live: bazos `area_m2` is
present on 84.1 % of active rows (shared grammar `scraper/area.py` over title + description) and agrees within 5 % with
a structured sibling on 97.3 % of cross-portal pairs (n=183). An LLM could add ~3 pp (only 3 % of area-less bazos rows
mention "m²"). The investigation (26 agents, critic-checked) found what is actually wrong:

1. **The LLM description-enrichment lane has been dead since 2026-07-23**, green the whole time. Its selector guards
   `sreality_id IS NOT NULL`; listing-identity Gate 2 makes every new non-sreality row NULL there → 223 of ~50k active
   bazos rows reachable. `_starved_lanes` needs `attempts > 0`, so a lane that selects nothing never rings.
2. **Every detail re-fetch rewrites every `LISTING_COLUMNS` member from the parser's output**
   (`scraper/db.py` `_listing_update_set_sql`; only `published_at` / `source_url` are preserved) → 12–14 % of every
   LLM-filled cell was wiped, permanently (the cache key never re-attempts). The enricher's bare `UPDATE` also skipped
   the snapshot rule and `dirty_properties`, and ~$65 of ~$207 was spent re-extracting on price-only snapshot churn.
   Its accuracy was never measured: floor ≈ 73 %, has_lift precision 92.9 %, `false` written from silence.
3. **Structured portals drop facts they publish** — parser key mismatches certified by tests over hand-authored
   fixtures: ceskereality reads `vybavení` / `počet podlaží` (never emitted; W4 measured the nearest live key,
   `vybavení pronájem`, and it is an APPLIANCE list — "Kuchyňská linka, Myčka" — not the ano/ne/castecne state, so
   that column stays a genuine portal gap) and
   ignores `parkování` (~27 % of pages); remax reads `balkon` / `lodzie` (never emitted) and ignores
   `pocet parkovacich mist`; realitymix never sets lift/cellar; idnes drops `total_floors` on 29.7k houses; mmreality
   `has_parking` (73.5 % true) matches "Parkety" (parquet flooring) and street parking.
4. **There is no single canonical vocabulary**: 13 live `condition` spellings and 15 `building_type` values against
   filter lists of 6 and 8 → ~14k + ~8.3k active rows unreachable by any Browse filter. 33 near-identical normaliser
   functions, 18 mapping dicts, 9 disposition regexes. The consumer-side canon already exists in
   `toolkit/filter_registry.py` (it generates the SPA + API schema under a CI gate).
5. **`listings.floor` is a ~50/50 mixed column**: ground = 0 on idnes, bazos, ceskereality; ground = 1 on sreality,
   realitymix, mmreality, remax, bezrealitky, maxima (sibling-pair proof, §5 W8).
6. **Nothing measures per-portal × per-field fill or validity.** The `data_quality_snapshots` pg_cron capture is
   FLAKY, not dead (31 of 40 runs cancelled in ten days — its command carries no `statement_timeout` prefix), it
   cannot see a wrong value, and no `verify_pipeline` check reads it — though the Health page does, through
   `scraper_health_checks_mv`.
7. The real area defect is **measure ambiguity**, not coverage: first-m²-token semantics store the parcel as the
   headline on ~1 in 6 bazos houses; `area_basis` is NULL on ~half the corpus and unbackfillable by design.

## 2. Rulings (binding)

- **R1 — Attributes stay `listings` columns written at ingest.** No claims store, no per-field side table, no new
  columns for prose-only facts. Every consumer (Browse, Stats, Map, watchdog, comparables, estimation, autodedup,
  extension) is column-driven and stays unchanged. A rule-14-style separate column costs ~90 files per field.
- **R2 — The attribute contract is a Python table in `scraper/`**, never a section of `contracts/portals/*.yaml`.
  The location contract is location-by-construction (closed 11-member `CLAIM_TYPES`, mandatory `obec_name`), its
  governed sha would turn one label edit into a re-mine of 10.9M location claims (PR #1209 precedent: 14 dead intake
  runs), and `location_data` imports `scraper`, not the reverse. Never add a seventh top-level key to those YAML files.
- **R3 — ONE producer per (portal, field)**: `structured | text | derived | none`. No precedence rule, no
  per-row provenance column — provenance IS the contract row. A `structured` cell also declares its source-key
  order, its **absence semantics** (missing key ⇒ `false` | `unknown`) and its **default sentinels** (values read
  as absent). *(W4 correction: no cell declares `false` any more. bezrealitky was the only one that did, and its
  census refutes it — `parking` and `garage` are on 100 % of adverts, so the "missing key" was a JSON null, which is
  the API saying "not stated". The axis stays as the declaration slot the next such portal needs.)* *(W2 correction: the three-value set was short by one. Declaring all 9 × 26 cells found 36 that are
  written from something that is not a payload attribute key at all — `category_main` from a URL segment or a
  breadcrumb, `subtype` from an SEO title, `price_unit` restated from `category_type` on seven portals,
  `area_basis` stamped by `scraper.area`. Calling those `structured` would have named keys that do not exist and
  failed gate A1; calling them `none` would have declared a 100 %-filled column empty. `derived` is the honest
  fourth member and is not a producer W7's text lane may ever write.)*
  Within a `text` cell the ingest-time grammar (regex) writes first; the post-publication lane fills **only NULLs**.
  The LLM never overwrites a regex value unless a labelled panel proves it better for that cell.
- **R4 — The wipe is closed inside the one shared SET builder** (`_listing_update_set_sql`), driven by the contract:
  a parser NULL preserves the stored value for `text` cells of that source; `structured` cells still clear normally.
  Zero extra statements on the ingest path.
- **R5 — The canon stays in `toolkit/filter_registry.py`.** `scraper/vocabulary.py` holds only the producer side:
  diacritic fold, ONE `(field, portal_label) → canonical` registry, ONE disposition grammar, ONE boolean helper.
  An unmapped label is NULL + a counted event, never a passthrough enum. LLM tool schemas and the DB-resident prompts
  are **generated from / CI-diffed against** it, with real JSON `enum` arrays.
- **R6 — Derived cells never mint a snapshot.** A text cell is a pure function of `description` (a hashed column);
  the extraction cache is keyed `(listing_id, description-hash, extractor_version)` — never `snapshot_id`, so a
  price-only change neither re-bills nor loses an extraction. No derived writer ever touches `raw_json`.
- **R7 — A field may be written by the text lane only after a labelled panel shows ≥ 95 % precision** (floor:
  ≥ 95 % within ±1 — operator ruling: populate even if off by one). `false` is written only from an explicit negation
  in the evidence quote ("bez výtahu"), never from silence. Floor is returned as the advert's own words and converted
  by `scraper/floor.py`, never by the model.
- **R8 — The lane and its health check share ONE eligibility function.** The check is oldest-eligible-unextracted age
  + waiting count per source. "Selects nothing while green" must be impossible.
- **R9 — ONE re-parse seam** reaches stored rows from a declared substrate per portal. **Built (W3,
  `scripts/reparse.py`): TWO substrates, not three.** `portal_raw_pages.html` for all seven HTML portals — bazos and
  mmreality included, against the plan's guess of `raw_json` for them, because both stage a detail body at 100 %
  coverage and neither exposes a public entry point that takes its `raw_json` alone (bazos stores no ad body there;
  mmreality's object→`ScrapedListing` construction lives inside `parse_detail`) — and `listings.raw_json` for sreality
  and bezrealitky, which stage no body at all. It absorbs `scripts/reextract.py`'s registry, deferral gate and hash
  assertion (`reextract.py` keeps only the two NON-column recoveries: `images` child rows and the `raw_json.broker`
  block) and reuses `scripts/backfill_support.py`. Every heal obeys: never write `listing_snapshots`; never blank a
  value a re-derive cannot produce; never touch `last_seen_at` (rule #4); enqueue `dirty_properties` in the same
  statement (rule #20); idempotent re-derive, never arithmetic (`floor = floor - 1`); and write a row only while it
  still holds the value the pass read (compare-and-set per named column — the seam leaves no snapshot and no
  `last_seen_at` behind, so reverting a concurrent detail write would be invisible).
  **Two measured limits the waves that heal must plan around.** (i) *The deferred snapshot is not universal.* On the
  eight portals hashing the PARSED fields the healed row's next detail fetch appends the one genuine snapshot; on
  **sreality** the hash is the RAW payload (`scraper.hashing.content_hash`, `scraper/main.py`), which a column heal
  never touches, so NO snapshot is ever appended and column and history diverge permanently — the asymmetry
  `docs/architecture.md` already records for the W17 land heal's 44,237 sreality rows. `--allow-snapshot-deferral`
  states whichever consequence the chosen portal will actually have. (ii) *The sreality raw_json arm does not reach
  that portal's oldest rows.* `parse_listing` needs `hash_id`/`id`; rows stored before the client unwrapped the estate
  object hold the wrapped response and carry neither, so they raise. Measured 2026-09-21: 234/1,000 rows at id ≤ 1,000
  carry a usable key, 609/1,001 at id ≈ 30k, 597/1,001 at id ≈ 60k, 1,001/1,001 from id ≈ 90k up. The run counts them
  as `parse_errors` and WARNs with the share, so a heal cannot exit clean over a population it never touched.
- **R10 — Text-lane scope: bazos first; expand by evidence** — another (portal, field) only when the census shows the
  portal never states it AND a panel passes R7. **Model: cost/benefit bake-off in ONE run** — gpt-5.6-luna vs
  open-source models served on RunPod (Gemma 4, Qwen 3 72B-class, or better candidates) via the existing `oss`
  provider; the cheapest model that passes the gates wins; `app_settings.enrichment_model` is the one switch.
  No Anthropic models.
- **R11 — Vocabulary defaults (operator-accepted):** add every real value as a canonical member (`ve_vystavbe`,
  `projekt`, `spatny`, `udrzovany`, `v_rekonstrukci`, building_type `jina`, dispositions above 5+1); collapse only
  true synonyms; never NULL a stated fact. `has_balcony` = balcony OR loggia (terrace has its own column; any union is
  computed once in the filter layer). `has_parking` = a space or right **belonging to** the property. The statutory
  PENB placeholder "G" is a distinct `unassessed` member where the portal marks it. The on-demand URL parser is kept
  and brought onto the generated vocabulary. `LLM_DAILY_COST_WARN_USD` moves to $15 with the lane.
- **R12 — Autodedup is another program's territory.** This program fixes upstream and hands over in writing; it never
  edits `autodedup/` or `docs/design/autodedup/`. Owed hand-overs: the false "zero area" sentences (E12, :241, :1046);
  the stale `PLOT_TRUNCATING_SOURCES` guard (the truncation was fixed by `scraper/area.py` + healed); the W8 floor
  conversion table and predicate **before** W8 merges (their `l0_floor_tolerance` and fitted weights depend on it).

**Approved destructive steps (operator OK 2026-09-21, each with a backup + before/after counts):** (i) drop the two
0-row batch tables; (ii) re-key `listing_description_enrichments`; (iii) clear the OLD lane's cells that failed
measurement (LLM-written floor, silence-based `false`) — only cells that lane wrote, never a portal-stated value.

**Awaiting the operator's word (NOT in migration 546, which carries only the two approved DROPs):** delete the retired
`llm_liveness` check's 1,162 `pipeline_check_results` rows (122 fails, oldest 2026-07-10, newest 2026-09-21 17:00Z) —
and, as the same decision, the six keys the removed dedup engine left frozen since 2026-08-06 (`geo_debt`,
`merge_latency`, `eligibility_funnel`, `engine_health`, `street_debt`, `merge_precision_sample`). Until then the Health
page shows those checks frozen (`pipeline_checks_public` serves the latest row per key with no recency filter). Backup =
a `\copy` of the rows. The matching bell incident (`sys:llm_liveness:onset:2026-09-21T10:38:34Z`) can never receive a
recovery row and is marked seen by hand — `notification_dispatches` is append-only (rule #16).

## 3. What this program never does

- Puts an LLM, a cache probe, or any per-row statement between a sighting and publication.
- Adds a flag, an `app_settings` key, an env var, or a column.
- Blanks a stated value, writes history in bulk, or lets a heal bump `last_seen_at`.
- Touches autodedup code/docs, or lets an attribute edit move a location contract's governed hash.

## 4. Waves at a glance

| Wave | Goal | Deletes | Adds | Depends |
| --- | --- | --- | --- | --- |
| **W0** | Stop the dead lane pretending | ~2,380 LOC: 4 scripts, 2 workflows, 3 test files, 2 empty tables | ~5 LOC + this doc | — |
| **W1** | Measurement before change: per-portal key census + fill **and validity** matrix; the flaky data-quality capture REPAIRED (the Health page reads it) | — (the one wave that only adds: it is the instrument) | ~2,200 incl. census + baseline JSON | — |
| **W2** | Vocabulary module + contract table + CI gates — identity-preserving | 52 per-parser fns, 19 dicts, 13 regexes, the key chains, 11 planted tests, 39 dead reads | one module + one 234-cell table + 3 gates | W1 |
| **W3** | The one re-parse seam — SHIPPED | 2,887 LOC across 11 deleted files (**4** backfill scripts, **4** workflows, **3** tests; two of the six named candidates survive on evidence — see R9) | 1,383 in the four new files; the branch's own total is +1,663 / −3,358 incl. the regenerated workflow-docs asset | W1 |
| **W4** | Close every structured gap the census proves; one `has_balcony` / `has_parking` definition; heal via seam | 3 + 4 rival definitions; dead reads | contract cells | W2, W3 |
| **W5** | Apply vocabulary collapses to stored rows, one counted batch each | spelling variants; `price_unit` 4 → 2 | missing canonical members | W2, W3 |
| **W6** | Close the wipe (R4); property rollup stops letting an inferred `true` beat a stated `false`; fills reach Browse in minutes | the `bool_or` special case | ~15 | W1 |
| **W7** | The text lane on the realtime worker; bake-off; per-field gates | duplicate tool enums; the old cache key | ~170 | W0, W2, W3, W6 |
| **W8** | Floor: ground = 0 everywhere — six portals re-derived; the SPA names the convention | 5 floor regexes, 4 inline FE expressions | ~60 | W2, W3, R12 hand-over |
| **W9** | Patchwork sweep (non-autodedup), one small PR each | stale docs, a dead rung or a dead path | — | — |

## 5. Wave gates (numeric; SQL or CI)

**W0.** CI green; no `enrich_bazos*` workflow; repo grep for the deleted symbols = 0; both batch tables verified 0 rows
immediately before the DROP; per-source non-NULL counts of the 8 formerly-enriched columns unchanged the day after.
`listing_description_enrichments` (37,754 rows) is **kept** — W7 re-keys it.

**W1.** 9/9 portals have a census with `generated_at`; staleness > 30 d is a `verify_pipeline` WARNING, never a CI
failure (a calendar-keyed test reds `main` on a branch that touched nothing). The matrix (`field_fill_matrix`) is
computed over the whole ACTIVE stock in aggregate form (12 s measured; a newest-N window rotates with cohort mix and
flaps) from `listings` columns — never from the enrichment ledger, which still records wiped cells as filled — and is
scored against a blessed baseline: a collapsed cell, a rising off-canon share, or a source missing from the matrix
rings; zero-fill cells and never-false booleans are reported from the LIVE matrix. It reproduces the known zeros
(remax `has_balcony`, mmreality `has_balcony`, ceskereality parking/garage/terrace/parking_lots/`total_floors`,
realitymix `has_lift`) and reports the statutory "G" share per portal. W2 replaces "known in the baseline" with the
contract's declared producer. The `capture-data-quality` job is repaired with a 900 s `statement_timeout`
(migration 548), not retired: the Health page's `field_null_drift` rung reads its series; the split of ownership
between the two instruments is written in that migration's header.

**W2 (SHIPPED).** Gate A1 (no dead read) fails on the seeded dead reads, passes after removal — **26 of them, not
6**: the six the investigation named were the ones a live census had been run against, and declaring all nine
portals' cells against the checked-in census found 26 (ceskereality 9, remax 6, realitymix 5, idnes 4, maxima 2).
A further **13** were outside A1's reach until review, because `areas_from_params` and realitymix's price fallback
kept their own key chains and the contract merely restated them: 39 dead reads in all, and the five area chains now
consume the contract so the gate covers them. Gate A2 (no unread emission ≥ 5 %) — every such key is mapped or on
the explicit `IGNORED` list with a reason (300 entries, and 12 of the 50 `none` cells named the census key W4 wires —
after W4, one does: mmreality `subtype`, which needs a vocabulary ruling, not a wire).
Gate A3 (no unmapped value). **Identity proof:** the characterisation goldens in
`tests/fixtures/field_capture/golden/` — 35 real detail payloads (one of which records a raise), 1,254 label probes,
33 source-key-chain probes — recorded from the parsers BEFORE the module existed and byte-identical after, except
four synthetic chain probes that exercise only deleted dead reads. The label corpus is one probe per (portal, key,
live value) **the census records a value for**, which `field_census.MAX_DISTINCT_FOR_VALUES = 12` caps: a key with
more than 12 distinct values gets no probe, so realitymix/idnes `balkon`/`terasa`, idnes `sklep` and sreality
`advert_name` are characterised by the page corpus alone. `count(distinct condition)` = 13 and
`count(distinct building_type)` = 15 hold by construction: every off-canon live value is an explicit LEGACY registry
entry mapped to itself. The LLM tool schema's enums are generated from `vocabulary.known_values` — canon PLUS the
legacy spellings, because that parser writes the same columns the scrapers do — except `disposition`, which keeps a
described free string: `DISPOSITION_OPTIONS` is the Browse filter pill list, stops at 5+1, and cannot name the 937
active rows above it. The nine DB-resident prompts are NOT reachable from CI and stay W7's (§7).

**W3 — met, with the gates restated as what is actually provable offline.** Idempotence is proven on the stored
substrate itself rather than by two live dry runs: pass one writes what the parse produced, pass two compares the same
parse against it and reports no movement, per portal over its committed fixture. A re-derive yielding None cannot
overwrite a stored value (unit test, on a boolean + a number + an enum at once). `listing_snapshots` and
`last_seen_at` appear nowhere in the seam's executable half — asserted over the module source AND over the built
statement, which needs one precondition the SQL text cannot show: `listings` carries no trigger and no rule (verified
2026-09-21, only RI constraint triggers), so nothing can mint a snapshot behind the statement. **The first wave that
actually heals owes the empirical half**: `count(*) listing_snapshots` identical across its batch and rows whose
`last_seen_at` moved = 0. The `dirty_properties` enqueue is in the SAME CTE as the UPDATE, asserted on the built
statement, which also carries an `IS NOT DISTINCT FROM` compare-and-set per named column. Substrate proof: the seam re-derives `cellar` and `has_balcony` as `true` from a stored idnes page
whose `raw_json['params']` carries both keys with a JSON null. `has_lift` is the same mechanism on a key no committed
fixture carries; live, over the 3,000 newest active idnes byt rows (2026-09-21): 'výtah' present on 1,328, text
non-null on 0, `has_lift` true on 1,328, false on 0. **No production row was healed** — W3 ships the seam, not a heal.

**W4 (SHIPPED, pre-heal).** Every `structured` cell has a producer the census proves; the twelve cells still at 0 %
are the heal's to-do list and `field_fill_matrix` names them (`zero_fill_undeclared`) until it lands.
**The gate as first written was the wrong measure and R11 supersedes it.** "Zero active rows with `terrace = true`
and `has_balcony` not true" is unreachable under `has_balcony = balcony OR loggia`: excluding the terrace arm *raises*
that count, because the three portals that folded a terrace into the flag (sreality, bezrealitky, idnes) stop doing so.
The honest measure is **zero active rows where `has_balcony` disagrees with `balcony OR loggia`** — i.e. no portal
makes a terrace-only listing a balcony match, and none drops a stated loggia. Measured moves, all deliberate:
sreality −4.9 pp of the active corpus from true to false (388 of 8,000 sampled), bezrealitky 395 of 1,417 true rows
back to unknown, idnes ~569, and maxima +14.3 pp of rows gained from the `lodžie` key nothing read.
mmreality `has_parking` falls from 73.5 % true to 59.1 % under the group-qualified reading (2,363 of 4,000 sampled),
with 886 stated `false` where it could previously only say "unknown"; "Parkety" is in the group `Podlahy` and
"Parkoviště poblíž" / "Parkování na ulici" are named exclusions, so none of the three can match. ceskereality
`has_parking` rises from 0 % to ≈ 18.8 % true / 9.5 % false. **Watchdog safety proven with SQL before any fill:** a
first-time attribute fill cannot mint a `:new:` dispatch — see § 5a.

**W4a — watchdog safety, established in code + read-only SQL before any heal (no notification code changed).**
A heal through the W3 seam writes only the named `listings` columns and a `dirty_properties` mark. It cannot mint a
`:new:` dispatch for an existing listing, on three independent grounds:

1. **The `:new:` window is keyed on arrival, not on content.** `api/notifications.match_once` evaluates
   `properties_public` where `first_seen_at > last_matched_first_seen_at` for that subscription, plus a re-scan of
   `first_seen_at <= cursor AND > now() - lookback`, where `lookback = max(60, 2 × notifications_new_requires_image_
   timeout_minutes)` — 60 minutes on today's settings (neither image-gate key is set in `app_settings`, so both
   defaults apply). `properties.first_seen_at` is `min(child.first_seen_at)` (`scripts/recompute_property_stats.py`
   :179) and no heal writes `listings.first_seen_at`, so a property older than the window cannot re-enter it however
   its columns move. 685 of 461k active properties were inside that 60-minute window at the time of measurement.
2. **The dedupe key is once-ever per property.** `wd:{sub}:new:{property_id}`, UNIQUE, `ON CONFLICT DO NOTHING`
   (:1420) — a property already dispatched to a subscription can never fire `new` again.
3. **`:price_drop:` needs a snapshot with a LOWER price.** Its grain is `wd:{sub}:price_drop:{snapshot_id}` over
   `listing_snapshots` rows with `price_czk < lag(price_czk)`. The seam writes no snapshot at all, and the one
   deferred snapshot a healed live row's next detail fetch appends carries the CURRENT price, so it is a drop only
   if the portal actually cut it. The same holds for `collection_monitor`, whose every detector is anchored on
   `monitor_since` over `listing_snapshots.scraped_at`, `first_seen_at` or `inactive_at` — none of which a heal moves.

The residual, stated rather than hidden: a property first seen INSIDE the 60-minute window that did not match a
saved filter before the heal and does match after it fires a genuinely-new dispatch. That is the filter seeing a fact
it should always have seen, bounded by one hour of ingest, and it is the reason the heal is run in one pass rather
than trickled. Live at the time of writing, `notification_subscriptions` has **0 active rows** and one monitored
collection, so the live blast radius of this wave's heal is zero; the mechanism above is what makes it safe when
subscriptions come back.

**W5.** Unmapped-value rate = 0 for condition, building_type, ownership, price_unit, disposition;
`count(distinct price_unit)` = 2; building_type `jina` ≈ 8,203 retained; per-value Browse membership delta published
before each batch; impossible dispositions (0+1, 8+7…) refused and counted, never silent.

**W6.** The preserved-cell rule is contract-driven (unit-tested per source); a synthetic re-fetch with parser NULL
leaves a text cell intact and clears a structured one. After one refetch cycle the wiped-cell count stops growing
(today 2,949 on `condition`). Property rollup: a stated `false` beats an inferred `true`. Seen-to-Browse p50 measured
before/after the `run_incremental_pass → sync_browse_list` change.

**W7.** `OPENAI_API_KEY` (and the RunPod route) verified on the worker before merge — no lane on that worker has ever
made an LLM call. Lane visible in `worker_heartbeats`. Bake-off: all candidates on the same labelled panel in ONE run;
per-field precision, cost/1k, p50 latency recorded in the PR. Oldest eligible-unextracted age < 24 h after the backlog
drains; `eligible > 0 AND claimed = 0` never persists two passes. p99 extraction ≤ 20 min from `first_seen_at`
(the watchdog lookback reads the same constant). Re-bill closed: no `(listing_id, description-hash)` extracted twice
per extractor version. Pre-call budget guard binds before spend.

**W8.** Sibling-pair gate (one SQL, no labels): mean(portal_floor − idnes_floor) within ± 0.25 of 0 for every portal.
Baseline 2026-09-21 — exactly +1 on: sreality 94.0 % (n=13,069), realitymix 92.3 %, mmreality 98.6 %, remax 87.1 %,
bezrealitky 73.1 %, maxima 85.1 % (n=47); ceskereality already 85.4 % same and **must not be converted**. Expected
residual after conversion 5–13 % (27 % on bezrealitky) = the field's own noise floor (ceskereality shows 14.6 %
today). Conversion = idempotent re-derive, `floor ≥ 1` only; 4,700 rows at 0 and 996 below 0 unchanged.
`is_plausible_floor` tightens to `total_floors − 1`. `listing_snapshots`/day stays < 12,500 for 30 days
(baseline 10,090; ~25k deferred snapshots on the five hashed portals). Hand-over to autodedup delivered first (R12).

**W9.** Each item its own PR (status as of 2026-09-21, worked alongside W0):

- ✅ **browse_list cadence docs.** It is `*/15` since migration 413; `toolkit/browse_read_model.py` and the Browse
  list query in `frontend/src/lib/queries.ts` said 5 min. Fixed.
- ✅ **`location_data/payloads.py` "NOT WIRED" docstring.** Live on all nine portals, 826,948 rows, oldest
  2026-05-28, written through `scraper.db.append_payload_if_enabled`. Fixed.
- ✅ **Browse Stats/Map `estate_area` → `plot_area_m2`, and the size filters the SPA never sent.** It is **seven**
  parameters, not five: `estate_area_{min,max}`, `usable_area_{min,max}`, `garden_area_{min,max}`,
  `parking_lots_min`. All seven have existed on both RPCs since migrations 133/439 with their predicates already
  written — dark for want of a caller, so a plot or usable-area bound narrowed the list while Stats and the map
  described the whole cohort. Migration 547 re-points the two estate predicates at `l.plot_area_m2` (the measure
  `min_estate_area` declares, and the one the list's registry dispatcher reads), and the rail in
  `tests/api/test_watchdog_browse_one_measure.py` — which covered `api/notifications.py` and
  `toolkit/comparables.py` but never the RPCs, which is why this survived — now covers them too.
- ⏳ **maxima coords read the pin, not the map view-centre.** **The item as written was wrong about where it
  lives.** `contracts/portals/maxima.yaml` ALREADY reads the drawn feature (`/features/0`,
  `position_branch: portal_pin`) and says so ("never `/center` (the view centre, 9.2 km out on d40031686)"), so no
  contract version bump, no goldens and no re-mine are involved. The live defect is in the SCRAPER:
  `scraper/maxima_parser._resolve_coords` matches `_CENTER_RE` (`"center":[lon,lat]`) and nothing else, and its
  output feeds `street_from_locality(..., require_morphology=True, lat=…, lon=…)` — a street-vs-village
  disambiguation decided by a coordinate that can be kilometres off. Own PR, because the honest fix needs a
  decision the item does not contain: the OpenLayers feature reader lives in `location_data`, and R2 forbids
  `scraper` importing it. **Blast radius, measured 2026-09-21:** narrower than the item implies — `listings`
  has no coordinate column (W4-c dropped `geom`) and `claims_common.COORDINATE_RULES["maxima"]` is
  `geom_column`, so the RESOLVER re-reads the pin from the archived page and never arbitrates the scraper's
  number. What the wrong coordinate decides is that one street-vs-village call plus the `raw_json.coords`
  provenance stamp. The open decision is the LineString branch: `location_data.page_readers.
  _openlayers_geometry` walks to the HALF-LENGTH point, so a naive midpoint in `scraper/` would be a second
  definition of the same point — the PR either ports that walk or returns no hint for a LineString and says why.
  Note for whoever takes it: `tests/scraper/test_maxima_parser.py`'s map fixture is hand-authored and carries
  `center` with NO `features` key at all, so it certifies today's behaviour; re-author it from the real shape in
  `tests/fixtures/location_w2/maxima_detail.html` (`{"center":[…],"zoom":15,"features":[{"type":"Point",…}]}`).
- ⏳ **bezrealitky `ruianId` rung — the evidence says FEED IT.** 2,836 of 5,716 active bezrealitky rows carry a
  non-empty `ruianId`, yet `location_claims` holds exactly **1** `address_point_id` claim from that portal and no
  portal contract declares the type at all — so bind.py's R0 rung ("the prize") has never fired. Of those 2,836
  rows, **732 resolve below `address_point` today** (390 street, 216 cast_obce_or_quarter, 86 obec, 40
  street_segment): the rung would lift each of them to an exact RÚIAN address point. Own PR, because feeding it IS
  the location-contract process (bezrealitky `contract_version` 3 → 4, goldens, a re-mine) that this item wrongly
  attributed to maxima.
- ⏳ **Branch protection on `main`** (PR + CI check, no review requirement) — an operator action on a GitHub
  settings page, not a code change. Last, announced first, because it changes how every parallel session merges.

## 6. Cut from scope (reported, not built)

New columns for facts no column holds today (year built, heating, orientation…); lowering the 15-minute
`browse_list` rebuild itself; re-keying `listing_summaries` / `listing_condition_scores` onto a text hash (right move,
LLM-pipelines track); merging the bazos location-claims LLM call with the attribute call (same text, different store,
different gate — coupling two lifecycles is not a subtraction); unifying the two content-hash implementations
(sreality raw-JSON vs `_HASH_FIELDS`) — a 100k-snapshot churn event that needs its own program.

## 7. Residual risks (accepted, watched)

- The census is a sample: a rare key under the 5 % floor is a blind spot, and a portal renaming a key leaves gate A1
  green against a stale census — hence census staleness is a gate and W1's live matrix is the runtime alarm.
- 34k inactive rows on the five hashed portals never refetch, so their column and their last snapshot disagree on
  `floor` forever — the recorded precedent's accepted asymmetry.
- An always-on paid lane can bill ~$130/day if its selector is mis-scoped; the pre-call budget guard is the only thing
  that binds before the money is spent. A pass that outlives `LANE_PASS_TIMEOUT_SECONDS` keeps billing — contained by
  pass sizing (≤ 1,000 rows), not by a kill.
- Two files will be called "the per-portal contract" (attributes: Python; location: YAML). Distinct names + a refusal
  note at `location_data/contracts.py` `_TOP_LEVEL_KEYS`.
- **The census is the only evidence gate A1 has, and it samples 1,000 rows.** A source key a portal emits on under
  ~0.1 % of pages is invisible to it, so W2 deleted 26 reads on that evidence. A key that turns out to be real
  returns as a re-blessed census diff plus a contract line; the cost of being wrong is a cell that was already
  0-filled staying 0-filled, never a value that moves.
- A fill-rate move > 5 pp on any portal changes cohort predicates and dedup features fitted under the old missingness
  — each such wave publishes its delta to the autodedup program (R12) and re-runs a fixed comparables basket.
