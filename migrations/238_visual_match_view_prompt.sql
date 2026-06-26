-- 238_visual_match_view_prompt.sql
-- Sharpen the forensic same-property prompt's VIEW comparison (operator request).
--
-- A reported false-merge scored High on balcony/terrace photos whose VIEWS were
-- actually different (one looked right over garages, the other left over rooftops).
-- The old prompt's "Views" clue was a single weak line. This rewrites it to demand a
-- rigorous, direction- and height-aware view comparison: same-direction only, estimate
-- the floor, trace the view line, and distinguish foreground vs background structure
-- types — a different orientation/height/structure set is a strong red flag even when
-- the interiors look alike (a development reuses fit-out across units facing different
-- ways). Helps non-byt exterior comparison AND byt interior rooms with window views.
-- Operator-tunable: this UPDATE is the new baseline; /settings edits still override.
update app_settings
set value = to_jsonb($prompt$Analyze these two images to determine the likelihood that they depict the exact same real estate property. Consider that they may show different rooms, use different camera angles, or represent a professional listing photo versus an amateur smartphone photo.

Please provide your analysis using the following structure:

1. OVERALL VERDICT: State your confidence level (High, Medium, Low) on whether these are the same property, plus a 1-sentence summary.

2. ARCHITECTURAL & STRUCTURAL MATCHES/DISCREPANCIES: Compare fixed elements that are difficult or expensive to change.
- Windows & Doors: Look at shapes, frame styles, trim, and placements.
- Fixtures: Compare electrical outlets, light switches, vents, radiators, and built-in lighting.
- Moldings & Trim: Analyze baseboards, crown molding, and door frames.
- Flooring & Ceilings: Look at plank widths, tile patterns, or ceiling textures.

3. ENVIRONMENTAL & CONTEXTUAL CLUES:
- Views (compare RIGOROUSLY — a view is one of the hardest things to fake): when either image looks out of a window, balcony, terrace, or shows the building's exterior surroundings:
  * Compare the SAME direction only — a right-facing view must be matched to a right-facing view and a left-facing view to a left-facing view; a view facing a different direction is a different vantage point, NOT a match.
  * Estimate the camera FLOOR / height from the horizon line and how steeply the scene drops away; two views from clearly different heights are different units.
  * Trace the view LINE and the specific elements along it — which structures, in what order, at what distance.
  * Distinguish the TYPES of structures and scenery in the FOREGROUND vs the BACKGROUND (e.g. low garages / parking / courtyards vs tall apartment blocks vs rooftops vs trees / open landscape).
  A genuinely different orientation, height, or set of foreground/background structures is a strong red flag (Low) even when the room interiors look alike — one development reuses identical fit-out across units that face different ways and sit on different floors.
- Layout: If showing the same area from different angles, does the spatial relationship between doors, walls, and windows align?

4. COSMETIC DIFFERENCES (To Ignore for Identity):
- Note any differences that could be explained by staging, renovation, lighting, camera quality (pro vs. amateur), or different times of day (e.g., paint color, furniture, clutter).

5. FINAL CONCLUSION: Summarize the definitive proof or red flags that led to your verdict.

You MUST call record_visual_match exactly once with your verdict (High|Medium|Low) and a rationale summarizing the definitive proof or red flags.$prompt$::text),
    updated_by = 'migration_238'
where key = 'llm_visual_match_prompt';
