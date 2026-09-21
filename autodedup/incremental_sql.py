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

**Why the feeds carry a settle LAG and a keyset tie-break (E73).** `id` is assigned at INSERT
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

# A pass is one transaction (E75) and a statement that runs away is a lease held past its TTL,
# so all three bounds are set as the transaction's FIRST statements. `SET` is a utility
# statement and takes no parameter, so each goes through `set_config` — same effect, one
# PREPARE-able statement each — and every one is LOCAL (W9d-4): `db.connect` speaks to Supabase's
# transaction-mode pooler, which rebinds the connection between queries, so a guard set on the
# session is a guard the pass's own transaction may never see (and one a later, unrelated
# consumer of that backend may inherit). `is_local = true` binds it to this transaction and
# unwinds with it.
RT_STATEMENT_GUARD_SQL = """
select set_config('statement_timeout', %(statement_timeout_ms)s::text, true)
"""

RT_LOCK_GUARD_SQL = """
select set_config('lock_timeout', %(lock_timeout_ms)s::text, true)
"""

RT_IDLE_GUARD_SQL = """
select set_config('idle_in_transaction_session_timeout', %(idle_timeout_ms)s::text, true)
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
# SIX bounded feeds, all read-only (D4) and all restricted to `rt_scope` (E79):
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
#   * drifted   — a listing the geocoder MOVES out of the scope touches no cursor either. The
#                 fifth feed sweeps this generation's own fingerprint rows against the scope
#                 and hands back the ones that left, to be retired — under a rail, because a
#                 scope that has gone wrong reports the WHOLE store as departed (W9d-1).
#   * entered   — and the mirror image, which W9c owed and W9d-3 pays: `listing_location` is
#                 written after the listing is, so an in-scope listing can be out of scope when
#                 its arrival window passes and in scope an hour later, with every forward
#                 cursor already past it. The sixth feed round-robins the SCOPE's own listing
#                 ids, one block a pass, and claims the ones with no fingerprint row.
# WINDOW FIRST, SCOPE SECOND — and the cursor advances over the WINDOW (E79). The lane holds
# only the listings of `rt_scope`, ~0.6% of the corpus, so a feed that filtered BY the scope
# would either seq-scan `listing_location` (there is no index on `cast_obce_kod`, and D8
# forbids adding one) or crawl. Each feed reads a bounded window off its OWN cursor index,
# left-joins that window's ids to `listing_location` through `listing_location_pkey` — one
# index probe per window row, the scope never the driving side — and returns the survivors
# beside the window's own end. The caller advances the cursor to that end even when nothing
# survived, which is what lets a feed cross a 99.4% out-of-scope corpus at window speed rather
# than at arrival speed. Nothing is skipped: when MORE rows survive than the claim's share, the
# caller cuts the list and advances only as far as the last row it took.
#
# `%(all_scope)s` is the `rt_scope = all` case, and it sits in the FILTER rather than in the
# join so that a whole-corpus run also keeps the listings with no `listing_location` row.
RT_NEW_LISTINGS_SQL = """
with win as (
  select l.id as id, l.first_seen_at as first_seen_at
    from public.listings l
   where l.id > %(after_id)s::bigint
     and (l.first_seen_at is null
          or l.first_seen_at <= now() - make_interval(secs => %(lag)s))
   order by l.id
   limit %(window)s
)
select coalesce(max(w.id), %(after_id)s::bigint) as window_max,
       count(*)                                  as window_size,
       coalesce(array_agg(w.id order by w.id)
                filter (where %(all_scope)s::boolean or ll.listing_id is not null),
                '{}'::bigint[])                  as ids,
       coalesce(array_agg(w.first_seen_at order by w.id)
                filter (where %(all_scope)s::boolean or ll.listing_id is not null),
                '{}'::timestamptz[])             as stamps
  from win w
  left join public.listing_location ll
         on ll.listing_id = w.id
        and (ll.obec_kod = any(%(obec)s::bigint[])
             or ll.cast_obce_kod = any(%(cast_obce)s::bigint[]))
"""

# The proof rather than the assumption: any id at or below the cursor that this generation has
# no fingerprint row for was committed after the pass that stepped over it (E73). Under a scope
# the anti-join ALONE would answer with out-of-scope ids for ever — almost nothing in the
# window has a fingerprint row, and almost nothing should — so the scope is part of this
# statement rather than a filter over its answer.
# The look-back is a ROW count and not an id range (W9d-3). Ids are sparse and unevenly so —
# measured live, the last 5,000 id UNITS hold 1,482 rows near the head and ~230 in older id
# space, against a constant whose comment always meant 5,000 ROWS. `order by id desc limit N`
# off `listings_pkey` is the honest spelling of "the last N rows", and it is a backwards index
# scan rather than a range predicate.
RT_NEW_STRAGGLERS_SQL = """
with win as (
  select l.id as id, l.first_seen_at as first_seen_at
    from public.listings l
   where l.id <= %(after_id)s::bigint
   order by l.id desc
   limit %(window)s
)
select w.id, w.first_seen_at
  from win w
  left join public.listing_location ll
         on ll.listing_id = w.id
        and (ll.obec_kod = any(%(obec)s::bigint[])
             or ll.cast_obce_kod = any(%(cast_obce)s::bigint[]))
 where (%(all_scope)s::boolean or ll.listing_id is not null)
   and not exists (
         select 1
           from autodedup.rt_fp f
          where f.generation = %(generation)s::text
            and f.listing_id = w.id)
 order by w.id
 limit %(limit)s
