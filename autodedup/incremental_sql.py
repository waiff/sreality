"""Every statement the `incremental` lane runs, as module-level constants (the PREPARE gate).

Two halves, and the line between them is ruling D4. The `public.*` statements are READS and
only reads: three index-served watermark cursors and two id-keyed fact fetches. Everything
written is inside schema `autodedup` (migrations 528, 538, 539).

**Why the probes are served from `autodedup.fp_key` and not from `public.listings`.** Each of
the six probes keys on a DERIVED value — a 16-bit band of the description SimHash, a 16-bit
band of an image pHash, `floor(ln(area_m2)/w)`, the cohort price decile, a grain-prefixed
obec/část code, a provenance-tagged house number. None of these is a column of
`public.listings`, so no index on that table could serve one however it were written, and
ruling D8 forbids adding one to a shared hot table. The side table is therefore the design, not
a fallback: it is written only by this lane, keyed by generation, and every lookup is its own
primary key. EXPLAIN against the live database confirms the three `public` cursors are index
scans (`listings_pkey`, `listing_snapshots_pkey`, `listings_inactive_at_idx`) and that the
gallery fetch rides `images_listing_id_idx`.

Nullable parameters carry explicit casts throughout: psycopg sends no type OID for a Python
`None`, so an uncast NULL parameter fails Parse with 42P18.
"""

from __future__ import annotations

# The lane refuses to start without its own store; unlike the progress ledger, persistence IS
# the deliverable.
RT_STORE_PRESENT_SQL = """
select to_regclass('autodedup.fp_key')         is not null
   and to_regclass('autodedup.rt_calibration') is not null
   and to_regclass('autodedup.rt_block_cell')  is not null
   and to_regclass('autodedup.rt_lease')       is not null as present
"""

# ------------------------------------------------------------------ mutual exclusion
#
# Lease-row CAS, never pg_advisory_lock: a session lock strands over the transaction pooler.
# The update is the claim — one statement, no read-then-write race — and a lease is taken only
# when the current one has expired.
RT_LEASE_TAKE_SQL = """
insert into autodedup.rt_lease (name, holder, taken_at, expires_at)
values (%(name)s::text, %(holder)s::text, now(), now() + make_interval(secs => %(ttl)s))
on conflict (name) do update set
    holder     = excluded.holder,
    taken_at   = now(),
    expires_at = excluded.expires_at
 where autodedup.rt_lease.expires_at < now()
returning holder
"""

RT_LEASE_RELEASE_SQL = """
update autodedup.rt_lease
   set expires_at = now()
 where name = %(name)s::text
   and holder = %(holder)s::text
"""

# ------------------------------------------------------------------ the watermark feeds
#
# THREE cursors, all index-served, all read-only (D4):
#   * new       — `listings_pkey`, and `id` is a bigserial so it is also the arrival order.
#   * changed   — `listing_snapshots_pkey`. Rule #2 makes that table an append-on-content-change
#                 feed, so it is exactly "a listing whose content moved", with no column of its
#                 own to add and no trigger to install.
#   * delisted  — `listings_inactive_at_idx` (partial, `inactive_at is not null`). An activity
#                 flip moves K-B's disjointness and E46/E47's overlap, so it re-decides.
RT_NEW_LISTINGS_SQL = """
select l.id
  from public.listings l
 where l.id > %(after_id)s::bigint
 order by l.id
 limit %(limit)s
"""

RT_CHANGED_LISTINGS_SQL = """
select s.id, s.listing_id
  from public.listing_snapshots s
 where s.id > %(after_id)s::bigint
 order by s.id
 limit %(limit)s
"""

RT_FLIPPED_LISTINGS_SQL = """
select l.id, l.inactive_at
  from public.listings l
 where l.inactive_at > %(after)s::timestamptz
 order by l.inactive_at
 limit %(limit)s
"""

RT_CURSOR_READ_SQL = """
select name, last_listing_id, last_snapshot_id, watermark
  from autodedup.scan_cursor
 where name = any(%(names)s::text[])
"""

RT_CURSOR_WRITE_SQL = """
insert into autodedup.scan_cursor (name, last_listing_id, last_snapshot_id, watermark,
                                   updated_at)
values (%(name)s::text, %(last_listing_id)s::bigint, %(last_snapshot_id)s::bigint,
        %(watermark)s::timestamptz, now())
on conflict (name) do update set
    last_listing_id  = coalesce(excluded.last_listing_id, autodedup.scan_cursor.last_listing_id),
    last_snapshot_id = coalesce(excluded.last_snapshot_id,
                                autodedup.scan_cursor.last_snapshot_id),
    watermark        = coalesce(excluded.watermark, autodedup.scan_cursor.watermark),
    updated_at       = now()
"""

