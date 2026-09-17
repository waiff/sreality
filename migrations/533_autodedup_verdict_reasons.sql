-- 533: why the operator ruled the way they did — `autodedup.verdicts.reasons`.
--
-- WHY. The verdict says WHAT the operator decided; nothing said what they SAW. A chip like
-- `floor_plan_differs` on a pair the engine merged names the evidence the features missed,
-- and the reason histogram is directly comparable with the judge's `unit_discriminator` —
-- which is the feature-gap loop of PROGRAM.md §9, not decoration. The free-text `note` has
-- been on the table since 528; it is not aggregatable, so the structured codes ride beside
-- it rather than inside it.
--
-- NO CHECK CONSTRAINT ON THE VOCABULARY, deliberately, unlike 532's `verdict` domain. The
-- API (`autodedup/verdict_reasons.py`) is the ONLY writer — there is no browser path into
-- this schema — and it validates every code before the insert, so a check here would buy
-- nothing and cost a migration every time a review session names a shape the list misses.
-- The verdict domain is different in kind: it drives must-not-link and the training labels.
--
-- `not null default '{}'` so every existing row reads as "no reason given" rather than NULL,
-- and `unnest` over the column in the stats histogram never has to guard for it.
--
-- ADDITIVE and idempotent: `add column if not exists`, and a default that Postgres 11+ fills
-- in the catalog rather than by rewriting the table. `lock_timeout` is still set — the DDL
-- takes an ACCESS EXCLUSIVE lock and must not queue behind a long reader. No new grants: the
-- table's own grants cover its columns, and nothing outside the admin API reads the schema.

set lock_timeout = '5s';

alter table autodedup.verdicts
  add column if not exists reasons text[] not null default '{}';
