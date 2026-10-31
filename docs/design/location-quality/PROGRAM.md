# Location data quality programme

**Status: APPROVED 2026-10-31 (operator rulings below). Build runs wave by wave; this file is the as-built record.**
Tickets: W0 #2210 (gate #2211, PR #2212); v5.9 #2192 (gate #2193, PR #2194). Later waves file their tickets when they start.
Origin: the operator's follow-up on the location refactoring sprint (2026-10-30), eight read-only investigations, a synthesis and an adversarial review; the plan as reviewed follows, then the rulings that amend it.

## Rulings 2026-10-31 (amend the plan below; where they differ, the ruling wins)

| # | Ruling | Effect on the plan |
|---|---|---|
| Q1 | Quarter = část obce only; MOMC names stay match keys. | W5 as planned. About 960 labels lose "Praha 5, ". |
| Q2 | A street keeps the claimed část when it has doors there; a part name that matches a cadastral area name climbs to its část. | W5 as planned (+12,441 quarters, about 1,460 climbs). |
| Q3 | **One truth for every portal, no bands.** The words bind the town (stated, or derived from finer words through RÚIAN). A pin outside that town, by the authoritative polygon, is never the position; a pin inside it refines. The bazos contract declares its pin an area marker, so a bazos pin never places an ad when words exist; without words the pin names the town (R7), as today. | W5 drops the 250 m tolerance and the declared-blurred condition. The 274 border pins move too. bazos: 18,091 town-grain and 5,248 quarter-grain ads move off their postal pin. |
| Q4 | Absence: measure the 630 towns / 397 streets first, then rule A or C. Default A. | W3 ships A unless the measurement says otherwise. |
| Q5 | Corrections stay, as a pencil on the property page's address line opening a RÚIAN-matched address picker; the chosen entity becomes an operator claim on every current ad of the property and outranks the portal. Admin-gated (builder's call). The admin inspector form goes. | New wave W3b after W3. |
| Q6 | Confirmed after the operator checked the 27 pages: unresolved stays red, page-withheld shows as Watch. | W1 as planned. |
| Q7 | Fixed red thresholds from day one; nothing measured twice: W1 first folds the five existing location counters (gauge cells, Location Quality aggregates, pin-audit matview, audit waterfall, gate SQL) into the one scorecard. | W1 acceptance line 1. Pin-accuracy cells wait for W6. |
| Q8 | **The map is the source when it is there.** An exact pin resolves to the RÚIAN address point and refines the row below the town the words bound (street, house number), never contradicting it. The remax header slot is NOT read; for remax pages without a pin the ruled map attribute stays. | New wave W5b after W5 (a pin → address point rung, tolerance calibrated against address points; June 2026 measured 99.2 % precision at 15 m). W4 shrinks to realitymix. |
| Q9 | Worker is the only resolve runtime. | W2 as planned. |
| Q10 | Praha's okres = 9999 under ČÚZK's own name. | W7 as planned. |
| Q11 | One `obec_population` table keyed by RÚIAN code and year; the curated-city population becomes a view over it. | W8. |
| Q12 | A: sreality's statistics stay an external source; the locality id is resolved at fetch time from current sreality ads (a view, no stored crosswalk). B (own index) is not started. | W8. |
| Q12b | *Confirmed by the operator in session on 2026-10-31.* Map-layer rulings the same day: next layers are noise pollution, air pollution and cadastral data (RÚIAN building and parcel attributes); RÚIAN building and parcel attributes become property facts and parcels a layer (a planned wave after W5b); the integration layer also owns per-property facts derived from layers, as its own wave after W8; revision-static values ride the tile path, selection-dependent values stay GeoJSON over mirror polygons; one provenance registry for every external dataset comes as a later wave, not inside W8; OSM amenities and transit fold in under that later wave; layer toggles on every MapLibre surface, phone app excluded, unchanged; a layer is added by one registry entry in code. Original note: "we are adding more and more layers over the map using different non-RÚIAN data sources and possibly other RÚIAN-compliant data sources, so we might want a unified approach, such as a map integration layer". | W8 states the one convention (below): the `map_overlays` registry (migration 600, `docs/design/map-overlays.md`) is the map integration layer for every reference geometry; every unit-keyed dataset carries a RÚIAN code at its native level and borrows its polygon from the mirror at render time; no dataset stores a second copy of an administrative polygon. |
| Q13 | Destructive steps pre-approved, each with a backup to R2 and a receipt. | W3, W6, W7, W8. |
| Q14 | Order: W0 → W1 → W2 → W7 → W8 → W4 → W3 → W3b → W5 → W5b → W6. | Sequencing. |
| — | **The legacy `admin_boundaries` table is retired and removed entirely; its eleven consumers are rebuilt on the RÚIAN mirror; cleanup is a deliverable.** It is the same ČÚZK RÚIAN boundary pack loaded in May 2026 by a simpler script, so "pre-mirror RÚIAN table" is the honest name. Only `population` (ČSÚ) and the sreality locality crosswalk were not RÚIAN data. | New wave W8 after W7 (Praha's okres must exist in the mirror first; brokers and the Datasets tree read parents). |

### W8: one registry (added 2026-10-31)
Consumers of `admin_boundaries` and what each takes from the mirror instead: brokers leaderboard and count book (migrations 580, 609: parents and names → `ruian_admin_units` chain); Datasets page and price statistics (obec → okres tree → the chain; sreality locality id → a view over current sreality ads' payload ids and their `listing_location.obec_kod`); sold comparables (`sold_db`, `sold_fetch`: the obec polygon envelope → `ruian_admin_unit_geometries` render geometry); map overlays (kraje outline, rent-map choropleth, migration 601 → render geometry); city proximity and curated city indexes (migrations 636, 674: polygon + population → geometry + `obec_population`); admin pages for CLIP tagging and condition scoring (kraj list → the mirror). `curated_cities.admin_boundary_id` is renamed to the RÚIAN code it already holds.

**Map integration, one convention (Q12b).** Two kinds of thing are drawn over the map. *Reference geometry* (metro, flood zones, railway stations, kraj borders) lives in the `map_overlays` registry: one store, one renderer into vector tiles, one API route, one frontend hook, one registry entry per layer (`docs/design/map-overlays.md`, operator rulings of 2026-10-14). *Unit-keyed datasets* (the MF rent map by cadastral area, population by obec, sreality price statistics by obec, the curated city indexes by obec) store a RÚIAN code at their native level and a value; their polygon is the mirror's render geometry, joined at render time. A dataset's source may be RÚIAN-compliant or not; what makes it integrable is the RÚIAN key, and the mirror is the only polygon store. W8 applies this to every consumer it re-points: the kraje outline and the rent-map section read the mirror; the two GeoJSON paths the overlay design left out (the Datasets page's price-growth obec shapes and the curated-city polygons) move onto mirror polygons as well, so `admin_boundaries` has no reader left. Adding a layer later is one registry entry (geometry) or one RÚIAN-keyed table (values); never a new polygon table. Deleted: the table (19,423 rows), `scripts/ingest_boundaries.py`, `ingest_boundaries.yml`, the population loader's old target, every `admin_boundaries` reference in docs and comments. Destructive gate: backup to R2 first (Q13).

### W3b: the address picker (added 2026-10-31)
On the property page, a pencil on the address line (admin-gated) opens a RÚIAN-matched picker: the place typeahead the chips use, extended to streets and house numbers (address points). The pick is written as one operator claim per current ad of the property (an `address_point_id`, `street_name` or `obec_name` claim with licence `operator`); operator outranks portal under W3's one precedence rule. Later-attached ads resolve from their own words. The Location Quality inspector form and its route are deleted.

### W5b: the pin refines within the words (added 2026-10-31)
After W5 binds the town from words, an exact pin inside that town resolves to the RÚIAN address point within a calibrated distance (one street, one point, else nothing) and refines the row: street, house number, address point. It never contradicts the words: a derived address on another street than the words name is dropped. Reverse resolution was refused since W2-a ("a pin never yields a street"); this ruling reverses that for exact pins only, and the tolerance is measured against address points before it ships.

---

# The plan as reviewed (2026-10-30)

Date: 2026-10-30. Baseline: resolver v5.8 live; v5.9 (#2194) open, green and mergeable, waiting for the merge light. Every number below is for **active ads** unless marked. Eight read-only investigations measured them live tonight and I spot-checked them. An adversarial review then re-checked the plan at 22Z, and its corrections are applied in place; figures it measured itself are marked "(review)".

**Short glossary:**
- **Claim:** one statement a page makes about location (a town name, a pin, a PSČ).
- **Resolver:** the code that turns an ad's claims into its one `listing_location` row. Its steps are:
  - BIND: choose the RÚIAN entity.
  - FILL: copy that entity's codes and names onto the row.
  - CHECK: compare the pin with the bound town.
  - GRADE: confidence and radius.
- **Grain:** how fine the row is: obec, street or address point.
- **Část obce:** a named part of a town; every door belongs to exactly one.
- **MOMC:** a city district (městská část), an administrative unit in 13 cities. Its borders cross the části.
- **KÚ:** cadastral area.
- **PIP:** point-in-polygon test.
- **CE95:** the radius that contains the true point 95 % of the time.
- **Upsert:** insert-or-update in one statement.
- **SCD-2:** register history kept as closed rows, never deleted.

## North star

> Each listing's location is a pure function of its newest evidence and the current RÚIAN register: one current claim per fact, one bound register entity on one ladder of levels, a position inside the town the words name, and a radius that states with a measured probability how far the truth can be. The row re-derives itself whenever evidence, code or register changes, and every deviation is counted in one place.

## The root cause, in one paragraph

Almost every open issue has the same shape: **one fact is stated in several places, and each place decides on its own.**
- **Claims:** every historical value is kept, and seven tie rules each pick a different end of that history. The newest value is right 98.6 to 100 % of the time, yet the code mostly serves the oldest.
- **Grain:** two steps write it after the keys are already filled.
- **Quarter field:** it holds two divisions whose borders cross.
- **Position:** a pin outside the named town can still be the position.
- **Radius:** a per-level constant that nobody has measured.
- **Re-resolution:** it waits for a clock in a second runtime.
- **Quality:** four surfaces count it, using three definitions.

Each wave below replaces several local rules with one rule.

## Principles

1. **One current claim per fact.** A producer holds the value it stated most recently, and the operator outranks the portal. A value the page stops stating is kept; retraction is an explicit act (Q4 decides what an extractor change counts as).
2. **Derive, never assert twice.** Every field on the row is a projection of the one bound entity's register chain, and the grain is the finest key on the row. The served ladder is kraj → okres → obec → část obce → ulice → adresní místo.
   - Kraj, okres and obec are full territorial partitions of Czechia (the NUTS convention, Praha's okres included).
   - Every address has exactly one část obce.
   - Ulice and adresní místo are keys, not partitions.
   - MOMC and obvod names are match keys: they still anchor their town, but they are never served levels.
3. **The position agrees with the words.** One containment test decides whether a pin may be the position, for every portal: the obec polygon, with the 250 m tolerance BIND's sliver rung already uses. BIND, place(), CHECK and the scorecard all use that same test, tolerance included.
4. **A radius states a measured probability.**
   - For a pin it is the CE95 of the pin's class (source, declared label).
   - For a register placement it is the entity's own reach, a sure bound given the bind.
   - A wrong bind (the wrong town) is not hidden in the radius; the scorecard counts it.
   - The radius is never set by grain. A row with no point has no radius.
5. **Re-derivation is level-triggered and evidence-first.** A version change sweeps from the runtime that runs that version, and fresh evidence goes first. No clock and no hand step triggers re-derivation. The one standing hand step is recalibrating the radius table after a contract change; the accuracy cell triggers it.
6. **Measure once; delete what nothing reads.** Every metric has one SQL definition. Red means the system can fix it. Each wave deletes at least what it adds, and a bug fix that cannot (W0) says so.

## Waves (delivery order)

| Wave | Component rewritten | Store slimmed | Covers | Net | Effort |
|---|---|---|---|---|---|
| W0 | Detail drain: an empty page is a failed fetch (scraper track, outside the north star) | junk snapshots stop | new incident; feeds issue 7 | about +10 lines; a bug fix for rules 2, 4 and 5, no paying deletion | S |
| W1 | One location scorecard | toolkit aggregates, 2 routes, about 200 TS lines | issues 7, 8, 10; radius coverage | about −85 lines; 3 town-share definitions → 1, 2 street definitions → 1; +1 view; about 55 Watch-only cells | M |
| W2 | Resolve lane: one order, one trigger, one runtime | dirty_locations (CHECK 8→3, index swap, 13.2k no-op rows a day) | issue 9 | a workflow, a cron, a flag, 3 knobs and a sweep arm out; about 20 lines in | M |
| W3 | Claim store: one current claim per fact | location_claims 14.77 M → about 5.95 M rows, −1 column, −2 indexes | cascade points; stale values | about −700 lines; 7 tie rules → 0; 4 supersession paths → 2 (the upsert and explicit retract) | L |
| W4 | realitymix + remax contracts | none | issues 5, 6 | contract changes net negative, 1 regex in | S |
| W5 | Resolver v5.10: one ladder, one matcher, one grain, one containment test | quarter field holds one level | issues 2, 3; cascade points | 6 special cases out; one match-key list and the shadow's `ids` block in (no distance read, Q3) | M |
| W6 | Radius v5.11: CE95 for pins, reach for register placements | −declared_radius_m, −match_confidence, −sentinel | issue 4 | hand table and two fields out; generated table, script and one standing recalibration in | M |
| W7 | RÚIAN loader: full partitions, read levels only | −2 levels, 23,626 units retired, −6 columns | Praha okres, mirror defects | negative | S |

**Sequencing.**
- Merge #2194 (v5.9) first, with its hand rollout for the last time.
  - **As built:** #2194 merged 2026-10-31 13:04Z; 55,114 disputed rows re-resolved by 13:17Z; the 1.03 M-row full sweep then running (about 9-10 h); gate #2193.
- The RÚIAN vintage is due 11-01 (GH cron '0 10 1 * *', throttled). If it publishes before W2 lands, it also needs today's hand full-resolve.
- W0 runs beside W1 on the scraper track.
- W3 and W4 can swap if W3 waits on its destructive gate. W5 depends on W3 only for code overlap in bind.py, so it can go first if W3's gate waits.
- **Standing gate for W3, W4, W5 and W6:** Praha (town 554782 and its 112 quarter blocks), Pardubice, Liberec and 30 okresy are inside the live autodedup apply scope. Every wave that moves positions or quarter codes therefore runs one shadow autodedup generation before deploy, judged on the rulings ledger inside the affected blocks (the 413 post-2026-10-27 `same` pairs must hold); how: `docs/design/location-quality/wave-shadow.md`. DECISION_MODULES stay untouched.
- Respect the two-implementer limit.

### W0: a page with no listing content is a failed fetch (urgent, scraper track)
I verified this tonight, and the review re-checked it at 22Z. At 18:17 to 18:20Z ceskereality served an anti-bot page, and our drain wrote it into 51 active listings as content: price, area, description and images blanked, `last_seen_at` bumped, completions marked "written". There has been no ceskereality completion since 18:20Z.

The review found the same defect class on realitymix. 4 active rows (e.g. 10104448) carry a category page ("Pronájem bytů - byty k pronájmu - Tábor") as content, the latest written at 13:47Z today.
- **Change:** one shared rule in `portal_runner`. A parsed detail with no price, area, params, images and description is a failed attempt: attempts++, no write, no sighting. No new Portal seam is added until a portal needs one.
- **Not deleted:** mmreality's NoPropertyObject branch. Half of it is a positive gone signal: the site's own page with no object is a removed plot (rule 3). The parser also raises that error before any listing exists, so the shared rule never sees those pages.
- **Owed with it:** realitymix's gone seam must recognise a redirect to a category page as gone (rule 3). Otherwise the shared rule turns those 4 rows into failures that are given up and never delist.
- **Net:** about +10 lines with no paying deletion. It is a bug fix for rules 2, 4 and 5, so it ships as its own scraper-track PR outside the sprint's subtraction budget.
- **Acceptance:**
  - 0 content-free snapshots on any portal for 7 days;
  - the 51 ceskereality listings healed by their next real fetch;
  - the 4 realitymix rows closed by a gone verdict or refreshed by a real page.
- **As built:** #2212 (a parsed detail with no listing content is a failed fetch: attempts counted, no write, no sighting) merged 2026-10-31 12:54Z; gate #2211.

### W1: one location scorecard
- **Change:** a new view, `location_quality_by_source`, defines six metrics:
  - missing town/KÚ, with foreign ads excluded from the denominator, plus KÚ coverage by grain (shown, not judged);
  - outside: the point is farther from the bound town's polygon than the 250 m tolerance, the same test as principle 3;
  - disputed-class mix;
  - street share by grain rank;
  - pin accuracy on a deterministic sample (listing_id % 10 = 0): pin-to-truth distance quantiles per (source, declared label), where the truth is the RÚIAN address point of the same property (the ad's own or a sibling location ad's) bound from words, with n and the Wilson bound;
  - version lag, plus backlog age reported separately for evidence and backfill.
- **Grant:** service_role only. Nothing in the browser reads the view, because the page reads stored cells. Mig 427's precedent grants to authenticated; do not copy it.
- **Cohorts:** two. "Listing" covers active ads; "served" covers published properties, joined through `location_listing_ref_id`. Joining through `browse_list.listing_id` would be wrong: it is the canonical ad, and the two differ on 14,635 properties.
- **Who runs it:** the existing `location_town_coverage` gauge executes it every 6 h; the key stays and the label becomes "Location quality". The Location Quality page renders the stored cells and stops aggregating, so its two routes, the toolkit aggregates and its browser-held thresholds are deleted. Gates cite cells. Health gains about 55 cells, all Watch-only.
- **Town cell split:** derived from claims, never stored.
  - *Unresolved:* there is town-bearing evidence but no town. It stays red until zero.
  - *Withheld:* the listing holds no town-bearing claim at all, with or without a row. It shows as Watch.
- **Accuracy:** shown, not judged until W6 ships the calibrated table. From then on it compares the measured q95 per cell against the shipped cell.
- **Rule 25:** the view is one added relation, paid for by the deletions above.
- **Acceptance:**
  - every cell's value counts exactly the rows that colour it;
  - Health and the Location Quality page show identical numbers;
  - the run fits inside the 45 s check budget;
  - the 25 page-withheld ads read Watch.
  - sreality/town stays red on one row (19405830: an address point RÚIAN publishes without coordinates, no KÚ) until W5's KÚ fix, so the cell leaves fail only then.
- **As built (2026-10-31, ticket #2218, PR #2221; the backend half):**
  - **Migration 698**, `location_quality_by_source`: additive, service_role only, `security_invoker`; long shape (cohort, source, metric, class, n, of_n, q50, q90, q95, q100) over the cohorts listing, served, sample and queue. The shared fragments are rendered verbatim and pinned by `tests/location_data/test_location_quality_view_contract.py`: `location_steps`' step flags, `town_missing_sql()` and the new `town_evidence_sql()`, the publication rule, and the containment probe of `resolve_db.CONTAINING_OBEC_SQL`. Every CTE but three scalars and two single-use feeders is `as materialized` (34.8 s inlined, 11.7 s materialized), and every UNION branch carries a literal cohort and no window function, so the gauge's two statements each prune the other's branches. **Apply 698 BEFORE the merge:** the worker reads it with no fallback.
  - **Outside** is strict containment by the pip polygon (ruling Q3), not the 250 m tolerance this section's plan names; asked where CHECK flagged the elected pin.
  - **67 cells**, not about 55, and not all Watch-only (ruling Q7): per portal `town` (unresolved, Problem), `town_withheld` (Watch), `ku` (Problem), `outside` (Problem), `street` and `accuracy`; `served/town`, `served/town_withheld`, `served/outside`, `served/street`; `registry`; `freshness/version` (Watch, Problem on a second reading 6 h on); `freshness/evidence` (Problem past 60 min). Label "Location quality", key kept.
  - **Open, W1-b:** the per-portal street floors (0.9x the 10-31 share) are unruled, so `street` and `served/street` are Watch only until the operator rules. `accuracy` is Watch only until W6.
  - **Day one** (the 10-31 13:09Z export, mid v5.9 sweep): 10 Problem (every portal's `outside` and `served/outside`: 11,568 published properties, bazos 10,459), 6 Watch (ceskereality 26 and mmreality 1 page-withheld, 21 served; bazos and sreality radius cover; the sweep's first version reading), 0 unresolved. `sreality/ku` opens once the sweep reaches 19405830.
- **As built (2026-10-31, ticket #2218, PR 2; the frontend half, no migration; record: `w1-page-reads-cells.md`):**
  - The Location Quality page reads the `location_town_coverage` row through Health's own `HEALTH_QUERY` (a `select` on the same cache entry), so the two pages show identical numbers by construction and Acknowledge works on both. It draws the measured line, a portal × arm matrix in the gauge's order (no per-key table in TS), the fleet strip, the selected portal's grain and disputed-class bars from the cells' facts, and Health's own `CheckRow`; it holds no threshold.
  - Deleted: `/location/quality/summary` and `/location/quality/source/{source}`, `toolkit/location_quality.py`'s aggregates, the page's overview panels, both browser thresholds and its All sources table, the audit waterfall's reader and panel, the pin-audit summary RPC's reader (matrix, state totals, split) and the `!AUDIT POLOH` nav badge. The audit list stays (until W6); its one number is the exact count of the filtered list.
  - Ledger against the brief's ≈ −765/+130: product code −884/+251 (net −633), tests −449/+224 (net −225). Watch: first-visit JS 328,004 / 330,000 B gzip.
  - **Next:** migration B (the waterfall table, its producer, the summary function and `refresh_location_pin_audit_mv()` dropped, the cron re-pointed) follows ruling W1-c; it is destructive and awaits the operator's yes. Apply it only after PR 2's frontend is deployed.

### W2: one queue discipline, one trigger, one runtime
- **Queue order:** the drain claims by `(reason = 'full_sweep'), enqueued_at, listing_id`, so fresh evidence always beats backfill. The sweep enqueues active ads first. The EXPLAIN rail must show that the new index still serves the `next_eligible_at <= now()` filter.
- **Trigger:** the worker sweeps when its (resolver version, registry label) pair changes, under the existing lease. This covers a deploy, a registry publish and a registry load interrupted mid-way (rows resolved during the load carry the old label and are swept after publish).
- **Deleted:**
  - the whole GitHub `location_resolve.yml`;
  - the sweep's red-line arm (13,224 no-op re-resolves a day, 99.8 % inactive);
  - the lane's on/off flag;
  - 5 dead queue reasons;
  - three refetch knobs;
  - every rollout's hand dispatch.
- **Lease row:** retag the `scheduled_jobs` row `location_resolve_incremental` (runner `github_actions`, now only the worker's lease) so liveness cells do not judge a dead runner.
- **Refetch lane:** it gets a constant hourly tick, and its documentation now states the real rule: a townless ad is re-read every 6 h or more while it stays townless.
- **Billing (operator action):** text_extract's failures were OpenAI credits running out. The fix is auto-recharge with a monthly cap, not code.
- **Acceptance:**
  - the next deploy re-resolves itself;
  - new ads get a row within 15 min even mid-sweep (v5.8: up to 3.4 h);
  - all active rows are current within 90 min, all rows within 4 h;
  - every boot runs the sweep, so a SWEEP without a version change must enqueue 0 rows.

- **As built 2026-10-31 (PR #2216, ticket #2215, migration 697 applied AFTER the deploy):** the drain claims by `(reason = 'full_sweep'), next_eligible_at, listing_id` (index `dirty_locations_claim`; `enqueued_at` in the plan text, `next_eligible_at` in the code because retries move it); the worker's `reconcile()` sweeps on a (resolver version, registry label) change, active rows first then inactive, under the maintenance lease (`drain.enqueue_full_sweep`, fence → None); `location_resolve.yml`, the red-line arm, the lane flag `realtime_location_resolve_enabled`, five queue reasons (CHECK 8 → 3 after the relabel to `full_sweep`), the `location_full_resolve` job and three refetch knobs are deleted; the refetch lane ticks hourly (`LOCATION_REFETCH_TICK_SECONDS = 3600`); the scheduled_jobs lease row is retagged `railway-realtime-worker`; the heartbeat carries `lanes.location_resolve.swept`. The CLI keeps dry-run only. Acceptance is measured by W1's `freshness/version` and `freshness/evidence` cells.

### W3: one current claim per fact
- **Rule:** `location_claims` gets the natural key (listing, claim type, operator-or-portal). Both writers use one upsert that writes only when the value changed. The resolver then sees at most one claim per type, which deletes:
  - the seven tie rules;
  - the per-type version window;
  - the readings DELETE;
  - the retire script and its workflow;
  - the 23-argument fingerprint.

  Contract retract stays as the explicit retraction.
- **Provenance:** a claim keeps the contract entry that first stated its current value, so a bump with unchanged values writes nothing. Re-activating an older contract version therefore leaves a type with no admissible claim until it is re-mined, so an activation or retract triggers a full re-mine.
- **Reverted pages:** a body that becomes latest again is re-mined.
- **Validation finding:** precision markers on bazos, idnes and realitymix only detect presence, so no recency rule could ever clear a blur. They will state their absence explicitly.
- **Migration:** back up to R2, then purge the 8.82 M dead rows in batches (a destructive gate). Same-type multiplicity on inactive listings, which the unique key also covers, is unmeasured, so purge step (b) may touch more than the 2.5k active listings.
- **Acceptance:**
  - about 5.95 M rows;
  - 0 duplicate keys;
  - the served pin equals the portal's current pin on at least 99 % of sreality multi-pin ads (today the oldest pin is served on 1,793 of 2,289);
  - 234 wrongly blurred rows read precise.
- **As built (2026-10-31, #2223; design `w3-one-claim-per-fact.md`):** rulings R1–R7 of the design taken as recorded (R1 = Q4 A, R3 a tombstone, R7 no VACUUM FULL). Key `location_claims_key (listing_id, claim_type, licence_class)`, migration 699; one write `claims_store.write_claims` (the operator's hand copy gone); one read `DISTINCT ON (listing_id, claim_type)`, operator first, and **no version filter**, so an activation strands nothing (the plan's "no admissible claim until re-mined" does not arise); a retract still deletes what its version first stated, hence one full re-mine of that source after a retract or an activation. `html_marker` `absent: retract` on bazos@10, idnes@7, realitymix@9 (after W4's @8). Payload revert arm in `payloads._APPEND_SQL`. Companion: an empty location block is a failed text-lane reading. `resolver:v5.9.1`. The purge's keeper is the row the old read served (newest version ≤ active, then newest observation), so the ≈ 15.7 k retired-version keys that are their type's only evidence stay; its receipt names the ≈ 2.8 k listings re-mined after the deploy. The acceptance pin line reads, as in the design § 7: sreality multi-pin ads whose position IS the pin serve the current pin (28 % stand on a register point by design). PR-2 (migration 700, the fingerprint column, index and functions, the purge script) follows one green intake tick.

### W4: the portal's own declaration and typed slot
- **realitymix v8:** the far pins are server geocodes. The page's print map ignores "okres X", which turns Beroun into Moravský Beroun. The "estimated" flag reads the print map, so it never fires. The flag will read the interactive `#map`, the node the portal's own script uses. About 11k pins become correctly declared estimates, and v5.9 then sets the far ones aside.
- **remax v7:** read the header's typed "ulice X" slot. The ruled map attribute exists only on pages without a pin. Expected: street share from 5 % to about 58 %, 94 % binding RÚIAN, pins unchanged.
- **Moved out:** deleting the unread street, house-number and ZIP parsing in 7 parsers is a separate scraper cleanup PR. The contract changes already delete more than they add.
- **Acceptance:** realitymix outside cell under 80 (from 721); remax street share at least 55 %; the standing autodedup gate passes (the shared geocode pins on 51 Beroun and 102 Brno ads stop counting as exact).

- **As built 2026-10-31 (PR #2220, ticket #2219, gate #2222; contract only, no migration, no resolver bump):** realitymix@8's `rm.det.agency_est_flag` reads `div#map` alone (3,622 of the 3,656 rows declared today had both nodes blank; verified on 112 pages: fires on exactly the 46 hidden ones, moves no pin). The dead C6 `odbl` licence branch is deleted (−145/+120 outside fixtures). **remax v7 is NOT built: ruling Q8 (the header slot is not read).** Acceptance corrected to ≤ 110 outside (68 undeclared agency pins and 6 placeholders stay until W5), 0 declared outside, 0 off-grid declared, declared pinned rows inside 7.4k–19,527. Merge order: after W2 is live; rollout = `gh workflow run location_claims_intake.yml --ref main` by hand (~102k bodies, ~1 h); rollback entry-scoped (`--retract realitymix@8 --extractor-id rm.det.agency_est_flag`), never whole-version. The standing shadow gate runs through #2225 (wave shadow) before the re-mine.

### W5: resolver v5.10
**As built** (#2251; design and measurements: `w5-resolver-v5-10.md`). `RESOLVER_VERSION = "resolver:v5.10"`; W2's reconcile sweeps on deploy, no migration, no hand step.
- **Position (Q3, one truth):** `bind.place`'s `may_place`. Where the words bound the row, a pin is its position only inside the bound obec's pip polygon (strict: no tolerance, so no registry distance read) and never when the portal declares an area marker (`AREA_MARKER_LABELS`: the `approximate_location` bazos already emits on all 52,185 active pins, so no contract bump). Without words (R7/R8) the pin names its town. v5.9's declared-blurred exception is deleted; CHECK keeps `pin_outside_obec` as evidence and loses its coarsening.
- **Grain = `grade.grain_of(keys)`**, the one writer (`grade()`, `Grade`, `Binding.granularity`, `Verdict.granularity` and `coarser_of` deleted).
- **Quarter = část obce only (Q1):** `MATCH_LEVELS = ("cast_obce", "momc")` and `composite.part_of` (a část itself, a KÚ the část its name spells, a MOMC nothing). MOMC stays a match key: the 1,607 remax "Praha N" rows still bind Praha. Správní obvod and ZSJ are no level. Part claims match like town names; a street keeps the claimed část when it has doors there (Q2).
- **KÚ fix:** an address point with no coordinates takes its část's door KÚ (19405830).
- **Determinism deferred:** the `ORDER BY` in `CONTAINING_OBEC_SQL` waits for the next re-render of the scorecard view (0 of 6,553 sampled pins lie on a shared border).
- **Shadow:** the `ids` export block is built, so the about 5.4k Praha rows that gain a quarter are judged; dispatch per slice (`wave-shadow.md`).
- **Consumers:** labels lose "Praha 5, " on about 935 rows and 1,899 gain their část; the location ad can change where an ad's grain changes; the bazos map "±" grows to the town's reach.
- **Acceptance** (before: 2026-10-31 16Z):

| Measure | Before | After |
|---|---|---|
| `listing/{p}/outside` (strict, Q3) | 1,359 | register slivers only: bazos ≤ 6, ceskereality ≤ 3, bezrealitky ≤ 1 |
| `served/{p}/outside` | 1,105 | the same residual |
| Position is a pin outside the bound obec | 1,353 | 0 |
| bazos words-bound rows on their pin | 23,297 | 0 |
| Grain differs from the finest key | 736 | 0 |
| MOMC codes in `cast_obce_kod` | 2,834 | 0 |
| Wrong-MČ rows | 518 | 0 |
| Quarter grain with no část code (SO ties) | 200 | 0 |
| Street rows without a část | 18,724 | ≤ 6,400 |
| Praha with no quarter | 9,308 | ≤ 4,800 |
| remax "Praha N" bound to Praha | 1,607 | 1,607 (about 1,600 with a část) |
| `sreality/ku` | 1 | 0 |
| `location_town_coverage` | W1 reading | no redder |
| `disputed/pin_outside_obec` | design §0 | about the same (evidence stays) |
| Shadow gate | — | PASS on every slice |

Residual, named: about 10 register-point slivers (not pins) and about 300 ceskereality ads with only a pin (R7: the pin names the town, the words cannot judge it).

### W6: one honest radius (v5.11)
Today the 1 km obec constant contains the truth 82 % of the time: 29 % for bazos, 100 % for remax.
- **New rule:** the radius is decided by how the point was made.
  - Address point: 10 m.
  - Register placement: the entity's own reach. This is a sure bound given the bind; obec placements still cover only 87.3 %, because about 12 % of town binds disagree with the truth, and the scorecard counts those.
  - Portal pin: a generated table keyed by (source, declared label), as validated, with no grain key. Each cell is a split-conformal 95 % quantile of pin-to-truth errors, where the truth is the address point of the same property. That truth reaches sreality's blurred labels, which never bind an address point themselves. A cell with fewer than 100 truths pools to its declared class.
- **Target:** obec coverage at least 93 % and median pin radius at most 60 m. The pre-v5.9 held-out run measured 95.3 % and 39 m; recalibrate after v5.9.
- **No point, no radius:** NULL replaces the 250 km sentinel.
- **Deleted:**
  - the hand table;
  - the dead declared-radius path;
  - `match_confidence`, together with its readers: the pin-audit matview (mig 524), its page and lib, and the Location Quality inspector column.
  - from W3: the pin-audit matview's `claims_now` ("a claim under an ACTIVE contract"), false under W3's first-stater provenance once a bump restates values unchanged; it goes with the matview. If the matview outlives W6, fold `claims_now` into "a claim of a type its active contract declares" instead. Its one reader selects it and renders nothing (`frontend/src/lib/pinAudit.ts`).
  - from W3: `claim_confidence` (ruling R6), written only by the operator ('exact') and shown only by the inspector; it holds the `match_confidence` enum.
- **Monitoring:** the W1 accuracy cell watches drift. Recalibration after a contract change is the one standing hand step; the cell turns Watch to trigger it, and the table header records the contract versions it was fitted on.
- **UX consult:** the popup "±" changes, and blurred classes hit the 2 km drawn-circle cap more often (bazos about 3 to 4 km after v5.9).
- **Deploy gate:** autodedup's `dist_norm` inputs shift, so the wave shadow must pass on the rulings ledger inside the affected blocks (the 413 post-2026-10-27 `same` pairs must hold). `DECISION_MODULES` stay untouched.

### W7: registry
- **Praha's okres:** obec Praha and ORP 19 take okres 9999 from ČÚZK's own SHP pack. A load assertion guards the value; reading each obec's okres from OBCE_P would remove the constant altogether.
- **Retired:** the conflated obvod/správní-obvod level (including a Praha unit named "Kutná Hora"), 23,604 ZSJ units that can never bind, 6 unread columns, and the contradictory CI Praha trees.
- **Rollout:** rides the next vintage load (the 11-01 vintage if W7 is ready, else 12-01 or a dispatch), and W2 sweeps the corpus by itself.
- **Acceptance:** 0 active Praha rows with okres NULL (from 50,766), and a Praha cell in okres pivots.

## Unification

| One... | Replaces |
|---|---|
| ladder of levels (partitions down to obec) | quarter field mixing část and MOMC; Praha with no okres; SO/ZSJ levels |
| precedence rule | 7 tie rules + 3 supersession paths (version window, readings DELETE, retire script); retract stays explicit |
| place-name matcher | 3 matchers that bind one string three ways |
| containment test (250 m) | declared-blurred special case, CHECK coarsening, strict CHECK flag, hand gate SQL |
| grain definition | 2 grain writers, enum-order comparisons, 2 meanings of "street" |
| radius (CE95 for pins, reach for register placements) + one truth query | per-grain constants, sentinel, dead declared radius, unread match_confidence |
| metric set | gauge + LQ page + waterfall-style hand SQL with 3 town-share definitions |
| queue order + trigger + runtime | FIFO, GitHub cron, hand dispatch, daily no-op sweep |
| street source | contract claims vs dead parser streets (cleanup PR) |
| fetch-failure rule | per-portal challenge guards (gone signals stay per portal) |

## Deferred or rejected (headline)
- **Deferred:** retiring the legacy `admin_boundaries` table (its own track; after W7 both registries agree on Praha).
- **Deferred:** a precise pin deciding the část through its KÚ (2,188 Praha rows). It reverses a ruling and adds without deleting; ask again after W5.
- **Deferred:** a radius gate in comparables (an estimation methodology change).
- **Deferred:** remax house numbers from index cards (a new surface; deletes nothing).
- **Rejected:** a served MOMC level.
- **Rejected as patchwork:** MOMC kept as a fallback in the quarter field.
- **Rejected:** a DB CHECK for grain (a second spelling of the rule).
- **Rejected:** `browse_list.disputed` with a popup caveat.
- **Rejected:** a cross-portal placeholder-pin list. idnes' two Brno placeholder points lie inside Brno, so the containment rule cannot replace them and they stay contract-local.

## Not covered (named, not built)
- **Qualifier scoring:** an obec R4 with two qualifiers (55) beats a claimed part R4 (50). This is unmeasured. **Deferred explicitly by W5** (2026-10-31): v5.10 changes no score (design § 10).
- **KÚ below address grain:** 162,791 obec-grain and 54,379 street-grain rows have no KÚ (all rows). W1 shows coverage by grain. Most of the gap is structural: multi-KÚ streets and towns. W5's část lending closes it where a část lies in one KÚ.
- **Issue 2 residue:** 10,408 town-only rows on the obec point (idnes 9,352) are structural, because the source withholds finer location. The row already says so honestly (obec grain, open ring, reach radius). No action.

## Questions for you (answers change the build)
1. **Quarter = část obce only** (recommended), or MOMC as a sixth level? About 960 MOMC-only labels lose "Praha 5, " in Browse and the extension. (The 201 SO-tie rows show no quarter today.) In return, 1,893 rows gain the correct část and 518 wrong-MČ rows are fixed.
2. **Amend Q3 of 2026-09-30** so the claimed část chooses among the street's door-parts? Recommended: yes. In the same ruling: may part claims climb a same-named KÚ to its část (about 1,460 rows, e.g. "Nové Sady u Olomouce")?
3. **Extend "the words decide" to every pin**, with the existing 250 m tolerance? The only truth (n = 36 non-bazos outside pins with a door truth) favours the pin: median error 357 m, against 2,964 m for the town point, with the pin closer in 56 % of cases. After W4 declares realitymix's estimates, the rule mainly moves about 315 pins that sit 0.25 to 5 km outside their town (sreality 88, idnes 129, realitymix 61, ceskereality 37); 274 border pins stay. Recommended: A with the tolerance only if a per-bin truth read after W4 shows the town point beating the pin beyond 250 m. Otherwise B: keep exact pins and bind their row at obec grain.
4. **Absence:** when a page stops stating a field, which option?
   - A: keep the last stated value, with markers stating absence explicitly.
   - C: as A, except that a new extractor version re-states the full set, so its silence retracts.

   C matters for the 630 towns and 397 streets where a newer model re-read the same text and returned null. Which value is right there is unmeasured. Recommended: measure those cases, then rule A or C.
5. **Operator corrections:** fold into the rule (recommended) or retire? **Radius:** CE95 for pins and reach for register placements, NULL without a point, and delete `match_confidence` together with its pin-audit readers (recommended)? Consult on the popup "±" and the 2 km cap.
6. **Town gauge:** split withheld (Watch) from unresolved (red)? Recommended.
7. **New cells:** Watch-only against each portal's own 28-day median? Recommended.
8. **remax street from the header's "ulice" slot**, lifting W2-6 for that slot only? Recommended.
9. **Worker as the only resolve runtime**, with the red-line arm and the GitHub workflow deleted? Recommended.
10. **Praha's okres = 9999** under ČÚZK's name? Recommended.

## Corrections to the briefing
| Briefing | Measured |
|---|---|
| 9,528 / 50 coarsened rows; 59,815 / 22,623 street rows without část; 463 code-less; 6,198 MOMC; 178,760 Praha okres NULL; 855 + 97 region-only | all-rows figures. Active: 3,415 / 31; 18,832 / 6,205; 201; 2,855; 50,766; 0 + 25 |
| radius not drawn | drawn for the opened pin (capped at 2 km), and the popup prints ±N |
| contracts bazos 9, bezrealitky 4 … | those are versions. Entries: 8/9/5/10/6/7/10/6/11/5 = 77 |
| containment town 60.4 %, quarter 86.1 %, street 98.5 % | pre-v5.8. Now obec 82.6 %, quarter 94.0 %, street 98.3 % |
| text_extract about 20 % failing | 16.4 % lifetime; OpenAI credits ran out 10-20 to 10-26; 0 readings lost; clean since 10-27 |
| refetch daily, one more fetch | about 14 runs a day (one per boot); every 6 h or more while townless |
| GitHub drain 0.7 rows/s | 87 to 108 rows/s |
| realitymix far pins undiagnosed | pin wrong, name right: print-map server geocode; the contract note is false |
| 2,823 multi-pin, 136 towns, 86 streets | 2,289, 104, 50; all are portal edits over time |
| #2102 open | closed 10-30 21:10Z. #1792 still open; #1634 also drops has_polygon |
| ZSJ binds unmapped | ZSJ can never bind (0 name-index entries) |
| ČÚZK publishes no Praha okres | the SHP pack and admin_boundaries carry 9999 |

## Corrections to this plan (review, 22Z)
| Plan said | Measured or read |
|---|---|
| about 165 sreality GPS pins 0.25 to 5 km lose precision | sreality 189 outside: 78 within 250 m, 88 at 0.25 to 5 km, 22 at 5 to 20 km, 1 beyond; all portals at 0.25 to 5 km: 315 |
| about 1,160 rows lose "Praha 5, " | about 960; the 201 SO-tie rows carry no quarter name today |
| mmreality's challenge branch folds into W0 | half of it is a gone signal raised inside the parser; it stays |
| W3 replaces four supersession paths | retract stays; 4 → 2 |
| radius never keyed by grain, yet the W6 table was keyed by grain | the validated cells were (source, label); grain is dropped |
| W6: 95 %, 39 m | pre-v5.9 held-out figures; the targets are at least 93 % and at most 60 m |
| W1 cell leaves fail after W0 and Q6 | sreality 19405830 keeps it red until W5 |
| W2: no SWEEP without a version change | every boot sweeps (the tuple is process-local); the acceptance is 0 rows enqueued |
| outside cell 0 after W5 | only with the tolerant test; strict containment leaves 274 |
| view admin-gated like mig 427 | 427 is granted to authenticated; this view is granted to service_role only |
| MOMC leaves PART_LEVELS | it must stay a match key: 1,432 remax town lines are MOMC names |

## Patchwork flagged
- **Being deleted in this sprint:**
  - the CHECK grain overwrite;
  - the Q3 gates;
  - the unit-id tie between levels;
  - the OP/SO hybrid level;
  - the dead ZSJ lookups;
  - seven tie rules;
  - the time-free fingerprint;
  - the manual retire workflow;
  - the red-line sweep arm;
  - browser-held thresholds;
  - the realitymix DOM-order selector.
- **New additions, each named and paid for:**
  - the scorecard view, paid for by the W1 deletions;
  - the generated radius table and its standing recalibration, paid for by the hand table, the sentinel and two fields;
  - the 250 m containment check and its registry distance read, paid for by the declared-blurred branch and CHECK's coarsening;
  - the match-key level list, paid for by the R4 all-levels matcher.
- **Not paid for, and said so:** W0's shared rule, a bug fix for rules 2, 4 and 5.
- **Rejected because it would be patchwork:**
  - a MOMC fallback in the quarter field;
  - per-reader junk-pin lists;
  - a radius floored to the pin's distance from the town;
  - a daily dollar cap in the text lane;
  - a Portal seam no portal needs;
  - a heavy view granted to every signed-in user.
