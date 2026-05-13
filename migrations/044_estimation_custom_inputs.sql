-- 042_estimation_custom_inputs.sql
--
-- Operator-supplied custom inputs that travel with an estimation:
--
--   1. estimation_runs.special_instructions / contextual_text — free-
--      form text that the agent receives inside fenced sections of the
--      initial user message. Lets the operator inject ground truth the
--      listing pages don't capture (legal status, corner unit, recent
--      heating refurb, etc.). Plain TEXT, immutability enforced in
--      api/estimation_runs.py (no PATCH endpoint exists).
--
--   2. building_runs.special_instructions / contextual_text — same two
--      fields on the building parent. The unit extractor consumes
--      these directly (single-shot vision call); per-unit child
--      estimations inherit them via the B2 orchestrator (forward-
--      compatible — B2 fan-out not wired in v1).
--
--   3. building_run_attachments — operator-uploaded images (photos,
--      floor plans, technical drawings) for one building run. Distinct
--      from `images` (sreality-scraped); the building flow is the only
--      surface that accepts attachments because the primary use case
--      is recovering floor-plan layout the listing didn't disclose.
--      Storage is Cloudflare R2 under the key prefix
--      `custom-attachments/building/{building_run_id}/{uuid}.{ext}`,
--      managed by api/attachments.py. The DB row is the audit record
--      + dedup key; bytes live in R2. ON DELETE CASCADE because the
--      attachments are meaningless without their parent building_run.
--
--   4. building_attachment_analyses — cache table for the new
--      `read_floor_plan` toolkit function (toolkit/floor_plan.py).
--      Same shape and rationale as listing_summaries (027) /
--      building_unit_extractions (036): the LLM is the source of
--      truth for the analysis, we cache locally to keep repeat calls
--      fast and cost-disciplined. Cache key is (attachment_id, model)
--      so a model bump invalidates without manual cleanup. Write-
--      allowed exception per CLAUDE.md toolkit rule #5.
--
--   5. llm_calls.called_for CHECK extended with 'read_floor_plan'
--      so the audit trail tags the new vision calls consistently
--      with summarize_listing / compare_listing_images /
--      extract_building_units.
--
-- RLS enabled on both new tables; no policies. The frontend never
-- reads them directly — attachments surface through
-- GET /buildings/{id}/attachments and the analysis cache is internal
-- to the toolkit.

------------------------------------------------------------------
-- 1. estimation_runs + building_runs text columns
------------------------------------------------------------------

alter table estimation_runs
  add column special_instructions text,
  add column contextual_text      text;

alter table building_runs
  add column special_instructions text,
  add column contextual_text      text;

------------------------------------------------------------------
-- 2. building_run_attachments
------------------------------------------------------------------

create table building_run_attachments (
  id              bigserial primary key,
  building_run_id bigint not null
    references building_runs(id) on delete cascade,
  storage_key     text not null unique,
  filename        text not null,
  mime_type       text not null
    check (mime_type in ('image/png', 'image/jpeg', 'image/webp')),
  byte_size       integer not null check (byte_size > 0),
  width_px        integer,
  height_px       integer,
  sha256_hex      text not null,
  uploaded_by     text
    check (uploaded_by is null or uploaded_by in ('ui', 'api', 'clickup')),
  created_at      timestamptz not null default now(),
  unique (building_run_id, sha256_hex)
);

create index on building_run_attachments (building_run_id, created_at);

alter table building_run_attachments enable row level security;

------------------------------------------------------------------
-- 3. building_attachment_analyses
------------------------------------------------------------------

create table building_attachment_analyses (
  id              bigserial primary key,
  attachment_id   bigint not null
    references building_run_attachments(id) on delete cascade,
  model           text not null,
  analysis        jsonb not null,
  llm_call_id     bigint references llm_calls(id) on delete set null,
  cost_usd        numeric(10, 6),
  created_at      timestamptz not null default now(),
  unique (attachment_id, model)
);

create index on building_attachment_analyses (attachment_id, created_at desc);

alter table building_attachment_analyses enable row level security;

------------------------------------------------------------------
-- 4. llm_calls.called_for: add 'read_floor_plan'
------------------------------------------------------------------

alter table llm_calls
  drop constraint llm_calls_called_for_check,
  add constraint llm_calls_called_for_check
    check (called_for in (
      'parse_url', 'summarize_listing', 'compare_listing_images',
      'agent_estimation', 'extract_building_units', 'read_floor_plan'
    ));
