-- 481: mmreality walks ten per-(sale type, property type) indexes, each with the
-- portal's own declared result count, instead of the bare /nemovitosti/ feed.
--
-- The registry row (117, re-enabled in 252) declared one descriptor,
-- {"index": "nemovitosti"}, on the belief that mmreality served "a single mixed
-- index with no result total" — the reason it was parked on
-- supports_complete_walk=false. Live-verified 2026-09-06: the bare feed's own SSR
-- `metadata.count` is 8,703, exactly the PRODEJ total; the 1,518 rentals never
-- appeared in it and were never scraped. Every per-type URL
-- (/nemovitosti/{prodej|pronajem}/{byty|domy|pozemky|komercni-objekty|ostatni}/)
-- declares its own count, and the five prodej counts sum to the prodej total, so
-- the ten partition the portal exactly. That is the axis a complete walk can be
-- proved on (architectural rule #3), category by category.
--
-- Data only. supports_complete_walk is NOT touched: the coverage gate (455) flips
-- it from slice-ledger evidence once every one of these ten categories has been
-- walked to its declared tail three cycles running. The code accepts the old
-- shape too (falls back to this same list), so apply order does not matter.
update portals
set categories = '[
  {"sale_type": "prodej",   "category": "byty"},
  {"sale_type": "prodej",   "category": "domy"},
  {"sale_type": "prodej",   "category": "pozemky"},
  {"sale_type": "prodej",   "category": "komercni-objekty"},
  {"sale_type": "prodej",   "category": "ostatni"},
  {"sale_type": "pronajem", "category": "byty"},
  {"sale_type": "pronajem", "category": "domy"},
  {"sale_type": "pronajem", "category": "pozemky"},
  {"sale_type": "pronajem", "category": "komercni-objekty"},
  {"sale_type": "pronajem", "category": "ostatni"}
]'::jsonb
where source = 'mmreality';
