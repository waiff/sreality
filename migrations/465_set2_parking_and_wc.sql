-- 465: set_2 gains parkoviště and wc — the two tags the operator named when
-- the iteration mechanism was ratified ("when we need new tags, such as
-- parkoviště, wc"). Both already exist in the taxonomy (7 exterier -
-- parkoviště, 36 interier - wc); they gain the draw-scoping flag and two of
-- set_2's four free slots (8 -> 10 of the 12 cap).
--
-- Timing verified before writing: NO set_2 sitting has started on any cohort
-- (zero answers against tag 28 on exam_v1 and gold_v1), so this lands inside
-- the runbook's edit window and re-serves nothing. The stored set_2
-- suggestions froze an 8-tag question list and stop being served the moment
-- this applies (asked-list mismatch, by design); the suggest lane re-runs
-- for both cohorts right after.
--
-- gold_v1 is sealed and does NOT grow columns' images here — the two new
-- columns are asked over the existing 553 exam images. A curated top-up wave
-- for parking/wc examples is a later gold_v2, if their drafts ever warrant one.

begin;

update tag_taxonomy set routing_categories = '{byt,dum,komercni}'
 where id in (7, 36) and routing_categories is null;

update tag_exam_sets
   set tag_ids = tag_ids || '{7,36}'::bigint[]
 where name = 'set_2'
   and not (tag_ids && '{7,36}'::bigint[]);

commit;
