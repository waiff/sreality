# NEW DEDUP — Program plan and living progress ledger

Status: **DRAFT — awaiting operator approval.** Date: 2026-08-05.
This file is the standing home for wave status and next-session pickup instructions.
Companion: `CUTOFF.md` (surgical removal spec, approved separately).

## Mission and non-negotiables

Rebuild the dedup **decision layer** from scratch as a **simulation engine**: every level of the
new workflow computes merge/dismiss outcomes "as if", into a separate droppable schema, over the
entire database and **ignoring all legacy merge decisions**. Nothing writes to production tables
until the whole stack is approved end-to-end. Other rules:

- Operator owns ALL merge/no-merge logic; Claude never invents thresholds/weights/rules.
- **The operator must be able to hold every step of the pipeline in their head** (operator
  instruction 2026-08-27): no new rules, safeguards, or decision mechanisms enter this plan
  until the operator asks for them. Claude surfaces risks and spec gaps as flagged questions
  only — never as designed options.
- Legacy code/comments/design docs (backup branch) are never consulted.
- Legacy manual decisions usable only as diagnostics, **per-case, requested in bold**, expect
  declines (operator prefers fresh manual review). Automatic legacy merges: never.
- Only dům ↔ komerční cross-category merges; sale ↔ rent never pair; same-portal pairs valid.
- Waves gate on operator confirmation; merge to main autonomously once a wave's gate passes.
- Explanatory communication style; every settings-panel knob carries a plain-language blurb.

## Decisions ledger (from the 2026-08-05 Q&A)

