-- 412_estimates_public_surrogate_subject_identity.sql
--
-- Finishes the estimation-subject-identity cutover (PR #1095, migration 411) on the
-- last surface that still keyed on the LEGACY sreality_id: property_estimates_public.
--
-- #1095 moved subject identity to the surrogate listings.id, stored on
-- estimation_runs.input_listing_id, and GET /estimations already filters on it with no
-- legacy fallback arm. This view was deliberately deferred. Measured at that merge and
-- re-measured here: 0 rows differ TODAY, but arm 1's join key (l.sreality_id =
-- er.input_sreality_id) can only ever match a listing whose native id happens to live in
-- the sreality-shaped column, so 100% of future non-sreality runs would be missed.
--
-- Change is exactly two join/gate keys. The UNION shape, the column list, all three
-- account arms, security_invoker=false (329), the `authenticated` SELECT grant (319) and
-- the anon revoke (331) are preserved verbatim -- CREATE OR REPLACE VIEW keeps reloptions
-- and grants, so none are re-issued.
--
--   arm 1 gate  er.input_sreality_id is not null  ->  er.input_listing_id is not null
--   arm 1 join  l.sreality_id = er.input_sreality_id  ->  l.id = er.input_listing_id
--   arm 2 gate  er.input_sreality_id is null      ->  er.input_listing_id is null
--
-- Arm 2's gate MUST flip with arm 1's so the two arms stay a disjoint partition of the
-- successful runs -- UNION ALL feeds a count(*), so any overlap would double-count
-- run_count. Arm 2 (the URL join) is otherwise untouched.
--
-- Deliberately NOT a fallback OR. api/estimation_runs.py:415 uses
-- `l.id = er.input_listing_id OR (er.input_listing_id IS NULL AND l.sreality_id = ...)`
-- for its own reasons; reproducing that here would make the join non-sargable across two
-- indexes on a 685k-row listings table. The straight swap keeps a unique index scan
-- (listings_sreality_id_uidx -> listings_pkey, same cost), verified by EXPLAIN below.
--
-- Handoff is gap-free for a new non-sreality run: created with input_url and a NULL
-- input_listing_id it matches arm 2; once the late-binding resolver in
-- scripts/recompute_property_stats.py stamps input_listing_id it moves to arm 1. Never
-- both, never neither.

begin;

set local lock_timeout = '5s';

create or replace view public.property_estimates_public as
with matched as (
  select l.property_id,
         er.created_at
    from estimation_runs er
    join listings l on l.id = er.input_listing_id
   where er.status = 'success'::text
     and er.input_listing_id is not null
     and l.property_id is not null
     and ( er.account_id in (select current_account_ids())
        or er.account_id = '00000000-0000-0000-0000-000000000000'::uuid
        or (er.account_id is null and is_platform_admin()) )
  union all
  select l.property_id,
         er.created_at
    from estimation_runs er
    join listings l on l.source_url = er.input_url
   where er.status = 'success'::text
     and er.input_listing_id is null
     and l.property_id is not null
     and ( er.account_id in (select current_account_ids())
        or er.account_id = '00000000-0000-0000-0000-000000000000'::uuid
        or (er.account_id is null and is_platform_admin()) )
)
select property_id,
       count(*)::integer as run_count,
       max(created_at) as last_run_at
  from matched
 group by property_id;

-- Post-conditions. The first three are migration 341's guards, reproduced verbatim: a
-- redefinition that loses owner-rights, the per-account predicate, or the shared
-- SYSTEM-account arm is a regression this migration must not introduce. The fourth is new
-- and is the point of this migration.
do $$
declare
  v_invoker text;
  v_def text;
begin
  select coalesce(
           (select option_value from pg_options_to_table(c.reloptions)
             where option_name = 'security_invoker'),
           'false')
    into v_invoker
    from pg_class c
   where c.oid = 'public.property_estimates_public'::regclass;
  if v_invoker <> 'false' then
    raise exception 'property_estimates_public must stay owner-rights (security_invoker=%)', v_invoker;
  end if;

  v_def := pg_get_viewdef('public.property_estimates_public'::regclass, true);
  if position('current_account_ids' in v_def) = 0 then
    raise exception 'property_estimates_public lost its per-account scoping predicate';
  end if;
  if position('00000000-0000-0000-0000-000000000000' in v_def) = 0 then
    raise exception 'property_estimates_public lost the shared SYSTEM-account arm — '
                    'Browse''s "with estimates" filter would return nothing';
  end if;
  if position('input_sreality_id' in v_def) > 0 then
    raise exception 'property_estimates_public still joins the legacy input_sreality_id';
  end if;
  if position('input_listing_id' in v_def) = 0 then
    raise exception 'property_estimates_public is not keyed on the surrogate input_listing_id';
  end if;

  perform 1 from public.property_estimates_public limit 1;
end $$;

commit;
