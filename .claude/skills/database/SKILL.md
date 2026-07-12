---
name: database
description: Use when working with this project's Postgres/Supabase database — reading data cheaply (psql vs the Supabase MCP), the three connection modes (connect vs connect_session vs the tenant pool, transaction- vs session-mode pooler, prepare_threshold), migrations as the source of truth, the additive-vs-destructive migration safety gate, multi-tenancy (account_id scoping, RLS, tenant role, tenant pool, composite PKs), pooler-safe locking (advisory locks vs lease-row CAS), or schema/column conventions (typed enum labels, geom-derived admin hierarchy, the shared street extractor, stored blocking keys, legacy booleans). Triggers on: migration, apply_migration, ALTER/CREATE TABLE, psycopg, pooler, backfill, PostGIS, RLS, account_id, tenant, tenant pool, advisory lock, connection mode, verifying data with a SELECT, schema or column change.
---

# Database

Everything for working with the Supabase Postgres store of record: reading cheaply, the
three connection modes, migrations as the source of truth, the migration safety gate,
multi-tenancy, and schema conventions. Full architectural rationale for the data model is
in `docs/architecture.md`.

## Reads: prefer psql over the Supabase MCP (cost)

Routine reads should NOT go through the Supabase MCP — its results persist in context for
the rest of the session (a large share of past token spend). Use `psql` against
`$SUPABASE_DB_URL` via Bash, piped through `head`/`grep` so only compact text enters
context:

```bash
psql "$SUPABASE_DB_URL" -c "select count(*) from listings where is_active;"
psql "$SUPABASE_DB_URL" -c "select source, count(*) from listings group by 1 order by 2 desc;"
psql "$SUPABASE_DB_URL" -c "select max(scraped_at) from scrape_runs where index_pages>0;"
psql "$SUPABASE_DB_URL" -c "select run_type, index_pages, new_listings, updated_listings, errors, scraped_at from scrape_runs order by scraped_at desc limit 5;"
psql "$SUPABASE_DB_URL" -c "\d listings" | head -60
```

- **The Supabase MCP is reserved for** applying migrations, backfill UPDATEs (under the
  safety policy below), and anything needing its confirmation gate. It *can* run SELECTs,
  but after a heavy MCP phase run `/compact`; in a session that never touches the DB,
  disable the server via `/mcp`.
- The production-safety warnings below are unchanged.

## Database access

We connect directly to Supabase Postgres with `psycopg` v3 (not the Supabase REST
client), for two reasons:
- **PostGIS support:** inserting `geography(point, 4326)` is one line of SQL with
  `ST_SetSRID(ST_MakePoint(lon, lat), 4326)`. The PostgREST equivalent needs a stored
  procedure or fragile GeoJSON casting.
- **Atomic transactions:** writing `listings`, `listing_snapshots`, and `images` for one
  listing happens inside a single transaction. The REST client cannot span tables
  atomically.

Do not introduce `supabase-py` without an explicit reason and a discussion.

**Three connection modes** now exist — pick by who's calling and whether the call is
tenant-scoped:
- `connect()` (`scraper/db.py`) — the **default for everything service-role** (scrape_run
  bookkeeping, bazos, images, recompute, most of the API, scripts). Points at
  `SUPABASE_DB_URL` (the **Transaction-mode pooler**, port 6543) with
  `prepare_threshold=None`. Disabling auto-prepare is **required** there: PgBouncer/
  Supavisor rebinds connections between queries, so a cached prepared statement would trip
  `DuplicatePreparedStatement`. Takes `attempts`/`retry_delay` for bounded retry on a flaky
  connect handshake (PR #663).
- `connect_session()` — **only** for the scraper's hot detail-write loop (the long-lived
  connection in `scraper/main.py:_run_full`). Points at `SUPABASE_DB_SESSION_URL` (the
  **Session-mode pooler**, port 5432) and leaves `prepare_threshold` at psycopg3's default,
  so the repeated upsert + spatial SQL gets server-side **prepared once and reused** across
  every listing in the run. The session pooler gives each client a dedicated backend, so
  prepared statements are safe there. Falls back to `connect()` if
  `SUPABASE_DB_SESSION_URL` is unset.
