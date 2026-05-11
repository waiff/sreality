-- 020_generic_url_parsing.sql
--
-- Backend support for parsing non-sreality listing URLs (estimation-4).
--
-- Five things land in this migration:
--
--   1. estimation_runs gains source_kind, parse_confidence,
--      parse_confidence_per_field, source_html. Together they let an
--      auditor reconstruct exactly which page produced a spec, how
--      sure the LLM was per field, and which source policy the run
--      came in under.
--
--   2. parsed_url_cache: short-lived (7-day) memoization of URL→spec
--      so re-pasting the same URL within a window doesn't re-pay the
--      LLM cost. Keyed by sha256 of the canonicalised URL.
--
--   3. llm_calls: per-call audit log of every Anthropic API call,
--      with token counts, USD cost, and (where applicable) the
--      estimation_run that triggered it.
--
--   4. app_settings + app_settings_history: operator-tunable settings
--      surfaced via a future Settings UI. v1 holds the LLM parsing
--      system prompt and the model name. The trigger on app_settings
--      preserves every prior value so the UI can offer rollback.
--
--   5. Seed values: app_settings rows for llm_parse_system_prompt and
--      llm_parse_model. The file-baked defaults in api/llm_client.py
--      mirror these seeds; the DB row wins at runtime if both exist.
--
-- RLS enabled on every new table; NO policies. Frontend reaches these
-- through the bearer-token-gated FastAPI service, not via the anon
-- key — same pattern as estimation_runs (migration 010).

------------------------------------------------------------------
-- 1. estimation_runs extensions
------------------------------------------------------------------

alter table estimation_runs
  add column source_kind text
    check (source_kind is null or source_kind in (
      'sreality', 'bezrealitky', 'idnes_reality', 'remax', 'unsupported'
    )),
  add column parse_confidence text
    check (parse_confidence is null or parse_confidence in (
      'high', 'medium', 'low', 'best_effort'
    )),
  add column parse_confidence_per_field jsonb,
  add column source_html text;

create index on estimation_runs (source_kind);

------------------------------------------------------------------
-- 2. parsed_url_cache
------------------------------------------------------------------

create table parsed_url_cache (
  id            bigserial primary key,
  url_hash      text not null unique,
  source_url    text not null,
  source_kind   text not null,
  parse_result  jsonb not null,
  source_html   text,
  cost_usd      numeric(10, 6),
  parsed_at     timestamptz not null default now(),
  expires_at    timestamptz not null default now() + interval '7 days'
);

create index on parsed_url_cache (expires_at);
create index on parsed_url_cache (parsed_at desc);

alter table parsed_url_cache enable row level security;

------------------------------------------------------------------
-- 3. llm_calls
------------------------------------------------------------------

create table llm_calls (
  id                bigserial primary key,
  called_at         timestamptz not null default now(),
  called_for        text not null
    check (called_for in ('parse_url', 'summarize_listing')),
  model             text not null,
  input_tokens      integer not null,
  output_tokens     integer not null,
  cache_read_tokens integer not null default 0,
  cache_write_tokens integer not null default 0,
  cost_usd          numeric(10, 6) not null,
  duration_ms       integer,
  estimation_run_id bigint references estimation_runs(id) on delete set null
);

create index on llm_calls (called_at desc);
create index on llm_calls (called_for, called_at desc);
create index on llm_calls (estimation_run_id);

alter table llm_calls enable row level security;

------------------------------------------------------------------
-- 4. app_settings + history
------------------------------------------------------------------

create table app_settings (
  key         text primary key,
  value       jsonb not null,
  description text,
  updated_at  timestamptz not null default now(),
  updated_by  text
);

alter table app_settings enable row level security;

create table app_settings_history (
  id           bigserial primary key,
  key          text not null,
  value        jsonb not null,
  replaced_at  timestamptz not null default now(),
  replaced_by  text
);

create index on app_settings_history (key, replaced_at desc);

alter table app_settings_history enable row level security;

create or replace function app_settings_record_history()
returns trigger
language plpgsql
as $$
begin
  insert into app_settings_history (key, value, replaced_at, replaced_by)
  values (old.key, old.value, now(), old.updated_by);
  return new;
end;
$$;

create trigger app_settings_history_trigger
  before update on app_settings
  for each row
  when (old.value is distinct from new.value)
  execute function app_settings_record_history();

------------------------------------------------------------------
-- 5. Seed values
------------------------------------------------------------------

