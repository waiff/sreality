-- 556_enrichment_model_is_the_bakeoff_winner.sql
-- FIELD CAPTURE W7 -- the text lane's one switch names the model the bake-off chose.
--
-- `app_settings.enrichment_model` still held 'gpt-5-mini', the DELETED lane's leftover
-- (never a chosen value: migration 546's header records it as such). The W7 bake-off
-- (docs/design/field-capture/PROGRAM.md, section 5 W7) scored every arm on the R7 gate,
-- precision >= 95 % where the model answers (floor: within +-1), over a 1,387-advert panel
-- labelled by the structured portals' own fields; the two open-source arms were re-run
-- once the floor labels were made canonical (runs 35760770180, 35763297512):
--
--   gpt-5.6-luna      floor 95.6 %  has_lift 96.7 %  energy 91.8 %   $0.47 / 1k adverts, p50 2.2 s
--   Gemma-4-26B-A4B   floor 97.5 %  has_lift 96.8 %  energy 94.0 %   $0.43 / 1k (A100 hours, pod saturated)
--   Qwen3-VL-32B      floor 97.2 %  has_lift 97.0 %  energy 95.3 %   $0.97 / 1k (A100 hours, pod saturated)
--
-- All three clear floor and has_lift. luna is the lane's model on the COST MODEL THAT
-- APPLIES TO A LANE, not to a batch: the oss figures assume a saturated pod, and a lane
-- polling every 300 s at bazos's ~1,250 adverts/day cannot saturate one -- a dedicated
-- A100 is ~$38/day against ~$0.60/day for luna at that inflow, and renting per pass costs
-- a 7-10 minute boot per 5-minute pass. Gemma is the right arm for a one-off backlog
-- drain (50k adverts: ~$21 vs ~$24), which is a batch job, not this setting.
-- energy_rating stays closed: only Qwen reached 95 % (95.3 %, n=255), and not the model
-- named here.
--
-- Data, not schema: one row. Reversible -- migration 020's trigger archives the previous
-- value into app_settings_history. The gates themselves are contract DATA in
-- scraper/attribute_contract.py (same PR), never a setting.

set lock_timeout = '5s';

update app_settings
   set value = to_jsonb('gpt-5.6-luna'::text)
 where key = 'enrichment_model'
   and value <> to_jsonb('gpt-5.6-luna'::text);
