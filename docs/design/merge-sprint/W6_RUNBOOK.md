# W6 runbook: the one destructive window (migration 593)

The order for the day the operator names (PROGRAM.md §5's W6 gate, §6's list, §7's brake). The file is
`migrations/593_merge_sprint_w6_drops.sql` on branch `feature/merge-sprint-w6`. Every SQL read below
goes through MCP `execute_sql` and starts with `SET statement_timeout='30s';`. The only production
writes are the brake (steps 5 and 13), which this session runs only with the operator's OK, otherwise
he sets it on /settings, the apply (step 10), which runs only after his "yes, apply it" (step 7), and at
most one PostgREST reload (step 16). Baselines were read on 2026-10-08 between 03:51 and 06:29 UTC.

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

4. **04:50–05:10, the backup.** Run step 3 with `-f label=merge-sprint-w6`. Run it as late as you can:
   the status log gains a few hundred rows an hour (459 in the hour to 06:29 on 2026-10-08) until
   section 2 drops it.
5. **05:00, the brake, whatever step 4's state.** It only pauses the engine's merges and step 13 lifts
   it, so it waits for neither the backup nor step 7. The operator sets it on /settings, or this
   session runs it with his OK:
   ```sql
   update public.app_settings set value = '0'::jsonb, updated_at = now(), updated_by = 'merge-sprint W6 brake'
    where key = 'realtime_autodedup_interval_seconds';
   ```
6. **Read the backup back** with C7. A `FAILED` line or a count outside C7's rule stops the day.
7. **The operator writes "yes, apply it" in his own message.** Steps 9 and 10 do not run without it.
8. **The lease and the bootstrap.** Run this twice, two minutes apart:
   ```sql
   select (select string_agg(holder || ' until ' || expires_at, '; ') from autodedup.rt_lease where expires_at > now()) live_lease,
          (select value #>> '{}' from autodedup.settings where key = 'rt_bootstrap:rt') bootstrap,
          details->'autodedup'->>'passes' passes, details->'autodedup'->>'last_pass_at' last_pass_at,
          details->'autodedup'->>'in_flight_s' in_flight_s
     from public.worker_heartbeats where worker = 'realtime-worker';
   ```
   Expect `live_lease` null, `bootstrap` false, and the same `passes` and `last_pass_at` in both reads
   with `in_flight_s` null. A pass ends by itself within 17.5 minutes. If `bootstrap` is true, move the
   window a day (the dedup session's rule). Section 0 refuses unless the brake is 0 and no lease is held.

### 05:15–05:20, checks C1–C10 (any other answer stops the window)

- **C1, brake and window:** expect `0`, `false`, null, null.
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
  After a stats reset the keyset half proves nothing: write that down.
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
  Then the function bodies, the view texts, the note's readers (by name, or as NEW / OLD in a trigger on
  its table) and any constraint over a dropped column and another. Expect these, every other name in
  none, only `property_status_events_public` and `listing_feed_public` among the views, and
  `note_readers` and `wider_constraints` null:
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
  count stops the window. The table's own counters are recorded, not judged: every backup dump and C7
  count scans it, and the trigger reads it when a detach brings a merged property back.
  ```sql
  select seq_scan, last_seq_scan, idx_scan, last_idx_scan from pg_stat_user_tables where relname = 'property_status_events';
  select coalesce(sum(calls) filter (where query ~* 'property_status_events_public'), 0) pse_calls,
         coalesce(sum(calls) filter (where query ~* 'listing_feed_(public|visible)' and query !~* '^\s*(grant|revoke|drop|create|alter|comment)'), 0) feed_calls
    from extensions.pg_stat_statements;
  ```
- **C7, the backup read back (step 6).** `<start>` is five minutes before the backup run's `createdAt`
  (`gh run view <id> --json createdAt`); `live / since` are the live rows and those added since `<start>`.
  - `assets` and `asset_membership_events`: 0. `property_pipeline`: the cards of C5, exactly.
  - `property_status_events` and `properties`: `live − since ≤ logged ≤ live` (both only grow).
  - `listing_detail_queue`: logged, not compared (it drains and refills by the minute).
  - `listings`: against the estimate only, because an exact count is a 1.4 GB scan. On 2026-10-08 06:29
    it was 961,105 × (1 − 0.525) ≈ 457k.
  ```sql
  select (select count(*) || ' / ' || count(*) filter (where event_at >= '<start>') from public.property_status_events) pse_live_since,
         (select count(*) from public.assets) a, (select count(*) from public.asset_membership_events) ame,
         (select count(*) || ' / ' || count(*) filter (where created_at >= '<start>') from public.properties) props_live_since,
         (select count(*) from public.property_pipeline) cards,
         (select reltuples::bigint from pg_class where oid = 'public.listings'::regclass) listings_est,
         (select null_frac from pg_stats where schemaname = 'public' and tablename = 'listings' and attname = 'discovery_seq') seq_null_frac;
  ```
- **C8, the pinned bodies.** Section 0 checks these too.
  Expect `044d7fb46125b5d68dd3cf19dca31812`, `0a1cf3a94fce99941adca7cbe1f375b9`,
  `{postgres=X/postgres,authenticated=X/postgres,service_role=X/postgres}`, then
  `06c950df3e18e0f58c4be6b06973600e` and `54671707e2d2c12d1a47971325e9a784`.
  Any other value means another session restated a body: regenerate section 5 or 6 from the live text.
  ```sql
  select md5(pg_get_functiondef(oid)), md5(prosrc), proacl::text from pg_proc where pronamespace = 'public'::regnamespace and proname = 'browse_map_cells';
  select md5(pg_get_viewdef('public.pipeline_board_public'::regclass)), md5(pg_get_viewdef('public.property_pipeline_public'::regclass));
  ```
  Then run `git fetch origin` and the grep below. On main it may show only `frontend/src/lib/measure.ts:12`,
  `queries.test.ts:883,886`, `toolkit/filter_registry.py:1899` and `property_carriers.py`'s three
  NOT_CARRIED entries, which this PR removes. Any other hit is a new reader.
  ```bash
  git grep -n -E 'property_status_events|asset_membership_events|asset_id|listing_feed_(public|visible)|discovery_seq|price_per_m2_source|distinct_site_count|listing_ids_filter|last_seen_keyset|portal_feed_idx' origin/main -- api toolkit scraper scripts autodedup location_data frontend/src chrome-extension
  ```
- **C9, the size baseline.** On 2026-10-08 05:19 these were 135,677,897,875, 3,073,794,048 and 12,047,605,760 bytes.
  ```sql
  select pg_database_size(current_database()), pg_total_relation_size('public.properties'), pg_total_relation_size('public.listings');
  ```
- **C10, the dispatch moment.** Dispatch once the 05:15 `browse-list-rebuild` and the 05:20
  `refresh-health-dashboard` have ended (7-day ends: 05:18–05:19:31 and 05:21:26–05:22:11), with no
  transaction older than about 30 s.
  ```sql
  select j.jobname, d.status, d.start_time at time zone 'utc' started, d.end_time at time zone 'utc' ended
    from cron.job_run_details d join cron.job j using (jobid) where d.start_time > now() - interval '20 minutes' order by d.start_time;
  select pid, now() - xact_start age, left(query, 80) from pg_stat_activity
   where backend_type = 'client backend' and state <> 'idle' and xact_start < now() - interval '10 seconds' order by age desc;
  ```

### 05:20:30, the apply

9. **Dry run again:** step 2.
10. **Apply:** `gh workflow run apply_migration.yml --ref feature/merge-sprint-w6 -f file=593_merge_sprint_w6_drops.sql -f dry_run=false -f confirm=APPLY`,
    then `gh run watch <id> --exit-status`. psql starts 1–2 minutes after the dispatch. Write down the
    `headSha` of the run that prints `593: landed` (`gh run view <id> --json headSha --jq .headSha`).
    - **What the log shows.** On success: `593: landed`, `applied migrations/593_merge_sprint_w6_drops.sql`,
      then the receipt `receipt OK: 3 object(s) present`.
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
    (else stop, rule 1):
    ```bash
    git fetch origin && git diff --quiet <headSha> origin/feature/merge-sprint-w6 -- migrations/593_merge_sprint_w6_drops.sql && gh pr merge <n> --squash
    ```
    The merge redeploys the worker, and a pass killed mid-flight would strand the lease for up to 40
    minutes. Confirm the rollout with
    `gh api repos/waiff/sreality/commits/<sha>/status --jq '.statuses[] | "\(.context) \(.state)"'`:
    `API - sreality`, `vite - sreality` and `realtime-worker - sreality` must all read `success`.
13. **Brake off** (/settings, or this session with the OK):
    ```sql
    update public.app_settings set value = '60'::jsonb, updated_at = now(), updated_by = 'merge-sprint W6 brake off'
     where key = 'realtime_autodedup_interval_seconds';
    ```
14. **The next pass, within about 2 minutes.** Expect `last_pass_at` after the restore, `ran` true,
    `failed` 0 (the counters restart with the worker) and no `failed` row.
    ```sql
    select beat_at, details->'autodedup'->>'passes' passes, details->'autodedup'->>'failed_passes' failed,
           details->'autodedup'->>'last_pass_at' last_pass_at, details->'autodedup'->'last'->>'ran' ran,
           details->'autodedup'->>'last_failure_at' last_failure_at
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
    - The Postgres logs since 05:20 (MCP `get_logs`, service postgres) show no `does not exist` naming a dropped object.
16. **PostgREST has reloaded.** The file ends with `pg_notify('pgrst', 'reload schema')`, and the DDL
    event triggers reload too. A fresh tab's map read answers with no PGRST202. If PostgREST still names
    `listing_ids_filter` or a dropped relation, send `select pg_notify('pgrst', 'reload schema');` once,
    with the OK. Tabs opened before W5 fail until they are reloaded; that is accepted (PROGRAM.md §10).
17. **Browser smoke on the production SPA, read-only.**
    - Browse: the default view, and one portal (maxima) with "Newest first".
    - The map in cells and in points, and the Stats tab.
    - A property page (chart, pipeline mark), `/pipeline` and the Watchdog page.
    - No 4xx or 5xx on app requests.
18. **Disk reclaimed.** Run C9 again. Expect about −1.66 GB for the database: the keyset indexes
    1.29 GB, the status log 226 MB, the feed index 147 MB. Expect `properties` at about 1.79 GB.
    About 13 MB of dropped column bytes are reused as rows are rewritten.
19. **The drift check.** Dispatch `verify_pipeline.yml`; `migration_drift` should read ok. A run between
    the apply and the merge reads WARN on 567 and 584 (`listing_feed_public`) until main carries 593.
20. **The hand-over note.** Write the times of the brake, the apply, the merge, the brake off and the first
    pass into `merge-sprint-handover-to-autodedup`, and record there:
    - The dedup session's `properties.published_at` drop is unblocked, but `properties_gate_cover_idx`
      includes that column, so its replacement comes first.
    - #1655 rebases.
    - #1634 removes its `listing_feed_public` reads (its 575 pre- and post-conditions) and takes a number above 593.
    Then mark W6 applied in PROGRAM.md §4, §5 and §6 and in `roadmap/merge-sprint.md`.
19. **The sampler goes.** The reader hunt's per-minute job and its table (2026-10-08) are not part of the
    schema: `select cron.unschedule('w6-feed-watch'); drop table if exists public.w6_feed_watch;`.

**If it stops halfway.** Every section is its own transaction (the concurrent index drops are their own
statements), so a stop leaves a consistent partial state that nothing reads. Re-dispatch step 10 later.
A restore is a new forward migration fed from the R2 backup (rule 1); no step here is undone by hand.
