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
| W2a payload archive rewrite | append-on-change `portal_raw_payloads` | 🟡 in progress (2026-08-14) — instrument live, measured DETAIL profiles for 5 portals (`payload_norm@3`); profiles now keyed by (source, page_kind), INDEX surfaces on the generic base + a `+base` cohort and still unprofiled; both write flags OFF, awaiting the operator's churn + storage sign-off |
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
- **W2a-3b measured volatile profiles**: `scripts/location_payload_diff_probe.py` +
  evidence-derived profiles for idnes / ceskereality / realitymix, `payload_norm@2`. Table
  and residue below.
- **W2a-4 the backfill + round-trip verifier** (#1059): `location_data/payload_backfill.py`
  (keyset-resumable, 445,191 `portal_raw_pages` rows → `portal_raw_payloads`, never dispatched)
  + `scripts/location_payload_roundtrip_verify.py` (1,000-row byte-for-byte compare, 06 W2a
  gate (a)). Review found and fixed three real gaps before merge: `sample_ids()` silently
  under-sampled by up to 97 % on a source-scoped draw (now an exact uniform id-space sample
  with a loud shortfall report); the success-path finalize stamp ran unguarded and could strand
  a batch row at `'running'` forever; re-dispatching after a `NORMALIZER_VERSION` bump would
  have created permanent, un-prunable duplicate rows (now refuses without `--force`). Dispatch-
  only workflow, never triggered.
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
index page. Fixed before either write flag goes on, which is the cheap moment: nothing is
archived yet (`portal_raw_payloads` = 0 rows, the backfill has never run), so no content
address has to be rewritten.

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
`volatile_profile(source, page_kind)` — one function shared by the churn instrument, the
archive writer and the backfill. An unmeasured surface gets `BASE_PROFILE`: the shared
`_HTML_BASE` + `_HTML_ATTRS` and **nothing measured**. Why the base rather than no
stripping: on a measured surface over-stripping is self-correcting (the residue diff shows
it); on an unmeasured one it is not — a profile that eats the listing grid reports **0 %**,
which reads as the best possible result. The base carries only portal-agnostic, content-free
rules, and it is inert on JSON by construction (no pointers), so the sreality/bezrealitky
JSON surfaces do not move at all.

**`NORMALIZER_VERSION` stays `payload_norm@3`** — detail normalisation is byte-identical
across all 23 committed detail fixtures on 8 portals, pinned as digests computed under the
old code (`tests/location_data/test_payload_norm_by_page_kind.py`). Instead the cohort label
is resolved per surface: `normalizer_version_for(source, page_kind)` appends **`+base`**
where no profile was measured. A global bump would have discarded ~24,600 detail fetches
across 9 portals to fix an at-most-one-phantom-change artefact on ceskereality's 694 index
keys — the standing "STOP BUMPING" instruction, honoured with the per-surface answer it was
asking for. The suffix maintains itself: measure an index profile and that surface leaves
the `+base` cohort on its own.

**Still not done, deliberately:** no index-surface profile is written here. Diffing index
pages is its own finding. **And the same collapse is waiting one layer down:**
`persistence.volatile_paths` in the contract YAML is a single flat list on the contract
HEADER while `fetch:` beneath it is already a per-`page_kind` list — so W2a's next step,
which sources selectors from `portal_contract_entries.persistence.volatile_paths`, will
re-introduce exactly this defect unless that key gains the surface axis first.

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

**Not enabled, deliberately:** `payload_dual_write` and `payload_index_archive` are OFF, the
445k-row backfill has not run, the P4 pruner lane ships with `location_jobs.enabled=false`,
and no per-portal W2 contract exists yet. Those wait on the operator's O3/O4 sign-off of
`volatile_paths` + the storage projection.

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
