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
-- change needs ACCESS EXCLUSIVE for an instant on a table the drain writes continuously, so the
-- hot-table rule applies: fail fast and let `apply_migration.yml` re-run the file (idempotent).
--
-- APPLY BEFORE THE CODE MERGES: the v5.4 upsert names the column. The view swap that makes the
-- serving surfaces read it is 567, applied only after the re-resolve drained (its precondition
-- refuses earlier).
--
-- Verify: select count(*) from pg_attribute where attrelid = 'listing_location'::regclass
--           and attname = 'katastr_kod' and not attisdropped;   -- 1
-- Rollback: none needed (additive, NULL-safe); a drop would be destructive (rule 1).

set lock_timeout = '5s';

alter table public.listing_location
  add column if not exists katastr_kod bigint;

comment on column public.listing_location.katastr_kod is
  'The single katastralni uzemi of the bound registry entity (address point PIP at the row''s '
  'registry version; KU/ZSJ unit; one-KU obec; street/cast obce whose every door lies in one '
  'KU), else NULL. Never from a portal pin. Written by the resolver''s FILL step (v5.4).';
