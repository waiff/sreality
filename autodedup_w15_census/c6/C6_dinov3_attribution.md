# C6: DINOv3 labels as the one image-semantics source (operator item 2) and one decision-attribution table per generation (operator item 4)

W0 census, 2026-09-26/27. Read-only. Tree `origin/main` @ `b2454fa1`, worktree
`/home/hejtm/dev/sreality/.claude/worktrees/w15-c6` (branch `census/w15-c6-dinov3-attribution`). Engine = the
live lane's `settings/w31.json` + `models/w6_gold.json`. Every `file:line` below is in that tree unless a
path says otherwise. No DB, no dispatch, no migration, no PR.

Runs read (all w31 + w6_gold):

| run | what | adverts | stored pairs | merge edges | groups |
|---|---|---:|---:|---:|---:|
| g15 | stored score run `36244048665` (trial, carried 1,251 operator must-links) | 5,195 | 17,057 | 7,971 | 1,133 |
| c17 | cohort 17, run LOCALLY from this worktree (`c6/run_w31.py`, 553 s; must-not-link file only, no must-links) | 17,897 | 85,136 | 39,845 | 3,888 |
| c18 | stored `s15/cohort18_runs/w31` (final check only) | 17,422 | 64,965 | 25,237 | 3,754 |

Reconstruction check: every merge / band / veto reason count of g15 recomputed from `pairs.jsonl.gz` equals
`run.json` `reasons` exactly; the only differences are the three reject reasons whose rows sit under the 0.02 store
floor (`auto_reject:numeral_conflict` 603 stored of 6,748; `auto_reject:attr_contradictions` 512 of 12,967; `model`
9,396 of 40,522).

---

## 0. Answers (coordinator lines)

1. **Item 4, definition.** Every stored decision gets exactly ONE cell (rung, family). Rung = the last ladder step
   that set the zone. Family = one of the 7 feature families already in code (`features.py:334` `FAMILY_OF`: ATTR
   PRICE TXT BRK LOC IMG TIME) plus OPERATOR (rulings) and NONE; model decisions attribute over PRESENT features
   only (absent-feature terms = MISSING, E12). Advert grain = the cell of the advert's `joined_via` edge (the
   column already exists, `migrations/528:388-395`; the batch lane fills it, `score_lane.py:365-382`; the LIVE lane
   writes NULL, `incremental_lane.py:704`).
2. **Item 4, store.** The store already holds zone, reason string, certificate, the full present-feature vector,
   evidence jsonb and the model version per pair (`migrations/528:284-301`, `539:227-235`). Missing for an exact
   table: (a) the promotion's corroboration name (not stored), (b) the model's family split (needs the fitted
   weights; SQL cannot compute it without a second copy of the model), (c) `joined_via` on `rt`. Fix: `Decision`
   carries `rung` + `carrier`, computed once in `decide_pair`; store columns `rung`, `carrier` REPLACE the E11
   bitmask `families` (E11 gate measured inert: 0 `evidence_gate` reasons on 16 S14 cohorts, g12-g14, c17, c18)
   and the never-written `applied_merge_group`; the lane writes `joined_via` with the score lane's function; one
   SQL view folds it. Net store columns: +2, -4 (incl. `clusters.evidence_families`), and the dead table
   `autodedup.models` (0 readers, 0 writers).
3. **Item 4, the answer the table gives (share of merge edges carried).** g15: IMG 59.1 %, TXT 39.4 %, ATTR 1.1 %,
   PRICE 0.5 %. c17: TXT 53.9 %, IMG 44.9 %, ATTR 0.9 %, PRICE 0.4 %. c18: IMG 71.8 %, TXT 26.4 %, ATTR 1.2 %,
   PRICE 0.7 %. LOC, BRK and TIME carry 0 merges in all three: they contribute log-odds (T4) but never
   dominate. Photos (K-C + model IMG + promotion-by-photo) and text (K-B + K-R + promotion-by-body) are the two
   families that merge; attributes and price act almost only as blockers (refusal rows).
4. **Item 4, page.** A fold of `/autodedup/progress`: generation picker + the ONE table. Deletes the unrendered
   `/stats` engine block (5 SQL constants + `_engine_stats`), the reason-string parsers `_certificate` /
   `_why_not_merged` / `CERTIFICATE_COUNTS_SQL`, the 7-bit family bitmask in 3 mirrors, the stale
   "shadow mode, merges nothing" copy. Section 1.6.
5. **Item 2, the 11 heads (model v1, `dinov3-b16@768/bf16`, `pos_neg`):** 3 `exterier - fasáda`, 17 `garáž`,
   22 `interier - koupelna`, 25 `interier - kuchyně`, 28 obývací pokoj, 39 `podklad - 3d plán`, 42 `podklad -
   katastrální mapa`, 43 `podklad - letecký snímek s ohraničením subjektu`, 45 property list, 46 `podklad -
   půdorys`, 48 `technické zařízení / místnost`. Labels in backticks are exact (`migrations/457:43-55`,
   `docs/design/new-dedup/PROGRAM.md:1580`); the exact label text of obývací pokoj and property list lives only in
   the DB. Id 39 = 3d plán is verified (`PROGRAM.md:1586`); the other ten id-to-label pairings are inferred from
   the two lists printed in the same order (`:764` ids, `:1031-1033` names), both also alphabetical by label; a DB
   read of `tag_taxonomy` confirms or corrects them.
6. **Item 2, where to score: measured.** ViT-B/16 at 768 px (2,309 tokens) = 589 GFLOP per image; one CPU core of
   this machine (i7-1260P, numpy/OpenBLAS fp32) runs the layer GEMMs at 60-68 GFLOP/s = **9.8 s per image per
   core** (512 px 3.1 s, 224 px 0.56 s). At 55-60k new images/day that is **6.8 cores busy all day**, about
   $136/month at Railway's list vCPU price, plus `onnxruntime` (rule 7) and an exported graph of a licence-gated
   model. A GPU batch (RTX 3090, measured 12.0 img/s end to end incl. download and decode) is 1.4 GPU-h/day,
   about $9/month plus pod start-up (about $23/month hourly). **Recommendation: ONE hourly GPU image job**
   that downloads each new image once and writes the CLIP vector, the DINOv3 head scores (and vector); it
   replaces `clip_tag.yml`. Decision 10's "inline, no hourly job" cannot hold at 768 px: the operator must
   choose between DINOv3 labels and "no hourly job"; it is not both.
