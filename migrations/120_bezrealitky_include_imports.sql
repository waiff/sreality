-- 120_bezrealitky_include_imports.sql
--
-- Flip bezrealitky's index walk to match the listAdverts API default
-- (includeImports:true, includeShortTerm:true) — which is also the CZ-scoped
-- count bezrealitky.cz shows. Previously the client hardcoded
-- includeImports:false (only the portal's own private-seller inventory), so a
-- significant slice of CZ listings imported into bezrealitky from other portals
-- was excluded. With imports on:
--   * byt/pronájem ~1,630 → ~2,483 (+853)
--   * byt/prodej   ~714   → ~812   (+98)
--   * dum/prodej   ~335   → ~408   (+73)
--   * pozemek/prodej ~290 → ~1,467 (+1,177)
--   * komercni/pronájem ~134 → ~223 (+89)
--   * ostatni/prodej ~126 → ~139  (+13)
-- The cross-source dedup engine collapses duplicates against sreality.
--
-- One exception: PRONAJEM/REKREACNI_OBJEKT imports are ~7000 vacation-rental
-- aggregator listings (not real-estate market data). Since REKREACNI shares the
-- 'ostatni' canonical with GARAZ (one descriptor), we set include_imports:false
-- on that one descriptor — losing ~13 garaz/pronájem imports to avoid the 7000
-- vacation-rental flood. The PRODEJ half keeps imports (rec. cabins for sale
-- are legit market inventory).
--
-- The Slovak listings the website ALSO shows (~1800 byt/pronájem) are not
-- accessible via listAdverts (which is CZ-only by default); they would only be
-- reachable via advertMarkers. Out of scope — this platform is CZ-focused.
--
-- Purely additive (one config row UPDATE — relies on the include_imports knob
-- the bezrealitky portal now reads per-descriptor).

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
  {"offer_type": "PRONAJEM", "estate_type": ["GARAZ", "REKREACNI_OBJEKT"], "category_main": "ostatni", "include_imports": false}
]'::jsonb
where source = 'bezrealitky';
