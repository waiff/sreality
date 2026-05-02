# CLAUDE.md

Standing context for any future Claude Code session that touches this repo.
Read this before changing anything.

## What this project is

A daily scraper for Czech rental listings from sreality.cz. The output is a
Postgres database (Supabase, Frankfurt region, PostGIS enabled) with full
listing history. Downstream goals (out of scope until explicitly opened):
rental-yield calculations, ClickUp integration, frontend.

## Operator profile

The owner of this repo is non-technical and works **only** through Claude Code
on the web (claude.ai/code) connected to GitHub. They have no terminal, no
local Python, no local Git.

- Never ask them to run a shell command on their laptop.
- Use GitHub Actions for any execution. Tests run via `.github/workflows/test.yml`,
  the scraper runs via `.github/workflows/scrape.yml`.
- For tasks that genuinely need a browser (Supabase SQL editor, GitHub Settings
  pages), give them click-by-click instructions: which page, which menu, which
  button.
- Define jargon the first time it appears ("upsert," "JWT," "RLS," etc.).

## Database access and Supabase MCP

Claude Code has direct read/write access to the Supabase project via the MCP
integration. Use it for: inspecting the live schema, running SELECT queries to
verify data state, applying migrations, running backfill UPDATEs, and
confirming changes succeeded.

The `migrations/` folder remains the source of truth for schema. Every schema
change still goes in a new numbered SQL file. MCP is the *execution*
mechanism, not a replacement for tracked migrations. Applying a schema change
without committing the corresponding migration file silently breaks the
codebase — future sessions or fresh rebuilds will be missing the change.

Correct flow for any schema change:

1. Write the new numbered migration file (`00N_*.sql`) in `migrations/`.
2. Show the migration to the operator and get explicit approval before running.
3. Apply via MCP (`apply_migration`), verify with a SELECT.
4. Commit the migration file in the same change.
5. Report what was applied and what was verified.

Never apply a SQL change that doesn't correspond to a committed migration
file.

Never run destructive operations (`DROP TABLE`, `DELETE` without `WHERE`,
`TRUNCATE`, `ALTER COLUMN` that changes type or drops a column) without
explicit operator confirmation in chat. "Yes, apply it" is required.

Read-only inspection (counts, sample rows, schema introspection, verifying
backfills) needs no confirmation — just do it and report findings.

The MCP connection points at the production Supabase project. There is no
separate dev/staging database. Treat every operation accordingly.

## Architectural rules (do not violate without asking)

1. **The schema in `migrations/` is append-only.** Never modify an existing
   migration. Schema changes go in a new numbered file (`002_*.sql`,
   `003_*.sql`...) and are applied via the Supabase MCP after operator
   approval. See "Database access and Supabase MCP" for the full flow.
2. **Snapshots on content change only.** Never insert into `listings` without
   computing the content hash and inserting into `listing_snapshots` if it
   differs from the most recent snapshot for that listing.
3. **Never delete listings.** Listings that disappear from sreality get
   `is_active=false`. History is sacred.
4. **`last_seen_at` is driven by index sightings and successful
   detail fetches; failed fetches never touch it.**
   Every existing listing whose id appears in the run's index gets its
   `last_seen_at` bumped before any detail fetches happen. A successful
   detail fetch (cron or on-demand via `freshness_check`) also bumps
   `last_seen_at` as a side effect of `db.upsert_listing` — that's
   real evidence the listing is alive. A *failed* detail fetch must
   not affect `last_seen_at`, otherwise repeated failures would falsely
   flip a still-live listing to `is_active=false`. The `unchanged`
   path of `freshness_check` deliberately does NOT bump `last_seen_at`
   either — for that case the "I confirmed it" signal lives in
   `listing_freshness_checks.checked_at` instead. See architectural
   rule #9.
5. **Failed detail fetches are tracked, not silently dropped.**
   When a detail fetch (HTTP, parse, or DB write) fails, we record it in
   `listing_fetch_failures(sreality_id, attempts, last_error, given_up)`.
   Next run, listings with an active failure row jump to the front of
   `to_refetch` so the per-run cap can't keep deferring them. After 5
   attempts a row's `given_up` flips to true and it falls out of the
   active retry queue (manual SQL un-flip required to retry). On
   successful fetch the failure row is deleted. Inspect with
   `SELECT * FROM listing_fetch_failures ORDER BY attempts DESC`.
