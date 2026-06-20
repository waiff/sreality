# Realitní výnos (MF) — Chrome extension

Overlays our **MF reference rent** (`mf_reference_rent_czk`) and the
**"Výnos MF"** gross yield (`mf_gross_yield_pct`) — the same precomputed
figures the SPA's Browse cards show for sale apartments — on listing pages
across **every portal we scrape** (sreality, bazos, bezrealitky, idnes,
maxima, remax, mmreality, ceskereality).

- **Detail pages** get a floating panel (closed shadow root). For **any**
  listing we have, it shows a **deal-pipeline control** (bookmark, then change
  stage / remove) + an **"Otevřít v aplikaci"** deep-link to that listing's page in our
  app (`/listing/{sreality_id}`) plus its subject facts. For **apartments for
  sale** it additionally shows the Výnos MF headline + MF reference rent, with a
  comparables-based estimation as the deeper tool / fallback. (MF + estimation
  are gated to byt+prodej; the bookmark + app link + facts are not.) Listings
  not in our DB show a short "není v databázi" note.
  - The **pipeline control** is property-grain (the deal pipeline, rule #22),
    the same as the SPA's listing-detail control. Out of pipeline → a
    **"Přidat do pipeline"** button that inserts the property at the entry stage.
    In pipeline → a pill with a stage **`<select>`** (change the deal stage) and a
    **`✕`** (remove). Writes go through the same bearer-gated
    `POST/DELETE /pipeline/cards` (bookmark/remove) + `PATCH /pipeline/cards/{id}`
    (change stage — stamps `entered_stage_at`, logs a `moved` event) the SPA uses;
    membership (incl. the current `stage_id`) comes back on the
    `POST /listings/lookup` response and the stage list from `GET /pipeline/stages`.
    Hidden only while a freshly-scraped listing has no property yet (a few minutes).
- **Index / search pages** get a small per-card badge: `Výnos MF X.X %` when
  we have it, otherwise a clickable **Odhadnout výnos** badge that runs one
  on-demand estimation by that card's own URL.

The default display is a **read** of data we already have — no LLM call. It
maps each portal listing to our row by `(source, native id)` through the
FastAPI `POST /listings/lookup` endpoint (the public views don't expose the
native id, so the browser can't resolve non-sreality listings on its own).
The editable estimation block still shares its scenario state (rent / fond
oprav / listing price) with the SPA's `/estimation/:id` page via the
`scenario` JSONB column on `estimation_runs`.

> **Backend dependency:** needs the `POST /listings/lookup` endpoint
> (shipped in the `feature/portal-mf-lookup` PR). Make sure that's deployed
> to the Railway API before loading this build.

## Distribution policy — read first

The extension bundles the FastAPI bearer token (`API_TOKEN`) into
the build output at `dist/`. That token gives write access to
`POST /estimations` (creates LLM-billed runs) and `PATCH
/estimations/:id/scenario`.

**Ship `dist/` only to operators you trust.** A `.crx` is a zip;
anyone with the file can extract the token. Do NOT upload this
build to the public Chrome Web Store. The same security posture as
the SPA today (see `frontend/.env.example`).

If you ever need a publicly-distributable build, see the "Path 3"
section at the bottom of this README.

## Build option A — GitHub Actions (recommended)

No local toolchain required. The repo has a workflow at
`.github/workflows/build-extension.yml` that builds the extension
in CI and uploads the result as a downloadable artifact.

One-time setup — repository secrets:
1. GitHub repo → **Settings** → **Secrets and variables** → **Actions**.
2. Add `EXT_API_BASE_URL` = the Railway FastAPI URL (no trailing slash).
3. Add `EXT_API_TOKEN` = the same `API_TOKEN` value the Railway
   service uses.
4. (Optional) Add `EXT_APP_BASE_URL` = the SPA (browser app) URL, no trailing
   slash — powers the "Otevřít v aplikaci" link to a listing's page in our app
   (`/listing/{sreality_id}`). Leave it unset to hide the link.

Building:
1. The workflow runs automatically on every push to `main` that
   touches `chrome-extension/**`, or you can trigger it manually:
   GitHub → **Actions** → **Build Chrome extension** → **Run
   workflow**.
2. Wait for the green check (~1 minute).
3. On the run page scroll to **Artifacts** at the bottom →
   download **chrome-extension-dist**.
4. Unzip it. The unzipped folder is what you "Load unpacked" into
   Chrome.

## Build option B — Local

If you have Node 20 installed locally:

```sh
cd chrome-extension
cp .env.example .env
# edit .env: set VITE_API_BASE_URL and VITE_API_TOKEN
npm install
npm run build
```

The output lands in `chrome-extension/dist/`.

## Install in Chrome (unpacked)

1. Open `chrome://extensions` in Chrome.
2. Toggle **Developer mode** on (top-right).
3. Click **Load unpacked**.
4. Pick the `dist/` folder you downloaded (Option A) or built
   (Option B).
5. Confirm the extension card shows "Sreality Yield Panel" with
   the copper "%" icon.
6. **Copy the 32-character extension ID** shown on the card. You
   need it for the CORS step below.

## Allow the extension's origin in CORS (Railway)

The extension's fetches run from the background service worker
under the origin `chrome-extension://<id>` (using the ID from
step 6 above). The FastAPI service must allow that origin or every
request will be blocked.

1. Open the **Railway** dashboard.
2. Pick the FastAPI service (the one running `api/main.py`).
3. Open the **Variables** tab.
4. Find `CORS_ALLOW_ORIGINS`. (If it doesn't exist yet, click
   **+ New Variable** and add it.)
5. Set the value to a comma-separated list including the
   extension's origin. Example:
   ```
   https://your-spa.up.railway.app,chrome-extension://abcdefghijklmnopabcdefghijklmnop
   ```
   Keep any existing origins (your SPA's URL) and just append the
   `chrome-extension://...` one. **No spaces** around the comma.
6. Save. Railway auto-redeploys the service. Wait ~30 seconds for
   the new deploy to come up.

## Use

**On a listing detail page** (any supported portal):

1. A floating panel appears bottom-right.
2. At the top, for **any** listing we have: a **Přidat do pipeline** bookmark
   (click to add the property to the deal pipeline / again to remove — it fills
   in and shows the current stage) and an **Otevřít v aplikaci** link.
3. For a **sale apartment** we have, it shows **Výnos MF X.X %** + the MF
   reference rent (per month and per m²) and the subject facts.
4. For anything that isn't an apartment for sale, the MF/estimation block is
   **visibly deactivated** with a short note (the bookmark + app link still show).
5. Below the MF headline, the **comparables estimation** block: if a
   successful estimation exists it loads the editable yield scenario; if not,
   a **Spustit odhad** button `POST`s to `/estimations` and polls until done.
   Edits to the three fields debounce 500 ms and PATCH the scenario back —
   the SPA's `/estimation/:id` page picks up the same state on next load.

**On a search / index page** (any supported portal):

1. Each sale-apartment card we recognise gets a small badge.
2. `Výnos MF X.X %` when we have the yield (rent in the tooltip); otherwise a
   clickable **Odhadnout výnos** badge that runs one estimation for that card
   and swaps in the result.
3. Cards aren't matched by portal-specific markup — the overlay scans each
   card's detail link and resolves it through `/listings/lookup`, so it
   survives the portals reshuffling their card HTML. Cards not in our DB (or
   not sale apartments) get no badge.

### Supported portals

sreality, bazos, bezrealitky, idnes, maxima, remax — plus mmreality and
ceskereality (URL→id extractors there are best-effort until those portals
have data; a miss just shows no badge, never a wrong one).

> **Note:** index pages on the React/Next.js portals (sreality, bezrealitky)
> render cards client-side, so badges appear a moment after the results do and
> re-attach as you scroll/paginate (a `MutationObserver` watches for new cards).

## Path 3 (publicly distributable build — not implemented)

If you ever want to upload to the public Chrome Web Store, the
extension must NOT contain `API_TOKEN`. The alternative is a
zero-secret build that:

1. Reads listing + estimation data via the Supabase **anon key**
   from the existing `*_public` views.
2. Bounces every write through the SPA in a new tab
   (`window.open('https://your-spa/estimate?source=' + ...)`).

The SPA's existing password gate continues to gate writes. This is
a meaningful redesign — opening it should be a deliberate decision.
