# Location resolution — one layer for address↔coordinates enrichment

Status: **shipped** (wave 1 — this doc's scope). Follow-ups tracked in the
dedup-blind-spots program: RÚIAN resolver portal extension, PIP boundary-sliver
fix, sreality/bezrealitky raw_json street extraction, byt geo rung B.

## Why

The 2026-07 dedup-blind-spots audit (159k listings structurally invisible to
dedup) traced a large share of the gap to location enrichment being per-portal
patchwork:

- **address→coords** existed only for bazos (rich resolution tree), idnes and
  realitymix (drain-time fallback that fires only INSIDE a detail refetch — the
  standing no-geom stock was never re-attempted: 842/842 idnes and 1,245/1,245
  realitymix no-geom rows showed zero attempts). ceskereality had geocoder
  plumbing in its parser that no caller ever wired; maxima/remax/mmreality had
  nothing.
- **No attempt ledger** for forward geocoding: failures were silent, so
  "attempted but failed" was indistinguishable from "never tried", and any
  backfill re-risked the 2026-06 250k-credit Mapy incident.
- **geom wipe hazard**: both ingest upserts wrote `geom = EXCLUDED.geom`, so a
  refetch whose page carried no coords silently NULLed geocoded/backfilled
  coordinates (only idnes/realitymix were protected, by carry-forward preloads).
  Wiped rows kept fossil hierarchy text (the admin trigger never clears on NULL
  geom) and dropped out of geo-keyed dedup.

## The layer

`scraper/location.py` is now the single home for the address→coords half.
(coords→hierarchy stays the in-DB admin-geo trigger, migrations 140/162/222;
coords→street stays the RÚIAN resolver, scripts/backfill_address_point_streets.)

- **`CoordResolver`** — the drain-path seam every portal now uses
  (idnes, realitymix, maxima, remax, mmreality, ceskereality):
  `page coords win → carry stored geom forward → geocode locality`, with the
  region/country skip, the CZ-bbox guard, and stable raw provenance stamps
  (`coords.source = 'carry_forward' | 'geocode'`) uniform across portals.
  bazos keeps its own richer resolution tree (maps-link corroboration) but
  shares `build_geocoder`/`CachingGeocoder` from this module.
- **`geocode_cache`** (migration 288) — persistent, query-keyed cache with
  negative caching (30-day TTL) for single-threaded backfill callers
  (`geocode_cached`). Drains use the in-run memo only: carry-forward already
  caps them at one geocode per listing ever.
- **`listings.geocode_attempted_at`** (migration 288) — the row-grain attempt
  ledger, mirroring `coord_street_attempt_version` in timestamp form. A COLUMN,
  not a raw_json marker: the mig-263 lesson — a refetch rebuilds raw_json and
  destroys markers, but never touches columns outside LISTING_COLUMNS.
- **`geom = COALESCE(EXCLUDED.geom, listings.geom)`** on both ingest upserts —
  the mig-263 preserve-if-null rail extended to coordinates. Incoming NULL means
  "page carried no coords", never "coords removed"; a real move (non-NULL) wins.
- **`scripts/backfill_geocode_coords.py`** (+ `backfill_geocode.yml`) — works
  the standing no-geom stock of any portal from STORED locality text, stamping
  the ledger on every processed row. Supersedes the completed one-off
  `backfill_realitymix_coords` (deleted). Active rows only, by design — flip if
  the inactive-geo dedup pass (P2) ever lands.

## Text→hierarchy (the operator's case C)

Deliberately NOT built as a text-matching mechanism: Czech obec names are
heavily ambiguous (the same-name-obce problem price-stats already hit). The
robust route is **C = B then A** — geocode the text to a coordinate, and let the
existing admin-geo trigger derive obec/okres/kraj/ku from it. One code path, no
second name-resolution system.

## Constraints honored

- Geocoding never fails a fetch (all errors swallowed at the resolver seam).
- No snapshot churn from backfills: geom is out of the content hash; the
  backfill writes geom + the ledger stamp only, via raw SQL (never
  upsert_listing).
- One Mapy credit per listing (drains, via carry-forward) / per distinct query
  (backfills, via geocode_cache). Key failover unchanged (MAPY2 on 401/403/429).
- Ops note: the Railway realtime worker needs MAPY_CZ_API_KEY in its env for the
  worker-lane drains to geocode (Railway env is managed manually).
