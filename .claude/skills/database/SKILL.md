---
name: database
description: Use when working with this project's Postgres/Supabase database — reading data cheaply (psql vs the Supabase MCP), the two connection modes (connect vs connect_session, transaction- vs session-mode pooler, prepare_threshold), migrations as the source of truth, the additive-vs-destructive migration safety gate, or schema/column conventions (typed enum labels, geom-derived admin hierarchy, the shared street extractor, legacy booleans). Triggers on: migration, apply_migration, ALTER/CREATE TABLE, psycopg, pooler, backfill, PostGIS, RLS, connection mode, verifying data with a SELECT, schema or column change.
---

# Database

Everything for working with the Supabase Postgres store of record: reading cheaply, the
two connection modes, migrations as the source of truth, the migration safety gate, and
schema conventions. Full architectural rationale for the data model is in
`docs/architecture.md`.

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

**Two connection modes.** `scraper/db.py` exposes two factories:
- `connect()` — the **default for everything** (scrape_run bookkeeping, bazos, images,
  recompute, API, scripts). Points at `SUPABASE_DB_URL` (the **Transaction-mode pooler**,
  port 6543) with `prepare_threshold=None`. Disabling auto-prepare is **required** there:
  PgBouncer rebinds connections between queries, so a cached prepared statement would trip
  `DuplicatePreparedStatement`.
- `connect_session()` — **only** for the scraper's hot detail-write loop (the long-lived
  connection in `scraper/main.py:_run_full`). Points at `SUPABASE_DB_SESSION_URL` (the
  **Session-mode pooler**, port 5432) and leaves `prepare_threshold` at psycopg3's default,
  so the repeated upsert + spatial SQL gets server-side **prepared once and reused** across
  every listing in the run (the plan isn't re-derived per call). The session pooler gives
  each client a dedicated backend, so prepared statements are safe there. If
  `SUPABASE_DB_SESSION_URL` is unset, `connect_session()` **falls back to `connect()`**, so
  nothing breaks where the secret isn't configured.

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
drops it (a destructive change — see the policy below).

**Migration safety policy (under autopilot):**
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
  and **uniform across every source** (only ~5% of listings, foreign points, lack a CZ match). The
  trustworthy anchor is the coordinate (~95% coverage, straight from each portal's map/GPS data);
  the free-text `locality` is portal-specific display text and unreliable for grouping. The legacy
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
  The NAME key is **obec-scoped** — a common name like "Žižkova" has 100+ active listings across dozens
  of towns; one nationwide group blows `MAX_GROUP_SIZE=40` and gets the whole group SKIPPED, so the
  cross-portal pairs there (HTML portals have no street_id → name group is the only place they meet a
  sreality row) were never compared. obec-scoping keeps each town's street its own small group AND
  blocks cross-town false merges (classify_pair has no geo check). The STORED `street_name_key` (stamped
  at EVERY `listings.street` write path via the one function, out of the content hash like `street`) is
  what lets the dedup `--dirty` drain scope its eligible load to the dirty street groups in SQL (rule
  #19); the 6h full scan recomputes it live, so a stale stored key only delays, never breaks, a merge.
  `street` / `house_number` / `zip` / `street_name_key` are OUT of the content hash, so backfilling
  them never churns snapshots (`scripts/backfill_portal_streets.py` +
  `scripts/backfill_street_name_key.py` re-derive from already-stored data — no re-fetch). Browse
  street picks ILIKE `properties_public.place_search_text`, which is a **group-best** street
  (`coalesce(p.street, l.street)`, migration 183) denormalized onto `properties` by
  `recompute_property_stats` — so a multi-portal property matches a street even when its representative
  listing lacks one. (The old expand-normalizer `toolkit/addresses.py` that turned `ul.`→`ulice` was
  dead code and was removed.)
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
