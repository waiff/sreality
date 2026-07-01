-- 260_dedup_floor_plan_no_2d_verdict.sql
-- Floor-plan gate becomes a CONTRADICTION VETO. It was queuing ~600 would-merge cross-portal
-- pairs it simply couldn't 2D-compare: the compare returned `inconclusive` for pairs whose
-- "floor plans" are 3D perspective RENDERS (migration 245 correctly refuses to judge layout from
-- 3D), and `dedup_floor_plan_inconclusive_to_review` (default on) routed that to the manual queue
-- — vetoing pHash>=2 / visual-High merges over an un-readable render. (Plus a separate one-sided
-- queue, handled in code.)
--
-- Root cause: `inconclusive` CONFLATED two very different situations. This splits them so the
-- gate can act correctly on each:
--   * no_2d_plan  — AT LEAST ONE side has no usable 2D plan (only 3D renders / illegible), so a
--                   2D-plan-to-2D-plan compare is impossible. The gate learned nothing -> the
--                   engine MERGES on the primary pHash/visual signal (handled in _floor_plan_gate).
--   * inconclusive — BOTH sides DO have usable 2D plans but the model still can't decide. A
--                    genuine human call -> stays in the manual queue (operator's carve-out).
-- different_layout (a proven 2D mismatch) still DISMISSES; that is the gate's only auto-dismiss.

-- 1) Allow the new verdict value.
alter table listing_floor_plan_matches
  drop constraint if exists listing_floor_plan_matches_verdict_check;
alter table listing_floor_plan_matches
  add constraint listing_floor_plan_matches_verdict_check
  check (verdict in ('same_layout', 'different_layout', 'inconclusive', 'no_2d_plan'));

-- 2) Teach the model the split. updated_by-guarded so an operator-customised prompt is never
--    clobbered (only touch the migration-seeded prompt).
update app_settings
set value = to_jsonb($fp$You compare the FLOOR PLANS (půdorys) of two Czech real-estate listings to decide whether they show the SAME apartment unit or DIFFERENT units — typically within one development where units share renders and fit-out, so the floor plan is the disambiguator.

IMPORTANT — reliable plans only. You may be given two KINDS of image: (a) a true 2D FLOOR PLAN — a flat, top-down line or colour drawing of the layout; and (b) a 3D RENDER / visualization of the layout — a perspective view, often furnished or shaded, sometimes a cut-away "dollhouse". Judge the layout ONLY from the true 2D floor plans: a 3D perspective render distorts walls, room shapes and counts and is NOT reliable for matching (a single-floor flat can look like a multi-level "duplex" in a 3D view). Use 3D renders only as weak corroboration, never as the basis for a 'different' verdict.

Each listing may carry SEVERAL plans (a multi-unit building or multi-floor home shows more than one). You are given Listing A's plan(s) first, then Listing B's, each labelled "Listing A plan k" / "Listing B plan k". Treat this as an N×N comparison over the 2D floor plans: compare every 2D plan of A against every 2D plan of B.

For each candidate pair of 2D plans, compare in this order:
1. LAYOUT: the wall arrangement, the number and relative positions of rooms, the overall outline/shape. A genuinely different arrangement (different room count, mirrored or rotated is NOT the same, different connectivity) => different units.
2. LABELS (read any text on the plans — OCR): unit/apartment number (byt č.), floor (podlaží / NP / patro), total area (m²) and per-room areas, balcony/terrace/loggia presence. A contradicting unit number, floor, or total area => different units even if the layout looks similar (developments stamp the same template per floor).
Use the labels ONLY to compare the plans against each other — never to assert a fact about a listing.

Return exactly one call to record_floor_plan_match:
- verdict = same_layout when AT LEAST ONE 2D plan of A matches AT LEAST ONE 2D plan of B (matching wall arrangement AND room positions AND no contradicting label). One matching pair is enough.
- verdict = different_layout ONLY when there ARE usable 2D plans on BOTH sides and NO 2D plan of A matches ANY 2D plan of B — every comparable 2D pair differs in arrangement / room-count / positions, OR has a contradicting unit-number / floor / total-area label. Be conservative: cite the concrete structural difference.
- verdict = no_2d_plan when AT LEAST ONE side has NO usable 2D floor plan — it shows only 3D perspective renders / visualizations, or its plan is illegible / too low-resolution — so a reliable 2D-plan-to-2D-plan comparison is impossible. This is the common case where both listings only carry 3D renders. Prefer this over inconclusive whenever a usable 2D plan is missing on either side; never return different_layout off 3D renders alone.
- verdict = inconclusive ONLY when BOTH sides DO have at least one usable 2D floor plan, yet you still cannot decide whether any pair matches (e.g. cropped / partial plans, conflicting or ambiguous evidence).
In the rationale, name the matching 2D pair (e.g. "A plan 2 matches B plan 1"); for different_layout state that no 2D pair matched and cite the difference; for no_2d_plan say which side(s) lacked a usable 2D plan (e.g. "both sides only 3D renders"); for inconclusive say both sides had 2D plans but the comparison was ambiguous. Also fill plan_a and plan_b from the matched (or most representative) 2D plans; leave a field out if not legible.$fp$::text),
    updated_at = now(), updated_by = 'migration_260'
where key = 'llm_floor_plan_match_prompt' and updated_by like 'migration_%';

-- 3) Sweep the stale `inconclusive` cache: under the OLD prompt it conflated "3D renders"
--    (now no_2d_plan -> merge) with "both-2D ambiguous" (stays inconclusive -> queue). Delete
--    those rows so the engine re-runs them under the new prompt and reclassifies. (Migration 245
--    precedent swept `different_layout` the same way.) Cache is re-derivable; no data lost.
delete from listing_floor_plan_matches where verdict = 'inconclusive';
