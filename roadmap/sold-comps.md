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
- **W2** — ✅ shipped (#1550, migration 544 applied). The fetch path, SHIPPING DARK: `scraper/reas_client.py` (a
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
- **W3** — 🟡 built, migration NOT yet applied. The read surface (migration 545): the SALES behind
  definer-style `sold_transactions_public`, read by `sold_comparables(lat, lng, radius)` —
  `language sql stable`, SECURITY INVOKER, NO `SET` clause, which is what keeps it inlined and on
  the geography index. The fetch LEDGER gets NO view: which cells were fetched is a projection of
  which towns hold a live pipeline card, so it stays admin-only and its one reader is
  `sold_coverage(lat, lng)` — SECURITY DEFINER, scoped to the obec that CONTAINS the point (cells
  are obec envelopes + 5 km and overlap heavily, so "newest box containing the point" would answer
  with whichever town was walked last), returning that town's name, its newest successful fetch and
  its newest attempt of ANY status. `Agenda.SOLD` re-tags the existing area /
  category / disposition / subtype defs and adds one `max_sold_age_days` — an integer day-count, so
  the sold-date bound is the `.lte` path the registry already had (no date control, no hand-coded
  escape); `applyRegistryFilters` became agenda-generic (`applyAgendaFilters`) rather than gaining a
  second copy. `SoldCompsBlock` on ListingDetail carries the coverage sentence in its four honest
  states (never checked / tried and FAILED / checked-and-empty / N held from the source's 24-month
  window against the all-time count it declares — two populations, never a shortfall), the ~30-day
  lag and reas's minority match of the register, a 1–3–5 km radius, the registry filter row, the
  table on W0's `Th`, and row → dialog with the hot-linked photos. The headline median holds the
  0–30 m² band out (a denominator defect, not a market fact) and refuses a flats-and-houses cohort.
  `reasSoldUrl`, the reas chip and their tests are deleted; the Cenová-mapa chip stays.
  **Migration 545 must be applied BEFORE this PR merges** — the SPA shipping with it calls both
  functions, which is exactly the migration-438 gap.
- **W4** — folded into W2 (above).
- **W5** — pay the rest. **`FilterChip.tsx` + its test are deleted.** It had zero importers: its
  five call sites (`Dedup`, `DedupAuditHistory`, `EligibilityMatrix`, `ClipAudit`,
  `LocationAudit`) all died with the legacy-dedup frontend teardown (b69da0d8, #967), and
  `Datasets.tsx` declares its own read-only `FilterChips` badges locally — a different shape,
  never an import. The `onRemove` split toggle+trash variant goes with it; nothing has used it
  since that teardown and git history holds it if it is ever wanted back.
  **`POST /tools/find_comparables` is deleted too** — the strict route, not the function. The
  SPA and the extension never call any `/tools/*` comparables route; the estimation agent
  registers `find_comparables_relaxed` and calls the toolkit function directly, never its own
  HTTP surface; `api/estimate_yield.py` imports the function from `toolkit`. The two surviving
  routes (`_relaxed`, `_along_axis`) had **no route test and no auth-census entry of their own**,
  so the five deleted-route tests and the `require_token` census row were RETARGETED onto
  `_relaxed` rather than deleted — `_build_comparables_inputs` and the required-category 422 are
  shared live code and keep their only route-level coverage.
  **The `(+ schema)` half of this bullet was wrong**: `FindComparablesIn` is the base class of
  `FindComparablesRelaxedIn` and `FindComparablesAlongAxisIn` and the declared type of
  `_build_comparables_inputs`, so it stays verbatim and saves zero lines. W5 therefore pays less
  than budgeted.
  **Merge gate (repo cannot settle it):** the route has existed since the first API commit
  (d6070f5a) and anything holding `API_TOKEN` could call it. In-repo the risk is as low as it
  can be read — the ClickUp integration is listed *Out of scope until explicitly opened*
  (`ROADMAP.md`), and `.claude/skills/toolkit-api` records a live check (2026-08-04) that it has
  **zero** historical rows and has never called the HTTP API with the static token; there is no
  `.http`/Postman/OpenAPI artifact or `curl` anywhere in the tree. The operator still confirms
  "nothing outside the repo calls it" before merge. `_relaxed` is **not** a drop-in substitute
  (it runs the relaxation ladder and stamps a different `metadata.tool`), so a silent 404 would
  be an outage, not payment.
- **W5 — `ComparableFilters.category_sub_cb`: REFUSED, deliberately left.** Deleting it is not
  subtraction, it is a behaviour change with an unpayable prerequisite. (1) The stated
  replacement does not exist: `subtype` is declared for `BROWSE`/`WATCHDOG`/`SOLD` only, not for
  `Agenda.COMPARABLES` or `Agenda.ESTIMATION`, so removing this field strips the
  comparables/estimation cohorts of their only house/commercial sub-type narrowing
  (`disposition_match` covers `byt` alone). Promoting `subtype` onto those agendas is an
  ADDITION, so it cannot be the justified deletion that pays for one. (2) It is a live knob in
  the estimation agent's generated tool schema (`api/agent.py`, `_FCR_OVERRIDE_FIELDS`) with a
  description telling the model when to use it — deleting it changes what the agent can do to a
  cohort. (3) Stored blobs would silently WIDEN, not fail: `WatchdogFilterSpec` loads with
  pydantic `extra='ignore'` and `coerceStoredFilters` drops unknown keys, so a watchdog pinned
  to a house sub-code would start emitting dispatches for chaty/vily — clearing that needs two
  SELECTs over `notification_subscriptions.filter_spec` and `filter_presets`, and this wave has
  no DB. (4) A bookmarked Browse URL carrying `?subcat=` widens the same way. (5) It could not
  be *complete* anyway: migration 537's Browse SRF keeps `category_sub_cb_filter`, and dropping
  that is a destructive migration needing operator confirmation plus a `pg_dump`.
  The defensible cheap alternative, if the operator wants it later: narrow the `FilterDef`'s
  agendas from `_ALL_AGENDAS` to the analytical set — dropping `BROWSE` + `WATCHDOG` removes an
  unusable control (`enum_values` is null, so `FilterForm` renders an options-less dropdown)
  while leaving the agent knob intact. Still subject to (3) and (4).

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
- An `ok` ledger row says what it COVERED. A walk the 25-page cap — or a `nextPage` that does
  not advance — cut short carries `truncated: took N of M in P pages` in `error`, and
  `record_count` is what the upsert wrote, not what the pages parsed. That row suppresses the
  cell for 35 days, so a clean `ok` over part of an answer would be the same half-truth by
  another door, and W3's coverage header would state it as fact.
- A cell that cannot be BOXED is not work: the work-list joins `admin_boundaries`. A skipped
  cell writes no ledger row, so `ORDER BY fetched_at NULLS FIRST` would re-offer it at the head
  of every pass for ever — starving the lane while the heartbeat read perfectly healthy.
