-- 201_filter_presets_color.sql
--
-- Optional operator-chosen colour for a Browse filter preset, drawn from the
-- SHARED tag-colour palette so tags and presets speak one colour vocabulary.
-- NULL = no colour (the neutral default chip).
--
-- The CHECK mirrors migration 024's `tags.color` exactly and is kept in lockstep
-- with `api.schemas.TagColor` and the TS `TAG_COLORS` list; adding a palette
-- colour is the same coordinated change tags already require (migration + token
-- + Python/TS bump). Nullable here (a preset may be uncolored) — that is the only
-- difference from `tags.color`, which is NOT NULL.

begin;

alter table filter_presets
    add column color text
        check (color is null or color in (
            'copper', 'sage', 'brick', 'ochre',
            'slate',  'plum', 'teal',  'sand'
        ));

comment on column filter_presets.color is
    'Optional preset chip colour from the shared tag palette (migration 024 / '
    'api.schemas.TagColor). NULL = neutral default chip.';

commit;
