-- 432_new_dedup_teardown.sql
-- Cardinality Doctrine W0b / F3 — finish the NEW DEDUP teardown.
--
-- THIS IS NOT A CADENCE DECISION. It is an UNFINISHED MIGRATION.
-- docs/design/new-dedup/CUTOFF.md:102-103 already sanctioned dropping
-- dedup_funnel_resolutions_mv/_public and dedup_llm_cost_by_category_mv/_public
-- "(+ cron.unschedule their refresh jobs)", and runbook step 7 lists the unschedule.
-- The step was written down and skipped. property_sources_mv joins them as an orphan of
-- the same teardown week.
--
-- WHAT IT COSTS TODAY, measured live (pg_stat_statements, reset 2026-08-11):
--   refresh ... dedup_funnel_resolutions_mv     1,106 calls / 16,681.7 s  (~15.1 s each)
--   refresh ... dedup_llm_cost_by_category_mv   1,106 calls /    562.4 s
-- That is 4h 37m of server time in two weeks to maintain 136 kB that NOTHING reads — each
-- refresh scanning dedup_pair_audit (130 MB) joined twice against the hot listings table.
-- Over 24 h the job books 34,399 s, and every tick competes with browse-list-rebuild
-- (28.2% failure rate) and refresh-health-dashboard (34.9%) on an instance whose scheduler
-- runs a 142% duty cycle.
--
-- NOTHING READS THEM. Zero hits across frontend/src, api/, toolkit/, scraper/,
-- location_data/, chrome-extension/. The only non-migration references are tests (updated
-- in this PR) and one backup script. Corroborated at the catalog level: property_sources_mv
-- has seq_scan = 0 and idx_scan = 5, all five attributable to investigation probes.
--
-- LITERAL NAMES ONLY, NEVER A PATTERN. public.property_sources_public is a DIFFERENT
-- object — a live view reading listings directly, on the Browse and Listing-Detail read
-- paths (frontend/src/lib/queries.ts, ListingDetail.tsx, priceHistory.ts). Any script that
-- pattern-matches `property_sources%` takes production down. It is untouched here.
--
-- Rollback: migrations/reverts/432_revert_new_dedup_teardown.sql (shipped unapplied,
-- executed once against a real PG17 replay — see the PR body for the run URL).

begin;

set local lock_timeout = '5s';

-- ---------------------------------------------------------------------------
-- 1. Unschedule FIRST, by NAME. Dropping the matviews while the job is live would make
--    every 15-minute tick error against objects that no longer exist.
--
--    Guarded two ways: cron.unschedule() RAISES if the job is absent, and the CI replay
--    container has no pg_cron extension at all.
--
--    to_regPROCEDURE, not to_regPROC. This guard was written as
--    `to_regproc('cron.unschedule(text)')` and that is ALWAYS NULL two ways over:
--    to_regproc takes a BARE name (the argument list belongs to to_regprocedure), and the
--    bare name is ambiguous across cron.unschedule(bigint) and cron.unschedule(text), which
--    also yields NULL. The DO block skipped silently, the job survived the teardown, and
--    for a few minutes it was scheduled against objects that no longer existed. A guard
--    that cannot fire is worse than no guard: it looks like protection and is not.
-- ---------------------------------------------------------------------------
do $$
begin
  if to_regprocedure('cron.unschedule(text)') is not null then
    perform cron.unschedule('dedup-funnel-mv-refresh');
  end if;
exception when others then
  raise notice 'cron.unschedule skipped: %', sqlerrm;
end $$;

-- ---------------------------------------------------------------------------
-- 2. Capture the ONLY content a rollback could not reproduce.
--
--    All three matviews are refreshable — their source tables all still exist, so the
--    "sources were dropped in the cutoff" hypothesis is REFUTED. But that inverts the
--    backup argument rather than satisfying it: dedup_funnel_resolutions_mv's definition
--    is 30-day-windowed over dedup_pair_audit, whose last write was 2026-08-06. A REFRESH
--    today returns a DIFFERENT, emptier answer, and after 2026-09-05 it returns zero rows.
--    The 35 stored rows (16,483 cutoff-era pairs) are a genuine historical record that
--    pg_cron — not this migration — was about to destroy within ~11 days.
--
--    A pg_dump could not have saved them either: `pg_dump --table <matview>` emits DDL
--    with NO DATA. The existing backup script structurally cannot capture what is at risk.
--
--    dedup_llm_cost_by_category_mv is already 0 rows; property_sources_mv is a frozen
--    2026-08-05 snapshot missing ~69,800 properties and fully derivable from listings.
--    Neither is archived.
-- ---------------------------------------------------------------------------
create table dedup_funnel_resolutions_archive as
  select * from dedup_funnel_resolutions_mv;

-- The revoke is MANDATORY, not hygiene. pg_default_acl grants `authenticated=r` on EVERY
-- table postgres creates in public — and the supabase_admin default grants anon full DML.
-- Without it, a teardown PR would hand every logged-in browser session admin-only dedup
-- funnel data. No existing test would have caught that:
-- test_no_ungated_relation_reads_admin_only_data scans views and functions only,
-- _ADMIN_GATED_MATVIEWS covers matviews, and anon is absent from the postgres default ACL
-- so test_anon_holds_no_relation_grants stays green.
revoke all on dedup_funnel_resolutions_archive from public, anon, authenticated;

-- Deny-by-default, matching the posture every admin-only table in this build gets. RLS with
-- ZERO policies means even a future accidental GRANT reads nothing. Required by
-- tests/test_migration_rls_grants.py::test_new_base_tables_enable_rls, which caught this
-- table on the first run.
alter table dedup_funnel_resolutions_archive enable row level security;

comment on table dedup_funnel_resolutions_archive is
  'Frozen copy of dedup_funnel_resolutions_mv, captured by migration 432 immediately before '
  'the NEW DEDUP teardown drop. Funnel outcomes for the 30 days ending 2026-08-06, when '
  'dedup_pair_audit stopped being written. A REFRESH could never reproduce it: the source '
  'definition is 30-day-windowed and that window empties 2026-09-05. Read-only historical '
  'record, admin-only; nothing writes it.';

-- ---------------------------------------------------------------------------
-- 3. Drop. Dependents before sources; `if exists` so the CI replay is order-independent.
--    Verified via pg_depend/pg_rewrite: the ONLY dependents are the two _public views on
--    their matviews. property_sources_mv has no external dependents at all, so no CASCADE.
--
--    Note the real names: dedup_funnel_resolutions_public and
--    dedup_llm_cost_by_category_public — NEITHER carries an `_mv` infix, which two of the
--    five names in the design proposal did.
-- ---------------------------------------------------------------------------
drop view if exists public.dedup_funnel_resolutions_public;
drop view if exists public.dedup_llm_cost_by_category_public;

drop materialized view if exists public.dedup_funnel_resolutions_mv;
drop materialized view if exists public.dedup_llm_cost_by_category_mv;

drop materialized view if exists public.property_sources_mv;

commit;
