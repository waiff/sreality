-- 323_child_listing_id_property_state.sql
-- R2 Phase A1 of the listing-identity refactor, file 4 of 6
-- (docs/design/listing-identity-r2-pk-swap-runbook.md § 2).
-- Additive: listing_id on the property-grain + operator-state carriers.
--
-- THREE OF THESE TAKE A NON-DEFAULT COLUMN NAME. properties.repr_listing_id,
-- property_notes.origin_listing_id and property_merge_events.listing_id already
-- exist and hold LEGACY sreality_id values — property_merge_events in particular
-- has a column literally named listing_id that is NOT a listings.id. Reusing or
-- renaming those in place would break every live reader mid-flight, so the new
-- surrogate columns take a _ref_id suffix and the legacy ones stay frozen until R5.
-- New code: *_ref_id means "FK to listings.id"; the bare legacy name means
-- "frozen sreality_id".
--
-- notification_dispatches is a corrected census entry: migration 274 made its
-- sreality_id NULLABLE and moved the once-ever dedup guard to UNIQUE(dedupe_key)
-- — the (subscription_id, sreality_id) guard the v2 design doc assumed no longer
-- exists. Its repoint therefore follows dedupe_key + property_id semantics, and
-- post-flip it would fail SILENTLY (NULL rows inserted) rather than loudly.
--
-- manual_rental_estimates_history is a carrier the original FK census missed (it
-- has no FK to listings, only a NOT NULL sreality_id) — its append-only rows need
-- the same treatment as their parent table.
--
-- Catalog-only, short lock_timeout, no FK yet — see 320's header for the rationale.

SET lock_timeout = '3s';

ALTER TABLE properties ADD COLUMN IF NOT EXISTS repr_listing_ref_id bigint;
ALTER TABLE property_notes ADD COLUMN IF NOT EXISTS origin_listing_ref_id bigint;
ALTER TABLE property_merge_events ADD COLUMN IF NOT EXISTS listing_ref_id bigint;
ALTER TABLE notification_dispatches ADD COLUMN IF NOT EXISTS listing_id bigint;
ALTER TABLE manual_rental_estimates ADD COLUMN IF NOT EXISTS listing_id bigint;
ALTER TABLE manual_rental_estimates_history ADD COLUMN IF NOT EXISTS listing_id bigint;
