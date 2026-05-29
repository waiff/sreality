-- 111_idnes_complete_walk.sql
--
-- Promote the idnes scraper portal (migration 110) from a partial-walk pilot to
-- a COMPLETE-WALK portal, like bezrealitky. iDNES's search pages carry a result
-- total and have no deep-pagination cap, so a per-category walk is provable-
-- complete: the runner can then mark delisted listings inactive under the
-- completeness guard (architectural rule #3), source-scoped (rule #15).
--
-- Also widens the operational `categories` to the four residential pairs
-- (byty + domy × prodej + pronájem). The detail URL carries the category
-- (/detail/{sale}/{cat}/…), so the drain derives each listing's category from
-- its own URL — one config walks many categories. Purely additive UPDATE of the
-- existing row's operational config (migration 107 columns); ON CONFLICT n/a.

update portals set
  supports_complete_walk = true,
  categories = '[
    {"sale_type": "prodej",   "category": "byty"},
    {"sale_type": "pronajem", "category": "byty"},
    {"sale_type": "prodej",   "category": "domy"},
    {"sale_type": "pronajem", "category": "domy"}
  ]'::jsonb
where source = 'idnes';
