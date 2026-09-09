-- 490: the versioned tag model registry and the per-image winner store
-- (docs/design/new-dedup/PROGRAM.md ledger 2026-09-09 (b); W3's "versioned
-- artifact ... campaign-retag the corpus into the sim tag store", pulled forward
-- so later waves can be built against it while training keeps iterating).
--
-- WHAT THIS IS. A tag model is ONE frozen decision: this encoder, this training
-- mode, this set of heads, these weights. Promoting one copies the heads out of a
-- bake-off run into permanent storage; scoring an image asks every head of that
-- model for a probability and keeps them all, plus which head won.
--
-- WHY THE PUBLIC SCHEMA AND NOT dedup_sim. The bake-off's tables (migration 489)
-- are experiment evidence and are droppable wholesale at Wave 8
-- (`drop schema dedup_sim cascade`). These three are the opposite: the model the
-- product tags with, and the tags themselves. They must survive that drop, so
-- they live in `public` and hold NO foreign key into `dedup_sim` —
-- `source_run_id` / `source_arm` are provenance TEXT AND NUMBERS, deliberately
-- un-referential, so dropping the evidence cannot cascade into the product.
--
-- THE WINNER RULE (operator ruling 2026-09-09 (a)), stated once and stored once:
-- an image's tag is the head with the HIGHEST score under the model, ties broken
-- toward the lower tag_id. There are NO per-head yes/no decisions in the product
-- at all — that is why `image_tag_scores` keeps the whole `scores` object and a
-- winner, and stores NO threshold and NO boolean. A consumer that wants "and only
-- if it is confident" applies its OWN floor to `winner_score`; the model does not
-- decide that on the consumer's behalf. A head's own `threshold` still lives in
-- its artifact, because that is what the bake-off measured it at, and it is
-- evidence rather than a gate.
--
-- WHY ONE ROW PER (IMAGE, MODEL) AND NOT PER HEAD. At the eventual corpus pass
-- this is 11.5M rows of roughly 300 bytes. One row per (image, model, head) would
-- be eleven times that for the same information, and the read every consumer
-- actually makes — "what is this photo?" — would become a per-image aggregate
-- instead of a primary-key lookup. Adding heads is a NEW model version anyway
-- (the winner is an argmax over the model's head set, so it is only meaningful
-- over the whole set at once), which is exactly what makes the wide row safe.
--
-- Security posture at creation, per migrations 237/447: RLS enabled with zero
-- policies, anon/authenticated DML revoked. All three relations are registered in
-- tests/test_migration_rls_grants.py::_ADMIN_ONLY_RELATIONS and
-- tests/test_tenant_isolation_live.py::_ADMIN_ONLY_RELATIONS. There is no
-- `_public` view and none is planned — the SPA reads through the admin-gated API
-- (`/new-dedup/tags/*`), never over supabase-js.

create table if not exists tag_head_models (
  id            bigserial primary key,
  version       text not null unique,
  label         text not null,
  status        text not null default 'candidate'
                check (status in ('candidate', 'active', 'retired')),
  created_at    timestamptz not null default now(),
  activated_at  timestamptz,
  -- Provenance into dedup_sim, carried as plain values with NO foreign key: the
  -- evidence schema is droppable and this table is not.
  source_run_id bigint,
  source_arm    text,
  mode          text not null,
  -- The seven encoder identity facts, same names as image_dinov3_embeddings
  -- (migration 480) and dedup_sim.tag_head_bakeoff_arms (489), so "can this
  -- vector be scored by this model" is a field-by-field comparison, never faith.
  model         text not null,
  revision      text not null,
  library       text not null,
  pooling       text not null,
  resolution    integer not null check (resolution > 0),
  preprocessing text not null,
  dtype         text not null,
  -- The tag ids this version scores. Frozen here because the winner is an argmax
  -- over exactly this set: a reader must be able to see the head set a stored
  -- winner was chosen from, even after the taxonomy has moved on.
  heads         bigint[] not null default '{}',
  dataset_hash  text,
  note          text
);

-- At most ONE active model, enforced by the database rather than by the
-- promotion code being careful. Everything downstream reads "the active model";
-- two of them is not a degraded state, it is an ambiguous one.
create unique index if not exists tag_head_models_one_active_idx
  on tag_head_models (status) where status = 'active';

create table if not exists tag_head_model_heads (
  model_id   bigint not null references tag_head_models(id) on delete cascade,
  -- No FK to tag_taxonomy on purpose: a model is a historical fact and must stay
  -- readable after a tag is retired from the live vocabulary.
  tag_id     bigint not null,
  -- The toolkit/tag_heads.py artifact verbatim (weights, bias, threshold, kind,
  -- the encoder identity, the dataset hash) — everything scoring needs, in pure
  -- Python, with no ML library at inference.
  artifact   jsonb  not null,
  -- The bake-off's cv/exam numbers for this head, COPIED at promotion. A copy and
  -- not a join: dedup_sim goes away, and "how good was this head when we shipped
  -- it" must not go away with it.
  metrics    jsonb  not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  primary key (model_id, tag_id)
);

create table if not exists image_tag_scores (
  image_id      bigint not null references images(id) on delete cascade,
  model_id      bigint not null references tag_head_models(id) on delete cascade,
  -- {"<tag_id>": <probability>} for EVERY head of that model. Keys are strings
  -- because JSON object keys are; the readers cast back to bigint.
  scores        jsonb  not null,
  winner_tag_id bigint not null,
  winner_score  real   not null,
  scored_at     timestamptz not null default now(),
  primary key (image_id, model_id)
);

-- "The most confident <tag> photos under this model", which is how every browse,
-- review and audit surface will page through the store.
create index if not exists image_tag_scores_winner_idx
  on image_tag_scores (model_id, winner_tag_id, winner_score desc);

-- The scoring job's resume scan ("which images does this model already have?"),
-- which needs model_id leading — the primary key has image_id leading.
create index if not exists image_tag_scores_model_image_idx
  on image_tag_scores (model_id, image_id);

alter table tag_head_models      enable row level security;
alter table tag_head_model_heads enable row level security;
alter table image_tag_scores     enable row level security;

revoke all on tag_head_models      from anon, authenticated;
revoke all on tag_head_model_heads from anon, authenticated;
revoke all on image_tag_scores     from anon, authenticated;

comment on table tag_head_models is
  'One versioned tag model: an encoder configuration, a training mode and a set '
  'of per-tag heads, promoted from a bake-off run. At most one row is active '
  '(partial unique index); consumers read the active one. Provenance into '
  'dedup_sim is carried without a foreign key, because that schema is droppable '
  'at Wave 8 and this table is not. Backend-only: RLS on, zero policies, '
  'anon/authenticated revoked, no _public view.';

comment on table tag_head_model_heads is
  'One head of one model: the toolkit/tag_heads.py artifact (weights, bias, '
  'threshold, encoder identity, dataset hash) plus the bake-off cv/exam metrics '
  'COPIED at promotion, so a shipped head stays explainable after the evidence '
  'schema is dropped.';

comment on table image_tag_scores is
  'One row per (image, model version): every head''s probability, plus the '
  'winner. THE TAG IS THE ARGMAX over that model''s heads, ties broken toward '
  'the lower tag_id. No threshold and no per-head yes/no is stored — the '
  'operator ruled (2026-09-09) that the product has no per-head decisions at '
  'all; a consumer that wants a minimum confidence applies its own floor to '
  'winner_score. Re-scoring the same model version overwrites the row (the '
  'model version IS the identity); adding heads means a new version.';
