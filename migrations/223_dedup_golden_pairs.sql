-- Labeled listing pairs for measuring the dedup / property-identity engine's
-- precision and recall. Append-only evaluation dataset (NOT operational state — the
-- engine never reads it): built by scripts/build_golden_set.py and read by
-- scripts/eval_identity.py, which gates any change to the matching rules at a
-- per-category precision floor before it may auto-merge.
--
-- Grain is the LISTING pair (sreality_id × sreality_id), canonical-ordered, because
-- listings are stable (never deleted, rule #3) whereas a property can merge away.
-- Positives come from operator/engine-confirmed merges (property_merge_events);
-- negatives from structurally-distinct units that share a coordinate (the apartment
-- "coordinate trap": same building, different disposition => different unit).

create table if not exists dedup_golden_pairs (
    id                 bigint generated always as identity primary key,
    left_sreality_id   bigint not null,
    right_sreality_id  bigint not null,
    is_same            boolean not null,       -- ground truth: same real-world property?
    category_main      text,                   -- pair's category family (byt/dum/pozemek/...)
    stratum            text not null,          -- 'positive' | 'negative'
    basis              text not null,          -- how it was sampled (provenance)
    note               text,
    created_at         timestamptz not null default now(),
    constraint dedup_golden_pairs_ordered check (left_sreality_id < right_sreality_id),
    constraint dedup_golden_pairs_uniq unique (left_sreality_id, right_sreality_id)
);

comment on table dedup_golden_pairs is
  'Labeled listing pairs (same vs distinct) for dedup precision/recall eval; built by '
  'scripts/build_golden_set.py, read by scripts/eval_identity.py. Not operational state.';
