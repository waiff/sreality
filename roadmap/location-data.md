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

## W1 — PR-D: portal contracts as data + the claims-intake extractor

Shipped (shadow-only; nothing reads these tables yet):

- `contracts/portals/<portal>.yaml` × 9 — the declarative contracts of 02 §2.1/§2.2, with
  the permanent extractor-id prefixes (`sr. bzr. bzs. id. mm. rx. cr. rm. mx.`), the §2.4
  caps/priors and the §2.5 exclusion-zone register. The full contract is declared,
  including the W2 html/map/slug surfaces; only entries naming a `locator.reader` are
  executable by W1.
- `location_data/contracts.py` — YAML → `portal_contracts` + `portal_contract_entries`
  deploy-time projection, idempotent per `contract_version`, refusing a changed body under
  a loaded version, plus the §2.1.8 retraction path. Git stays the store of record.
- `location_data/claims_intake.py` — the batched extractor over `listings.raw_json` for all
  nine sources: keyset pagination, 10–30k batches, watermark-incremental and re-runnable
  full mode, one `location_claim_batches` row per run, `dirty_locations` enqueued inside
  the claim-insert statement.
- `.github/workflows/location_claims_intake.yml` — dispatch + hourly incremental cron on
  its own `location-claims` concurrency group; every run re-projects the contracts first.

Decisions worth carrying forward:

- **The licence ladder is stronger than `carry_forward`.** W1's blocking gate is
  `claims JOIN <R2 inventory> WHERE claim_type='coordinate'` = 0, so presence in
  `mapy_affected` vetoes a coordinate on **every** substrate, including the three portals
  whose pin is first-party payload. The lane refuses to start if that inventory is missing
  or empty.
- **`claim_fingerprint` is computed in SQL**, from the same `location_value_norm()` the
  column uses. PostgreSQL's `unaccent` is a dictionary (ß→ss, ø→o, đ→d …) and a Python NFKD
  mirror drifts on exactly the foreign-address cohort — drift there means the unique index
  stops deduping, silently, in an append-only table. A diagnostic mirror + a parity battery
  keep the divergence documented.
- **W1 runs no evidence-bearing method.** `regex_text` / `llm_text` entries are declared but
  unexecuted until W2a's content-addressed payload store makes a span re-verifiable.
- Withheld coordinates and unreadable payloads are recorded, never silent: a class-E row
  gets a `location_claim_absences` row, and sreality's legacy-shape / 80 KB-truncated rows
  are routed to `location_enrichment_state(lane='sreality_detail_refetch')`.

Open: promoting the fingerprint expression to a named SQL function (it belongs beside
`location_value_norm` in 382); adding `location_data` to `tests/sql_corpus.RUNTIME_DIRS`
once the schema migrations are on `main`; the per-portal frozen labelled samples of
§6.4.0, which gate whether each contract resolves or stays in shadow.
