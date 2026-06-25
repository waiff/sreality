-- 235_dedup_pair_audit_sreality_idx.sql
-- Additive: index the per-listing columns of the decision ledger so a
-- property-scoped Decision-history query (the listing-detail "merge decisions"
-- link) can find the audit rows touching a property's child listings without a
-- full scan. Migration 227 indexed left/right_property_id but not the
-- sreality_id columns; the scope filter keys on sreality_id (the stable id —
-- property_id re-points on every merge, sreality_id never moves).

create index if not exists dedup_pair_audit_left_sid_idx
  on dedup_pair_audit (left_sreality_id)
  where left_sreality_id is not null;

create index if not exists dedup_pair_audit_right_sid_idx
  on dedup_pair_audit (right_sreality_id)
  where right_sreality_id is not null;
