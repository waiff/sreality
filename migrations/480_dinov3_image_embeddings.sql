-- 480: DINOv3 image embeddings — the vector store for the tag heads, Level-3
-- similarity and candidate path B (docs/design/new-dedup/ENCODER-DECISION.md,
-- accepted by the operator 2026-09-05, conditional on the DINOv3 licence).
--
-- WHY A NEW TABLE, NOT A COLUMN ON image_clip_embeddings. Two encoders producing
-- two different, incomparable vector spaces cannot share one table without a
-- caller-forgets-to-filter hazard — exactly the mistake migration 456 patched
-- retroactively onto image_clip_embeddings for a single encoder's pre/post-pin
-- populations. This table is designed to avoid that mistake from day one.
--
-- SIX IDENTITY FACTS, not one model string. ENCODER-DECISION.md §4.1: "a
-- vector's identity is six things: model, revision sha, library+pooling,
-- resolution, preprocessing transform, dtype ... any of them changing means a
-- new population, not a new value." Rather than inventing a synthetic
-- `encoder_id` column that some writer could set inconsistently with the six
-- underlying facts, the six facts themselves are the identity: they are part of
-- the primary key, so a config change produces a NEW row (a new population)
-- instead of silently overwriting a differently-configured vector for the same
-- image. "Were these two vectors made by the same pipeline?" is a `WHERE`
-- clause over six real columns, never an act of faith.
--
-- `revision` is NOT NULL here — unlike migration 456's retrofit onto an
-- already-unpinned CLIP table, this table has no pre-pin population to
-- accommodate: every row gets the pin discipline scraper/clip_tagger.py only
-- grew after the fact (see scraper/dinov3_tagger.py, same rail, from day one).
--
-- halfvec(768): pgvector 0.8.0 is live (halfvec shipped in 0.7.0). At 1,544
-- bytes/row this sits BELOW Postgres's ~2 KB TOAST_TUPLE_THRESHOLD (ENCODER-
-- DECISION.md §2.1) — unlike image_clip_embeddings's vector(512) at 2,056
-- bytes, every read here is a heap fetch alone, no separate TOAST fetch.
--
-- Guarded exactly like migration 226: the CI migration-replay image
-- (postgis/postgis) ships no pgvector, so `halfvec` cannot even be parsed
-- there. The DO block's EXECUTE defers parsing until the extension
-- conditionally exists; production (where pgvector 0.8.0 is already installed)
-- is unaffected, and the table is idempotent so a second apply is a no-op.
--
-- Security posture replayed from migrations 237/447 in the SAME migration this
-- time, not bolted on after an incident: RLS enabled, anon/authenticated DML
-- revoked, at creation. This is a backend-only table — the bake-off harness,
-- the production embedding job and the per-tag heads trainer all connect as the
-- DB owner / service_role (both bypass RLS); no `_public` view exists or is
-- planned.
--
-- ci-allow-dynamic: image_dinov3_embeddings the CREATE TABLE, the index, the
-- RLS enable and the REVOKE all live inside this DO block's EXECUTE strings
-- (the same pgvector-availability guard as migration 226), so the offline
-- statement scanner in tests/test_migration_rls_grants.py cannot see any of
-- them. Verified by hand instead: tests/test_dinov3_embeddings_migration.py
-- asserts every one of those statements is present in this file's text.
do $$
begin
  if exists (select 1 from pg_available_extensions where name = 'vector') then
    create extension if not exists vector;

    execute $sql$
      create table if not exists image_dinov3_embeddings (
        image_id      bigint  not null references images(id) on delete cascade,
        model         text    not null,
        revision      text    not null,
        library       text    not null,
        pooling       text    not null,
        resolution    integer not null check (resolution > 0),
        preprocessing text    not null,
        dtype         text    not null,
        embedding     halfvec(768) not null,
        created_at    timestamptz not null default now(),
        primary key (image_id, model, revision, library, pooling, resolution,
                     preprocessing, dtype)
      )
    $sql$;

    -- Leading with the six identity columns (image_id last) supports the
    -- production job's checkpoint/resume query ("which image ids has THE
    -- CURRENT canonical config already embedded") without touching the
    -- embedding column itself.
    execute $sql$
      create index if not exists image_dinov3_embeddings_encoder_idx
        on image_dinov3_embeddings
        (model, revision, library, pooling, resolution, preprocessing, dtype, image_id)
    $sql$;

    execute 'alter table image_dinov3_embeddings enable row level security';
    execute 'revoke all on image_dinov3_embeddings from anon, authenticated';

    execute $sql$
      comment on table image_dinov3_embeddings is
        'DINOv3 image embeddings (docs/design/new-dedup/ENCODER-DECISION.md). '
        'One row per (image, full encoder configuration) -- the six identity '
        'columns are part of the primary key, so a knob change adds a row '
        'instead of overwriting a differently-configured vector. Backend-only: '
        'RLS enabled with zero policies, anon/authenticated DML revoked '
        '(migrations 237/447 posture, replayed at creation, not bolted on '
        'after the fact).'
    $sql$;
  else
    raise notice 'pgvector unavailable; image_dinov3_embeddings skipped (CI replay only). Production has it.';
  end if;
end $$;
