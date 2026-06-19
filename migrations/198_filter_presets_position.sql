-- 198_filter_presets_position.sql
--
-- Operator-controlled display order for Browse filter presets.
--
-- Until now presets were ordered implicitly by `created_at DESC` (newest
-- first). Drag-to-reorder needs a durable, server-persisted order that
-- survives across devices/sessions, so we add a single explicit `position`
-- column (ascending, 0 = leftmost) and order by it.
--
-- The reorder endpoint rewrites the full ordered id-list in one transaction
-- (a single operator's handful of presets — O(n) per reorder is trivial and
-- drift-free, no fractional-index bookkeeping). New presets append at
-- `MAX(position) + 1` so saving one never reshuffles the curated arrangement.
--
-- Backfill preserves the current `created_at DESC` display order so nothing
-- visibly jumps on deploy: newest existing preset keeps position 0.

begin;

alter table filter_presets
    add column position integer not null default 0;

update filter_presets fp
set position = ordered.pos
from (
    select id, (row_number() over (order by created_at desc) - 1) as pos
    from filter_presets
) ordered
where fp.id = ordered.id;

create index filter_presets_position_idx on filter_presets (position);

comment on column filter_presets.position is
    'Operator-controlled display order (ascending, 0 = leftmost). Maintained '
    'by the /filter-presets/reorder endpoint as a full-list rewrite; new '
    'presets append at MAX(position)+1.';

commit;
