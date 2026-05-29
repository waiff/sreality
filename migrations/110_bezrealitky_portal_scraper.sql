-- 110_bezrealitky_portal_scraper.sql
--
-- Promote bezrealitky from an on-demand URL parser to a scheduled scraper on
-- the shared portal framework (migration 107 operational config). Bezrealitky
-- is a JSON-API portal (public GraphQL at api.bezrealitky.cz) like sreality —
-- not an HTML crawler like bazos — so its index walk reads listAdverts (with a
-- totalCount and no deep-pagination cap) and its detail-drain reads advert(id).
-- That makes a per-category walk provable-complete, so unlike bazos it is
-- complete-walk capable: the runner marks delisted bezrealitky listings
-- inactive under the completeness guard (architectural rule #3), source-scoped
-- so it only ever touches bezrealitky rows (rule #15).
--
-- The existing 'parser' on-demand URL flow (scraper/source_parsers/bezrealitky.py,
-- used by the estimation preview) is unchanged — it's a different entry point.
-- `kind` here describes the portal's PRIMARY scraping mode for the Health page.
--
-- Categories use {offer_type, estate_type} (bezrealitky's GraphQL OfferType /
-- EstateType enums); the parser maps each advert's own offerType/estateType to
-- the canonical category_main/category_type, so the drain derives category from
-- the response and one config can walk many categories. Edit this row to expand
-- coverage (e.g. POZEMEK, KANCELAR) — no migration needed.
--
-- Purely additive (an UPDATE of one registry row); no schema change.

update portals set
  kind = 'scraper',
  stage = 'pilot',
  supports_complete_walk = true,
  split_threshold = null,
  categories = '[
    {"offer_type": "PRODEJ",   "estate_type": "BYT"},
    {"offer_type": "PRONAJEM", "estate_type": "BYT"},
    {"offer_type": "PRODEJ",   "estate_type": "DUM"},
    {"offer_type": "PRONAJEM", "estate_type": "DUM"}
  ]'::jsonb
where source = 'bezrealitky';
