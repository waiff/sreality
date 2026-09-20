"""Every statement the `incremental` lane runs, as module-level constants (the PREPARE gate).

Two halves, and the line between them is ruling D4. The `public.*` statements are READS and
only reads: four bounded watermark feeds and three id-keyed fact fetches. Everything written
is inside schema `autodedup` (migrations 528, 538, 539) — `tests/autodedup/test_incremental.py`
asserts that over this file rather than trusting the reading.

**Why the probes are served from `autodedup.fp_key` and not from `public.listings`.** Each of
the six probes keys on a DERIVED value — a 16-bit band of the description SimHash, a 16-bit
band of an image pHash, `floor(ln(area_m2)/w)`, the cohort price decile, a grain-prefixed
obec/část code, a provenance-tagged house number. None of these is a column of
`public.listings`, so no index on that table could serve one however it were written, and
ruling D8 forbids adding one to a shared hot table. The side table is therefore the design, not
a fallback: it is written only by this lane, keyed by generation, and every lookup is its own
primary key. EXPLAIN against the live database confirms the `public` cursors are index scans
(`listings_pkey`, `listing_snapshots_pkey`, `listings_inactive_at_idx`) and that the gallery
fetch rides `images_listing_id_idx`.

**Why the feeds carry a settle LAG and a keyset tie-break (E68).** `id` is assigned at INSERT
and not at COMMIT, so two overlapping batch transactions can commit out of id order and a bare
`id > cursor` watermark would step over the slower one; `inactive_at` is `now()` — the
TRANSACTION timestamp — so one `mark_inactive` batch shares one stamp, and a `limit` that cuts
a tie group would skip its tail forever (measured live: groups of 5,534 / 3,341 / 3,062 flips
at one identical timestamp). Every feed therefore (a) ignores rows younger than a settle lag,
(b) pages on the FULL key — `(inactive_at, id)`, never `inactive_at` alone — and (c) the
new-row feed carries an anti-join straggler sweep over the last `straggler_window` ids, which
is what proves a row was not missed rather than assuming it.

Nullable parameters carry explicit casts throughout: psycopg sends no type OID for a Python
`None`, so an uncast NULL parameter fails Parse with 42P18.
"""

from __future__ import annotations

# The lane refuses to start without its own store; unlike the progress ledger, persistence IS
# the deliverable.
RT_STORE_PRESENT_SQL = """
select to_regclass('autodedup.fp_key')         is not null
   and to_regclass('autodedup.rt_fp')          is not null
   and to_regclass('autodedup.rt_calibration') is not null
   and to_regclass('autodedup.rt_block_cell')  is not null
   and to_regclass('autodedup.rt_lease')       is not null as present
"""

# A pass is one transaction (E70) and a statement that runs away is a lease held past its TTL,
# so both bounds are set on the session before any work begins.
# `SET` is a utility statement and takes no parameter, so the two session bounds go through
# `set_config` — same effect, one PREPARE-able statement each.
RT_STATEMENT_GUARD_SQL = """
select set_config('statement_timeout', %(statement_timeout_ms)s::text, false)
"""

RT_IDLE_GUARD_SQL = """
select set_config('idle_in_transaction_session_timeout', %(idle_timeout_ms)s::text, false)
"""

# The SECOND kill switch, in the database rather than in the repository: an operator can stop
# the lane without a workflow edit. Absent means "not blocked" — the repository variable is
# what makes the lane dark by default, and this row is what stops one that is already on.
RT_SETTING_SQL = """
select s.value
  from autodedup.settings s
 where s.key = %(key)s::text
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
# FOUR bounded feeds, all read-only (D4):
#   * new       — `listings_pkey`, paged on `id`, settle-lagged, plus a straggler anti-join.
#   * changed   — `listing_snapshots_pkey`. Rule #2 makes that table an append-on-content-change
#                 feed, so it is exactly "a listing whose content moved", with no column of its
#                 own to add and no trigger to install.
#   * delisted  — `listings_inactive_at_idx` (partial), paged on `(inactive_at, id)`.
#   * revived   — a delisting has an UNDO no cursor can see: `touch_listings` sets
#                 `is_active = true, inactive_at = null` on the same id and appends no snapshot
#                 (rule #2), so a revived advert is invisible to all three cursors above. The
#                 fourth feed is a bounded round-robin sweep of the rows this lane itself
#                 believes are inactive, anti-joined against the live flag.
RT_NEW_LISTINGS_SQL = """
select l.id, l.first_seen_at
  from public.listings l
 where l.id > %(after_id)s::bigint
   and (l.first_seen_at is null
        or l.first_seen_at <= now() - make_interval(secs => %(lag)s))
 order by l.id
 limit %(limit)s
