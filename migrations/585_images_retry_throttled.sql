-- 585: re-queue the photos a throttling portal made us give up on.
--
-- From 2026-10-01 iDNES's site rate-limited (429) and from 2026-10-05 12:38 UTC blocked
-- (403) the image drain on its gallery redirector `reality.idnes.cz/file/thumbnail/{id}`;
-- the drain counted every such answer as the image's own failure and, five attempts
-- later, gave the row up for good (`download_attempts >= 5` leaves the queue). The
-- engine's evidence rule then held every merge touching such an ad (evidence_pending).
-- The drain now parks the HOST on a throttle and burns no attempt (scraper/main.py,
-- THROTTLE_STATUSES); this heal gives the rows it already gave up their attempts back.
--
-- Count before (2026-10-06 18:40 UTC, listings first seen since 2026-09-25):
--   28,160 images on 2,140 ads, all on the iDNES redirector; 17,793 ended on a 429, 10,367 on a 403.
-- Apply AFTER the drain fix is deployed (the old drain would burn them again within minutes).
-- Not destructive: counters only; `last_error` is kept as the record of what happened.
UPDATE images i
SET download_attempts = 0
FROM listings l
WHERE l.id = i.listing_id
  AND l.first_seen_at >= '2026-09-25'
  AND i.storage_path IS NULL
  AND i.unavailable_reason IS NULL
  AND i.download_attempts >= 5
  AND i.last_error ~ '^(429|403) Client Error';
