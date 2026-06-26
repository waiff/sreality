-- 248_dedup_decision_feedback.sql
--
-- Operator feedback on dedup decisions: "this merge / dismissal was WRONG", with a
-- free-text note + the expected correct outcome, so the accumulated flags become a
-- labelled corpus for improving the dedup flow.
--
-- Grain = the canonical LISTING PAIR (left_sreality_id < right_sreality_id), NOT an
-- audit-row id. A pair surfaces on TWO surfaces — the Decision history feed
-- (dedup_pair_audit, terminal merged/dismissed) AND the Needs-review queue
-- (property_identity_candidates, not-yet-decided). Keying on the pair lets ONE flag
-- attach to whichever surface shows that pair (and persist across the pair's
-- lifecycle: a candidate flagged "should dismiss" stays flagged after it becomes a
-- terminal decision). sreality_id is the stable listing identity (never re-points on
-- merge, unlike property_id), so the pair key is durable.
--
-- One row per pair (UNIQUE), upserted by the bearer-gated API. Un-flagging deletes the
-- row. No FK to dedup_pair_audit / property_identity_candidates: the subject is the
-- real-world pair, which may exist on either, both, or (after a queue archive-reset)
-- neither — the flag is the operator's durable judgement about the pair, decoupled from
-- which transient row currently represents it.
--
-- Single-operator identity model (no user_id), same as filter_presets /
-- notification_subscriptions. No RLS policies: writes flow through the bearer-gated
-- FastAPI service (service-role connection); the browser never writes directly, and the
-- anon role never reads this operator-internal table.

begin;

create table dedup_decision_feedback (
    id                bigint generated always as identity primary key,
    left_sreality_id  bigint      not null,
    right_sreality_id bigint      not null,
    is_incorrect      boolean     not null default true,
    -- What the operator says SHOULD have happened, so the corpus can be sliced into
    -- "wrong merges" (should_dismiss) vs "wrong dismissals" (should_merge). NULL =
    -- flagged wrong but direction left unspecified.
    expected_outcome  text        check (expected_outcome in ('should_merge', 'should_dismiss', 'unsure')),
    note              text,
    category_main     text,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    created_by        text        not null default 'operator',
    constraint dedup_decision_feedback_pair_canonical check (left_sreality_id < right_sreality_id),
    constraint dedup_decision_feedback_pair_unique unique (left_sreality_id, right_sreality_id)
);

-- The "show only flagged-incorrect" filter scans a small set; a partial index keeps it cheap.
create index dedup_decision_feedback_incorrect_idx
    on dedup_decision_feedback (left_sreality_id, right_sreality_id)
    where is_incorrect;

alter table dedup_decision_feedback enable row level security;

create or replace function dedup_decision_feedback_touch_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

create trigger dedup_decision_feedback_touch_updated_at_trg
    before update on dedup_decision_feedback
    for each row execute function dedup_decision_feedback_touch_updated_at();

comment on table dedup_decision_feedback is
    'Operator "this dedup decision was wrong" flags + notes, keyed on the canonical '
    'listing pair (left_sreality_id < right_sreality_id). Spans the Decision history '
    'feed and the Needs-review queue; a labelled corpus for improving the dedup flow. '
    'Writes via the bearer-gated API only; anon never reads it.';
comment on column dedup_decision_feedback.expected_outcome is
    'should_merge | should_dismiss | unsure — what the operator says the engine '
    'SHOULD have done, so the corpus splits into wrong-merge vs wrong-dismiss.';

commit;
