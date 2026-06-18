# sreality · database browser

Web UI over the Supabase database produced by the scraper: it reads the
public views directly and routes any write through the FastAPI service.
Vite + React + TypeScript + Tailwind v4.  Static SPA — no SSR, no backend.

The estimation flow (`/estimate` → `/estimation/:id` → `/estimations`) is
end-to-end: paste a URL, edit the spec, submit, and the run is persisted by
the FastAPI service and rendered with a full trace timeline + comparables
table.  The Timeline component dispatches on `step.kind` so it renders
today's deterministic 4-step traces and the future U4 agent's longer
traces without rework.

> Audience for this README: anyone setting the project up locally or
> debugging the deploy.  The operator now works locally (VS Code on WSL2) and
> can run these commands; the live site is still built by Railway on push to
> `main`.

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
- Listing photos need **no** R2 env var. They are served through the API's
  `GET /images/{storage_path}` redirect (which mints a short-lived presigned
  R2 URL), so the durable R2 copy reaches the browser without baking a bucket
  base into the build or exposing the bucket publicly. The single
  `imageSrc()` helper (`src/lib/imageUrl.ts`) builds `${VITE_API_BASE_URL}/images/${storage_path}`,
  falling back to the original CDN URL only for a just-scraped listing whose
  bytes the async image job hasn't downloaded yet.

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
    listingUrl.ts    listingPath(srealityId) — the ONE internal /listing/:id
                     builder; every listing link routes through it (runLinks
                     builds the ?run= surface on top). Don't hand-roll the path.
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
runtime.  Practical consequence: changing any `VITE_*` value (e.g.
`VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, or `VITE_R2_PUBLIC_BASE`)
requires a redeploy (push or click "Redeploy" in Railway), not just a
variable update. (Listing-image serving deliberately has **no** build-time
R2 env — it routes through the API at runtime — so it can never silently
regress on a missing variable the way a baked-in bucket base could.)
