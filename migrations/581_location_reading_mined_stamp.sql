-- 581: the claim lane's stamp on a stored reading (location reader W3, final-plan D1/D5).
--
-- `location_data.claims_intake` stamps a listing's CURRENT reading `<source>@<version>` when it
-- mines it, a reading of another text the same with '~' (checked, not current), a same-text one
-- NULL: a reading that becomes current again is mined again. No index (final-plan §10).
-- ADDITIVE and catalog-only. Apply BEFORE the code merges: the hourly intake runs from main.

set lock_timeout = '5s';

alter table listing_description_enrichments add column if not exists mined_contract_version text;

comment on column listing_description_enrichments.mined_contract_version is
  'The contract (<source>@<version>) the claim lane last checked this reading''s listing at: '
  'plain = mined; with ~ = another text was current; NULL = not checked (migration 581, W3).';

reset lock_timeout;
