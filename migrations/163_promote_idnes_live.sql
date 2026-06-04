-- iDNES Reality has graduated from pilot to a production scraper: a complete
-- per-category index walk (migration 111) across all categories (120), run on a
-- decoupled cron split (idnes_index_walk.yml every 6h enqueues; idnes_detail_drain.yml
-- hourly drains the detail queue). The portal has been walking the full index and
-- inferring delistings for months, so its display stage no longer matches reality.
--
-- Promote the scraper facet's display stage pilot -> live. `stage` is a presentation
-- label only (no backend behaviour keys off it; the Health dashboard renders the
-- "SCRAPER · LIVE" badge from it). The on-demand URL-parser facet
-- (source='idnes_reality', kind='parser') is a distinct capability and stays
-- 'on_demand'. The other pilot scrapers are left untouched on purpose.

update portals set stage = 'live' where source = 'idnes' and kind = 'scraper';
