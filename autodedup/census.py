"""Region census: pick the one region the autonomous dedup engine is first trialled in.

Reads ALL-TIME listings (active + delisted) at two block grains — town
(`listing_location.obec_kod`) and quarter (`listing_location.cast_obce_kod`) — and reports,
per block, how much of the signal the future engine needs is actually present there: portal
breadth, category spread, disposition/area/floor/broker coverage, location precision, and
the raw in-block pair count under a COARSE block x category_main x category_type key — an
upper bound on any finer blocking the engine adopts, not a figure comparable to a live lane
(the shipped path-C generator blocks on town plus disposition or area, and splits Praha).

Three query layers, because cost differs by an order of magnitude. The COVERAGE layer is one
statement of corpus-wide totals, so the block layer's INNER JOIN has a denominator. The BLOCK
layer is one grouped pass over `listings JOIN listing_location` and nothing else. The DETAIL
layer joins images, pHash, CLIP embeddings and the shared-pin aggregate, and runs only for the
handful of blocks the block layer shortlists.

On top of those three sits a PROBE layer: the questions the block counts cannot answer. Four
probes run per shortlisted block (per-portal image/pHash/CLIP coverage, the same-source re-post
base rate, address-level developer density, and shared-image density) and are attached to the
block under `probes`; two more are corpus-wide and block-independent (one portal's location
posture, and the ingest rate every cost number scales with), so they live in their own lane
mode (`python3 -m autodedup.lane --mode probes`) writing `probes.json`. A probe that fails is
recorded on itself, never raised.

`census.json` is written incrementally — after coverage, after each grain's block pass, after
EVERY detailed block, and again after the detail loop — and a detail statement that fails is
recorded on its block (`detail_error`) rather than voiding the whole run or skipping that
block's probes.

Cohort note: the census counts every listing that has a `listing_location` row carrying the
grain's code. That is deliberately WIDER than the consumer serving rule
(`location_data.claims_common.served_location_predicate`) — blocking wants every row that has
a block code, not only the rows Browse shows — so the served count is reported alongside
(`n_served`) rather than used as a filter. Evaluated on the already-joined row (`listing_id`
is that table's primary key) the EXISTS in that predicate reduces to exactly the
geom/country_status test spelled out below — pinned against the one definition by
`tests/autodedup/test_census.py::test_served_clause_matches_the_one_pinned_definition`, so the
census's `n_served` can never come to mean something other than what Browse means.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from autodedup import readiness_sql

SOURCES: tuple[str, ...] = (
    "sreality", "bazos", "bezrealitky", "idnes", "mmreality", "remax", "ceskereality",
    "realitymix", "maxima",
)
CATEGORY_MAIN: tuple[str, ...] = ("byt", "dum", "pozemek", "komercni", "ostatni")
CATEGORY_TYPE: tuple[str, ...] = ("prodej", "pronajem", "drazba", "podil")

GRAINS: tuple[str, ...] = ("town", "quarter")

# The four categories a trial region must carry for the engine to be exercised on more than
# one shape of listing; `ostatni` is a residual bucket and is deliberately not required.
REQUIRED_CATEGORY_MAIN: tuple[str, ...] = ("byt", "dum", "pozemek", "komercni")

_PLACEHOLDER = re.compile(r"%\((\w+)\)s")


def _json_default(value: Any) -> str:
    return str(value)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Dump a report, stringifying what json cannot (datetimes, Decimals)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )



# --- SQL ------------------------------------------------------------------------------
# Two constants, one per grain, because the grain is a COLUMN and a column name is never
# string-built into SQL. Everything else is identical between them.

CENSUS_TOWNS_SQL = """
WITH base AS (
    SELECT
        ll.obec_kod                                   AS block_code,
        ll.obec_name                                  AS block_name,
        l.source                                      AS source,
        l.category_main                               AS category_main,
        l.category_type                               AS category_type,
        l.is_active                                   AS is_active,
        l.disposition                                 AS disposition,
        l.area_m2                                     AS area_m2,
        l.floor                                       AS floor,
        l.broker_identity_id                          AS broker_identity_id,
        (l.source_url IS NOT NULL)                    AS has_source_url,
        -- Zero area is not an area anywhere else in this file (`with_area` filters
        -- `area_m2 > 0`), so a zero-area row with nothing else on it is just as unreachable
        -- as a NULL-area one and counts against the ceiling the same way.
        (ll.geom IS NOT NULL OR ll.country_status = 'foreign') AS served,
        gr.is_address_grain                           AS is_address_grain,
        gr.rank >= (SELECT r.rank FROM location_granularity_rank r
                     WHERE r.granularity = 'street')  AS at_street_grain
    FROM listings l
    JOIN listing_location ll ON ll.listing_id = l.id
    LEFT JOIN location_granularity_rank gr ON gr.granularity = ll.granularity
    WHERE ll.obec_kod IS NOT NULL
),
cells AS (
    SELECT block_code, category_main, category_type, count(*) AS n
    FROM base
    WHERE category_main IS NOT NULL AND category_type IS NOT NULL
    GROUP BY 1, 2, 3
),
pairs AS (
    SELECT block_code, sum(n * (n - 1) / 2)::bigint AS raw_inblock_pairs
    FROM cells
    GROUP BY 1
),
agg AS (
    SELECT
        block_code,
        min(block_name)                                               AS block_name,
        count(*)                                                      AS n_total,
        count(*) FILTER (WHERE is_active)                             AS n_active,
        count(*) FILTER (WHERE NOT is_active)                         AS n_inactive,
        count(*) FILTER (WHERE served)                                AS n_served,
        count(*) FILTER (WHERE category_main IS NULL
                            OR category_type IS NULL)                 AS n_uncategorised,
        count(*) FILTER (WHERE source = 'sreality')                   AS src_sreality,
        count(*) FILTER (WHERE source = 'bazos')                      AS src_bazos,
        count(*) FILTER (WHERE source = 'bezrealitky')                AS src_bezrealitky,
        count(*) FILTER (WHERE source = 'idnes')                      AS src_idnes,
        count(*) FILTER (WHERE source = 'mmreality')                  AS src_mmreality,
        count(*) FILTER (WHERE source = 'remax')                      AS src_remax,
        count(*) FILTER (WHERE source = 'ceskereality')               AS src_ceskereality,
        count(*) FILTER (WHERE source = 'realitymix')                 AS src_realitymix,
        count(*) FILTER (WHERE source = 'maxima')                     AS src_maxima,
        count(*) FILTER (WHERE category_main = 'byt')                 AS cat_byt,
        count(*) FILTER (WHERE category_main = 'dum')                 AS cat_dum,
        count(*) FILTER (WHERE category_main = 'pozemek')             AS cat_pozemek,
        count(*) FILTER (WHERE category_main = 'komercni')            AS cat_komercni,
        count(*) FILTER (WHERE category_main = 'ostatni')             AS cat_ostatni,
        count(*) FILTER (WHERE category_type = 'prodej')              AS type_prodej,
        count(*) FILTER (WHERE category_type = 'pronajem')            AS type_pronajem,
        count(*) FILTER (WHERE category_type = 'drazba')              AS type_drazba,
        count(*) FILTER (WHERE category_type = 'podil')               AS type_podil,
        count(*) FILTER (WHERE disposition IS NOT NULL)               AS with_disposition,
        count(*) FILTER (WHERE area_m2 > 0)                           AS with_area,
        count(*) FILTER (WHERE floor IS NOT NULL)                     AS with_floor,
        count(*) FILTER (WHERE broker_identity_id IS NOT NULL)        AS with_broker,
        count(*) FILTER (WHERE at_street_grain)                       AS with_street,
        count(*) FILTER (WHERE is_address_grain)                      AS with_point,
        count(*) FILTER (WHERE has_source_url)                        AS with_source_url
    FROM base
    GROUP BY 1
)
-- Portal breadth is summed from the nine counters rather than aggregated with
-- a de-duplicating aggregate: that is an ordered agg, which bars HashAgg for the whole rollup
-- and forces a sort of every base row. Same number, hashable plan.
-- LEFT JOIN because `cells` drops uncategorised rows, so an all-uncategorised block still
-- gets a census line (with zero pairs) instead of disappearing.
SELECT
    a.*,
    coalesce(p.raw_inblock_pairs, 0)                                  AS raw_inblock_pairs,
    (  (a.src_sreality > 0)::int     + (a.src_bazos > 0)::int
     + (a.src_bezrealitky > 0)::int  + (a.src_idnes > 0)::int
     + (a.src_mmreality > 0)::int    + (a.src_remax > 0)::int
     + (a.src_ceskereality > 0)::int + (a.src_realitymix > 0)::int
     + (a.src_maxima > 0)::int)                                       AS n_sources
