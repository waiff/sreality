# C5: the data-science substrate (export contract, label store, model and eval, data-quality rails, scale)

W15 census, wave W0, read-only. Tree: origin/main @ b2454fa1 (worktree `.claude/worktrees/w15-c5-substrate`, branch `census/w15-c5-substrate`). No database. Pointers are `file:line` on that tree. Every number was measured here unless it names its source. Scripts and raw outputs are in `/home/hejtm/autodedup-artifacts/w15/census/c5_work/`: `leak.py`/`leak.json`, `ref_vs_operator.py` + `.log`/`.json`, `floor_delta.py`, `total_delta.py`, `plot_census.py`, `bool_census.py` and `numeral_np.py`.

Inputs:
- Trial export: g15, 5,195 adverts, export run 36242569357.
- Cohort 17: 17,897 adverts, run 36221961445.
- Cohort 18: 17,422 adverts, run 36237638871.
- Cohorts 3–16: `w14/cohortN/export`.
- Training labels: the 7 judgement files named in `models/w6_gold.json` provenance, copied at `w6/labelfiles/`.
- The model's training export: `w7/cohort/autodedup-export-35200225251`.
- Operator labels: `labels_g13_36225845749`.
- W7 refit record: `w14/refit_w7/{results_refit_w7,eval_common}.json`.

---

## 0. Headline findings (for the coordinator)

1. **No evaluation today is independent of the engine.**
   - **"Certain recall" measures agreement with the engine's own certificates.**
     - Its reference is `cohort16/scripts/truth16.py`, outside the repo, importing a pinned package snapshot. It builds three classes:
       - `A_code`: the rare order code, the same criterion as K-R;
       - `B_frames`: at least 4 non-stock frames, the same as K-C;
       - `C_text`: identical bodies of at least 300 characters, about K-B.
     - 95.7 % (trial), 92.1 % (c18) and 90.6 % (c17) of the reference pairs sit in the engine's own merge zone.
     - The reference covers only 66.1 % (trial, 338/511), 65.9 % (c17, 29/44) and 55.8 % (c18, 29/52) of the pairs the operator ruled `same`.
   - **The operator's rulings are engine inputs in every run.**
     - All 246 negatives (162 in the trial, 12 in c18) are bound as must-not-links. The g15 score lane reads the database. The c18 offline run reads `data/autodedup-labels-35609425873/must_not_link.jsonl`, which holds the same 246 pairs.
     - The 1,251 `same` pair rulings are bound as must-links in the lane and the score lane.
     - "Trial 511/511 together, 162/162 apart" is therefore true by construction.
2. **The shipped model is fitted on LLM labels only, on the trial ground.** This contradicts D20's premise that "the operator is the reference".
   - `w6_gold` has the same weights as `w5_gold`. They were fitted on 2,760 decided gold and vision labels (text contributes 0 after precedence) from one export, 35200225251:
     - jablonec 2,725, vysocany 1,421, turnov 741, negctl 800;
     - this is the trial area; 4,886 of the trial's 5,195 adverts are in it.
   - Gold agrees with the operator 92.65 %, against D6's bar of ≥0.95 (M46). D20 retired the gate, but the learned score still decides 1,942 g15 model merges and the band floor.
   - The shipped model's W5d sealed split is lost: `w6/refit/step1_split.json` records `w5d_seal_recovered: false`.
   - The W7 refusal compared challengers against the incumbent on 947 "sealed" rows. 537 of those rows are the incumbent's own training rows (`eval_common.json` subset `recipe_labels_(w6_gold_in_sample_risk)`), and the W7 splits leak 401–486 listings across splits (`results_refit_w7.json` `leakage_check`).
   - On the only independent labels in that seal (operator explicit, n=64, 25 negatives in the operator subset), w6_gold and w7a both read AUC 1.0.
3. **Calibration is 8 steps fitted on 287 LLM-labelled rows.**
   - The steps (calibrated level, validation rows) are: 0.0 (50), 0.136 (26), 0.1823 (14), 0.562 (7), 0.5745 (4), 0.9545 (105), 0.9788 (9) and 1.0 (72).
   - `t_hi` = 0.9788 and `t_lo` = 0.1823 in `settings/w31.json` are two of those plateaus. The merge cut rests on the 9 rows of one step, i.e. a raw score of at least 0.97997. E63's "score ≥ 0.9999" is the 1.0 plateau.
4. **The engine repairs source defects downstream, and five of those repairs are stale now that the source is fixed.**

   | Repair | Evidence it is stale |
   |---|---|
   | Floor camps | Idnes/sreality K-C pairs sit at Δ0 on 90 % (n=378) |
   | Plot-truncation table | ceskereality estate ≥1,000 m² on 24 % of rows, realitymix headline on 62 % (the table was built when both were 0 %) |
   | `FALSE_BY_OMISSION` | 3 of its 12 slots now emit `false` |
   | `price_unit` vocabulary exclusion | Sale `price_unit` is canonical on every row but 3 |
   | The engine's own text grammar | Diverges from the platform grammar (point 5) |

5. **Two grammars read one fact.**
   - The engine's `normalize.numeric_facts` reads "plocha 11 197 m²" as **197** and "3. NP" as floor **3**.
   - The ingest grammar reads 11,197 (`scraper.area.parse_area_text`) and 2 (`scraper.floor.normalize_floor`).
   - 189 of the 478 stored g15 `auto_reject:numeral_conflict` pairs with a floor conflict lose that conflict under the ingest grammar. `numeral_conflict` is a terminal auto-reject, and `numeric_fact_overlap` carries the model's 2nd-largest weight (+0.585).
