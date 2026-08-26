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

**`connect()` and `connect_session()` are BOTH `autocommit=True`** — "callers manage
transactions explicitly". Anything that must be atomic needs an explicit
`with conn.transaction():` block, or each statement commits on its own AND any
`SET LOCAL` (e.g. a `lock_timeout` guard) silently applies to nothing. This bites
multi-statement DDL hardest: a `DROP CONSTRAINT` + `ADD CONSTRAINT` pair without the
block leaves a real window where writers see the table with neither. Note
`apply_r2_constraints._with_lock_retry` commits per statement *by design* — right for
independent DDL, wrong for a unit that must not be split.

**Three connection modes** now exist — pick by who's calling and whether the call is
tenant-scoped:
- `connect()` (`scraper/db.py`) — the **default for everything service-role** (scrape_run
  bookkeeping, bazos, images, recompute, most of the API, scripts). Points at
  `SUPABASE_DB_URL` (the **Transaction-mode pooler**, port 6543) with
  `prepare_threshold=None`. Disabling auto-prepare is **required** there: PgBouncer/
  Supavisor rebinds connections between queries, so a cached prepared statement would trip
  `DuplicatePreparedStatement`. Takes `attempts`/`retry_delay` for bounded retry on a flaky
  connect handshake (PR #663).
- `connect_session()` — **only** for a long-lived hot loop that repeats the same SQL
  thousands of times: the scraper's detail-write loop (`scraper/main.py:_run_full`), the
  location resolve drain (`location_data/resolver/drain.py`), and the registry loaders
  (`location_data/loader_db.py`, which additionally REFUSES the fallback — a 3 M-row COPY
  needs session GUCs). Points at `SUPABASE_DB_SESSION_URL` (the **Session-mode pooler**,
  port 5432) and leaves `prepare_threshold` at psycopg3's default, so the repeated upsert +
  spatial SQL gets server-side **prepared once and reused** across every listing in the run.
  The session pooler gives each client a dedicated backend, so prepared statements are safe
  there. Falls back to `connect()` if `SUPABASE_DB_SESSION_URL` is unset — **silently**, so
  a loop that cares logs the fallback itself (the drain does; a quietly unprepared run is
  indistinguishable from a merely slow one). Prepared statements alone are not the win:
  they cut the cost of a round trip, not the COUNT of them, so a hot loop also has to batch
  its per-row reads and memoize whatever is constant for the run.
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
- **Open with `set local lock_timeout = '5s';` when a migration GRANT/REVOKEs or
  CREATE-OR-REPLACEs a hot or cron-refreshed relation** (any matview, `browse_list`,
  `listings`). Those take ACCESS EXCLUSIVE, and a whole-transaction loop holds every
  lock it has already taken — so without a timeout it queues behind, or blocks, the
  `*/10` health refresh or the 30-min map rebuild. Fail fast and retry instead.
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

**Billing (migration 298, PR #769 — Phase 1 increment 5)** extends the same RLS pattern:
`plans` (operator-curated tiers, `agendas jsonb` visibility map, one row `is_default`,
world-readable to `authenticated` — plan definitions aren't secret), `entitlements`
(≤1 row per account, `entitlements_read_own` policy, **service-role writes only** — no
`authenticated` insert/update policy at all, since only the Stripe webhook or the admin
comp screen may change a plan), `stripe_webhook_events` (idempotency ledger, service-role
only, no `authenticated` grant). See the "Identity, login, and admin gating" section of the
`toolkit-api` skill for `require_entitlement` and the webhook auth class.

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

**Shared-market tables under the tenant role (Amendment A5, migration 349)**: the
shared `listings`/`properties`/`images` tables are RLS-enabled-with-**zero**-policies
(deny-all), so a `tenant_conn` handler reading them directly gets nothing — the anon/tenant
SPA is expected to read the owner-bypass `*_public` views instead. **Exception: `properties`
now carries one permissive `FOR SELECT TO authenticated` policy** (its base columns are
market-only, no broker PII), so the merge-survivor resolver and similar tenant-conn reads
work. **`listings` stays deny-all** (broker_email/phone/name + raw_json inline — a row
policy would leak them column-wise via PostgREST); read a listing's identity on the tenant
conn through `listing_natural_key_public`, and market facts through a service-role
connection (as `portal_lookup` does), never a blanket policy. Don't add a `USING (true)`
policy to `listings`.

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
`properties_public`-style view is fed from `browse_projection` (the column contract,
defined once) into `browse_list` — an **UNLOGGED
table**, blue-green rebuilt every 5 minutes by a `SECURITY DEFINER` pg_cron function
(`rebuild_browse_list()`, `pg_try_advisory_lock` guards overlapping runs, `ANALYZE`
*before* the swap is mandatory or the planner uses stale stats on the fresh table).
`properties_map_mv` stays a real `MATERIALIZED VIEW` (30-min cadence) fed from the same
projection. (The projection still carries the old publication-gate predicate in SQL, but the
gate is **inert** — `dedup_publication_gate_enabled` is `false` and its code side is gone,
rule #15; the predicate itself comes out in the NEW DEDUP teardown migration.) This retired the old `scripts/refresh_map_mv.py` GH Actions cron entirely —
pg_cron runs on-the-minute where GH Actions cron was measured ~2× jittered (see
`gh-actions-cron-throttle-fleet` if you need the numbers).

**A single-value filter must reach `browse_list` as `=`, never as `= ANY`.** The serving
index is `(category_main, category_type, first_seen_at DESC, property_id DESC)` and Browse's
default sort is exactly its trailing pair — but a ScalarArrayOp ANYWHERE in the equality
prefix disqualifies the index from satisfying the `ORDER BY`, so the planner drops the
early-stopping ordered scan and reads the whole band into a top-N heapsort. Live on the
DEFAULT cohort (byt+pronájem, 105k rows, `LIMIT 24`): `= ANY('{byt}')` → 15,877 buffers /
4,452 ms (11–17 s when the band is cold, i.e. right after a rebuild — past `authenticated`'s
8 s `statement_timeout`, which is how the flagship surface came to answer HTTP 500 on its own
front page); `= 'byt'` → **6 buffers / 0.174 ms**, no Sort node. The client side is fixed in
`frontend/src/lib/registryQueryBuilder.ts` (a length-1 `string_list` emits `.eq()`), pinned by
three tests in its `.test.ts`, and the same rule governs the portal-mirror lane
(`source` → `listing_feed_public`). Genuine multi-selects still sort — that is a server-side
relation question, not this one. When reading a slow `browse_list` plan, check the operator
before anything else: a `Sort` node above the index scan is this defect's signature.

**`listing_cover_public` is fast ONLY when filtered by `listing_id`.** It reduces `images`
to one cover per listing with `DISTINCT ON (listing_id)` and then joins the CLIP-tag lateral
to that already-reduced set (migration 416) — 44 rows / ~788 buffers for a 44-id board, versus
901 rows / 901 lateral probes / 3,995 buffers reading `images_public` the old way. But the
`DISTINCT ON` key is `listing_id` while the view also projects `id` and `sreality_id`: a
predicate on either of those is evaluated ABOVE the `Unique` node, so the whole 10.4M-row
scan runs first and the query returns `57014` under `authenticated`'s 8 s `statement_timeout`.
Filter this view by `listing_id`, always. Its sreality-keyed sibling
(`fetchImagesByListingIds` in `frontend/src/lib/queries.ts`) exists for the frozen-comparable
id space and is NOT interchangeable — worth knowing before W7a collapses the image loaders.

**Never hand-retype `rebuild_browse_list()`/`rebuild_properties_map_mv()`.** Both blue-green
DROP+CREATE their object every tick, including the `GRANT SELECT ... TO authenticated` (never
`anon` — migration 299 deliberately narrowed this) and, for `browse_list`, the district/price
covering indexes (migration 283's 9-column form, not migration 277's original 3-column form).
Migration 371 copied from an outdated body and silently reintroduced BOTH the anon grant and
the narrow indexes while its own commit message claimed no behavior change — live-anon-readable
for an unknown period, fixed in migration 376. Always `CREATE OR REPLACE` from the CURRENT
function body (`pg_get_functiondef`), never from an old migration file or a design doc's
emergency-rollback snippet. Both functions now also self-check after granting
(`if has_table_privilege('anon', ..., 'SELECT') then raise exception`) — a regression RAISEs,
rolling back the whole tick so the last known-good object stays published instead of a
mis-permissioned one, and the artifact's `derived_artifacts.last_succeeded_at` (surfaced on
the Health page, migration 440) stops advancing within one cycle. `tests/test_browse_grant_drift.py`
is the matching OFFLINE gate — it reaches inside `EXECUTE`-embedded DDL for these two relations
specifically, unlike `test_migration_rls_grants.py`'s scanner, which treats dollar-quoted
function bodies as opaque by design.

**Every derived artifact declares its own freshness in `derived_artifacts`** (migration 437,
completed by 440) — one row per artifact carrying `producer` / `host` / `cadence` /
`staleness_budget`, plus `complete_through`, `last_succeeded_at`, `last_duration_ms`,
`last_rows`. It **replaced** the singleton `browse_read_model_state`, which migration 440
dropped along with its `_public` view, 437's temporary UNION adapter, and the SPA reader —
all 14 artifacts (12 matviews + `browse_list` + `llm_cost_hour_rollup`) are ordinary rows now.
Same posture as 276: RLS on with zero policies, everything revoked, published through
`derived_artifacts_public` (enumerated columns, `authenticated` only, not `security_invoker` —
owner rights are what read through the RLS wall). **A new derived artifact needs a registry
row**, and `tests/test_derived_artifacts_registry.py` goes RED without one — its `_W7_BACKLOG`
exemption set is now empty and **nothing may be added to it**. Two budgets are deliberately
NOT their cadence and 440's header carries the measurements: the six health matviews get
45 min (not 10 — the 30d p95 gap is 15.8 min and a 10-min budget reads red on the healthy
case), and `broker_region_type_stats` gets 30 h (not 24 — its daily sweep itself runs up to
2h01m, so 24 h alarms on every healthy run). `rent_map_choropleth`'s `host` is honestly
`api-request`: it is refreshed inside a FastAPI request handler — don't launder it into a
cron string. `llm_cost_rollup_state` is a watermark FOR an artifact, not an artifact, and is
deliberately unregistered.
There is deliberately **no `last_error` column**: a plpgsql `exception when others then
update …; raise;` handler cannot persist its write (the re-raise unwinds the subtransaction
it ran in — verified live), so such a column could only ever be NULL and would publish
"healthy" during an outage. Failures belong in `cron.job_run_details`; the published signal
is `last_succeeded_at` against `staleness_budget`, which is correct precisely *because* it
rolls back with the failed run. **Never add an error column to a registry a producer writes
inside its own transaction.**

**Every producer stamps through the ONE helper, `public.stamp_derived_artifact(name, rows,
duration_ms)`** (migration 441) — SECURITY DEFINER, `set search_path = public`, EXECUTE
revoked from `public` *and* `anon`/`authenticated` (PostgreSQL's built-in default grants
EXECUTE to PUBLIC, so revoking only the named roles leaves it callable). Python producers
call `scraper.db.stamp_derived_artifact(conn, name)`; SQL producers `perform` it. **Its
UPDATE is deliberately a silent no-op on an unregistered name** — a producer must never fail
because a metadata row is missing — so a typo'd name stamps nothing forever and reads on the
Health panel exactly like a dead artifact; that hole is closed offline by
`tests/test_derived_artifacts_stamping.py`, which extracts every name literal from both the
repo tree and `pg_proc.prosrc`, checks every `producer` resolves to a real function or file,
and pins the health fan-out to **six individual stamps, each right after its own REFRESH**
(that buys six real per-matview `last_duration_ms` values, since `clock_timestamp()` advances
inside a transaction). Two artifacts have **no other freshness signal in existence** —
`price_stat_choropleth` and `rent_map_choropleth` are refreshed non-concurrently, which swaps
the heap so `pg_stat_user_tables` reads zero, and `pg_stat_file` is denied on this instance.
`llm_cost_hour_rollup` keeps its own inline UPDATE because it must pass its own watermark as
`complete_through`; don't unify it onto the helper without a fourth parameter.

**A `SECURITY DEFINER` gate in a view's WHERE is per-row ONLY when it is combined with a
column predicate.** Three cases, don't conflate them:
- **Standalone** (`WHERE is_platform_admin()`) — the qual references no column, so it is a
  pseudoconstant and the planner emits a **One-Time Filter**: evaluated once, and for a
  false result the subtree is never scanned. This is the migration 318/332 admin-gate
  pattern; it is O(1) and needs no wrapping (re-confirmed by `EXPLAIN` on the live gated
  views during the 2026-07 remediation).
- **Combined with a column Var** (`(NOT publication_gate_enabled() OR published_at IS NOT
  NULL)`) — the Var defeats the pseudoconstant fold and the function runs **once per
  candidate row**. This is the actual migration 275 / PR #707 incident: ~87k calls for one
  cohort, shared buffers 33.5k→172k, warm latency 146ms→914ms, timing out cold under the
  anon 3s budget — what broke Browse market-wide.
- **Scalar-subquery wrap** (`(NOT (SELECT publication_gate_enabled()) OR ...)`) — folds the
  combined case to a one-time `InitPlan` (211 buffers instead of 172k). It is the rescue for
  case 2, not a requirement for case 1.

**The same three cases apply to RLS POLICY predicates, and case 2 lived there unnoticed
until migration 431.** All 10 tenancy policies spell the gate as
`... OR (account_id IS NULL AND is_platform_admin())` — an OR with column references, i.e.
case 2 — so the gate ran once per candidate row, on `llm_calls` at 293,551 rows. 431 wraps
all 11 sites. Two policy-specific rules that do not arise for views:
- **Use `ALTER POLICY`, never `DROP` + `CREATE`.** `ALTER POLICY` swaps the expression in
  place: no window where the table is unpoliced, and it **cannot lose `TO authenticated`**.
  A `CREATE POLICY` that omits the role clause silently defaults to `PUBLIC` — privilege
  escalation with no test to catch it.
- **Never point `tests/_admin_gate_shape.py` at `pg_policy`.** Its `_GATE_OR_EVASION` rule
  rejects any `or … is_platform_admin`, which is the exact shape every legitimate tenancy
  policy has (a tenancy policy *is* an OR of "my rows" and "platform rows"). It would have
  to be weakened to pass, and its docstring records two earlier weakenings that then let
  gate-defeating forms ship green. Guard policies behaviourally instead
  (`tests/test_admin_gate_policies_live.py`).

**`is_platform_admin()` has TWO arms, and which one you measure depends on how you connect.**
Claims present (a browser JWT through PostgREST) → `admins` keyed on the JWT `sub`. Claims
absent → `current_setting('role') = 'none'` AND `pg_roles.rolbypassrls` for `session_user`.
psycopg, psql, pg_cron **and the Supabase MCP** all take the second arm — so an MCP
measurement of this gate exercises a different branch than production browser traffic.

So: wrap a gate that sits alongside a column predicate; a standalone gate is already O(1).

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
  string logic), stored on `listings.street_name_key` (migration 256) by every `street` write path
  — don't confuse the human-readable `street` with the key. It is **obec-scoped**, so each town's
  street is its own small group. The legacy dedup engine that consumed it was removed in the
  2026-08 cutoff (rule #15), but the column, its trigger and its parity guards **stay**: the
  rebuilt engine's Level 0 blocking reuses it (`docs/design/new-dedup/PROGRAM.md`), so a
  normalizer edit still requires the `backfill_street_name_key.yml all=true` re-key and the weekly
  `street_key_parity.yml` job is still the alarm for forgetting it.
- **RÚIAN address-point resolver — AUTO-WRITE STOPPED (location-data W0 item 0a).** The
  coord→street resolver (`scripts/backfill_address_point_streets.py`) no longer runs on a
  weekly cron and a bare invocation/dispatch is a dry run; writes need the explicit
  `--write` flag. An LLM mining experiment measured ~11 of ~21 text-checkable
  resolver-derived streets wrong (2 fabricated street+number pairs on control rows).
  Already-written `street_source='resolver'` rows stay until the location program's
  migration wave quarantines them; the trigger rails below still govern them. Historical
  coverage: mmreality/ceskereality/realitymix added by PR #750, gated by
  `matched_type='regional.address'`; realitymix `locality_text` arm PR #756. The SEPARATE
  text-derived backfill (`scripts/backfill_portal_streets.py`, parser-grain, dispatch-only)
  is unaffected by 0a; it gained `--include-inactive` (PR #758) and fixed ID windows (PR #759).
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
- **Location-data W1 relations (`location_*`, `ruian_*`, `mapy_*`, `portal_contract*`, `pin_*`;
  migrations 380–389) are service-role-only and shadow-only** — RLS on plus explicit
  `anon`/`authenticated` REVOKEs on every table, sequence and function, and nothing outside
  `location_data/` reads them before W6 (Browse/map/watchdog/dedup still use `listings.geom` and the
  geo-derived admin columns). Three disciplines when you touch them: the claim layer is
  **append-only** — a wrong claim is retracted and a new one inserted, never UPDATEd, and the Mapy
  licence-evidence tables are trigger-immutable (42501 on UPDATE/DELETE/TRUNCATE); every heavy batch
  lane shares the ONE `location-batch` Actions concurrency group and arms a `SET LOCAL
  statement_timeout`; and the RÚIAN loaders + the resolve drain run on **`connect_session()`** (the
  loader refuses the transaction-pooler fallback — a 3 M-row COPY needs session GUCs). Rationale:
  `docs/architecture.md` § Location data (W1); sequencing: `roadmap/location-data.md`.

## See also

- `.claude/skills/database/references/tenancy.md` — full table-by-table RLS migration list,
  policy text, and the `property_pipeline` composite-FK detail.