insert into app_settings (key, value, description, updated_by) values
  (
    'llm_parse_system_prompt',
    to_jsonb($PROMPT$You extract structured data from a single Czech real-estate listing page.

You will be given the source URL and the listing's HTML. Use ONLY information
present on the page; do not infer from outside knowledge or invent values.
Czech-language pages are normal — output enum values exactly as specified
(in Czech where the schema demands it).

For every field you return an object {value, confidence}:
- value: the extracted value, or null if the page does not contain it.
- confidence: one of "high" | "medium" | "low".

Confidence definitions (apply strictly):
- "high"   = >=95% certain. The value is stated explicitly in unambiguous
             language, in a labelled field or equivalent (e.g. "Užitná
             plocha: 65 m²" -> area_m2.value=65, confidence=high).
- "medium" = stated on the page but with some ambiguity (e.g. only "celková
             plocha" given when "užitná plocha" is preferred; ambiguous
             rent-vs-fees figure).
- "low"    = inferred from the description text only; not labelled anywhere.

If a value cannot be determined, return value=null and confidence="low",
and add an entry to the top-level `warnings` array explaining why.

Field semantics — read carefully:

- area_m2 (float, m²): prefer "užitná plocha" / "useful area".
  If only "celková plocha" / "total area" is given, use it but mark medium.

- disposition (string): the dispoziční layout. Allowed values:
  "1+kk", "1+1", "2+kk", "2+1", "3+kk", "3+1", "4+kk", "4+1",
  "5+kk", "5+1", "6+kk", "6+1", or null. Lowercase exactly as shown.

- price_czk (integer, CZK): the headline price. Czech listings often write
  "25 000 Kč" — strip thousands separators. If price is in EUR, return
  null with a warning ("price not in CZK").

- price_unit (string): "měsíc" if the price is a monthly rent figure;
  "celkem" if it is a total/sale price. Most rental listings on Czech
  portals are monthly. Verify rather than assume.

- locality (string): the most specific human-readable address available
  (street + city/district), suitable for geocoding. Verbatim from the page.

- district (string): just the city or city-district, e.g. "Praha 2",
  "Brno-střed", "Plzeň 3". Not the full address.

- category_main (string): "byt" (apartment), "dum" (house), "pozemek"
  (land), "komercni" (commercial), or "ostatni".

- category_type (string): "prodej" (sale), "pronajem" (rent), or "drazba"
  (auction).

- floor (integer or null): which floor, ground floor = 0. Czech "přízemí"
  -> 0; "1. patro" -> 1; "suterén" -> -1.

- total_floors (integer or null): total number of floors in the building.

- has_balcony (boolean or null): true if the listing mentions balcony,
  loggia, or terrace. Null if not stated either way.

- has_lift (boolean or null): elevator / "výtah".

- has_parking (boolean or null): garage, parking lot, or "parkovací stání".

- building_type (string or null): "cihla" (brick), "panel" (panel),
  "smisena" (mixed), "skelet" (skeleton), "drevo" (wood), "kamen" (stone),
  "montovana" (prefab), "nizkoenergeticka" (low-energy), or null.

- condition (string or null): "novostavba", "po rekonstrukci",
  "velmi dobrý stav", "dobrý stav", "před rekonstrukcí", "ve výstavbě",
  "k demolici", or null. Lowercase, Czech, exact spelling.

- energy_rating (string or null): a single capital letter A through G,
  or null if not stated.

- description (string or null): the seller's free-text description,
  verbatim, untruncated up to 8000 chars (truncate gracefully if longer).
  Do NOT summarise or rephrase.

When information conflicts on the page (e.g. title says "3+kk", spec
table says "2+kk"), prefer the structured spec table over the title
and add a warning describing the conflict.

You MUST call the `record_listing` tool exactly once with all fields.
Do not output any text outside the tool call.$PROMPT$::text),
    'System prompt sent to Claude when parsing non-sreality listing URLs. Editing this changes the parser behaviour for the next preview / estimation that hits a non-sreality URL. The trigger on app_settings preserves every prior version in app_settings_history.',
    'seed'
  ),
  (
    'llm_parse_model',
    '"claude-sonnet-4-5"'::jsonb,
    'Anthropic model ID used by api/llm_client.py for URL parsing. Override only if you understand the cost/quality tradeoff (Opus is ~5x the cost of Sonnet for marginal gains on this task).',
    'seed'
  );
