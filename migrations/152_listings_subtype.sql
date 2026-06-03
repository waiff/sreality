-- 152_listings_subtype.sql
--
-- Add a portal-agnostic, normalized property SUB-TYPE for houses (dum) and
-- commercial (komercni). Today every house collapses into category_main='dum'
-- and every commercial unit into 'komercni', discarding a dimension the portals
-- already expose (rodinný dům vs chata vs vila; kanceláře vs sklady vs výroba).
--
-- WHY a new column (not the existing category_sub_cb): category_sub_cb holds
-- sreality's RAW integer sub-code and is sreality-only (every other portal
-- stores NULL). `subtype` is a normalized text slug any portal can populate
-- from its own structured signal — sreality maps its sub-code (scraper/parser.py
-- SUBTYPE), other portals follow in their own parsers. ADDITIVE: existing
-- category_main / category_type / category_sub_cb filtering is unchanged, and a
-- row with no resolved subtype stays valid (NULL).
--
-- Slugs are diacritics-free, mirroring the category_main / category_type
-- convention. House and commercial sreality codes occupy disjoint integer
-- ranges, so the backfill maps the code directly with a category_main guard for
-- provable safety + self-documentation.

alter table listings add column subtype text;

-- Hot path is "active dum/komercni filtered by subtype" — mirror migration 022's
-- partial index on category_sub_cb.
create index listings_subtype_active_idx on listings (subtype) where is_active = true;

-- Deterministic, set-based, idempotent backfill of historical sreality rows.
-- Only sreality populates category_sub_cb, so this never touches other portals.
update listings l set subtype = m.slug
from (values
  -- dum (houses)
  (37, 'rodinny_dum'),
  (33, 'chata'),
  (43, 'chalupa'),
  (54, 'vicegeneracni_dum'),
  (39, 'vila'),
  (44, 'zemedelska_usedlost'),
  (40, 'na_klic'),
  (35, 'pamatka_jine'),
  -- komercni (commercial)
  (25, 'kancelar'),
  (26, 'sklad'),
  (28, 'obchodni_prostor'),
  (27, 'vyroba'),
  (29, 'ubytovani'),
  (32, 'ostatni'),
  (38, 'cinzovni_dum'),
  (30, 'restaurace'),
  (57, 'apartmany'),
  (56, 'ordinace'),
  (31, 'zemedelsky'),
  (49, 'virtualni_kancelar')
) as m(code, slug)
where l.category_sub_cb = m.code
  and l.category_main in ('dum', 'komercni')
  and l.subtype is distinct from m.slug;
