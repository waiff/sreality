-- 538: a generation never rewrites another generation's rows, and a cluster verdict binds a
-- MEMBER SET (PROGRAM.md rule E58, §13).
--
-- WHY. `autodedup.clusters` was keyed on `cluster_key` ALONE, with `generation` a plain
-- column. A cluster key is the smallest listing id ever admitted, so two passes over the same
-- cohort produce the SAME keys — and the score lane's `on conflict (cluster_key) do update`
-- therefore re-stamped them. Promoting g5 (score run 35474719811) did exactly that: g4 went
-- from 870 clusters to the 34 keys g5 happened not to claim, `mode=labels generation=g4`
-- stopped being reproducible from the store, and the operator's 224 cluster-grain `same`
-- verdicts — taken on g4 groups — silently re-attached to g5 groups: 203 with an identical
-- member set, 11 that GREW by a bridge (keys 38324, 46536, 69651, 86335, 200790, 312776,
-- 337184, 414220, 425618, 530172, 13850718) and 10 absorbed into another key. A cluster
-- verdict is a statement about a SET OF LISTINGS and the store never recorded that set.
--
-- WHAT THIS MIGRATION DOES, in one sentence each:
--   * `clusters` is keyed `(generation, cluster_key)`, so a pass can only ever overwrite its
--     own rows;
--   * `cluster_members` carries the generation too (backfilled from its cluster) and is keyed
--     `(generation, cluster_key, listing_id)`;
--   * `pairs` carries the generation (backfilled from the `(model_version, feature_version)`
--     map `autodedup.runs` records, `autodedup.clusters` as the fallback, the literal
--     `legacy` for a row neither can place) and is keyed `(generation, listing_lo,
--     listing_hi)`;
--   * `verdicts` gains `generation` and `member_ids` — the sorted listing ids the operator was
--     looking at — the cluster-grain uniqueness gains the generation so one pass's ruling can
--     never overwrite another's, and the g4-era cluster rulings are backfilled from the
--     faithful g4 evidence.
-- Every index the reads need is re-created GENERATION-FIRST under a new name, and the old one
-- is dropped only after its replacement exists.
--
-- WHAT HAPPENS TO THE 34 SURVIVING g4 ROWS. Nothing: they keep their rows, their members and
-- their `generation = 'g4'`, and after this migration they are the only clusters g4 still has
-- in the store. This migration does NOT resurrect the 836 keys g5 re-stamped — their cluster
-- rows are g5's now and rewriting them back would be the same defect pointed the other way.
-- g4's clustering is restored by RE-RUNNING the score lane at g4's settings, which after this
-- migration writes only into `generation = 'g4'` and cannot disturb g5. Until then the 224
-- backfilled `member_ids` are what makes g4's rulings readable at all: the labels lane derives
-- its implied pairs from them, not from a clustering that is gone.
--
-- PAIR-GRAIN VERDICTS AND `must_not_link` STAY GENERATION-FREE, by design: a pair ruling is
-- about two listings and says exactly as much about g4 as about g5. The new CHECK states it,
-- so a future writer cannot quietly stamp a generation onto one.
--
-- ADDITIVE AND IDEMPOTENT. Every column is `add column if not exists`; every index is
-- `create index if not exists` under a NEW name with the old one dropped afterwards; every
-- constraint swap sits in a guarded `do` block, so a re-apply re-reads the catalog and does
-- nothing. No row is deleted and no column is dropped. `lock_timeout` is set plain (never
-- `SET LOCAL` — the apply path is statement-autocommit) because each of these ALTERs takes an
-- ACCESS EXCLUSIVE lock and must not queue behind a long reader.

set lock_timeout = '5s';

------------------------------------------------------------------
-- 1. cluster_members: the generation, carried from its cluster
------------------------------------------------------------------

alter table autodedup.cluster_members
  add column if not exists generation text;

-- Read through a scalar subquery rather than `update ... from`: the clusters PK is about to
-- change under it, and a correlated `order by ... limit 1` gives the same answer whatever
-- index is in place. `last_changed_at desc` is the tie-break that can never fire today (one
-- row per key) and is the honest one the day it could.
update autodedup.cluster_members m
   set generation = (
         select c.generation
           from autodedup.clusters c
          where c.cluster_key = m.cluster_key
          order by c.last_changed_at desc, c.generation desc
          limit 1
       )
 where m.generation is null;

