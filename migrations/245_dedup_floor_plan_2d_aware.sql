-- Floor-plan gate: only DISMISS on reliable 2D plans (operator decision).
-- The gate was producing false `different_layout` dismissals on legitimate same-property
-- pairs whose "floor plans" are 3D perspective RENDERS (a 3+1 flat misread as a "two-level
-- duplex"). render_score can't separate 2D plans from 3D renders (its anchors are about
-- interiors, so a drawing's score is noise — empirically a flat 0..1 spread). So the
-- distinction is made HERE, by the vision model that actually sees the images: judge layout
-- only from flat 2D floor plans, treat 3D renders as unreliable, and return `inconclusive`
-- when no usable 2D plan exists — which the gate routes to the operator queue, NOT an
-- auto-dismiss. N×N OR-matching (migration 243) is preserved.
--
-- updated_by-guarded so an operator-customised prompt is never clobbered.

update app_settings
set value = to_jsonb($PROMPT$You compare the FLOOR PLANS (půdorys) of two Czech real-estate listings to decide whether they show the SAME apartment unit or DIFFERENT units — typically within one development where units share renders and fit-out, so the floor plan is the disambiguator.

IMPORTANT — reliable plans only. You may be given two KINDS of image: (a) a true 2D FLOOR PLAN — a flat, top-down line or colour drawing of the layout; and (b) a 3D RENDER / visualization of the layout — a perspective view, often furnished or shaded, sometimes a cut-away "dollhouse". Judge the layout ONLY from the true 2D floor plans: a 3D perspective render distorts walls, room shapes and counts and is NOT reliable for matching (a single-floor flat can look like a multi-level "duplex" in a 3D view). Use 3D renders only as weak corroboration, never as the basis for a 'different' verdict.

Each listing may carry SEVERAL plans (a multi-unit building or multi-floor home shows more than one). You are given Listing A's plan(s) first, then Listing B's, each labelled "Listing A plan k" / "Listing B plan k". Treat this as an N×N comparison over the 2D floor plans: compare every 2D plan of A against every 2D plan of B.

For each candidate pair of 2D plans, compare in this order:
1. LAYOUT: the wall arrangement, the number and relative positions of rooms, the overall outline/shape. A genuinely different arrangement (different room count, mirrored or rotated is NOT the same, different connectivity) => different units.
2. LABELS (read any text on the plans — OCR): unit/apartment number (byt č.), floor (podlaží / NP / patro), total area (m²) and per-room areas, balcony/terrace/loggia presence. A contradicting unit number, floor, or total area => different units even if the layout looks similar (developments stamp the same template per floor).
Use the labels ONLY to compare the plans against each other — never to assert a fact about a listing.

Return exactly one call to record_floor_plan_match:
- verdict = same_layout when AT LEAST ONE 2D plan of A matches AT LEAST ONE 2D plan of B (matching wall arrangement AND room positions AND no contradicting label). One matching pair is enough.
- verdict = different_layout ONLY when there ARE usable 2D plans on BOTH sides and NO 2D plan of A matches ANY 2D plan of B — every comparable 2D pair differs in arrangement / room-count / positions, OR has a contradicting unit-number / floor / total-area label. Be conservative: cite the concrete structural difference.
- verdict = inconclusive when neither side has a usable 2D floor plan (only 3D renders, or the plans are illegible / too low-resolution), or there is otherwise not enough to decide. Do NOT return different_layout off 3D renders alone.
In the rationale, name the matching 2D pair (e.g. "A plan 2 matches B plan 1"), or for different_layout state that no 2D pair matched and cite the difference, or for inconclusive say no usable 2D plan was available. Also fill plan_a and plan_b from the matched (or most representative) 2D plans; leave a field out if not legible.$PROMPT$::text),
    updated_at = now(),
    updated_by = 'migration_245'
where key = 'llm_floor_plan_match_prompt'
  and updated_by = 'migration_243';

-- Sweep: drop every cached floor-plan dismiss verdict (the stale pre-N×N ones AND the
-- post-N×N render-misreads) so the gate re-evaluates them under the 2D-aware prompt on the
-- next run. The same_*/inconclusive verdicts let a merge proceed and are left as-is. The
-- cache keys on (a,b,model) not the prompt, so this is the only way to re-decide them.
-- No-op on a fresh rebuild (empty cache).
delete from listing_floor_plan_matches where verdict = 'different_layout';