"""

RT_CHANGED_LISTINGS_SQL = """
with win as (
  select s.id as snapshot_id, s.listing_id as listing_id, s.scraped_at as scraped_at
    from public.listing_snapshots s
   where s.id > %(after_id)s::bigint
     and s.listing_id is not null
     and (s.scraped_at is null
          or s.scraped_at <= now() - make_interval(secs => %(lag)s))
   order by s.id
   limit %(window)s
)
select coalesce(max(w.snapshot_id), %(after_id)s::bigint) as window_max,
       count(*)                                           as window_size,
       coalesce(array_agg(w.listing_id order by w.snapshot_id)
                filter (where %(all_scope)s::boolean or ll.listing_id is not null),
                '{}'::bigint[])                           as ids,
       coalesce(array_agg(w.scraped_at order by w.snapshot_id)
                filter (where %(all_scope)s::boolean or ll.listing_id is not null),
                '{}'::timestamptz[])                      as stamps,
       coalesce(array_agg(w.snapshot_id order by w.snapshot_id)
                filter (where %(all_scope)s::boolean or ll.listing_id is not null),
                '{}'::bigint[])                           as cursors
  from win w
  left join public.listing_location ll
         on ll.listing_id = w.listing_id
        and (ll.obec_kod = any(%(obec)s::bigint[])
             or ll.cast_obce_kod = any(%(cast_obce)s::bigint[]))
"""

# The flip feed pages on the FULL key, so its window END is a pair and not the maximum of
# either half: `inactive_at` is the transaction timestamp and a `mark_inactive` batch shares
# one, so the last row of the ordered window is the only honest watermark.
RT_FLIPPED_LISTINGS_SQL = """
with win as (
  select l.id as id, l.inactive_at as inactive_at
    from public.listings l
   where l.inactive_at is not null
     and (l.inactive_at, l.id) > (%(after)s::timestamptz, %(after_id)s::bigint)
     and l.inactive_at <= now() - make_interval(secs => %(lag)s)
   order by l.inactive_at, l.id
   limit %(window)s
)
select (select w2.inactive_at from win w2
         order by w2.inactive_at desc, w2.id desc limit 1) as window_stamp,
       (select w2.id from win w2
         order by w2.inactive_at desc, w2.id desc limit 1) as window_id,
       count(*)                                            as window_size,
       coalesce(array_agg(w.id order by w.inactive_at, w.id)
                filter (where %(all_scope)s::boolean or ll.listing_id is not null),
                '{}'::bigint[])                            as ids,
       coalesce(array_agg(w.inactive_at order by w.inactive_at, w.id)
                filter (where %(all_scope)s::boolean or ll.listing_id is not null),
                '{}'::timestamptz[])                       as stamps
  from win w
  left join public.listing_location ll
         on ll.listing_id = w.id
        and (ll.obec_kod = any(%(obec)s::bigint[])
             or ll.cast_obce_kod = any(%(cast_obce)s::bigint[]))
"""

# One row back, never the slice: the sweep reads a bounded window of this generation's inactive
# fingerprints and returns only the ids the live flag disagrees with, plus where to resume.
RT_REVIVED_SQL = """
select coalesce(max(f.listing_id), %(after_id)s::bigint) as slice_max,
       count(*)                                          as slice_size,
       coalesce(array_agg(f.listing_id order by f.listing_id)
                filter (where l.id is not null
                          and (%(all_scope)s::boolean or ll.listing_id is not null)),
                '{}'::bigint[])                          as revived
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
  left join public.listing_location ll
         on ll.listing_id = f.listing_id
        and (ll.obec_kod = any(%(obec)s::bigint[])
             or ll.cast_obce_kod = any(%(cast_obce)s::bigint[]))
"""

# The FIFTH feed, and the one the scope owes (E79): a listing the operator's geocoder MOVES —
# an obec correction, a resolved část — leaves the scope without touching any cursor the other
# four page over. The sweep is the revive sweep's twin: a bounded round-robin slice of THIS
# generation's own fingerprint rows, left-joined to the scope, returning the ids the scope no
# longer holds. They are RETIRED (postings, fingerprint row, pairs) rather than left
# half-indexed — a listing whose postings are stale is a listing every neighbour retrieves
# wrongly. Under a scope the store is small enough that one slice covers it whole; the cursor
# wraps when the slice runs short, exactly as the revive sweep's does.
RT_SCOPE_DRIFT_SQL = """
select coalesce(max(f.listing_id), %(after_id)s::bigint) as slice_max,
       count(*)                                          as slice_size,
       coalesce(array_agg(f.listing_id order by f.listing_id)
                filter (where not %(all_scope)s::boolean and ll.listing_id is null),
                '{}'::bigint[])                          as departed
  from (select listing_id
          from autodedup.rt_fp
         where generation = %(generation)s::text
           and listing_id > %(after_id)s::bigint
         order by listing_id
         limit %(limit)s) f
  left join public.listing_location ll
         on ll.listing_id = f.listing_id
        and (ll.obec_kod = any(%(obec)s::bigint[])
             or ll.cast_obce_kod = any(%(cast_obce)s::bigint[]))