"""

# The proof rather than the assumption: any id at or below the cursor that this generation has
# no fingerprint row for was committed after the pass that stepped over it (E68).
RT_NEW_STRAGGLERS_SQL = """
select l.id, l.first_seen_at
  from public.listings l
 where l.id > %(after_id)s::bigint - %(window)s::bigint
   and l.id <= %(after_id)s::bigint
   and not exists (
         select 1
           from autodedup.rt_fp f
          where f.generation = %(generation)s::text
            and f.listing_id = l.id)
 order by l.id
 limit %(limit)s
"""

RT_CHANGED_LISTINGS_SQL = """
select s.id, s.listing_id, s.scraped_at
  from public.listing_snapshots s
 where s.id > %(after_id)s::bigint
   and s.listing_id is not null
   and (s.scraped_at is null
        or s.scraped_at <= now() - make_interval(secs => %(lag)s))
 order by s.id
 limit %(limit)s
"""

RT_FLIPPED_LISTINGS_SQL = """
select l.id, l.inactive_at
  from public.listings l
 where l.inactive_at is not null
   and (l.inactive_at, l.id) > (%(after)s::timestamptz, %(after_id)s::bigint)
   and l.inactive_at <= now() - make_interval(secs => %(lag)s)
 order by l.inactive_at, l.id
 limit %(limit)s
"""

# One row back, never the slice: the sweep reads a bounded window of this generation's inactive
# fingerprints and returns only the ids the live flag disagrees with, plus where to resume.
RT_REVIVED_SQL = """
select coalesce(max(f.listing_id), %(after_id)s::bigint) as slice_max,
       count(*)                                          as slice_size,
       coalesce(array_remove(array_agg(
           case when l.id is not null then f.listing_id end), null),
           '{}'::bigint[])                               as revived
  from (select listing_id
          from autodedup.rt_fp
         where generation = %(generation)s::text
           and is_active = false
           and listing_id > %(after_id)s::bigint
         order by listing_id
         limit %(limit)s) f
  left join public.listings l
         on l.id = f.listing_id
        and l.is_active
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

# The revive sweep WRAPS rather than ends, so its cursor is written whole: a coalesce would
# never let it return to 0.
RT_CURSOR_SET_SQL = """
insert into autodedup.scan_cursor (name, last_listing_id, updated_at)
values (%(name)s::text, %(last_listing_id)s::bigint, now())
on conflict (name) do update set
    last_listing_id = excluded.last_listing_id,
    updated_at      = now()
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
#
# One statement per PASS rather than per probe key (E69): a listing carries 17.3 index keys and
# a pass touches hundreds of listings, so the per-key spelling was ~12,000 round trips a pass.
RT_LOOKUP_MANY_SQL = """
select k.probe, k.key_token, k.listing_id
  from autodedup.fp_key k
  join unnest(%(probes)s::text[], %(tokens)s::text[]) as w(probe, key_token)
    on w.probe = k.probe and w.key_token = k.key_token
 where k.generation = %(generation)s::text
 order by k.probe, k.key_token, k.listing_id
"""

RT_KEYS_MANY_SQL = """
select k.listing_id, k.probe, k.key_token
  from autodedup.fp_key k
 where k.generation = %(generation)s::text
   and k.listing_id = any(%(ids)s::bigint[])
 order by k.listing_id, k.probe, k.key_token