7. **Item 2, one tag table:** `image_tag_scores` (`migrations/490`), active model's winner with the consumer
   floor 0.5 (below = other). It replaces `image_clip_tags` for the engine export and lane
   (`export_sql.py:235-245`, `incremental_sql.py:781-794`), the SPA badges (`images_public`, latest
   `migrations/496:79-90`; `listing_cover_public`, `migrations/416:34-43`) and the labeling proposals
   (`scraper/label_proposal_tagger.py`). Blocker: `clip_render_score` (the "vizualizace" badge) lives in
   `image_clip_tags` and v1 has no render head.
8. **Item 2, exposure (why it must be measured, not assumed):** the room-tag features carry 24-25 % of the
   positive log-odds of model merges and are PIVOTAL (removing them drops the pair under the 0.9788 cut) for
   1,089 of 1,942 model merges on g15 (56 %), 4,611 of 7,794 on c17 (59 %), 4,467 of 7,995 on c18 (56 %). The
   tag-derived image facts refuse 33.2 % (g15), 20.0 % (c17), 7.9 % (c18) of all group joins. v1 has no head for
   about 52 % of trial photos (A5 3.1). Expect arm H (switch without refit) to fail the W5 bars and a refit to be
   needed; the order is v2 heads, pilot, refit, confirmation (section 2.7).
9. **Item 2, subtraction.** Stage 1 (labels): about 1,570 lines, 4 workflows (`clip_tag.yml`, `clip_retag.yml`,
   `label_proposal_backfill.yml`, `tag_model.yml` score stage), 1 config file + loader
   (`data/dinov3_config.json`, a second source of the encoder identity the registry row already holds), 1 JSON
   taxonomy (`data/clip_taxonomy.json`), 4 of 8 tag dials; added about 250. Stage 2 (one encoder, only if the
   unrun near-duplicate bake-off says DINOv3 separates duplicates at least as well as CLIP):
   `image_clip_embeddings` (31 GB) and the CLIP half of the job. D901 is superseded by the operator's
   2026-09-26 ruling.

---

## 1. Item 4: one table per generation of the families responsible for decisions

### 1.1 Definition (exact)

**Rung** (the ladder step that set the final zone; read in code order `decide.py:667-755`, then the lane's hold
`incremental.py:1256-1270`, then grouping `cluster.py:214-325`):

| rung | zone | reason shape today | carrier family |
|---|---|---|---|
| wall | veto | never stored (blocking drops it, `blocking.py:230`) | ATTR (deal, kind, area, disposition, floor) |
| unit-name veto (E61) | veto | `guard:unit_designator_conflict` | TXT |
| auto-reject | reject | `auto_reject:attr_contradictions` / `:numeral_conflict` | ATTR / TXT |
| certificate K-R / K-B / K-C | merge | `certificate:K-x` | TXT (order code) / TXT (contained body re-post) / IMG (same shoot) |
| model | merge / band / reject | `model` | merge: argmax of the POSITIVE family log-odds; band/reject: argmin (the family that pulled it down) |
| fact gate (D43) | band | `<base>:d43_gate:<fact>` | the first fact's family (reader table below) |
| promotion (D43+D50) | merge | `d43_promote:agree:2` / `:photo` / `:unit:<x>` | the (B) unit-grade corroboration `demonstrate.corroboration_warrant` names (`demonstrate.py:627-662`): photo IMG, body TXT, code TXT, twin TXT, pin LOC, price_path PRICE; warrant photo / unit:photos IMG, unit:code / unit:body TXT |
| promotion refused (D50) | band | `model:d43_demonstrate:A:<fact>` / `:B` | A: the fact's family; B: NONE (no unit-grade evidence) |
| hold (F3, lane only) | band | `evidence_pending` (+ `held_*` in evidence) | IMG |
| group refused (E132/E157) | none (merge edge not applied) | `cluster_conflicts.invariant` | the violating pair's first fact family, E157 price check PRICE, `must_not_link` OPERATOR, `area_spread` ATTR |
| must-link (E910) | group | no pair row | OPERATOR |
| never compared / walled / under the store floor | none | no pair row | NONE |

**Model family split.** `LogisticModel.contributions` (`model.py:450-467`) is an exact additive decomposition of
the log-odds: intercept + sum of (weight x standardised value) + presence terms + 8 interaction terms. A family's
sum = its features' terms; an interaction is split half/half between its two features. This is the SAME
decomposition the Pair page's `top_features` already shows (`api/routes/autodedup.py:545-578`), so the table and
the pair drill-down cannot disagree.

**E12 choice, measured.** An ABSENT feature enters the model as value 0 standardised, `(0 - mean) / scale`, so
missingness pays: `dispo_equal` has weight -0.606, mean 0.712, scale 0.453, presence weight -0.274, which gives
+0.95 log-odds when disposition is unknown on a side against -0.66 when both state the same disposition. Counting
absent terms in their family, ATTR "carries" 218 of 1,942 g15 model merges and 969 of 7,794 c17 ones; with absent
terms moved to MISSING, ATTR carries 84 (g15) and 357 (c17): **61-63 % of the ATTR-carried model merges were
carried by an attribute nobody stated.** Absent terms average 6.0 % of the positive log-odds of model merges
(`c6/missing_share.py`). The definition therefore attributes over PRESENT features; MISSING is reported, never a
carrier. (The negative `dispo_equal` weight is a refit finding, not C6's to fix: the E5 disposition wall removes
almost every disposition-differing pair from training, so the slot mostly measures missingness.)

**Fact-reader families** (64 readers, `indistinguishable.py:2591-3109`, one map, `c6/attribution.py` `FACT_FAMILY`):
ATTR = category, every area / extent / accessory / plot reader, disposition, unit count, every storey reader, use
and product readers; PRICE = price, charge, accessory_price, priced_row; LOC = obec (3 readers), street (2),
orientation, stored and printed house number, parcel; TXT = every unit designator / code / label reader, agency
codes, body_align; IMG = interior, floorplan. No code today maps a reader to a family.

