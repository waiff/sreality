-- 554_sreality_price_unit_leftovers_speak_the_canon.sql
-- FIELD CAPTURE W5 -- the last 31,911 sreality rows join the two-member price_unit canon.
--
-- W5 (#1574) made `price_unit` two canonical members -- 'za nemovitost' / 'za mesic' --
-- and the re-parse seam (reparse.yml, run 35700571901) renamed 211,820 sreality rows by
-- replaying the parser over their stored payload. It could not reach 31,911 rows: the
-- portal's OLDEST rows were stored before the client unwrapped the estate object, so
-- `parse_listing` raises on them (the R9 limit recorded in PROGRAM.md). They still carry
-- the retired spellings 'celkem' (16 active / 14,725 inactive) and 'měsíc' (12 / 17,158),
-- counted 2026-09-22 09:05Z -- and so `count(distinct price_unit)` cannot reach 2, the
-- W5 gate, while a Browse filter on the unit misses 28 live listings.
--
-- A spelling is a pure rename: the same two facts, the canon's words. This is a DATA
-- migration, not schema -- the sanctioned path for a heal the seam cannot make, applied
-- through apply_migration.yml. It obeys the seam's rules: no listing_snapshots row (sreality
-- hashes the RAW payload, so a derived spelling never enters its hash), no last_seen_at,
-- and every touched row's property is enqueued for the maintenance lane in the same
-- statement (rule 20). Idempotent: a second run finds nothing to rename.
--
-- Verify before/after:
--   select price_unit, count(*) from listings where source='sreality' group by 1;
-- expected after: only 'za nemovitost' and 'za mesic' (plus NULL).

set lock_timeout = '5s';
set statement_timeout = '600s';

with renamed as (
  update listings l
     set price_unit = case l.price_unit
                        when 'celkem' then 'za nemovitost'
                        when 'měsíc'  then 'za mesic'
                      end
   where l.source = 'sreality'
     and l.price_unit in ('celkem', 'měsíc')
  returning l.property_id
)
insert into dirty_properties (property_id)
select distinct property_id from renamed where property_id is not null
on conflict (property_id) do update set marked_at = now();