"""

RT_KEY_DELETE_SQL = """
delete from autodedup.fp_key
 where generation = %(generation)s::text
   and listing_id = any(%(ids)s::bigint[])
"""

RT_KEY_INSERT_SQL = """
insert into autodedup.fp_key (generation, probe, key_token, listing_id)
select %(generation)s::text, w.probe, w.key_token, w.listing_id
  from unnest(%(probes)s::text[], %(tokens)s::text[], %(listing_ids)s::bigint[])
       as w(probe, key_token, listing_id)
on conflict (generation, probe, key_token, listing_id) do nothing
"""

# ------------------------------------------------------------------ fingerprint row
#
# `autodedup.rt_fp`, NOT migration 528's `listing_fp`: that table is keyed on `listing_id`
# alone so it cannot hold two generations, and nothing has ever written it. This one is
# generation-scoped like every other row of this lane and carries exactly what the lane reads
# without a fact fetch — the five guard columns (E17's rule floor), the re-score digest, the
# census cell the listing is counted in (so a MOVE unbumps the cell it left, not the one it
# arrived in) and the activity flag the revive sweep anti-joins.
RT_FP_UPSERT_SQL = """
insert into autodedup.rt_fp (
    generation, listing_id, category_main, category_type, area_m2, disposition, floor,
    fp_digest, cell_key, cell_group, is_active, updated_at
) values (
    %(generation)s::text, %(listing_id)s::bigint, %(category_main)s::text,
    %(category_type)s::text, %(area_m2)s::double precision, %(disposition)s::text,
    %(floor)s::integer, %(fp_digest)s::text, %(cell_key)s::text, %(cell_group)s::text,
    %(is_active)s::boolean, now()
)
on conflict (generation, listing_id) do update set
    category_main = excluded.category_main,
    category_type = excluded.category_type,
    area_m2       = excluded.area_m2,
    disposition   = excluded.disposition,
    floor         = excluded.floor,
    fp_digest     = excluded.fp_digest,
    cell_key      = excluded.cell_key,
    cell_group    = excluded.cell_group,
    is_active     = excluded.is_active,
    updated_at    = now()
"""

RT_FP_DELETE_SQL = """
delete from autodedup.rt_fp
 where generation = %(generation)s::text
   and listing_id = any(%(ids)s::bigint[])
"""

RT_FP_READ_SQL = """
select f.listing_id, f.category_main, f.category_type, f.area_m2, f.disposition, f.floor,
       f.fp_digest, f.cell_key, f.cell_group, f.is_active
  from autodedup.rt_fp f
 where f.generation = %(generation)s::text
   and f.listing_id = any(%(ids)s::bigint[])
"""

RT_KNOWN_SQL = """
select f.listing_id
  from autodedup.rt_fp f
 where f.generation = %(generation)s::text
   and f.listing_id = any(%(ids)s::bigint[])
"""

RT_FP_COUNT_SQL = """
select count(*)
  from autodedup.rt_fp f
 where f.generation = %(generation)s::text
"""

# ------------------------------------------------------------------ pair grain
#
# `zone` carries the table's own CHECK (`merge`/`band`/`reject`), so a guard veto is written as
# `reject` with `guard_veto` naming the rule — the shape the score lane already writes. The
# certificate is a COLUMN here and not a parse of `decision`: E63 can re-promote a certified
# pair under a `context_rule:` reason, so the reason string is lossy, and E33 orders a
# component's edges certificate-first — a cluster that read its edges back without the
# certificate would union them in a different order than the cohort pass did.
_PAIR_COLUMNS = """p.listing_lo, p.listing_hi, p.probes, p.from_lo, p.from_hi, p.score,
       p.zone, p.decision, p.guard_veto, p.families, p.certificate, p.evidence, p.context,
       p.fp_lo, p.fp_hi"""

RT_PAIRS_TOUCHING_SQL = f"""
select {_PAIR_COLUMNS}
  from autodedup.pairs p
 where p.generation = %(generation)s::text
   and (p.listing_lo = any(%(ids)s::bigint[]) or p.listing_hi = any(%(ids)s::bigint[]))
