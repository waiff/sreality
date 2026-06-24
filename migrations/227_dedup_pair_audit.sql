-- Dedup v2 Phase 4: per-pair decision audit. One append-only row per pair the
-- engine actually DECIDED in a run (merged / dismissed / queued) — NOT the
-- millions of pairs rejected on cheap metadata (the dedup_engine_runs.rejected
-- count covers those). Answers, for the operator, "what happened to this pair,
-- at which stage, with what evidence" — the history surface reads it.
--
-- run_at groups one run's decisions (the engine stamps every row in a run with
-- the same start time; dedup_engine_runs is written at finalize, so no FK).

create table if not exists dedup_pair_audit (
  id                 bigserial primary key,
  run_at             timestamptz not null,
  left_sreality_id   bigint,
  right_sreality_id  bigint,
  left_property_id   bigint,
  right_property_id  bigint,
  category_main      text,
  stage              text not null,   -- address | phash | site_plan | visual | visual_skip
  outcome            text not null,   -- merged | dismissed | queued
  detail             jsonb,           -- reason, room_type, verdict, cosine, model, …
  created_at         timestamptz not null default now()
);

create index if not exists dedup_pair_audit_run_idx     on dedup_pair_audit (run_at desc);
create index if not exists dedup_pair_audit_outcome_idx on dedup_pair_audit (outcome);
create index if not exists dedup_pair_audit_left_idx    on dedup_pair_audit (left_property_id);
create index if not exists dedup_pair_audit_right_idx   on dedup_pair_audit (right_property_id);
