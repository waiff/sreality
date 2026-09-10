# Location serving contract — how a consumer reads the new location engine

**Status 2026-09-10.** The location-data program's engine (claims → resolution → projection, design
`~/location-data-architecture-2026-08-10/design/final/`, tracked in `roadmap/location-data.md`) is
live on production data and, since 2026-09-09, admits every portal's claims (all seven W2 contracts
un-shadowed). It is **readable now**. No user-facing feature reads it yet — that cutover is W6, per
feature, behind `location_v2.<feature>` runtime flags. This page is the contract a consumer codes
against, written first for the NEW DEDUP program (rule 15), which is the first consumer after the
admin dashboard.

The one-sentence rule (CLAUDE.md rule 24): **new location-reading code reads the projection, never
`listings.geom` or the geo-derived columns.** The legacy columns stay populated and stay the live
path for every feature that has not flipped — they are not wrong, they are *unqualified*: a
coordinate with no statement of how precisely or how trustworthily it is known.

## 1. What to read

Two tables, one row per grain, rebuilt by the resolver (`location_resolve.yml`, every 15 min, from
`dirty_locations`); nothing else is the serving surface.

| grain | table | key | notes |
| --- | --- | --- | --- |
| listing | `listing_location_current` | `listing_id` (PK); `property_id` carried | DDL migration 384, +388 (`position_quality_class`, `collision_epoch_id`), index 429 `(property_id, listing_id)` |
| property | `property_location_current` | `property_id` (PK) | highest-precision member wins (`winner_listing_id`, `winner_rule`); carries `member_count`, `member_spread_m`, `distinct_street_names`, `distinct_obec_kods`, `disagreement_flags` |

A listing with no row has never been resolved (new since the last drain, or a source with no
claims yet). Treat "no row" as *unknown*, never as "no location".

## 2. The columns a consumer needs (listing grain)

**Where.** `geom` (`geometry(Point,4326)`, NULL when nothing resolvable), `uncertainty_radius_m` +
`radius_semantics`, `render_as` (`point` | `circle` | `area`), `renderable_as_point`,
`is_low_precision`, `location_disputed`.

**How well it is known — the four D3 axes, all NOT NULL.** `granularity` (enum
`location_granularity`: `unknown` < `country` < `kraj` < `okres` < `obec` < `cast_obce_or_quarter`
< `street` < `street_segment` < `parcel` < `building` < `address_point`), `position_source`,
`match_confidence` (`low` < `medium` < `high` < `exact`), `blur_evidence`. **Compare granularity
by rank, never by string or enum order** — `location_granularity_rank` in SQL,
`location_data.resolver.types.GranularityRank` in Python.

**Registry identity (RÚIAN codes — the official Czech address registry, ČÚZK).** `ruian_adm_kod`
(address point), `stavebni_objekt_kod` (building), `parcela_id` (parcel), `ulice_kod` (street),
`obec_kod`, `cast_obce_kod`, `momc_kod`, `ku_kod`, `okres_kod`, `kraj_kod`; `admin_path` (ltree)
and `admin_assignment_method` say *how* the admin unit was assigned (point-in-polygon vs claimed
vs centroid).

**Names, for display only.** `display_label`, `display_path`, `street_name`, `house_number_cp`,
`house_number_co`, `evidencni`, `psc`, `obec_name`, `okres_name`, `kraj_name`, `cast_obce_name`.
Never match on these — match on the codes.

**Country.** `country_code`, `country_status`, `country_confidence`, `is_cz`. The default
foreign-listing predicate (05 §5.6.2) is `country_code = 'CZ' OR country_code IS NULL OR
COALESCE(country_confidence,'low') < 'high'` — the `COALESCE` is load-bearing.

**Blocking keys, precomputed (05 §5.4 / 01 §7.1).**

| key | built from | NULL when | tier |
| --- | --- | --- | --- |
| `addr_block_key` | `'a:' || ruian_adm_kod` | no registry address point | 0a |
| `building_block_key` | `'b:' || stavebni_objekt_kod` | no registry building | 0b |
| `street_block_key` | `obec_kod || ':' || lower(unaccent(street_name)) || ':' || coalesce(house_number_cp,'')` | no obec or no street | 1 |
| `geo_cell_key` | `'c:' || round(lat,4) || ':' || round(lon,4)` | **written only when `geo_blockable`** | 2 |
| `h3_r10` | — | always (h3-pg unavailable; the 4-dp cell is the shipped fallback) | — |

`geo_cell_key` equality must be expanded to the **3×3 neighbourhood** at query time (a 4-dp cell is
~11 m × ~7 m; a pair straddling a cell edge is otherwise missed). `geo_blockable` is true only at
granularity ≥ `street_segment` with an admissible `position_source` and collision OK — a listing
resolved to a town centroid is *not* geo-blockable, by construction.

**Collision evidence.** `pin_shared_by_n`, `pin_shared_by_n_25m`, `pin_shared_by_n_100m`,
`pin_cluster_id`, `pin_collision_class` (`normal` | `legitimate_multiunit` | `building_1_to_many` |
`town_centroid_suspect` | `parser_collapse_suspect` | `foreign_resort_centroid`),
`cluster_heterogeneity_ok`. The two regression classes the design names: `legitimate_multiunit`
(one building, many real units — a pin shared by 40 listings can be right) and
`town_centroid_suspect` (bazos runs 5.56 listings per point with 51.5 % in clusters ≥ 20).

## 3. The floors — what each feature may consume

Declared in `location_data/serving_contracts.py` (`FEATURE_FLOORS`, design 05 §5.5.2) and
checked with `meets_floor(feature, granularity=…, match_confidence=…)`. An undeclared feature
raises — never a permissive default. The dedup rows:

