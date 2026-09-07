-- 485: tag_review_samples — a drawn review sample, STORED so it cannot move.
--
-- The operator (2026-09-07): "Right now I have to verify thousands of negative
-- images per head. I would like to draw a random sample of only 1000 images per
-- head from the current negatives … I would prefer if the random sample was
-- sorted the same way it is now, because the current negatives were built in an
-- order, so images of certain types are grouped and that is easier for review."
--
-- Two properties, and the second is why this is a TABLE and not a query.
--
-- 1. RANDOM, then the EXISTING order. The draw picks 1000 uniformly; the page
--    then renders them under its own `updated_at DESC, image_id DESC`, so the
--    reviewer walks the same grouping the negatives already have and each photo
--    sits further down the list than the last. No new sort exists anywhere —
--    the sample is a FILTER over the order that was already there.
--
-- 2. IT MUST NOT MOVE WHILE IT IS BEING REVIEWED. A computed sample ("1000
--    random negatives") re-draws on every read, and worse, marking one of the
--    thousand takes it out of `negative` and silently promotes number 1001 into
--    the set. That is exactly the churn migration 484 was written to end: a set
--    the operator is working through must be a fact, not a query. So the draw
--    is written down once, and the review lane shows those 1000 rows by
--    MEMBERSHIP — not by current state — so a photo the operator has just
--    re-marked stays visible with its new mark instead of vanishing mid-page.
--
-- drawn_from_state records what the tray was at draw time; it is provenance for
-- the lane's label ("sample of negatives"), never a filter on what is shown.
-- Redrawing is deleting and drawing again, and it is the operator's call: this
-- table is review scaffolding, and no label depends on it.

begin;

create table tag_review_samples (
  tag_id            bigint not null references tag_taxonomy (id) on delete cascade,
  image_id          bigint not null references images (id) on delete cascade,
  drawn_from_state  text not null check (drawn_from_state in ('positive', 'negative', 'excluded')),
  drawn_at          timestamptz not null default now(),
  primary key (tag_id, image_id)
);

comment on table tag_review_samples is
  'A drawn review sample: N images picked at random from one head''s tray and '
  'written down, so the set stays put while the operator works through it. The '
  'page renders them in its own existing order — this is a filter, not a sort.';

alter table tag_review_samples enable row level security;
revoke all on tag_review_samples from anon, authenticated;

commit;
