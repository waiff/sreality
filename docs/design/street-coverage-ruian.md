# Street coverage via RÚIAN address points (coords → street)

Status: **proposed / scoped, not built.** Durable answer for the residual
apartment street gap that text parsing cannot reach. Governing principle from the
operator: **only precise available data, no estimates — a wrong street is worse
than NULL.** This design is exact-match-only by construction.

## Why

After the text-based levers (sreality `locality.value`, iDNES/bazoš fixes) the
remaining no-street apartments fall into one bucket: **the source publishes no
street text, but the listing has a precise coordinate.** Examples:

- sreality detail-shape rows (`locality.street=""`, no `value`) — ~3,300 byt, clean
  per-listing coords.
- the long tail of remax/maxima/bezrealitky CZ rows.

There is **no way to recover these from text** — the street simply isn't written
anywhere. The only remaining signal is the coordinate. And today the DB has **no
local street source**: `admin_boundaries` stops at the cadastral-area / municipality
polygon (a whole village), which cannot resolve a coordinate to a street.

Filling these *correctly* needs a coordinate → street lookup. Two ways exist; only
one fits the "no estimates, no Mapy credit burn" constraints.

## Decision: ingest RÚIAN "Adresní místa" locally (not Mapy reverse-geocode)

| | RÚIAN address points (proposed) | Mapy.cz reverse-geocode (rejected) |
|---|---|---|
| Source | ČÚZK open data, ~3M CZ address points (street + house number + obec code + lat/lng) | `api.mapy.com` reverse endpoint |
| Cost | **Free, offline, zero per-call**, reusable by every future scrape | **New recurring per-call billed cost** |
| Risk | none beyond the distance guard | credit exhaustion — the platform already had a 250k-credit key suspension from uncached forward geocoding (`mapy-credit-driver-idnes-geocode`) |
| Fit with existing stack | PostGIS PIP/GiST machinery already in place (`admin_boundaries`, `ST_DWithin`, the BEFORE-trigger pattern) | `scraper/geocoding.py` is forward-only; would need a new path + a mandatory persistent cache |

Mapy reverse-geocode is the **fallback only** if the RÚIAN ingest is deferred.

## The no-estimates assignment policy (the heart of this design)

A nearest-neighbour street from an imprecise coordinate is an *estimate*. To stay
within "precise data only", a coordinate is assigned a street **only** when all hold:

1. **Precise-coordinate portal only.** Apply to listings whose coordinate is a real
   per-listing geocode: sreality detail, idnes (map config), remax (`data-gps`),
   bezrealitky (`gps`), maxima. **Exclude bazoš** — its coordinate is a single
   "show on map" pin of unknown precision (`raw_json.coords.source='link'`), so even
   a nearby address point is a guess. Bazoš stays text-only.
2. **Tight distance tolerance.** The nearest RÚIAN address point must be within a
   small radius (start at **≤ 25 m**, tune against ground truth; never loosen past
   the point where a neighbouring street could win). `ST_DWithin(geography, …, 25)`.
3. **Unambiguous.** If two address points of **different streets** both fall within
   the tolerance, skip — leave NULL. Only assign when the candidate street is
   unique within the radius.
4. **Sanity cross-check.** The matched address point's obec code must equal the
   listing's geo-derived `obec_id` (migration 140). A match in a different
   municipality means the coordinate or the point is wrong → skip.

Anything that fails → **leave the street NULL.** No "closest guess" is ever stored.

Bonus: RÚIAN points also carry the **house number**, so a confident match fills
`listings.house_number` too (the rule-B dedup auto-merge lever) for precise-coord
portals — again only under the same exact-match guard.

## Data model

New table (new numbered migration):

```sql
create table address_points (
  id            bigint primary key,          -- RÚIAN "Kód ADM"
  street        text not null,
  house_number  text,
  obec_id       integer,                     -- = admin_boundaries.id (obec), the mig-140 join key
  geom          geography(point, 4326) not null
);
create index address_points_geom_gix on address_points using gist (geom);
create index address_points_obec_idx on address_points (obec_id);
```

~3M rows; the GiST index makes nearest-point KNN (`geom <-> subject` + `ST_DWithin`)
fast. The table is a **mirror of an external open dataset** (like `amenities` /
`admin_boundaries`), refreshed wholesale, not history-tracked.

## Components

1. **Ingest script** (`scripts/ingest_address_points.py` + a dispatch/scheduled
   workflow): download the ČÚZK RÚIAN "Adresní místa" export (CSV/XML, per-obec or
   the national dump), parse with stdlib, `COPY` into `address_points`, refresh in
   one transaction. No new dependency (stdlib `csv`/`zipfile`/`xml.etree`, same
   posture as `fetch_rent_map`). Refresh cadence: ČÚZK updates regularly — monthly
   or quarterly is ample (streets are stable).
2. **Assignment query** (a toolkit/SQL function, read-only): for a coordinate, return
   the unique address point within tolerance or NULL, applying the four guards above.
3. **Backfill** (`scripts/backfill_address_point_streets.py` or extend
   `backfill_portal_streets.py` with a coords source): for precise-coord, no-street,
   CZ rows, set `street` (+ `house_number`) from the guarded match. Snapshot-safe
   (street/house_number are out of the content hash). Idempotent + resumable.
4. **Forward (optional):** the scrape write path could call the same assignment when a
   portal yields a precise coord but no street — but the periodic backfill alone keeps
   coverage current with far less hot-path complexity. Prefer backfill-only first.

## Expected outcome

Filling every CZ-resolvable, precise-coord, unambiguous miss:

| portal | after text levers | + RÚIAN exact-match | note |
|---|--:|--:|---|
| sreality | ~87.6% | **~99%** | clean detail coords — the prime target |
| remax | 82.7% | ~98% | |
| maxima | 82.8% | ~95% | |
| bezrealitky | 97.0% | ~99% | tiny residual |
| idnes | ~62% (CZ ~87%) | CZ ~95% | foreign apartments stay correctly NULL |
| bazoš | ~55% | **~55% (unchanged)** | excluded — imprecise pin |

Overall apartment coverage ≈ **88–90%**, the realistic ceiling. The unrecoverable
remainder is genuinely-foreign listings and coordinates that fail the distance /
ambiguity guard — left NULL **by policy, not omission**.

## Effort & sequencing

High one-time effort (dataset ingest + spatial table + guarded assignment), low
ongoing (a scheduled refresh). Sequence after the free text levers ship and their
backfill settles; target sreality detail-shape first (cleanest coords), validate the
distance tolerance against a labelled sample before a wide backfill.
