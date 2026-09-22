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
  Zero extra statements on the ingest path. *(W6 correction: the preserving set was short by one producer, and
  `derived` needed naming. The split is whether the PARSE has an opinion. `structured` and `derived` cells are its
  verdict — the portal stopped stating the key, the breadcrumb stopped yielding it — so both still clear. `text` and
  `none` are silence: the ingest grammar speaks only when the prose does, and nothing in the parse ever looks at a
  `none` cell. `none` is not optional here — the measured wipe this ruling exists to close, 2,949 of 24,621
  `condition` fills, is on bazos, where the contract declares `condition` producer `none`, not `text`; a text-only
  rule would have left it growing. Evidence the contract is right about `none`: over active rows, every `none` cell
  on the eight structured portals is 0-filled (bezrealitky 2, mmreality 3, ceskereality 11, idnes 1, maxima 6,
  realitymix 6, remax 5 cells, all zero), so the rule is a no-op for the parsers and protects only post-publication
  producers. `area_basis` is the ONE cell that does not follow its own producer: it is `derived` everywhere, but
  `scraper.area.derive_headline_area` stamps it on the number it just picked and returns `(None, None)` with it, so
  it follows `area_m2`'s decision — otherwise a preserved bazos parcel figure (14,901 active rows read `'plot'`)
  would keep its number and lose the marker that stops it reading as a usable area. Residual, accepted: the ingest
  grammar can no longer CLEAR a `text` cell it stops matching (bazos `area_m2` 42,364, `disposition` 18,843, `floor`
  14,562, `total_floors` 1,158 active rows) — the same trade already accepted for `published_at` / `source_url` —
  and it has **no automated remedy today**: `scripts/reparse.py` (R9) re-derives and CORRECTS such a cell but never
  blanks one by design, so removing a stale preserved value is a hand-written UPDATE until something is built for it.
  The finer rule the brief asked about (freeze W7's fill, still let a bazos regex clear its own cell) needs per-row
  provenance, which R3 forbids; and adding it as a contract axis would re-open exactly this wipe, because R3 has the
  text lane fill only NULLs — a cleared regex cell IS where W7 writes, and the next refetch would wipe that fill.)*
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
  computed once in the filter layer). `has_parking` = a space or right **belonging to** the property. ~~The statutory
  PENB placeholder "G" is a distinct `unassessed` member where the portal marks it.~~ *(W5 correction: measured over
  all nine censuses, **no portal marks it**. Every one spells its G as the ordinary class label — "G - Mimořádně
  nehospodárná" on sreality / ceskereality / realitymix / mmreality, idnes's decree citation "G (vyhl. č. 78/2013
  Sb.)", a bare letter on maxima / remax / bezrealitky — the same shape it gives A–F. There is nothing to split on,
  so `energy_rating` stays A–G and the 67 % G share stays what W1 made it: a measure-validity fact the fill matrix
  reports.)* The on-demand URL parser is kept
  and brought onto the generated vocabulary. `LLM_DAILY_COST_WARN_USD` moves to $15 with the lane.
  *(W5 correction: ownership `jine` is the last clause's first real customer. The brief called it "semantically
  absent → NULL"; it is the portals' own "other" bucket — idnes states it three ways ("jiné", "s.r.o.",
  "podílové"), mmreality as "Jiné", bezrealitky as "OSTATNI" — and four of the five used to drop it while realitymix
  stored it off-canon. NULLing a stated fact is what the ruling forbids, so it is a canonical member on all nine and
  the per-portal override is gone.)*
