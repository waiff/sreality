-- 486: membership means the same thing for both signs.
--
-- The operator's renaming (2026-09-07), which is a model change and not a
-- vocabulary change:
--     training positive  stays training positive
--     training negative  becomes NEGATIVE RESERVE
--     reserve            becomes POSITIVE RESERVE
--     review sample      becomes TRAINING NEGATIVE
--
-- Read together those four lines say one thing: `in_training` is a fact about a
-- LABEL, not about a positive. Migration 484 admitted every negative wholesale
-- on the grounds that nobody reviews ten thousand of them — true, and the wrong
-- conclusion. The right one is the operator's: review a random thousand and
-- train on those, and the other nine thousand wait in a reserve exactly as
-- unadmitted positives do.
--
-- That collapses a mechanism. `tag_review_samples` (485, hours old) existed to
-- remember which thousand were drawn — but "the drawn thousand" IS "the
-- negatives admitted to training", so the draw needs no table of its own. This
-- migration moves that fact into `in_training` and leaves 485's table behind as
-- dead schema, to be dropped in a forward migration once the operator OKs a
-- destructive change. Drawing again is now just: un-admit this head's
-- negatives, admit a fresh random N.
--
-- Symmetry restored on the write path too (toolkit/tag_annotations.py): a
-- machine write proposes, whatever its sign. Before this, a machine NEGATIVE
-- was admitted on insert while a machine positive waited — the asymmetry that
-- made "training negative" mean "everything the model rejected".

begin;

-- Every negative starts in the reserve …
update image_tag_labels
   set in_training = false
 where state = 'negative' and in_training;

-- … and the drawn sample is what the heads actually train on.
update image_tag_labels l
   set in_training = true
  from tag_review_samples rs
 where rs.tag_id = l.tag_id and rs.image_id = l.image_id
   and l.state = 'negative';

comment on column image_tag_labels.in_training is
  'Does the head train on this label? Written by the operator only: a machine '
  'write always lands false. True is the training set for that sign, false is '
  'that sign''s reserve — positive and negative alike since migration 486.';

commit;