FROM agg a
LEFT JOIN pairs p ON p.block_code = a.block_code
WHERE a.n_total BETWEEN %(min_n)s AND %(max_n)s
ORDER BY n_sources DESC, a.n_total DESC
LIMIT %(top)s
"""

CENSUS_QUARTERS_SQL = """
WITH base AS (
    SELECT
        ll.cast_obce_kod                              AS block_code,
        ll.cast_obce_name                             AS block_name,
        l.source                                      AS source,
        l.category_main                               AS category_main,
        l.category_type                               AS category_type,
        l.is_active                                   AS is_active,
        l.disposition                                 AS disposition,
        l.area_m2                                     AS area_m2,
        l.floor                                       AS floor,
        l.broker_identity_id                          AS broker_identity_id,
        (l.source_url IS NOT NULL)                    AS has_source_url,
        -- Zero area is not an area anywhere else in this file (`with_area` filters
        -- `area_m2 > 0`), so a zero-area row with nothing else on it is just as unreachable
        -- as a NULL-area one and counts against the ceiling the same way.
        (ll.geom IS NOT NULL OR ll.country_status = 'foreign') AS served,
        gr.is_address_grain                           AS is_address_grain,
        gr.rank >= (SELECT r.rank FROM location_granularity_rank r
                     WHERE r.granularity = 'street')  AS at_street_grain
    FROM listings l
    JOIN listing_location ll ON ll.listing_id = l.id
    LEFT JOIN location_granularity_rank gr ON gr.granularity = ll.granularity
    WHERE ll.cast_obce_kod IS NOT NULL
),
cells AS (
    SELECT block_code, category_main, category_type, count(*) AS n
    FROM base
    WHERE category_main IS NOT NULL AND category_type IS NOT NULL
    GROUP BY 1, 2, 3
),
pairs AS (
    SELECT block_code, sum(n * (n - 1) / 2)::bigint AS raw_inblock_pairs
    FROM cells
    GROUP BY 1
),
agg AS (
    SELECT
        block_code,
        min(block_name)                                               AS block_name,
        count(*)                                                      AS n_total,
        count(*) FILTER (WHERE is_active)                             AS n_active,
        count(*) FILTER (WHERE NOT is_active)                         AS n_inactive,
        count(*) FILTER (WHERE served)                                AS n_served,
        count(*) FILTER (WHERE category_main IS NULL
                            OR category_type IS NULL)                 AS n_uncategorised,
        count(*) FILTER (WHERE source = 'sreality')                   AS src_sreality,
        count(*) FILTER (WHERE source = 'bazos')                      AS src_bazos,
        count(*) FILTER (WHERE source = 'bezrealitky')                AS src_bezrealitky,
        count(*) FILTER (WHERE source = 'idnes')                      AS src_idnes,
        count(*) FILTER (WHERE source = 'mmreality')                  AS src_mmreality,
        count(*) FILTER (WHERE source = 'remax')                      AS src_remax,
        count(*) FILTER (WHERE source = 'ceskereality')               AS src_ceskereality,
        count(*) FILTER (WHERE source = 'realitymix')                 AS src_realitymix,
        count(*) FILTER (WHERE source = 'maxima')                     AS src_maxima,
        count(*) FILTER (WHERE category_main = 'byt')                 AS cat_byt,
        count(*) FILTER (WHERE category_main = 'dum')                 AS cat_dum,
        count(*) FILTER (WHERE category_main = 'pozemek')             AS cat_pozemek,
        count(*) FILTER (WHERE category_main = 'komercni')            AS cat_komercni,
        count(*) FILTER (WHERE category_main = 'ostatni')             AS cat_ostatni,
        count(*) FILTER (WHERE category_type = 'prodej')              AS type_prodej,
        count(*) FILTER (WHERE category_type = 'pronajem')            AS type_pronajem,
        count(*) FILTER (WHERE category_type = 'drazba')              AS type_drazba,
        count(*) FILTER (WHERE category_type = 'podil')               AS type_podil,
        count(*) FILTER (WHERE disposition IS NOT NULL)               AS with_disposition,
        count(*) FILTER (WHERE area_m2 > 0)                           AS with_area,
        count(*) FILTER (WHERE floor IS NOT NULL)                     AS with_floor,
        count(*) FILTER (WHERE broker_identity_id IS NOT NULL)        AS with_broker,
        count(*) FILTER (WHERE at_street_grain)                       AS with_street,
        count(*) FILTER (WHERE is_address_grain)                      AS with_point,
        count(*) FILTER (WHERE has_source_url)                        AS with_source_url
    FROM base
    GROUP BY 1
)
-- Portal breadth is summed from the nine counters rather than aggregated with
-- a de-duplicating aggregate: that is an ordered agg, which bars HashAgg for the whole rollup
-- and forces a sort of every base row. Same number, hashable plan.
-- LEFT JOIN because `cells` drops uncategorised rows, so an all-uncategorised block still
-- gets a census line (with zero pairs) instead of disappearing.
SELECT
    a.*,
    coalesce(p.raw_inblock_pairs, 0)                                  AS raw_inblock_pairs,
    (  (a.src_sreality > 0)::int     + (a.src_bazos > 0)::int
     + (a.src_bezrealitky > 0)::int  + (a.src_idnes > 0)::int
     + (a.src_mmreality > 0)::int    + (a.src_remax > 0)::int
     + (a.src_ceskereality > 0)::int + (a.src_realitymix > 0)::int
     + (a.src_maxima > 0)::int)                                       AS n_sources
