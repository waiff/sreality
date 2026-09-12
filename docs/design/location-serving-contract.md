# Location serving contract — how a consumer reads the location engine

**Status 2026-09-12 (W2-b).** The location-data program's engine is **claims → four-step resolver →
one answer table**. `listing_location` (migration 501, 26 columns) is the whole serving surface;
W2-b dropped `listing_location_current`, `property_location_current` and every resolver-side
relation the deleted engines wrote. This page is the contract a consumer codes against, written
first for the NEW DEDUP program (rule 15), which is the first consumer after the admin dashboard.

The one-sentence rule (CLAUDE.md rule 25): **a listing's location is `listing_location`.** There is
no second store to read: W4-c (migration 508) dropped `listings.geom` and every geo-derived column
with it. They were not wrong, they were *unqualified* — a coordinate with no statement of how
precisely or how trustworthily it is known.

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

**The label is composed ONCE, at read time** (W3, migration 503). "A reader composes what it needs"
became eleven readers composing five different strings, so the composition moved into ONE immutable
SQL function, `location_display_label(street_name, house_number_cp, house_number_co, obec_name,
cast_obce_name, country_code, country_status)`, and every serving view publishes its result as
`display_label`: `browse_projection` (→ `browse_list`, `properties_map_mv`), `listing_feed_public`,
`properties_public`, `listings_public`, `broker_listings_public`, and `pipeline_board_public` (which
reads it off `properties_public` — it is `security_invoker` and `listing_location` is revoked from
`authenticated`). The four API surfaces that read `listings` directly — the extension's
`/listings/lookup`, the notification composer and outbox, the dispatch feed, the collections route
— call the same function. Still no STORED label: a function over the answer table's own columns is
one definition, computed where it is read.

**The fallback order, which is the rule and not a detail:**

1. `country_status = 'foreign'` → the country code. Foreign is a DETERMINATION, never a default for
   "no town found", and a foreign row has no RÚIAN chain to fall through to.
2. a street → `"Street čp/čo, Obec"`. Both house numbers when both are known, whichever exists
   otherwise, and a street with neither is still a street.
3. a část obce that DIFFERS from the town → `"Část obce, Obec"`. "Brno, Brno" is noise.
4. the town alone → `"Obec"`.
5. nothing → NULL. A CZ row with no town is the red line rule 25 measures; it renders as an em-dash,
   never as a bare "CZ".

**Precision is DRAWN, not described** (W3-3). `browse_projection` and `listing_feed_public` also
publish `granularity_rank` (the INT from `location_granularity_rank` — the SPA compares numbers,
never enum text) and `uncertainty_radius_m`. In point mode the Browse map draws a translucent
true-metre circle of that radius under any pin **below building level** (rank < 90); at or above it
the pin is the building and stands alone. Clusters and server-side grid cells carry no per-pin
identity or radius, so there is no per-pin circle above the point budget.

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
shared-pin count was parked as a read-time aggregate (`count(*) over (partition by geom)`) and W3
REFUSED it (decision W3-2): `sync_browse_list` filters `WHERE property_id = ANY(…)` and that qual
cannot be pushed below a window function, so every merge would aggregate the whole corpus. No
producer, no measured need; the circle rule above is granularity-only. `position_licence_class` — the licence rail
moved UPSTREAM: the resolver's claim projection admits only `licence_class IN ('portal','operator')`,
so a Mapy-class coordinate is never READ, and a partial index on `location_claims` keeps the
remediation set one indexed predicate away. `position_source`, `blur_evidence`,
`radius_semantics`, `admin_assignment_method`, the four `*_unit_id` surrogates and momc/ku/pou/orp
— producers deleted, or provably NULL on every row.

## 3. The floors — what each feature may consume

**There is no floor TABLE any more.** `location_data/serving_contracts.py` declared fourteen
per-consumer minimum-granularity floors and, in a year, acquired zero production readers: every one
was a declaration of what a consumer *would* accept once it flipped, asserted only by its own unit
test. W3 S4 deleted it, the same way and for the same reason W2-b deleted `serving_flags.py` — a
rail that nothing evaluates reads as enforcement and is not. A floor now lives where its consumer
does, in the code that would violate it.

Two survive as code, because they are the two that were ever reached:

* **path C's town floor** — `obec`, any confidence — is `toolkit/dedup_candidates.PATHS["C"]`
  itself (`block_key="obec_kod"`, `district_key="cast_obce_kod"`). The path definition IS the floor:
  it cannot block on anything finer than the column it blocks on.
* **the filter default** — operator action A5's `FILTER_DEFAULT_SEMANTICS = "include_and_badge"` —
  is a constant in `api/location_filter.py`, next to the predicate it governs.

`dedup_rung_0a` / `dedup_tier_1` / `dedup_tier_2` described a dedup engine that does not exist: the
2026-08 cutoff removed it wholesale and the rebuild is path C alone. `dedup_rung_0b` (building) and
`dedup_rung_0c` (parcel) had already gone with their key columns in W2-b — a floor whose gate column
does not exist is a floor nothing can evaluate.

The rung the operator raised on 2026-09-08 was ruled on 2026-09-10 as **path C** (NEW DEDUP ledger,
that date): town = `obec_kod`, no radius, attributes (disposition, then area) do the rest. Its
floor is the path's own block key — `obec_kod`, any confidence. Refined the same day: in Praha, Brno
and Ostrava the town is additionally split by **`cast_obce_kod`**, chosen over `momc_kod` on
measured coverage (85 % vs 24 % of Praha listings, because the quarter is often *claimed* in portal
text while the administrative district needs a resolved address). That split does not raise the
floor: a row with no `cast_obce_kod` still qualifies and matches against its whole town.

## 4. No flag

W2-b deleted `location_data/serving_flags.py`. Its per-feature `app_settings` keys were never seeded
and no consumer ever read one; the module documented a per-feature switch that the program now makes
by cutting a reader over in a PR, which is reversible the same way every other deploy is. Flipping a
consumer is a code change gated by its own program (rule 15 for dedup), not a runtime flag. W3 S4
removed the last two runtime switches of any kind on this path — the SPA bisect hatches
`?map=legacy` and `?cityQualityLegacy=1` — for the same reason.

## 5. There is no legacy path (W4-c)

Migration 508 dropped it. `listings` and `properties` carry NO place columns: no `geom`, no
`obec_id`/`okres_id`/`region_id`/`ku_id`, no `obec`/`okres`/`region`, no
`locality`/`district`/`street`/`house_number`/`zip`, no `street_name_key`/`geo_cell_key`/
`street_source`, and none of the sreality `locality_*_id` portal ids. The trigger that wrote them
(`listings_set_admin_geo`, migration 289), `geocode_cache`, the `mapy_affected` inventory and the
`address_points` mirror are gone with them. One trap survives the deletion: `listings.geom` WAS
`geography` and `listing_location.geom` IS `geometry(Point,4326)`, so every metre-based
`ST_DWithin` / `ST_Distance` must cast `ll.geom::geography` (index `listing_location_geog_gist`,
migration 507) or it silently measures degrees.

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