- `tenant_conn` (`api/tenant_pool.py`, FastAPI dependency, Phase 1 increment 3, migration
  293) — the RLS-scoped path for per-account API routes. Connects to
  `TENANT_POOL_DB_URL` as the `tenant_pool` role (`LOGIN NOINHERIT`, zero data access on
  its own), `autocommit=False`, `prepare_threshold=None`. Inside **one transaction per
  request** it runs `SET LOCAL ROLE authenticated` then
  `SELECT set_config('request.jwt.claims', %s, true)` (bind param — `SET` only takes a
  literal, so a bare `SET LOCAL ... = <claims>` would be both a syntax error and an
  injection surface for attacker-shaped JWT claims) before yielding the connection for
  BOTH the route's reads and writes — a `SET LOCAL` evaporates at transaction end, so a
  post-commit read-back on a fresh transaction would run claims-less and RLS would hide
  the row just written. `verify_jwt` is authentication; `tenant_conn` (via RLS) is
  authorization — a route needing per-account isolation must use it, not `get_db_conn`. A
  **legacy** caller (static `API_TOKEN` bearer, no Supabase `sub`) has no account
  membership and would see zero rows under RLS, so it's routed to the unscoped
  service-role connection instead (today's behavior, unchanged) until it re-auths with a
  real JWT.

**Pooler-safe mutual exclusion: lease-row CAS, not session advisory locks (migration
279, PR #717).** `pg_advisory_lock`/`unlock` are **session-scoped** — sound only on a
direct or session-pooled connection. Every service-role Python path uses the
**transaction-mode** pooler (`SUPABASE_DB_URL`, port 6543); under autocommit each
statement is its own transaction and can land on a *different* physical backend, so a
lock taken on backend X and released on backend Y silently fails to release — the lock
strands (caught live: PR #716's property-maintenance serialization stranded within
minutes, the "holder" pid was mid-way through an unrelated statement on another backend).
The pooler-proof primitive is a **single-row lease** claimed by one atomic
`UPDATE ... RETURNING` compare-and-set — atomic on whatever backend it lands on, no
session state, with an expiry that self-heals a crashed holder
(`property_maintenance_lease`, `scripts/recompute_property_stats.py`). pg_cron functions
(migration 277's Browse-rebuild included) are the one exception: each pg_cron call is a
single local session, so a session advisory lock there is sound — don't generalize the
lease-row fix to code that never sees the pooler.

**Supabase MCP.** Claude Code has direct read/write access to the production Supabase
project via the MCP integration. Use it for: inspecting the live schema, running SELECT
queries to verify data state, applying migrations, running backfill UPDATEs, and
confirming changes succeeded. The MCP connection points at **production** — there is no
separate dev/staging database. Treat every operation accordingly.

**`migrations/` is the source of truth for schema.** MCP is the *execution* mechanism,
not a replacement for tracked migrations. Applying a schema change without committing the
corresponding migration file silently breaks the codebase — future sessions or fresh
rebuilds will be missing the change. "Append-only" means **never rewrite migration
history** (never edit an existing numbered file); it does **not** trap us into keeping
dead schema — prune an unused table/column by writing a *new* forward migration that
drops it (a destructive change — see the policy below). **Confirm the next free number at
apply time** — parallel branches carry in-flight migrations; two genuine collisions
already exist on disk (`276_browse_read_model.sql` / `276_listings_geo_cell_key.sql`,
`277_browse_read_model_refresh.sql` / `277_candidates_archive_engine_columns.sql`) —
both pairs coexist harmlessly because the runner orders by filename, not the numeric
prefix alone, but don't count on that.

**This Supabase project's default privileges auto-GRANT, not just on tables.** New
tables get `anon`/`authenticated` grants by default (the Phase-0 hardening's root
cause); migration 287 found the **same default ACL applies to new functions** — a
freshly created `SECURITY DEFINER` function is directly callable by `anon` via PostgREST
RPC until explicitly revoked (`revoke execute on function ... from anon, authenticated`),
even though `revoke ... from public` is a no-op against an explicit ACL entry. Revoke
explicitly on every new function; grant back only the roles that need it.

## Migration safety policy (under autopilot)

- **Additive migrations** (new tables / columns / indexes / RPCs) — write the new
  numbered file, commit it, apply via MCP, verify with a SELECT, and report. No approval
  gate; CI + the tracked file are the net.
- **Destructive migrations** (`DROP TABLE`/`COLUMN`, type-changing `ALTER`, `DELETE`
  without `WHERE`, `TRUNCATE`) — **pause for explicit operator OK** ("yes, apply it") and
  take a `pg_dump` backup of the affected tables *first*. There's no staging DB, so these
  are largely irreversible.
- Read-only inspection (counts, sample rows, schema introspection, verifying backfills)
  needs no confirmation — just do it and report.

Correct flow for any schema change: (1) write the new numbered migration file in
`migrations/`; (2) for destructive changes, get explicit approval + back up first;
(3) apply via MCP (`apply_migration`), verify with a SELECT; (4) commit the migration
file in the same change; (5) report what was applied and verified.

## Multi-tenancy and RLS (Phase 1, migrations 286–295)

RLS is enabled **per-table, not project-wide** — check whether a table you're touching
has a policy before assuming service-role-only access still applies everywhere. The
model: `accounts` (`kind ∈ {personal,team,system}`, one fixed SYSTEM account
`00000000-0000-0000-0000-000000000000`) + `account_members(account_id, user_id, role)`
+ a separate `admins(user_id)` platform-admin allowlist (migration 286). Two SECURITY
DEFINER helpers, `current_account_ids()` and `is_platform_admin()` (keyed off the JWT
`sub` claim via `account_members`/`admins`), are the **sole** definition point for every
per-account RLS policy since — don't hand-roll a second way to check tenancy.

Per-table RLS pattern, repeated across migrations 290 (6 curation tables: `collections`,
`tags`, `property_notes`, `filter_presets`, `notification_subscriptions`,
`manual_rental_estimates`), 291 (`estimation_runs`/`building_runs`, `account_id`
NULLABLE, defaults to SYSTEM), 292 (6 child-grain tables incl. `notification_dispatches`,
`account_id` **trigger-derived** from the parent row, not caller-supplied), and 294
(pipeline tables): `revoke all ... from anon, authenticated` → `grant
select/insert/update/delete ... to authenticated` → a `for all using/with check
(account_id in (select current_account_ids()))` policy. **Grant the id sequence's
`USAGE` too** — `GRANT INSERT` on the table does not cover it, and a table with a
`bigserial`/`serial` PK will fail every `authenticated` insert until the sequence grant
is added (a real bug the tenant-isolation CI lane caught before deploy).

`property_pipeline` gets a **composite PK swap**, `(property_id)` → `(account_id,
property_id)`, migration 295 — the one table where the PK itself changed, not just an
added column. This migration is **explicitly gated**: it `raise exception`s if any NULL
`account_id` rows remain, and its own header states it must ship in the same deploy
window as `api/pipeline.py`'s matching `ON CONFLICT (account_id, property_id)` rewrite.
Don't assume every table with `account_id` also got a composite PK — check the specific
migration.

**Tenant DB role and pool**: `tenant_pool` (migration 293, `LOGIN NOINHERIT`, zero access
until an explicit `SET LOCAL ROLE authenticated`, fail-closed by construction) +
`api/tenant_pool.py`'s `tenant_conn` — see the connection-modes section above for the
runtime mechanics.

**First-signup backfill race**: the on-signup trigger (migration 294) does an atomic
INSERT-with-`ON CONFLICT` CAS into `legacy_backfill_claim` (mirrors the lease-row CAS
pattern above) — whoever signs up first wins and claims every pre-tenancy NULL-
`account_id` row via `backfill_legacy_account_id`; every later signup instead gets
`seed_default_pipeline`/`seed_default_collections`. The migration comment flags this as
unsafe once public (non-operator) signup ships — revisit before then.

Full table-by-table migration list, RLS policy text, and the composite-FK detail on
`property_pipeline`: `.claude/skills/database/references/tenancy.md`.

## Read-model patterns

**Browse read model** (migrations 275–278, 283; PRs #705/#707/#711/#714/#724): a
`properties_public`-style view is fed from `browse_projection` (the column contract +
the dedup-aware publication gate, defined once) into `browse_list` — an **UNLOGGED
table**, blue-green rebuilt every 5 minutes by a `SECURITY DEFINER` pg_cron function
(`rebuild_browse_list()`, `pg_try_advisory_lock` guards overlapping runs, `ANALYZE`
*before* the swap is mandatory or the planner uses stale stats on the fresh table).
`properties_map_mv` stays a real `MATERIALIZED VIEW` (30-min cadence) fed from the same
projection. This retired the old `scripts/refresh_map_mv.py` GH Actions cron entirely —
pg_cron runs on-the-minute where GH Actions cron was measured ~2× jittered (see
`gh-actions-cron-throttle-fleet` if you need the numbers).

**A bare `SECURITY DEFINER` function call in a view's WHERE is NOT inlined by the
planner and runs once per candidate row, not once per query** (migration 275, PR #707).
Migration 273 added `properties.published_at`'s gate as a bare
`(NOT publication_gate_enabled() OR published_at IS NOT NULL)`; measured live, that's
~87k calls for one cohort, shared buffers 33.5k→172k, warm latency 146ms→914ms, and it
timed out cold under the anon 3s statement budget (this is what broke Browse
market-wide). Wrapping the call as a scalar subquery,
`(NOT (SELECT publication_gate_enabled()) OR ...)`, folds it to a one-time `InitPlan`
(confirmed via `EXPLAIN ANALYZE`: 211 buffers instead of 172k) — same result, O(1)
instead of O(rows). Apply this to any future gate/flag function referenced from a view
WHERE.

**Stored blocking keys**: `listings.street_name_key` (migration 256) and
`listings.geo_cell_key` (migration 276, trigger-maintained, extended to the `byt` family
by migration 296) follow the same shape — a single SQL/function definition, stamped at
every write path, stored so the dirty-drain can scope its load in SQL instead of
recomputing live for every row (rule #19). See the street-lifecycle entry below for
`street_name_key`'s own history; `geo_cell_key` is its geo-blocking twin for families
that don't key on street.

## Schema conventions

- Sreality enum codes that we promote to typed columns are stored as Czech text labels without
  diacritics, mirroring the existing treatment of `category_main` / `category_type`. Source maps
  live next to the parser: `parser.CATEGORY_MAIN`, `parser.CATEGORY_TYPE`, `parser.FURNISHED`,
  `parser.OWNERSHIP`. Unknown source codes (including sreality's `0` "not specified") return
  `None`, never raise — same forgiving pattern that lets the parser tolerate sreality adding a
  new code (as it did for `category_type_cb=4` / `'podil'`).
- `has_balcony` / `has_parking` are LEGACY combined booleans. They conflate
  balcony+terrace+loggia and parking+garage respectively. The granular columns added in
  migration 022 (`terrace`, `garage`, `parking_lots`) are the correct fields for new analytical
  work. The legacy columns stay populated for backward compatibility with existing queries /
  RPCs.
- The Czech admin hierarchy on a listing is **derived from `geom`, not parsed from the address**
  (migration 140). `listings.obec` / `okres` / `region` (municipality / district / kraj) are set
  by a BEFORE INSERT/UPDATE-OF-geom trigger (`listings_set_admin_geo`) that PIPs the coordinate
  into `admin_boundaries` and walks `parent_id` — so they're populated **instantly at scrape time**
  and **uniform across every source**. Rows near a boundary that miss every polygon by a sliver
  now fall back to the **nearest obec/ku within 250m** (migration 289, PR #752) rather than
  going unresolved — only truly-foreign points (~5%) still lack a CZ match. The trustworthy
  anchor is the coordinate (~95% coverage, straight from each portal's map/GPS data); the
  free-text `locality` is portal-specific display text and unreliable for grouping. The legacy
  display `district` text column is filled from okres (or obec for Prague) only when NULL, so
  sreality's richer "City - Quarter" labels are preserved. Don't re-derive hierarchy from `locality`;
  read the normalized columns.
- `listings.street` is **portal-uniform via one shared extractor, `scraper/street.py`** (migration
  122 added the column). sreality + bezrealitky read a structured street (bezrealitky also fills
  `house_number` / `zip`); the HTML portals mine it from a free-text locality (`street_from_locality`:
  first segment for idnes/remax, last for maxima) or clean a regex capture (`clean_street` for bazos).
  The ONE don't-fabricate guard (`reject_as_town`) lives here so it isn't reimplemented per portal —
  it rejects foreign coords/countries, "Town - Quarter" forms, "okres X" qualifiers, and any candidate
  equal to the row's own geo-derived obec/okres/region; a wrong street is worse than NULL (it poisons
  the dedup street-key and Browse). Stored values are bare/human-readable for display; the SEPARATE
  match-time grouping NAME key is **`scraper.street.street_name_key`** (the single home for street
  string logic; consumed live by `toolkit.dedup_engine.street_group_keys` AND stored on
  `listings.street_name_key`, migration 256 — don't confuse the human-readable `street` with the key):
  a row dual-keys into `id:<street_id>` (sreality/bezrealitky) AND `name:<obec_id>:<street_name_key>`.
  The NAME key is **obec-scoped** to keep each town's street its own small group and block
  cross-town false merges (classify_pair has no geo check). An oversized nationwide group (a
  common name like "Žižkova" across 100+ towns) is no longer skipped whole — migration 271 (PR
  #699) processes it **bounded**, prioritizing the best candidate pairs up to a cap and
  recording `dedup_engine_runs.oversized_groups`/`skipped_oversized` for observability, so
  cross-portal pairs in an oversized group are still found, just not exhaustively.
- **RÚIAN address-point resolver** now covers mmreality, ceskereality, and realitymix (PR #750)
  in addition to its original portals, gated by `matched_type='regional.address'` — a
  geocoded street/town *centroid* match must never resolve a street, only an exact address
  point. realitymix additionally gained a `locality_text`-derived arm (PR #756, its index
  cards carry no structured address). Backfill (`scripts/backfill_portal_streets.py`) gained
  `--include-inactive` (PR #758, dedup needs delisted streets too) and now bounds its chunk
  scans to fixed ID windows (PR #759, avoiding pooler-timeout scans on the full table).
- **Location/geocode lifecycle** (migration 288, PR #749): a unified `CoordResolver`
  (`scraper/location.py`) now backs idnes/realitymix/maxima/remax/mmreality/ceskereality —
  four of which had no geocode path before. `geocode_cache` persists negative results (don't
  re-query a coordinate known to fail) and `listings.geocode_attempted_at` is a row-grain
  attempt-ledger **column**, not a `raw_json` marker (the same lesson migration 263 already
  learned for the street resolver: a marker in `raw_json` gets clobbered by the next refetch).
  Both ingest upserts extend `COALESCE(EXCLUDED.geom, listings.geom)` preserve-if-null to
  coordinates, mirroring the street-lifecycle rails below.
- **Street lifecycle: resolver fills survive refetches (migration 263).** The RÚIAN coord→street
  resolver fills `street`/`street_name_key`/`house_number` on rows whose portal page has no street —
  so the row's next detail refetch re-parses NULL, and a plain `street = EXCLUDED.street` used to
  CLOBBER the fill (measured: 40% of a resolver cohort lost in 2.5 days). Three rails now:
  (1) both ingest upserts (`upsert_listing` + `_BATCH_UPSERT_SQL`) build their SET from the ONE
  `_listing_update_set_sql()` builder, which makes the trio **preserve-if-null**
  (`COALESCE(EXCLUDED.c, listings.c)`) — an incoming NULL never erases a stored value, a page-parsed
  street still wins; (2) **`listings.street_source`** ('parser' | 'resolver') is durable provenance
  (replacing the resolver's raw_json marker, which the refetch destroyed) — ingest stamps 'parser'
  when the page yields a street, else preserves it, the resolver stamps 'resolver';
  (3) the admin-geo trigger drops a **'resolver'** street when the listing's COORDINATES change
  (derived from the old point → may be wrong → "wrong street worse than NULL"), and its existing
  tail block then re-opens the resolver for the new coords. Parser streets are untouched by the
  guard (the page re-derives them every fetch).

## See also

- `.claude/skills/database/references/tenancy.md` — full table-by-table RLS migration list,
  policy text, and the `property_pipeline` composite-FK detail.
