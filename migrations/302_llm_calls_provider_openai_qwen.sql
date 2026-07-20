-- 302: widen llm_calls.provider to allow 'openai' and 'qwen' (Session 3 vision
-- bake-off providers, api/providers/openai.py + api/providers/qwen.py).
--
-- Migration 029 originally scoped this CHECK to ('anthropic', 'gemini') — the only
-- two providers that existed then. A live smoke-test dispatch of the extended
-- scripts/validate_vision_models.py against qwen3-vl-30b-a3b-instruct failed with
-- "new row for relation llm_calls violates check constraint llm_calls_provider_check"
-- (caught by dispatching a tiny 2-pair smoke run BEFORE spending real money on the
-- full bake-off, not after). Can't incrementally widen a CHECK's IN-list, so drop +
-- recreate under the same (Postgres-auto-generated) name.
alter table llm_calls drop constraint llm_calls_provider_check;
alter table llm_calls add constraint llm_calls_provider_check
    check (provider in ('anthropic', 'gemini', 'openai', 'qwen'));
