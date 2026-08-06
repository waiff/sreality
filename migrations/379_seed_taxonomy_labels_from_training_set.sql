-- 379_seed_taxonomy_labels_from_training_set.sql
--
-- One-time backfill: dedup_sim.taxonomy_labels (migration 373) opened empty, but the
-- operator had already built a 48-label, ~1,185-image training set through /phash-audit's
-- "Train" CTA (image_training_examples, migration 309) BEFORE the Labeling page shipped
-- (#981) — exactly the vocabulary PROGRAM.md itself calls "Taxonomy v1" (docs/design/
-- new-dedup/PROGRAM.md: "the operator-curated `image_training_examples` label set").
-- taxonomy_overview's confirmed_count already LEFT JOINs image_training_examples on label
-- text, so once a label exists here its real confirmed count shows up automatically — the
-- only gap was that dedup_sim.taxonomy_labels had no rows to join against.
--
-- Not an ongoing sync: this seeds the vocabulary once from whatever's in
-- image_training_examples right now. From here on the Labeling page's add/rename/remove
-- flow is how Taxonomy v1 evolves (per migration 373's own comment: "the operator builds
-- it live through the Labeling page").

insert into dedup_sim.taxonomy_labels (label, created_by)
select distinct label, 'backfill:image_training_examples'
from image_training_examples
on conflict (label) do nothing;
