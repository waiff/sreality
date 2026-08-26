-- 443_tag_taxonomy_priority_and_training_ready.sql
--
-- Two operator flags on tag_taxonomy (migration 442), both surfaced in the
-- "Modify labels" popup:
--
-- * priority — this tag needs attention now; pins it to the top of the list
--   and marks it visually (red) so it isn't lost among 50+ others.
-- * ready_for_training — the operator has decided this tag's set is solid
--   enough to include in the next per-tag trainer run
--   (docs/design/clip-linear-probe.md, not yet built). A manual signal, not
--   derived from Gate 1: hitting 150 positives says a tag is LABELED enough,
--   not that the operator has actually reviewed its quality.
--
-- Purely additive.

begin;

alter table tag_taxonomy add column priority boolean not null default false;
alter table tag_taxonomy add column ready_for_training boolean not null default false;

commit;
