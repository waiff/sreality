-- 487: tag_taxonomy.review_state — the operator's review marker, three-valued.
--
-- Migration 443 added `ready_for_training`, a boolean nothing ever read; it was
-- given a UI on 2026-09-08 as a Ready / Not ready toggle so a review spanning
-- days does not lose its place. The operator immediately wanted a third state:
-- "make the toggle 3 way - ready, not ready, skip for now".
--
-- A boolean cannot carry three. The alternative considered and rejected was to
-- drop the NOT NULL and read NULL as "skipped": fewer columns, but NULL means
-- "nobody has said" in every other column of this schema, which is nearer to
-- "not ready" than to a deliberate decision to skip. A marker whose third value
-- is indistinguishable from "unset" is worse than an extra column.
--
-- So an explicit text state, constrained to the three the UI offers. It carries
-- `ready_for_training` forward, which becomes dead schema — to be dropped in the
-- same forward migration as `tag_review_samples` (485) and `training_target`
-- (474) once the operator OKs a destructive change.
--
--   not_ready  the default; nobody has been through this head
--   ready      the operator has reviewed it and is satisfied
--   skipped    deliberately set aside — NOT the same as untouched, which is the
--              whole reason this is not a boolean

begin;

alter table tag_taxonomy
  add column if not exists review_state text not null default 'not_ready'
    check (review_state in ('not_ready', 'ready', 'skipped'));

update tag_taxonomy set review_state = 'ready' where ready_for_training;

comment on column tag_taxonomy.review_state is
  'The operator''s own review marker: not_ready / ready / skipped. Nothing reads '
  'it but them — it gates nothing and affects no training. Supersedes migration '
  '443''s ready_for_training, which is now dead schema.';

commit;
