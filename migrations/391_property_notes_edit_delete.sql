-- 391: allow editing a property note in place.
--
-- WHY: property_notes (migration 202, rule #18) has been add-only since it
-- shipped — the API/UI only ever INSERT a new row. RLS already grants
-- update/delete to `authenticated` (290_curation_account_scoping.sql), so this
-- is a pure API/UI gap, not a schema one. The one thing schema-side genuinely
-- needs is a way to tell "written once" from "edited since" apart — there is
-- no updated_at/edited marker today, so an edited note would otherwise look
-- identical to an untouched one. NULL = never edited (the common case, so no
-- backfill needed); the API stamps it on every PATCH.
--
-- Also folds in a pre-existing drift: property_notes_public (202) was never
-- updated to expose origin_listing_ref_id after 323 added the column to the
-- base table, so the surrogate provenance handle has been silently
-- unreadable through the anon view since. Recreating the view to pick up
-- both columns in one pass.

alter table property_notes add column if not exists updated_at timestamptz;

-- CREATE OR REPLACE VIEW can only append columns, not reorder them, so the two
-- backfilled columns land at the end rather than next to their siblings.
create or replace view property_notes_public as
  select id, property_id, body, origin_listing_id, created_at,
         origin_listing_ref_id, updated_at
  from property_notes;
