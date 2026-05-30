-- 118_bezrealitky_all_categories.sql
--
-- Expand bezrealitky's portals.categories from 4 → 10 descriptors so the scrape
-- covers all 7 estate types × 2 offer types (the portal's full OWN inventory
-- under includeImports:false — about +700 listings on top of today's ~2,750).
--
-- KANCELAR + NEBYTOVY_PROSTOR both canonicalise to category_main='komercni',
-- and GARAZ + REKREACNI_OBJEKT both → 'ostatni'. Two descriptors that share a
-- canonical (cm, ct) would BREAK source-scoped mark_inactive (the second walk's
-- seen set wouldn't contain the first walk's listings, so the second would
-- flip them inactive). We group each colliding pair into ONE descriptor whose
-- estate_type is a list and carries an explicit category_main; the bezrealitky
-- client now accepts list-typed estate_type, walks them as a single
-- listAdverts query, and mark_inactive sees the union.
--
-- Purely additive (one config row UPDATE, no schema change).

update portals set categories = '[
  {"offer_type": "PRODEJ",   "estate_type": "BYT"},
  {"offer_type": "PRONAJEM", "estate_type": "BYT"},
  {"offer_type": "PRODEJ",   "estate_type": "DUM"},
  {"offer_type": "PRONAJEM", "estate_type": "DUM"},
  {"offer_type": "PRODEJ",   "estate_type": "POZEMEK"},
  {"offer_type": "PRONAJEM", "estate_type": "POZEMEK"},
  {"offer_type": "PRODEJ",   "estate_type": ["KANCELAR", "NEBYTOVY_PROSTOR"], "category_main": "komercni"},
  {"offer_type": "PRONAJEM", "estate_type": ["KANCELAR", "NEBYTOVY_PROSTOR"], "category_main": "komercni"},
  {"offer_type": "PRODEJ",   "estate_type": ["GARAZ", "REKREACNI_OBJEKT"], "category_main": "ostatni"},
  {"offer_type": "PRONAJEM", "estate_type": ["GARAZ", "REKREACNI_OBJEKT"], "category_main": "ostatni"}
]'::jsonb
where source = 'bezrealitky';
