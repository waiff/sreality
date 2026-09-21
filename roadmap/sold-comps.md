# Sold comps — registered sales as their own fact

Opened 2026-09-21. The estimator has never seen a realized price: every comparable it
reads is an ASKING price, and `lifecycle='delisted'` was documented as a "transacted-price
proxy" it is not (a delisting is the advertisement ending — migration 453's header, on
70,130 long-unseen rows: not "probably sold": unknown; that claim is deleted in W1). This
track adds the missing half as what it actually is.

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
- **W1 · `feature/sold-comps-store`** — ✅ shipped. Migration 542 (store only:
  `sold_transactions` + the `sold_transaction_fetches` ledger, RLS-on/no-policy + explicit
  revokes, GiST on `(geom::geography)`), `scraper/reas_parser.py` (pure payload→rows, three
  refusals, broker-reported rows dropped and counted), fixtures + hermetic tests, and the
  deletion of the false "delisted = closed deals / transacted-price proxy" claim from all
  four places it is written: `toolkit/comparables.py`, `toolkit/filter_registry.py`,
  `api/schemas.py` and — the copy the operator actually reads, on `/settings` — the seeded
  `app_settings.default_lifecycle` description (migration 543). No network, no runtime
  change, shippable alone; 543 is the one statement to apply.
- **W2** — 🟡 in progress. The fetch path, SHIPPING DARK: `scraper/reas_client.py` (a
  `BasePortalClient` subclass on the shared rate ledger at one request per 5 s),
  `scraper/sold_db.py` (cell box, work-list, batched upsert, ledger row),
  `scraper/sold_fetch.py` (`fetch_cell`, which never raises — a cell attempt always ends
  in the ledger), the CLI `python -m scraper.reas_main --obec <kód> [--dry-run]`
  (`--bbox … --dry-run` for a no-DB smoke) and the `sold_comps` worker lane. One HTML GET
  per cell page at `listPerPage=100`; re-fetch only while `nextPage` is non-null, hard cap
  25 pages. **W4's lane landed here** rather than as its own PR: a fetch path with no
  scheduler is a feature nobody can turn on, and the lane is ~40 lines over the existing
  `_lane_loop` contract. The cell is the obec's `admin_boundaries` envelope (its `id` IS
  the RÚIAN kód) widened by 5,000 m — the read surface's largest radius, so any subject
  inside the obec is covered by construction.
- **W3** — the read surface: a definer-style view + a three-arg inlinable SQL function
  (lat, lng, radius — `language sql stable`, SECURITY INVOKER, NO `SET` clause, which is
  what keeps it inlined and on the geography index), `Agenda.SOLD` filter defs, and a
  `SoldCompsBlock` on ListingDetail. Deletes the reas chip path. Its radius options are
  1 / 3 / 5 km against `sold_db.MAX_READ_RADIUS_M` — the same constant, or the box stops
  covering the read.
- **W4** — folded into W2 (above).
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
  sold comp says that. It is also why a cell that fetched cleanly is left alone for 35
  days: re-asking sooner cannot find anything new.
- Fetching is gated on the deal pipeline; READING never is. Terminal and archived stages
  drop out of the work-list, because stopping the re-fetch is the politeness lever — but a
  closed deal is exactly where the stored comps must survive, so nothing is ever deleted.
- A page the parser refuses fails the WHOLE cell (W1's open issue, decided in W2). The
  refusals are contract failures — the active catalogue under a 200, an identity grammar
  that moved, a fifth `type` — and storing the good 90% of such a page would bake a
  half-truth into a fact table with nothing able to say which rows were lost. The cell
  gets a `failed` ledger row carrying the parser's message and is retried in 6 hours.
