-- 449_tag_candidates.sql
--
-- The candidate store: which images are QUEUED for review on which tag, and WHY
-- each one was drawn. PURELY ADDITIVE -- no DROP, no DELETE, no destructive
-- ALTER. Nothing in this file removes a row, and nothing in dedup_sim is touched.
--
-- WHAT IT REPLACES, IN READERS ONLY. dedup_sim.labeling_sample is 1,200 images
-- chosen as "the 2,000 newest with a storage_path" -- untargeted, one pool shared
-- by all 51 tags, and 943 of them were never labeled at all. Rare tags are a
-- fraction of a percent of the corpus, so a random pool cannot build their
-- training sets: candidates have to be FOUND, not stumbled on. From here they
-- are, per tag, by ranking a bounded pool against a centroid of that tag's
-- HUMAN-VERIFIED positives (toolkit/tag_candidates.py). The dedup_sim table stays
-- exactly as it is -- it still feeds the secondary-CLIP proposal lane -- and its
-- eventual drop is a separate, gated PR that also has to retire that lane.
--
-- MEMBERSHIP CARRIES NO TRAINING SEMANTICS. This is the load-bearing property of
-- the whole table and the reason it is shaped the way it is. An image that is a
-- candidate for tag T is an image somebody should LOOK at for T. It is not a
-- positive, it is not a negative, and it is not "defaulted" to anything. The
-- operator's ruling (2026-08-27) supersedes migration 442's ledger decision that
-- an untouched cell inside the pool trains as negative: an image never reviewed
-- for T must stay distinguishable, forever, from one reviewed and judged not-T.
-- So this table has NO state column, NO reviewed flag, and NO status: whether a
-- candidate has been decided is DERIVED by joining image_tag_labels, which is the
-- only place a decision has ever lived. There is deliberately nothing here for a
-- future reader to mistake for a label.
--
-- WHY EACH ROW CARRIES ITS OWN PROVENANCE. `draw` says which rank band produced
-- it (a pure top-N produces prototypical training sets that fail on odd cases, so
-- the retriever mixes bands on purpose); `category_main` says which category
-- quota it was drawn under (the labeled set is 83.8% byt against a 43.9% corpus
-- -- the draw is stratified so that skew is diluted rather than inherited, and
-- storing the bucket is what makes the correction auditable instead of a claim);
-- `pool_rank` / `pool_size` / `distance` say where in the ranked pool it sat, and
-- rank is meaningless without the size it is a rank OF; `centroid_positive_count`
-- says how much evidence the centroid was built from; `definition_id` cites the
-- tag_definitions version (migration 445) that was active at draw time, resolved
-- inside the INSERT from the row's own tag_id and never passed in by a caller --
-- migration 446's rule, for the same reason.
--
-- Backend-only: RLS on, no _public view, and an EXPLICIT revoke. This project's
-- default privileges auto-GRANT on new relations; migrations 442 and 445 both got
-- bitten by exactly that and 447 had to clean it up. Not again.

begin;

create table tag_candidates (
  tag_id                  bigint not null references tag_taxonomy (id) on delete cascade,
  image_id                bigint not null references images (id) on delete cascade,

  -- Which band of the ranked pool produced this row. Vocabulary mirrored by
  -- toolkit/tag_candidates.DRAWS; a new band is a new migration.
  draw                    text not null
    check (draw in ('centroid_head', 'centroid_mid', 'random')),

  -- The listings.category_main bucket this draw was allocated under. NOT NULL:
  -- every draw this store ships with is category-scoped. No CHECK against an enum
  -- -- listings.category_main is free text in the database, and a vocabulary
  -- change must not require a migration here.
  category_main           text not null,

  -- Cosine DISTANCE (pgvector <=>, 0 = identical) to the tag's centroid at draw
  -- time. Never a similarity, and never comparable ACROSS tags: measured inter-tag
  -- centroid cosines span 0.58-0.99, so absolute values do not transfer. Rank and
  -- percentile are the transferable quantities; this is kept for auditing one
  -- pool, not for thresholding across tags.
  distance                double precision not null,

  -- Rank within the pool AFTER exclusions and exact-hash collapse, and the size
  -- of that same pool. pool_size is NOT the corpus size.
  pool_rank               integer not null check (pool_rank >= 1),
  pool_size               integer not null,

  -- Draw-time snapshot of the cap key, not a live join key: a later merge
  -- re-points listings.property_id without invalidating why this row was capped.
  -- Bare bigints for that reason -- no FK.
  listing_id              bigint not null,
  property_id             bigint,

  -- Draw-time snapshot of images.phash, so the near-duplicate check for a LATER
  -- draw on this tag is one read of this table instead of a join back to images.
  phash                   bigint,

  centroid_positive_count integer not null check (centroid_positive_count >= 0),
  model                   text not null,
  definition_id           bigint references tag_definitions (id) on delete set null,

  drawn_at                timestamptz not null default now(),
  drawn_by                text not null default 'operator',

  constraint tag_candidates_rank_within_pool check (pool_size >= pool_rank),

  -- A candidate is drawn FOR a tag, and the same image can legitimately be a
  -- candidate for several tags. tag_id leads because every read is tag-scoped.
  primary key (tag_id, image_id)
);

