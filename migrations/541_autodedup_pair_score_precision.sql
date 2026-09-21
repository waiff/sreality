-- 541: `autodedup.pairs.score` is the clustering's SORT KEY, so the store must carry it (E115).
--
-- WHY. `cluster.edge_rank` orders a component's merge edges `(certificate first, -score, lo,
-- hi)` and the invariants that then refuse a union — `floor_spread`, the size cap,
-- must-not-link — are NOT transitively closed: one of {a,c} and {b,c} survives and which one
-- is decided by the order. The batch score lane ranks the float64 `Decision`s in the process
-- that computed them; the real-time lane ranks `Decision`s rebuilt from these rows
-- (`incremental._recluster` -> `pairs_within` -> `PairRow.decision()`). While this column was
-- `real` those were two different numbers.
--
-- MEASURED (W9l, 2026-09-21, generation `rt` against `rt_base_w13` on export 35534165336):
-- 130 distinct float4 values carried 4,918 merge edges and 4,800 of them (97.6%) sat in one of
-- the 12 buckets holding more than one float64 score, so `-score` tied and the rank fell
-- through to `(lo, hi)`. Clustering the SAME decisions twice — once on float64, once narrowed
-- the way this column narrows — gave 903 groups against 900, with 74/71 member sets one-sided
-- and `size` refusals 14 against 289: the shape and the order of the live store's own 81/76.
-- A generation was therefore not re-clusterable from its own rows.
--
-- WHAT THIS DOES. Widens the score to `double precision`, which is the type the engine decides
-- in. Widening is value-preserving — every stored float4 has an exact float8 image, no row
-- changes meaning and no zone moves — so this is additive in effect: it does not repair the
-- generations already written (their rows were narrowed when they were written and the bits
-- are gone), it makes the NEXT generation re-clusterable. A re-score or a re-seed is what
-- carries an existing generation across.
--
-- COST. One table rewrite. `autodedup.pairs` was 93,935 rows / 152 MB on 2026-09-21, the
-- schema is shadow-only (ruling D4: nothing here is read by Browse, the API or the SPA), and
-- the two score indexes are rebuilt by the ALTER itself.
--
-- `clusters.min_edge_score` / `mean_edge_score` are deliberately LEFT `real`: they are report
-- columns and a UI sort key, and nothing re-clusters from them.

set lock_timeout = '5s';

alter table autodedup.pairs
  alter column score type double precision;

comment on column autodedup.pairs.score is
  'The decision''s score, in the precision the engine decides in. `cluster.edge_rank` RANKS on '
  'this column, so a lane that re-clusters from stored rows must read back the number the '
  'lane that wrote them ranked — see migration 541 and autodedup/store_score.py.';
