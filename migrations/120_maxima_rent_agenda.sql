-- 120_maxima_rent_agenda.sql
--
-- maxima's catalogue is served as TWO mixed indexes — sale (the default view,
-- af=1) and rent (the buy/rent toggle, af=2). Migration 116 registered maxima
-- with a single placeholder category descriptor ([{"label":"all"}]) that only
-- walked the default (sale) view, so the ~34 rental listings behind ?af=2 were
-- never discovered. Replace that descriptor with the per-(category_main,
-- category_type, af) list MaximaPortal now expects: each descriptor walks its
-- agenda once (agenda-cached) and keeps the id-prefix slice for its category, so
-- the runner gets real (cm, ct) labels (the Health reconciliation joins listings
-- on those) AND both agendas are covered.
--
-- `load_portal_config` reads `categories` from this row, so this UPDATE is what
-- actually takes effect at runtime (the code default in scraper/portal.py is only
-- the dry-run / DB-down fallback). Purely a config update — no schema change.

update portals
set categories = '[
  {"category_main": "byt",      "category_type": "prodej",   "af": 1},
  {"category_main": "dum",      "category_type": "prodej",   "af": 1},
  {"category_main": "pozemek",  "category_type": "prodej",   "af": 1},
  {"category_main": "komercni", "category_type": "prodej",   "af": 1},
  {"category_main": "ostatni",  "category_type": "prodej",   "af": 1},
  {"category_main": "byt",      "category_type": "pronajem", "af": 2},
  {"category_main": "dum",      "category_type": "pronajem", "af": 2},
  {"category_main": "pozemek",  "category_type": "pronajem", "af": 2},
  {"category_main": "komercni", "category_type": "pronajem", "af": 2},
  {"category_main": "ostatni",  "category_type": "pronajem", "af": 2}
]'::jsonb
where source = 'maxima';
