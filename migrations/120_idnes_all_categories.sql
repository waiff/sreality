-- 120_idnes_all_categories.sql
--
-- Expand idnes's portals.categories from 4 → 10 descriptors so the scrape covers
-- every iDNES property category (byty + domy + pozemky + komercni-nemovitosti +
-- male-objekty-garaze, all × prodej + pronájem). Adds ~45,591 listings on top of
-- today's ~63k byty+domy: pozemky 28,012; komercni-nemovitosti 15,576;
-- male-objekty-garaze 2,003.
--
-- Each idnes search slug canonicalises to a unique (category_main, category_type)
-- pair (komercni-nemovitosti → komercni; male-objekty-garaze → ostatni), so no
-- two descriptors collide on the same canonical — source-scoped mark_inactive
-- stays correct without the descriptor-merging trick bezrealitky needed
-- (migration 118). Detail URLs use the singular form (komercni-nemovitost /
-- maly-objekt-nebo-garaz); the parser's DETAIL_CATEGORY map carries the singular
-- → canonical mapping the drain uses to recover each listing's category.
--
-- Purely additive (one config row UPDATE, no schema change).

update portals set categories = '[
  {"sale_type": "prodej",   "category": "byty"},
  {"sale_type": "pronajem", "category": "byty"},
  {"sale_type": "prodej",   "category": "domy"},
  {"sale_type": "pronajem", "category": "domy"},
  {"sale_type": "prodej",   "category": "pozemky"},
  {"sale_type": "pronajem", "category": "pozemky"},
  {"sale_type": "prodej",   "category": "komercni-nemovitosti"},
  {"sale_type": "pronajem", "category": "komercni-nemovitosti"},
  {"sale_type": "prodej",   "category": "male-objekty-garaze"},
  {"sale_type": "pronajem", "category": "male-objekty-garaze"}
]'::jsonb
where source = 'idnes';
