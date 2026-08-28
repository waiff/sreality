-- 457: which property types a tag actually serves.
--
-- WHY. Candidate draws stratify across a FIXED global mix (toolkit/tag_candidates.py
-- CATEGORY_MIX: byt .30 / dum .25 / pozemek .20 / komercni .15 / ostatni .10), chosen
-- to dilute the labeled set's 83.8% byt skew. That is right for a tag with no opinion
-- about property type and wrong for every tag that has one.
--
-- Measured, 2026-08-28: the first live draw for `interier - koupelna` (count=120)
-- returned 54 rows, of which pozemek 24, komercni 18, ostatni 12 — and byt 0, dum 0.
-- The three least relevant quotas filled EXACTLY while the two that matter landed
-- nothing. A bathroom photo attached to a land listing is close to a contradiction,
-- so 100% of that sample was review time spent where the tag does not live.
--
-- Under the dedup routing north star a tag exists to decide which images are worth an
-- expensive vision comparison, and the operator's spec already says where each tag
-- applies: bathrooms/kitchens/technical rooms/floor plans for byt+dum+komercni,
-- facades for dum+komercni, cadastral maps and bounded aerials for pozemek, garages
-- for ostatni. This column records that spec so the draw can honour it.
--
-- NULL = no opinion = today's behaviour, unchanged. Only a tag with an explicit scope
-- changes, so this is additive in effect as well as in DDL. The array is validated in
-- Python against CATEGORY_MIX's keys rather than by a CHECK constraint, because the
-- vocabulary lives in listings.category_main and a CHECK here would be a second copy
-- of it that could drift.
--
-- This is also the router's own map arriving early: W6 reads the same column inverted
-- (property type -> the categories to compare), so "we might change the categories as
-- we learn which are effective" stays a row edit, never a deploy.

begin;

alter table tag_taxonomy
  add column if not exists routing_categories text[];

comment on column tag_taxonomy.routing_categories is
  'Property types (listings.category_main) this tag serves, e.g. {byt,dum,komercni}. '
  'Candidate draws restrict and renormalise their category mix to these. NULL = no '
  'scope = draw across the full CATEGORY_MIX. Operator-owned; validated in Python.';

-- The operator's ratified routing spec (2026-08-28). Set by label, not id, so this
-- reads as the spec it encodes; a renamed tag simply keeps its NULL and falls back to
-- the global mix rather than silently acquiring the wrong scope.
update tag_taxonomy set routing_categories = '{byt,dum,komercni}'
 where label in ('interier - koupelna', 'interier - kuchyně',
                 'technické zařízení / místnost', 'podklad - půdorys');

update tag_taxonomy set routing_categories = '{dum,komercni}'
 where label = 'exterier - fasáda';

update tag_taxonomy set routing_categories = '{dum,komercni,pozemek}'
 where label = 'podklad - katastrální mapa';

update tag_taxonomy set routing_categories = '{pozemek}'
 where label = 'podklad - letecký snímek s ohraničením subjektu';

update tag_taxonomy set routing_categories = '{ostatni}'
 where label = 'garáž';

commit;
