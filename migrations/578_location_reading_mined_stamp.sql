-- 578: the claim lane's stamp on a stored reading — `listing_description_enrichments.mined_contract_version`.
--
-- Location reader W3 (final-plan D1/D5). The text lane's reading of an advert (the row
-- migration 552 keys on `(listing_id, extractor_version, text_hash)`) becomes the claim lane's
-- third substrate: `location_data.claims_intake` mines a listing's CURRENT reading into
-- `location_claims` and stamps it with the contract that mined it, spelled `<source>@<version>`
-- as `location_claim_batches.bodies_cursor_versions` spells it. The same statement clears the
-- stamps of the listing's other readings, so a reading that becomes current again (a model
-- rolled back, a text that reverts) is mined again. NULL: never mined, or no longer current.
--
-- ONE COLUMN, NO INDEX (final-plan §10): the selector reads this table once a pass, hash-free
-- (0.6 s on prod, 2026-09-30), and a partial index on unstamped rows dies at the second bump.
--
-- ADDITIVE: a nullable column with no default is a catalog-only change. Apply it BEFORE the
-- code merges — the hourly intake runs from main and its readings half names the column.

set lock_timeout = '5s';

alter table listing_description_enrichments add column if not exists mined_contract_version text;

comment on column listing_description_enrichments.mined_contract_version is
  'The portal contract (<source>@<version>) the claim lane mined this reading under; NULL when '
  'never mined or superseded by another reading of the same listing (migration 578, W3).';

reset lock_timeout;
