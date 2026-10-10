# W6 runbook: the one destructive window (migration 593)

The order for the day the operator names (PROGRAM.md §5's W6 gate, §6's list, §7's brake). The file is
`migrations/593_merge_sprint_w6_drops.sql` on branch `feature/merge-sprint-w6`. Every SQL read below
goes through MCP `execute_sql` and starts with `SET statement_timeout='30s';`. `execute_sql` returns only
the last result set and collapses columns that share a name, so run each SELECT of C2, C4's second
block, C6, C8, C10 and step 14 as its own call, each starting with that SET, or fold a check into one
SELECT of scalar subqueries. The only production writes are the brake (steps 5 and 13), which the
operator sets and releases on /settings, because a Claude session's write of that setting has
been refused by the permission check before; the apply (step 10), which runs only after the operator's
"yes, apply it" (step 7); at most one PostgREST reload (step 16); and the sampler-table drop (step 21).
The reload and the drop need the operator's OK too. Baselines were read on 2026-10-08 between 03:51 and 06:29 UTC,
except those dated 2026-10-10.

```bash
TARGETS="property_status_events assets asset_membership_events properties:id,asset_id,price_per_m2_source_listing_id,distinct_site_count listings:id,discovery_seq listing_detail_queue:native_id,source,discovery_seq property_pipeline:property_id,account_id,note"
```

## D−1, off-peak (not 05:00–05:40, not 10:30–12:45 UTC)

1. **CI is green on the PR.** The migrations job replays 593 and its log shows `593: landed`.
2. **Plan and connectivity:**
   `gh workflow run apply_migration.yml --ref feature/merge-sprint-w6 -f file=593_merge_sprint_w6_drops.sql -f dry_run=true`,
   then `gh run list --workflow apply_migration.yml --limit 1 --json databaseId` and `gh run watch <id> --exit-status`.
   Expect `593_merge_sprint_w6_drops.sql: 136 statement(s), 3 probeable object(s)` and one connectivity row.
   Then run C1 and C2 once: each answers, and C1's `interval_s` reads 60 until the brake (null: a wrong key).
3. **Backup rehearsal.** The column-CSV path has never run in production.
   `gh workflow run backup_before_drop.yml --ref main -f label=merge-sprint-w6-rehearsal -f targets="$TARGETS"`,
   then `gh run watch <id> --exit-status` and `gh run view <id> --log | grep -E ' -> backups/|Backup complete|FAILED'`.
   Expect 7 `-> backups/...` lines whose rows pass C7 and `Backup complete: 7 target(s)`.

## The day (UTC)

**The morning hold, for every session, from 04:40 until the brake is off (step 13), or until step 3b
moves the day.** The operator relays it into every open session's chat before 04:40, the dedup session's
first.
- No merge to main, no deploy and no `apply_migration.yml` dispatch other than this runbook's own
  (steps 9, 10 and 12), and no Supabase compute change.
- No `autodedup.yml` dispatch in any mode (`rt_seed`, `apply`, `unapply` or a live-stream dry run): each
  takes `autodedup.rt_lease` for up to 5 hours, and a live apply or unapply runs only when the brake is 0.
- Until the apply, no ad-hoc read that filters, sorts or aggregates `properties.last_seen_at`, and none
  that names `property_status_events_public`, `listing_feed_public` or `listing_feed_visible` as an
  identifier. The keyset-index gate in section 0 is one-way: one planner probe of
  `properties_last_seen_keyset_idx` makes 593 refuse until a stats reset. C6 counts every statement that
  names those three.

3b. **04:40–04:45, the bootstrap gate.** Read the build flag, the lease and the worker in one call:
   ```sql
   select (select value #>> '{}' from autodedup.settings where key = 'rt_bootstrap:rt') bootstrap,
          (select string_agg(holder || ' until ' || expires_at, '; ') from autodedup.rt_lease where expires_at > now()) live_lease,
          started_at, details->'autodedup'->>'passes' passes, details->'autodedup'->>'failed_passes' failed_passes,
          details->'autodedup'->'last'->>'rss_after_trim_mb' rss_after_trim_mb,
          details->'autodedup'->'last'->>'memory_limit_mb' memory_limit_mb
     from public.worker_heartbeats where worker = 'realtime-worker';
   ```
   - **`bootstrap` true:** the dedup engine's build is running, and the dedup session's rule forbids both
     the window and the merge while it runs. The window then needs the operator's written override: their
     own yes, naming the apply and the merge of #1732 inside the build, relayed by the operator into the dedup
     session's own chat. Without it nothing runs this morning: no backup, no brake, and the window moves
     a day. With it, C1 and step 8 expect `bootstrap` true.
   - **The worker:** a `started_at` later than its last deploy means it died overnight (`passes` and
     `failed_passes` restart with it). Read `rss_after_trim_mb` (1,207.7 at 18:25 on 2026-10-10) against
     `memory_limit_mb` 7,629.4: a pass that runs out of memory kills the worker and leaves its lease for
     up to 40 minutes.
   - **`live_lease`:** before the brake, a lease held by the engine's own pass is normal: the pass
     releases it as it ends. If a holder will outlast 05:36:30, the latest dispatch step 8 allows, move
     the day now, before the backup and the brake.

4. **04:45–05:10, the backup, once step 3b has passed.** Dispatch it at 04:45 with the targets written
   inline (a fresh Bash call does not keep `$TARGETS`), then watch and grep it as step 3 does:
   ```bash
   gh workflow run backup_before_drop.yml --ref main -f label=merge-sprint-w6 -f targets="property_status_events assets asset_membership_events properties:id,asset_id,price_per_m2_source_listing_id,distinct_site_count listings:id,discovery_seq listing_detail_queue:native_id,source,discovery_seq property_pipeline:property_id,account_id,note"
   ```
   It goes early because backup_before_drop.yml:51-65 installs pg_dump 17 from apt.postgresql.org, and a
   failed install needs room for a retry before 05:10. The roughly 20–50 status-log rows written after
   the dump, until section 2 drops the table, are accepted and noted in the hand-over (step 20).
5. **05:00, the brake, once step 3b has passed, whatever step 4's state.** It only pauses the engine: its
   merges, or during a build the build itself, which makes no merges. Step 13 lifts it, so it waits for
   neither the backup nor step 7. The operator sets `realtime_autodedup_interval_seconds` to `0` on
   /settings; C1 reads it back.
6. **Read the backup back** with C7. A `FAILED` line or a count outside C7's rule stops the day.
7. **The operator writes "yes, apply it" in their own message.** Steps 9 and 10 do not run without it.
   A message that chooses the day, such as "ok, run W6 asap" on 2026-10-10, is not that go.
8. **The lease and the bootstrap.** Run this twice, two minutes apart:
   ```sql
   select (select string_agg(holder || ' until ' || expires_at, '; ') from autodedup.rt_lease where expires_at > now()) live_lease,
          (select value #>> '{}' from autodedup.settings where key = 'rt_bootstrap:rt') bootstrap,
          details->'autodedup'->>'passes' passes, details->'autodedup'->>'last_pass_at' last_pass_at,
          details->'autodedup'->>'in_flight_s' in_flight_s
     from public.worker_heartbeats where worker = 'realtime-worker';
   ```
   Expect `live_lease` null, `bootstrap` false (true on a morning under step 3b's override), and the same
   `passes` and `last_pass_at` in both reads with `in_flight_s` null. If `bootstrap` is true without that
   override, move the window a day (the dedup session's rule). Section 0 refuses unless the brake is 0,
   and until a held lease expires (593:114-116). A pass ends by itself within 17.5 minutes; a lease from
   a pass that died lasts up to 40 minutes, and a `dispatch:` or seed holder up to 5 hours. If a lease
   will outlast the dispatch, re-time the dispatch to 05:31:30–05:36:30, between the 05:30 rebuilds and
   the 05:37 map rebuild, or move the day.

### 05:15–05:20, checks C1–C11 (any other answer stops the window)

- **C1, brake and window:** expect `0`, `false`, null, null. On a morning under step 3b's override, expect
  `0`, `true`, null, null.
  ```sql
  select (select value #>> '{}' from public.app_settings where key = 'realtime_autodedup_interval_seconds') interval_s,
         (select value #>> '{}' from autodedup.settings where key = 'rt_bootstrap:rt') bootstrap,
         (select string_agg(holder, '; ') from autodedup.rt_lease where expires_at > now()) live_lease,
         (select string_agg(holder, '; ') from public.property_maintenance_lease where holder like 'full:%' and expires_at > now()) sweep;
  ```
- **C2, the index gate:** expect both keyset indexes at 0 scans and null, `stats_reset` null, and
  `listings_source_id_idx` present. The feed index's own count MOVES and that is expected: since W5 its
  only scans are the planner's prefix choice for the broker lane's per-portal statements on maxima /
  bezrealitky / mmreality / remax (hunted 2026-10-08 with the per-minute sampler `w6-feed-watch`);
  `listings_source_id_idx` (source, id) takes those over, as it already serves the other five portals.
  After a stats reset the keyset half proves nothing: write that down. One MCP call per SELECT:
  ```sql
  select indexrelname, idx_scan, last_idx_scan from pg_stat_user_indexes
   where indexrelname in ('listings_portal_feed_idx', 'properties_cat_last_seen_keyset_idx', 'properties_last_seen_keyset_idx');
  select to_regclass('public.listings_source_id_idx') as substitute, stats_reset from pg_stat_database where datname = current_database();
  ```
- **C3, the Watchdog plan:** no `*_last_seen_keyset_idx` in it (2026-10-08: `properties_cat_first_seen_keyset_idx`).
  ```sql
  explain (costs off) select max(first_seen_at), count(*) from (select l.first_seen_at from properties_public l
   where exists (select 1 from listing_location sl where sl.listing_id = l.listing_id and (sl.geom is not null or sl.country_status = 'foreign'))
     and l.category_main = any('{byt}'::text[]) and l.category_type = 'prodej' and l.disposition = any('{1+kk,1+1,2+kk,2+1,3+kk}'::text[])
     and l.all_sources && '{sreality}'::text[] and l.usable_area >= 35.0 and l.usable_area <= 68.0 and l.first_seen_at > now() - interval '1 day'
   order by l.first_seen_at asc limit 500) sub;
  ```
- **C4, dependants.** A normal dependant blocks a plain DROP; a column's removal also takes its indexes
  and constraints unasked, so a column lists all of its own. Each target must show exactly these (read
  2026-10-08 between 06:24 and 06:29), and every other target null:
  - `property_status_events`: its own view.
  - `log_property_status_event()`: its trigger.
  - `assets`: its own check and the two asset FKs.
  - `asset_membership_events`: its own two checks.
  - `listings.discovery_seq`: the feed view and `listings_portal_feed_idx` (section 1 drops it first).
  - `browse_list_mf(bigint)`: the feed view.
  - `listing_discovery_seq` and `listing_detail_queue.discovery_seq`: the queue column's default.
  - `properties.asset_id`: its FK and its partial index. `properties.distinct_site_count`: its default.
  - `property_pipeline.note`: its check and the card view.
  ```sql
  with t(label, classid, objid, objsubid) as (
    select c.oid::regclass::text, 'pg_class'::regclass::oid, c.oid, 0 from pg_class c
     where c.oid in (to_regclass('public.property_status_events'), to_regclass('public.property_status_events_public'), to_regclass('public.assets'),
                     to_regclass('public.asset_membership_events'), to_regclass('public.listing_feed_public'), to_regclass('public.listing_discovery_seq'),
                     to_regclass('public.listings_portal_feed_idx'), to_regclass('public.properties_cat_last_seen_keyset_idx'), to_regclass('public.properties_last_seen_keyset_idx'))
    union all select a.attrelid::regclass::text || '.' || a.attname, 'pg_class'::regclass::oid, a.attrelid, a.attnum from pg_attribute a
     where (a.attrelid = 'public.properties'::regclass and a.attname in ('asset_id', 'price_per_m2_source_listing_id', 'distinct_site_count'))
        or (a.attrelid in ('public.listings'::regclass, 'public.listing_detail_queue'::regclass) and a.attname = 'discovery_seq')
        or (a.attrelid = 'public.property_pipeline'::regclass and a.attname = 'note')
    union all select p.oid::regprocedure::text, 'pg_proc'::regclass::oid, p.oid, 0 from pg_proc p
     where p.pronamespace = 'public'::regnamespace and p.proname in ('listing_feed_visible', 'browse_map_cells', 'price_per_m2_source_id', 'log_property_status_event', 'browse_list_mf'))
  select left(t.label, 60) label, string_agg(distinct pg_describe_object(d.classid, d.objid, d.objsubid), ' | ') dependants
    from t left join pg_depend d on d.refclassid = t.classid and d.refobjid = t.objid
                                 and (t.objsubid = 0 or d.refobjsubid = t.objsubid) and (d.deptype = 'n' or t.objsubid <> 0)
   group by 1 order by 1;
  ```
  Then, one MCP call per SELECT, the function bodies, the view texts, the note's readers (by name, or as
  NEW / OLD in a trigger on its table) and any constraint over a dropped column and another. Expect these,
  every other name in none, only `property_status_events_public` and `listing_feed_public` among the
  views, and `note_readers` and `wider_constraints` null:
  - `listing_feed_public` in `listing_feed_visible`.
  - `property_status_events` in `log_property_status_event`.
  - `listing_ids_filter` and `browse_map_cells` in `browse_map_cells` only.
  ```sql
  select k.kw, string_agg(distinct p.proname, ', ') fns from (values ('discovery_seq'), ('listing_feed_public'), ('listing_feed_visible'),
    ('price_per_m2_source'), ('property_status_events'), ('asset_membership_events'), ('asset_id'), ('distinct_site_count'),
    ('listing_ids_filter'), ('browse_map_cells'), ('property_pipeline_public'), ('pipeline_board_public')) k(kw)
    left join pg_proc p on p.prosrc ilike '%' || k.kw || '%' and p.pronamespace not in ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
   group by 1 order by 1;
  select string_agg(c.oid::regclass::text, ', ') from pg_class c
   where c.relkind in ('v', 'm') and c.relnamespace not in ('pg_catalog'::regnamespace, 'information_schema'::regnamespace)
     and pg_get_viewdef(c.oid) ~* '(property_status_events|asset_membership_events|\massets\M|price_per_m2_source|distinct_site_count|listing_feed|discovery_seq|listing_ids_filter)';
  select (select string_agg(p.oid::regprocedure::text, ', ') from pg_proc p
           where p.pronamespace not in ('pg_catalog'::regnamespace, 'information_schema'::regnamespace) and p.prosrc ~* '\mnote\M'
             and (p.prosrc ~* '\mproperty_pipeline\M' or p.oid in (select tgfoid from pg_trigger where tgrelid = 'public.property_pipeline'::regclass))) note_readers,
         (select string_agg(k.conrelid::regclass::text || '.' || k.conname::text, ', ') from pg_constraint k
           where k.conrelid in ('public.properties'::regclass, 'public.listings'::regclass, 'public.listing_detail_queue'::regclass, 'public.property_pipeline'::regclass)
             and cardinality(k.conkey) > 1
             and exists (select 1 from pg_attribute a where a.attrelid = k.conrelid and a.attnum = any (k.conkey)
                          and a.attname in ('asset_id', 'price_per_m2_source_listing_id', 'distinct_site_count', 'discovery_seq', 'note'))) wider_constraints;
  ```
- **C5, notes:** `select count(*) cards, count(*) filter (where note is not null) notes from public.property_pipeline;`
  On 2026-10-08 that was 133 cards and 0 notes. If a card holds a note, section 6 skips and says so.
- **C6, no new reader.** Expect 789 and 177 calls, as from 04:01 to 06:29 on 2026-10-08: the status
  log's view and the feed, top-level statements only (`pg_stat_statements.track` is `top`). A higher
  count stops the window. Read them with `dealloc` (86 on 2026-10-10 18:27, and rising): a lower count
  with a higher `dealloc` means the full store (4,751–4,985 of its 5,000 entries) evicted entries, and
  that is not a failed check. The table's own counters are recorded, not judged: every backup dump and
  C7 count scans it, and the trigger reads it when a detach brings a merged property back. One MCP call
  per SELECT:
  ```sql
  select seq_scan, last_seq_scan, idx_scan, last_idx_scan from pg_stat_user_tables where relname = 'property_status_events';
  select coalesce(sum(calls) filter (where query ~* 'property_status_events_public'), 0) pse_calls,
         coalesce(sum(calls) filter (where query ~* 'listing_feed_(public|visible)' and query !~* '^\s*(grant|revoke|drop|create|alter|comment)'), 0) feed_calls,
         (select dealloc from extensions.pg_stat_statements_info) dealloc
    from extensions.pg_stat_statements;
  ```
- **C7, the backup read back (step 6).** `<start>` is five minutes before the backup run's `createdAt`
  (`gh run view <id> --json createdAt`); `live / since` are the live rows and those added since `<start>`.
  - `assets` and `asset_membership_events`: 0. `property_pipeline`: the cards of C5, exactly.
  - `property_status_events` and `properties`: `live − since ≤ logged ≤ live` (both only grow).
  - `listing_detail_queue`: logged, not compared (it drains and refills by the minute).
  - `listings`: against the estimate only, because an exact count is a 1.4 GB scan. On 2026-10-10 it was
    996,465 × (1 − 0.535) ≈ 463k, and that day's rehearsal logged 461,041.
  ```sql
  select (select count(*) || ' / ' || count(*) filter (where event_at >= '<start>') from public.property_status_events) pse_live_since,
         (select count(*) from public.assets) a, (select count(*) from public.asset_membership_events) ame,
         (select count(*) || ' / ' || count(*) filter (where created_at >= '<start>') from public.properties) props_live_since,
         (select count(*) from public.property_pipeline) cards,
         (select reltuples::bigint from pg_class where oid = 'public.listings'::regclass) listings_est,
         (select null_frac from pg_stats where schemaname = 'public' and tablename = 'listings' and attname = 'discovery_seq') seq_null_frac;
  ```
- **C8, the pinned bodies.** Section 0 checks these too.
  Expect `def_md5` `044d7fb46125b5d68dd3cf19dca31812`, `src_md5` `0a1cf3a94fce99941adca7cbe1f375b9`,
  `acl` `{postgres=X/postgres,authenticated=X/postgres,service_role=X/postgres}`, then
  `board_md5` `06c950df3e18e0f58c4be6b06973600e` and `card_md5` `54671707e2d2c12d1a47971325e9a784`.
  Any other value means another session restated a body: regenerate section 5 or 6 from the live text.
  One MCP call per SELECT:
  ```sql
  select md5(pg_get_functiondef(oid)) def_md5, md5(prosrc) src_md5, proacl::text acl from pg_proc where pronamespace = 'public'::regnamespace and proname = 'browse_map_cells';
  select md5(pg_get_viewdef('public.pipeline_board_public'::regclass)) board_md5, md5(pg_get_viewdef('public.property_pipeline_public'::regclass)) card_md5;
  ```
  Then run `git fetch origin` and the two commands below. On main the grep may show only
  `.github/workflows/migrations.yml:504` (a comment), `frontend/src/lib/measure.ts:12`, `queries.test.ts:883,886`,
  `toolkit/filter_registry.py:1899` and `property_carriers.py`'s three NOT_CARRIED entries, which this PR
  removes. Any other hit is a new reader, but read a bare `asset_id` hit (such as `photo_asset_id`) before
  it stops the window. The `ls-tree` must show only `migrations/594_gone_ledger_hysteresis.sql`.
  ```bash
  git grep -n -E -e 'property_status_events|asset_membership_events|asset_id|listing_feed_(public|visible)|discovery_seq|price_per_m2_source|distinct_site_count|listing_ids_filter|last_seen_keyset|portal_feed_idx' -e 'pp\.note\b|property_pipeline[a-z_]*.{0,80}\bnote\b' -e '(from|join|into|update)\s+(public\.)?assets\b' origin/main -- api toolkit scraper scripts autodedup location_data frontend/src chrome-extension .github/workflows 'migrations/59[5-9]_*'
  git ls-tree --name-only origin/main migrations/ | grep -E '/59[3-9]_'
  ```
- **C9, the size baseline.** On 2026-10-10 18:03 these were 137,149,631,635, 3,171,336,192 and 12,058,083,328 bytes.
  ```sql
  select pg_database_size(current_database()) db_bytes, pg_total_relation_size('public.properties') properties_bytes, pg_total_relation_size('public.listings') listings_bytes;
  ```
- **C10, the dispatch moment.** On the Large instance (compute changed 2026-10-10), the 05:15
  `browse-list-rebuild` ends about 05:16:00–05:16:30 and the 05:20 `refresh-health-dashboard` about
  05:20:30–05:20:50 (at worst it times out at 05:25). Dispatch step 10 at 05:20:50 or later, once the
  05:20 refresh reads `succeeded`. Also expect `refresh-location-pin-audit` at 05:25 (about 11–16 s,
  reads `listings`) and the bazos index walk, which was running during 05:20–05:30 on 9 of the last 10
  mornings and writes `listings` in 250-row autocommit updates: one or two `lock timeout` retries are
  normal. The second query also lists sessions `idle in transaction`, which matters because
  `idle_in_transaction_session_timeout` is 0: such a session never ends by itself and would fail all 30
  retries. pg_cron jobs show up there too, because `cron.use_background_workers` is off. Stop the
  dispatch for any transaction older than 30 s that is not a cron job you can name. One MCP call per
  SELECT:
  ```sql
  select j.jobname, d.status, d.start_time at time zone 'utc' started, d.end_time at time zone 'utc' ended
    from cron.job_run_details d join cron.job j using (jobid) where d.start_time > now() - interval '20 minutes' order by d.start_time;
  select pid, now() - xact_start age, left(query, 80) from pg_stat_activity
   where backend_type = 'client backend' and state <> 'idle' and xact_start < now() - interval '10 seconds' order by age desc;
  ```
- **C11, the PR, read at 05:15.** `gh pr view 1732 --json isDraft,mergeable,mergeStateStatus,headRefOid`
  must read `true`, `MERGEABLE`, `CLEAN` and the head that CI last passed. #1732 stays a draft until 593
  has landed (step 12).

### 05:20:30, the apply

9. **Dry run again:** step 2. Dispatch step 10 only after `gh run watch <id> --exit-status` shows this
   run completed, and only when `gh run list --workflow apply_migration.yml --limit 5 --json status,databaseId`
   shows nothing queued or in progress: the concurrency group `apply-migration` (cancel-in-progress false,
   apply_migration.yml:45-47) lets a newer dispatch cancel an older pending one.
10. **Apply:** `gh workflow run apply_migration.yml --ref feature/merge-sprint-w6 -f file=593_merge_sprint_w6_drops.sql -f dry_run=false -f confirm=APPLY`,
    then `gh run watch <id> --exit-status`. psql starts about 33 s after the dispatch, and its NOTICEs,
    `593: landed` among them, print only after psql exits. Write down the `headSha` of the run that prints
    `593: landed` (`gh run view <id> --json headSha --jq .headSha`).
    - **What the log shows.** On success: `593: landed`, `applied migrations/593_merge_sprint_w6_drops.sql`,
      then the receipt `receipt OK: 3 object(s) present`. The receipt proves nothing about the drops,
      because those three objects existed before: the proof is `593: landed` plus step 11's P1.
    - **`canceling statement due to lock timeout`.** This is the retry working (30 tries, 20 s apart). A
      concurrent index drop that times out leaves its index invalid until the next try.
    - **`593 refused: …`, or any other error** (`deadlock detected` is not retried). Its section and
      every later section did not run, and every earlier one stands. Read the message, fix the cause,
      and dispatch again: the file is idempotent.
    - **`property_pipeline.note KEPT`.** Section 6 skipped because a card holds a note. A later migration drops the note.
11. **The receipt, by hand (P1).** Expect every relation, function and trigger null or 0, and `cols_left` null
    (`property_pipeline.note` if section 6 skipped). Expect `map_defs` 1, `map_md5` `d60bf1ec13b86d3a7afeb0dee0695c26`
    and `pp_md5` `88093f6916bb54a0a9e4333fbf0cd544` (`54671707…` if section 6 skipped).
    ```sql
    select to_regclass('public.property_status_events') pse, to_regclass('public.property_status_events_public') psev, to_regclass('public.assets') a,
           to_regclass('public.asset_membership_events') ame, to_regclass('public.listing_feed_public') feed, to_regclass('public.listing_discovery_seq') seq,
           to_regclass('public.listings_portal_feed_idx') fidx, to_regclass('public.properties_cat_last_seen_keyset_idx') k1, to_regclass('public.properties_last_seen_keyset_idx') k2,
           to_regprocedure('public.listing_feed_visible()') fv, to_regprocedure('public.log_property_status_event()') lse,
           to_regprocedure('public.price_per_m2_source_id(numeric,numeric,bigint)') src,
           (select count(*) from pg_trigger where tgname = 'properties_log_status_event') trg,
           (select string_agg(table_name || '.' || column_name, ',') from information_schema.columns where table_schema = 'public'
             and ((table_name = 'properties' and column_name in ('asset_id', 'price_per_m2_source_listing_id', 'distinct_site_count'))
               or (table_name in ('listings', 'listing_detail_queue') and column_name = 'discovery_seq')
               or (table_name = 'property_pipeline' and column_name = 'note'))) cols_left,
           (select count(*) from pg_proc where pronamespace = 'public'::regnamespace and proname = 'browse_map_cells') map_defs,
           (select md5(prosrc) from pg_proc where pronamespace = 'public'::regnamespace and proname = 'browse_map_cells') map_md5,
           md5(pg_get_viewdef('public.property_pipeline_public'::regclass)) pp_md5;
    ```

### The merge, still braked

12. **Merge the PR** as W5 was (never `--admin`), only if the branch's 593 is the text production ran
    (else stop, rule 1). #1732 stays a draft until 593 has landed (C11); marking it ready re-runs no
    required check, because those workflows trigger on a bare `pull_request`:
    ```bash
    git fetch origin && git diff --quiet <headSha> origin/feature/merge-sprint-w6 -- migrations/593_merge_sprint_w6_drops.sql && gh pr ready 1732 && gh pr merge 1732 --squash
    ```
    The merge redeploys the worker, and a pass killed mid-flight would strand the lease for up to 40
    minutes. Confirm the rollout with
    `gh api repos/waiff/sreality/commits/<sha>/status --jq '.statuses[] | "\(.context) \(.state)"'`:
    `API - sreality`, `vite - sreality` and `realtime-worker - sreality` must all read `success`.
13. **Brake off.** The operator sets `realtime_autodedup_interval_seconds` back to `60` on /settings.
    The same holds when the window stops for the day, before the apply or halfway: the engine, and a
    build with it, stays paused until the operator does it.
14. **The next pass, about 5–6 minutes after the brake comes off.** The lane wakes within 60 s and a pass
    takes about 270 s. Expect `last_pass_at` after the restore, `ran` true, `failed` 0 (the counters
    restart because the merge redeploys the worker) and no `failed` row. While the build runs, expect
    `reconcile` `bootstrap`, `merged` 0 and `claimed` about 25. One MCP call per SELECT:
    ```sql
    select beat_at, details->'autodedup'->>'passes' passes, details->'autodedup'->>'failed_passes' failed,
           details->'autodedup'->>'last_pass_at' last_pass_at, details->'autodedup'->'last'->>'ran' ran,
           details->'autodedup'->'last'->>'reconcile' reconcile, details->'autodedup'->'last'->>'merged' merged,
           details->'autodedup'->'last'->>'claimed' claimed, details->'autodedup'->>'last_failure_at' last_failure_at
      from public.worker_heartbeats where worker = 'realtime-worker';
    select outcome, count(*) from autodedup.applied_merges where applied_at > '<restore time>' group by 1;
    ```

## After

15. **Smoke reads.**
    - `select (public.browse_map_cells(category_main_filter => '{byt}'::text[], category_type_filter => 'prodej', point_budget => 0))->>'total';`
    - `select count(*) from public.pipeline_board_public;` returns the number of cards.
    - `select count(*) from public.property_pipeline_public;`
    - `max(scraped_at)` from `listing_snapshots` and `max(enqueued_at)` from `listing_detail_queue`
      move after 05:30, and `dirty_properties` drains.
    - The Postgres logs since 05:20 (MCP `query_logs`, source `postgres_logs`) show no `does not exist` naming a dropped object.
16. **PostgREST has reloaded.** The file ends with `pg_notify('pgrst', 'reload schema')`, and the DDL
    event triggers reload too. A fresh tab's map read answers with no PGRST202. If PostgREST still names
    `listing_ids_filter` or a dropped relation, send `select pg_notify('pgrst', 'reload schema');` once,
    with the OK. Tabs opened before W5 fail until they are reloaded; that is accepted (PROGRAM.md §10).
17. **Browser smoke on the production SPA, read-only.**
    - Browse: the default view, and one portal (maxima) with "Newest first".
    - The map in cells and in points, and the Stats tab.
    - A property page (chart, pipeline mark), `/pipeline` and the Watchdog page.
    - No 4xx or 5xx on app requests.
18. **Disk reclaimed.** Run C9 again. Expect about −1.74 GB for the database: the keyset indexes
    1.36 GB, the status log 230 MB, the feed index 149 MB. Expect `properties` at about 1.81 GB.
    About 13 MB of dropped column bytes are reused as rows are rewritten.
19. **The drift check.** Dispatch `verify_pipeline.yml`; `migration_drift` should read ok. A run between
    the apply and the merge reads WARN on 567 and 584 (`listing_feed_public`) until main carries 593.
20. **The hand-over note.** Write the times of the brake, the apply, the merge, the brake off and the first
    pass into `merge-sprint-handover-to-autodedup`, and record there:
    - The status-log rows written between the dump and the apply, which no backup holds (roughly 20–50, step 4).
    - The dedup session's `properties.published_at` drop is unblocked, but `properties_gate_cover_idx`
      includes that column, so its replacement comes first.
    - #1655 rebases.
    - #1634 removes its `listing_feed_public` reads (its 575 pre- and post-conditions) and takes a number above 593.
    Then mark W6 applied in PROGRAM.md §4, §5 and §6 and in `roadmap/merge-sprint.md`.
21. **The sampler's table goes, with the operator's OK.** The reader hunt's per-minute job (2026-10-08/09)
    was unscheduled on 2026-10-09 once it had confirmed the reader; its unlogged table (41 MB) is not part
    of the schema: `drop table if exists public.w6_feed_watch;`, then
    `select count(*) from cron.job where jobname = 'w6-feed-watch'` must be 0.

**If it stops halfway.** Every section is its own transaction (the concurrent index drops are their own
statements), so a stop leaves a consistent partial state that nothing reads. Re-dispatch step 10 later.
A restore is a new forward migration fed from the R2 backup (rule 1); no step here is undone by hand.