-- A member row whose cluster row is already gone: an orphan from an interrupted pass. There
-- are none today; `legacy` is the value that keeps `not null` honest if there ever are.
update autodedup.cluster_members
   set generation = 'legacy'
 where generation is null;

alter table autodedup.cluster_members
  alter column generation set not null;

------------------------------------------------------------------
-- 2. clusters: keyed (generation, cluster_key)
------------------------------------------------------------------

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'autodedup_clusters_pkey'
       and conrelid = 'autodedup.clusters'::regclass
  ) then
    alter table autodedup.clusters drop constraint if exists clusters_pkey;
    alter table autodedup.clusters
      add constraint autodedup_clusters_pkey primary key (generation, cluster_key);
  end if;
end
$$;

-- The key ALONE is still asked for — "does any pass hold this cluster?" — and the composite PK
-- cannot answer a leading-column-free lookup.
create index if not exists autodedup_clusters_key_idx
  on autodedup.clusters (cluster_key);

-- The two working orders of the groups queue, generation-first because every read is now
-- scoped to one pass.
create index if not exists autodedup_clusters_gen_review_idx
  on autodedup.clusters (generation, min_edge_score, cluster_key desc);
create index if not exists autodedup_clusters_gen_status_idx
  on autodedup.clusters (generation, status, size desc);
drop index if exists autodedup.autodedup_clusters_review_idx;
drop index if exists autodedup.autodedup_clusters_status_idx;

------------------------------------------------------------------
-- 3. cluster_members: keyed (generation, cluster_key, listing_id)
------------------------------------------------------------------

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'autodedup_cluster_members_pkey'
       and conrelid = 'autodedup.cluster_members'::regclass
  ) then
    alter table autodedup.cluster_members drop constraint if exists cluster_members_pkey;
    alter table autodedup.cluster_members
      add constraint autodedup_cluster_members_pkey
      primary key (generation, cluster_key, listing_id);
  end if;
end
$$;

create index if not exists autodedup_cluster_members_gen_listing_idx
  on autodedup.cluster_members (generation, listing_id);
drop index if exists autodedup.autodedup_cluster_members_listing_idx;

------------------------------------------------------------------
-- 4. pairs: the generation, and (generation, listing_lo, listing_hi)
------------------------------------------------------------------

alter table autodedup.pairs
  add column if not exists generation text;

-- `autodedup.runs` is the authoritative map because a score run RECORDS its own
-- `(generation, model_version, feature_version)` in `params` and nothing re-stamps a finished
-- run. `clusters` is the fallback for a pass whose run row is gone, and `legacy` is what a row
-- neither can place reads as — never a guess. Compared as TEXT: `feature_version` is a jsonb
-- string on one side and a smallint on the other, and a cast would turn a malformed param into
-- an error for the whole statement.
update autodedup.pairs p
   set generation = coalesce(
         (select r.params ->> 'generation'
            from autodedup.runs r
           where r.mode = 'score'
             and r.status = 'success'
             and r.params ->> 'model_version' = p.model_version
             and r.params ->> 'feature_version' = p.feature_version::text
           order by r.started_at desc
           limit 1),
         (select c.generation
            from autodedup.clusters c
           where c.model_version = p.model_version
             and c.feature_version = p.feature_version
           order by c.last_changed_at desc
           limit 1),
         'legacy'
       )
 where p.generation is null;

alter table autodedup.pairs
  alter column generation set not null;

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'autodedup_pairs_pkey'
       and conrelid = 'autodedup.pairs'::regclass
  ) then
    alter table autodedup.pairs drop constraint if exists pairs_pkey;
    alter table autodedup.pairs
      add constraint autodedup_pairs_pkey primary key (generation, listing_lo, listing_hi);
  end if;
end
$$;

create index if not exists autodedup_pairs_gen_band_idx
  on autodedup.pairs (generation, score desc) where zone = 'band';
create index if not exists autodedup_pairs_gen_hi_idx
  on autodedup.pairs (generation, listing_hi);
create index if not exists autodedup_pairs_gen_cluster_idx
  on autodedup.pairs (generation, cluster_key) where cluster_key is not null;
create index if not exists autodedup_pairs_gen_decided_idx
  on autodedup.pairs (generation, decided_at desc);
drop index if exists autodedup.autodedup_pairs_band_idx;
drop index if exists autodedup.autodedup_pairs_hi_idx;
drop index if exists autodedup.autodedup_pairs_cluster_idx;
drop index if exists autodedup.autodedup_pairs_decided_idx;

