"""Every statement the cohort export runs, as module-level constants (the PREPARE gate).

Read-only, all of it: the export writes nothing into `public.*` (ruling D4) and reads
nothing from `property_merge_events` or any other legacy dedup relation (ruling D7).

PII (E28). `listings` carries three contact columns — `broker_name`, `broker_email`,
`broker_phone` (migration 025). `broker_name` is never selected at all; the other two are
selected ONLY as inputs to the salted `broker_key` digest computed in
`autodedup.export.broker_key` and are dropped before a record is built, so nothing that
identifies a person reaches the artifact. `LISTING_COLUMNS` itself (scraper/db.py) holds no
contact column: everything in it except `description` — which is exported scrubbed — is
non-PII and is exported verbatim.

Nullable parameters carry explicit casts: psycopg sends no type OID for a Python `None`, so
an uncast NULL parameter fails Parse with 42P18 (the shape census.CENSUS_DETAIL_SQL
documents). Id batches travel as `= any(%(ids)s::bigint[])` rather than an IN-list, so one
prepared plan serves every batch.
"""

from __future__ import annotations

# The listing fields promoted to their own key in the artifact's `listing` record. They are
# NOT repeated inside `attrs`; everything else in LISTING_COLUMNS is.
PROMOTED_COLUMNS: tuple[str, ...] = (
    "category_main",
    "category_type",
    "subtype",
    "disposition",
    "area_m2",
    "floor",
    "total_floors",
    "price_czk",
    "source_url",
    "description",
)

# LISTING_COLUMNS (scraper/db.py) minus PROMOTED_COLUMNS: the `attrs` bag, value-or-absent.
ATTR_COLUMNS: tuple[str, ...] = (
    "price_unit",
    "area_basis",
    "has_balcony",
    "has_parking",
    "has_lift",
    "building_type",
    "condition",
    "energy_rating",
    "estate_area",
    "usable_area",
    "garden_area",
    "category_sub_cb",
    "furnished",
    "terrace",
    "cellar",
    "garage",
    "parking_lots",
    "ownership",
    "published_at",
)

# Selected, never exported: the two hash inputs behind `broker_key` (E28).
BROKER_HASH_INPUT_COLUMNS: tuple[str, ...] = ("broker_phone", "broker_email")

# Never selected at all.
EXCLUDED_PII_COLUMNS: tuple[str, ...] = ("broker_name",)


COHORT_TOWN_IDS_SQL = """
SELECT l.id AS listing_id
FROM listings l
JOIN listing_location ll ON ll.listing_id = l.id
WHERE ll.obec_kod = %(code)s::bigint
ORDER BY l.id
"""

COHORT_QUARTER_IDS_SQL = """
SELECT l.id AS listing_id
FROM listings l
JOIN listing_location ll ON ll.listing_id = l.id
WHERE ll.cast_obce_kod = %(code)s::bigint
ORDER BY l.id
"""

# The negative control as W1 BUILDS it: Praha address points (minus the dense quarter,
# which is its own block) carrying >= 3 listings across >= 2 distinct floors — the
# same-building-different-unit class the engine must never merge.
#
# This is NOT verbatim ruling D1, which states the stratum as a LABEL over blocks 1-3
# ("every listing in blocks 1–3 that either shares a ruian_adm_kod with another listing at a
# different floor, or carries a dHash appearing on >= 5 distinct listings"). W1 builds the
# geographic draw instead, on the program lead's build instruction for this wave; the dHash
# arm is not sampled here. The divergence is deliberate and open: it needs a ruling line in
# PROGRAM.md section 14 before W1 closes, and until then nothing should cite D1 for this
# shape.
COHORT_NEGCTL_GROUPS_SQL = """
SELECT
    ll.ruian_adm_kod              AS ruian_adm_kod,
    count(*)                      AS n_listings,
    count(DISTINCT l.floor)       AS n_floors,
    array_agg(l.id ORDER BY l.id) AS listing_ids
FROM listings l
JOIN listing_location ll ON ll.listing_id = l.id
WHERE ll.obec_kod = %(obec_kod)s::bigint
  AND ll.ruian_adm_kod IS NOT NULL
  AND (%(exclude_cast_obce_kod)s::bigint IS NULL
       OR ll.cast_obce_kod IS DISTINCT FROM %(exclude_cast_obce_kod)s::bigint)
GROUP BY ll.ruian_adm_kod
HAVING count(*) >= %(min_group_size)s::int
   AND count(DISTINCT l.floor) >= %(min_distinct_floors)s::int
ORDER BY count(*) DESC, ll.ruian_adm_kod
"""