-- No second index. The PK's leading tag_id already bounds every read, the
-- per-property cap check and the near-dup history read to ONE tag's rows, and a
-- few hundred rows sort in microseconds. Add one when a single tag's pool is
-- measured in the tens of thousands -- not before.

alter table tag_candidates enable row level security;
revoke all on tag_candidates from anon, authenticated;

comment on table tag_candidates is
  'Per-(tag, image) REVIEW QUEUE produced by centroid retrieval over '
  'image_clip_embeddings (toolkit/tag_candidates.py). Membership carries NO '
  'training semantics whatsoever: a candidate is an image somebody should LOOK '
  'at for this tag -- not a positive, not a negative, not a default. There is no '
  'state column and no reviewed flag on purpose; whether a candidate has been '
  'decided is derived by joining image_tag_labels, the only place a decision has '
  'ever lived. Every row records why it was drawn: which rank band (draw), which '
  'category quota (category_main), where it sat in the ranked pool (pool_rank / '
  'pool_size / distance), how much evidence the centroid carried '
  '(centroid_positive_count) and which written definition was active '
  '(definition_id, migration 445). Rows are never deleted -- a decided candidate '
  'is the record of how that decision came to be reviewed. Backend-only, RLS on, '
  'no _public view, admin-gated API only.';

comment on column tag_candidates.draw is
  'Which band of the ranked pool produced this row. centroid_head = the nearest '
  'neighbours (high yield, but a pure top-N produces prototypical heads that fail '
  'on odd cases); centroid_mid = a random sample from just below the head, where '
  'the confusion clusters live (bathrooms, circulation, living spaces) and where '
  'the hard cases are; random = an unranked sample of the whole pool, which is '
  'the only honest source of a base rate AND the only band that can surface a '
  'positive the centroid is blind to. If the random band keeps yielding '
  'positives, the centroid is missing a mode.';

comment on column tag_candidates.category_main is
  'The listings.category_main quota this draw was allocated under. Stored so the '
  'labeled set''s 83.8%-byt skew (against a 43.9% corpus) is visible per tag '
  'rather than silently inherited.';

comment on column tag_candidates.distance is
  'Cosine DISTANCE to the tag centroid at draw time (0 = identical). Meaningful '
  'only WITHIN one tag''s pool: inter-tag centroid cosines span 0.58-0.99, so no '
  'global threshold on this column is ever valid.';

comment on column tag_candidates.definition_id is
  'The tag_definitions version (migration 445) active when this pool was drawn. '
  'Resolved inside the INSERT from the row''s own tag_id, never supplied by a '
  'caller (migration 446''s rule). ON DELETE SET NULL, never RESTRICT.';

-- Migration 442's table comment still asserts the rule the operator has since
-- overturned ("no row means untouched: displays and trains as negative once the
-- image is in dedup_sim.labeling_sample"). Migrations are append-only, so the old
-- text cannot be edited -- it is restated here instead. A comment that
-- contradicts the standing rule is a trap for whoever reads the catalog next.
comment on table image_tag_labels is
  'One row per explicit (image, tag) decision -- positive, negative, or excluded. '
  'No row means UNTOUCHED, and untouched NEVER trains as negative (operator '
  'ruling, 2026-08-27, superseding migration 442''s pool-scoped default-negative). '
  'An image never reviewed for a tag must stay distinguishable from one reviewed '
  'and judged not-that-tag; membership of the tag_candidates review queue '
  '(migration 449) confers no label of any kind. excluded rows are dropped at '
  'training time by definition, not by a separate flag. Backend-only, admin-gated.';

commit;