------------------------------------------------------------------
-- 5. verdicts: which pass, and WHICH SET OF LISTINGS
------------------------------------------------------------------

alter table autodedup.verdicts
  add column if not exists generation text;
alter table autodedup.verdicts
  add column if not exists member_ids bigint[];

do $$
begin
  if not exists (
    select 1 from pg_constraint
     where conname = 'autodedup_verdicts_grain_ck'
       and conrelid = 'autodedup.verdicts'::regclass
  ) then
    alter table autodedup.verdicts
      add constraint autodedup_verdicts_grain_ck
      check (kind = 'cluster' or (generation is null and member_ids is null));
  end if;
end
$$;

comment on column autodedup.verdicts.generation is
  'Which clustering pass the ruled group belonged to. NULL = legacy (taken before the store
   recorded it), and a legacy row reads as it did before migration 538.';
comment on column autodedup.verdicts.member_ids is
  'The sorted listing ids the operator was looking at. A cluster verdict APPLIES to a group
   only when this equals the group current member set; otherwise the group reads as unreviewed
   and carries a stale-verdict hint (rule E58). NULL on pair-grain and on legacy rows.';

-- E58'S OTHER HALF, on the one table this wave exists to protect. `autodedup_verdicts_cluster_uidx`
-- (migration 528) is UNIQUE on (kind, cluster_key, decided_by), so the store can hold ONE cluster
-- ruling per key per operator and the upsert re-stamps it in place: ruling the g5 group 38324 would
-- overwrite the g4 ruling backfilled below, which is the LAST copy of what the operator saw there.
-- That is the same defect as the one on `clusters`, one table over. The key now carries the pass, so
-- a re-ruling can only ever replace a ruling of the SAME pass. `coalesce` because NULL never equals
-- NULL in a unique index, which would let the six legacy rows duplicate. Created before the old one
-- is dropped, as everywhere else in this file. Pair-grain uniqueness is untouched: a pair ruling is
-- about two adverts and no pass owns it.
create unique index if not exists autodedup_verdicts_cluster_gen_uidx on autodedup.verdicts
  (kind, cluster_key, (coalesce(generation, ''::text)), decided_by) where kind = 'cluster';
drop index if exists autodedup.autodedup_verdicts_cluster_uidx;

