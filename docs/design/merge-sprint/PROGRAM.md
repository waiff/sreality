# MERGE SPRINT — one property, built from its ads

**Program document. This file is the program's source of truth.**
Status: decisions taken by the operator in a design interview on 2026-10-02/03 (four rounds; all 48
questions answered, and the later Q49 and Q51: §8). **Plan awaiting approval; only W0 is approved
to build.** Rules MS1–MS23 are binding once approved and supersede any design text, code comment or
docstring that differs. Design detail per wave (inputs, not rules) is kept outside the repo in
`~/merge-sprint-artifacts/`: `wf2/` designs and their skeptics, `wf3/` the non-interference check,
`wf5/` this plan's review, `operator_answers.md`. Where a rule here is silent, the critic's
resolutions in `wf2/critic.md` §2 apply; where they differ, the rule wins.

**Words used here.**
- *Property*: the group of ads for one real-world unit. *Ad*: one portal advertisement (`GLOSSARY.md`).
- *Canonical ad*: the one ad whose price, photos and text the property shows, chosen by a fixed order
  (MS5).
- *Curation*: what a user puts on a property: notes, a pipeline card (the entry on the deal board),
  collections, tags, a dismissal (the account hides the property from its own Browse).
- *Fold*: two items of one kind, such as one user's two pipeline cards, become one.
- *Carry record*: what a merge writes down about every user item it moved or folded, so a split can
  send it back.
- *Ruling*: a recorded "same unit" or "different units" verdict on two ads.
- *Engine*: the parallel session's automatic duplicate finder. Its *brake* undoes one engine run; the
  *clean-up* undoes merges made by the removed engine.
- *Recompute*: the job that rebuilds a property from its ads. *Read model*: the pre-built tables that
  Browse, the map and Stats read.

---

## 0. North star

> **A property shows nothing but what its ads say and what users put on it. Every fact is recomputed
> from the ads it holds now; every merge and split moves ads and curation by one written rule; and the
> app says what it did.**

Every wave is tested against this sentence. Work that does not serve it is cut (§9).

**Subtraction is the deliverable.** No flags, no settings to turn behaviour off, no second path beside
an old one. Estimate for code, tests and workflows: about **+3,200 / −4,900 lines**; **−3 tables, +1**;
about **1.5 GB** dropped from the database, 1.15 GB of it two indexes nothing has ever used, while the
Browse read models grow by roughly 0.2 GB (the two portal lists, the nine per-portal dates and their
indexes). Not counted: about 750 lines of migration text that restate views. Our own condition grades
stay for now (Q48, §9). Every wave removes more than it adds except W0 (a hotfix: +630 / −150 with its
tests and docs) and W2a (+220 / −185).

## 1. Why this program exists (verified 2026-10-02/03)

The trigger was a note that "disappeared": the operator merged two properties in Browse and the
extension then showed no note on the ad. The note sat on a third property, the inactive predecessor
of a re-list, 257 rows down the sort, and merge mode hides the marks that would have identified it.
The investigation (four workflows of agents, each finding checked by a skeptic) found real surface
defects and an unwritten model underneath:

1. **A property's facts had one rule in code and several in people's heads.** Choosing each field
   from a different ad was built in June and taken apart in three steps by September; nothing recorded
   which rule each field follows on a merge and on a split.
2. **A split gave back almost nothing.** Only the pipeline card (partly) and the asset link returned;
   notes, collections, tags and dismissals stayed. The "lossless restore" the docs promise has never
   restored a card in production.
3. **A merge said nothing and hid what mattered.** The toast counts properties and calls them
   listings; marks are hidden while ticking; notes have no mark; a failed read looks like "none".
4. **Two filters meant four things.** The portal filter matches the canonical ad's portal on most
   surfaces, hiding 12 % of properties with an active idnes ad, and the ad's own portal on one.
5. **Dead lanes ride on every merge.** Asset links (0 rows ever), the status log (1.6 M rows, one
   chart reader) and a pipeline note nobody can write are read or written inside the merge path.
6. **The daily recompute has not completed since 2026-09-29** (W0).

## 2. Decisions (MS1–MS23)

Items marked *(default)* were not asked; they are engineering defaults the operator can overrule.

