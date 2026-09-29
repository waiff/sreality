-- 576: which pair list a judge mark was requested under — `autodedup.judgements.stratum`.
--
-- WHY. A mark is training data for as long as the program keeps it, but the list that asked
-- for it (a label round's contested set, a sealed control draw) lived only in the pair file
-- on a branch and in a 30-day Actions artifact; `judge_version` names the prompt, not the
-- round. The Judge page (`/autodedup/judge`) filters on this column ("Seznam"), and a mark
-- whose provenance expires with an artifact is a mark nobody can audit.
--
-- The judge lane is the only writer: it stores the stratum the pair list STAMPED on the pair,
-- and stamps marks written before this column existed when the same list is dispatched again
-- (cached, so free). NULL = a pair the lane drew itself from the zone grid, or a mark nobody
-- has re-stamped yet.
--
-- ADDITIVE and idempotent: a nullable column is a catalog-only change. Apply it BEFORE the
-- code that writes it merges — the lane's upsert names the column.

set lock_timeout = '5s';

alter table autodedup.judgements add column if not exists stratum text;

comment on column autodedup.judgements.stratum is
  'The pair list''s stamped stratum this mark was requested under (e.g. g2:s3_mf_band); NULL '
  'for a pair the judge lane drew itself or a mark written before migration 576.';

reset lock_timeout;
