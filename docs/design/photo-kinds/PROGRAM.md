<!-- STATUS: PROPOSED 2026-10-20. Nothing built. Awaiting rulings R1–R8 (§8). On approval the sprint runs autonomously, one draft PR per wave (§5), one merge per operator green light. -->

# PHOTO KINDS: one label per photo, decided once (proposal, 2026-10-20)

**Written for:** the operator (the ruling is yours) and the sessions that will build it.

Every figure below was measured read-only on production on 2026-10-20 (Supabase project `erlvtprrmrylhznfyaih`, main at 51367266) or is quoted from a repo document with its path. Estimates are labelled. The investigation ran as two multi-agent rounds: seven readers plus a completeness critic over the code and the live database; then three independent designs, two judges, fourteen adversarial refuters on the load-bearing claims, and two live measurements (§10). Where a refuter overturned a design claim, this document carries the corrected version.

## 0. The answer in one paragraph

Yes, the trained DINOv3 heads can become the only labels users see, and the cost is small: about **$10–31 once** and **$9–24 a month** of rented GPU time, plus under a dollar a month of storage. Three things in the brief turned out false, and they shape the plan. (1) The dedup engine reads the CLIP **tag rows**, not just the CLIP vectors, so the CLIP lane can never be switched off: the switch saves no compute, only product code and view columns. (2) The active model (v1) has 11 heads, and about half of all listing photos (hallways, bedrooms, gardens, balconies, WC, documents) belong to kinds it has no head for, so v1 cannot label everything; a precision gate decides which of its kinds are shown, and the rest show no label until v2 adds heads. (3) Only 7.75% of active-listing photos carry a DINO score and nothing scores new photos, so a scoring lane and a backfill come before any user-visible change. On the seven kinds both taggers share, the DINO heads are measurably better than CLIP on the human-labelled holdout: at the proposed floor they are right about 89–91% of the time (pessimistic bound 0.80–0.83) against CLIP's 58%.

## 1. What you asked, and what the investigation overturned

You asked for the user-facing image labels (the badge on every photo and the Browse "Cover" choice) to come from the trained DINOv3 heads instead of zero-shot CLIP, without touching dedup.

| Premise in the brief | What is true (verified) | Consequence |
| --- | --- | --- |
| Dedup may use CLIP vectors; the CLIP tags are the product's. | autodedup reads `image_clip_tags` (fine_tag, logical_tag, confidence) directly: `autodedup/export_sql.py:233-245`, the live lane `autodedup/incremental_lane.py:1203-1206`, the fingerprint's room families (`autodedup/fingerprint.py:36-87`), the evidence rule on `images.clip_tagged_at` (`incremental.py:181-189`, `incremental_lane.py:1535-1537`). | The CLIP lane (`clip_tag.yml`, `scraper/clip_tagger.py`, `scripts/clip_tag_backfill.py`, `data/clip_taxonomy.json`, `toolkit/room_taxonomy.py`, both CLIP tables) is **untouchable** and keeps running. Only CLIP's *product read paths* are deleted. No compute is saved. |
| "Switch to DINO" is a source swap. | v1 = 11 heads (fasáda 3, garáž 17, koupelna 22, kuchyně 25, obývací pokoj 28, 3D plán 39, katastrální mapa 42, letecký snímek 43, property list 45, půdorys 46, technické 48). CLIP shows 19 kinds. By CLIP's own census about 51% of photos are hallway (18%), garden (12%), bedroom (9%), balcony (5%), WC (4%), staircases, documents, maps: no v1 head. On the holdout all 12 bedrooms got "obývací pokoj" as the top head (2 of them confidently) and all 4 WC photos got "koupelna" confidently. | A photo label must be allowed to be **absent**. The rule is "the top head, shown only when confident and only for kinds that passed the gate"; otherwise no label. No CLIP fallback (that would be two taggers behind one badge, the patchwork the mandate forbids). |
| DINO scores exist for the corpus. | 1,431,474 scores, all written on 2026-09-27 by an unmerged prototype (open draft autodedup PR #1639) over a dedup cohort: 463,233 of 5,978,172 stored photos on active listings (7.75%), 67.6% of the scored photos are on now-inactive listings, skewed to Plzeňský/Karlovarský kraj. Nothing scores new photos (about 73–79k a day, peaks 118k). CPU scoring at the model's resolution takes about 37 s per photo, so a GPU is required. | A scheduled GPU lane and a one-off backfill are prerequisites. |
| The Browse Cover is CLIP's. | The Cover has been DINO-first since migration 591 (2026-10-07): any head scoring at least 0.5 counts as a yes, CLIP only where unscored. The badge next to it still shows CLIP. The two can disagree on one card, and a test pins that. The 2026-10-07 per-head rule also contradicts your 2026-09-09 ruling "a photo's tag is the single highest-scoring head, no per-head yes/no" (migration 490). | One decision for badge and Cover; the 2026-09-09 rule stands, with a floor. |

Nothing else shows image tags: not the Chrome extension, the PDF download, notifications, or the Browse list read model (verified).

## 2. North star

> **One photo, one kind, decided once in the database.** A photo's kind is the active model's top head, shown only when its score clears that model version's measured floor and the head is in that version's admitted set; otherwise the photo has no kind. Every surface (badge, Cover, phone strip, the owed "Has a floor plan" filter) reads that one decision. CLIP leaves every user surface and stays only as dedup's signal. Every wave deletes at least as much as it adds.

Vocabulary (one new GLOSSARY term): a **photo kind** is the model's decided kind of a photo. It is not a *Tag* (rule 18, the operator's curation label on a property) and not a *label* (a human truth row in `image_tag_labels`). Jargon used below: a **view** is a saved query that behaves like a table; a **migration** is a numbered SQL file that changes the database; the **holdout** (exam_v1) is a sealed set of 250 photos the operator labelled by hand that no model was trained on; a **Wilson lower bound** is the pessimistic end of a 95% confidence interval for a proportion, so "lower bound 0.80" means "we are 95% sure the true precision is at least 80%".

