-- 576: which pair list a judge mark was requested under — `autodedup.judgements.stratum`.
--
-- The list (a label round's contested set, a sealed control draw) lived only in a pair file on
-- a branch and a 30-day Actions artifact; `judge_version` names the prompt, not the round. The
-- Judge page filters on it ("Seznam", PROGRAM.md E922). The judge lane is the only writer: the
-- list's stamp on each pair, and on older marks when the list is dispatched again (cached, free).
--
-- ADDITIVE: a nullable column is a catalog-only change. Apply it BEFORE the code merges — the
-- lane's upsert names the column.

set lock_timeout = '5s';

alter table autodedup.judgements add column if not exists stratum text;

comment on column autodedup.judgements.stratum is
  'The pair list''s stamped stratum this mark was requested under (e.g. g2:s3_mf_band); NULL '
  'for a pair the judge lane drew itself or a mark written before migration 576.';

reset lock_timeout;