6. **The label store holds one statement in up to four places.**
   - Must-not-link equals the negative pair rulings exactly: 246 = 246, 0 extra, 0 missing.
   - Browse groups hold 1,084 member pairs, but only 1,051 are ruled. That is 33 pairs in 27 groups.
   - Group rulings lose 437 of 714 implied pairs across generations (A2 §2.4).
   - Two decider identities exist for one human: `op:5f129dae…`, the literal `'operator'` on 1,051 rows, and `op:0871b32a…` on 769 rows.
7. **Scale limits.**
   - The trial schema cap is 800 MB (`incremental_lane.py:360`); the corpus projection is 17,000 MB (`incremental_scope.py:140`).
   - Lab generations are stored as DB rows at about 40 MB per trial generation (`incremental_lane.py:351`).
   - The batch harness runs in memory: c18 used 2.77 GB RSS and 451 s for 17,422 adverts, which is about 139 GB for 872k adverts.
   - The E300 town probe is 74.5 % of c18's scored pairs (150,503 of 201,967).

---

## 1. Assumption table

"Refuted" means the data or code contradicts the assumption as stated.

| # | Assumption | Where stated | Verdict | Evidence |
|---|---|---|---|---|
| S1 | Operator rulings are the reference; gold is a tier beneath them | PROGRAM.md:1801 (D20), :1875 | **Refuted as built** | The model's labels are 0 operator, 1,044 gold and 1,716 vision (`leak.json` `precedence`); the weights are unchanged since W5d (`models/w6_gold.json` provenance.inherits). The operator labels are bound as must-links and must-not-links in every scored run (`cohort18_runs/w31/run.json` `must_not_link_source`; `incremental_sql.py:1085-1105`). |
| S2 | "Certain recall" measures recall | preregistration_cohort18.json `reference`; PROGRAM.md:1401 | **Refuted** | The reference classes are the certificate criteria (`truth16.py`, `MIN_FRAMES=4`, `MIN_CHARS=300`, `reference_codes`). The reference agrees with the engine's merge zone on 95.7 / 90.6 / 92.1 % (trial / c17 / c18) and covers 66.1 / 65.9 / 55.8 % of operator `same` pairs (`ref_vs_operator.log`). |
| S3 | Structural negatives are independent truth | truth16.py docstring; bar 1 | **Refuted** | They are produced by `autodedup/structural_truth.py`, which is engine code, and imported from a pinned copy (`truth16.py:27-33`). |
| S4 | Cohorts 17 and 18 are unseen by the model | preregistration_cohort17/18.json | **Validated** | c17 has 0 training pairs and 21 listings shared with the training export. c18 has 89 training pairs and 175 shared listings (via the Praha negctl draw). That is 0.04 % of c18's 201,967 scored pairs (`leak.json`). |
| S5 | The trial is valid evaluation ground | TRIAL_LIVE.md; yardstick | **Refuted for anything score-dependent** | 4,886 of 5,195 trial adverts are in the training export and 2,366 training pairs sit inside it. The calibration and both cuts were fitted there. |
| S6 | w6_gold has a sealed test | models/w6_gold.json provenance.seal (ab2bd7ee…, 713 rows) | **Refuted** | `w6/refit/step1_split.json`: `w5d_seal_recovered: false`, "w5_scratch lost (tmpfs wipe)" (`w6/refit/numbers.json`). The metrics AUC 0.9675 and ECE 5.75 % cannot be reproduced. |
| S7 | The W7 refit was refused on a sound comparison | TRIAL_LIVE.md:163; results_refit_w7.json | **Refuted** | 537 of the 947 "sealed" rows are w6_gold's own training rows (`eval_common.json`). The W7 split puts 401 / 444 / 486 listings in more than one split (w7_0 / w7a / w7b `leakage_check`), touching 1,108 / 1,344 / 1,536 rows. On the independent subset (operator_explicit, n=64) both models read AUC 1.0. On the fresh subset (n=410) w6_gold still wins, 0.933 vs 0.865, so the refusal may stand, but not on the bar as it was read. |
| S8 | The training set is "1,584 pairs from 8 combinations" | coordinator brief | **Clarified** | Seven judgement runs over one export. The re-derivation here gives 2,959 labels: 1,024 positive, 1,736 negative, 199 abstentions (the provenance says 183 abstentions skipped). There are 2,584 split rows because a pair needs stored features. The split is train 1,584 / validation 287 / test 713. The only "8" in the fit is the 8 hand-picked interaction terms (`w6_gold.json` `interactions`). |
| S9 | Class balance and strata are representative | labels.py:32-38 (HT weights) | **Refuted as sampled** | Labels were drawn from the g3/g4 engine's own zones. By zone (decided labels): reject 1,320 (3.6 % positive), merge 610, band 463, catalog-only 367. 378 are certificate pairs (K-A 69, K-B 52, K-C 257), which the score never decides. The fit uses HT weights with `weight_cap` 82.49. |
| S10 | The score is a calibrated probability | A1 §1; PROGRAM.md:1360 | **Weak** | The calibration has 8 levels over 287 rows (`w6_gold.json` `calibration`). `t_hi` 0.9788 is the level of 9 rows. E21's ECE gate fails at 5.75 % (provenance.w5d). Gold agrees with the operator at 92.65 % [88.22, 95.49] (M46, PROGRAM.md:1457). |
| S11 | The operator tier can train a refit | refit_substrate.py; W7 arms | **Refuted as it stands** | The operator labels are 1,574 positive and 246 negative (86.5 % positive). 239 of the 246 negatives come from one session (2026-09-19) on trial candidate cards (A2 §2.2). Outside the trial there are 12 negatives in total, all in c18. |
| S12 | The yardstick measures recall on 16 + 2 cohorts | yardstick.py:1-37 | **Partial** | Browse-merge positives inside each cohort: trial 21, c3 92, c4 9, c5 140, c6 10, **c7 0, c8 0, c9 0**, c10 11, c11 4, c12 25, c13 19, c14 11, c15 3, c16 22, c17 41, c18 45. Zero negatives, so no precision. c7–c9 cannot be measured. |
| S13 | The pre-registered bars read precision on unseen ground | preregistration_cohort18.json `bars` | **Partial** | Bars 2, 4 and 5 are hand reads: the only independent precision reads, and they cover only the DIFF between two arms. Bars 1 and 3 are engine-derived (S2, S3). No bar reads absolute precision. |
| S14 | Operator negatives show the engine keeps units apart | TRIAL_LIVE.md:177 ("213/213 ruled pairs together") | **Refuted as evidence** | The rulings are bound. Unbound, at the pair decision layer: 2 of 162 trial negatives in the merge zone (1.2 %: `18705144×18706836` K-C, `18705144×18707946`), and 0 of 12 in c18. |
| S15 | Must-not-link is a distinct store of permanent negatives | 528:339-347; PROGRAM.md §9 | **Refuted** | 246 rows = the 246 negative pair rulings: 0 same∧MNL, 0 negatives without MNL, 0 MNL without a negative. `source` is only ever `operator`. It can diverge (A2 G7). |
| S16 | Group rulings are durable labels | PROGRAM.md §9 (E58) | **Refuted** | 437 of 714 g4 implied pairs are absent from the g13 labels (A2 §2.4). Browse groups have 1,084 member pairs vs 1,051 ruled pairs (33 missing in 27 groups; `labels.py` + `_canonical_pairs`). |
| S17 | Rulings keep a correction history (Decision 8) | DECISIONS.md #8 | **Refuted on main** | Same-login upserts (528:472-473, 538:241-243). The fix (mig 574, PR #1632) is unmerged. |
| S18 | One human, one decider | A2 §2.1 | **Refuted** | `op:5f129dae7aac4427` (the literal `'operator'` of mig 560) on 1,051 rows and `op:0871b32a9d227f60` on 769 rows. apply's per-decider group slot (G11) keys on it. |
| S19 | Stored floors differ by a per-portal "camp" | settings/w31.json `floor_camps` (sreality 1, idnes 0, …); floor_convention.py:1-20 | **Refuted since W8** | Floor Δ on K-C (photo-proven, floor-blind) byt pairs, trial: idnes−sreality Δ0 90 % (n=378, mean −0.05); realitymix−sreality 93 %; mmreality−sreality 97 %; ceskereality−idnes 91 % (`floor_delta.py`). Ingest converts every portal to ground = 0 (`scraper/floor.py:138-174`, `attribute_contract.py:128-463` conventions; handover-autodedup-floor.md). |
| S20 | total_floors is one quantity across portals | features.py `total_floors_equal`; w31 `d43_total_floors_camp`, `d43_gate_total_floors_slack` | **Refuted at source** | K-C pairs, trial: idnes−mmreality Δ−1 on 87 % (n=38; mmreality = over + underground, `scraper/mmreality_parser.py:485-490`); idnes−sreality Δ−1 on 14 % (n=311); ceskereality total is NULL on every row (handover §6). The engine absorbs this with camp and slack dials instead of a contract definition. |
| S21 | ceskereality / realitymix truncate plot thousands | features.py:100-126 (`PLOT_TRUNCATING_SOURCES`) | **Refuted now** | Share of plots ≥1,000 m² (trial + c17 + c18): ceskereality estate 24 % of 2,020 (was 0 % of 173); realitymix headline 62 % of 342 (was 0 % of 20); sreality 35 %, idnes 36 % (`plot_census.py`). The table blanks 2,362 readings. |
| S22 | These 12 (portal, slot) pairs never publish `false` | features.py:146-171 (`FALSE_BY_OMISSION`) | **Refuted for 3** | False counts: ceskereality has_balcony 165, idnes has_parking 886, mmreality has_parking 177 (`bool_census.py`). The contract already has the concept, `Cell.absence` (`attribute_contract.py:84`), but no cell declares `absence="false"`. |
| S23 | `price_unit` differs on 55 % of cross-portal true duplicates | features.py:58-80, :648-650 | **Refuted now** | Sales: `za nemovitost` on every row of every portal except 3 sreality rows `za mesic` (a category defect). |
| S24 | The engine's text facts read what ingest reads | implicit (normalize.py:36-54) | **Refuted** | "plocha 11 197 m²": engine 197, ingest 11,197. "3. NP": engine 3, ingest 2. "1. NP": 1 vs 0. "2. podlaží": 2 vs None. 189 of 478 stored g15 floor numeral conflicts vanish under the ingest grammar (`numeral_np.py`, which approximates the token regex). |
| S25 | Agency order codes can only be mined from text | text_facts.py:112-123; K-R | **Refuted** | idnes, maxima and remax publish them as structured data (`attribute_contract.py:607, 629, 655`, "the portal's own reference number", no column). realitymix `číslo jednotky` is discarded the same way. |
| S26 | Impossible dispositions are absent | vocabulary.py:228-250 (refused since W2) | **Refuted in stored rows** | 40 bazos rows in the A4 union (819 corpus-wide, field-capture PROGRAM.md:387) survive R4. `normalize._DISPO` (normalize.py:52-54) keeps "3+2" as a known value, so guards.py:77-79 vetoes on it. |
| S27 | The live lane and the batch harness read the same facts | E91; export_sql.py header | **Validated by construction** | `SqlFacts` uses the export statements (`incremental_lane.py:865`). Not re-measured here. |
| S28 | 800 MB is enough for the trial and gates nothing at corpus scale | incremental_lane.py:339-360 | **Validated for the trial; refutes corpus readiness** | rt ≈ 12 kB/listing, a generation ≈ 40 MB per trial. Corpus: 872,604 listings → 9.8–10.2 GB, plus 3.3 GB per 30 days (incremental_scope.py:134-140). |
| S29 | The batch harness can replay any scope | harness.py; E91 | **Refuted at corpus scale** | c18: 17,422 listings, 201,967 scored, 451 s wall (features+decide 377 s), 2,772 MB RSS (`cohort18_runs/w31/run.json`). The export is 15 KB/listing, about 13 GB for the corpus (E_lanes.md:62). |
| S30 | Newest-wins is one rule at every reader | A2 §3 | **Refuted on main** | apply's group slot is newest per (set, decider) (apply.py:449-466, G11). The labels group read is DISTINCT ON cluster_key per generation (labels_sql.py:72-90). The lane reads pairs only. |