## 3. What exists today (the facts the design stands on)

| Fact | Value (2026-10-20) |
| --- | --- |
| Stored photos (R2) / on active listings | 14.96M / 5,978,172 (396,271 active listings) |
| Photos with a v1 score / on active listings | 1,431,474 / 463,233 (7.75%) |
| New photos per day (14-day mean; weekday; peak) | ~73–79k; 77–108k; 117.6k |
| CLIP coverage of active photos | 99.8%, label within 2.6–6.5 h of download |
| v1 identity (registry row id 1) | DINOv3 ViT-B/16, revision 5931719e…, 768 px, letterbox_pad, bf16, CLS pooling, 11 logistic heads, threshold 0.5 (never tuned; scores are not calibrated probabilities) |
| Training labels | 85% unverified gpt-5-mini labels admitted by review; newest 2026-09-08 |
| Measured GPU throughput (2026-09-27 pass) | 24–46 photos/s per card, incl. JPEG decode; $1.75–1.80 per million photos at community list price, $5.42 realized (secure cloud, idle tails) |
| Vectors from the prototype pass | 1,423,041 float16 vectors in R2 under `dinov3-vectors/…/5931719e67bb-768-letterbox_pad-cls-bf16-transformers/` (existence to be re-listed at build time) |
| Database | 134.3 GB; `image_clip_embeddings` 41.4 GB (dedup's); `image_tag_scores` 0.61 GB at 458 B per row |

Pipeline state: `tag_model.yml` (promote / score / activate, manual) works; the GPU embed lane `dinov3_embed_backfill.yml` cannot run on main because `data/dinov3_config.json` still has three identity facts set to null while the registry row carries the full identity (a duplicate; the registry is the truth). The prototype branch holds the reusable pieces (R2 vector shards, in-pass head scoring, a GPU ladder over community and secure clouds, paged selection) mixed with dedup experiment code that is **not** ported.

## 4. The design

### 4.1 The one decision (SQL)

One internal view, `image_photo_kinds`, is the only place the decision is computed. No TypeScript, Python or API code re-derives it.

```text
image_photo_kinds(image_id, kind_id) =
  rows of image_tag_scores s
  where s.model_id      = the active model            (tag_head_models.status = 'active')
    and s.winner_score  >= the active model's floor     (new column tag_head_models.kind_floor)
    and s.winner_tag_id  in the active model's admitted set (new column tag_head_models.shown_heads)
```

- `winner_tag_id` / `winner_score` are already stored at write time by `toolkit/tag_models.winner_of` (the argmax over the version's heads, ties to the lower tag id, your 2026-09-09 rule). The view only filters; it computes nothing per row.
- The argmax runs over **all** heads before the gate. A photo whose top head is not admitted (e.g. property list) or is below the floor gets **no kind**, never its runner-up. This is the classic "reject option" of pattern recognition (Chow 1970), selected the risk-coverage way (Geifman & El-Yaniv 2017).
- The floor and the admitted set are facts of a **model version**, written at activation. A DB CHECK refuses an active version without a floor; a CHECK keeps `shown_heads` a subset of `heads`. Changing the floor is one UPDATE; every surface sees it on its next read, with no re-score and no deploy. Rollback is "activate the previous version".
- The Czech name lives once, in a new nullable column `tag_taxonomy.display_name` (vocabulary only, never the gate; `tag_taxonomy.label` is untouched because draft PR #1639 matches on it).
- Published to the browser through two owner-rights views, exactly the posture migration 591 already uses for scores: `images_public` gains `photo_kind_id` and `photo_kind` (plain columns via a LEFT JOIN, so the planner removes the join for readers that do not select them and the owed floor-plan filter can use the existing index on `(model_id, winner_tag_id, winner_score)`); `photo_kinds_public(id, name)` lists the active version's admitted kinds for the Cover dropdown. The three floor/gate values are read as uncorrelated sub-selects (InitPlans), which the refuters showed keeps the join removable and the cost within noise (measured: +1 to +4 buffers on about 5,100 for a 24-card Browse read).
- Consumers: the **badge** renders `photo_kind` or nothing (no percentage, because the scores are not probabilities). **Cover(T)** = the first photo in portal order whose kind is T, else the ad's first photo; one pure function serves Browse cards, the Explore modals and the phone strip. The owed **"Has a floor plan"** filter becomes `EXISTS (image_photo_kinds where kind_id = 46)` in one `filter_compiler` hook shared by Browse and Watchdog (rule 16), after this sprint.
- Retired: the 2026-10-07 per-head "at least 0.5 is a yes" Cover rule; the floorless argmax of the admin route `/new-dedup/tags/*` (no callers); the SPA's CLIP label map and `HEAD_SCORE_FLOOR`. Three readings of one tag become one.

### 4.2 The gate: which kinds v1 may show, and at what floor

Pre-registered, so it is not fitted to the data that grades it:

- **Floor F = 0.80** for every head of a version (one floor; per-head floors would be fitted on 0–20 holdout positives, i.e. on noise; per-head calibration is v2's job and makes one floor a real probability).
- **A head is admitted** when, on the ratified holdout grading (the rule already in code, `toolkit/exam_machine_review.py:317-334`, which abstains on untouched declared-default cells), it has at least 8 graded shown photos and a per-head Wilson 95% lower bound of at least 0.60.
- **The version passes** when the pooled shown precision of its admitted heads has a Wilson lower bound of at least 0.80 on the pure-random frame of the holdout (the stratified half's stored inclusion probabilities are phase-2 conditional and cannot be weighted honestly; the refuters established this).

Measured today on exam_v1 (§10, Measurement A):

| Floor | Photos badged | Badge precision | Wilson LB | Wrong badge on photos of no v1 kind |
| --- | --- | --- | --- | --- |
| 0.5 (today's Cover floor) | 60.0% | 0.686 | 0.599 | 18.5% |
| 0.7 | 47.6% | 0.839 | 0.751 | 7.7% |
| **0.8** | **41.6%** | **0.890** | **0.804** | **3.1%** |
| 0.9 | 32.8% | 0.937 | 0.848 | 1.5% |

CLIP, the badge users see today, on the same photos and the seven shared kinds: precision 0.578, recall 0.641. It badges 100% of photos, with no floor.

Per head at 0.80: fasáda 14/15 (LB 0.70), koupelna 12/13 (0.67), kuchyně 10/10 (0.72), obývací pokoj 15/16 (0.72), půdorys 9/9 (0.70) pass. Katastrální mapa 10/15 (LB 0.42) fails. Garáž (1 shown), letecký snímek (3 shown), 3D plán (9 shown but never asked on the exam, all look like real 3D plans) cannot be graded. Property list and technické are never the top head at 0.80. **So v1 would ship with five kinds: Fasáda, Kuchyně, Koupelna, Obývací pokoj, Půdorys**, covering roughly 27–38% of photos (27% on the pure-random frame, 38% on all 250). On the pure-random frame those five are 25/26 right (LB about 0.81), a pass by one error; the judge who verified it calls the pass **provisional** and asks for confirmation on a fresh holdout (exam_v2) before any v2 activation, which the plan does.

The residual error at 0.80 is the WC photo: both confident WC photos were called koupelna. A WC head (v2) fixes it; no floor can.

What the gate is **not**: it is a read-only CLI stage (`tag_model gate`) whose report is pasted into the PR; it writes nothing and is not a browser route (a browser route grading against holdout answers is a holdout-exposure risk). The holdout census test (`tests/test_holdout_exclusion_census.py`) is honoured: the gate reads exam answers through the existing door and no training read touches exam members.

### 4.3 The lane: embed on a rented GPU, vectors to R2, scores to Postgres

```text
GitHub Actions cron (hourly cron, fires every ~4.7–7 h in practice; p90 ~7 h, max ~13.5 h)
  dispatcher on the free runner
    reads the active model row (the ONLY identity source; data/dinov3_config.json + its loader + its test are deleted)
    sizes the pods from the pending count (at most 2 on cron, 8 on a manual backfill dispatch)
    rents RunPod GPUs through the ladder: community 3090/A4000/A5000 first, secure cloud as fallback
  each pod
    selects pending = stored photo on an ACTIVE listing (plus every labelled image and every holdout image)
                      with no score for the active model, OR a score older than images.last_download_attempt_at
                      (that is the re-arm for re-mastered bytes, done on the selection side so the CI rail that forbids
                       touching image_tag_scores from the byte-replace chokepoint stays untouched)
    downloads JPEGs from R2 on threads, letterboxes to 768, embeds in bf16 (24–46 photos/s)
    writes float16 vectors to R2 as UTC-stamped parts (newest part wins on a duplicate id; the prototype's manifest is dropped)
    scores all heads in pure Python in the same pass; one upsert per chunk into image_tag_scores
      with scored_at = the clock read BEFORE the R2 download (so a later byte replacement always looks newer)
    writes a per-run done record when its selection is exhausted or its time budget ends
  dispatcher sees every pod's done record → terminal → pods torn down at once (today every run idles ~900 s and ends red)
health
  a coverage cell in scripts/checks/quality.py (the existing flow/stock shape): active photos downloaded 6–30 h ago
  that have no current score, and the pending backlog with an ETA; the jobs cell stays green because the run ends green
```

Why these choices (each one was weighed in §9): Postgres is wrong for the vectors (14–36 GB that Supabase disk never gives back) and R2 is right ($0.14–0.35 a month); keeping vectors at all is right because a v2 with more heads is a pure-Python re-score from R2 (~$0.01) instead of a $26–81 GPU re-embed; the realtime worker cannot host this (no torch in its image, the RunPod and HF secrets live in Actions only); CPU cannot (37 s per photo). The one-off backfill is the same workflow dispatched by hand with 8 shards, active listings first: one code path, no backfill mode, no region-priority setting (the lane is ordered by active listings).

### 4.4 Model lifecycle, and v2 as data

This sprint ships v1 behind the gate. The next sprint, named here so it is not scope creep, makes **v2 a data step with no code change**: `promote` takes a vector source plus the encoder identity from the registry instead of a dedup_sim bake-off run (that deletes its coupling to a schema NEW DEDUP may drop wholesale), trains on one shared grouped split with per-head Platt calibration so one floor means one probability, `score --source r2:` re-scores the corpus from R2, `gate` grades on a fresh single-phase holdout (exam_v2, exact inclusion probabilities, at most one photo per property), `activate --floor --shown` flips every surface at once. Heads trainable today from existing labels: ložnice (300 admitted positives), jídelna (299), nezařízená místnost (300), domovní vchod (188). Heads that need your labelling first: WC, chodba, zahrada, balkon/terasa, schodiště, situační plán, a document kind (the data-science reader's estimate: 12–16 operator hours plus $50–80 of LLM labelling). Activating a version is a release: all badges and Cover options change at once, with no deploy; rollback is one activate.

### 4.5 What users see

- The badge on every photo (five mounts, one component) shows the photo's kind or nothing. Today: a CLIP label on every photo, 42% wrong on the shared kinds.
- The Cover dropdown offers Default plus the admitted kinds. Options with no admitted head (Ložnice, WC, Balkon/terasa, Zahrada, Situační plán, Garáž, 3D plán, Katastrální mapa, Letecký snímek) disappear until a version admits them, and return automatically when it does.
- The render pill ("Render NN / Foto NN"), a CLIP-only eyeball tool nothing consumes, is retired (your call, R3).
- New photos get their kind within one cron cycle (about 5–14 h after download; CLIP shows a label within 2.6–6.5 h today).
- Photos of listings that are inactive at backfill time show no kind unless the listing is relisted (your call, R2).

### 4.6 The dedup boundary (never edited by this sprint)

`autodedup/`, `dedup_sim`, `image_clip_tags`, `image_clip_embeddings`, `images.clip_tagged_at`, `clip_tag.yml`, `clip_retag.yml`, `scraper/clip_tagger.py` (including its orphaned `render_scores_from_emb`), `scripts/clip_tag_backfill.py`, `data/clip_taxonomy.json`, `toolkit/room_taxonomy.py`, the index `image_clip_tags_render_null_idx`, the column `image_clip_tags.render_score` (NEW DEDUP's Set-2 manifest reads it), `image_room_classifications` (frozen by migration 475). Edits under `frontend/src/components/autodedup/` are type-only (the shared photo type loses two fields) and are called out in the PR. Two items are handed to the dedup owner as a separate question: `clip_retag.yml` still runs four no-op jobs an hour, and the render index has never been scanned.

**Coupling to rule on (R6):** draft PR #1639 (the prototype) and its dependents #1643/#1648 add `autodedup/head_tags.py`, which reads the **active** model's scores and matches `tag_taxonomy.label` strings, raising on unknown labels. If they ever merge, a product-driven v2 activation would change dedup's input. Nothing in this sprint edits them; W3 lifts the shared lane files onto main under the prototype's names, so #1639 would rebase to its dedup-only remainder.

## 5. Waves (one draft PR each; CI green and one green light per merge; migrations start at 646 because 643–645 are held by open PRs)

| Wave | Goal | Adds (approx.) | Deletes (approx.) | Migration | Gate before merge |
| --- | --- | --- | --- | --- | --- |
| **W1** `cleanup/photo-kinds-dead-paths` | Delete paths dead whichever way the design goes. No behaviour change. | ~5 doc lines | `mirror_model_weights.yml` + script + test (404; zero runs ever); `backfill_render_score.yml` + script (167; one-shot, done 2026-06-25); `scripts/train_tag_head.py` (128); the `drop_dinov3` flag nobody sets + 3 tests (~45); `FINE_TAG_KEYS`/`COLLAPSE_ONLY_TAGS` + tests (~35); workflow-docs regenerated | none | CI |
| **W2** `feature/photo-kinds-decision` | The one decision in SQL plus the gate report. Nothing user-visible: the new columns are appended and nobody selects them yet. | mig 646 (~90); `tag_models.gate()` (~110) + `tag_model.py gate` stage + workflow input (~50); tests (~220); GLOSSARY entry | `api/new_dedup_tags.py` + tests + `winners()` (~445, if R5 = yes) | **646 additive**: `tag_head_models.kind_floor` + `shown_heads` + 2 CHECKs; `tag_taxonomy.display_name` UNIQUE; views `image_photo_kinds` (internal) and `photo_kinds_public`; `images_public` appended with `photo_kind_id`, `photo_kind`; the ruled floor, admitted set and five names | gate report pasted in the PR; R1, R4, R5; EXPLAIN of the 24-card Browse read |
| **W3** `feature/photo-kinds-lane` | Score every active-listing photo and every new one; vectors to R2, scores to Postgres, identity from the registry, run ends green, coverage on Health. Then dispatch the backfill. | manifest-free `toolkit/vector_shards.py` + tests (~300); embed script: R2 writer, in-pass scorer, key-resolved pending selection with the re-arm, done record (~300); dispatcher: ladder, pod sizing, terminal on done records (~150); `runpod_client` cloud type (+31); `R2Client.list_keys` (+8); `tag_models` r2 score source (~35); coverage check (~60); workflow cron/concurrency (~20); tests (~360) | `data/dinov3_config.json` + `scraper/dinov3_config.py` + its test (209); the Postgres vector write mode + its tests (~250); `select_gpus` + allowlist + the `terminal=False` posture + tests (~70); `production_source` + tests (~75); `dominant_encoder`/`_fetch_embeddings`/`encoder_vector_source` + tests (~190); the config-null preflight (~40) | none (the coverage check needs no row) | R2 (scope + spend), R8 (cadence); dry run lists the 2026-09-27 R2 prefix; a 2,000-photo re-score from R2 reproduces stored v1 scores; one manual run over the labelled pool before the cron is enabled |
| **W4** `feature/photo-kinds-spa` | Every photo surface reads the kind. CLIP, render-score and head-score reads leave the SPA, which stops selecting every column W5 drops. | `lib/photoKinds.ts` (~45: type, `usePhotoKinds` over `photo_kinds_public`, pure `coverIndex`); `PhotoKindBadge` (~18); dropdown from the view (~8); tests (~60) | `lib/imageTags.ts` (142); `ImageTagBadge` (36); `ImageRenderBadge` + 3 mounts + plumbing (~56, R3); CLIP fields of `ImagePublic` + `IMAGE_PUBLIC_COLS` + the second select list (~30); `imageTags.test.ts` (137) + CLIP fixtures in 11 test files (~90); null-fill sites (~14, type-only under autodedup); the labeling page's "Original tag" mode (~70, R5) | none | W3 backfill ≥ 99% of active stored photos with a current score; coverage cell green ≥ 72 h; R3; after merge: Railway vite status + a read-only browser check of a card, the gallery, the lightbox and the phone feed |
| **W5** `cleanup/photo-kinds-contract` | The view contract matches the one decision; the dead Postgres vector table goes. One destructive window. | mig 648 (~70) | `images_public` 15 → 8 columns (clip_fine_tag, clip_logical_tag, clip_confidence, clip_render_score, phash, rendition, tag_head_scores, and the per-photo CLIP LATERAL join); `listing_cover_public` 11 → 3 columns; table `image_dinov3_embeddings` (9,514 rows, 21 MB); `tests/test_dinov3_embeddings_migration.py` (97) | **648 destructive** (DROP VIEW + CREATE, because CREATE OR REPLACE can only append; `set local lock_timeout`; REVOKE ALL then GRANT SELECT to authenticated; `notify pgrst`) | W4 live ≥ 24 h; zero API-log reads of a dropped column; pg_dump of the table (the views hold no data; their definitions are in migrations 591/416); R7 in an operator-opened window |

Rough tally: about +1,900 / −2,800 lines, net about **−900**, plus 15 view columns, 1 table, 2 workflows, 1 API module and 1 config file gone. W3 is the only net-positive wave: it is the capability the north star needs.

Ordering rule the refuters confirmed: PostgREST answers HTTP 400 to a client that selects a column a view no longer has, so **the SPA must stop selecting the columns and be live before migration 648 applies** (the lesson of the migration 438 outage). W2 appends, W4 stops selecting, W5 drops.

## 6. Cost

| Item | Amount |
| --- | --- |
| One-off backfill, active-listing photos (5.52M unscored) + labelled pool + holdout | **$10–31** (community list $1.75–1.80 per million photos; the 2026-09-27 realized secure-cloud mix $5.42). Wall clock about 8 h on 8 pods. R2 JPEG reads within the 10M/month free tier or ≤ $2. |
| Alternative: every stored photo (13.53M) | $24–73, about 20 h, +6.3 GB of score rows instead of +2.7 GB |
| Steady state, ~79k photos/day, 4–6 runs/day, pods torn down on done | **$9–24 a month** ($0.29–0.80 a day; under the ledger's RunPod cap of $1/day) |
| Vectors in R2 (float16, 1,570 B each) | $0.14/month active, $0.35/month full corpus, +$0.06/month growth |
| Score rows in Postgres (458 B each) | +2.7 GB once, +1.1 GB/month (non-reclaimable disk, reused after version prunes) |
| v2 re-score from R2 (new heads, same encoder) | about $0.01 of R2 reads, 3–6 CPU-hours on free runners; the DB write (~9 GB) is the slow part |
| v2 labelling (next sprint) | 12–16 operator hours + $50–80 LLM (estimate) |
| CLIP lane | unchanged: $0, about 3.2 free runner-hours a day; cannot be retired (dedup) |

Not chosen: a 24/7 pod ($122–360/month); hourly on-demand pods ($25–65/month); serverless ($17–18/month, needs a container image carrying the gated DINOv3 weights, a licence question); 512 px instead of 768 (saves $6–9 once, costs 2.4 exam-F1 points, discards the 1.43M v1 scores and forces a retrain).

## 7. UX changes (each needs your yes; recommendations in §8)

1. Badges appear on roughly 30–40% of photos instead of nearly all, and only for Fasáda, Kuchyně, Koupelna, Obývací pokoj and Půdorys until v2. Bedrooms, hallways, WC, gardens, balconies, documents, maps show no badge. In exchange, a shown badge is right about 9 times in 10 (pessimistic bound 0.80) instead of about 6 in 10.
2. The badge tooltip "CLIP NN %" goes, with no replacement number (the scores are not probabilities; v2 calibration may bring a number back).
3. The render pill is retired.
4. The Cover dropdown lists Default plus the admitted kinds; nine options disappear until a version admits them; each browser's saved Cover choice resets to Default once.
5. Cover picks the first photo in portal order that has the chosen kind. Measured against today's per-head rule on 19,461 fully scored active listings (§10, Measurement B): the two rules pick two *different* real photos on at most 0.5% of listings; today's rule picks a photo where the new rule falls back to the first photo on 1.8% (koupelna) to 12% (fasáda) of listings at the 0.80 floor. Those are mostly photos another head claims more strongly (a "fasáda" pick that the model thinks is a garage), and there is no ground truth for which is right.
6. New photos get a kind within about 5–14 h (one cron cycle), against CLIP's 2.6–6.5 h today.
7. Photos of listings already inactive at backfill time show no kind unless relisted (detail pages of delisted properties, comparables, sold comps). Scoring every stored photo instead costs $24–73.
8. The admin labeling page's "Original tag" (CLIP) toggle, idle since 2026-08-27, is removed (or kept, served from its own API).

## 8. Rulings needed before the autonomous build (recommendation first)

- **R1. The decision and the gate.** One decision for badge and Cover: the top head, shown at or above a pre-registered floor of 0.80, only for heads admitted by the holdout gate (≥ 8 graded, per-head LB ≥ 0.60; version passes at pooled LB ≥ 0.80). v1 ships with five kinds now; the 2026-10-07 per-head Cover rule is retired. **Recommend yes.** Alternative: no user-visible change until v2 passes the gate (weeks; users keep the 58%-precision CLIP badge meanwhile).
- **R2. Backfill scope and spend.** Active-listing photos plus the labelled pool and the holdout ($10–31), then the steady lane ($9–24/month). Inactive galleries get no kind unless relisted (UX item 7). **Recommend yes.** Alternative: every stored photo ($24–73, +6.3 GB).
- **R3. The UX bundle** (items 1–6: coverage, no tooltip, render pill retired, Cover options from the model, first-in-portal-order Cover, 5–14 h lag). **Recommend yes to all.** The render pill is the one item with a cheap alternative: keeping it keeps one CLIP column and the per-photo CLIP join in `images_public`.
- **R4. Names users see.** Fasáda, Kuchyně, Koupelna, Obývací pokoj, Půdorys. Later kinds get names only when admitted; dedup-internal head names (property list, letecký snímek s ohraničením subjektu, technické zařízení) never appear as written. **Confirm the five words.**
- **R5. NEW DEDUP namespace deletions.** Delete the admin route `/new-dedup/tags/*` and `winners()` (zero callers; a third, floorless reading of the tag); remove the labeling page's "Original tag" mode. **Recommend both.** Alternative for the toggle: keep it, served from the labeling API (+15 lines).
- **R6. Draft PRs #1639, #1643, #1648** (autodedup, read the active model's scores and label strings; R1c ruled NO-GO 2026-10-01). Close them, or have dedup pin model v1 by id. **Recommend close.** Any v2 activation waits on this ruling either way.
- **R7. The destructive window (W5):** narrow `images_public` and `listing_cover_public`, drop `image_dinov3_embeddings` after a pg_dump, in a window you open, after W4 has been live ≥ 24 h with zero reads of the dropped columns. **Recommend yes.** Separately, for the dedup owner (not this sprint): `clip_retag.yml` (four no-op jobs an hour since June) and the never-scanned render index.
- **R8. Cadence.** Labels within one GitHub-cron cycle (5–14 h) on on-demand pods. **Recommend yes.** Alternative: hourly via serverless ($17–18/month, container with gated weights).

## 9. Rejected alternatives (so they are not re-litigated)

Decision in TypeScript (Watchdog and `filter_compiler` could not share it; every floor change a deploy; today's three readings are exactly this failure). Materialising the kind onto `image_tag_scores` (every floor or name change rewrites millions of rows). CLIP fallback for unscored or unheaded photos (two vocabularies in one badge; precision unmeasurable; CLIP is at 0.58). A catch-all "other" head in v1 (a heterogeneous background class generalises poorly, Dhamija 2018 / Vaze 2022; it must not be trained on exam negatives; the floor already is the reject option). Per-head floors in v1 (fitted on 0–20 positives). Display names as SPA constants or a names table (a second vocabulary home; a deploy per head). Publishing a score column (uncalibrated; invites a number in the UI). Vectors in Postgres (non-reclaimable 14–36 GB). Not storing vectors (every head change a GPU re-embed; dedup_sim, today's only training-vector store, is droppable). Scoring in the realtime worker or on CPU. The prototype's manifest as the checkpoint (reads every manifest file per pass; last key wins by id range, not by recency). A dark launch behind a null floor (a flag in disguise). A region-priority setting (a twin of `clip_tagging_priority_region_ids`). Repurposing the admin tag route as the gate (holdout exposure). The term "photo label" ("label" is human truth here).

## 10. Measurements (live, 2026-10-20; SQL kept with the investigation artefacts)

**A. exam_v1 head-to-head, ratified truth (untouched declared-default cells abstain), 7 shared kinds, 92 positives.**

| Tagger | TP / FP / FN | Precision | Recall | F1 |
| --- | --- | --- | --- | --- |
| CLIP zero-shot (today's badge) | 59 / 43 / 33 | 0.578 | 0.641 | 0.608 |
| DINO v1 top head, no floor | 85 / 101 / 7 | 0.457 | 0.924 | 0.612 |
| DINO v1 top head ≥ 0.5 | 82 / 35 / 10 | 0.701 | 0.891 | 0.785 |
| DINO v1 top head ≥ 0.7 | 77 / 14 / 15 | 0.846 | 0.837 | 0.842 |
| **DINO v1 top head ≥ 0.8** | **72 / 9 / 20** | **0.889** | **0.783** | **0.832** |
| DINO v1 top head ≥ 0.9 | 58 / 4 / 34 | 0.935 | 0.630 | 0.753 |

Weighted on the pure-random frame (n = 100): CLIP 0.556 / 0.658; DINO ≥ 0.8 0.909 / 0.789; shown precision 0.912 (composite 0.918, Kish n_eff 70, LB 0.830). Absorption: of 54 holdout photos whose only truth is a non-v1 kind, 3 get a badge at 0.80 (two WC photos as koupelna at 0.97 and 0.94, one bedroom as obývací pokoj at 0.83). Caveats: tag 28 and the absorption rows come from the 99 photos the operator re-answered, an enriched subset; per-head n is 3–32; the stored inclusion probabilities of the stratified half are phase-2 conditional, so only the pure-random 100 can be weighted honestly; exam_v2 must draw single-phase with exact probabilities.

**B. Cover rule, 19,461 active listings with every photo scored.** Swap (both rules pick, different photo) ≤ 0.5% for every kind. Visible lost covers at floor 0.80 (today picks a non-first photo, the winner rule falls back): fasáda 12.0%, kuchyně 3.9%, obývací pokoj 8.5%, koupelna 1.8%, půdorys 4.6%; the photos lost are mostly claimed more strongly by another head (fasáda ↔ garáž, katastr ↔ letecký, půdorys ↔ katastr/property list/3D). Share of v1-scored active photos whose top head clears 0.80, by kind: půdorys 94%, katastr 84%, koupelna 80%, 3D plán 79%, kuchyně 66%, fasáda 39%, obývací pokoj 24%, garáž 20%, letecký 19%, technické 18%; all photos 40%. 6.7% of photos have two or more heads at ≥ 0.5 (the per-head rule's multi-label case).

## 11. Rails and tests each wave must carry

`tests/test_clip_encoder_pin.py` `_TAGGING_WORKFLOWS` (W1); `tests/test_workflow_docs_codegen.py` (regenerate `frontend/public/workflow-docs.json`, W1/W3); `tests/scraper/test_image_rendition_provenance.py` (the reset set stays `{phash, clip_tagged_at}`; the drop_dinov3 tests go in W1; the FORBIDDEN_TABLES entry for the dropped table in W5); `tests/test_migration_rls_grants.py` (a `ci-allow-ungated` marker per new view, W2/W5); `tests/test_tenant_isolation_live.py` (its admin-only-reads assertion fails in code for a new view over `tag_taxonomy` until the view is allowlisted with its reason, W2); `tests/test_holdout_exclusion_census.py` (the gate reads exam answers through `tag_exam.answers`, no new statement names `image_tag_labels`, W2); `tests/test_tag_model_migration.py` (pins 490's SQL; 646 is a new file, W2); `tests/test_dinov3_config.py` deleted with its subject (W3); the eleven frontend test files with CLIP fixtures (W4). Documents rewritten in the same PR as the behaviour: `.claude/skills/scraper-ops/references/tag-model-lane.md` and `dinov3-embedding-lane.md` (both stale today: "never run", "production source returns nothing", "no R2"), `scraper-ops/SKILL.md`, `toolkit-api/SKILL.md:476-484`, `docs/architecture.md:1985-2012` and `3045-3054`, `roadmap/ui-track.md:145-161`, `roadmap/new-dedup.md:483-484,515`, `roadmap/next.md:12,61`, `docs/design/new-dedup/PROGRAM.md` ledger, `GLOSSARY.md`.

## 12. Ledger (receipts land here as waves ship)

- 2026-10-20 — proposal written; rulings R1–R8 open.