"""

RT_PAIRS_WITHIN_SQL = f"""
select {_PAIR_COLUMNS}
  from autodedup.pairs p
 where p.generation = %(generation)s::text
   and p.listing_lo = any(%(ids)s::bigint[])
   and p.listing_hi = any(%(ids)s::bigint[])
 order by p.listing_lo, p.listing_hi
"""

# E64's rail reads only the merges of the blocks whose census MOVED this pass: under a frozen
# calibration (E65) no other block's counter can have changed, so a generation-wide scan would
# read millions of rows to re-confirm what cannot have moved.
RT_STAMPED_MERGES_SQL = f"""
select {_PAIR_COLUMNS}
  from autodedup.pairs p
 where p.generation = %(generation)s::text
   and p.zone = 'merge'
   and p.context ->> 'block' = any(%(blocks)s::text[])
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
    generation, listing_lo, listing_hi, probes, from_lo, from_hi, families, certificate,
    features, fp_lo, fp_hi, score, zone, decision, guard_veto, evidence, context,
    calibration_digest, feature_version, model_version, decided_at
) values (
    %(generation)s::text, %(listing_lo)s::bigint, %(listing_hi)s::bigint, %(probes)s::text[],
    %(from_lo)s::boolean, %(from_hi)s::boolean, %(families)s::smallint, %(certificate)s::text,
    %(features)s::jsonb, %(fp_lo)s::text, %(fp_hi)s::text, %(score)s::real, %(zone)s::text,
    %(decision)s::text, %(guard_veto)s::text, %(evidence)s::jsonb, %(context)s::jsonb,
    %(calibration_digest)s::text, %(feature_version)s::smallint, %(model_version)s::text, now()
)
on conflict (generation, listing_lo, listing_hi) do update set
    probes             = excluded.probes,
    from_lo            = excluded.from_lo,
    from_hi            = excluded.from_hi,
    families           = excluded.families,
    certificate        = excluded.certificate,
    features           = excluded.features,
    fp_lo              = excluded.fp_lo,
    fp_hi              = excluded.fp_hi,
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

# A pair is a cluster's EDGE only when both sides landed in that cluster (the score lane's
# rule), so membership is stamped from the cluster's own member set.
RT_PAIR_CLUSTER_SQL = """
update autodedup.pairs p
   set cluster_key = %(cluster_key)s::bigint
 where p.generation = %(generation)s::text
   and p.listing_lo = any(%(ids)s::bigint[])
   and p.listing_hi = any(%(ids)s::bigint[])
"""

RT_PAIR_UNCLUSTER_SQL = """
update autodedup.pairs p
   set cluster_key = null
 where p.generation = %(generation)s::text
   and p.cluster_key = any(%(keys)s::bigint[])
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

# `cluster_conflicts` carries no generation column (migration 528) — the score lane stamps it
# into `detail`, and this lane scopes its own re-write the same way.
RT_CONFLICT_DROP_SQL = """
delete from autodedup.cluster_conflicts c
 where c.detail ->> 'generation' = %(generation)s::text
   and c.listing_lo = any(%(ids)s::bigint[])
   and c.listing_hi = any(%(ids)s::bigint[])
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

# The seed reads the PRESENT, so a new generation starts at the corpus instead of walking the
# whole history of it (E71): the cold-start cursors are the live maxima.
RT_SEED_CURSORS_SQL = """
select (select coalesce(max(id), 0) from public.listings)              as last_listing_id,
       (select coalesce(max(id), 0) from public.listing_snapshots)     as last_snapshot_id,
       (select coalesce(max(inactive_at), now()) from public.listings) as watermark,
       (select coalesce(max(id), 0) from public.listings
         where inactive_at = (select max(inactive_at) from public.listings)) as watermark_id
"""

RT_MUST_NOT_LINK_SQL = """
select listing_lo, listing_hi
  from autodedup.must_not_link
"""
