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
| W1 registry + claim spine (shadow) | full RÚIAN mirror, claims, resolutions, projection | 🟡 in progress — schema landed, loaders/writers next (apply still gated on the sizing pilot + A4) |
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

## W1 — in progress

- **PR-A the W1 schema** (migrations 380–384): every location enum + config seed
  (380), the RÚIAN mirror (381), the claim spine + portal contracts (382),
  resolutions + policy (383), the two serving projections + collision/ledger/ops
  tables (384). Purely additive, backend-only (RLS on + explicit anon/authenticated
  REVOKEs on every table, sequence and function), nothing reads or writes them yet.
  `tests/location_data/test_location_schema_contracts.py` is the offline gate for
  01 §A.2 (enum vocabularies, the forbidden `pin_collision_class IS NULL` form, no
  ordinal enum comparison in a CHECK/index predicate/generated column, NOT NULL
  axes on both projections, the three-artifact licence guard, `collision_epoch_id`
  in the resolution identity, dispositions keyed on `dedupe_key`).
- h3-pg is NOT available on this instance: the stated fallback ships — the rounded
  4-dp `location_geo_cell_key` with a MANDATORY 3×3 neighbourhood expansion at
  query time. `h3_r10` stays a nullable additive upgrade slot.
- **NOT YET APPLIED to production.** The migrations ship as files first; applying
  them ahead of the merge is what produced the earlier prod/git drift. Apply after
  merge, then verify.
- Uncalibrated by design and flagged in the seeds themselves: `threshold_n`
  (01 OQ3) and every R95 radius (01 OQ4, seeded as `geometric_bound`/`declared`,
  never `r95_empirical`). Calibration writes a new `policy_version`.

## Standing decisions

- The Mapy remediation ladder (R2 inventory → R3 re-resolve → R4 purge) is W1–W4
  work; the R2 evidence inventory is a W1 INPUT (first claim must not be written
  before it exists).
- W0 discipline held: no new precision columns on `listings`; precision signals ride
  in raw_json / archived pages until the claims spine exists.
