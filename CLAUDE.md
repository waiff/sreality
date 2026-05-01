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

## Architectural rules (do not violate without asking)

1. **The schema in `migrations/001_initial.sql` is fixed.** Never modify an
   existing migration. Schema changes go in a new numbered file
   (`002_*.sql`, `003_*.sql`...) which the operator runs by hand in the
   Supabase SQL editor.
2. **Snapshots on content change only.** Never insert into `listings` without
   computing the content hash and inserting into `listing_snapshots` if it
   differs from the most recent snapshot for that listing.
3. **Never delete listings.** Listings that disappear from sreality get
   `is_active=false`. History is sacred.
4. **Image URLs only in v1.** No file downloads, no S3, no Supabase Storage.
5. **No new dependencies without justification.** Each entry in
   `pyproject.toml` should have a clear reason. Prefer the stdlib.

## Auth and secrets

- Two env vars: `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`.
- The service role key uses Supabase's new 2025 format (`sb_secret_...`).
  It is **not** a JWT. The env var is still named `SUPABASE_SERVICE_ROLE_KEY`
  because that is what `supabase-py` reads.
- Production: these are GitHub Actions secrets at
  Settings -> Secrets and variables -> Actions.
- Never write the secret value into a committed file. `.env` is gitignored.
  Always reference secrets by env-var name.

## Coding conventions

- Python 3.12. Type hints on every function signature.
- Prefer the stdlib. Reach for a dependency only when stdlib is awkward.
- No comments unless the WHY is non-obvious. Don't narrate WHAT the code does.
- No multi-paragraph docstrings. One-line docstrings are fine for module heads.
- `requests` for HTTP (already a dependency). Don't add `httpx` or `aiohttp`
  without a strong reason.
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
- `DETAIL id=... new|updated|unchanged` per listing
- `IMAGE id=... inserted=N` per listing
- A final summary line: `RUN done pages=... new=... updated=... unchanged=... errors=...`

A run ending with `errors > 0` is not necessarily a failure (single-listing
fetch errors are tolerated). A run that did not emit a `RUN done` line is
a real failure - check the GitHub Actions log for a stack trace.

## What is explicitly out of scope right now

- Frontend (React, HTML, Lovable, anything user-facing).
- Yield-calculation API.
- ClickUp integration.
- Image file downloads.
- Slack/email notifications.
- Authentication or user management.
- Public read API.

Do not start any of these without explicit user direction in a new session.
