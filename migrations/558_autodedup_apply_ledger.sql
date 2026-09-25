-- 558_autodedup_apply_ledger.sql
--
-- AUTODEDUP A1: the ledger of the engine's production merges, and the merge
-- chokepoint's third `source`. Design: docs/design/autodedup/PROGRAM.md E900-E906; operator
-- summary: docs/design/autodedup/ROLLOUT.md section 7.1. Purely ADDITIVE.
--
-- WHAT CHANGES. Until A1 the engine wrote only its own schema (shadow mode, D4). The apply
-- path (autodedup/apply.py, lane modes `apply` / `unapply`) turns ONE generation's groups into
-- production merges through THE chokepoint, `toolkit.property_identity.merge_properties`
-- (CLAUDE.md rule 15), which already carries operator state (rule 18) and the deal pipeline
-- (rule 22) onto the survivor. This file creates nothing that runs by itself: the path is DARK
-- until `app_settings.autodedup_apply_enabled` is true AND a run is dispatched with
-- dry_run=0 AND `app_settings.autodedup_apply_scope` names both its deal types and its area.
-- Migration 557 was taken by the sibling real-time lane branch; this is the next free number.
--
-- 1. `property_merge_events.source` gains 'autodedup'. Migration 100's inline CHECK allowed
--    only 'auto' (the removed legacy engine) and 'operator'. The engine's merges get their own
--    value so the ledger and `GET /properties/merges` can tell them apart from both, and so
--    nothing here ever reads as the legacy engine (rule 15). The constraint is replaced inside
--    ONE DO block (a single statement, so autocommit apply leaves no window with neither
--    constraint) as NOT VALID, then validated separately under SHARE UPDATE EXCLUSIVE. Widening
--    a CHECK rejects no existing row: non-destructive.
--
-- 2. `autodedup.applied_merges`: one row per (engine group, retired property) the apply path
--    planned, applied, refused or skipped, dry runs included (`dry_run`). ONE merge_group_id
--    per engine group (E901), so `unmerge_group` undoes a whole group and `unapply` undoes a
--    generation newest-first. The partial UNIQUE index is the idempotency rail: two live
--    merges of the same (survivor, retired) pair can never both be recorded. `member_ids`
--    (GIN-indexed over every applied merge, undone ones included: an engine merge someone
--    else took apart is the operator's negative on its LISTINGS) is the group's listing set,
--    read back by later plans and by `unapply`'s later-merge guard.
--
--    WHY NOT `autodedup.merges` (migration 528's reserved ledger). Its grain is one retired
--    property per group keyed on merge_group_id with a listing PAIR (listing_lo < listing_hi);
--    the apply path writes one group per CLUSTER with several retired properties, records
--    dry runs and refusals as well, and keys idempotency on (survivor, retired). 528's table
--    stays reserved and empty.
--
--    READS NOTHING FROM `property_merge_events` (D7): the ledger is the engine's own record.
--    The apply path's one touch of the production ledger is a WRITE-ONLY stamp of
--    `generation = 'autodedup:<generation>'` on rows of merge groups it created itself, inside
--    the same transaction as the merges (E902, which makes E40's non-atomic seam atomic).
--
-- 3. `autodedup.unapplied_generations`: one row per WHOLE-generation `unapply` (E905), written
--    before it undoes anything. While a row of a generation stands unreleased, every plan of
--    that generation refuses every group — the ones it undid and the ones it never reached —
--    until an apply dispatched with `reapply=1` releases it (`released_at`). An unapply scoped
--    to one cluster_key writes no row. Same posture as the ledger.
--
-- 4. Two `public.app_settings` rows, the operator's switches, seeded OFF (E904). They live in
--    app_settings, not `autodedup.settings`, because /settings edits app_settings and
--    `PUT /admin/app_settings/{key}` 404s on a key with no row: an off switch the operator
--    cannot reach without SQL is not a kill switch. The `autodedup_apply_` prefix keeps them
--    apart from every other program's keys (528's reason for its own settings table).
--    `on conflict do nothing`: an operator's value is never overwritten by a re-run.
--
-- POSTURE. Backend-only like every autodedup relation: RLS on, anon/authenticated revoked,
-- no `_public` view; registered in tests/test_migration_rls_grants.py and
-- tests/test_tenant_isolation_live.py::_ADMIN_ONLY_RELATIONS. No foreign key into production
-- (528's droppable-schema rule). `set lock_timeout` is PLAIN, never SET LOCAL: the apply path
-- runs statements in autocommit, and every statement below is idempotent so a retry is safe.

set lock_timeout = '5s';

------------------------------------------------------------------
-- 1. the chokepoint's third source
------------------------------------------------------------------

do $$
declare
  c record;
begin
  if exists (
    select 1 from pg_constraint
     where conrelid = 'public.property_merge_events'::regclass
       and contype = 'c'
       and conname = 'property_merge_events_source_check'
       and position('autodedup' in pg_get_constraintdef(oid)) > 0
  ) then
    return;
  end if;
  for c in
    select conname from pg_constraint
     where conrelid = 'public.property_merge_events'::regclass
       and contype = 'c'
       and position('source' in pg_get_constraintdef(oid)) > 0
  loop
    execute format('alter table public.property_merge_events drop constraint %I', c.conname);
  end loop;
  alter table public.property_merge_events
    add constraint property_merge_events_source_check
    check (source in ('auto', 'operator', 'autodedup')) not valid;
end
$$;

alter table public.property_merge_events
  validate constraint property_merge_events_source_check;

comment on column public.property_merge_events.source is
  'Who ordered the merge: ''operator'' (Browse merge mode / POST /properties/merge), '
  '''autodedup'' (the AUTODEDUP apply path, migration 558, dark unless '
  'app_settings.autodedup_apply_enabled), ''auto'' (the legacy engine removed in the '
  '2026-08 NEW DEDUP cutoff; no new rows).';

comment on column public.property_merge_events.generation is
  'Which dedup engine made this merge: ''legacy'' = the pre-2026-08 engine removed in NEW DEDUP '
  'Wave 0 (backfilled by migration 475); ''autodedup:<generation>'' = the AUTODEDUP apply path '
  '(migration 558), stamped inside the merge transaction; ''v2'' is reserved for the NEW DEDUP '
  'rebuild. NULL means an operator merge. No default: a stamp is a fact about who merged.';

------------------------------------------------------------------
-- 2. the apply ledger
------------------------------------------------------------------

create table if not exists autodedup.applied_merges (
  id                   bigserial   primary key,
  run_id               text        not null,
  generation           text        not null,
  cluster_key          bigint      not null,
  survivor_property_id bigint,
  retired_property_id  bigint,
  merge_group_id       uuid,
  dry_run              boolean     not null,
  outcome              text        not null
    check (outcome in ('planned', 'applied', 'skipped', 'refused', 'failed')),
  error                text,
  listings_moved       integer,
  member_ids           bigint[],
  plan_json            jsonb,
  applied_at           timestamptz not null default now(),
  undone_at            timestamptz,
  undone_by            text,
  undo_result          jsonb,
  constraint autodedup_applied_merges_applied_ck check (
    outcome <> 'applied'
    or (not dry_run
        and merge_group_id is not null
        and survivor_property_id is not null
        and retired_property_id is not null)
  )
);

create index if not exists autodedup_applied_merges_cluster_idx
  on autodedup.applied_merges (generation, cluster_key);
create index if not exists autodedup_applied_merges_group_idx
  on autodedup.applied_merges (merge_group_id) where merge_group_id is not null;
create index if not exists autodedup_applied_merges_retired_idx
  on autodedup.applied_merges (retired_property_id) where outcome in ('applied', 'refused');
create unique index if not exists autodedup_applied_merges_live_pair_uidx
  on autodedup.applied_merges (survivor_property_id, retired_property_id)
  where outcome = 'applied' and undone_at is null;
create index if not exists autodedup_applied_merges_members_idx
  on autodedup.applied_merges using gin (member_ids)
  where outcome = 'applied';

comment on table autodedup.applied_merges is
  'AUTODEDUP A1: every group the apply path planned (dry run), applied, refused or skipped, '
  'one row per retired property. ONE merge_group_id per engine group, so unmerge_group '
  'undoes a whole group. The engine''s own record; nothing reads property_merge_events (D7).';

alter table autodedup.applied_merges enable row level security;
revoke all on autodedup.applied_merges from anon, authenticated;
revoke all on sequence autodedup.applied_merges_id_seq from anon, authenticated;

comment on column autodedup.applied_merges.member_ids is
  'The engine group''s listings as planned. A live merge''s set is the one thing that lets a '
  'later merge carry a listing its own group does not hold: only a listing this engine '
  'already merged onto the property together with a grouped member (E903).';

comment on column autodedup.applied_merges.undone_by is
  'Who undid this merge: ''autodedup-unapply:<run_id>'' = the engine''s own unapply, which '
  'releases the properties to a later generation; ''external'' = someone else had already '
  'undone it when unapply reached it, so the engine never re-unites those listings nor '
  're-merges those properties (E905).';

------------------------------------------------------------------
-- 3. the whole-generation unapply stamp
------------------------------------------------------------------

create table if not exists autodedup.unapplied_generations (
  id           bigserial   primary key,
  generation   text        not null,
  run_id       text        not null,
  undone_by    text        not null,
  unapplied_at timestamptz not null default now(),
  released_at  timestamptz,
  released_by  text
);

create index if not exists autodedup_unapplied_generations_gen_idx
  on autodedup.unapplied_generations (generation);

comment on table autodedup.unapplied_generations is
  'AUTODEDUP A1: one row per whole-generation unapply. While a row stands unreleased, no group '
  'of that generation applies again; an apply dispatched with reapply=1 releases it (E905).';

alter table autodedup.unapplied_generations enable row level security;
revoke all on autodedup.unapplied_generations from anon, authenticated;
revoke all on sequence autodedup.unapplied_generations_id_seq from anon, authenticated;

------------------------------------------------------------------
-- 4. the operator's switches, seeded OFF
------------------------------------------------------------------

insert into app_settings (key, value, description)
values
  (
    'autodedup_apply_enabled',
    'false'::jsonb,
    'Lets the duplicate-finding engine MERGE the adverts it groups, in Browse, through the same '
    'merge the operator uses (notes, tags, collections and pipeline cards move to the '
    'surviving entry). Off, a run only writes a plan you can read. On, a run still merges only '
    'when dispatched with dry_run=0 and only inside autodedup_apply_scope. Every merge it makes '
    'can be undone group by group (lane mode unapply). Turning it off stops a running merge '
    'between two groups. Set to true to turn it on, false to turn it off.'
  ),
  (
    'autodedup_apply_scope',
    '{"category_types": ["prodej"], "blocks": null, "listing_ids": null, "all_blocks": false, "max_cluster_size": 8, "max_clusters_per_run": 200}'::jsonb,
    'Where the duplicate-finding engine may merge while autodedup_apply_enabled is on. '
    'category_types: the deal types (prodej = sales, pronajem = rentals). An AREA is required '
    'too, or nothing merges: blocks (a list such as ["obec:563510", "cast_obce:490245"]), '
    'listing_ids (a list of advert ids), or all_blocks true for the whole country. '
    'max_cluster_size: the largest group it merges. max_clusters_per_run: the most groups one '
    'run merges. A run can narrow this scope, never widen it.'
  )
on conflict (key) do nothing;

reset lock_timeout;
