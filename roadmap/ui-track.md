> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

## UI track (parallel, independent of analytical phases)

A browser UI is now a recognized territory rather than a future "maybe."
This track runs in parallel with the analytical phases above; the
toolkit is what makes the UI worth building, but the UI doesn't gate
toolkit work.

### Property page: the "Trh v okolí" dashboard tile (2026-10-23, no migration)
- **Done** (operator ask + rulings 2026-10-23): Přehled ends with a full-width tile drawing the account's
  dashboard named "Trh v okolí" (Browse › Statistiky offers "+ Trh v okolí · karta nemovitosti"; its chip says
  "karta nemovitosti") over the property's neighbourhood — its type, deal, disposition and ownership (an unstated
  one is dropped and named), a radius (250 m – 2 km, 500 m default, Browse's centre + radius square), building
  material and condition chips — under "Prodáno (trh)" (default) or "Vše". ONE neighbourhood definition
  (`lib/neighbourhood`) also seeds the explore window, which the tile's "Prozkoumat okolí" opens with the same
  filter, lens and dashboard (the 5 km seed deleted). The minimap's "Explore area" went with it and came back the
  same day as the tile button's twin (operator): one opener over one scope (`neighbourhoodExplore`). The Stats tab's
  Vzorek gains "Vše". The reas.cz sold card stays beside it.
- **Next (offered, not built):** a true circle instead of the square (a new location predicate in the shared
  compiler, rule 16).

### Property page, second pass: notes in place, tags in the header (2026-10-23, no migration)
- **Done** (operator's list, 2026-10-23): the minimap has zoom only (no overlays, no scale, the
  attribution folded to its (i)); the status pill links to the latest active advert; the tags are a
  "Štítky" button beside the collections; the "Moje" line became the notes section — notes read,
  written, edited and shared in place, the artifact link (or "+ Odkaz na artefakt") in its header,
  one-line composer that opens on focus, cards with labelled Upravit / Smazat / Přiložit, Czech
  throughout (the phone's notes too); Přehled's adverts and price moves are one card with a simple
  chart, right under the description; Trh v okolí folded into Cena a hodnota (price history, Valuo,
  registered sales, our estimates); the Moje tab is gone ("Moje fotky" moved to Fotky). Old `?tab=trh`
  and `?tab=moje` addresses land on the tab that took them over.

### Property page: a header and tabs (2026-10-23, no migration)
- **Done.** The operator picked design A from four proposals (artifact "Property page redesign") and
  asked for the brokers' contact on the first tab and the description above "Je cena v pořádku?". The
  page was thirteen stacked blocks with the photos eighth, under the whole estimation chapter. Now:
  a header (photo mosaic, identity, map), a "Moje" line (tags, latest note, artifact link), then tabs
  Přehled · Fotky · Cena a hodnota · Trh v okolí · Inzeráty · Moje · Audit (second pass: five tabs). Přehled answers each
  question in one card, read from the same query as its tab; the ask sits on the valuation ruler
  against our estimate's p25–p75 (or Valuo's band). `?tab=` keeps the tab; the old deep links
  (`?run=`, `#estimations`, `?advert=`, `?letters=`) land on their section's tab. Detail:
  `docs/architecture.md` § decision 11.
- **Next (offered, not built):** Czech labels inside the sections that still read English (Estimates'
  inner headings, Price history's stats, Registered sales, Freshness); photos grouped by room on the
  Fotky tab.

### Pipeline compact bar (2026-10-22, no migration)
- **Done.** The operator picked variant A from a measured comparison (artifact "Hlavička Pipeline", built on the
  live board: 170 cards, 10 stages, 8 collections, 6 tags). One title row (24 px title, the board switcher inline,
  the count, Sdílení / Spravovat fáze, and "+ Nová nemovitost" as the page's one primary) and ONE filter row:
  Lokalita, Stav, then Typ / Kolekce / Štítky as menus (`components/pipeline/FilterMenu`) that open the same
  MultiselectChips / CuratedSetFilter the bar drew inline and count their choices; "Zrušit filtry" replaces the
  header Reset; Řazení and Karty at the row's end. A row under the bar names every active choice with its own ×.
  Measured on 1440 × 900 (test account): the column heads moved from 330 px to 207 px; the operator's sets no
  longer add rows. The board switcher stays pills (inline), not the mock's dropdown: one click per board.
- **Not done (from the same comparison):** variant B's stage strip and folding the closed stages into one column.

### Browse map rail, folded presets, merge bar that wraps (2026-10-22, no migration)
- **Done.** The operator picked variant B (a vertical icon rail) and the folding preset section from a
  measured comparison (artifact "Ovládání mapy v Browse"). The map's four corner stacks became one rail at
  the right edge plus two panels (Vrstvy, Piny a ceny); layer keys sit bottom-left and fold. Measured in a
  real browser at 1440 × 900: the controls cover 5.7 % of the 450 px map, down from about 25 %. Presets fold
  to the active one behind "Presety 11" (one row instead of three). Merge mode got its own row under the tabs:
  letters and statement wrap, Merge and Cancel stay at the right edge (eight letters verified), and the
  button counts groups past three. Detail: `docs/design/map-overlays.md` § MapOverlayControls.
- **Next:** the Stats dashboards bar can fold the same way (one prop) if it grows.

### One status: the property's, on every surface (2026-10-22, migration 657)
- **Done.** Operator report 2026-10-21: Neaktivní + Sreality drew a property still live on remax (cards
  and map). MS19 judged the status on the portal's ads while every surface draws the property's
  `is_active`; ruling 2026-10-22: the status is the property's, a portal (or broker) asks for an ad there
  in that same status. `portal_status_matches` became two conjuncts, `applyPortalRule` lost its
  inactive-on-portal arm, the broker lookup became one per-ad predicate, the Stats pivot lost 634's lens
  and turnover set-aside, and 634's broker known gap closed with it. Measured before: 2,184 of 159,711
  "inactive on sreality" properties were live elsewhere (3,771 on idnes).
- **Card portal badge:** no status word ("portál" / "neaktivní"); it overflowed a narrow card to the
  LEFT and the photo cut "NE", so an ended card read "AKTIVNÍ …". Now bounded by the card, truncated at
  its end, the full list in its tooltip.
- **One status parameter (2026-10-23, migrations 661 + 662):** the two aggregate RPCs took the Status filter as
  two booleans (`active_only_filter` / `inactive_only_filter`, four states for a three-value fact). Expand /
  migrate / contract: 661 adds a trailing `status_filter` (applied before the SPA merge), the SPA sends only it,
  662 drops the booleans once that SPA is deployed and open tabs have reloaded. One deploy window.

### Back to Browse keeps the filters (2026-10-22, no migration)
- **Built:** every way back from a property lands on the filtered Browse view that was left (operator
  request 2026-10-22). The same-tab routes (card, then the crumb or the browser's Back) already did, as
  Browse's whole view is the URL; two did not and went to a bare `/browse`: the nav's "Browse" entry, and
  the crumb on a property opened in a new tab or from a map pin's plain link (no history entry to step
  back to). Both now go to the last Browse query string, kept per browser in localStorage
  (`sreality.browse.lastSearch`, `lib/lastBrowseView.ts`, written by the Browse page). On Browse itself
  the nav entry stays the bare page, the reset, and a reset is remembered too.
- **Not done:** the list's scroll position still starts at the top after Back (the list is an inner
  scroller; restoring it means re-loading the pages above the card first).

### Filter presets frame their map area (2026-10-22, no migration)
- **Built:** loading a preset that saved a map area moves the Browse map to that area (operator
  request 2026-10-22); before, the cohort filtered on the saved box while the camera stayed put. A
  saved centre+radius frames its square, as Browse filters on (`filters.savedMapArea` over
  `effectiveBbox`); a preset without an area leaves the camera alone. One command, `ListingMap`
  `frame` (the `flyTo` twin), sent by `BrowseExperience` on the preset click.
- **Built (same day):** "Include the current map area in this preset" is ticked by default when saving a
  new preset (operator request); the Update dialog still starts from the preset's own choice.
- **Built (same day): a preset keeps every filter** (operator request, reversing the 2026-10-19 ruling): the
  curated sets (pipeline, collections, tags), the broker and the dismissed reveal are stored, a change to one
  offers "Update preset", and a click applies them — a preset saved before stored them off, so it turns them
  off. Desktop and phone alike. Stats dashboards still leave them out (shareable; operator choice):
  `filters.DASHBOARD_EXCLUDED_KEYS` / `filtersForDashboard`. Every other filter (velocity, price activity,
  the rest of the 70) was already stored.

### Condition filter: the "Neuvedeno" chip (2026-10-20, migration 644)
- **Built:** Browse's "Condition (Stav objektu)" filter offers "Neuvedeno" as furnished and ownership
  do: a property whose condition is NULL or off the canon. One meaning on every surface (rule 16):
  the list, count and pins (`queries.ts`), Stats and the map cells (644 restates both RPCs with the
  furnished shape, md5-gated on 640), the Watchdog and every estimation cohort (`filter_compiler`),
  the phone sheet. The canon itself (`CONDITION_OPTIONS`: stored values, Stats keys, the scraper's
  vocabulary) never gains the sentinel; only the filter's chips do (`CONDITION_FILTER_OPTIONS`).

### Browse filter layout (2026-10-20, no migration)
- **Shipped: the sidebar the operator arranged** (drag-and-drop draft, 2026-10-20). Four bands —
  Essentials, Curation, Velocity & price activity, Advanced — with one heading level (the group
  captions are gone on desktop and phone; `lib/filterLayout` rows), bigger band titles, and the
  header (Reset, fold arrow) pinned while the filters scroll. Status and "Seen at birth" (was
  "Newly listed only") share one line of buttons; Listing for has no "Vše" pill and Browse
  defaults to Prodej (a link without `deal=` opens on sale; Watchdog and estimation keep their
  own default). The 1–5 condition scores left both surfaces (scoring is parked, operator 2026-10-20).
- **Shipped (2026-10-21 follow-up):** Status, "All ads | Seen at birth" and "Show dismissed | Hide
  dismissed" are one block of segmented controls (`SegmentedBar`, the amenities' any/yes/no look,
  now shared); the dismissed switch moved there from Curation. Browse › Stats warns above the
  charts while Hide dismissed leaves dismissed properties out of the sample, with "Započítat skryté".
- **Done: Table tab removed 2026-10-20** (operator ruling; ~470 lines net; the cards sort menu
  gains "Plocha: od nejmenší / od největší" + "Naposledy viděno" (one direction; an ascending
  last-seen link reads descending); the sort fields without a control and `estate_area` deleted,
  a stored or linked one reads as the default). An old `?tab=table` link opens the Properties tab.
- **Done: Lot area sort 2026-10-21** (operator request; no migration). The cards menu (desktop + phone
  Řazení) gains "Pozemek: od nejmenšího / od největšího" on THE plot measure `plot_area_m2` (the Lot
  area filter's column; a flat has none and sorts last), and every card with a lot names it after the
  floor area ("180 m² · 850 m² pozemku"; a pozemek's area already is its plot). Same plan and cost as
  the price sort (~90 ms, houses for sale). Owed: the phone property page's line has no lot until
  `properties_public` carries the measure.
- **Shipped (2026-10-22): Advanced → Energy rating (PENB A–G)**, Browse + Watchdog (migration 648:
  `energy_rating_filter` on the Stats and map-tile RPCs; `browse_list` already carried the column). A
  property's rating is the first non-null across its ads; G is also the legal "no certificate"
  placeholder (~half of rated flats for sale), which no portal marks apart.
- **Shipped (2026-10-22): Advanced → Broker** — a name search (GET /brokers/search) that sets the
  existing broker scope (`brokerId`, MS19) on desktop and phone; it narrows the current filters
  (the explore-broker modal still opens a broker's whole book) and stays outside presets.
- **Moved (2026-10-22):** Source portal sits last in Essentials (was in Curation; operator).
- **Not built, by ruling:** Has a floor plan (operator 2026-10-21; CLIP-only signal, ~2 h lag).
  Condition scoring stays PARKED, not removed (operator 2026-10-20, re-confirmed 2026-10-21: keep the
  engine and data, nothing may read its levels; registry ids retired with 645).

### Pipeline sharing surfaces (2026-10-20, migration 641; PR #1908)
- **Built (merges with W1 #1906 in the window):** the Pipeline tab switches between my board and
  boards shared with me (per device, `lib/acting`), a shared board renders inside `<ActingAs>` (read:
  board only; edit: drag, stages, trash as the owner; the phone moves cards through the stage sheet),
  one `ShareBlock` serves the pipeline and collection pages, the desktop property page offers "Upravit
  jako {email}" with an in-page banner, and delegated edits carry one stamp
  (`lib/delegatedStamp`). Detail: `roadmap/operator-workflow-track.md` U-PIPE-SHARE.

### Stats dashboard (2026-10-19, migration 626)
- **Shipped: Browse › Stats is a dashboard the user composes** (`docs/design/stats-dashboard.md`,
  operator rulings 2026-10-19). Charts = rows (up to six nested dimensions) × one column × a measure
  (doba na trhu, plocha, cena, cena za m²), drawn as a table, a line, a bar or a box; one board per
  account (`stats_dashboards`, `PUT /stats/dashboard`, whole-document upsert validated by
  `toolkit/stats_registry.py`). ONE pivot RPC over 625's cohort WHERE byte for byte, ONE key
  function (`stats_dim_key`), ONE registry rendered to SQL, TypeScript and the API validator; every
  chart shows its sample ledger (in the filter / sample / excluded for missing data) and refuses
  rather than guesses (mixed categories, turnover over mixed statuses, too many cells).
- **Deleted, not patched:** the four fixed cards (`BrowseStats`, `PriceBandVelocity`,
  `DispositionBoxPlots`) and the old aggregate body with their five box types, and the LLM box-plot
  annotations (summarize-1: module, route, schemas, tests); migration 627 drops their cache and two
  settings — applied 2026-10-19 17:35Z on the operator's word, after an R2 backup
  (`backups/region-annotations-627/2026-10-19/`, 21 + 64 rows) and the autodedup session's green light.
- **Rule-25 clause (operator may veto):** a place cell keys on the RÚIAN CODE `browse_list` already
  carries (two same-named towns or streets stay two cells; the pivot reads no listing location), and
  the payload's `places` list resolves each key to its name through ONE new owner-rights view over
  the open register mirror, `ruian_place_names_public(level, code, name)` — the register is dark to
  browser roles and the pivot runs as its caller. A location read surface added by a PR that deletes
  none: the PR body says why (the five place dimensions were asked for on 2026-10-19).
- **Several statistics per chart (2026-10-19, migration 628):** a line, a bar and a table with a column draw the
  statistics the user picks (median, p10, p25 ... at once; the first pick leads in copper, the last cannot be
  unpicked). The saved chart's `stat` became `stats`, a list; 628 rewrites the stored board losslessly.
- **Floor lowered to five (2026-10-19, migration 632):** a Stats cell shows its statistics from 5 properties
  (operator ruling; it was 10). One constant in the pivot (`v_min_n`), mirrored by `MIN_N`, shipped as `min_n`.
- **Status judged by the cohort's rule (2026-10-19, migration 634):** with a portal filter the pivot's
  `status` fact and the turnover gate read the portal rule (597), not the property's is_active — a property
  delisted on the chosen portal but live elsewhere no longer flips a Neaktivní cohort to "aktivní i neaktivní"
  (it leaves a turnover sample as excluded instead). **Superseded 2026-10-22 (migration 657):** the status
  is the property's everywhere, so the lens, the set-aside and its broker gap are gone.
- **Legacy vocabulary healed (2026-10-20, migration 642):** the 31 off-canon condition spellings (105 bazos
  listings) and 28 impossible dispositions (812 listings) that charted as raw-spelled "pod 5" rows now read
  as the canon or NULL; the mapping is pinned to `scraper/vocabulary.py` by its test. `backup_642` holds the
  old values — drop it after a clean week.
- **Done: dashboard duplicate + the sold-market lens (2026-10-22):** an own dashboard's menu offers "Duplikovat"
  (a new row as stored, "<name> (kopie)"); the Stats tab's "Vzorek" switch counts every chart over the Browse
  filter or over the sold market (inactive, seen from birth, dismissed included, 2–60 days on market) — a URL
  lens, never stored, no backend change.
- **Done: saved dashboards 2026-10-20 (migration 647, `docs/design/saved-dashboards.md`):** named chart sets on
  a bar atop the Stats tab — load copies one into the working board (and its filter, when it was saved with
  one), "Uložit změny" / "Uložit jako dashboard", read-only sharing by email (Načíst, Uložit jako vlastní,
  Přestat sledovat), a share notification opens it. The filter-preset bar folded into the same `SavedSetBar`
  (presets unchanged). Line ledger (SPA production): net +407 (+733 as first cut) — the preset bar and its dialog
  (757) became the generic bar, its dialog and the preset instance (707); the dashboard instance is 300. The
  sprint's net is ≈ +350 production lines (≈ +0 to +50 without migration 647), not the planned ≈ −100.
- **Next:** (1) the land-rent basis ruling — rented land keeps the rent basis and its 25 / 50 bands until
  the operator rules; (2) optionally `reparse --source bazos --fields disposition --missing --write
  --allow-snapshot-deferral` (dry run first), which may fill a NULL disposition the page states grammatically;
  nothing else is owed on places.

### Negative portal filters (2026-10-13, migration 597)
- **Shipped: the portal filter is two-sided** — "on idnes, not on sreality". One chip per portal
  cycles off → on it → not on it (Browse sidebar, Watchdog editor, phone Feed); once something is
  excluded, the user picks whether any ad or only active ads count (default: any ad). The rule
  (`portal_status_matches`, `applyPortalRule`, the Watchdog hook) and its table
  (`tests/fixtures/portal_rule.json`) carry both sides; the exclusion ignores the status switch and
  a broker. Paid for by deleting the five RPC parameters nothing sent, the map's
  `listing_ids_filter`, the retired near-city filter state and dead filter plumbing (#1788).
- **Next (not started):** its precision tracks merging — outside the AUTODEDUP apply scope about
  half to three quarters of a "not on sreality" cohort have an unmerged sreality twin (measured
  2026-10-13); widening the scope (the Prague plan) is the fix, not a UI caveat (operator ruling).

### Phone app (W1 merged 2026-10-12, closes on the install walk; W2–W4 next — map [#1759](https://github.com/waiff/sreality/issues/1759))
- The platform on the operator's iPhone home screen: the same SPA at the same address, a
  phone shell by screen width. Waves W0 prototype → W1 feed → W2 property page → W3 pipeline
  → W4 collections + brokers; a wave closes on the operator's install walk, not the merge.
- **W1 slice: the installable shell** ([#1777](https://github.com/waiff/sreality/pull/1777)):
  self-hosted fonts, `manifest.webmanifest` + icons (`frontend/scripts/make-icons.py`), the iOS
  meta tags, and a version-agnostic `public/sw.js` (navigations network-first, `/assets/*`
  cache-first; buildSkew stays the update signal). Decided in #1763 and #1764. No phone layout yet.
- **W1 slices: per-account presets** ([#1775](https://github.com/waiff/sreality/pull/1775)) and **the
  1080 px photo copy on first request** ([#1776](https://github.com/waiff/sreality/pull/1776)).
- **W1 slice: the phone shell and the feed** (#1766): under 768 px `Shell.tsx` renders the routes in
  a phone frame (`components/phone/`, `lib/phoneShell.ts`) — an `h-14` header with the page title,
  a back affordance and the account sheet (theme, "Desktopová verze" = a per-device override, sign
  out); a four-tab bar (Nové, Pipeline, Sbírky, Makléři) hidden while typing. Every other route
  keeps its desktop page inside it. `/feed` ("Nové", the manifest's `start_url`, `/` on the phone)
  is Browse's read newest first (`fetchListingsForCards`, keyset, `CARD_COLS`, Browse's query key)
  with "Vše" + the account's presets (remembered per device), portal chips (one portal = that
  portal's newest-ad order) and "Skryté" (the hidden properties only, a page of dismissals at a
  time); Prague day rules with refresh; the prototype's card: the 1080 px cover, the margin of
  marks (`usePipelineCard`, the collection list, `useDismissal` — the stage and collection menus
  are the desktop bodies in a bottom sheet), the extension's stage wash, per-m² through
  `lib/measure` (rule 23).
  Follow-up: a photo strip per card (up to eight, cover first, the wash on each; `/feed` a lazy chunk), PR [#1785](https://github.com/waiff/sreality/pull/1785).
  Follow-up: full filters — the Filtry sheet (Browse's registry filters grouped as the sidebar, the view in the URL as Browse's, presets saved and updated from the phone), PR [#1866](https://github.com/waiff/sreality/pull/1866).
  Photos sprint W1: one attachment store per property (`property_attachments`, migration 660; a note's files are the same records, 1080 px display copies behind the JWT proxy), PR [#1974](https://github.com/waiff/sreality/pull/1974).
  Photos sprint W2: "Moje fotky" — the phone page's section after Poloha (Vyfotit / Z knihovny) and the desktop's Moje tab (Upload, drag-and-drop; the Fotky tab since 2026-10-23), the original in one viewer with Sdílet (share sheet) and Smazat, Vybrat, a split's file item, the zip's `moje-fotky/`, PR [#1975](https://github.com/waiff/sreality/pull/1975).
- Next: the install walk on the iPhone (resolves W1: Safari install, sign-in inside the app,
  `visibilitychange` on resume, the worker's cost on feed photos); then W2 — the property page as
  reading + curation and the paste box (the fifth tab, Odkaz).
- **W2 groundwork: one set of portal-id rules** (#1767): the extension's URL→`(source, native id)`
  rules moved to `frontend/src/lib/portalIds.ts`, imported by the extension cross-tree like
  `lib/brand.ts`; `parsePortalUrl` is what the paste-a-link box calls to land on
  `/listing/:source/:nativeId`. Also fixed: bezrealitky's English site (`www.bezrealitky.com`,
  `/properties-flats-houses/`), which no rule matched before. Next in W2: the paste box itself.
- **W2: the property page on the phone and Odkaz** (#1767, [#1782](https://github.com/waiff/sreality/pull/1782)): under the phone shell
  the property page is the prototype's "Nemovitost" — photo swiper (wash removed 2026-10-22; feed/pipeline wash at the extension's quiet values, PR [#1970](https://github.com/waiff/sreality/pull/1970)), price + basis,
  the curation row, then every non-map section as reading + curation (estimations read-only; no maps,
  no estimation start); the fifth tab `/link` takes a pasted link (the first URL in the text) to its
  property, keeps the last five per device, and says "Tento inzerát v databázi nemáme" with the
  portal link for an ad we lack. A password sign-in with nothing stashed lands on `/`. Resolves on the
  install walk. Follow-up (PR open): **Poloha** — Mapy's static map streamed through the API (never stored), label + note + Apple/Google/Mapy.cz links, as the feed card's fourth margin mark (a sheet) and the property page's first section; `MAPY_STATIC_MAPS_ENABLED=0` turns the image off.
- **W3: the pipeline as swiped stage pages** (#1768): on the phone `/pipeline` renders
  `components/phone/PhonePipeline.tsx` instead of the kanban (never mounted there) — a stage strip
  (code badge, label, count; a hairline before the closed stages) over one scroll-snapped page per
  stage, each scrolling on its own; cards are a stage-washed 104 px thumbnail, price, layout, place,
  days in the stage and MF; the stage badge opens the stage sheet (move, two-step remove through
  `usePipelineCard`). Same reads as the board (`lib/pipelineReads`); no drag, no stage management.
- **W4: collections and brokers on the phone** (#1769): `/collections` is a hairline list with
  "Nová sbírka" (a name in a bottom sheet, `createCollection`, monitoring off) and `/collection/:id`
  the members as feed cards (`fetchListingsForCards`, the collection as prefilter; the card's
  collection sheet is the removal; a member with no served location stays as a plain line);
  `/brokers` a reduced leaderboard (name search in place, place in a sheet, byty/domy,
  prodej/pronájem, metric) and `/brokers/:id` the dossier without the map (masked contact,
  "Kopírovat", kraje and per-type counts as numbers, firms, the live ads as feed cards by
  `brokerId`). Rename, delete, monitoring and the broker map stay on the desktop (#1760 Q11/Q12).
  Closes on the install walk.
- **Collection sharing on the phone** (collection sharing W5, `docs/design/collection-sharing.md`):
  sharing is managed on the desktop; the phone shows shared collections read-only — "Sdílené se
  mnou" under the own list (row "sdílí {email}", no monitored mark), the detail header "sdílí
  {email} · jen ke čtení" with no "Otevřít Nové", and the notes composer's "Sdílet s {email}"
  checkbox and "Sdíleno" mark. The footer says sharing lives on the desktop.

### Browse card cover photo by tag (done, 2026-10-07)
- A "Cover" dropdown beside Sort picks which photo every card opens on: Default (the ad's own first
  photo) or a CLIP logical tag (fasáda, kuchyně, obývací pokoj, ložnice, koupelna, WC,
  balkon/terasa, zahrada, půdorys, situační plán). The card opens on the photo CLIP is most sure
  carries that tag; an ad without one keeps its first photo. The carousel keeps the gallery order
  (the counter reads e.g. "3 / 12"), and paging still works.
- Front-end only: the cards already load every photo with its `clip_logical_tag` +
  `clip_confidence`. No confidence floor, so the chosen cover always agrees with its tag badge.
  The choice is a per-browser preference (`sreality.browse.cardCoverTag`, like the card image
  size), not part of the URL or a preset. Code: `lib/imageTags.ts` `COVER_TAGS` / `coverIndex`,
  `ImageCarousel` `startIndex`.
- **Same day, operator ruling: the trained DINOv3 heads decide, CLIP only where they have not
  scored.** Migration 591 publishes the ACTIVE tag model's per-head scores on `images_public`
  (`tag_head_scores`, read live). Per photo, a head score ≥ 0.5 is a yes and below it a no that
  overrules CLIP; a head's yes outranks any CLIP match. Options are keyed to `tag_taxonomy` ids,
  so a future head (ložnice, WC, balkon, zahrada) takes its option over with no code change.
  Added Garáž, 3D plán, Katastrální mapa, Letecký snímek. Coverage today: v1 scored ~9% of
  active listings once (2026-09-27); nothing scores new photos yet — that is the next step.

### SPA read seam: every PostgREST read through `pgRead` (done, 2026-10-01)
- `lib/pgRead.ts` is now the one way the SPA awaits a supabase-js read (56 direct awaits plus the
  18 `fetchAllRows` sites, whose every page it awaits; across `queries.ts`, `priceStats`, `pinAudit`,
  `locationWaterfall`, `auth.tsx`, `Filters.tsx`): a 20 s deadline (none before — a stalled read
  spun forever), React Query's signal (cancelled queries abort their request), and an `ApiError`
  with the wire status so `main.tsx`'s one retry rule now covers PostgREST too (main.tsx never
  retried a Supabase read; only postgrest-js's own hidden GET retry did, now switched off). Recorded
  trade: that hidden retry made up to 3 attempts (1/2/4 s) on a 503, a 520 or a network error; the
  one rule makes a single retry after 1 s, and now counts Cloudflare's 520 as transient. The Browse
  count fell back to the estimate only on its own 2.5 s budget (exact ONLY since 2026-10-16: the
  fallback and `deadlineMs` deleted, a slow count is loading, a failed one the header's error); the agenda gate
  rethrows a transient failure instead of caching "no constraint" for the session; dead `ping`
  deleted; an ESLint selector bans destructuring `data`/`error`/`count` off an awaited builder.

### Browse map: pin precision on request, not under every pin (done, 2026-09-22)
- Location W3-3 drew a true-metre uncertainty circle under every pin below building level. ~87 % of
  active pins are below it (street 300 m, část obce 750 m, obec 1 km), so at street zoom the map
  was stacked discs with the pins and prices buried: "totally unusable" (operator).
- Now the pin carries the answer: a solid dot for a building / address pin, an open ring for
  anything coarser, with a legend in the count's row ("Přesná poloha N · Přibližná N"). Clicking a
  pin draws its one circle for as long as its popup is open, and the popup says it in words
  ("Přibližná poloha: ulice, ±300 m", the TRUE radius even where the drawn circle is capped at 2 km).
  Covers Browse, Explore area and the broker explore map (all one `ListingMap`).
- Two map-chrome fixes found on the way: "Show all" sat under the zoom buttons, and a popup opened
  under the floating panels and lost its price line (popups now stack above them).

### Save-to-collection on the listing detail header (done, 2026-09-14)
- The Browse card's bookmark now has a twin in the listing-detail action bar, between "Přidat do
  pipeline" and "New estimation": out of every collection it reads "Uložit do kolekce"; saved, it
  reads "V kolekci" / "V kolekcích · N" and takes the soft copper tint. Monitoring rides on the
  collection (the bell in the panel), so this is also the page's monitor switch.
- Extraction, not duplication: `components/CollectionSaveMenu` (the panel + both writes + the four
  invalidations) and `components/CollectionMark` (the glyph) are now shared, with the card glyph and
  the header button as the two thin triggers — the same split PipelineFunnelButton / PipelineToggle
  already have over `<PipelineStageMenu>`. Membership comes from the caller: the grid's one shared
  map, or the per-property key the detail page's CurationBlock already subscribes to, so a save in
  the header is immediately true in the block below it.
- The panel now distinguishes a FAILED collection list ("Kolekce se nepodařilo načíst") from an empty
  one; both used to render "Create a collection →", which tells an operator whose collections exist
  that they have none.

### External map links on the listing detail (done, 2026-09-14)
- Under the header map, below "Explore area": Mapy.cz, Google Maps and iKatastr, each opened at the
  listing's resolved coordinate. "Explore area" is the market view of the NEIGHBOURHOOD; these three
  answer "where exactly is this, and on whose parcel" — the cadastre link lands on the parcel with
  iKatastr's info panel open.
- `lib/geoLinks.ts` builds the three documented, key-free deep links (Mapy.cz takes lon,lat; Google
  and iKatastr lat,lon — the swap is pinned by tests); `components/listing-detail/ExternalMapLinks`
  renders the row. Same coordinate gate as the map itself: no coordinate, no row. Precision is
  inherited, never implied — a street-level pin opens the wrong building on all three, by design.
- **+ Reas.cz (2026-09-16):** a fourth chip opens reas.cz's sold-properties search ("prodané
  nemovitosti" — actual sale prices) for a ~2 km box around the point: `?bounds=swLat,swLng,neLat,neLng`,
  LAT FIRST — reas also parses lng-first without complaint and then opens an empty map elsewhere
  (its own payload stores longitude in the field it calls `southWestLatitude`). The box, not reas's
  `/<obec>-<RÚIAN obec kód>` path form, because `listings_public` carries no RÚIAN codes and a box is
  the neighbourhood across municipal borders; ±1 km because ±500 m showed nothing around a small-town
  listing. The row is a 2×2 grid below `sm` (four equal chips truncated "Mapy.cz" at 360px).
  Deleted by sold-comps W3 (2026-09-21) as superseded by the in-app table, and RESTORED the same
  day on the operator's ruling: the table holds only what reas.cz shows anonymously (24 months),
  and the chip is the one-click way to reas.cz's own map, where a logged-in operator sees more.
- **+ sreality Cenová mapa (2026-09-17):** a fifth chip, and the row splits in two — WHERE (Mapy.cz,
  Google, Katastr) over what it SELLS for (Reas.cz, Cenová mapa). The price map addresses places by
  Seznam's own locality ids (`/cenova-mapa/hledani/byty/<kraj>-<id>/<okres>-<id>/<obec>-<id>`,
  `?ulice=` for a street, a path segment for a part of town) and never by a coordinate, so
  `GET /maps/sreality-price-map` (`api/sreality_price_map.py`) looks the listing's `display_label` up in
  sreality's locality suggest (no CORS upstream, hence the proxy): house number cut (with it, nothing
  matches), "street-or-part, obec" then "obec", the candidate NEAREST the listing's point inside a
  per-level cap (street 3 km, part 5 km, town 25 km) — "Nová Ves" alone is five towns. The chip renders
  at once with the national map and sharpens when the lookup answers; a failed or empty lookup keeps
  the national map. Only `byty` exists as a price-map category (`domy`/`pozemky` 404).

### Portal links read the stored URL — reconstruction deleted (2026-09-11, W2 of the URL contract)
- Operator-reported: the listing page's Sreality chip 404'd (`rodinny-dum` vs sreality's `rodinny`).
  The SPA rebuilt sreality URLs from `CATEGORY_SUB_LABELS`; wrong for 14 of 48 codes, both auction
  types, no link at all for land/other, and a merged property's siblings unreconstructable per row
  (`priceHistory` applied the PARENT's triple to every sibling). `docs/design/portal-listing-url.md`.
- Migration 494 appends `source_url` to `listings_public` (from 425's body); `ListingPublic` +
  `DETAIL_COLS` carry it. `srealityListingUrl` / `portalListingUrl` / `SrealityCategory` are DELETED
  (with `portals.test.ts`, which asserted the 404 as correct); six read sites read `source_url`
  (header chips, price history, estimation prefill, comparable modal — now linked for all nine
  portals —, Watchdog chip, BrokerDetail Portál cell). `portalUrl.census.test.ts` fails the build if
  app code ever assembles a sreality URL again.

### Phase U0: Foundation (done)
- `frontend/` folder with README declaring conventions.
- Migration 008 creates `*_public` views and grants `SELECT` to the
  `anon` role; sensitive columns (`raw_json`, `geom`, hashes, error
  messages) are never exposed.
- CLAUDE.md "Territories" section defines the boundary between the
  Python backend and the future frontend.

### Phase U1a: Database browser (done)
Read-only Vite + React + TS SPA over the `*_public` views with the
`anon` key. Deployed to Railway as a second service alongside the
FastAPI backend. Civic-archive visual direction (laid-paper canvas,
oxidised-copper accent, Fraunces / Inter / JetBrains Mono, tabular
numerals, Czech locale formatting).
- **Browse**: filter sidebar (district typeahead, disposition multi-toggle,
  dual-handle price + area sliders, tri-state status, last-seen-within,
  has-balcony/lift/parking) → Map / Stats tabs (the Table tab went 2026-10-20). Filter and
  sort state in URL params; bookmarkable, refresh-survives.
- **Listing detail** (`/listing/:sreality_id`): hero, mini-map, key
  facts, snapshot timeline strip (the product's signature visual
  vocabulary), per-snapshot diff table, freshness check log,
  outbound link to sreality.cz.
- **Region**: district multiselect or radius-from-pin definition; live
  aggregates (count, p25/median/p75 price + price/m², per-disposition
  median table), 90-day active-per-day chart, 12-week new-listings bar,
  median time-on-market for delisted listings.
- **Health**: operator dashboard. Last-scrape recency (with
  36-hour stale banner), active count + Δ vs 7 days ago, new-listings
  14-day chart, snapshot-density buckets, freshness checks 24h by
  outcome, fetch-failures table. Per-scrape audit added in
  migration 086 (`scrape_runs` + `recent_scrape_runs` /
  `image_storage_overview` RPCs) — Recharts time-series of
  scraped-new / inactive / images-stored over the last 14 days,
  expandable per-run table broken out by category pair, image-mirror
  progress (stored / total), and a static cron schedule card.
- Migrations 011 (`browse_stats`), 012 (`region_stats` +
  `region_active_by_day`), 013 (`health_summary`), 014 (`browse_stats`
  inactive-only filter), 021 (`region_stats` `ppm2_box` extension).
- Browse-2 add-ons (done): `LocationSearchBox` + Mapy.cz suggest /
  resolve proxy (`/maps/suggest`, `/maps/resolve`),
  `DispositionBoxPlots` on the Region page.

### Phase U1b: Estimation backend (done)
- `estimation_runs` table (migration 010): persistent record of every
  estimation, regardless of trigger. Schema reserves `mode='agent'`
  and `status='pending'/'running'` for U4 without forcing today's
  code to write twice.
- `scraper.url_parser`: turns a sreality URL into a parsed spec by
  reusing `scraper.parser`. (Retired 2026-10-20,
  `docs/design/new-property-from-link.md` §3.5.)
- `/estimations` endpoints: POST creates a run (URL or spec), GET-by-id
  reads one, GET lists with filters and pagination.
- Trace format v1: tool calls + computations recorded with
  `output_summary` only (full data in dedicated columns).

### Phase estimation-4: Generic URL parser (retired)
- Retired 2026-10-20 — `docs/design/new-property-from-link.md`: a pasted link becomes an ad
  through `POST /listings/ingest`, and an estimation is always of an ad we hold.

### Phase U2: Estimation flow (done)
End-to-end browser flow over the U1b backend.
- `/estimate`: two-step form (paste URL or pick listing → review and
  edit spec → submit). Pre-fills from `/estimations/preview`; on
  submit POSTs `CreateEstimationIn` to the FastAPI service. URL-origin
  runs send `url` + a minimal `spec_overrides` diff so the server
  records the original `input_url` for traceability.
- `/estimations`: list view of past runs with source/status filters,
  URL-state-driven pagination, links to detail.
- `/estimation/:id`: complete display — rent range strip,
  confidence/source pills, warnings block (failed runs render
  `error_message` and a truncated trace, no range), input recap,
  trace timeline, comparables table sorted by data age, re-run
  button (POSTs new run with `parent_run_id` set).
- `Timeline` component: dispatches on `step.kind` via a renderer
  map (`tool_call` / `computation` / `reasoning`). Today renders
  the deterministic 4-step trace; the same component will render
  the U4 agent's longer traces without rework. Smart default
  expansion (last step + steps over 500 ms).

### browse-2: Region search + box plots (done)
- Mapy.cz suggest / resolve proxy endpoints (`api/maps.py`) — bearer-
  gated, 5-min in-process TTL cache on suggest, admin_boundaries-aware
  polygon resolution that auto-degrades to point + radius when the
  table is missing or empty.
- Region page rebuilt around a single `LocationSearchBox`: typing a
  street address, neighbourhood, or kraj returns ranked Mapy.cz
  suggestions and resolves to either a polygon (when admin_boundaries
  ships) or a point + radius. Browse-1's district / radius pickers
  remain reachable under an "Advanced" disclosure for legacy
  bookmarks and direct radius drag-and-drop.
- Per-disposition price-per-m² box plots (custom SVG) replace
  browse-1's median-only summary table. Tukey 1.5×IQR whiskers
  clipped to min/max, copper median line, no outlier dots, no
  per-disposition colour-coding. A numeric table beneath the SVG
  preserves precise readouts.
- Migration 021 (`021_region_stats_box.sql`) extends `region_stats`
  with a per-disposition `ppm2_box` field; existing fields preserved
  for backwards compatibility.

### Phase estimation-5: URL-parser frontend (retired)
- Retired 2026-10-20 — `docs/design/new-property-from-link.md`: a pasted link becomes an ad
  through `POST /listings/ingest`, and an estimation is always of an ad we hold.

### Phase U-BV: Browse velocity, card badges, filter overhaul (done)
- Migration 052 promotes "turned in" (TOM = days on market) to a
  first-class column on `listings_public`. Same definition as
  `toolkit/velocity._tom_days`: `now() - first_seen_at` for active
  rows, `last_seen_at - first_seen_at` for delisted. SQL and Python
  now share one authoritative computation.
- Migration 053 redoes `browse_stats` with a new filter surface:
  `tom_days_min/max`, `last_seen_min/max_days` and
  `first_seen_min/max_days` (both replacing the old preset
  `seen_within_days_filter`), `building_type_filter text[]`.
  Implicit `active_only=true` default dropped — Browse no longer
  hides delisted listings unless asked.
- Toolkit `ComparableFilters` grows the same six filter fields
  (`tom_days_min/max`, `last_seen_min/max_days`,
  `first_seen_min/max_days`) and flips defaults so no implicit
  freshness gate fires. The deterministic estimator's
  `_DEFAULT_ACTIVE_ONLY` and per-kind `max_age_days` are gone with
  it. Velocity logic is unchanged; the new filter fields flow
  through `_shared_filter_where` for free.
- API: `FindComparablesIn`, `EstimateYieldIn`,
  `ComputeMarketVelocityIn`, `DescribeNeighborhoodIn`, and
  `CreateEstimationIn` all grow the six new optional filters; the
  deterministic `_build_filters` plumbs them through. Agent's
  `base_filters` carry them per-run without per-tool schema bloat.
- Frontend: `ListingFilters` adds `tomDaysMin/Max`,
  `lastSeenMinDays/MaxDays`, `firstSeenMinDays/MaxDays`,
  `buildingMaterial`. `applyFilters` plumbs the days-ago ranges
  against `last_seen_at` / `first_seen_at` and the TOM range against
  `tom_days`. The four-bucket Building material picker (Cihla /
  Panel / Smíšená / Ostatní) maps "Ostatní" to the five remaining
  sreality values. Default `status` is now `'any'`.
- Filter panel regrouped: Category / Location / Disposition / Price /
  Size / Status & velocity / Building / Amenities / Curation.
  ControlGroup legend bumped (0.82rem, ink-primary, semibold) so it
  visually outweighs the smaller Section labels (0.62rem,
  ink-tertiary). Redundant inner labels dropped on singleton groups.
- Browse cards now stack four metadata badges down the right margin:
  status (sage `Aktivní` / brick `Neaktivní`), first-seen (`od 5. 5.`),
  last-seen (`viděno 8. 5.`), and the copper TOM pill
  (`94 dní`, Czech plural). Re-uses the existing token palette and
  borders-only depth strategy; no new design tokens.
- Migration 061 enriches `browse_stats` with a
  `price_quartile_velocity` field: the filtered cohort is split into
  four equal-size price buckets via `ntile(4)` and each bucket reports
  its `tom_days` distribution alongside its price range. Stacks on
  top of 060's expanded signature — DROP-then-CREATE because the
  function body grows a new CTE; the parameter list is unchanged from
  060. The Stats tab renders this as a fourth Card ("Turnover by
  price quartile") with horizontal box plots reusing the
  `DispositionBoxPlots` SVG idiom. Active vs. delisted semantics of
  the per-bucket TOM follow the user's status filter — no per-bucket
  active/inactive split is computed.
- Migration 062 adds `mean` to each bucket's `tom_box` so the
  price/velocity signal isn't lost when `tom_days` is integer-clumped.
  With a 14-day scrape window the five-number summary collapses to
  identical medians across all four buckets even though means differ
  monotonically (active 2+kk byt/pronajem: 8.9 / 9.0 / 9.4 / 9.9 days).
  Frontend renders the mean as a copper dot on the box plot and a new
  MEAN column in the numeric table; the caption now names the
  integer-flooring caveat explicitly.
- Migration 063 replaces the four-equal-bucket
  `price_quartile_velocity` with a seven-band percentile split
  `price_band_velocity`: p0–p10, p10–p25, p25–p45, p45–p55, p55–p75,
  p75–p90, p90–p100. Narrower bands at the tails and around the
  median, wider through the body, so the chart surfaces tail-vs-body
  differences that an equal-quartile split would mask. The new
  payload also reports `pct_share` per band (actual share of priced
  cohort, since ties at percentile cuts make bucket sizes drift from
  their nominal 10/15/20/10/20/15/10). Active 2+kk byt/pronajem shows
  the body bands clustered at mean ≈ 9.2d while the priciest decile
  jumps to 10.5d. Frontend rewrite: `PriceQuartileVelocity` →
  `PriceBandVelocity`, seven rows on the y-axis with percentile +
  price-range + n + share labels; Card heading and caption updated
  accordingly.

### Phase U2.5: Freshness write-path (done)
- "Ověřit aktuálnost" (Verify freshness) button on Listing Detail's
  freshness-checks section. Calls the bearer-token-gated
  `POST /tools/verify_listing_freshness` on demand via the existing
  `request()` auth path (no new auth mechanism) — `max_age_hours: 0`
  so an operator click always forces a real re-fetch rather than the
  throttle's `cached` short-circuit.
- `verifyListingFreshness` wrapper + `VerifyFreshnessResult` /
  `FreshnessOutcome` types in `frontend/src/lib/api.ts`.
- Pending state ("Ověřuji…" / "Re-fetching the listing from the
  source…") and a result line that maps the outcome
  (`unchanged` / `updated` / `gone` / `fetch_error` / `cached`) to a
  human message + the existing `OutcomeChip`. On success it
  invalidates the `listing`, `snapshots`, and `freshness` queries so
  the timeline strip and the check log refetch immediately.
- The audit log table (`listing_freshness_checks`) and the wrapped
  `scraper.freshness.freshness_check` already existed from Phase 2.5;
  this phase added only the frontend affordance. Backed by a
  `FreshnessBlock` component test (the full live e2e needs production
  secrets).

### Phase U-RM: Unified Browse read model (done)
- Implements `docs/design/browse-read-model.md` (PRs #707/#708 aftermath;
  operator decisions 2026-07-07: default sort → `first_seen_at DESC`,
  list freshness 5 min).
- `browse_projection` (migration 276) — the ONE place the Browse column
  contract + publication-gate predicate live. `browse_list` (UNLOGGED
  snapshot, every active property incl. coordinate-less; lean 5-index
  set) + `properties_map_mv` both rebuild FROM it via SECURITY DEFINER
  functions on pg_cron (`*/5` list, `7,37` map — migration 277);
  `scripts/refresh_map_mv.py` + its workflow retired. Rebuild stamps in
  `browse_read_model_state(_public)`.
- Frontend cards/table/count/no-price re-pointed to `browse_list`;
  `fetchBrowseCount` flipped to EXACT-FIRST (index-only ~200 ms cold
  market-wide; `count=planned` was the fallback until 2026-10-16, exact only since);
  `DEFAULT_SORT` → `first_seen_at DESC`. Stats RPC re-pointed
  (migration 278, body-only) — cards/table/count/stats read ONE
  snapshot and cannot disagree.
- Guardrails: gate-wrap pinned on `browse_projection`; rebuild
  invariants (ANALYZE-before-swap, pg_notify); read-contract test
  (registry + CARD/TABLE/MAP_COLS + SORTABLE_FIELDS ⊆ projection).
- Deferred: live-table index retirement after a ≥7-day
  pg_stat_user_indexes observation window (operator OK required);
  row-comparison keyset cursor (PostgREST can't emit it yet).
- W1 of ONE PIN: map pins + tile counts read `browse_list`; the rebuild
  re-patches identity changes (migration 630).
- W2 of ONE PIN: `properties_map_mv` retired with its rebuild, cron job,
  registry row and `properties_map_visible`, plus the two dead MF
  functions (migration 631).
- W3 of ONE PIN: each property stores its location ad
  (`properties.location_listing_ref_id`: a map point first, then
  determined foreign, then the finest RÚIAN rung, ties by the canonical
  order), written with the canonical ad by the rollup and the singleton
  birth (migration 635; invisible until W4).
- W4 of ONE PIN: every property surface reads the location ad — Browse
  list, map and counts, the property page and phone map, the watchdog,
  notifications, the estimation subject and comparables, the extension
  label; a CHECK keeps both pointers NULL together (migration 636). Pin
  and card name one place: ~10.8k rings become dots.
- ONE PIN follow-up (ruled 2026-10-20): the Browse tab, its empty state
  and the filter summary say "properties" — the count was always
  properties, only the map pill had been renamed by W1.

### Chart axis unification (done)

- One axis vocabulary for every chart: `frontend/src/lib/chartAxis.ts`
  (`timeAxisSpec` / `valueAxisSpec` / `timeLabel`), Prague-pinned and
  unit-tested, replacing four per-chart formatter sets. Time ticks are
  calendar-aligned (hour/day/week/month/year) with span-driven label
  granularity; value ticks are nice-numbered with step-driven decimals,
  so no axis can print the same label twice (listing detail was showing
  "červenec 26" and "3,8 M" twice each).
- Listing detail also gained dated price-change events (exact day, old →
  new, %), dots only where a track was really observed, and a per-track
  change count (portal-to-portal price differences no longer counted as
  price moves).
- Adopted by the listing price chart, Health scrape trend, Costs, and the
  dedup pipeline timeline.

### Deploy-staleness resilience: the lazyChunk chokepoint (done)

- **The defect.** Clicking a pipeline card through to Listing Detail could
  flash a full-page "This page hit an error" screen showing
  `TypeError: Cannot read properties of undefined (reading 'default')`, then
  load correctly. Cause was the app's own recovery code: `main.tsx` listened
  for `vite:preloadError` and called `event.preventDefault()`, which is Vite's
  "I handled it" signal — the failed `import()` then RESOLVED to `undefined`
  and React's `lazy` initializer dereferenced `.default` on it. The reload the
  handler scheduled always worked; it just let React paint a crash first.
- **The fix.** `frontend/src/lib/lazyChunk.ts` is now the single code-splitting
  entry point (all 23 sites; an ESLint rule bans bare `lazy(` elsewhere). A
  failed chunk load returns a never-settling promise so React holds the Suspense
  fallback while the page reloads — no error can paint. Rails: a 60s rate limit
  (not the old one-shot flag, which its own `load` listener cleared), offline
  treated separately from staleness, storage failures favouring recovery.
- **Why it happens at all:** a deploy rotates every hashed chunk name (measured:
  30 of 30 on a one-character change) and Caddy hard-404s the old ones, so any
  tab open across a deploy hits it. Normal for a hashed-asset SPA; only the
  handling was wrong.
- `UserFacingError` (`frontend/src/lib/errors.ts`) + an `ErrorBoundary` branch
  so anticipated failures show plain-language copy and keep the raw text folded
  under "Technical details".

### Deploy hygiene: stop breaking open tabs (done)

- **A page crash no longer takes the nav.** The boundary moved from App level
  (above `Shell`) into `Shell` around the route body; App keeps a still-keyed
  last-resort net for what renders outside it (TopBar, Footer, ToastViewport,
  the Explore-area modal). `Suspense fallback={null}` replaced by a reserved-height
  `Skeleton` where the fallback occupies a visible page region.
- **Workflow docs left the SPA module graph.** `scripts/generate_workflow_docs.py`
  now emits `frontend/public/workflow-docs.json`, fetched by Settings + Health.
  It was a 180 KB `.ts` module that changed on every cron edit, so backend-only
  commits rebuilt the SPA and rotated every chunk hash. Measured after: a
  workflow-docs change rotates **0 of 35** chunks (was ~30 of 30).
- **Caddy no longer caches its own 404s for a year.** The `immutable` header on
  `/assets/*` was applied to misses too, so a stale chunk URL could be
  negatively cached for a year. Now scoped to a matched file, with a
  `handle_errors` block stamping `no-store`. Verified against a real Caddy 2.8.4.
- **Railway watch patterns recorded in `frontend/railway.json`** — they existed
  only in the dashboard, where nothing in Git or CI would notice them being
  widened or lost.

### Version-skew nudge (done)

- On tab focus (throttled to 5 min) the app compares its own entry-script hash
  with the one a fresh `index.html` names; a mismatch shows a sticky toast with
  a Reload button. Build identity comes from the DOM, so there is no build-time
  version plumbing to keep in sync. `pushToast` gained an optional `action`.
- Deliberately an OFFER: `lazyChunk` already makes a stale chunk harmless, so
  there is no case for reloading someone's tab out from under them.
- Every uncertainty reads as "no news" — a false positive is a toast telling the
  operator to reload a tab that is already current.
- **Not built, deliberately:** Vercel-style skew protection (publishing each
  build's assets to a durable archive so old chunk URLs outlive their deploy).
  It is the real structural fix and it was designed in full, but for a
  single-operator SPA the waves above remove the user-visible harm at a fraction
  of the operational surface. Revisit if `limen:chunkEvents` shows this is still
  happening often.

### Link semantics + the route registry (done — W1–W4, program closed)

Reported as "I want to right-click a broker in the žebříček and open it in a
new window". The row was a `<button onClick={navigate(...)}>`, so it offered no
open-in-new-tab, no ctrl/middle-click, no status-bar URL, no copyable address,
and screen readers announced a button rather than a link.

Audited app-wide before fixing (66 affordances confirmed by an adversarial
second reader). The verdict: **the affordance defect is small — four elements —
but the layer under it was not.** Only 20 of the 66 were straightforward
anchor conversions; 24 correctly are not navigation at all, and 7 sit inside
another anchor and must NOT become one.

**Shipped:**
- `frontend/src/lib/routes.ts` — one table of route patterns. `def()` derives
  `build()` from the pattern via react-router's `generatePath`, so the pattern
  is the only authored string and a builder cannot drift from it. `build()`
  percent-encodes params: `generatePath` substitutes RAW (verified against
  6.30.3), so an unencoded `/` in a portal's native id would have silently
  changed a URL's route shape.
- `routes.tsx` CONSUMES the registry (`.childPath` / `.pattern`). The router
  and the registry are one table with two readers, not two tables reconciled by
  a test — 76 hand-typed paths previously had no owner and `routes.tsx` was
  imported by zero tests.
- Four affordances became real anchors: the Brokers leaderboard row (the
  report), the broker name-search result, the Outreach campaign row (which also
  fixes invalid markup — its `<div>` root was inside a `<button>`), and
  AccountMenu's "Sign in".
- `Shell`'s 20 nav entries, `listingUrl.ts` and `runLinks.ts` now read the
  registry. NotFound gained a link home — the host serves the SPA at every
  depth (verified: `/nonexistent` returns HTTP 200), so a drifted URL was a
  dead end with nothing to click.

**Rails:** `lib/routes.test.ts` — every pattern resolves against the real
router and not the `*` catch-all; every destination the router declares has a
registry entry (mutation-proven: an unregistered route is named in the
failure). Plus role-based `getByRole('link')` href assertions on all four
converted surfaces, asserted against `ROUTES.*.build()` so a rename moves the
test instead of leaving it stale-but-green.

**Deliberately NOT done, and why:** the `ListingCards` restructure (a card-wide
`<Link>` forced a `stopPropagation` regime and hosts a nested `<Link>` that is
invalid HTML today — it needs its own risk budget); the accessibility track,
where most of the 66 findings actually live (`Field`'s bare `<label>` steals
the first pill's accessible name, `role="menu"`/`"tab"` without keyboard
contracts, 65 `focus:outline-none` sites); external-link hygiene; and the
remaining ~46 route literals plus the lint ban that closes the door on them.
Those are the next waves, not this one.

**Known gap, stated honestly:** no lint selector can catch the reported bug's
shape. The row's handler was `onClick={() => onOpen(id)}` — the route string
lived in a prop two levels away, so there is nothing for a syntax rule to
match. Anchor-ness is only provable by a test that asserts the rendered role,
which is why the per-surface assertions are the real rail.

**Still open (the operator's likely next want):** the Brokers board's own
filters live in `useState`, not the URL, so the leaderboard view itself is
still unshareable and Back from a broker returns to a reset board.

**North star for the program:** *every in-app destination is a real `<a href>`,
built from the one route registry, carrying the whole state the user asked for —
and a path that cannot resolve fails loudly, never silently.*

**W2 — links carry the whole destination (#1275).**
- `Login` read only `.pathname` from the location `RequireAuth` stashes whole, so
  a logged-out session opening a shared Browse cohort (filters live in the query
  string) or a `#estimations` deep link authenticated and then landed on a bare
  `/browse` — no error, no filters. Both consumers dropped it. Highest-severity
  item in the whole audit, and the cheapest to fix.
- `listingRowPath` gained a second overload: a nullable `property_id` returns
  `RoutePath | null`. `BrokerDetail` had been passing `property_id ?? 0`,
  building `/listing?property=0` — a link that type-checked and 404'd. The
  laundering is now unrepresentable, not merely discouraged.
- New `lib/linkGestures.ts` owns the browser-gesture rules once. MapLibre popups
  render anchors from a raw HTML string, so `<Link>` cannot reach them and a
  plain click did a **full document load**, discarding the bundle, the cache,
  the viewport and the filter cohort. Now delegated on the map container —
  verified in MapLibre's source that it parents every popup to
  `map.getContainer()`.
- `ExploreAreaModal`'s hand-rolled guard deleted: it tested `e.button === 1`
  for middle-click (dead code — that fires `auxclick`) and omitted `altKey`, so
  alt-click was being swallowed.

**W3 — the sweep, and the door closes (#1277).** 47 literals across 25 files now
build from the registry, including `lib/auth.tsx`'s two OAuth/reset redirects,
which are mirrored in Supabase's redirect allowlist — renaming either route
would have broken production sign-in while every test stayed green. Watchdog's
three direct `/estimation/${id}` links now route through `runSurfaceUrl`, so one
run resolves to one surface. Four selectors appended to the single
`no-restricted-syntax` array, scoped to POSITION not content (an API path and an
SPA route are byte-identical, so content matching was never an option). The rail
immediately found 12 sites the manual sweep had missed.

**W4 — the grammar that leaves the SPA (#1278).** A pytest in the unfiltered
`test.yml` pins the three patterns the API and the extension emit. It CALLS the
Python builder rather than diffing strings, and a guard test fails if the
contract ever pins something nothing outside the SPA emits.

**Corrections the waves made to their own audit** — recorded because the audit
reads authoritative and is wrong in these three places: `ListingMap` has ONE
popup anchor, not three; `BuildingChildRun` carries no `input_sreality_id`, so
that site cannot route through `runSurfaceUrl` without widening the wire type;
and `/brokers/:id` is not emitted outside the SPA at all, while `/notifications`
is.

**Still open, in priority order:** the `ListingCards` restructure (a card-wide
`<Link>` forced a `stopPropagation` regime and hosts a nested `<Link>` that is
invalid HTML today, masked because it only renders in the zero-collections empty
state); the accessibility track, where most of the audit's 66 findings live;
`ExploreAreaProvider` surviving a route change (fix must be applied symmetrically
to `NewEstimationProvider`); `mark seen` missing `onAuxClick` on four
notification anchors; the Google OAuth `redirectTo` hardcode, which is the Login
bug's defect class through a full-page round trip; and making the Brokers board's
own filters URL-addressable, which is what would let an operator share a
leaderboard view rather than only open one broker per tab.

### Interactive semantics (in progress — W0 + W1 up as stacked drafts #1281 / #1282)

The successor to the link-semantics program: that one fixed *destinations*;
this one fixes the *elements*. Audited first (eight clusters, each
adversarially re-read): **62 defect classes across ~388 sites**, 16 high.
They are not 388 mistakes — six missing primitives multiplied by their call
sites, one structural mistake on the Browse card, and one CSS token.

**North star:** *every interactive element is the right element — correct role,
named by the words the user sees, keyboard-reachable with a visible focus ring,
never nested inside another interactive — and when the view changes, focus
lands somewhere sensible. Each property comes from one shared primitive and is
proved by a rendered-role test, never inferred from a class string.*

**W0 — the rail (#1281).** `src/test/a11y.ts`: `expectNoNestedInteractive`,
`expectRovingGroup`, each shown failing on its defect and passing the correct
shape; proven on `PipelineStageMenu`, the tree's one honest APG menu.
**Decision reversed by measurement:** the design review wanted `axe-core` as
the program's one new dependency, on the claim that its `nested-interactive`
rule catches the Browse card generically. Probed in this jsdom: it fires for a
`<button>`/`role="button"` wrapping a control and *never* for an `<a>` wrapping
one — ARIA's `link` role is not children-presentational, so the rule's scope
excludes exactly the flagship case. Installed, measured, uninstalled; the
ten-line query in `a11y.ts` does see it. No new dependency.

**W1 — one Field primitive (#1282).** Six copies of "a caption over a control"
collapsed onto `Field` in `controls.tsx`; `as="group"` (role + aria-labelledby)
is the default, `as="control"` the sanctioned single-child `<label>` wrap.
Three of the six had wrapped pill GROUPS in `<label>`: the first pill inherited
the whole caption as its name and **clicking the caption selected it** —
clicking "Nabídka" chose "Prodej". `Section` (18 sidebar sites, not the 53 the
review counted) is now an alias and finally carries a group role. `Segmented`,
duplicated verbatim in Brokers and Outreach, moved beside it. The new
`label:has(button)` lint ban found a **seventh** site live (`Settings.tsx`, a
Save button inside its input's label). Rail mutation-proven: 8 tests fail if
the group form becomes a `<label>` again. Visible change for review: five
pages' captions converge on Section's typography.

**W2 — every form control is named by the words the user sees (merged).**
The review counted 63 nameless controls; eight area sweeps with adversarial
recounts found **148**. Fixed per shape: 40 `Field as="control"` wraps, 39
widget `label` props (LocationTypeahead was mounted six times and named at
zero; SingleSelectDropdown's `placeholder` only ever rendered as the blank
option's text), 33 `aria-labelledby` to existing captions, 14 icon-only
buttons, 11 decorative spans un-polluting an ancestor (the unread dot made a
notification row's *name* change when read), 9 `useId`s. Three year/month
pickers collapsed into one that names both selects; two dead components
deleted rather than named. Eight isolated worktrees produced the diffs, each
adversarially reviewed; six review findings were acted on (a placeholder trim
that broke a test, a state folded into a name, 2N caption rows per Outreach
card, a layout change on the Brokers search box, stale hunks, a required prop
with no caller). New lint rail: a nameless `input|textarea|select` outside a
`<label>` / `Field as="control"` fails lint — 50 → 0. 15 test files added.

**W3 — focus is visible (merged).** 63 controls had opted out of the global
`:focus-visible` ring with `focus:outline-none`, 32 of them swapping it for a
border that computes to 1.18:1. All deleted, mechanically and reported per
file (32 source files, +79/−63); the surviving real indicators normalised to
`focus-visible:`. A first pass with a generic whitespace tidy collapsed
indentation across 136 files and was reverted before commit. Unconditional lint
ban on the literal; a source-level test guards the global rule the 63 now rely
on. **Operator ask, not smuggled:** `--color-focus` composites to 1.86:1 in
light mode (the base palette) — under WCAG 2.4.11's 3:1; dark passes at 3.16:1.
Raising it is a `globals.css` `@theme` change reserved for you; no conformance
claim is made until it lands.

**W4 — focus has a destination (merged).** `<main id="main" tabIndex={-1}>`
is now both the skip link's target (first Tab stop on every page; WCAG 2.4.1
was unmet) and where `useRouteFocus` lands keyboard focus after every
navigation — keyed on pathname only, so a Browse filter click never hijacks
focus, and skipping the first render so a deep link is not stolen.
`isSafeInternalPath` extracted and hardened with the backslash arm the inline
check lacked; it is what the OAuth deep-link PR will gate `next=` on.

**W5 — the Browse card is a link on its title, not an anchor around eight
controls (merged).** The audit's one structural mistake. Stretched-link
pattern: a non-interactive card, ONE `<Link>` on the title (so the link's name
IS the listing), an unlayered `::after` expanding its hit area, every control a
DOM sibling raised on the `--z-card-action` rung; the estimate corner is a real
`<Link>`, the collection popover is portalled, merge mode has a real checkbox.
The whole `preventDefault`/`stopPropagation` regime is deleted. Built by an
isolated agent, adversarially reviewed; six review findings fixed rather than
carried (keyboard-orphaned popover, `aria-haspopup="true"` = menu, popover
below modals at z-40, six dead tooltips, lost whole-card focus ring, a
hard-coded z rung). Trades stated: no drag-select over the `::after`; the title
link is the 4th tab stop per card. 5 → 21 tests in the card's file.

**W6a — one Dialog primitive, proved on two pilots (merged).**
`lib/useDialog` (module-level layer stack, ONE window Escape listener, top
layer only, Tab containment, mount-only initial focus, focus restore, a
ref-counted scroll lock) + `components/Dialog` (role/aria-modal on the PANEL,
backdrop `role="presentation"`, naming a type union, portalled with the
reasons stated) + `useCloseOnNavigation`. Pilots ExploreArea/ExploreBroker.
The first cut was **rejected** by its reviewer: layers ordered by effect push
order, which is child-before-parent within a commit, so a same-commit nested
pair inverted "top" and the inner became un-closable. Rebuilt on a
render-phase sequence; then three more re-review findings fixed by hand (trap
yanking focus out of a popover opened from a modal; initial focus in the
outer layer; a backwards first-paint claim). Rails: `expectDialogContract`,
a same-commit pair, an outer remount, a portalled companion. `role="dialog"`
banned outside the primitive; the ten unmigrated modals carry per-line
exemptions naming W6b.

**Two follow-ups the W6a live check found (merged, #1302 / #1303).** The
anchored popover took focus on mount while still `visibility: hidden` (React
flushes the first commit's passive effects before a layout-effect setState;
Chromium refuses to focus a hidden element, jsdom does not care) — focus-in is
now keyed on the panel's position. And Tab past a one-control popover's edge
tabbed straight out to the portal's neighbour at the end of `<body>` — it now
closes the panel and continues from the trigger (APG disclosure). Both were
invisible to jsdom and caught only by the real-browser pass.

**W6b — the eleven remaining dialogs on the primitive (merged).** Four
disjoint clusters, each built in an isolated worktree and adversarially
re-read. Eight run on `<Dialog>` (NewEstimation, CreateWatchdog, PresetSave,
CityPicker, TaxonomyManage, ImageTagDetail, TagDeleteConfirm, Comparable); two
keep bespoke chrome over `useDialog` (RunDetail, ImageLightbox — the census
`dialogChrome.test.ts` proves the import behind every exemption); one was
reclassified (TagEditPopover announced `role="dialog"` with no modality at all
and is now a named AnchoredPopover). Behaviour kept on purpose: the
NewEstimation in-flight guard (one `requestClose`; the glyph DISABLED, not
dimmed), select-on-open for the two name fields, the destructive confirm
opening on Cancel. The reviewers' notes converged on the primitive and were
fixed there once: `initialFocus` inside the one guarded effect (five
hand-rolled `[]` focus effects deleted) plus a default that never opens on the
close glyph; a viewport-safe panel with two overflow LONGHANDS (three
migrations had bet on Tailwind's emission order for `overflow-y-auto` vs
`overflow-hidden`); `DialogClose` takes `disabled` and a `tone` instead of a
competing `text-*` class. Also: Escape in the taxonomy rename field no longer
closes the whole modal (one `preventDefault` + a test); CityPicker's six
buttons say `type="button"` so the fix does not rest on the portal; a
popover-in-dialog Escape test pins the `document`-vs-`window` listener
pairing. Chrome the operator will SEE: every panel centred, `shadow-2xl`, a
2px backdrop blur; the CurationBlock add-tag dropdown learned the
`[data-transient-layer]` guard (the one out-of-cluster edit, forced by
portalling). Zero W6b lint exemptions remain.

**Remaining waves, in the review's order, W2–W6 done:** W2 composite widgets
name their internals (63 controls with no name from any source); W3 focus is
visible (65 `focus:outline-none`, 33 with a 1.18:1 replacement — a deletion
wave, plus the `--color-focus` token which is operator territory); W4 focus has
a destination (route change + the three modal providers above `<Outlet>`); W5
the Browse card stops being an anchor around eight controls (the highest-value,
highest-risk change; stretched-link pattern; needs a real-browser pass); W6
one Dialog primitive over the 13 hand-rolled modals; W7 delete the decorative
ARIA and build one real Combobox (a role swap + ~19 test rewrites in one
2400-line file, not a deletion); W8 the five mouse-only `<tr>`/`<th>` surfaces;
W9 one `ExternalLink` and the middle-click gesture gap.

**Named adjacent, not folded in:** the Google OAuth `redirectTo` drop (needs a
Supabase dashboard allowlist step and shares that field with password reset —
own PR, early); `javascript:` scheme safety on five interpolated `href`s
(security, not a11y); the forms-and-tables tier (aria-describedby/invalid,
table header association) which Field *enables*; Brokers' URL-addressable
filters (a feature); MapLibre string-built popups (permanently unguarded by any
JSX rail — stated, not pretended); no screen reader has been run against the
app at any point.

### Phase U-Nav: Unified browse → detail navigation (next)

Today the top nav exposes `Listing` and (historically) `Estimate` as
top-level destinations. That conflates two distinct UX roles:
**list pages** (Browse, Estimations, Collections) are entry points,
**detail pages** (Listing, Estimation, Building, Collection item) are
drill-downs from those lists. The standalone `/listing` entry with no
id resolves to an empty shell and the entry only exists to satisfy
the menu link — a tell that the IA is wrong. This phase collapses
detail pages back into their parent flows and adds an explicit
"where am I, how do I get back" affordance.

**Scope:**
- **Remove from menu:** `Listing` link (currently `frontend/src/components/Shell.tsx:10`)
  and the `path: 'listing'` (no-id) route in `frontend/src/routes.tsx`.
  `Estimate` is already gone — the modal-trigger CTA in the top bar
  replaces it. Menu becomes: Browse, Region, Estimations, Collections,
  Health.
- **Detail pages stay reachable only via drill-down** from their
  parent list. `/listing/:sreality_id`, `/estimation/:id`,
  `/building/:id`, `/collection/:id` are unchanged as URLs; they
  just no longer have a nav entry.
- **Breadcrumbs + back affordance** on every detail page. Renders the
  parent context (e.g. "Browse / Praha 6 — 2+kk apartments / Listing
  detail") and preserves the parent's filter/sort/page state on
  click. Mechanism: when a list-page link navigates to a detail, it
  stashes the current URL (with all query params) into router
  state; the breadcrumb's "back" link reads from that state and
  falls back to the bare list URL if state is missing
  (deep-link / refresh case).
- **URL hierarchy** — pick one of the patterns below during the
  design kickoff. Not a foregone conclusion which is right for this
  app; documenting the trade-offs so the operator can choose.

**Proposed UX patterns (to discuss before any code lands):**

1. **Breadcrumb + flat URLs (recommended starting point).**
   Keep today's flat routes (`/browse`, `/listing/:id`) and add a
   breadcrumb strip + a sticky "← Back to results" link that
   restores the parent's filter state from router state. Pattern
   used by Airbnb, Zillow, GitHub's issue/PR detail pages.
   Cheap to ship, no migration of bookmarked URLs, breadcrumb
   degrades gracefully on deep-link / refresh (still shows
   "Browse" as parent, just without the specific filter context).
2. **Nested URLs reflecting drill-down.**
   `/browse/listing/:sreality_id`, `/estimations/:id`. The URL is
   the breadcrumb — back button = strip last segment. Pattern used
   by Linear, Notion, most file-tree UIs. Stronger sense of
   hierarchy; downside is the same listing reached from Collections
   would live at `/collections/:cid/listing/:sreality_id`, forcing
   either route duplication or a canonical-detail-URL convention.
   Bookmark migration needed (redirects from `/listing/:id`).
3. **Detail-as-overlay (modal / side sheet).**
   Clicking a listing in the Browse table opens an overlay over the
   filtered list rather than navigating away. Pattern used by Gmail,
   Linear's command-K previews, Booking.com's room picker. Best
   when users browse → preview → browse repeatedly. Trade-off: the
   detail loses real-estate, deep-linking requires a parallel
   full-page route anyway, and the snapshot timeline (the product's
   signature element) wants the full page.

Realistic combo: pattern 1 across the board, with pattern 3 as a
future enhancement on Browse → Listing once the breadcrumb is in
place and we know which detail interactions stay shallow.

**Out of scope for this phase:** changing the snapshot timeline,
adding new detail-page content, restyling list pages. Pure IA +
navigation work.

### Phase U-CAP: Exhaustive-read contract + versioned db-max-rows (done)
The 2026-08 cap-drift audit: PostgREST's `db-max-rows` clamp shipped two real
silent-truncation bugs at Supabase's 1,000 default (city-index popup, rent-map
choropleth), was then lifted OUT-OF-BAND in the dashboard for the 50k-point
map — stranding two generations of wrong comments (".range bypasses the cap"
never true; "hard-capped at 1,000" stale) and leaving ~15 fetch-all sites'
correctness resting on an invisible setting. Verified empirically against prod
(wire probes as the smoke-test user), then fixed as two independent layers:
- **`lib/fetchAllRows.ts`** — the one exhaustive read: complete-or-throw
  paging that terminates on the EMPTY page advancing by rows received (correct
  under ANY cap value, including one below the page size), a verified-unique
  key tuple per site (checked against live data), dedupe across page
  boundaries, and a loud `expectMax` overflow error instead of a partial set.
  All 15 fetch-all sites converted (both hand-rolled pagers deleted; the
  nearest-term casualty was `fetchTrainingLabelCounts`, 1,198 rows growing
  daily under a silent `.limit(2000)`). ESLint bans `.range()` outside the
  helper.
- **Migration 394** — `pgrst.db_max_rows = '50000'` (= `MAP_CAP`, the largest
  sanctioned single request) pinned on the `authenticator` role in git, so a
  project restore / Supabase preview branch can't silently revert the cap to
  1,000. Live-verified: 60k ask → exactly 50k; map single-shot unaffected.
- Reported, not fixed: prefilter id-lists ride back on GET `.in()` (~1–2k-id
  URL ceiling, fails loud, server-side predicate is the strategic fix when
  cohorts grow); `fetchBrokerListings` shows ≤500 of a 20k-listing mega-broker
  (brokers rebuild owns it).

### Broker explore map (done, 2026-09-05)
"Explore this broker's listings" on `/brokers/:id`. NOT a new map component — a
Browse cohort scope: `ListingFilters.brokerId`, resolved over the bearer-gated
`GET /brokers/{id}/listing-ids` (`toolkit.brokers.broker_listing_ids`, geom-only,
cap 50k + `capped`) into a `BrowsePrefilters.brokerListingIds` allowlist, and an
`ExploreBrokerModal` (sibling of `ExploreAreaModal`, provider in Shell) mounting
`<BrowseExperience>` with two new feature flags, `sidebar:false` + `stats:false`.
Seeds from the page's own Typ/Nabídka pills (state lifted to the page). Chosen over
a bespoke `BrokerMap` because that would be the 4th hand-rolled MapLibre
clustering/hover copy; broker data is dark to anon/authenticated PostgREST
(migration 299 A6), so the scope is an id prefilter like `pipeline`, never a
column on the public views. Listing-grain by construction: `isBrokerScoped` is
OR'd into `isListingGrain`, which now drives the relation swap AND the map's
point-lane guard (`tests/test_browse_map_read_contract.py` pins both, and now
checks every `applyPrefilters` id space by pair, not by column). Entry-only:
no sidebar control; `?broker=` round-trips; a "Broker scope applied ×" chip in
FilterSummary is the way out on `/browse`. Outside preset identity; listed as
unmonitored for watchdogs.
- Reported, not fixed: the broker allowlist rides the same GET `.in()` as tags /
  pipeline, so the ~1–2k-id URL ceiling above applies — the largest real broker
  (~1.9k active properties, more listings) can trip it, loudly (map error, not a
  silent subset); the server-side predicate is the strategic fix for all of them.
  `broker_listings()` ≤500/2000 truncation behind the Inventory table (see U-CAP);
  a shared `PointsMap` primitive (ListingMap + ComparablesMap + DatasetMap still
  duplicate the layer setup); a Watchdog broker dimension; Stats-tab parity (the
  modal hides Stats instead, mirroring the portal filter's accepted gap). WebGL
  page-crash isolation for the 6 map mount sites was declined again this sprint.
- Also surfaced, untouched: the agent tool schema advertises 10 comparables
  filters `_FCR_OVERRIDE_FIELDS` drops; `api/main.py:_build_comparables_inputs`
  drops `tom_days_*`/`*_seen_*_days` that `estimation_runs._build_filters` passes;
  5 registry ids falsely claim `Agenda.WATCHDOG`.

### Phase U3: Toolkit-backed views (later)
Surfacing `describe_neighborhood`, `find_distribution_outliers`, and
the velocity tools through the UI. Auth-gated; specific shape decided
when U1 + U2 are live.