- **R12 — Autodedup is another program's territory.** This program fixes upstream and hands over in writing; it never
  edits `autodedup/` or `docs/design/autodedup/`. Owed hand-overs: the false "zero area" sentences (E12, :241, :1046);
  the stale `PLOT_TRUNCATING_SOURCES` guard (the truncation was fixed by `scraper/area.py` + healed); the W8 floor
  conversion table and predicate **before** W8 merges — DELIVERED as
  `docs/design/field-capture/handover-autodedup-floor.md` (their `l0_floor_tolerance`, `floor_stated_conflict`, the
  guards' `band` arm and the persisted `floor_lo`/`floor_hi`/`floor_checked` pair state all depend on it).

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
| **W6 — SHIPPED** | Close the wipe (R4); property rollup stops letting a lower-trust `true` beat a higher-trust `false`; fills reach Browse in minutes | the `bool_or` special case; the per-listing re-render of the upsert statement | 114 / −38 across three files (most of it the WHY comments) + 156 test lines | W1 |
| **W7** | The text lane on the realtime worker; bake-off; per-field gates | duplicate tool enums; the old cache key | ~170 | W0, W2, W3, W6 |
| **W8 — SHIPPED (heal pending)** | Floor: ground = 0 everywhere — the convention as contract data; six portals' parsers converted; the SPA names the convention | 3 per-parser floor readers + 3 regexes, maxima's int-returning split, 4 inline FE expressions | the `convention` axis, `floor_from_portal`, `fmtFloor`, one verify_pipeline check | W2, W3, R12 hand-over |
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
active rows above it. *(W5 correction: none of that sentence survives W5. `known_values`, `LEGACY_COLLAPSES` and
the legacy tier are DELETED — `CANON` is the whole value space and the schema's enums are generated from it, for
five fields now including `disposition` and `price_unit`; `DISPOSITION_OPTIONS` runs 1+kk…9+1. The two
`count(distinct …)` numbers above were W2's, held by mapping each off-canon spelling to itself; W5 widens the canon
instead, so they hold because the values ARE members.)* The nine DB-resident prompts are NOT reachable from CI and
stay W7's (§7) — **except** `llm_parse_system_prompt`, which W5's migration 550 corrects, because giving
`price_unit` an enum turned that prompt's retired spelling from stale prose into a contradiction the provider
enforces.

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
makes a terrace-only listing a balcony match, and none drops a stated loggia. **That measure is met by the parsers
from now on and only PARTLY by the heal — see "what the seam can and cannot land" below.**

**Measured over the WHOLE active stock, not a newest-N window (W1's own rule; a newest-N window rotates with cohort
mix and reads systematically high, because older rows carry fewer keys — the first cut of these numbers was taken on
the newest 4,000/5,000/6,000/8,000 and every one of them was biased up).** Re-measured 2026-09-22:

| portal | cell | before | after (expected) |
| --- | --- | --- | --- |
| ceskereality (48,579) | `has_parking` | 0 % | 10,219 true (21.0 %) / 3,105 false (6.4 %) |
| ceskereality | `garage` | 0 % | 5,436 true / 7,888 false |
| ceskereality | `terrace` | 0 % | 3,259 true / 4,927 false |
| ceskereality | `has_balcony` | 5,800 true | 5,801 true / 2,385 false (a `Terasa`-only cell is now a stated false) |
| remax (9,086) | `parking_lots` | 0 of 9,086 | the key is on 1,575 (17.3 %) |
| remax | `has_parking` | 1,375 true — byte-identical to `garage` | `garaz` (1,375) ∪ a count row (1,575) |
| realitymix (48,763) | `has_lift` | 0 % | 1,296 true / 3,396 false (the `ostatní` multi-select, on 4,692 rows) |
| realitymix | `garage` | 1,680 true / 0 false | 1,680 true / 3,012 false |
| realitymix | `has_parking` | 3,254 true / 0 false | 3,254 true / 1,438 false |
| realitymix | `cellar` | 0 % | the key is on 6,982 (14.3 %) |
| realitymix | `garden_area` | 0 of 48,763 | the key is on 3,027 (6.2 %); ~40 % of those say "ano" with no measure and stay NULL |
| realitymix | `parking_lots` | 0 % | the key is on 224 (0.46 %) |
| mmreality (10,317) | `has_balcony` | 0/0 | the two booleans are on 2,309 (22.4 %): 1,292 true / 1,017 false |
| mmreality | `terrace` | 0/0 | `terraceArea` on 612 (5.9 %) |
| mmreality | `furnished` | 0 % | `equipment` on 5,878 (57.0 %) |
| mmreality | `has_parking` | 7,573 true (73.4 %) / 0 false | 5,557 true (53.9 %) / 2,361 false / 2,399 unknown |
| idnes (111,418) | `total_floors` on houses | NULL on 29,906 of 29,913 | 24,134 of them carry `počet podlaží` |
| idnes | `has_balcony` | 28,404 true | 17,845 of those are terrace-only and go to unknown |
| maxima (272) | `has_balcony` | 30 true | 69 (the `lodžie` key nothing read is on 39, every one of them loggia-only) |
| maxima | `furnished` | 0 of 272 | `vybavení` on 65 (23.9 %) |
| bezrealitky (5,696) | `has_balcony` | 1,418 true | 396 of those are terrace-only and go to unknown |
| bezrealitky | `price_czk` | 31 EUR amounts stored as CZK | refused (NULL) + a counted event |
| sreality (105,083) | `has_balcony` | 16,766 true | ~3,200 of them to false (19.1 % of true rows in the newest-8,000 sample; the whole-stock scan times out) |

"Parkety" is in the group `Podlahy` and "Parkoviště poblíž" (582 live rows) / "Parkování na ulici" (4,672) are named
exclusions, so none of the three can match mmreality's `has_parking` any more.

**What the seam can and cannot land (R9's never-blank rule, measured).** `scripts/reparse._merged` keeps the stored
value whenever the re-derive yields None, and there is no flag that overrides it — by design, so a parse that lost a
key cannot erase a column. So the heal lands every NULL→value and every true→false move, and **none of the
true→unknown ones**:

* Healable now: ceskereality (all four cells, 0 %→real), remax, realitymix, idnes `total_floors`, maxima,
  mmreality (2,015 of its 2,018 `has_parking` true rows move to an explicit false; 3 would go to unknown),
  sreality `has_balcony` (388 of 2,029 true rows in the newest-8,000 sample move to false, 0 to unknown).
* NOT reachable by the seam: **idnes `has_balcony` 17,845 rows**, **bezrealitky `has_balcony` 396 rows**, and
  **bezrealitky `price_czk` 31 EUR rows**. Those values change on each listing's next successful detail fetch
  (`upsert_listing` has no COALESCE for them), so active rows converge within a cadence or two and **inactive rows
  never do**. Until then `has_balcony` carries two definitions on those two portals, with no marker distinguishing
  which. Moving them now would need an explicit blank-allowed path in `reparse.py` — a later wave's call, not a
  silent expectation of this one.

**Watchdog safety proven with SQL before any fill:** a first-time attribute fill cannot mint a `:new:` dispatch —
see § W4a.

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

**W5 — met in code; the stored-row half is the operator's dispatch.** Unmapped-value rate = 0 for condition,
building_type, ownership, price_unit, disposition (gate A3, over every value the nine censuses record);
`count(distinct price_unit)` = 2 in the canon **and** on every row the heal reaches; building_type `jina` 8,223
active rows retained as a member; impossible dispositions refused and counted (`vocabulary.disposition` takes the
portal and raises a `disposition/{portal}/{pair}` event), never silent.

**What the canon gained, and what it cost.** 14,068 active rows become reachable by a Browse condition option they
were invisible to (`ve_vystavbe` 7,859 + `ve_vystavbe_(hruba_stavba)` 1,291, `projekt` 2,457, `spatny` 1,175,
`udrzovany` 675, `v_rekonstrukci` 553, `urceny_k_demolici` 58); 8,351 by a building_type option (`jina` 8,223,
`modularni` 99, `ocelova` 14, `roubena` 7, the two comma-joined ceskereality cells 7, bazos's `smisana` 1); 710 by a
disposition option (6+kk 361, 6+1 210, 7+1 56, 7+kk 47, 8+1 25, 8+kk 6, 9+1 4, 9+kk 1); 73 rows by the new
`jine` ownership option. *(Counts taken 2026-09-22 and re-measured the same day; hourly churn moves each by a few
rows — 14,044 / 8,351 / 708 / 73 on the second read. Only `smisana` changed in kind, below.)* **Only four of those need a stored-row heal** — the rest were already stored under the value
the canon now names. The four are the collapses: condition `ve_vystavbe_(hruba_stavba)` → `ve_vystavbe`
(realitymix 1,157 active / 383 inactive, remax 134 / 53) and `urceny_k_demolici` → `k_demolici` (realitymix 44 / 34,
remax 14 / 7), both through the W3 seam on the page substrate; building_type `zdena, kamenna` / `drevena, zdena` →
`smisena` (ceskereality 7 / 2 — two materials IS mixed construction, and that portal offers no "smíšená" option,
which is why it states the pair); and `price_unit` → `za nemovitost` / `za mesic` (sreality 103,841 active,
bezrealitky 5,696 active). **bazos's `smisana` is 0 active / 5 inactive** (re-measured 2026-09-22; an earlier read
the same day found one active row, since delisted), so there is nothing for a heal to reach: that portal's
`building_type` producer is `none` (the value came from the removed LLM lane), a re-derive yields None and
never-blank keeps it. No one-off `UPDATE` is proposed — five inactive rows stay as documented dead data, and the
8,351 building_type delta does not depend on them.

**Snapshot budget, which is what chose the `price_unit` spelling.** The eight non-sreality portals hash the PARSED
fields and `price_unit` is one of them, so each changed active row defers one snapshot to its next detail fetch.
Collapsing onto sreality's `celkem`/`měsíc` would have moved the seven text-priced portals — 278,844 active rows,
~28 days of the whole platform's snapshot budget. Collapsing onto their `za nemovitost`/`za mesic` moves sreality
(103,841 rows, which hash the RAW payload and so defer NOTHING — the recorded permanent column/history divergence)
and bezrealitky (5,696). **5,696 deferred snapshots against 278,844: a 49x difference for the same two facts.** The
slug is therefore not display Czech, so `PRICE_UNIT_OPTIONS` carries the label the SPA renders after the price
("celkem", "za měsíc") and the three render sites read it — which also un-regresses the two portals whose own
spelling was already the Czech word.

**The impossible dispositions stay stored (R9) and are listed for the operator.** 28 values, 230 active / 589
inactive rows, all bazos but one inactive idnes row: `0+1` 8/12, `0+2` 1/1, `1+0` 15/55, `1+2` 10/18, `1+3` 3/11,
`1+4` 2/9, `1+5` 0/1, `1+6` 1/1, `2+0` 9/39, `2+2` 1/3, `2+3` 0/2, `3+0` 0/1, `3+2` 3/5, `4+0` 1/1, `4+2` 25/45,
`4+3` 2/3, `5+2` 43/109, `5+5` 3/4, `6+2` 52/138, `6+3` 2/10, `6+7` 1/0, `7+2` 18/52, `8+2` 18/35, `8+3` 0/2,
`8+7` 3/3, `9+2` 6/21, `9+3` 2/3, `9+5` 1/5. The brief said "~60 rows"; it is 819. The grammar refuses them from now
on, so no NEW one can be written — but the stored ones do not clear: a re-derive yields None, and `disposition`
being `text` on bazos, R4's rule PRESERVES the stored value rather than clearing it. They persist until a heal
blanks them, which never-blank forbids: **the honest state is "stored, unreachable, counted, and listed here".**

**Two things the canon change reaches OUTSIDE the parsers, both shipped as migrations.** Widening a canon moves the
`__unknown__` predicate, and rule 16 says that predicate has one definition: `ownership` gaining `jine` made the
Browse LIST stop counting those 73 active rows as unknown while `browse_stats_properties` and `browse_map_cells`
kept their hardcoded three-member array — **migration 549** recreates both from 547's byte-identical bodies with
one literal one element longer. And giving `price_unit` its first real JSON enum turned a stale line in the
DB-resident `llm_parse_system_prompt` into a contradiction the provider enforces (it still instructed
"měsíc"/"celkem") — **migration 550** rewrites that block, and the disposition list beside it, with a guard that
raises rather than no-op if the operator has reworded either.

**The heal dispatch, in order.** `dry_run=true` first on each line; the real write needs `dry_run=false`,
`confirm=APPLY` and, because all three columns are in the content hash, `allow_snapshot_deferral=true`:

    gh workflow run reparse.yml -f source=realitymix   -f fields=condition     -f dry_run=true
    gh workflow run reparse.yml -f source=remax        -f fields=condition     -f dry_run=true
    gh workflow run reparse.yml -f source=ceskereality -f fields=building_type -f dry_run=true
    gh workflow run reparse.yml -f source=sreality     -f fields=price_unit    -f dry_run=true
    gh workflow run reparse.yml -f source=bezrealitky  -f fields=price_unit    -f dry_run=true

**Then re-bless, and that step is not optional.** `data/field_capture/fill_baseline.json` ships hand-re-scored, not
regenerated: the `off_canon` shares were rewritten to what the widened canon makes true TODAY, and the per-value
`values` maps were left as they were, so they under-count every newly-canonical member (idnes/condition reads
`filled: 72787` against a `values` sum of 67,363 — the 5,424 `ve_vystavbe`/`projekt`/`udrzovany`/`v_rekonstrukci`/
`spatny` rows a real re-bless would name). Worse, `sreality/price_unit` and `bezrealitky/price_unit` are blessed at
`off_canon: 1.0` — the honest pre-heal state, and also the CEILING, and `compare_to_baseline` fires only on a RISE.
Until the re-bless those two cells cannot fail, whatever drifts in them. So `python -m scraper.field_census --bless`
is **step 3 of this dispatch**, run once the five reparse batches complete, and the allowance is time-boxed to that
window rather than permanent.

**W6 — met offline; two gates are post-merge by nature.** The preserved-cell rule is contract-driven and rendered per
source in `tests/scraper/test_listing_write_preserve.py`: for all nine portals every `text`/`none` cell is
`COALESCE(EXCLUDED.c, listings.c)` and every `structured`/`derived` cell is `= EXCLUDED.c`, `published_at` /
`source_url` unchanged, `description` (not a contract cell) still clears. Both write paths are proven to carry the
identical fragment — the per-item statement for each portal and, for sreality, `_BATCH_UPSERT_SQL` too. Two further
rails: a fifth producer cannot silently fall through to "clears"; `area_m2` and `area_basis` render the SAME clause on
every source; and a source outside the contract keeps the pre-R4 rule — which is now more than a fallback nobody
checks, because the contract's portal keys are pinned to `scraper.portal._DEFAULTS`, the per-portal config fleet.
**Property rollup:** the `bool_or` special case is deleted; the six amenity booleans take the same trust-ordered
best-non-null as every scalar (ended on `id` so the pick is total — `bool_or` was order-independent and this is not).
Its stated reason (recover a fact from the sibling that parsed it) survives — that rule
skips NULLs too — so the two differ only on a true-vs-false disagreement. Measured over the 23,641 active multi-child
properties: has_parking 1,384 flips, cellar 486, has_balcony 212, garage 162, terrace 140, has_lift 64, all
one-directional true→false. The recompute writes the whole table, not only the Browse-visible part, so the **blast
radius over all 74,090 multi-child properties** is has_parking 4,095, cellar 1,388, has_balcony 682, terrace 410,
has_lift 406, garage 357 — same direction; the surplus is delisted properties Browse hides and comparables never
reads (it filters `listings`, not `properties`). **Honest reading of those flips:** only a minority are the "inferred `true`" this ruling
names — 1,027 of the 1,384 parking flips are an idnes-STATED true losing to a sreality-STATED false, which is the
declared `source_trust` policy rather than a provenance fix. The provenance fix is the same change seen forward: when
W7's lane fills bazos booleans at 50k-row scale, presence-wins would hand every one of them a veto over sreality's
stated false. **Seen-to-Browse baseline (2026-09-21, 94 succeeded rebuilds in 24 h):** mean 11.7 min from
properties-row-ready to visible in `browse_list`, best case 2.3, worst 36.6, p90-of-worst 22.0, rebuild duration
4.1 min average. The patch is a fast path, not a guarantee, so the expected shape is **bimodal, not a flat ~2 min**:
a rebuild snapshots `browse_projection` at its start and renames the new table in at its end, so a patch that commits
inside that window is superseded without erroring — over 72 h, 283 succeeded rebuilds, mean 237 s / p50 200 / p90 358
/ max 1,295 against a 900 s cadence, i.e. **in flight ~26 % of wall-clock**. Post-merge: the same seen-to-Browse
measure, expected ≈ 3 in 4 changes on the maintenance lane's cadence and the rest unchanged; and the wiped-cell
count on `condition` (today 2,949) stops growing after one refetch cycle.

**W7.** `OPENAI_API_KEY` (and the RunPod route) verified on the worker before merge — no lane on that worker has ever
made an LLM call. Lane visible in `worker_heartbeats`. Bake-off: all candidates on the same labelled panel in ONE run;
per-field precision, cost/1k, p50 latency recorded in the PR. Oldest eligible-unextracted age < 24 h after the backlog
drains; `eligible > 0 AND claimed = 0` never persists two passes. p99 extraction ≤ 20 min from `first_seen_at`
(the watchdog lookback reads the same constant). Re-bill closed: no `(listing_id, description-hash)` extracted twice
per extractor version. Pre-call budget guard binds before spend.

**W8 — SHIPPED in code; the GATE and the heal are post-merge by nature.** Sibling-pair gate (one SQL, no
labels), now registered as `verify_pipeline`'s `floor_convention` check — the first floor check of any kind:
mean(portal_floor − idnes_floor) over unique (price_czk, area_m2, disposition) active byt keys, within ± 0.35 of 0
for every portal, fail at ± 0.50, min 40 pairs. The warn tier is 0.35, not the planned 0.25: ceskereality never
states the ground storey (0 of its 34,350 floored rows read 0), so its sample is conditioned on `floor ≥ 1` and
sits at +0.20 — 0.25 left a CORRECT portal 0.05 from an amber it could reach in an ordinary week. Measured
27.0 s / 35.1 s on two EXPLAIN (ANALYZE) runs over 34,801 pairs — the lane's most expensive check by some way
(a Bitmap Heap Scan that spills, ~103k buffers almost all `read`, so it never stays cached), which is why it is
registered LAST among the DB checks and runs under the per-check `statement_timeout`: cancelled on a bad day it
reports `warn / timed out`, which says UNKNOWN, never a false green. Baseline re-measured 2026-09-22 (mean, and
the share at exactly +1): sreality +0.97 / 94.0 % (n=13,574), realitymix +0.96 / 92.4 % (7,416), remax +1.05 /
86.8 % (1,248), mmreality +0.99 / 98.6 % (1,574), bezrealitky +0.87 / 73.0 % (900), maxima +0.82 / 85.7 % (49);
ceskereality already +0.20 (85.6 % same, n=7,453) and **must not be converted**, bazos +0.10. The residual the six
should land on after the heal is those two portals' own reading, not 0 — a price/area/disposition match is a
sibling signal, not a proven duplicate. **The check therefore reads RED from merge until all six heal passes have
run**; that is the gate working, not a regression.

Conversion = idempotent re-derive through `scripts/reparse.py --fields floor`, `floor ≥ 1` only; re-measured
2026-09-22 the six portals hold **183,745 rows that move (63,328 active)**, and **4,707 rows at 0** (sreality 4,597,
mmreality 108, maxima 2) plus **900 below 0** stay put. `is_plausible_floor` tightened to `total_floors − 1`.
`listing_snapshots`/day stays < 12,500 for 30 days (baseline 10,090; **25,330** deferred snapshots on the five
hashed portals — sreality's 124,263 rows churn none, it hashes the raw payload). Hand-over to autodedup delivered
first (R12): `docs/design/field-capture/handover-autodedup-floor.md`.

**Three things the wave found that the plan did not have.** (i) The convention needed a THIRD member, not two:
idnes states the scale in the VALUE ("2. patro (3. NP)"), so its cell declares `word` and the Czech grammar reads
it — `ground0`/`ground1` alone would have forced a choice between two numbers one storey apart on the one portal
that never had the bug. (ii) `total_floors` does NOT convert — it is a podlaží count including the ground storey on
every portal — but the bazos free-text miner's two **patra-worded** total cues ("z celkových 10 pater", "6patrový")
count storeys ABOVE the ground one, so they now read `n + 1`; without that the tightened guard would have refused
correct floors. 28 of 1,158 active bazos rows with a total carry such a cue. (iii) bezrealitky uses **0 as its
numeric "not specified" sentinel** (it has never emitted a floor=0 row), so its call site reads the value through
`_int` before the converter — under ground = 0 a bare 0 would otherwise have become the ground storey.
`is_plausible_floor`'s only other caller named in the design, `toolkit/bazos_enrichment.py`, no longer exists: W0
deleted it, so the tightening reaches exactly one live producer, the bazos miner.

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
