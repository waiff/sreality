-- 371_repair_colliding_image_storage_paths.sql
--
-- Repair the rows whose R2 object was silently overwritten by a key collision.
--
-- Two key schemes shared one numeric namespace: the pre-Gate-2
-- `{sreality_id}/{seq:04d}.jpg` and the current `{listings.id}/{seq:04d}.jpg`.
-- Where a NEW listing's surrogate id equalled an OLD listing's sreality_id, both
-- listings minted the same keys and the later upload overwrote the earlier one.
-- The older row kept pointing at that key AND kept the pHash of the bytes it no
-- longer owns — so it renders another listing's photo, and feeds dedup a hash for
-- an image nobody can see. `scraper/image_storage.image_key` now namespaces the
-- key and embeds `images.id`, so no future key can collide.
--
-- Verified live 2026-08-05 before writing this: a full-table
-- `GROUP BY storage_path HAVING count(*) > 1` returns 16 keys / 32 rows and
-- nothing else. For every one of the 16, the R2 object was downloaded and hashed
-- with `scraper.image_phash.compute_dhash`: the LATEST-downloaded row's stored
-- pHash matched the served bytes exactly (Hamming 0) and the earlier row's did
-- not (Hamming 24-38). Damaged rows: images 529986-529994 (listing 31419) and
-- 70400322-70400328 (listing 332521), both live sreality listings whose photos
-- were overwritten by realitymix listing 13251404 / idnes listing 13561932.
--
-- The repair is written as a PREDICATE, not an id list, so a collision that lands
-- between now and apply time is repaired too. Survivor rule = the latest
-- `last_download_attempt_at`, which is exactly the last successful upload: a row
-- with a storage_path can never re-enter the download queue
-- (`pending_image_downloads` requires `storage_path IS NULL`), so for a stored row
-- that timestamp IS its upload time. Clearing the losers puts them back in the
-- queue; the next detail scrape also refreshes their `sreality_url`
-- (`record_images` updates only `WHERE images.storage_path IS NULL`), so a stale
-- CDN URL self-heals. A row that cannot be re-fetched ends with
-- `unavailable_reason` set and renders a placeholder — correct, where showing a
-- different property's photo was not.
--
-- pHash is cleared with the path: it describes bytes that no longer exist in the
-- bucket, and a NULL pHash is simply invisible to every dedup query until the
-- re-download recomputes it inline.

-- Duplicate detection has to see every stored key, and there is no index on a
-- non-null storage_path (`images_storage_path_idx` is partial on IS NULL, for the
-- download queue) — so this is one deliberate seq scan of a 3.6 GB table. Measured
-- at 60-115s live, i.e. close enough to the default ceiling to trip on a cold
-- cache. Raised for this session only; the UPDATE itself touches 16 rows.
SET statement_timeout = '600s';

WITH dup AS (
    SELECT storage_path
    FROM images
    WHERE storage_path IS NOT NULL
    GROUP BY storage_path
    HAVING count(*) > 1
),
colliding AS (
    SELECT i.id, i.storage_path,
           row_number() OVER (
               PARTITION BY i.storage_path
               ORDER BY i.last_download_attempt_at DESC NULLS LAST, i.id DESC
           ) AS owns_object
    FROM images i
    JOIN dup d ON d.storage_path = i.storage_path
)
UPDATE images i
SET storage_path = NULL,
    phash = NULL,
    download_attempts = 0,
    last_error = NULL
FROM colliding c
WHERE c.id = i.id
  AND c.owns_object > 1;
