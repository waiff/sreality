> Track file — part of [ROADMAP.md](../ROADMAP.md). After shipping, edit only this file + its index row.

## Operator workflow track (parallel)

User-facing features that don't fit the analytical, estimation, UI,
map, or scraper tracks. Operator-scoped (single shared identity, no
per-user accounts — matches today's bearer-token model) *(superseded:
per-user Supabase Auth is live — CLAUDE.md § out of scope)*.

### Phase U-NOTIFY-PUSH: Notifications push program (approved 2026-10-21)

Program `docs/design/notifications-push/PROGRAM.md` (audit `AUDIT.md`, F1–F28; eleven rulings taken 2026-10-21). North
star: one real-world change becomes one immutable row, owned by one account, shown in one feed and pushed to that
account's iPhones (the installed PWA). Channels: the in-app feed, iPhone push, email (kept, inactive); Telegram is out.
- [x] **W1 (merged #1944 + live 2026-10-22; migration 651)** — subtract the dead and the dangerous: Estimate rent and "Run matcher
      now" (R10); the Email/Telegram picker, Settings › Delivery, Telegram, the outbox, ChannelClient, the Resend
      transport, webhook and unsubscribe (R1, R5); the duplicate /watchdog feed and the System tab (R3); system alerts
      leave the bell for Health + the canary e-mail (R2b, R9a; migration 651).
- [x] **W2a (merged #1946 + live 2026-10-22 08:46Z; migration 652; the 48 h overlap gate runs until 2026-10-24)** — the worker's `notifications` lane
      (300 s; steps watchdog_new / price / monitor, each its own transaction and `lane/notifications/<step>` cell) runs
      the producers beside the API's matcher: the cursor and lookback go (24 h from `active_since`), one canonical-ad
      price fragment (7 days), 'inactive' after 24 h gone (R6 a); keys byte-identical (render test), `event_ref` +
      `actor_account_id` written; shares become `shared_collection | shared_dashboard | shared_pipeline` (migration 652:
      the five columns, the `active_since` trigger, the wider CHECK, backfills). Gate: cells green, the 9-preset replay,
      EXPLAIN < 2 s, the false-new baseline.
- [x] **W3 (merged #1949 + live 2026-10-22; migration 653)** — one event identity:
      `notification_dispatches_identity_uq` over `EVENT_IDENTITY` (NULLS NOT DISTINCT, partial on `duplicate_of IS
      NULL`; migration 653 = Migration C: 652's fills since A, system history keyed on its old key, exact repeats
      marked, `dedupe_key` nullable, the feed index, the index CONCURRENTLY); every producer and the share grant
      conflict on it and write no `dedupe_key` / `trigger_snapshot_id`; ONE `VISIBLE_SQL` (repeat, own-account
      dismissal, retired watchdog) on every read, count and update (mark-seen of a hidden row: 404); deleting a
      watchdog retires it (F21); the carrier is one UPDATE that re-points and marks `duplicate_of`, deleting nothing
      (F6, F13); `share_ref` reads columns, no `split_part`. Gate: CI green; a merged 'new' row is marked and never
      re-fires, 0 deleted; a retired watchdog's alerts hidden, rows kept; 0 identity violations.
- [x] **W4 backend (merged #1950 + live 2026-10-22; migration 654; dark until the VAPID key)** — push plumbing,
      dark: migration 654 = Migration D (`push_subscriptions`, `notification_deliveries`, service role only);
      `api/push.py` (RFC 8291 aes128gcm + RFC 8292 VAPID, the RFC vector byte for byte); `GET /push/key`, `PUT`/`DELETE
      /push/subscription`, `POST /push/opened` (never act); the Czech renderer; the worker's `push` lane (15 s; fan-out
      with the one-share-a-day cap R11, a 2-min leased claim, one push per device, a 429 holds the device); the
      `notifications` Health check (stall, recall, failures, unconfigured). **Operator:** generate `VAPID_PRIVATE_KEY`
      with the toolkit-api one-liner, paste it as a Railway Shared Variable on the API AND the realtime-worker, delete the
      file. Gate: CI-Postgres lease/claim/429 tests green; lane `unconfigured`, then `live_devices` 0; `/push/key` 503
      then 200. The SPA half (sw.js, the switch, `/n/:dispatchId`) is #1948.
- [x] **W5a (merged #1947 + live 2026-10-22)** — the phone surfaces that need no push plumbing
      (R4, F9): one unread observer in Shell for both frames and the page (`lib/unreadCount`), the app-icon badge
      from it, the phone header's bell beside Účet, and /notifications at phone width (one page, `max-md:`).
- [x] **W4-fe + W5b (merged #1948 + live 2026-10-22 10:51Z)** — sw.js `push`
      (always one notification, then the badge) and `notificationclick` (focus + navigate, else open) with a vitest
      harness over every payload shape; the landing `/n/:dispatchId` (mark-seen → `dispatchTarget`, `?d=` → `/push/opened`);
      `lib/push` + `usePush` and the sheet's switch "Oznámení na tomto iPhonu"; re-sync on sign-in and the hourly
      resume; binding follows identity (F18). Owed: the on-device proof (W5 gate, the operator's iPhone).
- [ ] **W2b (built 2026-10-21, stacked on W4; held until W2a has had 48 h of overlap in production; migration 655 applied
      first)** — one clock in the worker: `matcher_loop` goes with its lease, three interval readers, three thread
      wrappers and two daily gates; the API's lifespan starts the health teller alone, under `HEALTH_TELLER_DISABLED`
      (was `NOTIFICATIONS_MATCHER_DISABLED`, no alias); migration 655 = Migration B (`health_liveness()` without its
      `api` lease cell, an open `api` incident recovers); the jobs check reads a registered lane missing from the
      heartbeat as Watch, then Problem. Gate: Railway API + worker success, no matcher log lines; over 7 days inactive
      and reactivated ~60% down, new ≥ the replay. For W6: the lease table and six settings rows are read by nothing.
- [ ] **Next:** W2b merges after the 48 h overlap (migration 655 first) · W6 the one destructive prune, backup
      first (R8; drops `dedupe_key`, its UNIQUE, `trigger_snapshot_id`, `notification_matcher_lease` and the matcher's
      settings rows) · the on-device proof of W5 (your iPhone, a second account; after the VAPID key).

### Phase U-NEW-PROPERTY: a property from a pasted ad link (in progress, 2026-10-20)

Spec `docs/design/new-property-from-link.md` (PR #1913, approved 2026-10-20, six rulings in). North star: a
pasted link is an ordinary ad — one identity rule, one fetch path (the drain), one write, one birth; the sites
we do not crawl are the tenth portal `web`.
- [ ] **W1 `feature/link-ingest`** — `scraper/portal_identity.py` (the one URL→(source, native id) canon; the
      seven parsers import it; `tests/fixtures/portal_url_probes.json` pins Python and `portalIds.ts` alike),
      `POST/GET /listings/ingest` (admin; enqueue, never fetch), the maintenance lane at 30 s + births inside the
      daily sweep, `NewPropertyDialog` on Pipeline, Browse and the Odkaz tab, the extension's "Přidat do databáze".
- [ ] **W2 `cleanup/estimation-four`** — estimation = add the ad, then estimate it (`listing_id` in, `url` out);
      the LLM URL parser, both previews, `url_parser`, the orphan machinery and the sreality-only late-binder go.
- [ ] **W3 `feature/web-portal`** — the tenth portal (deterministic parser, text-lane reading, `indexless`
      presence seam, daily walk, contract, broker row, chip "Web"); after #1910.
- [ ] **W4 `cleanup/estimation-four-schema`** — drop `parsed_url_cache`, nine parse columns, two settings, the
      `idnes_reality` portals row (destructive, backup + OK given 2026-10-20; after 643).
### Phase U-MERGE-LETTERS: merge mode as one statement of letters (live 2026-10-21 06:10Z, PR #1926 = 3e6a904d)
- **Done (PR #1926, no migration):** Browse's merge mode marks cards with letters — a letter key marks the focused
  card (the pointer focuses the card it is over; Tab / Shift+Tab move card to card; ← → page the photos),
  a click writes the active letter (A at first, so click-click-merge still makes one group), the same
  letter again clears, Backspace / Delete clear — and ONE click merges every letter in ONE transaction:
  `POST /properties/merge {letters: {property_id: "A"}}`, the split's statement shape and codec, over
  `toolkit.property_identity.merge_letters` (shared with the split's join; every property locked up front
  in id order, each letter its own group; a refusal names the letter and writes nothing); one receipt per
  letter; no ruling crosses letters; the Rulings page merges a pair as a one-letter statement. Rulings
  D1–D6 and the as-built record: `docs/design/merge-letters/PROGRAM.md`.
- **Owed:** the first real multi-letter merge by the operator (the read-only production check of the keys
  passed 2026-10-21: 19 steps over 24 live cards, 0 console errors, nothing merged).

### Phase U-MERGE-FEEDBACK: what Browse shows after a big merge (reported 2026-10-19, investigated 2026-10-20)

The operator merged 26 inactive ads of one flat (Lužická 1681/17, Praha; byt 2+1, 50 m², 10.87 M Kč;
survivor property 813505) and read Browse as if the merge had come undone. Root cause, read-only: the
merge (15:15:29Z) landed inside the 15:15 list rebuild (15:15:00–15:16:01Z), whose swap discarded the
merge's patch, so the 26 retired cards reappeared until the 15:30 rebuild — the patch race ONE PIN W1
closed (migration 630 re-patches identity changes inside the rebuild). The detail page was right all
along. The other "Lužická" cards are at least twelve different flats (prices 2.5–14.5 M, areas 50–88 m²);
the only unmerged twin of the operator's flat is property 983290, one active sreality ad hidden by the
status=inactive filter at merge time. AUTODEDUP confirmed (2026-10-20) that all 28 ads sit in one rt group
(cluster 18836145) and its lane folds 983290 into 813505 at the next re-read under the 12 h freshness rule.
- Open idea (not started): merge mode could name twins outside the current status/portal filter
  ("3 more properties share this address") so a merge made under a narrow filter is not read as broken.

### Phase U-VALUO: a Valuo valuation on the property page (in progress, 2026-10-19)
- **Done (W2, backend):** migration 637 `valuo_valuations` (one row per paid call, service-role only, keyed by ad),
  `GET`/`POST /properties/{id}/valuo` for every signed-in user (the caller's own account; save-before-call, same-request
  reuse, 409 on a running twin, no retry), `api/valuo.py` as the one home of Valuo's vocabulary + the mapping with its
  provenance and the operator's careful defaults, `api/valuo_client.py` (`VALUO_API_TOKEN` wins over
  `VALUO_SANDBOX_API_TOKEN`), `api/http_json.py` folding the two `_http_get_json` copies (the Mapy key no longer reaches
  logs), the admin-claim copies folded into `deps.is_admin`, the server label dicts into `filter_registry.label_cs`,
  and the phone Poloha no longer calls a district point a town. Rationale: `docs/architecture.md` § rule 12.
- **Done (W3, the section, on Valuo v3):** `<section id="valuo">` after Odhady on the property page for every signed-in
  user (`components/listing-detail/ValuoSection.tsx` over `lib/valuo.ts`): the editor flips in place and is drawn from
  the API's `fields` alone — exactly each type's v3 request schema (`api.valuo.com/v1/market-value/{type}`, commercial
  included), the place editable — each value tagged with its provenance (`z inzerátu` / `odvozeno` / `předpoklad` /
  `chybí` / `upraveno`), Valuo's refused parameters under their fields; the result card reads Valuo's confidence band
  (`range_price`) on the ruler against the asking price, our latest run and MF, a reliability line (comparables, reach,
  match; no grade) and the comparables with their photos (one `advert/detail` call, migration 646); a sandbox answer
  is stored `sandbox` whatever the token's name; the history is the estimations' `RunHistory`, generalised; the phone
  shows the latest answer only. The estimate card frame is one `EstimateCard`. Patterns: `.interface-design/system.md`.
- **Settled (production call 2026-10-21, property 1012847):** Valuo's lease figures are MONTHLY (≈50 m² flats at
  17 500–20 000 Kč, avg 18 850 Kč, 377 Kč/m²), so a lease shows `/měs` and the rent basis.
- **Done (review fixes):** Valuo is a named shape-3 site in `tenancy.md` on the one no-account 400; its per-m² figures
  are rule 23's named outside exception (`REGISTERED_SITES`); one error shape (`code`, one `unconfigured`), the location
  reader out of the route module (`api/property_location.py`); a house's `garage_area` / `other_area` are user-set. SPA:
  the editor's controls are `controls.tsx`'s form box (`box=`: one 34 px height, dashed absent, brick refused, the unit
  inside), no local patches; one `useEstimationRuns` query for Odhady and the Valuo mark; both ledgers are `table.tsx`
  `Column`s under `SectionLabel`/`Th`, their data ink-3; Valuo's failures in Czech.
- **Done (W5, 2026-10-21):** "Náš přepočet" — a tick box per comparable (all ticked), a card under Valuo's own with the
  total, the per-m² and its distance from Valuo, "orientační" from 3 kept and a band from 5, "Vrátit výchozí"; browser
  only (`lib/valuo.ts` `recalc`: Valuo's weighted geometric mean, 0.11 % from its own on five real answers), nothing
  stored, desktop only, offered only while the recipe lands within 1 % of Valuo's average. The subject's own ad (matched
  by the portal id over ALL the property's ads, delisted too) is tagged "tento inzerát" and stays ticked; adding
  comparables is not part of it. The unread record/summary names left the API payload (Valuo's answer is stored whole).
- **Next:** one production call confirms whether a house's plot is sent; a per-plan monthly
  quota counted from the rows before the production token; Valuo's own field messages still show as bare English under
  a Czech caption (quote them as the failure card does: `Valuo: „…“`); ONE app-wide Czech translation pass (operator
  ruling): the run ledger's `SUCCESS`/`FAILED` chips, the estimation block's English copy (`ESTIMATES`, the runs ledger's
  `When` / `viewing` / `/mo` — `RunHistory`'s `when`/`viewing` and `PerMonth`'s `unit` go with it) stay English until
  it. **Ruled 2026-10-21 (operator OK):** the Valuo routes answer a caller without an account with the one shared 400,
  English `no account for caller` (it was „Přihlášený uživatel nemá účet, ocenění nelze uložit.“; no one reaches it:
  signup seeds an account, 0 of 5 users lack one), and the sentence joins the app-wide translation pass above.

### Phase U-CURATED-SCOPE: curated-set scope (done, 2026-10-19)
- **Done:** tags, collections and pipeline stages became ONE kind of filter (brief + rulings:
  `docs/design/curated-set-scope.md`): one value (`lib/setScope.ts` `SetScope`: in it / not in it /
  in none of it), one URL grammar (`<set>` + `<set>_excluded`, `=any` on both), one control
  (`CuratedSetFilter`, with "V žádné kolekci" / "Mimo pipeline" / "Bez štítků" and "V pipeline") on
  the Browse sidebar, the phone sheet and the board bar. Membership is judged in the database under
  the caller's RLS (migration 629: nine scope parameters + `hide_dismissed` on the four cohort
  functions), so no property id rides a URL. All three are lenses outside preset identity, reset
  by a preset click (ruling 2026-10-19). One settle (`lib/curationSettle.ts`) drops a card the moment a save,
  funnel, tag or dismiss takes it out of the active lens. Layers A–C (858a5b5b, 91812539,
  1ed93180). Deleted: `tagScope`, `collectionScope`, `PipelineScopePicker`, `TagFilterControl`,
  `CollectionsPicker`, the three client resolvers + the denylist seam, `HIDING_DISMISSED`, the
  board's second codec, the `cohortScoped` / `pipelineScoped` / `collectionScoped` props, the
  registry's `tags_excluded`. Architecture § rules 18 + 22.
- **Done (2026-10-20):** the contract migration 640 dropped `property_ids_excluded` from both
  aggregate RPCs on the operator's word (revert shipped unapplied under `migrations/reverts/`).
- **Next:** whole-collection / tag DELETEs and merges still revalidate broadly instead of settling.

### Phase U-PIPE-SHARE: Pipeline sharing — view or edit another account's board, act as its owner (built, 2026-10-20; merges in the window)
- **Approved (v1.2):** a share row (owner, recipient, mode read|edit); the recipient switches to the owner's
  board in the Pipeline tab and, under an edit share, acts as the owner on curation ("Upravit jako
  {email}"); a read share sees the board only. One identity function for curation, `acting_account_ids()`,
  driven by an `x-act-as` header the database validates; nothing else widens; every delegated edit is
  stamped in `delegated_edits`. Spec + rulings: `docs/design/pipeline-sharing.md`. Two waves.
- **Built:** the spec in #1903; W1 (migration 641 + API + doctrine) in PR #1906; W2 (the SPA, §3.5) in
  PR #1908 — the Pipeline tab's board switcher (desktop + phone), read boards without drag / stage menu /
  trash / stage editor / tags, edit boards acting as the owner (the phone through the stage sheet), the
  one `ShareBlock` on the pipeline and collection pages, the viewer's two-step leave, a revoked board
  resetting with a toast, "Upravit jako {email}" + the in-page banner on the desktop property page
  (sticky per device), the `share` notification row, and the delegated stamps on notes, cards and
  stages (`lib/delegatedStamp`). W1 and W2 merge together inside the autodedup session's window, 641
  applied immediately before (the `collection_share` → `share` rename couples them). Ruling 12
  (`DELETE …?side=`) awaits the operator. **Owed:** the board cannot say "vámi" on a delegate's own
  stamps — the SPA has no source for its own account id (an `own_account_id` on `GET /pipeline/shares`
  would give it one); until then a delegate sees the time alone.

### Phase U-SHARE: Collection sharing, view-only (done, 2026-10-17)
- **Done:** share a collection with another account by email, read-only (spec + rulings:
  `docs/design/collection-sharing.md`; a share is one row that widens READS only, `owner_email` is
  the one "mine vs shared" marker, a note crosses a share only when its author marks it shared).
  Seven waves: W1 #1833 (the write is the gate: one UPDATE/DELETE decides ownership, probes gone),
  W2 #1834 (dead `collections_public`/`tags_public` dropped, `account_id` NOT NULL on collections +
  tags, migrations 607/608), W3 #1835 (three SPA hooks replace 14 copies, the property page's second
  collection editor and the legacy note origin deleted), W4 #1837 (`collection_shares`, migration
  610: party read policies, share/revoke/leave routes, the one share notification, Collection and
  Note gain `owner_email` / `shared_with` / `shared`), W5 #1838 (SPA: Browse picker "Shared with
  me", the Sharing block, viewer pages, foreign notes read-only, the "Share with …" checkbox, the
  Shared tab, phone read-only in Czech), W6 #1839 (Chrome extension: "saved" and the save checklist
  are mine only, foreign notes read-only "od {email}", the "Sdílet s …" checkbox + "Sdíleno" /
  "Nesdílet" toggle on the SPA's audience rule). W7 PR #1840 (migration 613): the operator ruled
  2026-10-17 that a marked note reaches shares made later too, so 610's consent-time anchor
  (`property_notes.shared_at`, its trigger and the policy's time clause) is dropped; the bait-share
  edge is accepted (spec §7, §8 ruling 9). 613 is applied AFTER the merge + Railway rollout.
- **Next (unscheduled):**
  - `property_notes.account_id` NOT NULL — ruled 2026-10-17: NOT NULL after #1829 lands (it
    attributes the one pre-tenancy NULL row); a follow-up migration numbered past 613; the split live test's bare (NULL-account) note must be
    re-modelled first (`tests/test_property_split_live.py`).
  - `WatchdogDispatch.sreality_id` is typed `number` but is null on rows that carry no listing,
    a `share` row among them — widen the type with its readers.
  - pytest order pollution met in W1: `tests/api/test_curation.py`, then
    `tests/test_curation_account_stamping.py`, then `tests/api/test_auth.py` gives 15 × 503 "Auth is
    not configured"; each file passes alone and CI's path order (tests/api first) never hits it —
    find the fixture that leaks the auth state.
  - ~~Share notifications are in-app only: no per-recipient email delivery is built (ruling 5e).~~ Superseded
    2026-10-21: shares are pushed to the recipient's iPhone from W4 (Phase U-NOTIFY-PUSH; ruling 5(e) superseded).

### Phase U-PIPE-SIGNUP: Every account starts on the default board (done, 2026-10-16)
- **Done:** migration 612 — one signup path (account, owner membership, the 10-stage default board with codes from
  `seed_default_pipeline`, the monitoring collection, the 7-day trial), functions born private, the event ledger's stage FKs
  account-qualified. Own PRs, same sprint (merge separately): #1828 the SPA draws an empty board's columns, #1830
  migration 611 (tenant serial ids → identity; apply it BEFORE 612), #1831 the extension resets its caches on an
  identity change; migration 613 drops `legacy_backfill_claim` (operator ruling 2026-10-17).
- **Next (unscheduled):** ~~`notification_dispatches` needs a column-level UPDATE grant (its global `dedupe_key` is
  browser-writable)~~ stale: 619 narrowed the browser's UPDATE to columns and 651 to `seen_at` alone; the global
  `dedupe_key` itself is replaced in W3 (Phase U-NOTIFY-PUSH: nothing writes it since migration 653; W6 drops it). The 60 pre-299 public sequences still grant `authenticated` setval (one revoke migration + a
  census arm).

### Phase U-LINK: Artifact link on the property (done, 2026-10-14)
- **Done:** one operator-typed http(s) URL per property (`property_artifact_links`, migration 599),
  a single-valued sibling of notes: `PUT/DELETE /properties/{id}/artifact-link`, a row between
  tags and notes on the property page and the phone page (`ArtifactLinkRow`), drawn as an anchor
  only when it parses as http(s). Carried across a merge (the survivor's link wins a collision).
  Read as ONE whole set (`GET /artifact-links`, `lib/useArtifactLinks`) — the shape every
  curation fact is read in — so the pipeline board's card and the phone board's card print it as
  a small button (`ArtifactLinkButton`, new tab) for one request, not one per card; the
  per-property GET went with it.
- **Next (unscheduled):** Browse cards and the Chrome-extension panel do not show it.

### Phase U-TAGS: Tags unified, on the pipeline board (done, 2026-10-14)
- **Done:** tags became collections' twin — one member map, `lib/tagScope.ts`, `lib/tagCache.ts`,
  one `TagAssignList` (inline on the property page, a popover on the board card, a sheet on the
  phone card), one `TagChip`, one tri-state `TagFilterControl` (Browse's sidebar + the board's
  bar; `tags` / `tags_excluded`). The board card prints its tags and assigns from them; the board
  and the phone board filter by tag, including "none of". Browse's exclusion rides migration 598's
  generic `property_ids_excluded` on both aggregate RPCs. Deleted: the per-property tag read, the
  prefilter's second read, the property page's private editor, the sidebar's include-only picker,
  the edit popover's stale `listing-tags` invalidation. Architecture § rule 18.
- **Next (unscheduled):**
  - Browse cards and the Chrome-extension panel show no tag and cannot assign one; when they do,
    their writes settle through `settleTagWrite` (the `cohortScoped` knob is gone, U-CURATED-SCOPE).
  - ~~"Not in collection" on Browse~~ — done 2026-10-19 in U-CURATED-SCOPE ("V žádné kolekci",
    judged in SQL by migration 629, not by `property_ids_excluded`).

### Phase U-NOTE-FILES: Attachments on notes (done, 2026-10-07)
- **Done:** files on a property's notes. Drop, paste or pick them into the Notes composer
  (staged until the note is saved) or drop them straight onto a saved note. Images show as
  thumbnails that open full size, other files download, and removal asks first. The
  `property_note_attachments` table is note-grain (migration 592), so the files follow the
  note through merges and a split's note copy takes them. The bytes live in R2 under
  `custom-attachments/note/` and are served by the API, with an extension allowlist that
  admits no active types.
- **Next (unscheduled):**
  - The Chrome-extension panel lists a note's text only, not its files.
  - Nothing deletes R2 objects; a sweep for objects no row names would have to spare the
    ones a split's copy shares.

### Rule #18: an executed no-orphan merge test (done, 2026-10-01)
- **Done:** `tests/test_property_carriers_live.py` (PR 0 #1664, extended by the
  `PROPERTY_CARRIERS` PR) merges and detaches through the public writers, one test per
  carrier over two accounts, and asserts the retired property is left holding nothing; a
  census offline (migrations) and live (replayed schema) fails on any column naming a
  property that is neither carried nor in `NOT_CARRIED`. See "Merge chokepoint deepened"
  below.

### Merge chokepoint deepened: PROPERTY_CARRIERS, properties_changed, detach_listings (in progress, 2026-10)
- **Done:** PR 0 #1664: an executed live baseline per carrier (two strict xfails pin
  the dispatch-send abort and the chained-detach double card). PR 1:
  `toolkit/property_carriers.py` — ONE ordered `PROPERTY_CARRIERS` list replaces the
  `toolkit/operator_state.py` registry (Phase U2.6b below), `toolkit/dismissal_identity.py`
  and the inline asset/pipeline calls; `NOT_CARRIED` names every other property column,
  enforced by a census offline (migrations) and live (replayed schema);
  `merge_properties` became the private `_merge_pair` under the set's one lock and gate;
  typed `CategoryClash`; the `_Ledger` fake raises on SQL it does not model. PR 2:
  `properties_changed` (`scripts/recompute_property_stats.py`) — ONE after-step (scoped
  recompute → Browse patch → broker queue) shared by the merge, the detach and the dirty
  drain (one commit per slice, was three); merges, detaches and splits now
  reach `brokers.property_count` on the broker drain's cadence instead of the daily sweep.
  PR 3: set-shaped `detach_listings` — one lock up front, each advert re-planned in order,
  rulings once (movers vs stayers, never between two movers), one after-step per call;
  the split detaches its movers in ONE call
  (1 + J recomputes for J joined units, was M + J) and its `restore_must_not_link` is proven
  to write nothing (deleted in follow-up F1). PR 4: the dispatch collapse keeps its sends —
  `Dispatches` re-points a collapsed row's `channel_sends` onto the kept twin first, so a
  delivered alert no longer aborts the merge (`channel_sends_check`; live test 3b un-xfailed).
  PR 6: the engine's undo on the set form — `unapply` and `legacy_retire` detach each group in ONE
  `detach_listings` call (the injected `detach=` takes the set); the one-advert adapter is deleted.
- **Next:** a chained detach restores one card (PR 5, draft #1681 — needs an operator decision:
  best-effort card identity inferred from the ledgers, or a stable card id via an additive migration).
  After PR 2 deploys, `dirty_broker_listings` depth steps up after merges (the fix
  working); the `property_sweep` artifact (the `jobs` check's `artifact/property_sweep` cell since
  Health wave 4, which retired `property_sweep_last_complete`) should stay fresh.

### Rule #22: converge the kanban + extension pipeline copies (partly done, 2026-10-01)
- **Done (2026-10-01):** the kanban's move/remove no longer re-implement the hook —
  `pages/Pipeline.tsx` calls `const { move, remove } = usePipelineCard()` and passes the
  `property_id` per call (the hook takes it at call time now), so one instance serves the
  whole board's drops and trashes (see "One optimistic-write hook" under Phase U-PIPE below).
  The kanban's shape (drag to move, own trash + two-step confirm to remove) stays sanctioned.
- **Owed (not built):** the board tints column headers + stage-editor swatches with its own
  `stageColor` (grey fallback) instead of `stageAccent` (copper) — converge, or record the
  grey fallback as deliberate. The extension hand-copies the funnel SVG (forced: no React)
  plus `stageBadge` and `stageAccent` (not forced: pure TS it could import from
  `frontend/src/lib/pipelineStage.ts`, as it already imports `lib/brand`) — import it; since
  Phase U-EXT-INDEX the copies live in ONE file, `chrome-extension/src/glyphs.ts`. Then
  rule #22 (CLAUDE.md + architecture) drops the remaining exceptions (origin: Phase U-PIPE
  3i below).

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
  future property-anchored operator-state table = one registry line
  *(superseded 2026-10-01: one `CurationTable(...)` line in
  `PROPERTY_CARRIERS`, or one adapter for any other shape — rule #18;
  see "Merge chokepoint deepened" above)*.
- API re-keyed to property grain: `/collections/{id}/properties`,
  `/properties/{id}/tags`, `/properties/{id}/notes`. Frontend Browse
  tag filter + CurationBlock + CollectionDetail operate on
  `property_id`. Design validated by adversarial red-team before build
  (CLAUDE.md rule #18, #16).
- **Write-side invariant completed (2026-09-17).** `resolve_active_property_id`
  now runs on the REMOVE/EDIT half of every property-anchored write too:
  `remove_property_from_collection`, `detach_tag`, `update_note`, `delete_note`
  (curation) + `remove_card` / `move_card` (pipeline). 426fa575 had hardened only
  the INSERT half while claiming "every property-anchored write entry", so for
  fourteen months a stale id could CREATE a membership it could never remove —
  the DELETE matched nothing and answered a success-shaped `{"removed": false}`,
  HTTP 200, which no client reads. Rail: `tests/api/test_property_anchored_write_census.py`,
  an AST census over `api/` with a reasoned allowlist (it reproduces all six sites
  on the pre-fix tree, so it is not vacuous). Prod held 0 orphaned curation /
  pipeline rows against 105,513 merged-away properties, so no repair migration.

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
- Move UX is drag-and-drop (`@dnd-kit/core`, column droppables only); the
  stage-picker dropdown it replaced is gone.
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

### Phase U-PIPE Phase 3g: Board sort + card signals (done)
Vertical ordering the operator controls, plus two data signals on the card.
- **Sort** as a fourth row in the existing Stav/Typ/Lokalita chip stack, applied
  WITHIN each column: Ruční pořadí (default) / Přidáno / Ve fázi nejdéle / Cena /
  Změna ceny / Město. Manual order is modelled as a named option over the long-dormant
  `board_position` rather than replaced — the API always accepted it and the
  frontend never wrote it, and its values collide *within* a stage, so the old
  single global `ORDER BY board_position` was non-deterministic between refetches.
  Every comparator tiebreaks on `property_id`. `lib/pipelineSort` + `lib/cardSort`
  (the shared serialization / NULLS-LAST / Czech-collation half; Browse keeps its
  server-side keyset path — different execution models by design).
- **Civic index strip** — four fixed cells (celkové hodnocení, přírůstek obyvatel,
  stěhování mladých, nabídka práce) underscored by a 2px rule in the shared
  city-index scale's band colour, repeating the column header's motif at card
  scale. Joined `properties.obec_id = curated_cities.admin_boundary_id`, an
  integer equi-join that reproduces the SQL predicate's `ST_Covers` arm (all 206
  curated cities have a boundary, so its radius arm is dead). No migration.
  ~52% of properties resolve to a curated city; the rest render no strip.
- **Price movement** — shared `<PriceDelta>` (drop = sage, rise = brick, the
  polarity ListingDetail already used). Renders NOTHING when
  `total_price_change_pct` is NULL (fewer than two observed prices), which is not
  the same claim as "unchanged". Also adopted on Browse cards + table. Sortable
  on the SIGNED percent (ascending = deepest cut first, matching the buyer-board
  polarity); no |abs| "biggest mover" option, and the NULL majority sinks below
  the movers in both directions rather than being read as 0%.
- **URL state** (`lib/pipelineState`) for sort AND the three existing filters, in
  Browse's own param vocabulary — the board's filters were `useState`-only and
  silently reset on reload.
- The card adopts the shared `placePrimary()` (it hand-rolled `[street, district]`,
  printing "okres Beroun" for a village); `BoardCard`/`CardFace` extracted out of
  the 863-line page. Rule #22 unchanged.

### Phase U-PIPE Phase 3h: Pipeline scope on Browse + stage badge (done)
Two halves of one idea — make "which deals am I working" legible and filterable
from Browse, not just from the board.
- **Stage badge.** `pipeline_stages.code` (migration 377) is the short mark the
  funnel renders. The number is operator DATA, not ordering: the live board
  hand-numbers stages inside the labels ("1. For Review" … "9. Passed", "9.
  Bought", "9. Lost"), so three stages share "9" while `position` runs 5/6/7 —
  an ordinal badge would contradict the operator's own labels, and a regex over
  the label would break on the first unnumbered rename. Nullable + NOT unique;
  NULL falls back to the ordinal at render time (`lib/pipelineStage.ts`).
  Backfilled once from the existing label prefixes; the stage editor exposes it
  as a 4-char box whose placeholder is the ordinal.
- **One mark, one accent, one write path.** `<PipelineMark>` (funnel + badge) and
  `stageAccent`/`stageBadge` replace the per-surface copies — the board fell back
  to grey for an uncoloured stage while the listing header fell back to copper.
  `PipelineFunnelButton` now serves the Browse cards AND the Table (which had no
  pipeline affordance at all, quietly breaking rule #22's "every surface"; the Table
  tab itself was removed 2026-10-20), and
  `usePipelineCard` is the single add/remove/move hook + invalidation policy.
  The Chrome-extension panel mirrors the badge and tints its pill by stage, with
  the tag palette added to its by-value mirror of the SPA tokens.
- **Pipeline scope.** `ListingFilters.pipeline` (`?pipeline=any` or `?pipeline=`
  stage ids; registry id `pipeline`, BROWSE agenda only) resolves a property-id
  allowlist from `property_pipeline_public` and AND's it onto the cohort exactly
  like tags / with-estimates, reading through the SAME `fetchPipelineMembers` the
  funnels render from. `browse_stats_properties` gained a generic
  `property_ids_filter` (migration 378) so the Stats tab can't diverge from the
  list — the failure class migration 351 closed.
- **A scope, not a preset.** The chip leads the preset row, but `pipeline` sits in
  `PRESET_EXCLUDED_KEYS`: it is never stored in a preset and never counted in the
  dirty comparison, so toggling it shows no "Update preset" and it survives
  loading one. Per-stage narrowing lives in the sidebar (Curation → Pipeline).
  Watchdogs report it as unsupported — the operator's own state can't be a
  trigger for their own clicks.
- **Follow-up, same day:** the chip shipped as a MODIFIER and that was wrong —
  it AND'd the scope onto Browse's default cohort (byt + pronájem), so an
  operator with 45 deals saw 1. It now loads a VIEW: neutral cohort + scope,
  active preset deselected, one atomic write, Back as the undo. That required
  `category_type` to become nullable ("Vše"), which every backend consumer
  already supported — see architecture rule 21b — and which incidentally fixes
  a control that lied: clicking the selected deal-type pill wrote `deal=null`
  and silently snapped back to `pronajem`.
- Next: nothing queued here; a "days in stage" signal is the obvious follow-up if
  the scope gets heavy use. (Per-stage columns on the Table: moot, the Table tab was
  removed 2026-10-20.)

### Phase U-PIPE Phase 3i: The funnel opens a stage menu (done)
The funnel was a toggle: clicking it on a property already in the pipeline
REMOVED the card — unconfirmed, undoable, on a grid of 60 — and offered no way
to advance a deal without opening the listing page. It now opens a shared menu.
- **Why removal is not a toggle.** `remove_card` DELETEs the row; there is no
  restore path, and re-adding stamps a fresh `added_at`, so an accidental
  remove + re-add silently resets "in pipeline since" and every time-in-stage
  figure the board sorts on. The transition trail survives in
  `property_pipeline_events` but nothing reads it back. The confirm says so and
  points at the terminal stages — closing a deal there KEEPS its record.
  (Operator notes are property-grain, rule #18, and were never at stake;
  `property_pipeline.note` is a separate column with no writer.)
- **One menu, three surfaces.** `PipelineStageMenu` (stage list badged from
  `code`, current stage checked, terminal stages under their own divider, then
  a two-step remove) is opened by the Browse cards, the Browse table rows (gone
  with the Table tab, 2026-10-20) AND the listing header — which drops its own `<select>` + bare ✕. Adding stays
  one click into the entry stage: cheap and reversible. The kanban keeps
  drag-to-move (rule #22) and its existing trash confirm; the Chrome extension
  keeps its `<select>` and gains the same confirm on ✕ via a generic
  `buildConfirmRow` — one destructive-confirm shape for the whole panel.
- **`AnchoredPopover`** — the missing primitive. The funnel sits inside an
  `overflow-hidden` card AND inside the card's `<Link>`, so the app's existing
  `absolute` popovers would be clipped and every click inside one would
  navigate. Portals to `<body>`, fixed coordinates off the anchor rect, flips
  up when it would overflow, closes when the anchor scrolls out of view. No new
  dependency.
- **`lib/pipelineCache.ts`** — one cache policy for every pipeline write, shared
  by `usePipelineCard` and the kanban's own bulk mutations. Two bugs fell out:
  a drag invalidated `board` alone, so every Browse funnel kept badging the
  pre-drag stage; and the board's rollback lived in `onError`, which opts a
  mutation out of the app's global error toast (`main.tsx`) — a failed drag
  snapped back with no explanation. Rollback now rides `onSettled`.
- **One optimistic-write hook** (done, 2026-10-01): `lib/useOptimisticWrite`
  (hold → patch → rollback in `onSettled` → revalidate; per-key `pendingFor`
  from one pending index per MutationCache, so a write re-renders only the
  rows that asked about its key — a Browse hover no longer scans the
  MutationCache once per hook per row) replaces `lib/optimisticCache` and
  every hand-rolled copy — the kanban's own `move`/`remove` (now
  `usePipelineCard` with the id per call), dismissals, border cases, the
  autodedup verdict overlay, the Settings toggles, the training-set marks,
  the preset reorder, the exam-review edits. 23 toast-only `onError`s deleted
  (the global toast already said it); `lib/browseKeys` / `lib/autodedupKeys` /
  `trainingSetKeys` factories feed both readers and sweeps. Side fixes: a
  failed filter-visibility toggle and a failed preset reorder now say why; a
  failed dismissal re-reads the lists and a failed training-set mark its row
  page (no resurrected neighbour, no erased move or note); the funnel and the
  header pill stay busy while their menu writes (`aria-disabled`, never
  `disabled`, so the menu's focus hand-back still lands on them); the no-price
  count is now a Browse surface — merges, splits/links, cohort-scoped
  collection and pipeline writes, and dismissals re-read it (it went stale
  before).
- **Badge projections fixed.** `fetchPipelineStages` never selected `code` and
  `fetchPropertyPipeline` never selected `stage_code`, though migration 377
  exposes both: the same property badged "9" on a card and "5" in its own
  header, and the stage editor rendered every set code as blank.
- Rule #22 updated (CLAUDE.md + architecture).

### Phase U-PIPE Phase 3j: Board geometry — reachable drop zones + card size (done)
Two board-ergonomics fixes, measured in a real browser rather than eyeballed
(jsdom lays nothing out, so a CSS change here is structurally untestable by
vitest — the numbers below come from Playwright against live data).
- **Every stage's drop zone is as tall as the tallest column** (#1399). It was
  `min-h-24` plus its own cards: on a board with 37 cards in one stage, an empty
  stage offered a 96px target pinned to the top of a column thousands of pixels
  tall — scrolled down to the card you wanted to move, there was nothing to drop
  onto. Columns now stretch (`items-stretch`, each column a flex column, the
  card list `grow`), with a floor so a SHORT board keeps a real target. Live
  measurement: 401/140/96/96/96/96/96 px → uniform 401; 96 → 288 with every
  column empty.
- **Three card sizes** (`lib/pipelineCardSize.ts`, one geometry table read by
  the page — a size can never be half-applied). `sm` is the board as it was;
  `md` is the SAME design with the thumbnail at exactly double (3rem → 6rem,
  the doubling convention Browse's `--card-min` already follows); `lg` is a
  DIFFERENT design — the photo leaves the text's row and spans the card 16:10,
  the Browse-card idiom. The drop-zone floor, column width, drag ghost and
  skeleton row all move with the size. Measured: column 288/384/416 px, photo
  48/96/348 px wide, card 130/153/356 px tall.
- The switch is the app's existing segmented control. `ImageSizeToggle`'s pill
  chrome moved into `SizeToggle` (N steps) and `ImageSizeToggle` became a thin
  boolean wrapper, so its five existing call sites (Browse cards, three NEW
  DEDUP grids) are untouched and the board's three-step version cannot drift
  into a second visual idiom for the same gesture.
- Size is a per-browser workspace preference (`usePersistedChoice`, validated on
  read), NOT part of the URL view state: a shared link carries which deals to
  look at, not how this browser likes its photos.

### Phase U-PIPE Phase 3k: Pinned stage headers + address-led card (done)
Two operator asks, measured in a real browser (Playwright, local build, a
98-card fixture board served to the page; nothing written).
- **Stage headers stay on screen while the page scrolls.** A `sticky` header
  inside its column cannot work: the board scrolls sideways, `overflow-x: auto`
  forces `overflow-y` to auto, and a sticky child then pins to the board — which
  never scrolls vertically — instead of the page. The headers now sit in their
  own row (`BoardFrame`) outside the horizontal scroller, `sticky top-14` under
  the top bar, copying the columns' `scrollLeft`; both rows share widths and
  gaps, so their scroll ranges are identical. Measured: pinned at y=56 after a
  1,400px scroll; a 700px sideways wheel leaves both rows at 700 with every
  header 0px off its column; `z-10` stays under the Lokalita dropdown (z-20).
  Each column list now carries its stage as `aria-label`, since the heading is
  no longer its neighbour.
- **The card leads with its address, and the address is the link** (new tab, as
  before); the price is the plain second line. A card whose place is unresolved
  links "Lokalita neurčena" rather than losing its only way to the listing.

### Phase U-DISMISS: Dismiss a property (done)
North star: a dismissal is ONE durable, account-scoped fact about a property
("reviewed, never show it again") with one meaning everywhere — discovery
(Browse, notification feed + delivery) hides it by default, non-destructively
(lift, never delete), enforced server-side once, rendered by one shared
control. Not a special collection (collections are m2m groupings; see
`docs/architecture.md` rule 18). A LIVE deal always wins over a dismissal; a
deal closed into a terminal stage can be dismissed.
- **W1 — store, merge carry, API** (done, #1515): migration 536
  (`property_dismissals` + `property_dismissals_public`), `POST /dismissals`,
  `DELETE /dismissals/{property_id}` (lift), 409 for a piped property,
  `add_card` lifts, `toolkit/dismissal_identity.py` in `merge_properties`.
- **W2 — Browse hides dismissed by default** (done, #1516): migration 537's
  inlinable invoker twins (`browse_list_visible()` & co — functions because
  `browse_list` is blue-green rebuilt) + `hide_dismissed` on the Stats/map
  RPCs; `queries.ts:readSource` is the one SPA seam; `?dismissed=show`
  reveals, outside preset identity; the sidebar counts what the cohort hides.
  Merge mode shares the source (reveal to merge a dismissed duplicate).
- **W3 — notification visibility** (done, #1517): one `_NOT_DISMISSED`
  predicate on every in-app read (feed + total, unread badge, mark-all-seen,
  per-watchdog counts) and an account-scoped twin on both outbox passes;
  detection is untouched (its cursors and the `reactivated` detector depend on
  every dispatch existing), so an undo brings a hidden dispatch back intact.
- **W4 — the shared control** (done, #1518): `<DismissButton>` beside the
  funnel on the Browse card, Table row and listing header over
  `lib/useDismissal` (list-aware optimistic hide, one sticky "Vrátit" toast,
  batched per-property state reads); `lib/optimisticCache.holdQueries` now
  shared with the pipeline cache; absent while the property is piped.
- **W5 — Chrome extension** (done): the panel's "Skrýt" / "Skryto"
  toggle beside the monitoring bell, state as `dismissed` on
  `POST /listings/lookup`, writes on `/dismissals`; the panel mirrors the
  lift when a card is added.
- **W6 — closed deals can be dismissed** (done, 2026-09-21): the rule narrowed
  from "any pipeline card blocks a dismissal" to "a LIVE card does". Under the
  old rule a deal moved to "Passed" stayed in Browse forever with no Skrýt
  button — 44 closed cards (29 Passed, 15 Lost) were stuck, and the 409's own
  advice ("close the deal there instead") hid nothing. One statement,
  `lift_dismissals_of_live_deals`, decides from the data after every write that
  can leave a card live (add, a stage move, a stage re-opened); the merge
  reconciler states the same rule per account. The SPA and the extension read
  liveness off the stage (`is_terminal`). No migration, no backfill: 0 active
  dismissals sat on a carded property.
- **Next (not built):** a dismissal *reason* (a small operator taxonomy — the
  label set a future ranking/scoring model would learn from; `property_notes`
  covers free text today), and an "only dismissed" review lens if the reveal
  proves too coarse for reviewing the pile.

### Phase U-EXT-INDEX: Triage from the portals' search pages (done, 2026-10-03)
Operator ask: file and hide listings straight from a portal's result list, the
way Browse cards allow — and be able to dim what is already hidden on any index
page, not only on flat searches (the overlay drew nothing but the sale-flat
yield badge).
- Every card we have a property for gets the Browse card's cluster — funnel,
  bookmark, eye — with the app's semantics (rule #22: add in one click, then a
  stage menu with removal behind a two-step confirm; rule #18 checklist;
  one-click hide/restore, absent on a live deal). Same routes as the panel; no
  API change, no migration — `POST /listings/lookup` already served all three
  states for every category.
- A bottom-left dock on every index page carries the **Skrýt skryté** switch
  (per browser, synced across tabs): hidden properties stay in the portal's
  grid as barely visible cards instead of being removed.
- Card writes and panel writes repaint each other (property-grain), and a
  sign-in/out in the panel resets the cards.
- Found against live markup: a carousel portal (bezrealitky) links the listing
  once per photo slide, so "nearest card-ish ancestor" drew one set of controls
  per slide — a card is now the outermost `li`/`article` still holding one
  listing; and the controls' stacking level dropped from the maximum to 900 so
  they no longer show through cookie walls and filter drawers.
- **Photo mark (2026-10-11, collections 2026-10-12):** the cluster's small
  glyphs were too quiet to scan a grid by — a card in the pipeline now has its
  photo toned in the stage colour with the stage badge and name over it
  (hovering lifts the tone), and a card in a collection gets a copper rim and
  a tag naming the collection (`chrome-extension/src/index_photo_mark.ts`).
  The photo is found by geometry, not markup; a veiled card, or a card guess
  holding several listings, gets no mark.
- **Sorted cards and ads (2026-10-14):** a saved card is toned like a stage
  (copper, bookmark + collection name) — the copper rim let a property already
  reviewed shine like a new one; and the results grid's non-listing cards
  (paid ads, project promos) are washed whitish (`index_promo.ts`, found by
  structure: card-sized grid children holding no listing link).
- **Tags (2026-10-14):** the cards print the property's tag chips over the
  photo, and the detail panel assigns tags — a chip row under its action bar
  and a checklist of every tag over `POST/DELETE /properties/{id}/tags`.
  `POST /listings/lookup` returns `tag_ids`. Creating and editing tags stay in
  the app.
- **Next (not built):** assigning tags from a search-page card (the panel
  does it; the card cluster has no tag control); verifying the card placement
  on sreality itself (its consent wall blocks an unattended check — idnes and
  bezrealitky were checked against live pages).

### Phase U-EXT-COLL: Every collection from the extension panel (done, #1562, 2026-09-21)
Operator ask: use any collection from the portal page the way the app's
listing page does. The panel could only reach ONE collection — a "Sledovat"
bell hard-wired to the system monitoring collection (else the first monitored
one); every other collection needed a trip to the app.
- The bell is replaced by the SPA header's control reproduced by value
  (`CollectionSaveToggle` + `CollectionSaveMenu`): "Uložit do kolekce" /
  "V kolekci" / "V kolekcích · N", opening a checklist of every collection,
  monitored first and bell-marked, one click to add or remove. Same routes as
  the SPA; no API change, no migration.
- The list is re-read whenever the checklist opens (it was cached for the life
  of the tab, so a collection made in the app never appeared), and both
  account-scoped caches (collections, stages) are dropped on sign-out.
- Two panel-wide fixes the checklist needed: the panel now scrolls when taller
  than the window (pinned to the bottom edge, it grew off the top of short
  screens), keeping its scroll position across re-renders; and focus survives a
  re-render again — current Chrome fires `blur` on a removed element, which had
  silently disabled the panel's focus restore (the note box too).
- An independent review before merge found that re-enabled focus restore could
  also STEAL focus: a row being saved dropped focus to the page, and when the
  save landed the panel pulled it back from the portal's search box, so the
  next Space undid the save (reproduced on the pre-fix build). Now focus is
  carried only while it is still in the panel, busy rows stay focusable
  (`aria-disabled`), a write is applied as one add/remove to the CURRENT state
  (a panel re-opened mid-write keeps newer saves), a failure with the checklist
  closed surfaces on the panel's error line, and a sign-out invalidates list
  reads still in flight (`sessionGen`).
- Verified by driving the built `content.js` in headless Chromium with mocked
  `chrome.*` APIs — 44 checks (order, writes, revert on error, keyboard,
  Escape, a 620px window, and one regression check per review finding, each
  shown to fail on the pre-fix build). Not built: creating a collection from
  the panel (the app's listing page doesn't either — "Spravovat kolekce →"
  links there).

### Phase U-COLL-SCOPE: Collections as a Browse cohort filter (done)
Collections could group properties but not SHOW them: seeing "everything in
Shortlist" meant opening the collection page, which is a list, not Browse — no
map, no stats, no price bounds on top.
- **One vocabulary.** `lib/collectionScope.ts` owns what a selection means,
  rendered as a property-id allowlist for Browse; the pipeline board's
  rendering will land beside it rather than as a second answer. OR,
  deliberately unlike `tags` (AND) — collections read as folders.
- **A lens, not criteria.** Registry id `collections`, BROWSE agenda only (the
  watchdog would fire on the operator's own clicks; the estimation agent must
  not see their taste), and outside preset identity like `pipeline` / `broker` /
  `dismissed`, so toggling it never dirties a loaded preset. No migration: the
  allowlist rides `browse_stats_properties`' generic `property_ids_filter`
  (migration 378), the seam that filter was built to be reused as.
- **The subtraction that paid for it.** `fetchBrowseStats` now resolves through
  `resolveBrowsePrefilters` like every other lane, `tags` moved off the
  `properties_with_tags` RPC onto the membership rows, and the registry's dead
  WATCHDOG agenda on `tags` went with it. Rule 18 in `docs/architecture.md` has
  the detail.
- **Cache coherence.** `revalidateCollections` gained `cohortScoped`, the mirror
  of the pipeline's knob: when Browse is scoped to collections, membership IS the
  cohort, so a save from the Browse card refetches the list too. Off by default —
  everywhere else, refetching map + cards + count + stats on a bookmark click is
  waste.
- **The board, same filter.** The pipeline bar was rebuilt on the shared
  primitives (Field + Segmented + MultiselectChips) — Stav / Typ / Lokalita /
  **Kolekce**, each offered only when it could change the view, all client-side
  over the one board read plus the shared member map, cleared by one header
  Reset instead of two hand-rolled idioms. Rule 22 in `docs/architecture.md`
  carries the availability and fail-open contract.
- Next: dropping `properties_with_tags` once this SPA build has rolled out.

### Phase U-ME: Manual rental estimates (retired 2026-10-19)

Shipped as migrations 046/047: operator-recorded point rents per listing (`manual_rental_estimates` +
`_history`, the "Ruční odhady" block on the property page and phone, four API routes, the agent tool
`get_manual_rental_estimates` in both rental estimator skills). **Retired 2026-10-19** by operator ruling
(Valuo sprint, ruling 7) as part of the line payment for the Valuo section: neither table ever held a row.
Removed end to end in `cleanup/retire-manual-estimates`: the code, the tool (migration 638 takes it out of
`skills.allowed_tools` and drops the verbatim "Consult manual estimates" bullet from
`rental_estimator_full_v1`; apply it BEFORE the code deploys), and the storage (migration 639, destructive:
backup + the operator's OK, applied after the code is gone). The original spec is in git history.

### Phase U2.7: New-listing notifications — in-app slice (shipped)

In-app slice landed: saved-filter "Watchdog" surface in the SPA, a
background matcher loop in the FastAPI service, and per-row
estimation kickoff that runs deterministically in the background and
surfaces the yield once it lands. Email / SMS / push remain deferred
(see open questions below). Cron cadence is still nightly; the
operator can call `POST /notifications/matcher/run` from the UI's
"Run matcher now" button to trigger an immediate evaluation against
any newly-scraped listings *(superseded 2026-10-21: that button, the
route and the per-row estimation kickoff were deleted in W1 of
Phase U-NOTIFY-PUSH, ruling R10)*.

**What shipped**

- Schema: migrations `056_notification_subscriptions.sql`,
  `057_notification_dispatches.sql`, `058_notifications_app_settings.sql`.
  Dispatches carry a nullable `estimation_run_id` FK so the
  operator-triggered yield calculation links back to the
  estimation row that lives on the existing `/estimation/:id` page.
- Backend: `api/notifications.py` owns the `WatchdogFilterSpec`
  Pydantic model, the SQL-clause renderer (mirrors
  `_shared_filter_where` semantics — *superseded 2026-10-01: both now
  compile from `toolkit/filter_compiler.py`; see the Rule 16 bullet
  below*),
  and the matcher loop spawned via
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
- ✅ Rule 16: one filter compiler, `toolkit/filter_compiler.py`; the Watchdog + every
  cohort compile the registry (`sql_kind` + one hook table; Browse's TS pinned to the same
  `sql_kind`); −12 dead `ComparableFilters` fields; `subscriptions_failed` in the matcher
  stats (cleanup/filter-compiler, C4 PR 1).
- ✅ M1: the Watchdog honours `building_material` and `min/max_garden_area` again (lost by
  merge f2d7b359; `extra='ignore'`); `ComparableFilters` gains the garden pair; both models
  now EQUAL their agendas by test; Browse reads the generated `BUILDING_MATERIAL_BUCKETS`
  (fix/watchdog-dropped-filters, C4 PR 2).
- ✅ M3 + watchdog map area (2026-10-21, feature/watchdog-map-area): the map box is the ONE
  spatial shape — `WatchdogFilterSpec.bounds` (Browse's `MapBounds`) replaces `lat`/`lng`/
  `radius_m` and the matcher's ST_DWithin circle; `api/location_filter.bounds_where` renders
  the lat/lng band the list and both stats RPCs apply; `filtersToWatchdogSpec` reads the one
  `effectiveBbox` (moved to `lib/filters.ts`, over the one `bboxAround` formula), so a
  centre+radius pick saves as the square Browse filtered on and the viewport box rides along
  through the create dialog's checkbox (ticked); `MapAreaControl` edits the box on
  `/watchdog/:id/edit` ("Use current view"); the two map controls paint through
  `lib/mapShape`. 0 stored specs carried a circle, so nothing was migrated.

**Next (filter definition, tracked from C4)**

- M2: the agent advertises 10 COMPARABLES ids that `_FCR_OVERRIDE_FIELDS` drops — changes
  agent cohorts, needs an operator ruling, own PR.
- The agent's null `category_type` path (`api/agent.py`) — same posture as M2.
- M4: `tom_days` reaches Stats but not the Browse list.
- M7: integer area params in the browse RPCs. M11: canonical-advert row vs property rollup.
- Browse's TS dispatch switches to `sql_kind` (drops `isMinId`/`isMaxId` + the duplicated
  dispatch).
- The NEIGHBORHOOD agenda over-declares ~30 filters `toolkit/neighborhoods.py` never reads.

**What's deferred**

- Email / SMS / push channels. `notification_dispatches.channel` is
  CHECK-bounded to `'in_app'` only; a future migration adds the new
  enum values and the dispatch worker grows a fan-out branch
  *(superseded: email + Telegram deliver through the `channel_sends`
  ledger, migration 207, not a widened `channel` — rule #16; superseded
  again 2026-10-21: Telegram is out, email inactive, iPhone push from W4
  of Phase U-NOTIFY-PUSH)*.
- 5-minute scrape cadence (Shape A from the original proposal).
  Today's nightly cron still applies; the matcher loop honestly
  surfaces "no fresh listings" between scrapes. A new
  `.github/workflows/scrape_probe.yml` is a separate slice.
- Per-user identity (one shared operator stays the model)
  *(superseded: per-user Supabase Auth is live — CLAUDE.md § out of scope)*.

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


### Phase U-COSTS 2: dedup funnel + cost grouping (done)

- /dedup gains the full **free-vs-paid funnel** (`DedupFunnel`, anchored `#funnel`):
  every resolve_pair step in engine order with pairs/properties/listings resolved
  (audit-log matview), work counters labeled *evaluations* (engine-run sums —
  re-scans repeat, deliberately distinct from distinct pairs), paid-step $ from
  the same category view /costs reads, a free/paid/manual capture strip, and
  per-step expandable byty/domy/komerční/pozemky/ostatní × prodej/pronájem
  breakdowns. 7d/30d switch.
- /costs feature table groups the four dedup vision features under an expandable
  **Dedup pipeline** row; child rows deep-link to their funnel step anchor. New
  "Dedup spend by category" card (rolling 30 d, matview) — same source as the
  funnel's paid steps, so the two tabs agree by construction.
- Shared step registry `lib/dedupFunnel.ts` (free/paid kinds, called_for→step,
  anchors) is the single taxonomy both pages consume. Migration 282: two
  matviews (pg_cron 15-min refresh; live joins were ~24 s > anon 3 s) + live
  flow/queue views.

### Phase U-COSTS: LLM spend dashboard (done)

- `/costs` operator page (nav: Settings cluster): KPI tiles (today / 7 d /
  30 d / projected month / calls / **errors**), a stacked daily-spend chart
  by feature (entity-stable civic-archive tag colors), a per-feature 7 d/30 d
  breakdown table (calls, errors, avg per call, share), and a per-model split.
- Backed by `llm_cost_daily_public` (migration 280) — per-day × called_for ×
  provider × model aggregates over the `llm_calls` audit table; anon reads
  the aggregate only. Error counts surface failed (unbilled) calls, the
  credit-outage / vision-error tripwire at spend grain.