FROM agg a
LEFT JOIN pairs p ON p.block_code = a.block_code
WHERE a.n_total BETWEEN %(min_n)s AND %(max_n)s
ORDER BY n_sources DESC, a.n_total DESC
LIMIT %(top)s
"""

# One block at a time. The grain travels as a pair of nullable codes rather than a column
# name: the caller binds the grain's code and NULL for the other, so each branch stays an
# indexable equality instead of a CASE over a column name. Both codes carry an explicit
# `::bigint` because the unused one is ALWAYS NULL — psycopg sends no type OID for a None, so
# without the cast Postgres cannot infer a type for the `IS NULL` branch and Parse fails with
# 42P18 (the same shape tests/test_sql_schema_prepare.py records as an unchecked artifact).
CENSUS_DETAIL_SQL = """
WITH block AS (
    SELECT
        l.id                 AS id,
        l.broker_identity_id AS broker_identity_id,
        l.first_seen_at      AS first_seen_at,
        l.inactive_at        AS inactive_at,
        ll.geom              AS geom,
        (length(coalesce(substr(l.description, 1, 200), '')) >= 200) AS has_desc200,
        (coalesce(l.area_m2, 0) <= 0 AND l.disposition IS NULL
         AND l.broker_identity_id IS NULL
         AND length(coalesce(substr(l.description, 1, 200), '')) < 200) AS no_signal
    FROM listings l
    JOIN listing_location ll ON ll.listing_id = l.id
    WHERE (%(obec_kod)s::bigint IS NULL OR ll.obec_kod = %(obec_kod)s::bigint)
      AND (%(cast_obce_kod)s::bigint IS NULL OR ll.cast_obce_kod = %(cast_obce_kod)s::bigint)
),
stored_images AS (
    SELECT
        i.listing_id           AS listing_id,
        (i.phash IS NOT NULL)  AS has_phash,
        EXISTS (SELECT 1 FROM image_clip_embeddings e WHERE e.image_id = i.id) AS has_clip
    FROM images i
    JOIN block b ON b.id = i.listing_id
    WHERE i.storage_path IS NOT NULL
),
img AS (
    SELECT
        listing_id                             AS listing_id,
        count(*)                               AS n_images,
        count(*) FILTER (WHERE has_phash)      AS n_phash,
        count(*) FILTER (WHERE has_clip)       AS n_clip
    FROM stored_images
    GROUP BY 1
),
pins AS (
    SELECT ST_AsBinary(geom) AS pin, count(*) AS n
    FROM block
    WHERE geom IS NOT NULL
    GROUP BY 1
),
years AS (
    SELECT jsonb_object_agg(y, c) AS by_year
    FROM (
        SELECT extract(year FROM inactive_at)::int::text AS y, count(*) AS c
        FROM block
        WHERE inactive_at IS NOT NULL
        GROUP BY 1
    ) t
)
SELECT
    (SELECT count(*) FROM block)                                      AS n_block_listings,
    (SELECT count(*) FROM block WHERE has_desc200)                    AS with_desc200,
    (SELECT count(*) FROM block WHERE no_signal)                      AS no_signal_at_all,
    coalesce((SELECT sum(n_images) FROM img), 0)::bigint              AS n_images,
    (SELECT count(*) FROM img)                                        AS n_listings_with_images,
    (SELECT count(*) FROM img WHERE n_phash > 0)                      AS n_listings_with_phash,
    (SELECT count(*) FROM img WHERE n_clip > 0)                       AS n_listings_with_clip,
    coalesce((SELECT sum(n) FROM pins WHERE n >= %(pin_share_min)s), 0)::bigint
                                                                      AS shared_pin_listings,
    (SELECT count(DISTINCT broker_identity_id) FROM block
      WHERE broker_identity_id IS NOT NULL)                           AS n_distinct_brokers,
    (SELECT min(first_seen_at) FROM block)                            AS first_seen_min,
    (SELECT max(first_seen_at) FROM block)                            AS first_seen_max,
    coalesce((SELECT by_year FROM years), '{}'::jsonb)                AS n_inactive_by_year
"""

# The denominator for everything the block layer's INNER JOIN drops: a block's n_total is only
# interpretable next to how much of the corpus carries a grain code at all. One pass, no
# per-block work, so it is cheap enough to run every time.
CENSUS_COVERAGE_SQL = """
SELECT
    (SELECT count(*) FROM listings)                                   AS n_listings,
    (SELECT count(*) FROM listings l JOIN listing_location ll ON ll.listing_id = l.id)
                                                                      AS n_with_location_row,
    (SELECT count(*) FROM listing_location WHERE obec_kod IS NOT NULL)
                                                                      AS n_with_town_code,
    (SELECT count(*) FROM listing_location WHERE cast_obce_kod IS NOT NULL)
                                                                      AS n_with_quarter_code,
    (SELECT count(*) FROM listing_location ll
      WHERE (ll.geom IS NOT NULL OR ll.country_status = 'foreign'))   AS n_served
"""

# --- probes ---------------------------------------------------------------------------
# The census answers "what is in this block"; the probes answer the four questions every
# candidate design GUESSED at. Same nullable-grain-code binding as the detail statement, same
# explicit `::bigint` on both codes (psycopg sends no type OID for a None, so an uncast NULL
# fails Parse with 42P18).

# B1. Per-portal signal coverage. Block-level image coverage hides that `images.phash` is
# produced by `scraper.image_phash.compute_dhash` and that the only index touching it is keyed
# on `sreality_id`, NULL for eight of the nine portals - so every image-lane reach claim is a
# per-portal number or it is a guess.
CENSUS_PROBE_PORTAL_SIGNAL_SQL = """
WITH block AS (
    SELECT l.id AS id, l.source AS source
    FROM listings l
    JOIN listing_location ll ON ll.listing_id = l.id
    WHERE (%(obec_kod)s::bigint IS NULL OR ll.obec_kod = %(obec_kod)s::bigint)
      AND (%(cast_obce_kod)s::bigint IS NULL OR ll.cast_obce_kod = %(cast_obce_kod)s::bigint)
),
stored_images AS (
    SELECT
        i.listing_id           AS listing_id,
        (i.phash IS NOT NULL)  AS has_phash,
        EXISTS (SELECT 1 FROM image_clip_embeddings e WHERE e.image_id = i.id) AS has_clip
    FROM images i
    JOIN block b ON b.id = i.listing_id
    WHERE i.storage_path IS NOT NULL
),
per_listing AS (
    SELECT
        b.id                                        AS id,
        b.source                                    AS source,
        count(s.listing_id)                         AS n_images,
        count(*) FILTER (WHERE s.has_phash)         AS n_phash,
        count(*) FILTER (WHERE s.has_clip)          AS n_clip
    FROM block b
    LEFT JOIN stored_images s ON s.listing_id = b.id
    GROUP BY 1, 2
)
SELECT
    source                                                            AS source,
    count(*)                                                          AS n_listings,
    count(*) FILTER (WHERE n_images > 0)                              AS n_with_images,
    count(*) FILTER (WHERE n_phash > 0)                               AS n_with_phash,
    count(*) FILTER (WHERE n_clip > 0)                                AS n_with_clip,
    round((percentile_cont(0.5)
             WITHIN GROUP (ORDER BY n_images::double precision))::numeric, 2)::float8
                                                                      AS median_images
