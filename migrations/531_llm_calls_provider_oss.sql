-- 531: admit provider 'oss' on llm_calls (api/providers/oss.py, the OSS-vs-judge arm).
--
-- WHY THIS IS A LAUNCH GATE, NOT A FOLLOW-UP. `LLMClient.call` INSERTs one `llm_calls` row
-- per call — on success (`_record_call`) AND on failure (`_record_failure`) — and migration
-- 302 pinned the provider domain to ('anthropic','gemini','openai','qwen'). Without this file
-- the FIRST tier=oss judgement is produced by the GPU and then dies at the recording step with
-- "new row for relation llm_calls violates check constraint llm_calls_provider_check", AFTER
-- the verdict exists and BEFORE it is persisted. The judge lane counts that as a failed pair
-- and takes the next one, so nothing stops: the rented pod bills until the budget cap, writes
-- zero judgements, and leaves no audit row either (the failure INSERT carries the same illegal
-- provider). Migration 530 widened the judgement TIERS; this widens the CALLER.
--
-- Exactly the 302 shape: a CHECK's IN-list cannot be widened in place, so drop + re-add. Two
-- differences, both deliberate:
--   * the new constraint is NAMED (`llm_calls_provider_ck`), so the next widening does not have
--     to know what Postgres auto-named the inline one, and both spellings are dropped IF EXISTS
--     so re-applying this file is a no-op;
--   * it is added NOT VALID and validated in a second statement. llm_calls is a high-traffic
--     append table; `add constraint ... check` would hold ACCESS EXCLUSIVE for a full-table
--     scan, while NOT VALID takes that lock for an instant and `validate constraint` runs its
--     scan under SHARE UPDATE EXCLUSIVE, which does not block the writers. The accepted set
--     only GROWS, so no existing row can fail the validation.

set lock_timeout = '5s';

alter table llm_calls drop constraint if exists llm_calls_provider_check;
alter table llm_calls drop constraint if exists llm_calls_provider_ck;
alter table llm_calls add constraint llm_calls_provider_ck
    check (provider in ('anthropic', 'gemini', 'openai', 'qwen', 'oss')) not valid;

reset lock_timeout;

alter table llm_calls validate constraint llm_calls_provider_ck;
