# C4 — DESCRIPTIONS census (W15 W0)

Tree: `origin/main` @ `b2454fa1` (worktree `/home/hejtm/dev/sreality/.claude/worktrees/w15-c4-descriptions`, branch `census/w15-c4-descriptions`). Settings `w31`, model `w6_gold`. Pointers are `file:line` relative to the worktree unless another root is named. All counts below were re-measured on this tree; nothing is copied from earlier docs without a re-count. Scratch data: `/tmp/claude-1000/-home-hejtm-dev-sreality/e6b78d33-4258-44e0-b4ff-c53da4ed8c50/scratchpad/` (`E_class.json`, `E_table.json`, `settings_fields.json`). Experiment outputs: `/home/hejtm/autodedup-artifacts/w15/census/c4_runs/`.

---

## 1. Inventory of every written description of the engine

| # | surface | path | lines | words | bytes | live? |
|---|---|---|---:|---:|---:|---|
| 1 | Program doc | `docs/design/autodedup/PROGRAM.md` | 1,907 | 152,097 | 936,877 | yes (declares itself "source of truth", :3) |
| 2 | README | `docs/design/autodedup/README.md` | 19 | 157 | 1,203 | yes |
| 3 | Rollout report | `docs/design/autodedup/ROLLOUT.md` | 139 | 4,291 | 24,896 | yes (dated 2026-09-25) |
| 4 | Refit W7 data | `docs/design/autodedup/refit_w7_{preregistration,results}.json` | 111 + 3,847 | — | 17,184 + 84,569 | data, not description |
| 5 | Architecture, rule 15 autodedup paragraphs | `docs/architecture.md:1313-1401` (+ :1192-1196, :2577-2578) | 89 (+7) | 1,164 | — | yes |
| 6 | Real-time design, autodedup lane | `docs/design/realtime-scrapers.md:333-393` | 61 | 799 | — | yes |
| 7 | CLAUDE.md rule 15 clause (read only, not touched) | `CLAUDE.md:184-186` | 3 | — | — | yes |
| 8 | Skills (read only, not touched) | `.claude/skills/scraper-ops/SKILL.md:3,190,393-395`; `toolkit-api/SKILL.md:98-99,415`; `scraper-ops/references/pipeline-verification.md:205,269,361` | 11 | — | — | yes |
| 9 | Settings comments | `autodedup/settings.py` (1,617 lines) | 759 comment lines (718 inside `class Settings`); 171 of 307 fields carry a comment; 137 cite an E number | — | — | yes |
| 10 | Settings files | `autodedup/settings/*.json` (50) + `settings/refuted/` (4) | 15,059 | — | — | 1 live (`w31.json`), 53 historical |
| 11 | Code descriptions in `autodedup/*.py` (68 modules, 50,900 lines) | module docstrings / function+class docstrings / `#` comments | 1,684 / 4,141 / 4,786 = **10,611** | — | — | yes |
| 12 | Published page | `/home/hejtm/autodedup-artifacts/w14/prod_readiness/engine_explained.html` | 1,941 | 19,659 visible | 172,303 | operator-facing; 46 headings, 79 tables / 597 rows, 8 mermaid diagrams, 78-term glossary |
| 13 | Published page | `/home/hejtm/autodedup-artifacts/w14/prod_readiness/five_asks_2026-09-26.html` | 1,230 | 10,082 visible | 92,998 | operator-facing; 47 headings, 56 tables, 16-term glossary |
| 14 | Walkthrough (artifact, not repo) | `/home/hejtm/autodedup-artifacts/w14/operator_five/A1_engine_walkthrough.md` | 663 | — | 76,856 | source text of #12 |

Code-convention check (CLAUDE.md "no multi-paragraph docstrings (one-liners fine)"): **67 of 68** module docstrings are multi-line (max `legacy_retire.py` 61 lines, `labels_lane.py` 56, `rt_equivalence.py` 55, `indistinguishable.py` 52); **612** function/class docstrings are multi-line (3,559 lines) against 437 one-liners; `settings.py` is 47 % comments.

Frontend: 37 E-number citations in `frontend/src` (`lib/api.ts` 10, `pages/AutodedupProgress.tsx` 6, 14 other files).

Numbers cited from code: E numbers 1,398 occurrences / 222 distinct in production code (`autodedup/`, `api/`, `scraper/`, `toolkit/`), 739 occurrences / 210 distinct in tests; D numbers 200 occurrences / 33 distinct in production; M numbers 62 occurrences / 36 distinct in production (36 distinct in tests). 46 Python files cite `PROGRAM.md`. **This is why the numbers must stay resolvable — and is the only reason the ledger must survive.**

---

## 2. PROGRAM.md anatomy

| section | lines | lines # | words | bytes |
|---|---|---:|---:|---:|
| preamble + §0 what this is | 1-39 | 39 | 618 | 4,125 |
| **§1 "Rule index — the engine in one page"** | 40-441 | **402** | **49,791** | **304,119** |
| §2-§10 Q1-Q9 (design answers) | 442-678 | 237 | 6,795 | 45,018 |
| §11 Q10 realtime (designed) | 679-725 | 47 | 757 | 5,764 |
| **§11a real-time SHADOW lane W9-W9m** | 726-1188 | **463** | **13,808** | **87,541** |
| §12 progress page + validation UI | 1189-1249 | 61 | 3,737 | 24,230 |
| §13 schema, §14 wave plan | 1250-1344 | 95 | 2,967 | 20,725 |
| §15 progress ledger | 1347-1407 | 61 | 12,770 | 80,874 |
| **§15 measurements ledger** | 1408-1773 | 366 | **42,739** | **256,870** |
| **§15 decisions ledger** | 1774-1881 | 108 | **17,160** | **101,421** |
| §16 standing notes + App. A | 1882-1907 | 26 | 952 | 6,175 |

- 288 lines exceed 1,000 characters (max 5,996); mean line 491 characters, so "1,907 lines" understates the doc by ~6x against a normal 80-column doc.
- The "one page" rule index is 304 KB (about 90 printed pages).

**Ledger counts (exact).**

| series | entries | numbering | where defined |
|---|---:|---|---|
| E rules | **252** | E1-E305r and E900-E918 with gaps; 238 bulleted in §1 (+E54 in §12), 14 defined only in prose: E97a, E97b (:1062-1064), E112-E121 (:1125-1173), E128, E129 (:167, "REFUTED") | §1, §11a, §12 |
| D decisions | **91** | D1-D94 minus D35, D38, D40, D55, D56; plus D901, D902 | §15 decisions ledger |
| M measurements | **355** | M1-M907 with gaps (median row 569 chars, max 3,275) | §15 measurements ledger |
| progress rows | 53 | W0 ... W30, A1, W5 | §15 progress ledger |

**Structural defects of the doc itself (each verified).**

1. `PROGRAM.md:99` header "**Write path — designed here, exercised nowhere in this program (D4)**" files **73** rules (E38-E145 incl. E90a, E97, E110, E111); **46** of those 73 are LIVE (certificates, the lane, D43). The header is false for 63 % of what sits under it.
2. `PROGRAM.md:42` "Never renumber **E1-E254**"; rules run to E305r and E918. `README.md:5` says "numbered **E1-E44**".
3. `PROGRAM.md:4`, `README.md:10`, `ROLLOUT.md:7` ("It runs in shadow mode ... never written a merge into production data"), ruling D4 (:1783): the engine has merged in production since g12 (964 groups applied g12-g14, `TRIAL_LIVE.md:119`) and the lane merges every 60 s (migration 572).
4. **Code and doc numbers disagree** (the "never renumber" invariant is already broken): code labels `E221 E222 E223 E224` (`autodedup/settings.py:780-800`, `text_facts.py:1778,1829`, `indistinguishable.py:2169,2199,3001,3006`) are doc `E222 E223 E224 E225` (`PROGRAM.md:268-271`); doc E221 ("four guards a tenancy's second number needs", :267) has no code label. Found by matching each knob name to the rule that names it: 40 knobs match their own rule, 4 match only the next rule.
5. False data carried as rule text: E12 (:56), :518, :1330 "zero area on all **138,997** bazos rows"; field capture's hand-over (`roadmap/field-capture.md:168-169`): bazos `area_m2` present on **84.1 %** of active rows.
6. `E14` names the store `autodedup.listing_fp`; the lane uses `autodedup.rt_fp` and migration 528's `listing_fp` "nothing has ever written" (`autodedup/incremental_sql.py:695`); `fingerprint.py:1` and `normalize.py:6` still describe `listing_fp` as production.
7. `E15` "Six probes"; code runs **eight** (`blocking.py:33-35`: addr, phash, text, broker, attr_dispo, attr_area, foreign, town). `blocking.py:1` docstring also says "six optional probes".
8. Module docstrings that describe a superseded engine: `cluster.py:1` "Constrained union-find" (w31 runs `repartition`, `settings/w31.json` `repartition: true`); `decide.py:1` "The two-layer decision" (it is an 11-step ladder, A1 §7); `incremental.py:1` "The real-time SHADOW lane".
9. Numbering collisions (one label, two meanings):
   - `DECISIONS.md` "Decision N" (prod sprint, 18 items) vs `PROGRAM.md` "DN": Decision 9 = splits propose-only, D9 = backfill budget; Decision 17 = oldest survivor, D17 = judge at rollout; Decision 7 = rejects not stored, D7 = clean slate. PROGRAM.md itself cites both series (E910 "Decision 8", E911, `apply.py` "Decision 17").
   - `W5` = validation views (PROGRAM §12:1202) and `W5` = the one lane (E908-E915).
   - `F3` = the photo hold (E908, A1 §7.10) and `F3` = the co-live price contradiction "of the W14 group attack" (D49).
   - `A1` = the apply wave (E900-E906) and `A1` = the operator_five walkthrough doc.
   - `C1/C2` = sprint checkpoints (`DECISIONS.md`) and C1-C4 = this census.
   - One engine version has three names: settings `w31` = "S15b" (D94) = generation `g15`; plus model `w6_gold` and seed version `w5` (`rt_seed_version:rt`).
