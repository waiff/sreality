-- 102_image_phash.sql
-- PR5 of the multi-portal dedup completion: the cheap image corroborator for
-- the Tier-2 dedup sweep. A 64-bit perceptual hash (dHash, Pillow-only — no
-- numpy) per stored image. The sweep compares the min Hamming across two
-- properties' representative images via bit_count(a # b) and treats a close
-- match as a strong auto-merge corroborator alongside the (expensive) vision
-- rung. Portal-agnostic; purely additive.
--
-- Stored as a SIGNED bigint (the 64-bit hash mapped into bigint's range, bit
-- pattern preserved). Hamming distance is `bit_count((a # b)::bit(64))` — note
-- bit_count is defined for bit/bytea, not bigint, so the XOR is cast to bit(64)
-- first (verified on this PG17 DB; the cast handles the sign correctly).

alter table images add column phash bigint;

-- Partial index for the sweep's per-listing "images with a hash" lookups.
create index images_phash_idx on images (sreality_id) where phash is not null;
