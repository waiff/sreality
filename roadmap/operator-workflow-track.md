> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

## Operator workflow track (parallel)

User-facing features that don't fit the analytical, estimation, UI,
map, or scraper tracks. Operator-scoped (single shared identity, no
per-user accounts — matches today's bearer-token model).

### Phase U2.6: Collections + tags + notes (done)
Operator watchlists, freeform coloured tags, and per-listing journal
notes — end-to-end.
- Migrations 022 (`collections` + `collection_listings`), 023
  (`listing_notes`), 024 (`tags` + `listing_tags`, palette pinned
  to eight named colours by CHECK), 025 (`*_public` views +
  `listings_with_tags(tag_ids)` RPC with AND-semantics, capped at
  5000 rows).
- API: `api/curation.py` exposes CRUD over `/collections`,
  `/listings/{id}/notes`, and `/tags`; routes wired in `api/main.py`
  around line 612+. All bearer-gated per CLAUDE.md toolkit rule #8.
  Tag colour mirrored in `api/schemas.TagColor` (eight-name Literal).
- Frontend:
  - `/collections` index with inline new-collection form, listing
    counts, soft-delete with confirm.
  - `/collection/:id` detail with rename/description edit, delete,
    and a slim member-listings table reusing the Browse/ListingTable
    visual language (sreality_id link, district / disposition / area
    / price / last seen / status / added_at + remove button).
  - `ListingDetail` gains a `CurationBlock` sitting between
    KeyFactsBlock and TimestampsBlock: every collection rendered as a
    toggle (✓/+ chip), tag chips with an autocomplete picker that
    can create a new tag inline (eight-colour palette), and a
    collapsing notes journal (textarea + chronological list).
  - Browse `Filters.tsx` Curation group exposes a tags facet —
    AND-semantics, delegates to the `listings_with_tags` RPC.
- New tokens: `--color-tag-{copper,sage,brick,ochre,slate,plum,teal,sand}`
  + `-soft` pair, scoped at the bottom of `globals.css` per the
  "new tokens by domain-name" rule; the four pre-existing semantic
  colours alias their global token, the four new ones (slate, plum,
  teal, sand) ship with new swatches and light/dark variants.
- Future hook (out of scope, but the schema supports it): the agent
  reads collections as seed examples — "estimate this listing using
  only comparables from collection X."
- Follow-ups landed:
  - Migration 033 adds `tag_ids bigint[]` to `browse_stats` with the
    same AND-semantics as `listings_with_tags`. The Browse Stats
    tab now agrees with Map / Table when the operator filters by
    tag.
  - `PATCH /tags/{tag_id}` (`api/curation.update_tag` +
    `api/schemas.UpdateTagIn`) supports in-place rename + recolour;
    listing attachments are preserved because `listing_tags` joins
    by `tag_id`, not by name. A shared `TagEditPopover` wires
    rename / recolour / delete into both tag pickers — the
    CurationBlock matches list and the Browse Filters "Add" rows.

### Phase U2.6b: Curation made dedup-stable — property grain (done)
Re-keyed all operator curation from listing grain (`sreality_id`) to
PROPERTY grain (`property_id`) so a tag / collection membership / note
describes the real-world property and survives the daily dedup engine,
and built the unified operator-state reconciler that the multi-portal
pipeline (Phase U-PIPE) plugs into next.
- Migration 202 (`collection_properties`, `property_tags`,
  `property_notes(+origin_listing_id)` + `*_public` views +
  `properties_with_tags(tag_ids)` RPC), 203 (retire the listing-grain
  tables; repoint `browse_stats_properties`' tag clause to
  `property_tags`), 204 (one-time repair of watchdog dispatches
  orphaned onto merged_away properties — **pending operator approval to
  apply**, destructive data migration). Curation tables were empty in
  prod, so the re-key needed no backfill.
- `toolkit/operator_state.py` — `OPERATOR_STATE_TABLES` registry +
  `carry_operator_state_on_merge`, called inside `merge_properties`
  (the single merge chokepoint). On merge it re-points every
  property-anchored operator-state row (collections, tags, notes, AND
  `notification_dispatches`) onto the survivor (SET tables union with
  collision-collapse; APPEND moves all), so nothing orphans onto a
  merged_away property — invariant by construction. Unmerge/split are
  best-effort (state stays on the surviving/anchor property). Adding a
  future property-anchored operator-state table = one registry line.
- API re-keyed to property grain: `/collections/{id}/properties`,
  `/properties/{id}/tags`, `/properties/{id}/notes`. Frontend Browse
  tag filter + CurationBlock + CollectionDetail operate on
  `property_id`. Design validated by adversarial red-team before build
  (CLAUDE.md rule #18, #16).

### Phase U-PIPE Phase 0: Deal pipeline — bookmark MVP (done)
A Trello-style deal pipeline over properties. Phase 0 ships the schema + the
bookmark entry point; the kanban board and stage moves are the next phases.
- Migration 205: `pipeline_stages` (TABLE not enum — operator-curatable; seeded
  Zájem[entry] / Prohlídka / Nabídka / Koupeno[terminal] / Zamítnuto[terminal]),
  `property_pipeline` (PK property_id — single-valued; stage_id, board_position,
  note, entered_stage_at), append-only `property_pipeline_events` ledger, +
  `pipeline_stages_public` / `property_pipeline_public` anon views.
- "Bookmark / interested" == the entry stage (presence of a card), not a flag.
- Merge reconciler `toolkit/pipeline_identity.reconcile_pipeline_on_merge` runs
  in the `merge_properties` chokepoint alongside the curation carry (rule #22):
  keeps the most-advanced stage on the survivor, logs the dropped card. Best-
  effort unmerge/split today; lossless replay + terminal-aware policy deferred.
- API `api/pipeline.py`: `POST/DELETE /pipeline/cards`, `GET /pipeline/stages`.
- Frontend: a bookmark toggle (★/☆ + stage label) in CurationBlock on listing
  detail; membership read from `property_pipeline_public`.
- Next: kanban board + drag stage-moves (Phase 1); lossless unmerge + terminal
  policy (Phase 2); Browse-card bookmark icons; stage management UI.

### Phase U-PIPE Phase 1: Kanban board + stage moves (done)
The `/pipeline` board itself — columns per stage, cards per property, move a
card between stages.
- `PATCH /pipeline/cards/{property_id}` ({stage_id, board_position?}) — moves a
  card; a stage change stamps `entered_stage_at` and logs a `moved` event, a
  pure within-stage reorder logs nothing (`api/pipeline.move_card`).
- `Pipeline.tsx` (`/pipeline`, in the Shell nav): columns from
  `pipeline_stages_public`, cards from `property_pipeline_public` hydrated
  against `properties_public` (batched join by property_id); each card links to
  its representative listing + a per-card stage picker that PATCHes the move.
- Move UX is a stage-picker dropdown for now; drag-and-drop (`@dnd-kit`, already
  a dep) is a deferred progressive enhancement.
- Next: Phase 2 = lossless unmerge replay + terminal-aware merge policy;
  Browse-card bookmark icons; operator stage-management UI (rename/reorder/add).

### Phase U-PIPE Phase 2: Lossless unmerge + terminal-aware merge (done)
Hardens the merge reconciler against the cases the design red-team flagged.
- TERMINAL-AWARE merge keep (`reconcile_pipeline_on_merge`): a live (non-
  terminal) stage always beats a closed/terminal one, so merging an active deal
  into a `lost`/`won` property never buries the live deal; within the same
  terminality, higher position wins (tie → updated_at). No new schema — uses
  `pipeline_stages.is_terminal`.
- LOSSLESS unmerge (`reconcile_pipeline_on_unmerge`, wired into `unmerge_group`):
  the merge now snapshots BOTH sides' pre-merge cards to `property_pipeline_events`;
  on unmerge the reactivated retired property's card is restored from its
  snapshot, and in the move-if-empty case the survivor's absorbed card is dropped
  so it isn't duplicated. The survivor's own stage is left as-is (chained-merge-
  safe best-effort — documented).
- Verified on temp tables via MCP (active Offer not buried by Lost; Offer wins
  over Viewing). Hermetic SQL-shape tests for both reconcilers + the unmerge
  integration. CLAUDE.md rule #22 updated.
- Next: Browse-card bookmark icons; operator stage-management UI; drag-and-drop.

### Phase U-PIPE Phase 3a: Browse-card bookmark icons (done)
A ★/☆ bookmark toggle on every Browse card (Map/Table cards view, top-left of
the photo; hidden in merge-mode where that slot holds the select checkbox).
Toggling adds/removes the property at the pipeline entry stage via
`POST/DELETE /pipeline/cards`. Membership is read once per page from a shared
`fetchPipelineMemberSet()` (React Query dedupes `pipelineKeys.members` across all
cards → one query), and the toggle invalidates it. Lets the operator bookmark
straight from the search results, not just the listing-detail page.

### Phase U-PIPE Phase 3b: Operator stage-management UI (done)
The operator now curates the kanban columns from the board itself — no migration
to add/rename/reorder a stage (the curated-index precedent; rule #22).
- API stage CRUD (`api/pipeline.py`): `POST /pipeline/stages` (create — the `key`
  slug derived server-side from the label via `_slugify`, deduped), `PATCH
  /pipeline/stages/{id}` (rename / recolor / retag terminal / crown-entry), `POST
  /pipeline/stages/reorder` (rewrite left-to-right; `ordered_ids` must be exactly
  the live set), `DELETE /pipeline/stages/{id}` (soft-archive via `archived_at`).
- Two invariants enforced in the handler, not just the DB: a stage can't be both
  entry and terminal; `is_entry` may only be SET (re-home the single-entry crown
  by crowning another, never un-crown the only one). Archive is refused (409) for
  the entry stage or a stage still holding cards (FK `ON DELETE RESTRICT`).
- Frontend: a "Spravovat fáze" panel on `/pipeline` (`StageManager` /
  `StageEditorRow` in `Pipeline.tsx`) — per-row rename (save on blur), color
  picker (the 8 `TAG_COLORS`), terminal checkbox, ★ entry crown, ▲▼ reorder, ✕
  archive, plus an add-stage row. Mutations invalidate the `['pipeline']` key
  prefix so the board, stages, and membership all refresh.
- Hermetic tests (`tests/api/test_pipeline.py`): route smoke + logic against the
  scripted fake conn (key derivation, append position, entry-crown demotes others,
  entry≠terminal reject, un-crown reject, reorder set-mismatch reject, archive
  entry/with-cards 409, soft-retire empty).

### Phase U-PIPE Phase 3c: Drag-and-drop card moves (done)
The kanban board's "Trello" gesture — drag a card from one stage column to
another (the deal-pipeline feature is now complete end-to-end).
- `@dnd-kit/core` (already a dep): `Board` owns a `DndContext`; each column is a
  `useDroppable`, each card a `useDraggable` with a ⠿ grip handle (`PointerSensor`
  distance:6 so a click on the card's link/select doesn't start a drag;
  `KeyboardSensor` for a11y). A `DragOverlay` renders the card ghost mid-drag.
- The board owns ONE optimistic move mutation (card jumps instantly, rolls back on
  error, reconciles on settle); drag-end AND the per-card `<select>` both call it.
  The `<select>` is **kept as the keyboard/accessible fallback**, not removed.
- Drag→move resolution is the pure, exported `planMove(activeId, overId, cards)`
  (same column / dropped-outside-a-column / unknown card → no-op), unit-tested
  directly + a board render/select-move smoke test (`Pipeline.test.tsx`, 7 cases)
  — jsdom can't faithfully simulate the pointer drag, so the bug-prone resolution
  is tested as a pure function. CLAUDE.md rule #22 updated.

### Phase U-PIPE Phase 3d: Card-surface polish + unified colour picker (done)
Operator-feedback refinements once the pipeline was in real use. Two bugs first:
the bookmark was hoisted from a buried CurationBlock row to the listing-detail
header action bar (`PipelineToggle`, next to "New estimation") — the operator
couldn't find it; and the drag fly-back was killed (`DragOverlay dropAnimation={null}`).
Then six refinements:
- **Unified colour picker.** Extracted `<TagColorPicker>` (the swatch grid that was
  inline-duplicated in the filter-preset save modal, the two tag pickers, AND the
  stage editor) into ONE shared component; all four now render it. The stage colour
  is no longer a native `<select>` of colour names.
- **Pipeline mark.** Added `components/icons.tsx` (first reusable SVG-icon module;
  repo has no icon library by design). The "Přidat do pipeline" ★ became a shared
  icon on BOTH the header toggle and the Browse cards. (The glyph was a funnel here;
  superseded by a horizontal filter/sliders glyph in 3e — funnels read ambiguously.)
- **Card trash + confirm.** Each kanban card carries a trash → inline two-step
  confirm → optimistic remove-from-pipeline (the app's destructive-action pattern).
- **(i) hints.** The stage-editor entry-star and "konec" (terminal) controls carry
  `<InfoIcon>` (i) hints (native `title=`).
- **Stage `<select>` removed** from the card; stage moves are drag-only (keyboard via
  `KeyboardSensor`). Rule #22 + the test updated for the removed select.
- **Richer card.** Cards now show a thumbnail + street + MF gross yield (image via
  the shared `fetchImagesByListingIds` + `imageSrc()` Browse helpers; street + yield
  off `properties_public`). Broker name + hover contact landed in 3e.

### Phase U-PIPE Phase 3e: Card broker + filter icon (done)
Closes the two items 3d deferred / the operator flagged.
- **Broker on the card** — the **canonical resolved broker** (not the drift-prone raw
  `properties_public.broker_*`), fetched **batched** (no N+1) via two new anon reads in
  `lib/brokers.ts`: `fetchListingBrokersByIds` (`listing_broker_public` → name + firm +
  `broker_id`) + `fetchBrokersByIds` (`brokers_public` → primary email/phone). Folded into
  `fetchPipelineBoard`; the card shows the name linking to `/brokers/{id}` (like the rest
  of the app) with a native-title hover carrying firm + phone + email. NULL-safe (private
  bazos sellers have no resolved broker → line omitted). No new endpoint, no migration —
  both views were already anon-readable.
- **Pipeline mark — a funnel.** The shared pipeline glyph is a **funnel with three arrows
  feeding into it** (`FunnelIcon`; filled body = in-pipeline / entry), the deal-funnel
  metaphor. (It briefly used a horizontal filter / sliders glyph — the funnel had read
  ambiguously — but the operator chose a clear funnel-with-arrows mark instead, so it's
  back.) Applied on every pipeline surface — header toggle, Browse cards, the stage-manager
  entry-stage indicator, AND the Chrome-extension panel (reproduced by value in vanilla TS)
  — so the "into the pipeline" concept reads as one icon everywhere. Rule #22 updated.

### Phase U-PIPE Phase 3f: Board property-type filter (done)
Basic filtering of the kanban board by property type (`category_main`).
- Multi-select chips (Byty / Domy / Komerční / Pozemky / Ostatní) above the board;
  empty = all, client-side filter (the board is small). Only the types actually
  present in the pipeline get a chip; the chip row hides entirely with <2 types.
- The chip labels come from the **same generated filter registry as Browse's TYPE
  tabs** (`FILTER_REGISTRY` `category_main` enum_values) — no parallel hardcode.
- `category_main` added to `fetchPipelineBoard`'s `properties_public` select +
  `PipelineBoardCard` (the column was already on the view). `Pipeline.test.tsx`
  gains a filter case. Rule #22 updated.

### Phase U-ME: Manual rental estimates (next)

Capture operator-judgement rent figures as first-class data and
make them visible to both humans (a panel on Listing Detail) and
the agent (a new toolkit tool, consulted by
`rental_estimator_v1` before `record_estimate`). Bridges the gap
between an operator's broker quote / portfolio benchmark / gut
number and the agent's defensible distribution — today that
private signal only lives in `listing_notes` free text where
neither side can use it as a number.

Shape locked with the operator:
- Point estimate (`rent_czk` integer, CHECK 1000–1000000), not
  a range. Simpler than mirroring the estimator's p25/p75 and
  matches how operators actually write the number down.
- One row per estimate; many per listing. Mutable rows with
  full audit history on UPDATE and DELETE via a trigger (same
  pattern as `app_settings_history` in migration 020).
- Free-text `author` + `source_kind` CHECK ∈
  `broker / gut / external_comp / portfolio / other` + optional
  `notes` (≤4000 chars).

Scope:
- Migration 046: `manual_rental_estimates` (FK
  `sreality_id`, the fields above, `created_at`, `updated_at`,
  `updated_by`) + `manual_rental_estimates_history` (append-only,
  `change_kind` ∈ `update / delete`) + BEFORE UPDATE / AFTER
  DELETE trigger. `manual_rental_estimates_public` view with
  anon select grant (same pattern as `listing_notes_public` in
  migration 025). (Originally drafted as 043; renumbered after
  main's Phase AI slice A claimed slot 043 with
  `043_estimation_trace_payloads.sql`.)
- API: new `api/manual_estimates.py` exposing CRUD over
  `/listings/{id}/manual_estimates` (GET + POST) and
  `/manual_estimates/{id}` (PATCH + DELETE). All bearer-gated
  per CLAUDE.md toolkit rule #8. Pydantic schemas appended to
  `api/schemas.py`.
- Toolkit: `toolkit/manual_estimates.py:get_manual_rental_estimates(conn,
  sreality_id)` returns the standard `{data, metadata}` envelope
  with `data.estimates` (empty list when none exist).
  POST `/tools/get_manual_rental_estimates` route in
  `api/main.py`.
- Agent: handler + `_ToolDef` entry registered in
  `api/agent.py:_build_tool_registry()` so the tool is callable
  by name from the agent loop. Provider-agnostic — no changes
  needed in `api/providers/`.
- Migration 047: `UPDATE skills` for `rental_estimator_v1` *and*
  `rental_estimator_full_v1` (the sibling skill added by main's
  PR #77 — both want the new tool). Appends
  `get_manual_rental_estimates` to `allowed_tools` (idempotent
  guard via `not (allowed_tools @> ...)`), same shape as
  migration 045's `read_floor_plan` add. The `system_prompt`
  update that inserts the "CONSULT MANUAL ESTIMATES" step into
  each skill's instructions is *not* part of this migration —
  per migration 045's precedent, prompt edits are an operator
  action via the Settings UI so we never overwrite hand-edits.
  The on-disk `SKILL.md` files carry the inline numbered step
  as canonical documentation. (Originally drafted as 046; bumped
  alongside the 043 → 046 rename above.)
- Frontend: `ManualEstimatesBlock` slotted into
  `frontend/src/pages/ListingDetail.tsx` after `CurationBlock`
  (manual estimates are operator-curated like tags/notes;
  same shelf is the natural home). Reads via
  `manual_rental_estimates_public` with the anon key; writes go
  through the bearer-gated API endpoints. Wrappers in
  `frontend/src/lib/api.ts` (`listManualEstimates`,
  `createManualEstimate`, `updateManualEstimate`,
  `deleteManualEstimate`). No design-token changes.

Out of scope for this phase:
- Manual estimates on sales / commercial listings (the field
  is named `rent_czk` and CHECK-bounded; a future migration
  generalises).
- The agent's ad-hoc Python code execution capability — that's
  Phase 7d above, deferred.

### Phase U2.7: New-listing notifications — in-app slice (shipped)

In-app slice landed: saved-filter "Watchdog" surface in the SPA, a
background matcher loop in the FastAPI service, and per-row
estimation kickoff that runs deterministically in the background and
surfaces the yield once it lands. Email / SMS / push remain deferred
(see open questions below). Cron cadence is still nightly; the
operator can call `POST /notifications/matcher/run` from the UI's
"Run matcher now" button to trigger an immediate evaluation against
any newly-scraped listings.

**What shipped**

- Schema: migrations `056_notification_subscriptions.sql`,
  `057_notification_dispatches.sql`, `058_notifications_app_settings.sql`.
  Dispatches carry a nullable `estimation_run_id` FK so the
  operator-triggered yield calculation links back to the
  estimation row that lives on the existing `/estimation/:id` page.
- Backend: `api/notifications.py` owns the `WatchdogFilterSpec`
  Pydantic model, the SQL-clause renderer (mirrors
  `_shared_filter_where` semantics), and the matcher loop spawned via
  FastAPI's lifespan context manager. `api/routes/notifications.py`
  exposes the standard bearer-gated CRUD + dispatch endpoints. The
  matcher reads its cadence and the watermark from `app_settings`
  rows seeded by 058 so the operator can tune both without a
  redeploy.
- Frontend: new `Watchdog` nav tab, `/watchdog` feed page,
  `/watchdog/manage` list, `/watchdog/new` and `/watchdog/:id/edit`
  filter editor. Notification rows expose the listing, disposition,
  price, when it fired, the watchdog name, an "estimation" column
  that streams the yield once the background task completes, and a
  per-row "Run estimation" button.

**What's deferred**

- Email / SMS / push channels. `notification_dispatches.channel` is
  CHECK-bounded to `'in_app'` only; a future migration adds the new
  enum values and the dispatch worker grows a fan-out branch.
- 5-minute scrape cadence (Shape A from the original proposal).
  Today's nightly cron still applies; the matcher loop honestly
  surfaces "no fresh listings" between scrapes. A new
  `.github/workflows/scrape_probe.yml` is a separate slice.
- Per-user identity (one shared operator stays the model).

**Original brief (kept below as the design rationale)**



Two cross-cutting pieces have to land together: a notification
backend + UI for managing subscriptions, and a scraper cadence
change so the underlying data refreshes more often than nightly.

**Notification surface**

- Migration: `notification_subscriptions` (one row per saved filter
  spec, columns mirroring the Browse filter sidebar — district /
  disposition / price range / area range / has-balcony / has-parking
  / category_main / category_type / tag_ids, plus `is_active`,
  `name`, `created_at`, `updated_at`). One operator identity today
  so no `user_id` column yet — see open questions below.
- Migration: `notification_dispatches(subscription_id, sreality_id,
  dispatched_at, channel, status, error_message)` — append-only
  audit + dedup guard so a (subscription, listing) pair never
  re-fires even if the matcher re-runs. **Cross-link to Dedup
  track Phase D1:** once D1 ships, the dedup key changes from
  `sreality_id` to the canonical `listing_id`, so a property
  surfaced on multiple portals fires one notification rather than
  one-per-portal. This is a single-column rename on
  `notification_dispatches`; no functional change to the dispatch
  worker beyond reading from the canonical row.
- API: new `/notifications/*` routes (CRUD on subscriptions, list of
  recent dispatches, manual "test send" for a subscription). Bearer-
  gated; browser writes flow through here, never direct Postgres.
- Frontend `/notifications` page: list / create / edit / delete
  subscriptions, reusing the Browse `Filters.tsx` components so the
  filter spec stays canonical across surfaces. A "matches today"
  counter per subscription drives intuition before the operator
  enables alerts.
- Listing Detail gets a "notify on listings like this" affordance
  that pre-fills a new subscription from the listing's facets.

**Dispatch worker**

- New scheduled job (GitHub Actions cron, or Railway scheduled
  function — pick alongside the cadence decision below). Every run:
  1. Find listings inserted into `listings` since the previous
     successful dispatch run. Driven by `listings.first_seen_at` (or
     `created_at` if cleaner). Anti-join against
     `notification_dispatches` to skip anything already fired.
  2. For each active subscription, run the filter spec against that
     window. Reuse `_shared_filter_where` so the matcher and Browse
     can never disagree on what a filter means.
  3. Fan out emails (one message per (subscription, listing) match,
     or one digest per subscription per run — pick during scope
     review). Write a row to `notification_dispatches` per send.
- Email provider: one of SendGrid / Postmark / Mailgun / SES (see
  open questions). Provider credentials are env-only, never
  inlined into the browser bundle. Architectural rule #1 (append-
  only migrations), #2 (snapshot-on-change), #3 (no deletes),
  #4 (last_seen_at semantics) all preserved — this feature is
  read-mostly over the listings tables and writes only to the new
  notification tables.

**Scraper cadence change (cross-cutting, required)**

Current nightly cron surfaces new listings ~24h late, which makes
the alert feature feel useless. Operator proposal: run the scraper
every five minutes. Naive translation of the six-category nightly
walk to a 5-min cron is too aggressive — 288 full runs/day would
hammer sreality and blow the GitHub Actions minute budget. Two
viable shapes to choose between:

- **Shape A — light "new-listings probe":** a new entry point that
  walks only the first 1-2 index pages per category sorted by
  newest, no detail refetch of existing listings, no
  `mark_inactive` call (architectural rule #3 already forbids
  inferring inactivity from a partial walk — the existing
  `mark_inactive` skip-when-`--limit` branch lights up here). The
  full nightly walk stays untouched, preserving snapshot density
  and inactive bookkeeping. Recommended default.
- **Shape B — lower-footprint full walk on a tighter cron:** keep
  one cron, drop per-run cost, accept that inactive inference still
  only runs in the nightly job. Higher risk of rate-limiting and
  minute-budget pressure; only worth doing if shape A leaves
  meaningful new listings undetected.

Both shapes preserve the snapshot-on-change discipline (rule #2)
and the is_active-after-complete-walk rule (rule #3). Both reuse
the existing `listing_fetch_failures` queue so a probe that fails
to fetch a fresh listing doesn't drop it on the floor.

**Open questions (operator to decide before B1-equivalent work
starts)**

- **Channels.** Start with email only, or include SMS / push from
  the outset? Email-first is the assumption above.
- **Email provider.** SendGrid, Postmark, Mailgun, or AWS SES?
  Affects pricing model, env-var surface, and template tooling. No
  current dependency, so this is a fresh pick — same discipline as
  CLAUDE.md's "no new dependencies without justification" rule.
- **Cadence.** Is 5 minutes the firm target, or is 15-30 minutes
  acceptable? Lower cadence relaxes rate-limit and minute-budget
  pressure. Affects shape A vs. shape B above.
- **Per-user identity.** Today's model is one shared operator
  (`API_TOKEN` bearer, shared `anon` key). Multi-recipient
  notifications are the first real argument for opening per-user
  accounts — explicitly out of scope today. Default for this phase:
  stay single-operator, send all alerts to one configured address
  in env. Reopen identity work as a separate phase if a second
  recipient is needed.
- **Digest vs. per-listing.** One email per match (chatty, fast) or
  one digest per subscription per run (quieter, slight latency
  cost)? Affects `notification_dispatches` shape — current schema
  draft supports both.

**Out of scope for this phase**

- Per-user accounts / authentication (one shared operator stays the
  identity model; see open question above).
- SMS / push notifications (email-first).
- "AI-curated" alerts where the Phase 7 agent picks listings the
  operator might like — that's a later layer on top of this
  scaffolding.
- Re-notification on snapshot change (price drop, status change).
  Listed as a "next" follow-up once new-listing alerts ship.


### Phase U-COSTS: LLM spend dashboard (done)

- `/costs` operator page (nav: Settings cluster): KPI tiles (today / 7 d /
  30 d / projected month / calls / **errors**), a stacked daily-spend chart
  by feature (entity-stable civic-archive tag colors), a per-feature 7 d/30 d
  breakdown table (calls, errors, avg per call, share), and a per-model split.
- Backed by `llm_cost_daily_public` (migration 280) — per-day × called_for ×
  provider × model aggregates over the `llm_calls` audit table; anon reads
  the aggregate only. Error counts surface failed (unbilled) calls, the
  credit-outage / vision-error tripwire at spend grain.
