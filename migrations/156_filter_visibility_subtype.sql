-- 156_filter_visibility_subtype.sql
--
-- Register the new `subtype` filter (migration 152 / filter_registry) in
-- filter_visibility for the agendas it declares. A missing row already defaults
-- to enabled=true, so this is the same explicit-seed convention migration 059
-- established — it just makes the operator's Settings agenda × filter matrix
-- show the toggle from day one. Watchdog only for now; the Browse agenda is
-- seeded when the Browse sidebar UI ships.

insert into filter_visibility (agenda, filter_id, enabled) values
  ('watchdog', 'subtype', true)
on conflict (agenda, filter_id) do nothing;