10. Stale outside PROGRAM.md: `docs/architecture.md:1313` "AUTODEDUP one lane (W5, **dark: interval 0**)" and :1332 "AUTODEDUP apply path (**dark**)"; `docs/design/realtime-scrapers.md:333` "ships DARK"; `.claude/skills/scraper-ops/SKILL.md:393-395` "real-time SHADOW pass ... Writes only `rt`, never a merge ... `autodedup.settings.realtime_enabled=false` stops both" (E914 deleted that switch; the lane reconciles); `ROADMAP.md:25-28` has **no AUTODEDUP row** and says "only the operator-ordered merge mechanics are live". `docs/architecture.md` DINOv3 paragraph ("Nothing has been promoted or scored yet") vs `DECISIONS.md` addendum ("the active tag-head model is DINOv3-based (v1, 11 heads, 9,514 images scored)").

---

## 3. E-rule classification (all 252)

Classes (every rule gets exactly one):
- **LIVE** — enforced by code on the w31 decision / lane / apply path. Sub-flags in the note: *dormant* (on, 0 firings in g15), *floor-camp family* (premise removed, section 5), *text stale*.
- **OFF** — code present, switched off under w31 (a deletion candidate: code + knob + doc).
- **SUPERSEDED** — replaced by a later rule (named).
- **HISTORICAL** — designed and never built, a wave's record, a defect record, or refused with numbers and no code.
- **MEASUREMENT** — judge, labels, evaluation or instrument; never on the decision path (D19: no judge arm has merge authority).

### 3.1 Summary

| class | rules | bytes of rule text in §1 | share |
|---|---:|---:|---:|
| LIVE | 184 | 204,396 | 74 % |
| OFF | 17 | 25,894 | 9 % |
| SUPERSEDED | 16 | 17,489 | 6 % |
| HISTORICAL | 15 | 9,741 | 4 % |
| MEASUREMENT | 20 | 16,872 | 6 % |
| **total** | **252** | 274,392 (prose-only rules not counted) | |

| range | LIVE | OFF | SUPERSEDED | HISTORICAL | MEASUREMENT | total |
|---|---:|---:|---:|---:|---:|---:|
| E1-E37 core design (W0-W4) | 24 | 0 | 4 | 3 | 6 | 37 |
| E38-E59 write path / judge / review UI | 10 | 3 | 5 | 1 | 3 | 22 |
| E60-E121 certificates, lane, instruments (W8-W13, W9x) | 26 | 5 | 7 | 5 | 10 | 53 |
| E128-E145 D43 facts (W14-W15) | 15 | 1 | 0 | 2 | 0 | 18 |
| E150-E305r fact readers + clustering (W16-W30) | 91 | 8 | 0 | 4 | 0 | 103 |
| E900-E918 apply + one lane (A1, W5) | 18 | 0 | 0 | 0 | 1 | 19 |

Of the 184 LIVE rules, **107 (58 %)** are the D43 stated-fact rung, its demonstration and its group-grain repartition (E61 + E130-E305r): the "stated facts" rung is most of the rulebook. Dormant among LIVE: E11 (evidence-family gate, 1 family, `evidence_gate` fired 0 times in g15), E63 (context rule, 0 promotions), E64 (its rail, nothing to re-open). Floor-camp family among LIVE: E133, E145, E154, E180, E190, E213, E272, E281, E290 (+ OFF E301, E301b, E302).

The 17 OFF rules correspond to 33 OFF booleans in w31 (`catalog_carrier_aware`, `certificate_ka_enabled`, `family_guard_ref_code_clause`, `unit_evidence_required`, `unit_rare_requires_support`, `developer_signature_guard`, `developer_signature_same_broker_only`, `developer_colive_guard`, `cluster_floor_spread`, `cluster_disposition`, and 23 `d43_*`), plus the non-boolean off modes `family_guard_mode=off`, `development_hold_mode=off`, `certificate_b_min_images=0`, `d43_price_colive_min_overlap_days=0`. Settings totals: **307 fields, 182 booleans (149 on, 33 off), 184 `d43_*` fields**; w31 overrides 201 fields, 45 of them to the default value they already had; `Settings.validate` has **126** `raise ValueError` branches (`settings.py:1085-1600`), most encoding flag-on-flag dependencies.

### 3.2 The table

