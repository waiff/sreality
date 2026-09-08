-- 489: the tagging bake-off results store (docs/design/new-dedup/ENCODER-DECISION.md
-- §5.2 "Set 1" + §5.4 readout 4; docs/design/new-dedup/PROGRAM.md ledger 2026-09-08 (d)).
--
-- WHAT THIS IS. One experiment: train one yes/no head per photo tag on frozen
-- embeddings from SEVERAL encoder configurations ("arms"), under three training
-- modes, score every labelled image under every arm x mode, and keep the numbers
-- AND the per-image scores so a human can look at the photos behind a number
-- rather than only at the number.
--
-- WHY schema `dedup_sim` AND NOT public. This is experiment evidence, not
-- production state. Migration 372 created `dedup_sim` precisely so a wave's
-- evidence is droppable wholesale (`drop schema dedup_sim cascade`, Wave 8)
-- without a single reference from production code paths. The heads themselves,
-- when one is adopted, ship as a JSON artifact (toolkit/tag_heads.py) — nothing
-- in a live read path ever joins these tables.
--
-- WHY THE VECTORS LIVE HERE AND NOT IN image_dinov3_embeddings. That table's
-- primary key is `image_id + the seven identity facts` and its column is
-- `halfvec(768)` — a fixed width, because production has exactly one encoder.
-- A bake-off arm may be 512, 768 or 1024 dimensions, so the bake-off's own store
-- declares `halfvec` WITHOUT a dimension (pgvector permits an unmodified halfvec
-- column; the width is then a property of each value). Keeping the experiment's
-- vectors out of the production table also means the GPU job cannot pollute the
-- production population with an arm that loses.
--
-- WHY A `vectors` TABLE AT ALL, rather than reading image_dinov3_embeddings. An
-- arm is a hypothetical: most of them will never be adopted, and half are not
-- even DINOv3. The trainer reads its vectors through an injectable source
-- (toolkit/tag_heads.VectorSource), whose default is still the production table;
-- the bake-off runner injects a reader over this one.
--
-- SCORES ARE PER IMAGE, ON PURPOSE. A precision figure says how often a head is
-- wrong and never which photo it was wrong about. §5.4 readout 3 ("the twenty
-- worst pairs, side by side") is the same demand one level down: the bake-off
-- page shows the true-positive / false-positive / false-negative / true-negative
-- buckets as tiles. That needs one row per (arm, mode, tag, image, split), which
-- is what `tag_head_bakeoff_scores` is.
--
-- SPLITS. 'cv' rows are OUT-OF-FOLD scores over the training population (grouped
-- by listing_id, so a head never grades a photo of a listing it trained on).
-- 'exam' rows are the sealed exam_v1 holdout, scored by the head refit on all
-- training rows. `label` is nullable in the exam split ONLY: the ratified grading
-- rule (scripts/exam_agreement.py) says a cell grades only when both sides said
-- yes or no, so an abstention is stored as a real score with a NULL label rather
-- than being dropped — the page can still show the photo and the score, and the
-- metrics still exclude it.
--
-- Guarded exactly like migration 480: the halfvec column cannot be PARSED where
-- pgvector is absent, so every statement is deferred into a DO/EXECUTE that only
-- fires when the extension is available. CI's migration replay installs pgvector
-- (.github/workflows/migrations.yml), so these tables DO get built there.
--
-- Security posture in the same migration, at creation, per 237/447: RLS enabled
-- with zero policies, anon/authenticated DML revoked. All five relations are
-- registered in tests/test_migration_rls_grants.py::_ADMIN_ONLY_RELATIONS and
-- tests/test_tenant_isolation_live.py::_ADMIN_ONLY_RELATIONS. There is no
-- `_public` view and none is planned — the bake-off page reads through the
-- admin-gated API (`/new-dedup/tagging-bakeoff/*`), never over supabase-js.
--
-- ci-allow-dynamic: tag_head_bakeoff every CREATE TABLE, index, RLS enable and
-- REVOKE below lives inside this DO block's EXECUTE strings (the pgvector
-- availability guard of migrations 226/480), so the offline statement scanner in
-- tests/test_migration_rls_grants.py cannot see any of them. Checked by hand
-- instead: tests/test_tag_head_bakeoff_migration.py asserts each statement is
-- present in this file's text.
do $$
begin
  if exists (select 1 from pg_available_extensions where name = 'vector') then
    create extension if not exists vector;

    ----------------------------------------------------------------
    -- runs — one bake-off. `heads` freezes WHICH tags were selected
    -- at plan time: `min_train_positives` alone would re-select a
    -- different set a week later, and a run must stay readable.
    ----------------------------------------------------------------
    execute $sql$
      create table if not exists dedup_sim.tag_head_bakeoff_runs (
        id                  bigserial primary key,
        created_at          timestamptz not null default now(),
        label               text not null,
        note                text,
        status              text not null default 'running'
                            check (status in ('running', 'ok', 'failed')),
        manifest_key        text,
        heads               bigint[] not null default '{}',
        min_train_positives integer not null default 100
      )
    $sql$;

    ----------------------------------------------------------------
    -- arms — one encoder configuration. The seven identity facts are
    -- the SAME seven image_dinov3_embeddings keys on (migration 480),
    -- carried as plain columns so an arm can be compared to the
    -- production population field by field. `arm` is the short human
    -- name the page renders ("dinov3-b16@768/bf16"); it is unique per
    -- run, never globally, so two runs may reuse a name.
    ----------------------------------------------------------------
    execute $sql$
      create table if not exists dedup_sim.tag_head_bakeoff_arms (
        id            bigserial primary key,
        run_id        bigint not null
                      references dedup_sim.tag_head_bakeoff_runs(id) on delete cascade,
        arm           text not null,
        model         text not null,
        revision      text not null,
        library       text not null,
        pooling       text not null,
        resolution    integer not null check (resolution > 0),
        preprocessing text not null,
        dtype         text not null,
        dim           integer check (dim > 0),
        status        text not null default 'pending',
        note          text,
        created_at    timestamptz not null default now(),
        unique (run_id, arm)
      )
    $sql$;

    ----------------------------------------------------------------
    -- vectors — written by the GPU embedding job (phase 2), read by
    -- the CPU trainer. `embedding halfvec` carries NO dimension: the
    -- arms differ in width by design.
    ----------------------------------------------------------------
    execute $sql$
      create table if not exists dedup_sim.tag_head_bakeoff_vectors (
        arm_id     bigint not null
                   references dedup_sim.tag_head_bakeoff_arms(id) on delete cascade,
        image_id   bigint not null references images(id) on delete cascade,
        embedding  halfvec not null,
        created_at timestamptz not null default now(),
        primary key (arm_id, image_id)
      )
    $sql$;

    ----------------------------------------------------------------
    -- scores — one row per (arm, mode, tag, image, split).
    ----------------------------------------------------------------
    execute $sql$
      create table if not exists dedup_sim.tag_head_bakeoff_scores (
        arm_id    bigint not null
                  references dedup_sim.tag_head_bakeoff_arms(id) on delete cascade,
        mode      text not null,
        tag_id    bigint not null,
        image_id  bigint not null references images(id) on delete cascade,
        split     text not null check (split in ('cv', 'exam')),
        fold      smallint,
        label     smallint check (label in (0, 1)),
        score     real not null,
        predicted boolean not null,
        primary key (arm_id, mode, tag_id, image_id, split)
      )
    $sql$;

    -- The bucket view asks "the highest-scoring rows for this one
    -- arm/mode/tag/split", paging down. Score descending is the
    -- ordering it pages in.
    execute $sql$
      create index if not exists tag_head_bakeoff_scores_bucket_idx
        on dedup_sim.tag_head_bakeoff_scores
        (arm_id, mode, tag_id, split, score desc)
    $sql$;

    -- The image view pivots the other way: one image, every head.
    execute $sql$
      create index if not exists tag_head_bakeoff_scores_image_idx
        on dedup_sim.tag_head_bakeoff_scores (arm_id, mode, split, image_id)
    $sql$;

    ----------------------------------------------------------------
    -- metrics — one row per (arm, mode, tag). Precision/recall are
    -- NULLABLE, never 0.0, when nothing was proposed: "no positive was
    -- ever proposed" and "every proposal was wrong" are opposite facts
    -- (toolkit/exam_machine_review.scored says the same in Python).
    -- Every count needed to recompute a Wilson bound is stored, so a
    -- reader never has to trust a bare proportion.
    ----------------------------------------------------------------
    execute $sql$
      create table if not exists dedup_sim.tag_head_bakeoff_metrics (
        arm_id           bigint not null
                         references dedup_sim.tag_head_bakeoff_arms(id) on delete cascade,
        mode             text not null,
        tag_id           bigint not null,
        n_pos            integer not null,
        n_neg            integer not null,
        n_groups         integer,
        cv_precision     double precision,
        cv_recall        double precision,
        cv_f1            double precision,
        cv_graded_n      integer not null default 0,
        cv_tp            integer not null default 0,
        cv_fp            integer not null default 0,
        cv_tn            integer not null default 0,
        cv_fn            integer not null default 0,
        exam_precision   double precision,
        exam_recall      double precision,
        exam_f1          double precision,
        exam_graded_n    integer not null default 0,
        exam_abstained_n integer not null default 0,
        exam_tp          integer not null default 0,
        exam_fp          integer not null default 0,
        exam_tn          integer not null default 0,
        exam_fn          integer not null default 0,
        threshold        real not null,
        dataset_hash     text not null,
        status           text not null default 'ok',
        note             text,
        trained_at       timestamptz not null default now(),
        primary key (arm_id, mode, tag_id)
      )
    $sql$;

    execute 'alter table dedup_sim.tag_head_bakeoff_runs enable row level security';
    execute 'alter table dedup_sim.tag_head_bakeoff_arms enable row level security';
    execute 'alter table dedup_sim.tag_head_bakeoff_vectors enable row level security';
    execute 'alter table dedup_sim.tag_head_bakeoff_scores enable row level security';
    execute 'alter table dedup_sim.tag_head_bakeoff_metrics enable row level security';

    execute 'revoke all on dedup_sim.tag_head_bakeoff_runs from anon, authenticated';
    execute 'revoke all on dedup_sim.tag_head_bakeoff_arms from anon, authenticated';
    execute 'revoke all on dedup_sim.tag_head_bakeoff_vectors from anon, authenticated';
    execute 'revoke all on dedup_sim.tag_head_bakeoff_scores from anon, authenticated';
    execute 'revoke all on dedup_sim.tag_head_bakeoff_metrics from anon, authenticated';

    execute $sql$
      comment on table dedup_sim.tag_head_bakeoff_scores is
        'Per-image out-of-fold (split=cv) and sealed-exam (split=exam) scores for '
        'one tag head under one encoder arm and one training mode. label is NULL '
        'only in the exam split, where the ratified grading rule '
        '(scripts/exam_agreement.py) abstains on a cell either side left out; the '
        'row is kept so the page can still show the photo and the score.'
    $sql$;
  else
    raise notice 'pgvector unavailable; dedup_sim.tag_head_bakeoff_* skipped (CI replay only). Production has it.';
  end if;
end $$;