---

## 2. (a) The export contract

### 2.1 What an advert carries (`autodedup/export_sql.py`)

| Group | Content | Pointer |
|---|---|---|
| Promoted columns | category_main/type, subtype, disposition, area_m2, floor, total_floors, price_czk, source_url, description | :23-34 |
| `attrs` bag (19) | price_unit, area_basis, has_balcony/parking/lift, building_type, condition, energy_rating, estate/usable/garden_area, category_sub_cb, furnished, terrace, cellar, garage, parking_lots, ownership, published_at | :37-57 |
| Time | first_seen_at, last_seen_at, inactive_at, is_active | listing query |
| Broker | broker_identity_id, broker_firm_id; phone/email only as salt inputs, broker_name never read | :59-63 |
| Location | `listing_location`: obec/cast kod + name, granularity + rank, lat/lon, uncertainty_radius_m, street_name, cp/co, psc, ruian_adm_kod, country_status | query at ~:160-185 |
| History | snapshots (price series), at most 12 price events | export.py:70, :205 |
| Images | phash + corpus population, CLIP vector (`image_clip_embeddings`), **zero-shot CLIP tags** (`image_clip_tags`, :232-241). DINOv3 head scores (`image_tag_scores`) are not exported | :209-255 |

### 2.2 Defective at the source (A4 census plus this census)

