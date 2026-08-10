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
| W1 registry + claim spine (shadow) | full RÚIAN mirror, claims, resolutions, projection | ⚪ not started (gated on W0 + sizing pilot + A4) |
| W1v bezrealitky vertical slice | one portal end-to-end + location-quality dashboard | ⚪ not started |
| W2a payload archive rewrite | append-on-change `portal_raw_payloads` | ⚪ not started (byte-churn measurement is the gate) |
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

## Standing decisions

- The Mapy remediation ladder (R2 inventory → R3 re-resolve → R4 purge) is W1–W4
  work; the R2 evidence inventory is a W1 INPUT (first claim must not be written
  before it exists).
- W0 discipline held: no new precision columns on `listings`; precision signals ride
  in raw_json / archived pages until the claims spine exists.

## W1 PR-E — the resolver, the projections and the collision epoch (shipped)

`location_data/resolver/` — S1–S9 as a **pure function** plus the three jobs that feed it.

- **S1–S7 pure core** (`normalize`/`country`/`candidates`/`position`/`admin`/`precision`/
  `survivorship`, orchestrated by `core.resolve`): no wall clock (`as_of = max(observed_at)`),
  no network, no randomness — enforced by an AST scan, not by review. The candidate ladder
  R0–R8 runs against a `RegistryView` PROTOCOL, so the whole core is runnable with no
  database and the byte-identical replay gate is a normal pytest test.
- **Five version inputs in the identity**, `collision_epoch_id` included, with a canonical
  serialization + content hash stamped on the row.
- **S8 builders** for both projections, with the canonical class-aware `geo_blockable` /
  `renderable_as_point` and a parity test against migration 384's IMMUTABLE SQL functions;
  property grain is a reconciliation over children with mandatory disagreement columns —
  a precise child can never lose to a centroid child.
- **Collision-epoch producer** (`collision` + `epoch_job`): rounded 4-dp cells with the
  mandatory 3×3 expansion (h3-pg unavailable), the six-value classification, and
  bucket-change-only re-enqueue.
- **`dirty_locations` drain** (`drain`): `FOR UPDATE SKIP LOCKED` slices, one transaction
  per batch with a per-listing savepoint, lease-row CAS on `location_jobs`, judged by
  oldest-row age. `.github/workflows/location_resolve.yml` (drain | epoch | full-resolve,
  `*/15` cron, concurrency group `location-resolve`).
- **S9 reconciler v1**: the cheap structural rules of 03 §3.11.1 into the append-only
  ledger, keyed on the version-free `dedupe_key`; auto-close only when the predicate stops
  firing AND the inputs changed.

Open against the schema: `ruian_admin_unit_geometries.purpose` does not admit `'pip'`
(04 C4.3 wants the subdivided geometries; the resolver prefers `'pip'` and degrades to
`'authoritative'`); `listing_location_current` carries neither `position_quality_class` nor
`collision_epoch_id` (03 §3.10 change requests), so the builder computes both and the writer
drops them; `location_uncertainty_policy` has no seed row for a pin capped to an admin rung,
which the lookup resolves to the unit's own area bound.
