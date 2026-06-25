-- 235_dedup_batch_floor_plan_kind.sql
-- Widen dedup_batch_requests.kind for the floor-plan validation gate (migration 234).
-- The batch warm-up lane (_warm_floor_plan) now enqueues 'floor_plan' requests; the
-- migration-197 CHECK only allowed classify/compare/site_plan, so the first such INSERT
-- would trip the constraint in PRODUCTION — invisible to CI, whose fake conn ignores
-- constraints. Additive: re-points the same CHECK with the new value.
alter table dedup_batch_requests drop constraint if exists dedup_batch_requests_kind_check;
alter table dedup_batch_requests add constraint dedup_batch_requests_kind_check
  check (kind in ('classify', 'compare', 'site_plan', 'floor_plan'));
