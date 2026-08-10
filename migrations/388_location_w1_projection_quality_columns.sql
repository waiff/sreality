-- 388_location_w1_projection_quality_columns.sql
--
-- Location-data W1, resolver PR remediation. Two additive columns on the
-- listing serving projection, plus the five location_field_policy v1 rows the
-- survivorship evaluator has no policy for.
--
-- WHY THE COLUMNS. 03-resolution-pipeline.md 3.10 lists, verbatim, the "columns
-- this section requires 01 to carry beyond what 01 section 7.1 already
-- declares": position_quality_class (3.8.6) and collision_epoch_id. 01 shipped
-- neither, so migration 384 created listing_location_current without them and
-- the projection builder had to POP both values before every upsert --
-- computing an answer and throwing it away. Consequences, all live:
--   * no consumer can ask the 3.8.6 question ("may this row enter a metric
--     radius / drawn-polygon set as `certain`?"), which is the ONE question the
--     class exists to answer;
--   * property_location_current picks its winner partly on the member's quality
--     class (projection._precision_key), and a column that is never stored reads
--     as NULL for every member, flattening that term to a constant;
--   * a resolution's fifth version input was unreadable from the serving row, so
--     "which epoch is this pin's collision evidence from?" needed a join back
--     through location_resolutions.
--
-- NULLABLE, not NOT NULL. The builder writes both on every upsert, but the
-- projection is a CACHE that predates this migration on any environment where
-- W1 already ran; a NOT NULL with no default would fail on a non-empty table and
-- an invented default would be a false statement about rows nobody rebuilt.
-- The 05 P5 "every axis NOT NULL" rule covers the FOUR canonical axes plus
-- blur_evidence and radius_semantics -- position_quality_class is explicitly
-- "an additional quality axis carried alongside them, never a replacement gate"
-- (03 3.8.6), so it is not in that set.
--
-- The CHECK carries the four-value vocabulary of 3.8.6 as text, not as an enum:
-- 01 declares no type for it, and inventing one here would put a new location
-- enum into the corpus without the design declaring its members.
--
-- WHY THE POLICY ROWS. location_field_policy is MUTABLE config (383's own
-- header: "adding a field or a per-portal override is a data change, never a
-- migration") -- these INSERTs are additive config, not a schema change to an
-- applied migration. The v1 seed covers ten claim types; core.SURVIVORSHIP_FIELDS
-- arbitrates thirteen. The five with no policy row at all --
--   evidencni, postal_town, development_name, cadastral_territory_name,
--   parcel_number
-- -- could never produce a winner: _best_policy returns None and the claim is
-- skipped, so the columns were structurally always NULL however many portals
-- claimed them. postal_town is the loudest: 03 names it as a field that is NEVER
-- reconciled against obec_name precisely because it is a real, separately-served
-- value (the two disagree on 57.0 % of bazos rows).
--
-- The ladder is 383's, verbatim: registry beats portal beats mined text (lower
-- rank wins), and the llm_text lane keeps D7's graded write-back guard
-- (may_overwrite_non_null = false, requires_independent_agreement = true).
-- ON CONFLICT DO NOTHING keeps it re-runnable.

begin;

set local lock_timeout = '5s';
set local statement_timeout = '60s';

------------------------------------------------------------------
-- 1. The two projection columns (03 section 3.10).
------------------------------------------------------------------

alter table listing_location_current
  add column if not exists position_quality_class text,
  add column if not exists collision_epoch_id     bigint references pin_cluster_epochs(id);

alter table listing_location_current
  add constraint llc_position_quality_class
  check (position_quality_class is null
         or position_quality_class in ('precise', 'approximate', 'area', 'none'));

comment on column listing_location_current.position_quality_class is
  'Derived gate for geometric consumers (03 3.8.6). Carried ALONGSIDE the four '
  'canonical axes, never in place of them: renderable_as_point and geo_blockable '
  'keep their granularity rung. Written by the projection builder only.';

comment on column listing_location_current.collision_epoch_id is
  'The pin_cluster_epochs row whose classification this projection consumed - '
  'the fifth version input of the resolution identity (00 10.3).';

create index llc_collision_epoch on listing_location_current (collision_epoch_id)
  where collision_epoch_id is not null;

------------------------------------------------------------------
-- 2. The five missing location_field_policy v1 rows.
------------------------------------------------------------------

insert into location_field_policy
  (policy_version, field, source_pattern, method_pattern, rank,
   min_confidence, may_fill_null, may_overwrite_non_null, requires_independent_agreement)
select 'v1', f.field, l.source_pattern, l.method_pattern, l.rank,
       l.min_confidence, l.may_fill_null, l.may_overwrite_non_null, l.requires_independent_agreement
from unnest(array[
       'evidencni', 'postal_town', 'development_name', 'cadastral_territory_name',
       'parcel_number'
     ]::location_claim_type[]) as f(field)
cross join (values
  ('ruian',     'registry_derived',        100, null::match_confidence, true, true,  false),
  ('portal:*',  'portal_structured_field', 300, null::match_confidence, true, true,  false),
  ('portal:*',  'html_selector_parse',     400, null::match_confidence, true, true,  false),
  ('llm_text',  'llm_text',                900, 'high'::match_confidence, true, false, true)
) as l(source_pattern, method_pattern, rank, min_confidence,
       may_fill_null, may_overwrite_non_null, requires_independent_agreement)
on conflict (policy_version, field, source_pattern, method_pattern) do nothing;

commit;