| E | PROGRAM.md line | class | code pointer (w31) / superseded by | note |
|---|---|---|---|---|
| E1 | 45 | SUPERSEDED | E130 / D43 | "same physical unit" replaced by "no stated fact tells them apart" (identical units merge) |
| E2 | 46 | LIVE | guards.py:69 (wall 1) |  |
| E3 | 47 | LIVE | guards.py:71; fingerprint.py:100-104 |  |
| E4 | 48 | LIVE | apply.py:102-122 (category_type_mix / category_main_incompatible over the whole moved set) |  |
| E5 | 49 | LIVE | guards.py:74 (area wall >8%); floor/disposition walls guards.py:76-83 | 3-8% band clause re-expressed by E136 (strict 3% promotion); floor wall at >=2 rests on the pre-W8 ground-floor ambiguity |
| E6 | 50 | LIVE | features.py:228 (gap_days / same_source are features, no temporal veto) |  |
| E7 | 51 | LIVE | features.py:228 (gap_days / same_source are features, no temporal veto) |  |
| E8 | 52 | LIVE | export_sql.py:149 (no is_active filter; all-time scope inside the lane area) | no code site names it |
| E9 | 53 | LIVE | fingerprint.py:211-217; stock.py:117-186; settings catalog_df=8 |  |
| E10 | 54 | LIVE | fingerprint.py:63; features.py:1064-1100 (interior/exterior/plan ratios) |  |
| E11 | 55 | LIVE | features.py:233-244,1675; w31 min_evidence_families=1 | design said 2; evidence_gate fired 0 times in g15 = dormant |
| E12 | 56 | LIVE | normalize.py:277; features (value,present) pairs | its "zero area on all 138,997 bazos rows" is FALSE (field-capture hand-over: 84.1% present) |
| E13 | 59 | SUPERSEDED | E914 / E72 | no resolve(conn, listing_id) exists; the primitive is one bounded pass over a claim |
| E14 | 60 | LIVE | incremental_sql.py:695 (rt_fp) | text names autodedup.listing_fp; mig 528 table never written (incremental_sql.py:695) |
| E15 | 61 | LIVE | blocking.py:33-59 | text says SIX probes; code runs EIGHT (addr, phash, text, broker, attr_dispo, attr_area, foreign, town/E300) |
| E16 | 62 | LIVE | blocking.py:102-104,209-232 |  |
| E17 | 63 | LIVE | blocking.py:102-104,209-232 |  |
| E18 | 66 | HISTORICAL | - | feature catalogue ~45 / core ~25; engine now 60 |
| E19 | 67 | LIVE | features.py:1 (first citation of this E id) |  |
| E20 | 68 | LIVE | normalize.py:1; features.py TXT family |  |
| E21 | 69 | LIVE | model.py:1 (first citation of this E id) | ECE bar <=0.05 unmet (sealed 5.75%) |
| E22 | 70 | LIVE | decide.py:745-755 (t_hi 0.9788 / t_lo 0.1823) | "band = LLM judge" and "never applied (D4)" clauses superseded by D17/D19 and DECISIONS 1 |
| E23 | 71 | LIVE | decide.py:406-430 via E48 |  |
| E24 | 72 | LIVE | decide.py:190-275 (K-B, K-C) | K-A clause OFF (certificate_ka_enabled=false) |
| E25 | 74 | HISTORICAL | - | band-budget dial never built (no band_budget in code) |
| E26 | 83 | MEASUREMENT | judge.py / judge_lane.py / labels.py | label production; D19: no merge authority |
| E27 | 84 | MEASUREMENT | judge.py / judge_lane.py / labels.py | label production; D19: no merge authority |
| E28 | 85 | LIVE | export_sql.py:23-66; export.py:144,168 (PII scrub on the lane facts) |  |
| E29 | 86 | MEASUREMENT | judge.py / judge_lane.py / labels.py | label production; D19: no merge authority |
| E30 | 88 | MEASUREMENT | judge.py / judge_lane.py / labels.py | label production; D19: no merge authority |
| E31 | 89 | MEASUREMENT | judge.py / judge_lane.py / labels.py | label production; D19: no merge authority |
| E32 | 90 | MEASUREMENT | judge.py / judge_lane.py / labels.py | label production; D19: no merge authority |
| E33 | 93 | SUPERSEDED | E137 | constrained union-find path cluster.py:376-451 not run while repartition=true |
| E34 | 94 | LIVE | guards.py:180-245 | disposition/floor spread limbs OFF in w31; size cap 256 not 8 |
| E35 | 95 | HISTORICAL | - | cluster size measured in W0; now 256 |
| E36 | 96 | LIVE | cluster.py:63-100,304-305 | "attaching to the medoid" clause not in code |
| E37 | 97 | SUPERSEDED | E137 | bridge path (cluster.py:125-145) not run under repartition |
| E38 | 100 | SUPERSEDED | E900 / E901 |  |
| E39 | 101 | SUPERSEDED | E904 / E914 |  |
| E40 | 102 | SUPERSEDED | E902 |  |
| E41 | 103 | LIVE | apply.py:917; reconcile.py (3 failures / 24 h skip) |  |
| E42 | 104 | SUPERSEDED | E904 | latch never built; scope row max_clusters_per_run instead |
| E43 | 105 | HISTORICAL | - | plausibility monitor never built |
| E44 | 106 | LIVE | toolkit/property_identity.py (carry_operator_state_on_merge inside merge) |  |
| E45 | 77 | OFF | decide.py:445-453; w31 unit_evidence_required=false |  |
| E46 | 78 | OFF | decide.py:445-453; w31 developer_signature_guard=false |  |
| E47 | 79 | OFF | decide.py:445-453; w31 developer_colive_guard=false |  |
| E48 | 80 | LIVE | decide.py:406-430; w31 t_hi_by_stratum | K-A cells inert |
| E49 | 107 | LIVE | labels.py:69 (first citation of this E id) |  |
| E50 | 108 | LIVE | api/routes/autodedup.py (verdict store / split read-back / veto retraction) |  |
| E51 | 109 | LIVE | api/routes/autodedup.py (verdict store / split read-back / veto retraction) |  |
| E52 | 110 | LIVE | api/routes/autodedup.py (verdict store / split read-back / veto retraction) |  |
| E53 | 87 | MEASUREMENT | judge.py / judge_lane.py / labels.py | label production; D19: no merge authority |
| E54 | 1220 | LIVE | api/routes/autodedup.py generation resolution | amended by E914 |
| E55 | 111 | MEASUREMENT | labels_sql.py:141 | D6 blind session; D6 retired by D20 |
| E56 | 112 | LIVE | ui_sql.py:309; candidates.py (Residual candidate groups page) |  |
| E57 | 113 | SUPERSEDED | E137 | bridge path (cluster.py:125-145) not run under repartition |
| E58 | 116 | LIVE | apply_sql.py:113; score_sql.py (generation-scoped keys) |  |
| E59 | 114 | MEASUREMENT | structural_truth.py |  |
| E60 | 117 | LIVE | decide.py:244-275; features.py:863 (K-R) |  |
| E61 | 118 | LIVE | decide.py:714-720; guards.py:87-115; store_score.py:60 |  |
| E62 | 119 | LIVE | dataset.py:237-252 live_end_stamp; w31 live_window_from_sighting=true |  |
| E63 | 120 | LIVE | decide.py:456-540; hazard_context.py:297-318 | ON but 0 promotions in g15 = dormant |
| E64 | 121 | LIVE | incremental.py:1655-1708 | re-opens E63 promotions only; 0 to re-open = dormant |
| E65 | 122 | OFF | decide.py:156-187; w31 certificate_b_min_images=0 |  |
| E68 | 123 | MEASUREMENT | evaluate.py:89,2032; seals.py |  |
| E69 | 124 | MEASUREMENT | evaluate.py:89,2032; seals.py |  |
| E70 | 125 | LIVE | incremental.py:325-430 | amended by E912/E915 |
| E71 | 126 | LIVE | blocking.py:58; incremental.py:596-665 |  |
| E72 | 127 | LIVE | incremental.py:1574-1652; repartition.py:9 |  |
| E73 | 128 | LIVE | incremental_sql.py:19-1076; incremental.py:695 (150,000-pair bound) |  |
| E74 | 129 | LIVE | incremental_sql.py:19-1076; incremental.py:695 (150,000-pair bound) |  |
| E75 | 130 | LIVE | incremental_sql.py:19-1076; incremental.py:695 (150,000-pair bound) |  |
| E76 | 131 | LIVE | incremental_sql.py:19-1076; incremental.py:695 (150,000-pair bound) |  |
| E77 | 132 | LIVE | incremental_sql.py:19-1076; incremental.py:695 (150,000-pair bound) |  |
| E78 | 134 | LIVE | incremental.py:37,1417-1440 (idle pass re-reads rulings) |  |
| E79 | 133 | LIVE | incremental_scope.py; incremental_sql.py:99 |  |
| E80 | 135 | SUPERSEDED | E914 | residue live: an unseeded pass skips |
| E81 | 136 | LIVE | incremental_lane.py (entrant snapshot walk 1 h / 6 h) |  |
| E82 | 137 | SUPERSEDED | E914 |  |
| E83 | 138 | OFF | stock.py; w31 catalog_carrier_aware=false; lane refuses it (incremental.py:994-1035) |  |
| E84 | 139 | LIVE | features.py:1317; settings.py:27 (1-minute K-B gap) |  |
| E85 | 140 | OFF | family.py; w31 family_guard_mode=off; lane refuses it |  |
| E86 | 141 | OFF | family.py; w31 family_guard_mode=off; lane refuses it |  |
| E87 | 142 | SUPERSEDED | E110 | Settings.validate history |
| E88 | 143 | OFF | development.py; w31 development_hold_mode=off; lane refuses it |  |
| E89 | 144 | SUPERSEDED | E110 | Settings.validate history |
| E90 | 149 | HISTORICAL | rt_equivalence.py:8 | W9g defect record |
| E90a | 150 | LIVE | incremental_lane.py:424 (scorer read back from the calibration row) |  |
| E91 | 151 | SUPERSEDED | E912 |  |
| E92 | 152 | LIVE | incremental.py:157-235; incremental_sql.py:796 | E93 amended by E908 |
| E93 | 153 | LIVE | incremental.py:157-235; incremental_sql.py:796 | E93 amended by E908 |
| E94 | 154 | SUPERSEDED | E912 |  |
| E95 | 155 | SUPERSEDED | E912 / E915 |  |
| E96 | 156 | LIVE | fingerprint.py:212-214,267 (stock share only when every population measured) |  |
| E97 | 159 | LIVE | incremental_lane.py:2137-2284 (rt_seed fresh=true) |  |
| E97a | 1062 | HISTORICAL | - | W9j defect records |
| E97b | 1064 | HISTORICAL | - | W9j defect records |
| E98 | 160 | LIVE | incremental_sql.py:1236 (bootstrap phase) |  |
| E99 | 161 | MEASUREMENT | rt_equivalence.py |  |
| E110 | 157 | HISTORICAL | settings.py:96,215 | validator record; live behaviour = E62 + E84 |
| E111 | 158 | MEASUREMENT | revocation.py |  |
| E112 | 1131 | MEASUREMENT | rt_equivalence.py / replay.py / incremental_store.py |  |
| E113 | 1127 | HISTORICAL | - | W9l drift record |
| E114 | 1157 | MEASUREMENT | rt_equivalence.py / replay.py / incremental_store.py |  |
| E115 | 1165 | LIVE | store_score.py:1-40 (pairs.score double precision) |  |
| E116 | 1167 | MEASUREMENT | rt_equivalence.py / replay.py / incremental_store.py |  |
| E117 | 1169 | LIVE | score_lane.pair_params / score_sql.PAIR_UPSERT_SQL write certificate; cluster.py:109 |  |
| E118 | 1169 | LIVE | score_lane.pair_params / score_sql.PAIR_UPSERT_SQL write certificate; cluster.py:109 |  |
| E119 | 1171 | MEASUREMENT | rt_equivalence.py / replay.py / incremental_store.py |  |
| E120 | 1173 | MEASUREMENT | rt_equivalence.py / replay.py / incremental_store.py |  |
| E121 | 1125 | MEASUREMENT | rt_equivalence.py / replay.py / incremental_store.py |  |
| E128 | 167 | HISTORICAL | - | REFUTED in prose, never bulleted |
| E129 | 167 | HISTORICAL | - | REFUTED in prose, never bulleted |
| E130 | 162 | LIVE | decide.py:543-589; w31 d43_gate=true |  |
| E131 | 163 | LIVE | indistinguishable.py:3187-3246 (warrant) |  |
| E132 | 164 | LIVE | d43.py:39-138 |  |
| E133 | 165 | LIVE | floor_convention.py; indistinguishable.py:2657-2680 | PREMISE REMOVED: field-capture W8 made listings.floor ground=0 everywhere; w31 floor_camps still says sreality/realitymix/mmreality/remax/bezrealitky=1 (measured, see section 5) |
| E134 | 166 | LIVE | indistinguishable.py:286 (price path) | co-live limb OFF (d43_price_colive_contradiction=false) |
| E135 | 167 | LIVE | text_facts.py:1333 (first citation of this E id) |  |
| E136 | 168 | LIVE | d43.py:75 (first citation of this E id) |  |
| E137 | 169 | LIVE | cluster.py:222 (first citation of this E id) |  |
| E138 | 170 | LIVE | indistinguishable.py:351 (first citation of this E id) |  |
| E139 | 171 | LIVE | score_lane.py:596,798 (reads autodedup.must_not_link; reports the count by source) |  |
| E140 | 172 | LIVE | text_facts.py:460 (first citation of this E id) |  |
| E141 | 173 | LIVE | text_facts.py:607 (first citation of this E id) |  |
| E142 | 174 | LIVE | text_facts.py:642 (first citation of this E id) |  |
| E143 | 175 | LIVE | indistinguishable.py:480 (first citation of this E id) |  |
| E144 | 176 | OFF | indistinguishable.py:321; w31 d43_price_colive_min_overlap_days=0, d43_price_colive_contradiction=false |  |
| E145 | 177 | LIVE | indistinguishable.py:399; body_align.py:120 | floor-camp family, premise removed by field-capture W8 |
| E150 | 205 | LIVE | body_align.py:1 (first citation of this E id) |  |
| E151 | 206 | LIVE | body_align.py:133 (first citation of this E id) |  |
| E152 | 207 | LIVE | indistinguishable.py:565 (first citation of this E id) |  |
| E153 | 208 | LIVE | text_facts.py:821 (first citation of this E id) |  |
| E154 | 209 | LIVE | indistinguishable.py:389 (first citation of this E id) | floor-camp family (feed convention) |
| E155 | 210 | LIVE | text_facts.py:486 (first citation of this E id) |  |
| E156 | 211 | LIVE | repartition.py:93 (first citation of this E id) |  |
| E157 | 212 | LIVE | d43.py:7 (first citation of this E id) |  |
| E158 | 213 | LIVE | demonstrate.py:1 (first citation of this E id) |  |
| E159 | 214 | LIVE | demonstrate.py:18 (first citation of this E id) |  |
| E160 | 218 | LIVE | demonstrate.py:154 (first citation of this E id) |  |
| E161 | 219 | LIVE | text_facts.py:160 (first citation of this E id) |  |
| E162 | 220 | LIVE | demonstrate.py:493 (first citation of this E id) |  |
| E163 | 221 | LIVE | body_align.py:69 (first citation of this E id) |  |
| E164 | 222 | LIVE | demonstrate.py:400 (first citation of this E id) |  |
| E165 | 223 | LIVE | indistinguishable.py:499 (first citation of this E id) |  |
| E166 | 224 | LIVE | text_facts.py:230 (first citation of this E id) | storey reader (camp-independent) |
| E180 | 230 | LIVE | floor_convention.py:90 | floor-camp family, premise removed by field-capture W8 |
| E181 | 231 | LIVE | text_facts.py:314 (first citation of this E id) |  |
| E182 | 232 | LIVE | features.py:504 (first citation of this E id) |  |
| E183 | 233 | LIVE | text_facts.py:421 (first citation of this E id) |  |
| E184 | 234 | LIVE | settings.py:627 (first citation of this E id) |  |
| E185 | 235 | LIVE | indistinguishable.py:796 (first citation of this E id) |  |
| E186 | 236 | OFF | text_facts.py:893; w31 d43_offer_area=false (REFUSED) |  |
| E190 | 242 | LIVE | floor_convention.py:150; w31 d43_total_floors_camp=true | floor-camp family: with the heal it forgives a REAL one-storey total_floors gap between a "camp 1" and a "camp 0" portal |
| E191 | 243 | LIVE | indistinguishable.py:3219; text_facts.py:2385; d43.py:73 |  |
| E192 | 244 | LIVE | indistinguishable.py:3219; text_facts.py:2385; d43.py:73 |  |
| E193 | 245 | LIVE | indistinguishable.py:3219; text_facts.py:2385; d43.py:73 |  |
| E200 | 249 | OFF | body_align.py:349; w31 d43_agency_code_conflict=false (REFUTED) |  |
| E201 | 250 | LIVE | text_facts.py:239 (first citation of this E id) |  |
| E202 | 251 | LIVE | text_facts.py:1085 (first citation of this E id) |  |
| E203 | 252 | LIVE | text_facts.py:1042 (first citation of this E id) |  |
| E204 | 253 | LIVE | text_facts.py:1113 (first citation of this E id) |  |
| E210 | 255 | LIVE | text_facts.py:394 (first citation of this E id) |  |
| E211 | 256 | LIVE | indistinguishable.py:1132 (first citation of this E id) |  |
| E212 | 257 | LIVE | text_facts.py:1157 (first citation of this E id) |  |
| E213 | 258 | LIVE | indistinguishable.py:399 (floor_same_source_feed=firm) | floor-camp family |
| E214 | 259 | LIVE | text_facts.py:1209 (first citation of this E id) |  |
| E215 | 260 | LIVE | text_facts.py:1235 (first citation of this E id) |  |
| E216 | 261 | LIVE | text_facts.py:1269 (first citation of this E id) |  |
| E217 | 262 | LIVE | text_facts.py:1332 (first citation of this E id) |  |
| E218 | 263 | LIVE | text_facts.py:1377 (first citation of this E id) |  |
| E219 | 264 | LIVE | text_facts.py:1444 (first citation of this E id) |  |
| E220 | 266 | LIVE | text_facts.py:1536 (first citation of this E id) |  |
| E221 | 267 | LIVE | settings.py:744-751 (d43_rental_colive_honest_clock, _charge_rent_multiple, _services_below_rent, _charge_requires_equal_rent; commented under E220, unlabelled) | CODE LABELS DRIFT: the code label E221 (text_facts.py:1701) is doc E222 |
| E222 | 268 | LIVE | text_facts.py:1829-1918; settings.py:780-800 | CODE LABELS DRIFT: code E221..E224 = doc E222..E225; doc E221 has no code label |
| E223 | 269 | LIVE | text_facts.py:1829-1918; settings.py:780-800 | CODE LABELS DRIFT: code E221..E224 = doc E222..E225; doc E221 has no code label |
| E224 | 270 | LIVE | text_facts.py:1829-1918; settings.py:780-800 | CODE LABELS DRIFT: code E221..E224 = doc E222..E225; doc E221 has no code label |
| E225 | 271 | LIVE | text_facts.py:1829-1918; settings.py:780-800 | CODE LABELS DRIFT: code E221..E224 = doc E222..E225; doc E221 has no code label |
| E226 | 272 | LIVE | indistinguishable.py:2132 (first citation of this E id) |  |
| E230 | 273 | LIVE | text_facts.py:1918 (first citation of this E id) |  |
| E231 | 274 | LIVE | text_facts.py:1960 (first citation of this E id) |  |
| E240 | 276 | LIVE | text_facts.py:1991 (first citation of this E id) |  |
| E241 | 277 | LIVE | indistinguishable.py:1726 (first citation of this E id) |  |
| E242 | 278 | LIVE | indistinguishable.py:1828 (first citation of this E id) |  |
| E243 | 279 | LIVE | indistinguishable.py:3097 (first citation of this E id) |  |
| E244 | 281 | LIVE | text_facts.py:1508 (first citation of this E id) |  |
| E250 | 286 | LIVE | text_facts.py:2136 (first citation of this E id) |  |
| E251 | 287 | LIVE | text_facts.py:2227 (first citation of this E id) |  |
| E252 | 288 | LIVE | text_facts.py:2191 (first citation of this E id) |  |
| E253 | 289 | LIVE | cluster.py:258 (first citation of this E id) |  |
| E254 | 290 | LIVE | indistinguishable.py:1646 (first citation of this E id) |  |
| E260 | 295 | LIVE | text_facts.py:1479 (first citation of this E id) |  |
| E261 | 296 | LIVE | indistinguishable.py:1456 (first citation of this E id) |  |
| E262 | 297 | LIVE | repartition.py:387 (first citation of this E id) |  |
| E263 | 298 | LIVE | repartition.py:177 (first citation of this E id) |  |
| E264 | 299 | LIVE | d43.py:100 (first citation of this E id) |  |
| E270 | 305 | LIVE | text_facts.py:2270 (first citation of this E id) |  |
| E271 | 306 | LIVE | indistinguishable.py:715 (first citation of this E id) |  |
| E272 | 307 | LIVE | indistinguishable.py:715 (first citation of this E id) | floor-camp family (sequential one-storey excuse) |
| E273 | 308 | LIVE | indistinguishable.py:800 (first citation of this E id) |  |
| E274 | 309 | LIVE | indistinguishable.py:1939 (first citation of this E id) |  |
| E275 | 310 | LIVE | indistinguishable.py:792 (first citation of this E id) |  |
| E276 | 311 | OFF | indistinguishable.py:1971; w31 d43_agency_code_colive_price=false (REFUSED) |  |
| E277 | 312 | LIVE | indistinguishable.py:811 (first citation of this E id) |  |
| E280 | 376 | LIVE | indistinguishable.py / demonstrate.py / features.py:656 |  |
| E281 | 377 | LIVE | indistinguishable.py / demonstrate.py / features.py:656 | floor-camp family (conventions taken out) |
| E282 | 378 | LIVE | indistinguishable.py / demonstrate.py / features.py:656 |  |
| E283 | 379 | LIVE | indistinguishable.py / demonstrate.py / features.py:656 |  |
| E284 | 380 | LIVE | indistinguishable.py / demonstrate.py / features.py:656 |  |
| E285 | 381 | LIVE | indistinguishable.py / demonstrate.py / features.py:656 |  |
| E286 | 382 | LIVE | indistinguishable.py / demonstrate.py / features.py:656 |  |
| E287 | 383 | LIVE | indistinguishable.py / demonstrate.py / features.py:656 |  |
| E288 | 384 | LIVE | indistinguishable.py / demonstrate.py / features.py:656 |  |
| E289 | 385 | LIVE | indistinguishable.py / demonstrate.py / features.py:656 |  |
| E290 | 386 | LIVE | indistinguishable.py / demonstrate.py / features.py:656 | floor-camp family (same-feed one-storey) |
| E291 | 387 | LIVE | indistinguishable.py / demonstrate.py / features.py:656 |  |
| E292 | 388 | HISTORICAL | - | REFUSED with numbers, no code |
| E293 | 402 | LIVE | text_facts.py:2382-2542 |  |
| E294 | 403 | LIVE | text_facts.py:2382-2542 |  |
| E295 | 404 | LIVE | text_facts.py:2382-2542 |  |
| E296 | 405 | OFF | text_facts.py:2606; w31 d43_named_villa=false (REFUSED, "kept in code, off in every table") |  |
| E297 | 406 | HISTORICAL | - | REFUSED with numbers, no code |
| E298 | 407 | HISTORICAL | - | REFUSED with numbers, no code |
| E299 | 415 | LIVE | lane.py:142; labels_lane; yardstick.py; incremental_sql.py:1094-1104 |  |
| E300 | 426 | LIVE | blocking.py:17,42-59; w31 attr_probe_town_grain=true |  |
| E301 | 427 | OFF | indistinguishable.py:2360-2368; w31 d43_floor_total_camp_shift(_mixed)=false (REFUSED) | floor-camp family |
| E301b | 428 | OFF | indistinguishable.py:2360-2368; w31 d43_floor_total_camp_shift(_mixed)=false (REFUSED) | floor-camp family |
| E302 | 429 | OFF | indistinguishable.py:2383; w31 d43_total_floors_agreeing_unit=false (REFUSED) | floor-camp family |
| E303 | 430 | OFF | d43.py:58; w31 d43_cluster_price_kc_house_number=false |  |
| E304 | 432 | HISTORICAL | - | REFUSED with numbers, no code |
| E305 | 434 | LIVE | text_facts.py:2493; indistinguishable.py:1248 |  |
| E305r | 438 | LIVE | text_facts.py:2493; indistinguishable.py:1248 |  |
| E900 | 181 | LIVE | apply_sql.py:1 (first citation of this E id) |  |
| E901 | 182 | LIVE | apply.py:12 (first citation of this E id) |  |
| E902 | 183 | LIVE | apply.py / apply_sql.py (planning + chokepoint) |  |
| E903 | 184 | LIVE | apply_sql.py:68 (first citation of this E id) |  |
| E904 | 185 | LIVE | apply_sql.py:39 (first citation of this E id) |  |
| E905 | 186 | LIVE | apply_sql.py:152 (first citation of this E id) |  |
| E906 | 187 | LIVE | apply_sql.py:1 (first citation of this E id) |  |
| E907 | 188 | LIVE | legacy_retire.py:20 | temporary scaffold, "kept until W8" (DECISIONS) |
| E908 | 191 | LIVE | incremental.py / incremental_lane.py / reconcile.py / cluster.py:28 / score_sql.py:260 |  |
| E909 | 192 | LIVE | incremental.py / incremental_lane.py / reconcile.py / cluster.py:28 / score_sql.py:260 |  |
| E910 | 193 | LIVE | incremental.py / incremental_lane.py / reconcile.py / cluster.py:28 / score_sql.py:260 |  |
| E911 | 194 | LIVE | incremental.py / incremental_lane.py / reconcile.py / cluster.py:28 / score_sql.py:260 |  |
| E912 | 195 | LIVE | incremental.py / incremental_lane.py / reconcile.py / cluster.py:28 / score_sql.py:260 |  |
| E913 | 196 | LIVE | incremental.py / incremental_lane.py / reconcile.py / cluster.py:28 / score_sql.py:260 |  |
| E914 | 197 | LIVE | incremental.py / incremental_lane.py / reconcile.py / cluster.py:28 / score_sql.py:260 |  |
| E915 | 198 | LIVE | incremental.py / incremental_lane.py / reconcile.py / cluster.py:28 / score_sql.py:260 |  |
| E916 | 199 | LIVE | incremental.py / incremental_lane.py / reconcile.py / cluster.py:28 / score_sql.py:260 |  |
| E917 | 200 | LIVE | incremental.py / incremental_lane.py / reconcile.py / cluster.py:28 / score_sql.py:260 |  |
| E918 | 201 | MEASUREMENT | rt_equivalence.py; incremental_sql.py:1294 |  |