------------------------------------------------------------------
-- 6. the backfill: which set the operator actually ruled on
------------------------------------------------------------------
--
-- 224 literal UPDATEs, one per cluster key the operator confirmed on a g4 group. The member
-- sets come from the FAITHFUL g4 evidence, cross-checked two ways and byte-identical in both:
-- the implied labels of the last faithful export (labels run 35468267042, taken 2026-09-19
-- 22:44 UTC — 14 minutes before the g5 promotion — each carrying its `cluster_key`), and the
-- local g4 engine reproduction (870 clusters, `model_version` `w5_gold`). Every one of the 224
-- keys is present in both and no member set differs.
--
-- `member_ids is null` is the idempotency guard AND the precedence rule: a re-ruling taken
-- after this migration stamps its own set, and re-applying the file must not overwrite it.
-- The ids are written sorted, which is the shape every read compares against.
--
-- BOUNDED IN TIME AS WELL AS ON NULL. The apply happens BEFORE the PR merges, so for a few
-- minutes the OLD api writes cluster rulings with `generation` and `member_ids` both NULL —
-- rows the NULL guard alone cannot tell from the September ones. A lock-timeout retry, or any
-- later replay of this file, would then stamp a ruling about TODAY's group with September's
-- member set. Every cluster verdict this backfill is about was decided by 2026-09-18 14:17
-- UTC (the whole store: 230 cluster rulings, one operator), so nothing decided from
-- 2026-09-19 on can be claimed by this file however often it runs.
update autodedup.verdicts set generation = 'g4', member_ids = array[309,140919]::bigint[] where kind = 'cluster' and cluster_key = 309 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[318,140923]::bigint[] where kind = 'cluster' and cluster_key = 318 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[335,140928]::bigint[] where kind = 'cluster' and cluster_key = 335 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[1060,93626]::bigint[] where kind = 'cluster' and cluster_key = 1060 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[1314,134344]::bigint[] where kind = 'cluster' and cluster_key = 1314 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[1717,149955]::bigint[] where kind = 'cluster' and cluster_key = 1717 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[4992,380925]::bigint[] where kind = 'cluster' and cluster_key = 4992 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[7057,10371937]::bigint[] where kind = 'cluster' and cluster_key = 7057 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[7258,381602]::bigint[] where kind = 'cluster' and cluster_key = 7258 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[8918,158484,180065]::bigint[] where kind = 'cluster' and cluster_key = 8918 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[9192,144193,381110,432346]::bigint[] where kind = 'cluster' and cluster_key = 9192 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[11561,377683]::bigint[] where kind = 'cluster' and cluster_key = 11561 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[14093,149907]::bigint[] where kind = 'cluster' and cluster_key = 14093 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[14581,93520]::bigint[] where kind = 'cluster' and cluster_key = 14581 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[16407,118753]::bigint[] where kind = 'cluster' and cluster_key = 16407 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[16438,92824]::bigint[] where kind = 'cluster' and cluster_key = 16438 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[16552,18661514]::bigint[] where kind = 'cluster' and cluster_key = 16552 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18181,140826]::bigint[] where kind = 'cluster' and cluster_key = 18181 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18685,367561]::bigint[] where kind = 'cluster' and cluster_key = 18685 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[20650,13435823]::bigint[] where kind = 'cluster' and cluster_key = 20650 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[22951,11725458]::bigint[] where kind = 'cluster' and cluster_key = 22951 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[23140,420274]::bigint[] where kind = 'cluster' and cluster_key = 23140 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[23198,399606]::bigint[] where kind = 'cluster' and cluster_key = 23198 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[24065,104956,420176,10513535]::bigint[] where kind = 'cluster' and cluster_key = 24065 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[24071,10513534]::bigint[] where kind = 'cluster' and cluster_key = 24071 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[24072,10513526]::bigint[] where kind = 'cluster' and cluster_key = 24072 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[24074,10513429]::bigint[] where kind = 'cluster' and cluster_key = 24074 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[24077,10513425]::bigint[] where kind = 'cluster' and cluster_key = 24077 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[24205,401459]::bigint[] where kind = 'cluster' and cluster_key = 24205 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[24213,97651]::bigint[] where kind = 'cluster' and cluster_key = 24213 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[24491,140843,18890779]::bigint[] where kind = 'cluster' and cluster_key = 24491 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[24535,10558867]::bigint[] where kind = 'cluster' and cluster_key = 24535 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[24991,358812]::bigint[] where kind = 'cluster' and cluster_key = 24991 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[29234,165681,373213]::bigint[] where kind = 'cluster' and cluster_key = 29234 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[29913,173937,367559,415951]::bigint[] where kind = 'cluster' and cluster_key = 29913 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[31052,373842,420241]::bigint[] where kind = 'cluster' and cluster_key = 31052 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[32677,179736]::bigint[] where kind = 'cluster' and cluster_key = 32677 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[34406,371479]::bigint[] where kind = 'cluster' and cluster_key = 34406 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[35981,141669,186500,394858]::bigint[] where kind = 'cluster' and cluster_key = 35981 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[36078,116461,197893,359819]::bigint[] where kind = 'cluster' and cluster_key = 36078 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[36079,116356,359820]::bigint[] where kind = 'cluster' and cluster_key = 36079 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[36560,222565]::bigint[] where kind = 'cluster' and cluster_key = 36560 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[37074,152670,388721]::bigint[] where kind = 'cluster' and cluster_key = 37074 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[37543,254803]::bigint[] where kind = 'cluster' and cluster_key = 37543 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[37721,10504646]::bigint[] where kind = 'cluster' and cluster_key = 37721 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[38120,111734]::bigint[] where kind = 'cluster' and cluster_key = 38120 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[38324,254433,481849]::bigint[] where kind = 'cluster' and cluster_key = 38324 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[38616,10682123]::bigint[] where kind = 'cluster' and cluster_key = 38616 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[38879,212578,254168]::bigint[] where kind = 'cluster' and cluster_key = 38879 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[39047,216087,259749,355106,455798]::bigint[] where kind = 'cluster' and cluster_key = 39047 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[39048,215497,257809,454855,491533]::bigint[] where kind = 'cluster' and cluster_key = 39048 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[39164,10682007]::bigint[] where kind = 'cluster' and cluster_key = 39164 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[39448,255779,428938,451990]::bigint[] where kind = 'cluster' and cluster_key = 39448 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[39788,245098,359289,439553]::bigint[] where kind = 'cluster' and cluster_key = 39788 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[40015,200054,244334,414957,441076,17903375]::bigint[] where kind = 'cluster' and cluster_key = 40015 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[40256,246761]::bigint[] where kind = 'cluster' and cluster_key = 40256 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[40384,246547,440401,488594]::bigint[] where kind = 'cluster' and cluster_key = 40384 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[40447,248510]::bigint[] where kind = 'cluster' and cluster_key = 40447 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[41230,488294]::bigint[] where kind = 'cluster' and cluster_key = 41230 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[41240,245630,446115,488493]::bigint[] where kind = 'cluster' and cluster_key = 41240 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[41292,245431]::bigint[] where kind = 'cluster' and cluster_key = 41292 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[41883,215940]::bigint[] where kind = 'cluster' and cluster_key = 41883 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[43402,172076,223209,388969,442592]::bigint[] where kind = 'cluster' and cluster_key = 43402 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[43634,259206,456906]::bigint[] where kind = 'cluster' and cluster_key = 43634 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[44056,415021]::bigint[] where kind = 'cluster' and cluster_key = 44056 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[44057,415019]::bigint[] where kind = 'cluster' and cluster_key = 44057 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[46536,442359]::bigint[] where kind = 'cluster' and cluster_key = 46536 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[48671,254829]::bigint[] where kind = 'cluster' and cluster_key = 48671 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[48771,254844,450976]::bigint[] where kind = 'cluster' and cluster_key = 48771 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[49509,247648]::bigint[] where kind = 'cluster' and cluster_key = 49509 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[49714,245963]::bigint[] where kind = 'cluster' and cluster_key = 49714 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[50973,156796]::bigint[] where kind = 'cluster' and cluster_key = 50973 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[53580,322003]::bigint[] where kind = 'cluster' and cluster_key = 53580 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[54159,108619,150370,153327]::bigint[] where kind = 'cluster' and cluster_key = 54159 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[55672,151476]::bigint[] where kind = 'cluster' and cluster_key = 55672 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[56795,70589,107324,114840]::bigint[] where kind = 'cluster' and cluster_key = 56795 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[56924,165974]::bigint[] where kind = 'cluster' and cluster_key = 56924 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[57661,137017]::bigint[] where kind = 'cluster' and cluster_key = 57661 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[58482,149796]::bigint[] where kind = 'cluster' and cluster_key = 58482 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[58594,134099,143601]::bigint[] where kind = 'cluster' and cluster_key = 58594 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[58888,166747]::bigint[] where kind = 'cluster' and cluster_key = 58888 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[59419,93135]::bigint[] where kind = 'cluster' and cluster_key = 59419 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[59617,18673913]::bigint[] where kind = 'cluster' and cluster_key = 59617 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[59903,105458]::bigint[] where kind = 'cluster' and cluster_key = 59903 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[60261,218796]::bigint[] where kind = 'cluster' and cluster_key = 60261 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[60437,148483]::bigint[] where kind = 'cluster' and cluster_key = 60437 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[60895,219877]::bigint[] where kind = 'cluster' and cluster_key = 60895 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[62521,18583081]::bigint[] where kind = 'cluster' and cluster_key = 62521 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[63070,229888,445705]::bigint[] where kind = 'cluster' and cluster_key = 63070 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[64441,346609]::bigint[] where kind = 'cluster' and cluster_key = 64441 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[64978,141169]::bigint[] where kind = 'cluster' and cluster_key = 64978 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[65799,380190]::bigint[] where kind = 'cluster' and cluster_key = 65799 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[67283,138260]::bigint[] where kind = 'cluster' and cluster_key = 67283 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[67303,114278,370678,420150]::bigint[] where kind = 'cluster' and cluster_key = 67303 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[67724,226155]::bigint[] where kind = 'cluster' and cluster_key = 67724 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[68647,140778]::bigint[] where kind = 'cluster' and cluster_key = 68647 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[68973,217627,386912,511277]::bigint[] where kind = 'cluster' and cluster_key = 68973 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[69651,253456]::bigint[] where kind = 'cluster' and cluster_key = 69651 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[76171,337961,18692884]::bigint[] where kind = 'cluster' and cluster_key = 76171 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[76305,421599]::bigint[] where kind = 'cluster' and cluster_key = 76305 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[78250,231141,394330,443199]::bigint[] where kind = 'cluster' and cluster_key = 78250 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[79106,104080]::bigint[] where kind = 'cluster' and cluster_key = 79106 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[80765,205614,256885]::bigint[] where kind = 'cluster' and cluster_key = 80765 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[82014,10281401]::bigint[] where kind = 'cluster' and cluster_key = 82014 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[83499,413054]::bigint[] where kind = 'cluster' and cluster_key = 83499 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[86335,11523088]::bigint[] where kind = 'cluster' and cluster_key = 86335 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[97343,104553,418942,13410047,13410297,13426379,15425287]::bigint[] where kind = 'cluster' and cluster_key = 97343 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[97812,17245917]::bigint[] where kind = 'cluster' and cluster_key = 97812 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[102432,142199]::bigint[] where kind = 'cluster' and cluster_key = 102432 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[103218,420144]::bigint[] where kind = 'cluster' and cluster_key = 103218 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[108597,148699,150359]::bigint[] where kind = 'cluster' and cluster_key = 108597 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[119302,420154]::bigint[] where kind = 'cluster' and cluster_key = 119302 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[122755,165541,372556,420112]::bigint[] where kind = 'cluster' and cluster_key = 122755 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[128136,420167,469815,472089]::bigint[] where kind = 'cluster' and cluster_key = 128136 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[137525,142542,430172]::bigint[] where kind = 'cluster' and cluster_key = 137525 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[137953,144720,429620]::bigint[] where kind = 'cluster' and cluster_key = 137953 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[138148,348637,386962]::bigint[] where kind = 'cluster' and cluster_key = 138148 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[138805,431113,479699]::bigint[] where kind = 'cluster' and cluster_key = 138805 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[164519,285538]::bigint[] where kind = 'cluster' and cluster_key = 164519 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[165664,373028]::bigint[] where kind = 'cluster' and cluster_key = 165664 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[170729,427015,13173714]::bigint[] where kind = 'cluster' and cluster_key = 170729 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[183847,285578]::bigint[] where kind = 'cluster' and cluster_key = 183847 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[200790,219934,512890,522125,522198]::bigint[] where kind = 'cluster' and cluster_key = 200790 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[210104,449478,528129,528345,529137]::bigint[] where kind = 'cluster' and cluster_key = 210104 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[215278,359216,410036]::bigint[] where kind = 'cluster' and cluster_key = 215278 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[225129,378988]::bigint[] where kind = 'cluster' and cluster_key = 225129 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[225165,18754085]::bigint[] where kind = 'cluster' and cluster_key = 225165 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[257939,415376,450878]::bigint[] where kind = 'cluster' and cluster_key = 257939 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[266372,420131,17429052]::bigint[] where kind = 'cluster' and cluster_key = 266372 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[268914,378589]::bigint[] where kind = 'cluster' and cluster_key = 268914 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[292271,346593]::bigint[] where kind = 'cluster' and cluster_key = 292271 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[308473,314447,377974,430130,477496]::bigint[] where kind = 'cluster' and cluster_key = 308473 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[312776,425049]::bigint[] where kind = 'cluster' and cluster_key = 312776 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[320735,509346]::bigint[] where kind = 'cluster' and cluster_key = 320735 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[330112,330494,348613,376697,430125,472274,18639016]::bigint[] where kind = 'cluster' and cluster_key = 330112 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[337184,353118,360513]::bigint[] where kind = 'cluster' and cluster_key = 337184 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[338390,14048411]::bigint[] where kind = 'cluster' and cluster_key = 338390 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[339390,14048398,18564111]::bigint[] where kind = 'cluster' and cluster_key = 339390 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[343576,429530]::bigint[] where kind = 'cluster' and cluster_key = 343576 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[344912,356329]::bigint[] where kind = 'cluster' and cluster_key = 344912 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[351672,353356]::bigint[] where kind = 'cluster' and cluster_key = 351672 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[353682,353948]::bigint[] where kind = 'cluster' and cluster_key = 353682 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[358603,359472]::bigint[] where kind = 'cluster' and cluster_key = 358603 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[360178,18854158]::bigint[] where kind = 'cluster' and cluster_key = 360178 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[386333,389528,392972]::bigint[] where kind = 'cluster' and cluster_key = 386333 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[414220,414785,418524]::bigint[] where kind = 'cluster' and cluster_key = 414220 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[414294,429394]::bigint[] where kind = 'cluster' and cluster_key = 414294 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[420129,14345467]::bigint[] where kind = 'cluster' and cluster_key = 420129 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[421245,490182]::bigint[] where kind = 'cluster' and cluster_key = 421245 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[425618,472253,487018,511477]::bigint[] where kind = 'cluster' and cluster_key = 425618 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[431605,431799,443968]::bigint[] where kind = 'cluster' and cluster_key = 431605 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[448367,481229]::bigint[] where kind = 'cluster' and cluster_key = 448367 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[467015,491751]::bigint[] where kind = 'cluster' and cluster_key = 467015 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[468653,475022]::bigint[] where kind = 'cluster' and cluster_key = 468653 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[469867,472327,18765422]::bigint[] where kind = 'cluster' and cluster_key = 469867 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[482346,492230]::bigint[] where kind = 'cluster' and cluster_key = 482346 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[506095,520865]::bigint[] where kind = 'cluster' and cluster_key = 506095 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[512438,512595,512680]::bigint[] where kind = 'cluster' and cluster_key = 512438 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[514225,514428,515778]::bigint[] where kind = 'cluster' and cluster_key = 514225 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[518787,519535,519773,13220659]::bigint[] where kind = 'cluster' and cluster_key = 518787 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[519009,18718984]::bigint[] where kind = 'cluster' and cluster_key = 519009 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[522399,522756]::bigint[] where kind = 'cluster' and cluster_key = 522399 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[527229,10143279]::bigint[] where kind = 'cluster' and cluster_key = 527229 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[530172,12123552]::bigint[] where kind = 'cluster' and cluster_key = 530172 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[535131,10103060]::bigint[] where kind = 'cluster' and cluster_key = 535131 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[540998,541556,541717]::bigint[] where kind = 'cluster' and cluster_key = 540998 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[547586,547714]::bigint[] where kind = 'cluster' and cluster_key = 547586 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[555448,10130088,10534199,11192830,12947380,13221981,18747838,18854134]::bigint[] where kind = 'cluster' and cluster_key = 555448 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[10089198,10099140,10105174,10131391,10193527]::bigint[] where kind = 'cluster' and cluster_key = 10089198 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[10196193,10308853,10410436,10414702,18362178,18598884,18696264,18854132]::bigint[] where kind = 'cluster' and cluster_key = 10196193 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[10305671,10367731]::bigint[] where kind = 'cluster' and cluster_key = 10305671 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[10324809,10325845]::bigint[] where kind = 'cluster' and cluster_key = 10324809 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[10385402,10396252]::bigint[] where kind = 'cluster' and cluster_key = 10385402 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[10502733,10528614]::bigint[] where kind = 'cluster' and cluster_key = 10502733 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[11384043,11834158,11845609,11895678]::bigint[] where kind = 'cluster' and cluster_key = 11384043 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[11610597,11628291,11652587]::bigint[] where kind = 'cluster' and cluster_key = 11610597 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[11675518,11684732]::bigint[] where kind = 'cluster' and cluster_key = 11675518 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[11795516,11799040,11803544]::bigint[] where kind = 'cluster' and cluster_key = 11795516 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[11897374,12680082,13475372]::bigint[] where kind = 'cluster' and cluster_key = 11897374 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[12029919,13402690]::bigint[] where kind = 'cluster' and cluster_key = 12029919 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[12051667,12060037,12073565,12117048]::bigint[] where kind = 'cluster' and cluster_key = 12051667 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[12094218,12123060,12143512]::bigint[] where kind = 'cluster' and cluster_key = 12094218 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[12106579,12123175]::bigint[] where kind = 'cluster' and cluster_key = 12106579 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[13144781,14048572]::bigint[] where kind = 'cluster' and cluster_key = 13144781 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[13195369,13210231,13222133,18331395,18496332,18566990]::bigint[] where kind = 'cluster' and cluster_key = 13195369 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[13250742,13659984]::bigint[] where kind = 'cluster' and cluster_key = 13250742 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[13388529,13435429,13457932]::bigint[] where kind = 'cluster' and cluster_key = 13388529 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[13462905,14089984,15358962,15992219]::bigint[] where kind = 'cluster' and cluster_key = 13462905 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[13785990,13791106]::bigint[] where kind = 'cluster' and cluster_key = 13785990 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[13850718,14974320]::bigint[] where kind = 'cluster' and cluster_key = 13850718 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[13982300,13988302]::bigint[] where kind = 'cluster' and cluster_key = 13982300 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[14217487,15424454]::bigint[] where kind = 'cluster' and cluster_key = 14217487 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[14965218,14977363]::bigint[] where kind = 'cluster' and cluster_key = 14965218 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[15331914,15348707]::bigint[] where kind = 'cluster' and cluster_key = 15331914 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[15424452,16557693,17245302,17495311,17755451,18584843,18651034,18671037]::bigint[] where kind = 'cluster' and cluster_key = 15424452 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[15545029,15554993]::bigint[] where kind = 'cluster' and cluster_key = 15545029 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[15605379,16176360]::bigint[] where kind = 'cluster' and cluster_key = 15605379 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[15866963,15912495]::bigint[] where kind = 'cluster' and cluster_key = 15866963 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[16144100,16144335,16144792,16181913]::bigint[] where kind = 'cluster' and cluster_key = 16144100 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[17236058,18592640,18635168,18659091,18712086,18731992,18846684,18870845]::bigint[] where kind = 'cluster' and cluster_key = 17236058 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[17239925,17245919,17270653,17302524]::bigint[] where kind = 'cluster' and cluster_key = 17239925 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[17454462,17467135,17467651,18572494]::bigint[] where kind = 'cluster' and cluster_key = 17454462 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[17467324,18572500]::bigint[] where kind = 'cluster' and cluster_key = 17467324 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[17599825,17658453]::bigint[] where kind = 'cluster' and cluster_key = 17599825 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18008658,18362330,18444915]::bigint[] where kind = 'cluster' and cluster_key = 18008658 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18322788,18602314]::bigint[] where kind = 'cluster' and cluster_key = 18322788 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18496296,18568904]::bigint[] where kind = 'cluster' and cluster_key = 18496296 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18562199,18607560]::bigint[] where kind = 'cluster' and cluster_key = 18562199 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18564022,18606821]::bigint[] where kind = 'cluster' and cluster_key = 18564022 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18588784,18589533,18590375,18590776,18595162,18617060]::bigint[] where kind = 'cluster' and cluster_key = 18588784 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18597093,18717574]::bigint[] where kind = 'cluster' and cluster_key = 18597093 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18599860,18792288]::bigint[] where kind = 'cluster' and cluster_key = 18599860 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18632715,18633067,18634662]::bigint[] where kind = 'cluster' and cluster_key = 18632715 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18650269,18650307,18650330,18650351,18650464,18651734]::bigint[] where kind = 'cluster' and cluster_key = 18650269 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18657612,18657900]::bigint[] where kind = 'cluster' and cluster_key = 18657612 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18669939,18669989,18670086,18670371,18672679]::bigint[] where kind = 'cluster' and cluster_key = 18669939 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18673845,18675912,18676568]::bigint[] where kind = 'cluster' and cluster_key = 18673845 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18695898,18710161]::bigint[] where kind = 'cluster' and cluster_key = 18695898 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18725991,18728583,18731484]::bigint[] where kind = 'cluster' and cluster_key = 18725991 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18732347,18734038,18736253]::bigint[] where kind = 'cluster' and cluster_key = 18732347 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18765596,18766669]::bigint[] where kind = 'cluster' and cluster_key = 18765596 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18826060,18826140,18826756,18828588]::bigint[] where kind = 'cluster' and cluster_key = 18826060 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18863103,18865178]::bigint[] where kind = 'cluster' and cluster_key = 18863103 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';
update autodedup.verdicts set generation = 'g4', member_ids = array[18908029,18908047,18908756,18911304]::bigint[] where kind = 'cluster' and cluster_key = 18908029 and member_ids is null and decided_at < timestamptz '2026-09-19 00:00+00';

-- Every OTHER cluster-grain verdict: the generation that was newest when it was decided,
-- read off the score runs' own finish times — and only where that pass actually holds the
-- ruled key. Six rows fail that test (cluster keys 55344, 94020, 94492, 15012011, 243787,
-- 13572872): they were taken on g1 groups that no later pass ever re-clustered, so no
-- generation can be claimed for them and they stay NULL, which reads as legacy. They carry
-- no `member_ids` either — there is no faithful record of what the operator saw — so they go
-- on applying to their own cluster exactly as they did before this migration.
with era as (
    select v.id,
           (select r.params ->> 'generation'
              from autodedup.runs r
             where r.mode = 'score'
               and r.status = 'success'
               and r.finished_at is not null
               and r.finished_at <= v.decided_at
             order by r.finished_at desc
             limit 1) as generation
      from autodedup.verdicts v
     where v.kind = 'cluster'
       and v.generation is null
       and v.decided_at < timestamptz '2026-09-19 00:00+00'
)
update autodedup.verdicts v
   set generation = era.generation
  from era
 where v.id = era.id
   and era.generation is not null
   and exists (
         select 1
           from autodedup.clusters c
          where c.cluster_key = v.cluster_key
            and c.generation = era.generation
       );

reset lock_timeout;
