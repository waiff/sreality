-- New shared-circulation logical tags: staircase_interior (a building's interior stairwell)
-- and staircase_exterior (outdoor steps). They're a SHARED common area — every unit in a
-- building shows the same one — so room_taxonomy puts them in the new `common` family,
-- excluded from the byt unit-match signal (NON_INTERIOR_TAGS) like the exterior/plan families.
-- image_clip_tags.logical_tag is free-text (CLIP writes them already); this only extends the
-- Haiku room-classifier cache CHECK so that classifier could emit them too. Additive.

alter table image_room_classifications drop constraint if exists image_room_classifications_room_type_check;
alter table image_room_classifications add constraint image_room_classifications_room_type_check
  check (room_type = any (array[
    'kitchen','bathroom','toilet','living_room','bedroom','hallway',
    'staircase_interior','staircase_exterior',
    'exterior_facade','balcony_terrace','garden',
    'floor_plan','site_plan','property_document','other']));

-- Populating these (and the sharpened toilet/WC anchor) on EXISTING images is done by the
-- re-tag-from-stored-embeddings job (scripts/retag_from_embeddings + clip_retag.yml) — it
-- re-runs the CLIP zero-shot over the already-stored embeddings under the new taxonomy, no
-- image re-download/re-embed. New images pick up the taxonomy automatically (clip_tag.yml
-- loads it at runtime).