---

## 4. D and M ledgers

**D (91 rows), first-pass classes.**

| class | n | ids |
|---|---:|---|
| STANDING (binding today) | 58 | D2 D5 D7 D8 D10 D14 D17 D19 D20 D27 D34 D37 D39 D41 D43 D44 D47 D48 D49 D50 D51 D52 D53 D54 D57 D58 D59 D60 D61 D62 D63 D64 D66 D67 D68 D69 D70 D71 D73 D74 D75 D76 D78 D79 D80 D81 D82 D83 D84 D85 D86 D87 D88 D89 D90 D93 D901 D902 |
| OPEN | 1 | D92 (developer-catalogue pairs with no unit fact) — D902 is also "for the operator to confirm" |
| SUPERSEDED | 9 | D3 (by D11/E68, D43) · D4 (by DECISIONS 1) · D6 (by D20) · D15 (by D17/D19) · D16, D18 (models/settings since promoted) · D26 (by E914) · D33 (by E912) · D65 (by DECISIONS 2; hold table empty in w31) |
| HISTORICAL (wave outcome / promotion record) | 23 | D1 D9 D11 D12 D13 D21 D22 D23 D24 D25 D28 D29 D30 D31 D32 D36 D42 D45 D46 D72 D77 D91 D94 |

32 of the 58 STANDING rows (D57-D90) are rulings on single fact readers: each belongs as ONE line beside its reader in the ladder's rung 2, not as a 778-character median ledger cell.

