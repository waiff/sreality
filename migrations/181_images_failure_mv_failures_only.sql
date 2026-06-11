-- 181_images_failure_mv_failures_only.sql
--
-- Migration 177's matview bucketed ALL 4.4M image rows, and its initial
-- population exceeded both the MCP statement timeout and the pooler's ~2-min
-- statement cap (the first-populate fallback in refresh_image_stats died at
-- exactly 120s). The 'stored' bucket was redundant anyway — stored counts live
-- in image_storage_overview_mv (migration 115), and the Health panel filters
-- 'stored' out client-side. Restrict the matview to storage_path IS NULL
-- (~80k rows, seconds to build); same row shape, no 'stored' rows.

drop materialized view if exists images_failure_overview_mv;

create materialized view images_failure_overview_mv as
  select
    l.source,
    case
      when i.unavailable_reason is not null then 'unavailable'
      when i.download_attempts >= 5 then 'exhausted'
      else 'pending'
    end as bucket,
    case
      when i.unavailable_reason is not null then i.unavailable_reason
      when i.last_error is null then ''
      when i.last_error ~ '^[0-9]{3}' then 'HTTP ' || left(i.last_error, 3)
      else 'other'
    end as detail,
    count(*)::bigint as n
  from images i
  join listings l on l.sreality_id = i.sreality_id
  where i.storage_path is null
  group by 1, 2, 3;

create unique index if not exists images_failure_overview_mv_key
  on images_failure_overview_mv (source, bucket, detail);

-- images_failure_overview() (migration 177) reads the same columns — unchanged.