| Topic | Decision |
|---|---|
| Teardown | Full removal now; duplicate build-up in Browse accepted until cutover |
| Publication gate | Removed (M-0 flip first, then code+views) |
| Legacy marking | DB-only: `property_merge_events.generation='legacy'` backfill |
| Old DB objects | Queues dropped; decision ledger + golden + LLM verdict caches kept frozen |
| L0 fields | street=`street`/`street_name_key`, geo=`geom` 75 m, dispo=`disposition`, floor=`floor` ±2 (byt only), area=`usable_area` (`estate_area` for pozemek) |
| Area tolerance | **5% general, 2% pozemek** (exposed settings) |
| Clique guard | **PARKED** — operator runs separate location data-quality sessions; candidate audit ships pin/clique statistics only |
| Family semantics | First-shared-family vs waterfall = settings **toggle**, per level (pHash and embeddings separately); **default waterfall** |
| pHash | Global default ≤11 + per-tag overrides (drawing-tag risk) |
| Embeddings (ruled 2026-09-05) | **DINOv3 ViT-B/16, 768-d `halfvec`, corpus-wide, is the PRIMARY embedding for all three consumers — the tag heads, Level 3 similarity, and path B** — conditional on the operator accepting the DINOv3 licence (free of charge + commercial use permitted; the terms in ENCODER-DECISION.md §2.8 are the operator's to accept). If declined: DINOv2 ViT-L/14-with-registers (Apache-2.0). ≥0.98 starting L3 threshold, expect recalibration. **The CLIP lane keeps running on new images in parallel** so results can be compared later. Cadence for new images: OPEN (see 2026-09-05 (b)) |
| Candidate path B (2026-08-27; vectors re-ruled 2026-09-05) | **Image-similarity candidate generation runs in parallel with path A**, all property types: batch k-NN over per-type priority **same-tag** image embeddings proposes pairs, using the **DINOv3 vectors** (same store as the heads and L3) once W3's retag supplies the new tags. **B is only another way to FIND pairs** — everything downstream (levels, rules, settings) is identical to A; nothing B-specific exists. B's two search parameters (neighbor count, minimum similarity to propose a pair) are not yet specified — the operator is asked at build time. Audit C (W5) shows whether CLIP vectors suffice or B should read DINOv2 vectors — operator decides |
| Candidate path C (ruled 2026-09-10) | **Town + attributes, built FIRST and alone; paths A and B are not being built now.** Path C replaces path A's "street + geo" and "geo" tests and every ± metre distance with **"same town" = `listing_location_current.obec_kod`** (the new location engine's projection — never the legacy `listings.obec_id` / `geom`), because the input location data is only reliably right at town grain. Rungs: **C1 = town + disposition**; **C2 not applicable** (town already stands in for both of A's first two location tests); **C3 = town + area**, taken when a disposition is **not available** on either side ("if not available then fall back" — absence, never a mismatch); **radius not applicable**; the rest (area tolerance 5 % / 2 % pozemek, byt floor ±2, sale ≠ rent, dům ↔ komerční only, same-portal pairs valid) stays the same. Keep the build expandable to path A (a second `PathDef`, not a second engine). **That is the ruling.** *Defined by PR 1 and AWAITING the operator's confirmation (open question 1 of 2026-09-10 (a)) — not ruled:* fallback on absence only, never on a mismatch; "not available", per side: disposition NULL/blank, area NULL/zero (`usable_area`, `estate_area` for pozemek), town = no `obec_kod`, floor NULL = rule unchecked and the pair kept; the category guards = the merge chokepoint's (NULL = unknown); scope = the entire database (the mission's wording; open question 2); the floor row `dedup_path_c` in `location_data/serving_contracts.py` (obec, any confidence). |
| Gate 1 (ruled 2026-09-05; supersedes "Probe scope 2026-08-27"; **CLOSED 2026-09-09** — the operator's confirmation, relayed in the 2026-09-10 session brief: "Wave 1 is complete (Gate 1 closed: set finalized, tag model v1 live)"; see 2026-09-10 (a)) | **Target tags = 12**: fasáda, nezařízená místnost, půdorys, katastrální mapa, kuchyně, obývací pokoj, koupelna, garáž, jídelna, ložnice, technické zařízení, domovní vchod (open: which of the two "domovní vchod" tags — exteriér id 2 or interiér id 19). **Machine-made labels COUNT** toward the per-tag target; the operator expects ~300–400 positives per head, machine-labeled under the operator-approved definitions and process. The per-head agreement report stays a **diagnostic the operator reads**, not a threshold in code. ~~**The training set is not finalized or reviewed yet — no training on it until the operator says so.**~~ (superseded by the closure above: the set as finalized is the one tag model v1 was trained on — 11 heads, per 2026-09-09 (d); the confirmation as relayed did not name which "domovní vchod" tag is the 12th, so that question is carried as open question 6 of 2026-09-10 (a), not silently closed). Open question carried: how ostatní's any-two-interior rule is represented at labeling time |
| RunPod | Set up in Wave 1; serverless/on-demand only, **<$1/day** run-rate; may reuse PR #804 harness |
| Vision | GPT-5-mini, manual batches only; qwen pluggable later |
| Taxonomy v1 | The operator-curated `image_training_examples` label set (49 labels: `interier -*`, `exterier -*`, `podklad -*`, standalone garáž/technické zařízení/other); "katastr" ≙ `podklad - katastrální mapa`; tag-family defaults reconfirmed at training-set finalization |
| Exact attrs (L1) | Ships inactive; calibrated only after full stack has produced a sample (Wave 7) |
| Gate 1 counting (2026-08-21) | **Border cases do not count toward Gate 1** — an image nobody could classify is not evidence a tag is learnable. The exclusion is a JOIN on `image_border_cases`, not a stamp on the training row, so clearing the flag makes the image count again with no relabelling. `gate_count` (unparked) is what every coverage surface reads; `confirmed_count` stays the raw inventory total |

## Simulation architecture (Q15-confirmed)

Schema `dedup_sim` (droppable wholesale). Two-tier recompute:

- **Evidence tier (expensive, computed once, reused across runs):** candidate pairs from L0
  (keyed by listing pair + path + inputs; paths = **C** town+disposition (C1) / town+area (C3)
  — built first, migration 492 —, A1 street / A2 geo+dispo / A3 geo+area (not built) and,
  from W3, B image-similarity), and per `(pair, tag_family)` image-comparison
  evidence — best/qualifying pHash distances + pair counts, later best/qualifying DINOv2
  similarities. Evidence rows carry the image-set fingerprint so stale rows recompute when a
  listing's images/tags change. Additive columns expected as criteria evolve.
- **Decision tier (cheap, seconds-to-minutes):** a `simulation_runs` row snapshots the full
  settings JSON; decisions + as-if groups (union-find over merge edges, category guard applied)
  are materialized per run. Threshold/priority/pair-count/toggle changes → decision-tier rerun
  only. Radius/floor/area changes → candidate regeneration (hours, full corpus).
- **Retention:** statistics for all runs; browsable as-if grouping for the latest 3.
- **Compute placement:** GH Actions dispatch for evidence/candidate generation (free, existing
  ops pattern); decision-tier reruns via the API ("Run dedup simulation" button). RunPod pods
  spin up on demand for embedding batches and terminate (zero idle cost).

## Waves

Each wave: prerequisites → mechanics → audit page → testing → results → iteration → **gate**.
Session handoff points marked ⛳ (good places to end a session; update the ledger below).

- **W0 — Backup + teardown + scaffolding.** Execute `CUTOFF.md` §7 (PR-0, backup branch/tag,
  Day-0 freeze + M-0, pg_dump, PR-1/2/3). Scaffold: `NEW DEDUP` nav group (menu pattern per
  recon), placeholder Dashboard + Settings pages, `roadmap/new-dedup.md` track (+ centroid
  data-quality prerequisite entry for the operator's parallel sessions). ⛳ after each PR.
  **Gate 0: operator approves CUTOFF.md before execution; post-teardown verification checklist green.**
- **W1 — Shared prerequisites + labeling program.** `dedup_sim` schema foundation +
  `simulation_runs`; settings framework (registry + explanations); dashboard skeleton (funnel +
  cost table with estimates); RunPod account (operator) + serverless workflow (me); **Labeling
  page** = ClipAudit clone minus dedup block, plus: "new tag vs original tag" toggle, sample
  management, tag add/rename/remove + batch tooling. Secondary CLIP (stronger encoder) relabels
  growing samples over taxonomy v1 into a sim-side proposal store (never `image_clip_tags` —
  gallery-flip hazard); iterate sample until 300 proposals for ≥50% of the **target** categories,
  then assess coverage with operator. Operator confirms/dismisses into the training set. ⛳ per
  sample round.
  **Gate 1: 150 training images (human or machine) per target tag — the 12 tags ruled 2026-09-05 — AND the operator has reviewed and finalized the set.** **CLOSED 2026-09-09** (operator confirmation; tag model v1 live per 2026-09-09 (d)). The dashboard skeleton, W1's one unbuilt bullet, is carried into W2 (its funnel top ships with the candidate audit page).
- **W2 — Level 0: candidate selection — PATH C FIRST (ruling 2026-09-10; paths A and B are
  not built in this wave).** Three PRs, each draft-first with tests and CI green, the ledger
  updated at every handoff: **(1)** the sim candidate store (migration 492: one row per listing
  pair × path × the inputs that produced it, a fingerprint so a changed input re-generates the
  pair; a `simulation_runs` row per generation run; the confirmed parameters as settings);
  **(2)** path C generation as a dispatchable GitHub Actions lane (set-based SQL, full corpus,
  re-runnable — an estimate mode that writes nothing comes first, because the volume at obec
  grain is the open question) plus the funnel counts it feeds; **(3)** the **Candidate audit
  page** (property type × path matrix **with a path-B column from day one** — empty until W3;
  missing-data tables overall, then per portal per type; town/bucket statistics in the place of
  pin/clique statistics — the clique guard stays parked) and the dashboard's funnel top (all
  listings → candidates by type and path). Location is read ONLY through the projection
  (CLAUDE.md rule 24, `docs/design/location-serving-contract.md` §7). Recall diagnostic vs
  legacy manual merges only if granted (**bold request** at that moment). ⛳ after each PR.
  **Gate 2 (the operator's wording, brief of 2026-09-10): the operator, reading the audit page,
  is satisfied that the built path loses no rightful candidates to data quality (poor-geo gaps
  explicitly covered later by path B + the operator's parallel location-DQ work); path B's first
  output reviewed.** Flagged, not resolved: the second clause can only be met once W3 has built
  path B, so as written Gate 2 cannot close before W3 starts — open question 5 of 2026-09-10 (a)
  asks whether it closes on path C alone.
- **W3 — Linear probe + full retag + candidate path B.** Train probe on the gated training set
  (grouped splits, pinned encoder, versioned artifact); validate on the Labeling page;
  campaign-retag the corpus into the sim tag store. Then **path B generation**: a batch k-NN job
  over per-type priority same-tag images on the DINOv3 vectors (off-DB, e.g. FAISS on a
  pod/runner; writes candidate pairs + best-similarity evidence into the sim store; candidate
  audit + funnel gain their B numbers; B's two search parameters asked of the operator at build
  time). **Gate 3: operator accepts tag quality; per-type default tag-family orders
  reconfirmed against final taxonomy; path B volume/quality reviewed with the operator.**
- **W4 — Level 2: pHash.** Evidence computation over candidates (A ∪ B); decision tier; settings
  (threshold 11 + per-tag overrides, pairs required =1, family toggle, drag-priorities per
  type); **pHash audit page** (side-by-side pairs, filter by type/tag/hamming/result);
  **Browse-as-if page** (BrowseExperience reduced-feature adapter over sim groups);
  **Suspicious-properties page** (concurrent price divergence; ≥N listings merged, default 6;
  best-pair-vs-next-tag divergence filter). ⛳ evidence / decisions / each page.
  **Gate 4: visual validation — no easy merges missed, no strong signal underused, threshold calibrated.**
- **W5 — Level 3: embeddings.** DINOv3 ViT-B/16 vectors (corpus-wide, computed on RunPod — the
  same store the heads and path B read), in Supabase;
  evidence + decisions; audit A (pHash-style with similarity), B (click-an-image search),
  C (all-candidates pHash-vs-embeddings comparison, plus CLIP-vs-DINOv3 on the same pairs, since
  the CLIP lane keeps running); dismiss-decision validation; DINOv2 audit
  page. **Gate 5: measurable lift over pHash; similarity calibrated; dismiss confidence decided.**
- **W6 — Level 4: vision.** Batch selector (cohort filters, model routing), robust prompt +
  3-outcome contract, decision counts per type (all operator-editable in settings); results/
  stats/cost page; manual-review queue + notes (sandbox — never read by Claude);
  velocity-based daily cost + review-volume projections.
  **Gate 6: <$1/day projected (ex-backlog) AND ≤50/month to manual review (target <20).**
- **W7 — Level 1: exact attributes.** Analyze accumulated sim outcomes; operator defines the
  filter; activate toggle. **Gate 7: operator approves the filter.**
- **W8 — End-to-end approval + production wiring.** Full-corpus simulation signed off →
  productionize: engine writes real merges through `merge_properties` (generation `v2`),
  backlog + steady-state scheduling, monitoring/health, re-feed of everything undecided,
  Browse-as-if retired, `dedup_sim` dropped, docs/skills/CLAUDE.md updated.
  **Gate 8: operator orders the real run.**

## Parked / open items

- Clique guard (operator location-DQ sessions running in parallel; revisit before W2 gate).
- ~~Same-town candidate rung (operator request 2026-09-08; definition pending)~~ — **RULED
  2026-09-10 as candidate path C** (decisions ledger row + the 2026-09-10 (a) entry). What is
  still open about it is the VOLUME it implies at obec grain in Praha, listed in that entry.
- **Path B risk, noted only — no mechanism designed (no-invented-rules instruction):** B has no
  location anchor, so identical marketing photos (developer catalogs, staged/stock interiors,
  reused renders across a project's units) can become candidate pairs and would merge at L2
  under the operator's current rules. The W3/W4 audits will make this visible; whether any rule
  is wanted is entirely the operator's call, made then.
- Qwen vision provider route (W6).
- Near-duplicate training labels flagged 2026-08-05 (operator cleanup via batch reassign).
- Interim unmerge has no UI home (API-only) until W8. Confirmed again on 2026-09-05 while
  running the Gate 0 checklist: the merge went through Browse's own `POST /properties/merge`,
  the unmerge had to go through the API by hand.
- ~~CLAUDE.md "psql" guidance inoperable in cloud-only mode~~ — FIXED 2026-09-05 (#1286). The
  bullet now says what to do when `psql` is absent or `SUPABASE_DB_URL` is unset: fall back to
  the Supabase MCP `execute_sql`, and carry the INTENT of the psql preference across (it was
  about context cost, not correctness) — one aggregate row per question, `md5(string_agg(...))`
  to compare a list without printing it, never a wide result set.

## Runbook — adding a head (verified on `podklad - property list`, 2026-09-07)

The operator's requirement: adding a head ad hoc must be a repeatable process, because the
full dedup engine will keep needing new ones. Every step below is an existing surface or lane;
the two gaps found while adding property list are closed in the same PR that adds this section.

1. **The tag exists in the taxonomy** (Labeling page → add tag), with the `family - name` form.
2. **Make it a head**: set its routing categories on the Taxonomy page (which property types it
   serves — byt / dům / komerční / pozemek / ostatní). A tag with NO routing categories is not
   a head: the training-set page, the heads read and the labeler all key on that column. *Gap
   closed: this used to be settable only by migration (457).*
3. **Write the definition with real exclusions.** The model treats DOES NOT COUNT as law and
   `confusable_with` as advice (measured on fasáda: 0.60→0.95 precision). A boundary written on
   the *neighbour's* side is invisible when THIS head is labeled alone — mirror it. **Mirroring
   means editing the neighbours too**: adding a head between two existing ones changes THEIR
   boundaries, and a neighbour left on its old wording will keep claiming what now belongs to
   the newcomer. Bump their definitions in the same change; their existing labels then read
   "old wording" on the training-set page, which is the honest signal, not a regression. Rules:
   what the image is OF; three tiers on a space head; exclusivity on a document head;
   `means` ≤ 500 chars, `leave_out_when` ≤ 300.
4. **Seed candidates.** In order of yield per dollar, measured: the operator's drafts (96%),
   a CLIP near-tag draw (41%), random (≈1% for a rare head). A NEW head has no positives to
   seed from — seed the near-tag draw from its nearest relative (`--near-tag <relative>`;
   property list from půdorys). *Gap closed: the seed used to have to be a labeled head.*
   **`--like-tag <sibling>` draws the images that sibling has ALREADY been judged on**, which
   is the right draw whenever the new head's boundary runs against an existing one: both heads
   are then judged over the same photos, so every image that made one say yes has been asked
   about the other and the boundary is reviewable instead of inferred. It takes the sibling's
   POOL, never its verdicts — filtering by them would bake an unreviewed boundary in as ground
   truth. (Operator, adding 3D plán beside půdorys: *"use the pictures we have already used for
   other heads"*.)
5. **Label** with `label_images.yml`: dry run, then a small count, then scale. `--tags` names
   ONLY the new head so nothing else is re-judged.
5b. **After ANY definition revision, `--rejudge <tag> --rejudge-state positive`.** A revision
   makes stale labels eligible but nothing draws them preferentially, so old answers under old
   wording survive indefinitely — which is how a head keeps positives its current definition
   would refuse. Scoping to positives is the cheap half: a NARROWING revision can only lose
   positives, so the negatives need no second opinion and ~10k images of budget stays unspent.
   (A widening revision is the other case and does need the negatives re-asked.)
6. **The cutoff applies automatically** (default 300, editable per head); review the in-set
   positives on the training-set page; "Confirm the other N" per page; notes on corrections.
7. **Optional gate.** Without an exam sitting there is no measured precision/recall for the
   head; the operator's review IS the quality control. Say so in the ledger.
8. **Ledger entry** here, memory note, roadmap line.

## Progress ledger (update every session, newest first)

- 2026-09-10 (c) — **W2 PR 3: the Candidate audit page + the dashboard's funnel top.**
  The last of W2's three PRs, and the surface Gate 2 is read against. Nothing is generated or
  decided here — the page is read-only, admin-gated, and every figure on it is read off the
  generation row's `stats` (written once by `scripts/dedup_candidates_generate.py:generation_stats`).
  - **The page** (`/new-dedup/candidates`, `frontend/src/pages/NewDedupCandidates.tsx`), five
    sections in the order Gate 2's question is argued: **the funnel** (every listing → known to
    the location engine → placed to a town → has a comparable attribute → ended up in a pair),
    **property type × path** (a column per path, **A and B present and empty from day one** —
    an omitted column would hide the gap; each carries the plain-language reason it is empty),
    **missing data** overall and then per portal per property type, **town statistics** (the
    pair histogram over towns, the towns that produced the most, the largest (town, disposition)
    buckets, the town-assignment breakdown — the pin/clique analogue; the clique guard stays
    parked), and **the parameter set** the run used. The run is in the URL
    (`?generation_id=`), so comparing two parameter sets is a link the operator can keep;
    without it the newest SUCCESSFUL path C run is shown.
  - **The funnel is ONE component** (`frontend/src/components/new-dedup/CandidateFunnel.tsx`)
    rendered by both the audit page and the program dashboard, so the two surfaces can never
    disagree about how many listings the program can reach. Its last step is broken out **by
    property type** (the `listings_with_candidates` rows) and **by rung** (C1 / C3), which is
    the wave text's "by type and path"; the dashboard has no matrix underneath it, so the type
    half of that requirement lives in the funnel or nowhere. This closes **W1's carried-forward
    dashboard skeleton** (named in entry (a) as shipping with PR 3).
  - **The API** (`api/routes/new_dedup_candidates.py`): `/new-dedup/candidates/overview` is the
    whole page in one read, plus `/generations` for the run picker. **Migration 492 is still not
    applied**, so every route answers `store_ready: false` with empty content rather than a 500,
    and the page says the store has not been created yet.
  - **A gap is rendered as a gap.** Two calls worth recording, because both look like data:
    a run that carries no per-town listing count shows an em dash, not a zero; and the town
    histogram's **0-pairs band is blank**, because `generation_stats` builds that histogram from
    the pair rows — a town that produced no pair never enters it, so a printed "0" there would
    read as "every town produced a pair". (Only estimate mode feeds `_distribution` every town.)
  - **The lane's first two dispatches, on `main` after PR 2 merged (both read-only).**
    `verify` over three small towns — Aš (554499, 665 listings), Bohumín (599051, 601),
    Benátky nad Jizerou (535451, 475): **AGREE on all three** — the SQL returned exactly the
    pair set the Python rule accepts (7,262 / 3,091 / 1,745 pairs), rung for rung, evidence
    value for evidence value, zero disagreements of any class. So the set-based SQL IS the rule
    as written in entry (a), on real data, before a single row is stored. A first sense of the
    volume at small-town scale: 3–11 pairs per listing. `estimate` over the whole corpus
    (scopes all + active) was dispatched right after; its job summary is the number the open
    questions 2 and 3 are answered with.
  - **Not decided.** The page has no finished generation to show until the estimate is read,
    migration 492 is applied and a generate runs. Gate 2 stays open, and so do the six open
    questions of entry (a) — including question 5 (whether Gate 2 closes on path C alone).

- 2026-09-10 (b) — **W2 PR 2: the path C generation LANE — `new_dedup_candidates.yml`,
  three modes, nothing run yet.** PR 1 (entry (a)) built the store and wrote the rule as
  Python; this PR turns the rule into set-based SQL and a dispatchable GitHub Actions lane that
  can be run before, and independently of, migration 492 being applied.
  - **The SQL** (`toolkit/dedup_candidates_sql.py`): one `base` per town — the projection row
    (`obec_kod`, granularity floor applied by RANK) joined to the listing's attributes, nothing
    legacy — and one statement per rung, in three forms that share ONE select body so they
    cannot drift: INSERT (upsert into the store), COUNT (estimate) and ROWS (verify). C1 joins on
    equal disposition; C3 takes pairs with a disposition missing on either side and compares
    areas. **The area rung uses a log-band**: `band = floor(ln(area) / w)`, w = −ln(1 − t) for
    the widest tolerance t, so "within t of each other" implies "adjacent or same band", and a
    town-wide range join (Praha: ~3 × 10⁹ comparisons as a nested loop) becomes three equality
    hash joins on `band + d`, d ∈ {−1, 0, +1}, with the exact per-pair tolerance re-checked
    after. Every statement is a `*_SQL` constant, so CI's schema-replay job PREPAREs each one
    against a freshly migrated database on every push.
  - **The lane** (`scripts/dedup_candidates_generate.py`): `estimate` (reads only — pairs per
    rung and per town for every scope asked, the funnel per portal × type, the largest
    (town, disposition) buckets, the town-assignment breakdown; needs no 492 table), `verify`
    (reads only — for the named small towns, the SQL's pair set must equal the oracle's,
    rung for rung and evidence value for evidence value; the job FAILS on any disagreement),
    and `generate` (writes — one `simulation_runs` + one `candidate_generations` row, then town
    by town in id-range chunks with a resume cursor after every chunk; `resume=true` continues
    the last running generation of the same parameter set; the audit statistics are computed
    once at the end onto the generation row; the stale sweep runs only after a COMPLETE run,
    never after a `blocks`-limited pilot). `dry_run` is the default for `generate`.
  - **One new setting, marked undecided**: `l0_candidate_scope` (`all` / `active`), because
    the operator's open question 2 needs a switch the estimate can report both sides of. It
    is part of path C's fingerprint, which moves the ruled defaults' fingerprint from
    `ebc60a2894867cc7` (PR 1) to `0ec174f0693a2c01` — no pair rows exist yet, so nothing is
    orphaned. Its default, `all`, is the mission's "entire database".
  - **Skill updated** (`scraper-ops`, plus `references/new-dedup-candidates-lane.md`): the
    order of operations is estimate → verify on two or three small towns → generate dry-run →
    a Brno pilot → the corpus.
  - **Reviewed adversarially before merge (three lenses, two refuters per finding): three real
    defects, fixed.** (1) The byt floor guard was three-valued — a NULL `category_main` (which
    exists: remax/mmreality can leave it unknown) made `NULL = 'byt'` poison the whole AND, so the
    SQL dropped pairs the oracle keeps and would have written NULL into a NOT NULL column;
    `COALESCE(category_main, '')` makes it two-valued, and verify mode now treats a NULL
    `floor_checked` as a mismatch. (2) A resumed generation took "partial" from the CURRENT
    dispatch, so resuming an interrupted pilot without repeating `blocks` would have run the
    corpus-wide stale sweep; the scope now comes from the generation row, a differing `blocks`
    is refused, and a `failed` generation is resumable (reopened first). (3) A zero area
    tolerance — registry-legal — overflowed the int4 band; the band is a bigint.
  - **Not run.** Dispatching needs the workflow on `main`; the estimate is the first dispatch
    and is read-only. The generate needs migration 492 applied — asked together with the
    estimate's numbers.

- 2026-09-10 (a) — **Gate 1 CLOSED. Wave 2 opened on the operator's PATH C ruling. PR 1 of
  three: the candidate store (migration 492 — written, NOT applied), the parameters as settings,
  the rule as code. And one number the operator has to see before anything is generated.**

  **Gate 1, closed.** The operator confirmed on 2026-09-09 that Wave 1 is complete: the training
  set is finalized and tag model v1 is live (entry (d) of that day). The one W1 bullet still open
  — the dashboard skeleton (funnel + cost table) — is carried into this wave and ships with PR 3,
  because until a generation has run there is still nothing for a funnel to count.

  **The ruling, in the operator's terms.** Candidate selection has several paths; **paths A and B
  are not being built now — path C is, alone, and the build stays expandable to A.** Path C
  differs from A in one move: every "street + geo" and "geo" test, and every ± metre distance, is
  replaced by **"same town"**, because our location data on input can be inaccurate and the best
  relatively confident accuracy is at town level. Town is `listing_location_current.obec_kod` —
  the new location engine's projection (CLAUDE.md rule 24; the serving contract's §7 query is the
  ONE way location is read), never `listings.obec_id` / `geom` / `street`, even where those
  legacy columns are better populated today: they are being deprecated. Rung **A1 → C1 = town +
  disposition; A2 → not applicable; A3 → C3 = town + area; radius → not applicable;** the rest
  stays the same (5 % / 2 % pozemek area tolerance, byt floor ±2, sale ≠ rent, dům ↔ komerční the
  only cross-type, same-portal pairs valid, the whole database, legacy merges ignored).

  **What the brief left to be defined, and how PR 1 defines it (the operator asked for "the 'if
  not available then fall back' rule and what 'not available' means on each side" — these are
  the answers, written so they can be overruled in one line each):**
  1. **Fallback is on ABSENCE only.** A pair is evaluated on C1 when BOTH listings carry a
     disposition; if EITHER lacks one, the pair is evaluated on C3 instead. Two present but
     different dispositions are NOT a candidate and do not fall back to area — a mismatch is an
     answer, not a gap.
  2. **"Not available", per field, per side:** disposition = NULL or blank after trimming; area
     = NULL or zero (`usable_area`; `estate_area` when the listing is pozemek); town = no
     `obec_kod` (no projection row counts as unknown, never as "no town"); floor = NULL.
  3. **A missing floor does not drop the pair.** The byt ±2 rule is checked only when both sides
     are byt with a known floor; otherwise the pair is kept and marked `floor_checked = false`,
     so the audit page can say how often the rule could not be applied. (The alternative —
     dropping the pair — would lose candidates to data quality, which is what Gate 2 guards.)
  4. **The category guards are the merge chokepoint's, verbatim** (`toolkit/property_identity.py`):
     `category_type` equal, NULL = unknown = not a conflict; `category_main_compatible` (equal, or
     dům ↔ komerční). The pozemek area tolerance applies when EITHER side is pozemek.
  5. **Floor for the town key**: a new row `dedup_path_c` (obec, any confidence, `obec_kod`
     present) in `location_data/serving_contracts.py` — the contract said the rung "needs its own
     operator ruling"; it now has one, so the row exists rather than an inline exception.

  **Built (PR 1).** Migration **492**: `dedup_sim.candidate_inputs` (one parameter set: path,
  fingerprint = 16 hex of SHA-256 over the canonical inputs JSON + generator version — the ruled
  defaults hash to `ebc60a2894867cc7`, pinned by a test so a moved default is a visible change —,
  the inputs themselves), `dedup_sim.candidate_generations` (one run of one parameter set, linked to its
  `simulation_runs` row; `progress` = the lane's resume cursor, `stats` = the funnel and audit
  numbers, computed once at the end), `dedup_sim.candidate_pairs` (PK `(inputs_id, lo, hi)`,
  `lo < hi` enforced, `rung` an attribute — a pair sits on exactly one rung per path — and TYPED
  evidence columns: the shared disposition, the two areas and their gap in percent, the two
  floors and `floor_checked`; no per-row jsonb, no foreign key into `listings`). A re-run under
  the same inputs UPSERTS (same key space; `generation_id` and `last_seen_at` move); changed
  inputs land in a new key space; the pair table carries NO secondary index — the primary key's
  `inputs_id` prefix already serves the stale sweep and every per-parameter-set aggregate, and
  leaving `generation_id` / `last_seen_at` unindexed keeps a re-run's upserts cheap. Two
  generations of one parameter set are kept from overlapping by the lane's concurrency group, not
  by the schema. `simulation_runs.triggered_by` gains `'lane'`. Settings: one new knob,
  `l0_path_c_town_key` (only choice `obec_kod`, in the fingerprint; a finer key for statutory
  cities would be a second choice there AND its column in the generation SQL — the lane refuses
  a value its SQL does not implement), and the geo-radius / floor / area blurbs now say which
  path each reaches. Code: `toolkit/dedup_candidates.py` — the path/rung registry
  (`PATHS["C"]`, rungs C1/C3 with plain-language explanations), `path_inputs` + `fingerprint`,
  the rule as pure Python (`evaluate_pair`, the ORACLE the lane's SQL is held to in tests), and
  the lifecycle (`begin_generation` → `record_progress` → `finish_generation`, a failed run keeps
  its row). Tests: the migration's shape, every definition above, symmetry of the rule, the
  lifecycle over a fake connection; both RLS rails register the three tables. **The migration is
  NOT applied** — additive, but the operator's rule is apply-after-OK; PR 2's estimate mode does
  not need it.

  **Measured 2026-09-10 (projection + listings, read-only; the lane's estimate mode will give the
  exact figure):** projection rows 755,650, of which **610,119 carry an `obec_kod` (80.7 %)** —
  path C's whole population; `geo_blockable` only 77,407 (10.2 %) and a street key on 132,530
  (17.5 %), which is the size of the gap path C exists for. Listings 819,401 (388,629 active);
  disposition on 362,133, `usable_area` on 513,941, `estate_area` on 262,288, floor on 339,849.
  **Praha (obec 554782) holds 108,552 projection rows.** A 3 % sample of them: 70 % carry a
  disposition; the single largest C1 bucket, pronájem / byt / 2+kk, scales to ~16k listings —
  **~1.3 × 10⁸ pairs on its own before the floor rule**, and Praha's C1 rungs together are on the
  order of 10⁸ rows all-time (roughly a seventh of that active-only; every other town is at least
  16× smaller, Brno being ¼ of Praha's listings and so ¹⁄₁₆ of its pairs). **This volume follows
  from the rule as stated — "same town" at obec grain in the capital — not from any knob.** It is
  not a problem the code hides: the store is built narrow for it, and PR 2 runs an estimate
  before writing a row. Whether the number is acceptable, or whether Praha needs the "town
  district" the operator's first phrasing mentioned, is the operator's call, with the exact count
  in hand. One caveat on every coverage number above: the idnes / ceskereality / realitymix archive
  sweeps run from 2026-09-10, so their older listings are thin on the projection until each reports
  `reached_end=true` (serving contract §6) — "with a town" will grow for those three portals without
  any change to path C, and the audit page's per-portal table is where that shows.

  **Open, and the operator's to decide (numbered; answers move into the decisions ledger):**
  1. Confirm the four definitions above (fallback on absence only; "not available" per field;
     missing floor = kept, unchecked; chokepoint category guards).
  2. **Scope:** the whole database (active + inactive, per the mission) is what is built. The
     estimate will show all-time and active-only side by side; say which the first generation
     runs on.
  3. **Praha at obec grain** (10⁸-row order): accept, or rule a finer town key for the statutory
     cities (`momc_kod` exists on the projection). Nothing is designed until ruled.
  4. OK to apply migration 492 — asked at the PR 2 handoff together with the estimate.
  5. **Gate 2's second clause.** The brief's Gate 2 wording ends "path B's first output reviewed";
     path B is built in W3, which sits after Gate 2 in the wave order. Does Gate 2 close on path
     C alone, with B's first output reviewed at Gate 3 as already written there?
  6. **The 12th target tag.** Gate 1 was ruled at 12 tags with "domovní vchod" (exteriér id 2 or
     interiér id 19) left open; the finalized set that trained v1 has 11 heads. Is the 12th tag
     dropped, or still to be added as a new model version?

  **Housekeeping.** Draft PR #1186 (2026-08-26, "L0 parallel candidate path B — geo-free town
  blocking", docs only) proposed town blocking under the name path B before B became image
  similarity; this ruling supersedes it under the name path C — closing it is suggested, not done.

- 2026-09-10 — **The location engine is readable; read it, not the legacy columns** (recorded
  by the location track, not a dedup session). All seven W2 portal contracts un-shadowed
  2026-09-09; `listing_location_current` / `property_location_current` carry the precision axes,
  the RÚIAN codes and precomputed `addr_block_key` / `building_block_key` / `street_block_key` /
  `geo_cell_key` (the last only when `geo_blockable`). The 2026-09-08 (b) sequencing note — "W2
  should read location through ONE query it can later point at the location projection" — can
  now point at it directly: the query, the dedup floors (`dedup_rung_0a/0b/0c`, `dedup_tier_1/2`)
  and the do-not-read list are `docs/design/location-serving-contract.md` (CLAUDE.md rule 24).
  The L0 "geo 75 m" rung is Tier 2 there, so it applies only to `geo_blockable` rows; the
  "same town only" rung is definable on `obec_kod` + `admin_assignment_method` but still needs
  its own ruling. Coverage caveat: idnes / ceskereality / realitymix archive sweeps run from
  2026-09-10 — until each reports `reached_end=true`, their older listings are thin.
  `location_v2.dedup` is this program's flag to flip, after its own 7-day shadow compare.
- 2026-09-09 (f) — **The bake-off page's top selectors now govern the whole page, and the zoom
  panel leads with the top head per model (two operator asks, same day).** (1) The ARM
  dropdown and TRAINED ON chips in the cell controls (Views B and C) offer only the arms and
  modes turned on in the chips at the top — the one exception is an arm or mode a link already
  names for the cell, which stays listed so it can be undone; turning off the arm or mode the
  cell is on hands the cell to the first one still on, because a cell left on a switched-off
  arm would be a choice with no control to undo it. The zoom panel already filtered to the
  selection by default (its **Every arm** chip widens). (2) The zoom panel now opens with a
  **top head per model** block: for each selected arm (and mode, on the split on show) the
  strongest head, its score, and how many heads it beat — "only head that scored it" when there
  was no contest, which on cross-validation is the common case, since a photo is scored only by
  the heads whose training set holds it (the exam scores every photo with every head). The
  per-head detail groups follow unchanged.
- 2026-09-09 (e) — **"All photos by score" gets the training-set grid's controls, to the
  control: the same five page sizes (50 … 10,000), "x–y of N", a last-page jump, and the
  Small/Large photo switch (operator ask, same day).** To carry them, `/runs/{id}/scores`
  moved from the keyset cursor described in (c) to **limit/offset paging** — which is safe
  precisely because the ordering was already total: `score desc, image_id desc` has a unique
  tiebreaker, so an offset names the same photos on every visit; the cursor was guarding
  against a reshuffle that this ORDER BY cannot produce. `limit` now accepts up to 10,000 (the
  widest training-set page; a cell is at most the run's ~10k-photo corpus). Ranks are now
  known on every page (offset + place), so the "somewhere below the top" state for a
  mid-ranking link is gone. Page state travels in the URL as `n` (page size) and `soff`
  (offset), rewound with the rest of the position keys when the cell changes; the photo size
  is page-local state, as on the training-set page, and uses the same 8rem/16rem grid
  minimums so "large" means one thing on both pages. Views A and B are untouched.
- 2026-09-09 (d) — **Tag model v1 is LIVE: migration 490 applied, the whole loop
  (promote -> score -> activate) run for real, and the winner rule graded for the first time.
  On the sealed exam the winner names the operator's own tag 95.7% of the time; the open
  problem is the photos that are none of the eleven tags.** Entry (b) built the machinery and
  deliberately left it empty — "nothing is trained, scored or activated yet". This entry is
  that sentence retired. Every number below is **measured on 2026-09-09**, not projected.

  **Two words used throughout, defined once.** A model is **activated** when it becomes the one
  version consumers read — a separate, explicit flip, so nobody ever meets a half-scored model.
  A **floor** is a minimum the *consumer* insists on before it believes a tag ("give me the
  winner, but only if its score clears 0.5") — it is not a per-head yes/no and does not
  reintroduce one, because the winner is still chosen by competition between heads and the floor
  only decides whether to trust the competition's answer at all.

  - **What went live.** Migration 490 was applied to production at **~08:45 UTC**: the three
    tables of entry (b) exist in the **public** schema with row-level security on and the
    browser-facing roles (`anon`, `authenticated`) revoked — verified after applying, not
    assumed. The three pull requests of the round merged in order — **#1365** (the narrowed
    experiment, ask 2), **#1366** (the tag-model shape, ask 1), **#1368** (the page's third view,
    the narrowed defaults and the per-photo probability modal, asks 3 and 4) — and the page
    rollout was confirmed on Railway rather than inferred from the merge.
  - **What v1 IS — one frozen decision, named.** Promoted from **bake-off run 1**, arm
    **`dinov3-b16@768/bf16`**, mode **`pos_neg`** (the heads trained on the operator's own yes
    *and* no labels), **11 heads** — tag ids 3, 17, 22, 25, 28, 39, 42, 43, 45, 46, 48 — each
    refit on all of its training rows rather than on a cross-validation fold, because the
    shipped head should learn from every label there is. Promotion **copied** each head's
    bake-off numbers onto the model so they cannot drift from the artifact they describe:
    katastrální mapa 0.969, půdorys 0.961, obývací pokoj 0.918, 3d plán 0.909, letecký snímek
    0.881, technické zařízení 0.873, and property list **0.687**, still the one weak head.
  - **What it scored, and what that cost.** **9,514 images** — the 9,264 training photos plus
    the sealed 250-photo exam — read from the bake-off arm's own stored vectors (`source
    bakeoff:1`), **0 missing**, in **about 70 seconds on a free GitHub runner**. Inference is a
    dot product and a logistic function in plain Python, so there is no ML install, no GPU and
    no bill anywhere in this step. v1 was then **activated**: it is THE active model, and
    `toolkit.tag_models.winners()` plus `GET /new-dedup/tags/images/{image_id}` now answer from
    it. The lane `tag_model.yml` carried all three stages (promote / score / activate, plus
    status) and each ran with `dry_run=false` — the dispatch-only lane built in (b) has now done
    real work, not only a rehearsal.

  **The winner rule's first honest grade.** The exam is the only place this can be measured
  properly: 250 photographs sealed away from every training tray, with the operator's own
  answers on them. All eleven heads scored all 250, and the winner — the highest scorer — was
  compared with what the operator said.
  - **When the photo really is one of the eleven tags, the winner is right 95.7% of the time.**
    Of the 94 exam photos carrying a human positive on one of the eleven heads, the winner names
    that tag in 90 of them. Set that beside the same heads' **mean exam F1 of 0.73** under
    independent yes/no decisions and the gap is the whole argument for ruling 1: **competition
    between heads removes most cross-tag false alarms.** A head that fires on a photo belonging
    to another head no longer produces a wrong tag — it just loses. **96.8%** of those correct
    winners also clear a 0.5 score.
  - **The remaining problem is "none of the above", and it is the real one.** 156 of the 250
    exam photos carry no positive on any of the eleven heads — they are bedrooms, corridors,
    balconies, whatever the eleven tags do not cover. **All of them get a winner**, by
    construction: an argmax always names something. **41% of them get a winner scoring 0.5 or
    higher** — a confident-looking tag on a photo the model has no tag for.
  - **So a consumer floor is not optional in practice.** At 0.5 it keeps **96.8%** of the true
    tags while cutting the confident-looking wrong ones on "other" photos from 100% to **41%**;
    raising it trades one against the other. **Whose decision this is has not changed** — the
    floor belongs to the consumer, because the product takes no per-head yes/no (ruling 1). The
    alternative is an explicit **"other" head**, trained on exactly those photos so the argmax
    has somewhere honest to put them. That is an operator decision and is listed below, not
    taken here.
  - **The training-pool numbers, marked as what they are.** 88.4% of the 2,940 training photos
    carrying a positive get it as their winner; 72% of all 9,514 scored photos clear 0.5; the
    mean winner score is 0.70. These are **in-sample** — v1's heads were refit on these very
    photos — so they are optimistic and belong beside the exam figures, never instead of them.
  - **A measurement gap found, and the fix belongs to run 2.** The bake-off's cross-validated
    scores exist only for each head's **own** training rows — every head was validated on its
    own tray, not on the whole pool — so there is no way to grade a winner **out-of-fold**
    (scored by a model that never saw the photo) across the training pool. Restricting a
    "winner" to that subset produces **99.8%**, a number that measures nothing but the
    restriction. **NEXT for run 2: score every image with every head out-of-fold, under one
    shared grouped split used by all heads**, so the winner rule gets an honest training-pool
    grade beside the exam's 94 photos instead of resting on them alone.

  - **The iteration loop is now proven end to end, and it is free.** `a new bake-off run ->
    promote -> score -> activate`, all of it on CPU at no cost while scoring stays inside the
    bake-off arm's 9,514 photos. **Adding heads is a new version**, never an edit to this one —
    an argmax is only meaningful over one frozen head set. That is what makes ask 5 ("keep
    iterating") cheap to obey: each improvement is another turn of the same four verbs, and the
    previous version stays readable for comparison.
  - **Not done, on purpose, and by whose rule.** (1) **No corpus pass.** The production vector
    source reads `image_dinov3_embeddings` under the model's seven encoder-identity facts, and
    **nothing has populated that table for this configuration** — the full 11.5M-image pass is
    ask 5's, waiting on the operator being satisfied with accuracy. (2) **The three nulls in
    `data/dinov3_config.json` are still untouched**, and v1 did not need them: a model's identity
    lives in the **registry row**, not in that config file, so activating v1 settles nothing there
    and nothing there gates v1. (3) **The encoder decision for the near-duplicate job is still
    unmade** — #1300's Set 2 harness remains unrun, so everything measured here is about tagging
    only, exactly as entry (a) readout 3 warned.
  - **Open, and the operator's to decide — listed, not decided here.**
    1. **The winner floor — or an "other" head instead.** The measured trade is above; both
       answers are legitimate and they are not exclusive.
    2. **Which arm the next iteration promotes.** `dinov2-l14-reg@504/bf16` posts the best exam
       F1 and is the fastest DINO arm; `dinov3-b16@768/bf16` is the program's accepted encoder
       and is what v1 froze. Entry (a) readout 3 still applies: this cannot be settled on tagging
       numbers alone.
    3. **Property list's definition** — 0.687 CV F1 with high recall and poor precision still
       reads as a definition admitting too much, not as a training failure.
    4. **A fresh exam cohort covering all eleven heads.** The current one cannot grade **3d plán**
       or **property list** at all (both post-date it), and **garáž** and the document tags were
       answered under **older definitions**. The 94 gradable photos are a thin foundation for the
       headline number above, and this is the cheapest way to thicken it.

- 2026-09-09 (c) — **The tagging bake-off, phase 2c: the operator's three rulings of the day,
  and the product contract they settle. TAGS ARE ASSIGNED WINNER-TAKES-ALL — the product takes
  no per-head yes/no decision at all.** Phase 2a gave run 1 a face and (a) read its numbers;
  this entry records what the operator DECIDED on reading them, because two of the three
  rulings change what gets built next and one of them changes what "a tag" means.

  Vocabulary once, since the rulings turn on it. A **head** is one yes/no classifier for one
  photo tag ("is this a kitchen?"). An **arm** is one encoder configuration — a model at a
  resolution, pooled a particular way. A **mode** is what a head was allowed to train on. A
  **threshold** is the cut a score has to clear before a head is read as saying "yes".

  - **Ruling (a) — HOW A TAG GETS ASSIGNED, and it is the durable one.** Every head scores the
    photograph; the **highest score names the tag**; no head's own yes/no verdict is consulted
    anywhere in the product. Three consequences the later waves inherit and must not quietly
    undo:
    - **A per-head threshold is no longer a product decision.** It cannot be: winner-takes-all
      compares scores against each other, never against a cut. Thresholds keep exactly one job
      — reading the EXPERIMENT (an F1, a confusion square, a "wrongly caught" pile all need a
      cut to exist at all) — and that is a measurement knob, not a step towards shipping. Entry
      (a)'s open item (3) and the roadmap's "next knob" line are narrowed to that reading.
    - **The winner is DERIVED, never stored.** Heads will be added over time, and a stored
      winner would be a fact about the head set that existed when it was written. Recomputing
      it from whatever heads a run holds means a new head can change a photo's tag with nothing
      re-decided and nothing migrated.
    - **A winner is only a winner within one (arm, mode, split).** Two arms are two different
      models, and the modes do not even share a scale — the two logistic modes score in [0, 1]
      while `pos_only_centroid` is a cosine in [-1, 1]. A ranking that mixed them would be
      arithmetic on incomparable numbers.
  - **Ruling (b) — RETIRE THE LOSERS so the experiment narrows.** Out: the two positive-only
    training modes (`pos_only_free_neg`, `pos_only_centroid`) and **every arm below 512 px**.
    **DINOv2's 504 stays** — patch 14 does not divide 512, so 504 IS this arm's 512, snapped to
    its own grid. Retirement means *stops competing*, never *deleted*: every row run 1 wrote is
    still in `dedup_sim` and still on the page behind a "show retired set-ups" toggle.
  - **Ruling (c) — cost, licence and speed are ACCEPTED for both DINOv3 and DINOv2; accuracy is
    not yet.** So the training set, the head set, the model and the parameters keep iterating in
    parallel, and **the full image pool is scored only after that** — no corpus pass is
    scheduled by this entry.

  **What shipped (#1368).** The page gained the third view and the modal the rulings need, and
  the LANE was narrowed to match, which is the half a display filter cannot do:
  - **View C, "all photos by score"** — view B's cell with the buckets removed: every photo the
    head scored, strongest first, abstentions included. Ranked by **the head's score, not F1**:
    F1 is one number for a whole head, so it can rank heads against each other but cannot order
    photographs. The view says so in its own help line rather than substituting silently.
  - **The per-photo probability panel** — opening any photo in any view lists **every head's raw
    probability for that photograph, strongest first, the top one marked winner**. That panel IS
    ruling (a) made visible, and it is recomputed from the scores on screen every time.
  - **The narrowing is enforced in TWO places, deliberately.** On the page it is a display
    filter (`RETIRED_MODES` / `MIN_LIVE_RESOLUTION = 504` in
    `frontend/src/pages/NewDedupTaggingBakeoff.tsx`), so no result is erased and the toggle
    brings the retired set-ups back. In the LANE it is a **default**: `scripts/tag_head_bakeoff.py`
    trains `pos_neg` only (`LIVE_MODES`), `scripts/tagging_bakeoff_manifest.build_arms` mints no
    arm below 504 px (`tagging_bakeoff_arms.live_arms`), and the workflow gained a `modes` input
    beside its `arms` one. **Naming a retired set-up still runs it** — `arms=clip-b32-stored`
    re-mints the zero-GPU incumbent baseline, `modes=pos_only_centroid` re-trains a retired mode
    — because ruling (c) has the experiment iterating and a narrowed default must not become a
    locked door. Without the lane half, the next default dispatch would have re-embedded a
    retired arm on a rented GPU and re-trained both retired modes on every head; the page would
    simply not have shown the result.
  - **The read surface is now SIX routes**, all admin-gated and read-only:
    `/new-dedup/tagging-bakeoff/{runs, runs/{id}/metrics, runs/{id}/images, runs/{id}/buckets,
    runs/{id}/scores, runs/{id}/images/{image_id}}`. The ranking pages by **keyset cursor** —
    the API hands back the last row and you ask for what follows it — because scores tie
    constantly and an offset over a tied ordering shows one photo twice and skips another.
  - **The incumbent baseline is among the retired.** `clip-b32-stored` is the zero-GPU copy of
    the live CLIP vectors that readout 4 measures every other arm against ("leaving the incumbent
    is worth about +0.06 mean CV F1"), and the 512 px floor hides it by the same rule that hides
    the LAION arm. That is the ruling applied, not an oversight — the toggle and
    `arms=clip-b32-stored` are how the comparison anchor comes back when a new arm needs
    measuring against it.
- 2026-09-09 (b) — **Wave-1 "iteration 1": the versioned TAG MODEL is built — a registry, a
  per-image winner store, and the three jobs that move a model through its life (migration 490).**
  W3 already said "train probe on the gated training set (grouped splits, pinned encoder,
  versioned artifact) … campaign-retag the corpus into the sim tag store"; this pulls that shape
  forward so W2-W5 can be built against it **while training keeps iterating** (ask 5 below).
  Nothing is trained, scored or activated by the build itself.
  - **THE OPERATOR'S RULING OF 2026-09-09, IN FULL — five asks, and where each one landed.**
    One list, because the round split the ruling across three pull requests and nothing else in
    the repo holds all of it: a reader coming in cold needs the whole instruction, not the fifth
    of it that any single PR happened to build. **The asks are NUMBERED, never lettered** — the
    `(a)` / `(b)` in a ledger heading are that DAY'S ENTRIES (this is entry (b), and (a) is
    run 1 of the bake-off), so a citation like "ruling 2026-09-09 (c)" points at an entry that
    does not exist. Cite an ask by its number or by its short name.
    1. **THE WINNER RULE** — *"tags will be assigned by the winner: for each image store the
       probability for each head, the tag is the head with the highest score; no per-head yes/no
       decisions in the product at all; heads will be added over time, so the winner must be
       recomputable from an expanded head set."*
       → **BUILT HERE** (PR **#1366**, migration 490, `toolkit/tag_models.py`). The recomputable
       half is what makes "adding a head is a new version, never an edit" non-negotiable.
    2. **THE NARROWING** — *"remove the worst set-ups so the experiment narrows: the 'borrowed
       no' and 'closeness only' training modes, and every arm below 512 px — dinov2's 504 IS the
       512 arm snapped to its patch size, so keep it."*
       → **PR #1365**, in the bake-off's own lane. Retired is not deleted: run 1's 363 cells stay
       readable and the retired set-ups stay reachable by name for reproduction.
    3. **THE ZOOM READ-OUT** — *"when zooming into any image on the bake-off page, show per-head
       probabilities for each model sorted by raw score."*
       → **PR #1368**, on `/new-dedup/tagging-bakeoff`.
    4. **THE THIRD VIEW** — *"add a third view listing every photo a head scored, sorted by
       score."*
       → **PR #1368**, beside view A (per-image) and view B (buckets).
    5. **KEEP ITERATING** — *the operator is content with the cost, licence and speed of both
       DINOv3 and DINOv2, and is **not** content with accuracy, so the training set, the head
       set, the model and the parameters keep iterating in parallel; the full image pool is
       scored only after that.*
       → **the shape built here serves it** (promote / score / activate is cheap to repeat, and
       scoring runs over the bake-off arm's 9,514 photos for nothing), and it stays **OPEN** as
       the roadmap's parallel track. It is also the reason no corpus pass is run yet.
  - **Vocabulary, once.** A **head** is one yes/no classifier for one photo tag. A **model** is
    one FROZEN DECISION — this encoder configuration, this training mode, this set of heads,
    these weights — with a **version** string as its name (`v1`). The bake-off produced 363
    heads as evidence; a model is what you get when one cell of that grid is chosen and made
    permanent.
  - **THE WINNER RULE, and it decides the whole shape (ask 1 above).** An image's tag is the
    **argmax** — the highest-scoring head — with **ties broken toward the lower tag_id**.
    Three consequences are schema, not convention:
    1. **Every head's probability is stored**, not only the winner's: a winner means nothing
       except beside the field it beat, and a consumer may want the runner-up.
    2. **No threshold and no boolean is stored anywhere.** A head's own threshold still lives
       inside its artifact because that is what the bake-off measured it at — it is EVIDENCE,
       not a gate. **A floor is the consumer's**: anything wanting "…and only if it is
       confident" applies its own minimum to `winner_score`.
    3. **A tie rule is stated rather than left to chance.** Two heads returning the same number
       is ordinary on a photo neither recognises, and "whichever the dict yielded first" would
       tag the same image differently between two runs of the same model.
  - **Adding heads is a NEW VERSION, never an edit** — which is exactly what makes the ruling's
    "the winner must be recomputable from an expanded head set" true. An argmax is only
    meaningful over one head set at a time, so `tag_head_models.heads` freezes the set and
    `image_tag_scores` is keyed by model: v1 and v2 coexist, each with its own answer for the
    same photo.
  - **The store (migration 490, PUBLIC schema).** `tag_head_models` (version, status
    candidate/active/retired, mode, the seven encoder identity facts as columns, the frozen head
    set, a dataset hash; a **partial unique index makes two active models impossible** — that is
    an ambiguous state, not a degraded one), `tag_head_model_heads` (the `toolkit/tag_heads.py`
    artifact plus the bake-off's cv/exam numbers **COPIED** at promotion), `image_tag_scores`
    (one row per image per model version: `scores` jsonb, `winner_tag_id`, `winner_score`).
    **Public and not `dedup_sim` on purpose**: the bake-off's tables are evidence and are dropped
    wholesale at Wave 8, and this is the model the product tags with. For the same reason there
    is **no foreign key into `dedup_sim`** — `source_run_id` / `source_arm` are provenance
    values, so dropping the evidence cannot cascade into the product. One row per (image, model)
    rather than per head: 11.5M x ~300 B at the eventual corpus pass against eleven times that
    for identical information, and the read everyone makes ("what is this photo?") stays a
    primary-key lookup.
  - **THE ITERATION CONTRACT** — `a new bake-off run -> promote -> score -> activate`, and the
    reason it is four verbs and not one. `promote` freezes one (run, arm, mode) cell into a
    `candidate` and **nothing reads what it writes**; `score` fills that version's store,
    resumably and at whatever pace (pure-Python inference — a dot product and a logistic — so
    the corpus pass never needs an ML install); `activate` flips exactly one version in one
    transaction. **Activation being separate is the guarantee**: no consumer ever meets a
    half-scored version. Rolling forward is another promote, rolling back is `activate` on the
    older version. Two vector sources: `bakeoff:<run_id>` (the arm's own stored vectors — the
    cheap loop over run 1's 9,514 labelled + exam photos) and `production`
    (`image_dinov3_embeddings` under the model's seven facts — works today, returns nothing,
    because nothing has populated that table for this configuration yet).
  - **Surfaces.** `toolkit/tag_models.py` (`promote` / `activate` / `score` / `winners`, the read
    contract later waves code against), `scripts/tag_model.py`, `.github/workflows/tag_model.yml`
    (dispatch-only, `dry_run` default true, `SUPABASE_DB_URL` only, CPU), and read routes
    `GET /new-dedup/tags/{models, models/{version}/heads, images/{image_id}}` — admin-gated,
    read-only, weights never returned. Labels still arrive ONLY through
    `machine_labeling.training_rows`, so the holdout census has nothing new to exempt.
  - **DELIBERATELY NOT DONE HERE — and by whom instead.** No corpus pass: the full image pool is
    scored only once the operator is satisfied with accuracy (**ask 5**). The three nulls in
    `data/dinov3_config.json` are untouched. No frontend of this store's own. The other three
    asks of the same ruling are being carried out in the bake-off's own lane, in the same round:
    **ask 2, the narrowing, is PR #1365** (drop `pos_only_free_neg` "borrowed no" and
    `pos_only_centroid` "closeness only", drop every arm below 512 px, keep dinov2's 504) and
    **asks 3 and 4, the zoom read-out and the third view, are PR #1368**. Nothing of the ruling
    is unowned. The narrowing in particular is not this PR's business because a model is
    single-mode by construction — this store only records which mode a version was frozen at;
    what the promote lane *offers* as a default is a separate question, answered in the lane
    note below.
  - **THE PROMOTE LANE IS PRODUCT-FACING, so it says which mode is live.** `pos_neg` — a real
    labelled "no" — is the mode the product should be promoted from. The other two remain
    selectable **only to reproduce a bake-off cell**, and the lane's own input description says
    so, because a promoted model is what the product tags with and three unlabelled equal
    choices would read as three equally good ones. It also keeps the store from misdescribing
    itself: a `pos_only_centroid` head's score is a cosine similarity, not a calibrated
    probability, so a version promoted from it must be read as evidence, not as a percentage.

- 2026-09-09 (a) — **Run 1 of the tagging bake-off COMPLETED. 11 heads x 11 encoder arms x 3
  training modes = 363 trained cells, 529,188 per-photo scores, 0 ungradable, ~$1.55 of GPU
  in total.** The experiment (d) built and (e) gave a face has now been run end to end, and
  every number below is measured on run 1 (`run_id 1`, schema `dedup_sim`) rather than
  estimated. It answers three of the four things ENCODER-DECISION.md §5 said had to be
  measured before the corpus is embedded; the fourth (near-duplicate matching) is still
  untouched.
  - **What was run.** The 11 heads are exactly the tags the operator has marked
    `review_state='ready'` on `/new-dedup/training-set` — fasáda, garáž, koupelna, kuchyně,
    obývací pokoj, 3d plán, katastrální mapa, letecký snímek s ohraničením, property list,
    půdorys, technické zařízení — selected by the flag, never by a list (the (d) addendum's
    ruling). **9,264 training photos** and the sealed **250-photo exam**; **0 exam photos
    appear in any training tray** (verified, not assumed — that separation is the only thing
    that makes the exam an exam). 264 cells were trained in the first pass and 99 in the
    second, and not one cell came back ungradable.
  - **The cost of getting here.** Seven pod attempts, all on 2026-09-08: ≈ $0.50 + $0.08 +
    $0.12 + $0.09 + $0.00 + $0.36 + $0.40 ≈ **$1.55**. The first five are post-mortemed in
    entries (f)–(j) — a clone that could never have worked, a bootstrap that could not report
    itself, a crash loop over an undersized disk, a missing `torchvision`, and two rules that
    each defined `failed` correctly in their own scope and deadlocked together. The CPU train
    stages cost nothing (they are not GPU work). The watchdog built in (f) is what kept the
    total at a dollar and a half instead of the two-hour idle bill the first attempt was on
    course for.
  - **What F1 means, once.** A head can be wrong two ways: it flags photos that are not the
    tag (**precision** = of what it flagged, how much was right) and it misses photos that
    are (**recall** = of what was really there, how much it found). **F1 is the harmonic mean
    of the two** — 1.0 is perfect, and it is only high when BOTH are, so it cannot be gamed by
    flagging everything (perfect recall, useless precision) or by flagging only the single
    photo the model is surest of (perfect precision, useless recall). Two F1s are reported per
    cell and they answer different questions: **CV F1** is grouped 5-fold cross-validation over
    the training photos — every photo scored by a model that never saw any photo from its
    listing, so it measures the head on material like the material it was taught on — and
    **exam F1** is the sealed `exam_v1` holdout graded by the ratified rule, which measures the
    head on a random slice of the real corpus.

  Mean F1 across the 11 heads, `pos_neg` mode (the head trained on the operator's own yes AND
  no labels), with end-to-end throughput measured on an RTX 3090 at batch 32, 16 decode
  workers, `letterbox_pad` — decode, preprocessing and forward pass together, which is the
  number §5.3 asked for and never had:

  | arm | CV F1 | exam F1 | img/s |
  | --- | --- | --- | --- |
  | `dinov3-l16@512/bf16` | 0.905 | 0.739 | 5.0 |
  | `dinov2-l14-reg@504/bf16` | 0.904 | **0.758** | 26.2 |
  | `dinov3-b16@1024/bf16` | 0.903 | 0.744 | 4.3 |
  | `dinov3-b16@1024/fp32` | 0.903 | 0.742 | 2.9 |
  | `dinov3-b16@768/bf16` | 0.903 | 0.732 | 12.0 |
  | `dinov3-b16@768/fp32` | 0.903 | 0.732 | 4.5 |
  | `dinov3-b16@512/bf16` | 0.903 | 0.708 | 20.5 |
  | `dinov3-b16@512/fp32` | 0.903 | 0.708 | 12.9 |
  | `siglip2-b16@512/bf16` | 0.878 | 0.694 | 37.0 |
  | `clip-b32-laion@224/fp32` | 0.876 | 0.669 | 55.9 |
  | `clip-b32-stored` (incumbent, copied) | 0.841 | 0.633 | — |

  - **1. Half-precision is free speed — bf16 and fp32 agree to three decimals on every arm.**
    `bf16` stores each number in half the space, which was the one thing that could have made
    it worse; on this job it does not move F1 at any resolution and it runs **1.6-2.7× faster**
    (512: 12.9 → 20.5 img/s; 768: 4.5 → 12.0; 1024: 2.9 → 4.3). For the `dtype` null in
    `data/dinov3_config.json` this is as close to a decided answer as measurement gets: fp32
    buys nothing and costs between a third and two thirds of the throughput.
  - **2. Resolution buys a little, and it is expensive.** On DINOv3-B the CV F1 is **identical**
    at 512, 768 and 1024 (0.903 three times) — on the training material, the extra pixels teach
    the head nothing. On the exam it does move, monotonically: **0.708 → 0.732 → 0.744**, for
    **20.5 → 12.0 → 4.3 img/s**. So 1024 is worth about 3.6 F1 points over 512 on real-corpus
    photos and costs **4.8× the compute**. That is a price question, not an accuracy question,
    and the arithmetic is below.
  - **3. DINOv2-L is not worse than any DINOv3 arm on THIS job — and it is Apache-2.0.**
    `dinov2-l14-reg@504/bf16` posts the **best exam F1 of the whole field (0.758)**, ties the
    best CV F1 to a thousandth, and is the **fastest DINO arm by 6×** (26.2 img/s against
    DINOv3-B@768's 12.0). It also carries no licence acceptance step. **This does not overturn
    ENCODER-DECISION.md**, and it must not be read as if it did: that document chose DINOv3 on
    **near-duplicate retrieval** — telling two photographs of the same flat apart from two
    photographs of similar flats — which is a different job from tagging and is **still
    completely unmeasured** here. The harness for it exists (#1300's "Set 2"). Until it runs,
    the honest statement is: on tagging, DINOv2-L is at least as good and much cheaper; on the
    job the encoder was actually chosen for, we have no measurement at all.
  - **4. Leaving the incumbent CLIP is worth about +0.06 mean CV F1**, and the gain is
    concentrated where the incumbent is weakest rather than spread thinly: **3d plán 0.71 →
    0.91**, **obývací pokoj 0.83 → 0.92**, **letecký snímek 0.77 → 0.88**, **technické zařízení
    0.81 → 0.87**. Those are the four heads where the stored CLIP vectors were not good enough
    to build on; a better encoder fixes them without a single new label.
  - **5. The operator's negative labels are worth roughly what a whole encoder upgrade is
    worth — and the value is wildly uneven.** Comparing `pos_neg` against `pos_only_free_neg`
    (the mode that borrows other heads' positives as free stand-in negatives) on
    `dinov3-b16@768/bf16`: **+0.055 CV / +0.074 exam** on average. Per head: **≈0 on the
    document tags and on koupelna** (a floor plan is so unlike anything else that free
    negatives are enough), **+0.11 on garáž and obývací pokoj**, **+0.22 on property list**.
    Dropping negatives entirely (`pos_only_centroid`, no classifier at all — just cosine
    distance to the average positive) costs a further **−0.07** (0.833 CV / 0.647 exam). The
    reading: labelling negatives is not universally necessary, but on the confusable heads it
    is the difference-maker, and the operator can now spend label-days on the heads where it
    pays instead of uniformly.
  - **6. Per-head CV F1 (`dinov3-b16@768/bf16`), and the one weak head.** katastrální mapa
    0.97, koupelna 0.97, půdorys 0.96, kuchyně 0.95, garáž 0.92, obývací pokoj 0.92, 3d plán
    0.91, fasáda 0.89, letecký snímek 0.88, technické zařízení 0.87 — and **property list
    0.69** (precision 0.57, recall 0.87). Ten heads are usable; property list finds most of
    what it should but nearly half of what it flags is wrong. Recall that high with precision
    that low is the signature of a **definition** that is admitting more than it means to, not
    of a training failure — which is why it appears in the open decisions below rather than in
    a threshold sweep.
  - **7. The exam column is a PREVALENCE story, not a definition story — read it that way or
    it will mislead.** Exam recall is ~1.0 nearly everywhere; what collapses is precision, and
    it collapses on the RARE tags for an arithmetic reason. The exam is 250 photos drawn at
    random, so a tag that is rare in the corpus has almost no positives in it: **garáž 2 true
    positives against 7 false; katastrální mapa 10 against 9; letecký snímek 8 against 10;
    fasáda 19 against 15**. A handful of extra false flags halves a precision computed over two
    or ten true ones. **technické zařízení has 0 positives in the exam at all** — its metrics
    are correctly NULL (nothing to grade), which is the (d) contract behaving as designed. **3d
    plán and property list have no gradable exam cells** because they are post-exam tags: they
    did not exist when the exam was answered.
    - **The mechanism, plainly:** every head is trained at roughly **1:3 positives to
      negatives** and then judged at the **0.5** threshold, but out in the corpus the true rate
      is far below 1:3. A head calibrated for a balanced tray **over-fires** at natural
      prevalence. **Per-head thresholds read off each head's own precision/recall curve are the
      next knob**, and they are cheap: no re-embedding, no re-training, just a number per head.
      (**Narrowed the same day by ruling (a), entry (b) above**: the product assigns tags
      winner-takes-all and reads no head's yes/no, so a threshold now tunes only what the
      MEASUREMENT says — the F1s, the confusion square, the wrongly-caught pile — and is not a
      step towards shipping.)
      **Before turning it, the operator should look at the false-positive buckets** on
      `/new-dedup/tagging-bakeoff` **view B** — the wrongly-caught pile, most-confident first —
      because some of those "errors" will be photos the head got right and the label got wrong,
      and a threshold tuned against mislabelled evidence bakes the mistake in.
  - **The corpus-cost arithmetic, stated as arithmetic and not as a recommendation.** At the
    measured end-to-end rates, **11.5M stored images** on a **$0.22/hr RTX 3090**:
    `dinov2-l14` ≈ **123 h ≈ $27**; `dinov3-b16@512/bf16` ≈ **156 h ≈ $34**;
    `@768/bf16` ≈ **266 h ≈ $59**; `@1024/bf16` ≈ **743 h ≈ $163**. **This is far above
    ENCODER-DECISION.md's $1-12 band**, and the band is what is wrong, not the measurement:
    that band came from GPU-only synthetic-tensor throughput, while these numbers include JPEG
    decode and preprocessing — the very thing §5.3 said had to be measured once before the
    corpus pass was scheduled. This is that measurement. Bigger batches, more decode workers or
    a faster card may well move it, but **nothing here measured that**, so nothing here claims
    it. What it does establish is that resolution is now a **$34-vs-$163** decision rather than
    a rounding error.
  - **Open, and the operator's to decide — listed, not decided here.** (1) The three nulls in
    `data/dinov3_config.json`: `preprocessing` was already ruled `letterbox_pad`, `dtype` is
    answered by the data (readout 1), **`resolution` is the live question** (readout 2 + the
    cost arithmetic). (2) Whether the tagging result changes the encoder choice at all — it
    cannot be settled until the near-duplicate bake-off runs, because that is the job the
    choice was made on. (3) Per-head thresholds — **a measurement knob only, since ruling (a)
    of the same day (entry (b) above) assigns tags winner-takes-all and takes no per-head
    yes/no in the product**. (4) Property list's definition. (5) Whether some exam-side false
    positives should be re-judged before any of the above is tuned against them.

- 2026-09-08 (j) — **Attempt 5 was killed by our own two definitions of `failed`, 2 s in,
  $0.00 — and a retry was impossible as designed.** GitHub run 34274077032 rented pod
  `w8rh1rekxwo57z` for run 1's embed stage and the watchdog tore it down immediately with
  `case=all-terminal … arms 10/10 terminal`, before the payload could claim a thing.
  - **The rule interaction.** `scripts/pod_watchdog.py`'s all-terminal case counts an arm
    as terminal when its status is `ok`/`failed`/`skipped` (nothing will move again, stop
    paying); `tagging_bakeoff_embed.pending_arms` counts `failed` as work to do (the
    vectors, not the status, are the record of what is done). Both are right in their own
    scope. The seven DINOv3 arms carried attempt 4's `failed` (the torchvision gap, fixed
    in #1361), so the launch read 10/10 terminal on its first poll. Ledger entry (i)'s
    claim that "the re-dispatch is free of charge to reason about" was true of the payload
    and false of the watchdog watching it.
  - **The fix is in the DISPATCHER, not in either rule.** It is the one place that knows a
    NEW attempt is starting: `tagging_bakeoff_dispatch.reset_failed_arms` now clears this
    run's `failed` arms back to `pending` immediately before the pod launches — scoped to
    the run, and to `--arms` when given — prefixing each arm's note with `retry <ISO ts>
    (attempt from GitHub run <id or 'local'>): ` so the attempt that cleared the verdict is
    named in front of the failure text it cleared, and logging exactly which arms moved.
    `ok`/`skipped` never move without the new `--force-arms` (which requires `--arms` and
    only re-opens the arms it names). A dry run prints what it WOULD reset and writes
    nothing. The watchdog's terminal rule is untouched: an arm that fails AGAIN during
    this run is genuinely terminal, and the happy-path teardown still fires.
  - **The invariant is now stated at both ends** — a comment in `pod_watchdog`'s
    all-terminal case pointing at `pending_arms`, and one on `pending_arms` pointing back
    — so neither gets "fixed" into agreeing with the other. `scripts/dinov3_embed_dispatch.py`
    needs no reset and says so: that lane has no per-arm status at all, its progress record
    IS the vector count, and `terminal` is hard-wired False.
  - **The operator's session reset run 1's seven arms by hand** to unblock the next
    attempt; this change is what makes the next one unnecessary.
  - Tests (offline, fake conn): failed arms are reset and logged; the statement asks only
    for `failed` and excludes the zero-GPU stored arm; `--arms` narrows the reset;
    `--force-arms` widens it only for the named arms and is refused bare; a dry run resets
    nothing; the note prefix carries the ISO stamp and the GitHub run id (`local` off
    Actions).

- 2026-09-08 (i) — **Attempt 4 BOOTED, EMBEDDED, and told us the next bug in one line: the
  pod has torch but not torchvision, so every DINOv3 arm dies at model load.** Run 1's embed
  stage on pod `4rgi66lggbty11` (RTX 3090) ran the whole bootstrap clean, cached the corpus,
  embedded three arms and was torn down by the watchdog on **all-terminal at 1,547 s, ≈$0.09**
  — the first run of this lane that produced vectors at all.
  - **Three arms ok, 9,514 vectors each**: `dinov2-l14-reg@504/bf16` at **26.2 img/s**,
    `siglip2-b16@512/bf16` at **37.0 img/s**, `clip-b32-laion@224/fp32` at **55.9 img/s**.
    Those are the first real throughput numbers for the corpus and they price the production
    backfill honestly.
  - **All SEVEN DINOv3 arms failed identically**: ``DINOv3 model load failed after retries:
    `DINOv3ViTImageProcessorFast` requires `torchvision` to be installed``. Cause:
    `scripts/pod_bootstrap.py`'s torch step installed `torch` alone from the cu118 index, and
    the fast image processor transformers selects for the DINOv3 configs does its resize/crop
    in **torchvision**. Only DINOv3 resolves to a fast processor here, which is exactly why
    three arms were fine and seven were not — a partial success is the hardest failure shape
    to predict, and it cost a boot to find.
  - **Fix: one command, one index.** `uv pip install torch torchvision --index-url
    …/whl/cu118`, so the two CUDA builds are the pair the index publishes together (cp312
    wheels confirmed on the public listing: torch 2.7.1+cu118 ↔ torchvision 0.22.1+cu118).
    Both lanes share the bootstrap, so the production `dinov3_embed_backfill` dispatch — which
    would have hit this on its first GPU — is fixed by the same line. **No pyproject change**:
    torchvision is a pod-side install like torch, not a project dependency.
  - **The processor stays FAST.** `use_fast=False` would change preprocessing and therefore
    the vector identity, orphaning every embedding already written; the failure mode is
    documented at the call site in `scraper/dinov3_tagger.py` instead. Fix the box, not the
    processor.
  - **The re-dispatch is free of charge to reason about**: `pending_arms` already treats
    `failed` as work to do, and the vectors table is the per-image checkpoint, so re-running
    `stage=embed` for run 1 with no `--arms` picks up exactly the seven DINOv3 arms and leaves
    the three `ok` ones alone.
  - **Second bug from the same run, fixed here too: the train stage could not narrow to
    arms.** GitHub run 34272736891 (`stage=train`, `arms=dinov2-…,siglip2-…,clip-…`) failed
    with `BAKEOFF run 1 has no arms named ['dinov2-…,siglip2-…,clip-b32-stored']` and wrote
    nothing. A workflow_dispatch input is ONE string, and `scripts/tag_head_bakeoff.py`
    declared `--arms` as `nargs="+"`, so the comma-joined token read as a single arm name.
    `--arms`, `--modes` and `--heads` now split on commas the way the embed payload always
    has (space-separated still works); `choices=`/`type=int` moved out of argparse into a
    post-split check, because both run per token and would reject the joined form first.
  - Tests: the generated bootstrap's torch step contains `torchvision` and exactly one
    `--index-url`; the dry-mode preflight still passes all three cases; a comma-joined
    `--arms` selects those arms in the trainer; an unknown mode and a non-numeric head still
    exit non-zero. Verified locally against the real CPU index (`uv pip install torch
    torchvision --index-url …/whl/cpu` into a scratch 3.12 venv): torch 2.14.0+cpu with
    torchvision 0.29.0+cpu, one resolve, `torchvision.transforms.v2` imports.

- 2026-09-08 (h) — **Attempt 3 finally produced readable heartbeats, and they showed a
  RESTART LOOP over a disk that was never big enough. Three fixes, one PR.** Run 1's embed
  stage went to pod `lg5oy1ivlgoyh7`; the watchdog's stall rail ended it at **~33 min**
  (~$0.12). What the note said, for the first time:
  - `18:30:34 step=uv ok`, `18:30:38 step=venv ok` — **63 s after launch**. Fetch-by-sha, uv
    and the 3.12 venv all work on the pod; (f) and (g) hold.
  - `18:32:16` — a **SECOND pass of the whole start command**, failing at `step=fetch` with
    `error: remote origin already exists` (exit 3), then repeating every ~60 s.
  - **RunPod re-runs the docker start command whenever it exits.** That is the loop: the
    first pass died, the container restarted, `git remote add origin` met a directory that
    already had a remote, and every restart wrote a fresh heartbeat over the previous one.
    Fix: **every step is idempotent** (`rm -rf` before the checkout and before the venv;
    `$PODBOOT_ROOT` itself — reporter, log, step file, history, pass counter — is kept), and
    after a clean payload the script **`sleep infinity`** instead of exiting. The dispatcher's
    watchdog is what ends the pod. A pass counter on disk survives the restart and every
    heartbeat now carries `pass=N`, so a loop reads as a loop instead of as progress.
  - **The first pass died fast, and the most likely reason is disk.** `launch_pod` defaulted
    `volume_gb=1`; RunPod mounts the pod volume at `/workspace`; `pod_bootstrap` put
    everything — including a `uv pip install torch …cu118` (~2.5 GB downloaded, ~5 GB
    installed) — under `/workspace`. **This is a reading, not a confirmed diagnosis**: the
    report that would have named it was overwritten by pass 2 before the 60 s poll saw it.
    Fix regardless: the work dir moves to the **container disk** (`/opt/podboot`, never
    `/workspace`), both lanes rent **no volume** (`volumeInGb: 0`) and size
    `container_disk_gb` (a new dispatcher flag + workflow input, default **60**, floor 40:
    devel base image + torch + weights + the ~1 GB image cache), and the bootstrap logs
    `df -h` at the top plus a `step=disk <N>GB free` heartbeat immediately before the torch
    step. The next disk problem is read, not guessed. The bake-off payload's own image cache
    moved off `/workspace` with it (`tagging_bakeoff_embed.DEFAULT_CACHE_DIR`).
  - **A crash loop must not erase its own evidence.** `pod_report.py` used to rewrite ONE
    `pod step {…}` line, so pass 2's `deps ok` destroyed pass 1's `exit=… step=torch`. It now
    keeps a **bounded history**: the last 8 records as a JSON array under one `pod steps [...]`
    marker, ≤7,000 chars, in which the FIRST `exit=` record is never trimmed away and is the
    last tail surrendered to the size cap (staleness is answered by `ts` + `pass=N`, not by
    erasure). The history file lives on the pod's own disk, which is what makes it survive the
    restart it describes. The lane's UPDATE now bounds the note's OTHER lines (`left(…, 6000)`)
    and appends the heartbeat whole, so the record we came for can never be the part cut.
  - **The watchdog recognises the loop.** It reads the array, logs EVERY new record (not just
    the latest), and terminates immediately with `case=crash-loop` on **two or more `exit=`
    reports within the stall window**, printing the FIRST one's tail — the original cause;
    the rest are its restarts. The three existing rails are untouched, and a lane that reports
    only a single record (the pre-(h) shape) keeps exactly the old behaviour.
  - **No migration, no new dependency.** All three fixes are in the shared bootstrap, the
    reporter, the watchdog and the two dispatchers.
  - Tests, all offline and free: the generated bash runs TWICE over one work dir without error
    and reports `pass=2`; it ends in the sleep after a clean payload (proved with a short
    `PODBOOT_SLEEP_S`); the work dir is not under `/workspace`; the REST body carries
    `containerDiskInGb` and `volumeInGb: 0`; the reporter's history is bounded and never drops
    the first `exit=`; the watchdog fires `crash-loop` on two exits and prints the first one's
    tail. `--dry-run` preflight now runs the script three times — clean, **restart over the
    same root**, and `torch` forced to fail — and refuses to launch if any fails.
  - **Run 1 is still re-dispatchable as it stands** — arms `pending`, zero vectors.

- 2026-09-08 (g) — **The retry died too (~$0.08 — the watchdog worked), and we still could
  not say why. The bootstrap now reports itself.** Run 1's embed stage was re-dispatched onto
  pod `bsg9k5ee9y6jcm` (RTX 3090) with (f)'s fixes in place. The watchdog terminated it at
  **1,245 s** with `case=bootstrap-deadline: no heartbeat within 1200s — the pod never
  reported Python running`. That is the watchdog doing exactly its job: the whole failure
  cost about eight cents instead of fifty. **The cause is unknown and stays unknown** — this
  entry records the fix for the blindness, not a diagnosis.
  - **Why nothing could be said.** RunPod's Pod logs endpoint answers **400**, and the first
    heartbeat was the PAYLOAD's — written only after fetch + `pip install uv` + `uv venv
    --python 3.12` + `uv pip install torch` (2 GB, cu118) + `uv pip install -e '.[clip]'` had
    ALL succeeded. From the runner, "the torch download is slow" and "the clone is dead" are
    the same observation: silence. A deadline can only ever be a guess about which one it is.
  - **The fix: step-level heartbeats from inside the bootstrap, on the IMAGE's own Python.**
    `scripts/pod_report.py` is embedded verbatim in the start command's heredoc — it is on
    the box before the checkout exists, and it runs on the image's 3.10, never the 3.12 venv
    the bootstrap builds (which is one of the things that can fail). The start command
    installs `psycopg[binary]` into that interpreter first and then calls the reporter after
    every step: `step=deps ok`, `step=fetch ok`, `step=uv ok`, `step=venv ok`,
    `step=torch ok`, `step=repo ok`, `step=payload starting`. A background beat repeats the
    current step every 300 s so a long silent install still proves life, and stops before the
    payload takes over the note.
  - **The error ships itself.** All output is tee'd to `/workspace/bootstrap.log` and an
    `EXIT` trap reports `exit=<code> step=<the step that failed>` with the last ~3,000
    characters of that log — into the same row. The failing step is tracked in a shell
    variable set before each step, which is what the trap reads; the tail is read from the
    file by Python rather than interpolated by the shell, because a pip error contains
    quotes and backticks. **The next failure names itself**, in
    `select note from dedup_sim.tag_head_bakeoff_runs where id = N` and in the dispatcher's
    own log after teardown.
  - **The reporter is lane-agnostic and never fatal.** It knows no table: `HEARTBEAT_SQL`
    (an UPDATE with `%(note)s` / `%(run_id)s`) and `HEARTBEAT_RUN_ID` arrive in the pod's
    REST-body env; the bake-off supplies an UPDATE that replaces any previous `pod step` line
    (`regexp_replace` + `left(…, 8000)`), so the note holds the newest step, keeps the
    manifest's and the payload's own lines, and never grows. With the env absent it prints
    and exits 0, and every call site is `|| true`. **The production DINOv3 lane is
    deliberately NOT wired**: it has no run row to report into (its progress record is the
    vector table), and minting one is a schema decision, not a bug fix — written down in
    `dinov3-embedding-lane.md` so the gap is a choice rather than an oversight.
  - **The watchdog now counts a step as progress.** `bootstrap_deadline_s` runs from the last
    heartbeat rather than from launch (default raised **1200 → 1800 s**, both workflow inputs
    with it), so a slow torch download keeps buying time; a bootstrap that reported and then
    went quiet is caught by the stall deadline (900 s = three missed beats); a pod that never
    reported at all still trips the bootstrap deadline, and now says so ("reported nothing at
    all"). Each new step is logged as it arrives and the last one — error tail included — is
    printed after teardown.
  - **The generated bash is now EXECUTED before a pod is rented.** `PODBOOT_DRY=1` swaps
    every real step for a stub and `PODBOOT_DRY_FAIL=<step>` forces one to fail, so
    `pod_bootstrap.preflight()` runs the exact script twice offline — clean (expects
    `exit=0 step=payload`) and broken (expects `exit=1 step=torch`) — and the dispatchers run
    it on every `--dry-run`, refusing to launch if it fails. A syntax error in this script
    was previously discoverable only by renting a GPU and waiting out a deadline.
  - **No migration, no new dependency.** The heartbeat rides on the existing `note` column;
    `psycopg` is installed inside the pod at run time and is not in `pyproject.toml`.
  - Tests, all offline: `tests/scripts/test_pod_bootstrap.py` (structure AND three real
    subprocess runs of the generated bash — clean, forced failure with the trap naming
    `step=torch` and carrying the tail, and the beat firing through a slow step),
    `tests/scripts/test_pod_report.py` (one line whatever the tail contains; the SQL the lane
    supplied; a refused write is not fatal), `tests/scripts/test_pod_watchdog.py` (a slow
    reporting bootstrap is not a dead pod; a bootstrap that went quiet still is),
    `tests/test_tagging_bakeoff.py` (the pod is told which row to report into; a step is
    progress; the dry run proves the script).
  - **Run 1 is still re-dispatchable as it stands** — arms `pending`, zero vectors, nothing
    to clean up.

- 2026-09-08 (f) — **The bake-off's first real GPU run FAILED and cost ~$0.50 for zero
  vectors. Three causes, all fixed; the third is the one that turned a 10-second bug into an
  8,115-second bill.** Run 1's embed stage rented an RTX 3090 (pod `u1yvcktjn6dbrt`,
  $0.22/hr), held it for the whole 8,115 s wait window, and produced **no vectors for any of
  the ten GPU arms** — all ten still `pending`. RunPod's Pod logs endpoint answers **400**, so
  the run left no readable trace at all; the post-mortem was done offline, against the code.
  - **Cause 1 — the clone could never have succeeded.** The start command was
    `git clone --depth 1 --branch {ref} …` with `ref = GITHUB_SHA`. `--branch` resolves a
    branch or a tag, never a commit sha: `fatal: Remote branch 2d00061d… not found in upstream
    origin`, exit 128. Under `set -euo pipefail` that is the entire pod, dead in seconds and
    idling on the meter for two hours. Now `git init` + `git remote add` +
    `git fetch --depth 1 origin <sha>` + `git checkout FETCH_HEAD`, verified against GitHub.
  - **Cause 2 — the wrong Python, waiting behind it.** The image
    (`runpod/pytorch:2.1.0-py3.10-…`) ships **3.10**; `pyproject.toml` requires **>=3.12**, so
    `pip install -e '.[clip]'` would have refused even with a healthy checkout. RunPod's newer
    tags do not state a Python version, so the fix does not chase an image: the pod installs
    `uv`, builds a **3.12** venv (uv downloads a managed CPython) and takes torch from the
    **cu118** index — the flavour with cp312 wheels furthest up the series (through 2.6.0;
    cu121 stops at 2.5.1) and the one matching this CUDA-11.8-era image. Proven locally in CPU
    mode end-to-end: `python=3.12.14 torch=2.14.0+cpu transformers=4.57.6`, with
    `from scraper import dinov3_tagger` importing in that venv. The RESOLVED versions are
    recorded at run time into the arm rows' `note`, because a bootstrapped interpreter is not
    knowable from the repo.
  - **Cause 3 — nothing was watching (the expensive one).** The dispatcher waited
    `job_max_seconds + 900` no matter what the pod did; a pod that dies at second 10 bills
    identically to one that works. **`scripts/pod_watchdog.py`** now polls the payload's OWN
    rows from the runner (which has `SUPABASE_DB_URL`) every 60 s and terminates on: no boot
    heartbeat within `bootstrap_deadline_seconds` (default 1200), no progress for
    `stall_deadline_seconds` (default 900), or **every arm terminal** — the happy path, which
    now stops paying when the work stops rather than when the window does. It logs the case and
    a spend estimate; a bootstrap/stall teardown FAILS the workflow, because a green Actions
    run that embedded nothing is exactly what this incident looked like. Both lanes
    (bake-off + the production `dinov3_embed_backfill`) share it, and `RunPodClient.run_job`
    grew one `progress` hook — the `finally`-terminate guarantee is untouched.
  - **No migration.** The heartbeat rides on columns that already exist: the run row's `note`
    carries `pod booted <iso> <versions>` (the payload's very FIRST DB write, before the
    manifest and any weight download) and a rewritten `pod alive <iso> <phase>` through the
    phases that write no vector; each arm's `note` is rewritten at every batch with the vectors
    already committed. The boot stamp is what separates "the clone or install failed" from "the
    weights are downloading slowly" — the distinction this incident had no way to make.
  - **Run 1 is re-dispatchable as it stands**: arms `pending`, zero vectors, nothing to clean
    up. An arm a killed pod leaves `running` is picked up again (`pending_arms` treats
    pending/running/failed alike; the vectors, not the status, are the record of work).
  - Tests, all offline: `tests/scripts/test_pod_bootstrap.py` (fetch-by-sha, never `--branch`;
    the 3.12 venv; the cu118 index; both lanes share one script),
    `tests/scripts/test_pod_watchdog.py` (each case fires, and does NOT fire while progress
    advances — fake clock, fake poller), plus the payload's boot-heartbeat-first,
    heartbeat-with-its-vectors and stale-`running`-resumes cases.

- 2026-09-08 (e) — **The tagging bake-off, phase 2a: the comparison page** (`NEW DEDUP ·
  Tagging bake-off`, `/new-dedup/tagging-bakeoff`). The read surface of (d) given an operator
  face. No backend change: the page is a pure consumer of the four documented routes.
  - **Three reads, in the order the decision is made.** A MATRIX of one row per head and one
    column per selected (arm x mode), F1 leading with precision, recall and the graded n under
    it, tinted in one accent so the best arm per head is visible without reading — then VIEW A,
    a grid of photographs each carrying what every selected arm said about it, so the arms can
    be compared on the SAME picture — then VIEW B, one head under one arm and mode split into
    its four outcome buckets, most-confident first, under a 20-bin histogram with the threshold
    marked. Number, photograph, mistake.
  - **The two contract conventions are RENDERED, not assumed.** A null rate reads "nothing
    proposed" beside its graded n and says on hover that it is not a zero; a `status: 'failed'`
    cell is drawn struck, carrying its own note, as a decided outcome rather than a gap; an
    exam abstention is counted beside the split and folded into no rate or bucket. Score bars
    are normalised by their own mode's scale and the centroid mode is named "cosine" wherever
    it appears, so no bar invites a comparison across modes that the numbers cannot support.
  - **Plain words lead, codes follow.** Caught / wrongly caught / missed / correctly rejected,
    with tp/fp/fn/tn secondary; "Yes + no", "Yes only, borrowed no", "Closeness only" for the
    three modes, each carrying the full explanation on hover and again in a glossary the page
    ships with itself. The repeated glyph is a 2x2 CONFUSION SQUARE — top row = the model said
    yes, left column = you said yes — in every matrix cell, on every bucket header, and as the
    colour of every outcome mark.
  - **Deep-linkable.** `run`, `view`, `arms`, `mode` (both multi), `tag`, `split`, `outcome`
    and the two paging cursors live in the query string; a default derived from the data is
    never written into it, so a shared link says only what was actually chosen. Arms, modes,
    head and split survive the view switch; View B's single arm/mode (`barm`/`bmode`) are
    separate keys so narrowing there never discards the matrix's selection.
  - **Three places the contract fell short, worked around rather than changed.** `/buckets`
    carries no threshold, so it is read off the metric row for the same (arm, mode, head) — and
    left UNMARKED, never guessed, when there is no such row. A head's human label arrives only
    on a metric row, so a run whose metrics have not landed names its heads by tag id.
    `/images` pages forward only, so "previous" is a cursor stack the page keeps. Also: when
    more than one mode is selected the page asks `/images` for ALL modes and narrows client-side,
    because the route takes exactly one.
  - Tests: `frontend/src/pages/NewDedupTaggingBakeoff.test.tsx` (22) pin the run picker, the
    matrix including the null-rate and failed-cell readings, the outcome-requires-a-head rule,
    both paging shapes, the threshold join, and the URL round-trip.
  - **Still phase 2**: the GPU job that fills `tag_head_bakeoff_vectors` per arm, and its
    workflow. Nothing has been embedded, trained or scored yet, so the page has no run to show
    until that lane runs.

- 2026-09-08 (d) — **The tagging bake-off, phase 1: the results store, three training modes, the
  CPU runner and its read surface.** ENCODER-DECISION.md §5.2 "Set 1" and §5.4 readout 4 turned
  into something that can actually be run and looked at. Nothing has been embedded, trained or
  scored — this is the durable half; the GPU job and the comparison page are phase 2.
  - **Arms.** An arm is one encoder configuration, identified by the SAME seven facts migration
    480 keys the production vector table on (`model, revision, library, pooling, resolution,
    preprocessing, dtype`) plus a short human name (`dinov3-b16@768/bf16`). Vector width VARIES
    by arm — 512, 768, 1024 — so `dedup_sim.tag_head_bakeoff_vectors.embedding` is an
    **unmodified `halfvec`**, not `halfvec(768)`; a fixed width would silently exclude every arm
    that is not DINOv3 ViT-B. The arms' vectors live in `dedup_sim` rather than in
    `image_dinov3_embeddings` for the same reason a losing arm must not be able to pollute the
    production population.
  - **Three modes, because the instruction admits three readings.** The operator asked for
    "positive training only, no negative training", and that sentence has more than one honest
    meaning; guessing one would have decided the experiment by assumption. So all three run side
    by side: `pos_neg` (the existing trainer — this head's admitted positives against its
    admitted negatives), `pos_only_free_neg` (logistic regression whose negatives are the OTHER
    selected heads' positives, standing in as material that cost no label-day; an image positive
    for THIS head, or one the operator LEFT OUT for it, is never among them), and
    `pos_only_centroid` (no classifier at all — cosine to the L2-normalised mean of this head's
    positives; negatives reach the threshold and nothing else). Read together they answer the
    question the instruction was really about: **what did labelling negatives buy over the free
    alternative, and over no negatives at all?**
  - **Every mode is graded on the same rows.** The modes differ in what reaches the FIT and never
    in what is graded — otherwise "positive-only did better" could just mean "positive-only was
    asked an easier question". Grouped `StratifiedGroupKFold` on `listing_id` as before, and a
    free negative whose listing is being graded is dropped from that fold's training set, so the
    extra rows cannot re-open the leak the grouped split closes.
  - **Two scored populations, stored per image.** `split='cv'` is every training row scored
    out-of-fold; `split='exam'` is the sealed 250-image `exam_v1` holdout scored by the head refit
    on all training rows. Exam cells are graded by the ratified rule — a cell grades only when
    both sides said yes or no, an abstention on either side grades nothing, a declared default is
    not a judgment. That rule was **extracted** out of `exam_machine_review.agreement` into
    `exam_machine_review.human_verdict` and imported, never re-typed, so the machine review and
    the bake-off cannot drift apart. An abstained cell keeps its photo, its score and a NULL
    label; it is in no bucket and in no rate. Precision/recall/F1 are **NULL, not 0**, when
    nothing was proposed.
  - **The centroid mode's threshold is chosen, and says so.** A cosine has no natural 0.5, so the
    threshold maximising F1 on the pooled out-of-fold scores is picked and REPORTED — on the same
    scores it is then measured against, which makes that mode's F1 optimistic by exactly what the
    choice buys. Stated in the code rather than hidden; the alternative (a fixed cut on a cosine)
    would have handicapped the mode instead.
  - **Heads are chosen by a FLAG, never a list.** `--min-train-positives` (default 100) is the
    whole rule; every rejected tag is still reported with the count that rejected it. Measured
    2026-09-08: 15 heads carry 159-316 admitted positives against ~1,000 drawn negatives; a long
    tail has under 20 or none.
    - **Addendum, same day — the flag is the OPERATOR'S, not a count.** Ruling: *"Select heads by
      the ready flag I have on the training set page."* A positives count decided the experiment's
      scope on the operator's behalf; the ready marker on `/new-dedup/training-set`
      (`tag_taxonomy.review_state = 'ready'`, migration 487 — 443's `ready_for_training` was carried
      forward into it and is dead schema, so reading THAT column would freeze selection at the 487
      backfill) is a review decision and now selects, through one shared
      `tag_head_bakeoff.ready_heads` that both the manifest stage and the CPU runner call.
      `--heads` remains the explicit override; the admitted counts are still reported (log + the
      run's `note`) but never filter, so a ready head with too few rows trains, fails, and records
      why. `runs.min_train_positives` is retired in place — written 0, still echoed by the API.
  - **Migration 489** (`dedup_sim`, pgvector-guarded `DO`/`EXECUTE` like 480, RLS + revoke at
    creation, registered in both admin registries, `-- ci-allow-dynamic:` annotated, shape-tested
    offline by `tests/test_tag_head_bakeoff_migration.py`). In `dedup_sim` deliberately: this is
    experiment evidence, droppable wholesale at Wave 8, and nothing in a live read path joins it.
  - **The lane.** `scripts/tag_head_bakeoff.py --run-id N` runs the arm x mode x head cross
    product on CPU (the `training` extra only), resumable per cell and `--dry-run`-able; a cell
    that cannot be graded is RECORDED as failed with its reason rather than skipped silently.
    Read surface, admin-gated and read-only: `GET /new-dedup/tagging-bakeoff/runs`,
    `/runs/{id}/metrics` (the whole table in one payload), `/runs/{id}/images` (view A — a page of
    photos, each carrying what every arm and head said about it) and `/runs/{id}/buckets` (view B
    — one head under one arm and mode as its four outcome buckets plus a 20-bin score histogram
    split by label, its range measured rather than assumed, since the logistic modes score in
    [0,1] and the centroid mode in cosine space).
  - **No new label door.** Neither new module contains SQL naming `image_tag_labels`: training
    labels come through `machine_labeling.training_rows`, left-outs through
    `machine_labeling.training_set_page`, exam answers through `tag_exam.answers`. The holdout
    census therefore has nothing to exempt, and gained no entry.
  - **Phase 2, not built here**: the GPU job that fills `tag_head_bakeoff_vectors` per arm, its
    workflow, and the comparison page.

- 2026-09-08 (c) — **DINOv3 readiness closed out: licence accepted, migration 480 applied,
  revision pinned, manifest lane proven live.** Corrects entry (b)'s parallel-state line, which
  was already stale when written.
  - **Licence: ACCEPTED.** The operator's Hugging Face account (`waiff`) shows "DINOv3 — Gating
    Group Collection — Sep 5 — ACCEPTED" (screenshot shared 2026-09-08). Route = the HF gated
    click-through, not Meta's download form, so no `.pth` mirror is needed: the lanes read the
    gated repo with `HF_TOKEN`, which is set as an Actions secret alongside `RUNPOD_API_KEY`,
    `SUPABASE_DB_URL` and the four `R2_*`. For ENCODER-DECISION.md §3.4 item 4 (archive the
    text + hash + date): the public repo copy of the licence
    (`facebookresearch/dinov3` `LICENSE.md`, *Last Updated: August 19, 2025*, "Sections 3, 4 and
    7 shall survive") hashes to sha256 `25d122eb8f5b880fd23c736fb6ea8018ee45c12237e00b8a86d14c653904999e`.
    Which of the two circulating texts the HF gate itself presented can only be confirmed by a
    token-authenticated fetch of the gated repo's own licence file — small, still open.
  - **Migration 480 applied 2026-09-08 ~12:57 UTC** via the Supabase MCP, verbatim from main
    (sha256 47f5faa2…), and verified live in one row: RLS on with zero policies, `anon` and
    `authenticated` hold no privilege at all, `embedding halfvec(768)`, the eight-column primary
    key, `image_dinov3_embeddings_encoder_idx` present, 0 rows. It had been skipped over:
    481–488 were applied around it — the merged ≠ applied trap, caught by checking
    `to_regclass` rather than trusting the merge.
  - **Revision pinned** to `5931719e67bbdb9737e363e781fb0c67687896bc` in `data/dinov3_config.json`
    — the gated repo's HEAD as reported by the public model-metadata endpoint (lastModified
    2025-08-19T09:00:44Z), i.e. the commit the acceptance covers. Resolution, preprocessing and
    dtype stay null, so the loader still refuses and the embedding lane stays inert — the
    bake-off decides those three.
  - **Manifest lane dry-run (run 34228869976) succeeded against live data**: planner estimate
    11,540,593 images; P1a[sample] pool 1,000,000 → 3,000 pairs; P1b 3,000 images × 6
    transforms; P2 119 pairs (thin — same-listing same-tag hard negatives are rare by nature,
    inspect before trusting); P3 3,784; P4 961 (inspection only); 13,554 distinct images,
    0.61 MB manifest, no URL minted, nothing uploaded.
  - **Trainer already on the final door**: `toolkit/tag_heads.py` reads
    `machine_labeling.training_rows` (the migration-486 `in_training` model); the
    `training_set_positive_ids` door it was built on is gone with 474. Nothing to change.
  - #804 (the DINOv2-era bake-off draft) closed as superseded by #1300.
  - **Still not run, on purpose**: the bake-off pod pass (~$2 — the operator's call), any
    embedding pass, any training. Training waits on the set (one category still open).
- 2026-09-08 (b) — **Roadmap-vs-progress review; same-town candidate rung requested (docs
  only).** The operator asked whether "for the location candidates we will also select
  candidates based on same town only (much lower location accuracy, input data quality we
  cannot fix)" is in the roadmap. **It is not**: L0 as specified is street+geo+dispo →
  geo+dispo → geo+area with coordinates + 75 m and "no other fields such as obec"; the only
  town-related item is the parked centroid data-quality prerequisite. Recorded as an OPEN
  operator ruling, not designed. Facts the rule has to reckon with (measured today):
  Praha = 128,266 listings (47,801 active), Brno 32,174 (12,020), Ostrava 20,269 (8,752),
  Plzeň 13,442 (5,747) — "same town" alone in Praha pairs every listing with every other
  (~8 billion pairs all-time, ~1.1 billion active-only), so the rule must say at least: which
  listings it applies to (all, or only those whose location is town-grade per the location
  program's precision class), whether it combines with the disposition/area rungs like the
  other paths, what "town" is (obec; část obce for statutory cities?), and whether it is a
  4th fallback rung or a parallel path like B. Sequencing note: the location program's
  consumer flip is its own W6 — dedup still reads `listings.geom` + geo-derived admin columns;
  W2 should read location through ONE query it can later point at the location projection.
  Parallel state today: W0 closed (mig 475); W1 = operator reviewing the training set (heads
  13/14 added, membership model final mig 486, review sample mig 485, ready/not-ready marker
  mig 487); DINOv3 readiness code merged 09-06 (#1296-#1298, #1300) but nothing run — mig 480
  not applied, weights not mirrored, licence acceptance pending (HF request awaiting Meta;
  Meta's own download form is the no-queue route), bake-off not run. W2-W8 unstarted.
- 2026-09-08 — **Head 14, `podklad - 3d plán`, and the first head whose arrival CHANGED ITS
  NEIGHBOURS.** The tag already existed (id 39) and both neighbours already pointed at it; it had
  no definition and no routing, so it was never a head. Operator's precedence, now written on all
  three sides: **property list > 3D plán > půdorys** — *"if the document has both a půdorys on it
  and a 3d plan, include it on the 3d plan! A 3d plan can have the same basic description features
  as a půdorys — a list of rooms with areas, a simple summary of information or a broker logo, but
  not in the extent of a property list"*. So půdorys went v9→v10 (a mixed sheet now loses to the
  3D view; its old wording said only "rendered in 3D", which left mixed sheets ambiguous) and
  property list v5→v6, **narrowed**: it used to claim any 3D drawing composed with tables and
  branding, which under the ruling is wrong — a room list, a summary or a logo no longer promote a
  3D plan to a property list. Both neighbours' existing labels now read "old wording"; neither was
  re-judged (operator's standing instruction on půdorys).

  **`--like-tag` (#1335) is the new draw**: judge the newcomer over the images a sibling has
  ALREADY been judged on, so the boundary between them is reviewable on the same photos instead of
  inferred across two different random samples. Pool, never verdicts.

  **v1 was measurably wrong and a 200-image probe ($0.10) caught it.** The word "shading" read as
  flat colour fill, so a tinted 2D vector plan came back positive — 1 of the 3 positives was a
  půdorys. v2 makes the test RENDERED LIGHT (materials, cast shadows, visible wall thickness) and
  says outright that colour fill is not depth. **Ruling recorded, since the operator did not
  overrule it: a top-down plan rendered photorealistically IS a 3D plán** — the industry's "3D
  floor plan" — so the head is "rendered plans", not "plans in perspective".

  **Result: 251 positives / 10,293 negatives over 10,544 images, $5.57**, target met from the
  shared pool with no near-tag top-up. v2 verified on the three images that forced it: the flat
  tinted plan flipped to negative, both genuine renders held. Trays: 0 training positive / 251
  positive reserve / 1,000 training negative (drawn) / 9,293 negative reserve. The positives sit
  in RESERVE on purpose — a machine write proposes and only the operator admits (484/486); nothing
  about a new head suspends that rule.

  **135 of the 251 are still positive on půdorys too** — an exclusivity breach, and the predicted
  cost of adding a head between two others: those labels were written under v9, when a rendered
  plan was půdorys. Property list clashes: 0, so the narrowing held. The breach resolves by
  re-judging půdorys under v10 (~10.5k images, ≈$5.5) or by hand; NOT done here, per the
  operator's standing "do not re-judge půdorys".

  Measured: **$0.00052/image** single-head (vs a $0.00174 pre-flight, which blends the old
  twelve-head runs). That gap is a trap, not a curiosity: **`max_usd` is priced against the
  pre-flight estimate, not the real cost**, so a ceiling set from the measured rate refuses the
  batch outright — five 3,000-image batches did exactly that and spent nothing, reporting only
  their draw line until the database was checked. Set the ceiling against the pessimistic
  estimate and read `LABEL done`, never `LABEL drew`.


- 2026-09-07 (g) — **One rule for both signs (migration 486).** The operator renamed the trays —
  *training negative → **negative reserve**, reserve → **positive reserve**, review sample →
  **training negative*** — and the renaming is a model change, not vocabulary. Read together those
  lines say `in_training` is a fact about a LABEL, not about a positive. 484 admitted every negative
  wholesale ("nobody reviews ten thousand negatives" — true observation, wrong conclusion); the
  right one is to train on a drawn thousand and leave the rest in a reserve exactly as unadmitted
  positives sit in theirs. **This collapsed a mechanism a few hours old**: 485's `tag_review_samples`
  existed to remember which thousand were drawn, but "the drawn thousand" IS "the negatives admitted
  to training", so the draw needs no table — 486 moves that fact into `in_training` and 485's table
  becomes dead schema awaiting a destructive OK. The write path is symmetric too: a machine write
  PROPOSES whatever its sign, where before a machine negative was admitted on insert — the asymmetry
  that made "training negative" mean "everything the model ever rejected". Five trays now come from
  one expression (`state` + `in_training`), the page derives `inReserve` once instead of comparing
  against a literal in four places, and pre-rename links still resolve (`reserve` → positive reserve,
  `sample` → training negative). **The machine/yours marker is gone from the tile and progress
  counting with it** — the operator, for the second time: *"do not differentiate between what I or
  machine chose, if review sample has 1000 images, then they are all equal"*. The human-wins upsert
  rail still holds in the database; it is simply not a thing to read on every photo.

- 2026-09-07 (f) — **The review sample: a drawn thousand that does not move (migration 485).**
  The operator: *"Right now I have to verify thousands of negative images per head. I would like to
  draw a random sample of only 1000 images per head from the current negatives … I would prefer if
  the random sample was sorted the same way it is now, because the current negatives were built in
  an order, so images of certain types are grouped and that is easier for review."* So the draw is
  `ORDER BY random() LIMIT n` and NOTHING ELSE — no ranking, no clustering, and explicitly no new
  sort (*"I do not want you to create a sorting mechanism"*): the page renders the sample under its
  own `updated_at DESC, image_id DESC`, so the sample is a FILTER over the order that was already
  there and each photo sits further down the list than the last. **It is a TABLE, not a query**, for
  the reason 484 exists: a computed "1000 random negatives" re-draws on every read, and re-marking
  one of the thousand takes it out of `negative` and silently promotes number 1001. The lane
  therefore selects by MEMBERSHIP and passes NO state, so a photo just re-marked keeps its place
  wearing its new mark instead of vanishing mid-page; the drawn count is pinned while `sample_reviewed`
  moves. Progress counts `source <> 'machine'`, because confirming a machine negative leaves it a
  negative and state alone cannot see the work. Redraw is explicit (a confirm), since it discards a
  half-reviewed list. Same rails as every training read: no holdout, no missing bytes.

- 2026-09-07 (e) — **A link names a head and a photo, never a page number.** The duplicate audit
  produced 36 conflicting labels to look at, and the raw R2 links handed over were useless: they
  pointed at a stale API host from `.env` (`sreality-api`, unprovisioned — the live one is
  `sreality-production`), and even correct they would only have shown bytes, not the row's mark,
  its note or its tray. So `?image=<id>` on the training-set page, resolved by
  `GET /training-set/locate` → `machine_labeling.locate_in_training_set`: the SERVER computes the
  tray and the 0-based rank under the page's own `ORDER BY updated_at DESC, image_id DESC` AND its
  `storage_path IS NOT NULL` join, because a rank computed against any other row set pages to a
  different photo. The page divides by its page size, lands there, rings the tile and scrolls to it;
  a 404 (no label on that head, or a HOLDOUT image — refused on both the row and the rank's cohort)
  says so and leaves the page where it was. `scrollIntoView` is called optionally: jsdom lacks it,
  and a tile the operator scrolls to themselves beats a ref callback that throws mid-commit.

- 2026-09-07 (d) — **A tray and its count must ask the same question.** Migration 484 landed and
  the backfill verified to the row (katastrální mapa 301 in set / 535 reserve, garáž 329/120 — the
  29 over 300 are labels the operator made themself, kept wherever they rank). Two defects surfaced
  the moment the page was used at scale, both the SAME shape as the "300/300 that would not move":
  a control whose bookkeeping the operator cannot see. (1) `in_training` is a fact about a
  POSITIVE — every negative is admitted and a left-out trains nothing whatever the flag says
  (`training_rows` filters on state) — but the page filtered EVERY non-reserve tray by
  `in_training = true`. Harmless on negatives; on left-outs the backfill never admitted one, so the
  tray rendered 4 rows under a count of 1,064. The tray query now asks about membership only where
  membership is a question. (2) The move affordance was offered on the negative tray, where
  "return to reserve" un-admitted a row that NO tray counts: the photo vanished from the page and
  the optimistic patch moved `positive` and `reserve`, neither of which was involved. Membership
  controls now exist on Training · positive and Reserve only (`MEMBERSHIP_TRAYS`); a negative comes
  out by re-marking it, which is what the marks are for. Also: `PAGE_MAX` 2000 → 10000 so the
  largest tray (~10.5k negatives) is one page, and the tile opens the SHARED `ImageLightbox` — the
  listing gallery's viewer — instead of a new browser tab, widening the training row to
  `ImagePublic` rather than re-fetching a full image row per tile.

- 2026-09-07 (c) — **Membership is STORED and the operator's alone (migration 484).** Two models
  were wrong in opposite directions and the operator named both: 474's `training_target` COMPUTED
  membership from a rank, so the boundary moved on its own and a reviewed set was never stable;
  removing the cutoff (#1321) then dumped every reserve positive into the training set, turning a
  reviewed 300 into an unreviewed 836. Their words: *"I asked you to keep whatever was in the
  training positive set and I would move images from reserve to the training positive set as I
  would deem necessary."* So `image_tag_labels.in_training` is a fact about a row: a machine write
  PROPOSES (lands in reserve, always false for a positive; negatives are admitted wholesale since
  nobody reviews ten thousand), re-labelling never demotes, and the only deliberate change is the
  operator's own move (`POST /tags/{id}/training-membership`, chunked). The 484 backfill does NOT
  replay the old rank: `(source='machine') ASC, created_at ASC` was stable between READS but not
  across WRITES — confirming one reserve photo made it 'human', moved it to rank ~1 and EVICTED
  whatever sat at rank 300, a photo already reviewed and accepted (the operator: *"do not split
  between machine and me, those that were in the training set before needs to be there, I have
  already reviewed them and they were ok, the rest was in the reserve"*). So the reconstruction is
  the union of the oldest `target` positives by `created_at` (stable — a re-label cannot change
  when a row was created) with every positive the operator labeled themself, wherever it ranks
  (those floated to the front under the old expression, so they were in the set). Four trays:
  Training · positive / Training · negative / Reserve / Left out; the anyone-machine-yours
  breakdown is gone at their request. `training_rows` reads admitted rows only, so the page and
  the DINOv3 trainer cannot disagree. 474's `training_target` is now doubly dead — prune both it
  and nothing else in a forward migration when a destructive change is OK'd.

- 2026-09-07 (b) — **No limits: a head's training set is every label it has (operator ruling,
  replacing the cutoff).** The operator confirmed a reserve photo on katastrální mapa and saw
  "300/300" unchanged — correct under a capped set (the confirmation swapped it in and pushed
  the last machine positive out), and precisely the kind of invisible bookkeeping they did not
  want: "I do not need the limits anymore, just make sure counts are up to date and react
  instantly." So: no target, no ranking, no reserve. **Training · positive = every positive,
  Training · negative = every negative — the operator's and the machine's — Left out = every
  exclusion**; a count is a plain count of labels and moves optimistically on every click. ONE
  trainer door, `machine_labeling.training_rows` (positives + negatives, human + machine,
  holdout excluded) — the trays read the same rows, so page and trainer cannot disagree. This
  also overrides the four-tray redesign's choice that only HUMAN negatives train: the machine's
  ~10k negatives per head ARE training material now (the operator asked for the actual count).
  The bounded review survives as a filter, not a cap: "the machine's" within a tray. Migration
  474's `training_target` column is now unused — dead schema to prune in a forward migration
  when the operator OKs a destructive change; it is harmless meanwhile.

- 2026-09-07 — **`/new-dedup/training-set` reduced to four trays (operator ruling: "the
  filters are too difficult").** The three composable filter groups (cutoff × verdict ×
  decided-by), the "To review" preset and the per-page "Confirm the other N" bulk-confirm are
  gone. What remains is one tray strip, each tray ONE server query that is exactly what the
  trainer meets: **Training · positive** = `membership=set` (the ranked positives up to the
  target, the operator's first, then the machine's oldest-first — `training_set_positive_ids`);
  **Training · negative** = `state=negative&source=human` (the human/human_confirmed negatives,
  the only door `tag_holdout.training_label_rows` reads negatives through — the machine's
  negatives are in NO tray, and the page states their count rather than hiding it;
  `training_set_counts` now splits negatives by who decided, `human_negative` being the size of
  that set); **Reserve** = `membership=reserve`; **Left out** = `state=excluded`. The tile keeps
  its three verbs, who-decided, set position, old-wording flag and the note; the target editor
  stays because it IS the set/reserve boundary. Old links carrying `set=review|all` land on the
  positive tray. Finalisation is now the operator saying the trays are right — that, not a
  confirm count, is the gate to the first head training run.

- 2026-09-07 — **Head 13, `podklad - property list`, built ad hoc in one session (~$1.80);
  the add-a-head runbook above is what it verified.** No exam (operator's call: the taxonomy
  suffices for the model; the operator's review is the QC — so this head has NO measured
  precision/recall and cannot be compared on the gate). Definition rewritten to v5 with six
  DOES NOT COUNT cases — the boundary had lived only on půdorys's side, invisible when this
  head is labeled alone; ruling: a no-plan marketing sheet still counts. Routing set from the
  new Taxonomy-page control (byt, dům, komerční). Draws: seeded from půdorys 800 → 182 positives
  (23%, $0.63); its 10 drafts; seeded from its own positives 800; random 1,000 for unbiased
  negatives. Result: **412 positives, 2,198 negatives** across 2,610 images — 300 in the set,
  112 in reserve, all 300 awaiting the operator's review. Cost fact: a single-head prompt is a
  fifth of the twelve-head one ($0.0008 vs $0.0023 per image) — the definitions, not the photo,
  dominate at twelve heads. Spend ≈ $33 of $50.

- 2026-09-05 (f) — **DINOv3 readiness build: all four PRs up, draft, CI green, per entry
  (b)'s directive.** #1296 (migration 480, the vector store), #1300 (the bake-off harness),
  #1298 (the production embedding job + dispatch workflow), #1297 (the per-tag heads trainer +
  eval harness). **None of it has been run against real data** — no corpus pass, no bake-off
  dispatch, no training, no money spent, no gated weights downloaded, no vectors written
  anywhere — per entry (b)'s "training set is not finalized" ruling; everything is built and
  tested on synthetic/offline fixtures only. Merge order matters only for one mechanical
  cleanup: #1297 and #1298 each carry a temporary `tests/test_sql_schema_prepare.py` allowlist
  entry (self-flagged in both) excusing `image_dinov3_embeddings` from the schema-replay check
  because migration 480 isn't live in their branches — **delete both entries the day #1296
  merges**, or they'll silently keep masking a real PREPARE failure if the table is ever renamed.

  **Migration 480**: one row per (image, full six-fact encoder configuration) — model, revision,
  library, pooling, resolution, preprocessing, dtype are all part of the primary key (not a
  synthetic `encoder_id` some writer could set inconsistently), so a knob change adds a row
  instead of silently overwriting a differently-configured vector for the same image. `halfvec(768)`
  (pgvector 0.8.0 confirmed live) sits below the ~2 KB TOAST threshold that `vector(512)` on
  `image_clip_embeddings` sits above (ENCODER-DECISION.md §2.1) — a genuine read-cost win, not
  just a size one. RLS + REVOKE posture (migrations 237/447) replayed at creation, inside the
  same pgvector-conditional `DO` block migration 226 uses for CI replay. Additive; **not yet
  applied to production** — pending operator OK per the migration-safety gate.

  **Gate 1's 12th tag resolved differently than expected.** Asked the operator which
  "domovní vchod" tag (exteriér id 2 / interiér id 19) was the 12th target; the answer was
  that BOTH stay, now under separated names (`tag_taxonomy` live: id 2 = "exterier - domovní
  vchod", id 19 = "interier - domovní vchod / chodba") — the choice between them was never
  meant to be made, the ambiguity was in the shared name. Consequence for the heads trainer
  (PR 4): it takes a target tag id as a plain argument rather than hard-coding a resolved
  12-tag list, so it needs no further change whichever way Gate 1's list finally reads.

  **RunPod / HF acquisition note for PR 2/3**: `scripts/mirror_model_weights.py` +
  `mirror_model_weights.yml` already exist (an earlier session's prep, never run —
  `MANIFEST.json` does not exist in R2 yet) for mirroring Meta's raw, licence-accepted
  `.pth` download-e-mail files into R2, avoiding both Meta's time-limited links and an
  HF_TOKEN dependency for an unattended job. That consumption path (loading a raw state
  dict rather than `from_pretrained`) is NOT built here — out of scope for this readiness
  pass. The bake-off and the production job instead load gated weights the standard HF way
  (`from_pretrained(model_id, revision=..., token=...)`, the operator's `HF_TOKEN`), matching
  `scraper/clip_tagger.py`'s existing pin discipline. Revisit the R2-mirror consumption path
  later if an HF-token-free production lane becomes worth building.

- 2026-09-05 (e) — **GATE 0 CLOSED. Wave 0 finished for real (migration 475, #1286),
  eleven days after it was recorded closed.** Entry (b) above corrected the record; this one
  discharges it. Migration 475 applied live at **11:26:48 UTC**, verified immediately after.

  **Dropped** (all seven re-dumped to `backups/new-dedup-teardown/2026-09-05/` first — run
  33962204424, COPY row counts read out of the artifacts themselves and matching live
  exactly): `property_identity_candidates` 159,260 + `_archive` 5,542,
  `dedup_dirty_properties` 15,357, `dedup_scan_state` 3, `dedup_batches` 265,
  `dedup_batch_requests` 18,250, `dedup_engine_runs` 9,932 — **112 MB** — plus the six admin
  views (`dedup_engine_runs_public`, `dedup_scan_state_public`, `dedup_engine_flow_public`,
  `dedup_queue_snapshot_public`, `dedup_recency_backlog`, `dedup_label_events`) and
  `listings_dedup_eligible_idx` (migration 127), which pg_stat_user_indexes reported at
  **idx_scan = 0** while costing 6 MB of write amplification on every `listings` write. The
  backup script needed one fix to run at all: two of its nine relations were already dropped by
  migration 432 and `pg_dump --table` on a missing relation exits non-zero, so `check=True` had
  been turning the whole run red — a missing relation is now `ALREADY GONE`, named, and
  excluded from the failure list (both matviews are in the 2026-08-05 dump).

  **The publication gate is gone, not just inert.** CUTOFF §3 step 2 was never done in August:
  the dead predicate was still in `properties_public`, `browse_projection` AND
  `listing_feed_public` (that third one is not in CUTOFF's list — found by asking the catalog
  which view definitions mention the function, rather than trusting the doc). All three
  redefined without it, then `publication_gate_enabled()` and `publication_gate_health_public`
  dropped. `properties.published_at` / `publish_reason` kept frozen. **Note for anyone reading
  the old incident:** the PR-#707 InitPlan lesson now has no live example — the function is
  gone — but case 2 is still reachable through any `SECURITY DEFINER` gate, and the RLS
  policies are where it lives now. The `database` skill says so.

  **Two things the doc got wrong and live state settled.** (1) `properties_map_mv` does not
  embed the gate — it reads `browse_projection`, so redefining the projection was enough; no
  rebuild function mentions the gate at all. (2) CUTOFF says "the four `realtime_dedup_*`
  worker keys"; exactly one still existed. 24 `app_settings` keys deleted by literal name,
  never by `LIKE` — `dedup_%` would have swept away Wave 1's own `dedup_sim` keys. Inside
  `pipeline_check_thresholds`, which is a LIVE key (verify_pipeline / ops_incidents /
  system_alerts all read it), only the 11 dedup-engine checks came out; the 6 that are still
  code defaults survive, verified by grepping each key across the whole repo first.

  **A near miss worth keeping.** The three view definitions were taken from the latest
  migration that defines each, then checked against live by md5 of the ordered column list
  before anything was written. `properties_public` came back 82 columns against live's 83 —
  because migration 425 writes `create or replace view public.properties_public` and the
  "latest defining migration" regex (in `tests/test_browse_read_path_guardrail.py`, reused
  here) does not allow a schema prefix, so it had been resolving to migration 375, one column
  stale, for months. Its read-contract test was reading the wrong select list. The regex is
  fixed and all three hashes matched before the migration was written. **Hash-check a live
  object before restating it** — the lesson from the migration-438 outage, earning its keep.

  **Legacy stamp:** `property_merge_events.generation` = `'legacy'` on all 124,363 existing
  rows, nullable with no default so new rows cannot inherit the claim. Confirmed on a real
  merge during the checklist below: an operator merge writes `generation = NULL`, which is
  correct — it is neither the removed engine's nor Wave 8's.

  **CUTOFF §7 step-8 verification checklist — six items, ALL PASS** (run 2026-09-05, live):
  1. **CI green** — `CI: tests` and `CI: schema replay + SQL correctness` both succeeded on
     the branch. The replay job rebuilds the schema from migration zero, so it is what
     validated 475 itself. 6,374 tests pass locally, 247 skipped.
  2. **A brand-new property appears everywhere without a stamp** — of the properties created
     after the apply, 100% have `published_at IS NULL` and 100% are returned by
     `properties_public`, `browse_projection` and the watchdog matcher's own query shape
     (`properties_public` filtered on `first_seen_at`). They flow into `browse_list` and
     `properties_map_mv` on the next rebuild of each (15-minute and :07/:37 cadences — the
     lag is the cron, not the gate). 152,049 active properties carry no stamp and are visible.
  3. **Scrape lanes unaffected** — in the first three minutes after the apply, all eight
     portals wrote: sreality 2,057, bezrealitky 782, realitymix 526, bazos 280, idnes 260,
     ceskereality 235, remax 39, maxima 28. Realtime worker heartbeat 11 s old.
  4. **Image + tag lanes unaffected** — 983 image download attempts in the same window;
     `clip_tag.yml` and `compute_image_phash.yml` both green on their last runs, 5,830 CLIP
     tags written in the most recent lane run.
  5. **Health clean** — every active pg_cron job succeeded after the apply, including
     `refresh-health-dashboard` and `browse-list-rebuild`. The only failures in the table are
     pre-existing statement timeouts on health matview refreshes (the ~35% rate migration 432
     documented), none of them touching a dropped object.
  6. **Merge → carry-over → browse → unmerge, end to end** — performed by CLAUDE under the
     operator's explicit authorization in this session, through the real admin auth path (the
     `+claude-admin` smoke-test account's Supabase JWT; every merge route is `require_admin`,
     so no shared token was involved). Properties 227457 (survivor, older) and 258581, a
     genuine same-street/same-disposition/same-area/same-price byt pair on different portals.
     `POST /properties/merge` → retired went `merged_away`, its listing re-pointed, survivor
     `source_count` 1→2, **its notification_dispatch and status event carried onto the
     survivor**, one `property_merge_events` row, and `browse_list` updated synchronously
     (retired gone, survivor present — read-your-writes held). `POST
     /properties/merges/{id}/unmerge` → `conflicts: []`, both properties active again with one
     listing each, both back in `browse_list` and `properties_public`, event marked undone.
     **Recorded asymmetry, expected and not a regression:** the carried notification_dispatch
     and the status events stayed on the survivor rather than returning. That is CLAUDE.md
     rule 18's documented "unmerge/split are best-effort" for operator state, and append-only
     journals do not un-append; it predates this migration.

  Nothing in Wave 1, the encoder decision or Wave 2 was touched. No dedup rule, threshold or
  setting was added or changed — the 24 deleted keys configured code that no longer exists.

  **Numbering footnote.** This shipped as 474 and was renumbered to **475** when entry (d)
  below merged 474 first — `tests/test_migration_numbers.py` caught the collision, which is
  what it is for. The file had already been applied live under its old number, and the only
  place the number reached the DATABASE was the `generation` column's comment, which was
  corrected in place so live matches the file. Worth knowing for next time: this collision is
  structural, not carelessness — `ls migrations | tail` reads the number free WHEN YOU LOOK,
  and a long session between looking and merging is exactly the window another branch lands in.
- 2026-09-05 (d) — **The cutoff: a head's training set is a QUERY, not a list (migration 474).**
  The operator's objection: reviewing 1,149 fasáda positives is exactly the manual work the
  programme exists to remove. Answer: each head has a TARGET (`tag_taxonomy.training_target`,
  NULL = 300 — the per-class count past which a logistic probe on frozen CLIP features shows
  little further gain), and its set is defined as the ranked positives up to that target: the
  operator's own first (confirmed), then the machine's oldest-first in a total order. Past the
  target is the RESERVE. Because it is a query, removing a wrong positive pulls the first
  reserve image in with no bookkeeping, and confirming one (writing it as a human label) keeps
  it in. The review is therefore bounded: "To review" on `/new-dedup/training-set` = in-set
  positives still on the machine's word alone (fasáda: 282 of 1,149). **The trainer reads the
  same list** — `training_set_positive_ids` is the second sanctioned door beside
  `tag_holdout.training_label_rows` (which reads human labels ONLY, so the 10,544 machine labels
  would otherwise train nothing) — so what was reviewed and what is trained on cannot diverge.
  Every read tolerates 474 not being applied (default target for all heads).

- 2026-09-05 (c) — **Training-set review surface + the operator's reasons (migration 473).**
  10,544 machine labels existed with no way to look at them; `/new-dedup/training-set` reads
  them head by head (server-side filters by verdict and by who decided; paging with a unique
  tiebreaker; each tile names who decided and flags a label written under since-replaced
  wording; the holdout excluded and said so). A correction there is a HUMAN label, which the
  store's human-wins rail protects from every later machine pass. Then the operator's ask: a
  NOTE with each changed mark, so the why reaches the definition. `tag_label_notes` records
  (from_state, to_state, note) beside the write — one request for mark and reason — and the
  taxonomy page shows a head's open notes beside its editor with an "absorbed into vN" action.
  **THE ABSORPTION RULE (operator's words, ratified):** notes are NOT copied into the definition
  one sentence per note. The definition is read by a model and by a person, and either absorbs
  a short general rule and drowns in a list of specifics — think of a human annotator who must
  hold the whole definition in their head. The reviser reads a head's open notes TOGETHER,
  finds the rule they point at, states it ONCE at the level of the existing lines (most often
  as a DOES NOT COUNT boundary — the model treats that list as law and `confusable_with` as
  advice, measured on fasáda), saves, and marks the batch absorbed by that version so no note is
  read into two revisions. The rule lives in the migration comment and the module docstring,
  not only here.

- 2026-09-05 (b) — **Operator rulings on Gate 1, the labeling budget and the encoder** (docs
  only; the decisions-ledger rows above were rewritten to match). Gate 1: 12 target tags (listed
  in the ledger row); machine-made labels count; the agreement report is a diagnostic, not a
  gate; **the training set is not finalized — no training on it yet.** Budget: the remaining
  ~$47 is NOT to be spent unless needed; no paid next step had been agreed, so nothing is
  scheduled. Encoder: **DINOv3 ViT-B/16 as the primary embedding for heads + L3 + path B**,
  conditional on the operator accepting the licence (free + commercial-use yes; §2.8 terms are
  theirs to accept — needs the operator's Hugging Face click-through so an acceptance record
  exists); the CLIP lane continues on new images in parallel for later comparison (storage
  grows on both stores, ~6 GB/month combined). Directive: get the DINOv3 embedding job and the
  per-tag heads trainer READY so training starts the moment the set is finalized. Dependencies
  to be added with rule-7 justification as analysis/training-only extras: scikit-learn (heads),
  faiss (path B k-NN). Open, to be answered by the operator: (a) which "domovní vchod" tag is
  the 12th target (exteriér id 2 / interiér id 19); (b) embedding cadence for new images —
  near-real-time question raised; options costed in chat (nightly batch ≈ $0.05/day, up to 24 h
  latency · hourly pods ≈ $0.30–0.50/day, ~1 h · RunPod serverless per-request ≈ $0.3–1/day
  estimated, minutes — needs a measured number; CPU on the always-on worker rejected as a risk to
  the scrape loops). Design rule regardless of cadence: embedding + tagging are an asynchronous
  stage AFTER publication; nothing in the ingest path waits for them; the LLM labeler is never
  in the pipeline at all (training-set construction only). Next session (fresh context): the
  readiness build — bake-off harness rewrite, new vector table migration (RLS/REVOKE posture
  replayed, pgvector-conditional for CI), DINOv3 embedding job on RunPod with checkpoint/resume +
  write throttle, heads trainer + eval harness reading `image_tag_labels` with the holdout
  excluded — none of it run against the unfinalized training set.
- 2026-09-05 — **Program review + encoder decision draft (docs only; nothing decided).** A
  12-agent review pass (progress audit, four encoder studies, one-encoder feasibility, cost
  model, synthesis, three adversarial refuters, revision). Findings:
  1. **Correction to the 2026-08-27 entry: Gate 0 is NOT closed.** Migration 432 (#1167) dropped
     only the funnel/cost matviews + their `_public` views, archived the funnel matview and
     unscheduled its cron job. It did NOT drop the six legacy tables
     (`property_identity_candidates` + archive, `dedup_dirty_properties`, `dedup_scan_state`,
     `dedup_batches`, `dedup_batch_requests`, `dedup_engine_runs`), the six other views, or the
     127 eligibility index; did not add `property_merge_events.generation` (the legacy stamp W8
     needs); and the `app_settings` sweep + the CUTOFF §7 step-8 verification checklist were
     never run or recorded. W0 stays open on exactly those.
  2. **W1 tooling is complete** through #1280; seven 2026-09-05 PRs (#1263, #1264, #1265,
     #1266, #1268, #1269, #1280) were unrecorded until this entry. Live training-set state
     (measured today): 18 active heads carry human labels — 459 human positives (max 40 on one
     head), ~9.4k human negatives, 90 excluded, 1,054 `human_draft` cells; 13 heads carry
     250–1,130 gpt-5-mini machine positives over 10,544 labeled images (7,501 positives / 118k
     negatives), 5 heads none (předsíň/chodba, letecký snímek s ohraničením, chodba/schodiště,
     domovní vchod/chodba, wc, parkoviště); exam holdout = 250 images.
  3. **No trainer exists** (grep for sklearn / LogisticRegression / GroupKFold across toolkit,
     scripts, api = zero hits; no training extra in pyproject). W2–W8 not started. Note the
     numbering trap: roadmap's "W2 SHIPPED (#1228–#1231)" is the labeling sub-programme's wave
     (the sealed exam), not this file's W2 (Level 0 candidates), which has no code.
  4. **Gate 1 has three live ambiguities the operator must rule on** (not resolved here): the
     target list (11 spec-named tags vs 18 defined heads vs the 8 routing tags flagged
     `priority`); whether machine positives count toward "150 per tag" (today's `gate_count` is
     source-blind, so they do count on every surface); and the per-head agreement threshold that
     clears a head for machine building (deliberately not in code).
  5. **Encoder decision**: `docs/design/new-dedup/ENCODER-DECISION.md` (DRAFT — proposed,
     gated, operator-owned) recommends ONE encoder serving the tag heads, L3 similarity and path
     B — DINOv3 ViT-B/16, 768-d `halfvec` — gated on a ~$2 bake-off, a defined licence review,
     scikit-learn + faiss approvals under rule 7, and a disk-headroom check; DINOv2
     ViT-L/14-with-registers (Apache-2.0) is the licence-clean near-equal; SigLIP2 wins the tag
     job but fails near-duplicate retrieval; keeping CLIP B/32 is the zero-cost stick option with
     no licence grant and no measurement on job (b). Pre-flight readout run live today:
     `image_clip_embeddings` = 11,301,885 rows (10,489,289 `revision NULL` = pre-pin, 812,596
     pinned), table 31 GB, database 150 GB; 11,303,863 images stored in R2; velocity ≈ 55–60k
     images/day (39,794 listings first seen in the last 7 days). **No ledger decision moves until
     the operator rules.**
  Next session: operator rulings (Gate 1 meaning, bake-off go/no-go, dependency approvals,
  embedding cadence, sampling strategy for the remaining labeling budget); independently and
  in parallel, close W0 for real (the proper PR-3 migration + the recorded checklist).
- 2026-09-04 (b) — **The LLM builds the training sets; the gate decides which heads it may
  build (operator direction; migration 468).** Ruling: stop drawing human cohorts to rescue thin
  heads — wc and parkoviště are left alone — and push instead on the model labeling in quantity
  for the heads already defined. Two pieces, in the order evidence demands. First
  `scripts/exam_agreement.py`: per-head precision / recall of the machine review against the
  human exam answers, with the ratified grading rule enforced — a cell grades ONLY when both
  sides said yes or no, an abstention on either side trains nothing and grades nothing (scoring
  it as a negative would punish the model for obeying the leave-out rule and inflate the
  denominator), and precision/recall are None rather than 0.00 when nothing was proposed, since
  "never proposed" and "always wrong" are opposite facts. Second `scripts/label_images.py` +
  `label_images.yml`: one call per image carrying the ACTIVE definitions of the NAMED heads,
  verdicts written to `image_tag_labels` as `source='machine'` — no new store, because that
  upsert already refuses to overwrite a human cell and stamps `definition_id` + `model` (the
  exam keeps its separate table only because there the suppression would hide the disagreement
  worth reading). Rails: no exam member is ever labeled (holdout unseen, curated is the
  operator's); heads named explicitly, no label-everything switch, since bulk labeling is only
  justified for a head the gate cleared; a leave-out stored as excluded/'pruned', NEVER as a
  negative; an unusable reply writes nothing at all; resume by provenance, so a definition edit
  re-opens exactly the heads whose wording moved; lane defaults to dry_run.
  **Cost structure, measured from the rendered prompt:** the 768px photo and the reasoning
  dominate — the eighteen definitions add only ~$0.0016/image — so a cheap screen-then-verify
  two-stage would pay for the photo twice and save almost nothing. One good pass per image is
  the right shape. With ~$47 of the $50 left, that is roughly 4-8k labeled images IN TOTAL,
  once; a purely random draw would spend most of it re-confirming the already-strong heads, so
  the sampling strategy is an open operator decision to be taken WITH the agreement numbers.

- 2026-09-04 — **One ruleset, written the same way on every tag; the machine reviews the exam
  against it (migration 467).** The operator's audit question — is "left out" vs "negative"
  applied by one logic across fasáda-among-houses, open-plan kitchen/dining/living, the three
  document kinds, a bathroom seen through a door, a toilet inside a bathroom — exposed that the
  eighteen definitions carried the ratified three-tier calculus unevenly (only kuchyně spelled
  out the present-but-secondary tier; koupelna/wc wrote the same case as a named exclusion, i.e.
  a NO). The ruleset, now stated once: the question is what the photo is an image OF; the tag's
  SUBJECT is what the definition says (a room kind, a composed shot, a document kind), never the
  object inside it — so a toilet is not a wc room and a building among many is not a fasáda
  (absent subject = negative), while "left out" is reserved for the subject itself being present
  but secondary. Documents are the deliberate exception: kinds are exclusive, one decisive
  feature assigns exactly one, the rival is negative. Three rulings: fasáda = ONE building even
  inside a joined block (a long angled/distant stretch of 3+ is the street, negative); a CLOSED
  garage door in a house photo is negative (subject absent), reversing the earlier left-out; every
  space tag now carries the same three sentences (composed on it → yes; glimpsed → no; clearly
  and substantially in frame but composed elsewhere → skip), every document tag the same
  exclusivity sentence, and the prompt/card label for that field reads "leave out (skip)" instead
  of "undecidable". Definitions saved live (all 18 bumped). Then the mechanism the operator asked
  for: a definition-driven machine review — one gpt-5-mini call per exam image carrying ALL
  eighteen definitions and the three-tier rule once, yes/no/skip per tag, stored in its own table
  (`tag_exam_machine_reviews`, provenance = asked list + definition versions frozen per row; stale
  rows never served, re-offered by the lane; dismissals reset on re-review) and rendered on the
  review page as PROPOSALS per row: apply = the exam's own whole-image /answer, keep mine = a
  dismissal. Never labels. The 461 suggestions (name-only, pre-definition) stay as the anchoring
  audit and are not the same thing.

- 2026-08-31 — **The exam instrument decouples from the holdout role; drafts declared (operator
  ruling; migration 464).** The operator's reframing, accepted after working the inconsistencies
  through: the pre-exam labels (1,522 cells, mostly positives, made without guidelines) are NOT
  the training set — they are DRAFTS, and the trusted labeling instrument is the exam UI. So
  cohorts carry a PURPOSE: 'holdout' (unchanged contract — random/stratified, weighted, excluded
  from training; exam_v1 + its 84 careful answers stay the yardstick) and 'curated'
  (operator-marked images re-seated for careful re-labeling through the same UI; their answers
  ARE training material; frame='curated', p=1, excluded from population-weighted statistics by
  FRAME, never by luck). The one-exam-per-image index now works FOR the split: a trained-on image
  can never later enter a holdout. Mechanics: HOLDOUT_EXCLUSION narrowed to purpose='holdout'
  (one constant; census marker updated); the WARM-UP deliberately stays cohort-blind (the
  answer-refusal rail only refuses NON-members, so a curated member served as practice would be
  silently accepted — caught by the inventory pass); existing 'human' labels off holdout members
  demoted to source='human_draft' (drafts never win an upsert, are read by no truth path, and
  seed the curated draw); tag_candidates cleared on operator order (2,282 rows; backups
  backup_464_*). The curated draw is rarest-first, 20/tag across all 16 flagged categories.

- 2026-08-30 — **Exam keys become letters, sets cap 12, machine suggestions ON (operator
  ruling; PR: exam letters + suggestions, migration 461).** Three operator instructions in one
  turn: (1) set_2 extended by "chodba / předsíň, ložnice, chodba / schodiště, vstupní dveře" —
  mapped to tags 30/26/18 one-to-one; "vstupní dveře" has no single tag, so BOTH entrance tags
  (2 exterier, 19 interier) were seated, trimming either is one array edit. (2) The exam keys
  are the letter grid w e i o / s d k l / y x n m — twelve positions laid out like the keyboard
  (Czech QWERTZ's digit row is shifted), sets capped at 12 where the keys run out. (3) Each
  exam image is pre-run through gpt-5-mini and the suggested buttons get a subtle dot
  (`tag_exam_suggestions`, `scripts/suggest_exam_answers.py`, suggest action on the screen
  lane). This REVERSES the exam's founding no-suggestion posture ("an exam the machine helped
  answer cannot grade the machine") — the operator ordered it knowing that context. Recorded
  consequence: sittings now measure agreement with a machine-ANCHORED human, not blind
  agreement. Mitigations built in: every suggestion is stored beside the final answer with the
  exact question list it answered (suggested-vs-final anchoring stays computable per image and
  per tag, forever); a suggestion is a mark, never a pre-filled verdict; a stored suggestion is
  served ONLY when its asked list equals the sitting's current list (sets grow by columns — a
  3-tag answer must not mark a subset of 8 buttons while looking complete); the suggester's
  prompt is precision-tuned, the exact opposite of the screener (a wrong mark anchors, an
  omission merely leaves a button unmarked). The worker engine was extracted to
  `toolkit/vision_batch.py` — screen and suggest share one loop (budget checked pre-call in the
  worker, per-worker connections), third consumer (W3 machine relabel) already on the roadmap.

- 2026-08-27 (part 2) — **Course correction (operator instruction): no invented rules.** The
  operator's standing principle, now in the non-negotiables: the pipeline must stay simple
  enough to hold in one head; NO new rules, safeguards, or mechanisms enter the plan until the
  operator asks. Retracted from part 1 accordingly: the three path-B anti-catalog safeguard
  options (the RISK stays noted under Open items with no mechanism attached); W4's "per-path
  safeguard settings" clause; and the two Claude-added tag classes (pooled `interiér – ostatní`,
  `other` sink). The probe target list is now **exactly the 11 tags the operator's spec names**:
  interier - kuchyně · interier - koupelna se sprchovým koutem · interier - koupelna s vanou ·
  interier - koupelna · technické zařízení / místnost · exterier - fasáda · podklad - půdorys ·
  podklad - katastrální mapa · podklad - letecký snímek s ohraničením subjektu · garáž ·
  exterier - parkoviště. Open operator questions carried (answered whenever the operator
  chooses; nothing is designed around them meanwhile): (a) how "interior" is recognized for
  ostatní's any-two-interior rule at labeling/probe time; (b) path B's two search parameters
  (neighbor count, minimum similarity to propose) at W3 build time. Images matching none of the
  11 classes are handled inside implementation space (abstain — no trained class, no operator
  labeling budget) unless the operator directs otherwise.
- 2026-08-27 — **[PARTLY SUPERSEDED by part 2 above — the safeguard options and the two added
  tag classes described here were retracted; read part 2 first.]** Plan updated on two operator
  edits (docs only, no engine code; challenges raised and recorded). **(1) Candidate path B** — previously only a parked poor-geo idea — is
  now a first-class selection path in parallel with A for all types (ledger row added; W2's audit
  is B-ready; B builds in **W3**, not W5: it needs the new tags but NOT DINOv2, because it runs
  on the existing corpus-wide CLIP 512-d vectors — so the candidate-scoped DINOv2 decision stands
  and no ~35 GB corpus backfill returns). Challenge raised, parked as an operator decision at the
  W3 gate: B has no location anchor, so developer-catalog/stock photos would merge at L2 on one
  identical pair — safeguard options under Open items. **(2) Probe v1 narrowed to ~10–15 target
  tags** (was: the whole ~49-label taxonomy). Gate 1 re-scoped to target tags (border-case
  exclusion from 2026-08-21 unchanged); existing granular interior labels fold into a pooled
  class via the training-time collapse map, so prior labeling effort still counts. Proposed
  target list (13 — operator confirms at the next labeling round): interier - kuchyně · interier
  - koupelna se sprchovým koutem · interier - koupelna s vanou · interier - koupelna · technické
  zařízení / místnost · exterier - fasáda · podklad - půdorys · podklad - katastrální mapa ·
  podklad - letecký snímek s ohraničením subjektu · garáž · exterier - parkoviště · interiér –
  ostatní (pooled: all other interior rooms; serves ostatní's any-two-interior rule) · other
  (non-interior OOD sink). Follow-up promoted from low-severity (2026-08-06) to real W1 work:
  `taxonomy_labels` needs a non-destructive **probe-target flag** so the coverage strip + Gate-1
  bar track target tags only (today the only lever is the cascading DELETE). Also noting for the
  record: the W0 teardown migration (the long-open "PR-3" blocker) **landed 2026-08-25 via
  #1167 (mig 432, cardinality W0b)** — Gate 0's remaining item is done. **[CORRECTED 2026-09-05: overstated — 432 closed only part of CUTOFF §4; Gate 0 is still open. See the 2026-09-05 entry.]** Next session: implement
  the probe-target flag + scope the coverage UI to it; then labeling rounds continue.
- 2026-08-21 (part 2) — **Gate 1 stops counting border cases** (operator decision, reversing the
  call recorded in part 1 below — which had deliberately left `confirmed_count` alone rather than
  redefine the gate metric unilaterally). Operator's rule, verbatim: *"border case does not count
  toward gate 1 unless removed from border case group."*
  - **`taxonomy_overview` now returns three numbers instead of one**, from a single subquery
    (`count(*) FILTER (WHERE bc.image_id IS NULL)` over a LEFT JOIN — still **no migration**):
    `gate_count` (what Gate 1 measures), `border_case_count` (parked), and `confirmed_count`
    (their sum, the raw inventory). The gate predicate is computed in SQL, not subtracted in the
    client, so it has exactly one definition for every future consumer — W3's trainer included.
  - **"Unless removed from the group" is free**, because the exclusion is a JOIN and not a column
    on the training row: clearing the flag restores the count immediately, with no relabelling and
    no backfill. Pinned by its own test.
  - **Every coverage surface moved to `gate_count`** — the bar and its value, the sort, the
    domain max, the `≤ N imgs` ceiling, and the tag-picker counts. `confirmed_count` survives in
    exactly the two places that mean "rows that exist", not "progress": the manage modal's
    "N confirmed · M pending" line and its remove-confirmation ("N training examples go with it"),
    which is literally what the DELETE takes.
  - The chart's border annotation flipped meaning with the number and now reads `· N parked`,
    outside the bar rather than inside it. It stays visible on purpose: a tag sitting on a pile of
    parked images is a signal about the TAG (too vague to label against), not just those photos.
  - Tests: 2 new backend cases (the split; unflagging restores the count) and 2 new page cases
    (the bar reads the gate number; the ceiling narrows by it), both page ones verified to go red
    when the surface is pointed back at `confirmed_count`.

- 2026-08-21 — **Labeling page: the "Border case" flag** (operator request, pointing at
  /clip-audit as the model). The flag itself is not new — `image_border_cases` (migration 310) and
  `/labeling/border-case` have existed since /clip-audit; the review grid that is meant to REPLACE
  that page simply had no way to reach them, so "unclear even to a human" had nowhere to go except
  a wrong tag or a Dismiss that means something else.
  - **Shared, not copied.** The read, the write and the stability policy moved into one hook,
    `lib/useBorderCases.ts`, with `components/BorderCaseButton.tsx` as the (purely presentational)
    control; /clip-audit's `TrainControl` now renders that same button instead of owning its own
    mutations, and its two hand-rolled `border-cases` queries are gone. That is the same lesson
    TrainControl itself records — a second labeling surface holding a byte-for-byte copy is how
    these two drifted apart before.
  - **Grid stability is the hook's job, not each page's.** Ids ACCUMULATE and only never-seen ones
    are ever requested, so a review — which changes the visible id list — can't blank every flag in
    the grid the way an id-list-keyed query would (PR #994's lesson, now enforced in one place).
    Toggles patch the store; nothing invalidates. Writes are optimistic and roll back from
    `onSettled`, never `onError`, so main.tsx's global "the write failed" toast still fires —
    rule #22's cache policy, same idiom as `pipelineCache`.
  - **Independent of the verdict, deliberately.** A border case is not a third review outcome: the
    tile keeps its Confirm/Dismiss buttons and its place in the grid, and the flag is offered on
    DISMISSED tiles too (which get no tag picker) — "I rejected the model's tag" and "I can't tell
    what it should be" are two different facts and the schema keeps them apart. Nothing
    auto-dismisses; if the operator wants both, that is two clicks.
  - **The data-quality consequence, made visible.** Gate 1 counts `image_training_examples` rows
    and a border case IS one, so flagging alone would have let a tag reach "150 confirmed" on
    images nobody could classify. `taxonomy_overview` now also returns `border_case_count` (the
    uncertain slice of that same total, one added LEFT JOIN — **no migration**), rendered as a
    brick "· N border" beside the bar. Deliberately NOT netted out of `confirmed_count`: whether
    those images train, validate, or get dropped is a W3 decision, and silently redefining the
    gate metric mid-program is not this query's call.
  - Tests: `useBorderCases.test.tsx` (5), `BorderCaseButton.test.tsx` (4), 4 more on the page,
    `TrainControl.test.tsx` rewritten around the split, plus a backend case pinning the
    confirmed/border overlap. Every behavioral guard was verified to go red under a mutation that
    removes it — which is how a sixth test was caught as vacuous (it "pinned" a monotonic-merge
    guard against a late read that the query key already makes unreachable) and deleted along with
    the wrong rationale in the comment.
  - Suites: `pytest -q` 4493 passed / 112 skipped, `vitest run` 631 passed (61 files),
    `tsc --noEmit` clean, `vite build` clean, `eslint` clean on every touched file.

- 2026-08-19 — **Labeling page: the small/large photo switch, shared with Browse** (operator
  request: "the same switch as we have on the browse page"). Taken literally — the control was
  private to `BrowseExperience.tsx`, so it moved to `components/ImageSizeToggle.tsx` and both pages
  now render the SAME component rather than a copy that can drift.
  - **Same mechanism, not just the same buttons.** Both grids express the choice as one
    `--*-min` custom property on the grid wrapper, flowed by `auto-fill minmax(min(var(…),100%),1fr)`,
    with the large value **exactly double** the small one. On the review grid that is
    `TILE_MIN` 14rem → 28rem: 14rem reproduces today's density (against the page's own
    `max-w-5xl` it flows to the same four columns the fixed `md:grid-cols-4` gave) and degrades
    to 3/2/1 on narrower windows instead of cramming four in; 28rem is two big tiles. The
    `aspect-[4/3]` frame is untouched, so the photo doubles while the fixed-rem buttons and tag
    picker below it do not.
  - **Its own persisted key** (`sreality.newDedupLabeling.imageLarge`), NOT Browse's — resizing
    tiles here must never reshape the listing cards there. Pinned by a test that asserts Browse's
    key stays untouched. The boolean-preference machinery moved out of `browseLayout.ts` (a
    Browse-specific module) into `lib/persistedFlag.ts`; `browseLayout` re-exports `readFlag` so
    its existing entry point and tests are unchanged.
  - Tests: first `ImageSizeToggle.test.tsx` (3 — pressed state, sets-a-value-not-toggles, the
    a11y group label each surface passes) plus 2 on the page (the grid's track floor changes and
    the choice survives a remount; Browse's key stays clean). Both page tests verified to go red
    when the flag is de-persisted or the grid ignores it.
  - Suites: `vitest run` 617 passed (59 files), `tsc --noEmit` clean, `eslint` clean (7 pre-existing
    warnings elsewhere, none in the touched files).
- 2026-08-10 — **Labeling page: click a tile to enlarge it** (operator request, pointing at
  /clip-audit as the model). Reuses the SHARED `ImageLightbox` — the one full-screen photo modal
  behind listing detail's gallery and /clip-audit — rather than a second one-off, so keyboard nav,
  the scroll lock and the badge treatment stay identical across the app. A four-up tile is too
  small to judge a room tag on; the modal's arrow keys then walk the rest of the grid without
  going back to it.
  - **The gallery is parallel to the TILES, not to the images**: a position is one proposal row,
    so two models' proposals on the same photo are two stops (exactly as they are two tiles) and
    each stop carries its own proposed tag. Tiles whose photo hasn't arrived yet are skipped, so
    the index handed to the modal is a position in that gallery and never in `proposals` — a
    pinned regression, since the two lists differ precisely while images are still streaming in.
  - **The modal must never contradict the tile it was opened from.** The grid's default view
    badges the *proposed* tag, which `images_public` knows nothing about, so `ImageLightbox` gained
    an optional `tagAt(index)` override; on "Original tag" it is omitted and the lightbox's own
    default (the image row's CLIP call) is already right. Deliberately an override rather than
    synthesising an `ImagePublic` with the label swapped in — a row must not claim CLIP predicted
    something it didn't.
  - Also in the shared component: the position is now **clamped** to the array. The grid behind can
    shrink (a reviewed tile leaving its tab), and an out-of-range index rendered nothing at all
    while the dialog kept holding the page's scroll lock. First real `ImageLightbox.test.tsx`
    (5 tests) pins that plus nav/Escape/badge; both new behaviours were verified to go red when
    reverted.
  - Housekeeping: `rowKey`/`draftKey` were the same string built two ways — now one module-scope
    helper (also what lets the gallery memo drop an `exhaustive-deps` suppression). `cursor-zoom-in`
    on all three thumbnail surfaces that open the modal, so the affordance reads the same app-wide.
  - Suites: `vitest run` 513 passed (48 files), `tsc --noEmit` + `eslint` clean.
- 2026-08-07 (part 2) — **Labeling page review ergonomics** (operator request, four edits): a
  collapsible taxonomy chart, filtering the grid by tag + filtering the tag list by how much
  training data it already has, an **All** tab alongside pending/confirmed/dismissed, and — the
  substantive one — **the grid no longer churns when you review a tile**.
  - **The churn had two independent causes**, both fixed:
    1. `ORDER BY proposed_at DESC` had no tiebreaker, and the backfill inserts a whole batch in
       one transaction — so every row in it carries the *same* `now()` and Postgres was free to
       return ties in a different order on each call. The grid genuinely reshuffled on any
       refetch. Every list query is now totally ordered (`, image_id DESC`).
    2. Reviewing one proposal invalidated the whole proposals query AND the image query was keyed
       on the current id list, so a confirm swapped in an empty cache entry and every tile lost
       its photo at once. Single-tile actions now **patch the cached list in place** (drop the row
       on Pending, patch it in place elsewhere) and photos accumulate in a page-level id→image map
       that only ever fetches ids it has never seen. Other tabs are invalidated, never the visible
       one. Five tests pin this; all five were verified to go red when the old invalidate-and-
       refetch behavior is restored.
  - **The All tab** (`status='all'`) is the union of the other three — every `label_proposals` row
    plus training examples that never had a proposal, as synthetic `model='manual'` rows, same as
    the Confirmed tab already did. A new `trained_label` on every row (the image's *current*
    `image_training_examples` label, or NULL) is what lets the page grey already-handled tiles
    without a second query; reviewing a tile there greys it **in place**, nothing moves.
    `status` is now validated server-side — an unknown value 422s instead of silently listing
    everything while the tab claims to be filtered. **No migration**: both new reads are plain
    queries over existing tables.
  - **The coverage ceiling** ("≤ N training images") narrows the chart *and* the grid's tag
    select — the Gate-1 question is "which tags are still short" — but deliberately NOT the
    per-tile correction picker, which must always offer the whole vocabulary. The currently
    filtered tag is never hidden by the ceiling.
  - Suites: `pytest -q` 2690 passed / 32 skipped, `vitest run` 503 passed, `tsc --noEmit` clean.
- 2026-08-07 — **Labeling page: correct a wrong suggestion instead of only accept/reject it**
  (operator request, pointing at /clip-audit's combobox as the model). Every non-dismissed tile now
  carries a `LabelCombobox` seeded from the proposal's tag; Confirm writes whatever is in it.
  Already-confirmed tiles get "Save tag", which relabels in place through the EXISTING
  `/labeling/training-example` endpoint (the same one /clip-audit's Train CTA uses) rather than
  re-running the confirm flow.
  - **Backend**: `confirm_proposal(..., label=None)`. The override lands in
    `image_training_examples`; `dedup_sim.label_proposals.label` deliberately keeps the model's own
    prediction, so "model said X, operator said Y" stays derivable by comparing the two tables —
    **no migration needed**, which is why none was written.
  - **A freehand correction also registers itself in `dedup_sim.taxonomy_labels`** (same
    transaction, `ON CONFLICT (label) DO NOTHING`). This is load-bearing, not tidiness: the coverage
    chart, the tag picker's options, AND `scripts/label_proposal_backfill.py`'s class list all read
    the taxonomy table, not the training set — so an unregistered label would be invisible in the
    chart, never re-offered for the next image, and impossible for the secondary encoder to ever
    propose. Migration 379 backfilled exactly this class of gap once already; this closes the door
    that would have reopened it.
  - **Adversarial review before merge** (3 dimensions — backend, frontend, UX/regression — each
    finding independently re-verified): 15 candidates, 9 confirmed, deduplicating to 5 real issues,
    all fixed. Two were things neither the type-checker nor jsdom tests could have caught:
    1. *(high)* The picker sat directly ABOVE the Confirm/Dismiss row, so its downward-opening
       absolutely-positioned dropdown painted over those buttons and would have swallowed the first
       click aimed at Confirm — committing whichever taxonomy option happened to sit at that
       y-offset. Both pre-existing usages avoid this by laying picker and action side-by-side; this
       was the first vertical layout. Fixed by moving the picker BELOW the action row.
    2. *(high)* Off-taxonomy labels silently dead-ending (the registration fix above).
    3. *(high)* The page-level `drafts` map was keyed by `image_id` alone while proposals are keyed
       `(image_id, model)` — two models' proposals for one image shared a draft slot, so correcting
       one tile rewrote the other's tag. Re-keyed to `${image_id}:${model}`.
    4. *(high, found while the review ran)* "Confirm selected" ignores per-tile corrections (the
       bulk endpoint takes ids only), so a corrected-AND-selected tile would have had its fix
       silently overwritten by the model's label. A corrected tile now drops out of the selection.
    5. *(medium)* The client always sent its draft, even untouched — a page whose proposal list
       predated a taxonomy rename would resurrect the retired spelling. The label now travels only
       when it is an actual correction; otherwise the server uses its own stored value.
  - I'd also independently caught and fixed a clipping bug before the review: the tile card's
    `overflow-hidden` (there to round the photo) would have clipped the dropdown to nothing. Moved
    it to the inner photo wrapper and pinned it with a structural test that walks the listbox's
    ancestors — verified the test genuinely fails when the class is put back, so it isn't vacuous.
  - Suites: `pytest -q` 2683, `vitest run` 494, `tsc --noEmit` clean, `vite build` clean, both
    codegen checks fresh. No migration in this PR.
- 2026-08-06 (session continuation, part 7) — Fixed a gap in the just-shipped Labeling page
  (operator report: taxonomy showed "0 labels, 0 sampled" and the Confirmed tab was empty despite
  an existing 48-label / ~1,185-image training set). Root cause: `dedup_sim.taxonomy_labels`
  opened empty — nothing ever seeded it from `image_training_examples`'
  pre-existing labels, even though `taxonomy_overview`'s confirmed_count already LEFT JOINs on
  label text (it just had nothing on the left side to join against) and PROGRAM.md's own
  Taxonomy v1 definition (line 38) already points at that table. **Migration 379** backfills
  `dedup_sim.taxonomy_labels` with the 48 distinct `image_training_examples` labels
  (`on conflict (label) do nothing`, one-time, not an ongoing sync) — applied live, confirmed
  48 rows. Separately, `list_proposals(status='confirmed')` in `toolkit/dedup_sim_labeling.py`
  now drives FROM `image_training_examples` (LEFT JOINing the most-recently-confirmed
  `label_proposals` row per image, via `DISTINCT ON`, for display provenance only) instead of
  querying `label_proposals` alone — the pre-existing 1,185 images were trained via
  `/phash-audit`'s Train CTA, never through a proposal, so the Confirmed tab was only ever
  going to show this page's own review actions without it. First cut used a UNION keyed off
  `label_proposals.label`; adversarial review caught that `/phash-audit`'s Train CTA can still
  relabel an image AFTER it's confirmed here (it only ever writes `image_training_examples`,
  never touches `label_proposals`), which would have shown a stale label — switched to always
  reading `te.label` live instead. Verified live: returns exactly 1,185 rows, one per image, all
  `model='manual'` (no secondary-CLIP proposals exist yet). Tests added in
  `tests/toolkit/test_dedup_sim_labeling.py` (no-duplicate-when-both-exist, label filter,
  manual-model tagging, stale-label-after-relabel regression).

- 2026-08-06 (session continuation, part 6 — parallel to the RunPod session below) — Built the
  Labeling page (W1's last unstarted mechanic besides the Dashboard skeleton, which stays a
  placeholder — genuinely no data until W2+).
  - **Migration 373** (`dedup_sim_labeling`) applied live via MCP: `dedup_sim.taxonomy_labels`
    (the operator-curated Taxonomy v1 vocabulary — free text, add/rename/remove; deliberately
    NOT pre-seeded with the ledger's "49 labels" description, since PROGRAM.md itself flags that
    exact list as unfinalized and operator-owned), `dedup_sim.labeling_sample` (which images are
    in scope for the relabel job), `dedup_sim.label_proposals` (one row per (image, model): what
    the secondary CLIP proposes, `pending`/`confirmed`/`dismissed`). All three backend-only (no
    `_public` view), matching migration 372's settings/simulation_runs precedent. Every join/
    upsert/cascade statement verified live via `EXPLAIN` against the real schema before landing.
  - **`toolkit/dedup_sim_labeling.py`**: taxonomy CRUD (add; rename cascades to every
    `image_training_examples` + `label_proposals` row under the old text in one transaction;
    remove purges both, images untouched — mirrors `api/labeling.py`'s
    `delete_training_label` "images stay" semantics), `grow_sample` (newest not-yet-sampled
    images, optional category filter), proposal review (`list_proposals`,
    `confirm_proposal` — upserts into `image_training_examples`, the ONLY path that promotes a
    sim-side proposal into the real confirmed store, never `image_clip_tags` — gallery-flip
    hazard — `dismiss_proposal`, plus `bulk_confirm_proposals`/`bulk_dismiss_proposals` for the
    review queue's batch action). 40 hermetic tests (hand-rolled in-memory SQL dispatcher, since
    the queries are multi-table joins/cascades the simple key-value fake `dedup_sim_settings`
    tests use couldn't model).
  - **3 new registry settings** (`toolkit/dedup_sim_settings.py`, new `Category.LABELING`):
    `labeling_secondary_model` (text, default `openai/clip-vit-large-patch14`, decided=False —
    a starting pick pending calibration), `labeling_target_proposals_per_category` (300) and
    `labeling_gate1_target_per_tag` (150) — both `decided=True` since PROGRAM.md's own text
    states these numbers verbatim (the 300-proposal sample-widening target and the Gate 1
    criterion), not invented here.
  - **Secondary CLIP encoder — new, separate infra from the DINOv2/RunPod embeddings path**:
    `scraper/label_proposal_tagger.py` (a self-contained `ProposalTagger`, deliberately NOT a
    change to the production `scraper/clip_tagger.py` — zero risk to the live gallery tagger —
    zero-shot against whatever labels are currently active in `taxonomy_labels`, simple "a photo
    of {label}" prompts, no fine/logical collapse layer since proposals are flat single-label).
    `scripts/label_proposal_backfill.py` mirrors `clip_tag_backfill.py`'s shape (R2 download,
    sharded, chunked) but selects from `labeling_sample` minus already-proposed-for-this-model
    images. `.github/workflows/label_proposal_backfill.yml`, dispatch-only (2-way shard, smaller
    than the production 4-way — this is a curated sample, not the full corpus), needs the same
    R2 secrets as `clip_tag.yml`. No RunPod involved — this is a bigger CLIP checkpoint on CPU,
    unrelated to Wave 5's DINOv2-on-RunPod plan; don't conflate the two "secondary encoder"
    mentions in the program.
  - **API**: `api/new_dedup_labeling.py` (new file, mirrors the existing `api/labeling.py` vs
    `api/property_merge.py` one-file-per-concern split rather than growing
    `api/routes/new_dedup.py`), mounted at `/new-dedup/labeling/*`, admin-gated. 18 hermetic
    route tests (toolkit functions monkeypatched, so this layer only proves status codes +
    error-mapping, not SQL — that's the toolkit test file's job).
  - **Frontend**: `frontend/src/pages/NewDedupLabeling.tsx` + nav/route wiring
    (`routes.tsx`/`Shell.tsx`, third NEW DEDUP item after Dashboard/Settings) + ~150 lines of new
    `api.ts` functions/types. Structure: a Taxonomy v1 coverage strip (per-label
    confirmed/pending/dismissed counts + a Gate-1 progress bar, inline rename, two-step-confirm
    remove, add-label form) above a sample-management panel (size + grow-by-N-images form) above
    the proposal review grid (status tabs, a "New tag"/"Original tag" toggle that swaps the
    `ImageTagBadge` between the proposal's label and the image's live `clip_fine_tag` for visual
    comparison, per-tile confirm/dismiss, and a batch select-all + bulk confirm/dismiss bar
    scoped to the CURRENT `labeling_secondary_model` — an older model's leftover pending rows
    review one at a time only). Investigated first via a 4-way parallel research pass (ClipAudit's
    full structure — turned out to have **no dedup-pair UI to subtract**, the whole file is
    already single-image labeling; the `/labeling/*` schema family; the CLIP pipeline + taxonomy
    landscape — confirmed the "49 labels" vocabulary exists nowhere in code, design-doc-only;
    the `NewDedupSettings.tsx` wiring pattern) before writing any page code, so the page reuses
    established components (`FilterChip`-style toggles, `Tabs`, `ImageTagBadge`, the
    `fetchImagesByImageIds`/`imageSrc` Supabase-read pattern) rather than reinventing them. 10
    new vitest tests. `tsc --noEmit` clean, production `vite build` clean.
  - Full suite green: `pytest -q` 2612 passed (up from 2568 at session start); `vitest run` 390
    passed; `tsc --noEmit` clean; both codegen checks OK (`generate_workflow_docs.py` needed a
    regen for the new GH Actions file, `generate_filter_registry.py` already matched).
  - Not done, deliberately: the operator still has to run several `grow_sample` +
    `label_proposal_backfill.yml` + review rounds to actually reach Gate 1 (150 confirmed images
    per active tag) — this session shipped the tool, not the labeling itself. Dashboard skeleton
    stays a placeholder (same "no data yet" reasoning as prior sessions). RunPod is unrelated to
    this work (Wave 5 only). Not built: a way to deactivate a taxonomy label without hard-deleting
    it (`taxonomy_labels.active` is read by the backfill's label selector but nothing ever sets it
    false — the only lever today is the destructive DELETE, which cascades away confirmed training
    examples). Flagged by the review pass below as a real but low-severity gap; a follow-up PR if
    the operator hits it in practice, not addressed now to avoid unbounded scope growth.
  - **Adversarial review before merge** (5-dimension parallel pass — backend correctness, security/
    migration, the CLIP pipeline, frontend correctness, test quality — each finding independently
    re-verified by a second agent against the actual code): 16 findings, all 16 confirmed real
    on verification, all fixed same-session:
    - **High**: `confirm_proposal`/`dismiss_proposal` had no `status = 'pending'` guard (unlike
      their bulk siblings) — a stale/retried dismiss after a confirm would flip
      `label_proposals.status` without ever retracting the `image_training_examples` row the
      confirm had already written, silently diverging the two stores. Fixed: both now require
      `status = 'pending'` and 404 otherwise, exactly like the bulk functions.
    - **High**: the Labeling page's rename handler switched the active proposals filter to
      *whatever label was just renamed* rather than checking it was the SAME label being
      filtered — renaming an unrelated taxonomy row silently hijacked the operator's filter.
      Fixed by threading the pre-rename label text through the mutation and comparing it, not
      just checking "is some filter active".
    - **High**: the new `label_proposal_backfill.yml` GH Actions workflow interpolated
      `workflow_dispatch` string inputs directly into the shell `run:` block via `${{ }}` — a
      classic GH Actions script-injection surface (a `"` in the input breaks out of the quoted
      arg string), reachable by anyone who can dispatch the workflow, with every R2/DB secret in
      scope. Fixed: inputs now pass through `env:` and are referenced as quoted shell variables.
    - **Medium**: `grow_sample`'s SQL used a plain `JOIN` from `listings` to `properties`, so an
      image whose listing hasn't had a `properties` row attached yet (rule #19/#20: new rows land
      `property_id` NULL until the incremental maintenance cron runs) was silently excluded even
      with no category filter — contradicting the "newest not-yet-sampled images" contract. Fixed
      with a `LEFT JOIN`.
    - **Medium**: the secondary-CLIP tagger's confidence is a softmax over the active taxonomy
      labels — mathematically always exactly 1.0 when only one label is active (the realistic
      bootstrap state right after the operator adds their first label), making the confidence
      column meaningless exactly when an operator might lean on it most to triage a bulk-confirm.
      Fixed: falls back to raw cosine similarity (not softmax-normalized) when there's only one
      active label.
    - **Medium**: the page's confirm/dismiss mutations were one shared `useMutation` instance for
      the whole grid — TanStack Query's observer only reflects the most-recently-clicked tile's
      `isPending`/`variables`, so clicking Confirm on a second tile made an earlier still-in-flight
      tile's buttons visually re-enable, opening a real race (confirm and dismiss in flight for the
      same proposal at once). Fixed with a local per-image-id pending set, independent of which
      mutation call is "current".
    - **Medium** (×4, test-quality): `PUT /taxonomy/{id}`'s 422 path, `POST /proposals/dismiss`'s
      404 path, and `POST /proposals/bulk-dismiss`'s 422 path were untested at the route level
      (asymmetric with their tested siblings); the page's mutation tests only asserted API-call
      args, never that `invalidateQueries` actually fired or that the UI reflected it. All four
      closed — the three route tests added, and the confirm/dismiss/bulk-confirm page tests now
      also assert the proposals grid actually empties after the refetch.
    - **Low** (×3): the fake-conn test double for `INSERT ... ON CONFLICT DO UPDATE SET label,
      updated_at` was overwriting `created_by` on every call, diverging from Postgres' real
      partial-column update (fixed, + a regression test); `scripts/label_proposal_backfill.py` had
      zero tests unlike its production analogue `clip_tag_backfill.py` (added
      `tests/scripts/test_label_proposal_backfill.py`, same shape as the existing
      `test_clip_tag_backfill.py`); the GH workflow's cache-key comment claimed "a model swap just
      costs one cold cache fill", which is false (the static key never varies with the
      operator-tunable `labeling_secondary_model` setting, so `actions/cache`'s immutable-key
      behavior means a swap re-downloads on *every* run, forever) — comment corrected to state the
      actual (accepted, CI-cost-only) behavior rather than engineer a fully dynamic cache key for a
      low-severity, non-correctness issue.
    - Full re-run after all fixes: `pytest -q` 2626 passed (up from 2612), `vitest run` 392 passed
      (up from 390), `tsc --noEmit` clean, `vite build` clean, both codegen checks OK.
  - Next session: once the operator starts labeling rounds, watch for real usage friction (is the
    batch-review flow fast enough, does the coverage strip's progress read clearly); revisit W0's
    PR-3 (still the only blocker on Gate 0) if it hasn't landed by then; consider a non-destructive
    "deactivate a taxonomy label" affordance if the hard-delete-only gap above turns out to matter
    in practice.
- 2026-08-06 (session continuation, part 5) — **W1's RunPod deliverable closes out.** After
  #982's redesigned pass criteria landed, re-dispatched once more
  ([31085275059](https://github.com/waiff/sreality/actions/runs/31085275059)): pod `ci87ta3vltru6l`
  launched on the RTX 3070 fallback (RTX A2000 again had no capacity), ran for 126s, was cleanly
  terminated — **SMOKE TEST PASSED**, workflow run status `success`, estimated cost **~$0.0046**.
  `desiredStatus` stayed `RUNNING` and the logs endpoint still 400'd, exactly as the part-4 entry
  predicted — no longer treated as failures. Total real spend across all 5 live dispatches this
  session: ~2.2¢. The RunPod client, its capacity/pricing edge cases, and the cost-safety
  guarantee are now proven against real infrastructure end-to-end; nothing further needed here
  until Wave 5 builds the real embedding batch job on top of `RunPodClient.run_job`.
- 2026-08-06 (session continuation, part 4) — Operator added RunPod account funds. Re-dispatch
  ([31083936844](https://github.com/waiff/sreality/actions/runs/31083936844)) got past the
  account-balance blocker and, for the first time, **actually launched a real pod**: `g39f02wj642her`
  on an `NVIDIA GeForce RTX 3070` (\$0.13/hr, after the cheaper RTX A2000 again had no capacity —
  the fallback from #977 worked as designed). Real GPU-hours were billed (~482s ≈ 1.7¢) and the
  pod was cleanly terminated by the client's `finally` guarantee. **Two real API limitations
  surfaced that no amount of pre-reading the docs caught, only actually running it did:**
  - `desiredStatus` never left `RUNNING` for the entire wait window, even though the smoke
    test's own command should finish in well under a minute. On-demand Pods appear to track pod
    (rental) lifecycle, not the inner container process — there's no evidence they self-report
    "my command finished," unlike RunPod's separate Serverless product.
  - The documented SSE logs endpoint (`GET /pods/{id}/logs`) returned a bare 400 with no
    pod-specific detail — RunPod's REST API doesn't appear to actually expose Pod log retrieval
    yet (a Feb-2025 GitHub feature request corroborates this), despite docs suggesting otherwise.
  - **Neither is a code bug** — `run_job`'s `finally`-terminate held regardless, so the pod was
    still torn down correctly. Fixed the *expectations*, not a defect: `wait_for_exit`/
    `fetch_logs` docstrings now say plainly not to rely on either signal; the smoke test's pass
    criteria dropped the "success marker in logs" requirement (structurally unreliable) in favor
    of "a real pod launched on a real GPU, accrued measurable cost, and was torn down cleanly" —
    which IS what actually matters for Wave 1's "prove the pipeline works" goal. Wait window cut
    480s → 120s (no point paying to wait for a status flip that isn't coming).
  - **Design note for whoever builds Wave 5's real embedding batch job:** don't rely on RunPod
    Pod status or logs for "is it done" / "what did it produce" — have the job write its result
    directly to Postgres or R2 (it'll have network access + credentials via env) and have the
    orchestrator poll THAT for completion instead.
  - Also fixed pre-existing (unrelated, landed via #971) `filterRegistry.generated.ts` codegen
    staleness that was blocking this PR's `build` check — bundled since it had to be regenerated
    to get CI green, not touched otherwise.
  - **W1's RunPod deliverable is now genuinely done**: client built, 23 hermetic tests, and the
    core guarantee (launch on a real GPU, bill real time, always tear down) verified against
    real infrastructure across 4 live dispatches. Log/status-based completion detection is
    explicitly NOT solved and explicitly deferred to Wave 5's real design (see note above).
- 2026-08-06 (session continuation, part 3) — Operator confirmed `RUNPOD_API_KEY` was added as
  a repo secret; asked to finish the RunPod piece (Labeling page picked up by a different
  session in parallel). Built `scripts/runpod_client.py` (#972) + a `new_dedup_runpod_smoke_test`
  workflow_dispatch to prove it end-to-end. Three real live dispatches, two real bugs found and
  fixed by actually running it rather than trusting the code on paper — **zero cost incurred
  across all three**, since every failure happened before RunPod ever started billing (the
  point of `run_job`'s launch-outside-try / terminate-in-finally split):
  1. Run [31081790027](https://github.com/waiff/sreality/actions/runs/31081790027) — `cheapest_gpu()`'s
     `communityPrice is not None` filter let through a placeholder catalog entry (id "unknown",
     price 0), which always "won" as cheapest and got rejected by RunPod's pod API (400, not a
     valid `gpuTypeIds` value). Fixed in #975: require `communityPrice > 0`.
  2. Run [31082278388](https://github.com/waiff/sreality/actions/runs/31082278388) — the
     (correctly-picked, this time) cheapest real GPU, RTX A2000, had zero free community-cloud
     instances at that moment — a live availability condition, not a bug (RunPod's community
     cloud is peer-hosted). Fixed in #977: `NoCapacityError` + `run_job_with_fallback`, which
     tries `eligible_gpus()` in ascending price order and only advances past a genuine
     capacity 500, not any other failure.
  3. Run [31083022260](https://github.com/waiff/sreality/actions/runs/31083022260) — the
     fallback logic worked exactly as designed (RTX A2000 → no capacity → tried RTX 3070 next),
     but that attempt hit `"Your account balance is too low to rent a pod. Please add funds to
     your account."` **This is the actual current blocker, and it's on the operator's side, not
     code**: a payment method on file isn't the same as an available RunPod balance. Needs the
     operator to add funds/credit in the RunPod dashboard (Billing) before any pod can launch.
  - Minor known cosmetic gap (not fixed, not worth its own PR): the smoke-test driver's
    top-level error message ("job failed on every eligible GPU type") is misleading for case 3
    — the fallback didn't actually exhaust every option, it stopped correctly on a non-capacity
    error after trying 2. Functionally correct (it did NOT keep retrying), just an imprecise
    log line; worth tightening whenever this file is touched again for the real end-to-end run.
  - Next session (once funds are added): re-dispatch `new_dedup_runpod_smoke_test.yml` from
    main, confirm a pod actually boots + runs the CUDA op + reports `SMOKE_TEST_OK` + terminates,
    note the real elapsed time / cost in this ledger. That closes out W1's RunPod deliverable.
- 2026-08-06 (session continuation, part 2) — With PR #965 merged and PR-2's minimal nav
  placeholder confirmed live, continued W1: made the Settings page real.
  - **Backend**: `api/routes/new_dedup.py` — `GET /new-dedup/settings` (full registry +
    effective values in one call), `PUT /new-dedup/settings/{key}` (validated write, 400 on a
    bad type/range/enum, 404 on an unknown key), `DELETE /new-dedup/settings/{key}` (drop the
    override, revert to the registry default). Admin-gated (`require_admin`), mounted in
    `api/main.py` alongside the other split-out routers. 7 new hermetic tests
    (`tests/api/test_new_dedup_routes.py`), including the 401-without-admin gate.
  - **Frontend**: `frontend/src/pages/NewDedupSettings.tsx` replaces PR-2's placeholder — all 12
    registry settings grouped by category (L0-L4 + general), each card showing the
    plain-language explanation, a `not yet calibrated` tag (ochre — this app's existing
    low-confidence/pending semantic, e.g. `EstimationList`'s confidence badge,
    `BuildingDetail`'s `awaiting_input` status) for the two settings the ledger flagged as
    calibration-pending, and an `edited` tag + "reset to default" once overridden. Controls are
    type-aware: a toggle switch for the boolean (mirrors `Settings.tsx`'s `FilterCell`), a
    number input with min/max + explicit Save once dirty, a native `<select>` for the two
    family-semantics enums (native select is this codebase's established pattern —
    `Watchdog.tsx`/`ListingMap.tsx`/others — not the interface-design skill's generic custom-
    dropdown guidance, since this is one page inside an already-coherent app, not a new
    product). Reused `frontend/src/lib/api.ts`'s existing `request()` + react-query mutation
    pattern (`AppSettingRow`'s shape) rather than inventing a new one. 6 new vitest tests.
  - Verified against the app's EXISTING design tokens/components (no `.interface-design/
    system.md` in this repo; treated `Settings.tsx`'s established patterns as the de facto
    system rather than running a fresh domain-exploration pass, since this is one page inside
    an existing coherent product, not a new one).
  - Full suite green: `pytest -q` 2568 passed; `tsc --noEmit` clean; `vitest run` 380 passed;
    both codegen checks (`generate_filter_registry.py`, `generate_workflow_docs.py`) OK.
  - Not done: the Dashboard skeleton (funnel + cost table) — genuinely has no data to show yet
    (candidates/evidence/decisions are W2/W4/W5); building more than PR-2's placeholder there
    now would be speculative. RunPod serverless workflow — still no `RUNPOD_API_KEY` secret.
- 2026-08-06 (session continuation) — Operator confirmed the freeze + PRs were landing and asked
  to check back in 5-10 minutes. Re-verified live state after the wait: **Day-0 freeze and M-0
  are both actually done now** (`dedup_publication_gate_enabled=false`,
  `realtime_dedup_interval_seconds=0`; the 6 legacy workflow IDs from the entry below no longer
  resolve via the GH API at all — PR-1 deleted the files outright rather than merely disabling
  them, which supersedes "disabled"). **PR-1 (#966) and PR-2 (#967) are both merged**
  (2026-08-06T05:12/05:14), landed by a parallel session that also finished the CUTOFF §6 doc
  pass this ledger flagged as missing (verified: PR-1's diff touches CLAUDE.md,
  architecture.md, both skills, and the legacy design-doc deletes). That session's own ledger
  entry (immediately below) landed as #968 before PR-1/PR-2 merged, then PR-1/PR-2 landed later
  once M-0 was applied — the entry order below is chronological-as-written, not
  chronological-as-true; treat this entry as the current source of truth. **PR-3 (teardown
  migration: table drops + view redefinition + legacy generation stamp) has NOT landed** — the
  only piece left before Gate 0's post-teardown verification checklist can go green.
  - Merged `origin/main` into this session's W1 branch (`feature/new-dedup-w1-foundation`,
    PR #965): one real conflict, in `ROADMAP.md`'s NEW DEDUP row (both this branch and #968 had
    edited it independently) — resolved by combining both: legacy-removed status from #968's
    wording + the W0+W1-parallel note from this branch's wording. `docs/design/new-dedup/
    PROGRAM.md` merged clean (this entry's insertion point didn't overlap #968's rewritten
    entry). Full suite re-run green post-merge; pushing and merging #965 now that CI is clean.
  - PR-2 already shipped "a minimal NEW DEDUP nav placeholder (Dashboard + Settings stub
    pages)" per its own description — checking that before building W1's dashboard skeleton
    further, to build ON it rather than duplicate it.
  - Continuing W1: with PR-2 landed, the dashboard skeleton + Labeling page are now unblocked
    (the nav-territory reason for holding them is gone).
- 2026-08-06 (later same day) — Operator asked whether to start W1 in parallel with the still-
  in-progress W0. Verified LIVE state instead of trusting the entry below (it undersold how far
  W0 had actually gotten in places, overstated it in one):
  - Day-0 freeze and M-0 both **still not done** — confirmed live (all 6 legacy decision
    workflows still `active` on GitHub; `dedup_publication_gate_enabled=true`;
    `realtime_dedup_interval_seconds=90`). Attempted both again (`gh api .../disable` on all 6
    workflow IDs, the two `app_settings` UPDATEs via Supabase MCP) — **blocked by the permission
    classifier again**, same as last session. Needs the operator to run these two directly or
    grant permission; exact commands were relayed to the operator in-chat this session.
  - pg_dump backup (CUTOFF.md §4) — **actually done**, contrary to the "not yet started" note
    below: workflow run `31052835193` succeeded 2026-08-05T22:27:48Z, after the pg_dump-17 fix
    (#964) landed.
  - **PR-1 (backend removal) branch — further along than this ledger said, but not opened.** The
    worktree (`feature/new-dedup-backend-removal`) is correctly based on PR-0's merge commit
    (`9d1eb177`, verified via `git merge-base` against `origin/main` — first miscalculated this
    against a stale *local* `main` ref, caught before reporting it, see
    [[worktree-absolute-path-stale-branch-hazard]]). Diff vs `origin/main`: 77 files,
    +179/-20,776, matching CUTOFF §1/§2 exactly. The ~8 decision-side test file deletes/edits and
    the S5 split (`api/property_merge.py` + `api/labeling.py`, wired into `api/main.py`) are done
    and staged. **Missing entirely: CUTOFF §6's doc pass** (CLAUDE.md rule 15, architecture.md
    §15, the `scraper-ops`/`llm-pipelines` skills, deleting the ~7 legacy design docs) — per
    CLAUDE.md's own same-PR-doc-update rule this needs to land before PR-1 opens for real review.
    Not pushed/opened this session (would need the doc pass first); left as-is for a future
    session or the operator's own continuation.
  - **W1 backend slice started** (operator chose this over finishing W0's blockers, in a fresh
    worktree/branch `feature/new-dedup-w1-foundation` off current `origin/main`, unrelated to the
    PR-1 branch above):
    - **Migration 372** (`dedup_sim_foundation`) applied live via MCP: schema `dedup_sim`
      (droppable wholesale, Wave 8) with `settings` + `settings_history` (override-only,
      mirrors `filter_registry.py` + `filter_visibility` migration 059 — a missing row means
      "use the code registry's default," so a later wave's new setting needs no migration) and
      `simulation_runs` (decision-tier run bookkeeping, mirrors `estimation_runs` shape,
      migration 010). Evidence-tier tables (candidates, pHash/embedding evidence) are NOT part
      of this migration — each lands with the wave that needs it (W2/W4/W5).
    - **`toolkit/dedup_sim_settings.py`**: the registry half — 12 `SettingDef`s, one per value
      PROGRAM.md's 2026-08-05 Q&A ledger already decided (L0 radius/floor/area tolerances, pHash
      threshold + family-semantics toggle, embeddings threshold + family-semantics toggle,
      RunPod cost cap, vision model + manual-batch-only toggle, L1 exact-attrs OFF), each with a
      plain-language `explanation` (mission non-negotiable) and a `decided` flag (false on the
      two the ledger flagged as starting points pending later-wave calibration: embeddings
      threshold, L1 enabled). No new thresholds invented — every default traces to the ledger.
      `effective_value`/`effective_settings`/`update_setting`/`reset_setting` CRUD, validated
      against each setting's declared type/range/enum. 20 hermetic tests, all passing.
    - **Fixed a latent CI gap** this migration exposed: `tests/test_migration_rls_grants.py`'s
      table-name regexes assumed every table lives in `public` (or unqualified) — `dedup_sim.*`
      broke both the created-table capture (truncated to just `dedup_sim`) and the
      RLS-enabled-table capture (schema-qualified name didn't match `\s+enable row level
      security` right after the truncated capture), producing a false "table dedup_sim never
      gets RLS" failure despite RLS being correctly enabled on all 3 real tables. Fixed the
      regex to handle a non-`public` schema prefix generically; full suite green (3089 passed).
    - **RunPod serverless workflow — not built, and deliberately so.** No `RUNPOD_API_KEY` (or
      similarly named) secret exists in GH Actions yet — the operator's RunPod account (W1's
      "(operator)" half) hasn't been created. Building pod-orchestration code now, with nothing
      real to call it against (candidate-scoped embedding computation is Wave 5) and no way to
      test it, would be exactly the kind of speculative scaffolding CLAUDE.md's conventions warn
      against. Reuse target once the account exists: PR #804's `scripts/embedding_gpu_bench.py`
      (`download_images`/`embed_images` pattern, presigned-URL manifest, no repo imports) — that
      harness is a bake-off tool (reads the legacy `dedup_label_events` golden set, which
      CUTOFF.md §4 drops), so its DATA SOURCE isn't reusable, but its POD-SIDE MECHANICS are.
    - **Dashboard skeleton + Labeling page — deliberately held**, not started: both sit in the
      same nav territory the ledger already decided to defer to ride with PR-2 (avoids building
      UI that PR-2's frontend removal immediately restructures); same reasoning extends from the
      dashboard (explicitly deferred below) to the Labeling page (not explicitly said before, but
      identical logic).
  - Next session: get the operator to unblock Day-0 freeze + M-0 (or grant permission), finish
    PR-1's CUTOFF §6 doc pass and open it as a draft PR, then continue W1 (candidate-store-
    adjacent settings will grow the registry once W2 starts; RunPod once the account exists).
- 2026-08-06 — W0 execution started (operator kickoff: "start with the dedup workflow refactor
  based on program.md and cutoff.md" = Gate 0 approval). Done this session:
  - **PR-0 merged** (#960, commit `9d1eb177`) — clip-linear-probe.md + this PROGRAM.md/CUTOFF.md
    pair landed on `main`. Note: the branch CUTOFF.md's §7 step 1 named
    (`feature/clip-audit-tag-management`) had already merged its own content separately via #954
    before this session started; PR-0 was re-cut as a fresh docs-only branch off current `main`.
  - **Backup cut**: branch `backup/pre-new-dedup-2026-08` + tag `backup-pre-new-dedup`, both at
    `9d1eb177` (post-PR-0 main). Supabase DB confirmed live and unmodified at
    **2026-08-05 21:55:22 UTC** (`select now()` reading taken before any Wave 0 DB write was
    attempted) — use this as the PITR reference point.
  - **Scaffolding merged** (#961) — `roadmap/new-dedup.md` track created, `ROADMAP.md` index
    updated, old `roadmap/dedup-track.md` marked superseded.
  - **pg_dump-to-R2 backup done and verified** (#963, fix #964) — a `workflow_dispatch`-only GH
    Actions job (`scripts/backup_new_dedup_teardown_tables.py`) dumped all 9 CUTOFF.md §4
    "Drop"-list tables/matviews to R2 under `backups/new-dedup-teardown/2026-08-05/`. First run
    failed on every table (`pg_dump` 16 vs. server 17.6 — "aborting because of server version
    mismatch"); fixed by pulling `postgresql-client-17` from the PGDG apt repo; re-run succeeded
    9/9 (largest: `property_identity_candidates`, 4.4 MB gzipped / ~158k rows). Satisfies the
    destructive-migration safety net ahead of PR-3.
  - **PR-1 (backend removal) — done, draft, CI green, mergeable**: [#966](https://github.com/waiff/sreality/pull/966),
    branch `feature/new-dedup-backend-removal`. Ran as a 12-stage background workflow covering
    CUTOFF.md §1 (C1/C2/C3/C6/C7 + C5 code-only + C4 workflow-YAML), §2 (wholesale deletes +
    S3/S5/S6 splits), §6 (backend docs/tests) — 120 files, +1,441/−28,772, 2,541 tests passing.
    Its own verification pass flagged one real blocker pre-fix-up (stale
    `workflowDocs.generated.ts`, since fixed) plus several non-blocking gaps (stale comments
    citing the removed engine, a few dead links to deleted design docs, one pre-existing dead
    threshold in `verify_pipeline.py` — none are regressions or build-affecting, left as
    follow-up cleanup rather than another fix round). Picked up a real merge conflict against
    `main` after the scaffolding/backup PRs landed (ROADMAP.md's Dedup row, `roadmap/dedup-track.md`'s
    superseded banner — both had been touched independently on both sides); resolved by a
    follow-up agent, merge commit `f99fb819`, now `mergeable: MERGEABLE` / `mergeStateStatus: CLEAN`.
    New backend route shapes it landed (ground truth for PR-2 and any future caller):
    `POST /properties/merge`, `GET /properties/merges`, `POST /properties/merges/{id}/unmerge`,
    `GET /properties/merged`, `POST /properties/assets/{link,unlink}` (all in
    `api/property_merge.py`, mounted at `/properties`), plus label/annotation CRUD moved to
    `api/labeling.py` under `/labeling/*` (image-annotation, phash-note, training-example(s),
    border-case). The old `/dedup/*` router is gone entirely.
  - **PR-2 (frontend removal) — done, draft, CI green, mergeable**: [#967](https://github.com/waiff/sreality/pull/967),
    branch `feature/new-dedup-frontend-removal`, **stacked on PR-1** (base branch
    `feature/new-dedup-backend-removal`, not `main` — it depends on PR-1's route renames). 62
    files, +240/−11,716, tsc/vitest/build all green. Repoints every Browse `mergeMode` /
    labeling-CRUD caller in `lib/api.ts` from the old `/dedup/*` paths to the new ones (spot-
    verified against the diff — matches the ground-truth route audit exactly), deletes the
    decision-layer pages/components/libs, trims the dedup sections out of Settings/Health/Costs/
    ListingDetail, and adds a minimal NEW DEDUP nav placeholder (Dashboard + Settings stub pages,
    real content comes in Wave 1). `/clip-audit` and all its labeling widgets
    (TrainControl/LabelCombobox/NoteFlagControl/ImageTagBadge/RenderBadge/ImageLightbox) are
    untouched. Non-blocking gaps from its own verification pass (a handful of now-orphaned
    `api.ts`/`queries.ts` exports, stale comments citing deleted backend modules, one unused
    `dedup_eligible_pct` type field) — cosmetic, left for follow-up cleanup, not a merge blocker.
  - **Still blocked by the permission classifier this session** (both explicitly part of
    CUTOFF.md §7's Day-0 freeze, both need the operator to either run them directly or grant
    permission — see the ask below):
    1. `gh workflow disable` on the 6 legacy decision workflows (dedup_engine.yml,
       dedup_batches.yml, dedup_model_compare.yml, clip_trial.yml, embedding_ab.yml,
       validate_render_detection.yml). Note: PR-1 already deletes 4 of these 6 outright
       (dedup_engine/dedup_batches/dedup_model_compare/validate_render_detection) plus
       clip_trial/embedding_ab — so once PR-1 merges, disabling becomes moot for those files;
       the gap is only the WINDOW between now and that merge.
    2. The M-0 DB flip itself (`update app_settings set value='false'::jsonb where
       key='dedup_publication_gate_enabled'`) via Supabase execute_sql. **Confirmed live current
       value: `true`** — the gate is actively hiding un-evaluated new properties from Browse/map/
       watchdogs right now. The worker dedup lane is also confirmed live at
       `realtime_dedup_interval_seconds = 90` (NOT dark) — same freeze dependency.
    Net effect: the legacy engine + its scheduled jobs are still fully live; duplicates are
    accumulating per the accepted Q1 tradeoff. **PR-1 must not merge until M-0 actually lands**
    (PR-1 deletes the only code that stamps `properties.published_at`; merging while the gate is
    still `true` would hide every new property with no self-heal). **PR-1 and PR-2 should then
    merge back-to-back** (PR-2 already stacked correctly for this).
  - **Operator ask to unblock the rest of W0**: either (a) run the `gh workflow disable` x6 and
    the M-0 SQL update yourself (exact statement above), or (b) grant this session permission to
    do so. Once M-0 is applied, PR-1 → PR-2 can merge in sequence; PR-3 (the actual teardown
    migration + view redefinition) is separately gated on your explicit OK per CLAUDE.md rule 1,
    independent of this.
  - Next session: get the two blockers resolved; merge PR-1 then PR-2; watch the post-merge
    verification checklist (CI green, a brand-new property visible in Browse without a stamp,
    manual merge/unmerge end-to-end, scrape/image/tag lanes unaffected, Health page clean); only
    then draft PR-3 (migration) — it stays separately gated regardless of Gate 0's status.
- 2026-08-05 — Program + cutoff drafts written; awaiting operator approval of both.
