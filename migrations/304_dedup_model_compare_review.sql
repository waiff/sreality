-- 304: model-comparison on the review queue (decision support).
--
-- The operator, mid-review on a pair the engine couldn't decide, clicks "compare all models" on
-- /dedup. The API snapshots the exact undecided pair(s) into dedup_model_compare_sets under a fresh
-- run_label, dispatches the vision harness once per connected model (review mode), and each model's
-- verdict lands in dedup_vision_bakeoff_results (migration 303) as a check_type='review' row — no
-- ground truth, just "how did every model vote". The operator reads the jury on /model-testing.
--
-- Why a snapshot table (not a live re-read of the queue per model): the ~5 per-model dispatches fire
-- seconds apart; snapshotting the pairs once guarantees every model scores the IDENTICAL set, so the
-- side-by-side grid is honest. Pairs are property-grain in property_identity_candidates; the snapshot
-- stores the resolved representative-listing sreality_ids (the media handle the harness needs).

create table if not exists dedup_model_compare_sets (
    id             bigserial primary key,
    run_label      text not null,
    sreality_id_a  bigint not null,
    sreality_id_b  bigint not null,
    left_property_id  bigint,
    right_property_id bigint,
    category_main  text,
    candidate_id   bigint,          -- property_identity_candidates.id this pair came from (NULL for an ad-hoc pair)
    created_at     timestamptz not null default now(),
    unique (run_label, sreality_id_a, sreality_id_b)
);
create index if not exists dedup_model_compare_sets_run_label_idx
    on dedup_model_compare_sets (run_label);

comment on table dedup_model_compare_sets is
    'Frozen pair snapshot for one /dedup "compare all models" click; scripts/validate_vision_models.py '
    '--review-set-name reads it and writes check_type=''review'' rows to dedup_vision_bakeoff_results. '
    'Not a production dedup path — decision-support only.';

-- Widen the results table for review rows: a third check_type, and is_correct becomes nullable
-- (a review pair has no ground truth, so "correct" is undefined — the signal is is_dangerous, i.e.
-- did the model vote to MERGE).
alter table dedup_vision_bakeoff_results
    drop constraint if exists dedup_vision_bakeoff_results_check_type_check;
alter table dedup_vision_bakeoff_results
    add constraint dedup_vision_bakeoff_results_check_type_check
    check (check_type in ('recall', 'precision', 'review'));
alter table dedup_vision_bakeoff_results
    alter column is_correct drop not null;