"""

# The SIXTH feed, and the one the scope owes in the other direction (W9d-3):
# `listing_location` is written asynchronously from the scrape, so an in-scope listing can carry
# no `obec_kod` at all when its arrival window passes. The forward feed correctly skips it — it
# is not yet in scope — and the cursor steps over it; when the geocoder resolves it an hour
# later NOTHING claims it (the drift sweep only sweeps OUT, the revive sweep only reads rows the
# store already holds, and the straggler sweep looks back a bounded number of rows). Measured
# live: 6.2% of the listings first seen in the last 6 h carry a NULL `obec_kod`.
#
# W9d served that feed by scanning a scope block on `public` EVERY cycle, and W9e's cost audit
# priced it: there is no index-served path to a quarter (`listing_location` has a btree on
# `(obec_kod, granularity)` and none on `cast_obce_kod`; D8 forbids adding one; `listings`
# carries no obec or quarter column indexed either — all three checked live against
# `pg_indexes`), so the quarter block is a bitmap heap scan of its PARENT obec filtered on
# `cast_obce_kod`: measured live `EXPLAIN (ANALYZE, BUFFERS)`, **159,357 index rows -> 1,439
# rows, 29,265 heap blocks, 29,568 buffers = 231 MB, 6.4 s, cold every time** (the working set
# does not stay in shared_buffers). One block a cycle over three blocks is ~48 scans of it a
# day: **~11 GB a day of cold reads on the instance that serves Browse**, whether or not
# anything entered, and `enter_slice` does not bound it — the `order by ... limit` is applied
# AFTER the bitmap is read.
#
# So the scan is no longer what a pass does. It REFRESHES `autodedup.rt_scope_ids` — the block's
# membership snapshot — on a cadence that is data, and an ordinary pass claims its entrants out
# of that snapshot with an anti-join that touches `public` not at all. What is lost is latency,
# not coverage, and only for the TAIL: a listing whose location resolves shortly after it
# arrives is already claimed by the straggler sweep (the last N rows of `listings`, anti-joined
# on `rt_fp`), so this feed exists for the long tail — an old listing re-geocoded into the
# scope — which is measured in days rather than in minutes.
RT_SCOPE_BLOCK_SQL = """
select ll.listing_id  as listing_id,
       ll.resolved_at as resolved_at
  from public.listing_location ll
 where ll.obec_kod = %(obec)s::bigint
   and (%(cast_obce)s::bigint is null
        or ll.cast_obce_kod = %(cast_obce)s::bigint)
 order by ll.listing_id
 limit %(limit)s
"""

# The snapshot write. One block is replaced whole: what the scan found is upserted, and what it
# no longer finds is pruned — a listing the geocoder moved OUT leaves the snapshot here and is
# retired by the drift sweep there, which is the other direction and keeps its own rail.
# `resolved_at` travels as TEXT and is cast per element: a block whose rows all carry a NULL
# `resolved_at` would otherwise hand psycopg a list with nothing to infer a type from, and
# Postgres has no cast from `text[]` to `timestamptz[]`.
RT_SCOPE_IDS_WRITE_SQL = """
insert into autodedup.rt_scope_ids (generation, block_key, listing_id, resolved_at,
                                    refreshed_at)
select %(generation)s::text, %(block_key)s::text, t.listing_id,
       t.resolved_at::timestamptz, now()
  from unnest(%(listing_ids)s::bigint[], %(resolved)s::text[])
         as t(listing_id, resolved_at)
on conflict (generation, block_key, listing_id) do update set
    resolved_at  = excluded.resolved_at,
    refreshed_at = now()
"""

RT_SCOPE_IDS_PRUNE_SQL = """
delete from autodedup.rt_scope_ids s
 where s.generation = %(generation)s::text
   and s.block_key = %(block_key)s::text
   and not (s.listing_id = any(%(listing_ids)s::bigint[]))
"""

# A listing the drift sweep RETIRES must leave the membership snapshot in the same transaction
# (W9e-1): the entrant claim reads the snapshot with no scope check of its own, so a row left
# behind hands the listing straight back, it is retired again, and the rolling-day retire
# budget burns down until the lane refuses itself for up to 24 h.
RT_SCOPE_IDS_DELETE_SQL = """
delete from autodedup.rt_scope_ids s
 where s.generation = %(generation)s::text
   and s.listing_id = any(%(ids)s::bigint[])
"""

# A rescope that DROPS a block leaves that block's snapshot rows with no walk to prune them
# (the prune above is per walked block), so they would be claimed and retired for ever (W9e-1b).
RT_SCOPE_IDS_PRUNE_BLOCKS_SQL = """
delete from autodedup.rt_scope_ids s
 where s.generation = %(generation)s::text
   and not (s.block_key = any(%(block_keys)s::text[]))