FROM per_listing
GROUP BY 1
ORDER BY 2 DESC
"""

# B3. Re-post base rate - the single largest unmeasured quantity in this program. Same source,
# same broker identity, SAME category_main and category_type, equal disposition, area within
# two percent, and DISJOINT lifetime windows, so a pair is a RE-POST rather than two units
# advertised side by side. The category equality is not optional: sale != rent and flat !=
# house are hard merge-compatibility rules (CLAUDE.md rule 15), so a prodej listing relisted
# as pronajem is a pair the engine may never merge and must not inflate the base rate - it is
# counted separately as `repost_pairs_cross_type`. The tolerance is measured against the
# SMALLER area so the band does not depend on which row happens to carry the lower id, and is
# spelled as a multiplication, never a literal percent sign: psycopg scans the whole statement
# for placeholders. Phone-number identity is NOT a second arm here - normalisation lives in the
# broker resolver and broker_identity_id is its output, so pairs whose broker never resolved go
# uncounted and `repost_pairs` is a lower bound. Cost is bounded by the block (a few thousand
# rows) and by the statement timeout.
CENSUS_PROBE_REPOSTS_SQL = """
WITH block AS (
    SELECT
        l.id                                AS id,
        l.source                            AS source,
        l.category_main                     AS category_main,
        l.category_type                     AS category_type,
        l.broker_identity_id                AS broker_identity_id,
        l.disposition                       AS disposition,
        l.area_m2                           AS area_m2,
        l.first_seen_at                     AS first_seen_at,
        coalesce(l.last_seen_at, l.inactive_at, now()) AS ended_at
    FROM listings l
    JOIN listing_location ll ON ll.listing_id = l.id
    WHERE (%(obec_kod)s::bigint IS NULL OR ll.obec_kod = %(obec_kod)s::bigint)
      AND (%(cast_obce_kod)s::bigint IS NULL OR ll.cast_obce_kod = %(cast_obce_kod)s::bigint)
      AND l.broker_identity_id IS NOT NULL
      AND l.disposition IS NOT NULL
      AND l.area_m2 > 0
),
candidate_pairs AS (
    SELECT
        a.id AS a_id,
        b.id AS b_id,
        (b.category_main IS NOT DISTINCT FROM a.category_main
         AND b.category_type IS NOT DISTINCT FROM a.category_type)    AS same_category
    FROM block a
    JOIN block b
      ON b.id > a.id
     AND b.source = a.source
     AND b.broker_identity_id = a.broker_identity_id
     AND b.disposition = a.disposition
     AND abs(b.area_m2 - a.area_m2) <= 0.02 * least(a.area_m2, b.area_m2)
     AND (b.first_seen_at > a.ended_at OR a.first_seen_at > b.ended_at)
),
involved AS (
    SELECT a_id AS id FROM candidate_pairs WHERE same_category
    UNION
    SELECT b_id AS id FROM candidate_pairs WHERE same_category
)
SELECT
    (SELECT count(*) FROM candidate_pairs WHERE same_category)        AS repost_pairs,
    (SELECT count(*) FROM candidate_pairs WHERE NOT same_category)    AS repost_pairs_cross_type,
    (SELECT count(*) FROM involved)                                   AS repost_listings,
    (SELECT count(*) FROM block)                                      AS repost_candidates
"""

# B4a. Developer-project density by address. `n_addr_3plus_multi_floor` reads ">= 3 listings at
# distinct floors" loosely (>= 2 distinct floors), `n_addr_3plus_distinct_floors` strictly (>= 3);
# both are reported because the spec's phrasing carries both readings and neither costs a pass.
# The denominator travels with them: ruian_adm_kod coverage is portal-skewed (bezrealitky ~61%,
# sreality ~27%, idnes ~1%, bazos 0%), so "12 dense addresses" means nothing until the reader
# knows how many of the block's rows carried an address at all.
CENSUS_PROBE_ADDRESS_DENSITY_SQL = """
WITH block AS (
    SELECT ll.ruian_adm_kod AS ruian_adm_kod, l.floor AS floor
    FROM listings l
    JOIN listing_location ll ON ll.listing_id = l.id
    WHERE (%(obec_kod)s::bigint IS NULL OR ll.obec_kod = %(obec_kod)s::bigint)
      AND (%(cast_obce_kod)s::bigint IS NULL OR ll.cast_obce_kod = %(cast_obce_kod)s::bigint)
),
per_addr AS (
    SELECT
        ruian_adm_kod            AS ruian_adm_kod,
        count(*)                 AS n_listings,
        count(DISTINCT floor)    AS n_floors
    FROM block
    WHERE ruian_adm_kod IS NOT NULL
    GROUP BY 1
)
SELECT
    (SELECT count(*) FROM block)                                      AS n_block_listings,
    (SELECT count(*) FROM block WHERE ruian_adm_kod IS NOT NULL)      AS n_addressed_listings,
    (SELECT count(*) FROM per_addr)                                   AS n_addresses,
    (SELECT count(*) FROM per_addr WHERE n_listings >= 3)             AS n_addr_3plus,
    (SELECT count(*) FROM per_addr WHERE n_listings >= 3 AND n_floors >= 2)
                                                                      AS n_addr_3plus_multi_floor,
    (SELECT count(*) FROM per_addr WHERE n_listings >= 3 AND n_floors >= 3)
                                                                      AS n_addr_3plus_distinct_floors,
    coalesce((SELECT sum(n_listings) FROM per_addr
               WHERE n_listings >= 3 AND n_floors >= 2), 0)::bigint   AS n_addr_dense_listings,
    coalesce((SELECT max(n_listings) FROM per_addr), 0)::bigint       AS largest_address_group
"""

# B4b. Shared-image density. `images.phash` holds a dHash (scraper/image_phash.py), so an exact
# value shared across many listings is the developer-photoset negative class this program's
# precision is decided on - and the seed for the assembled negative-control cohort.
# CAVEAT the four scalars cannot express: a Hamming-0 dHash group is NOT automatically a shared
# photoset. dHash collapses mostly-white documents, so floor plans and energy certificates hash
# alike (docs/design/tag-annotation-matrix.md, docs/design/new-dedup/ENCODER-DECISION.md). The
# top groups are therefore returned with a sample listing, so the operator can classify
# photoset-vs-document BEFORE the negative-control cohort is assembled off this probe; a cheap
# automatic filter, if one is wanted later, is excluding document-tagged images via
# `image_tag_scores` (migration 490).
CENSUS_PROBE_SHARED_HASH_SQL = """
WITH block AS (
    SELECT l.id AS id, l.source AS source
    FROM listings l
    JOIN listing_location ll ON ll.listing_id = l.id
    WHERE (%(obec_kod)s::bigint IS NULL OR ll.obec_kod = %(obec_kod)s::bigint)
      AND (%(cast_obce_kod)s::bigint IS NULL OR ll.cast_obce_kod = %(cast_obce_kod)s::bigint)
),
block_images AS (
    SELECT i.phash AS phash, i.listing_id AS listing_id, b.source AS source
    FROM images i
    JOIN block b ON b.id = i.listing_id
    WHERE i.phash IS NOT NULL AND i.storage_path IS NOT NULL
),
hashes AS (
    SELECT
        phash                        AS phash,
        count(DISTINCT listing_id)   AS n_listings,
        count(DISTINCT source)       AS n_sources,
        min(listing_id)              AS sample_listing_id
    FROM block_images
    GROUP BY 1
),
top_groups AS (
    SELECT phash, n_listings, n_sources, sample_listing_id
    FROM hashes
    WHERE n_listings >= %(hash_share_min)s::int
    ORDER BY n_listings DESC, phash
    LIMIT 10
)
SELECT
    (SELECT count(*) FROM hashes)                                     AS n_distinct_hashes,
    (SELECT count(*) FROM hashes WHERE n_listings >= %(hash_share_min)s::int)
                                                                      AS n_shared_hashes,
    coalesce((SELECT count(DISTINCT bi.listing_id)
                FROM block_images bi
                JOIN hashes h ON h.phash = bi.phash
               WHERE h.n_listings >= %(hash_share_min)s::int), 0)::bigint
                                                                      AS n_shared_hash_listings,
    coalesce((SELECT max(n_listings) FROM hashes), 0)::bigint         AS largest_hash_group,
    coalesce((SELECT jsonb_agg(jsonb_build_object(
                         'phash', t.phash::text,
                         'n_listings', t.n_listings,
                         'n_sources', t.n_sources,
                         'sample_listing_id', t.sample_listing_id)
                       ORDER BY t.n_listings DESC, t.phash)
                FROM top_groups t), '[]'::jsonb)                      AS top_hash_groups
