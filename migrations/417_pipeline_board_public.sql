-- W5 (hydration sprint): the kanban's structural read was two sequential
-- PostgREST round trips joined client-side — property_pipeline_public (which
-- stage + position) then properties_public.in('property_id', ids) (the
-- display fields), the second unable to start until the first's ids landed.
-- pipeline_board_public does the join server-side, cutting the board's
-- structural read to one round trip.
--
-- security_invoker = true (required — property_pipeline is account-scoped
-- RLS, same as its sibling property_pipeline_public; a plain view here would
-- silently reopen the tenant boundary this table's RLS exists to enforce).
-- properties_public stays a plain (definer-style) inner view, unchanged from
-- its own definition — properties carries a permissive
-- FOR SELECT TO authenticated policy (not per-account), so nesting it inside
-- an invoker-mode outer view is correct as-is.
--
-- Column list mirrors PIPELINE_PROPERTY_COLS (frontend/src/lib/pipelineBoardModel.ts)
-- exactly — that constant and this view must never drift apart; a column
-- rename on either side breaks the composePipelineCards projection silently.
create or replace view pipeline_board_public
with (security_invoker = true) as
select
  pp.property_id,
  pp.stage_id,
  pp.board_position,
  pp.entered_stage_at,
  pp.added_at,
  p.sreality_id,
  p.source,
  p.source_id_native,
  p.listing_id,
  p.category_main,
  p.street,
  p.district,
  p.disposition,
  p.subtype,
  p.area_m2,
  p.price_czk,
  p.mf_gross_yield_pct,
  p.total_price_change_pct,
  p.price_change_count,
  p.obec_id,
  p.okres_id,
  p.region_id,
  p.place_search_text,
  p.obec,
  p.locality,
  p.okres,
  p.region,
  p.is_active
from property_pipeline_public pp
left join properties_public p on p.property_id = pp.property_id;

revoke all on pipeline_board_public from public, anon;
grant select on pipeline_board_public to authenticated;