| feature key | min granularity | min confidence | extra gate (the consumer's job) |
| --- | --- | --- | --- |
| `dedup_rung_0a` | `address_point` | `high` | `addr_block_key` present |
| `dedup_rung_0b` | `building` | `high` | `building_block_key` present **+ a published per-source coverage denominator** (05 §5.4.3) |
| `dedup_rung_0c` | `parcel` | `high` | `parcela_id` present; `pozemek` / auction / cadastral |
| `dedup_tier_1` | `street` | `medium` | ≥ 1 side has a portal-claimed house number |
| `dedup_tier_2` | `street_segment` | `medium` | `geo_blockable` |
| `dedup_path_c` | `obec` | any | `obec_kod` present (added 2026-09-10 on the dedup operator's path C ruling — not one of 05 §5.5.2's original rows) |

Two consequences the dedup design (`docs/design/new-dedup/PROGRAM.md`) must reckon with: the L0
"geo 75 m" rung is Tier 2 and therefore applies only to `geo_blockable` rows — a 75 m circle
around a town-centroid pin is the false-merge factory the classes above exist to catch; and the
"same town only" rung the operator raised on 2026-09-08 was ruled on 2026-09-10 as **path C**
(NEW DEDUP ledger, that date): town = `obec_kod`, no radius, attributes (disposition, then area)
do the rest. Its floor is the `dedup_path_c` row above — `obec` at any confidence, keyed on
`obec_kod` alone; `admin_assignment_method` is carried into the candidate audit as a breakdown,
not used as a gate.

## 4. The flag

`location_v2.dedup` (`location_data/serving_flags.py`, `app_settings` key, missing = OFF). It gates
the **production** candidate path, not simulation: a simulation that reads the projection to score
candidates needs no flag, because it writes nothing a user sees. The dedup program owns this flag;
flipping it is that program's gate (rule 15), sequenced after the 7-day shadow compare the design
requires per feature (06 §6.x).

## 5. What NOT to read (the legacy path)

These are the columns the un-flipped features still serve from, and they will be retired only after
their consumers flip (class C/D retirement, 06 §6.x). New location-reading code must not touch:

- `listings.geom`, and anything derived from it in the same table: `obec_id`, `okres_id`,
  `region_id`, `ku_id`, `locality_district_id`, `locality_region_id`, `obec`, `okres`, `region`
  (the BEFORE trigger `listings_set_admin_geo`, migration 289, writes these on every ingest);
- `listings.street`, `listings.house_number`, `listings.street_name_key` (migration 256) —
  `scraper/street.py`'s output; the projection's `street_name` / `ulice_kod` / `street_block_key`
  replace them, and the projection's street is about to become the RÚIAN canonical form;
- `geocode_cache` and any Mapy-derived coordinate (licence class E; the R4 purge nulls them);
- `browse_list` / `browse_projection` geo columns, and `properties.*` geography (quarantined, not
  migrated — the property grain is `property_location_current`);
- anything from the removed dedup engine (rule 15).

If a new answer and a legacy answer disagree, **the projection is the one with a stated precision**.
Do not "correct" a projection row from `listings.geom`; open it as a location-program finding.

## 6. Coverage and freshness, dated (re-check before trusting a number)

- **Un-shadow 2026-09-09 17:54–17:59Z:** every portal's stored claims are now resolver inputs.
- **Sweeps.** remax, mmreality, maxima were fully re-mined from the archive on 2026-09-08
  (99.2 % / 93.8 % / 98.6 % of archived listings carry a claim). **idnes, ceskereality, realitymix
  are being swept from 2026-09-10** (`location_claims_remine_archive.yml`, one portal per run);
  until a portal's batch reports `reached_end=true`, its listings older than the hourly intake's
  watermark have thin claims — expect `granularity` to sit at `obec`/`okres` and `geo_blockable`
  false on many of them, *temporarily*. bazos: structured sweep not run; the LLM lane
  (`claims_llm@2`, town → část obce → street → house number from free text) runs from 2026-09-10.
- **Resolver lag.** `dirty_locations` held ≥ 26.7k listings on 2026-09-09; the `*/15` drain works
  from the oldest row. Judge freshness by `built_at` and by the oldest `dirty_locations.enqueued_at`,
  not by queue length.
- **Registry-bound share** (a listing matched to a RÚIAN address point): sreality 26.9 %,
  bezrealitky 60.7 % street+house-number; bazos 0 %, idnes 1.4 % (design `db-coverage-stats.md` §3).
  Tier 0 keys are only as common as that.

## 7. One query, as the dedup design asked for

`PROGRAM.md` (2026-09-08 (b)) records: "W2 should read location through ONE query it can later
point at the location projection." That query is:

```sql
select l.listing_id, l.property_id, l.source,
       l.geom, l.granularity, l.match_confidence, l.position_source,
       l.uncertainty_radius_m, l.radius_semantics,
       l.geo_blockable, l.renderable_as_point, l.is_low_precision, l.location_disputed,
       l.pin_collision_class, l.cluster_heterogeneity_ok, l.pin_shared_by_n,
       l.addr_block_key, l.building_block_key, l.street_block_key, l.geo_cell_key,
       l.ruian_adm_kod, l.stavebni_objekt_kod, l.parcela_id, l.ulice_kod, l.obec_kod,
       l.admin_assignment_method, l.country_code, l.country_confidence, l.is_cz,
       l.street_name, l.house_number_cp, l.built_at
  from listing_location_current l
 where l.listing_id = any(%(listing_ids)s::bigint[]);
```

Everything a candidate path needs is in that row. Joining `listings` for `disposition`, `floor`,
`usable_area`, `price_czk` is fine — those are not location.