"""

# B2. Corpus-wide, run once: one portal's location posture as a granularity histogram, because
# remax was recorded at zero pin reach in this program's notes and the location track corrected
# that afterwards. Granularity is reported as a label, never compared by enum order.
CENSUS_PROBE_SOURCE_LOCATION_SQL = """
SELECT
    ll.granularity::text                                              AS granularity,
    count(*)                                                          AS n_listings,
    count(*) FILTER (WHERE ll.geom IS NOT NULL)                       AS n_with_geom,
    count(*) FILTER (WHERE l.is_active)                               AS n_active,
    count(*) FILTER (WHERE l.is_active AND ll.geom IS NOT NULL)       AS n_active_with_geom
FROM listings l
JOIN listing_location ll ON ll.listing_id = l.id
WHERE l.source = %(source)s::text
GROUP BY 1
ORDER BY 2 DESC
"""

# B5. Corpus-wide, run once: the ingest rate L that every monthly cost number scales with,
# measured instead of inferred from an images-per-day ratio. Totals are summed in Python.
# NO listings(first_seen_at) index exists (every `create index ... on listings` in migrations/
# is accounted for; the (first_seen_at, id) keyset index is on PROPERTIES, migration 198), so
# the predicate shape is index-friendly but this is a seq scan of `listings` - bounded by the
# statement timeout, and worth dispatching off-peak. Adding that index is out of scope: ruling
# D8 keeps this program off DDL on the hot shared tables.
CENSUS_PROBE_INGEST_SQL = """
SELECT
    l.source                                                          AS source,
    count(*) FILTER (WHERE l.first_seen_at > now() - interval '1 day')   AS n_1d,
    count(*) FILTER (WHERE l.first_seen_at > now() - interval '7 days')  AS n_7d