COHORT_LISTINGS_SQL = """
SELECT
    l.id                  AS id,
    l.source              AS source,
    l.source_id_native    AS source_id_native,
    l.source_url          AS source_url,
    l.category_main       AS category_main,
    l.category_type       AS category_type,
    l.subtype             AS subtype,
    l.disposition         AS disposition,
    l.area_m2             AS area_m2,
    l.floor               AS floor,
    l.total_floors        AS total_floors,
    l.price_czk           AS price_czk,
    l.price_unit          AS price_unit,
    l.area_basis          AS area_basis,
    l.has_balcony         AS has_balcony,
    l.has_parking         AS has_parking,
    l.has_lift            AS has_lift,
    l.building_type       AS building_type,
    l.condition           AS condition,
    l.energy_rating       AS energy_rating,
    l.estate_area         AS estate_area,
    l.usable_area         AS usable_area,
    l.garden_area         AS garden_area,
    l.category_sub_cb     AS category_sub_cb,
    l.furnished           AS furnished,
    l.terrace             AS terrace,
    l.cellar              AS cellar,
    l.garage              AS garage,
    l.parking_lots        AS parking_lots,
    l.ownership           AS ownership,
    l.published_at        AS published_at,
    l.description         AS description,
    l.first_seen_at       AS first_seen_at,
    l.last_seen_at        AS last_seen_at,
    l.inactive_at         AS inactive_at,
    l.is_active           AS is_active,
    l.broker_identity_id  AS broker_identity_id,
    l.broker_firm_id      AS broker_firm_id,
    l.broker_phone        AS broker_phone,
    l.broker_email        AS broker_email
FROM listings l
WHERE l.id = any(%(ids)s::bigint[])
ORDER BY l.id
"""

# Location is served from ONE store (migration 501); the listing-level columns are gone
# (508). `granularity` and `country_status` are enum-typed, so they are cast to text rather
# than compared or ordered as ordinals; the rank comes from the ranking relation, never from
# the enum's declaration order.
COHORT_LOCATION_SQL = """
SELECT
    ll.listing_id            AS listing_id,
    ll.obec_kod              AS obec_kod,
    ll.obec_name             AS obec_name,
    ll.cast_obce_kod         AS cast_obce_kod,
    ll.cast_obce_name        AS cast_obce_name,
    ll.granularity::text     AS granularity,
    gr.rank                  AS granularity_rank,
    gr.is_address_grain      AS is_address_grain,
    ST_Y(ll.geom)            AS lat,
    ST_X(ll.geom)            AS lon,
    ll.uncertainty_radius_m  AS uncertainty_radius_m,
    ll.street_name           AS street_name,
    ll.house_number_cp       AS house_number_cp,
    ll.house_number_co       AS house_number_co,
    ll.psc                   AS psc,
    ll.ruian_adm_kod         AS ruian_adm_kod,
    ll.country_status::text  AS country_status
FROM listing_location ll
LEFT JOIN location_granularity_rank gr ON gr.granularity = ll.granularity
WHERE ll.listing_id = any(%(ids)s::bigint[])
"""

# The price path (E19). Snapshots are append-only and keyed on the surrogate `listing_id`
# (migration 333's index), so this is an index range scan per listing; the change events
# themselves are derived in Python, which keeps the "distinct consecutive change" rule in
# one testable place.
COHORT_PRICE_HISTORY_SQL = """
SELECT
    s.listing_id  AS listing_id,
    s.scraped_at  AS scraped_at,
    s.price_czk   AS price_czk
FROM listing_snapshots s
WHERE s.listing_id = any(%(ids)s::bigint[])
  AND s.price_czk IS NOT NULL
ORDER BY s.listing_id, s.scraped_at
"""

COHORT_IMAGES_SQL = """
SELECT
    i.id           AS image_id,
    i.listing_id   AS listing_id,
    i.sequence     AS sequence,
    i.storage_path AS storage_path,
    i.phash        AS phash
FROM images i
WHERE i.listing_id = any(%(ids)s::bigint[])
ORDER BY i.listing_id, i.sequence NULLS LAST, i.id
"""

# pgvector's text output is `[0.1,0.2,…]`; `::text` makes that explicit rather than relying
# on whether a vector adapter happens to be registered in this process.
COHORT_CLIP_SQL = """
SELECT
    e.image_id          AS image_id,
    e.embedding::text   AS embedding
FROM image_clip_embeddings e
WHERE e.image_id = any(%(ids)s::bigint[])
  AND e.model = %(model)s
"""

COHORT_CLIP_COUNT_SQL = """
SELECT count(*) AS n
FROM image_clip_embeddings e
WHERE e.image_id = any(%(ids)s::bigint[])
  AND e.model = %(model)s
"""

# `image_clip_tags` is keyed (image_id, model) just like the embeddings, and
# `scripts/clip_tag_backfill.py` writes both tables under the SAME model key, so scoping the
# tags by the model the vectors came from keeps one image to one undiscriminated tag list.
COHORT_CLIP_TAGS_SQL = """
SELECT
    t.image_id     AS image_id,
    t.fine_tag     AS fine_tag,
    t.logical_tag  AS logical_tag,
    t.confidence   AS confidence
FROM image_clip_tags t
WHERE t.image_id = any(%(ids)s::bigint[])
  AND t.model = %(model)s
"""

# Catalog-photo subtraction (E9) needs the CORPUS-WIDE count of listings sharing a hash, not
# the cohort's — a developer catalogue spans blocks. There is no index on `images.phash` and
# ruling D8 forbids adding one (no DDL on a shared hot table), so this is one deliberate
# sequential scan run ONCE per export under a generous statement timeout, and its wall time
# is recorded in the run summary so the rollout conversation has the real number.
COHORT_PHASH_POP_SQL = """
SELECT
    i.phash                     AS phash,
    count(DISTINCT i.listing_id) AS n_listings
FROM images i
WHERE i.phash = any(%(hashes)s::bigint[])
GROUP BY i.phash
"""