**M (355 rows)**: all measurements, all historical by nature. Only 36 distinct M ids are cited by production code and 36 by tests; the rest are read by nobody but a human auditing a wave.

**Progress ledger (53 rows, 80,874 B)**: wave history; duplicated by `/autodedup/progress` (read from `autodedup.iterations`, PROGRAM.md:1349).

---

## 5. Validated assumption: the floor "camp" premise is half gone from the data

The E133 family (E133, E145, E154, E180, E190, E213, E272, E281, E290; OFF: E301, E301b, E302) and `autodedup/floor_convention.py` (280 lines) rest on "some portals count the ground floor as 1". w31 still carries `floor_camps = {sreality: 1, realitymix: 1, mmreality: 1, remax: 1, bezrealitky: 1, idnes: 0, ceskereality: 0}` and `floor_camps_reads = joint` (`settings/w31.json`). Field capture W8 ("floor: ground = 0 everywhere", `roadmap/field-capture.md:123-131`, hand-over `docs/design/field-capture/handover-autodedup-floor.md`) converted the stored floors.

Measured on the trial export the live lane reads (`score_g15/.../artifact/cohort.jsonl.gz`, exported 2026-09-26 12:38Z): every cross-portal `byt` pair the g15 run certified (K-C or K-R) with both floors stated, floor(portal) − floor(idnes):

