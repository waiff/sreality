-- 566_listing_location_katastr_kod.sql -- MF program PR-B, step 1 of 2: the 27th column.
--
-- `listing_location.katastr_kod` is the katastrální území (KÚ) of the listing's BOUND registry
-- entity, stored once by the resolver's FILL step (resolver v5.4, location_data/resolver/fill.py)
-- and read by `mf_reference()` (565) as a code. Operator ruling 2026-09 (D5): a column, because
-- the per-read KÚ point-in-polygon it replaces is what killed the hourly MF job on 2026-09-13.
--
-- The rule it holds is one sentence: the single KÚ of the bound entity, else NULL -- an address
-- point's own KÚ; a KÚ (or a ZSJ inside one) bound as a unit; the one KÚ of a one-KÚ obec; a
-- street or část obce whose every RÚIAN door lies in one KÚ (the Q7 door rule). NEVER from a
-- portal pin. NULL = "not known at KÚ grain", which is a town-level location to MF.
--
-- METADATA-ONLY: nullable, no default, no backfill statement -- the v5.4 re-resolve (the drain's
-- nightly sweep re-queues every row whose `resolver_version` differs) populates it. A catalog
-- change needs ACCESS EXCLUSIVE for an instant, but `listing_location` is read for minutes by
-- both read-model rebuilds (browse_list */15 via browse_projection, properties_map_mv 7,37):
-- a bare ALTER would lose its lock race to a rebuild on about a third of attempts. So the file
-- takes both rebuild advisory locks FIRST (522/535/561's preamble: the acquire queues behind
-- an in-flight rebuild, then every pg_cron tick self-skips), and only then fails fast on the
-- ALTER itself (the hot-table rule): 5 s, under the 8 s statement budget of the serving reads
-- that queue behind it; `apply_migration.yml` re-runs the file on a lock timeout, and every
-- statement is idempotent.
--
-- APPLY BEFORE THE CODE MERGES: the v5.4 upsert names the column. The view swap that makes the
-- serving surfaces read it is 567, applied only after the re-resolve drained (its precondition
-- refuses earlier).
--
-- Verify: select count(*) from pg_attribute where attrelid = 'listing_location'::regclass
--           and attname = 'katastr_kod' and not attisdropped;   -- 1
-- Rollback: none needed (additive, NULL-safe); a drop would be destructive (rule 1).

set statement_timeout = '900s';
set lock_timeout = 0;

select pg_advisory_lock(hashtext('rebuild_browse_list'));
select pg_advisory_lock(hashtext('rebuild_properties_map_mv'));

set lock_timeout = '5s';

alter table public.listing_location
  add column if not exists katastr_kod bigint;

comment on column public.listing_location.katastr_kod is
  'The single katastralni uzemi of the bound registry entity (address point PIP at the row''s '
  'registry version; KU/ZSJ unit; one-KU obec; street/cast obce whose every door lies in one '
  'KU), else NULL. Never from a portal pin. Written by the resolver''s FILL step (v5.4).';

select pg_advisory_unlock(hashtext('rebuild_browse_list'));
select pg_advisory_unlock(hashtext('rebuild_properties_map_mv'));

reset statement_timeout;
reset lock_timeout;