**Advert grain.** An advert in a group of 2 or more takes the cell of its best in-group incident merge edge under
`cluster.edge_rank` (`cluster.py:103-112`: certificate first, score descending, ids) = exactly `score_lane.joined_via`
(`score_lane.py:365-382`); a member with no such edge is must-link / OPERATOR. An advert in no group takes, in
order: group refused, best band pair, best stored reject / veto, else never compared / walled / under the floor.
For joined rows the family is what CARRIED the join; for alone rows it is what STOPPED it.

### 1.2 What the store holds per decision today

| need | stored? | where |
|---|---|---|
| zone, reason string | yes | `autodedup.pairs.zone`, `.decision` (`migrations/528:284-301`) |
| certificate | yes, as a column | `pairs.certificate` (`539:227-235`); yet the API still parses it out of the reason string, and its docstring says the column does not exist (`api/routes/autodedup.py:458-465`); `CERTIFICATE_COUNTS_SQL` parses too (`ui_sql.py:1170-1177`) |
| feature vector | yes, present features only | `pairs.features` jsonb via `score_lane.present_features` |
| model version | yes, per row | `pairs.model_version` |
| E11 families | yes, 7-bit smallint (PRICE / TIME bits never set) | `pairs.families`; mirrors `score_lane.py:96-104`, `api/routes/autodedup.py:246-254`, `frontend/src/components/autodedup/EvidenceChips.tsx:21-29` |
| gate facts, demonstration refusal, promotion's band reason, hold | yes, in evidence jsonb | `d43_gate_facts`, `d43_demonstrate`, `d43_banded_as`, `held_*` (`decide.py:560-588`, `incremental.py:1265-1267`) |
| the promotion's (B) corroboration | **no** | recomputable only with both adverts |
| the model's family split | **no** | needs the weights; `autodedup.models` (the table meant for weights, `528:499-512`) has 0 readers and 0 writers |
| the refusing fact of a conflict | **no** | `cluster_conflicts` keeps `invariant` + `detail` (`528:402-413`), not the fact |
| the advert's attaching edge | batch yes, **rt no** | `cluster_members.joined_via_lo/hi` (`528:388-395`); `incremental_lane.py:704` writes NULL |
| the population (adverts that passed through) | rt yes (`rt_fp`, `539:81-96`); batch only as a count (`runs` stats) | |

### 1.3 What one column / one view needs, and the recommendation

- **View only, no DDL:** impossible for model rows (needs the weights) and promotions (corroboration not stored).
- **Evidence-jsonb only:** `decide_pair` writes `evidence.carrier`; a view parses the rung out of `decision`.
  Works, but hides a first-class classification inside jsonb and keeps reason-string parsing (the thing
  `_certificate` and `CERTIFICATE_COUNTS_SQL` already do twice).
