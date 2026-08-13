# Location-data program (greenfield location SSOT)

The market-wide location-data rebuild designed in the 2026-08-10 scoping engagement:
a three-layer truth model (append-only `location_claims` → pure-function
`location_resolutions` → rebuildable serving projections) on a RÚIAN identity spine,
with a four-axis precision taxonomy and structural licence enforcement. The
authoritative plan lives OUTSIDE the repo (operator-held:
`~/location-data-architecture-2026-08-10/design/final/MASTER.md`; `00-shared-contracts.md`
is the tie-breaker). This track records sequencing + shipped state only.

## Wave status

| Wave | Scope | Status |
| --- | --- | --- |
| W0 "stop the bleeding" | 15 interim fixes + 2 measurements against the CURRENT system | 🟡 in progress (2026-08-10) |
| W1 registry + claim spine (shadow) | full RÚIAN mirror, claims, resolutions, projection | ✅ shipped 2026-08-12 (migrations 380–389 applied; shadow-only, no consumer reads it) |
| W1v bezrealitky vertical slice | one portal end-to-end + location-quality dashboard | ✅ shipped 2026-08-13 (every layer exercised in prod; gate answered — portal-inventory-capped, not pipeline-capped) |
| W2a payload archive rewrite | append-on-change `portal_raw_payloads` | 🟡 in progress (2026-08-13) — instrument live and measuring; store + writer landed, dual-write NOT enabled (churn sign-off is the gate) |
| W2–W6 | HTML re-mine, history backfill, refetch cohorts, LLM lane, serving flip | ⚪ not started |

## W0 — done

