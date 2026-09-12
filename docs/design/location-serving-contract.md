# Location serving contract — how a consumer reads the location engine

**Status 2026-09-12 (W2-b).** The location-data program's engine is **claims → four-step resolver →
one answer table**. `listing_location` (migration 501, 26 columns) is the whole serving surface;
W2-b dropped `listing_location_current`, `property_location_current` and every resolver-side
relation the deleted engines wrote. This page is the contract a consumer codes against, written
first for the NEW DEDUP program (rule 15), which is the first consumer after the admin dashboard.

The one-sentence rule (CLAUDE.md rule 24): **location-reading code reads `listing_location`, never
`listings.geom` or the geo-derived columns.** The legacy columns stay populated and stay the live
path for every feature that has not flipped — they are not wrong, they are *unqualified*: a
coordinate with no statement of how precisely or how trustworthily it is known.

## 1. What to read

ONE table, listing grain, rebuilt by the resolver (`location_resolve.yml` + the Railway worker's
`location_resolve` lane, from `dirty_locations`); nothing else is the serving surface.

| grain | table | key | notes |
| --- | --- | --- | --- |
| listing | `listing_location` | `listing_id` (PK) | DDL migration 501; indexes `(obec_kod, granularity)` and GiST `(geom)` |

**There is no property-grain table.** `property_location_current` was a verbatim copy of its
winner's row — migration 493 measured `p.kraj_kod` and `w.kraj_kod` agreeing on 0 of 637,381 rows —
and nothing outside one pg_cron statement read it. A property's location is its members' rows
aggregated at read: join `listings.property_id` and pick or summarise.

A listing with no row has never been resolved (brand new, between drains). Rule 25 makes that a
vanishing case: a listing with no live claim gets an `undetermined` row (granularity `unknown`, no
position) rather than no row, so `count(listing_location) = count(active listings)` holds by
construction. Treat "no row" as *unknown*, never as "no location".

## 2. The 26 columns

**Where.** `geom` (`geometry(Point,4326)`, NULL when nothing resolvable) and
`uncertainty_radius_m`. One point, one radius: the radius is what says how much to trust the point,
and there is no second position column.

**How well it is known — three axes, all NOT NULL.** `granularity` (enum `location_granularity`:
`unknown` < `country` < `kraj` < `okres` < `obec` < `cast_obce_or_quarter` < `street` <
`street_segment` < `parcel` < `building` < `address_point`), `match_confidence` (`low` < `medium` <
`high` < `exact`, from how many INDEPENDENT fields agreed with the bound entity), and the radius
above. **Compare granularity by rank, never by string or enum order** — `location_granularity_rank`
in SQL, `location_data.resolver.types.GranularityRank` in Python. All three are NOT NULL because a
NULL reads as "no gate" and fails open: a NULL radius makes both branches of the three-valued
containment test evaluate NULL and the row drops out of `certain` AND `possible`.

**Registry identity (RÚIAN codes — the official Czech address registry, ČÚZK).** `ruian_adm_kod`
(address point), `ulice_kod` (street), `obec_kod`, `cast_obce_kod`, `okres_kod`, `kraj_kod`. NULL
means "not bound to that level". The building (`stavebni_objekt_kod`) and parcel (`parcela_id`)
keys are **gone**: the building code was never loaded and the parcel rung was unreachable, so W2-a
deleted the rung and W2-b the columns.

**Names, for display only.** `country_code`, `kraj_name`, `okres_name`, `obec_name`,
`cast_obce_name`, `street_name`, `house_number_cp`, `house_number_co`, `psc`. Administrative names
are ALWAYS the RÚIAN chain's own spelling, never a portal's. Never match on these — match on the
codes. There is **no stored display label, `place_search_text`, `admin_path` or blocking key**: a
stored derivation is a second definition, so a reader composes what it needs from the parts.

**Country and self-disagreement.** `country_status` (NOT NULL: `cz` | `foreign` | `disputed` |
`undetermined`) — foreign is a DETERMINATION the resolver makes, never a default for "no town
found". `disputed` is ONE nullable text column whose VALUE is the reason (`pin_outside_obec`,
`pin_outside_cz`, `country_conflict`), NULL when clean — never a boolean plus a reason pair, which
can disagree with itself.

**Housekeeping.** `resolver_version`, `registry_version`, `claim_set_hash`, `resolved_at`. The
first two are what the sweep compares to decide a row is stale; `claim_set_hash` is what says the
inputs themselves moved. A row REPLAYS byte-identically from those three ids, which is why no
resolution trace is stored.

**Not on the table, and where each went.** `source` — join `listings`, one primary-key hop away.
`pin_shared_by_n` and the whole pin-collision block — the epoch that produced them is deleted; the
shared-pin count is a read-time aggregate (`count(*) over (partition by geom)`) and W3 computes it
in the `browse_list` rebuild, where the map needs it. `position_licence_class` — the licence rail
moved UPSTREAM: the resolver's claim projection admits only `licence_class IN ('portal','operator')`,
so a Mapy-class coordinate is never READ, and a partial index on `location_claims` keeps the
remediation set one indexed predicate away. `position_source`, `blur_evidence`,
`radius_semantics`, `admin_assignment_method`, the four `*_unit_id` surrogates and momc/ku/pou/orp
— producers deleted, or provably NULL on every row.

