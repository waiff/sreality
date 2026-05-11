-- 015_images_public.sql
--
-- Public read-only view over the `images` table for the listing-detail
-- page gallery. Mirrors the column-exposure boundary established in
-- migration 008: only safe columns are visible to anon.
--
-- Exposed:
--   id, sreality_id, sequence       — identity + ordering
--   sreality_url                    — the original sreality CDN URL,
--                                     directly loadable in <img>
--   storage_path                    — R2 object key for when the
--                                     download phase is running and
--                                     a public R2 URL prefix is wired
--                                     up via VITE_R2_PUBLIC_BASE
--
-- Withheld (operational, not user-facing):
--   download_attempts, last_download_attempt_at
--
-- Note: this file is being applied to the live database via the
-- Supabase MCP in the same change as the commit. SECURITY INVOKER
-- semantics inherit from the base table; we add no policies because
-- the base table is RLS-blocked and access flows through this view's
-- explicit grant only.

create view images_public as
select id, sreality_id, sequence, sreality_url, storage_path
from images;

grant select on images_public to anon;