"""

# What an ordinary pass costs the production instance for this feed: NOTHING. Both sides of the
# anti-join are in schema `autodedup`, served by `autodedup_rt_scope_ids_claim_idx` and the
# `rt_fp` primary key. The settle lag is the same one every other feed honours (W9e/R5) and it
# is spelled on `resolved_at` — the moment the location that PUT the listing in scope was
# written — because that, not the listing's age, is this feed's arrival event.
RT_SCOPE_ENTRANTS_SQL = """
select s.listing_id, s.resolved_at
  from autodedup.rt_scope_ids s
 where s.generation = %(generation)s::text
   and s.listing_id > %(after_id)s::bigint
   and (s.resolved_at is null
        or s.resolved_at <= now() - make_interval(secs => %(lag)s))
   and not exists (select 1
                     from autodedup.rt_fp f
                    where f.generation = %(generation)s::text
                      and f.listing_id = s.listing_id)
 order by s.listing_id
 limit %(limit)s
"""

# The cadence's two inputs, in one statement: how long ago each block was walked, and how many
# walks this generation has spent in the rolling day the cap is measured over. `age_s` is
# computed by the SERVER — a runner's clock is not the database's, and the cadence is the only
# thing standing between this lane and W9d's ~11 GB a day.
RT_SCOPE_SCAN_STATE_SQL = """
select s.block_key                                                     as block_key,
       extract(epoch from now() - max(s.scanned_at))::double precision as age_s,
       (count(*) filter (
          where s.scanned_at > now() - make_interval(hours => %(hours)s::int)))::int as scans
  from autodedup.rt_scope_scan s
 where s.generation = %(generation)s::text
 group by s.block_key
"""

RT_SCOPE_SCAN_WRITE_SQL = """
insert into autodedup.rt_scope_scan (generation, block_key, scanned_at, rows_found,
                                     elapsed_ms)
values (%(generation)s::text, %(block_key)s::text, now(), %(rows_found)s::int,
        %(elapsed_ms)s::double precision)
"""

# The retire rail's rolling window (W9e/R2). `retired` is what the generation has already
# retired inside it, and `store_at_start` is the store as the OLDEST pass in the window
# measured it — the denominator W9d took from the current store, which shrinks with every
# retirement and so could never bound a slow grind.
RT_RETIRE_WINDOW_SQL = """
select coalesce(sum(e.n_retired), 0)::bigint as retired,
       (select e2.store_rows
          from autodedup.rt_retire_event e2
         where e2.generation = %(generation)s::text
           and e2.retired_at > now() - make_interval(hours => %(hours)s::int)
         order by e2.retired_at, e2.id
         limit 1)                            as store_at_start
  from autodedup.rt_retire_event e
 where e.generation = %(generation)s::text
   and e.retired_at > now() - make_interval(hours => %(hours)s::int)
"""

RT_RETIRE_EVENT_WRITE_SQL = """
insert into autodedup.rt_retire_event (generation, retired_at, n_retired, store_rows)
values (%(generation)s::text, now(), %(n_retired)s::int, %(store_rows)s::bigint)
"""

# A quarter has no index of its own, so the entrant sweep reaches it through its PARENT obec.
# The parent is a registry fact, read once a pass from `ruian_admin_units` through its
# `(level, code)` index and its primary key (`EXPLAIN`: two Index Scans, 4.7 ms). A quarter the
# registry cannot place is a hard error rather than a block the sweep silently skips.
RT_SCOPE_PARENT_OBEC_SQL = """
select u.code as cast_obce_kod, p.code as obec_kod
  from public.ruian_admin_units u
  join public.ruian_admin_units p
    on p.id = u.parent_id
   and p.level = 'obec'
 where u.level = 'cast_obce'
   and u.code = any(%(codes)s::bigint[])
   and u.valid_to is null
"""

# ------------------------------------------------------------------ the storage budget (E79)
#
# The operator pays for this store by the megabyte, so a pass reads what the schema already
# costs BEFORE it writes anything and refuses to run over the budget. `pg_total_relation_size`
# already counts a table's indexes and its TOAST, so the sum runs over base relations only
# (`r`/`p`/`m`) — adding index relations would double-count them.
RT_SCHEMA_SIZE_SQL = """
select coalesce(sum(pg_total_relation_size(c.oid)), 0)::bigint as bytes
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'autodedup'
   and c.relkind in ('r', 'p', 'm')
"""

# What the generation holds, exactly. Cheap while the scope is small — which is the point of
# having a scope — and replaced by the planner's estimate when `rt_scope = all`.
RT_ROW_CENSUS_SQL = """
select 'fp_key' as table_name, count(*) as n
  from autodedup.fp_key where generation = %(generation)s::text
union all
select 'rt_fp', count(*) from autodedup.rt_fp where generation = %(generation)s::text
union all
select 'pairs', count(*) from autodedup.pairs where generation = %(generation)s::text
union all
select 'rt_block_cell', count(*)
  from autodedup.rt_block_cell where generation = %(generation)s::text
"""

RT_ROW_ESTIMATE_SQL = """
select c.relname as table_name, greatest(c.reltuples, 0)::bigint as n
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'autodedup'
   and c.relname in ('fp_key', 'rt_fp', 'pairs', 'rt_block_cell')