| Defect | Status on main | Where the engine absorbs it instead |
|---|---|---|
| idnes floor 20 placeholder | Fixed by #1630 (sentinel); stored rows NULLed by mig 573 (PR #1633) | none (guards veto) |
| Floor or total_floors outside the band | Fixed by #1630 (`floor.py:174`, `:177`); mig 573 | none |
| Area < 5 / < 8 per room / ≥ 1,000 m² on a flat | Fixed by #1630 (`area.py:166, 175, 188`); mig 573 | guards area wall |
| Dotted and space-grouped thousands | Fixed at ingest (`area.py` `AREA_NUMBER_SRC`) | **The engine's own grammar still truncates** (S24), then compensates: `plot_truncated_in_text` (features.py:544), `thousands_truncation_suspect` (:561), `plot_reading`/`plot_residue` (:503, :536) and the dial `d43_plot_truncation_residue` |
| Plot truncation on ceskereality / realitymix | Healed at source (S21) | `PLOT_TRUNCATING_SOURCES` (features.py:122), stale |
| Floor convention mixed 50/50 | Healed by W8 (ground = 0) | `floor_convention.py` (280 lines) + `floor_camps` + dials `floor_camps_reads`, `d43_total_floors_camp`, `d43_floor_total_camp_shift(_mixed)`, `d43_floor_within_camp*` (5), `floor_same_source_feed`, `floor_feed_unknown_closed` (S19) |
| total_floors has no single definition (mmreality over+under; ceskereality NULL; idnes −1 on 14 %) | **Open at source** (handover §6) | `d43_gate_total_floors_slack`, `d43_total_floors_camp`, `total_convention_shift`, `joint_convention_shift` (indistinguishable.py:2657-2680, :3150-3166) |
| ceskereality floor one storey high on ~12 % | Open (handover §6) | floor-gap tolerance of 1 in guards (guards.py:80-83) and `d43_floor_within_camp*` |
| Boolean written `false` by parser default | **Open**: `Cell.absence` is declared and never used | `FALSE_BY_OMISSION` (features.py:158), stale on 3 of 12 (S22) |
| Impossible dispositions (40 stored bazos rows) | Refused at ingest; stored rows kept by R4 | `normalize._DISPO` keeps them "known" (normalize.py:52-54), so a false veto |
| Agency reference number and unit number discarded by the contract | **Open** (`attribute_contract.py:607, 629, 655` + realitymix `číslo jednotky`) | 133 regexes in `text_facts.py` (reference_codes :112, unit designators) + `ref_code_shared` + K-R + `d43_unit_codes*` (4 dials) |
| `price_unit` vocabulary | Canonical now on sales (S23) | `DEFAULT_VOCABULARY_ATTR_KEYS` (features.py:80), stale |
| `area_basis` (usable / total / floor / plot / unknown / None: 5 values + NULL on ~40 %) | Open (meta, not a fact) | read as an attr, then excluded as "vocabulary" (features.py:70); arbitrated at compare time by `effective_area` (indistinguishable.py:2475), `d43_headline_vs_column*` (4 dials), `d43_printed_area*` (3), `demonstrate_area_printed_decides` |
| Location errors (e.g. idnes Brno shared pin) | Location program (`listing_location`) | `pin_pop`, `dist_norm`, `d43_stored_house_number`, `d43_house_number_entrance`, `d43_street_min_distance_m`, `d43_obec_street_grain_only`, `d43_body_obec`, `d43_prose_obec`, `d43_prose_street` (indistinguishable.py:1813, 1843, 2902, 2949) |
| Rent price on a `prodej` advert (4 rows, A4 §6) | Open (category defect) | none |

