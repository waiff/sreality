-- 166_backfill_remax_furnished_ownership.sql
--
-- One-time normalisation of the non-canonical furnished/ownership labels the
-- remax parser used to emit (scraper/remax_parser.py, fixed in this PR):
--   furnished 'vybaveno'   -> 'ano'   (the canonical "furnished" code)
--   furnished 'nevybaveno' -> 'ne'    (defensive; none in prod today)
--   ownership 'ostatni'    -> NULL    ("other" has no canonical option; the
--                                      "Unknown" filter bucket surfaces NULLs)
--
-- These strings are never canonical for any source, so the UPDATEs are scoped
-- by VALUE (not source). Idempotent: a re-run (or a fresh DB) matches nothing.
-- The denormalised `properties` columns (read by properties_public / Browse)
-- are normalised alongside `listings` so the fix is visible immediately rather
-- than waiting for the next property recompute.
--
-- Apply AFTER the remax parser fix is live, so the scraper stops re-introducing
-- 'vybaveno'. Any stragglers ingested in between remain reachable via the
-- robust "Unknown" bucket (value IS NULL OR value NOT IN canonical set).

update listings  set furnished = 'ano' where furnished = 'vybaveno';
update listings  set furnished = 'ne'  where furnished = 'nevybaveno';
update listings  set ownership = null  where ownership = 'ostatni';

update properties set furnished = 'ano' where furnished = 'vybaveno';
update properties set furnished = 'ne'  where furnished = 'nevybaveno';
update properties set ownership = null  where ownership = 'ostatni';
