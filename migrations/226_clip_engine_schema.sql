-- Dedup v2 Phase 3 schema: CLIP embeddings store + engine run counters.
--
-- image_clip_embeddings holds the 512-d CLIP vector per (image, model) so the
-- cosine recall tier (stage 4b) can score a candidate pair's like-room images.
-- Deliberately NO ANN index: dedup computes cosine between TWO specific listings'
-- images (exact per-id retrieval + a dot product), never a global nearest-neighbour
-- search, so the pgvector 5M-vector index RAM wall does not apply. Keeping the
-- vectors in DB lets the (torch-free) engine job compute cosine in SQL via `<=>`.
-- Active-listing images are tagged/embedded first, which bounds the footprint to
-- the dedup-relevant set. halfvec is a later size optimization (vector = 2 KB/row).

create extension if not exists vector;

create table if not exists image_clip_embeddings (
  image_id   bigint not null references images(id) on delete cascade,
  model      text   not null,
  embedding  vector(512) not null,
  primary key (image_id, model)
);

-- New per-run counters for the CLIP tiers (additive; existing rows read 0/NULL).
alter table dedup_engine_runs
  add column if not exists clip_classified  integer not null default 0,  -- listings whose room tags came from CLIP (free), not the LLM
  add column if not exists clip_cosine_calls integer not null default 0,  -- room-pair cosine scores computed
  add column if not exists routed_haiku      integer not null default 0,  -- forensic compares routed to Haiku by a high cosine band
  add column if not exists routed_sonnet     integer not null default 0;  -- forensic compares routed to Sonnet (the uncertain band)
