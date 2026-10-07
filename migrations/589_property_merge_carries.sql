-- 589_property_merge_carries.sql -- MERGE SPRINT W3 (docs/design/merge-sprint/PROGRAM.md MS14,
-- MS15, MS17). ADDITIVE: one table, two indexes, RLS on, no grants; nothing existing changes.
--
-- THE CARRY RECORD: every merge writes one row per curation row it moved or folded onto its
-- survivor (notes, pipeline cards, collection entries, tags, dismissals; alert events are not
-- curation and get none). A merge of three or more is one step per retired property under the
-- ledger's one merge_group_id. A fold: the retired's collection entry / tag whose twin the
-- survivor holds (deleted), the losing pipeline card (deleted), a dismissal the merge lifted
-- ('merge' or 'pipeline'); `snapshot` = that row before the fold, so a split can re-create it
-- (W4). A card or dismissal folded on the survivor itself names as from_property_id the property
-- it came from (its newest standing carry row: undone_at empty and a ledger row of its merge
-- retiring that property not undone), else the survivor. row_key + row_at (the row's
-- own timestamp) name the row: a row removed and added again is another row. row_key is the
-- note or dismissal id, the collection_id or the tag_id; NULL for a pipeline card, whose key is
-- (account_id, to_property_id). undone_at: when a split undid the carry (W4 writes it).
--
-- Written only by toolkit.property_identity._merge_pair, in the merge transaction, after every
-- carrier and before the retire; read by POST /properties/merge (the acting account's moved
-- items), the brake's dry run (toolkit.property_carriers.curation_preview) and, from W4, the
-- split. Backend-only like property_merge_events (RLS on, no policy, anon/authenticated
-- revoked, no _public view); every read names its account (tenancy shape 3). Never pruned; an
-- account's rows go with the account.
--
-- APPLY BEFORE THE CODE MERGES: from the deploy on, every merge step runs two statements that
-- name this table (the pipeline and dismissal carriers' came-from lookups; Postgres resolves the
-- name before it reads a row) and a step that carried curation writes it, so an absent table
-- fails EVERY merge, the engine's included (its lane records 'failed' and stops the pass), not
-- only one that carries curation. Idempotent; re-run the file on a lock timeout.
-- Verify:
--   select relrowsecurity from pg_class where oid = 'public.property_merge_carries'::regclass;   -- t
--   select has_table_privilege('authenticated', 'public.property_merge_carries', 'select');      -- f
--   select has_table_privilege('anon', 'public.property_merge_carries', 'select');               -- f

set lock_timeout = '5s';

create table if not exists public.property_merge_carries (
  id               bigserial   primary key,
  merge_group_id   uuid        not null,
  table_name       text        not null,
  row_key          bigint,
  row_at           timestamptz not null,
  account_id       uuid        references public.accounts(id) on delete cascade,
  from_property_id bigint      not null references public.properties(id),
  to_property_id   bigint      not null references public.properties(id),
  kind             text        not null check (kind in ('moved', 'folded')),
  snapshot         jsonb,
  undone_at        timestamptz,
  created_at       timestamptz not null default now(),
  constraint property_merge_carries_snapshot_ck check ((kind = 'folded') = (snapshot is not null))
);

create index if not exists property_merge_carries_group_idx
  on public.property_merge_carries (merge_group_id);
create index if not exists property_merge_carries_to_live_idx
  on public.property_merge_carries (to_property_id) where undone_at is null;

alter table public.property_merge_carries enable row level security;
revoke all on public.property_merge_carries from anon, authenticated;
revoke all on sequence public.property_merge_carries_id_seq from anon, authenticated;

comment on table public.property_merge_carries is
  'The carry record (MERGE SPRINT MS14): one row per curation row a merge moved or folded onto '
  'its survivor, written in the merge transaction by toolkit.property_identity._merge_pair. '
  'A folded row keeps its snapshot so a split can re-create it (W4). Backend-only, never pruned.';
