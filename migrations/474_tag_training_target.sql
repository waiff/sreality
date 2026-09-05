-- 474: tag_taxonomy.training_target — the cutoff that defines a head's set.
--
-- The machine labeled 10,544 images; fasáda alone has 1,149 positives. Nobody
-- reviews 1,149 photos, and a linear probe on frozen CLIP features does not
-- need them: a few hundred clean positives per head is where its accuracy
-- stops improving. So each head gets a TARGET, and its training set is defined
-- as a QUERY over that target rather than as a list anyone maintains:
--
--   the operator's own positives first (they are confirmed), then the
--   machine's positives oldest-first, up to `training_target`; everything
--   past it is the RESERVE.
--
-- Because the set is a query, removing a wrong positive (the operator marks it
-- negative or left out) shrinks the ranked list by one and the first reserve
-- image steps into the set — no bookkeeping, no gap, and the review stays a
-- bounded job of `training_target` images per head. Confirming a positive
-- (writing it as a human label) moves it to the front and keeps it in.
--
-- NULL = the programme default (toolkit.machine_labeling.DEFAULT_TRAINING_TARGET,
-- 300). The column is the operator's per-head override, same posture as 443's
-- `priority` and `ready_for_training`.

begin;

alter table tag_taxonomy
  add column training_target integer
  check (training_target is null or training_target between 1 and 5000);

comment on column tag_taxonomy.training_target is
  'Cutoff defining this head''s training set: human positives first, then machine '
  'positives oldest-first, up to this many. Past it is the reserve, which refills '
  'the set automatically when a positive is removed. NULL = programme default.';

commit;
