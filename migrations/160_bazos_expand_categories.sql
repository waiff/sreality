-- 160_bazos_expand_categories.sql
--
-- Expand what bazoš crawls from apartments-only (prodam/byt + pronajmu/byt) to
-- also walk houses (dum, chata) and commercial (restaurace, kancelar, prostory,
-- sklad), sale + rent. The portal registry's `categories` is what the index walk
-- loops over (mirrors the code default in scraper/portal.py).
--
-- These fine sections carry the property SUBTYPE in the detail breadcrumb
-- (bazos_parser SUBTYPE: chata->chata, kancelar->kancelar, sklad->sklad,
-- prostory->obchodni_prostor, restaurace->restaurace; dum stays generic). They
-- collapse onto one category_main (chata+dum -> dum; the four commercial ->
-- komercni), so the index-absence sweep is SUBTYPE-scoped
-- (BazosPortal.mark_inactive -> db.mark_inactive_native(scope_subtype=True)) —
-- otherwise each section's per-scope sweep would flip the sibling sections
-- inactive. (pozemek / garaz / ostatni are deliberately left out for now.)

update portals
set categories = '[
  {"sale_type": "prodam",   "category": "byt"},
  {"sale_type": "prodam",   "category": "dum"},
  {"sale_type": "prodam",   "category": "chata"},
  {"sale_type": "prodam",   "category": "restaurace"},
  {"sale_type": "prodam",   "category": "kancelar"},
  {"sale_type": "prodam",   "category": "prostory"},
  {"sale_type": "prodam",   "category": "sklad"},
  {"sale_type": "pronajmu", "category": "byt"},
  {"sale_type": "pronajmu", "category": "dum"},
  {"sale_type": "pronajmu", "category": "chata"},
  {"sale_type": "pronajmu", "category": "restaurace"},
  {"sale_type": "pronajmu", "category": "kancelar"},
  {"sale_type": "pronajmu", "category": "prostory"},
  {"sale_type": "pronajmu", "category": "sklad"}
]'::jsonb
where source = 'bazos';
