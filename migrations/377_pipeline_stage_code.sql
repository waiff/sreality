-- 377_pipeline_stage_code.sql
-- The deal pipeline's stage badge (rule #22): give a stage a short operator-owned
-- CODE, so the number the funnel renders is data instead of a parsed substring.
--
-- Why this exists. The live board's labels are "1. For Review", "2. For Call",
-- "3. For Visit", "4. Negotiations", "9. Passed", "9. Bought", "9. Lost" — the
-- operator numbers stages by hand, inside the display string, and deliberately
-- reuses 9 for all three closed stages. So the number is NOT derivable from
-- ordering: `position` runs 1..7 and would render 5/6/7 on the three "9." stages.
-- Nor is it safe to regex out of `label` at render time (a label without a
-- prefix has no number, and a rename silently changes the badge).
--
-- `code` is therefore its own nullable column:
--   * NOT unique — three stages sharing "9" is the operator's intent, and a
--     unique index would make the live board unrepresentable.
--   * NULL = "no explicit code"; the UI falls back to the stage's 1-based
--     ordinal among live stages (lib/pipelineStage.ts:stageBadge). No trigger,
--     no backfill job, no drift: the fallback is computed where it is rendered.
--   * Backfilled ONCE below from the leading digits of the existing labels, so
--     today's board keeps its numbering with no operator action. Labels are left
--     exactly as they are — renaming operator-curated display text is their call,
--     not this migration's.
--
-- Additive: one nullable column + two view columns appended at the end. No
-- existing consumer selects * from these views, and both keep their grants and
-- their security_invoker setting (re-asserted below rather than assumed).

begin;

alter table pipeline_stages
  add column if not exists code text
  check (
    code is null
    or (char_length(code) between 1 and 4 and btrim(code) = code and btrim(code) <> '')
  );

comment on column pipeline_stages.code is
  'Short operator-owned stage badge (e.g. "1", "9", "2b"). Not unique — the '
  'operator may code several closed stages the same. NULL = fall back to the '
  'stage''s ordinal at render time.';

-- One-time backfill from the hand-numbered labels the board already carries.
update pipeline_stages
   set code = substring(label from '^\s*([0-9]{1,3})')
 where code is null
   and label ~ '^\s*[0-9]';

-- Anon/tenant read surfaces (migration 205, security_invoker since 316): expose
-- `code` so every funnel surface reads the badge from the same place the kanban
-- and the stage editor do. Appended last so column order stays stable.
create or replace view pipeline_stages_public as
  select id, key, label, position, color, is_terminal, is_entry, code
  from pipeline_stages
  where archived_at is null;

create or replace view property_pipeline_public as
  select pp.property_id, pp.stage_id, ps.key as stage_key, ps.label as stage_label,
         ps.position as stage_position, ps.color as stage_color, ps.is_terminal,
         pp.board_position, pp.note, pp.entered_stage_at, pp.added_at, pp.updated_at,
         ps.code as stage_code
  from property_pipeline pp
  join pipeline_stages ps on ps.id = pp.stage_id;

-- CREATE OR REPLACE VIEW keeps reloptions, but these two views are RLS-gated
-- tenant surfaces (migration 316) — re-assert rather than trust, because a view
-- that silently reverted to security_definer would leak every account's cards.
alter view pipeline_stages_public   set (security_invoker = true);
alter view property_pipeline_public set (security_invoker = true);

commit;
