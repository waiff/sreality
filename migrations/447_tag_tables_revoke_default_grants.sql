-- 447_tag_tables_revoke_default_grants.sql
--
-- Revoke the anon/authenticated grants this project's DEFAULT PRIVILEGES handed
-- the three tag tables at CREATE time. Purely a lockdown; no schema change.
--
-- This project auto-GRANTs on new relations -- the root cause the Phase-0
-- hardening chased and migration 237 already fixed once for image_clip_tags /
-- image_room_classifications / image_clip_embeddings. Migration 442 created
-- tag_taxonomy and image_tag_labels, and 445 created tag_definitions, all three
-- documenting themselves as "backend-only, no _public view" -- and all three
-- silently carrying `grant select to authenticated` anyway, because none of them
-- revoked. Migration 446 got it right for image_tag_label_events, which is the
-- only one of the four that is actually clean today.
--
-- Effect today is nil and that is exactly why it is worth fixing now: all four
-- tables are RLS-enabled with ZERO policies, so authenticated already reads no
-- rows -- RLS denies by default. The grant is inert. But it is inert only for as
-- long as nobody adds a permissive policy, and the labeling program is going to
-- keep adding tables and surfaces here. A grant that contradicts the migration
-- comment above it is a trap primed for whoever adds that first policy: they
-- would reasonably expect the table to still be backend-only, and it would not
-- be. Defence in depth -- the grant and the policy should BOTH have to be wrong
-- before a row escapes, not just the policy.
--
-- Verified before writing: no SPA or extension code reads these tables directly
-- (the labeling surfaces go through the admin-gated API on a service-role
-- connection), so nothing loses access.

begin;

revoke all on tag_taxonomy     from anon, authenticated;
revoke all on image_tag_labels from anon, authenticated;
revoke all on tag_definitions  from anon, authenticated;

commit;
