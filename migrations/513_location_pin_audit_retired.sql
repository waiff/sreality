-- 513_location_pin_audit_retired.sql
--
-- The pin-loss audit surface (migration 510) is retired. It was built as a
-- TEMPORARY operator review surface — 510's own header and the roadmap entry
-- both say "delete the whole set once the operator has ruled" — and the ruling
-- landed on 2026-09-13: migration 503's guard now allows ZERO new pin loss,
-- exempting exactly the audited residue, and 503 is applied.
--
-- IT IS ALSO A PRECONDITION FOR 508, not merely tidying. `location_pin_audit_mv`
-- selects `p.street`, `p.district`, `p.locality`, `p.lat` and `p.lng` from
-- `properties`, so Postgres records a column dependency on all five, and 508's
-- `alter table properties drop column ...` would fail with "cannot drop column
-- street of table properties because other objects depend on it". 508 predates
-- 510 and cannot know about it. So this file runs BEFORE 508 on production:
--
--     511 (backup) -> 506 -> 512 -> 507 -> 513 (this file) -> 508
--
-- THE REPLAY. In a fresh schema replay the files run in numeric order, so 508
-- would drop the columns and 510 would then try to build the matview out of
-- them and fail. 510 is therefore skipped by the replay
-- (`.github/workflows/migrations.yml`), which reaches the same end state this
-- file leaves behind: no audit objects at all. Migrations are append-only
-- (CLAUDE.md rule 1) — 510 stays on disk exactly as it was applied.
--
-- WHAT GOES: the matview, its two functions, the hourly pg_cron job and the
-- `derived_artifacts` registry row. The SPA page that read them
-- (`/new-dedup/pin-audit`) is unrouted in the same PR.

-- The cron job first: an hourly refresh of a matview that is about to not exist
-- would fail every hour. Guarded because the CI replay container has no pg_cron
-- (migrations 136/274/510's own precedent).
do $$
begin
  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    perform cron.unschedule('refresh-location-pin-audit')
      where exists (select 1 from cron.job where jobname = 'refresh-location-pin-audit');
    raise notice 'pin-audit cron job unscheduled (if it was there)';
  else
    raise notice 'pg_cron unavailable; nothing to unschedule';
  end if;
end
$$;

drop function if exists location_pin_audit_summary();
drop function if exists refresh_location_pin_audit_mv();

-- CASCADE is deliberate and narrow: the matview's own indexes are the only
-- objects that depend on it, the SPA page having lost its route in this PR.
drop materialized view if exists location_pin_audit_mv cascade;

delete from derived_artifacts where name = 'location_pin_audit_mv';

do $$
begin
  if to_regclass('public.location_pin_audit_mv') is not null
     or to_regproc('public.location_pin_audit_summary') is not null
     or to_regproc('public.refresh_location_pin_audit_mv') is not null then
    raise exception 'the pin-audit surface is still present; 508 will fail on the '
      'properties column dependency';
  end if;
  raise notice 'pin-audit surface retired';
end
$$;