6. **Images are downloaded to Cloudflare R2.** v1 only stored URLs; v1.5
   downloads the bytes to an R2 bucket (S3-compatible) so the data
   survives sreality's CDN expiring listing photos. The `images` table
   tracks per-image download state via `storage_path`,
   `download_attempts`, and `last_download_attempt_at`. Image-download
   is a separate phase after the scrape phase; it's a no-op if R2 env
   vars are missing, so a partial deploy never breaks the scrape.
7. **No new dependencies without justification.** Each entry in
   `pyproject.toml` should have a clear reason. Prefer the stdlib.
8. **Latest-wins data model with snapshot history.** The `listings`
   table always reflects the most recent state. Every meaningful
   change appends a row to `listing_snapshots`. Analytical queries
   default to current state for relevance. Estimates that need
   retrospective auditability record the `snapshot_id` of each
   comparable they used — that resolves to the exact JSON the
   estimate relied on, even if the listing has since been updated or
   marked inactive. Avoid building "as-of" semantics into live
   queries; capture snapshot IDs in the estimate response instead.
9. **`listing_freshness_checks` is append-only and ephemeral.** Rows
   older than 30 days are safe to delete. No automated pruning is
   built; manual SQL when the table gets large. The table records
   every on-demand verification triggered by
   `verify_listing_freshness` — its primary purpose is observability
   and per-listing throttling, not history. The primary history
   table is `listing_snapshots`.

## Toolkit and API rules

These rules govern the analytical toolkit (`toolkit/`) and the FastAPI
service that exposes it (`api/`). They do not apply to the scraper.

1. **Tools return facts, not opinions.** No "recommended price", no "this
   looks like a good deal." Tools return data + provenance. Reasoning
   happens at the agent layer.
2. **Standard envelope on every tool's return value:**
   ```python
   {
     "data": ...,
     "metadata": {
       "tool": "tool_name",
       "filters_used": {...},      # echo of actual params after defaults applied
       "result_count": int,
       "queried_at": iso8601,
       "data_freshness": iso8601,  # max(last_seen_at) of considered listings, or null
     }
   }
   ```
3. **Every tool excludes `given_up = true` listings** from
   `listing_fetch_failures` by default. An `include_unreliable: bool = False`
   parameter overrides.
4. **"Active" filter is `is_active = true AND last_seen_at > now() - interval
   'X days'` (default 7).** Don't trust `is_active` alone — a listing not
   seen for 30 days is functionally inactive.
5. **No writes from the toolkit, with one explicit exception.**
   Read-only by default. The single exception is
   `verify_listing_freshness` (and `scraper.freshness.freshness_check`
   that it wraps), which exists so an agent can confirm a comparable
   is still valid before relying on it. Every call logs to
   `listing_freshness_checks` for observability and may also write a
   new `listing_snapshots` row, flip `listings.is_active`, or both.
   No other toolkit function may write. The API service should still
   connect with a read-only role if Postgres permits; the freshness
   check then needs a separately-elevated path. For now we ship with
   one role and discipline.
6. **Spatial queries use `geography(point, 4326)`.** Always
   `ST_DWithin(geom, target_geom, radius_m)`. Never compute distance in
   Python.
7. **psycopg directly, not supabase-py.** Same reasoning as the scraper.
   `prepare_threshold=None` for pgbouncer-mode pooler.

## Database access

We connect directly to Supabase Postgres using `psycopg` v3, not the Supabase
REST client. This was a deliberate choice for two reasons:

- PostGIS support: inserting `geography(point, 4326)` is one line of SQL with
  `ST_SetSRID(ST_MakePoint(lon, lat), 4326)`. Doing the equivalent through
  PostgREST requires a stored procedure or fragile GeoJSON casting.
- Atomic transactions: writing `listings`, `listing_snapshots`, and `images`
  for a single listing happens inside one transaction. The REST client cannot
  span tables atomically.

Do not introduce `supabase-py` without an explicit reason and a discussion.

## Auth and secrets