## 3. The floors — what each feature may consume

Declared in `location_data/serving_contracts.py` (`FEATURE_FLOORS`, design 05 §5.5.2) and checked
with `meets_floor(feature, granularity=…, match_confidence=…)`. An undeclared feature raises —
never a permissive default. The dedup rows:

| feature key | min granularity | min confidence | extra gate (the consumer's job) |
| --- | --- | --- | --- |
| `dedup_rung_0a` | `address_point` | `high` | `ruian_adm_kod` present |
| `dedup_tier_1` | `street` | `medium` | ≥ 1 side has a portal-claimed house number |
| `dedup_tier_2` | `street_segment` | `medium` | `geom` present, radius inside the rung's tolerance |
| `dedup_path_c` | `obec` | any | `obec_kod` present (added 2026-09-10 on the dedup operator's path C ruling — not one of 05 §5.5.2's original rows) |

`dedup_rung_0b` (building) and `dedup_rung_0c` (parcel) went with their key columns in W2-b: a
floor whose gate column does not exist is a floor nothing can evaluate.

The rung the operator raised on 2026-09-08 was ruled on 2026-09-10 as **path C** (NEW DEDUP ledger,
that date): town = `obec_kod`, no radius, attributes (disposition, then area) do the rest. Its
floor is the `dedup_path_c` row — `obec` at any confidence. Refined the same day: in Praha, Brno
and Ostrava the town is additionally split by **`cast_obce_kod`**, chosen over `momc_kod` on
measured coverage (85 % vs 24 % of Praha listings, because the quarter is often *claimed* in portal
text while the administrative district needs a resolved address). That split does not raise the
floor: a row with no `cast_obce_kod` still qualifies and matches against its whole town.

## 4. No flag

W2-b deleted `location_data/serving_flags.py`. The `location_v2.<feature>` `app_settings` keys were
never seeded and no consumer ever read one; the module documented a per-feature switch that the
program now makes by cutting a reader over in a PR, which is reversible the same way every other
deploy is. Flipping a consumer is a code change gated by its own program (rule 15 for dedup), not
a runtime flag.

## 5. What NOT to read (the legacy path)

These are the columns the un-flipped features still serve from, and they will be retired only after
their consumers flip (W4). New location-reading code must not touch:

- `listings.geom`, and anything derived from it in the same table: `obec_id`, `okres_id`,
  `region_id`, `ku_id`, `locality_district_id`, `locality_region_id`, `obec`, `okres`, `region`
  (the BEFORE trigger `listings_set_admin_geo`, migration 289, writes these on every ingest);
- `listings.street`, `listings.house_number`, `listings.street_name_key` (migration 256) —
  `scraper/street.py`'s output; `listing_location.street_name` / `ulice_kod` replace them, and the
  answer table's street is the RÚIAN canonical form;
- `geocode_cache` and any Mapy-derived coordinate (licence class E; the R4 purge nulls them);
- `browse_list` / `browse_projection` geo columns, and `properties.*` geography;
- anything from the removed dedup engine (rule 15).

If a new answer and a legacy answer disagree, **the answer table is the one with a stated
precision**. Do not "correct" a `listing_location` row from `listings.geom`; open it as a
location-program finding.

## 6. Coverage and freshness, dated (re-check before trusting a number)

- **Un-shadow 2026-09-09 17:54–17:59Z:** every portal's stored claims are resolver inputs, and
  W1-b deleted the shadow mechanism whole.
- **Rule 25's invariant is the coverage number**: `count(listing_location) = count(active
  listings)`, and every active Czech listing has a town. `verify_pipeline`'s
  `location_town_coverage` check measures both and is red until the second is zero.
- **Resolver lag.** The drain works `dirty_locations` from the oldest row. Judge freshness by
  `resolved_at` and by the oldest `dirty_locations.enqueued_at`, not by queue length. The Location
  Quality page's "on an older registry" share is the same signal per portal.
- **Registry-bound share** (a listing matched to a RÚIAN address point): sreality 26.9 %,
  bezrealitky 60.7 % street+house-number; bazos 0 %, idnes 1.4 % (design `db-coverage-stats.md`
  §3). Tier 0 is only as common as that.

## 7. One query, as the dedup design asked for

`PROGRAM.md` (2026-09-08 (b)) records: "W2 should read location through ONE query it can later
point at the location projection." That query is:

```sql
select l.listing_id, l.geom, l.granularity, l.match_confidence, l.uncertainty_radius_m,
       l.ruian_adm_kod, l.ulice_kod, l.obec_kod, l.cast_obce_kod, l.okres_kod, l.kraj_kod,
       l.country_code, l.country_status, l.disputed,
       l.street_name, l.house_number_cp, l.house_number_co, l.psc,
       l.obec_name, l.cast_obce_name, l.okres_name, l.kraj_name,
       l.resolver_version, l.registry_version, l.resolved_at
  from listing_location l
 where l.listing_id = any(%(listing_ids)s::bigint[]);
```

Everything a candidate path needs is in that row. Joining `listings` for `source`, `disposition`,
`floor`, `usable_area`, `price_czk` is fine — those are not location.
