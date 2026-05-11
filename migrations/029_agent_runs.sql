-- 029_agent_runs.sql
--
-- Phase 7 slice 1: the reasoning agent. Four things land here:
--
--   1. llm_calls.called_for: add 'agent_estimation' so the agent's
--      per-turn LLM calls can be attributed and aggregated separately
--      from URL parsing / summarization / vision.
--
--   2. llm_calls.provider: new column distinguishing 'anthropic' from
--      'gemini' (and future providers). Existing rows backfill to
--      'anthropic' since that was the only provider used to date.
--
--   3. skills + skills_history + trigger: operator-tunable agent
--      configuration. Each skill is a bundle of (system prompt +
--      allowed tools + per-provider preferred model + loop limits).
--      Mirrors migration 020's app_settings / app_settings_history
--      pattern — the trigger preserves every prior version so the
--      Settings UI can offer rollback.
--
--   4. Seed: the slice-1 skill `rental_estimator_v1`. The repo's
--      skills/rental_estimator_v1/SKILL.md file is the canonical
--      version of this content; the migration imports it once. After
--      deploy, the DB row is the source of truth; edit via the
--      Settings UI.
--
-- RLS enabled on the new tables; no policies. The skill / app_settings
-- editor surfaces on the Settings page reach these through the
-- FastAPI /admin/* routes, which are exempted from the API_TOKEN
-- bearer gate per the slice-1 design (private Railway URL is the
-- security perimeter; same exemption category as /health).

------------------------------------------------------------------
-- 1. llm_calls.called_for: add 'agent_estimation'
------------------------------------------------------------------

alter table llm_calls
  drop constraint llm_calls_called_for_check,
  add constraint llm_calls_called_for_check
    check (called_for in (
      'parse_url', 'summarize_listing', 'compare_listing_images',
      'agent_estimation'
    ));

------------------------------------------------------------------
-- 2. llm_calls.provider
------------------------------------------------------------------

alter table llm_calls
  add column provider text not null default 'anthropic'
    check (provider in ('anthropic', 'gemini'));

create index on llm_calls (provider, called_at desc);

------------------------------------------------------------------
-- 3. skills + skills_history + trigger
------------------------------------------------------------------

create table skills (
  name            text primary key,
  description     text not null,
  system_prompt   text not null,
  allowed_tools   jsonb not null,
  preferred_model jsonb not null,
  limits          jsonb not null,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  updated_by      text
);

alter table skills enable row level security;

create table skills_history (
  id              bigserial primary key,
  name            text not null,
  description     text not null,
  system_prompt   text not null,
  allowed_tools   jsonb not null,
  preferred_model jsonb not null,
  limits          jsonb not null,
  replaced_at     timestamptz not null default now(),
  replaced_by     text
);

create index on skills_history (name, replaced_at desc);

alter table skills_history enable row level security;

create or replace function skills_record_history()
returns trigger
language plpgsql
as $$
begin
  insert into skills_history (
    name, description, system_prompt, allowed_tools,
    preferred_model, limits, replaced_at, replaced_by
  )
  values (
    old.name, old.description, old.system_prompt, old.allowed_tools,
    old.preferred_model, old.limits, now(), old.updated_by
  );
  new.updated_at := now();
  return new;
end;
$$;

create trigger skills_history_trigger
  before update on skills
  for each row
  when (
    old.description     is distinct from new.description
    or old.system_prompt   is distinct from new.system_prompt
    or old.allowed_tools   is distinct from new.allowed_tools
    or old.preferred_model is distinct from new.preferred_model
    or old.limits          is distinct from new.limits
  )
  execute function skills_record_history();

------------------------------------------------------------------
-- 4. Seed: rental_estimator_v1
------------------------------------------------------------------

insert into skills (
  name, description, system_prompt,
  allowed_tools, preferred_model, limits, updated_by
) values (
  'rental_estimator_v1',
  'Czech apartment rental estimator (slice 1). Defaults to byt / pronajem.',
  $PROMPT$You are a Czech real estate rental analyst. Your job is to produce a
defensible monthly rental estimate (in CZK) for a target apartment, with a
distribution (p25 / median / p75), a sample size, and a confidence label.

You operate by calling tools. You see structured tool results and reason
between calls. Every claim you make in the final estimate must be grounded
in tool output you actually saw.

Operating principles (apply strictly):

1. START BROAD. Your first tool call is almost always `find_comparables_relaxed`
   with the target's lat/lng, area, disposition, and a sensible radius (1000m
   in Prague, 1500m in regional cities). The "relaxed" variant automatically
   widens area_band_pct / disposition_match until it has enough comparables,
   so you don't need to guess thresholds.

2. ANALYZE THE DISTRIBUTION. Once you have a cohort, call `analyze_distribution`
   on the `listings` array with `field="price_per_m2"`. Look at p25 / median /
   p75 / iqr, not just the mean. The target's monthly rent estimate is the
   median price-per-m2 times the target's area.

3. NEVER QUOTE A POINT WITHOUT A RANGE. Your final estimate always includes
   p25 and p75. A median without a range is a lie about the data's spread.

4. CROSS-CHECK THE TAILS. After analyze_distribution, call
   `find_distribution_outliers` with the same cohort. If the outliers
   include strong upward pulls (luxury / furnished / short-term), consider
   whether they should be in your final cohort or be set aside.

5. SANITY-CHECK THE AREA. Call `describe_neighborhood` with the target's
   lat/lng + the same radius. Compare its median price-per-m2 to your cohort
   median. If they diverge by more than ~15%, mention this in your warnings.

6. VERIFY A SUSPICIOUS COMPARABLE. If any comparable looks anomalous in a way
   that materially moves the estimate (e.g., a single listing pulls p75 up
   significantly), call `verify_listing_freshness` on it before relying on
   it. Stale listings get filtered out automatically; you only need this
   tool when you doubt a *specific* row.

7. WRITE 1-2 SENTENCES OF REASONING BEFORE EVERY TOOL CALL. Plain text
   before the tool block: what you're about to do and why. This text is
   captured into the trace and is the audit trail.

8. STOP WITH `record_estimate`. When your cohort is solid and your range is
   defensible, call `record_estimate` exactly once with:
   - estimated_monthly_rent_czk (your point estimate; median * area)
   - rent_p25_czk, rent_p75_czk (the IQR-derived range)
   - confidence: one of "high" | "medium" | "low" based on sample size and
     spread (high = n>=20 and iqr/median < 0.25; low = n<10 or iqr/median > 0.5;
     medium otherwise)
   - comparables_used: list of sreality_id from the cohort you actually
     based the estimate on (typically the relaxed find returned)
   - warnings: any concerns (small sample, spread too wide, neighbourhood
     mismatch, suspicious outliers you didn't exclude, etc.)

   The estimate fields are CZK monthly rent figures. Round to the nearest 100.

9. ONE record_estimate ENDS THE RUN. Do not call any more tools after it. The
   harness exits immediately on `record_estimate`.

You will be given the target spec (lat, lng, area_m2, disposition, optional
floor) and the user-supplied filter overrides (radius, max_age_days, etc.)
in the first user message. Czech text is normal — the listings are Czech;
your reasoning and warnings can be in English.$PROMPT$,
  '[
    "find_comparables_relaxed",
    "analyze_distribution",
    "find_distribution_outliers",
    "describe_neighborhood",
    "verify_listing_freshness",
    "record_estimate"
  ]'::jsonb,
  '{
    "anthropic": "claude-sonnet-4-5",
    "gemini": "gemini-2.5-pro"
  }'::jsonb,
  '{
    "max_iterations": 12,
    "max_cost_usd": 1.00,
    "wall_clock_timeout_s": 120
  }'::jsonb,
  'seed'
)
on conflict (name) do nothing;
