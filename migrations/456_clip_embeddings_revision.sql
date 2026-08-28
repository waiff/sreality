-- 456: record WHICH weights produced each CLIP vector.
--
-- WHY. `image_clip_embeddings.model` stores a model NAME ('openai/clip-vit-base-patch32'),
-- and until this migration's companion code change that name was passed to
-- from_pretrained() with no revision — so it resolved to whatever the HuggingFace
-- hub's `main` branch held on the day each shard ran. An upstream re-upload would
-- have changed every vector written after it while leaving the `model` column
-- byte-identical: old and new vectors silently incomparable, cosine distances
-- quietly wrong, and NOTHING in the schema able to tell the two populations apart.
-- 10.36M vectors and every per-tag centroid built on them depend on this being one
-- coherent population.
--
-- The code side (scraper/clip_tagger.py) now REQUIRES data/clip_taxonomy.json's
-- `revision` and passes it to from_pretrained. This column records it per row so
-- the question "were these vectors made by the same weights?" becomes a SELECT
-- rather than an act of faith.
--
-- ADDITIVE and NULLABLE on purpose. The join key stays (image_id, model) — every
-- reader (toolkit/tag_candidates.py, toolkit/tag_definitions.py,
-- scripts/retag_from_embeddings.py, scripts/backfill_render_score.py) keeps working
-- untouched. Folding the sha into the key would orphan all 10.36M existing rows
-- from all of them for no gain. NULL means exactly "written before the pin".
--
-- Ops readout:  select model, revision, count(*) from image_clip_embeddings group by 1,2;
--
-- NOT covered here: the labeling secondary encoder, which is a DB setting
-- (`labeling_secondary_model`, toolkit/dedup_sim_settings.py) and therefore cannot
-- be pinned in a file. It writes no rows to this table, so it is out of scope.

begin;

alter table image_clip_embeddings
  add column if not exists revision text;

comment on column image_clip_embeddings.revision is
  'HuggingFace commit sha of the checkpoint that produced this vector. NULL = written '
  'before the 2026-08 encoder pin (migration 456), when the model name alone resolved '
  'to whatever the hub head held at download time.';

commit;
