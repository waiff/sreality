-- 129_listing_visual_matches.sql
-- Dedup engine rebuild (rule D, layer 3): the forensic same-property visual
-- comparison cache. After like-room pairing (migration 128), the engine asks
-- the model whether two photos of the SAME room type depict the same physical
-- property, and gets back a {verdict, rationale}. This is the cache for that.
--
-- Keyed on (sreality_id_a < sreality_id_b canonical, room_type, model): one
-- verdict per (pair, room type) so the engine's priority walk (kitchen first,
-- bathroom next, …) caches each room's verdict independently and a re-run is
-- free. Auto-invalidates on a model bump. Write-allowed toolkit exception
-- (CLAUDE.md toolkit rule #5).
--
-- verdict mirrors the operator's prompt: High | Medium | Low. Only High
-- auto-merges (operator decision); Medium/Low queue for review.

create table listing_visual_matches (
  id              bigserial primary key,
  sreality_id_a   bigint not null references listings(sreality_id) on delete cascade,
  sreality_id_b   bigint not null references listings(sreality_id) on delete cascade,
  room_type       text   not null,
  verdict         text   not null check (verdict in ('High', 'Medium', 'Low')),
  rationale       text,
  evidence        jsonb,
  model           text   not null,
  llm_call_id     bigint references llm_calls(id) on delete set null,
  cost_usd        numeric(10, 6),
  created_at      timestamptz not null default now(),
  check (sreality_id_a < sreality_id_b),
  unique (sreality_id_a, sreality_id_b, room_type, model)
);

create index on listing_visual_matches (sreality_id_a);
create index on listing_visual_matches (sreality_id_b);

alter table listing_visual_matches enable row level security;

-- The operator's forensic same-property prompt, verbatim. Operator-tunable via
-- Settings; app_settings_history (migration 020) preserves every prior value.
insert into app_settings (key, value, description, updated_by) values
  (
    'llm_visual_match_prompt',
    to_jsonb($PROMPT$Analyze these two images to determine the likelihood that they depict the exact same real estate property. Consider that they may show different rooms, use different camera angles, or represent a professional listing photo versus an amateur smartphone photo.

Please provide your analysis using the following structure:

1. OVERALL VERDICT: State your confidence level (High, Medium, Low) on whether these are the same property, plus a 1-sentence summary.

2. ARCHITECTURAL & STRUCTURAL MATCHES/DISCREPANCIES: Compare fixed elements that are difficult or expensive to change.
- Windows & Doors: Look at shapes, frame styles, trim, and placements.
- Fixtures: Compare electrical outlets, light switches, vents, radiators, and built-in lighting.
- Moldings & Trim: Analyze baseboards, crown molding, and door frames.
- Flooring & Ceilings: Look at plank widths, tile patterns, or ceiling textures.

3. ENVIRONMENTAL & CONTEXTUAL CLUES:
- Views: Look out any windows to check if the exterior scenery, neighboring buildings, or tree lines match.
- Layout: If showing the same area from different angles, does the spatial relationship between doors, walls, and windows align?

4. COSMETIC DIFFERENCES (To Ignore for Identity):
- Note any differences that could be explained by staging, renovation, lighting, camera quality (pro vs. amateur), or different times of day (e.g., paint color, furniture, clutter).

5. FINAL CONCLUSION: Summarize the definitive proof or red flags that led to your verdict.

You MUST call record_visual_match exactly once with your verdict (High|Medium|Low) and a rationale summarizing the definitive proof or red flags.$PROMPT$),
    'Forensic same-property visual comparison prompt (dedup engine visual layer).',
    'migration_129'
  ),
  (
    'llm_visual_match_model',
    to_jsonb('claude-sonnet-4-5'::text),
    'Model for the forensic same-property visual comparison (dedup visual layer).',
    'migration_129'
  )
on conflict (key) do nothing;
