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
   fixtures: ceskereality reads `vybavení` / `počet podlaží` (never emitted; the live key is `vybavení pronájem`) and
   ignores `parkování` (~27 % of pages); remax reads `balkon` / `lodzie` (never emitted) and ignores
   `pocet parkovacich mist`; realitymix never sets lift/cellar; idnes drops `total_floors` on 29.7k houses; mmreality
   `has_parking` (73.5 % true) matches "Parkety" (parquet flooring) and street parking.
4. **There is no single canonical vocabulary**: 13 live `condition` spellings and 15 `building_type` values against
   filter lists of 6 and 8 → ~14k + ~8.3k active rows unreachable by any Browse filter. 33 near-identical normaliser
   functions, 18 mapping dicts, 9 disposition regexes. The consumer-side canon already exists in
   `toolkit/filter_registry.py` (it generates the SPA + API schema under a CI gate).
5. **`listings.floor` is a ~50/50 mixed column**: ground = 0 on idnes, bazos, ceskereality; ground = 1 on sreality,
   realitymix, mmreality, remax, bezrealitky, maxima (sibling-pair proof, §5 W8).
6. **Nothing measures per-portal × per-field fill or validity**; the `data_quality_snapshots` pg_cron capture has
   failed since 2026-09-15 (no `statement_timeout` prefix) and no check reads it.
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
- **R3 — ONE producer per (portal, field)**: `structured | text | none`. No precedence rule, no per-row provenance
  column — provenance IS the contract row. A `structured` cell also declares its source-key order, its **absence
  semantics** (missing key ⇒ `false` | `unknown`) and its **default sentinels** (values read as absent).
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
- **R9 — ONE re-parse seam** reaches stored rows from a declared substrate per portal (`raw_json`; `raw_json` +
  `description` for bazos; the stored detail page for portals whose `raw_json` is insufficient). It absorbs
  `scripts/reextract.py` and reuses `scripts/backfill_support.py`. Every heal obeys: never bulk-write
  `listing_snapshots`; never blank a value a re-derive cannot produce; never touch `last_seen_at` (rule #4); enqueue
  `dirty_properties` in the same statement (rule #20); idempotent re-derive, never arithmetic (`floor = floor - 1`).
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

## 3. What this program never does

- Puts an LLM, a cache probe, or any per-row statement between a sighting and publication.
- Adds a flag, an `app_settings` key, an env var, or a column.
- Blanks a stated value, writes history in bulk, or lets a heal bump `last_seen_at`.
- Touches autodedup code/docs, or lets an attribute edit move a location contract's governed hash.

## 4. Waves at a glance

| Wave | Goal | Deletes | Adds | Depends |
| --- | --- | --- | --- | --- |
| **W0** | Stop the dead lane pretending | ~2,380 LOC: 4 scripts, 2 workflows, 3 test files, 2 empty tables | ~5 LOC + this doc | — |
| **W1** | Measurement before change: per-portal key census + fill **and validity** matrix | the dead `data_quality_snapshots` capture | ~700 incl. census JSON | — |
| **W2** | Vocabulary module + contract table + CI gates — identity-preserving | ~1,000 LOC (33 fns, 18 dicts, key chains, planted tests, 6 dead reads) | ~750 | W1 |
| **W3** | The one re-parse seam | ~3,800 LOC (6 backfill scripts, 6 workflows, 4 tests) | ~950 | W1 |
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

**W1.** 9/9 portals have a census with `generated_at`; staleness > 30 d is itself a failure. The matrix reproduces the
known zeros (remax `has_balcony` 0/0, mmreality `has_balcony` 0/0, ceskereality parking/garage/terrace/parking_lots 0
and `total_floors` 0, realitymix `has_lift` 0) and the validity half flags mmreality `has_parking`. Fill is computed
from `listings` columns (sampled — the full-table form does not return in 90 s), never from the enrichment ledger
(which still records wiped cells as filled). No active `capture-data-quality` cron row.

**W2.** Gate A1 (no dead read) fails on the 6 seeded dead reads, passes after removal. Gate A2 (no unread emission
≥ 5 %) — every such key is mapped or on an explicit `ignored:` list with a reason. Gate A3 (no unmapped value).
**Identity proof:** `count(distinct condition)` = 13 and `count(distinct building_type)` = 15 unchanged; per-cell fill
unchanged ± 0.1 pp. Prompt/tool-schema drift check = 0.

**W3.** Dry-run over ≥ 1,000 rows per portal twice → second pass `changed = 0`. A re-derive yielding None cannot
overwrite a stored value (unit test). `count(*) listing_snapshots` identical across a 10,000-row heal; rows whose
`last_seen_at` moved = 0; every changed row is in `dirty_properties`. Substrate proof: an idnes `has_lift` re-derived
correctly from the stored page where `raw_json` carries the key with a null value.

**W4.** Every `structured` cell > 0 %. Zero active rows with `terrace = true` and `has_balcony` not true under the new
definition (today 2,471 + 572 + 37). mmreality `has_parking` falls to the group-qualified rate; a 300-row audit shows
0 matches on "Parkety" / "Parkoviště poblíž" / "Parkování na ulici". ceskereality `has_parking` rises from 0 %.
**Watchdog safety proven with SQL before any fill:** a first-time attribute fill cannot mint a `:new:` dispatch.

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

**W9.** Each item its own PR: browse_list cadence docs (it is `*/15`, two docs say 5 min); Browse Stats/Map
`estate_area` → `plot_area_m2` + the five filters `buildBrowseStatsArgs` never sends (rule #16); maxima coords read
the pin, not the map view-centre (location contract process: version bump, goldens, no partial claim set);
bezrealitky `ruianId` rung — feed it or delete it, decided on evidence in the PR; `location_data/payloads.py`
"NOT WIRED" docstring (825k rows live); branch protection on `main` (PR + CI check, no review requirement) — last,
announced first, because it changes how every parallel session merges.

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
- A fill-rate move > 5 pp on any portal changes cohort predicates and dedup features fitted under the old missingness
  — each such wave publishes its delta to the autodedup program (R12) and re-runs a fixed comparables basket.