"""

RT_SETTINGS_MANY_SQL = """
select s.key, s.value
  from autodedup.settings s
 where s.key = any(%(keys)s::text[])
"""

# The growth readout's own watermark. An OBSERVATION row rather than a knob: the lane writes
# it, nothing reads it but the next pass's summary, and it lives in `autodedup.settings`
# because a new key there needs no migration (528).
RT_SETTING_WRITE_SQL = """
insert into autodedup.settings (key, value, updated_at, updated_by)
values (%(key)s::text, %(value)s::jsonb, now(), %(updated_by)s::text)
on conflict (key) do update set
    value      = excluded.value,
    updated_at = now(),
    updated_by = excluded.updated_by
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
# THE LANE DOES NOT SPELL THESE ITSELF. A listing's facts are whatever the EXPORT lane reads
# for the cohort pass — `export_sql.COHORT_LISTINGS_SQL`, `COHORT_LOCATION_SQL`,
# `COHORT_PRICE_HISTORY_SQL`, `COHORT_IMAGES_SQL`, `COHORT_CLIP_SQL`, `COHORT_CLIP_TAGS_SQL`
# — assembled by the export's own `build_listing_record` / `build_image_record`. W9 hand-wrote
# a thinner version of them and the schema gate caught only the loudest symptom (`loc.lat`
# does not exist; the store keeps a `geom`). The quiet ones were worse: no `attrs`, so every
# attribute feature would have been ABSENT in production while the replay had them; no price
# history, so E19's price-event features too; no `broker_key`, so the whole BRK family; and
# `granularity` read as an enum rather than text. A replay against an exported artifact cannot
# see any of that, which is why the lane now reads through the export's definitions instead of
# beside them (E77).
#
# The ONE deliberate difference is the corpus-wide pHash population: the export computes it
# with a sequential scan over `public.images` (no index, D8 forbids adding one), and the lane
# reads the frozen one out of `autodedup.phash_pop`, because E70 freezes it with the other
# cohort statistics — a pass that recomputed it would move `catalog_ratio`, `anchor_bands` and
# with them every certificate that reads a catalogue ratio.
RT_PHASH_POP_SQL = """
select p.phash, p.n_listings
  from autodedup.phash_pop p
 where p.phash = any(%(hashes)s::bigint[])
"""

# The one WRITER of the frozen population (E91). It is the SEED's, and what it writes is the
# artifact's own `pop` — the number `COHORT_PHASH_POP_SQL` counted over `public.images` when
# the calibration was cut — so the live lane joins against exactly the statistic the batch
# engine scored with, at zero cost against `public`. Migration 528's prose says "only hashes on
# >= 3 listings"; that was never true of the table, only of an intention, and a population of 1
# or 2 is the difference between "this photo is unique" and "nobody measured it".
RT_PHASH_POP_WRITE_SQL = """
insert into autodedup.phash_pop (phash, n_listings, computed_at)
values (%(phash)s::bigint, %(n_listings)s::integer, now())
on conflict (phash) do update set
    n_listings  = excluded.n_listings,
    computed_at = now()
"""

RT_PHASH_POP_COUNT_SQL = """
select count(*) as n from autodedup.phash_pop
"""

# The change stamp of a listing's CONTENT, for the parity gate and for the instrument that
# shares its definition: rule #2 appends a `listing_snapshots` row only when the content hash
# moves, so the newest snapshot is when this row last really changed. There is no
# `last_change_at` column on `listings` to read instead.
RT_PARITY_CHANGE_SQL = """
select s.listing_id           as listing_id,
       max(s.scraped_at)      as last_change_at,
       count(*)               as n_snapshots
from listing_snapshots s
where s.listing_id = any(%(ids)s::bigint[])
group by s.listing_id
"""

# ------------------------------------------------------------------ probe postings
#
# One statement per PASS rather than per probe key (E74): a listing carries 17.3 index keys and
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
# `first_decided_at` is COALESCED, never overwritten (E92, migration 540): the evidence
# horizon is measured from a generation's FIRST decision about a listing, so a refresh — and a
# re-decision the evidence sweep itself asked for — must not restart that clock. The four
# `ev_*` counts and the derived `ev_complete` are the opposite: they are what THIS decision
# rested on, so they are replaced every time.
RT_FP_UPSERT_SQL = """
insert into autodedup.rt_fp (
    generation, listing_id, category_main, category_type, area_m2, disposition, floor,
    fp_digest, cell_key, cell_group, is_active, ev_images, ev_phash, ev_clip, ev_tags,
    ev_complete, first_decided_at, updated_at
) values (
    %(generation)s::text, %(listing_id)s::bigint, %(category_main)s::text,
    %(category_type)s::text, %(area_m2)s::double precision, %(disposition)s::text,
    %(floor)s::integer, %(fp_digest)s::text, %(cell_key)s::text, %(cell_group)s::text,
    %(is_active)s::boolean, %(ev_images)s::integer, %(ev_phash)s::integer,
    %(ev_clip)s::integer, %(ev_tags)s::integer, %(ev_complete)s::boolean, now(), now()
)
on conflict (generation, listing_id) do update set
    category_main    = excluded.category_main,
    category_type    = excluded.category_type,
    area_m2          = excluded.area_m2,
    disposition      = excluded.disposition,
    floor            = excluded.floor,
    fp_digest        = excluded.fp_digest,
    cell_key         = excluded.cell_key,
    cell_group       = excluded.cell_group,
    is_active        = excluded.is_active,
    ev_images        = excluded.ev_images,
    ev_phash         = excluded.ev_phash,
    ev_clip          = excluded.ev_clip,
    ev_tags          = excluded.ev_tags,
    ev_complete      = excluded.ev_complete,
    first_decided_at = coalesce(autodedup.rt_fp.first_decided_at,
                                excluded.first_decided_at),
    updated_at       = now()
"""