# ------------------------------------------------------------------ facts (read-only)
#
# Id-keyed, so both ride a primary key. Location comes only from `public.listing_location`
# (migration 508 dropped the listing-level columns).
RT_FACTS_SQL = """
select l.id, l.source, l.source_id_native, l.source_url, l.category_main, l.category_type,
       l.subtype, l.disposition, l.area_m2, l.floor, l.total_floors, l.price_czk,
       l.description, l.first_seen_at, l.last_seen_at, l.inactive_at, l.is_active,
       l.broker_identity_id, l.broker_firm_id,
       loc.obec_kod, loc.cast_obce_kod, loc.granularity, loc.lat, loc.lon,
       loc.street_key, loc.house_number, loc.house_number_cp, loc.house_number_co,
       loc.psc, loc.ruian_adm_kod, loc.country_code, loc.country_status
  from public.listings l
  left join public.listing_location loc on loc.listing_id = l.id
 where l.id = any(%(ids)s::bigint[])
"""

# The gallery, with the three things a feature reads off an image beside its pHash.
#
# `pop` — the CORPUS-WIDE count of listings carrying a hash, which E9's catalogue subtraction
# and K-C's `catalog_ratio_max` both read — comes from `autodedup.phash_pop`, NEVER from a
# live count: `public.images.phash` carries no index and ruling D8 forbids adding one, so the
# count is one deliberate sequential scan the COHORT lane runs once per pass. That makes the
# population a cohort statistic like the others, and E65 freezes it with them: a pass that
# recomputed it would silently move `anchor_bands`, and with them the K4 probe and every
# certificate that reads a catalogue ratio.
RT_IMAGES_SQL = """
select i.listing_id, i.id, i.sequence, i.phash, pp.n_listings
  from public.images i
  left join autodedup.phash_pop pp on pp.phash = i.phash
 where i.listing_id = any(%(ids)s::bigint[])
 order by i.listing_id, i.sequence nulls last, i.id
"""

RT_IMAGE_TAGS_SQL = """
select t.image_id, t.fine_tag, t.logical_tag, t.confidence
  from public.image_clip_tags t
 where t.image_id = any(%(ids)s::bigint[])
   and t.model = %(model)s::text
"""

RT_IMAGE_CLIP_SQL = """
select e.image_id, e.embedding::text
  from public.image_clip_embeddings e
 where e.image_id = any(%(ids)s::bigint[])
   and e.model = %(model)s::text
"""

# ------------------------------------------------------------------ probe postings
RT_LOOKUP_SQL = """
select k.listing_id
  from autodedup.fp_key k
 where k.generation = %(generation)s::text
   and k.probe = %(probe)s::text
   and k.key_token = %(key_token)s::text
 order by k.listing_id
"""

RT_KEYS_OF_SQL = """
select k.probe, k.key_token
  from autodedup.fp_key k
 where k.generation = %(generation)s::text
   and k.listing_id = %(listing_id)s::bigint
 order by k.probe, k.key_token
"""

RT_KEY_DELETE_SQL = """
delete from autodedup.fp_key
 where generation = %(generation)s::text
   and listing_id = %(listing_id)s::bigint
"""

RT_KEY_INSERT_SQL = """
insert into autodedup.fp_key (generation, probe, key_token, listing_id)
values (%(generation)s::text, %(probe)s::text, %(key_token)s::text, %(listing_id)s::bigint)
on conflict (generation, probe, key_token, listing_id) do nothing
"""

