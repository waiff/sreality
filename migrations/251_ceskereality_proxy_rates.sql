-- 251_ceskereality_proxy_rates.sql
--
-- ceskereality now crawls through a residential proxy (SCRAPER_PROXY_URL,
-- ceskereality_client.USE_PROXY), which removes the Cloudflare datacenter-IP
-- throttle that previously degraded our walks. With the throttle gone — and the
-- region × disposition/type split issuing many small index queries — we restore
-- normal crawl speed: index_rate 0.7 -> 2.0, detail_workers 2 -> 4, detail_rate
-- 0.7 -> 2.0, and drop the per-run detail cap (the --max-seconds budget governs).
--
-- Operational tuning only (updates the existing registry row).

update portals
set operational_limits = '{
  "index_rate": 2.0,
  "detail_workers": 4,
  "detail_rate": 2.0
}'::jsonb
where source = 'ceskereality';
