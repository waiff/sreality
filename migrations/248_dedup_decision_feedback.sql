-- 248_dedup_decision_feedback.sql
--
-- Operator feedback on dedup decisions: "this merge / dismissal was WRONG", with a
-- free-text note + the expected correct outcome, so the accumulated flags become a
-- labelled corpus for improving the dedup flow.
--
-- Grain = the canonical PROPERTY PAIR (left_property_id < right_property_id), NOT a
-- listing-pair or an audit-row id. A pair surfaces on TWO surfaces — the Decision history
-- feed (dedup_pair_audit, terminal merged/dismissed) AND the Needs-review queue
-- (property_identity_candidates, not-yet-decided). Both record the two property_ids of the
-- decision: the audit row SNAPSHOTS them at decision time (immutable forever) and the
-- candidate carries the live pair (stable while it is pending — no merge has collapsed it
-- yet). So one flag attaches to whichever surface shows that pair, and FOLLOWS the pair
-- across its lifecycle: a candidate flagged "should dismiss" stays flagged once it becomes
-- a terminal decision (the merge/dismiss audit row carries the SAME two property_ids).
--
-- Why property-grain and not the listing (sreality_id) pair: a property's representative
-- listing (`repr_listing_id`) DRIFTS — `recompute_property_stats` re-picks it when the
-- current repr goes inactive — so a flag keyed on the repr listing pair would silently
-- orphan off the Needs-review card after a recompute. The property_id pair is the stable
-- identity of "these two real-world properties", on both surfaces.
--
-- One row per pair (UNIQUE), upserted by the bearer-gated API. Un-flagging deletes the
-- row. No FK to dedup_pair_audit / property_identity_candidates: the subject is the pair,
-- which may exist on either, both, or (after a queue archive-reset) neither.
--
-- Single-operator identity model (no user_id), same as filter_presets /
-- notification_subscriptions. No RLS policies: writes flow through the bearer-gated
-- FastAPI service (service-role connection); the browser never writes directly, and the
-- anon role never reads this operator-internal table.

begin;

create table dedup_decision_feedback (
    id                bigint generated always as identity primary key,
    left_property_id  bigint      not null,
    right_property_id bigint      not null,
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
    constraint dedup_decision_feedback_pair_canonical check (left_property_id < right_property_id),
    constraint dedup_decision_feedback_pair_unique unique (left_property_id, right_property_id)
);

-- The "show only flagged-incorrect" filter scans a small set; a partial index keeps it cheap.
create index dedup_decision_feedback_incorrect_idx
    on dedup_decision_feedback (left_property_id, right_property_id)
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
    'property pair (left_property_id < right_property_id). Spans the Decision history '
    'feed and the Needs-review queue; a labelled corpus for improving the dedup flow. '
    'Property-grain (not the drifting repr-listing pair) so a flag never orphans on a '
    'recompute. Writes via the bearer-gated API only; anon never reads it.';
comment on column dedup_decision_feedback.expected_outcome is
    'should_merge | should_dismiss | unsure — what the operator says the engine '
    'SHOULD have done, so the corpus splits into wrong-merge vs wrong-dismiss.';

commit;
