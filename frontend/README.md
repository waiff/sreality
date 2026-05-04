# sreality · database browser

Read-only web UI over the Supabase database produced by the daily scraper.
Vite + React + TypeScript + Tailwind v4.  Static SPA — no SSR, no backend.

> Audience for this README: a developer setting the project up locally or
> debugging the deploy.  The operator does not run any of these commands;
> the live site is built by Railway on push.

## Stack

| Concern        | Choice                                              |
| -------------- | --------------------------------------------------- |
| Build          | Vite 5                                              |
| Framework      | React 18 (StrictMode)                               |
| Routing        | `react-router-dom` v6                               |
| Server state   | `@tanstack/react-query` v5                          |
| Styling        | Tailwind CSS v4 (CSS-only config via `@theme`)      |
| DB client      | `@supabase/supabase-js` reading `*_public` views    |
| Map            | `maplibre-gl` + OpenFreeMap (Positron tiles)        |
| Charts         | `recharts` (lazy-loaded; SVG sparklines elsewhere)  |

## Local development

```sh
cd frontend
cp .env.example .env
# fill in VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY
npm install
npm run dev      # http://localhost:5173
npm run build    # type-check + production bundle to dist/
```

Node 20+ required.

### Where to find each env var

- `VITE_SUPABASE_URL` — Supabase dashboard → Project Settings → API → Project URL.
- `VITE_SUPABASE_ANON_KEY` — same page → "anon public" / "publishable" key.
  Safe to ship to the browser; reads are fenced by the `*_public` views in
  migration 008.

## Project layout

```
src/
  main.tsx           react root + react-query provider + router
  App.tsx            mounts the routed shell
  routes.tsx         route table
  lib/
    supabase.ts      single shared client
    types.ts         shapes mirroring listings_public et al.
    queries.ts       supabase query helpers (grows with parts B–E)
  components/
    Shell.tsx        top bar + nav + footer
  pages/
    Browse.tsx       part B
    ListingDetail.tsx part C
    Region.tsx       part D
    Health.tsx       part E
  styles/
    globals.css      design tokens (@theme), typography, base reset
```

## Design system in one place

All visual tokens live in `src/styles/globals.css` under a single `@theme`
block (light) plus a dark-mode mirror.  Colour names are domain words —
`--color-paper`, `--color-ink`, `--color-copper`, `--color-brick`, etc. —
not abstract greys.  Depth strategy is **borders only** (no shadows),
with one carve-out: map-anchored popovers may use a small shadow because
borders fail the squint test against arbitrary tile imagery.

Numbers always use tabular figures (`font-variant-numeric: tabular-nums`)
and Czech locale (`Intl.NumberFormat('cs-CZ')`) so a column of prices
aligns to the rightmost digit.

## Deploy

Railway, as a second service in the same project as the API.  The
service builds from this `frontend/` directory using the local
`Dockerfile` (Node build stage → Caddy serve stage) and exposes a
public domain that Railway terminates TLS at.  See the root `README.md`
for the click-by-click setup.

`Caddyfile` handles SPA routing (`try_files {path} /index.html`),
gzip + zstd compression, immutable caching for hashed `assets/*`
bundles, and a `/healthz` endpoint that Railway uses for liveness.

CI lives at `.github/workflows/frontend-build.yml` and runs
`npm install && npm run build` on every push to catch broken builds
before Railway sees them.

### Build-time vs runtime env vars

Vite inlines `import.meta.env.VITE_*` into the JS bundle at **build
time** as string constants — the deployed bundle never reads env at
runtime.  Practical consequence: rotating either of `VITE_SUPABASE_URL`
or `VITE_SUPABASE_ANON_KEY` requires a redeploy (push or click
"Redeploy" in Railway), not just a variable update.
