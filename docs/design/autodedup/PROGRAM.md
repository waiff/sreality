# AUTODEDUP — autonomous cross-portal listing deduplication

**Program document. This file is the program's source of truth.**
Status: design accepted; W0 census **closed**, W0b in flight; **the entire program runs in shadow mode** (decide, store, write nothing).
Date: 2026-09-16. Rulings D1–D10 below were made by the program lead (operator delegated) on 2026-09-16 and are **binding — they supersede the design text wherever the two differ**.

---

## 0. What this is, and what it must never touch

An **autonomous** engine that finds the same real-world property across nine Czech portals and across time, and decides whether to merge it — at a precision that would make an unattended merge stream safe.

It lives in its own package `autodedup/`, its own Postgres schema `autodedup`, its own Actions lane `autodedup.yml`, its own concurrency group `autodedup`, its own settings namespace `ad_*`.

**What it shares with the rest of the platform, in this program (D4 — the whole trial is shadow):**

- **reads** `listings`, `properties` (for the E4 property-grain guard and the cluster `property_id` stamp), `listing_location`, `listing_snapshots`, `images` (+`phash`), `image_clip_embeddings`, `image_clip_tags`, broker identities — read-only, no DDL on any of them (D8);
- **writes** exactly one shared thing: three new `llm_calls.called_for` values — `autodedup_judge_text`, `autodedup_judge_vision`, `autodedup_judge_gold` — migrated and applied *before* any calling code. **No other shared table is written by this program.**

Everything else in §11 (the merge chokepoint call, the `property_merge_events.generation` post-stamp, the `dirty_properties` enqueue) is **designed here and exercised nowhere in this program**. It is the write path a *later* operator decision may switch on; see Appendix A.

It **never** reads or writes `dedup_sim.*`, never dispatches a `new-dedup-*` lane, never touches the parallel operator-guided program's settings.

### Clean slate is total (D7)

Per CLAUDE.md rule 15 and ruling D7, the engine reads **nothing** from any prior dedup effort: no golden pairs, no `dedup_pair_audit`, no frozen LLM verdict caches, **no `property_merge_events` rows — not as labels, not as must-not-link seeds, not as evaluation truth, not even as counts**. There is no unmerge/split history in this engine's input set at any grain. Ground truth is built fresh, from the judge and the operator, inside `autodedup.*`. `property_merge_events` appears in this document in exactly one role: as the target of a *future* write (E40), never as a read.

### The thesis, in one paragraph

Every expensive signal in this problem is already paid for. `images.phash` (dHash, 64-bit) exists corpus-wide; `image_clip_embeddings` holds ~11.3M vectors; `listing_snapshots` holds the whole price path; `listing_location` holds graded geo; broker identities resolve nightly. The engine's job is to **spend an LLM only where the free signals genuinely cannot decide** — and the width of that uncertain band is not a hope, it is a settings-capped dial that auto-tightens until the month's bill fits. The LLM is the **label oracle**, not the runtime decision engine: a stdlib logistic regression trained on judge labels does the scoring, so cost scales with *new listings*, not with corpus size. No GPU, ever, in steady state.

### The shape, in one sentence

The only primitive is `resolve(conn, listing_id)`. The trial and (beyond this program) the always-on worker lane are that one function in a loop — so there is no second code path to calibrate, and no divergence between what was measured in the trial and what would run later.

---

## 1. Rule index — the engine in one page

Every rule is stated **once**, here or in the section that owns it, and referenced by number everywhere else. **Never renumber E1–E44** — the numbers are cited by code, tests and the other program docs. A rule that changes meaning gets a new number and the old one is marked superseded.

**Identity and guards**
- **E1** A duplicate is the *same physical unit* (dwelling, parcel, commercial space) under the same deal type. Not the same building, not the same project, not the same floor plan.
- **E2** **Guard G1 — `category_type` equality.** Both known and unequal ⇒ never a pair, never a merge. NULL = *unknown*, not a conflict. Verbatim from `toolkit/dedup_candidates.py` `categories_compatible`; re-enforced at the chokepoint (`toolkit/property_identity.py`, the `FOR UPDATE` block: `if s_ct is not None and r_ct is not None and s_ct != r_ct: raise MergeError`).
- **E3** **Guard G2 — `category_main_compatible`** (`toolkit/room_taxonomy.py`): compatible when equal, when either is NULL, or when the unordered pair is `{dum, komercni}` — *the one sanctioned cross-type, irrespective of subtype*. So `byt` never merges with `dum`, `komercni`, `pozemek` or `ostatni`; `pronajem` never merges with `prodej`/`drazba`.
- **E4** Guards are evaluated at **property grain as well as listing grain**. The chokepoint reads `properties.category_type` / `properties.category_main` (rolled up from the representative listing), so two listings can pass a listing-grain guard while their properties carry different rolled-up values. The engine evaluates the guard on the same rows the chokepoint would, or it manufactures merges that the last rail refuses.
- **E5** **Merge-grade attribute guards**, deliberately tighter than the parallel program's *candidate* tolerances (whose ±20% area and ±2 floor gate blocking, not a merge): both areas known and differing >8% ⇒ hard reject, 3–8% ⇒ band only, ≤3% ⇒ supporting. Both dispositions known and unequal (non-`pozemek`) ⇒ hard reject. Both `byt` floors known and |Δ| ≥ 2 ⇒ hard reject; Δ = 1 ⇒ band only (portals disagree on whether *přízemí* is 0 or 1).
- **E6** A re-listing after a gap **is** a duplicate. There is no temporal veto anywhere in the engine. `gap_days` is a feature with a learned weight, never a penalty.
- **E7** A same-portal re-post by the same broker **is** a duplicate. `same_source` is a feature, never a veto.
- **E8** Scope is **all-time**. No `is_active` filter in blocking, and **no retention window** on the fingerprint store — a 24-month window would structurally make pre-2024 re-listings unretrievable, which is exactly the class E6 exists to capture.
- **E9** **Catalog-photo subtraction.** A dHash appearing on ≥ `ad_catalog_df` distinct listings *corpus-wide* is marketing stock. It is removed from the evidence set **before any image feature is computed**; its share survives as `catalog_ratio`, a negative feature. Default 8, measured in W2. Corpus-wide, not per-block, because a developer catalogue spans blocks.
- **E10** **Image evidence is three numbers, not one**: `interior_match_ratio`, `exterior_match_ratio`, `plan_match_ratio`, split by `image_clip_tags` family (`toolkit/room_taxonomy` ROOM_FAMILIES: interior / exterior / common / plan / other). A shared site plan or facade is a *building* signal and must never be readable as a *unit* signal. Degrades gracefully when tag coverage is thin (the plan arm goes to zero) instead of leaking into one blended score.
- **E11** **Evidence-diversity precondition — a hard gate, not a weight.** An auto-merge requires corroboration from **≥ 2 of 5 independent families**: IMG, TXT, LOC, BRK, ATTR. *Images alone can never merge a pair.* A hard gate survives a mis-weighted model; developer projects are precisely where the model will be mis-weighted.
- **E12** **Missing data is never a mismatch.** Every feature is a `(value, present)` pair and the model consumes `value·present` **and** `present` separately, so absence learns its own weight. Any other rule erases bazos (zero area on all 138,997 rows), remax, and realitymix's 20,722 attribute-less rows.

**Blocking**
- **E13** The only primitive is `resolve(conn, listing_id)`. Trial and (later) worker lane are that function in a loop.
- **E14** Blocking is **index probes over one precomputed row per listing** (`autodedup.listing_fp`), never a set-based corpus join.
- **E15** Six probes, unioned, each optional, none required: **K1** block+attribute (log-band), **K2** address, **K3** broker+price decile, **K4** dHash bands *(location-free)*, **K5** text SimHash bands *(location-free)*, **K6** foreign.
- **E16** **No probe keys on a coordinate.** Distance is a feature only, masked unless both pins are street-grain or finer, normalised by the two uncertainty radii, and discounted by pin population.
- **E17** Hard caps: ≤ `ad_max_candidates_per_listing` (60) per listing, filled in priority order so the cap can only discard the weakest evidence class; any `(probe, key)` over `ad_max_block_size` (200) is **exploded** — never expanded, and membership becomes a negative feature.

**Features and scoring**
- **E18** ~45 features across 7 families are catalogued (§5), all computed in Python from one bulk fetch per block; zero marginal cost, since every input already exists. **Per the program lead's scope ruling, W2 ships a core set of ~25 and the set grows only by error analysis** — a feature earns its place by fixing an observed error class, not by being cheap.
- **E19** **Price-path EVENT matching**: the fraction of the smaller side's price-*change* events matched by the other within ±3 days and ±1% of Δ. Cross-posted adverts drop price together; two different units do not. Free from `listing_snapshots`, and almost nothing in this repo uses it.
- **E20** Text is split into **template similarity** (tfidf / jaccard / simhash) and **unit-specific similarity** (`rare_token_overlap` over tokens with in-block document frequency ≤ 2, plus numeric-fact overlap). The *gap between the two* is what separates a broker's reused template from the same flat re-posted.
- **E21** Scoring is a **stdlib logistic regression**, calibrated (ECE ≤ 0.05 on a 10-bin reliability diagram, isotonic over bins if not), artifact stored as JSON in `autodedup.models`, activated by a partial unique index — the `tag_head_models` idiom. Stdlib inference, so the same scorer can later run inside `scraper/realtime_worker.py` with no numpy and no sklearn (CLAUDE.md rule 7). A linear model is also *legible*: the validation UI shows per-feature contributions, so the operator can see why a merge was proposed.
- **E22** **Three zones**: `p ≥ T_hi` auto-merge; `T_lo < p < T_hi` LLM judge; `p ≤ T_lo` auto-reject. Below `ad_store_floor` (0.02) nothing is persisted, only counted. In this program "auto-merge" means *auto-merge-decided and stored*, never applied (D4).
- **E23** **Thresholds are per stratum** (D10). A stratum that cannot reach its precision bar gets **no auto-merge band at all** and ships propose-only into the residual view. This is how bazos's weakness becomes visible recall loss instead of invisible precision loss.
- **E24** **Certificates** short-circuit to auto-merge, because their precision is structural rather than learned: **K-A** exact `ruian_adm_kod` ∧ equal disposition ∧ area ≤2% ∧ equal floor; **K-B** same source ∧ same broker ∧ text containment ≥0.90 ∧ area ≤1% ∧ **zero active overlap**; **K-C** ≥4 non-catalog exact dHash matches ∧ gallery order preserved ∧ area ≤3% ∧ equal disposition ∧ `catalog_ratio` ≤0.2. All three are retained by ruling. All still pass E2–E5 and E11. (The *disjoint windows* clause in K-B is load-bearing: two simultaneously active listings on one portal by one broker with near-identical text is the developer signature, not a re-post.)
- **E25** **Band width `b` IS the budget dial.** `monthly = L × c × b × $/pair`. `T_lo` auto-raises until `b` fits `ad_band_budget`, and the **sacrificed recall is printed in every run summary** rather than hidden.