Seven env vars (all GitHub Actions secrets in production):

Database:
- `SUPABASE_URL` - public project URL.
- `SUPABASE_SERVICE_ROLE_KEY` - the new 2025 `sb_secret_...` token.
  **Not** a JWT. The env var name is preserved for forward compatibility;
  the v1 scraper does not actually need it because we connect to Postgres
  directly.
- `SUPABASE_DB_URL` - Postgres connection string from
  Supabase Project Settings -> Database -> Connection string -> Transaction
  pooler (port 6543). Contains the database password embedded in the URL.

Image storage (Cloudflare R2, S3-compatible):
- `R2_ACCOUNT_ID` - 32-char hex from the Cloudflare dashboard.
- `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` - generated when creating
  an R2 API token with Object Read & Write scope on the bucket.
- `R2_BUCKET_NAME` - usually `sreality-images`.

If any R2_* var is missing the image-download phase logs a skip and
exits zero. The scrape still records image URLs in the database;
downloading is decoupled and can be backfilled later.

Never write any of these values into a committed file. `.env` is gitignored.
Always reference secrets by env-var name in code.

## Coding conventions

- Python 3.12. Type hints on every function signature.
- Prefer the stdlib. Reach for a dependency only when stdlib is awkward.
- No comments unless the WHY is non-obvious. Don't narrate WHAT the code does.
- No multi-paragraph docstrings. One-line docstrings are fine for module heads.
- `requests` for HTTP, `psycopg` for DB. Don't add `httpx`, `aiohttp`,
  `sqlalchemy`, or `supabase-py` without a strong reason.
- Keep files small and single-purpose: `sreality_client.py` is HTTP only,
  `parser.py` is JSON-to-row mapping only, `db.py` is database I/O only.

## Adding a new scraper field without breaking existing data

1. Add the column with a new numbered migration (`alter table listings add
   column ...`). Never touch `001_initial.sql`.
2. Update the parser in `scraper/parser.py` to extract the field.
3. Update the upsert in `scraper/db.py` to include the new column.
4. Backfill old rows: either leave them NULL (acceptable if the column is
   nullable) or run a one-off SQL update from the `raw_json` column, which
   already contains the full source record.

## How to test changes

- Push to a branch. `.github/workflows/test.yml` runs pytest on every push.
- For end-to-end testing without polluting the DB: use `--dry-run`
  (logs what would be written, writes nothing).
- For testing a single listing: `--detail-only <sreality_id>`.
- For a small live run: `--limit 10` (caps at 10 listings).

## How to manually trigger the scraper

GitHub repo -> **Actions** tab -> **Daily Sreality scrape** workflow ->
**Run workflow** button -> pick branch and optional flags -> **Run workflow**.

## Reading the logs

The scraper emits structured progress lines:

- `INDEX page=N estates=M` per index page
- `INDEX total=N pages=M` once at end of index walk
- `PLAN unchanged=N refetch=M` once after deciding what to fetch
- `PLAN priority_retry=N` once if any listings have prior failure rows
- `PLAN cap=N deferred=M` once if the per-run refetch cap kicks in
- `DETAIL starting refetch=N` once before the refetch loop
- `DETAIL progress=N/M new=... updated=... errors=...` every 50 refetches
- `DETAIL id=... new|updated|unchanged` per refetched listing
- `IMAGE id=... inserted=N` per listing with new image rows recorded
- `INACTIVE marked=N` once after marking unseen listings
- `RUN done pages=... new=... updated=... unchanged=... errors=...`
- `IMAGES pending=N cap=N workers=N` once before the image-download phase
- `IMAGES progress=N/M ...` every 50 images during the phase
- `IMAGES done downloaded=... errors=... attempted=...` after image phase

A run ending with `errors > 0` is not necessarily a failure (single-listing
fetch errors are tolerated). A run that did not emit a `RUN done` line is
a real failure - check the GitHub Actions log for a stack trace.

## What is explicitly out of scope right now

- Frontend (React, HTML, Lovable, anything user-facing).
- Yield-calculation API.
- ClickUp integration.
- Slack/email notifications.
- Authentication or user management.
- Public read API.

Do not start any of these without explicit user direction in a new session.