- **Recommended:** `Decision` gains `rung: str` and `carrier: str`, set in ONE place (`decide_pair` for the ladder,
  the lane's hold rewrite for `hold`); the three lanes (harness, score lane, rt lane) already call `decide_pair`,
  so one code path. Store: `pairs.rung`, `pairs.carrier` REPLACE `pairs.families` (inert E11) and
  `pairs.applied_merge_group` (never written: `score_sql.py:25`, `score_lane.py:24`); `clusters.evidence_families`
  goes (derived from the edges). The lane writes `joined_via` with `score_lane.joined_via` (deletes the NULL
  write). ONE view `autodedup.generation_attribution(generation, outcome, rung, carrier, n_adverts, n_pairs)`:
  joined adverts via `cluster_members` x `pairs` on `joined_via`; alone adverts via best stored pair; the
  "never compared" row = population (rt: `rt_fp`; batch: `runs` n_listings) minus covered adverts. Cost: one
  grouped scan per generation on the `(generation, ...)` indexes (`538:193-200`); c17 is 85,136 rows.

### 1.4 The prototype tables (script `c6/attribution.py` + `c6/summary.py`)

Operator table, advert grain, ladder order; joined rows = family that carried the join, alone rows = family that
stopped it.

**g15 (trial): 5,195 adverts, 3,914 grouped**

| outcome (rung that decided) | IMG | TXT | ATTR | PRICE | LOC | BRK | TIME | OPERATOR | NONE | adverts | share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| joined: operator ruling (must-link) | - | - | - | - | - | - | - | 9 | - | 9 | 0.2% |
| joined: certificate (K-B) | - | 382 | - | - | - | - | - | - | - | 382 | 7.4% |
| joined: certificate (K-C) | 1,603 | - | - | - | - | - | - | - | - | 1,603 | 30.9% |
| joined: certificate (K-R) | - | 170 | - | - | - | - | - | - | - | 170 | 3.3% |
| joined: model score >= cut | 786 | 348 | 41 | 28 | - | - | - | - | - | 1,203 | 23.2% |
| joined: fact-layer promotion | 413 | 134 | - | - | - | - | - | - | - | 547 | 10.5% |
| alone: merge edge refused by the group check | 4 | - | 17 | 21 | - | - | - | - | 1 | 43 | 0.8% |
| alone: merge demoted by a stated fact (gate) | - | 8 | 25 | - | 2 | - | - | - | - | 35 | 0.7% |
| alone: promotion refused (demonstration A/B) | - | 3 | 26 | 38 | - | - | - | - | 37 | 104 | 2.0% |
| alone: model band | 57 | 67 | 27 | 7 | 4 | 2 | 9 | - | 3 | 176 | 3.4% |
| alone: rejected / vetoed | 86 | 277 | 58 | 7 | 1 | - | 3 | - | - | 432 | 8.3% |
| alone: never compared, walled, or under the store floor | - | - | - | - | - | - | - | - | 491 | 491 | 9.5% |
| **all adverts** | 2,949 | 1,389 | 194 | 101 | 7 | 2 | 12 | 9 | 532 | 5,195 | 100% |
| **share of joined adverts** | 71.6% | 26.4% | 1.0% | 0.7% | - | - | - | 0.2% | - | 3,914 | |
| **share of merge edges** | 59.1% | 39.4% | 1.1% | 0.5% | - | - | - | - | - | 7,971 edges | |

**c17 (unseen): 17,897 adverts, 14,301 grouped**

| outcome (rung that decided) | IMG | TXT | ATTR | PRICE | LOC | BRK | TIME | OPERATOR | NONE | adverts | share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| joined: certificate (K-B) | - | 1,830 | - | - | - | - | - | - | - | 1,830 | 10.2% |
| joined: certificate (K-C) | 4,816 | - | - | - | - | - | - | - | - | 4,816 | 26.9% |
| joined: certificate (K-R) | - | 1,193 | - | - | - | - | - | - | - | 1,193 | 6.7% |
| joined: model score >= cut | 2,904 | 1,121 | 133 | 92 | - | - | - | - | - | 4,250 | 23.7% |
| joined: fact-layer promotion | 1,529 | 683 | - | - | - | - | - | - | - | 2,212 | 12.4% |
| alone: merge edge refused by the group check | 18 | 3 | 65 | 64 | 4 | - | - | - | 3 | 157 | 0.9% |
| alone: merge demoted by a stated fact (gate) | - | 10 | 36 | 4 | - | - | - | - | - | 50 | 0.3% |
| alone: promotion refused (demonstration A/B) | - | - | 67 | 179 | - | - | - | - | 58 | 304 | 1.7% |
| alone: model band | 151 | 189 | 52 | 16 | 10 | 17 | 43 | - | - | 478 | 2.7% |
| alone: rejected / vetoed | 375 | 825 | 88 | 28 | 1 | 2 | 27 | - | - | 1,346 | 7.5% |
| alone: never compared, walled, or under the store floor | - | - | - | - | - | - | - | - | 1,261 | 1,261 | 7.0% |
| **all adverts** | 9,793 | 5,854 | 441 | 383 | 15 | 19 | 70 | 0 | 1,322 | 17,897 | 100% |
| **share of joined adverts** | 64.7% | 33.8% | 0.9% | 0.6% | - | - | - | - | - | 14,301 | |
| **share of merge edges** | 44.9% | 53.9% | 0.9% | 0.4% | - | - | - | - | - | 39,845 edges | |

**c18 (final check): 17,422 adverts, 13,566 grouped** (full table `c6/c18/operator_table.md`): joined via K-C
4,727, model 4,564 (IMG 3,055, TXT 1,261, PRICE 129, ATTR 119), promotion 1,945 (IMG 1,629, TXT 316), K-B 1,493,
K-R 837; share of joined adverts IMG 69.4 %, TXT 28.8 %, PRICE 1.0 %, ATTR 0.9 %; share of merge edges IMG
71.8 %, TXT 26.4 %, ATTR 1.2 %, PRICE 0.7 %.

**Pair grain, merge edges by rung x carrier (T1):**

| rung | g15 | c17 | c18 |
|---|---|---|---|
| K-C (IMG) | 2,100 | 6,492 | 6,915 |
| K-B (TXT) | 1,564 | 2,614 | 1,445 |
| K-R (TXT) | 173 | 1,708 | 1,059 |
| model | 1,942: IMG 1,166, TXT 654, ATTR 84, PRICE 38 | 7,794: IMG 4,904, TXT 2,391, ATTR 357, PRICE 142 | 7,995: IMG 5,060, TXT 2,470, ATTR 295, PRICE 170 |
| promotion `agree:2` | 2,114: photo IMG 1,393, body TXT 721 | 21,156: body TXT 14,725, photo IMG 6,419, twin TXT 12 | 7,663: photo 6,009, body 1,646, twin 8 |
| promotion `photo` / `unit` | 48 IMG / 30 TXT | 73 IMG / 8 TXT | 134 IMG / 26 TXT |

**Positive log-odds share by family on MODEL merges (T4; w6_gold's own contributions, absent terms = MISSING):**
g15 IMG 36.2 %, TXT 29.0 %, ATTR 14.0 %, PRICE 7.4 %, MISSING 6.0 %, TIME 3.6 %, LOC 3.5 %, BRK 0.3 %;
c17 IMG 36.2 %, TXT 29.9 %, ATTR 13.6 %, PRICE 7.2 %, MISSING 5.9 %, TIME 3.9 %, LOC 3.1 %, BRK 0.2 %;
c18 IMG 35.9 %, TXT 29.3 %, ATTR 14.1 %, PRICE 7.3 %, MISSING 6.4 %, TIME 3.6 %, LOC 3.1 %, BRK 0.3 %.
Stable to a point across three independent areas: the table is a property of the engine, not of the area.

**Blocking side:** gate demotions g15 92 / c17 406 / c18 319 (ATTR dominant: floor, total_floors, plot_area,
printed / stated area; PRICE: price); demonstration refusals A g15 292 / c17 2,032 / c18 1,801 (price 146 / 973,
area 106 / 758, disposition 37 / 258), B 114 / 878 / 831; group refusals g15 265 (ATTR 34.3 %, IMG 33.2 %, PRICE
20.4 %), c17 1,370 (PRICE 45.7 %, ATTR 24.6 %, IMG 20.0 %), c18 1,057 (PRICE 63.8 %, ATTR 23.5 %, IMG 7.9 %).
The violating pair is another member in 219 of the 230 reproduced g15 fact conflicts (the refused edge itself is
clean); 30 g15, 56 c17 and 1 c18 conflicts are not reproduced by my relation (it lacks the run's must-link
closures and the unscored pairs' feature rows).

### 1.5 Findings the prototype surfaced (numbers)

1. **The E11 evidence gate is inert.** `min_evidence_families: 1` in w31 (`w31.json:199`; `settings.py:462`
   default 2); 0 `evidence_gate` reasons on every stored S14 cohort (3-16, region, trial), g12, g13, g14, c17
   (39,845 merges), c18 (25,237). Deleting `diverse` (`decide.py:729,743,752`) changes 0 decisions on that ground.
   It pays for the `rung` + `carrier` columns: `EVIDENCE_RULES` (`features.py:404-462`, about 60 lines),
   `evidence_families` (`:1675-1681`), the setting and its validation, the `families` column and its 3 mirrors,
   `bit_or` (`ui_sql.py:538`), `clusters.evidence_families`.
2. **"agree:2" does not mean agreement.** `agreeing_attributes` counts `price` whenever both adverts state a price
   (`indistinguishable.py:3168-3169`) and `floor` whenever both state a floor (`:3153-3156`); 1,924 of 2,114 g15
   `agree` promotions and 20,296 of 21,156 c17 ones have `price` in the set. The positive agreement is
   demonstrated later by D50 (A). The warrant still holds back pairs: 13 of 1,035 g15 and 60 of 7,303 c17 plain
   `model` band pairs have no stated fact, no warrant and pass the demonstration (`c6/*/warrant_probe.txt`).
   So E131 is load-bearing (a small loosening if deleted) and its name is wrong; the carrier of every `agree`
   promotion is D50's (B) corroboration (body or photo).
3. **Promotion is the biggest merge rung on c17:** 21,237 of 39,845 merge edges (53 %), 14,725 of them carried by
   a contained body (bazos re-post trains). g15 27 %, c18 31 %.
4. **The live stream cannot produce the advert-grain table today** (`joined_via` NULL on `rt`,
   `incremental_lane.py:704`): the batch and live lanes write different member rows.
5. **The Progress page describes a program that no longer exists:** "shadow mode ... merges nothing"
   (`frontend/src/pages/AutodedupProgress.tsx:11-12,58,441-442,459`), while the trial has merged live since
   2026-09-25 (`prod_readiness/TRIAL_LIVE.md`).

### 1.6 Page spec: a fold of the existing stats pages

**Where:** `/autodedup/progress` (`frontend/src/pages/AutodedupProgress.tsx`, 549 lines) becomes the engine page
of a generation: `GenerationSelect` (exists, `components/autodedup/GenerationSelect.tsx`) + the ONE table of
1.4 (rows = rungs in ladder order, columns = ATTR PRICE TXT BRK LOC IMG TIME OPERATOR NONE, cells = adverts,
row share, then the two share rows) + the existing operator-reason table. A cell links to Groups / Residual
filtered by `(rung, carrier)`; those queues' filter replaces the family chip.

**Deleted by the fold:**

| what | where | lines (approx.) |
|---|---|---:|
| unrendered engine stats: `pairs_by_zone`, `certificates`, `generations`, `verdicts`, `judgements`, `last_score_run` (fetched, typed, never rendered) | `api/routes/autodedup.py:1007-1061` `_engine_stats`; `ui_sql.py:1155-1177` `PAIR_ZONES_SQL`, `CERTIFICATE_COUNTS_SQL`; `:1226-1234` `VERDICT_COUNTS_SQL`; `:1250-1257` `JUDGEMENT_COUNTS_SQL`; `:1259-1280` `LAST_SCORE_RUN_SQL` + column tuples; `frontend/src/lib/api.ts:3613-3643` fields | 130 |
| reason-string parsers | `_certificate` (`api/routes/autodedup.py:458-465`), `_why_not_merged` (`:468-481`), `CERTIFICATE_COUNTS_SQL`; `n_certificates` in `EDGE_SUMMARY_SQL` (`ui_sql.py:535`) reads the column instead | 35 |
| the 7-bit family bitmask, 3 mirrors + decoders | `score_lane.py:96-104,230-250`; `api/routes/autodedup.py:244-255,453-455`; `EvidenceChips.tsx:10-35` | 70 |
| stale shadow-mode copy + MODE chip | `AutodedupProgress.tsx:11-12,58,436-460` | 30 |
| E11 gate and rules | section 1.5.1 | 90 |

No page is deleted outright by C6. Candidates the table makes redundant but C6 did not measure: the iteration
ledger tiles (program spend) and the D6 AgreementPanel (D6 is recorded as moot, `docs/design/autodedup/PROGRAM.md`
"D6 status" note).

### 1.7 Subtraction ledger, item 4

| added | lines | deleted | lines |
|---|---:|---|---:|
| `Decision.rung`, `Decision.carrier` + family split (reuses `model.contributions`, `FAMILY_OF`) | 45 | E11 gate + rules + setting | 90 |
| `FACT_FAMILY` (64 readers -> 5 families) | 25 | family bitmask x3 + decoders | 70 |
| migration: +`pairs.rung`, +`pairs.carrier`, -`pairs.families`, -`pairs.applied_merge_group`, -`clusters.evidence_families`, drop `autodedup.models` (destructive: operator OK + backup per the database skill) | 30 | `_engine_stats` + 5 SQL + types | 130 |
| lane `joined_via` (call the score lane's function) | 5 | reason-string parsers | 35 |
| view `generation_attribution` + endpoint | 60 | stale copy | 30 |
| table component | 110 | lane NULL write | 3 |
| **total** | **about 275** | **total** | **about 360** |

Net: -85 lines, -2 columns, -1 table, -1 setting, -3 duplicated contracts, -2 reason parsers; one family
vocabulary instead of three (E11 families, feature families, the unnamed reader groups).

### 1.8 Validity limits of the prototype

- Model attribution uses the training-mean baseline (`model.py` standardisation); a different baseline moves the
  shares, not the decisions. Family sums are exact decompositions of the log-odds; single-feature signs are not
  causal (`dispo_equal` above).
- c17 ran without operator must-links (the offline file holds must-not-links only), so its "operator" row is 0.
- `no_stored_pair` cannot be split into never-compared / walled / under-the-floor from stored rows; `run.json`
  counts walled PAIRS only (g15: area 357,509, floor 45,577, disposition 45,576, category 77,121).

---

## 2. Item 2: the DINOv3 labels as the ONE image-semantics source

### 2.1 The heads (model v1), and the coverage gap

- Model v1: promoted from bake-off run 1, arm `dinov3-b16@768/bf16`, mode `pos_neg`, 11 heads, activated
  2026-09-09 (`docs/design/new-dedup/PROGRAM.md:741-800`); tables `tag_head_models`, `tag_head_model_heads`,
  `image_tag_scores` (`migrations/490`), embeddings table `image_dinov3_embeddings` (`migrations/480`, primary
  key = the seven encoder identity facts).
- The 11 classes: section 0 line 5. Quality: mean CV F1 0.903, exam F1 0.732 (`PROGRAM.md:1063-1075`); per head
  CV F1 katastrální mapa 0.969, půdorys 0.961, obývací pokoj 0.918, 3d plán 0.909, letecký snímek 0.881, technické
  zařízení 0.873, property list 0.687 (`:766-770`); the winner is right on 90 of 94 exam photos that carry one of
  the 11 (95.7 %); 156 of 250 exam photos are none of the 11 and 41 % of those still get a winner of 0.5 or more
  (`:785-800`).
- Scored so far: 9,514 images (training set + exam), not the corpus; `data/dinov3_config.json:6-8` still holds
  three nulls and the embed lane refuses to run (`scraper/dinov3_config.py:47-66`).
- Coverage against the engine's 15 CLIP rooms (`toolkit/room_taxonomy.py:19-35`), trial top tags (A5 3.1): no v1
  head for hallway 24.6 %, bedroom 8.9 %, garden 6.3 %, balcony/terrace 4.8 %, toilet 4.7 %, staircase 2.1 %,
  other 0.9 % = about 52 % of photos. The private-room set the hazard features read (`features.py:213-215`)
  shrinks from {kitchen, bathroom, toilet, living room, bedroom} to {kitchen, bathroom, living room}.
- Arm choice is still open in the NEW DEDUP ledger (open question 2, `PROGRAM.md:833-837`):
  `dinov2-l14-reg@504/bf16` has the best exam F1 (0.758) at 26.2 img/s (2.2x faster, Apache-2.0) against v1's
  0.732 at 12.0 img/s. It changes every cost below by 2.2x.

### 2.2 What reads image semantics today, app-wide

| reader | reads | where |
|---|---|---|
| engine export + live lane | `image_clip_tags.logical_tag`, confidence | `autodedup/export_sql.py:235-245`; `export.py:645-660`; `incremental_lane.py:946-977` |
| engine photo hold F3 | `images.clip_tagged_at` (the CLIP job's stamp) | `autodedup/incremental_sql.py:781-794`; `incremental_lane.py:1221-1244` |
| engine fine-to-logical collapse | `data/clip_taxonomy.json` read at import | `autodedup/fingerprint.py:40-70` |
| engine room vocabulary | `ROOM_FAMILIES` (15 CLIP codes) | `toolkit/room_taxonomy.py:19-35`; `features.py:24,213`; `fingerprint.py:34,66` |
| SPA photo tag badge | `images_public.clip_fine_tag` / `clip_logical_tag` | view `migrations/496:79-90`; `frontend/src/lib/imageTags.ts` (a hand mirror of 3 backend vocabularies), `components/ImageTagBadge.tsx` |
| SPA "vizualizace" badge | `clip_render_score` | view `migrations/416:34-43`; `components/ImageRenderBadge.tsx`, `listing-detail/Gallery.tsx`, `ImageLightbox.tsx`, `lib/hydration/useCardHydration.ts` |
| labeling proposals | a SECOND zero-shot CLIP tagger over the operator's labels | `scraper/label_proposal_tagger.py`, `scripts/label_proposal_backfill.py`, `.github/workflows/label_proposal_backfill.yml` |
| labeling / taxonomy / tag model pages | `image_tag_scores`, `image_tag_labels`, `tag_taxonomy` | `api/new_dedup_tags.py:49,91,116`; `toolkit/tag_models.py` |
| candidate draws, exam screening | `image_clip_embeddings` centroids | `toolkit/tag_candidates.py`, `toolkit/tag_exam.py` |

Four vocabularies describe one photo: `ROOM_FAMILIES`, `data/clip_taxonomy.json` (19 fine prompts + collapse +
render anchors), the SPA's `IMAGE_TAG_LABELS`, and the operator's `tag_taxonomy` (49 labels, with a `family`
column already, `migrations/442:28-35`). The operator's taxonomy is the only one a human curates.

### 2.3 Where to score: measured

| option | compute per image | new images/day (55-60k, `ENCODER-DECISION.md:1-7`) | money | dependency / licence | latency for F3 |
|---|---|---|---|---|---|
| CPU inline in the worker, 768 px (v1's resolution) | 9.8 s/core (measured, section 0) | 6.8 cores all day | about $136/month vCPU at Railway list price (not verified this session) + RAM | `onnxruntime` (rule 7) + an exported graph of the licence-gated DINOv3 | minutes |
| CPU inline, 512 px | 3.1 s/core | 2.2 cores | about $43/month | same; a NEW population: v1's heads were trained on 768-px vectors, `dinov3-b16@512` exam F1 0.708 | minutes |
| GPU hourly batch (RunPod RTX 3090, $0.22/h) | 12.0 img/s end to end (measured, incl. R2 download and decode) | 1.4 GPU-h/day | $0.31/day pure, about $0.75/day with 24 pod start-ups (about $23/month) | none new in the worker; RunPod already used (`scripts/dinov3_embed_dispatch.py`, `.github/workflows/dinov3_embed_backfill.yml`); 7 pod attempts were needed for bake-off run 1 | hourly cadence; today's CLIP lane: p50 2.5 h, p90 5.6 h (`W5_DESIGN.md` 3) |
| CLIP B/32 at 224 px, for scale | about 9 GFLOP, about 0.15 s/core | 0.1 core | 0 | torch today (free runners) | |

**Decision: ONE hourly GPU image job.** Per new image, one R2 download feeds: the CLIP B/32 vector (still read by
the room cosines `tag_clip_*`, `clip_max_cos`, `clip_mean_top3_cos`), the DINOv3 vector, and the active model's
11 head scores (a dot product and a logistic, `toolkit/tag_heads.py:796-820`) written straight to
`image_tag_scores` in the same process. It replaces `clip_tag.yml` (4 shards of CPU torch on free runners,
`.github/workflows/clip_tag.yml:1-19`) and the `score` stage of `tag_model.yml`. Stage 1 therefore ends with ONE
image job, not two: adding a DINOv3 lane beside `clip_tag.yml` would make F3 wait for two hourly lanes, which is
more jobs, not fewer. Risk carried by the choice: F3 then depends on RunPod availability; the 48 h horizon
(`incremental_lane.py:286`) bounds it.

### 2.4 ONE tag table

- `image_tag_scores` (`migrations/490`) for the ACTIVE model (`tag_head_models` partial unique index): the winner
  and its score, with the consumer floor 0.5 (the only floor the NEW DEDUP ledger measured: keeps 96.8 % of the
  true tags, cuts confident wrong tags on none-of-the-eleven photos to 41 %, `PROGRAM.md:785-800`); below the floor
  the photo is `other`. No per-head yes/no (operator ruling 2026-09-09, `490` header).
- Switched readers: `COHORT_CLIP_TAGS_SQL` becomes one tag SQL over `image_tag_scores` joined to `tag_taxonomy`
  (export and lane share it, so one edit moves both); the F3 probe keeps reading `images.clip_tagged_at`, which the
  one job stamps only after the CLIP vector AND the head scores are written (no new column; the name is renamed
  in W8's cleanup); `images_public` and `listing_cover_public` read the head winner's taxonomy label (a view
  migration); labeling proposals read the active model's winners (the second zero-shot tagger goes).
- Replaced: `image_clip_tags` (every reader), the fine/logical/render vocabularies, the SPA mirror.
- **Blocker:** `render_score` lives only in `image_clip_tags` (`scripts/clip_tag_backfill.py:80`) and feeds the SPA
  "vizualizace" badge. The engine never reads it (the export selects fine_tag, logical_tag, confidence only). v1
  has no render head. Either head set v2 gets a render head, or the badge goes (operator's product call); until
  then `image_clip_tags` survives for that one column and the table is not deleted.

### 2.5 Routing features switched to head tags

Mapping (one dict, keyed by taxonomy label, replacing `ROOM_FAMILIES`; the export refuses a head label the dict
does not know):

| head | engine room | family | private room |
|---|---|---|---|
| interier - kuchyně | kitchen | interior | yes |
| interier - koupelna | bathroom | interior | yes |
| obývací pokoj (exact label in DB) | living_room | interior | yes |
| technické zařízení / místnost | technical | interior | no |
| exterier - fasáda | exterior_facade | exterior | no |
| podklad - půdorys | floor_plan (`FLOOR_PLAN_TAG`) | plan | no |
| podklad - 3d plán | plan_3d (NOT floor_plan: a 3D view against a 2D drawing of one unit cannot match by dHash and would fire `floorplan_conflict` falsely) | plan | no |
| podklad - katastrální mapa, letecký snímek s ohraničením | site_plan | plan | no |
| property list (exact label in DB) | property_document | plan | no |
| garáž | garage | other | no |
| winner under 0.5 | other | other | no |

Features and rules that move (all read the per-photo top tag, `dataset.py:316-320`): the 11 `tag_*` /
`floorplan_*` features (`features.py:1160-1300`), the 3 family ratios (`:1114-1127`), the anchor order
(`fingerprint.py:186-223`), D43's interior and floorplan facts (`indistinguishable.py:186-190,3088-3108`), the
`room_photo` agreeing attribute (`:3181-3183`), `strong_corroboration`'s room floor (`demonstrate.py:695`),
the judge's room pairing (`judge.py:791-836`). Unchanged: pHash, E9 stock, K-C, the CLIP cosines themselves.

Tag dials: `d43_gate_image_facts` (default true, w31 false: the E138 standing rule written as a dial), 
`d43_cluster_image_facts` (true), `d43_interior_requires_no_tight_photo` (default false, w31 true),
`d43_interior_sequential_repost` (default false, w31 true): four dials whose shipped value never changes and
whose defaults contradict the shipped file; they become the code. E45's `unit_interior_min`,
`unit_rare_support_interior_min` and `context_rule_interior_min` read `interior_match_ratio` behind gates that are
off (`unit_evidence_required: false`, `w31.json`); `clip_sample` (8) stays while CLIP vectors stay.

### 2.6 Storage and backfill

- Corpus: 11.3-11.5M images (`image_clip_embeddings` 11,301,885 rows on 2026-09-05; 10,489,289 of them
  unpinned, the CLIP-drift note). v1 arm: 262-266 GPU-h = **$58-59** plus failed attempts; the DINOv2-L arm
  about $27.
- `image_tag_scores`: 11.5M x about 300 B = about 3.5 GB (`490` header). Every reader reads only the winner; the
  jsonb of all 11 probabilities is the ruled design, not a need of any consumer outside the labeling pages.
- DINOv3 vectors: `halfvec(768)` about 17.5 GB payload, 19-20 GB on disk (database 150 GB on 2026-09-05).
  Recommendation: store them, in the existing table. Reason: head versions iterate ("adding heads is a new
  version", `PROGRAM.md:818`), and with stored vectors a v2 re-score is a free CPU pass; without them every
  head version is another $27-59 GPU pass and the measurement below cannot re-run offline. The storage is paid
  back only in stage 2, when `image_clip_embeddings` (31 GB) goes: net -11 GB.
- Order: pilot cohorts first (below), the corpus after the pilot passes; the lane switch after the corpus.

### 2.7 Measurement before the switch lands (D83, D90/D91, the W5 bars)

Ground images: cohorts 3-16 3,903,267; region 231,283; trial 80,394; c17 258,120; c18 256,993 = about 4.7M
images, 42 % of the corpus (export meta counts). All exports carry `image_id` per image, so head tags attach by
id without re-export (a side file `image_id -> winner label, score` dumped once from `image_tag_scores` in
Actions; the harness loader takes it the way W5's arm T transform took `img.tags = []`).

| step | what | cost | gate |
|---|---|---|---|
| M0 | operator: head set v2 (bedroom, hallway, toilet, balcony/terrace, garden, staircase; render; an explicit other head or the 0.5 floor) on `/new-dedup/training-set`; bake-off run 2 | labeling + under $2 | operator |
| M1 | pilot embed: cohort6 (316,248), cohort3 (240,875), region (231,283) = 788,406 images, the three cohorts where silencing tags hurt most (W5: -0.88, -0.67, -0.57 pp) | 18 GPU-h, about $4 | |
| M2 | arm H: tags := head winners, w31 + w6_gold UNCHANGED | local harness, 0 | W5 bars per cohort: certain recall down at most 0.10 pp; merge flips at most 1 % of merge edges; 0 new structural negatives; 0 fused-ledger regressions; every gained pair and changed group hand-read (D83); re-post trains 0 split; land and new-development hazard classes reported apart |
| M3 | arm H+R: refit w6_gold on head-tag features over the operator labels, sealed test | local, 0 | sealed AUC not below w6_gold's 0.9766 (the bar W7 failed); then M2's bars |
| M4 | corpus embed, then the 16 cohorts + trial + c17 under the winning arm; a new unseen cohort (D91: a tagger change can merge more) | $58-59 | M2's bars on all ground |
| M5 | lane switch = new engine version = `rt` re-seed; delete the CLIP tag path | | |

Expected, from the numbers: arm H fails. Tag features are pivotal for 56-59 % of model merges and 1,311 / 3,701
/ 3,937 K-C merges (g15 / c17 / c18); the tag-derived image facts refuse 88, 274, 83 group joins; the private-room
set shrinks from 5 rooms to 3 and about half the photos have no class. A refit (M3) is the likely path, and the
W7 precedent (refit refused on the sealed test, `TRIAL_LIVE.md:163`) says it can fail.

### 2.8 Subtraction ledger, item 2

**Stage 1 (labels).**

| deleted | lines |
|---|---:|
| `.github/workflows/clip_tag.yml` (replaced by the one GPU job) | 105 |
| `scripts/clip_tag_backfill.py` + `tests/scripts/test_clip_tag_backfill.py` (the CLIP embed half, about 100 lines, moves into the GPU job) | 449 -> about 100 |
| `.github/workflows/clip_retag.yml`, `scripts/retag_from_embeddings.py` | 200 |
| `data/clip_taxonomy.json`, `tests/test_clip_taxonomy.py`, `fingerprint.py:40-70` collapse | 177 |
| `scraper/label_proposal_tagger.py`, `scripts/label_proposal_backfill.py`, its workflow, its test | 457 |
| `data/dinov3_config.json`, `scraper/dinov3_config.py`, `tests/test_dinov3_config.py` (identity comes from the active model's registry row, which already holds all seven facts) | 209 |
| `tag_model.yml` score stage + its script branch | about 60 |
| SPA `IMAGE_TAG_LABELS` fine / logical mirror (labels come from `tag_taxonomy`) | about 40 |
| 4 tag dials (2.5) | about 25 |
| **total** | **about 1,570** |

| added | lines |
|---|---:|
| GPU job: CLIP embed + head scoring + stamp in `scripts/dinov3_embed_backfill.py` | about 90 |
| one tag SQL + label-keyed room map + export refusal rail | about 40 |
| view migration for `images_public` / `listing_cover_public` | about 60 |
| harness side-file loader (measurement), tests | about 60 |
| **total** | **about 250** |

Kept until the render blocker is settled: `image_clip_tags` (one column read), `scripts/backfill_render_score.py` +
`.github/workflows/backfill_render_score.yml` (167 lines), the render anchors. Superseded: D901 (by the operator's
ruling of 2026-09-26), the addendum's "inline via ONNX" clause of Decision 10 (measured infeasible at 768 px).

**Stage 2 (one encoder; only after the near-duplicate bake-off, `ENCODER-DECISION.md` 5 item 4, unrun):** the
CLIP half of the job, `image_clip_embeddings` (31 GB), `scraper/clip_tagger.py` (183), the CLIP identity pin
tests, and every image threshold re-cut on DINOv3's cosine scale (`indistinguishable.py:188,190`,
`features.py:206,243`) with a refit.

### 2.9 Operator decisions this design needs

1. DINOv3 labels versus Decision 10's "no hourly job": measured, the two cannot both hold at 768 px.
2. Head set v2: which rooms, a render head (or drop the "vizualizace" badge), an explicit other head or the 0.5
   floor.
3. The arm: v1's `dinov3-b16@768` (the program's accepted encoder) or `dinov2-l14-reg@504` (better exam F1,
   2.2x cheaper, Apache-2.0); open since 2026-09-09.
4. Store the DINOv3 vectors (about 20 GB) or re-embed per head version.
5. Rule 7: none is needed on the GPU path; `onnxruntime` is needed only on the rejected CPU path.

---

## 3. Assumptions checked

| assumption | verdict | evidence |
|---|---|---|
| "The engine uses the DINOv3 labels" / could switch by a SQL change | refuted | the engine reads `image_clip_tags` only; `image_tag_scores` holds 9,514 images; 3 identity nulls block the embed |
| "Inline DINOv3 in the worker" (Decision 10 addendum) | refuted by measurement | 9.8 s/image/core at 768 px, 6.8 cores all day |
| "Deleting the CLIP tag path deletes the hourly CLIP job" | false for stage 1 | the room cosines still read CLIP vectors; only a merged job keeps the job count at one |
| "`image_clip_tags` serves only dedup" | refuted | the SPA tag and render badges read it through `images_public` and `listing_cover_public` |
| "`agree:2` = two attributes agree" | refuted | price and floor count when merely stated (`indistinguishable.py:3153-3169`) |
| "E11 families guard merges" (w31) | refuted | 0 firings on all stored ground at `min_evidence_families: 1` |
| "`pairs.certificate` is not a column" (API docstring) | refuted | `migrations/539:227-235` |
| "The rt stream records how each member joined" | refuted | `joined_via` NULL (`incremental_lane.py:704`) |
| "The model's ATTR evidence is stated attributes" | refuted | 61-63 % of ATTR-carried model merges ride on absent attributes |
| "The heads cover the rooms the features route on" | refuted | about 52 % of trial photos have no v1 class |
| "The trial is shadow mode" (Progress page) | refuted | live merges since 2026-09-25 |

## 4. Files

- this report: `/home/hejtm/autodedup-artifacts/w15/census/C6_dinov3_attribution.md`
- scripts `/home/hejtm/autodedup-artifacts/w15/census/c6/`: `attribution.py` (the prototype table, all tables
  T1-T7), `summary.py` (the operator table), `tagshare.py` (room-tag share and pivotality), `missing_share.py`
  (E12 check), `warrant_probe.py` (E131 check), `run_w31.py` (the c17 harness run), `run_all.sh`
- outputs: `c6/g15/`, `c6/c17/`, `c6/c18/` (`attribution.json`, `attribution.md`, `operator_table.md`,
  `tagshare.json`, `warrant_probe.txt` for g15 and c17); the c17 w31 run `c6/runs/c17_w31/`
  (`run.json`, `pairs.jsonl.gz`, `clusters.json`)
- CPU timing script: `c6/cpu_vit_gemm.py` (numbers in section 0 line 6; run with OPENBLAS_NUM_THREADS=1 while
  other census agents shared the machine, so the per-core rate is a floor, not a ceiling)
- the same scripts and this report are committed on branch `census/w15-c6-dinov3-attribution` @ `d1e2d74d`
  (pushed; no PR), path `autodedup_w15_census/c6/`