RT_FP_DELETE_SQL = """
delete from autodedup.rt_fp
 where generation = %(generation)s::text
   and listing_id = any(%(ids)s::bigint[])
"""

RT_FP_READ_SQL = """
select f.listing_id, f.category_main, f.category_type, f.area_m2, f.disposition, f.floor,
       f.fp_digest, f.cell_key, f.cell_group, f.is_active,
       f.ev_images, f.ev_phash, f.ev_clip, f.ev_tags,
       extract(epoch from f.first_decided_at)::double precision as first_decided_epoch
  from autodedup.rt_fp f
 where f.generation = %(generation)s::text
   and f.listing_id = any(%(ids)s::bigint[])
"""

# ------------------------------------------------------------------ the evidence sweep (E92)
#
# The SEVENTH feed, and the real-time analogue of the defect W9g closed. The six feeds of W9
# are keyed on `first_seen_at`, a snapshot id, `inactive_at`, a revival and scope drift — NOT
# ONE of them is keyed on a hash or a vector arriving, and the producers that write those
# arrive hours after the lane has already decided the listing (dHash hourly at :20, CLIP at
# :40; p50 2.47 h to a first tag, against a claim at `first_seen_at + 5..15 min`). So ~80% of
# arrivals were decided with every phash NULL and never looked at again.
#
# Arm one: this generation's own rows whose evidence was incomplete when it decided them, or
# which it decided recently enough that a producer could still be behind. It reads `rt_fp`
# alone — zero blocks of `public` — and hands the ids to the probe below. Measured on the
# trial scope: 76 of 4,974 rows carry incomplete evidence in steady state, plus ~74 inside a
# 48 h horizon at 37 arrivals a day.
RT_EVIDENCE_CANDIDATES_SQL = """
select f.listing_id, f.ev_images, f.ev_phash, f.ev_clip, f.ev_tags
  from autodedup.rt_fp f
 where f.generation = %(generation)s::text
   and f.listing_id > %(after_id)s::bigint
   and (f.ev_complete is not true
        or f.first_decided_at is null
        or f.first_decided_at > now() - make_interval(hours => %(horizon_hours)s::int))
 order by f.listing_id
 limit %(limit)s
"""

# The probe itself: `public.images` by listing id, through `images_listing_id_idx`, and NOTHING
# else. `clip_tagged_at` is the CLIP job's own stamp on the image row, so the vector and the
# tags are read without joining `image_clip_embeddings` or `image_clip_tags` — measured on the
# trial scope's 73,208 images, the stamp and the two tables agree on every single row (72,171
# each, 0 disagreements either way), and the two joins cost 14x the buffers (34,419 against
# 2,375 for 200 listings).
RT_EVIDENCE_PROBE_SQL = """
select i.listing_id                as listing_id,
       count(*)                    as n_images,
       count(i.phash)              as n_phash,
       count(i.clip_tagged_at)     as n_tagged
  from public.images i
 where i.listing_id = any(%(ids)s::bigint[])
 group by i.listing_id
"""

# Arm two: a merge HELD in the band for want of photographs (E93) whose hold is over — neither
# side is still pending inside the horizon. Nothing about such a pair has moved, so no digest
# and no feed could ever find it again; it is asked for by reason. The horizon is evaluated by
# the SERVER's clock, because a runner's is not the database's.
RT_EVIDENCE_RELEASE_SQL = """
select p.listing_lo, p.listing_hi
  from autodedup.pairs p
  left join autodedup.rt_fp flo
         on flo.generation = p.generation and flo.listing_id = p.listing_lo
  left join autodedup.rt_fp fhi
         on fhi.generation = p.generation and fhi.listing_id = p.listing_hi
 where p.generation = %(generation)s::text
   and p.decision = %(reason)s::text
   and not (coalesce(flo.ev_images, 0) > 0 and coalesce(flo.ev_phash, 0) = 0
            and flo.first_decided_at
                > now() - make_interval(hours => %(horizon_hours)s::int))
   and not (coalesce(fhi.ev_images, 0) > 0 and coalesce(fhi.ev_phash, 0) = 0
            and fhi.first_decided_at
                > now() - make_interval(hours => %(horizon_hours)s::int))
 order by p.listing_lo, p.listing_hi
 limit %(limit)s
"""

