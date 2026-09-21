# Sold comps — registered sales as their own fact

Opened 2026-09-21. The estimator has never seen a realized price: every comparable it
reads is an ASKING price, and `lifecycle='delisted'` was documented as a "transacted-price
proxy" it is not (a delisting is the advertisement ending — migration 453's header says so;
that claim is deleted in W1). This track adds the missing half as what it actually is.

## North star

> **A registered sale is an account-less external FACT, not a listing: one row per sale
> under the sale's own cadastral identity, fetched per municipality (obec) cell and only
> for towns where the deal pipeline has a live card, read through ONE SQL definition.
> Never a listing row, never linked to a property, never mixed with asking prices, never
> adjusted, never shown without saying where it came from and when we last looked.**

Two tests for every choice: can the operator name each moving part in one sentence? Is the
table still honest when it is empty?

## Operator rulings (2026-09-21)

- Source is **reas.cz only**. sreality's `/cenova-mapa` is NOT ingested; ČÚZK WSDP is a
  named future source and nothing is built for it.
- Photos are **hot-linked** from the source's public URLs (`photo_urls text[]`). No R2
  copy, no `images` rows.
- The Cenová-mapa chip and `api/sreality_price_map.py` STAY. Only the reas.cz chip is
  deleted, in the wave that ships the read surface.
- The shared table/section primitive consolidation ships as its own PR, first.

## Waves — one PR each

- **W0 · `cleanup/shared-table-primitives`** — collapse the duplicated `Th` (8),
  `SectionLabel` (7) and `Hairline` (7) frontend copies into shared primitives. Pure
  refactor, pixel/class parity, no sold code.
- **W1 · `feature/sold-comps-store`** — 🟡 in progress. Migration 542 (store only:
  `sold_transactions` + the `sold_transaction_fetches` ledger, RLS-on/no-policy + explicit
  revokes, GiST on `(geom::geography)`), `scraper/reas_parser.py` (pure payload→rows, three
  refusals, broker-reported rows dropped and counted), fixtures + hermetic tests, and the
  deletion of the false "delisted = closed deals / transacted-price proxy" claim in
  `toolkit/comparables.py`, `toolkit/filter_registry.py` and `api/schemas.py`. No network,
  no runtime change, shippable alone.
- **W2** — `scraper/reas_client.py` (a `BasePortalClient` subclass on a shared rate
  ledger) + the DB writer + CLI `python -m scraper.reas_main --obec <kod> [--dry-run]`.
  One HTML GET per cell at `listPerPage=100`; re-fetch only while `nextPage` is non-null.
- **W3** — the read surface: a definer-style view + a three-arg inlinable SQL function
  (lat, lng, radius — `language sql stable`, SECURITY INVOKER, NO `SET` clause, which is
  what keeps it inlined and on the geography index), `Agenda.SOLD` filter defs, and a
  `SoldCompsBlock` on ListingDetail. Deletes the reas chip path.
- **W4** — the realtime-worker lane, shipping dark: one `app_settings` interval int with
  `default_interval=0` as the fail-safe. No flag, no workflow YAML, no registry row.
- **W5** — pay the rest: delete `FilterChip.tsx` (+ its test), `POST /tools/find_comparables`
  (+ schema) and `ComparableFilters.category_sub_cb`.

## Standing constraints

- The store is deliberately WITHOUT `account_id`, `property_id`, `listing_id`, a link
  table, snapshots, `is_active`, a queue, a failures table, a runs table, `images` rows, a
  feature flag and a `price_kind` column. Rules #2/#3/#15/#19 are about listings and do not
  reach a transactions fact.
- `displayArea`, `histogramPrice`, `originalPrice` and seller/broker identity reach neither
  a column nor `raw`. Each is a measured defect, not a preference — see
  `docs/architecture.md` § Data sources.
- No enum member is added anywhere. `larger` / `atypic` become NULL.
- A ~30-day publication embargo on `mapPointerPublishedAt` is the crawl watermark, so the
  freshest sale this source can ever show is about a month old. Any surface that renders a
  sold comp says that.
