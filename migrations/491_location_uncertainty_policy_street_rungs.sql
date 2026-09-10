-- 491_location_uncertainty_policy_street_rungs.sql
--
-- Two (position_source, granularity) pairs the v1 uncertainty ladder (migration 383)
-- never seeded, measured on the 2026-09-10 06:00Z resolve tick: of 500 listings drained,
-- 73 FAILED with "location_uncertainty_policy has no usable row" — 50 on
-- ('registry_point', 'street') and 23 on ('portal_pin_blurred', 'street_segment'). A
-- failed listing is retried on a later tick, so each failure spends the scarce drain
-- budget twice (GitHub fires the */15 schedule ~8x/day; a tick drains ~500 rows).
--
-- The radii follow the seeded ladder rather than inventing a band:
--   registry_point / street — a registry match that reached the street but not a house
--   number sits at a street-level point, the same statement 383 makes for portal_pin and
--   derived_geocode at 'street' (300 m, "street centroid").
--   portal_pin_blurred / street_segment — the blur fallback ladder is obec 1000 / quarter
--   750 / street 500; a blurred pin capped at street_segment is still a blur nobody
--   published a shape for, and the B3 band's floor is 500 m. A finer rung does not make the
--   portal's obfuscation smaller, so it takes the street value, not a smaller one.
-- Both stay geometric_bound / constant: r95_empirical is forbidden in v1 (uncalibrated,
-- 01 OQ4). Additive, applied once — like 383, no ON CONFLICT (the PK refuses a re-apply loudly).

insert into location_uncertainty_policy
  (policy_version, position_source, granularity, source, r95_m, radius_semantics, derivation, note) values
  ('v1', 'registry_point',     'street',         '*', 300, 'geometric_bound', 'constant',
   'registry street match without a house number: street centroid, as portal_pin/street'),
  ('v1', 'portal_pin_blurred', 'street_segment', '*', 500, 'geometric_bound', 'constant',
   'blur fallback at segment rung: the B3 band floor, same as street; a finer rung does not shrink a blur');