**Every row in the right-hand column is patchwork.**
- The engine re-derives, second-guesses or down-weights a field whose owner is the per-portal contract (`scraper/attribute_contract.py` + `scraper/{area,floor,vocabulary}.py`) or the location program (`listing_location`).
- The engine imports none of those grammars. Its only cross-package imports are `toolkit.room_taxonomy`, `toolkit.property_identity` and `location_data...deaccent` (normalize.py:21).

---

## 3. (b) The label store

### 3.1 Today

| Store | Rows (g13 export) | Grain | Duplicates what |
|---|---:|---|---|
| `autodedup.verdicts` kind='pair' | 1,497 newest (1,051 browse_merge + 446 explicit) | pair | none; the primary record |
| `autodedup.verdicts` kind='cluster' | ≥224 over time; 83 exported in g13 (82 same) | set (`member_ids`, `generation`, `cluster_key`) | the pair rulings it implies (323 exported in g13, 437 lost) |
| `autodedup.must_not_link` | 246 | pair | **exactly** the newest negative pair rulings (S15) |
| `autodedup.operator_merges` | 363 groups | set | the 1,051 browse_merge pair rows (via `operator_merge_group_id`); 33 member pairs unruled; `status` never written (G6) |
| `autodedup.judgements` (LLM) | tiers gold / vision / text | pair | a second opinion used as the TRAINING label source (S1) |

**Operator-tier precedence and grain rules:**
- Inside the tier: explicit > browse_merge > implied (labels.py:79).
- Across tiers: operator > gold > vision > text (labels.py:57).
- Five verdict values (labels.py:60-72), of which the page offers three (D39).
- Readers use three different newest-wins keys (S30).

**Readers:** 21 SQL references to `autodedup.verdicts`, 6 to `must_not_link`, 1 to `operator_merges` and 10 to `judgements`, across ui_sql, labels_sql, judge_sql, apply_sql, score_sql, incremental_sql and harness (grep count).

**Writers** (A2 §1.1 has the full list):
- `POST /verdict`;
- `/verdict/split`;
- `/verdict/candidate-split`, which writes through its own duplicated upsert plus must-not-link code at `api/routes/autodedup.py:2291-2327, 2605-2640, 2856-2880`;
- `toolkit/property_identity.record_rulings` (:216-242);
- migrations 560 and 564.

**Holes G1–G11 (A2 §5), re-validated against the g13 export here:**
- G3: 437 implied pairs lost.
- G5: post-564 Browse merges are exported as `explicit`.
- G7: the must-not-link mirror.
- G11: newest per decider, with two decider ids (S18).

PR #1632, unmerged, fixes G1, G2, G4, G8, G9 and G10. It does that by adding a supersedes protocol, statuses (`standing`, `withdrawn`, `unsure`, `superseded`) and 6 new SQL statements. It keeps all four stores.

### 3.2 One label table, newest wins (the substrate proposal, part 1)

```sql
autodedup.rulings (
  id          bigserial PRIMARY KEY,
  listing_lo  bigint NOT NULL, listing_hi bigint NOT NULL, CHECK (listing_lo < listing_hi),
  verdict     text NOT NULL CHECK (verdict IN ('same','different','unsure')),  -- 'unsure' = withdrawn / no statement
  event_id    uuid NOT NULL,          -- one operator action; its fan-out rows share it (a group, a split, a Browse merge, a detach)
  origin      text NOT NULL CHECK (origin IN ('review','browse','detach')),    -- where it was typed; display only, never a precedence
  note text, reasons text[] NOT NULL DEFAULT '{}',
  decided_by  text NOT NULL,          -- one identity per human (560's literal 'operator' is re-stamped to the login)
  decided_at  timestamptz NOT NULL DEFAULT now()
);  -- append-only; no UPDATE, no DELETE, no unique index
CREATE VIEW autodedup.rulings_now AS
  SELECT DISTINCT ON (listing_lo, listing_hi) * FROM autodedup.rulings
  ORDER BY listing_lo, listing_hi, decided_at DESC, id DESC;
```

**Rules:**
- One grain: the pair.
- A group `same` fans out to its member pairs at write time, under one `event_id`. That preserves "which set the operator looked at" without a second grain.
- A group negative is not a pair statement. It must be expressed as a split into units, which the split route already writes as pairs. g13 holds one such row.
- Every consumer reads `rulings_now`:
  - lane must-links (`verdict='same'`) and must-not-links (`verdict='different'`);
  - apply's refusals;
  - proposed splits;
  - the labels export;
  - the UI and the Pair page.
- The E61 machine vetoes stay where they already are: pair rows with `zone='veto'`, not the ruling store.

**What it deletes (measured against the g13 export):**

