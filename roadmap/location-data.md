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
| W2a payload archive rewrite | append-on-change `portal_raw_payloads` | 🟡 **backfill COMPLETE; gate (a) blocked on a verifier fix, not on the data** (2026-08-18) — migrations 405–408 applied; bodies to R2, storage bounded by construction (cap 2 + 7-day floor). **`payload_dual_write` ON globally since 2026-08-17 12:29 UTC, verified on all nine portals** (fresh rows per portal, mmreality last; corpus-wide evidence: the backfill's 12,044 `skipped_existing` are pages the live dual-write path archived before the scan reached them). `payload_index_archive` remains **OFF** (operator decision 2026-08-16: index keys are week-stamped so the cap bounds detail but not index; ~100 % churn on a surface nobody has diffed). #1074/#1075 wired the R2 secrets that made the flag real. **Backfill terminal 2026-08-18 01:59 UTC** — `reached_end=true`, `outcome='ok'`, 11 budgeted dispatches across two driver sessions: **472,429 pages scanned, 460,385 inserted, 12,044 skipped (dual-write overlap), 0 unmapped**, 39.68 GB read → **8.53 GB stored (4.65x)**, final cursor 5,617,479, batch 274. **The 445,191 inventory count is superseded — do not compute a percentage against it**: the archive grew 6.1 % while the scan walked it; `reached_end` is the only completion signal. Compression settled at 4.65x (the proving run's 7.5x and the mid-run ≈7.9 GB projection were both optimistic); one-time footprint 8.53 GB, still well inside the ~28.6 GB steady-state R2 projection (the ~4 GB figure elsewhere in this file is the POSTGRES metadata allowance, a different budget). **Gate (a) verify over the complete corpus (run 32090281321): FAIL, 31/1000 mismatch — diagnosed as a comparator artifact, not damage.** 0 missing / 0 unreadable (every sampled page was found in R2 and decoded cleanly). Every checkable failure (25/25 from the run log) had its `portal_raw_pages` source refetched **5–13 h AFTER the payload was stored** (idnes 18 / realitymix 5 / ceskereality 1 / mmreality 1 — exactly the live-drain portals, in proportion to measured churn; the no-drain portals produced zero), and the writer's **7-day append floor refuses by design to chase the refetch**, so the live source legitimately diverges from the stored copy for up to 7 days. The verifier's own `hash_matches` compares the writer-time hash against the LIVE source hash — it measures drift, not fidelity — and R2 keys are content-addressed on `body_sha256`, so key⇒content at write time. **As written, gate (a) cannot pass while any portal is being scraped — structurally unsignable — and that is the finding, not "31 bad pages".** Follow-ups (recorded, unstarted): (1) verifier compares like-for-like (`prp.fetched_at = payload.fetched_at`) or classifies source-refetched-after-store-with-floor-active as its own `stale-source` category instead of `mismatch`; (2) verifier should re-hash the downloaded R2 object against `body_sha256` — content-addressing proves key⇒content at write time but nothing re-checks the object after write; (3) raise `max_pool_connections` in the uploader's botocore config (urllib3 "pool is full" warnings across all 11 runs — throughput left on the table, correctness unaffected). `location-batch` saturation during the run is recorded as structure in #1084/#1086/#1087. **Gate (a), once fixed and green, licenses the STORAGE decision only** — nothing reads the archive until W2-13 gives the re-mine lane a workflow (#1082) |
| W2 HTML re-mine | claims from archived `portal_raw_payloads` bodies | 🟡 **infrastructure shipped, lane deliberately inert** (2026-08-17) — W2-0/W2-1 (#1048/#1045), W2-3 the exclusion-zone scoper (#1053), W2-4 the contract shadow mechanism (#1050, mig 404), W2-5 the permanent fixture-diff gate (#1058) and W2-2 the evidence-bearing claims + archived-HTML re-mine lane (#1079) are all merged. The lane still mines **nothing**, but the reason moved: `ARCHIVE_READERS` now holds four generic DOM readers (#1081 `html_text`/`html_attr`/`html_point_dms`, #1090 `html_point_attrs`) and **no contract entry names one**, so a run finds no executable entry and returns *before* it opens a batch row — a batch stamped `'ok'` would move the incremental watermark over a corpus it never opened. **W2-6…W2-12 is READER work, not YAML work** (see the verification table below): six portals need a JSON-pointer reader, a regex reader with capture-group spans, and splitting transforms before their contracts become one-line activations, and no contract may activate before W2-13 anyway (#1082). **W2-13 SHIPPED** — the archived-HTML sweep now has a dispatch-only workflow (`location_claims_remine_archive.yml`, in `location-batch`) and the W2 gate is readable per portal (`scripts/location_w2_gate_report.py` + its own read-only workflow); the lane also gained its own batch bounds (50-5000, default 500) and a zero-claim tripwire. **W2-6…W2-12 SHIPPED 2026-09-05 — all seven portal contracts activated and SHADOWED in one wave** (bazos@2, ceskereality@5, idnes@2, maxima@2, mmreality@2, realitymix@4, remax@3): 43 entries moved inert→executable (fleet split 69/70 → 112/47), every one on an archive-only reader, and **migration 470 closes the policy gap that would have blocked the lot** — four of the ten extraction methods had no `location_field_policy` v1 row, so their claims would have been skipped at S7 forever. **The lane is no longer inert and no longer dark-free**: it can mine, and nothing it mines reaches `location_claims_live` until the operator un-shadows each portal off the W2-13 gate report. `shadow` is header-grain, so each of the seven also parks its own already-live W1 entries meanwhile — a freeze, not a blackout, and the reason the un-shadow decision is per portal and not a wave flip. See the W2-6…W2-12 section |
| W3 history backfill | claims from `listing_snapshots.raw_json` | ✅ **shipped 2026-08-19 — scan complete (`reached_end=true`) and all four gate arms PASS** (verify run 32223331085). **1,634,096 snapshots mined over five windows → 92,312 historical location claims + ~10.85 M observations**, terminal batch 289 / cursor 1,634,096. Note the terminal denominator against the 1,574,313 the wave opened with: `listing_snapshots` grew ~60 k rows while the scan walked it, so **`reached_end` is the only completion signal** — the same lesson W2a's backfill recorded (#1088). Unblocking it needed BOTH `location-batch` crons paused (#1100 intake, #1101 resolve), because resolve oversubscribes the group by itself — measured over eleven ticks, a run occupies it 11–27 min on a 15-min cadence, so ~1 tick in 3 completed and the rest superseded each other; both restored (#1105) the moment the scan finished. Gate arm 4 (the corpus arm) needed a lane that did not exist: `claims_remine_verify` + its own read-only workflow (#1102), then two corrections — scoping by anchor/observation rather than `extractor_version` (#1104), and **partitioning the series by contract entry (#1106), without which the gate PASSED on an artifact** (165,706 of 165,708 listings "oscillating"; the real figure is 324). See the W3 section for the measured oscillation and what it says about the program's churn premise |
| W4 targeted refetch cohorts | sreality legacy-shape + truncated refetch, bezrealitky remainder | 🟡 **build started (2026-08-18)** — the consumer for the cohort W1 has been filling since 2026-08-12 (#1083) + its dispatch lane. **Nothing dispatched**; substrate-disjoint from W2/W3 and outside `location-batch`. See the W4 section |
| W2-10 bazos free-text LLM lane | one structured LLM call per archived bazos body → evidence-quoted `llm_text` claims | 🟡 **lane + 3-model bake-off shipped, lane INERT** (2026-09-05) — `location_data/claims_llm.py` (LANE `location_claims_llm`, `claims_llm@1`, group `location-llm`), a THIRD reader registry (`LLM_READERS` / `claims_intake.LLM_ONLY_READERS` / `contracts.READER_CONTRACTS['llm_location_text']`, with the three shipped equalities re-scoped in the same commit), migration 470 (`called_for` += `extract_location_claims`, `location_llm_bakeoff`), `QwenProvider` registered + `PRICES` rows for gpt-5-nano / gpt-5.6-luna / qwen3.7-flash, and `scripts/location_llm_bakeoff.py` + two dispatch-only workflows. **bazos@3 SHIPPED 2026-09-05 and the lane is live-able**: the contract now names `llm_location_text` on **sixteen** entries — eight output fields (`obec_name`, `cast_obce_name`, `psc`, `street_name`, `house_number_cp/co`, `landmark`, `address_line_verbatim`) read from the ad's DESCRIPTION and the same eight from its TITLE — and **migration 472** adds the `('portal:bazos','llm_text', rank 350, min_confidence='medium', may_overwrite_non_null=false, requires_independent_agreement=FALSE)` rungs for the six of those that are survivorship fields. Two rulings are baked in: the two entry families are the two RUNGS of ONE ladder (the lane emits exactly one claim per output field per listing, description-first, title as fallback — policy cannot see `surface`, so this is an EXTRACTOR decision), and rank 350 puts the free text above every structured read on this portal because they are okres-grade (the town anchor's text is the okres; `postal_town` disagrees with the geo obec on 57.0 % of rows; the pin-derived obec is itself wrong). `requires_independent_agreement=FALSE` is a deliberate D7 relaxation: bazos free text is the only carrier for these fields, so requiring a second source makes single-source claims permanently unusable. **Still shadowed** (`shadow: true` carries forward) and the workflow is still dispatch-only — nothing runs until an operator dispatches it, and nothing it writes reaches `location_claims_live` until `--unshadow bazos@3`. Next: the bake-off, then a capped campaign |
| W5–W6 | LLM lane, serving flip | ⚪ not started |

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
- **W2a-3b measured volatile profiles**: `scripts/location_payload_diff_probe.py` +
  evidence-derived profiles for idnes / ceskereality / realitymix, `payload_norm@2`. Table
  and residue below. Keyed by `(source, page_kind)` in W2a-3d (mig 405), then **moved into
  `contracts/portals/*.yaml` → `persistence.volatile_paths` in W2a-3e (migs 407+408)**, which
  retired the Python table and relabelled the detail cohorts
  `payload_norm@3+profile@<digest>`.
- **W2a-4 the backfill + round-trip verifier** (#1059): `location_data/payload_backfill.py`
  (keyset-resumable, `portal_raw_pages` → `portal_raw_payloads`; **ran to `reached_end`
  2026-08-18 — terminal numbers and the gate (a) finding are in the status table above**)
  + `scripts/location_payload_roundtrip_verify.py` (1,000-row byte-for-byte compare, 06 W2a
  gate (a)). Review found and fixed three real gaps before merge: `sample_ids()` silently
  under-sampled by up to 97 % on a source-scoped draw (now an exact uniform id-space sample
  with a loud shortfall report); the success-path finalize stamp ran unguarded and could strand
  a batch row at `'running'` forever; re-dispatching after a `NORMALIZER_VERSION` bump would
  have created permanent, un-prunable duplicate rows (now refuses without `--force`).
- **W2a-5 the P4 pruner** (#1063): version-cap re-assertion plus a genuinely new time-based
  hot-window eviction (`LOCATION_PAYLOAD_HOT_WINDOW_DAYS`, placeholder pending operator
  sign-off — 02 leaves the window undefined), scoped exclusively to `portal_raw_payloads`.
  Ships with a live weekly cron (Sun 04:00), but the `location_jobs` row is seeded
  `enabled=false` **before** any lease attempt — `lease.held()`'s own upsert defaults
  `enabled=true`, and review caught that assumption before it shipped as a pruner that would
  have self-enabled behind a live cron. The disabled path is proven inert by a
  recording-connection test asserting the exact statement list (a seed and a flag read, no
  `portal_raw_payloads` touch, no `DELETE`).
- **W2a-6 the index-coverage audit** (#1062): `scripts/location_index_archive_audit.py` +
  a second flag, `payload_index_archive` (baked default False), so index writes can be
  enabled separately from detail. Reports three axes per portal — what the contract declares,
  what the code actually does, what `portal_raw_pages`/`portal_raw_payloads` show — and found
  real drift: bezrealitky declares `archive: true` with no call site; ceskereality mines an
  index claim off a fetch surface its own contract never declares. Confirms live what the
  "First before/after" table below quantifies: sreality/remax/ceskereality's index archivers
  are `gated`, not wired — the freshness pre-filter suppresses the archive on any key still
  inside its refresh window, silently, which is why the index-surface numbers below have never
  been measured against a working archive. Also closed a real hole in the flag split itself:
  `map`/`gazetteer` page kinds (already declared `archive: true` by two live contracts) would
  have archived on `payload_dual_write` alone, bypassing the second gate entirely.
- **W2-3 the exclusion-zone scoper** (#1053): D7's security boundary — strips every declared
  exclusion zone before any extraction selector runs. **Hard precondition for every per-portal
  contract PR**; without it the deterministic re-miner re-imports at 445k-page scale exactly the
  contamination the LLM validator exists to reject.
- **W2-4 the contract shadow mechanism** (#1050, migration **404 applied**): `portal_contracts.shadow`,
  a contract whose claims are mined and stored but excluded from `location_claims_live`, with
  `location_claims_shadow` as the scoring surface so the un-shadow gate is decidable, and an
  un-shadow that enqueues `dirty_locations` so promotion actually re-resolves.
- **W2-5 the fixture-diff gate** (no migration): `tests/location_data/test_contract_fixture_diff.py`
  + a golden claim set per portal under `tests/fixtures/location_w2/golden/<portal>@<version>.json`.
  Runs each contract's named `regressions:` listings through the real extractor on every push and
  fails with a claim-level diff — extractor id, field, old value → new value — when a contract or a
  fixture body changes what is claimed; a reviewed change is accepted by re-blessing the golden.
  **Permanent CI, not a wave gate** (02 §2.1.8.3), and with `location_claim_retractions` (mig 382)
  it completes 02 §2.7 item 0(b), the pair that must stand before the first contract writes a
  production claim. **Hard precondition for W2-6…W2-12.** Coverage today is the subset of pinned
  listings that have a frozen body in-repo; each gap is written into the golden as
  `listings_without_a_fixture_body`, so coverage arriving with a portal PR is itself a reviewed diff.
- **W2-1 / W2-0** (#1045, #1048): per-reader substrate legality (a contract entry can no longer
  declare a transform or guard its reader will never consult) and the archive denominator every
  W2 gate is a share of.
- **W2-2 evidence-bearing claims + the archived-HTML re-mine lane** (no migration — 382 already
  carries every evidence column and CHECK): `Claim` gains the D7 evidence set (`payload_id`,
  `payload_sha256`, `evidence_quote`, `span_start`, `span_end`, `payload_scope_version`) plus
  `model`/`prompt_version`, carried through `to_row()`, `_CLAIM_WRITE_SQL` and on into
  `location_claim_observations`; `claim_fingerprint` stays time- AND evidence-free.
  `location_data/claims_remine_archive.py` scans the latest body per
  `(source, source_id_native, page_kind)`, reads it out of R2 or the row, and stamps
  `surface='archived_html'` (C9), the page's own `page_kind` (C10),
  `snapshot_anchor='unanchored_latest_fetch'` (C4), plus the archived arm of the coordinate
  ladder (`ARCHIVED_COORDINATE_RULES`, one detail-map entry per portal; the `mapy_affected` veto
  above the substrate branch; realitymix's Nominatim fallback is `'odbl'`). Evidence is refused in
  Python, never by the CHECK. Also: C7 now counts distinct **sources**, so one portal read two
  ways is one voice, and `tests/location_data/test_lane_identifiers.py` makes every `LANE` /
  `JOB_NAME` / `CONCURRENCY_GROUP` / version constant globally unique. **Inert on merge,
  structurally** — no contract entry names an archive reader, so a run returns before it opens a
  batch row, because a batch stamped `'ok'` moves the incremental watermark.

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
open item was three HTML portals whose measurement-phase volatile profiles stripped nothing
that actually moves — **closed by W2a-3b below**. remax and bazos had no repeat fetches yet.

### W2a-3b measured volatile profiles (2026-08-13)

`scripts/location_payload_diff_probe.py` refetches one live detail page 2-3× seconds apart
and structurally diffs the results into the three shapes a `VolatileProfile` can express
(CSS selector / attribute name / JSON pointer). Nothing about a listing can change in ten
seconds, so everything it reports is volatile by construction. 5 listings × 3 fetches per
portal; sreality ran as the control and reported **zero** divergences (its 15 bodies were
byte-identical), which is what makes the other three readings trustworthy.

| source | what actually moved | measured profile |
| --- | --- | --- |
| idnes | Nette contact-form anti-spam, 5/5 listings: `input[name=tshee]` counter, `input[name=schpeckc]` captcha hash, `#schpeckIn` question ("3 ➕ 6" → "1 ➕ 1"); plus the `.grid-similar-offers` rail (other listings) | those four + the pre-existing `.advertisement` |
| ceskereality | `input#bug-report-token` on **4 of 5 listings the only difference at all**; the 5th also rotated `section.s-estates-slide` | both |
| realitymix | ONE footer badge cycling `0.85 / 0.84 / 101.85` (a per-response backend stamp) — nothing else moved in 15 fetches | `footer div.absolute.bottom-2.right-2` |

realitymix's three values at their observed frequencies predict a 63% chance any two
consecutive fetches disagree, against the **66%** measured in production: that badge is the
whole number. `NORMALIZER_VERSION` → `payload_norm@2`, so the new cohort cannot blend with
rows measured under the guesses (migration 402 has it in the PK for exactly this).

**Known residue, deliberately not stripped:** ceskereality renders "Datum vložení" as a
RELATIVE time on fresh listings ("před 22 minutami" → "před 23 minutami"), and that row is
identified only by its label text — no CSS selector reaches it. The portal switches the field
to an absolute date at ~2 weeks, so it touches fresh inventory only, and under-stripping
over-states churn (the safe direction).

**Hazard found:** selectolax **segfaults** (exit 139, reproducible, 0.4.10) on
`:contains()` against a full-size page while returning cleanly on a small one. A segfault is
not catchable, so `normalise`'s "never raises" contract would not survive one — pseudo-classes
are now allowlisted (`selector_is_safe`) before any selector reaches the CSS engine. This
binds W2a's next step, which sources selectors from `portal_contract_entries.persistence.volatile_paths`,
i.e. from outside the reviewed module.

### W2a-3c — mmreality + remax (2026-08-14, `payload_norm@3`)

The fuller baseline revealed the problem was broader than the three portals W2a-3b fixed:
**mmreality and remax were also at 100 %**, invisible earlier only because neither had
accumulated a repeat fetch yet. Same method, same tooling:

| source | what actually moved | profile |
| --- | --- | --- |
| mmreality | Cloudflare **email obfuscation** — the edge re-encodes the same `mailto:` as the address XOR'd with a fresh leading byte on every response (`data-cfemail`, `/cdn-cgi/l/email-protection#`). A 100 % rate produced by re-encoding a constant. | `a[href^="/cdn-cgi/l/email-protection"]`, `span.__cf_email__` |
| remax | **byte-identical within an HTTP session**, 5/5 different across sessions. Symfony CSRF material minted per session, and the live drain is a fresh process per run, so every production refetch is cross-session. One token sits in a `data-content` **attribute** carrying an escaped `<form>` — a string, not a node, that no CSS selector can ever reach. | `div.pd-share__buttons button[data-content]` |

That last one forced a tooling change: `--fresh-session-per-round` is now the probe's
default. Without it the probe measures **zero** on a portal production measures at 100 %.

### First before/after (2026-08-14, early)

**ALWAYS SPLIT BY `page_kind`.** An earlier revision of this section quoted sreality at
"4.5 %", which is the aggregate of a detail surface at **0.04 %** and an index surface at
**97.7 %** — arithmetically correct, analytically worthless, and describing neither. Caught
by the sibling W2a session re-deriving it from `portal_payload_churn` rather than trusting
the summary. Every figure below is per `(source, page_kind, normalizer_version)`, which is
the grain the PK already uses.

**Detail surfaces — second reading (2026-08-14, later the same day)** — what
`payload_dual_write` would archive. More of the 6 h portals had accrued real repeats by
the time of this reading; treat the numbers below as current as of this section's own
timestamp, not frozen — `portal_payload_churn` is a live, growing table and every cell
here will keep drifting until each portal has a few hundred stable repeats:

| source | `@1` (guessed) | measured | repeats |
| --- | --- | --- | --- |
| sreality | 0.04 % | **0.50 %** (2 changes / 400 repeats, `@3`) | 400 |
| bezrealitky *(control)* | 0.02 % | **0.00 %** | 861 (`@3`) |
| ceskereality | **100 %** | **17.84 %** (38/213, `@3`) — moved from 24.32 %/37 at `@2`; **not a trend**, each `@N` is its own clean cohort and the two readings shouldn't be read as declining | 213 (`@3`) |
| maxima | 17.1 % | **0.00 %** — but from **one listing** repeated 18 times, not portal-level coverage | 18 (`@3`, 1 key) |
| realitymix | 67.7 % | **0.00 %** (0/473) — the footer-badge fix holds, now on a solid sample | 473 (`@3`) |
| idnes | 100 % | **100 %** (285/285) — unchanged, see below | 285 (`@3`) |
| remax | 100 % | **not yet measurable** — 6 fetches total across `@2`+`@3`, still 0 repeats (no listing refetched twice yet) | 0 |
| mmreality | 100 % | **zero fetches recorded at all** under `@2` or `@3` | 0 |

**idnes: the W2a-3b fix was never actually validated live — a measurement gap, not a
regression.** Nothing that used to work stopped working: `@2` closed with 0 repeats
(untested), so the 100 % baseline never got re-checked against real traffic until `@3`
(same idnes `VolatileProfile`, untouched by the mmreality/remax bump) finally accrued
285. It reads 100 % again, and `raw_changes` == `norm_changes` **exactly**, at every
normalizer version measured so far — `@1` 6,514/6,514, `@3` 285/285 — the strip is removing
nothing that actually matters in production, even though the 5-listing/3-fetch diff-probe
that derived it found only the Nette anti-spam fields and the similar-offers rail. One
plausible cause: the same session-scoped blind spot `@3` uncovered on remax (the diff-probe
ran with one persistent session across its three fetches until `--fresh-session-per-round`
became the default in #1064, *after* idnes was measured) — but that's a hypothesis, not a
confirmed cause, and worth noting remax's own fix is itself still unverified in production
(0 repeats above). Contrast: ceskereality and realitymix both show `raw_changes >
norm_changes` (realitymix 323 → 0 at `@3`), which is evidence the normalizer mechanism
itself works — idnes specifically isn't benefiting from its own profile, not that the
instrument is broken. **Not chased further per scope** — recorded here so the storage
projection counts idnes's detail surface at its true ~100 %, not the earlier "fixed" figure
from #1066.

**Index surfaces** — what `payload_index_archive` would archive, and **none is profiled**:

| source | `@1` | note |
| --- | --- | --- |
| sreality | **97.7 %** | 3,621 / 3,705 repeats |
| ceskereality | **100 %** | 694 / 694 |
| remax | **100 %** | 385 / 385 |

Only those three portals archive index pages at all. Their keys are **week-stamped**
(migration 402's header says so), so the surface accrues new rows forever *and* churns at
~100 %. **`payload_dual_write` and `payload_index_archive` therefore deserve OPPOSITE
recommendations on current evidence**, and the two flags exist separately for exactly this
reason. Nobody has yet diffed an index page to find out what moves on it.

### W2a-3d — profiles are keyed by (source, page_kind), not by portal (2026-08-14, migration 405)

The line above — "none is profiled" — was **not what the code did**. Every profile was
selected by `source` alone and therefore also applied to index bodies, so those ~100 %
index figures were measured through detail-page rules that nobody had ever pointed at an
index page. Fixed before either write flag went on, which was the cheap moment: **at that
time** nothing was archived (`portal_raw_payloads` 0 rows, the backfill not yet run), so no
content address had to be rewritten. (Both are now true in the other direction — dual-write
is ON and the archive is filling; see "Those three decisions — answered" below.)

**Measured first, on live pages, because "harmful" and "merely useless" are different
verdicts.** The prior was that a detail selector describing *other listings* (idnes's
`div.grid-similar-offers`, ceskereality's `section.s-estates-slide`) would strip an index
page's whole content and make different pages hash alike. On today's templates it does not:

| surface | what the detail profile did to a live index body |
| --- | --- |
| sreality index (JSON, 2.5 MB) | 26 pointers, **0 bytes removed** — they address an estate document; an index page is a list of them |
| remax index | 21 selectors, **2 matched** (`noscript`, `style`), 173 B of 209 KB |
| ceskereality index | 22 selectors, **5 matched**, ~1.5 KB of 180 KB — shared chrome plus `input#bug-report-token`; `section.s-estates-slide` matched **0 nodes** on 5 bodies (2 www + 3 region-host slices) |
| **bazos index** | **`div.inzeratyview` matched 21 nodes** — one per listing card |

Two different index pages still hashed apart in every case, so **change detection was not
broken**. But bazos shows the mechanism is live, and `portal_raw_pages` holds index rows for
**five** portals, not three — idnes 4,152, bazos 1,497, remax 770, ceskereality 697,
sreality 505, maxima 38 (7,659 total, historical: index archiving was removed in June 2026
and re-enabled for three portals in W0-0n). All of them are input to the W2a-4 backfill,
which normalises through the same resolver and writes `payload_sha256` — a **permanent**
content address. So the fix is structural rather than cosmetic: correctness here was
coincidence, one measured selector wide.

**The shape.** `MEASURED_VOLATILE_PROFILES` is now `source -> page_kind -> profile` (the
absence of an `index` key on every line is the point), resolved only through
`resolve_normalisation(source, page_kind)` — one function shared by the churn instrument,
the archive writer and the backfill, returning the profile **and** the cohort label as one
`Resolution`. The pair is inseparable on purpose: review found `append_payload` normalising
under an explicitly-passed `volatile=` profile while stamping `normalizer_version` from the
profile TABLE, i.e. from whether an entry exists rather than from what was applied. Latent
today (no production caller passes `volatile`, the store is empty, both flags OFF) and
load-bearing from W2a-3b, which passes contract-sourced selectors in exactly that shape —
`normalizer_version` is permanent and its only job is to explain a `payload_sha256`, so a
row hashed under a real measured profile would have asserted "only the generic base was
stripped" with nothing downstream able to detect it. An explicit profile now **requires**
the label that names it (refused otherwise, before any statement runs); overriding the label
alone stays allowed, which is `record_payload_churn`'s existing probe shape.
An unmeasured surface gets `BASE_PROFILE`: the shared
`_HTML_BASE` + `_HTML_ATTRS` and **nothing measured**. Why the base rather than no
stripping: on a measured surface over-stripping is self-correcting (the residue diff shows
it); on an unmeasured one it is not — a profile that eats the listing grid reports **0 %**,
which reads as the best possible result. The base carries only portal-agnostic, content-free
rules, and it is inert on JSON by construction (no pointers), so the sreality/bezrealitky
JSON surfaces do not move at all.

**`NORMALIZER_VERSION` stays `payload_norm@3`** — detail normalisation is byte-identical
across all **26** committed detail fixtures on 8 portals, pinned as digests computed under
the old code, with the pin's COVERAGE asserted from the fixture tree rather than a hand-kept
list (`tests/location_data/test_payload_norm_by_page_kind.py`). Instead the cohort label
is resolved per surface: `normalizer_version_for(source, page_kind)` appends **`+base`**
where no profile was measured. A global bump would have discarded ~24,600 detail fetches
across 9 portals to fix an at-most-one-phantom-change artefact on ceskereality's 694 index
keys — the standing "STOP BUMPING" instruction, honoured with the per-surface answer it was
asking for. The suffix maintains itself: measure an index profile and that surface leaves
the `+base` cohort on its own.

**Still not done, deliberately:** no index-surface profile is written here. Diffing index
pages is its own finding. **And the same collapse was waiting one layer down** —
`persistence.volatile_paths` was a single flat list on the contract HEADER while `fetch:`
beneath it was already per-`page_kind` — **closed by W2a-3e below**, which gave the contract
key the surface axis before moving the values into it.

### W2a-3e — the profiles move into the contracts (2026-08-14, migrations 407+408)

`MEASURED_VOLATILE_PROFILES` is **retired**. Each portal's rules are now
`persistence.volatile_paths.<page_kind>` in `contracts/portals/<portal>.yaml`, so a change
to what a portal strips is a reviewed, versioned, retractable diff like every other
extraction rule instead of a Python edit — which was the point of 02 §2.3.2 / 06 W2a-3b and
what migration 405's own comment already anticipated.

**Per page_kind, and the leaf is explicit.** `volatile_paths` is a mapping
`page_kind -> {base, json_pointers, css_selectors, strip_attributes}`; a flat list is
refused by name. `base:` (`html` | `none`) names the portal-agnostic floor `payload_norm`
applies underneath and is stated per surface with no default, so no body acquires a floor by
accident. Only `detail` is declared anywhere — an index profile still needs an index diff.

**Read from git, not from the DB projection.** `contracts.py` still projects `persistence`
into `portal_contracts.fetch_config` (verbatim, and refreshed in place by each load) for
review in psql, but the scrape parses the same key out of the same files, which ship in the
image (`COPY contracts/`, PyYAML promoted to a runtime dependency). `payload_sha256` is a
PERMANENT content address that every evidence span inherits, so the projection producing it
has to be a function of the deployed artefact alone; read from the DB it would also be a
function of whether the contract-load job had run yet — two runners hashing one body two
ways at the same moment.

**Validated where refusing is allowed.** `payload_norm.parse_profile_block` runs
`selector_is_usable` on every selector at contract-parse time and at load time — a
`:contains()` SEGFAULTS selectolax (exit 139, uncatchable) and a typo raises inside `.css()`,
and `normalise` is silent by contract, so a bad rule that got that far would not fail, it
would quietly stop stripping. Every contract on disk is parsed by the test suite, so a typo
fails `test.yml` on the push that introduces it.

**Nothing was re-measured.** All nine profiles resolve byte-identical to the retired table
(pinned per portal as digests computed under the old code) and all 26 committed detail
fixtures normalise to their existing pinned hashes. `NORMALIZER_VERSION` stays `payload_norm@3`.

**And nothing was re-versioned — `contract_sha256` now covers what it governs** (migration
408, from the adversarial review of #1072). The hash was taken over the whole file, so
editing archive configuration forced a `contract_version` bump; a bump re-stamps
`extractor_version` and `contract_entry_id`, both of which feed `location_claim_fingerprint`
(mig 386) and its UNIQUE index — so the next incremental scan would have **re-inserted the
claims corpus**: 5,135,469 rows / 2,625 MB on 2026-08-14, append-only, in a subsystem with
~4 GB of allowance left, and once more per future selector edit. `persistence:` is therefore
excluded from the hash (the precedent `shadow:` set in mig 404), the nine bumps this wave
proposed were dropped, and 408 restates the nine stored hashes into the new dialect. The
`fetch_config` projection is refreshed in place by each load so the psql copy still tracks
git. **Apply 408 in the same window as the merge**: in between, the intake's contract-load
step fails loudly (naming the migration) and that hour's scan is skipped — the keyset
watermark resumes, nothing is half-written.

**The cohort label stops naming a table that no longer exists — and stops borrowing the
contract's version.** Two independent things can move a normalised byte — the engine and the
portal's declaration — so both are named: `payload_norm@3+profile@<8 hex of
payload_norm.profile_digest>` where a contract declares the surface, and the unchanged
`payload_norm@3+base` where it does not (the base belongs to the normaliser and is identical
under every contract version, so the index cohorts accumulating today are NOT thrown away).
The digest, not `contract_version`, because a version moves for extraction reasons —
ceskereality and realitymix each took two such bumps in the fortnight before this shipped —
and keyed on the version, every one of those would have orphaned that surface's counters in
`portal_payload_churn`'s PK and restarted the readout at `fetches=1` for a projection that
never moved. That is the same waste `payload_norm` refuses on the engine axis by not bumping
`NORMALIZER_VERSION` for output that did not move. Each portal's detail surface opens one
clean cohort **once**, which is 402's discipline rather than an exception to it — the
instrument's identity changed even though its output did not — and by construction it is the
last such break that is not a real profile change. The probe suffix composes onto that label
instead of hard-coding the bare version.

**Read the detail table honestly:** one portal has moved, the control is unregressed, and
sreality's detail surface holds at zero. The rest is blank because a change rate needs the
SAME page fetched twice under the SAME `normalizer_version`, and two bumps in one morning
(`@2`, then `@3`) each started a clean cohort. The detail drain only refetches on an
index-signalled change, so repeats accrue slowly on the 6 h portals.

**Consequence, and the standing instruction that follows: STOP BUMPING `NORMALIZER_VERSION`.**
Every further profile change restarts the measurement the storage sign-off depends on. Leave
`@3` undisturbed until each portal has a few hundred repeats. Resist fixing portals that
newly surface at 100 % — record the number and move on; the profiles only need to be good
enough that the projection is meaningful.

### W2a-7 bounded storage — the cap's arithmetic, and a time floor (2026-08-14)

**The affordability question was being answered by filter quality, and it should never have
been.** Good `volatile_paths` meant a listing kept ~1 body; a portal redesign that defeated
them meant it raced to the retention cap. Since the profiles are hand-written per portal and
rot silently, that is an indefinite treadmill — and it was hiding the real number, because
**the cap sets the CEILING and churn only sets how fast it is reached.**

The arithmetic, re-derived from production (`portal_raw_pages` 463,256 real bodies +
`portal_payload_churn` @3 + `listings`), gzipped through `payloads.encode_body` itself.
Cross-check: applying these per-portal figures to W0's exported corpus predicts 17,227 B/page
against the **17,184 B/page actually measured** (7.69 GB gz / 447,510 pages) — 0.3 % apart.

Worst case is **cap + 1** bodies, not cap: the first version is pinned OUTSIDE the cap. One
body for every listing *ever* is **9.56 GB** — and the subsystem's 20 GB envelope already has
**~16 GB spent** (RÚIAN mirror, claim spine, projections), so the archive's real allowance is
**~4 GB**. Against that honest pair of numbers there is **no cap at which bodies fit in
Postgres**: even cap 1 is 19.1 GB. The inherited default of 20 was 200 GB.

**So the bodies do not go in Postgres.** `payload_dual_write` now spills every body larger
than Postgres's own TOAST threshold (2 KB) to R2 and keeps the metadata row — identity, both
hashes, sizes, version, pin state, the content-addressed key. Two ledgers, two currencies:

| cap | bodies/group | rows (ever) | Postgres | R2 | R2 $/month | fits ~4 GB |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 2 | 1.34 M | 1.00 GB | 19.1 GB | $0.29 | yes |
| **2 (default)** | **3** | **2.02 M** | **1.49 GB** | **28.6 GB** | **$0.43** | **yes** |
| 3 | 4 | 2.69 M | 1.99 GB | 38.2 GB | $0.57 | yes |
| 5 | 6 | 4.03 M | 2.99 GB | 57.2 GB | $0.86 | yes |
| 7 | 8 | 5.38 M | 3.97 GB | 76.3 GB | $1.14 | yes (last) |
| 10 | 11 | 7.39 M | 5.48 GB | 104.9 GB | $1.57 | no |
| 20 (as shipped) | 21 | 14.1 M | 10.45 GB | 200.4 GB | $3.01 | no |

A metadata row measures **713 B** (200k rows loaded into the applied 382+403+406 shape,
`pg_relation_size` per relation) against ~20 KB for the same row carrying its body — 29x. The
Postgres footprint is now row overhead, not body weight, so it moves with the corpus and the
cap rather than with how heavy a portal's HTML is.

**Why R2 and not a hot window in Postgres.** Nothing on a latency-critical path ever reads a
body — the readers are the W2 re-mine, one-off backfills and the round-trip verifier, all
batch — so this trades batch wall-clock, not user-facing latency, and a full 445k-page sweep
parallelises to roughly ten minutes at 32-64 concurrent GETs. Postgres-resident bodies would
tax the whole instance's shared buffer cache, which this platform has been burned by twice
(the Browse timeout saga; the 2026-08-10 multi-lane incident). A hot-window hybrid was
rejected because its eviction predicate cannot be written: "processed" is undefinable when the
archive's purpose is re-mining with extractors not yet authored. If a batch job later proves
slow the remedy is a per-run local disk cache in the worker, **not** a second DB-resident tier.

**Degradation is a refusal, not a fallback.** An unconfigured store used to mean "keep the
body inline", which would now silently rebuild the database-resident archive one missing env
var at a time. A body that needs the bucket and has none raises; every scraper reaches the
writer through `append_payload_if_enabled`, which warns and returns, so the walk and the drain
are untouched. On a fresh deploy and in CI the branch is unreachable rather than tolerated —
`payload_dual_write` is OFF per portal, so nothing calls the writer at all. The upload runs
INSIDE the write transaction, so a failed PUT rolls the metadata row back; the reverse orphan
is harmless because the key is the hash of the bytes.

- **Default cap 20 → 2** ("first, previous, current"), and it survives the R2 re-derivation
  for a *different* reason than it was first chosen for. A unit of cap used to cost 6.1 GB of
  Postgres — a third of the subsystem's envelope per slot — which made the cap a budget
  instrument. It now costs 0.5 GB of metadata rows and ~$0.14/month of object storage, and the
  archive fits **up to cap 7**. So the number is chosen on evidentiary grounds: a body a claim
  references is pinned by the claim FK regardless of the cap, so anything that produced a
  location fact is already exempt; the cap governs only bodies no claim points at, which by
  construction produced no fact — and under the 7-day floor cap 2 is already ~three weeks of
  page eras. Cheap storage is a reason not to panic about depth; it is not a reader. **The
  headroom is the deliverable**: `payload_budget.largest_affordable_cap()` publishes it, so a
  future re-mine that wants deeper history raises one constant and CI says whether it fits.
- **New: a per-listing time floor** (`LOCATION_PAYLOAD_MIN_APPEND_INTERVAL_DAYS`, default 7) —
  at most one new body per `(source, source_id_native, page_kind)` per window, enforced as a
  predicate INSIDE the append statement. **This is the structural fix**: it decouples storage
  from filter quality entirely, so total profile failure costs one body per listing per week
  rather than one per fetch. It never suppresses a group's first body and never suppresses an
  unchanged refetch (that collides and writes no row). No "important change" bypass, on
  purpose — any such predicate is the same per-portal judgement that rots on a redesign.
  Nothing that PERSISTS is lost: the next fetch past the window archives the page whole. Only
  content that appears *and* disappears inside one window is missed, which is the transient
  noise the profiles are hand-written to drop anyway.
- **Why both.** The cap bounds the STOCK, the floor bounds the FLOW, and the flow is what a
  100 %-churn portal actually costs: idnes at ~4 fetches/day would write 8.9 GB/day of
  INSERT-then-DELETE against a 4.4 GB standing archive — dead tuples, WAL and autovacuum an
  order of magnitude larger than the data kept. Under the floor, 0.32 GB/day (**28x**), and
  the bodies retained span weeks instead of hours. On the (still OFF) index surfaces the floor
  matters more than the cap: week-stamped keys at ~100 % churn project **~140 GB/year** at cap
  20, ~20 GB/year at cap 2, and **~6.7 GB/year with the floor at either cap** — so index
  archiving no longer waits on profiling nobody has done.
- **Observable, not magic**: `PayloadRef.suppressed` plus process counters
  (appended / unchanged / floor_suppressed / evicted rows + bytes) rolled into one log line
  every 200 decisions. A floor that suppresses everything and a portal that stopped changing
  produce the same empty archive diff; these counters are the only thing that separates them.
- **Concurrency**: the floor's check and its insert share one statement but not one snapshot
  with a concurrent writer on the same key, so both could find the window empty and both
  insert — and two overlapping re-pins and prunes on one group are a deadlock class rather
  than a rounding error. A transaction-scoped advisory lock on `(source, native, page_kind)`
  closes both; xact-scoped, so it is safe behind the transaction-mode pooler.
- **One statement, two outcomes.** The suppressed path used to issue a second read to report
  what the archive actually holds — on the path that runs on nearly every fetch of a
  high-churn portal. That is now `_APPEND_SQL`'s fallback arm.
- **No migration for the floor or the cap.** Both guard arms are exact index probes on relations 382 already ships —
  the dedupe arm on the identity UNIQUE, the window arm on `prp_native (source,
  source_id_native, page_kind, first_observed_at DESC)`. Adding an index here would be the
  dead index 403's header refuses. `NORMALIZER_VERSION` is untouched (normalisation output is
  unchanged), so the `@3` measurement cohort keeps accumulating.
- `location_data/payload_budget.py` freezes the measurements with their provenance and
  `tests/location_data/test_payload_budget.py` **fails CI if the default cap's ceiling leaves
  the budget** — the number cannot drift from the arithmetic that chose it.
  `scripts/location_payload_storage_ceiling.py` re-derives it live and prints the drift.
- **A surface nobody has costed cannot be archived.** The frozen corpus carries every
  portal's `detail` and nothing else, because nothing else has been weighed — index surfaces
  are week-stamped, ~100 % churn and unprofiled. `append_payload_if_enabled` now refuses an
  unmeasured `(source, page_kind)` outright, so turning `payload_index_archive` on cannot
  silently invalidate the ceiling the operator signed; it forces the measurement first.
- **Migration 405** adds `stored_byte_size`. Both retention statements reported what they
  freed as `octet_length(body)`, which is NULL once the body is in the bucket — every eviction
  would have reported zero bytes reclaimed, on the one figure the storage sign-off is read
  from. `byte_size` cannot stand in: it is the decoded length, ~5x larger.
- **Still open**: per-portal caps in `persistence.version_cap` (idnes + ceskereality +
  mmreality are 62 % of the R2 bill) and the index surfaces' own profiling. Both are now
  decisions with a number attached rather than assumptions.

### PII incident (2026-08-13/14) — three axes, each found only after the previous was fixed

Fixture capture publishes real people's data to a **public** repo, and this went wrong twice:

1. **Phones + emails** (#1056): 9 live pages committed with four brokers' mobile numbers and
   three work emails. Caught by review before merge. The tip was scrubbed — but the raw pages
   were still fetchable in the branch's **history**, so the branch was squashed to one clean
   commit and force-pushed (operator-approved). **Trap worth remembering: rebasing a branch
   whose history holds the bad commit REPLAYS it as a new commit** — rebuild from the tree
   (`git commit-tree <tree> -p origin/main`) instead.
2. **Obfuscated emails** (#1064): Cloudflare's XOR payload publishes an address while matching
   no plaintext rule. The scrub now re-encodes the PLACEHOLDER under the page's own key, so the
   fixture still demonstrates its churn instead of becoming a tautology.
3. **Names** (#1064 → fixed in #1065): two mortgage advisers' real names reached `main`. Their
   emails HAD been scrubbed, so every assertion passed and CI was green. `--scrub-contacts`
   takes names as a hand-supplied `--name` list; the listing agent's was passed, the
   `mortgageAdviser` block on the same page was not. **A missed input, not a code defect** —
   and it merged because the merge went ahead before the adversarial review completed.

Mitigations now standing: the contact-scoped `--scrub-contacts` mode, plus three CI gates —
contact details, the obfuscated-email hex, and person-bearing JSON keys
(`test_no_committed_fixture_carries_a_real_persons_name`, verified to FAIL on a restored real
name rather than merely pass) — and the rule in `.claude/skills/scraper-ops/SKILL.md`.
**Open, operator's decision:** the raw values remain in history (#1056's pre-rewrite blobs by
SHA; the names in `04e1db9a`). A history rewrite of `main` or a GitHub Support purge are the
only true removals; neither is engineering's call.

**Not enabled, deliberately — state as of 2026-08-14, since superseded:** `payload_dual_write`
and `payload_index_archive` were both OFF (which is exactly why W2a-7's retention change was
free to make — nothing had been archived, so no body was evicted by lowering the cap), the
445k-row backfill had not run, the P4 pruner lane shipped with `location_jobs.enabled=false`,
and no per-portal W2 contract existed. Those waited on the operator's O3/O4 sign-off of
`volatile_paths` + the storage projection. **O3/O4 were answered 2026-08-16/17** — dual-write
is ON and the backfill is running; see "Those three decisions — answered" below. The pruner
and the per-portal contracts are still as described.

### W2a hardening — CLOSED (2026-08-14, migrations 405–408 all applied)

Five PRs taken before enabling any write, because every one of them gets harder once the
archive holds rows. **When they landed** `portal_raw_payloads` was **0 rows** and both write
flags were **OFF** — none of this touched production data, which was the entire point of
sequencing them first. (Both have since changed: dual-write ON 2026-08-17, archive filling.)

| PR | What it changed | Why it had to precede the first write |
| --- | --- | --- |
| #1070 (mig 405) | Volatile profiles keyed by **(source, page_kind)**, not by portal | Every measured profile was derived by diffing DETAIL pages and was being applied to INDEX bodies too — a different document (a list of *other people's* listings). Measured before fixing: on today's templates the mis-application is inert by coincidence, but `portal_raw_pages` holds 7,659 index rows across FIVE portals that W2a-4 would have migrated under a detail projection, baking a permanent wrong content address. |
| #1071 (mig 406) | **Bodies to R2**; Postgres holds the metadata row. Retention cap 20 → **2**, plus a **7-day per-listing floor** | The archive does not fit in Postgres at ANY cap: one body per listing over the cohort it converges on is ~19 GB against ~4 GB of subsystem allowance. Operator decided R2 after being shown the tradeoff against Postgres-only and a hot-window hybrid; the hybrid was rejected because its eviction predicate cannot be written — "processed" is undefinable when the archive exists to be re-mined by extractors not yet authored. |
| #1072 (migs 407+408) | `volatile_paths` moved into the **portal contracts**, per page_kind, validated at load; **`persistence:` excluded from `contract_sha256`** | One concept had two homes. And the header hash governed the whole file, so editing ARCHIVE config bumped the version governing CLAIM identity — re-inserting 5,135,469 claims / **2,625 MB** of an append-only table, once per tweak. Proven closed end-to-end against a replayed DB: a persistence edit now re-inserts **0** claims; the negative control (a version bump) still re-inserts. |

Also closed in-wave: the cohort stamp can no longer name an instrument that was not applied
(`append_payload` refuses a caller profile without its label); a malformed or `:contains()`
selector is refused at contract load rather than silently collapsing a body to the raw-bytes
fallback (selectolax **segfaults** on `:contains()` — exit 139, uncatchable); and a comment at
column 0 inside `persistence:` is refused, because it would end the exclusion block early and
the resulting refusal message would misdirect an operator into spending exactly the 2.6 GB.

**Storage, re-derived from live data (the artefact the sign-off rests on):**

| | at cap 2 |
| --- | --- |
| R2 (bodies) | ~28.6 GB ≈ **$0.43/month** |
| Postgres (metadata rows, 713 B/row measured) | **~1.5 GB** of ~4 GB allowance |
| Largest cap that still fits | **7** |

### The churn numbers, per (source, page_kind) — first substantial reading under `payload_norm@3`

| source | detail, `@1` (guessed) | detail, `@3` (measured) | repeats |
| --- | --- | --- | --- |
| sreality | 0.04 % | **0.2 %** | 63,494 |
| bezrealitky *(control)* | 0.02 % | **0.1 %** | 3,596 |
| remax | 100 % | **0.9 %** | 226 |
| mmreality | 100 % | **2.4 %** | 592 |
| realitymix | 67.7 % | **12.4 %** | 6,108 |
| ceskereality | 100 % | **16.2 %** | 2,305 |
| maxima | 17.1 % | 24.7 % | 89 |
| **idnes** | 100 % | **98.9 %** | 5,456 |

**The profiles worked, except on idnes.** remax and mmreality went from every-fetch to
essentially never; ceskereality and realitymix fell by 5x. idnes did not move: the measured
profile named its Nette contact form (captcha counter, hash, question) and the similar-offers
rail, and something else on that page is still moving. **Recorded, not chased** — the wave's
goal was a trustworthy number, and this is one.

**Index surfaces remain ~100 % and unprofiled** (sreality 99.4 %, ceskereality 100 %, remax
100 %). Nobody has diffed an index page yet. `payload_index_archive` is a separate flag from
`payload_dual_write` for exactly this reason, and on current evidence they deserve opposite
answers.

**Why idnes at 99 % no longer threatens the storage projection:** the cap and the 7-day floor
bound a listing at 2 bodies whatever its churn rate, so a failed profile is now a cost
optimisation rather than a storage risk. That is the whole point of bounding by construction
rather than by filter quality.

### Those three decisions — answered (2026-08-16/17)

The wave put three questions to the operator. All three now have answers, and W2a's
measurement phase is closed:

| Decision | Answer | State |
| --- | --- | --- |
| enable `payload_dual_write` | **YES** — decided 2026-08-16 | **ON globally 2026-08-17 12:29 UTC**, all nine portals, no per-portal overrides (as reported by the session that flipped it — re-query `app_settings` to confirm) |
| enable `payload_index_archive` | **NOT YET** — the evidence said opposite answers for the two flags, and the flags exist separately for exactly that reason | **OFF.** Index keys are week-stamped, so the cap-2 bound holds detail but not index, and the surface still churns ~100 % un-diffed |
| run the 445k-row backfill | **YES** | **In progress** — ≈61 % at 2026-08-17 17:08 UTC, resumable, 0 unmapped; see the wave-status table |

**What that leaves open is no longer a W2a question.** The one measurement still missing is
what actually moves on an INDEX page — nobody has diffed one, which is why the second flag
stays off. That is a prerequisite for enabling `payload_index_archive` later, not for
anything W2a shipped, and it does not block W2: the re-mine lane reads detail bodies.

## W3 — shipped (history backfill from `listing_snapshots`)

### First contact, 2026-08-17 — and what a green tick did not tell us

The lane's first-ever production run was a `dry_run=true` smoke (Actions 32077606722, `mode=full`,
`max_seconds=300`, `batch_size=10000`, writes nothing). It walked **90,000 snapshots at ~300/s**,
stamped `outcome='stopped'` on its budget with `cursor_after_id=90000` for a clean resume, and
exited green. It validated DB connect, the blocking Mapy-inventory precondition
(`inventory_rows=57204`, terminal AND complete in the current restart epoch — the lane refuses
outright otherwise), the contract projection load, the keyset scan and extraction across all nine
portals' contracts.

**It also reported exactly 1 claim and exactly 1 absence per snapshot, uniformly, across all
90,000 — and that is indistinguishable from a broken classifier.** Genuine extraction produces a
variable count per row. The run was investigated rather than accepted, by probing the classifier
against the committed fixtures: legacy-shape sreality yields exactly 1 claim
(`sr.det.legacy_locality_value` → `address_line_verbatim`) plus 1 coordinate absence, while
post-cutover yields 21 claims and 0 absences. So snapshot ids 1–90,000 are *entirely* pre-cutover
legacy-shape sreality — the oldest rows in the table, all predating the June-2026 payload change.
Uniform cohort, not a defect.

**The rule this is recorded for: a green exit says the code ran, never that it did the right
thing.** The distance between "clean first contact" and "silent defect shipped" here was one probe
run against a passing signal, and nothing in CI, the exit code or the log line would have closed
it.

### The legacy-shape absence is not an edge case — it covers the entire early corpus

W3 deliberately disables the refetch-cohort enrollment W1 uses to surface legacy-shape rows
(`route_legacy_shape_to_refetch=False` — a snapshot cannot be refetched, so enrolling one would
flood the live cohort with rows that were never wrong, only old). That left a hole:
`_read_point_pair` refuses a pre-cutover payload outright (no `gps_lat`/`gps_lon` at the
post-cutover locator), so such a snapshot produced **neither a coordinate claim nor any record that
a coordinate had been sought** — silently indistinguishable, forever, from "not yet re-mined", in an
append-only table. `record_legacy_shape_absence=True` (default False, so W1's already-measured D3
gate is untouched) closes it with an explicit `not_attempted` absence per snapshot.

The smoke measured the blast radius of that fix: it fires on **every one of the first 90,000
snapshots**, i.e. on what looks like the whole early corpus rather than a rare cohort. It was found
by a peer session's review of #1057 and would otherwise have shipped silently.

### First production WRITE, 2026-08-17 — a pre-stated prediction, falsified

Run 32081432670 (`dry_run=false`, `max_seconds=600`, `batch_size=10000`), resumed from id 0
because a dry run writes no batch row and therefore leaves no cursor:

| counter | value |
| --- | ---: |
| snapshots | 130,000 |
| claims | 170,569 |
| **claims_inserted** | **33,064 (19.4 %)** |
| observations | 134,741 |
| absences | 127,559 |
| dirty enqueues | 32,961 |
| oversized | 0 |
| outcome / cursor | `stopped` / 130,000 (resumable) |

Throughput ~213 snapshots/s with writes, against ~300/s dry.

**The prediction, stated in advance and wrong.** Before dispatch it was written down that claims
would be *mostly genuine inserts rather than dedupes*, on the reasoning that W1 mined current-state
`listings.raw_json`, so an old snapshot's locality string is a different value → different
fingerprint → a new claim. A falsifier was committed to at the same time: *if `claims_inserted`
comes back far below `claims`, that assumption is wrong.* It came back at 19.4 %. The assumption
was wrong.

**The actual mechanism, and the measurement that distinguishes it from the rival explanation.**
`listing_snapshots` appends on **any** content change — a price edit mints a snapshot whose
locality is byte-identical to the previous twenty. So one listing contributes many snapshots, the
first minting a claim and the rest re-sighting it. That is *within-W3* dedup, not the
*cross-substrate* dedup the prediction reasoned about. A rival hypothesis was on the table (that
these listings simply never changed locality, making the cohort narrower than "all pre-cutover" and
weakening extrapolation). The per-batch insert rate discriminates them:

**69 % → 60 % → 45 % → 34 % → 18 % → 12 % → 8 % → 4 % → 2 %**

A cohort property would hold roughly flat across batches — it is a fact about *which* listings are
scanned. Monotonic decay is a fact about *how far into the scan* you are: later snapshots
increasingly belong to listings already claimed. The rival hypothesis predicts flat and is refuted.
This is the difference between an explanation that fits and a measurement that decides.

**The legacy→post-cutover boundary is visible and sits at snapshot id ≈ 120,000–130,000.** Batch 13
jumps to 5.06 claims/snapshot while absences fall to 0.76/snapshot. Solving each independently for
the post-cutover fraction (claims = 1 + 20x, absences = 1 − x) gives **x = 0.203 from claims and
x = 0.244 from absences** — two unrelated counters agreeing, which is what makes the cohort model
credible rather than merely consistent.

### W3 progress, and where it is BLOCKED (2026-08-18)

Four windows dispatched after the first write run; three completed, the rest cancelled while
pending. **Cursor 1,040,013, `reached_end=false`, all work committed and resumable.**

| window | snapshots | claims | inserted | observations | cursor |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 130,000 | 170,569 | 33,064 (19.4 %) | 134,741 | 130,000 |
| 2 | 470,000 | 4,331,091 | 9,282 (0.21 %) | 4,309,552 | 600,011 |
| 3 | 440,000 | 2,612,223 | 2,608 (0.10 %) | 2,599,685 | 1,040,013 |

**~44,950 historical location claims recovered** — values that differ from what those listings
serve today, i.e. the location changes that existed nowhere before this wave — plus **~7.04 M
observations** dating when each value was seen. The insert-rate decay is the §"falsified
prediction" mechanism going asymptotic: nearly every snapshot restates an unchanged location, so it
becomes an observation, and the small insert residue IS the history.

**BLOCKED: `location-batch` is saturated by its own crons and a 45-minute window cannot win a
slot.** Six dispatches were cancelled while pending. The mechanism, measured:

- The hourly **claims-intake** holds the group for a 2700 s budget — e.g. `32101486996` in_progress
  05:04:38→05:53+, with the next intake `32103795929` starting 05:57:47 before the previous had
  cleared. That is a ~75 % duty cycle on its own.
- The **`*/15` resolve tick** arrives four times an hour and, per GitHub's group semantics,
  **supersedes whatever is pending**. Observed repeatedly: `32103183492` sat pending 05:31:02→05:51:30
  and was killed by the 05:51:29 tick; `32105198517` sat 06:01:44→06:20:48 and was killed by 06:20:47.
- Therefore **while a long holder occupies the group, every pending entry is doomed — only the last
  arrival before the holder releases ever runs.** A cadenced tick that has nothing to do reliably
  beats a backfill window that does, because it arrives later.

This is the §Standing-decisions oversubscription finding as a hard stop rather than an argument: it
is no longer costing lag, it is costing progress. **The unblock is an operator action**, and W1's own
backfill already set the precedent — its `*/15` resolve cron was removed for the duration of the
sweep. Either of the two documented fixes (stagger the crons; or let a running backfill outrank an
empty tick) also resolves it.

**Do not "fix" this by retrying.** A retry dispatches a *new* run into the same group, which
supersedes your own previous pending entry and sends you to the back of the queue — four W3 runs
were lost that way before the mechanism was understood. Under contention: dispatch **once**, then
wait. A pending run is queued, not wedged, and the two are indistinguishable from outside — which is
the same *snapshot-treated-as-current-state* failure as everything else in this file.

### The denominator is not a finish line — `reached_end` is

**No percentage-complete figure for this wave should be quoted, including from this document.**
`1,574,313` is a point-in-time reading taken 2026-08-10 during the recon, and `listing_snapshots`
is **live-written** — the scrapers append a row on every content change, continuously, while the
backfill walks. So the denominator grows underneath the scan and any "N % done" computed against it
is wrong in an optimistic direction and gets more wrong with time.

The sibling W2a payload backfill demonstrated this the hard way on the same night: it reported
"90.2 %, one window from done" against its own 445,191-row inventory count, then ran to completion
at **472,429 pages — 27,238 past the supposed total, 6.1 % larger than the number both sessions were
computing percentages against.** At the moment "90 % complete" was being quoted, the undercount alone
exceeded the entire remaining tail. Same shape, same cause, a
caveat that had been stated once and then quietly ignored for four hours of progress reports.

**The only completion signal is `outcome='ok'` / `reached_end=true`**, which this lane's module and
workflow header already define correctly: `'ok'` means the scan ran out of rows, never that it ran
out of budget. Report that. Reporting remaining work requires a live `count(*)`/`max(id)` against
`listing_snapshots`, which no session had access to on the night this was written.

### Supersession is structural, and it happened here

A follow-up diagnostic run (32082367045) was dispatched into a gap that closed between the check
and the dispatch: the hourly intake cron took the slot first, leaving the run `pending`. A routine
`*/15` resolve tick was created at **23:55:53Z** and the pending run was cancelled at **23:55:54Z**.
Zero log lines — it never started, wrote nothing, and left no stranded `outcome='running'` row.

**The operational rule, which is W3's to carry:** a point-in-time idle check followed by a dispatch
has a race in the gap that no amount of care closes; only
*dispatch-then-verify-`in_progress`-within-N-seconds* detects the loss. Disarming automation is not
enough either — **looking is not holding**. Any future W3 dispatch should verify it reached
`in_progress` and re-dispatch if it did not.

**The structural argument is NOT recorded here on purpose.** This incident is the third direction of
the same `location-batch` oversubscription finding (intake displaced by backfills at 57.5 %
cancellation; a backfill displaced by intake; a backfill displaced by a resolve tick that had
nothing to do). That case rests on all three instances together and belongs with the cancellation
measurement in the W2a/group write-up, not duplicated in a wave section — see the `location-batch`
entry under Standing decisions and the W2a wrap-up.

### Volume: watch observations, not claims

**Measured, not projected, as of cursor 130,000:** 134,741 observations from 130,000 snapshots
(~1.04/snapshot) and 33,064 new claims. Naively that is ~1.6 M observations for a ~1.57 M-row
table — but that figure is a **floor, not an estimate**, on two counts: it was measured almost
entirely inside the 1-claim legacy cohort, and **the denominator itself is stale and growing**
(see below). ~300 snapshots/s holds only while the scan is in the 1-claim legacy cohort. Post-cutover ids carry
~21 claims each, and the great majority will dedupe on the time-free `claim_fingerprint` against
what W1 already wrote from `listings.raw_json` — so `claims_inserted` should stay modest while
**`location_claim_observations` grows hard**, and 01 §4.3 already names it the highest-cardinality
table in the design. That counter, not the claim count, is the one to watch as the scan advances,
and it must not be conflated with W2a's separate storage budgets (the archive's own ~7.9 GB R2
footprint and the ~4 GB Postgres metadata allowance are different ledgers).

### How the lane is being run

Built in a separate git worktree off `origin/main` (never on the main checkout's branch), on a
DIFFERENT substrate from W2/W2a (`listing_snapshots.raw_json`, not archived HTML), so it does not
wait on that in-progress work — coordinated with the session building W2a/W2 to avoid touching the
same files without visibility into each other's changes.

**Rebased onto current main 2026-08-17** (PR #1057, originally cut 2026-08-13 at `1cf383c9`). Main
had moved 25+ commits, including W2-2 (#1079), which independently landed ONE of this PR's six
additive `claims_intake.py` changes — `write_result(..., extractor_version=...)` — in a converged
form. The other five (the `snapshot_id` field on `Claim`/`Absence` and its plumbing through
`_CLAIM_WRITE_SQL` / `_ABSENCE_WRITE_SQL` / the `resighted`+`obs` CTEs / `dedupe_absence_rows`, plus
the two `extract_listing` flags) were untouched by it and applied as union merges alongside W2's
evidence columns — the two sets are additive to the same statements and do not overlap
semantically. `claims_remine.py` itself rebased with zero conflicts. Three assertions in W2's
`test_claims_remine_archive.py` were written as `"snapshot_id" not in row` when no lane wrote the
column; they now assert `row["snapshot_id"] is None`, which is the same guarantee stated against the
value rather than the key, and strictly stronger.

**What shipped, in one PR:**

- **`location_data/claims_remine.py`** — reuses W1's (`location_data.claims_intake`) readers,
  `Entry`/`ListingRow`/`Claim`/`Absence` shapes, licence ladder (`coordinate_verdict`) and
  batch/write SQL wholesale rather than re-deriving them: a snapshot's `raw_json` is a verbatim
  historical copy of the SAME payload shape `listings.raw_json` holds today, so the SAME
  contract-entry readers apply unchanged. The module's own job is the snapshot scan/keyset over
  `listing_snapshots` instead of `listings`, which claim types are admissible from that substrate
  per source, and the snapshot-anchor / observation-time plumbing.
- **Small additive changes to `location_data/claims_intake.py`** so the two lanes share one write
  path: `Claim.snapshot_id` / `Absence.snapshot_id` (both `None` by default — W1 unaffected),
  `_CLAIM_WRITE_SQL` / `_ABSENCE_WRITE_SQL` and the `resighted`/`obs` observation CTE now carry
  `snapshot_id` through to `location_claims` / `location_claim_absences` /
  `location_claim_observations`, `dedupe_absence_rows`'s in-batch key includes it (a W3 batch
  routinely holds several snapshots of ONE listing, unlike W1), `write_result()` takes an
  `extractor_version` parameter (default unchanged) so a re-mined absence is never misattributed to
  the W1 lane, and `extract_listing()` gains `route_legacy_shape_to_refetch` (default `True`,
  unchanged for W1) so a historical legacy-shape snapshot — an accurate fact about the past, not a
  gap a live refetch could close — never enrolls in the refetch cohort, while the truncation
  ABSENCE itself still gets recorded (03 §3.2 rule 4: every attempt is recorded, including
  negatives). **No new migration** — every construct this needed (`snapshot_anchor`, `snapshot_id`,
  `history_completeness`) was already on the schema per 06 §6.6.6; only the write path had to learn
  to populate them.
- **Coordinate-history scoping, resolved against ground truth, not just the design table's
  hedge.** 06 §6.2.2 hedges mmreality's coordinate with "but only where those fields participated
  in the hash" — flagged uncertainty in the design corpus. `scraper/scraped_listing.py`'s
  `_HASH_FIELDS` (the ONE allowlist all eight non-sreality portals share, confirmed live in both
  `bezrealitky_parser.py` and `mmreality_parser.py`) excludes `lat`/`lon`/`street`/`house_number`/
  `zip` for ALL eight — so a coordinate-only change never appends a snapshot for any of them,
  mmreality and bezrealitky included, and a snapshot's mere existence is not evidence a coordinate
  was checked. Worse for the six `geom_column`-substrate portals (bazos/idnes/ceskereality/
  realitymix/maxima/remax): `listing_snapshots` carries no `geom`/`lat`/`lon` column at all
  (migration 001 + 320), and 06 §6.1.3 states their raw_json never carried the coordinate VALUE to
  begin with, only the provenance method — no historical value to read, full stop. **Only sreality
  gets `claim_type='coordinate'` claims from this lane.** Every OTHER claim type the contract
  yields for the eight (obec_code, cast_obce_name, precision_declaration, locality/district text,
  ...) is still mined; only the coordinate type is filtered out of the entries handed to the
  reader loop. `history_completeness` is `'full'` for sreality and `'locality_text_only'` for the
  other eight uniformly, matching 06 §6.4's W3 gate verbatim — deliberately NOT
  `claims_intake.HISTORY_COMPLETENESS`'s richer `payload_only` split for mmreality/bezrealitky,
  which answers a different question (W1's "is the CURRENT payload present", not W3's "does a
  snapshot's existence mean this field was checked").
- **Batching discipline matches §6.7's W3 risk note** (a prior single-transaction backfill
  deadlocked against live ingest writers — `repo-known-issues.md` #25): bounded batches with
  per-batch commit (`guarded()`, reused from `claims_intake`, 5 s lock timeout), keyset pagination
  over `listing_snapshots.id` (full mode) or `(scraped_at, id)` (incremental), a resumable cursor
  stamped on `location_claim_batches` (lane `location_claims_remine`, wave `W3`) exactly like W1's
  `outcome='stopped'` vs `'ok'` split, and an attempt row for every candidate including negatives
  (a licence-refused or truncated snapshot still writes an absence row, snapshot-scoped).
- **Lease-row CAS + concurrency group.** `.github/workflows/location_claims_remine.yml` joins the
  outer `location-batch` GH concurrency group (shared with registry load / claim intake / Mapy
  inventory / resolve — the 2026-08-10 incident's guard) plus its own inner `location-remine`
  group, AND takes a `location_jobs` lease-row CAS (`location_data.resolver.lease`, job name
  `location_claims_remine`) — never an advisory lock, since the transaction-mode pooler would
  strand one. The lease is a second, orthogonal guard that also catches a manual local invocation
  racing the scheduled workflow, which a GH-only concurrency group cannot.
- **`workflowDocs.generated.ts` regenerated** (`python scripts/generate_workflow_docs.py`) so the
  new workflow's docs stay in sync (CI gate).
- **Tests** (`tests/location_data/test_claims_remine.py`, 27 cases) cover: coordinate-entry
  scoping per source, snapshot anchoring (`snapshot_anchor='snapshot'` + the right `snapshot_id` on
  every claim AND absence), `history_completeness` per the W3 mapping, the refetch-cohort
  suppression (with the truncation absence still recorded), and — the gate's own language made
  concrete — that a genuinely changed coordinate across two snapshots produces two Claim objects
  with different values while a repeated one produces the same value (the corpus-level "one claim
  row per distinct value, an observation row per re-sighting" guarantee itself is a SQL/DB-level
  fact exercised by `test_claims_intake_fingerprint.py`'s sibling coverage, not re-derived here).
  Full existing suite re-run clean after every additive `claims_intake.py` change (3,901 passed,
  74 skipped, 0 failures).

**Deliberately NOT done in this PR — dispatch is a separate, operator-gated step.** The workflow
is `workflow_dispatch`-only, no `schedule:` trigger, so merging cannot start the backfill on its
own. Per standing instruction: before dispatching the ~1.57M-row backfill against production, check
with the operator — this is a shared instance that four concurrent location lanes have already
knocked over once (2026-08-10 incident, below). A follow-up PR can add an `incremental` schedule
(mirroring W1 intake's hourly cron) once the initial full pass has completed.

**A SECOND gate now applies, and it is not the operator's: `location-batch` is currently
saturated.** As of 2026-08-17 the W2a payload backfill is mid-flight (≈61 % of 445k pages, ~4
dispatches left, each holding the group ~45 min) while hourly intake and the `*/15` resolve drain
compete for the same single pending slot. This lane joins that same outer group by design, so
dispatching it now would either queue behind the payload backfill or displace a tick of it — the
exact contention W1's own backfill hit when its `*/15` resolve cron had to be removed for the
duration. **Wait for the payload backfill to finish before the first W3 dispatch**, then treat W3's
own run the same way W1's was: budgeted dispatches into idle gaps, not back-to-back.

### Close-out, 2026-08-19 — scan complete, all four gate arms PASS

The `mode=full` backfill reached the end of the corpus and the gate was verified against it.
Terminal batch 289: `{"outcome": "ok", "reached_end": true, "cursor_after_id": 1634096}`.

| | total |
| --- | ---: |
| snapshots mined (5 windows) | 1,634,096 |
| historical location claims | 92,312 |
| observations | ~10.85 M |

**The denominator moved again, exactly as W2a's did.** The wave opened against 1,574,313 rows; the
scan terminated at 1,634,096, because `listing_snapshots` is live-written and grew ~60 k rows over
the nine days the wave ran. `reached_end` is the only completion signal — never a percentage
against a remembered count (#1088, and the identical W2a lesson).

**The "decaying tail" reading was wrong, and the numbers say so plainly.** Windows 1–3 decayed
69 % → … → 0.59 % insert rate, which looked like a corpus converging and made the residual seem
marginal. Window 4 came back at **7.7 %** and window 5 at **9.3 %** — a 13× reversal — with the
refetch cohort jumping 1,063 → 21,618 in one window. Insert rate is a property of the CONTENT at
each scan depth, not a monotonic function of depth, and the newest snapshots were the opposite of
a low-value tail. Had the residual been abandoned on the decay reading, ~47 k claims and ~3.8 M
observations would have been left unmined.

**Unblocking it needed BOTH crons paused, not one.** Pausing the hourly intake (#1100) was
necessary and insufficient: measured over eleven ticks on 2026-08-18, a `location_resolve` run
occupies `location-batch` for **11–27 minutes on a 15-minute cadence**, so that lane oversubscribes
the group by itself — about one tick in three completed and the rest superseded each other, leaving
no gap wide enough for a 45-minute window to acquire a runner. With both paused (#1101), each
dispatch picked up a runner immediately; both were restored (#1105) the moment the scan finished.
`location_resolve.yml` already carried W1's account of this; W3 added the measurement W1 asserted.
**The next backfill that needs this group should expect to pause both.**

### The gate arm that no test could hold, and the artifact it passed on first

Three gate arms are properties of the extractor and the suite already held them. The fourth — a
sreality per-listing coordinate/precision time series with VISIBLE oscillation — is a property of
the CORPUS, so nothing offline could show it, and there was no lane to read it with and no session
holding DB credentials. `location_data/claims_remine_verify.py` + a read-only workflow (#1102)
closed that, deliberately taking **no lease** and sitting in its **own** concurrency group: a
verifier must be runnable while a backfill window is in flight, and an audit queued behind the lane
it audits is unrunnable.

It took two corrections before its numbers were worth anything.

1. **Scoping (#1104).** A claim's `extractor_version` is the CONTRACT's
   (`contract:{source}@{version}`); `REMINE_VERSION` rides the batch and absence rows only, so
   filtering claims by it matched nothing and the first run refused against a fully mined corpus.
   The anchor is the whole discriminator. The same PR fixed a subtler one: the series must be
   scoped on `o.snapshot_id IS NOT NULL`, not the claim's anchor — because the fingerprint is
   time-free, a historical value equal to one W1 already claimed gets a W3 *observation* on W1's
   claim row, so anchoring the series on the claim would have dropped precisely the listings whose
   history returns to today's value.
2. **Partitioning (#1106) — the one that matters.** The gate then PASSED, reporting **165,706 of
   165,708** sreality listings as oscillating. That is not a result; it is the shape of a bug. A
   `claim_type` can be emitted by more than one contract entry (sreality declares two for
   `precision_declaration` and two for `coordinate`), and those entries write distinct claims
   sharing one `observed_at` — so a series partitioned by listing alone interleaves them and scores
   a change at every step. Partitioned by `(listing, contract_entry_id)`, the figure is **324**.

**A gate arm that passes with a suspiciously TOTAL number deserves the scrutiny of one that
fails.** 165,706-of-165,708 has the same signature as this wave's own smoke run reporting a uniform
1.0 claims / 1.0 absences per snapshot — both looked like results, both were properties of the
cohort. Nothing in the pipeline would have caught it: CI was green, the arm said PASS.

### The measured oscillation — and what it says about the program's premise

Verify run 32223331085, after both corrections:

| | listings | changed | changed ≥2× | returned to a prior value | max changes |
| --- | ---: | ---: | ---: | ---: | ---: |
| `coordinate` | 161,405 | 480 | 31 | 25 | 9 |
| `precision_declaration` | 165,708 | 324 | 22 | 21 | 9 |
| **union** | **327,113** | **804** | — | **46** | 9 |

The arm passes: the series exists at scale, values demonstrably move, and 46 listings return to a
value they previously held — oscillation proper, not one-time correction.

**But it is a small cohort, and that is the honest headline: 804 of 327,113 listings (0.25 %) ever
changed a location value across the entire snapshot history.** sreality's location data is far more
stable than the churn premise behind this program assumed. That does not invalidate the claims
layer — history that is *provably* stable is a real answer, and the 46 oscillating listings are
exactly the cohort a resolver must not thrash on — but it should temper any wave whose value rests
on location churn being common. **Worth measuring on the other eight portals before W4 leans on
it.** (The `coordinate` figure is sreality-only by construction — the other eight portals'
snapshot hash excluded lat/lon, so their history is `locality_text_only`.)

**Still open:**

- Decide whether to add the `incremental` cron now that the full pass is done. Not urgent: the W1
  intake already claims live rows hourly, so the incremental W3 lane only recovers snapshots
  written between intake ticks.
- The 8-portal `locality_text_only` cohort has no coordinate history to oscillate. If per-portal
  coordinate history matters, it needs `_HASH_FIELDS` widened at the scraper — a W0/W4 decision,
  not a W3 one.

**Erratum to the design corpus, discovered coordinating with the concurrent W2/W2a session
(2026-08-13): the module name `location_data/claims_remine.py` is independently assigned to TWO
different waves.** This section's module re-mines `listing_snapshots` (history, this wave); the
W2-2 plan (`~/location-data-architecture-2026-08-10/BUILD-PLAN-w2a-w2.md`, "Evidence-bearing
claims + the re-mine lane") independently assigns the SAME file path, `LANE =
"location_claims_remine"` and `REMINE_VERSION = "claims_remine@1"` to a module re-mining
`portal_raw_payloads` (archived HTML, W2) — same naming convention, baked into two design-corpus
sections for two different substrates. Not cosmetic: `location_claim_batches`'s resume/watermark
queries key on `(lane, source, scan_mode)` only, so two waves sharing one `lane` string would read
and corrupt each other's resume cursors the moment both ran for real. Resolution (this wave ships
first and is already tested): **W3 keeps `location_data/claims_remine.py` / `LANE =
"location_claims_remine"` / `REMINE_VERSION = "claims_remine@1"` / workflow
`location_claims_remine.yml` / concurrency group `location-remine`, exactly as shipped here.** W2-2
(and W2-13's eventual dispatch workflow) must use a disambiguated name instead —
`location_data/claims_remine_archive.py`, `LANE = "location_claims_remine_archive"`,
`REMINE_VERSION = "claims_remine_archive@1"`, workflow `location_claims_remine_archive.yml`, group
`location-remine-archive` — so a future reader hits this note instead of rediscovering the
collision by watching a resume cursor jump between two substrates.
**RESOLVED AND HONOURED — W2-2 shipped as #1079 using exactly the disambiguated spellings**
(`location_data/claims_remine_archive.py`, `LANE = "location_claims_remine_archive"`), verified
against main 2026-08-17. The two lanes now key `location_claim_batches` on distinct `lane` strings
and cannot read each other's resume cursors. The note stays as the record of WHY the archive lane
carries the longer name — deleting it would invite the next reader to "simplify" it back.

## W2 after the reader layer: what the first activation attempt taught (2026-08-17)

The archived-HTML reader layer shipped (#1081). **No portal contract is activated yet, and the
remax attempt was reverted TWICE for two different real reasons** — both worth carrying, because
they are properties of the activation itself and will recur for every portal.

**Attempt 1 — `shadow: true` would have darkened LIVE claims.** Shadow is HEADER-grain: projecting
a version DEACTIVATES the previous one and W1 loads `WHERE is_active`, so shadowing remax@3 would
have taken remax's four already-live W1 entries dark too — a certain, immediate regression lasting
until someone un-shadowed, with the frozen sample not even drawn.

**Attempt 2 — an unshadowed bump buys NOTHING and costs a corpus re-insert.**
`location_claim_fingerprint` (migration 386) hashes **both** `extractor_version` and
`contract_entry_id`, and a version bump changes both. So the next hourly scans re-insert every
remax claim as a new row in an append-only table — the exact waste migration 408 was written to
prevent, and which the contracts test still warns about verbatim. And the gain today is **zero
claim values**: the two activated entries are DOM entries, W1 skips them, and the archive lane has
no workflow. Paying an unrecoverable re-insert for nothing.

**Therefore: a portal activation must be bundled with W2-13**, the wave that gives the archive lane
a workflow. That is the first moment the re-insert buys something, and — via the rail below — the
moment shadow becomes both necessary and correct. Activating earlier is strictly worse on both axes.

### Two rails found the hard way, both now permanent

- **The shadow decision is now decidable rather than dogma.** The build plan says "each ships in
  shadow"; the header-grain problem above means that is wrong while nothing can run a DOM entry, and
  right the moment something can. `test_a_dom_contract_must_be_shadowed_once_a_lane_can_run_it`
  encodes exactly that: it scans for anything that could put `claims_remine_archive` on a runner and,
  the moment one exists, requires every contract with an executable DOM entry to be shadowed. The
  scan is deliberately wider than `.github/workflows/*.yml` — `.yaml`, `scripts/`, and shell
  wrappers all count, because a `*.yml`-only glob would walk past a workflow that calls a wrapper —
  and it accepts false positives over false negatives, since the cost of the former is one
  `shadow: true` line and the cost of the latter is unreviewed claims serving live.
  `test_the_dispatcher_scan_sees_a_lane_however_it_is_spelled` is the rail's own negative control,
  against a synthetic tree: WITHOUT it the guard never executes its assertion in CI (there is no
  dispatcher today, so it returns early) and would be a rail nobody has ever watched fire. It also
  pins that W3's `claims_remine` — a different lane on a different substrate — must NOT trip it.
- **A DOM reader in a W1 contract would have taken the hourly intake down.** `extract_listing`
  refuses an unknown reader, and DOM readers live in a separate registry, so loading any W2 contract
  would have thrown `IntakeRefused` for that whole portal, hourly. W1 now SKIPS an archive-only
  reader; a name in NEITHER registry stays a hard refusal; `ARCHIVE_ONLY_READERS` is pinned equal to
  `ARCHIVE_READERS` by test.

### The finding that reshapes the wave

Six agents verified every declared-but-inert DETAIL entry of the remaining six portals against its
pinned fixture. **Zero of them can be activated with the three generic DOM readers**, and the reason
is structural rather than marginal: those readers (a CSS selector, an attribute, a DMS attribute)
fit remax's markup and essentially nothing else.

| portal | candidates | why none activates |
| --- | --- | --- |
| ceskereality | 4 | `cr.det.data_city` is the closest call and works on the fixture (`data-city="České Budějovice"`), but the entry's OWN note documents the live shape as `"Praha (okres Praha)"` — obec + parenthesised okres in one string, the remax `"Úvaly, okres Praha-východ"` defect exactly, and the fixture does not cover it. The other three need a regex tail-parse or sit on `og_meta`/`regex_text`. |
| realitymix | 1 | `rm.det.gps` carries **separate decimal attributes** (`data-gps-lat="49.73561"`, `data-gps-lon="13.39051"`) — not a DMS string. `html_point_dms` cannot read it. *(Independently re-verified with selectolax.)* |
| idnes | — | `id.det.subject_feature` is a MapTiler JSON blob needing `properties.id == <listing id>` selection. |
| bazos | — | `bzs.det.blur_hint` is `portal_declared_quality`, not a DOM text read. |
| mmreality | 4 | An **`embedded_json` portal** (a Vue `:property` prop). DOM readers are confined to DOM surfaces, and three candidates declare no `css` at all. |
| maxima | 7 | Four are `map_config` — coordinates inside a JS string needing regex → js-string decode → JSON → pointer → OpenLayers geometry. `mx.det.locality` reads `"Brno, Brno-střed, Veveří"` (obec + obvod + quarter) typed as `mestsky_obvod_name`; `mx.det.title` includes the agency's own branding. *(Both re-verified with selectolax.)* |

**So the remaining scope of W2 is READER work, not YAML work.** In rough value order: a JSON-pointer
archive reader over an embedded blob (unlocks idnes, mmreality, and maxima's map config), an
attribute-PAIR coordinate reader (unlocks realitymix immediately — its entry is already the one
`ARCHIVED_COORDINATE_RULES` names), a regex reader emitting an evidence span over the **capture
group** (unlocks ceskereality's title and mmreality's `originalTitle` street, called "the single
highest-yield fix for this portal"), and splitting transforms for the combined administrative
strings. Only then do the contracts become one-line activations.

**The sequencing that follows from all of the above**, and it is now well determined: build the
missing readers → build W2-13's sweep lane and workflow → THEN activate all seven portal contracts,
shadowed, in one wave. That order pays the claims re-insert exactly once, at the only moment it buys
anything, and it puts the shadow flag on at precisely the moment the rail starts demanding it.
Activating any portal before W2-13 is strictly worse on both counts, which is why remax@3 was
written, reviewed, and then deliberately not merged — twice.

**Two fixture gaps worth knowing before trusting a future activation:** ceskereality's fixture is a
České Budějovice page, so the documented `"Praha (okres Praha)"` shape is untested; and mmreality's
carries exactly ONE `:property` blob, so the zone that strips non-subject blobs — which that
contract calls its principal hazard — is entirely unexercised.

### W2-13 — sweep lane + per-portal gate report (SHIPPED)

`.github/workflows/location_claims_remine_archive.yml` (dispatch-only, outer group
`location-batch` + inner `location-remine-archive`, SUPABASE_DB_URL + the four `R2_*` secrets, a
contracts projection step) and `scripts/location_w2_gate_report.py` +
`.github/workflows/location_w2_gate_report.yml` (read-only, no lease, its own
`location-w2-gate` group so it is runnable *while* a sweep is in flight). No reader, no contract
entry, no `contract_version` bump, no migration: the shadow rail stays green because it fires only
when a dispatcher AND an executable DOM entry both exist, and no shipped contract names one yet.

**Two lane fixes, both measured.** (1) The batch bounds are now this lane's own — 50/5000, default
500 (`ARCHIVE_MIN/MAX/DEFAULT_BATCH_SIZE`, `clamp_batch_size`) — because `claims_intake`'s shared
10,000 floor is sized for a keyset page over `listings`, while one iteration here holds a single
transaction across `batch_size` sequential R2 GETs plus every decompressed body at once (41–245 KB
each: 0.4–2.4 GB at 10,000). Even at 500 the transaction holds ~10–25 s of sequential HTTP; moving
the body fetches outside it is a named follow-up, not a fix in this wave. (2) An
`applicable_payloads` counter (bodies at least one entry was *declared* for) now rides the stats
dict, the progress log and the batch note, and feeds a zero-claim tripwire: `zero_claim_sources`
exits 3 (waivable with `--allow-zero-claims`) when a source saw ≥500 applicable bodies and mined
nothing. Until now a portal whose selectors had gone stale swept the whole archive, wrote nothing,
stamped `ok` and **moved the incremental watermark** — indistinguishable from success. Note the
watermark still moves in that case (`ok` honestly means "the scan ran out of rows"), so the repair
after a stale-selector sweep is a re-run in `mode=full`; an incremental re-run would skip the ground
it covered.

**Four CLI gaps declined, with reasons.** `--page-kind`: the fixture-diff gate's archived arm scores
only `page_kind='detail'`, so an index/map reader would ship ungated; land it *with* the first such
activation and with the watermark rule (a restricted run must not stamp a watermark covering the
kinds it skipped). `--json-out` on the lane: the durable record is the `location_claim_batches` row
the gate report already reads, and `main()` already logs the full stats dict as one JSON line.
`zones_unmatched` plumbing: `scope_html` runs *inside* `extract_payload`, so the document never
reaches `_run_source` — surfacing it means changing the extraction contract, and it stays the
sharpest open follow-up (a register whose zone stops matching while claims still flow is the one
failure the tripwire does NOT catch). Fetching bodies outside the batch transaction: a refactor of
`load_bodies`' cursor-taking signature.

**Deferred: the per-source `FLOORS` override.** `toolkit.location_labels.FLOORS` stays uniform.
`portal_contracts` has no floor column, and a governed top-level YAML key would make a floor edit a
`contract_version` bump — which changes `contract_entry_id` + `extractor_version`, both inputs to
`location_claim_fingerprint`, re-writing the whole 5.1 M-row / 2.6 GB claim corpus for a threshold
tweak. Two candidate homes when it is wanted: a new additive `portal_contracts` column (operational
state, like `shadow`) or the existing `precision_priors` jsonb. Whichever it is, it must be threaded
through `_block()` so `score_sample` and `score_shadow_claims` cannot disagree about which floor a
field was judged against. **Also still open:** house number is not scored (`_SCORE_SQL` computes no
counters and the NEW side is three columns against one free-text label), the old system's precision
class is structurally unscorable (mig 399 snapshots no granularity), and O18 — whether gate outcomes
persist to `location_metrics_rollup` — is untouched; the report writes nothing.

**Operating rule for a sweep dispatch:** pause BOTH `location_claims_intake.yml` (`35 * * * *`) and
`location_resolve.yml` (`*/15`) for the duration and restore them the moment the scan finishes. The
group holds one pending slot and a resolve tick with zero work supersedes a queued sweep purely by
arriving later. Do not answer a cancellation with a retry — a retry dispatches a new run into the
same race. One portal per dispatch: `run()` opens one batch per readable source and they share a
single `max_seconds` budget.

**O8 — the seven frozen-sample draws, and they are a one-way door.** Every draw must happen BEFORE
that portal's first sweep dispatch: the member row snapshots `legacy_street/_street_source/
_house_number/_obec/_okres/_zip` at draw time so the old system is scored "as it stood", and the
sweep plus the resolve drain rewrite exactly those serving values. Per portal, in order
(remax, ceskereality, realitymix, idnes, bazos, mmreality, maxima): dispatch
`location_labelled_sample.yml` with `source=<portal>, n=120, seed=(blank), write=false` for the
count-only dry run, then the same with `write=true`. `replace=true` is only for a deliberate
re-draw. Then label ≥100 members per portal through the admin-gated surface
(`frontend/src/pages/LocationQuality.tsx`, `GET/POST /location/sample/{source}`) — 2–4 h per portal,
14–28 h total. Check `portals.supports_complete_walk` first: ceskereality (mig 449) and idnes (453)
may be parked and `coverage_gate.yml` can un-park either autonomously four times a day, which
changes the population a sample is drawn from. Until a portal has ≥100 labels the gate report
correctly reads `NO SAMPLE` for it.

## W2-6…W2-12 — shipped (2026-09-05)

Seven portal contracts activated and **shadowed** in one wave, one PR, on top of the reader
foundation (#1270) and the W2-13 sweep lane (#1271). Nothing about the archive substrate
changed; what changed is that entries which had been *declared and inert* since W1 now name a
reader, so `claims_remine_archive` finds executable work for the first time.

The fleet census moved further in this one merge than in the whole of W1: **69 executable / 70
inert → 112 / 47**. Two portals are untouched — sreality and bezrealitky name no archive reader,
so they stay live and un-shadowed.

Per portal, what the bump turns on (each verified end to end through the real `ScopeRegister`,
`scope_html`, `stamp_archive_claim` and the C6 licence ladder, with every evidence span slicing
back to its own quote):

- **remax@3** — `rx.det.gps` (the DMS pin, licence `portal`, branch `portal_pin`) and
  `rx.det.header_address`; `rx.det.map_address` appended and matches nothing today, which is its
  expected steady state. The pinned fixture's hand-written one-line header was replaced with the
  real nested block, which moved four shared pins (`test_html_scope`, the payload-norm digest).
  Recorded finding, not fixed: `rx.det.raw_address_conflict` reads `/address`, a key the scraper
  renamed to `carousel_address` post-W0-0d, so the contradiction ledger only receives material
  from rows drained before that rename.
- **ceskereality@5** — `cr.det.title_line` (street) and `cr.det.data_city` (obec) on the pinned
  body, plus the new `cr.det.title_okres`, which is gated against the two REAL archived bodies
  (3861311 → Karlovy Vary, 3680359 → Trutnov) because the pinned `<title>` predates v5 and carries
  no okres suffix. ceskereality stays deliberately absent from `ARCHIVED_COORDINATE_RULES`: its
  pin already comes from `listings.geom` under `cr.det.legacy_pin`, and a second fingerprint for
  one position under an unlicensed locator is not an improvement.
- **realitymix@4** — the largest single activation (13 → 23 entries, 20 executable): the map pin,
  the four `map_*` address parts, the segmented `data-address`, both agency accuracy flags and
  four JSON-LD breadcrumb entries. The breadcrumbs **fail closed** — the reader is anchored on the
  kraj `@id` slug and only 3 of 14 kraj slugs are observed, so W2-13 must report per-kraj
  breadcrumb claim rates before anyone reads a yield number from them.
- **idnes@2** — the one bump that appends **nothing**: five already-declared entries given readers
  (`subject_feature` pin, `subject_address`, `info_text`, `no_exact_disclaimer`, `zoom`), with
  `on_miss=fail` subject selection, the three enumerated `reject_points`, the CZ bbox and the Mapy
  inventory veto all asserted. Honest coverage gap: idnes' `regressions:` block names no listing
  ids at all, and the repo's only real archived idnes page had its coordinate arrays destroyed by
  an old PII scrub, so the archived arm scores these five entries against one 2.3 KB modelled page.
- **bazos@2** — the town anchor's obec slug, the new `bzs.det.psc`, the maps anchor's zoom and the
  declared-blur marker. `bzs.det.blur_hint` claims the CONTRACT's canonical label
  (`approximate_location`) with bazos' own wording as the evidence rail, so a reword stops
  asserting rather than restating a different fact. An archived bazos pin stays unlicensable by
  construction (no `ARCHIVED_COORDINATE_RULES` row). The portal's first genuinely archived body is
  now in-repo. The `llm_text` entries are **bazos@3** and ship with the LLM lane, not here.
- **mmreality@2** — five new `blob_*` entries plus the pin, all id-matched on the subject blob
  rather than positionally (scored under a neighbour's id the same page yields that neighbour's
  obec, which is the test). `mm.det.point` moved from W1's `point_pair` to the archived
  `json_point`; `claims_remine._payload_lat_lon` was re-keyed on `locator.lat_pointer`/`lon_pointer`
  so W3's mmreality rows do not silently lose their lat/lon on that move. Pre-existing defect
  recorded, not fixed: `mm.det.placement` reads a dict with W1's scalar reader and gets None.
- **maxima@2** — the map pin, the uncertainty shape, the zoom and two new locality parts. A live
  check settled the shape the spec had guessed: a maxima Circle is
  `{"type":"Circle","center":[lon,lat],"radius":<degrees>}` — `center`, not `coordinates`. The
  regression it was re-mined for is confirmed: `d40026367` and `f60012682` are the same plot with
  stored pins ~830 m apart.

**The policy-gap migration (470).** Ruling: the activation is worthless without it. Seven of the
ten `location_extraction_method` labels had no `location_field_policy` v1 row, and
`survivorship.evaluate_field` does not rank an unmatched claim — it SKIPS it, and the field lands
in `survivorship_blocked` permanently. Deriving the (method × field) pairs the activated entries
actually emit gives four methods, seeded into the EXISTING `policy_version='v1'` (never a new
version — it is one of the five resolution-identity columns) with `ON CONFLICT DO NOTHING`:
`breadcrumb_parse` 450, `url_slug_parse` 500, `regex_text` 550, `legacy_column` 600, all
`may_fill_null=true`, `may_overwrite_non_null=FALSE` (D7's graded write-back: a new instrument may
fill a hole, not correct a record) and `requires_independent_agreement=FALSE`. That last one is the
deliberate relaxation and the C7 rule is why: agreement counts distinct **sources**, so a portal
re-mined from a second substrate cannot corroborate itself; every claim these rows govern is one
portal reading its own page, there is no second voice that could ever agree, and requiring one
makes the claims permanently unusable rather than safer. `jsonld_parse`, `map_widget_parse` and
`portal_declared_quality` get **no** rows on purpose — everything they emit is a pin, a shape, a
zoom or a blur hint, arbitrated by S4/S6 where policy rows are inert. `legacy_column` is the only
one of the four whose claims already exist, so the migration enqueues those listings into
`dirty_locations` with reason `'policy_version'` itself, scoped to the exact pairs it governs.
`tests/location_data/test_resolver_seed_policy.py::test_every_producer_a_shipped_contract_can_emit_has_a_v1_policy_row`
is the standing gate that makes the next activation red instead of silent.

**The un-shadow path, and it is per portal.** Nothing here promotes anything. The order is:
dispatch `location_claims_remine_archive.yml` for one portal → read
`scripts/location_w2_gate_report.py` for that portal (which needs its O8 frozen sample drawn
FIRST — that draw is a one-way door) → the operator runs
`python -m location_data.contracts --unshadow <portal>@<version>`, which writes the DB column, not
the YAML, and enqueues that contract's listings so the promotion actually re-resolves. Until then
the seven contracts' claims are stored and scored (`location_claims_shadow`) and excluded from
`location_claims_live`.

Two things to carry into the gate report rather than discover later: idnes' shipped exclusion
register already names markup the portal no longer emits (`zones_unmatched == ('.b-similar',
'.broker')`) and `claims_remine_archive` never reads `zones_unmatched`, so a corpus-wide register
miss is invisible and every batch still stamps `'ok'`; and `reject_points` is compared at 5 dp
(~1.1 m) with no measured false-positive rate, so a genuine address within ~1.1 m of a junk pin
loses its coordinate claim with no absence emitted.

## W4 — build started (targeted refetch cohorts)

Substrate-disjoint from W2 and W3 on purpose, which is why it could start while both were
still running: W2 mines archived HTML for the seven HTML portals, W3 mined
`listing_snapshots.raw_json`, and W4 re-fetches live pages for sreality + bezrealitky —
neither of which is in the archive substrate at all. W3 additionally *declines* to feed the
cohort (`route_legacy_shape_to_refetch=False`: a historical snapshot is an accurate fact
about the past, not a gap a live refetch could close), so what W4 finds in
`location_enrichment_state` is exactly what W1's live intake put there.

**Sequenced first, because it is a one-way door:** sreality's frozen labelled sample was
drawn **before** any refetch — sample 2, 120 members, seed `0.20260813`, drawn from 101,200
active rows, 0 labelled. Migration 399 snapshots the OLD system's serving values at draw
time precisely because the refetch that follows rewrites `listings.street`; drawing after
would have destroyed the "how good was the old pipeline" baseline permanently. bezrealitky's
120-row sample was already frozen in W1v. The draw is portal-uniform (`where source = %s and
is_active order by random()`), so it scores CONTRACT precision — W4's own gate is coverage
SQL and needs no labelling.

**#1083 — the consumer migration 384 designed and nobody built.** `location_enrichment_state`
had a producer since W1 (intake enrolls a sreality row whose payload is legacy-shape or 80 KB
truncated; 38,612 rows parked) and no consumer, while 384 shipped the scheduling columns AND
the partial index `les_due (lane, next_eligible_at) where not given_up` — keyed exactly the
way a work-claiming driver reads it. Two defects fell out of that gap, both closed by
`location_data/refetch_cohort.py`:

- **The cohort was a HIGH-WATER MARK.** Nothing DELETEs from the table and a task is emitted
  only while `sreality_payload_shape() != 'post_cutover'`, so a successful refetch merely
  stopped the producer and left the row stale forever. A "legacy share" computed from it can
  only grow — **W4 could not have passed its own `< 2 %` gate by succeeding.** `reconcile()`
  retires placed / delisted / exhausted rows and makes it current-state.
- **Every row was permanently DUE.** `_ENRICHMENT_WRITE_SQL`'s DO UPDATE is gated
  `WHERE input_hash IS DISTINCT FROM EXCLUDED.input_hash`, so re-seeing an unchanged legacy
  payload no-ops entirely and `next_eligible_at` stays frozen at its first-sight
  `now() + 6 hours`. A driver reading `les_due` would re-claim the whole cohort every pass.
  `mark_dispatched()` is the only thing that advances it.

Retirement is `next_eligible_at = NULL`, **never `given_up`** — 384 gives that column the
"stopped trying" meaning it carries in `listing_fetch_failures`, and a row that succeeded did
not give up. NULL also survives the producer: a payload that regresses to legacy shape changes
the hash, re-arms the schedule and pulls the row back in on its own. The shape test calls W1's
own `sreality_payload_shape` rather than a SQL mirror — the classifier returns `absent` from
TWO arms and tests post-cutover BEFORE legacy, and `jsonb_typeof(raw_json->'locality') <>
'object'` is NULL-blind on a missing key, i.e. blind to exactly the truncation cohort. Only
`raw_json->'locality'` is projected, never the whole payload.

**The dispatch lane** (this PR) is `workflow_dispatch`-only with `mode` defaulting to
`reconcile` — the phase that retires finished rows and enqueues nothing, which is the correct
first run against a cohort that has never been cleaned. `dispatch` mode enqueues the still-
pending rows through the ordinary `listing_detail_queue` at `priority = -1` (06 §6.4: "route
through the existing bounded drain rather than a bespoke crawler"), strictly behind real-time
discovery. It takes a `location_jobs` lease-row CAS (`location_refetch_cohort`) as a second
guard the GitHub group cannot provide, and it is deliberately **outside `location-batch`**:
that group serialises heavy corpus-wide DB sweeps and is measurably oversubscribed (#1084),
while this lane's scarce resource is portal egress, already governed by the drain it enqueues
into. No migration, and the lane can fetch nothing itself.

**Naming, for the next module:** the cohort constant is `COHORT_LANE`, not `LANE`. That name
is reserved for `location_claim_batches.lane` — the resume-cursor identity
`test_lane_identifiers.py` polices after the W3 erratum — and this module stamps no batch row.
The gate caught it; the fix was renaming, not inventing an extractor version for a lane that
versions nothing.

**Open, in order:**

- Dispatch `mode=reconcile` once. Until it runs, the 38,612 says nothing about work remaining.
- **The gate denominator, and it is NOT the cohort table.** W4's gate is "legacy-shape share of
  active rows < 2 %, from the W0-recorded baseline". The cohort spans ALL sreality rows
  (`_LISTINGS_FULL_SQL` has no `is_active` filter, deliberately) while W0's 8.4 % was
  active-only, and a listing delisted before the June-2026 cutover keeps its pre-cutover shape
  forever — so the two numbers are consistent, not contradictory. The gate wants a direct shape
  scan over active sreality rows, mirroring the classifier as one exhaustive `CASE` (post-cutover
  tested first, `IS DISTINCT FROM 'object'`, `ELSE 'absent'`) so the percentages self-check to 100.
  Sub-cohorts separate directly: `last_outcome='skipped'` is legacy-shape, `='error'` is truncated.
- W4(b) bezrealitky remainder (`ruianId` ≥ 95 % of active) and W4(c) truncated payloads.
- The P6 standing `payload_schema_detector` check, scoped to sreality + bezrealitky —
  `ceskereality.yaml` carries one too and belongs to W2's per-portal work.
- **R4 (Mapy purge) stays carved out and operator-gated.** It is the program's only destructive
  surface and the only part touching live serving; 06 §6.4's coexistence promise ("nothing in the
  ingest write path changes") covers W1–W3 and pointedly excludes W4.

## Standing decisions

- **The four heavy location lanes share ONE outer concurrency group, `location-batch`**
  (registry load, claim intake, Mapy inventory, resolve), each keeping its own group at
  the JOB level. Set after the 2026-08-10 incident: four lanes ran concurrently against
  the shared 75 GB production instance, dropped backends across the fleet (SSL EOF, one
  AdminShutdown), degraded the live Browse rebuild to multi-minute DataFileReads, and
  wedged two lanes with no error at all. A new heavy location lane joins the group.
  **The group is now demonstrably oversubscribed, measured 2026-08-17/18:** across the last 40
  `location_claims_intake.yml` runs, **23 cancelled / 16 success / 1 failure — 57.5 % cancelled**.
  The cancellations map onto backfill activity rather than any defect in intake: six consecutive
  intake cancellations 11:53→15:53Z sit exactly under the six payload-backfill runs
  12:30→16:22Z, intake was almost all green 06:02→10:59Z before the backfill began, and it
  cancelled again under each later backfill window. **It is LAG, NOT LOSS** — intake is
  incremental with a resume cursor, so a cancelled tick re-covers its ground on the next
  successful one; the claim layer falls behind, it does not go wrong. State it that way, because
  "57 % of runs cancelled" reads as an incident and the honest version is milder. **Open:**
  re-measure once the payload backfill and W3's history backfill both reach `reached_end`; if the
  rate does not return to the pre-backfill baseline, the group needs a real cadence fix (staggered
  crons, or intake yielding to a running backfill) rather than another note.
  **THE CASE IS STRUCTURAL, NOT "THE GROUP IS BUSY" — demonstrated in three directions in one
  evening (2026-08-17/18):**
  1. **intake displaced by backfills** — the 40-run measurement above;
  2. **a backfill displaced by intake** — a W3 history-backfill dispatch at 23:53Z lost the slot to
     the hourly intake cron by seconds and sat `pending`;
  3. **a backfill displaced by a resolve tick that had nothing to do** — `location_resolve.yml`'s
     `*/15` cron created run 32082491932 at **23:55:53Z**; the queued W3 run 32082367045 was
     cancelled at **23:55:54Z**. One second. Zero `REMINE` log lines, so it never started, wrote
     nothing, and left no stranded batch row.
  Instance 3 is the one that makes this structural rather than a capacity complaint: **a routine
  tick with no work to do destroyed a queued production run purely by arriving.** No lane is
  privileged, no discipline on either side would have prevented it, and no amount of politeness
  between operators fixes it — GitHub supersedes the older pending entry and that is the whole
  mechanism. A statistic argues the group is busy; this sequence argues its shape is wrong.
  **Told in wall-clock, which is the unit an operator feels:** at midnight, with two production
  backfills parked on clean resumable cursors and ~43k pages left to finish W2a, the group spent
  its time serving an 8-minute incremental intake and a resolve tick that displaced nothing.
  Neither backfill moved. **Measured, not rhetorical: that backfill window then waited 54 minutes
  for its slot** — driver armed 23:56Z, dispatched 00:50:37Z — behind the hourly intake burning
  its full 55-minute budget with the resolve tick queued behind it. One 45-minute unit of work
  cost nearly two hours wall-clock, and the delay was pure queueing rather than any lane doing
  more work.
  **Two operator options when this is fixed properly**, both cheap: stagger the crons so intake and
  resolve cannot collide with a long-running lane, or give a running backfill precedence — a
  `*/15` tick with an empty queue should yield to a 45-minute job, not cancel it.
  **The coordination discipline that made this survivable is worth carrying, because it is
  transferable and cost nothing:** two sessions sharing the group hand-negotiated slots all
  evening, and the rules that actually worked were (a) *intending to wait is not holding* — disarm
  the automation that dispatches on your behalf, since a dispatch loop will break your word without
  you noticing; (b) *looking is not holding either* — a point-in-time idle check followed by a
  dispatch has a race in the gap, so the robust form is dispatch-then-verify-`in_progress`-within-N
  -seconds, which detects the loss rather than pretending to prevent it; and (c) key a hold on
  OBSERVABLE STATE (watch the other run) rather than on a promise either party must remember.
  **And yielding the slot paid for itself, measurably**: two windows given up to the W3 lane bought
  a validated first production write path and a falsified prediction with the decay curve that
  explained it. Defending the queue position would have finished the backfill ~40 minutes sooner
  and produced no knowledge. Sequence a contended shared queue by which run is *riskier or more
  informative*, not by who is furthest along.
  **A FOURTH failure joined the family later that night, and it is the one that generalises
  furthest: *stating a caveat is not applying it*.** Both sessions wrote down that their scan's
  denominator was a point-in-time inventory rather than a finish line — and then quoted
  percentages against it for four hours. Measured proof: the payload backfill finished at **472,429
  pages against a "445,191-row" archive — 27,238 over, 6.1 %**, while W3 was quoting
  "8.3 % of 1,574,313" against a `listing_snapshots` count eight days stale. **Both source tables
  are live-written while the scan walks them**, so for any resumable scan here `reached_end=true` /
  `outcome='ok'` is the ONLY completion signal, remaining work is unknown without a live
  `count(*)`/`max(id)`, and a percentage-complete should not be quoted at all. The sharpest
  instance was subtler than the arithmetic: one session corrected its OUTBOUND reports and left its
  own task list carrying the stale figure — **knowing something and not propagating it everywhere
  it lives**, which is the version that actually bites, because the corrected copy makes you
  believe you have handled it.
  **All four are one failure — *something true was known and then not acted on*** — which is a more
  useful thing to look for than four rules to remember.
  **AND THE DEEPER FRAME, which arrived last and covers more than the rules do: A SNAPSHOT
  TREATED AS CURRENT STATE.** Five findings that night, reached independently by three sessions
  across three different substrates, are one bug wearing five costumes:
  1. the archive "inventory" of 445,191 quoted as a denominator while the table grew to 472,429;
  2. gate (a)'s verifier comparing archived bytes against a `portal_raw_pages` row the scrapers
     had since overwritten — which is why it FAILS on exactly the high-churn portals and passes
     on sreality, and why it is unsignable while any portal is being scraped;
  3. a session acting for 40 minutes on a peer's *declaration* that a dispatcher was armed,
     after that peer had retired it and told only the other session;
  4. the ~23 stranded `outcome='running'` batch rows, which a future health panel would read as
     "a lane is running right now";
  5. W3's `listing_snapshots` denominator, eight days stale and growing while its own scan walked it.
  **The operational form: a message and the world drift apart exactly like a stored payload and
  its live source. Never act on a stored copy — a count, a peer's last message, a status column,
  a cached body — as if it were the world. Re-read the world.** The three sessions' agreement on
  this is worth less than it looks (shared mental model, correlated error); what makes it solid is
  that each instance was found by a session other than the one that made it.
  **This paragraph exists on `main` for the reason it describes.** It was written first into one
  session's private memory and pairwise chat messages — i.e. into exactly the point-to-point
  substrate the rule above forbids — and would have died with that session. A peer noticed it was
  not in the repo and said so. Put the synthesis where every future reader looks, not where the
  authors can see it.
  **AND THIS IS THE ARGUMENT FOR CROSS-SESSION REVIEW, not merely for the rules.** Three of the
  four were caught by the OTHER session rather than the one that made the error, and the
  denominators are the decisive case: **neither session could have caught its own, because in both
  cases the error was invisible from inside the reasoning that produced it.** Each had written the
  caveat, believed it, and could re-read its own paragraph without seeing the contradiction sitting
  in the next sentence. A session reviewing its own work is the weakest link in the arrangement —
  not through carelessness, but structurally, because the blind spot and the reasoning share an
  author. Run concurrent sessions where they can see and challenge each other's *numbers*, not just
  hand off artefacts; the review is the deliverable as much as the work is.
  **A cancelled run leaves a `location_claim_batches` row at `outcome='running'` and nothing
  reaps it — this is inert, by construction, and must not be "fixed" casually.** Both consumers
  exclude it: `_WATERMARK_SQL` filters `outcome = 'ok'` and `_RESUME_SQL` filters
  `outcome IN ('ok','stopped','failed')`, so a stray can neither move the incremental floor nor
  be resumed from, and the intake workflow's own comment already states this is intended. It bites
  in exactly one place: **anything treating `outcome='running'` as "a lane is running right now"
  will report one phantom in-flight run per cancellation, forever** (~23 today, growing). Document
  that before building a health panel on the column; do not add a reaper while nothing reads it.
  Note this is a DIFFERENT shape from the payload-backfill finalize defect an adversarial review
  caught in #1059, where the stranded batch WAS read by consumers.
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

## W2-10 — the bazos free-text LLM lane (2026-09-05, shipped inert)

bazos' `raw_json` carries no description at all, so every street, část obce and house number
an ad states in prose is invisible to W1; the regex path is junk-prone (regression 220870847
mined `street='Nový'` out of "Nový 2 pokojový byt" and geocoded ~130 km off); and the pin
cannot stand in for the text (the pin-derived obec is measurably wrong). 29,546 active bazos
rows share only 90 distinct `locality` values. `location_data/claims_llm.py` is the follower
lane that answers that: it reads archived DETAIL bodies out of the content-addressed payload
store, scopes them through the DEPLOYED exclusion-zone register, and makes ONE forced-tool
call per listing.

**Ranking is an EXTRACTOR decision, not a resolution one.** The call asks for
`from_description` and `from_title` separately; the lane emits exactly ONE claim per field,
description-first, and consults the title only where the description is silent. That is the
operator's "free text beats headline" rule expressed where it is expressible:
`survivorship.matches` sees only `(source, extraction_method)` and `tie_breaker` is loaded
and never read, so two `llm_text` claims from one portal cannot be ranked as data at all.
The alternative considered and rejected was ranking the two blocks by `claim_confidence` —
that would have made `claim_confidence` mean "which node did this come from" rather than
"how sure is the extractor", and it hands a same-rank tie to `claim.id`.

**What the lane refuses, and records instead of dropping:**
- an unlocatable evidence quote — the hallucination guard, a skip PLUS an absence, never a
  raise (`assert_evidence_complete` would otherwise take a batch of paid work down on one
  bad response);
- a quote that exists only inside a stripped exclusion zone — recorded as
  `only_in_excluded_block`, which is how "the model invented it" stays distinguishable from
  "the model read the neighbour carousel";
- a name RÚIAN does not carry; a street the candidate obec does not carry (the
  phantom-street guard, a REGISTRY membership test rather than the morphology heuristic
  `bzs.det.street_text` declared and never implemented); a house number with no address
  point, and a house number with no candidate obec at all — doctrine #5's "no address point
  exists here" is an honest answer, never a nearest-neighbour snap. The street gate PASSES
  when the obec is unknown, mirroring S7 exactly, so the lane never drops a claim the
  resolver would have accepted.

**The one real divergence from the archived-HTML lane: the model is never called inside a
transaction.** That lane holds one `guarded()` block across its scan, its R2 GETs, its
extraction and its write; here a single call takes seconds, and holding a transaction across
a batch of them is idle-in-transaction on the transaction-mode pooler for the whole batch.
Each iteration is scan / body-load / (no transaction: scope, prompt, call, extract) / write.
An AST test asserts it structurally rather than by review.

**The three-model bake-off** (`scripts/location_llm_bakeoff.py`, dispatch-only, read-only,
`$5` PRE-FLIGHT cap) sends the IDENTICAL scoped text to `gpt-5-nano`, `gpt-5.6-luna` and
`qwen3.7-flash` over a deterministic md5-seeded sample and scores per-field yield,
gazetteer-resolution rate, evidence-quote validity (the exact production check — a bake-off
scored by a looser validator picks the model best at fooling it), latency, cost and the
pairwise agreement matrix. It writes no claims. Adjudication happens outside the repo.

**Recorded, unstarted, and each one blocks something:**
1. **the bazos@3 contract bump** (13 entries in two families, `bzs.desc.*` / `bzs.title.*`).
   Until it lands the lane is inert by construction. It costs a full bazos claim re-insert
   (`location_claim_fingerprint` hashes `extractor_version` AND `contract_entry_id`, and both
   move) — the second such re-insert this portal will have paid, so fold the pin fix into
   the same bump if the operator wants it.
2. **the `location_field_policy` rungs.** Seven of the ten extraction methods have no v1 row
   at all, and the generic `('llm_text','llm_text',900)` row sets
   `requires_independent_agreement=true` — which counts DISTINCT SOURCES, so a bazos-only
   LLM claim can never fill even a NULL. Without a per-portal row at `requires_independent_
   agreement=false` the lane writes claims that are live, auditable and permanently unusable.
   That relaxation is a real weakening of D7's graded write-back guard and belongs in a PR
   body, not buried in a migration.
3. **re-measure `ESTIMATED_USD_PER_CALL` from `llm_calls` after the first real pass.** The
   pre-flight cap is sized from planning figures today, and a cap sized from a guess is a cap
   in name only.
4. **the spend anti-join is asymmetric.** A payload the model extracted NOTHING from leaves
   no claim row and is re-called on a later `--mode full` pass. Bounded by the incremental
   watermark; the durable fix is a `location_llm_attempts` table keyed
   `(payload_sha256, model, prompt_version)`.
5. **all three prices are unverified against a live call.** Nothing in this repo has ever
   dispatched to DashScope. A missing or wrong `PRICES` row records `cost_usd=0.0` with only
   a log line, and every downstream spend signal then under-reports; the bake-off flags any
   model whose total cost is exactly zero.
6. **the fixture-diff golden will NOT cover this lane** — `score_archived` filters on
   `ARCHIVE_READERS` and this reader is in `LLM_READERS`. `tests/location_data/test_claims_llm.py`
   is the only coverage; do not read a green golden as coverage of the free-text lane.
