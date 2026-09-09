> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

# NEW DEDUP — ground-up decision-layer rebuild

Full plan: [`docs/design/new-dedup/PROGRAM.md`](../docs/design/new-dedup/PROGRAM.md) (mission,
decisions ledger, simulation architecture, Wave 0-8 plan — the standing home for wave status).
Surgical removal spec: [`docs/design/new-dedup/CUTOFF.md`](../docs/design/new-dedup/CUTOFF.md).

This track **supersedes** [`roadmap/dedup-track.md`](dedup-track.md) — that file documents the
legacy engine's incremental development and is retained only as history; do not resume work
there.

## Why a rebuild instead of another iteration

The legacy dedup decision engine (candidate discovery → pHash/CLIP cosine → forensic vision
compare, `toolkit/dedup_engine.py` + friends) accumulated years of incremental patches and is
being torn out entirely (2026-08-05 operator directive) rather than patched further. The
replacement is built as a **simulation engine first** — every level computes merge/dismiss
outcomes "as if," over the whole database, into a droppable schema — with nothing writing to
production tables until the full stack is approved end-to-end. See PROGRAM.md's "Mission and
non-negotiables" for the full rationale.

## Status

🟢 **W0 DONE — Gate 0 closed 2026-09-05** (Wave 0 — backup + teardown + scaffolding),
**W1 in progress** (Wave 1 — shared prerequisites). See PROGRAM.md's progress ledger for
session-by-session detail; this file tracks only the phase-to-phase status.

W0 was recorded done on 2026-08-25 and was not. Migration 432 dropped only the two funnel/cost
matviews; every other CUTOFF §4 object, the whole publication gate (§3 step 2) and the
verification checklist were still outstanding. A 2026-09-05 review caught it, migration 475
finished the job, and the checklist was run and recorded item by item.

