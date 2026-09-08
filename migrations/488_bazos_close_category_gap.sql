-- 488_bazos_close_category_gap.sql
--
-- Close the coverage gap migration 160 deferred: "(pozemek / garaz / ostatni are
-- deliberately left out for now.)" — nobody ever came back, and zahrada was never
-- named at all even though bazos_parser.CATEGORY_MAIN has always mapped it. Four
-- of bazos's own left-nav sections were therefore never walked, so listings filed
-- there were never enqueued: no error, no listing_fetch_failures row, nothing in
-- scrape_runs. The absence was silent by construction. Found by a live
-- production/warehouse ad in Havlíčkův Brod (inzerat 221963410, breadcrumb
-- "Reality > Prodej > Ostatní") that stayed out of `listings` for weeks.
--
-- The four added URL slugs are the portal's real path segments, verified against
-- the live left nav of /prodam/ and /pronajmu/ (both carry all four):
--   pozemek  "Pozemky"    -> category_main pozemek  (generic land, no subtype)
--   zahrada  "Zahrady"    -> category_main pozemek  (subtype zahrada)
--   garaz    "Garáže"     -> category_main ostatni  (subtype garaz)
--   ostatni  "Ostatní"    -> category_main ostatni  (generic other, no subtype)
--
-- Same collapse hazard migration 160 called out: pozemek+zahrada share
-- category_main=pozemek and garaz+ostatni share category_main=ostatni, so the two
-- new pairs get SUBTYPE entries (bazos_parser: zahrada->zahrada, garaz->garaz)
-- and the per-section presence nomination stays subtype-scoped
-- (BazosPortal.presence_candidates -> db.presence_candidates(scope_subtype=True)).
-- Without that, each section would nominate its sibling's rows every walk.
--
-- Still intentionally NOT walked, and this is the record of why:
--   projekty ("Nové projekty") — new-construction developer marketing that also
--     appears under byt/dum; no other portal in the fleet has a separate
--     new-projects category, so excluding it keeps bazos consistent.
--   podnajem / ubytovani ("Podnájem, spolubydlící" and "Ubytování", rent side
--     only) — roommate search and short-term lodging classifieds, not the sale or
--     rental of a property; a different ad category the platform tracks nowhere.
--
-- Additive: rewrites one registry row's `categories` (mirrors the code default in
-- scraper/portal.py). 14 pairs -> 22.

update portals
set categories = '[
  {"sale_type": "prodam",   "category": "byt"},
  {"sale_type": "prodam",   "category": "dum"},
  {"sale_type": "prodam",   "category": "chata"},
  {"sale_type": "prodam",   "category": "restaurace"},
  {"sale_type": "prodam",   "category": "kancelar"},
  {"sale_type": "prodam",   "category": "prostory"},
  {"sale_type": "prodam",   "category": "sklad"},
  {"sale_type": "prodam",   "category": "pozemek"},
  {"sale_type": "prodam",   "category": "zahrada"},
  {"sale_type": "prodam",   "category": "garaz"},
  {"sale_type": "prodam",   "category": "ostatni"},
  {"sale_type": "pronajmu", "category": "byt"},
  {"sale_type": "pronajmu", "category": "dum"},
  {"sale_type": "pronajmu", "category": "chata"},
  {"sale_type": "pronajmu", "category": "restaurace"},
  {"sale_type": "pronajmu", "category": "kancelar"},
  {"sale_type": "pronajmu", "category": "prostory"},
  {"sale_type": "pronajmu", "category": "sklad"},
  {"sale_type": "pronajmu", "category": "pozemek"},
  {"sale_type": "pronajmu", "category": "zahrada"},
  {"sale_type": "pronajmu", "category": "garaz"},
  {"sale_type": "pronajmu", "category": "ostatni"}
]'::jsonb
where source = 'bazos';