# How many pairs of this generation are waiting on photographs, for the pass summary.
RT_EVIDENCE_HELD_COUNT_SQL = """
select count(*) as n
  from autodedup.pairs p
 where p.generation = %(generation)s::text
   and p.decision = %(reason)s::text
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
# calibration (E70) no other block's counter can have changed, so a generation-wide scan would
# read millions of rows to re-confirm what cannot have moved.
RT_STAMPED_MERGES_SQL = f"""
select {_PAIR_COLUMNS}
  from autodedup.pairs p
 where p.generation = %(generation)s::text
   and p.zone = 'merge'
   and p.context ->> 'block' = any(%(blocks)s::text[])
 order by p.listing_lo, p.listing_hi
"""

# E72's BFS step: the merge edges out of a frontier, both directions in one statement.
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
    %(features)s::jsonb, %(fp_lo)s::text, %(fp_hi)s::text, %(score)s::double precision,
    %(zone)s::text,
    %(decision)s::text, %(guard_veto)s::text, %(evidence)s::jsonb, %(context)s::jsonb,
    %(calibration_digest)s::text, %(feature_version)s::smallint, %(model_version)s::text, now()
)
on conflict (generation, listing_lo, listing_hi) do update set
    probes             = excluded.probes,
    from_lo            = excluded.from_lo,
    from_hi            = excluded.from_hi,
    families           = excluded.families,
    certificate        = excluded.certificate,
    -- A probe-only update carries no vector; it must not erase the one the decision
    -- was taken on.
    features           = coalesce(excluded.features, autodedup.pairs.features),
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
# Scoped to the COMPONENT, not to the generation: E72 rewrites the components it recomputed and
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

# ------------------------------------------------------------------ calibration (E70)
# Is this generation SEEDED at all? The payload is megabytes of frozen statistics, so the
# question "has `rt_seed` run here" is asked on its own (W9e/R1): an unseeded generation is a
# green `skipped: unseeded`, not a pass that hard-errors every ten minutes for ever.
RT_CALIBRATION_PRESENT_SQL = """
select c.generation, c.digest, c.built_at
  from autodedup.rt_calibration c
 where c.generation = %(generation)s::text
"""

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
# whole history of it (E76): the cold-start cursors are the live maxima.
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


# ------------------------------------------------------------------ the clean reset (E97)
#
# `rt_seed reseed=true fresh=true`: the twelve statements that empty ONE generation and
# nothing else, run inside the seed's own transaction so a refusal anywhere after them puts
# every row back. Each is a `delete ... returning` wrapped in a count, because the summary has
# to say what it removed per table — a reset whose receipt is "ok" is the reset that left
# 15,923 undecidable pairs in `rt` on 2026-09-20 and nobody noticed for a day.
#
# What is NOT here is the point of the list: `autodedup.verdicts`, `must_not_link` and the
# judge's `judgements` are GENERATION-FREE operator evidence (E95 says so for a re-seed and it
# is the same rule here), `rt_calibration` is rewritten two statements later by the seed
# itself, `phash_pop` is the frozen population the seed re-materialises, and no statement in
# this file names a table outside schema `autodedup` (D4/D8).
RT_FRESH_PAIRS_SQL = """
with gone as (
    delete from autodedup.pairs
     where generation = %(generation)s::text
 returning 1
)
select count(*)::bigint from gone
"""

RT_FRESH_CLUSTER_MEMBERS_SQL = """
with gone as (
    delete from autodedup.cluster_members
     where generation = %(generation)s::text
 returning 1
)
select count(*)::bigint from gone
"""

RT_FRESH_CLUSTERS_SQL = """
with gone as (
    delete from autodedup.clusters
     where generation = %(generation)s::text
 returning 1
)
select count(*)::bigint from gone
"""

# `cluster_conflicts` carries no generation COLUMN — the score lane writes the generation into
# `detail` and prunes on it there, so the reset reads it the same way rather than inventing a
# second spelling.
RT_FRESH_CLUSTER_CONFLICTS_SQL = """
with gone as (
    delete from autodedup.cluster_conflicts
     where detail ->> 'generation' = %(generation)s::text
 returning 1
)
select count(*)::bigint from gone
"""

RT_FRESH_RT_FP_SQL = """
with gone as (
    delete from autodedup.rt_fp
     where generation = %(generation)s::text
 returning 1
)
select count(*)::bigint from gone
"""

RT_FRESH_FP_KEY_SQL = """
with gone as (
    delete from autodedup.fp_key
     where generation = %(generation)s::text
 returning 1
)
select count(*)::bigint from gone
"""

RT_FRESH_BLOCK_CELL_SQL = """
with gone as (
    delete from autodedup.rt_block_cell
     where generation = %(generation)s::text
 returning 1
)
select count(*)::bigint from gone
"""

RT_FRESH_SCOPE_IDS_SQL = """
with gone as (
    delete from autodedup.rt_scope_ids
     where generation = %(generation)s::text
 returning 1
)
select count(*)::bigint from gone
"""

RT_FRESH_SCOPE_SCAN_SQL = """
with gone as (
    delete from autodedup.rt_scope_scan
     where generation = %(generation)s::text
 returning 1
)
select count(*)::bigint from gone
"""

RT_FRESH_RETIRE_EVENT_SQL = """
with gone as (
    delete from autodedup.rt_retire_event
     where generation = %(generation)s::text
 returning 1
)
select count(*)::bigint from gone
"""

# `scan_cursor` is keyed on the cursor NAME alone, so the rows this generation owns are named
# explicitly by the caller rather than filtered by a column that does not exist.
RT_FRESH_CURSORS_SQL = """
with gone as (
    delete from autodedup.scan_cursor
     where name = any(%(names)s::text[])
 returning 1
)
select count(*)::bigint from gone
"""

RT_FRESH_LEASE_SQL = """
with gone as (
    delete from autodedup.rt_lease
     where name = %(name)s::text
 returning 1
)
select count(*)::bigint from gone
"""

# ------------------------------------------------------------------ the bootstrap phase (E98)
#
# Which blocks this generation has EVER walked, with no 24-hour window on it. The cadence's own
# state query is windowed (a block last walked two days ago has to read as due), but the
# bootstrap phase ends on "every block has been walked once", which is a question about all of
# history and cannot be asked of a rolling day.
RT_SCOPE_SCAN_SEEN_SQL = """
select distinct s.block_key
  from autodedup.rt_scope_scan s
 where s.generation = %(generation)s::text
