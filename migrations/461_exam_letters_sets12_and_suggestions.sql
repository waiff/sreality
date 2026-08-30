-- 461: iteration-2 exam upgrades — a 12-tag set cap, set_2 extended by five tags,
-- and a per-sitting machine-suggestion store.
--
-- THE CAP MOVES FROM 10 TO 12 because the exam screen no longer keys on digits.
-- 460 capped a set at ten "because the keyboard has digits 1-9 and 0"; the
-- operator replaced the digits with a letter grid (w e i o / s d k l / y x n m —
-- twelve keys, laid out like the keyboard itself) and ruled twelve tags max per
-- sitting. On the Czech QWERTZ layout the digit row is shifted (unshifted it
-- types diacritics), so letters are also the ergonomic fix. Widening a CHECK is
-- a drop-and-add; both statements sit in this one transaction.
--
-- SET_2 GAINS FIVE TAGS, in the operator's stated order: "chodba / předsíň,
-- ložnice, chodba / schodiště, vstupní dveře". The first three map one-to-one
-- (30 předsíň/chodba, 26 ložnice, 18 chodba/schodiště). "vstupní dveře" has no
-- single tag: the taxonomy splits the entry door by which side it is photographed
-- from — 2 (exterier - domovní vchod) and 19 (interier - domovní vchod / chodba)
-- — so BOTH are seated; trimming either later is one array edit. Appended, never
-- reordered: array order is the on-screen key order, and set_2's sitting has not
-- started (the runbook's window for edits).
--
-- tag_exam_suggestions is the operator's OWN ruling against 458/459's original
-- posture ("no machine suggestion is ever returned with a question"): each exam
-- image is pre-run through the model and the suggested buttons are marked,
-- subtly, on screen. The honest cost, stated once and mitigated here: a
-- machine-assisted sitting measures agreement with a machine-ANCHORED human, not
-- blind agreement. The mitigation is provenance — every suggestion is stored
-- with the exact question list it answered, so suggested-vs-final disagreement
-- stays computable per image and per tag forever. Suggestions live in their own
-- table and NEVER touch image_tag_labels: they are not labels, they train
-- nothing, and the holdout census does not apply to them.
--
-- asked_tag_ids is frozen at call time because sets grow by design: a suggestion
-- computed for a 3-tag set_2 says nothing about the 8-tag set_2, and serving it
-- anyway would mark a subset of the buttons while looking complete. The API
-- serves a suggestion only when its asked list equals the sitting's current
-- list; after a set edit the suggest lane simply runs again.

begin;

alter table tag_exam_sets drop constraint tag_exam_sets_tag_ids_check;
alter table tag_exam_sets add constraint tag_exam_sets_tag_ids_check
  check (array_length(tag_ids, 1) between 1 and 12);

update tag_exam_sets
   set tag_ids = tag_ids || '{30,26,18,2,19}'::bigint[]
 where name = 'set_2'
   and not (tag_ids && '{30,26,18,2,19}'::bigint[]);

-- Draw scoping for the five, same as 460's trio: these subjects live in
-- byt/dum/komercni listings, so their candidate draws must not spend quota on
-- pozemek/ostatni. Property-type scope ONLY, not router membership (460's
-- header records why). Safe against the live sitting: since #1239 a bare exam
-- URL resolves to the FIRST SET, never to these flags.
update tag_taxonomy set routing_categories = '{byt,dum,komercni}'
 where id in (30, 26, 18, 2, 19) and routing_categories is null;

create table tag_exam_suggestions (
  cohort_id    bigint not null references tag_exam_cohorts (id) on delete cascade,
  image_id     bigint not null references images (id) on delete cascade,
  set_id       bigint not null references tag_exam_sets (id) on delete cascade,
  -- The question list this suggestion answered, frozen at call time. Compared
  -- (order-insensitively) with the set's current tag_ids before serving.
  asked_tag_ids     bigint[] not null,
  -- Empty array = the model saw none of them, which IS shown ("machine: none").
  -- On error it stays empty and `error` says why; such a row is retried, never
  -- served.
  suggested_tag_ids bigint[] not null default '{}'::bigint[],
  model        text not null,
  error        text,
  suggested_at timestamptz not null default now(),
  primary key (cohort_id, image_id, set_id)
);

comment on table tag_exam_suggestions is
  'Machine pre-answers for exam sittings, marked subtly on the exam buttons. '
  'Ordered by the operator 2026-08-30, reversing the no-suggestion posture; the '
  'stored suggestion vs the final human answer is the anchoring audit. Never '
  'labels, never training data.';

alter table tag_exam_suggestions enable row level security;
revoke all on tag_exam_suggestions from anon, authenticated;

alter table llm_calls drop constraint if exists llm_calls_called_for_check;
alter table llm_calls add constraint llm_calls_called_for_check
  check (called_for in (
    'parse_url',
    'summarize_listing',
    'compare_listing_images',
    'agent_estimation',
    'extract_building_units',
    'read_floor_plan',
    'refine_skill',
    'discover_condition_markers',
    'score_listing_condition',
    'summarize_region_dispositions',
    'enrich_listing_description',
    'classify_listing_images',
    'compare_listings_visually',
    'compare_listing_site_plans',
    'compare_listing_floor_plans',
    'screen_exam_image',
    'suggest_exam_answer'
  ));

commit;