- **0o archive preservation** (#995 + hardening #1005): inventory recorded (447,164
  pages / 14 GB; oldest page per source == portal onboarding date — nothing ever
  pruned), chunked+resumable R2 export, CI guard against DELETE/TRUNCATE/DROP of
  `portal_raw_pages` (supersedes migration 099's "safe to delete" comment).
- **0n index-page archiving re-enabled** (#996): sreality (raw index JSON: geohash,
  POI distances, locality.geometry), remax (data-display-address), ceskereality (map
  markers); week-stamped accumulating keys + client-side freshness skip + probe guard.
- **R1 Mapy kill switch** (#997, operator action A3): `MAPY_GEOCODE_ENABLED` default
  OFF; geocode client unreachable; display-only tiles/suggest unaffected; URL-parse
  estimations fail fast pre-spend with an honest 422 while off.
- **0a resolver-street auto-write stopped** (#998): weekly cron removed, writes need
  explicit `--write`; dead sreality precision guard (`locality.accuracy`) replaced
  with `inaccuracy_type IN ('gps','address')`.
- **0b+0c zip fixes** (#999): sreality `-1` sentinel → NULL; bazos `raw_json.psc` →
  `listings.zip` (+ standing-corpus backfills post-merge).
- **0d remax carousel fix** (#1000): street/locality from the subject's own
  `.pd-header__address`; `data-address` demoted to evidence
  (`raw_json.carousel_address`); poisoned backfill arm disabled.
- **0m bezrealitky registry key** (#1001): GraphQL detail query requests `ruianId`
  (kód ADM) + `addressInput` + `regionTree` — the W1v precondition.
- **0g/0i/0j street-extraction fixes** (#1002/#1003/#1004): mmreality originalTitle
  "ul." fallback; bazos numeral street names + Lokalita trailer; ceskereality
  accented street + okres from `<title>`.
- **Measurements**: realitymix out-of-bbox = 0 (gate met, guards pre-shipped);
  sreality legacy-payload share 8.4% of active (sampled n=2,110, 2026-08-10 — down
  from the recon's 32%); bazos backfillable PSČ 60,266 rows.

## W0 — remaining

- ~~0b/0c standing-corpus backfills~~ DONE 2026-08-10: sreality sentinel 43,371→0;
  bazos psc→zip 60,278 rows (30,013 active — gate ≥29k MET); PLUS the 0d repair
  (5,591 carousel-derived remax streets nulled + dirty-enqueued).
- Post-merge gate verifications: index rows accumulating ✅ (243 sreality pages in
  2h, week-stamped keys); geocode silent ✅ (last geocode_cache write 2026-07-11);
  ruianId collecting ✅ (41 rows in 40 min). R2 export VERIFIED ✅ 2026-08-10 (snapshot 2026-08-10b:
  447,510 rows / 801 chunks / 7.69 GB gz; manifest == chunk sum == DB count; the
  cancelled first run's partial `2026-08-10/` prefix in R2 is garbage, safe to
  delete). Still open: resolver-write rate 0 over 7 days (structurally true;
  confirm 2026-08-17); bezrealitky ruianId ≥95% of NEW rows (measure after a few
  drain cycles).
- 0k idnes disclaimer + 0l remaining declared-quality signals: forward-preserved
  already (detail HTML archived + raw_json); querying them as claims is the W1
  loader's job — no interim side table needed. ceskereality's `exact` map-endpoint
  flag is the one true gap (new endpoint; deferred per the plan's implementation
  order, open question Q17).
- 0e residue: accent-fold in realitymix `_town_from_url` display locality (bbox gate
  already met; geocode use mooted by the kill switch).
- Operator action items A1 (ČÚZK helpdesk), A2 (quarterly licence review), A4
  (Supabase plan/tier price — blocks W1 sizing), A5 (filter semantics default) —
  surfaced 2026-08-10 with written defaults.

## W1 — shipped (registry + claim spine, shadow-only)

Landed 2026-08-10 → 2026-08-12 as five feature PRs plus seven fixes. **Migrations 380–389,
all applied to production.** Everything is **shadow-only**: nothing outside `location_data/`
reads a claim, a resolution or a projection — Browse, the map, the watchdog and dedup still
run on `listings.geom` and the geo-derived admin columns. The consumer flip is W6.

| PR | Scope | Migrations |
| --- | --- | --- |
| #1009 | PR-A the W1 schema — every location enum + config seed, the RÚIAN mirror, the claim spine + portal contracts, resolutions + policy, the two serving projections + collision/ledger/ops tables. Additive, backend-only (RLS on + explicit `anon`/`authenticated` REVOKEs on every table, sequence and function) | 380–384 |
| #1008 | The five-arm **R2 Mapy affected-set inventory** — a W1 *input*, shipped ahead of the spine: set A materialised into immutable evidence tables carrying identity and reason codes, never a coordinate (06 §6.1.5 class-E carve-out) | 385 |
| #1010 | PR-B **RÚIAN loaders** — the `KrovakPositive` value object with ONE audited WGS84 conversion on an explicitly chosen 1 m PROJ pipeline, streaming CSV of both products (sha256 + etag + last-modified per artefact), baseline load (staging → blocking assertions → pointer swap) stamping one `registry_version`, boundary packs, gazetteer rebuild, `location_registry_load.yml` | — |
| #1012 | PR-D **portal contracts as data** (9 YAML → `portal_contracts`/`_entries`, git stays the store of record) + the batched **claims-intake extractor** over `listings.raw_json` for all nine sources + its hourly cron | 386 `location_claim_fingerprint()`, 387 intake resume cursor |
| #1013 | PR-E **the resolver** — S1–S9 as a pure function (no clock, no network, no randomness; AST-enforced), both projection builders, the collision-epoch producer, the `dirty_locations` drain, S9 reconciler v1 | 388 (review remediation) |
| #1014–#1019, #1023 | Fixes — contract-projection Jsonb bind; boundary-loader reconnect/resume; drain throughput rounds 1 and 2; batch-lane hardening; contracts v2 coverage repair; byte-bounded claim writes | 389 (three registry indexes) |

**Live state** (production, 2026-08-12):

- **Claims** — 4.03 M+ over 655 k listings; **zero** `ephemeral_display_only` rows persisted
  and **zero** `claim_type='coordinate'` rows on the 57,204-listing Mapy inventory. The licence
  ladder is holding structurally, not by convention.
- **Resolutions** — 645 k+ with their candidates; the full corpus swept with **zero failed
  listings**. Both projections (`listing_location_current`, `property_location_current`) are
  built from them and the contradiction ledger is active.
- **RÚIAN mirror** — registry version `ruian:2026-07-31`: **3,020,222** address points (golden-point
  Křovák→WGS84 check **0.03 m** on the pinned PROJ pipeline), **20,034** polygonal units each
  carrying three geometries (authoritative + subdivided pip + render), gazetteer **217,515** names.
  Boundary packs resume through per-layer done-sets; monthly baseline cron + the boundaries lane.
- **Contracts** — 9 portal YAMLs projected, **v2** for remax and **v3** for ceskereality /
  realitymix; intake runs hourly-incremental with byte-bounded chunked writes.
- **Refetch cohort** — **38,612** sreality rows (legacy-shape or 80 KB-truncated payloads) parked
  in `location_enrichment_state(lane='sreality_detail_refetch')`. **W4 work.**

**Gate outcomes (final, measured 2026-08-13, all PASS):**

| Gate | Requirement | Measured |
| --- | --- | --- |
| Registry + sign trap | national CSV, 19 cols; golden-point round trip | 3,020,222 points; kód ADM 21690278 at **0.03 m** (pinned PROJ pipeline) |
| Claim coverage | ≥99 % of active listings ≥1 claim | **99.18 %** (382,901+/386,065; 4.91 M claims) |
| Licence (blocking) | 0 ephemeral claims; 0 coordinate claims on the inventory | **0 / 0** (inventory: 57,204 listings, terminal) |
| D3 axes | ≥98 % sreality post-cutover; 100 % mmreality | **100 %** (94,113/94,113) / **100 %** (10,731/10,731) |
| Deterministic replay | byte-identical on unchanged inputs | **PASS** — 1,000-listing production sample, before/after hash identical, 0 new resolutions; + hermetic CI test |
| PIP latency (Q7) | p95 < 5 ms | containing 0.24 ms / nearest-within 2.95 ms |

Corpus state at gate time: 725,164 resolutions (zero failed listings), 3,903 contradiction
detections, epoch 2 current, subsystem ≈16 GB (observations carry ~7 GB of one-time
re-scan bloat — a same-day observation dedup guard is queued follow-up work).

### Decisions worth carrying forward

- **The licence ladder is stronger than `carry_forward`.** The blocking gate is
  `claims JOIN <R2 inventory> WHERE claim_type='coordinate'` = 0, so presence in `mapy_affected`
  vetoes a coordinate on **every** substrate, including the three portals whose pin is
  first-party payload. The lane refuses to start unless that inventory is TERMINAL **and**
  COMPLETE (a `resumable` run at `status='completed'` in the current `restart_epoch`, not merely
  `count(*) > 0`) — a half-built inventory is worse than none, because every listing past its
  high-water mark reads as *absent*, which is exactly the verdict that admits a Mapy-derived
  coordinate as first-party.
- **`claim_fingerprint` is computed in SQL**, from the same `location_value_norm()` the column
  uses, wrapped as migration 386's IMMUTABLE `location_claim_fingerprint()` so W2's re-mine, W3's
  snapshot backfill and the LLM lane reuse the definition instead of re-transcribing it.
  PostgreSQL's `unaccent` is a dictionary (ß→ss, ø→o, đ→d …) and a Python NFKD mirror drifts on
  exactly the foreign-address cohort — drift there means the unique index stops deduping,
  silently, in an append-only table. A diagnostic mirror + a parity battery keep it documented.
- **Legacy entries never burn a permanent extractor id.** 02 §2.2.3's ids are fixed on the W2
  *HTML* parses; the raw_json / `listings.geom` mirrors of the same facts ship as
  `bzs.det.legacy_psc` / `bzs.det.legacy_link_pin` / `id.det.legacy_pin`, so the two provenances
  stay distinguishable in `location_claims.extractor_id` when W2 lands.
- **Withheld coordinates and unreadable payloads are recorded, never silent** — a class-E row gets
  a `location_claim_absences` row; sreality's legacy-shape / truncated rows are routed to the
  refetch lane above.
- **Contracts v2 was a coverage repair, not an edit** (2026-08-11). W1's gate is "≥99% of ACTIVE
  listings carry ≥1 claim"; production measured 97.66% with realitymix, ceskereality and remax
  owning 8,720 of the ~9,000 zero-claim rows. Entries are immutable (02 §2.1.8), so the fix was
  three **version bumps**: `rx.det.legacy_display_address` (W0's 0d moved remax's subject header
  and v1 only read the banned `/address`) and `rx./cr./rm..det.legacy_locality` (a new
  `legacy_text_column` reader over `listings.locality`, capped at `claim_confidence='medium'` and
  flagged `legacy_write_path_unknown` **from the contract**).
- **v3 guards a legacy column on its PROVENANCE, not on NOT NULL** (2026-08-13). v2 left the gate
  at 98.94% (4,109 zero-claim ACTIVE rows), and on ceskereality's 3,280 the payload
  `locality_text` *and* `listings.locality` are both NULL — `listings.street` is the last
  W1-readable signal. 06 §6.1.3 classes that column per **writer**, so `cr./rm..det.legacy_street`
  read it through a generic `locator.require_column_equals: {listings.street_source: parser}`:
  the class-B parser arm is admitted, the class-D `resolver` (RÚIAN inference) and NULL (legacy
  writes) arms are refused before they can become claims. The guard is contract DATA, not a portal
  branch, and it is the reason those two entries declare `write_path_unknown: false` — a guard
  that names the writer answers §6.6 rule 3. Measured recovery: **+957** ceskereality rows →
  **99.18%**, clearing the ≥99% gate; realitymix contributes **0** (all 243 of its street-bearing
  zero-claim rows are `street_source IS NULL`) and gains ~10k street claims on already-covered
  rows instead. A `street IS NOT NULL` guard would have bought those 243 by admitting class D.
- **Throughput was round trips, not indexes.** The drain went 3 s/listing → the current rate in
  two rounds, and round 2 measured the floor: a GitHub-runner↔Frankfurt round trip prices at
  **~75 ms** while the same registry questions cost **0.02–0.5 ms** server-side, so the rate was
  `1 / (75 ms × trips)` and nothing else. The fixes were all I/O-layer, the pure core untouched
  and replay still bit-for-bit: the session pooler (`connect_session`, with a logged fallback),
  run-memoized registry + collision views, per-slice prefetch and warming, `executemany` writers,
  and slice-batched writes (~10 statements per 250-listing slice, optimistic under one savepoint
  with the per-listing SAVEPOINT path as the retry). Two genuine plan faults were fixed as query
  *shapes*, not indexes — `cast_obce_for_point` 2,944 → 0.15 ms/point and `nearest_obec_within`
  6,752 → 2.95 ms/point (`ST_DWithin(geom::geography, …)` cannot use the geometry GiST index).
  From Actions the drain now runs at **~5–17 listings/s and is network-RTT-bound**; the remaining
  order of magnitude is a placement on the always-on Railway worker (~1–2 ms RTT to the DB),
  where every one of these fixes compounds. Per-query-KIND counters mean each run names its own
  offenders (`BATCH n=… rate=…/s`, `BATCH queries …`, `DRAIN cache misses …`).
- **h3-pg is NOT available on this instance** — the stated fallback shipped: the rounded 4-dp
  `location_geo_cell_key` with a MANDATORY 3×3 neighbourhood expansion at query time. `h3_r10`
  stays a nullable additive upgrade slot.
- **Uncalibrated by design, and flagged in the seeds themselves:** `threshold_n` (01 OQ3) and
  every R95 radius (01 OQ4, seeded `geometric_bound`/`declared`, never `r95_empirical`).
  Calibration writes a new `policy_version`. Only portals that genuinely publish a shape (maxima
  `Circle.radius`, sreality `locality.geometry` bbox) get `derivation='declared_shape'` rows with
  `r95_m` NULL — one row can never be both (01 §3.3.1). A `declared_shape` row with no declared
  shape **degrades** to the `'*'` row and then to the admin geometric bound; it never raises
  (with the v1 seed it raised, and every sreality portal-pin listing resolving at `obec` /
  `cast_obce_or_quarter` / `street` got no resolution at all — migration 388).
- **Erratum to the design corpus:** 00 §1.5 says the `uncertainty_radius_m` + `radius_semantics`
  pair is NOT NULL "on the claim, the resolution, the candidate and both projections". 01 §4.2
  owns the DDL and wins: at claim grain there is only a nullable `declared_radius_m` (a claim
  records what the portal declared, and most declare nothing). The NOT NULL pair is real on the
  resolution, the candidate and both projections. Recorded in migration 382 at the column itself.
- **W1 runs no evidence-bearing method.** `regex_text` / `llm_text` entries are declared but
  unexecuted until W2a's content-addressed payload store makes a span re-verifiable.

### Open, carried forward

- **The per-portal frozen labelled samples (n ≥ 100/portal) are the gate on each CONTRACT** —
  they decide whether it resolves or stays in shadow. The machinery shipped in W1v (migration
  399 + `scripts/location_draw_labelled_sample` + `location_labelled_sample.yml` + the
  labelling surface and live floor scoring on the Location quality page). **bezrealitky's
  sample is drawn and frozen: 120 rows, 0 labelled.** Each remaining portal needs its sample
  drawn BEFORE its W2 sweep (one dispatch, seconds); the 2–4 h of hand-labelling per portal
  stays operator work and can trail the sweep — an unlabelled contract sweeps in shadow.

## W1v — shipped (bezrealitky vertical slice)

**The first consumer of the location stack is live in production.** Four PRs on 2026-08-13:
**#1041** the spine (migrations 399–400 + the operator claim producer + the admin-gated
`/location/*` API), **#1043** the Location quality page (Settings → Location Quality),
**#1044** + **#1046** two production defects the live round-trip caught (below).

- **Migration 399** — frozen labelled samples (06 §6.4.0): membership frozen at draw, the
  OLD system's serving values snapshotted at draw time (the refetch that follows a draw
  rewrites `listings.street`, so scoring "the old system as it stood" needs them as they
  stood), one `is_current` sample per source. No coordinate is ever copied (class-E carve-out).
- **Migration 400** — operator rows in `location_field_policy` v1 (rank 50,
  `may_overwrite_non_null`). The v1 seed had no operator producer and `evaluate_field` SKIPS
  a claim with no policy row, so an operator correction would have won the pin and silently
  lost every FIELD. W1 could not observe the gap: no operator write path existed.
- **`location_data/operator_corrections.py`** — the append-only operator claim + an
  **UNCONDITIONAL** `dirty_locations` enqueue (the fingerprint is time-free, so an A→B→A
  restatement inserts nothing and an `ins`-gated enqueue would be a dead button), then a
  synchronous single-listing resolve for 05 §5.5.5 read-your-writes.
- **The dashboard** reads the projection ONLY, every panel grain-labelled.

Sequencing executed (order is load-bearing): sample drawn and frozen FIRST (120 rows,
06 §6.4.0 "drawn before the sweep") → 5,233-row refetch cohort enqueued at `priority = -1`
(strictly behind real-time discovery; source-scoped claims mean no cross-portal starvation)
→ realtime worker drained it at production politeness, zero failures → hourly intake →
`*/15` resolve drain.

### The gate — answered honestly, and OQ2 with it

| Measure | Result |
| --- | --- |
| Published `ruianId` resolving to **exactly one** current address point | **2,775 / 2,781 = 99.78 %** |
| Residual | 6 rows (0.22 %) whose kód ADM is in **no** mirror vintage — portal keys ahead of `ruian:2026-07-31`; the resolver's own `kod_adm_not_in_mirror` trace |
| Active rows carrying a published `ruianId` | **2,781 / 5,746 = 48.4 %** |
| Structural ceiling for `address_point`/`building` | ≈ **67 %** (2,781 R0-eligible + 1,075 R1-eligible) |

**OQ2 is answered and the recon's expectation is corrected.** bezrealitky publishes `ruianId`
on **48.4 %** of active adverts, not ~100 % — the live-probe sample was 4/4 rows and the
field is JSON `null` on street-less/approximate adverts. That was flagged DOC-CLAIM-ONLY and
the risk was real. **Both numeric gate arms therefore fail on portal inventory, not on the
pipeline**: every key the portal publishes resolves, essentially perfectly. The design
anticipated exactly this ("the slice still ships, and OQ2 is answered either way"), and
W1v's actual deliverable — every layer exercised against real data in production with a
visible artefact — is met.

### Read-your-writes, verified live (listing 156144, nám. Budovatelů 1415/5, Karviná)

`street_segment` / `portal_pin` / `medium` / r=100 m / no kód ADM →
**`address_point` / `registry_point` / `exact` / r=10 m / kód ADM 24252301**, plus číslo
orientační `5` and PSČ recovered — the čo the old pipeline drops on 4 of 5 rows that have one.

### What the live check caught that no offline gate could

Two production defects, both invisible to CI, and the argument for keeping the live
round-trip as a gate rather than a nicety:

- **#1044** — `location_data/` was never COPY'd into the service image. W1 was shadow-only,
  so nothing on the API had ever imported it; the route's lazy import kept boot alive and
  turned it into a 500 on first use instead of a boot crash.
- **#1046** — the operator claim INSERT passed `NULL::boolean` for
  `legacy_write_path_unknown` (`NOT NULL DEFAULT false`). PREPARE type-checks without
  executing and a fake connection cannot raise a constraint, so both offline gates passed it.
  The fix ships a regression test that parses the 382 DDL for NOT-NULL-with-default columns
  and forbids NULL overrides.

### Finding carried to W2/W6: the registry's canonical street form is available but not adopted

On R0-bound rows the mirror's own street name is one join away, yet **153 of 3,121 (4.9 %)**
registry-bound bezrealitky rows serve a different string, and **344** bezrealitky
`street_not_in_obec` contradictions stand open. Characterised, the two populations are:

- **Case convention** (the bulk): the portal title-cases the non-initial word where RÚIAN
  lowercases it — `Na Strži` vs `Na strži`, `V Zahradách` vs `V zahradách`. Plus one Unicode
  Roman numeral (`Ⅰ` U+2160 vs `I`) and one genuine typo (`Jungmannová`/`Jungmannova`).
- **Dropped prefix** (what fires `street_not_in_obec`): the portal omits `nám.` / `tř.`, so
  the normalized gazetteer join misses — listing 156144's `Budovatelů` vs `nám. Budovatelů`.

Root cause: S7's registry fill is preserve-if-null, so no registry-derived `street_name`
claim is emitted and policy v1's `('ruian','registry_derived',100)` row — which would beat
the portal's 300 — never gets a chance. **Not fixed in W1v on purpose**: it changes
resolution output for every registry-bound row corpus-wide (sreality is 26.9 % kód ADM), so
it is a W2/W6 decision, and the contradiction ledger is meanwhile doing exactly its job.
- **Operator items:** **A1** (ČÚZK helpdesk) — letter drafted, awaiting send. **A5** (filter
  semantics default) — still undecided, and it is a serving-layer decision W6 needs. **A2**
  (quarterly licence review) standing. **A4** (Supabase plan/tier) no longer blocks: W1 is applied
  and living inside the current instance.
- **Q11 `stavebni_objekt`** — open. **Q17** ceskereality's `exact` map-endpoint flag is still the
  one true W0 signal gap (new endpoint, deferred per the plan's implementation order).
- The **VFR daily-delta lane** ships as chain-verification only and fails loudly: it cannot apply
  deltas until the `ST_ZZSZ` element schema and the `TypPrvkuKod` vocabulary are pinned down.
  Until then freshness is the monthly baseline — the design's own free degradation path.
- `location_uncertainty_policy` has no seed row for a pin capped to an admin rung; the lookup
  resolves it to the unit's own area bound. (The earlier `pip` item is closed — migration 381
  admits the purpose; the resolver prefers `'pip'` rows and degrades to `'authoritative'`.)
- The `*/15` **resolve cron is restored** now that the backfill is done. It was removed for the
  duration: the outer `location-batch` group holds ONE pending slot and GitHub supersedes the
  older pending entry, so while the backfill driver dispatched back-to-back runs a tick displaced
  the driver's own next dispatch while adding nothing to a queue it was draining from the same
  front.

## W2a / W2 — in progress (2026-08-13)

Seven PRs across two rounds, every one adversarially reviewed and the findings fixed and
re-verified by probe before merge (the reviews found 1 BLOCKER + 2 MAJOR on the payload
store, 2 BLOCKER + 3 MAJOR on the scoper, 4 MAJOR on the churn report, 2 MAJOR on shadow —
all closed, several proven against a throwaway PostgreSQL 18 built from `.deb` files).

- **W2a-0 the churn instrument** (#1047, migration **402 applied**): `portal_payload_churn`,
  one counter row per `(source, source_id_native, page_kind, normalizer_version)`, no body
  ever stored. `location_data/payload_norm.py` normalises a body (volatile paths stripped,
  key order + whitespace canonicalised) so a page that did not really change stops looking
  changed. Behind `app_settings.location_payload_shadow_hash`, **flipped ON 2026-08-13 18:00Z**.
  Counters are replay-safe (a per-fetch token gates the `DO UPDATE`, so a retried drain batch
  bumps nothing) and `normalizer_version` is in the PK, so a profile change starts a clean
  cohort instead of blending into the old one.
- **W2a-1 the payload store** (#1051, migration **403 applied**): additive ALTER completing
  `portal_raw_payloads` (382 already created it — W2a is not a CREATE TABLE wave) +
  `location_data/payloads.py`. Content-addressed on the NORMALISED hash; P4 retention runs in
  the same bounded transaction as the append. **No caller anywhere** — the library is inert
  until W2a-2 wires it in, which is gated on the churn sign-off.
- **W2a-2 the dual-write** (no migration): `upsert_portal_raw_page` appends every body it stages
  into the payload store — one chokepoint edit covering all 7 HTML detail writers and all 3 index
  archivers with no per-portal branch — plus a call site each for the two portals that stage no
  body: sreality's estate JSON (unwrapped, untrimmed) and bezrealitky's advert **with the exact
  GraphQL query text + sha256** beside it (02 §2.3.2 P3). Gated per portal by
  `PortalLimits.payload_dual_write` (baked default False, operator-overridable through
  `portals.operational_limits` / `scraper_limits_global`, no migration), **OFF everywhere**.
  Idempotent by content addressing, so a replayed drain batch collides instead of duplicating.
- **W2a-3 the churn readout** (#1052): `scripts/location_payload_churn_report.py` (the artefact
  the storage gate is signed from) + the 200×3 confirmation probe, dispatch-only in the
  `location-batch` group.
- **W2-3 the exclusion-zone scoper** (#1053): D7's security boundary — strips every declared
  exclusion zone before any extraction selector runs. **Hard precondition for every per-portal
  contract PR**; without it the deterministic re-miner re-imports at 445k-page scale exactly the
  contamination the LLM validator exists to reject.
- **W2-4 the contract shadow mechanism** (#1050, migration **404 applied**): `portal_contracts.shadow`,
  a contract whose claims are mined and stored but excluded from `location_claims_live`, with
  `location_claims_shadow` as the scoring surface so the un-shadow gate is decidable, and an
  un-shadow that enqueues `dirty_locations` so promotion actually re-resolves.
- **W2-1 / W2-0** (#1045, #1048): per-reader substrate legality (a contract entry can no longer
  declare a transform or guard its reader will never consult) and the archive denominator every
  W2 gate is a share of.

### First churn numbers (2026-08-13, ~4h of live traffic — early, not the sign-off)

| source | repeat fetches | changed after normalisation | normalisation removing false churn? |
| --- | --- | --- | --- |
| sreality | 8,689 | **0.1 %** | yes — 30 raw changes → 8 normalised |
| bezrealitky | 616 | 0.2 % | n/a (closed GraphQL field list) |
| maxima | 20 | 5 % | — |
| realitymix | 547 | **66 %** | **no — raw == norm exactly** |
| idnes | 514 | **100 %** | **no — raw == norm exactly** |
| ceskereality | 153 | **100 %** | **no — raw == norm exactly** |

The volume portal is stable and the mechanism demonstrably works where a profile fits. The
open item is three HTML portals whose measurement-phase volatile profiles strip nothing that
actually moves — a profile-improvement pass (diff genuinely-unchanged page pairs per portal)
before W2a-3b writes the measured `volatile_paths` into the contracts. remax and bazos had no
repeat fetches yet.

**Not enabled, deliberately:** `payload_dual_write` is OFF on every portal (and its
index-only sibling `payload_index_archive` is not built yet — W2a-6), the
445k-row backfill has not run, and no per-portal W2 contract exists yet. Those wait on the
operator's O3/O4 sign-off of `volatile_paths` + the storage projection.

## Standing decisions

- **The four heavy location lanes share ONE outer concurrency group, `location-batch`**
  (registry load, claim intake, Mapy inventory, resolve), each keeping its own group at
  the JOB level. Set after the 2026-08-10 incident: four lanes ran concurrently against
  the shared 75 GB production instance, dropped backends across the fleet (SSL EOF, one
  AdminShutdown), degraded the live Browse rebuild to multi-minute DataFileReads, and
  wedged two lanes with no error at all. A new heavy location lane joins the group.
- **No batch statement runs without a ceiling.** `statement_timeout = 0` is for genuine
  bulk phases (COPY, index build, whole-table rebuild) and nothing else; per-unit and
  per-batch work arms `SET LOCAL statement_timeout` inside its own transaction, so a wedge
  becomes an error the existing per-row / per-unit resilience already handles. Budgets are
  env-overridable (`LOCATION_BOUNDARY_UNIT_TIMEOUT_S`, `LOCATION_RESOLVE_BATCH_TIMEOUT_S`,
  `LOCATION_RESOLVE_SWEEP_TIMEOUT_S`, `LOCATION_INTAKE_TIMEOUT_S`,
  `LOCATION_REGISTRY_PUBLISH_TIMEOUT_S`). Gate: `tests/location_data/test_location_batch_hardening.py`.
- **The scan batch is not the write size.** A `jsonb_to_recordset` parameter is ONE jsonb
  value and Postgres caps a jsonb array's elements at 256 MB; one post-cutover sreality
  listing yields ~18.9 KB of claims, so a 20 000-listing all-sreality batch is ~378 MB in
  one array — which is how the hourly incremental died on 2026-08-11 (`ProgramLimitExceeded`,
  Actions run 31482522487) once `last_seen_at` order stopped diluting sreality across nine
  portals. Every claim/absence/enrichment array is flushed in chunks bounded by BOTH rows
  and serialized bytes (`LOCATION_INTAKE_CHUNK_ROWS`, `LOCATION_INTAKE_CHUNK_BYTES`),
  inside the same batch transaction, and chunk boundaries fall only between listings so
  `DISTINCT ON (claim_fingerprint)` still arbitrates every fingerprint-equal set. A single
  value over `LOCATION_INTAKE_MAX_VALUE_BYTES` (2 MB) is refused at extraction — never
  dropped: absence row + refetch-cohort row, the truncated-payload treatment. Gate:
  `tests/location_data/test_claims_intake_write_bounds.py`.
- The Mapy remediation ladder (R2 inventory → R3 re-resolve → R4 purge) is W1–W4
  work; the R2 evidence inventory is a W1 INPUT (first claim must not be written
  before it exists).
- W0 discipline held: no new precision columns on `listings`; precision signals ride
  in raw_json / archived pages until the claims spine exists.