"""

# How many in-scope listings this generation has not fingerprinted yet — the bootstrap phase's
# own end condition, read from the membership snapshot and the store, both in schema
# `autodedup`: ZERO blocks of `public`.
RT_SCOPE_BACKLOG_SQL = """
select count(*)::bigint
  from autodedup.rt_scope_ids s
 where s.generation = %(generation)s::text
   and not exists (select 1
                     from autodedup.rt_fp f
                    where f.generation = %(generation)s::text
                      and f.listing_id = s.listing_id)
"""

# ------------------------------------------------------------------ live equivalence (E99)
#
# `--mode rt_equivalence`: the LIVE store against a batch generation scored on the same export.
# Read-only, and deliberately whole-table rather than id-filtered — one generation of the trial
# scope is ~16,000 pair rows, so the scope restriction is applied in Python against the
# membership snapshot instead of shipping a 5,000-element array into every statement.
RT_EQUIV_PAIRS_SQL = """
select p.listing_lo, p.listing_hi, p.score, p.zone, p.certificate, p.decision,
       p.guard_veto, p.families, p.model_version
  from autodedup.pairs p
 where p.generation = %(generation)s::text
 order by p.listing_lo, p.listing_hi
"""

# The feature vectors of the pairs that DIFFER, and only those: a difference is attributed by
# reading which features moved (E119), and the vector is ~60 keys, so reading it for every
# shared pair would carry tens of megabytes to answer a question about a few thousand. The two
# arrays are zipped by `unnest`, never crossed — the shape `JUDGED_EDGES_SQL` documents.
RT_EQUIV_PAIR_FEATURES_SQL = """
select p.listing_lo, p.listing_hi, p.features
  from autodedup.pairs p
 where p.generation = %(generation)s::text
   and (p.listing_lo, p.listing_hi) in (
         select lo, hi
           from unnest(%(los)s::bigint[], %(his)s::bigint[]) as pair(lo, hi)
       )
"""

# What the BATCH generation was scored under. `autodedup.runs` is the authoritative map from a
# generation to its pass (migration 538's own convention), and a score run records its whole
# settings blob — so the instrument can say whether the two sides ran the same clock rather
# than assuming it (E120). The newest successful pass wins: a generation re-scored under new
# settings IS the newer pass.
RT_EQUIV_BATCH_SETTINGS_SQL = """
select r.params -> 'settings' as settings, r.params ->> 'model_version' as model_version,
       r.finished_at
  from autodedup.runs r
 where r.mode = 'score'
   and r.status = 'success'
   and r.params ->> 'generation' = %(generation)s::text
 order by r.id desc
 limit 1
"""

# The declared type of the column `cluster.edge_rank` RANKS on. A store that cannot carry the
# number the engine decides in reorders a component's edges (E114/E115), and that is a defect
# the instrument must name rather than report as a cluster disagreement.
RT_EQUIV_SCORE_TYPE_SQL = """
select c.data_type, c.numeric_precision
  from information_schema.columns c
 where c.table_schema = 'autodedup'
   and c.table_name = 'pairs'
   and c.column_name = 'score'
"""

RT_EQUIV_MEMBERS_SQL = """
select m.cluster_key, m.listing_id
  from autodedup.cluster_members m
 where m.generation = %(generation)s::text
 order by m.cluster_key, m.listing_id
"""

RT_EQUIV_SCOPE_IDS_SQL = """
select s.listing_id
  from autodedup.rt_scope_ids s
 where s.generation = %(generation)s::text
 order by s.listing_id
"""

# The only read of `public` this mode makes, and it is `listings_pkey`. It answers two
# questions with one statement: a pair the live store holds and the batch generation cannot is
# EXPLAINED when either endpoint arrived after the export (`first_seen_at`), and a shared pair
# whose CLOCK features moved is explained when the live value is the one that matches the facts
# as they stand NOW — which is what the other three columns recompute (E119, `features.
# clock_features`).
RT_EQUIV_CLOCK_FACTS_SQL = """
select l.id, l.first_seen_at, l.last_seen_at, l.inactive_at, l.is_active
  from public.listings l
 where l.id = any(%(ids)s::bigint[])
"""
