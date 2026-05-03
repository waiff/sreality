-- 005_total_floors.sql
-- Promote the building's total floor count from the "Podlaží" raw_json
-- item to a typed column.
--
-- The raw text takes one of two shapes:
--   "3. podlaží"                 -> total unknown, total_floors stays NULL
--   "3. podlaží z celkem 5"      -> total_floors = 5
--
-- Coverage at apply time: ~62% of listings include the "z celkem N"
-- suffix. The remaining 38% leave total_floors NULL. Downstream code
-- must handle NULL gracefully (e.g. a "top floor" derivation only
-- works when total_floors IS NOT NULL).
--
-- The existing `floor` column is unchanged. Both numbers together
-- let analytics distinguish "3rd of 3" (top, often discounted for
-- roof-leak risk) from "3rd of 8" (mid-rise).

alter table listings
  add column total_floors integer;

create index on listings (total_floors);

update listings
set total_floors = (regexp_match(
  jsonb_path_query_first(
    raw_json, '$.items[*] ? (@.name == "Podlaží").value'
  ) #>> '{}',
  'z celkem (\d+)'
))[1]::integer
where jsonb_path_query_first(
  raw_json, '$.items[*] ? (@.name == "Podlaží").value'
) #>> '{}'
  like '%z celkem%';