**Judge** *(full spec in §6 and the separate JUDGE SPEC)*
- **E26** Two tiers, one forced tool: T1 text (`gpt-5.6-luna`, batched), T2 vision (`gpt-5-mini`, batched, 4+4 images at 768 px q80, `max_tokens=4096`).
- **E27** **Four-way verdict**: `same_property | different_property | same_building_different_unit | insufficient_evidence`. `unit_discriminator` is **required** unless `same_property`. `same_building_different_unit` writes a permanent `must_not_link` row.
- **E28** **PII never crosses the wire**: no broker name, phone or email — only the derived `same_broker` / `same_firm` booleans — and descriptions are scrubbed of Czech contact patterns before truncation.
- **E29** A pair is judged **once**, cached on `(lo, hi, judge_version, tier)`; re-judged only when a *judged field* changed.
- **E30** Every call goes through `LLMClient.call` so it lands one `llm_calls` row. The `called_for` migration and the `PRICES` rows ship **before** the calling code.
- **E31** Every paid lane carries a pre-flight `--max-usd`, **and asserts it read "done", never only "drew"** — a cap priced against the pre-flight *estimate* has silently refused five whole batches in this repo, spending nothing and reporting nothing wrong. Spend gates per D2: **<$10/run autonomous, $25 hard cap per run, $200 total program**.
- **E32** Cost is read from `llm_calls` after the first pass, never extrapolated from token prices again.

**Clustering**
- **E33** **Constrained union-find with must-not-link**, edges processed in descending confidence so the outcome is reproducible from the edge set alone. Every union validates the **merged cluster**, not the edge. Plain connected components is rejected: one bad edge inside a development would merge the whole project.
- **E34** Cluster invariants: single `category_type`; one compat class; area spread ≤ `ad_cluster_area_spread` (0.08); ≤1 non-NULL disposition for non-land; `byt` floor spread 0; size ≤ `ad_max_cluster_size`.
- **E35** `ad_max_cluster_size` is **measured in W0, not inherited**. The "max 3 children" figure everyone quotes came from a system that *structurally forbade same-source merges* over five portals; E6/E7 invalidate it as a prior. Provisional 8, pending the census.
- **E36** Cluster identity = the smallest `listings.id` ever admitted, immutable for the cluster's life. A new listing **appends** (attaching to the medoid). A cluster is **never split automatically**.
- **E37** **Bridging two existing clusters is outside the autonomous envelope.** The bridge edge is recorded and surfaced; nothing is applied. One wrong bridge would corrupt two properties at once, and there is no data to calibrate a bridging premium.

