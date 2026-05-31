-- 121_bazos_rentals_category.sql
--
-- Add bazos rental apartments (pronajmu/byt) alongside the existing sale scope
-- (prodam/byt). The portal registry's `categories` is what the index walk loops
-- over, so this one row update is the whole config change — the drain reads each
-- ad's category (sale vs rent) off its detail-page breadcrumb, so one queue +
-- one drain cover both. Mirrors the code default in scraper/portal.py.

update portals
set categories = '[
  {"sale_type": "prodam", "category": "byt"},
  {"sale_type": "pronajmu", "category": "byt"}
]'::jsonb
where source = 'bazos';