| portal | w31 camp | pairs | Δ = −1 | Δ = 0 | Δ = +1 | mean Δ |
|---|---:|---:|---:|---:|---:|---:|
| sreality | 1 | 382 | 8 | 346 | 28 | **+0.05** |
| realitymix | 1 | 130 | 4 | 126 | 0 | **−0.03** |
| mmreality | 1 | 38 | 0 | 37 | 1 | **+0.03** |
| remax | 1 | 16 | 0 | 16 | 0 | **0.00** |
| bezrealitky | 1 | 15 | 0 | 15 | 0 | **0.00** |
| ceskereality | 0 | 177 | 1 | 162 | 14 | +0.07 |
| bazos | — | 81 | 5 | 55 | 21 | +0.20 |

Before the heal the hand-over measured +0.973 (sreality), +0.962 (realitymix), +1.050 (remax), +0.988 (mmreality), +0.869 (bezrealitky). **`floor` is now one convention everywhere; the camp table is wrong for `floor` on all five "camp 1" portals.**

The same read for `total_floors` (certified cross-portal pairs of any kind, both counts stated), total(portal) − total(idnes):

| portal | w31 camp | pairs | Δ = −1 | Δ = 0 | Δ = +1 | Δ = +2 | mean Δ |
|---|---:|---:|---:|---:|---:|---:|---:|
| mmreality | 1 | 38 | 0 | 4 | 33 | 1 | **+0.92** |
| sreality | 1 | 315 | 1 | 267 | 45 | 2 | +0.15 |
| realitymix | 1 | 131 | 0 | 119 | 12 | 0 | +0.09 |
| bezrealitky | 1 | 9 | 0 | 8 | 1 | 0 | +0.11 |
| remax | 1 | 20 | 0 | 20 | 0 | 0 | 0.00 |
| bazos | — | 35 | 2 | 33 | 0 | 0 | −0.06 |

