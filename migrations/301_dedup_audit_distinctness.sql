-- 301_dedup_audit_distinctness.sql
-- Defense-in-depth for the merge/audit identity-integrity work (see
-- docs/design/dedup-geo-town-pin-false-merge.md). A "merged" pair is always TWO
-- distinct properties AND two distinct listings; the self-paired-LISTING display
-- bug (PR #778) came from denormalizing the drifting properties.repr_listing_id.
-- Both write paths now GUARANTEE distinctness independently:
--   - engine: classify's `same_listing` reject (toolkit/dedup_engine.py) fires
--     before any merge/audit — 0 engine self-paired rows written since 2026-06-24.
--   - operator: listing identity resolved from the property_merge_events ledger,
--     not repr_listing_id (PR #778) — never yields an equal pair.
-- So these CHECKs can only ever catch a future REGRESSION, never legitimate data.
--
-- DISTINCTNESS (inequality), NOT ordering: left/right and survivor/retired are
-- semantically unordered here (43,615 dedup_pair_audit + 20 property_merge_events
-- rows legitimately have left/survivor > right/retired — an operator may choose any
-- survivor), so a `<` ordering constraint would be WRONG. `<>` yields UNKNOWN on a
-- NULL side and a CHECK passes on UNKNOWN, so a future property-only audit row
-- (NULL sreality ids) stays legal.

-- Property grain: 0 live violations -> add + validate immediately.
alter table property_merge_events
  add constraint property_merge_events_distinct
  check (survivor_property_id <> retired_property_id) not valid;
alter table property_merge_events
  validate constraint property_merge_events_distinct;

alter table dedup_pair_audit
  add constraint dedup_pair_audit_distinct_property
  check (left_property_id <> right_property_id) not valid;
alter table dedup_pair_audit
  validate constraint dedup_pair_audit_distinct_property;

-- Listing grain: ~4,542 pre-existing legacy self-paired rows (immutable, append-only
-- history; their DISPLAY is repaired at read time from the merge ledger in
-- api.property_dedup.list_pair_audit). Add NOT VALID so every FUTURE write is
-- enforced without rewriting history; deliberately NOT validated (would fail on the
-- legacy rows). Revisit a one-off backfill + VALIDATE only if the legacy rows are
-- ever migrated.
alter table dedup_pair_audit
  add constraint dedup_pair_audit_distinct_listing
  check (left_sreality_id <> right_sreality_id) not valid;