| Deleted | Size / evidence | Pointer |
|---|---|---|
| table `autodedup.must_not_link` + upsert/retract SQL + `source` vocabulary + dial `operator_must_not_link` | 246 rows = derivable (S15); G7 disappears | 528:339-347; ui_sql.py:1325-1342; settings.py:466; score_sql.py:77-80 |
| table `autodedup.operator_merges` + `verdicts.operator_merge_group_id` + migration-564 copy logic | 363 groups = events with origin `browse`; the 33-pair grain gap closes; G6 (`status` never written) disappears | 564:60-213; labels_lane.py:656-706 |
| `verdicts.kind='cluster'`, `cluster_key`, `generation`, `member_ids`, E58 applicability, the group readers | 437 lost labels (G3) recovered by construction; G11 disappears | 538:205-243; labels_sql.py:72-90; apply.py:449-473; ui_sql `_CLUSTER_FROM` :96-115 |
| verdict values `same_building_different_unit`, `same_project_different_unit` | 46 stored rows map to `different`; the reason chip `same_project` carries the nuance | 532; labels.py:66-72 |
| operator-tier sources and weights (`SOURCE_RANK`, `--implied-weight`, `--browse-merge-weight`, `--exclude-*`, `OPERATOR_SOURCES`) | one statement = one label | labels.py:74-79; harness.py:1076-1125; compare.py:96, :188-205 |
| the unique indexes and ON CONFLICT upserts (supersedes PR #1632's 574 and its `superseded` status) | append-only by definition | 528:472-473; 538:241-243; ui_sql.py:1283-1320 |
| duplicated route writers | `record_rulings` becomes the one writer | api/routes/autodedup.py:2289-2327, 2600-2640, 2850-2880 |

**Added:** one table and one view, both replacing four stores. The data migration copies:
- pair rows as they are;
- cluster `same` rows fanned out over `member_ids`;
- operator_merges pairs completed to all member pairs;
- the literal `'operator'` re-stamped to the login.

It is destructive (it drops three stores), so it needs the rule-1 gate and a backup.

**The judge tier is not a label** (S1, S10, M46, D20). Precedence operator > gold > vision > text (labels.py:1-45, 57, 245-330) collapses to the one table. What happens to the LLM judge lane as a producer (`judge.py` 1,378, `judge_lane.py` 2,290, `judge_prompts.py` 249, `judge_sql.py` 91, `agreement.py` 136) is an operator decision: see open question 1.

---

## 4. (c) Model and evaluation soundness

| Item | Fact | Pointer |
|---|---|---|
| Training ground | export 35200225251: jablonec 2,725, vysocany 1,421, turnov 741, negctl 800 (5,687) = the trial blocks | `leak.json` `training_cohort` |
| Label sources | 7 judgement files, 8,714 rows; after precedence 2,959 pairs: gold 577 neg / 467 pos / 112 abstain; vision 1,159 / 557 / 87; text 0 | `leak.json` `precedence` |
| Class balance | 37.1 % positive (1,024 / 2,760); by zone: reject 3.6 % positive (47 / 1,320), merge 92.0 % (561 / 610), band 62.0 % (287 / 463), catalog-only 35.1 % (129 / 367) | `leak.py` strata output |
| Split | cluster split, seed 20260916, 1,584 / 287 / 713; the seal is lost (S6) | w6_gold provenance; w6/refit/step1_split.json |
| Calibration | isotonic_pav, 8 steps over 287 rows; `t_hi` = the 9-row plateau (S10) | w6_gold.json `calibration` |
| Features | 59 in the model (+ `ref_code_shared` unused by it); 8 hand-picked interactions; largest |w| on standardized scale: `dispo_equal` −0.606, `numeric_fact_overlap` +0.585 (read through the diverging grammar, S24), `area_rel_diff` −0.485, `interior_match_ratio` +0.481, `tfidf_cos` +0.443, `tag_dhash_min` −0.419, `overlap_days` −0.393, `ppm2_rel_diff` −0.392. `dispo_equal` and the time features (`overlap_days`, `both_active` −0.262, `same_source` −0.244) carry signs that reflect the zone-drawn sample rather than a causal direction. They are reported, not tested here. | w6_gold.json `weights` |
| Leakage vs bars | trial: 2,366 training pairs, 94 % of adverts; c17: 0; c18: 89; c3: 6; c4: 7; c5–c16: 0 | `leak.json` `cohorts` |
| Operator vs training labels | 77 pairs judged by both; 70 agree (90.9 %) | `leak.json` `operator_vs_training_labels` |
| Label circularity in the runs | every operator label is bound as a must-link or must-not-link (S1, S14) | run.json `must_not_link_source`; incremental_sql.py:1085-1105 |
| Reference circularity | certain = the certificate criteria (S2); structural = engine code (S3) | truth16.py; structural_truth.py |
| Independent precision | only the hand reads of arm diffs (bars 2, 4, 5); unbound pair-grain operator negatives: 160 of 162 correct on the trial (training ground) and 12 of 12 on c18 | ref_vs_operator.log |

**Is the evaluation sound? No.**
- Recall is read against an engine-derived reference.
- Precision is read only on diffs between arms.
- Model selection (W7) was read on in-sample incumbent rows and LLM labels.
- The model's own sealed evidence is lost.
- The one independent label source, the operator, is fed to the engine before every measurement.

---

## 5. (d) Data-quality controls: ingest versus engine

| Control | Ingest (owner) | Engine | Verdict |
|---|---|---|---|
| Storey band −3..40, total 1..40 | `scraper/floor.py:55, 88-89, 174, 177` | guards floor wall `|Δ| ≥ 2` (guards.py:80-83) | wall legit; the band is ingest's |
| Floor convention (ground = 0) | declared per cell (`attribute_contract.py:128, 166, 202, 255, 306, 345, 379, 423, 463`) | `floor_camps` + `floor_convention.py` | **duplicated and stale** (S19) |
| total_floors definition | none | 4 dials + camp readers | **missing at ingest** (S20) |
| Area bands (5 / 8 per room / 1,000 on a flat) | `scraper/area.py:166, 175, 188` | area wall 8 % (guards.py:74-75) | ingest-owned; wall legit |
| Plot band / truncation | none (no plot min at ingest) | `PLOT_MIN_M2` 10 (features.py:144), `PLOT_TRUNCATING_SOURCES`, 2 text readers | **missing at ingest; engine table stale** (S21) |
| Thousands grammar | `scraper/area.py` `AREA_NUMBER_SRC` | `normalize._M2` (normalize.py:38) | **two grammars; the engine's is wrong** (S24) |
| Storey words (NP / patro / přízemí) | `scraper/floor.py` `normalize_floor` | `normalize._FLOOR` (normalize.py:44) | **two grammars; they diverge** (S24) |
| Disposition vocabulary | `scraper/vocabulary.py:228-250` | `normalize._DISPO` keeps impossible values (normalize.py:52-54) | **diverge**; stored leftovers need the heal (S26) |
| Boolean absence | `Cell.absence` (attribute_contract.py:84), never set | `FALSE_BY_OMISSION` (features.py:158) | **duplicated; engine copy stale** (S22) |
| Sentinels ("- nezadáno", "neuvedeno", "20. patro a vyšší") | `Cell.sentinels` + `source_value` (attribute_contract.py:684-701) | none | ingest-owned; correct |
| Agency ref / unit number | discarded (attribute_contract.py:607, 629, 655) | mined from prose (text_facts.py, 133 regexes) | **should be contract columns** (S25) |
| Price vocabulary | canonical (S23) | `DEFAULT_VOCABULARY_ATTR_KEYS` | **stale engine exclusion** |
| Stored pre-rail leftovers | R4 preserve-if-null (`scraper/db.py:154-193`); hand-written heals (mig 554, 573) | read as facts | ingest-owned; the heal must run before the engine reads |
| Location quality | `listing_location` (location program) | 9 dials that re-read the town and street from the body | **second-guessing another program's store** |
| Stock photos | none | `catalog_df` 8 (stock.py:117-186) | engine-owned corpus statistic; legit |
| PII | the stored columns | export scrub (export.py:144, :168) | legit (the export boundary) |

**What the per-portal contract should own, one declaration each:**
- the floor convention (done);
- a total_floors definition (above-ground storeys including the ground floor; underground as its own column, or dropped);
- boolean absence per cell (enforced at write);
- a plot band;
- `agency_ref` and `unit_number` columns, from the structured keys the contract already names;
- the thousands and storey grammar exported as the one library the engine imports;
- the heal of stored leftovers (impossible dispositions) as a data migration.

Then the engine compares facts and never repairs them.

---

## 6. (e) Scalability

| Bound | Value | Pointer | Limits growth? |
|---|---|---|---|
| Claim per pass | min(500, 525 s × measured rate); each of 7 feeds ~limit//5 | incremental_lane.py:1065-1196 (A1 §11) | throughput: the corpus needs about 1 advert/s (872k over 14 days = 0.72/s, plus 0.28/s inflow, PLAN.md:247). Measured: 0.5855 (W0 probe), 1.48 (G2 with the blocks in place), 3.50 (bootstrap) |
| Pair budget | 150,000 per pass | incremental.py:695, :1205 | no, while the town probe stays bounded |
| Deadline | 1,050 s | incremental_lane.py:205 | no |
| Schema cap | 800 MB | incremental_lane.py:360 | **yes**: the corpus projection is 17,000 MB (incremental_scope.py:140), 21× the cap |
| Lab generations as DB rows | ~40 MB per trial generation; keep_generations | incremental_lane.py:351-356; score_lane.py:141-195 | **yes**: a corpus-scale batch generation would be about 7 GB |
| Candidate caps | key > 200 exploded; ≤ 60 candidates per advert | settings.py:57-58; blocking.py:102-104, 209-232 | c18: 1,079 adverts at the cap (6.2 %), 1,324 in exploded keys, 370 with zero candidates |
| Town probe (E300) | 150,503 of 201,967 c18 pairs (74.5 %); largest bucket 182 | cohort18_runs/w31/run.json `blocking` | the main cost driver; big towns explode past 200 and become a recall hole |
| Batch harness memory | 2,772 MB RSS for 17,422 listings | same | the harness is cohort-scale by necessity, so parity with the lane is per cohort, never per corpus |
| Cluster cap | 256 (`max_cluster_size`, incremental.py:318 `SET_CAP`) | guards.py:180-245 | no |

**A clean design removes:**
- lab generations from the database (score lane writes, `keep_generations`, E916/E917 budget juggling, generation-scoped review queries). A lab run is an artifact, which it already is for c17 and c18.
- the claim / budget / deadline triple, in favour of one time bound.
- The DB then holds `rt` + `rulings` only, at about 12 kB per listing (10 GB for the corpus). That is the only projection the schema budget has to meet.

---

## 7. Substrate proposal: one label table, one eval harness, one metric set

**North-star fit:**
- One evidence ladder needs one fact source (the contract) and one label source (the rulings).
- One code path needs the eval to run the same `decide` + `cluster` as the lane, with the rulings as an input the eval can withhold.
- One table of attribution is metric M4 below.

1. **One label table**: §3.2.
2. **One eval harness**: `python3 -m autodedup.harness evaluate <run dir> <rulings_now.jsonl>`.
   - It reads one generation's artifacts and the one rulings file.
   - **Withholding is by input, not by flag.** The run under evaluation is scored with no rulings file, i.e. blind. The live lane binds them.
   - It evaluates on every ruled pair whose two adverts are in the cohort.
   - It replaces:
     - `yardstick.py` (665 lines);
     - `compare.py` (1,627);
     - the generation-evaluation half of `evaluate.py` (2,992 in total; the fit half stays with `model.py`);
     - `errors.py` (965);
     - `agreement.py` (136);
     - the out-of-repo readers: s15/scripts has 38 files and 2,505 lines, including `read_cohort.py` and `read17.py`, plus `cohort16/scripts/truth16.py` and its pinned `pkg` snapshot.
3. **One metric set**, per cohort and per generation:
   - **M1 pair precision**: ruled `different` among co-clustered ruled pairs, with a Wilson lower bound (keeps D3's arithmetic).
   - **M2 pair recall**: co-clustered among ruled `same`.
   - **M3 group purity**: engine groups with no ruled-`different` pair inside.
   - **M4 attribution**: for every co-clustered pair and every ruled pair, the deciding rung (wall / stated fact / photo proof K-C / text proof K-B / order code K-R / learned score / demonstration) and the measurement family that carried it (ATTR, PRICE, TXT, BRK, LOC, IMG, TIME). This is the operator's ask 4, computed by the same run.
   - **M5 coverage**: number of held-out ruled pairs (positive and negative) per cohort. Below D3's n ≥ 380 the harness prints "not measurable" instead of a verdict.
   - **Plus the D83 hand read of every pair a wave gains.** Keep it; it is the only independent precision read today.
   - **Retired:**
     - certain recall (S2);
     - structural negatives as a reference (S3);
     - sealed AUC/ECE on LLM labels (S6, S10);
     - HT-weighted judge precision;
     - the seven bespoke bars. Bars 2/4/5 collapse into the D83 read; bar 6 is a special case of M2 on re-post trains; bar 7 (order shuffles) is a unit test of `cluster`, not a cohort metric.
4. **Labelling protocol**, so that M1 is measurable outside the trial:
   - a seeded random sample of co-clustered pairs per new scope, ruled blind through the existing E55 tooling (`sort=random`);
   - the D3 arithmetic sets the size: n ≥ 380 for a Wilson LB ≥ 0.99 at p = 0.995.
   - Today no scope except the trial has more than 12 negative rulings (S11, S12).

### 7.1 Subtraction ledger

| Change | Removes | Adds | Net |
|---|---|---|---|
| One ruling table (§3.2) | 3 stores (`must_not_link`, `operator_merges`, cluster-grain `verdicts`); 2 verdict values; 3 operator sources + 3 CLI weights/exclusions; 2 unique indexes; 3 duplicated route writers; G3, G5, G6, G7, G11 | 1 table, 1 view, 1 data migration | −4 structures, −5 holes |
| Judge out of the label precedence | 3 machine tiers + weights (labels.py:57-95, :245-330), HT sample machinery (labels.py:665-846), agreement.py (136) | 0 | −3 tiers, about −300 lines |
| One eval harness + one metric set | yardstick, compare, errors, evaluate's eval half, agreement, 38 s15 scripts, truth16 + pinned pkg, 7 bars, certain / structural references | 1 command, 5 metrics (M1–M5) | about −6,000 lines in the repo, −2,500 outside |
| Contract owns the facts (§5) | `floor_convention.py` (280) + `floor_camps` + 9 floor/total dials; `PLOT_TRUNCATING_SOURCES` + 2 truncation readers + `d43_plot_truncation_residue`; `FALSE_BY_OMISSION`; `DEFAULT_VOCABULARY_ATTR_KEYS`; `normalize` fact regexes (6) in favour of the platform grammar | per contract: a total_floors definition, `Cell.absence` enforced, a plot band, `agency_ref` + `unit_number` columns (2 columns) | −12+ dials, −3 per-portal tables, −1 grammar |
| Lab generations off the DB (§6) | score-lane DB writes, `keep_generations`, E916/E917 budget logic, generation-scoped review queries | none (artifacts already exist) | −1 path |

**Cost of the additions:**
- The two contract columns (`agency_ref`, `unit_number`) are the only schema additions. They pay for themselves by deleting the text mining behind K-R and the unit-designator readers: `text_facts.reference_codes` and its 133-regex module surface, `d43_unit_codes*` (4 dials) and `d43_labelled_unit_ids`. That second deletion needs its own measurement on the cohorts before it lands.
- The ruling migration is destructive (rule 1): operator OK plus a backup.

---

## 8. Open questions (need the operator or another census)

1. **The learned rung has no clean label source.**
   - Refit on operator rulings only: 1,574 positive / 246 negative, the negatives concentrated in the trial.
   - Or buy a blind operator sample (§7 point 4).
   - Or drop the learned score from the ladder: facts, then photo and text proofs, then the demonstration.
   - The judge lane as a label producer (about 4,100 lines) stands or falls with this choice.
2. **Group negatives.** 1 in g13. Accept "a group negative must be a split", or keep a set grain for it alone.
3. **DINOv3.** The export carries zero-shot CLIP tags (`export_sql.py:232-241`), not the operator's DINOv3 heads (`image_tag_scores`). One tag path is D901's open dependency decision (ONNX in the worker image, rule 7).
4. **total_floors definition.** Above-ground including the ground floor (proposed), or storey count as printed. mmreality and idnes need a per-portal read of their underground cells.
5. **Which rung owns the 189 NP-vs-patro floor conflicts** (S24) once the grammar is shared: `numeral_conflict` stays a terminal auto-reject only if it reads the platform grammar.
