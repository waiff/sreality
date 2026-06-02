-- 139_dedup_auto_merge_toggle.sql
--
-- Operator on/off switch for the dedup engine's automatic merging, toggled
-- from the top of the /dedup page. When true (default), the engine auto-merges
-- high-confidence matches (exact address, near-identical photos, a High visual
-- verdict). When false, the engine still finds candidates but QUEUES all of
-- them for manual review on /dedup instead of merging — and skips the paid
-- forensic vision step. Read by scripts/dedup_engine.py at run start.

INSERT INTO app_settings (key, value, description, updated_by)
VALUES (
  'dedup_auto_merge_enabled',
  'true'::jsonb,
  'When true (default), the dedup engine auto-merges high-confidence matches '
  '(exact address / near-identical photos / High visual verdict). When false, '
  'the engine still finds candidates but queues ALL of them on /dedup for '
  'manual review instead of merging, and skips the forensic vision step. '
  'Toggle from the top of the Dedup page.',
  'migration'
)
ON CONFLICT (key) DO NOTHING;
