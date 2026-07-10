-- 285: enable the §2.2 free arms + the facade dismisser (operator decision 2026-07-10).
--
-- Flips the three operator gates shipped default-OFF by #742/#745, per the cost plan's
-- rollout shape (build gated -> replay evidence -> operator enables). Evidence at flip
-- time (fresh replays on current data, in the PR bodies):
--   dedup_nonbyt_phash_single_enabled  99.41% agreement (1,017/1,023 decided non-byt pairs)
--   dedup_nonbyt_cosine_merge_min 0.98 99.71% agreement (1,018/1,021)
--   dedup_facade_dismiss_enabled       15/15 dismissed-agreement, 0 of 79 operator merges conflict
-- Queue sample: ~62% of the 19.6k proposed review queue concludes via the arms for $0.
-- All merges reversible (unmerge_group); floor/site-plan guards unchanged; the
-- app_settings_history trigger records these flips like any Settings-page change.

INSERT INTO app_settings (key, value, description, updated_by) VALUES
  ('dedup_nonbyt_phash_single_enabled', 'true'::jsonb,
   'Enabled by migration 285 (operator decision 2026-07-10; replay 99.41%).', 'migration-285'),
  ('dedup_nonbyt_cosine_merge_min', '0.98'::jsonb,
   'Set to the validated operating point by migration 285 (replay 99.71%).', 'migration-285'),
  ('dedup_facade_dismiss_enabled', 'true'::jsonb,
   'Enabled by migration 285 (operator decision 2026-07-10; replay 15/15, 0/79 conflicts).', 'migration-285')
ON CONFLICT (key) DO UPDATE
  SET value = excluded.value, description = excluded.description,
      updated_at = now(), updated_by = excluded.updated_by;
