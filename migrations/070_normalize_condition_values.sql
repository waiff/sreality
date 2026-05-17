-- 070_normalize_condition_values.sql
--
-- Backfill `listings.condition` from the raw Czech display strings
-- ("po rekonstrukci", "velmi dobrý", …) to the canonical underscore /
-- no-diacritics form used by every other enum-coded column
-- (`category_main`, `category_type`, `furnished`, `ownership`).
--
-- The old text values were the output of `scraper.parser._condition`,
-- which prior to this migration just lowercased the raw display
-- string. The filter registry (`toolkit.filter_registry.CONDITION_OPTIONS`)
-- already encoded the canonical form, which meant the
-- `condition_match` filter never matched any rows. Same migration
-- ships the parser fix; from this commit onward all new rows arrive
-- in the canonical form.
--
-- The `listings.content_hash` is computed over `raw_json`, not the
-- parsed columns, so updating `condition` here does NOT cause the
-- next scrape to mistake every row for "changed" and append a fresh
-- snapshot per listing.
--
-- The 12 values below cover every distinct value present in
-- production at the time of writing. Any future label that doesn't
-- map cleanly will be stored as NULL by the new parser (forgiving
-- failure, matching the FURNISHED / OWNERSHIP convention).
--
-- Practical note: applying this migration via Supabase MCP timed
-- out at the 120-second statement_timeout against ~48k rows, so the
-- production rollout was done in 200-500 row CTE-batched chunks via
-- `execute_sql`. The single UPDATE below is left as the canonical
-- statement for fresh-rebuild migrations against a smaller dataset;
-- a fresh local run finishes inside the default timeout. On the
-- live cluster, anything that lingers on the un-normalised label
-- gets rewritten on its next successful detail fetch — the upsert
-- in `scraper.db.upsert_listing` overwrites every parsed column
-- on every successful fetch, regardless of whether the snapshot
-- changes, so daily scrape activity drains the long tail without
-- further intervention.

update listings
set condition = case condition
        when 'velmi dobrý'       then 'velmi_dobry'
        when 'dobrý'             then 'dobry'
        when 'po rekonstrukci'   then 'po_rekonstrukci'
        when 've výstavbě'       then 've_vystavbe'
        when 'před rekonstrukcí' then 'pred_rekonstrukci'
        when 'rezervováno'       then 'rezervovano'
        when 'v rekonstrukci'    then 'v_rekonstrukci'
        when 'špatný'            then 'spatny'
        when 'k demolici'        then 'k_demolici'
        when 'prodáno'           then 'prodano'
        else condition
    end
where condition in (
    'velmi dobrý', 'dobrý', 'po rekonstrukci', 've výstavbě',
    'před rekonstrukcí', 'rezervováno', 'v rekonstrukci',
    'špatný', 'k demolici', 'prodáno'
);
