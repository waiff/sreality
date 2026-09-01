-- 466: one question list. The operator's ruling: a single set holding all 18
-- tags ever asked for — set_1's routing eight plus set_2's ten — replacing the
-- two-set split; the exam screen grows to an 18-key grid.
--
-- BACKFILL. exam_v1's 250 images are fully answered on the original eight.
-- Rather than re-sit 250 images for the ten new columns, the operator declared
-- them NEGATIVE by default and will correct through the review page. These
-- cells carry created_by='backfill:466' — a declared default is not a
-- judgment, and the marker is what lets the review page fence the new buttons
-- off visually, lets anyone count how much of the default remains unreviewed,
-- and keeps the holdout audit honest (an unreviewed default that is actually
-- wrong will mis-grade the machine on that cell until fixed). Re-answering an
-- image through the exam's single write path stamps the operator over the
-- marker, cell by cell.
--
-- gold_v1 gets NO backfill: zero answers exist there, so its sittings simply
-- ask all 18 from the start.
--
-- set_2's row is deleted; its suggestion rows cascade away and the suggest
-- lane refills the renamed set (the stale-refill rail, #1251, makes that a
-- plain re-dispatch).

begin;

alter table tag_exam_sets drop constraint tag_exam_sets_tag_ids_check;
alter table tag_exam_sets add constraint tag_exam_sets_tag_ids_check
  check (array_length(tag_ids, 1) between 1 and 18);

update tag_exam_sets
   set name = 'all',
       tag_ids = '{3,17,22,25,42,43,46,48,28,20,27,30,26,18,2,19,7,36}'::bigint[],
       note = 'The single question list (2026-09-01): the routing eight in their '
              'original key order, then the ten additions in the order the '
              'operator asked for them.'
 where name = 'set_1';

delete from tag_exam_sets where name = 'set_2';

insert into image_tag_labels (image_id, tag_id, state, created_by, source, definition_id)
select m.image_id, t.tag_id, 'negative', 'backfill:466', 'human',
       (select d.id from tag_definitions d
         where d.tag_id = t.tag_id and d.status = 'active')
from tag_exam_members m
join tag_exam_cohorts c on c.id = m.cohort_id and c.name = 'exam_v1'
cross join unnest('{28,20,27,30,26,18,2,19,7,36}'::bigint[]) as t(tag_id)
where 8 = (select count(*) from image_tag_labels l
            where l.image_id = m.image_id
              and l.tag_id in (3,17,22,25,42,43,46,48)
              and l.source in ('human', 'human_confirmed'))
on conflict (image_id, tag_id) do nothing;

commit;
