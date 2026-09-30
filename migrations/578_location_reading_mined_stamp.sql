-- 578: the claim lane's stamp on a stored reading — `listing_description_enrichments.mined_contract_version`.
--
-- Location reader W3 (final-plan D1/D5): the text lane's reading (migration 552's row) is the
-- claim lane's third substrate. `location_data.claims_intake` mines a listing's CURRENT reading
-- and stamps it `<source>@<version>` (as `bodies_cursor_versions` spells it), clearing the
-- listing's other stamps in the same statement, so a reading that becomes current again (a model
-- rolled back, a text that reverts) is mined again. NULL: never mined, or no longer current.
-- No index (final-plan §10: the selector reads the table once a pass, 0.6 s on prod).
--
-- ADDITIVE and catalog-only. Apply BEFORE the code merges: the hourly intake runs from main.

set lock_timeout = '5s';

alter table listing_description_enrichments add column if not exists mined_contract_version text;

comment on column listing_description_enrichments.mined_contract_version is
  'The portal contract (<source>@<version>) the claim lane mined this reading under; NULL when '
  'never mined or superseded by another reading of the same listing (migration 578, W3).';

reset lock_timeout;