FROM listings l
WHERE l.first_seen_at > now() - interval '7 days'
GROUP BY 1
ORDER BY 3 DESC
"""


@dataclass(frozen=True)
class Probe:
    """One probe statement: its payload key, and whether it returns rows or one row."""

    name: str
    sql: str
    many: bool


BLOCK_PROBES: tuple[Probe, ...] = (
    Probe("portal_signal", CENSUS_PROBE_PORTAL_SIGNAL_SQL, True),
    Probe("reposts", CENSUS_PROBE_REPOSTS_SQL, False),
    Probe("address_density", CENSUS_PROBE_ADDRESS_DENSITY_SQL, False),
    Probe("shared_hash", CENSUS_PROBE_SHARED_HASH_SQL, False),
)

CORPUS_PROBES: tuple[Probe, ...] = (
    Probe("source_location", CENSUS_PROBE_SOURCE_LOCATION_SQL, True),
    Probe("ingest_rate", CENSUS_PROBE_INGEST_SQL, True),
)

# `probes set=<name>`: fixed, versioned query sets only; args never carry SQL.
PROBE_SETS: dict[str, tuple[Probe, ...]] = {
    "corpus": CORPUS_PROBES,
    readiness_sql.SET_NAME: tuple(Probe(q.name, q.sql, True) for q in readiness_sql.READINESS_V1),
}

BLOCK_SQL_BY_GRAIN: dict[str, str] = {
    "town": CENSUS_TOWNS_SQL,
    "quarter": CENSUS_QUARTERS_SQL,
}


def sql_placeholders(sql: str) -> set[str]:
    """The `%(name)s` parameter names a statement binds."""
    return set(_PLACEHOLDER.findall(sql))


def block_params(*, min_n: int, max_n: int, top: int) -> dict[str, int]:
    return {"min_n": min_n, "max_n": max_n, "top": top}


def _grain_codes(grain: str, block_code: int) -> dict[str, Any]:
    if grain not in GRAINS:
        raise ValueError(f"unknown grain {grain!r}; use one of {', '.join(GRAINS)}")
    return {
        "obec_kod": block_code if grain == "town" else None,
        "cast_obce_kod": block_code if grain == "quarter" else None,
    }


def detail_params(*, grain: str, block_code: int, pin_share_min: int) -> dict[str, Any]:
    return {**_grain_codes(grain, block_code), "pin_share_min": pin_share_min}


def probe_params(*, grain: str, block_code: int, hash_share_min: int) -> dict[str, Any]:
    """The superset every per-block probe binds from; psycopg looks up only the names the
    statement names, so a probe that wants no threshold simply never reads it."""
    return {**_grain_codes(grain, block_code), "hash_share_min": hash_share_min}


def corpus_probe_params(probe: Probe, *, source: str) -> dict[str, Any]:
    """Corpus probes take at most the portal name, so bind it only where it appears."""
    return {"source": source} if "source" in sql_placeholders(probe.sql) else {}


# --- selection score -------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreTargets:
    min_sources: int = 6
    min_per_category: int = 30
    min_image_share: float = 0.7
    size_min: int = 1500
    size_max: int = 5000


@dataclass(frozen=True)
class BlockScore:
    total: float
    sources: float
    categories: float
    types: float
    images: float
    size: float
    pin_penalty: float

    def as_dict(self) -> dict[str, float]:
        return {
            "total": self.total,
            "sources": self.sources,
            "categories": self.categories,
            "types": self.types,
            "images": self.images,
            "size": self.size,
            "pin_penalty": self.pin_penalty,
        }


WEIGHTS: dict[str, float] = {
    "sources": 0.25,
    "categories": 0.25,
    "types": 0.15,
    "images": 0.15,
    "size": 0.20,
    "pin_penalty": -0.20,
}


def _num(block: dict[str, Any], key: str) -> float:
    value = block.get(key)
    return 0.0 if value is None else float(value)


def score_block(block: dict[str, Any], targets: ScoreTargets = ScoreTargets()) -> BlockScore:
    """Rank a censused block as a dedup trial region. Pure: no DB, no clock."""
    n_total = _num(block, "n_total")

    sources = min(_num(block, "n_sources") / targets.min_sources, 1.0)

    present = sum(
        1 for cat in REQUIRED_CATEGORY_MAIN if _num(block, f"cat_{cat}") >= targets.min_per_category
    )
    categories = present / len(REQUIRED_CATEGORY_MAIN)

    types = 1.0 if _num(block, "type_prodej") > 0 and _num(block, "type_pronajem") > 0 else 0.0

    with_images = block.get("n_listings_with_images")
    if with_images is None or n_total <= 0:
        images = 0.0
    else:
        images = min((float(with_images) / n_total) / targets.min_image_share, 1.0)

    if n_total <= 0:
        size = 0.0
    elif n_total < targets.size_min:
        size = n_total / targets.size_min
    elif n_total > targets.size_max:
        size = targets.size_max / n_total
    else:
        size = 1.0

    pin_penalty = min(_num(block, "shared_pin_listings") / n_total, 1.0) if n_total > 0 else 0.0

    total = (
        WEIGHTS["sources"] * sources
        + WEIGHTS["categories"] * categories
        + WEIGHTS["types"] * types
        + WEIGHTS["images"] * images
        + WEIGHTS["size"] * size
        + WEIGHTS["pin_penalty"] * pin_penalty
    )
    return BlockScore(
        total=max(0.0, min(1.0, total)),
        sources=sources,
        categories=categories,
        types=types,
        images=images,
        size=size,
        pin_penalty=pin_penalty,
    )


# --- rendering -------------------------------------------------------------------------

TABLE_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("grain", "grain", 7),
    ("block_code", "code", 9),
    ("block_name", "name", 22),
    ("n_total", "total", 7),
    ("n_active", "active", 7),
    ("n_sources", "src", 4),
    ("cat_byt", "byt", 7),
    ("cat_dum", "dum", 7),
    ("cat_pozemek", "pozem", 7),
    ("cat_komercni", "komer", 7),
    ("type_prodej", "prodej", 7),
    ("type_pronajem", "najem", 7),
    ("with_area", "area", 7),
    ("with_street", "street", 7),
    ("n_listings_with_images", "imgs", 7),
    ("n_listings_with_clip", "clip", 7),
    ("shared_pin_listings", "pinsh", 7),
    ("raw_inblock_pairs", "pairs", 13),
    ("score", "score", 6),
)


def _cell(value: Any, width: int) -> str:
    if value is None:
        text = "-"
    elif isinstance(value, float):
        text = f"{value:.3f}"
    else:
        text = str(value)
    if len(text) > width:
        text = text[: width - 1] + "…"
    return text.ljust(width)


Columns = Sequence[tuple[str, str, int]]

# The probes get their own table rather than more columns on the census table: nineteen columns
# is already the width of a terminal, and the two readings are answers to different questions.
PROBE_TABLE_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("grain", "grain", 7),
    ("block_code", "code", 9),
    ("block_name", "name", 22),
    ("n_total", "total", 7),
    ("with_desc200", "desc200", 7),
    ("with_source_url", "srcurl", 7),
    ("no_signal_at_all", "nosig", 7),
    ("n_portals", "portal", 6),
    ("repost_pairs", "repair", 7),
    ("repost_pairs_cross_type", "repxt", 6),
    ("repost_listings", "replist", 7),
    ("n_addressed_listings", "addrok", 7),
    ("n_addr_3plus_multi_floor", "addr3f", 7),
    ("n_addr_dense_listings", "addrls", 7),
    ("n_shared_hashes", "hashgr", 7),
    ("n_shared_hash_listings", "hashls", 7),
    ("largest_hash_group", "hashmx", 7),
)

PORTAL_SIGNAL_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("source", "source", 13),
    ("n_listings", "listings", 9),
    ("n_with_images", "imgs", 9),
    ("n_with_phash", "phash", 9),
    ("n_with_clip", "clip", 9),
    ("median_images", "med_img", 9),
)

SOURCE_LOCATION_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("granularity", "granularity", 18),
    ("n_listings", "listings", 10),
    ("n_with_geom", "geom", 10),
    ("n_active", "active", 10),
    ("n_active_with_geom", "act_geom", 10),
)

INGEST_COLUMNS: tuple[tuple[str, str, int], ...] = (
    ("source", "source", 13),
    ("n_1d", "1d", 9),
    ("n_7d", "7d", 9),
)


def render_table(rows: Sequence[dict[str, Any]], columns: Columns = TABLE_COLUMNS) -> str:
    """Fixed-width ranked table, one line per row."""
    header = " ".join(label.ljust(width) for _, label, width in columns).rstrip()
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(" ".join(_cell(row.get(key), width) for key, _, width in columns).rstrip())
    return "\n".join(lines)


def _section(probes: dict[str, Any], name: str) -> dict[str, Any]:
    value = probes.get(name)
    return value if isinstance(value, dict) else {}


def probe_row(block: dict[str, Any]) -> dict[str, Any]:
    """Flatten one block's probe payload into a single printable line."""
    probes = block.get("probes") or {}
    portal = probes.get("portal_signal")
    reposts = _section(probes, "reposts")
    addresses = _section(probes, "address_density")
    hashes = _section(probes, "shared_hash")
    return {
        "grain": block.get("grain"),
        "block_code": block.get("block_code"),
        "block_name": block.get("block_name"),
        "n_total": block.get("n_total"),
        "with_desc200": block.get("with_desc200"),
        "with_source_url": block.get("with_source_url"),
        "no_signal_at_all": block.get("no_signal_at_all"),
        "n_portals": len(portal) if isinstance(portal, list) else None,
        "repost_pairs": reposts.get("repost_pairs"),
        "repost_pairs_cross_type": reposts.get("repost_pairs_cross_type"),
        "repost_listings": reposts.get("repost_listings"),
        "n_addressed_listings": addresses.get("n_addressed_listings"),
        "n_addr_3plus_multi_floor": addresses.get("n_addr_3plus_multi_floor"),
        "n_addr_dense_listings": addresses.get("n_addr_dense_listings"),
        "n_shared_hashes": hashes.get("n_shared_hashes"),
        "n_shared_hash_listings": hashes.get("n_shared_hash_listings"),
        "largest_hash_group": hashes.get("largest_hash_group"),
    }


def render_corpus_probes(probes: dict[str, Any], *, source: str) -> str:
    """The two corpus-wide probes as two small tables."""
    ingest = probes.get("ingest_rate")
    location = probes.get("source_location")
    parts = [
        "ingest rate (listings first seen, by source)",
        render_table(ingest if isinstance(ingest, list) else [], INGEST_COLUMNS),
        "",
        f"location posture: {source}",
        render_table(location if isinstance(location, list) else [], SOURCE_LOCATION_COLUMNS),
    ]
    return "\n".join(parts)


# --- DB layer --------------------------------------------------------------------------


def _rows_as_dicts(cur: Any) -> list[dict[str, Any]]:
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


# Every census/probes/town statement runs under it, so a write is refused by the database
# itself (SQLSTATE 25006), not merely absent from the code.
READ_ONLY_GUARD = "SET TRANSACTION READ ONLY"


