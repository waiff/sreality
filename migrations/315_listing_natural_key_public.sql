-- 315: an UNFILTERED natural-key resolver surface for the canonical listing URL.
--
-- The canonical /listing/{source}/{native} route (PR #821) resolves the natural key
-- via property_sources_public, which filters `where property_id is not null`
-- (migration 093). But a freshly-scraped listing lands property_id NULL for ~5 min
-- (rules #19/#20 -- singleton attached asynchronously by property_maintenance) while
-- its source_id_native IS already stamped at INSERT (migration 314). So during that
-- window the canonical URL resolved to no row -> "not found", even though the same
-- listing's legacy /listing/{sreality_id} URL (listings_public, unfiltered) loads fine
-- and the Chrome-extension deep link (#823) emits the canonical form for exactly such
-- rows. Adversarial review of #821/#823 flagged this coverage gap.
--
-- This view exposes ONLY the natural key, for EVERY listing (no property_id filter), so
-- the resolver covers fresh, not-yet-grouped rows. source_id_native is the portal's
-- public listing id (already anon-exposed via property_sources_public) -- not sensitive.
create view listing_natural_key_public as
select sreality_id, source, source_id_native
from listings;

grant select on listing_natural_key_public to anon, authenticated;
