-- 363_properties_published_at_idx.sql
--
-- The Watchdog matcher's new-listing producer (api/notifications.py::match_once) walks
-- properties_public with a per-subscription forward cursor: `published_at > cursor
-- ORDER BY published_at ASC LIMIT window`. That hot scan had NO supporting btree — the
-- only published_at index was properties_unpublished_idx (migration 273), a partial
-- index over the INVERSE rows (`WHERE published_at IS NULL`), useless for the published
-- forward window. So every matcher pass was O(subs x ~all published properties). This is
-- the single highest-value Wave 3 detection fix: a btree over the published rows the
-- cursor actually scans.
--
-- Applied to production CONCURRENTLY (out-of-band, non-blocking) BEFORE this file lands;
-- the plain `create index if not exists` below is the replay/CI record (a no-op against
-- prod where the index already exists, and a fast create on CI's empty properties table).
-- Same discipline as migration 360.
create index if not exists properties_published_at_idx
  on properties (published_at)
  where published_at is not null;