# ------------------------------------------------------------------ fingerprint row
#
# Only the columns retrieval and the rule floor read. The full fingerprint is rebuilt in
# process from the facts on every pass — it carries token sets, shingles and hash sets that no
# column could serve a probe from, and rebuilding it costs 0.43 ms a listing.
RT_FP_UPSERT_SQL = """
insert into autodedup.listing_fp (
    listing_id, source, category_main, category_type, cat_group, block_key, obec_kod,
    cast_obce_kod, country_status, lat, lon, street_key, house_number_cp, psc, ruian_adm_kod,
    area_m2, area_band, disposition, floor, total_floors, broker_key, broker_identity_id,
    desc_simhash, n_images, first_seen_at, last_seen_at, inactive_at, is_active,
    fp_version, fp_digest, generation, built_at
) values (
    %(listing_id)s::bigint, %(source)s::text, %(category_main)s::text, %(category_type)s::text,
    %(cat_group)s::text, %(block_key)s::bigint, %(obec_kod)s::bigint,
    %(cast_obce_kod)s::bigint, %(country_status)s::text, %(lat)s::double precision,
    %(lon)s::double precision, %(street_key)s::text, %(house_number_cp)s::text, %(psc)s::text,
    %(ruian_adm_kod)s::bigint, %(area_m2)s::numeric, %(area_band)s::integer,
    %(disposition)s::text, %(floor)s::integer, %(total_floors)s::integer,
    %(broker_key)s::text, %(broker_identity_id)s::bigint, %(desc_simhash)s::bigint,
    %(n_images)s::smallint, %(first_seen_at)s::timestamptz, %(last_seen_at)s::timestamptz,
    %(inactive_at)s::timestamptz, %(is_active)s::boolean, %(fp_version)s::smallint,
    %(fp_digest)s::text, %(generation)s::text, now()
)
on conflict (listing_id) do update set
    source             = excluded.source,
    category_main      = excluded.category_main,
    category_type      = excluded.category_type,
    cat_group          = excluded.cat_group,
    block_key          = excluded.block_key,
    obec_kod           = excluded.obec_kod,
    cast_obce_kod      = excluded.cast_obce_kod,
    country_status     = excluded.country_status,
    lat                = excluded.lat,
    lon                = excluded.lon,
    street_key         = excluded.street_key,
    house_number_cp    = excluded.house_number_cp,
    psc                = excluded.psc,
    ruian_adm_kod      = excluded.ruian_adm_kod,
    area_m2            = excluded.area_m2,
    area_band          = excluded.area_band,
    disposition        = excluded.disposition,
    floor              = excluded.floor,
    total_floors       = excluded.total_floors,
    broker_key         = excluded.broker_key,
    broker_identity_id = excluded.broker_identity_id,
    desc_simhash       = excluded.desc_simhash,
    n_images           = excluded.n_images,
    first_seen_at      = excluded.first_seen_at,
    last_seen_at       = excluded.last_seen_at,
    inactive_at        = excluded.inactive_at,
    is_active          = excluded.is_active,
    fp_version         = excluded.fp_version,
    fp_digest          = excluded.fp_digest,
    generation         = excluded.generation,
    built_at           = now()
"""

RT_GUARDS_SQL = """
select f.listing_id, f.category_main, f.category_type, f.area_m2, f.disposition, f.floor,
       f.fp_digest
  from autodedup.listing_fp f
 where f.listing_id = any(%(ids)s::bigint[])
"""

RT_KNOWN_SQL = """
select f.listing_id
  from autodedup.listing_fp f
 where f.generation = %(generation)s::text
"""

# ------------------------------------------------------------------ pair grain
#
# `zone` carries the table's own CHECK (`merge`/`band`/`reject`), so a guard veto is written as
# `reject` with `guard_veto` naming the rule — the shape the score lane already writes.
RT_PAIRS_TOUCHING_SQL = """
select p.listing_lo, p.listing_hi, p.probes, p.from_lo, p.from_hi, p.score, p.zone,
       p.decision, p.guard_veto, p.families, p.evidence, p.context, p.features
  from autodedup.pairs p
 where p.generation = %(generation)s::text
   and (p.listing_lo = any(%(ids)s::bigint[]) or p.listing_hi = any(%(ids)s::bigint[]))
"""

RT_PAIRS_WITHIN_SQL = """
select p.listing_lo, p.listing_hi, p.probes, p.from_lo, p.from_hi, p.score, p.zone,
       p.decision, p.guard_veto, p.families, p.evidence, p.context
  from autodedup.pairs p
 where p.generation = %(generation)s::text
   and p.listing_lo = any(%(ids)s::bigint[])
   and p.listing_hi = any(%(ids)s::bigint[])
 order by p.listing_lo, p.listing_hi
"""

# E67's BFS step: the merge edges out of a frontier, both directions in one statement.
RT_MERGE_NEIGHBOURS_SQL = """
select p.listing_lo as a, p.listing_hi as b
  from autodedup.pairs p
 where p.generation = %(generation)s::text
   and p.zone = 'merge'
   and p.listing_lo = any(%(ids)s::bigint[])
union all
select p.listing_hi as a, p.listing_lo as b
  from autodedup.pairs p
 where p.generation = %(generation)s::text
   and p.zone = 'merge'
   and p.listing_hi = any(%(ids)s::bigint[])
"""

