-- 218_maxima_remax_complete_walk.sql
--
-- Promote maxima + remax from partial-walk to complete-walk. Both serve TWO
-- mixed agenda indexes (sale / rent ≡ category_type) that report a per-AGENDA
-- total but only a TITLE-DERIVED per-category slice — so a per-(category_main,
-- category_type) completeness gate was never available, which is why they were
-- supports_complete_walk=false (no index-absence delisting; ~4–5% of their
-- "active" rows were actually stale).
--
-- maxima_main / remax_main now do AGENDA-GRAIN delisting: once an agenda walk
-- reaches its reported total, mark_inactive flips that whole agenda
-- (category_type) against the FULL walk's id set via db.mark_inactive_agenda
-- (NOT the title-derived per-category slice, which could false-flip a listing
-- whose index-time title category disagrees with its detail-time category) +
-- the 24h staleness rail (rule #3). With the badge now derived from this column
-- (migration 217), they auto-show LIVE once this lands.
--
-- Data UPDATE, reversible (set back to false). Safe to apply before the code
-- deploys: the currently-deployed walk_category returns complete=false, so the
-- runner's `supports_complete_walk AND complete` gate stays false until the new
-- agenda-grain code ships.

update portals set supports_complete_walk = true
where source in ('maxima', 'remax');
