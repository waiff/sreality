-- 177_images_failure_overview.sql
--
-- Make image-download failures visible. The images table has carried
-- per-image failure state for a long time (download_attempts since migration
-- 002, last_error + unavailable_reason since 087), but NOTHING surfaced it: the
-- Health "Image mirror" tile only shows stored-vs-total, so exhausted retries
-- and dead-CDN classifications were invisible without raw SQL.
--
-- Same shape as migration 115 (image_storage_overview_mv): precompute the
-- rollup into a matview refreshed OFF the request path by
-- scripts/refresh_image_stats.py (runs after the 2-hourly image drain in
-- images.yml), because the rollup scans the full multi-million-row images
-- table. Long format — one row per (source, bucket, detail) — so a new
-- unavailable_reason or error class needs no schema change.
--
--   bucket 'stored'      storage_path set — downloaded to R2.
--   bucket 'unavailable' terminally classified (detail = unavailable_reason,
--                        e.g. source_unavailable / listing_taken_down).
--   bucket 'exhausted'   download_attempts >= 5 with no classification and no
--                        bytes — fell out of the retry queue silently.
--   bucket 'pending'     still in the retry queue (attempts < 5, no reason).
--
-- detail for exhausted/pending rows is the coarse last_error class: the
-- leading 3-digit HTTP status when the stored exception text starts with one
-- (requests' HTTPError stringifies as e.g. "404 Client Error: ..."), else
-- 'other'; '' when no error recorded yet. Non-NULL '' (not NULL) so the
-- unique index below covers every row, which REFRESH ... CONCURRENTLY needs.

create materialized view if not exists images_failure_overview_mv as
  select
    l.source,
    case
      when i.storage_path is not null then 'stored'
      when i.unavailable_reason is not null then 'unavailable'
      when i.download_attempts >= 5 then 'exhausted'
      else 'pending'
    end as bucket,
    case
      when i.storage_path is not null then ''
      when i.unavailable_reason is not null then i.unavailable_reason
      when i.last_error is null then ''
      when i.last_error ~ '^[0-9]{3}' then 'HTTP ' || left(i.last_error, 3)
      else 'other'
    end as detail,
    count(*)::bigint as n
  from images i
  join listings l on l.sreality_id = i.sreality_id
  group by 1, 2, 3;

-- Required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
create unique index if not exists images_failure_overview_mv_key
  on images_failure_overview_mv (source, bucket, detail);

-- SECURITY DEFINER: the matview reads base tables, not *_public views, so the
-- aggregate is exposed to anon only through this fixed-shape function (the
-- migration 170 stat-helper pattern; migration 115 granted the matview itself
-- because it was built on public views).
create or replace function images_failure_overview()
 returns table(source text, bucket text, detail text, n bigint)
 language sql
 stable
 security definer
 set search_path = public
as $$
  select m.source, m.bucket, m.detail, m.n from images_failure_overview_mv m;
$$;

grant execute on function images_failure_overview() to anon, authenticated;
