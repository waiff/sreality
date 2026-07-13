-- 303: dedup vision-model bake-off results (Session 3) — the per-pair, per-model, per-lane
-- verdict matrix behind the /model-testing explorer page.
--
-- scripts/validate_vision_models.py --persist-results writes one row per (model, lane, pair,
-- room) evaluation it makes, so the operator can scroll pair-by-pair and see every candidate
-- model's verdict side by side against ground truth (which is why the cheap models fail: they
-- emit the dangerous verdict — compare High / floor same_layout / site same_unit — on
-- confirmed-DIFFERENT pairs; the aggregate precision numbers in
-- docs/design/dedup-vision-model-bakeoff-2026-07.md are computed FROM this table).
--
-- Not a production dedup path: the harness writes NO production verdict cache; this table is a
-- benchmark artifact only. Anon reads a display view (definer-rights, like images_public); the
-- base table is RLS-locked so anon can't touch it directly (matches the 299/285 anon posture).

create table if not exists dedup_vision_bakeoff_results (
    id             bigserial primary key,
    run_label      text not null,        -- groups one bake-off batch (e.g. '2026-07-13-session3')
    set_name       text not null,        -- golden set the pair provenance traces to
    check_type     text not null,        -- 'recall' (reproduce a cached decisive verdict) | 'precision' (avoid danger on a confirmed-different pair)
    lane           text not null,        -- 'compare' | 'floor_plan' | 'site_plan'
    model          text not null,
    sreality_id_a  bigint not null,
    sreality_id_b  bigint not null,
    room_type      text,
    is_same        boolean,              -- ground truth; precision pairs = false, recall pairs = null (verdict-derived, not operator-confirmed)
    label_source   text,
    category_main  text,
    expected_verdict  text,              -- recall: the cached verdict to reproduce; precision: null (any non-danger verdict is safe)
    danger_verdict    text not null,     -- the lane's dangerous verdict for this check
    candidate_verdict text not null,     -- what the candidate model actually said
    is_correct     boolean not null,     -- recall: candidate == expected; precision: candidate != danger
    is_dangerous   boolean not null,     -- candidate emitted the lane's dangerous verdict
    cost_usd       numeric,
    created_at     timestamptz not null default now(),
    constraint dedup_vision_bakeoff_results_lane_check
        check (lane in ('compare', 'floor_plan', 'site_plan')),
    constraint dedup_vision_bakeoff_results_check_type_check
        check (check_type in ('recall', 'precision'))
);

create index if not exists dedup_vision_bakeoff_results_run_pair_idx
    on dedup_vision_bakeoff_results (run_label, sreality_id_a, sreality_id_b);
create index if not exists dedup_vision_bakeoff_results_run_model_lane_idx
    on dedup_vision_bakeoff_results (run_label, model, lane);

alter table dedup_vision_bakeoff_results enable row level security;
-- Intentionally NO anon/authenticated policy: direct base-table access is denied; anon reads
-- only the display view below, which runs with the (postgres) owner's rights and so bypasses RLS
-- — the same definer-view pattern images_public / dedup_funnel_public use.

create or replace view dedup_vision_bakeoff_results_public as
select id, run_label, set_name, check_type, lane, model,
       sreality_id_a, sreality_id_b, room_type, is_same, label_source, category_main,
       expected_verdict, danger_verdict, candidate_verdict, is_correct, is_dangerous,
       cost_usd, created_at
from dedup_vision_bakeoff_results;

grant select on dedup_vision_bakeoff_results_public to anon, authenticated;

comment on table dedup_vision_bakeoff_results is
    'Per-pair/model/lane verdict matrix from scripts/validate_vision_models.py --persist-results '
    '(Session 3 vision bake-off). Benchmark artifact, not a production verdict cache. '
    'Explorer: /model-testing. Findings: docs/design/dedup-vision-model-bakeoff-2026-07.md.';