### The model
- **MS1 — Vocabulary.** Property and ad are the two primary terms; `GLOSSARY.md` lists words to avoid.
- **MS2 — A property is a recomputed view.** Every ad gets its own property when the maintenance job
  first sees it, within about two minutes, never while the ad is being saved. A later merge, by the
  engine inside its area or by a user anywhere, merges properties into the oldest one. Nothing is
  remembered from merge time except which ad moved where and the carry record (MS14). *(Q1 was left
  blank; taken as yes because every later answer relies on it.)*
- **MS3 — Ads are untouched.** A merge or split changes only which property an ad belongs to, plus the
  user's rulings. Never an ad's facts or history. A test over the code pins it: the census of every
  write into ads (`tests/scraper/test_listing_write_census.py`). One named exception, paused and never
  run by a merge or split: the grading job that copies a graded ad's two grades onto the other ads of
  its property (`propagate_condition_levels`, in that census as paused). It stays off with the grades
  (MS4); resuming it needs the operator.

### What a property shows
- **MS4 — Condition: our own grades stay for now, untouched (Q48 b).** This sprint changes nothing in
  condition grading: the two 1–5 grades on ads and properties, the stored scores and extracted clues,
  the paused jobs that made them, the two grade filters, the bulk-AI interface and the estimator's
  instructions. With no prompt rewrite, the two stale prompt suggestions stay as they are (Q43). A
  property's grades keep following its canonical ad, as today, so MS5 moves them (§10) *(default)*. The
  portal's condition text stays an ad field, the canonical ad's (MS11). The AI ad summaries and the
  photo-comparison tool are untouched too (Q41, Q42). The grade removal in the design inputs
  (`wf2/design_condition.md`; the grade parts of the critic's C4 and D) is void.
- **MS5 — One canonical ad.** Order: active ads first; then ads with a map point
  (`listing_location.geom` present); then, among active ads, the earliest `first_seen_at`; among
  inactive ads, the latest `last_seen_at`; then portal trust; then id. One function, spelled once.
  With no active ad, an ad with a map point outranks one without, even if that one was seen later.
- **MS6 — Amenities are a union.** Balcony, lift, parking, terrace, garage, cellar: yes if any of the
  property's ads says yes, active or not. Accepted cost: an unreliable "yes" beats a stated "no"; MF
  rent and yield rise on about 2,300 sale flats.
- **MS7 — Brokers are a list.** The brokers of the active ads, one entry per person, the canonical ad's
  broker first. When no ad is active: the brokers of all its ads, marked as from inactive ads. The
  property header shows the list; the pipeline board shows its first entry plus "+N"; each ad's own row
  keeps showing its own broker *(the last two: default)*. Nothing is stored.
- **MS8 — Lowest active price.** On the property page, a labelled display-only line whenever the
  lowest price among active ads differs from the header price. When the header has no price, the line
  takes its place, still labelled. It never feeds per-m², yield, alerts or filters.
- **MS9 — On-market windows come from the ads.** The chart draws every ad's own line;
  `property_status_events` is dropped.
- **MS10 — Price history spans a re-list.** A predecessor is an ad on the same portal whose
  `last_seen_at` is strictly before its successor's `first_seen_at`. Its price steps plus one step at
  the handover count toward the property's price-change figures; the total is compounded; ads that ran
  at the same time never form a step. No alert fires for a cheaper re-list. Days on market stays first
  seen to last seen. The list of dated price moves under the chart shows every ad's moves, labelled by
  portal *(default)*.
- **MS11 — Every property field, one rule.** On every merge and every split each row below is
  recomputed from the ads the property then holds. The toast (MS15) reports curation only; this table
  is the written rule for fields.

| Field | Rule |
|---|---|
| Price, price per m², area, layout, floor, category, description, photos, link, furnished, condition text, our two condition grades (MS4), map point, address | the canonical ad (MS5) |
| Building type, ownership, energy rating, usable / plot / garden area, parking spaces | the first ad in the canonical order that states it (today's rule, kept) |
| Balcony, lift, parking, terrace, garage, cellar | yes if any ad says yes (MS6) |
| Brokers | MS7 |
| Portals | every portal with an ad; portals with an active ad kept separately (MS19) |
| Active | any ad active |
| First seen / last seen / days on market | earliest ad / latest ad / the span between |
| Newest ad on each portal | when the property's newest ad on that portal was first seen, active or not; empty when it has no ad there (MS19) |
| Price changes, price drops, total change | the canonical ad and its same-portal predecessors (MS10) |
| Lowest active price | MS8 |
| MF rent and yield | computed from the fields above |
| Notes, pipeline card, collections, tags, dismissal | stay on the property; MS14 on a merge, MS18 on a split |

### Merge
- **MS12 — The oldest property survives a merge**, for users and the engine alike.
- **MS13 — Collisions.** A live pipeline stage beats a closed one; otherwise the stage further along
  the board wins (rule 22, unchanged). No new rule lifts a dismissal. The toast says "hidden for
  you" whenever the acting account has an active dismissal on the surviving property.
- **MS14 — Carry record; nothing a user made is ever lost.** A merge writes one row per curation row
  it moved or folded (`property_merge_carries`): table, key, the row's own timestamp, account,
  from-property, to-property, merge group, whether it was moved or folded, a snapshot of folded rows,
  and when a split undid it. A merge of three or more properties is one step per retired property
  under one merge group; a pipeline card overwritten by a later step is recorded as folded from the property it
  came from. Alert events are not curation and get no rows *(default)*. **Invariant, tested on every
  merge and split:** each account's count of notes, pipeline cards, collection entries, tags and
  dismissals is the same before and after, except that a fold removes one, its split brings it back, and each
  "both" copy adds one.
- **MS15 — One click, one toast.** A Browse merge has no confirm step. The toast names the surviving
  property, lists only the acting account's moved items (notes as a count, the rest by name) and
  offers "Otevřít #S". The engine's Rulings page keeps its own two-step confirm and shows the same
  toast *(default)*.
- **MS16 — A merge hides nothing it will touch.** In merge mode every Browse row keeps its pipeline,
  collection and dismissal marks, shown read-only, and the merge bar lists what is ticked. Every row gets a
  note mark with the count of the acting account's notes. When notes, the pipeline card, collections
  or tags fail to load, the app and the extension say so and offer a retry; they never show an empty
  list. Reading notes with an id that was merged away follows it to the survivor, as writes already do.
  The merge-list routes had no screen and are deleted; the toast and the carry record are the record.
- **MS17 — No whole-merge undo for people.** A split is the reversal. The engine's brake and the
  clean-up stay until their planned end, as splits that record no ruling, through the same code. With
  no user present every account's items follow MS18's preselection: a note goes with the ad it was
  written on, even for merges older than the carry record; other items move only where a carry record
  says so; nothing is copied.

### Split
- **MS18 — One dialog, two parts.** The user ticks the ads that leave; the ads that stay together keep
  the property: its number, its page and everything marked "stays" *(as described in Q24)*. Leaving
  ads return to the property they came from when it is still free, otherwise to a new property; if
  they land on two or more properties those are merged into the oldest. Separating three units takes
  two splits. Before the click the dialog shows where each part and each item will land; the preview
  is a read (`split-plan`), and the write re-checks it and refuses if anything changed.
  - **Per item, the acting user chooses stays, goes, or both.** Preselected: a note goes with the ad
    it was written on; other curation goes back to the property its oldest standing carry row names,
    when this split gives that property ads back; anything else stays.
  - **"Both" is a copy**, a new row on the other part: a note keeps its text, date and origin ad; a
    pipeline card keeps its stage and gets a new added-date, last in its column, as a second
    independent card; a collection entry and a tag are dated now, so collection alerts start at the
    split; a hide is a new active hide dated now.
  - **Folded items** are re-created on the part that gets their origin back, only while the item they
    folded into still exists. After routing, the existing rule that a live pipeline card lifts the
    same account's hide runs on both properties, and MS13 settles two cards of one account.
  - **Rulings:** a split records only "different" between the two parts. It has no undo of its own; a
    mistaken split is fixed by a one-click merge.
  - **Other accounts** cannot be asked at the moment of a split. Their items follow the preselection,
    are never copied, never deleted and never shown to the person splitting. No notice (Q33).
  - **The toast** after a split says where each part landed and which of the acting account's items
    went or were copied.

### Filters
- **MS19 — Portal and broker filters select ads; rows are always properties.** A property matches
  portal P when any of its ads is on P. The status switch is judged on the selected ads: *active* = an
  active ad on P; *inactive* = ads on P and none of them active; *any* = any ad on P. Several portals =
  any of them *(default, as today)*. A broker filter works the same over that broker's ads; portal and
  broker together mean one ad satisfies both. One rule on Browse list, count, map, Stats and Watchdog.
  A row shows the canonical ad's facts; its portal badge lists the portals where the property has an
  active ad, or, when none is active, all its portals marked inactive. **Newest first (Q49 b):** with
  exactly one portal P selected, "Newest first" and "Oldest first" order properties by when their
  newest ad on P was first seen, active or not, so a property advertised on P again rises to the top
  *(its newest ad on P, not its first: default)*. With no portal or several portals they order by when
  the property was first seen *(default)*; a broker filter changes neither rule. Every portal uses our
  own first sighting, bazos and ceskereality too, whose August order followed the portal's own date
  *(default)*. Only the order changes: every filter, "added in the last N days" included, keeps its
  meaning on every surface. Rows keep the property's own dates; a header chip names the order, and a
  card whose date on P differs from its first seen shows both *(default)*. The per-ad Browse lane is
  deleted.
- **MS20 — Estimation subject = the property**, through one lookup. Recorded, not built here (§9).

### Housekeeping
- **MS21 — What is deleted, what is kept.** Deleted: asset links end to end; the merge-list routes;
  the pipeline card's note field, which nothing can write and no card holds. Kept: tags; the pipeline
  history's merge columns (history is never erased). Kept and reused: `properties.all_sources` /
  `active_sources`. This reverses Q19 for these two columns only: MS19 needs two stored portal lists
  per property and they already exist. Q19's other removals belong to the dedup session (MS22).
- **MS22 — Coordination with the parallel dedup session** (§7).
- **MS23 — The daily recompute resumes where it stopped** (W0, approved).

## 3. What this program never does

- Writes an ad's facts or history on a merge or split.
- Changes our condition-grading code, its tables or its filters (MS4).
- Remembers property facts from merge time.
- Deletes a note, pipeline card, collection entry, tag or dismissal of any account.
- Adds a flag, a setting or a second path beside an old one.
- Touches the dedup session's leave list (§7) or allocates its rule numbers.

## 4. Waves

"Engine path" = code that runs inside an engine merge or on the engine's two review pages.

| Wave | Content | Engine path | Needs | Lines (≈) |
|---|---|---|---|---|
| **W0** | Daily recompute resumes where the last run stopped (draft PR #1695) | no, but a shared file | dedup session's OK to merge before its PR #1655 | +630 / −150 |
| **W1b** | Asset links and the pipeline note field out (code) | yes | #1655 | 0 / −840 |
| **W2a** | Recompute: canonical-ad order, amenity union, the two portal lists and one "newest ad" date per portal (MS19), price lineage; stops writing two write-only columns; keeps #1655's share-sale rule; realitymix joins the portal list | yes | #1655; its additive migration (nine `properties` date columns) applied before merge | +220 / −185 |
| **W2b** | Property page, pipeline board and Browse rows: broker list, lowest price line, chart of every ad, everything in MS16 | no | — | +360 / −480 |
| **W3** | Carry record and the count invariant; one toast; the brake's dry run counts carry rows; split hooks, the pipeline snapshot and restore, and the merge-list routes deleted | yes | #1655 | +330 / −655 |
| **W4** | One split dialog (stays / goes / both), curation routing; three dialogs and the split undo deleted | yes | W3 | +1,400 / −1,740 |
| **W5** | One read-model rewrite, the one portal rule and the one-portal "Newest first" (the nine dates copied into `browse_list`, one index each), broker lookup; the per-ad Browse lane and its writers deleted; old PR #956 closed | read model | W1b, W2a, one full recompute cycle after W2a | +290 / −870 |
| **W6** | The destructive window (§6), with its registry and test edits | registry only | W1b–W5 live | database |

**Order.** Before the dedup session's review closes (2026-10-06 13:45 UTC): W0 and W2b. After its
"C2 CLOSED" line: W1b and W2a back to back; then W3 and W4 back to back; then W5; then W6 on the
first 05:20 UTC window after W5 is live. Everything is built in worktrees ahead of time and waits.

**Every wave** is built in its own worktree by a builder with two adversarial reviewers, ships as one
PR with its tests and docs, states adds against deletes, and is confirmed on Railway after merge. A
failing test, a merge conflict or a red gate stops the wave and is reported.

**Rule edits.** CLAUDE.md has 291 of its 300 lines on main (PR #1697, 2026-10-03; CI's docs budget
fails above 300). Each wave's edit is net zero except W5 (+1); rule 14 and the "Derived — condition
scores" line stay (MS4). W1b drops the asset link from rule 18. W2a puts the canonical-ad order and the
amenity union into rule 15 in place of two clauses that repeat rules 18 and 20. W3 replaces rule 22's
"lossless restore" clause and rule 18's detach sentence with the carry record. W4 rewrites rule 15's
"or a refusal" and rule 18's split sentence. W5 adds the portal rule to rule 16; the one-portal
"Newest first" and its per-portal columns go to `docs/architecture.md` (rules 16 and 21), not CLAUDE.md.

## 5. Gates

- **Rule 0: code before drops.** Nothing is dropped before the code that stops naming it is live on the
  API, the worker and the scheduled scrapers. Asset links, the pipeline note and two recompute columns
  are read or written inside every merge, and every saved ad writes `discovery_seq`; dropping them
  early would fail every merge or every save.
- **W0:** a completion stamp appears within two runs and the saved position clears.
- **W2a:** its nine date columns exist in production before the code merges (an additive migration,
  confirmed by a catalog read). The fill is a one-off script, not a migration: it queues the roughly
  64,000 properties with two or more ads, at most 2,000 per maintenance tick, off-peak, pausing while
  the engine is bootstrapping. After it, a 1 % sample shows 0 properties whose stored canonical ad,
  portal lists or per-portal dates differ from the rule, and reports how many changed their condition
  grades (expected: about 1.7 % of properties with two or more ads). Single-ad properties get their
  portal lists and dates from the next full recompute cycle.
- **W3:** the carry write adds at most 100 ms at the 95th percentile; no new failed engine merge in
  the first 24 hours; the count invariant (MS14) passes.
- **W4:** the count invariant runs on production before and after the release; a two-account live
  test passes for every curation table.
- **W5:** one full recompute cycle has completed since W2a; a sample of active properties that
  includes single-ad ones shows 0 whose stored portal lists or per-portal dates differ from their ads,
  and 0 where a portal is listed without its date or dated without being listed; one fixture pins the
  portal rule in both the database and the browser; on a shadow copy, off-peak, the nine per-portal
  indexes add at most 30 s to a rebuild, the whole new rebuild stays within 1.25 times today's (7 days:
  median 255 s, p90 751 s), every new index serves a measured page or count or is not built, and a
  24-row "Newest first" page for the largest and the smallest portal (idnes, maxima) reads that
  portal's index with no sort step; every read-model object changes in one transaction, off-peak,
  never while a recompute runs, both read models are rebuilt once before the code merges, and the two
  Stats/map functions are restated from their live bodies; order agreed with the street-filter session
  (its 585 restates both functions) and with the dedup session's wave that deletes the removed engine's tables.
- **W6:** each object re-checked on the day: no view or function depends on it, and no cascade is
  used; backup to R2 read back; engine paused; 05:20–05:30 UTC; a 5-second lock limit with retries;
  indexes dropped outside the transaction, after a query plan shows the Watchdog does not use them and
  the feed index's scan count has not moved since W5 went live;
  the pipeline note field is skipped if a single card holds a note.
- **Migrations** are numbered 586–599.

## 6. The destructive window (W6)

| Group | What it is | Objects |
|---|---|---|
| Status log | the stored on/off-market log, replaced by MS9 | `property_status_events`, its view, trigger and function |
| Asset links | "same building" links, never used | `assets`, `asset_membership_events`, `properties.asset_id` |
| Pipeline note field | a field nothing can write | `property_pipeline.note` |
| Per-ad Browse lane | the "one portal's own page" machinery, replaced by MS19's one-portal "Newest first" | `listing_feed_visible()`, then `listing_feed_public`; the `listing_ids_filter` parameter of `browse_map_cells` (the function re-created with its grants); `listings_portal_feed_idx` (136 MB, dropped concurrently, outside the transaction); `listing_detail_queue.discovery_seq` with its default and `listings.discovery_seq`, then the sequence `listing_discovery_seq` |
| Write-only columns | written by the recompute, read by nothing | `properties.price_per_m2_source_listing_id` and its function; `properties.distinct_site_count` |
| Never-used indexes | built for an old Browse path | `properties_cat_last_seen_keyset_idx`, `properties_last_seen_keyset_idx` (1.15 GB) |

**Never dropped by this sprint:** any curation table; the pipeline history; `properties.all_sources` /
`active_sources`; `listings.published_at` and `listings.discovered_at`; the AI summary cache; our
condition grades with their scores, jobs and settings (MS4); anything on the leave list in §7.

## 7. Coordination with the parallel dedup session (MS22)

The hand-over and that session's reply of 2026-10-03 14:34 UTC live in the memory note
`merge-sprint-handover-to-autodedup`. Verified on 2026-10-03: nothing in §6 was built by or is used by
that session; the two never-used indexes come from migrations 198 and 275.

- **Left to that session** (its own deletion wave, after its own backup): its unused tables; the
  removed engine's 11 caches, one of which holds 16 rows the operator wrote; the merge ledger's
  `generation` column; `properties.published_at` and `publish_reason`.
- **This sprint changes the engine's path in four ways**, all accepted by that session in writing:
  asset links go; a new canonical-ad order; a carry record inside every merge; one split dialog, which
  removes its Proposed-splits page's batch run, "keep together" and "Vrátit". These replace three of
  that session's recorded decisions.
- **Its conditions, adopted:** its PR #1655 (share sales) merges before any sprint PR that touches
  `recompute_property_stats.py`, `property_identity.py`, `room_taxonomy.py` or `apply.py`, or the
  sprint goes first from 2026-10-08 00:00 UTC; the recompute and the merge code keep #1655's rule;
  engine-path PRs merge only after its "C2 CLOSED" line, back to back; before any merge to main the
  engine is not bootstrapping and no dispatch holds its writer lease; migrations 586–599; no engine
  rule numbers allocated here; the brake's dry run counts carry rows.
- **The engine is paused for W6 by the operator or by this session**, not by that session: set
  `realtime_autodedup_interval_seconds` to 0 at 05:00 UTC, wait for its lease to expire, run the
  window, set it back to 60, confirm the next pass.
- **Corrections sent back to that session:** on a split with no user present, notes follow the ad
  they were written on; its Rulings page keeps its confirm; this sprint restates `properties_public`
  once (W5), so that session's own restatement is struck and it drops `published_at` after W6.

## 8. Open items

1. **Q48, answered 2026-10-03: (b), keep our condition grades for now.** W1a and the condition row
   of §6 are cut; nothing in condition grading changes (MS4). A property's grades keep following its
   canonical ad: on a 2 % sample the new order (MS5) changes them on about 1,200 properties (about 850
   lose them although another of their ads is graded, 250 take another ad's, 100 gain), almost all
   with no active ad. Taking the grades from the first graded ad instead would differ on about 1,600
   properties and separate a property's grades from its condition text; it is not done *(default)*.
2. **Q49, answered 2026-10-03: (b).** With exactly one portal selected, "Newest first" orders by when
   the property's newest ad on that portal was first seen (MS19). It is stored as one date per property
   and portal: nine `properties` columns written by the recompute (W2a), copied into `browse_list` with
   one index each (W5). The August machinery still goes: `discovery_seq` is empty on 54 % of ads and
   does not follow the portal's own order inside one index walk, and its 136 MB index is written on
   almost every update of an ad. Known prices: nine more indexes on every Browse rebuild (W5 measures
   them first); a tenth portal needs its own date column before it can be offered (a test fails until
   it has one); a bazos ad its seller bumps no longer rises. Today the new order differs from (a) for
   about 18 properties a day (55 of 17,672 ads first seen in three days joined an older property, 40
   of them re-listed on the same portal); the number grows when the engine's area widens. Its defaults
   are in item 4.
3. **Q51, answered 2026-10-03: (a).** The health check on the daily recompute now warns at 52 hours
   and fails at 56 hours since the last complete cycle (was 26 and 30), sized for a two-run cycle. It is
   part of W0's PR. Known price: a recompute that dies silently is flagged after about two days.
4. **Defaults taken without asking** are marked *(default)* in §2: the pipeline board's broker line;
   each ad's row keeping its broker; the Rulings page keeping its confirm; several portals meaning
   any of them; alert events outside the carry record; the price-move list showing every ad; a
   property's condition grades following its canonical ad; under one portal, "Newest first" following
   that portal's newest ad, and the property's first seen with no portal or several; our own first
   sighting on every portal, bazos and ceskereality included; a card showing its date on the selected
   portal when that differs from its first seen.
5. **The repair merge of properties 310481 and 876074 is still not done.** The operator chose "your
   request" (Q26); the permission layer blocked it. Either click it in Browse or allow the request in
   an interactive session. Until then the extension shows no note on the re-listed ads.
6. **Glossary PR #1688** merges before W1b; until then MS1 points at a file not yet on main.
7. **Three properties await splits** (14655, 120548, 687023; reported by the dedup session). They are
   W4's first real cases; until W4 ships the existing split is used.

## 9. Cut from scope (reported, not built)

- **The estimation subject lookup (MS20).** Its own design and approval; it keeps giving the
  estimator the subject's two condition grades (MS4). Until then estimation comparables read amenity
  and portal filters per ad, a named exception to rule 16.
- **A notice to other accounts after a split.** Worth revisiting when a second real account curates a
  property with two or more ads, or when splitting opens beyond admins.
- **The lowest active price on Browse rows.**
- **The broker count book's seven-day rule.**
- **Widening the engine's area.** The dedup track's call.
- **Removing our own condition grades** (Q48 b). Its inventory (about −7,700 lines, 21,000 data
  lines, 4 tables, 6 columns, 7 settings rows) stays in `wf2/design_condition.md`. Resuming the paused
  grading jobs is a separate decision (MS3).

## 10. Risks

- **The database is close to its disk-reading limit.** Browse rebuilds and the daily recompute have
  timed out this week. Every rollout uses a queue, never a full-table pass, and heavy steps run
  off-peak.
- **More work inside every merge** (the carry record) against the engine's 25-second limit per merge.
- **Canonical-ad changes** restart price-alert clocks on about 3,600 properties with an active ad. No
  alert is replayed. Properties with no active ad are re-measured in W2a's sample.
- **Our condition grades move with the canonical ad** on about 1,200 properties (about 850 lose them),
  nearly all with no active ad. Every surface reads the canonical ad's grades, so Browse, Stats, the
  map, the Watchdog and the estimator's comparables still agree.
- **Browse counts move.** One portal now counts properties, not ads; several portals gain the
  properties the old rule hid.
- **Browse tabs left open from before W5** fail until reloaded in two places: a broker filter from W5
  on (its route is replaced), and the map and a one-portal list from W6 on (W6 drops the per-ad view
  and the map's ad-id parameter). W5 changes no database function's parameters.
- **Browse rebuilds get heavier.** W5 adds the two portal lists, nine per-portal dates and nine
  per-portal indexes (about 43 MB; each is one full pass over the ~280 MB table, even for a portal with
  500 rows) to a rebuild that runs every 15 minutes and already reads about 586,000 blocks a run
  (7 days: median 255 s, p90 751 s, 8 of 617 runs failed, the slowest at the 1,800 s cap). W5's gate
  measures it on a shadow copy first.
- **Ties inside one save batch.** 93 % of idnes's and bazos's ads first seen in the last 24 hours share
  that time with another ad (up to 62 in one batch on bazos). Inside a batch rows fall back to property
  number: new properties then follow the portal's own ad numbering on most portals, and a property
  advertised again comes last in its batch.
- **Order and filter can disagree under one portal.** A property advertised there again can lead
  "Newest first" while "added in the last N days" leaves it out; rule 16 keeps the filter's meaning.
- **A tenth portal** needs its date column on `properties` and the read models before it can be
  offered; a test fails until it has one.
- **Three sessions edit nearby files:** this one, the dedup session (#1655, which makes the recompute
  statement an f-string; whichever lands second rebases) and the street-filter session. That session's
  584 is applied (2026-10-03); its 585 (unmerged) restates `browse_stats_properties` and
  `browse_map_cells` with unchanged parameters, so W5 restates both from what is live; its untracked
  `586_chip_codes_and_watchdog_removal.sql` (seen 2026-10-03) sits in this sprint's 586–599 block and
  must be renumbered before W2a's migration takes a number; its uncommitted edits touch `queries.ts`,
  `filters.ts`, `brokers.ts` and `BrowseExperience.tsx`, all W5 files.