**`total_floors` is NOT unified at the source**: mmreality still counts one more storey than idnes on 33 of 38 duplicates, and a minority of sreality (45 / 315 = 14 %) and realitymix (12 / 131 = 9 %) adverts do too. The camp table encodes one offset for both columns (E133 "moves `floor` and `total_floors` together or not at all"), and that coupling is what the data no longer has: the floor half is dead, the storey-count half is right for one portal and a minority of two others. The honest home of this fact is the portal contract (field capture), not an engine table. The floor fact itself is read raw under `floor_camps_reads='joint'` (`indistinguishable.py:2413`), so the live effect is confined to the `total_floors` excuses (`joint_convention_shift`, E190's `total_convention_shift`, `indistinguishable.py:2662-2672`) — measured in 5b.

### 5b. Measured: what the camp table does on the trial

Harness on `origin/main` @ `b2454fa1`, model w6_gold, trial export, no rulings file. Runs under `/home/hejtm/autodedup-artifacts/w15/census/c4_runs/`: `trial_w31` (baseline; reproduces g15's pair decisions exactly: zones 7,971 / 1,533 / 57,260 / 19, certificates 173 / 1,570 / 2,139, all five wall counts), `trial_w31_nocamps` (`floor_camps = {}`, `floor_camps_reads = off`, `d43_total_floors_camp = false`: the machinery deleted; settings `w31_nocamps.json`), `trial_w31_camps0` (every portal camp 0 = the table the floor heal implies; `w31_camps0.json`). Diffs: `trial_*/cmp_vs_w31.json`; script `scratchpad/cmp_runs.py`. Operator labels: `labels_g13_36225845749/autodedup-labels-36225845749/operator_labels.jsonl` (1,820 pair verdicts). Each run 197-220 s wall, 0.89 GB peak.

| arm | pair zone moves vs w31 | which pairs | stored pairs co-grouped (w31: 7,816) | operator verdicts on moved pairs |
|---|---|---|---|---|
| machinery deleted | 58 merge → band; 4 band → merge | all have `total_floors` 1 apart. The 58 are promotions `d43_promote:agree:2` (idnes × sreality 39, × mmreality 12, × realitymix 6; bazos × sreality 1) that the strict reading now refuses; the 4 are gate demotions (`...:d43_gate:total_floors`) that vanish because with no table every portal pair is "ambiguous" at the lenient gate (`d43_gate_total_floors_slack`) | 7,815 (9 separated, 8 joined) | 1 same, 0 different |
| table set to the healed floor (all camps 0) | **226 merge → band** | all `total_floors` 1 apart: **90 K-C photo certificates (89 with the SAME floor)**, 79 model merges, 57 promotions; idnes × sreality 123, × mmreality 78, × realitymix 24, bezrealitky × idnes 1 | **7,402 (418 separated, 4 joined)** | 10 same, 0 different |

Reading: the `floor` half of the camp table is dead (section 5); the `total_floors` half is load-bearing, and it compensates a **source defect**: the portals' storey COUNT is still two conventions (mmreality +1 on 33 of 38 certified duplicates, sreality +1 on 14 %), and 89 photo-certified same-floor duplicates would split without the excuse. Deleting the machinery alone is not neutral either: it makes the same one-storey count gap lenient at the gate and strict at promotion — the "three readings" inconsistency (section 6) in one measurement. The subtraction that satisfies the north star is ordered: (1) the portal contract states `total_floors` in one convention (field capture's lane, the same shape as its W8 floor fix, then its backfill); (2) then `floor_convention.py` (280 lines), `floor_camps`, `floor_camps_reads`, the ~16 camp-dependent knobs and the 12 E rules are deleted, measured on cohort 17 and confirmed on 18. An engine-side re-tune before (1) would be patchwork over a data defect.

---

## 6. Concepts a reader must hold

- Glossary union of the three operator texts (A1 36 terms, engine_explained 78, five_asks 16), after merging obvious synonyms: **80** concepts.
- Concepts PROGRAM.md additionally requires (not in any glossary): **74** — seal, spent seal, arm, stratum, cell, tier, honest clock, camp, feed convention, train, carrier, limb, dial, shed, rejoin, heal, twin, parity gate, digest, census, confirmation, wave, S-number, W-number, g-number, w-settings file, certain duplicate, structural truth, residual, hazard context, context rule, development vocabulary, band budget, yardstick, closure, iteration, pre-registration, addendum, sealed test, holdout, ECE, Wilson bound, isotonic, exploded key, fan-out, watermark, settle lag, entrant, revive, straggler, deadline, re-cut, calibration digest, seed version, store floor, propose-only, proposal, split, detach, origin, asset link, unapply, evidence_pending, F3, G1-G3 gates, C1-C2 checkpoints, Decision-N vs DN, EN, MN, the three readings (gate / strict / cluster), warrant modes, development context.
- **Total ≈ 154 distinct concepts.**

Occurrences in PROGRAM.md of a sample (case-insensitive substring; `arm`/`cell` overcount): cohort 965, hold 400, label 381, generation 330, band 327, seal 314, certain 286, scope 277, seed 256, judge 250, gate 184, family 169, certificate 156, guard 152, replay 147, rail 122, limb 118, conflict 114, zone 109, feature 107, probe 101, calibration 97, tier 96, reader 96, co-live 91, dial 85, honest clock 79, shed 74, carrier 73, veto 71, parity 69, camp 58, stratum 56, invariant 56, yardstick 15, closure 14, wall 7.

**One concept, several names** (each cluster is a unification target):

| concept | names in use |
|---|---|
| hard stop on a stored column | wall, guard, G1/G2, rule floor, hard reject, veto (`guard:` reason prefix), "pair_veto" |
| a stated difference | stated fact, D43 fact, distinguishing fact, reader, fact reader, limb, dial, gate reason |
| proof by itself | certificate, proof, K-A/K-B/K-C/K-R, short-circuit |
| set of adverts = one home | group, cluster, component, cell, closure (must-link set), family (K-B family), property |
| pair verdict | zone, decision, verdict, reason string (38 distinct strings in g15, 8 prefixes) |
| "look at this" | band, maybe, residual, propose-only, evidence_pending hold |
| photo wait | hold, photo hold, evidence hold, F3, `evidence_pending`, `held_zone` |
| the live answer set | `rt`, live stream, real-time generation, the lane's generation |
| one engine version | settings `w31`, "S15b", generation `g15`, model `w6_gold`, seed version `w5` |
| operator "different" | different ruling, negative verdict, must-not-link, never merge, operator veto, `same_building_different_unit`, `same_project_different_unit` |
| operator "same" | same ruling, must-link, Browse merge, `browse_merge` provenance, closure |

**One name, several concepts**: band (zone / area band / price band / simhash band / `band_bits`), family (evidence family / K-B family), veto (zone / wall / unit-name must-not-link), cell (stratum cell / repartition cell / measurement cell D70), W5, F3, A1, C1/C2, Decision-N/DN (section 2, defect 9).

**The same fact compared many times, at different tolerances.** Area alone is compared at: blocking band (±1 step of ~25 %, `area_band_tol` 0.20), the wall (8 %, `area_reject_pct`), model features (`area_rel_diff`, `area_equal`), the ATTR corroboration family (exact / 1 % / 2 %), K-A 2 %, K-B 1 %, K-C 3 % (`decide.py:69-74`), the context rule 1 % (`context_rule_area_max`), the D43 gate 8 % (`d43_gate_area_tol`), D43 strict promotion 3 % (`area_band_pct`), the two-unit signature 0.5 % (`d43_two_unit_area_tol`), plan rows 3 % (`d43_plan_area_tol`), demonstration (A) "equal within rounding" (printed decides), the warrant ("stated by both"), and the cluster invariant 8 % unless both print one figure (E280) — **15 comparison sites, 8 distinct tolerances, 30 `area` settings fields**. Price has the same shape (cross-portal 5 %, same-portal 60 %, context 0.5 %, cluster limb 20 %, path tolerance, per-m² 0.1 %, two-unit, demonstration "exact or on the path").

---

## 7. The ONE description that replaces the prose

### 7.1 Files

| file | role | size target | replaces |
|---|---|---|---|
| `docs/design/autodedup/ENGINE.md` | THE live description: north star, the ladder table, the runtime table, the operator-voice table, the explanation-table spec, the glossary | ≤ 300 lines, ≤ 6,000 words | PROGRAM.md §0-§14, §16, App. A; ROLLOUT.md; README.md; architecture.md:1313-1401 (→ a 10-line contract + pointer); realtime-scrapers.md:333-393 (→ 8 lines) |
| `docs/design/autodedup/LEDGER.md` | append-only, never renumbered: one line per E, D (and per M that code or ENGINE.md cites) — `| id | class | one-sentence rule | code pointer | superseded by | measured by |` | 252 E + 91 D + ≤ 72 M ≈ 415 lines, ≤ 60 KB | PROGRAM.md §1 rule index, §15 ledgers |
| history | the verbatim PROGRAM.md, ROLLOUT.md, refit JSONs are deleted from the tree; `LEDGER.md` line 1 pins the last commit that holds them (git history is the archive) | 0 | PROGRAM.md §11a, §15 progress ledger, every "What SN does NOT fix" block |

Rules for LEDGER.md: a new rule appends a line; a changed meaning appends a new id and flips the old line's class to SUPERSEDED with the new id; nothing is edited in place except that status column. The code-vs-doc drift of E221-E225 (section 2, defect 4) is resolved in LEDGER by recording the code's numbering as the truth and marking doc E221 "unlabelled in code" (the code and tests cite the numbers; the doc must follow them).

### 7.2 The ladder table (the core of ENGINE.md)

North star (as drafted by the coordinator, unchanged in substance): every merge is one decision on one evidence ladder — stated facts, then photo proofs, then the learned score — computed by one code path for the lane, the harness and the reports, and explained in one table.

Pair rungs, in order. The first rung that answers ends the pair.

| rung | name | input | rule (w31 values) | replaces (E / D) | measurement (g15, run 36244048665) |
|---|---|---|---|---|---|
| 0 | Candidates | one fingerprint row per advert | 8 probes (addr, phash, text, broker, attr_dispo, attr_area, foreign, town); key cap 200; ≤ 60 per advert in probe priority; rung 1's column walls checked before a slot is spent | E14-E17, E71, E300; E13 (superseded) | 66,783 pairs; 507 adverts at cap; 161 with none; 2 keys exploded |
| 1 | Stated facts | both adverts' columns and texts; ONE reader set, ONE reading | deal type, kind (dům↔komerční allowed), area > 8 %, disposition (land exempt), flat floor ≥ 2, unit name, and the D43 readers: any stated difference → **not the same** (reject; a unit-name difference is also a must-not-link). An operator must-link (rung 5) is the only override. | walls E2-E5, E34 (pair half); E61; the two auto-reject limbs (`attr_contradictions ≥ 3`, `numeral_conflict`; `decide.py:433-442`; defined only in prose at PROGRAM.md:555, **no E number**); D43 gate E130, E136, E138 and strict E131 readings collapsed into one; readers E132-E305r; D43, D44, D47-D90 as reader scopes | today split over 5 places: 525,783 stopped at candidate time; 19 vetoes; 19,715 auto-rejects (12,967 contradictions + 6,748 numeral); 92 gate demotions; 406 demonstration refusals; 260 group refusals on a stated fact |
| 2 | Photo / text proofs | pair features | K-R shared rare order code; K-B same broker re-post never co-live (1-minute gap); K-C ≥ 4 tight non-stock photos in gallery order, area ≤ 3 %, same disposition, stock ≤ 20 % → **merge** | E24, E60, E84, E9, E10; K-A (OFF) | K-R 173, K-B 1,570, K-C 2,139 |
| 3 | Learned score | 59 features, `w6_gold` | ≥ 0.9788 → merge; > 0.1823 → band; else reject. A band pair merges only when **demonstrated**: (A) area, disposition, price, town positively agree and (B) one unit-grade evidence | E21, E22, E23, E48, E131 warrant, E157-E165, E191, D50, D53 | model merges 1,942; promotions 2,192; band 1,533 (model 1,035 + refusals 406 + demotions 92) |
| — | Lane timing (not a rung) | gallery completeness | a merge resting on photos is written `band / evidence_pending` until every frame has pHash + CLIP + tags, ≤ 48 h | E92, E93, E908, D34 | 118 of 563 live-vs-g15 differing pairs were holds (`TRIAL_LIVE.md:194`) |

Group rungs:

| rung | name | input | rule | replaces | measurement (g15) |
|---|---|---|---|---|---|
| 4 | Group | merge pairs | components of merge pairs, contracted to must-link closures; each component checked WHOLE with rung 1's reader at group grain (plus the group price limb); a failing component is re-partitioned into maximal consistent sub-groups; identity = smallest advert id | E33 (superseded), E36, E37/E57 (superseded), E72, E132, E137, E156, E157 cluster limb, E193, E253, E262-E264, E280, E910 | 1,133 groups from 7,971 merge pairs; 265 refused joins (fact 260, area spread 3, must-not-link 2); 54 re-partitioned; largest 40 |
| 5 | Operator rulings (constraints, see 7.3) | verdict store | `same` → must-link closure; `different` → must-not-link; both read every pass | E27 (verdict half), E49-E52, E78, E299, E910, DECISIONS 8, D39, D87 | 1,251 must-link pairs in 41 closures; 265 must-not-link (246 operator + 19 unit-name) |
| 6 | Apply | groups + scope row + rulings | scope; 4 out-of-scope counts; 12 plan refusals + 2 at merge time; the chokepoint; oldest survivor; never split | E900-E907, E911, E41, E44, E4 (property grain), DECISIONS 6/9/17 | g15 live apply: 1,133 groups, 1,053 already one property, 14 applied, 66 skipped |

What the ladder deletes from the description (all LIVE-but-dormant or OFF under w31, section 3): E11 family gate (0 firings), E45-E47 developer gates (OFF), E48's K-A cells, E63/E64 context rule (0 promotions), D65 category hold (empty), K-A, the union-find + bridge path (E33, E37, E57; `n_bridges_applied 0` in g15), the three readings (one reading), the 17 OFF rules, and — only after the portal contract states `total_floors` in one convention — the floor-camp family (sections 5, 5b). Each deletion is an engine change and is measured by C1-C3 before it lands; the description change follows the engine change, never precedes it.

### 7.3 The operator's voice is a separate process (answers operator ask 1)

It is not a pipeline stage. It enters the ladder at exactly two places and feeds the next engine version at a third:

| what the operator does | written as | enters the engine at | effect |
|---|---|---|---|
| Browse merge | `same` pair rulings (`toolkit/property_identity.py:405-418`) | rung 4 (must-link closure) and rung 6 (never re-split) | the engine never separates it, even across a stated fact (`d43.py:61-63,86-90`) |
| split / "different" verdict | `different` ruling + `must_not_link` | rung 4 and rung 6 refusals | never grouped, never merged again |
| scope row | `app_settings.autodedup_apply_scope` | rung 6 | where merges may happen |
| every ruling | `labels` mode export | offline refit (next engine version only) | labels for measuring and retraining; D82/D91 confirmation discipline |

### 7.4 The explanation table (one format, answers operator ask 4 in shape)

One row per stored pair carries `rung` (0-6) and `carried_by` (the evidence family that decided it: ATTR, PRICE, TXT, BRK, LOC, IMG, TIME, or the fact / proof / rule name). The per-generation report is `count(*) group by generation, rung, carried_by, zone`. Today the same information is split across the pair `reason` string (38 distinct strings in g15, 8 prefixes: `model`, `certificate:`, `auto_reject:`, `guard:`, `d43_promote:`, `model:d43_demonstrate:`, `model:d43_gate:`, `certificate:K-x:d43_gate:`), `run.json` (`reasons`, `guarded_at_blocking`, `certificates`, `evidence_families`), the conflicts' `invariant` names and the apply ledger's reason codes: four formats for one question.

### 7.5 Glossary (one name per concept; 30 terms; everything else lives in LEDGER.md or nowhere)

advert · property · pair · candidate · probe · stated fact · wall (a stated fact on the five stored columns) · proof (K-B, K-C, K-R) · score · zone (merge / band / reject) · demonstration · hold · group · must-link · must-not-link · ruling (same / different) · conflict · generation · live stream (`rt`) · lane · pass · feed · seed · scope · apply · chokepoint · survivor · evidence family · label · yardstick.

Retired names (→ replacement): guard, rule floor, hard reject, G1/G2 → wall; certificate → proof; cluster, component, closure, cell → group / must-link; reader, limb, dial, D43 fact, distinguishing fact → stated fact; photo hold, evidence hold, F3, evidence_pending → hold; S-number, g-number for the same version → one version id `<settings>/<model>` (e.g. `w31/w6_gold`) and generation names only for stored answer sets; residual, propose-only → band; tier, seal, arm, stratum, holdout, ECE, Wilson → LEDGER.md only (measurement vocabulary, not engine vocabulary).

---

## 8. The subtraction this yields (descriptions only)

Repo docs (the live reading path):

| surface | today lines | today words | proposed lines | proposed words |
|---|---:|---:|---:|---:|
| PROGRAM.md | 1,907 (≈ 11,700 at 80 columns) | 152,097 | 0 (history = git) | 0 |
| README.md | 19 | 157 | 0 (ENGINE.md is the entry) | 0 |
| ROLLOUT.md | 139 | 4,291 | 0 | 0 |
| architecture.md autodedup paragraphs | 89 | 1,164 | 10 | ≤ 150 |
| realtime-scrapers.md autodedup section | 61 | 799 | 8 | ≤ 120 |
| **ENGINE.md (new)** | — | — | ≤ 300 | ≤ 6,000 |
| **live total** | **2,215** | **158,508** | **≤ 318** | **≤ 6,270** |
| LEDGER.md (new, append-only index, not required reading) | — | — | ≈ 415 | ≈ 9,000 |
| refit_w7_*.json | 3,958 | — | 0 (the refit run's artifact) | — |

Net: live description **−1,897 lines (−86 %) / −152,238 words (−96 %)**; with LEDGER.md counted, −1,482 lines / −143,000 words; bytes in `docs/design/autodedup/` 1,064,729 → ≈ 105,000 (−90 %).

Code-side descriptions (execution belongs to the waves that touch the code; the arithmetic is here so each wave can be held to it):

| surface | today | target | delta |
|---|---:|---:|---:|
| `settings.py` comment lines | 759 | ≤ 1 per surviving knob, citing its E id (≤ 274 after the 33 OFF booleans go, fewer as knobs die) | ≥ −485 |
| module docstrings | 1,684 lines (67 of 68 multi-line) | ≤ 3 lines × 68 | ≥ −1,480 |
| multi-line function/class docstrings | 612 docstrings / 3,559 lines | one-liners (CLAUDE.md convention) | ≈ −2,950 |
| settings JSON files | 54 files / 15,059 lines, 1 live | `w31.json` + the files tests pin | measured by C1-C3 |
| published operator page | engine_explained.html 19,659 words + five_asks 10,082 | regenerated from ENGINE.md's tables (ladder, operator voice, explanation table, glossary) | ≈ −25,000 words |

Concept count: ≈ 154 → 30 glossary terms plus the proof names K-B, K-C, K-R.

---

## 9. Engine subtractions this census surfaced (for C1-C3; each must be measured before it lands)

| candidate | evidence | size |
|---|---|---|
| the 17 OFF rules and their 33 OFF booleans + 4 off modes | section 3.1; A1 §13 | code in decide.py:445-453, stock.py, family.py (335), development.py (361), text_facts / indistinguishable readers; `Settings.validate` branches that only police them |
| union-find + bridge path (E33, E37, E57) | w31 `repartition: true`; g15 `n_bridges_applied 0, n_bridges_refused 0` | `cluster.py:125-145,376-451` |
| dormant rungs: E11 family gate (0 firings), E63/E64 context rule (0 promotions), K-A (off), D65 category hold (empty) | A1 §7.5-7.9; g15 reasons | `decide.py:143-153,456-540,611-636`; `hazard_context.py` (401 lines); `incremental.py:1655-1708` |
| the floor-camp family — ONLY after the source states `total_floors` in one convention | sections 5 and 5b: floor half dead; storey-count half load-bearing (226 merges / 418 co-grouped pairs on the trial) because of a portal-contract defect | `floor_convention.py` (280 lines), `floor_camps`, `floor_camps_reads`, ~16 camp-dependent knobs, 12 E rules; then re-measure the flat floor wall (≥ 2 storeys, E5), whose one-storey slack exists only for the pre-W8 ground-floor ambiguity |
| three readings of one fact reader (gate 8 % no photos / strict 3 % / cluster 8 % with photos) → one reading | E136, E138, D44; section 6 (area compared at 15 sites, 8 tolerances) | `d43_gate_*`, `d43_cluster_*`, `area_band_pct` vs `d43_gate_area_tol` |
| auto-reject limbs as rules outside the fact reader | no E number; 19,715 rejects in g15 | `decide.py:433-442` → fold into rung 1 |
| `legacy_retire` (E907) and batch `apply` | "kept until W8" / "three live lane days" (`TRIAL_LIVE.md:195`) | `legacy_retire.py` 735 lines; `apply.py:1657-1760` |
| `listing_fp` table (migration 528) | "nothing has ever written it" (`incremental_sql.py:695`) | a forward migration dropping it (destructive → operator gate) |

---

## 10. Assumptions checked

| assumption (where stated) | verdict | evidence |
|---|---|---|
| "PROGRAM.md is the source of truth" (:3) | REFUTED | defects 1-10 in section 2; 46 of 73 rules under a "never exercised" header are live |
| "numbers are never renumbered; code cites them" (:42, README) | REFUTED in part | code E221-E224 = doc E222-E225 |
| "the program runs in shadow mode" (:4, README:10, ROLLOUT:7, D4) | REFUTED | production merges since g12; lane at 60 s since migration 572 |
| E12 "zero area on all 138,997 bazos rows" | REFUTED | field-capture hand-over: 84.1 % present |
| E133 family: portals count the ground floor differently, one offset for `floor` and `total_floors` | REFUTED for `floor` (mean Δ vs idnes −0.03 .. +0.05 on the five "camp 1" portals); HOLDS for `total_floors` on mmreality only (+0.92) and a 9-14 % minority of realitymix / sreality | section 5 tables; the coupling of the two columns is what broke |
| E15 "six probes" | REFUTED | eight in `blocking.py:33-35` |
| E14 store `autodedup.listing_fp` | REFUTED | `rt_fp`; `listing_fp` never written |
| the harness reproduces the stored g15 pair decisions on main | CONFIRMED | local w31 run on the trial export: zones 7,971 / 1,533 / 57,260 / 19, certificates 173 / 1,570 / 2,139 and all five wall counts identical to run 36244048665; groups differ (1,134 vs 1,133) only because the harness run had no rulings file |
| ROADMAP index describes the dedup state | REFUTED | `ROADMAP.md:25-28`, no AUTODEDUP row |
| every E rule has a number | REFUTED | the two auto-reject limbs have none |
| field capture W8 unified the storey data the engine reads | HALF: `floor` yes, `total_floors` no | section 5 second table; 5b camps0 arm |
| the floor-camp machinery can simply be deleted | REFUTED today (−58 promotions deleting it, −226 merges correcting it); TRUE after a source fix of `total_floors` | section 5b |
| operator ask 1: "the *you* path is only used when updating the pipeline" | HALF | rulings are also a live constraint every pass (`incremental.py:1395-1440`); section 11 |

---

## 11. Notes for the coordinator

- **Operator ask 1 premise, checked against code.** "The *you* part is only used when updating the pipeline" is half right. Rulings act in two separate processes: (1) a standing constraint read by EVERY lane pass, even an idle one (`incremental.py:1395-1440`, `incremental_sql.py:1085-1104`; must-link at grouping, must-not-link at grouping and apply, within ~60 s), and (2) labels for the next refit only (`labels.py:1-95`). The page's overview diagram (`engine_explained.html` mermaid block 0) draws the ruling store as an input to "Grouping" and "Plan and merge" inside the pipeline figure; block 7 already splits "Process 1 / Process 2". The ENGINE.md shape in 7.3 keeps the pipeline figure free of the operator and gives the operator's voice its own two-row table.
- **Operator ask 2 (DINOv3) and descriptions.** `docs/architecture.md` says of the tag model "Nothing has been promoted or scored yet"; `DECISIONS.md` addendum says the active tag-head model is DINOv3-based (v1, 11 heads, 9,514 images scored); A1 §2 says the engine reads only CLIP zero-shot tags (`image_clip_tags`); D901 ruled "hold on CLIP, tags kept (branch b)". Three texts, three states — whoever builds ask 2 owns reconciling them.
- **What is not touched here.** `CLAUDE.md:184-186` and the three skills (`.claude/skills/...`) carry stale autodedup text (section 2, defect 10); by the hard rule they are listed, not edited.
- **Order of work implied by the north star.** The description follows the engine: ENGINE.md's ladder table is written once, then each engine deletion (section 9) lands with its measurement and deletes its rows from the table and its knob comments in the same PR; LEDGER.md flips the rule's class. No description is written ahead of the code it describes.