def _run(conn: Any, sql: str, params: dict[str, Any], timeout_ms: int) -> list[dict[str, Any]]:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(READ_ONLY_GUARD)
            cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            cur.execute(sql, params)
            return _rows_as_dicts(cur)


def fetch_coverage(conn: Any, *, timeout_ms: int) -> dict[str, Any]:
    rows = _run(conn, CENSUS_COVERAGE_SQL, {}, timeout_ms)
    return rows[0] if rows else {}


def fetch_blocks(
    conn: Any, *, grain: str, min_n: int, max_n: int, top: int, timeout_ms: int
) -> list[dict[str, Any]]:
    sql = BLOCK_SQL_BY_GRAIN[grain]
    rows = _run(conn, sql, block_params(min_n=min_n, max_n=max_n, top=top), timeout_ms)
    for row in rows:
        row["grain"] = grain
    return rows


def fetch_detail(
    conn: Any, *, grain: str, block_code: int, pin_share_min: int, timeout_ms: int
) -> dict[str, Any]:
    params = detail_params(grain=grain, block_code=block_code, pin_share_min=pin_share_min)
    rows = _run(conn, CENSUS_DETAIL_SQL, params, timeout_ms)
    return rows[0] if rows else {}


def _probe_payload(probe: Probe, rows: list[dict[str, Any]]) -> Any:
    if probe.many:
        return rows
    return rows[0] if rows else {}


def fetch_block_probes(
    conn: Any, *, grain: str, block_code: int, hash_share_min: int, timeout_ms: int
) -> dict[str, Any]:
    """Run every per-block probe. One probe that times out is recorded on itself, never
    thrown: a block with three of four probes still tells the operator more than a void run."""
    params = probe_params(grain=grain, block_code=block_code, hash_share_min=hash_share_min)
    out: dict[str, Any] = {}
    for probe in BLOCK_PROBES:
        try:
            rows = _run(conn, probe.sql, params, timeout_ms)
        except Exception as exc:  # noqa: BLE001 — one probe must not void the block
            out[probe.name] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        out[probe.name] = _probe_payload(probe, rows)
    return out


# --- mode entry ------------------------------------------------------------------------

TIMEOUT_S_MAX = 900

ARG_DEFAULTS: dict[str, int] = {
    "min_n": 800,
    "max_n": 8000,
    "top": 60,
    "detail_top": 12,
    "timeout_s": 540,
    "pin_share_min": 10,
    # The per-block probes are on by default: they are the numbers the region is chosen on.
    # `probes=0` is the escape hatch for a census re-run that only needs the block layer.
    "probes": 1,
    "hash_share_min": 5,
}

PROBES_ARG_DEFAULTS: dict[str, Any] = {
    "timeout_s": 540,
    "source": "remax",
    "set": "corpus",
}


def parse_census_args(args: dict[str, str]) -> dict[str, int]:
    """Coerce the lane's k=v strings into the census parameters. Unknown keys are fatal."""
    unknown = sorted(set(args) - set(ARG_DEFAULTS))
    if unknown:
        raise ValueError(
            f"unknown census arg(s) {', '.join(unknown)}; known: {', '.join(sorted(ARG_DEFAULTS))}"
        )
    out = dict(ARG_DEFAULTS)
    for key, raw in args.items():
        try:
            out[key] = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"census arg {key} must be an integer, got {raw!r}") from None
    if out["min_n"] > out["max_n"]:
        raise ValueError("min_n must not exceed max_n")
    for key in ("top", "detail_top", "timeout_s"):
        if out[key] <= 0:
            raise ValueError(f"census arg {key} must be positive")
    if out["probes"] not in (0, 1):
        raise ValueError("census arg probes must be 0 or 1")
    if out["hash_share_min"] < 2:
        raise ValueError("census arg hash_share_min must be at least 2")
    # timeout_s is the one workflow-supplied value that reaches SQL as text (SET LOCAL takes no
    # parameter), so it is clamped here as well as coerced.
    if not 1 <= out["timeout_s"] <= TIMEOUT_S_MAX:
        raise ValueError(f"census arg timeout_s must be between 1 and {TIMEOUT_S_MAX}")
    return out


def parse_probes_args(args: dict[str, str]) -> dict[str, Any]:
    """Coerce the lane's k=v strings into the corpus-probe parameters. Unknown keys are fatal."""
    unknown = sorted(set(args) - set(PROBES_ARG_DEFAULTS))
    if unknown:
        raise ValueError(
            f"unknown probes arg(s) {', '.join(unknown)}; "
            f"known: {', '.join(sorted(PROBES_ARG_DEFAULTS))}"
        )
    out: dict[str, Any] = dict(PROBES_ARG_DEFAULTS)
    out.update(args)
    try:
        out["timeout_s"] = int(out["timeout_s"])
    except (TypeError, ValueError):
        raise ValueError(
            f"probes arg timeout_s must be an integer, got {out['timeout_s']!r}"
        ) from None
    if not 1 <= out["timeout_s"] <= TIMEOUT_S_MAX:
        raise ValueError(f"probes arg timeout_s must be between 1 and {TIMEOUT_S_MAX}")
    if out["source"] not in SOURCES:
        raise ValueError(f"probes arg source must be one of {', '.join(SOURCES)}")
    if out["set"] not in PROBE_SETS:
        raise ValueError(f"probes arg set must be one of {', '.join(sorted(PROBE_SETS))}")
    return out


def _run_readiness(
    conn_factory: Callable[[], Any], params: dict[str, Any], out_dir: Any
) -> dict[str, Any]:
    """One named read-only query set into `out/probes/<set>.json`, landed after every query."""
    name = str(params["set"])
    timeout_ms = int(params["timeout_s"]) * 1000
    timings: dict[str, float] = {}
    probes: dict[str, Any] = {}
    errors: list[str] = []
    out_path = Path(out_dir) / "probes" / f"{name}.json"
    payload: dict[str, Any] = {
        "set": name,
        "trial_blocks": list(readiness_sql.TRIAL_BLOCKS),
        "parameters": params,
        "queries": {
            q.name: {"why": q.why, "drives": q.drives} for q in readiness_sql.READINESS_V1
        },
        "timings": timings,
        "probes": probes,
        "errors": errors,
        "headline": {},
    }

    conn = conn_factory()
    try:
        for probe in PROBE_SETS[name]:
            started = time.monotonic()
            try:
                probes[probe.name] = _run(conn, probe.sql, {}, timeout_ms)
            except Exception as exc:  # noqa: BLE001 — one query must not void the set
                errors.append(f"{probe.name}: {type(exc).__name__}: {exc}")
            timings[probe.name] = round(time.monotonic() - started, 3)
            write_json(out_path, payload)
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()

    payload["headline"] = readiness_sql.headline(probes)
    write_json(out_path, payload)
    for probe in PROBE_SETS[name]:
        rows = probes.get(probe.name)
        shape = f"{len(rows)} rows" if isinstance(rows, list) else "FAILED"
        print(f"{probe.name:<34} {shape:>10} {timings.get(probe.name, 0):>9.1f}s")
    return {
        "set": name,
        "queries": len(PROBE_SETS[name]),
        "succeeded": len(probes),
        "headline": payload["headline"],
        "errors": errors,
        "timings": timings,
        "probes_json": str(out_path),
    }


