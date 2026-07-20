-- 331_anon_drift_and_matview_acl_hardening.sql
--
-- Closes two grant-drift holes found by the post-ship review of 316-319. Full
-- evidence: docs/design/public-release-remediation-2026-07.md § R2.
--
-- PART A — anon drift. Migration 299 revoked anon to ~nothing in one sweep, but a
-- one-time sweep cannot cover grants added LATER: migrations 303/308/309/310/311/315
-- each shipped `grant select ... to anon, authenticated` on a new view. Live audit
-- found exactly seven such views still readable by anon. Five embed
-- `is_platform_admin()` (migration 318), whose EXECUTE anon does NOT hold (revoked
-- in 287) — for those anon gets a raw `permission denied for function
-- is_platform_admin` rather than a clean deny, leaking implementation detail; and
-- if that EXECUTE were ever re-granted they would become fully public with no
-- further change. The other two are worse: they are ungated, so anon reads real
-- rows today — `listing_natural_key_public` is an unfiltered
-- (sreality_id, source, source_id_native) dump of every listing, and
-- `property_estimates_public` exposes which properties carry estimates.
--
-- The settled posture (Phase 0, operator decision 2026-07-13) is that the SPA is
-- fully login-gated and anon reads NOTHING. All seven are read by the SPA as
-- `authenticated` behind RequireAuth (grep-verified), so revoking anon changes no
-- application behaviour.
--
-- PART B — materialized-view ACL drift. Migration 299's authenticated-write revoke
-- loop scoped itself to `relkind in ('r','p','v')`, so materialized views ('m') were
-- skipped entirely and kept their pre-299 default ACLs. Live audit: 12 of 13 public
-- matviews still grant `authenticated` the full DELETE/INSERT/UPDATE/TRUNCATE/
-- REFERENCES/TRIGGER set (plus MAINTAIN on PG17, which permits REFRESH).
--
-- Three of them back migration 318's admin gate — `dedup_funnel_resolutions_mv`,
-- `dedup_llm_cost_by_category_mv`, `images_failure_overview_mv`. A matview cannot
-- carry RLS and cannot embed the gate, so `authenticated` holding SELECT on the raw
-- matview bypasses that gate completely: a non-admin can read exactly what the
-- gated wrapper is there to hide. Revoking is safe because every legitimate reader
-- reaches them through an owner-rights view or a SECURITY DEFINER function
-- (`images_failure_overview()`), which retain access via the owner, and no frontend
-- or backend code reads these three matviews by name (grep-verified).
--
-- SELECT is deliberately PRESERVED on the other ten. `properties_map_mv`,
-- `price_stat_choropleth` and `rent_map_choropleth` are read directly by the SPA;
-- the health/ops matviews are read through `health_summary()` /
-- `portal_health_summary()`, which are SECURITY INVOKER — revoking their SELECT
-- would break the Health dashboard. (Those two functions are themselves an ungated
-- admin-ops surface that migration 318's triage missed; that is a separate finding
-- with its own fix, deliberately not bundled here.)
--
-- Additive/permission-only: no table, column, view body, or policy changes.

begin;

-- PART A ---------------------------------------------------------------------
revoke select on
  public.dedup_vision_bakeoff_results_public,
  public.image_border_cases_public,
  public.image_tag_annotations_public,
  public.image_training_examples_public,
  public.phash_pair_notes_public,
  public.property_estimates_public,
  public.listing_natural_key_public
from anon;

-- PART B ---------------------------------------------------------------------
-- B1: the three matviews that back migration 318's gate go fully dark to both
-- browser roles.
revoke all on
  public.dedup_funnel_resolutions_mv,
  public.dedup_llm_cost_by_category_mv,
  public.images_failure_overview_mv
from anon, authenticated;

-- B2: strip write + REFRESH from every matview, closing migration 299's relkind
-- gap. SELECT is left untouched here (see the header for which readers depend on
-- it). MAINTAIN is PostgreSQL 17+; the CI schema replay runs 15, so name it only
-- where it exists or the replay fails on an unknown privilege.
do $$
declare
  r record;
  v_privs text := 'insert, update, delete, truncate, references, trigger';
begin
  if current_setting('server_version_num')::int >= 170000 then
    v_privs := v_privs || ', maintain';
  end if;
  for r in
    select c.relname
      from pg_class c
      join pg_namespace n on n.oid = c.relnamespace
     where n.nspname = 'public' and c.relkind = 'm'
     order by c.relname
  loop
    execute format('revoke %s on public.%I from anon, authenticated', v_privs, r.relname);
  end loop;
end $$;

-- Post-conditions -------------------------------------------------------------
do $$
declare
  v_anon text[];
  v_bypass text[];
  v_writable text[];
begin
  -- No relation of any kind may remain readable by anon.
  select coalesce(array_agg(distinct c.relname order by c.relname), '{}')
    into v_anon
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public'
     and c.relkind in ('r', 'v', 'm', 'p')
     and has_table_privilege('anon', c.oid, 'SELECT');
  if array_length(v_anon, 1) is not null then
    raise exception 'anon can still SELECT: %', v_anon;
  end if;

  -- The gate-backing matviews must be dark to authenticated.
  select coalesce(array_agg(x order by x), '{}') into v_bypass
    from unnest(array[
      'dedup_funnel_resolutions_mv',
      'dedup_llm_cost_by_category_mv',
      'images_failure_overview_mv'
    ]) as x
   where to_regclass('public.' || x) is not null
     and has_table_privilege('authenticated', ('public.' || x)::regclass, 'SELECT');
  if array_length(v_bypass, 1) is not null then
    raise exception 'matview(s) still bypass the admin gate for authenticated: %', v_bypass;
  end if;

  -- No matview may remain writable by a browser role.
  select coalesce(array_agg(distinct c.relname order by c.relname), '{}')
    into v_writable
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public'
     and c.relkind = 'm'
     and (has_table_privilege('authenticated', c.oid, 'INSERT')
       or has_table_privilege('authenticated', c.oid, 'UPDATE')
       or has_table_privilege('authenticated', c.oid, 'DELETE')
       or has_table_privilege('anon', c.oid, 'INSERT'));
  if array_length(v_writable, 1) is not null then
    raise exception 'matview(s) still writable by a browser role: %', v_writable;
  end if;
end $$;

commit;
