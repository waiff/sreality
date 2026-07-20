-- 333_listing_snapshots_listing_id_scraped_at_index.sql
-- R2 Phase C prerequisite: db.py's rule-2 latest-snapshot guard (the
-- "SELECT content_hash FROM listing_snapshots WHERE sreality_id = %s ORDER BY
-- scraped_at DESC LIMIT 1" check inside upsert_listing, run on every scrape write)
-- is being rekeyed onto listing_id (§4 of the R2 runbook). Phase B only built a
-- bare listing_id index for every R2 carrier (listing_snapshots_listing_id_idx);
-- without a composite mirroring the legacy listing_snapshots_sreality_id_scraped_at_idx,
-- the rekeyed query would fall back to a full index scan + sort per listing on a
-- hot path run every ~5 min for the whole market. Built CONCURRENTLY out of band
-- on prod (1.4M rows); the plain form here is for fresh rebuilds.

CREATE INDEX IF NOT EXISTS listing_snapshots_listing_id_scraped_at_idx
  ON listing_snapshots (listing_id, scraped_at DESC);
