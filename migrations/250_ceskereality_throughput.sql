-- 250_ceskereality_throughput.sql
--
-- Raise ceskereality's scrape throughput toward the other portals' speeds.
--
-- The first live runs collected only ~240 listings per large category (~6% of the
-- 68k inventory): NOT a speed problem but a paging one — ceskereality is Cloudflare-
-- fronted and, under load, serves a throttled/degraded page that drops the pager's
-- "next" arrow, which the old arrow-following walk misread as end-of-category and
-- stopped at ~12 pages. ceskereality_main now drives ?strana straight to
-- ceil(total/page) (retrying barren pages), so the walk reaches the full total; this
-- migration bumps the per-portal rate ceilings to match (the shared RateLimiter still
-- backs off on a 429/403, so they are ceilings, not floors).
--
-- index_rate 0.7 -> 1.5, detail_workers 2 -> 6, detail_rate 0.7 -> 3.0, and the
-- per-run detail cap is dropped (the drain's --max-seconds budget governs, like
-- idnes). Purely an operational tuning update to the existing registry row.

update portals
set operational_limits = '{
  "index_rate": 1.5,
  "detail_workers": 6,
  "detail_rate": 3.0
}'::jsonb
where source = 'ceskereality';
