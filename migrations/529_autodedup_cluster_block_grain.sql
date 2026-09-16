-- 529: keep the BLOCK GRAIN on an autodedup cluster (docs/design/autodedup/PROGRAM.md §8).
--
-- `fingerprint.block_key_of` returns a grain-PREFIXED key — `c490245` (a část obce) or
-- `o563510` (an obec) — precisely so that a quarter code can never collide numerically with
-- a town code. Migration 528 typed `clusters.block_key` as a bigint, so the lane had to drop
-- that prefix to store it, and two different blocks that happen to share a RÚIAN number
-- became one value: a silent conflation in every `?block=` filter over the table.
--
-- ADDITIVE and narrow: one nullable text column holding the grain letter, so `block_key`
-- keeps the readable numeric code (what an operator types) and the pair (block_key,
-- block_grain) is the lossless key again. Re-typing `block_key` itself to text would be the
-- other fix; it is not taken because the UI's read SQL casts the filter to bigint, and a
-- type change under a live reader buys nothing this column does not.
--
-- NULL on every row written before this migration, and on any cluster whose members span
-- more than one block (that cluster stores no block_key either).

alter table autodedup.clusters
  add column if not exists block_grain text;

comment on column autodedup.clusters.block_grain is
  'Grain of block_key: c = cast obce (quarter), o = obec (town). NULL when the cluster
   spans more than one block.';