- [x] PR-0: design docs landed (#960).
- [x] Backup branch `backup/pre-new-dedup-2026-08` + tag `backup-pre-new-dedup` cut.
- [x] Day-0 freeze (the 6 legacy decision workflow files were deleted outright by PR-1, which
      supersedes "disabled").
- [x] M-0 (`dedup_publication_gate_enabled=false`, `realtime_dedup_interval_seconds=0` — both
      confirmed live).
- [x] pg_dump backup of to-be-dropped tables to R2.
- [x] PR-1 (backend decision-layer removal) — merged (#966), including the CUTOFF §6 doc pass.
- [x] PR-2 (frontend decision-layer removal) — merged (#967), shipped a minimal NEW DEDUP nav
      placeholder (Dashboard + Settings stub) that W1 is now filling in.
- [x] PR-3 (migration 475: table drops + view redefinition + legacy stamp) — #1286, applied
      live 2026-09-05 11:26:48 UTC. Dropped `property_identity_candidates` (159,260 rows) +
      `_archive` (5,542), `dedup_dirty_properties` (15,357), `dedup_scan_state` (3),
      `dedup_batches` (265), `dedup_batch_requests` (18,250), `dedup_engine_runs` (9,932), the
      six admin views over them, and the unused migration-127 `listings_dedup_eligible_idx`
      (idx_scan = 0) — 112 MB reclaimed, every table re-dumped to
      `backups/new-dedup-teardown/2026-09-05/` first with counts matching live.
      Added `property_merge_events.generation`, `'legacy'` on all 124,363 existing rows.
      Also finished CUTOFF §3 step 2, which August never did: the gate predicate is out of
      `properties_public` / `browse_projection` / `listing_feed_public`, and
      `publication_gate_enabled()` + `publication_gate_health_public` are dropped. 24
      `app_settings` keys deleted plus 11 dead dedup keys inside `pipeline_check_thresholds`.
- [x] W0 verification checklist (CUTOFF §7 step 8) — **all six items pass**, run 2026-09-05
      and recorded item by item in PROGRAM.md's ledger entry for that date. → **Gate 0 CLOSED.**

W1 (shared prerequisites + labeling program):
- [x] `dedup_sim` schema + `settings`/`settings_history`/`simulation_runs` (migration 372, #965).
- [x] Settings registry (`toolkit/dedup_sim_settings.py`), 12 knobs seeded from the decisions
      ledger, each with a plain-language blurb (#965).
- [x] Settings API (`api/routes/new_dedup.py`, admin-gated) + the real NEW DEDUP Settings page
      (`frontend/src/pages/NewDedupSettings.tsx`, replaces PR-2's placeholder) — operator can now
      review/tune every decided default ahead of any wave consuming it.
- [ ] Dashboard skeleton (funnel + cost table) — still just PR-2's placeholder; genuinely no data
      to show until W2+ produces candidates/decisions. Revisit once W2 lands.
- [x] Labeling page, tri-state rework (docs/design/tag-annotation-matrix.md, 2026-08-26) — the
      taxonomy and the confirmed ground truth are now PERMANENT tables outside `dedup_sim`:
      `tag_taxonomy` + `image_tag_labels` (migration 442, one positive/negative/excluded row per
      (image, tag) — every independent per-tag classifier head's future training set), promoted
      because `dedup_sim` is planned to drop wholesale at Wave 8 and a real surrogate key replaces
      the old text-keyed rename cascade. `dedup_sim.labeling_sample`/`label_proposals` stay as the
      transient machine-suggestion queue (`toolkit/dedup_sim_labeling.py`). `/new-dedup/labeling/*`
      API + `frontend/src/pages/NewDedupLabeling.tsx`: tag-centric batch review of proposals by
      default (a tri-state control replaces Confirm/Dismiss), a Sample browse mode that reaches
      every image in the pool for one tag/state (including ones no model ever proposed — answers
      "show me every image where kitchen = excluded"), an image-centric detail panel for the
      multi-tag-on-one-photo case, keyboard shortcuts (arrows/j-k + 1/2/3, none existed before),
      and per-tag positive/negative/excluded counts. **ClipAudit retired outright** (frontend page,
      `TrainControl`, and every backend route/table exclusive to it — `image_tag_annotations` and
      `phash_pair_notes` had zero live callers already); `image_training_examples` is superseded
      but not yet dropped (separately-gated destructive migration). "Border case" (#1113) is
      unaffected — still a whole-image flag orthogonal to any tag's state, still excluded from
      Gate 1 the same way. Secondary-CLIP scoring (`scraper/label_proposal_tagger.py` +
      `scripts/label_proposal_backfill.py`, dispatch-only GH Actions workflow) is separate infra
      from the DINOv2/RunPod embeddings path below — a stronger CLIP checkpoint, CPU, no RunPod
      dependency. Operator still needs to run several labeling rounds to reach Gate 1 (150 positive
      images/tag) — the tool is built, the labeling itself is ongoing curation work. The per-tag
      trainer itself (docs/design/clip-linear-probe.md) is a separate, not-yet-built follow-up.
      Follow-up same day: every tile shows its image's already-assigned tags in one batched call
      (`list_positive_tags_for_images`), and "Modify labels" gained two operator flags — `priority`
      (pins + reddens a tag needing attention) and `ready_for_training` (migration 443).
- [x] Tag definitions store + operator workbench (migration 446, `tag_definitions`) — each tag now
      gets a WRITTEN definition (means / counts / does_not_count / confusable_with + the visual
      tell / leave_out_when / example images), versioned supersede-never-overwrite with no drafts:
      one Save = one new active version, the previous one flipped to `superseded` in the same
      transaction, exactly one active per tag enforced by a partial unique index. Every save states
      the version it was written against (`base_version`) and is refused when that is no longer the
      active one, so a stale second tab cannot silently revert a definition. Other tags are
      referenced BY ID inside the versioned JSONB document (a rename can't rot a definition) and
      resolved to labels leniently on read. `toolkit/tag_definitions.py` + seven routes on
      `/new-dedup/labeling/*` + the `NEW DEDUP · Taxonomy` page (`/new-dedup/labeling/taxonomy`):
      tag list with a `v{n}` status chip, the editor, a gallery of what the tag ACTUALLY contains
      (read straight from `image_tag_labels`, no `dedup_sim` dependency), and CLIP-centroid overlap
      evidence (`nearest_tags`, cosine DISTANCE, min 5 embedded positives). **Writing the ~51
      definitions is now the operator-side blocker**: labeling at scale and per-tag heads both wait
      on them, and the definitions are also the diagnostic that settles the taxonomy — two tags
      whose does_not_count lines can't be written apart are one tag.
      Follow-up: **the two fixes reading a tag provokes now happen in place**, so noticing drift no
      longer costs the operator their position. An `all tags` pill on every gallery tile opens the
      image's full tri-state panel (`ImageTagDetailPanel` + `TriStateControl` EXTRACTED out of
      NewDedupLabeling into `components/tag-annotations/` and used by both pages — a copy would
      drift), and the tag being read is pinned above it with **four word-labeled outcomes**, not
      three glyphs: `keeps it`, `not this tag` (a real negative), `belongs elsewhere` (excluded ·
      pruned — the subject IS present but another tag fits, so a negative would poison this head)
      and `can't tell` (excluded · ambiguous, which feeds the tag's ambiguity rate). Equal cost,
      named consequences on screen before the click, and none of them can clear a cell back to
      untouched. `rename` on the selected row edits the label in place, losing neither the
      selection nor an unsaved definition draft (`PUT /taxonomy/{tag_id}`; every reference is by
      numeric id, so nothing rots). No new routes — the backend was already there.
- [x] Reshaping a tag from the workbench: filing a BATCH under another tag, and DELETING a tag —
      the two operations the definitions kept producing and that were being executed by hand in
      SQL. Both are pure reuse: no new route, no migration. The batch is
      `POST /tags/{tag_id}/annotations/bulk` twice, **destination first** (source-first would
      strand images with nowhere to go), chunked sequentially at the server's 200 cap. Selection
      is a MODE, not a modifier, because a tile click already means "stage as a canonical
      example". What happens to the SOURCE is the choice a naive "move" gets wrong, so it has
      **three** outcomes in the panel's own vocabulary: `keeps it` (a COPY — no source write at
      all, the default, and the motivating case: 145 images that genuinely are
      bathrooms-with-bathtubs AND bathrooms, so `koupelna` went 19 → 164 human positives with the
      child untouched), `not this tag` (a real negative) and `belongs elsewhere` (excluded ·
      pruned). No `can't tell`: a batch filed somewhere specific is not undecidable, and 145
      manufactured ambiguous exclusions would make the ambiguity rate report a broken definition
      that isn't. The destination can never be the tag being read (caught in review — it would
      have written positive then negative on ONE tag, manufacturing human negatives over that
      tag's own positives). Delete leads with the **human decisions**, not the row count that
      buries them (~1,300 manufactured `backfill_442` rows vs a few dozen real ones), gates on
      naming that number only when it is above zero, and says accurately that recovery exists —
      `image_tag_label_events` keeps every destroyed decision with the label denormalised onto
      it, because that table has no foreign keys — as a hand-written SQL job, not a button.
- [x] Outlier-first tag contents (no migration) — the workbench gallery now defaults to ordering a
      tag's positives by cosine distance from that tag's OWN centroid, farthest first, so the
      mis-filed images are the ones on screen instead of somewhere in a wall of 300 photos.
      Measured on `exterier - fasáda` (71 positives): the 12 nearest are exterior_facade /
      staircase_exterior, the 12 farthest are bathroom, document_text, energy_certificate and
      garden. `list_positive_images_outlier_first` (centroid over `human`/`human_confirmed`
      positives only, `MIN_POSITIVES_FOR_CENTROID` floored in a CASE so a tag under the floor can
      still be told how many it has) + `?order=` on `GET /tags/{id}/positive-images`; below the
      floor the route degrades to the existing order, reports `order:'recent'` and the page SAYS
      why. **No threshold on the distance, ever** — inter-tag centroid distances span ~0.01 to
      ~0.42, so only rank inside one tag transfers; the tile shows a distance and a rank, never a
      score. One fetch per tag: "Newest first" is a client-side re-sort, so flipping the order
      refetches nothing — and because the server LIMITs after the sort, a tag over the 300 cap
      says "Newest of these 300" rather than promise a recency the window cannot hold. The
      centroid's basis is on screen either side of the floor ("this tag's 71 human-verified
      positives"), named that way because the overlap panel's identical floor counts a different
      population; a count that was never taken is dropped, never drawn as 0. Same PR: the
      all-tags panel's subject row now STATES the state it is in
      (`✓ currently in this tag`) — four word buttons and a coloured fill read as "nothing is
      checked" to the operator, because the ✓ they scan the list for is not there to be found.
- [x] Annotation provenance + append-only history (migration 446) — every `image_tag_labels`
      cell now records WHO decided it (`source`: human / human_confirmed / machine /
      backfill_442), under WHICH `tag_definitions` version (`definition_id`, resolved at write
      time, never a parameter), when a human last checked it (`verified_at`, derived) and — on an
      excluded cell only, CHECK-enforced — WHY (`excluded_reason`: ambiguous vs pruned). This makes
      the **72,000 rows migration 442 manufactured from a one-hot assumption** (98% of the table)
      precisely identifiable; **nothing is deleted here** — the removal is a separate, gated,
      backed-up PR keyed on `source = 'backfill_442'`. History lands in `image_tag_label_events`,
      written by a TRIGGER rather than by any of the four (soon more) write paths, because a log
      every future writer must remember to append to is a log with holes; clearing a cell back to
      untouched is itself a recorded event. "Machine proposes, human disposes" is now a SQL rail
      (the upsert's `DO UPDATE … WHERE`), not a convention. `tag_overview` gains the provenance
      inventory and a per-tag **ambiguity rate** — ambiguous exclusions over decisions, with pruned
      rows outside numerator AND denominator so pruning can't dilute the signal, both halves scoped
      to what a HUMAN decided so neither the 72,000 backfill rows nor a future flood of unreviewed
      machine rows can bury it, and NULL (never 0) when nothing is decided.
      Above `AMBIGUITY_RATE_THRESHOLD` (0.15, with a 20-decision floor) the tag's DEFINITION is the
      problem, not the labeling.
- [x] Candidate retrieval + the per-tag review queue (migration 450, `tag_candidates`) —
      the review universe stops being `dedup_sim.labeling_sample` (1,200 untargeted images,
      one pool shared by all 51 tags, 943 of them never labeled) and becomes a PER-TAG queue
      filled by CLIP centroid retrieval: rare tags are a fraction of a percent of the corpus,
      so their candidates have to be FOUND, not stumbled on. `toolkit/tag_candidates.py`
      ranks a bounded, category-stratified, per-listing-capped pool against a centroid built
      **only** from that tag's human-verified positives (migration 442's 72,000 manufactured
      negatives and unreviewed `machine` rows excluded by predicate, never by deletion — this
      creates no dependency on the gated deletion PR). Three named mixes, not magic numbers:
      rank bands 50/30/20 (a pure top-N produces prototypical heads that fail on odd cases;
      the mid band is where the measured confusion clusters live; the random band is the
      honesty rail — sustained positives there mean the centroid is missing a mode) and a
      category mix that caps `byt` BELOW its corpus share, so every sitting dilutes the
      83.8%-byt labeled-set skew instead of inheriting it. Exact-hash collapse in SQL plus a
      Hamming-6 near-dup drop and a 2-per-property cap, so a head cannot look like it has 200
      examples when it has 40. **Queue membership carries no training semantics** — no state
      column, no reviewed flag, nothing to misread: absence is not a negative, which
      overturns migration 442's ledger decision (450 restates it as a fresh table comment).
      A tag under 15 human-verified positives is told so (`status='insufficient_positives'`,
      zero rows) rather than handed a garbage pool. Every band and category bucket also
      reports its YIELD (`positive` / `negative`), so the honesty rail is something the
      operator can read rather than something the design asserts. Two admin routes + `python -m
      scripts.draw_tag_candidates` (no workflow: no GPU, no torch, no R2). `sample_size` is
      REMOVED from the overview payload, not repurposed — per-tag `candidate_count` /
      `candidate_open_count` replace it. Does NOT unblock dropping `dedup_sim`: the
      secondary-CLIP proposal lane still writes `labeling_sample` and reads
      `label_proposals`.
- [x] **Routing north star ratified (2026-08-28)** — tags exist to ROUTE the expensive vision
      dedup step: when two listings might be the same property, compare kitchen-to-kitchen and
      floorplan-to-floorplan, and skip the rest. That re-ranks the whole programme. Eight
      ROUTING tags carry it (koupelna, kuchyně, technické zařízení, půdorys for byt; + fasáda
      and katastrální mapa for dům/komerční; letecký snímek s ohraničením for pozemek; garáž
      for ostatní) — exactly the eight already flagged `priority`, all eight already defined.
      Consequences: **no taxonomy restructure** (tags get TIERED — routing / border-guard /
      parked — not rebuilt), and the `smíšený městský` / `mimoměstský` / `zahrada` work is
      **cancelled**: those describe a photo's SETTING, and no dedup decision pairs two listings
      on "both look urban". `fasáda ∪ vyznačení bytu na fasádě` and `katastrální ∪ letecký
      s ohraničením` become router-config unions, never tag merges.
- [x] The 67,654 surviving `backfill_442` manufactured negatives DELETED (operator-approved).
      Every deletion is recorded in `image_tag_label_events`, so the record survives the rows.
      The store is now 1,522 rows of pure human judgement (1,453 pos / 55 neg / 14 excluded)
      and **absence means untouched, universally** — the premise every later wave assumes.
- [x] CLIP encoder PINNED (#1221, migration 456) — `image_clip_embeddings` is keyed
      `(image_id, model)` where `model` is a NAME, passed to `from_pretrained` with no
      revision, so it resolved to whatever the hub head held at download time. An upstream
      re-upload would have changed every vector written afterwards while the `model` column
      stayed byte-identical: two incomparable populations inside 10.36M rows, invisible at
      runtime, every centroid quietly wrong. Now pinned by sha, REQUIRED (a missing revision
      raises), stamped per row, and guarded by an AST test over every `from_pretrained` call
      site. Nothing trains until this holds.
- [x] Candidate draws honour each tag's property types (#1222, migration 457) — **the first
      live draw was the bug report**. `interier - koupelna`, count=120, returned 54 rows:
      pozemek 24, komercni 18, ostatni 12, byt 0, dum 0. The three least relevant quotas
      filled EXACTLY while the two that matter landed nothing, so 100% of that sample was
      review time spent where bathrooms essentially do not occur. Two causes: a FIXED global
      `CATEGORY_MIX` for every tag (right for a tag with no opinion about property type, wrong
      for every tag that has one — `tag_taxonomy.routing_categories` now scopes and
      renormalises it), and GREEDY budget ceilings (each category could claim the whole
      remaining budget; since the loop runs smallest-quota-first the starved ones were always
      byt and dum — now a fair share that still rolls unused time forward). Not a cause but
      worth recording: retrieval never used the old zero-shot CLIP *tags*, only the raw
      embeddings ranked against a centroid of the operator's own positives.
- [x] Dispatchable draw lane (#1223) — the draw was reachable only from a synchronous admin
      request whose whole-call budget is 45s, while the `byt` pool query alone measures ~14s
      (bitmap heap scan of 320,909 listings sorted by `random()`). The lane removes the
      ceiling and the need for an operator with a browser open. Also fixes an exit code that
      returned 0 even when every tag raised — on a dispatched lane that is the only failure
      signal there is. Cron deliberately commented out.
- [x] **W2 SHIPPED (#1228-#1231, migrations 458+459)** — the sealed exam exists end to end.
      458 stores MEMBERSHIP, not answers: an operator's verdict IS a human judgement
      about an (image, tag) pair, so it goes in `image_tag_labels` through the existing
      upsert and this table records only which images are protected. The cost of that
      single-write-path choice is that every training read owes an exclusion it cannot
      discover from the schema — discharged by ONE constant and policed by
      `tests/test_holdout_exclusion_census.py`, which fails on any statement reading
      `image_tag_labels` that neither excludes nor is censused with a reason. The
      obvious narrower guard ("joins labels to embeddings") would have covered 4 of 19
      statements and, worse, would have missed the trainer it exists for: a trainer
      naturally SELECTs labels and embeddings separately and joins them in numpy.
      MEMBERSHIP ALONE PROTECTS — `sealed_at` means "finished", never "protected", or
      the whole drawing window is open. The draw is two frames: 100 pure-random (probe
      by random id, NOT TABLESAMPLE — a page of `images` is one listing's photos, so a
      block sample would be ~25 listings seen four times) and 150 stratified on
      gpt-5-mini's guesses, each row carrying the odds it was drawn under so statistics
      are inverse-probability weighted. STRATIFY NEVER FILTER: `screen_none` keeps a
      non-zero share, and a screener ERROR is never binned as "saw nothing". The
      screening lane CALIBRATES before it spends — gpt-5-mini bills reasoning as output
      at $2.00/M and the repo had no measured per-image cost, so the pre-flight cap
      refuses to start on an unmeasured rate. The exam screen is deliberately unlike
      the labeling grid (one large image, permanent key legend): that grid binds 1-4 to
      STATES while the exam binds 1-8 to TAGS, same operator, adjacent pages.
- [x] **Exam iteration-2 upgrades (2026-08-30, migration 461)** — set_2 extended to 8 tags
      (operator's four names; "vstupní dveře" seated as BOTH entrance tags, 2+19, since the
      taxonomy splits the door by side); exam keys became the letter grid w e i o / s d k l /
      y x n m (Czech QWERTZ digits are shifted; letters also can't collide with the labeling
      grid's digit-to-state habit), set cap 10→12 where the keys run out; and machine
      suggestions ON per operator ruling — each exam image pre-run through gpt-5-mini
      (`tag_exam_suggestions` + suggest action on the screen lane), rendered as a subtle dot,
      never a pre-filled verdict, served only when the stored answer matches the sitting's
      exact question list. Anchoring cost recorded in PROGRAM.md's ledger; suggested-vs-final
      stays auditable per cell. Shared worker engine extracted to `toolkit/vision_batch.py`.
      Follow-ups same day: Exam in the new-dedup nav (#1241) and an **exam review subpage**
      (`/new-dedup/exam/review`) — every answered image in a list, same click semantics,
      every correction re-answering the whole image through the exam's own /answer route
      (one write path; review can never produce a row shape the exam could not).
- [x] **Cohort purposes + draft labels (2026-08-31, migration 464)** — the exam UI now serves two
      roles: 'holdout' cohorts stay the excluded, weighted yardstick (exam_v1 untouched);
      'curated' cohorts re-seat the operator's draft-marked images for careful re-labeling whose
      answers feed training. Pre-exam labels demoted to `human_draft` (never win an upsert, read
      by no truth path); candidate queues cleared; warm-up made cohort-blind; gold_v1 = up to
      20/tag across all 16 flagged categories, rarest-first.
- [x] **One ruleset on every tag + the definition-driven machine review (2026-09-04, migration
      467)** — the operator's left-out-vs-negative audit rewrote all 18 definitions in the same
      words (subject = what the definition says, never the object inside it; three tiers on every
      space tag; exclusivity on every document tag; fasáda = one building even in a joined block;
      a closed garage door is negative). `review` action on the screen lane: every exam image
      judged against ALL definitions in one call, verdicts in `tag_exam_machine_reviews` (never
      labels; provenance frozen; stale never served), shown on the review page as per-row
      proposals with apply (whole-image /answer) / keep mine (dismiss). PROGRAM.md ledger has the
      full ruleset.
- [x] **The agreement gate + bulk machine labeling (2026-09-04, migration 468)** — the gate
      (`scripts/exam_agreement.py`) scores the machine review per head against the human exam
      answers, abstentions reported apart and never as negatives; the labeler
      (`scripts/label_images.py`, lane `label_images.yml`) then labels ONLY heads named
      explicitly, into `image_tag_labels` as machine cells stamped with the definition that
      produced them, never touching an exam member and never writing a failed call as a
      negative. Operator direction: thin heads (wc, parkoviště) are left alone — the LLM builds
      the training sets for the heads already defined.
- [x] **Training sets BUILT (2026-09-05): 10,544 images, 12 heads, ~$31 of $50.** Gate read on
      553 reviewed exam images; fasáda's entrance boundary promoted from advice to law
      (0.60→0.95 precision). Draw yields measured: operator drafts 96%, CLIP near-tag 41%,
      random 0.9% (garáž). Review surface `/new-dedup/training-set` + `tag_label_notes` (mig
      473) so a changed mark carries its reason into the next definition revision — distilled
      as ONE rule per batch, never one line per note.
- [x] **Adding a head is a repeatable process (2026-09-07)** — runbook in PROGRAM.md, verified
      on `podklad - property list` (head 13, no exam). Two gaps closed: routing categories are
      set from the Taxonomy page (was migration-only, 457), and the CLIP draw can seed a NEW
      head from a relative's positives (`--near-tag <relative>`).
- [x] **Head 13 built: `podklad - property list` (2026-09-07, ~$1.80, no exam)** — 412
      positives / 2,198 negatives from a půdorys-seeded draw, its drafts, a self-seeded draw and
      a random slice; 300 in set awaiting review. Spend ≈ $33 of $50.
- [x] **Head 14 built: `podklad - 3d plán` (2026-09-08, $5.57, no exam)** — 251 positives /
      10,293 negatives over the SAME 10,544 images půdorys was judged on (the new `--like-tag`
      draw, #1335), so the boundary between the two is reviewable on the same photos. Operator's
      precedence written on all three sides: property list > 3D plán > půdorys. Neighbours bumped
      with it (půdorys v10, property list v6 — narrowed), so both now read "old wording"; neither
      re-judged. **135 images are still positive on both 3D plán and půdorys** — the old wording's
      residue, resolvable by a ~$5.5 re-judge of půdorys or by hand. Spend ≈ $32 of $50.
- [ ] **NEXT — train the CLIP linear probe on the 14 heads; evaluate the 12 gated ones on exam_v1;**
      spend the remaining ~$18 where the probe's per-head numbers show labels actually help.
 Then choose the sampling
      strategy WITH those numbers: ~$47 of the $50 buys ~4-8k labeled images once, and a random
      draw spends most of it on heads that are already strong.
- [ ] **NEXT — W1 remainder:** one definition renderer with two outputs (the machine prompt and
      a plain-language handbook card, so the operator never meets `counts` / `does_not_count` /
      `confusable_with` / `leave_out_when` while labeling); the machine-label store (a SEPARATE
      table — `image_tag_labels`' human-wins upsert SUPPRESSES a machine write onto a human
      cell, which is precisely the disagreement worth surfacing); the sealed 250-image exam
      (100 pure-random + 150 LLM-stratified with recorded inclusion odds, so the four rare tags
      are measurable at all); and a derived per-tag lifecycle so adding parkoviště/wc later is
      a Tuesday. Sprint budget: **$50 hard ceiling**, gpt-5-mini.
- [x] RunPod client (`scripts/runpod_client.py`, #972/#975/#977) — launch/poll/terminate an
      on-demand pod, live cheapest-GPU catalog lookup, capacity fallback. Guaranteed teardown
      verified across 4 real live dispatches (3 zero-cost, 1 real ~1.7¢ pod rental).
- [x] RunPod end-to-end proof — a real pod (`g39f02wj642her`, RTX 3070) launched, billed, and
      was cleanly torn down. Pod status/logs turned out unreliable for on-demand Pods (design
      note left for Wave 5, which should have its job write results to Postgres/R2 instead of
      relying on either) — not a blocker for W1's actual goal, which was proving the launch→
      bill→terminate pipeline itself works.
- [x] **DINOv3 readiness build (2026-09-05, PROGRAM.md ledger (f); closed out 2026-09-08 (c)):**
      vector table (migration 480, #1296 — **applied live 2026-09-08**), bake-off harness (#1300),
      production embedding job (#1298), and the per-tag heads trainer (#1297) — all merged
      2026-09-06. Licence accepted by the operator on Hugging Face (2026-09-05), the checkpoint
      revision pinned, the manifest lane dry-run proven against live data. Nothing has embedded
      or trained yet: the bake-off pod run (~$2, the operator's call) still has to choose
      resolution/preprocessing/dtype, and training waits on the training set (one category
      still open).
- [ ] **NEXT — run the bake-off** (manifest lane with `dry_run=false`, then the pod-side
      harness by hand with `HF_TOKEN`), read ENCODER-DECISION.md §5.4's seven readouts with the
      operator, fill the three remaining nulls in `data/dinov3_config.json`, then a small-limit
      embedding pass before any corpus pass. **Partly answered by the tagging bake-off's run 1
      (2026-09-09, ledger (a)): `dtype` and the measured end-to-end throughput §5.3 asked for are
      now in hand; near-duplicate accuracy (Set 2) is not, and it is the readout the encoder
      choice actually rests on.**
- [x] **Tagging bake-off, phase 1 — the durable half (2026-09-08, PROGRAM.md ledger (d)):**
      ENCODER-DECISION.md §5.2 "Set 1" made runnable. Migration 489 adds the results store in
      `dedup_sim` (`tag_head_bakeoff_runs` / `_arms` / `_vectors` / `_scores` / `_metrics`; the
      vector column is an UNMODIFIED `halfvec`, because arms differ in width). `toolkit/tag_heads.py`
      gained an injectable vector source (the production `image_dinov3_embeddings` reader stays the
      default) and two positive-only training modes beside the existing `pos_neg` —
      `pos_only_free_neg` (other heads' positives stand in as free negatives) and
      `pos_only_centroid` (cosine to the positives' mean, negatives touch only the threshold) —
      because the operator's "positive training only" admits both readings and the point is to show
      them side by side. `toolkit/tag_head_bakeoff.py` + `scripts/tag_head_bakeoff.py` run the
      arm x mode x head cross product on CPU, resumable per cell, grading the sealed exam by the
      ratified rule (`exam_machine_review.human_verdict`, now extracted so the two graders cannot
      drift). Read surface: `/new-dedup/tagging-bakeoff/{runs,runs/{id}/metrics,runs/{id}/images,
      runs/{id}/buckets}`, admin-gated and read-only — **two more routes since phase 2c below,
      six in all**.
- [x] **Tagging bake-off, phase 2a — the comparison page (2026-09-08, PROGRAM.md ledger (e)):**
      `NEW DEDUP · Tagging bake-off` at `/new-dedup/tagging-bakeoff`, a pure consumer of the four
      routes above. A matrix of head x (arm x mode) with F1 leading and the graded n always beside
      it; view A, photographs each carrying what every selected arm said about them; view B, one
      head's four outcome buckets in plain words (caught / wrongly caught / missed / correctly
      rejected) under a 20-bin histogram with the threshold marked. Both contract conventions are
      rendered rather than assumed — a null rate reads "nothing proposed", never zero, and an exam
      abstention is counted beside the split and folded into no rate. Every selector is in the URL.
- [x] **Tagging bake-off, phase 2b — the GPU lane, and RUN 1 IS DONE (2026-09-09, PROGRAM.md
      ledger (a)):** the embed job that fills `dedup_sim.tag_head_bakeoff_vectors` per arm plus
      its workflow shipped over seven pod attempts on 2026-09-08 (post-mortems in ledger (f)–(j):
      a clone that could never work, a bootstrap that could not report itself, a crash loop over
      an undersized disk, a missing `torchvision`, and two correct-but-deadlocked definitions of
      `failed`). **Run 1 completed: 11 heads (`review_state='ready'`) x 11 arms x 3 training
      modes = 363 cells, 9,264 training photos + the sealed 250-photo exam (0 exam photos in any
      training tray, verified), 529,188 per-photo scores, 0 ungradable, ≈$1.55 of GPU all in.**
      Headline measured results — F1 is the balance of precision (of what it flagged, how much
      was right) and recall (of what was there, how much it found): **bf16 = fp32 to three
      decimals on every arm** at 1.6-2.7× the speed (the `dtype` null is answered);
      **resolution on DINOv3-B is flat in cross-validation and worth +0.036 exam F1 from 512 to
      1024 at 4.8× the compute**; **DINOv2-L (Apache-2.0) posts the best exam F1 (0.758) and is
      the fastest DINO arm** — on the TAG job only, the near-duplicate job the encoder was
      actually chosen on is still unmeasured; **leaving the incumbent CLIP buys +0.06 mean CV
      F1**, concentrated on 3d plán, obývací pokoj, letecký snímek and technické zařízení; the
      operator's negative labels buy **+0.055 CV / +0.074 exam** over free borrowed negatives,
      ≈0 on documents but +0.22 on property list. And the corpus arithmetic, now measured
      end-to-end rather than GPU-only: **11.5M images ≈ $27 (DINOv2-L) / $34 (@512) / $59 (@768)
      / $163 (@1024)** on a $0.22/hr 3090 — far above ENCODER-DECISION.md's $1-12 band, which
      was synthetic-tensor throughput. Results are browsable at `/new-dedup/tagging-bakeoff`.
- [x] **Tagging bake-off, phase 2c — the operator's rulings of 2026-09-09, and the product
      contract they settle (PROGRAM.md ledger (c)):** **tags are assigned WINNER-TAKES-ALL** —
      every head scores the photo, the highest score names the tag, and **the product takes no
      per-head yes/no decision at all**. The winner is **recomputed from whatever heads a run
      holds, never stored** (heads keep being added, and a stored winner would be a fact about
      yesterday's head set), and it is only a winner **within one (arm, mode, split)**, since two
      arms are two models and the modes do not share a scale. The page gained the two views that
      make this readable: **view C**, one head's whole ranking unbucketed, sorted by the head's
      SCORE (F1 is one number per head and cannot order photographs), and a **per-photo
      probability panel** listing every head's raw score strongest-first with the winner marked.
      **The retirement is enforced twice, deliberately:** on the page as a display filter
      (`RETIRED_MODES` / `MIN_LIVE_RESOLUTION = 504`, with a toggle that brings the retired rows
      back — nothing is deleted), and in the LANE as a default (`scripts/tag_head_bakeoff.py`
      trains `pos_neg` only, `tagging_bakeoff_manifest.build_arms` mints no arm below 504 px, and
      the workflow gained a `modes` input), so the next default dispatch cannot re-embed a retired
      arm on a rented GPU or re-train a rejected mode. **Naming one still runs it**
      (`arms=clip-b32-stored` — the zero-GPU incumbent baseline — or `modes=pos_only_centroid`),
      because ruling (c) keeps the training set, head set, model and parameters iterating and a
      narrowed default must not become a locked door. Read surface now **six** routes:
      `.../runs/{id}/scores` (one head's ranking, keyset-paged) and
      `.../runs/{id}/images/{image_id}` (one photo across every arm, mode, head and split).
- [x] **Tag model, iteration 1 — the versioned model + the winner store (2026-09-09, PR #1366;
      PROGRAM.md ledger entry `2026-09-09 (b)`, which also lists the day's ruling in
      full):** the bake-off produced evidence; this is where a chosen cell of it becomes
      something the product can use. Migration 490 adds three PUBLIC-schema tables —
      `tag_head_models` (one frozen decision: encoder, training mode, head set, weights, under a
      version name like `v1`; a partial unique index makes two active models impossible),
      `tag_head_model_heads` (the `toolkit/tag_heads.py` artifact plus the bake-off's cv/exam
      numbers COPIED at promotion) and `image_tag_scores` (one row per image per version: every
      head's probability, the winner tag, its score). Public and NOT `dedup_sim`, with no foreign
      key into it, because that schema is dropped wholesale at Wave 8 and this store outlives it.
      **The operator's rule (2026-09-09), which decides the shape: the tag is the head that scored
      HIGHEST — no per-head yes/no anywhere.** So every head's score is stored, no threshold and no
      boolean is (a consumer applies its own floor to `winner_score`), ties break toward the lower
      tag id, and **adding a head is a new version rather than an edit** — an argmax is only
      meaningful over one head set, so the set is frozen on the model and two versions can hold
      different answers for the same photo. The loop is
      `a new bake-off run -> promote -> score -> activate`: a promoted version is a `candidate`
      nobody reads, scoring is resumable and pure-Python (no ML library at inference), and
      **activation is a separate explicit step** so no consumer ever meets a half-scored version.
      `toolkit/tag_models.py` + `scripts/tag_model.py` + `tag_model.yml` (dispatch-only, dry-run by
      default, CPU, `SUPABASE_DB_URL` only); read surface
      `/new-dedup/tags/{models, models/{version}/heads, images/{image_id}}`, admin-gated.
      **v1 IS NOW LIVE (2026-09-09, PROGRAM.md ledger `(d)`).** Migration 490 applied to
      production ~08:45 UTC (public schema, row-level security on, the browser-facing `anon` /
      `authenticated` roles revoked — verified after applying); #1365, #1366 and #1368 merged in
      that order and the page rollout confirmed on Railway. **v1** was promoted from bake-off
      run 1, arm `dinov3-b16@768/bf16`, mode `pos_neg`, **11 heads** (tag ids 3, 17, 22, 25, 28,
      39, 42, 43, 45, 46, 48) refit on all their training rows, each head's bake-off numbers
      copied onto the model (katastrální mapa 0.969, půdorys 0.961, obývací pokoj 0.918, 3d plán
      0.909, letecký snímek 0.881, technické zařízení 0.873, property list 0.687). It **scored
      9,514 images** — 9,264 training photos + the sealed 250-photo exam — from the bake-off
      arm's own vectors (`source bakeoff:1`), **0 missing, in ~70 s on a free runner** (inference
      is a dot product and a logistic in plain Python: no GPU, no bill), and was **activated** —
      it is THE active model, read by `toolkit.tag_models.winners()` and
      `GET /new-dedup/tags/images/{id}`. All three `tag_model.yml` stages ran with
      `dry_run=false`. **The winner rule's first grade, on the sealed exam:** of the 94 exam
      photos carrying a human positive on one of the 11 heads, **the winner names that tag
      95.7%** of the time (against a mean per-head exam F1 of **0.73** under independent yes/no
      decisions — competition between heads removes most cross-tag false alarms), and 96.8% of
      those clear a 0.5 score. **The open problem is "none of the above":** of the 156 exam
      photos with no positive on any of the 11 heads, all get a winner by construction and
      **41% get one scoring ≥ 0.5**. So a **floor** — a minimum the consumer insists on before
      believing the tag, not a per-head yes/no — is not optional in practice: 0.5 keeps 96.8% of
      the true tags and cuts the confident-looking wrong ones from 100% to 41%; a higher floor
      trades the two. Training-pool figures are **in-sample** and read as optimistic: 88.4% of
      the 2,940 training photos with a positive get it as winner, 72% of all 9,514 clear 0.5,
      mean winner score 0.70. **A measurement gap:** the bake-off cross-validated each head only
      on its OWN training rows, so no winner can be graded out-of-fold across the training pool
      (restricting it to that subset returns a meaningless 99.8%). Still **not done, on purpose**:
      no corpus pass (the production source needs `image_dinov3_embeddings` under the model's
      seven encoder facts and nothing populates it yet), the three nulls in
      `data/dinov3_config.json` (v1's identity lives in the registry, not that config), and the
      encoder decision for the near-duplicate job (#1300's harness still unrun).
- [ ] **NEXT — the four decisions v1's first grade puts in front of the operator** (listed, not
      decided; PROGRAM.md ledger `(d)`): (1) **the winner floor — or an "other" head instead**,
      the two answers to the 41% "none of the above" number, and they are not exclusive: a floor
      is a consumer's minimum on `winner_score`, an "other" head is a twelfth head trained on
      exactly those photos so the argmax has somewhere honest to put them; (2) **which arm
      iteration 2 promotes** — `dinov2-l14-reg@504/bf16` has the best exam F1 and is the fastest
      DINO arm, `dinov3-b16@768/bf16` is the programme's accepted encoder and is what v1 froze,
      and neither can be settled on tagging numbers alone (readout 3); (3) **property list's
      definition**, still 0.687 with high recall and poor precision — the shape of a definition
      admitting too much, not of a training failure; (4) **a fresh exam cohort covering all 11
      heads** — the current one cannot grade `3d plán` or `property list` at all (both post-date
      it) and answered `garáž` and the document tags under older definitions, so 94 gradable
      photos is a thin foundation for the 95.7% headline and this is the cheapest way to thicken
      it.
- [ ] **NEXT — run 2 grades the winner OUT-OF-FOLD.** The bake-off validated each head only on
      its own training rows, so today the winner rule has an honest grade on the exam's 94
      photos and nothing else. Run 2 should score **every image with every head out-of-fold**
      under **one shared grouped split used by all heads** (grouped so no photo is ever scored
      by a model that saw another photo from its listing), which gives the winner rule a
      training-pool grade beside the exam instead of resting on it alone.
- [ ] **NEXT — read run 1 with the operator, then turn the cheap knobs.** In order: (1) look at
      the **false-positive buckets in view B** before tuning anything — the exam column is a
      *prevalence* story (250 random photos hold few positives of a rare tag: garáž 2 true
      against 7 false, katastrální mapa 10 against 9; technické zařízení has 0 exam positives so
      its metrics are correctly NULL; 3d plán and property list are post-exam and have no
      gradable cells), and some "errors" will be label mistakes worth re-judging first;
      (2) **per-head thresholds** off each head's own precision/recall curve — heads trained at
      ~1:3 pos:neg and judged at 0.5 over-fire at natural prevalence, and this costs no
      re-embedding and no re-training. **A MEASUREMENT knob only, since phase 2c:** the product
      assigns tags winner-takes-all and reads no head's yes/no, so a threshold now only sharpens
      what the experiment REPORTS (the F1s, the confusion square, the wrongly-caught pile) and is
      not a step towards shipping; (3) **property list's definition** — the one weak head at
      0.69 CV F1 (precision 0.57 / recall 0.87), a shape that reads as a definition admitting too
      much rather than a training failure; (4) **fill `resolution` in `data/dinov3_config.json`**
      (`preprocessing` was already ruled `letterbox_pad`, `dtype` is answered by the data) — now
      a $34-vs-$163 decision; (5) **run the near-duplicate bake-off (#1300's "Set 2")** before
      concluding anything about the encoder choice from the tagging numbers.

- [ ] **PARALLEL TRACK — accuracy keeps iterating; the shape does not.** The operator is content
      with the cost, licence and speed of both DINOv3 and DINOv2 and is **not yet content with
      accuracy** (the 2026-09-09 ruling, **ask 5 "keep iterating"** — the ruling's five asks are
      listed in full in PROGRAM.md's `2026-09-09 (b)` entry; cite them by NUMBER, because the
      letters in a ledger heading are that day's entries, not the ruling's parts). So the
      training set, the head set, the model and the parameters go on changing together. Two
      things follow. (1) **Narrow the experiment** (**ask 2**, shipped as **PR #1365**, merged 2026-09-09): drop
      the two weak training modes — `pos_only_free_neg` (the "borrowed no") and
      `pos_only_centroid` (the "closeness only") — and every arm below 512 px, keeping dinov2's
      504 because that IS 512 snapped to its patch size. (2) **Each improvement is just the next
      version**: promote, score, activate, and the previous version stays readable for
      comparison. **The full 11.5M image pool is scored only when the operator is satisfied** —
      until then scoring runs over the bake-off arm's 9,514 labelled + exam photos, which costs
      nothing. The same ruling's **asks 3 and 4** (per-head probabilities when zooming into a
      photo; a third view listing every photo a head scored, sorted by score) shipped as
      **PR #1368**, merged the same day.

Waves W2-W8 (candidate selection through production wiring) are not started; see PROGRAM.md.

## Data-quality prerequisite (operator-run, parallel to the code work)

Flagged during W0 recon (2026-08-05): **122,920 listings sit on 4,206 coordinate pins shared by
>10 listings each** (town/municipality centroids used as a geocoding fallback), with the biggest
single pin holding 1,029 listings. This is a candidate-storm risk for any future geo-proximity
fallback rung (L0's third path) and degrades geo-based candidate generation generally. The
operator is investigating this in separate data-quality sessions (per-portal geocoding /
enrichment fix, cost-aware) — not blocked on or blocking the code removal above, but a
precondition for trusting L0's geo-fallback path once built. The clique guard question (whether
candidate generation needs an explicit "too many pins share this exact point" veto) is **parked**
pending that investigation; see PROGRAM.md's decisions ledger.