def run_probes(
    conn_factory: Callable[[], Any], args: dict[str, str], out_dir: Any
) -> dict[str, Any]:
    """The corpus-wide probes (B2 location posture, B5 ingest rate). Block-independent, so they
    run once in their own mode rather than once per censused block."""
    params = parse_probes_args(args)
    if params["set"] != "corpus":
        return _run_readiness(conn_factory, params, out_dir)
    timeout_ms = int(params["timeout_s"]) * 1000
    source = str(params["source"])
    timings: dict[str, float] = {}
    probes: dict[str, Any] = {}
    errors: list[str] = []

    out_path = Path(out_dir) / "probes.json"
    payload: dict[str, Any] = {
        "parameters": params,
        "timings": timings,
        "probes": probes,
        "errors": errors,
        "table": "",
    }

    def flush() -> None:
        write_json(out_path, payload)

    conn = conn_factory()
    try:
        for probe in CORPUS_PROBES:
            started = time.monotonic()
            try:
                rows = _run(conn, probe.sql, corpus_probe_params(probe, source=source), timeout_ms)
            except Exception as exc:  # noqa: BLE001 — one probe must not void the other
                errors.append(f"{probe.name}: {type(exc).__name__}: {exc}")
                flush()
                continue
            probes[probe.name] = _probe_payload(probe, rows)
            timings[f"{probe.name}_s"] = round(time.monotonic() - started, 3)
            flush()
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()

    ingest = probes.get("ingest_rate")
    ingest_rows = ingest if isinstance(ingest, list) else []
    totals = {
        "n_1d": sum(int(row.get("n_1d") or 0) for row in ingest_rows),
        "n_7d": sum(int(row.get("n_7d") or 0) for row in ingest_rows),
    }
    probes["ingest_totals"] = totals

    table = render_corpus_probes(probes, source=source)
    print(table)
    payload["table"] = table
    flush()
    return {
        "source": source,
        "ingest_1d": totals["n_1d"],
        "ingest_7d": totals["n_7d"],
        "granularities": len(probes.get("source_location") or []),
        "errors": errors,
        "timings": timings,
        "probes_json": str(out_path),
    }


def run_census(
    conn_factory: Callable[[], Any], args: dict[str, str], out_dir: Any
) -> dict[str, Any]:
    params = parse_census_args(args)
    timeout_ms = params["timeout_s"] * 1000
    timings: dict[str, float] = {}
    grains: dict[str, list[dict[str, Any]]] = {}
    coverage: dict[str, Any] = {}
    errors: list[str] = []

    out_path = Path(out_dir) / "census.json"
    payload: dict[str, Any] = {
        "parameters": params,
        "timings": timings,
        "coverage": coverage,
        "grains": grains,
        "errors": errors,
        "ranking": [],
        "table": "",
    }

    def flush() -> None:
        write_json(out_path, payload)

    conn = conn_factory()
    try:
        started = time.monotonic()
        try:
            coverage.update(fetch_coverage(conn, timeout_ms=timeout_ms))
        except Exception as exc:  # noqa: BLE001 — a denominator is nice-to-have, not the census
            errors.append(f"coverage: {type(exc).__name__}: {exc}")
        timings["coverage_s"] = round(time.monotonic() - started, 3)
        flush()

        for grain in GRAINS:
            started = time.monotonic()
            blocks = fetch_blocks(
                conn,
                grain=grain,
                min_n=params["min_n"],
                max_n=params["max_n"],
                top=params["top"],
                timeout_ms=timeout_ms,
            )
            timings[f"blocks_{grain}_s"] = round(time.monotonic() - started, 3)
            grains[grain] = blocks
            # Each grain's pass costs minutes of production DB time; land it before spending
            # more, so a later timeout cannot throw away what already succeeded.
            flush()

        detail_started = time.monotonic()
        for grain, blocks in grains.items():
            for block in blocks[: params["detail_top"]]:
                block["has_detail"] = False
                try:
                    detail = fetch_detail(
                        conn,
                        grain=grain,
                        block_code=block["block_code"],
                        pin_share_min=params["pin_share_min"],
                        timeout_ms=timeout_ms,
                    )
                except Exception as exc:  # noqa: BLE001 — one slow block must not void the rest
                    block["detail_error"] = f"{type(exc).__name__}: {exc}"
                    errors.append(f"detail {grain}/{block['block_code']}: {block['detail_error']}")
                else:
                    block.update(detail)
                    block["has_detail"] = bool(detail)
                # The probes do not read the detail result, and the detail statement is the
                # heaviest one in this loop — so a detail timeout must not take down the four
                # numbers the region is actually chosen on.
                if params["probes"]:
                    block["probes"] = fetch_block_probes(
                        conn,
                        grain=grain,
                        block_code=block["block_code"],
                        hash_share_min=params["hash_share_min"],
                        timeout_ms=timeout_ms,
                    )
                    errors.extend(
                        f"probe {name} {grain}/{block['block_code']}: {value['error']}"
                        for name, value in block["probes"].items()
                        if isinstance(value, dict) and "error" in value
                    )
                # Five statements per block now, each with the full budget: land every block
                # as it completes so hitting the job's wall clock cannot discard the lot.
                flush()
        timings["detail_s"] = round(time.monotonic() - detail_started, 3)
        flush()
    finally:
        close = getattr(conn, "close", None)
        if callable(close):
            close()

    ranking: list[dict[str, Any]] = []
    for blocks in grains.values():
        for block in blocks:
            if not block.get("has_detail"):
                continue
            score = score_block(block)
            block["score"] = score.total
            block["score_parts"] = score.as_dict()
            ranking.append(block)
    ranking.sort(key=lambda b: (-float(b["score"]), -float(b["n_total"] or 0)))

    table = render_table(ranking)
    print(table)

    # Ranked blocks first, then any probed block the detail statement dropped from the
    # ranking — a probe-only line still answers the questions the probes were added for.
    probed = [block for block in ranking if block.get("probes")]
    seen_probed = {id(block) for block in probed}
    for blocks in grains.values():
        for block in blocks:
            if block.get("probes") and id(block) not in seen_probed:
                probed.append(block)
    probe_rows = [probe_row(block) for block in probed]
    probe_table = render_table(probe_rows, PROBE_TABLE_COLUMNS) if probe_rows else ""
    if probe_table:
        print()
        print(probe_table)

    payload["ranking"] = [
        {
            "grain": b["grain"],
            "block_code": b["block_code"],
            "block_name": b["block_name"],
            "score": b["score"],
            "score_parts": b["score_parts"],
        }
        for b in ranking
    ]
    payload["table"] = table
    payload["probe_table"] = probe_table
    flush()
    return {
        "blocks_town": len(grains.get("town", [])),
        "blocks_quarter": len(grains.get("quarter", [])),
        "detailed": len(ranking),
        "probed": len(probe_rows),
        "errors": errors,
        "best": ranking[0]["block_name"] if ranking else None,
        "best_code": ranking[0]["block_code"] if ranking else None,
        "timings": timings,
        "census_json": str(out_path),
    }