**Write path — designed here, exercised nowhere in this program (D4). See Appendix A.**
- **E38** The only write would be `merge_properties(source='auto', merge_group_id=<engine uuid>)` — **one retired property per group**, so `unmerge_group` reverses it exactly and reverting a whole generation is linear and conflict-free.
- **E39** `autodedup_write_enabled` is read **fresh at the top of every merge attempt**, never cached across a pass. **Shadow mode — decide, store, write nothing — is the default and the only mode for this entire program**; switching it is a separate operator decision, not a deliverable here.
- **E40** `generation = 'autodedup_v1'` would be **post-stamped** onto `property_merge_events` (`merge_properties` doesn't write it). The seam is non-atomic: the engine records `applied_merge_group` on the pair row **before** calling, and a reconciler re-stamps anything it owns that is unstamped. The engine reads **no row of `property_merge_events` it did not itself create** — the reconciler's predicate is scoped to its own `merge_group_id`s — and nothing read there is ever an input to a decision (D7: no prior merge, unmerge or split history as seeds, labels, truth or counts).
- **E41** **MergeError policy.** A category or property-state refusal is recorded as **terminal** on the pair and never retried. A transient DB error retries ≤3× and then **poisons** the queue row with a reason. A wedged drain is a known failure shape in this repo and must not be invented again.
- **E42** A **merges-per-hour latch** (default 50) and a merges-per-run cap stop a storm; both **latch** — writes stay off until the operator clears them.
- **E43** A **physical-plausibility monitor** runs over every applied merge's spliced price series (simultaneous overlapping prices differing >20%; a step with no market move) as a free, continuous, **label-free precision alarm**. Nobody has to judge anything for it to fire. In shadow mode it runs over *proposed* splices and is a pure metric.
- **E44** **Operator state moves with the merge and the engine says so** (D5 adopted). `merge_properties` runs `carry_operator_state_on_merge` and `reconcile_pipeline_on_merge` inside its transaction (CLAUDE.md rules 16/18/22): pipeline cards move, collections/tags/notes re-point, `notification_dispatches` rows re-point onto survivors. Every applied merge reports what moved; in shadow mode every *proposed* merge reports what **would** move, and that report is shown in the validation UI.

---

## 2. Q1 — Duplicate definition and hard guards

**Definition (E1).** Two listings are duplicates iff they advertise the same physical unit under the same deal type — irrespective of portal, broker, advert age, price, or photo set.

**In scope:** re-listings across time (E6), same-portal same-broker re-posts (E7), active↔inactive (E8).

**Explicitly not duplicates:** two different units in the same building or development, *even sharing marketing photos, the same site plan, the same address, the same broker and boilerplate text*. This is the hard negative class and the engine is shaped around it.

**The hard guards, restated exactly** (E2, E3), with the tightenings the engine adds because it gates a merge rather than a candidate (E5), and the grain correction (E4).

One note on mechanics: a re-post that reuses the same `source_id_native` is already **one row** via `listings_source_native_uidx` (migration 091). Only new-id re-posts create duplicate rows, which is what the engine is for.

### How re-listings and re-posts are handled

These are mechanically the easiest pairs (byte-identical photos, near-identical text) and the most dangerous (a broker's template is reused across units). The engine therefore never accepts *template evidence* alone. A re-post merges on **unit-specific** evidence: identical or ±1 m² area, identical floor, interior-room photo matches (not exterior, not plan), rare-token overlap in the description. Certificate K-B (E24) handles the clean case without spending a judge; its zero-active-overlap clause is what separates a re-post from a development.

### How developer projects are kept apart — five independent rails

1. **Blocking never lets a shared-photo block explode into pairs** (E17): an exploded photo block is the catalogue detector, and membership becomes a negative feature.
2. **Catalog-photo subtraction before any image feature** (E9), with the leftover ratio fed back as a negative.
3. **Image evidence split by tag family** (E10) — "same building" and "same unit" are *different numbers*, so a shared plan can never be read as unit identity.
4. **The evidence-diversity gate** (E11): images alone can never merge.
5. **The four-way judge verdict** (E27): `same_building_different_unit` becomes a permanent must-not-link and is a far more informative negative label than plain "different", and the prompt is explicitly adversarial about Czech developer practice.

Plus two block-level priors: a block where ≥8 listings share one `broker_identity_id` with pairwise text containment ≥0.7 is flagged `project_suspected`, which raises `T_hi` inside the block and routes every borderline pair to vision; and the assembled negative-control cohort (§3) exists so this failure mode is *measured*, not assumed.

**Honest limit.** Shared photos + identical text + identical disposition + areas within 8% + an absent unit number is genuinely under-determined. If the trial shows this, the correct answer is to route the whole `project_suspected` block to propose-only. That costs recall, not precision.

---

## 3. Q2 — Trial region and sample

The corpus is all-time (822,599 listings; 612,076 reach a town), so town counts run ~2.2–2.7× their active counts. Praha alone is 79% of all country pairs even after a 119-quarter split, so it distorts every cost estimate and is excluded as the primary block.

**Ruling D1 fixes the cohort shape — four blocks, sized to budget, final pick after the census:**

1. **PRIMARY** — the top-ranked non-Praha district town, **2,000–3,500 all-time** listings. The regime a rollout would live in.
2. **SPARSE** — one small town, **500–700 all-time**. This block doubles as the **exhaustive O(n²) blocking-recall bound**: at ~600 listings that is ~180k pairs of pure CPU, $0 (the *labels* for it are budgeted; enumerating the pairs is free).
3. **DENSE** — one Praha `cast_obce_kod` quarter, **1,000–1,500 all-time**, chosen for new-build activity (developer stress). `cast_obce_kod` covers 85% of Praha vs `momc_kod`'s 24%.
4. **NEGATIVE CONTROL** — assembled, not geographic, **≤ 800 listings**: every listing in blocks 1–3 that either shares a `ruian_adm_kod` with another listing at a **different floor**, or carries a dHash appearing on **≥ 5 distinct listings**. Free to build from census probe B4. A mandatory evaluation stratum (E23).

Hard filters and the weighted score are specified in full in the CENSUS SPEC: ≥6 of 9 portals; all four `category_main` cells ≥30 rows; both deal types present; ≥70% of listings with a stored image and ≥90% of those with CLIP; size 1,500–5,000; not Praha as primary. **The operator picks the blocks off the census table; the program does not pick for them.**

**Expected pair volume.** The small-town pilot measured 3–11 candidates/listing with a single town+attribute rung; six probes roughly triples that. Expect 12–25 p50 in a district town, 30–60 p99 in the Praha quarter, capped at 60 (E17). At the cohort sizes D1 fixes, that is **~40,000–60,000 distinct trial pairs** (cohort × c / 2 — a pair is scored once, not once per side; the Appendix A convention) — a small artifact, iterable locally at zero cost.

**Iteration plan and what it costs.** The secretless shell is unblocked by **one export dispatch**: W1 ships the whole cohort as a gzipped Actions artifact (fingerprints, scrubbed descriptions, phash lists, snapshot price series, hashed broker keys, location fields, **and precomputed CLIP/pHash cross-evidence for every generated candidate pair**). After that, blocking, features, model fitting, calibration, clustering and evaluation all run **locally at $0**. Photos can be eyeballed locally through the public `GET /images/{storage_path}` → 302 presigned R2, no secrets needed.

Expect **6–10 offline iterations** (free) and **3 judge-prompt iterations** (paid). Paid sequence: I0 census $0 → I1 export $0 → I2 ground truth $32 → I3 prompt iteration $10 → I4 full trial pass + recall probes $60 → I5 shadow week $10 (only if W7 is approved). **~$102 committed of $200**, ~$112 if the W7 shadow week is approved (D2), and **every one of those iterations is a row in `autodedup.iterations`**, which is what the progress page renders (§12).

---

## 4. Q3 — Candidate generation (blocking)

O(block) per new listing (E13, E14), robust to the four structural holes: shared town-centroid pins, missing area, missing broker, missing disposition.

Each listing emits a small set of blocking keys into `autodedup.listing_fp` and `autodedup.image_band`. Retrieval is a lookup of that listing's keys, union of co-members, minus self, capped.

| probe | key | fires when | what it buys |
|---|---|---|---|
| **K1** block+attr | `(block_key, cat_group, category_type)` narrowed by equal `disposition` **or** `area_band ∈ {b−1,b,b+1}` | either attribute present | the workhorse |
| **K2** address | `(obec_kod, street_key, house_number_cp)` | both present (~17.5% corpus) | near-decisive; **rescues attribute-less rows** |
| **K3** broker | `(broker_key, cat_group, category_type, price_decile)` | broker resolved | same-broker re-posts **across town boundaries** |
| **K4** dHash bands | 4 × 16-bit bands of the anchor images' dHash | any phashed image | **location-free**; the only lane that works when every attribute changed |
| **K5** text bands | SimHash/MinHash bands over description shingles | description ≥200 chars | **location-free**; the bazos lifeline |
| **K6** foreign | `(country_code, cat_group, category_type, area_band)` | `country_status='foreign'` | the 45,619 abroad listings are an answer, not a loss |

`block_key` = `cast_obce_kod` in the split cities, else `obec_kod`; `cat_group` folds `{dum, komercni}` into one token so the sanctioned cross-type survives blocking. The **log-band trick** (`band = FLOOR(LN(area)/w)`, `w = −ln(1−t)`, join offsets {−1,0,+1}) is lifted verbatim from `toolkit/dedup_candidates_sql.py` as the *key derivation*, which turns a range tolerance into an equality lookup.

**K4/K5 are deliberately location-free, and that is the design's most important reach decision.** Only 10.2% of the corpus is geo-blockable and 17.5% carries a street key. The engine's reach must not be capped by the location program's reach.

**Anchor-image selection for K4** (`ad_anchor_images`, **3** per D8): the images with the highest *interior*-family tag score, falling back to sequence 0,1,2. Indexing by cover shot instead would bias the whole image lane toward facade and site imagery — exactly the catalogue failure mode. Catalog hashes (E9) are excluded from anchors. **D8 also rules out any DDL on `public.images` in this program** — no new shared-table index of any kind; the image lane runs on what already exists, and if that proves too slow the finding is reported, not patched with a hot-table index.

**The failure modes, answered**
- *Shared town-centroid pins* (122,920 listings on 4,206 pins, largest 1,029): a non-issue by construction — no probe keys on a coordinate (E16). Distance is a masked, uncertainty-normalised feature with a `pin_pop` discount.
- *Missing area* (all 138,997 bazos rows): K1's disposition arm still fires; K2/K4/K5 are unaffected. bazos's `postal_town` disagrees with the geo obec on 57% of rows, so **for bazos the photo and text probes outrank the town probe** — expressed as a data row in `autodedup.settings`, never a branch in shared code (CLAUDE.md rule 21).
- *Missing broker* (bazos, bezrealitky, maxima carry no broker row at all): K3 emits nothing; `broker_known` is a mask feature.
- *Missing disposition* (realitymix's 20,722 town-known attribute-less rows, all land): K1's area arm plus K2/K4/K5. For `pozemek`, area is the only identity attribute anyway.
- *remax*: reach is contradicted between two repo sources and is **re-measured in the census** (probe B2). If still unlocated, remax rides K3/K4/K5.

**Explosion control (E17).** An exploded `attr` block falls through to the next-most-specific key; an exploded *photo* block is retained as the catalogue detector. Fan-out fills in priority order `addr > phash > text > broker > attr1 > attr3`, so the cap discards only the weakest class.

**Expected pairs per listing: 12–25 p50, ≤60 p99.** At 25 candidates a `resolve()` pass is ~6 index probes plus ~25 feature computations — sub-second, which is what would make it a worker lane rather than a batch job.

---

## 5. Q4 — Pairwise features at zero marginal cost

One bulk fetch per block: the listing row (`scraper/db.py` `LISTING_COLUMNS`), the `listing_location` row, images (sequence, phash, storage_path, CLIP vector, tag), the last ~12 `listing_snapshots` price points, broker ids. Order versioned in `autodedup/features.py:FEATURE_ORDER`. All obey E12.

**Scope ruling (program lead).** The catalogue below is ~45 features. **W2 ships a core set of ~25** — the ones with a stated mechanism and no measurement dependency — and the set **grows only by error analysis in W4**: a feature is added because a named error class demands it, and the iteration that added it is recorded in `autodedup.iterations` with the metric delta it bought. Certificates K-A/K-B/K-C (E24) are in the core set.

**Attributes.** `area_rel_diff`, `area_exact`, `area_known_both`; `dispo_equal`; `floor_diff`, `floor_known_both`; `total_floors_equal`; `ownership_equal`, `building_type_equal`, `condition_equal`, `energy_equal`, `furnished_equal`, `subtype_equal`; boolean amenities (`has_lift`, `cellar`, `garage`, `terrace`, `parking_lots`) collapsed into **`attr_agreements`**, **`attr_contradictions`** (both known and different — the strongest single negative in the set) and **`attr_agreements_rare`** (each agreement weighted by inverse in-block frequency, so "both energy B" outscores "both has_lift"). `has_balcony`/`has_parking` are legacy conflated booleans and carry a reduced weight. `area_m2` only — the one headline area since location W17.

**Price and price history.** `price_last_ratio`, `ppm2_rel_diff`, `price_magnitude_class_equal` (sale-scale vs rent-scale — the fallback when `category_type` is NULL), and the strong one: **`price_path_event_match`** (E19) plus `price_path_shift_ratio` for the re-listing shape.

**Text (stdlib only).** Reuses `location_data/resolver/normalize.py` (`deaccent`, `normalize_match_key`). `jaccard_5shingle`; **`containment_max`** (catches truncated re-posts, which plain Jaccard misses); `tfidf_cos` over the block corpus; `desc_simhash_hamming`; `len_ratio`; and the discriminators (E20) **`rare_token_overlap`** (in-block df ≤2: street names, unit codes, project names) and **`numeric_fact_overlap`** / **`numeral_conflict`** over `(value, unit)` pairs (m², Kč, NP/patro, unit numbers). Conflicting numerals in the same slot type is itself a reject condition. No MinHash storage beyond the blocking sketch — exact Jaccard is computed on the fly for ≤60 candidates.

**Broker.** `same_broker_identity`, `same_firm`, `same_phone_norm`, `same_email_domain`, `broker_known_both`, `firm_is_franchise` (a franchise email is not an identity), `contact_rarity` (a phone/email with corpus df ≥50 — mmreality's single switchboard across 12 broker ids — contributes nothing).

**Location** — read only from `listing_location` (location lives ONLY there since migration 508). `same_ruian_adm_kod` (address-point identity, near-decisive: sreality 26.9%, bezrealitky 60.7%, idnes 1.4%, bazos 0%); `same_street_key`, `same_house_number_cp`, `same_psc`, `same_cast_obce`; `dist_m` emitted **only** when both pins clear the precision gate, else NULL with an explicit `pin_quality` indicator; **`dist_norm = dist_m / (r_a + r_b + 25)`** so 400 m between two address points is damning and 400 m between two municipal pins is noise; `granularity_rank_min` via `location_granularity_rank` (never the enum's order); `same_exact_pin` paired with `pin_pop`.

**Images** — all computed **after** catalog subtraction (E9). `phash_tight` (Hamming ≤6, `bit_count((a # b)::bit(64))` — with the cast; `bit_count` is not defined for bigint), `phash_loose` (≤11), **`phash_match_ratio` = matches / min(n_a, n_b)** (the ratio carries more signal than the count); `clip_max_cos`, `clip_mean_top3_cos` (11.3M vectors, no ANN index needed at ≤60 candidates × ≤15 images, and **zero storage** — computed lazily per pair); **`seq_monotone_ratio`** (matched pairs preserving gallery order — a re-post keeps order, a catalogue reuse does not); the **three tag-family ratios** (E10); `catalog_ratio`; `shared_photo_df_max`; `n_images_min`.

**Temporal.** `gap_days`, `overlap_days`, `both_active`, `same_source`, and `source_pair` one-hot over the 45 unordered source pairs, which lets the model learn per-portal-pair priors.

---

## 6. Q5 — Decision model

**Layer 1 — the rule floor** (`autodedup/guards.py` + `decide.py`). Pure Python, no training, absolute authority, evaluated first and never overridden. It (a) vetoes on E2–E5, (b) auto-rejects on `attr_contradictions ≥ 3` or `numeral_conflict`, (c) auto-accepts the three certificates (E24) — which also seed the bootstrap positive labels before any judge has run.

**Layer 2 — a calibrated stdlib logistic regression** (E21), over the feature set plus a handful of hand-specified interaction terms encoding the domain physics a linear model cannot see alone: `interior_match_ratio × (1 − shared_photo_block)`, `rare_token_overlap × same_source`, `dist_m × pin_quality_min`, `area_exact × dispo_equal`, `price_path_event_match × gap_days_bucket`, `catalog_ratio × phash_match_ratio`.

Why learned rather than hand-tuned: the interaction between *missingness patterns* and *source pair* is a surface nobody will tune by hand, and it is exactly where bazos, remax and realitymix live. But the model **starts hand-initialised from priors** so W2 can run before any labels exist, and those hand weights remain the fallback if a fit degrades. If measured AUC lands below 0.985, the fallback is ~200 lines of stdlib gradient-boosted decision stumps, still interpretable.

**Three zones** (E22) and **the diversity gate** (E11) — a pair with only one evidence family, however high its score, is forced into the band.

**Calibration and thresholds.** On the sealed, cluster-split holdout: precision(t) and recall(t) with **Clopper–Pearson** exact intervals, **per stratum** (E23/D10). `T_hi` = the smallest t whose **Wilson 95% lower bound on precision ≥ 0.99 with point ≥ 0.995, and no stratum below 0.97** (D3). `T_lo` = the largest t at which cumulative missed positives below it are ≤3% of all positives, **Horvitz–Thompson-weighted** by the known per-stratum sampling rates. `b = P(T_lo < p < T_hi)` is then read off and is the budget dial (E25). Thresholds live on the `models` row, so a threshold change *is* a model version change.

**The sample-size arithmetic, stated because it is where evaluation plans usually break.** A Wilson 95% lower bound of 0.99 on a perfect sample needs **n ≥ 380**; with one error, ~650. A 150-pair stratum can never clear a 0.99 gate — its best possible lower bound is 0.975. The precision sample is therefore **400 gold-judged auto-merge pairs minimum** (D3).

**Why 99.5% precision would justify autonomy — and what it does not.** A false merge fuses two properties and corrupts the price history the operator is buying. It is recoverable: `unmerge_group` replays the ledger deterministically, and every autonomous merge would be a **single-child** `merge_group_id` (E38), so reverting a whole generation is linear. At ~30,000 expected corpus-wide merges, 99.5% would leave ~150 bad merges — all surfaced in the residual/groups UI and all revertible. **Reversibility is what buys the threshold, not the threshold itself.** In this program nothing is applied at all (D4), so nothing can be reverted either: reversibility stays an **argued** claim here — argued from `unmerge_group`'s deterministic replay and from the single-child group shape (E38) — and is carried into W6's decision package **with that caveat stated**. Measuring it means applying and undoing real merges through the production chokepoint, which this program may not do; that rehearsal is Appendix A5 and needs its own operator decision.

---

## 7. Q6 — LLM judge

Full specification is delivered separately as the **JUDGE SPEC**. In outline: pre-flight obligations (the `called_for` migration and `PRICES` rows before any calling code; the `--max-usd` "done not drew" assertion; smoke runs for the never-exercised vision-in-batch and Qwen paths); T1 `gpt-5.6-luna` text on 100% of band pairs at **$0.00086/pair batched**, T2 `gpt-5-mini` vision on ~30% at **$0.00284/pair batched**, blended **$0.0017 per banded pair**; a three-vote gold judge at ~$0.018/pair; the four-way forced-tool verdict with a required `unit_discriminator` (E27); the PII rule (E28); the three separately-reported agreement numbers with **gold-vs-operator below 0.95 stopping the program** (D6).

Two things worth repeating here because they change the cost model. First, `gpt-5-mini` and `gpt-5.6-luna` are **reasoning models**: this design budgets 1,200–1,500 output tokens per verdict, roughly double what a naive count gives, and W3 replaces the estimate with the measured `llm_calls` average before any threshold is fixed (E32). Second, `COMPARISON_MAX_EDGE = 768` at q80 is the single biggest cost lever in the whole program — roughly one third the vision tokens of the 1568 tier.

**Spend gate (D2).** A lane costing **under $10 runs autonomously**; **$25 is the hard per-run cap** wired into `--max-usd`; **$200 is the program total**. Every paid lane asserts the batch read *done*, not merely *drew*, and writes its measured spend into the `autodedup.iterations` row for that iteration so the progress page shows real dollars, never forecasts.

---

## 8. Q7 — Clustering

Constrained union-find with must-not-link (E33), invariants (E34), immutable identity and medoid attachment (E36), and no autonomous bridging (E37).

Every refused union writes an `autodedup.cluster_conflicts` row with the offending invariant — these are the **highest-value items in the validation UI**, because a conflict means the engine found strong evidence in both directions.

**What the cluster row stores** (`autodedup.clusters`): `cluster_key`, `generation`, `size`, `block_key`, `cat_group`, `category_main`, `category_type`, `area_min/max`, `sources[]`, `medoid_listing_id`, `min_edge_score` (the weakest accepted edge — the UI's default sort, because that is where errors live), `mean_edge_score`, `n_judged_edges`, `n_certificate_edges`, `evidence_families` (bitmask), `max_gap_days`, `shared_photo_warning`, `status`, `property_id`, `model_version`, `feature_version`, timestamps. Members live in `cluster_members` with `joined_via_lo/hi`.

**Fragmentation is a gate, not just a metric.** Under E6 a property may carry five or more listings across years. Splitting one true property into three clusters is invisible both to pairwise precision *and* to a residual scan that only surfaces pairs above a score floor — so the operator's UI would show a clean result while the price history the program exists to build stays fragmented. §9 therefore gates on cluster **recall** (mean clusters per true property ≤ 1.15 on gold-judged properties), not only purity.

---

## 9. Q8 — Evaluation protocol

**Labels** (`autodedup.labels`), all fresh and all built inside `autodedup.*` (D7 — nothing is imported from any prior dedup effort, and `property_merge_events` is not a label source): operator verdicts (weight 1.0, the oracle), gold LLM three-vote (0.6), cheap LLM (0.3), rule-certain certificates (bootstrap). Split **60/20/20 by cluster, never by pair** — pairs inside one cluster are not independent and a pair-level split leaks.

**Metrics and pass criteria**

| # | metric | how | pass |
|---|---|---|---|
| 1 | **Pairwise precision** at auto-merge | 400 gold-judged auto-merge pairs, Clopper–Pearson/Wilson 95% | **LB ≥ 0.99 on n ≥ 380, point ≥ 0.995, no stratum < 0.97** (D3), no systematic error class ≥3 occurrences |
| 2 | **Pairwise recall** | Horvitz–Thompson-weighted over score-decile strata with known sampling rates; bootstrap CI over 2,000 resamples | ≥ 0.85 at auto-merge, ≥ 0.95 including the band |
| 3 | **The recall probe** — the honest attack on "we cannot see the positives we threw away" | 300 sampled listings: uncapped retrieval (every probe, no `max_block_size`) **plus** brute-force in-block CLIP top-50; cheap-judge everything the engine rejected (~9,000 pairs), gold-judge whatever the cheap judge calls a duplicate (~250) | missed-duplicate rate ≤ 2% of listings |
| 4 | **Blocking-recall bound** | one exhaustive O(n²) pass over the ~600-listing sparse block (~180k pairs, pure CPU, $0), labels budgeted | reported, not gated — it bounds what the probes miss |
| 5 | **Cluster purity** | 100 sampled clusters, every internal pair judged | ≥ 0.98 |
| 6 | **Cluster recall / fragmentation** | mean clusters per gold-judged true property | ≤ 1.15 |
| 7 | **Residual-duplicate rate** | the operator scrolls the residual view for 30 minutes | < 5 true duplicates per 100 properties reviewed |
| 8 | **Judge fidelity** | cheap-vs-gold ≥ 0.93; **gold-vs-operator ≥ 0.95 on a 200-pair operator session run through the validation UI, or the program stops** (D6); gold unanimity ≥ 0.92 | as stated |
| 9 | **Cost per decided listing** | read from `llm_calls` by `called_for` and model (E32) | projected monthly ≤ $150 |

**Metric 10 (revert wall-clock) has moved to Appendix A5 and is not a gate of this program.** Timing a revert means applying 500 real merges through `merge_properties` and undoing them — production writes to `properties`, `listings.property_id`, `property_merge_events` and operator state, which D4 forbids here. Reversibility therefore stays the argued claim of §6, and W6's decision package says so in those words.

**Sample sizes.** 4,000 cheap bootstrap labels; 1,200 gold; 400 precision; ~9,250 recall probe; 200 operator-anchored (the D6 session); 100 clusters. Strata: auto-merge / band / near-reject / far-reject / same-portal / cross-portal / bazos / remax / land+commercial / exploded-photo-block / **negative control**.

**Feedback loop.** Operator verdicts land in `autodedup.verdicts` and, for negatives, in `must_not_link`. They override gold labels, become weight-1.0 training rows, and would drive **the latch** (E42) on any future write path. A refit is triggered manually, never automatically, and a new model must beat the incumbent **on the sealed test split** before `activate` flips the active flag.

---

## 10. Q9 — Cost model at full scale

**Governing equation (E25):** `monthly = L × c × b × $0.0017`.

**L, measured not guessed.** The doc-sourced figure is 39,794 listings first seen in 7 days ≈ 5,685/day ≈ **170,000/month**. The census re-measures it (probe B5) before any threshold is fixed. `c = 20` candidates/listing.

| band `b` | monthly | per listing |
|---|---|---|
| 0.005 | **$29** | $0.00017 |
| 0.010 | **$58** | $0.00034 |
| 0.015 | **$87** | $0.00051 |
| 0.020 | **$116** | $0.00068 |
| 0.026 | $150 | $0.00088 — the ceiling |

`ad_band_budget` ships at **0.015 ≈ $87/month**, with `T_lo` auto-raising to fit and the sacrificed recall printed. Everything outside the band costs **$0**. This is the steady-state figure a *future* always-on lane would carry; in this program the only spend is the trial's own iterations (§14).

**Per-listing marginal cost** for blocking, features, scoring and clustering: pure CPU over already-paid substrate. **$0.**

**Corpus backfill is explicitly out of scope for this program (D9).** The arithmetic is retained in Appendix A so the rollout conversation has a number to start from, but nothing in W0–W7 backfills anything.

**Storage** (Supabase, outside the LLM budget — see the schema migration's sizing block): trial cohort well under 60 MB. The full-corpus projection is `listing_fp` 250 MB, `image_band` 660 MB (the largest line; `ad_anchor_images` 3→2 would take it to ~440 MB at a recall cost), `phash_pop` 30 MB, `pairs` 200 MB (only `score ≥ 0.02` persists), everything else ~100 MB — **~1.2 GB** — and that projection only becomes real if a backfill is later approved. No multi-GB pair table: rejected pairs are re-derivable from the block index and are counted, not stored.

**R2 Class-B reads.** The 10M/month free tier is per account and already consumed by production `/images/` redirects. The trial's vision judging adds a five-figure number of GetObjects at $0.36/M overage — pennies, but it belongs in the table because it lands on a line the $200 does not cover.

**GPU: $0.** CLIP (11.3M vectors) and dHash exist corpus-wide; cosines over ≤60 candidates need no ANN index. DINOv3 is a non-asset: migration 480 applied with **0 rows**, and `data/dinov3_config.json` still has null `resolution`/`preprocessing`/`dtype` so the loader deliberately refuses. **$20 is reserved** for a single RTX 3090 bake-off ($0.22/h) *only if* W4's error analysis proves image confusion is the dominant residual error class.

---

## 11. Q10 — Realtime integration (designed; not built in this program unless approved)

W7 ships this section as a **design-only document** by default; building the lane requires explicit operator approval (§14).

**The lane.** One tuple appended to the `lanes` list in `scraper/realtime_worker.py`, following the three-piece contract exactly as `location_resolve` does:

```python
("autodedup", lambda: _lane_loop(
    "autodedup", stop_event, _read_autodedup_interval,
    lambda: _autodedup_pass(stop_event, state), state,
    default_interval=0)),
```

`default_interval=0` is the **fail-safe, not a default**: `_lane_loop` keeps this value when the `app_settings` read *raises*, and the flag lives inside the read that just failed — a positive fallback would turn a dark lane on for one pass on any pooler blip, which is exactly when it must not run. The reader returns 0 unless `autodedup_enabled` is true. `_record_pass(state, "autodedup", last)` puts it in `worker_heartbeats` and on the Health page for free; `check_worker_lane_stall` reads `in_flight_s` only, so an idle lane never false-alarms. A pass is bounded by `LANE_PASS_TIMEOUT_SECONDS = 1800` and `_supervised` restarts it after 30 s if it crashes.

**The queue.** `autodedup.resolve_queue`, drained `FOR UPDATE SKIP LOCKED` (the `listing_detail_queue` idiom, CLAUDE.md rule 19). Two producers: a one-line enqueue beside the existing `dirty_properties` enqueues in `scraper/db.py` (fired on `new`, and on `updated` only when a **matching-relevant** column moved — price, area, disposition, floor, description, image set); and a backstop sweep over `listing_fp` rows whose `fp_version` is stale. `listing_snapshots` is an append-on-content-change feed (CLAUDE.md rules 2 and 8), so this needs no change to any scraper write path beyond the enqueue line.

**Per-listing pass** (target p95 < 60 s from enqueue): build/refresh `listing_fp` + `image_band` (delete-then-insert by `listing_id`, idempotent) → probe K1–K6 → bulk-fetch the block → features → rule floor → model → zone. Band pairs go to `autodedup.judge_queue`; the worker would drain them **synchronously** with a pre-flight budget check (the 24 h batch window plus GitHub cron throttle — a `*/30` lane has measured 80–256 minute gaps — would make "realtime" a fiction), while a dispatched Actions lane uses Batch at half price.

**Mutual exclusion:** lease-row CAS (`location_data/resolver/lease.py` pattern), **never** `pg_advisory_lock` — session locks strand over the transaction pooler. The same lease lets the worker and a dispatched Actions run share the code without overlap.

**Idempotency.** `listing_fp` keyed on `listing_id`; `pairs` on `(listing_lo, listing_hi)` with `lo < hi` CHECK; `image_band` on `(band_no, band_val, listing_id)`. All writes `ON CONFLICT DO UPDATE`. Re-running `resolve()` on the same listing, or re-running a whole generation, is safe. Both sides are additionally resolved through `resolve_active_property_ids` (`toolkit/property_identity.py`, which follows `merged_into`) before a pair is proposed: if they already share an active property the decision is a recorded **no-op** and never reaches `/autodedup/groups` — in shadow mode that is what keeps already-merged pairs out of the operator's review queue.

**The merge write — Appendix A material, never executed in this program.** Survivor = the property of the listing with the oldest `first_seen_at`, matching `api/property_merge.py`'s convention.

```python
group = str(uuid.uuid4())
store.record_intent(conn, lo, hi, group)          # E40: recorded BEFORE the call
res = merge_properties(conn, survivor_id=surv, retired_id=ret,
        reason=f"autodedup:{model_version}:{lo}-{hi}", source="auto",
        confidence=score, merge_group_id=group,
        markers={"engine": "autodedup", "generation": GENERATION,
                 "model_version": MV, "feature_version": FV,
                 "cluster_key": ck, "families": fams,
                 "gap_days": gap, "judge_llm_call_id": cid})
store.stamp_generation(conn, group)               # write-only touch of property_merge_events
store.enqueue_dirty(conn, surv)
```

`merge_properties` takes `merge_group_id` as a real keyword (verified), re-enforces E2/E3 at the chokepoint and raises `MergeError` — the last rail, never bypassed, with the policy at E41. Note that `recompute_one`, `recompute_mf_one` and `sync_browse_list` run **inline per merge**, which is why a bulk revert is a bulk read-model rebuild of unmeasured duration — the reason the rehearsal is gated as Appendix A5 rather than run here.

**Kill switches, escalating:** `autodedup_write_enabled=false` (score and store, write nothing — **the whole program's mode**, E39/D4) → `autodedup_enabled` interval 0 (lane dark) → the merges-per-hour latch (E42) → the operator-error-rate latch that a future write path would add (§9's feedback loop: operator verdicts marking applied merges wrong flip `autodedup_write_enabled` false above a rolling error rate the rollout decision sets). The first of these is the whole program's mode; the rest are Appendix A material.

**Unmerge safety.** `scripts/autodedup_revert.py --generation <g> | --since <ts> | --merge-group <id>` walks `autodedup.merges` newest-first and calls `unmerge_group`. Because every autonomous group has exactly one child, the revert is linear and conflict-free. **It is never exercised in this program** — no merge is applied, so none can be reverted (D4). The rehearsal that would time it is Appendix A5.

---

## 12. Q11 — Progress page and validation UI

**Reuse, don't duplicate — but reuse the right layer.** `BrowseExperience.tsx` is the whole Browse *cohort* surface (filters + map + cards), bound to `BrowseViewState` and to property-grain `CardRow` from `properties_public`. The autodedup surfaces are **iteration-, pair- and cluster-grain**. So: reuse `ImageCarousel`, `ImageLightbox`, `Tabs`, `Dialog`, `Spinner`, `Skeleton`, `ErrorBanner`, `SectionChrome` (`Chevron`, `useCollapsed`), `lib/format`, `lib/enums`, `lib/portals`, `lib/imageUrl` — and build exactly one new comparison component, `PairCard.tsx` (two-column listing comparison + evidence panel), used by both verdict views. The in-repo precedent for a verdict surface is `pages/NewDedupExamReview.tsx`; for a program drill-down, `pages/NewDedupCandidates.tsx`.

### The progress page (W1, before any validation view)

The operator asked for a **top-line menu entry** that shows how the program is actually going, iteration by iteration. It is the first UI this program ships — **W1, ahead of the validation views** — because it is what makes a long autonomous program legible while it runs.

- **Route `/autodedup/progress`** (nav entry `AUTODEDUP → Progress`), backed by `GET /autodedup/iterations`.
- **One row per iteration**, newest first, grouped by wave: wave and title, status (`running` / `done` / `failed` / `skipped`), **approach** (what was tried, in a sentence), **tools used** (the chips this document's per-wave "Tools used" paragraphs feed), **sample statistics** (cohort, blocks, n pairs, n labels), **metrics** (the numbers that iteration moved, with the gate it was measured against), **cost in USD read from `llm_calls`, never forecast** (E32), links to the GitHub run and to any artifact, and free-text notes.
- **Header strip**: total spend to date against the **$200 program cap** and the **$25/run cap** (D2), waves closed vs open, and the current mode — which reads **SHADOW** for the whole program (D4).
- The page is a read of `autodedup.iterations` (§13). Every lane writes its own row: `status='running'` at start, terminal status plus metrics and cost at the end. A lane that dies leaves a `running` row with a stale `started_at`, which is itself the signal.

### The validation views (W5)

- `/autodedup` — funnel, cost-to-date, precision/recall with CIs, per-stratum thresholds, latch state.
- `/autodedup/groups` — **proposed groups** (shadow mode: nothing has been applied). Infinite list of cluster cards: member listings side by side with portal badges, thumbnails, price, area, disposition, floor, first/last seen and active state; `min_edge_score`; `decided_by`; evidence chips (`6 interior photos matched`, `same street+no.`, `price path shares 3 events`, `re-listed after 214 days`, `judge: same_property 0.93`); a **merged price-history sparkline** — the operator's stated reason for wanting re-listings merged; and the **"what would move" panel** (E44/D5): the pipeline card, collections, tags, notes and notification rows that a merge *would* carry onto the survivor. Default sort **weakest edge first**, because that is where errors live. Actions: **Confirm** / **Not the same** (verdict + permanent must-not-link, two-step confirm) / **Flag**.
- `/autodedup/residual` — **residual duplicates**: pairs the engine did *not* accept but scored above a display floor (`p ≥ 0.20`), plus everything the recall probe flagged, ranked by score so the scroll is ordered by expected yield. Attribute diff table highlighting only the disagreeing fields, both photo sets, the per-feature model contribution breakdown (a linear model is legible), and **"why it wasn't merged"** — which precondition failed. That last field is what turns a review session into design feedback. Actions: **This IS a duplicate** / **Correctly separate** / **Same building, different unit** / **Unsure**.
- The **200-pair gold-vs-operator session (D6)** runs through these views; the session's agreement number is metric 8 and gates the program.

**Filters on both verdict views:** town/quarter, source pair, `category_main`, `category_type`, cluster size, score range, evidence family, verdict state (unreviewed/confirmed/rejected), `decided_by`, `has_llm_verdict`, `shared_photo_warning`, `project_suspected`, date range.

**API** — `api/routes/autodedup.py`, `APIRouter(prefix="/autodedup", dependencies=[Depends(deps.require_admin)])`, registered in `api/main.py` beside the other program routers, with the `store_ready(conn)` probe idiom of `api/routes/new_dedup_candidates.py` so a pre-migration DB renders an empty state rather than a 500:

- `GET /autodedup/iterations` — the progress page (W1)
- `GET /autodedup/stats`
- `GET /autodedup/groups?after=&limit=&…` — keyset by `(min_edge_score asc, cluster_key desc)`
- `GET /autodedup/groups/{cluster_key}` — members, per-edge evidence, judge rationale, the "what would move" report
- `GET /autodedup/residual?after=&limit=&…`
- `GET /autodedup/pair/{lo}/{hi}` — full evidence: every feature with value and presence flag, both digests, both image lists with per-image Hamming/cosine, the judge transcript, the score decomposition
- `POST /autodedup/verdict` `{kind, cluster_key|lo+hi, verdict, note}`

Filters are validated as **keys against a server-side registry**, never a predicate off the wire (the `AUDIT_BUCKETS` idiom). Reads go through the bearer-gated API rather than `*_public` views because the browser Supabase client is pinned to schema `public`, so `autodedup` is unreachable from the browser by construction — and because `listings` is RLS deny-all (raw broker PII inline). Photos render through the existing public `GET /images/{storage_path}` → 302 presigned R2 via `imageSrc({sreality_url, storage_path})`, so the UI needs no new media path.

**Nav:** one `AUTODEDUP` dropdown in `Shell.tsx` inside the `showAdmin` block, mirroring `NEW DEDUP` exactly, with **Progress** as its first entry. Routes are declared in `frontend/src/lib/routes.ts` `def()` then wired in `frontend/src/routes.tsx` wrapped in `<AdminPage>`; hand-typed route literals are an eslint error. Copy in English; property vocabulary in Czech, pulled from the shared `lib/enums` / `lib/portals` modules.

---

## 13. Q12 — Postgres schema

Two migrations, both **applied via `gh workflow run apply_migration.yml --ref <branch> -f file=NNN_x.sql -f dry_run=false -f confirm=APPLY` before the PR merges** (a merged PR is not an applied migration):

- **`migrations/527_autodedup_called_for.sql`** — the three `called_for` values, the check constraint restated whole, nothing removed. Shipped and applied *before* any calling code, because `LLMClient._record_call` on the success path is not wrapped in try/except: a CHECK violation raises *after* the provider has already billed.
- **`migrations/528_autodedup_foundation.sql`** — schema `autodedup`, its tables, indexes, `enable row level security` on every table in the same migration that creates it, `revoke all … from anon, authenticated`, default privileges revoked, and **no FK into any production table** (the schema must be droppable in one statement without touching production). `set lock_timeout = '5s'` plain — never `SET LOCAL`; the apply path is statement-autocommit.

**Tables by concept:** control (`settings`, `runs`, **`iterations`**) · blocking substrate (`listing_fp`, `image_band`, `phash_pop`, `exploded_blocks`) · pair grain (`pairs`, `judgements`, `must_not_link`) · cluster grain (`clusters`, `cluster_members`, `cluster_conflicts`) · write path and learning (`merges`, `verdicts`, `labels`, `models`, `eval_samples`) · realtime (`resolve_queue`, `scan_cursor`, `judge_queue`).

**`autodedup.iterations` — the progress ledger the SPA renders** (§12). One row per iteration of any wave, written by the lane itself:

| column | type | meaning |
|---|---|---|
| `id` | `bigserial` primary key | |
| `wave` | `text` | `W0`, `W0b`, `W1`, … |
| `title` | `text` | what this iteration tried, in a few words |
| `status` | `text not null check (status in ('running','done','failed','skipped'))` | |
| `approach` | `text` | the sentence the progress page shows |
| `tools` | `text[]` | the per-wave "Tools used" chips |
| `sample_stats` | `jsonb` | cohort, blocks, n pairs, n labels |
| `metrics` | `jsonb` | the numbers this iteration moved, each with its gate |
| `cost_usd` | `numeric(10,4) not null default 0` | **read from `llm_calls`** (E32), never forecast |
| `artifacts` | `jsonb` | name → url |
| `run_id` | `bigint` | the GitHub Actions run |
| `notes` | `text` | |
| `started_at` / `finished_at` / `created_at` | `timestamptz` | `created_at default now()` |

RLS on, `anon`/`authenticated` revoked, and registered — with every other new relation — in `tests/test_migration_rls_grants.py::_ADMIN_ONLY_RELATIONS`.

**Python package `autodedup/`:** `guards.py` (E2–E5, E34 — imports only `toolkit.room_taxonomy`) · `normalize.py` · `fingerprint.py` + `fingerprint_sql.py` · `blocking.py` + `blocking_sql.py` · `fetch.py` + `fetch_sql.py` · `features.py` · `model.py` · `decide.py` · `cluster.py` · `judge.py` + `judge_batch.py` · `apply.py` · `store.py` + `store_sql.py` · `iterations.py` (the ledger writer) · `evaluate.py` · `settings.py` · `census.py` **[built]** · `lane.py` **[built]**.

**CI and packaging chores:** `"autodedup"` in `tests/sql_corpus.py` `RUNTIME_DIRS` **[done]** so every `*_SQL` constant joins the PREPARE gate for free; `autodedup*` in `pyproject.toml` `packages.find` **[done]**; the new relations registered in `tests/test_migration_rls_grants.py::_ADMIN_ONLY_RELATIONS`; `frontend/public/workflow-docs.json` regenerated with `python scripts/generate_workflow_docs.py` whenever `autodedup.yml`'s leading comment changes (a **hard** CI gate); a `COPY autodedup/` line in the `Dockerfile` only if and when the worker lane ships.

**In-function concatenated SQL is invisible to the PREPARE gate.** Every executed statement lives in a module-level `*_SQL` constant with `%(name)s` params. Two grain variants mean two constants, never a string-built column name — the pattern `census.py` already follows.

---

## 14. Q13 — Wave plan, gates, cost

`ad_*` settings ship dark and **shadow mode is the only mode in W0–W7** (E39/D4). The program's terminal deliverable is **W6: a measured, fully evaluated engine that has written nothing**. Any canary or rollout after that is a separate operator decision (Appendix A).

| W | deliverable | gate | LLM cost |
|---|---|---|---|
| **W0** Census *(closed)* | `autodedup.yml` + `autodedup/lane.py` + `autodedup/census.py` on `main` — **PR #1482, merged as `f236d2f7`**; first census run **35088558149** | census artifact returned and readable — **met** (`autodedup-census-35088558149`, 17,455 B). The *delta* probes are W0b's gate, not W0's | **$0** |
| **W0b** Program doc + foundation *(this PR)* | `docs/design/autodedup/PROGRAM.md` + `README.md`; migrations **527** (`called_for`) and **528** (schema `autodedup`, incl. `iterations`) written **and applied**; the census **delta** probes: per-portal signal coverage (B1), remax location re-measure (B2), re-post base-rate (B3), developer-project density (B4), ingest-rate `L` (B5), RLS/grant state (B7), plus the block-layer adds `with_desc200` / `with_source_url` / `no_signal_at_all`. **Probe B6 (merge-ledger split) is dropped by D7** — the engine reads nothing from `property_merge_events`, not even counts | both migrations applied before merge; delta census runs green; **the operator picks the four blocks** off the table; `L`, the re-post base rate and `ad_max_cluster_size` are measured, not guessed | **$0** |
| **W1** Export + local harness + **progress page** | `mode=export` dumps the cohort as one gzipped JSONL artifact: fingerprints, **scrubbed** descriptions, phash lists, CLIP vectors as float16, tags, last 12 snapshots, **hashed** broker keys, location fields, and precomputed cross-evidence for every candidate pair — no PII in the artifact. Plus `GET /autodedup/iterations`, the `/autodedup/progress` SPA page and the `AUTODEDUP` nav entry, and `autodedup/iterations.py` so every lane from here on writes its own row | features for 10k pairs compute locally in < 60 s (zero-cost iteration proven); the progress page renders W0/W0b/W1 rows with real costs from `llm_calls` | **$0** |
| **W2** Blocking + core features | K1–K6, explosion control, catalog subtraction, the **core ~25 features** (grows only by error analysis), hand-initialised model; `estimate`/`verify` modes with a pure-Python oracle diffed against the SQL | oracle and SQL agree on every block, zero disagreements; ≤ 25 candidates/listing p50, ≤ 60 p99; certificates retrieve 100% of rule-certain positives; **`b` measured on the real cohort** | **$0** |
| **W3** Ground truth | vision-in-batch smoke (20); Qwen smoke (20); 4,000 cheap bootstrap labels; 1,200 gold three-vote labels; **measured output-token anchor** | gold unanimity ≥ 0.92; cheap-vs-gold ≥ 0.93; `llm_calls` shows the spend and the measured $/pair; every lane asserted "done" not "drew" | **$32** |
| **W4** Model + thresholds | Stdlib LR fit, reliability diagram, **per-stratum** `T_hi`/`T_lo` (D10), diversity-gate tuning, error analysis, up to 3 prompt iterations, feature-set growth driven by the named error classes | ECE ≤ 0.05; auto-band precision Wilson-LB ≥ 0.99 **on ≥ 380 pairs**, point ≥ 0.995, no stratum < 0.97; `b ≤ ad_band_budget`; weak strata explicitly demoted to propose-only | **$10** |
| **W5** Validation UI | `/autodedup`, `/autodedup/groups`, `/autodedup/residual`, `PairCard.tsx`, verdict capture, the "what would move" panel (E44/D5) | operator can scroll both views and verdicts persist; **the 200-pair gold-vs-operator session runs (D6) and reaches ≥ 0.95, or the program stops** | **$0** |
| **W6** Full trial pass — **the program's terminal gate** | Score all four blocks; judge the band; the recall probe; the exhaustive O(n²) bound on the sparse block; the sealed holdout; the decision package, including the reversibility argument **stated as argued, not measured** (A5) | every §9 criterion: precision per D3, cluster recall ≤ 1.15, missed-duplicate ≤ 2%, projected monthly ≤ $150. Outcome = a decision package for the operator, **not a rollout** | **$60** |
| **W7** Realtime lane — **design-only unless approved** | By default: a written lane design (this §11) plus the queue/enqueue/latch/plausibility-monitor specification, reviewed and left unbuilt. **Only on explicit operator approval** does it become code: worker lane, `resolve_queue`, enqueue hooks, latches, budget rails, `verify_pipeline` arm, E43 monitor, run 7 days with **writes off** | design-only: the operator has read it. If built: 7 days shadow, queue p95 < 60 s, zero lane crashes, measured spend within ±30% of forecast | **$0** design-only / **$10** if built |

**Committed development spend ≈ $102 design-only, ≈ $112 if W7 is built — of a $200 program cap (D2).** GPU $0 planned, **$20 reserved**, contingency **$68–78** — roughly two extra full W6 passes.

### Tools used, per wave (the progress page mirrors these as chips)

- **W0 — Census.** `autodedup/census.py` + `autodedup/lane.py` dispatched through `.github/workflows/autodedup.yml` (`mode=census`), read-only SQL over `listings` ⋈ `listing_location` ⋈ `location_granularity_rank`, `out/census.json` uploaded as a 30-day Actions artifact. No LLM, no GPU, no writes.
- **W0b — Doc + foundation.** `apply_migration.yml` (the no-MCP apply path) for 527/528; `psql "$SUPABASE_DB_URL"` for verification SELECTs; `tests/sql_corpus.py`'s PREPARE gate and `tests/test_migration_rls_grants.py` as the CI rails; the census lane again for the delta probes.
- **W1 — Export + harness + progress page.** `mode=export` on the same Actions lane (gzipped JSONL artifact, scrubbed and hashed); a local Python 3.12 stdlib harness reading that artifact; FastAPI (`api/routes/autodedup.py`) + React/Vite/Tailwind (`/autodedup/progress`, `Shell.tsx` nav); `autodedup/iterations.py` writing `autodedup.iterations`.
- **W2 — Blocking + core features.** Pure stdlib Python locally (no DB, no network): `blocking.py`, `features.py`, `guards.py`, plus the pure-Python oracle diffed against the SQL constants. `hashlib`/`unicodedata` for SimHash and deaccenting; `math` only. Zero cost, unlimited iterations.
- **W3 — Ground truth.** `LLMClient.call` via the OpenAI Batch API — `gpt-5.6-luna` (T1 text), `gpt-5-mini` (T2 vision, 768 px q80), a three-vote gold panel; every call lands an `llm_calls` row under `autodedup_judge_text` / `autodedup_judge_vision` / `autodedup_judge_gold`; `--max-usd` pre-flight asserting "done"; R2 presigned reads for the vision images.
- **W4 — Model + thresholds.** Stdlib logistic regression (`autodedup/model.py`), a 10-bin reliability diagram, Wilson and Clopper–Pearson intervals computed in `statistics`/`math`, Horvitz–Thompson weighting, bootstrap resampling — all local, all free. Prompt iterations go back through the W3 tools.
- **W5 — Validation UI.** FastAPI admin routes behind `require_admin`, React + TanStack Query on the SPA, `PairCard.tsx`, the shared `imageSrc` → `/images/{storage_path}` → 302 R2 media path, `lib/routes.ts` `def()` for every route literal.
- **W6 — Full trial pass.** Everything above at once: the Actions lane for the scored pass and the band judging, the local harness for evaluation (Wilson/Clopper–Pearson, Horvitz–Thompson, bootstrap), and `psql` for read-only verification SELECTs. `scripts/autodedup_revert.py` is *written and unit-tested against fixtures* here; it is not run against the database in this program (A5).
- **W7 — Realtime lane.** Design-only by default: this document plus a reviewed lane spec. If approved: `scraper/realtime_worker.py` lane tuple, `FOR UPDATE SKIP LOCKED` queue drain, lease-row CAS (`location_data/resolver/lease.py` pattern), `worker_heartbeats` + the Health page, `verify_pipeline` arm.

### Risks and mitigations

1. **Circular ground truth** — the judge is both arbiter and label source. → Three separately reported agreement numbers, model-family/vote/image-count separation between gold and cheap, the 200 operator-anchored pairs, and **gold-vs-operator < 0.95 stops the program** (D6). Residual risk: a blind spot shared by both OpenAI models and Qwen would survive all three checks.
2. **Developer projects** — the precision killer. → Five rails (§2), the dense Praha block, and the **assembled negative-control cohort** measured as its own stratum (D1). If precision there cannot clear 0.97, `project_suspected` blocks ship propose-only (D10).
3. **The band-width assumption drives every cost number.** → It is a *measured* quantity at the end of W2 and a settings-capped dial thereafter (E25), with the sacrificed recall reported. This is a mechanism, not a forecast.
4. **bazos may be undecidable at 99%** — zero area on 138,997 rows, no broker row, obec-grain pins, 57% pin/town disagreement. → Its own stratum and its own threshold (E23/D10); propose-only if it cannot clear. The area parser gap belongs to the scraper track.
5. **Vision-in-batch and Qwen are both unexercised in this repo.** → Smoke first (E31); fallbacks are sync vision at 2× and `gpt-5-mini` self-consistency n=3 (a weaker independence check, reported as such).
6. **Reasoning tokens.** → Budgeted at 1,200–1,500 output tokens and replaced by the measured `llm_calls` average before any threshold is fixed (E32).
7. **The generation stamp is a non-atomic seam.** → Intent recorded before the call plus a reconciler sweep (E40). Not exercised in this program (D4), so the seam is a design obligation carried into Appendix A, not a live risk here.
8. **`MergeError` on a queue race.** → Explicit policy (E41): terminal vs retry vs poison, never silent.
9. **Guards at the wrong grain.** → Evaluated at property grain too (E4), or the engine manufactures merges the last rail would refuse and counts them as errors.
10. **Reversibility is asserted, not measured** — it is the claim that would license the threshold, and under D4 this program cannot measure it without doing the very writes it forbids. → It stays an *open* risk: W6's decision package states the claim as argued (from `unmerge_group`'s deterministic replay and the single-child group shape, E38) and names **A5 — the revert rehearsal** as the operator decision that must close it *before* any write path is switched on. Residual risk: `recompute_one` / `sync_browse_list` run inline per merge, so a bulk revert is a bulk read-model rebuild of unmeasured duration.
11. **Silent changes to operator state** (CLAUDE.md rules 16/18/22). → E44/D5: every proposal shows exactly what would move, in the UI, before anything is ever applied.
12. **Storage growth, and the temptation to index a hot shared table.** → Measured sizing, the `ad_anchor_images` knob, no multi-GB pair table, and **D8: no DDL on `public.images` — no shared-table index at all in this program**. If the image lane proves slow on the existing indexes, that is a finding for the rollout conversation, not a migration.
13. **Cron throttle** (80–256 minute gaps on a `*/30` lane). → Nothing latency-sensitive runs on cron; a future band drain would run sync inside the worker.
14. **Collision with the parallel operator-guided program.** → Separate schema, package, workflow, concurrency group, settings prefix, `called_for` values; **`llm_calls` is the only shared table this program writes** (three new `called_for` values), and under D7 this engine reads nothing the other program produces. Two engines eventually writing through one chokepoint is a **rollout** problem, and it is explicitly deferred to Appendix A rather than mitigated by importing the operator's merge history — D7 forbids that import.
15. **Every corpus number here is doc-sourced, dated 2026-09-08 to 2026-09-15, and unverified live.** W0's census is the first real measurement and may move the region choice, `L`, `c`, `b` and the cluster cap materially.

---

## 15. Ledgers

### Progress ledger

One row per wave, appended as it closes. A wave is **closed** only when its gate is met and the evidence line names where the number came from. The SPA's `/autodedup/progress` page is the per-*iteration* view of the same story, read from `autodedup.iterations`.

| wave | opened | closed | gate met? | evidence (artifact / run id / query) | measured cost | notes |
|---|---|---|---|---|---|---|
| W0 Census | 2026-09-16 | 2026-09-16 | yes | PR #1482 merged as `f236d2f7`; census run `35088558149` (job `lane` success); artifact `autodedup-census-35088558149`, 17,455 B | $0 | delta probes land in W0b |
| W0b Doc + foundation | 2026-09-16 | | | | $0 | migrations 527/528; B6 dropped (D7) |
| W1 Export + harness + progress page | | | | | | |
| W2 Blocking + core features | | | | | | |
| W3 Ground truth | | | | | | |
| W4 Model + thresholds | | | | | | |
| W5 Validation UI | | | | | | |
| W6 Full trial pass | | | | | | terminal gate of this program |
| W7 Realtime lane (design-only unless approved) | | | | | | |

### Measurements ledger — every number this design guessed, replaced by a measured one

| # | quantity | design assumption | measured | source | when |
|---|---|---|---|---|---|
| M1 | `L` new listings/month | 170,000 | | census B5 | W0b |
| M2 | same-portal re-post base rate | unknown | | census B3 | W0b |
| M3 | `ad_max_cluster_size` | 8 (provisional) | | census B3+B4 | W0b |
| M4 | remax location reach | contradicted | | census B2 | W0b |
| M5 | per-portal phash/CLIP coverage | unstated | | census B1 | W0b |
| M6 | hard recall ceiling (`no_signal_at_all`) | unknown | | census block layer | W0b |
| M7 | candidates/listing p50 / p99 | 12–25 / ≤60 | | W2 estimate mode | W2 |
| M8 | band width `b` | 0.015 | | W2 on the real cohort | W2 |
| M9 | `ad_catalog_df` | 8 | | W2 | W2 |
| M10 | judge output tokens per verdict | 1,200–1,500 | | `llm_calls` | W3 |
| M11 | $/pair T1 / T2 / gold | $0.00086 / $0.00284 / $0.018 | | `llm_calls` | W3 |
| M12 | cheap-vs-gold agreement | ≥0.93 target | | W3/W4 | W4 |
| M13 | gold-vs-operator agreement | ≥0.95 **gate** (D6) | | W5 session | W5 |
| M14 | auto-merge precision (Wilson LB, n≥380) | ≥0.99 target (D3) | | W6 | W6 |
| M15 | missed-duplicate rate per listing | ≤2% target | | recall probe | W6 |
| M16 | blocking recall (exhaustive bound) | unknown | | O(n²) sparse block | W6 |
| M17 | cluster fragmentation | ≤1.15 target | | W6 | W6 |
| M18 | 500-merge revert wall-clock | ≤30 min target | | **not measured in this program (D4)** — rehearsal A5 | deferred |
| M19 | `image_band` real bytes/row | ~80 B | | after W2 load | W2 |
| M20 | steady-state monthly spend | $87 | | `llm_calls` | W7 (if built) |

### Decisions ledger

All ten ruled on **2026-09-16**, **ruled by: program lead (operator delegated)**. These rulings are binding and supersede the design text wherever the two differ.

| # | decision | ruling | date |
|---|---|---|---|
| D1 | trial cohort shape and size | **Four blocks sized to budget:** primary district town 2,000–3,500 all-time; a sparse town 500–700 (which is also the exhaustive O(n²) blocking bound); one Praha `cast_obce_kod` quarter 1,000–1,500 (developer stress); an assembled negative-control set ≤ 800 (same `ruian_adm_kod` different floor + shared dHash on ≥ 5 listings). **Final pick after the census.** | 2026-09-16 |
| D2 | spend gate mechanics | **<$10/run autonomous; $25 hard cap per run; $200 total program.** Every paid lane carries `--max-usd` and asserts the batch read **"done"**, not merely "drew". | 2026-09-16 |
| D3 | the precision bar, as arithmetic | **Wilson lower bound ≥ 0.99 on n ≥ 380 auto-merge pairs, point ≥ 0.995, and no stratum below 0.97.** | 2026-09-16 |
| D4 | shadow vs canary | **The ENTIRE trial is shadow mode — decide, store, write nothing.** Any canary or rollout is a later operator decision and is explicitly *not* part of this program's deliverable (Appendix A). | 2026-09-16 |
| D5 | operator-state movement | **Adopted for the (future) write path: report what operator state moved** — and, in shadow, what *would* move, shown per proposal in the validation UI (E44). | 2026-09-16 |
| D6 | the circularity stop | **Adopted: gold-vs-operator ≥ 0.95 on a 200-pair operator session run through the validation UI, else the program stops.** | 2026-09-16 |
| D7 | how clean is the clean slate | **TOTAL. The engine reads NOTHING from `property_merge_events` — no unmerge or split history, no seeds, no evaluation labels, not even counts. Census probe B6 is dropped.** The generation stamp survives only as a future *write* (E40). | 2026-09-16 |
| D8 | storage: anchor images and the shared-table index | **3 anchor images. NO shared-table index — no DDL on `public.images` — anywhere in this trial.** | 2026-09-16 |
| D9 | backfill budget | **Corpus backfill is out of scope for this program.** Its arithmetic is retained in Appendix A for the later conversation only. | 2026-09-16 |
| D10 | what the engine may give up | **Per-stratum thresholds; weak strata ship propose-only.** | 2026-09-16 |

**Extra scope rulings (same date, same authority).** (i) The SPA gets a **progress page in the top-line menu** showing each iteration — sample statistics, approach, tools used, cost — backed by `autodedup.iterations`, and it **ships in W1, before the validation views**. (ii) **W2's feature scope starts at a core set of ~25 and grows only by error analysis**; certificates K-A/K-B/K-C stay. (iii) **The only shared writes in this program are the three `llm_calls.called_for` values**; no other shared table is written.

---

## 16. Standing notes

- **Numbers are cited by code and tests — never renumber E1–E44.** A rule that changes meaning gets a new number and the old one is marked superseded.
- **A merge is not a deploy, and a merged PR is not an applied migration.** Migrations apply via `apply_migration.yml` *before* the PR merges; Railway rollout is confirmed via `gh api repos/{owner}/{repo}/commits/<sha>/status`.
- **Shadow is the mode.** If any document, comment or code path in this program implies a production write, it is wrong (D4).
- **This document is the program's source of truth.** If a PR changes behaviour described here, it updates this file in the same PR.

---

## Appendix A — Beyond this program

**Nothing in this appendix is a deliverable of this program.** It exists so the rollout conversation starts from written numbers instead of a blank page. Each item needs its own operator decision, taken *after* W6's decision package.

**A1 — Canary on one block (the former W8).** `autodedup_write_enabled=true` for **one small block only**, one week, with a before/after diff of Browse, Stats, watchdog match counts and the pipeline board (E44/D5). Gate: the operator approves the diff, plus a weekly precision re-audit on 200 fresh merges. Estimated ~$5.

**A2 — Staged backfill of the corpus (the former W9; out of scope by D9).** Region cohorts, newest-first, monthly slices, the revert script proven on a real generation, each slice inside the $150/month envelope and haltable at any slice boundary. The arithmetic, for when that conversation happens: 822,000 × 20 / 2 = **8.2M distinct pairs** (a pair is scored once, not once per side). Free-signal scoring is DB + CPU — at a measured 56.5M pairs in 2h32m of DB time, well under an hour of DB plus a few hours of Actions CPU, **$0**. The LLM band at `b = 0.015` ⇒ 123,000 × $0.0017 = **$209**; at `b = 0.008` (achievable once the model is trained on backfill-era pairs, plus auto-rejecting low-prior pairs where both sides are long-inactive) ⇒ **$111**. **Outside the $200 development budget** either way.

**A3 — The shared-table index.** If the image lane's latency turns out to depend on an index over `images(phash)`, that is its own operator-confirmed migration under a **new name** — `images_phash_idx` already exists on `images(sreality_id)` and an `IF NOT EXISTS` would silently no-op. D8 keeps it out of this program entirely.

**A4 — Coexistence with the parallel operator-guided program.** Two engines writing through one merge chokepoint will eventually disagree. That is a rollout-time design problem, and under D7 it may **not** be solved by importing the operator's merge or unmerge history into this engine.

**A5 — The revert rehearsal (the former §9 metric 10).** Timing a 500-merge revert is the only way to turn §6's reversibility *argument* into a measurement, and it cannot be done in shadow: `merge_properties` writes `properties`, re-points `listings.property_id`, logs `property_merge_events` and carries operator state (`carry_operator_state_on_merge`, `reconcile_pipeline_on_merge`) inside one transaction, with `recompute_one` / `recompute_mf_one` / `sync_browse_list` inline per merge. A rehearsal therefore applies 500 real merges under one `generation` label, runs `scripts/autodedup_revert.py --generation <g>`, and diffs the browse and stats read models before and after. It needs **explicit operator sign-off, a `pg_dump` first, and a named blast radius** — one block, newest listings only. Pass: ≤ 30 min and consistent read models. Until it happens, every threshold argument in this program carries the caveat in risk 10.
