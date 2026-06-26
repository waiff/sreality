-- Wave 5 Stage 3: N:N plan gate.
-- A listing can carry SEVERAL floor / site plans (a multi-unit building or multi-floor home
-- shows more than one). The gate already sends every plan of both listings in one vision
-- call, but the prompts read as if comparing a single pair, so a listing whose matching plan
-- sits among several could be wrongly dismissed. These updated prompts make the matching
-- explicit: compare EVERY plan of A against EVERY plan of B (N×N) and dismiss ONLY when NO
-- pair matches. No schema / cost change — same single call, the model now reasons over the
-- cross-product (the payload also labels each plan: "Listing A plan 2", …).
--
-- Guarded on updated_by so an operator-customised prompt (edited via Settings → updated_by
-- = 'settings_ui') is never clobbered; the app_settings_history trigger keeps the prior text.

update app_settings
set value = to_jsonb($PROMPT$You compare the FLOOR PLANS (půdorys) of two Czech real-estate listings to decide whether they show the SAME apartment unit or DIFFERENT units — typically within one development where units share renders and fit-out, so the floor plan is the disambiguator.

Each listing may carry SEVERAL floor plans (a multi-unit building or a multi-floor home often shows more than one). You are given Listing A's plan(s) first, then Listing B's, each labelled "Listing A plan k" / "Listing B plan k". Treat this as an N×N comparison: compare every plan of A against every plan of B.

For each candidate pair, compare in this order:
1. LAYOUT: the wall arrangement, the number and relative positions of rooms, the overall outline/shape. A genuinely different arrangement (different room count, mirrored or rotated is NOT the same, different connectivity) => different units.
2. LABELS (read any text on the plans — OCR): unit/apartment number (byt č.), floor (podlaží / NP / patro), total area (m²) and per-room areas, and balcony/terrace/loggia presence. A contradicting unit number, floor, or total area => different units even if the layout looks similar (developments stamp the same template per floor).
Use the labels ONLY to compare the plans against each other — never to assert a fact about a listing.

Return exactly one call to record_floor_plan_match with the verdict over ALL pairs:
- verdict = same_layout when AT LEAST ONE plan of A matches AT LEAST ONE plan of B (matching wall arrangement AND room positions AND no contradicting label). One matching pair is enough — the listings share a unit.
- verdict = different_layout ONLY when NO plan of A matches ANY plan of B (every comparable pair differs in arrangement / room-count / positions, OR has a contradicting unit-number / floor / total-area label).
- verdict = inconclusive when the plans are illegible, too low-resolution, or there is not enough to decide.
In the rationale, name the matching pair (e.g. "A plan 2 matches B plan 1") or, for different_layout, state that no pair matched and cite a concrete difference. Be conservative: only say different_layout when you can point to concrete differences across all pairs. Also fill plan_a and plan_b from the matched (or most representative) plans; leave a field out if not legible.$PROMPT$::text),
    updated_at = now(),
    updated_by = 'migration_243'
where key = 'llm_floor_plan_match_prompt'
  and updated_by = 'migration_234';

update app_settings
set value = to_jsonb($PROMPT$You are given site / situation plans from TWO real-estate listings in what may be the SAME development project. Each set may contain SEVERAL plans (a masterplan, a plot map, or a unit highlighted within a building/block layout), labelled "Listing A plan k" / "Listing B plan k".

Your job: decide whether the two listings point to the SAME specific unit, or to DIFFERENT units within the same development. Treat this as an N×N comparison: identify which unit each listing highlights across its own plans, then compare A against B.

Determine which unit each listing highlights — look for a coloured/outlined plot, an arrow or marker, a "Prodáno/Volné/Rezervováno" status label, a plot/building/unit number or letter (e.g. "Pozemek 3", "Budova A", "Byt 12"), or a single apartment outlined on a floor among several.

Call record_site_plan_match exactly once with the verdict over ALL pairs:
- "same_unit": at least one plan of A and one plan of B clearly highlight the SAME unit (same number/letter/position).
- "different_unit": the listings highlight DIFFERENT units of the same development and NO pair shares a unit (e.g. plot 3 vs plot 4, building A vs B, a different apartment outlined). This is a strong signal they are NOT the same property.
- "inconclusive": you cannot tell which unit is highlighted, the plans are unrelated, or there isn't enough detail.
- rationale: 1-3 sentences citing the specific evidence (the number/letter/position you read) and which plans you matched.$PROMPT$::text),
    updated_at = now(),
    updated_by = 'migration_243'
where key = 'llm_site_plan_match_prompt'
  and updated_by = 'migration_171';
