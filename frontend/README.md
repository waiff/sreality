# Frontend (placeholder)

This directory is reserved for the future browser-facing UI. **Nothing
ships here yet.** When work begins it will most likely be a Vite + React +
TypeScript app that talks to Supabase directly via the `supabase-js`
client using the publishable (`anon`) key.

## Why a separate territory

The Python codebase (`scraper/`, `toolkit/`, `api/`) is the backend
territory: server-side code, service-role database access, no browser. The
frontend is a different territory with different rules — different
language, different deploy target, different auth posture, different
dependencies. Keeping them in sibling folders rather than one mixed tree
makes the boundary obvious to humans and to future Claude Code sessions.

See `CLAUDE.md` ("Territories") for the full rule.

## Read surface

The frontend reads from four projected views, each created in
`migrations/008_ui_read_policies.sql`:

- `listings_public` — current listing state (no `raw_json`, no `geom`;
  `lat` / `lng` exposed as scalars).
- `listing_snapshots_public` — append-only price history (no
  `raw_json`, no hashes).
- `listing_freshness_checks_public` — observability of on-demand
  freshness verifications (no error-message column).
- `listing_fetch_failures_public` — current scraper-side fetch
  failures (no `last_error` column).

The base tables remain RLS-blocked to the `anon` role; the frontend has
no path to `raw_json`, `geom`, `content_hash`, or any other column not
listed above.

## Writes

The frontend has **no write path**. Mutations (snapshot inserts,
`is_active` flips, freshness check rows) happen only via the scraper
(GitHub Actions, service role) or the bearer-token-gated FastAPI service.
If the UI ever needs to trigger a write, it will go through that API, not
direct Postgres.

## Auth

Two Supabase keys, do not confuse them:

- **Publishable (`anon`) key**: safe to embed in browser bundles.
  Read-only against the four `*_public` views above.
- **Service-role key (`sb_secret_…`)**: server-side only.
  Used by the scraper. Never goes near the browser.

## Status

Empty. Real UI work begins in a separate session, against a fresh
brief. Until then this folder exists only to declare the territory.
