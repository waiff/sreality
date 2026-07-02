-- 266_listings_published_at.sql (264 taken by open PR #677, 265 doubly-claimed by #679/#681)
-- Promote portal-published timestamps to a first-class column — the ground truth every
-- real-time SLO (publish -> our ingest latency) needs. Until now every portal date signal
-- was fetched and DISCARDED into raw_json (or not extracted at all):
--   * bazos:         span.velikost10 "[D.M. YYYY]" — raw only. CAVEAT: bazos re-stamps it
--                    on every bump / TOP renewal, so it is a LAST-BUMP date, not first
--                    publication — still the tightest publish bound bazos exposes.
--   * ceskereality:  "Datum vložení" spec row ("27. února 2026") — not extracted before.
--   * bezrealitky:   timeActivated is requested in the detail query but the anon API
--                    returns it NULL — mapped anyway (free if access ever appears).
--   * sreality:      raw "edited" (day-granular, ~40% of rows) — a last-edit date, the
--                    weak fallback.
--   * idnes / realitymix / remax / maxima / mmreality: no date markup — stay NULL.
-- timestamptz (not date): the day-granular sources land as midnight UTC, while a portal
-- that exposes a real timestamp (bezrealitky's timeActivated) is stored losslessly.
-- Out of the content hash on every path, so populating/backfilling it never churns
-- snapshots. NO index and NOT on listings_public yet: internal SLO instrumentation first —
-- no reader filters on it today.

alter table listings add column if not exists published_at timestamptz;

comment on column listings.published_at is
  'Portal-declared publication/last-bump timestamp of the advert, as currently shown by '
  'the source portal. Day-granular for most portals (stored as midnight UTC); bazos '
  're-stamps it on bump/TOP renewal (a last-bump date); sreality maps its day-granular '
  '"edited" as a weak fallback. NULL = the portal exposes no date. Out of the content '
  'hash; preserve-if-null on ingest so a fetch without the signal never erases it.';