RT_PAIR_UPSERT_SQL = """
insert into autodedup.pairs (
    generation, listing_lo, listing_hi, probes, from_lo, from_hi, families, features, score,
    zone, decision, guard_veto, evidence, context, calibration_digest, feature_version,
    model_version, decided_at
) values (
    %(generation)s::text, %(listing_lo)s::bigint, %(listing_hi)s::bigint, %(probes)s::text[],
    %(from_lo)s::boolean, %(from_hi)s::boolean, %(families)s::smallint, %(features)s::jsonb,
    %(score)s::real, %(zone)s::text, %(decision)s::text, %(guard_veto)s::text,
    %(evidence)s::jsonb, %(context)s::jsonb, %(calibration_digest)s::text,
    %(feature_version)s::smallint, %(model_version)s::text, now()
)
on conflict (generation, listing_lo, listing_hi) do update set
    probes             = excluded.probes,
    from_lo            = excluded.from_lo,
    from_hi            = excluded.from_hi,
    families           = excluded.families,
    features           = excluded.features,
    score              = excluded.score,
    zone               = excluded.zone,
    decision           = excluded.decision,
    guard_veto         = excluded.guard_veto,
    evidence           = excluded.evidence,
    context            = excluded.context,
    calibration_digest = excluded.calibration_digest,
    feature_version    = excluded.feature_version,
    model_version      = excluded.model_version,
    decided_at         = now()
"""

RT_PAIR_DELETE_SQL = """
delete from autodedup.pairs
 where generation = %(generation)s::text
   and listing_lo = %(listing_lo)s::bigint
   and listing_hi = %(listing_hi)s::bigint
"""

# ------------------------------------------------------------------ cluster grain
#
# Scoped to the COMPONENT, not to the generation: E67 rewrites the components it recomputed and
# leaves every other group of the same pass alone, which is what makes a bounded pass safe to
# interrupt.
RT_CLUSTERS_TOUCHING_SQL = """
select m.cluster_key, m.listing_id
  from autodedup.cluster_members m
 where m.generation = %(generation)s::text
   and m.cluster_key in (
         select m2.cluster_key
           from autodedup.cluster_members m2
          where m2.generation = %(generation)s::text
            and m2.listing_id = any(%(ids)s::bigint[])
       )
 order by m.cluster_key, m.listing_id
"""

RT_CLUSTER_DROP_SQL = """
delete from autodedup.clusters
 where generation = %(generation)s::text
   and cluster_key = any(%(keys)s::bigint[])
"""

RT_CLUSTER_MEMBERS_DROP_SQL = """
delete from autodedup.cluster_members
 where generation = %(generation)s::text
   and cluster_key = any(%(keys)s::bigint[])
"""

# ------------------------------------------------------------------ the live census
RT_CELL_READ_SQL = """
select c.cell_key, c.category_group, c.n_listings, c.shapes, c.brokers, c.source_ids, c.capped
  from autodedup.rt_block_cell c
 where c.generation = %(generation)s::text
   and c.cell_key = any(%(keys)s::text[])
"""

RT_CELL_UPSERT_SQL = """
insert into autodedup.rt_block_cell (
    generation, cell_key, category_group, n_listings, shapes, brokers, source_ids, capped,
    updated_at
) values (
    %(generation)s::text, %(cell_key)s::text, %(category_group)s::text, %(n_listings)s::integer,
    %(shapes)s::jsonb, %(brokers)s::jsonb, %(source_ids)s::jsonb, %(capped)s::boolean, now()
)
on conflict (generation, cell_key, category_group) do update set
    n_listings = excluded.n_listings,
    shapes     = excluded.shapes,
    brokers    = excluded.brokers,
    source_ids = excluded.source_ids,
    capped     = excluded.capped,
    updated_at = now()
"""

# ------------------------------------------------------------------ calibration (E65)
RT_CALIBRATION_READ_SQL = """
select c.generation, c.digest, c.n_listings, c.payload, c.artifact_url, c.settings,
       c.model_version, c.built_at
  from autodedup.rt_calibration c
 where c.generation = %(generation)s::text
"""

RT_CALIBRATION_WRITE_SQL = """
insert into autodedup.rt_calibration (generation, digest, n_listings, payload, artifact_url,
                                      settings, model_version, built_at)
values (%(generation)s::text, %(digest)s::text, %(n_listings)s::integer, %(payload)s::jsonb,
        %(artifact_url)s::text, %(settings)s::jsonb, %(model_version)s::text, now())
on conflict (generation) do update set
    digest        = excluded.digest,
    n_listings    = excluded.n_listings,
    payload       = excluded.payload,
    artifact_url  = excluded.artifact_url,
    settings      = excluded.settings,
    model_version = excluded.model_version,
    built_at      = now()
"""

RT_MUST_NOT_LINK_SQL = """
select listing_lo, listing_hi
  from autodedup.must_not_link
"""
