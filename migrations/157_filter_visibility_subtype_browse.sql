-- 157_filter_visibility_subtype_browse.sql
--
-- The `subtype` filter gained the BROWSE agenda when the Browse sidebar UI
-- shipped (slice 3); seed its browse visibility row, same convention as the
-- watchdog seed in migration 156. A missing row already defaults to
-- enabled=true, so this just surfaces the toggle in the Settings agenda matrix.

insert into filter_visibility (agenda, filter_id, enabled) values
  ('browse', 'subtype', true)
on conflict (agenda, filter_id) do nothing;
