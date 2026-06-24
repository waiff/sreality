-- The /dedup pipeline-overview polls a "tagged in the last 24h" count off
-- image_clip_tags every 60s. Without an index on tagged_at that 24h window is a
-- full sequential scan of a table on a 5M-row growth path (CLIP tagging is now
-- global full-scale). This index turns the moving-number query into a bounded
-- range scan. (The cumulative totals use pg_class.reltuples estimates instead, so
-- they never scan at all.) On prod this is applied CREATE INDEX CONCURRENTLY out of
-- band; the plain form here is for fresh rebuilds where the table is empty.
create index if not exists image_clip_tags_tagged_at_idx
  on image_clip_tags (tagged_at);
