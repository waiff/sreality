# Sreality Yield Panel — Chrome extension

An inline yield-scenario panel that mounts on `sreality.cz` listing
detail pages, talking to the project's FastAPI service. The panel
shares its scenario state (rent / fond oprav / listing price) with
the SPA's `/estimation/:id` page via the `scenario` JSONB column on
`estimation_runs` — edit on either surface and the other sees it on
next load.

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

1. Open any `https://www.sreality.cz/detail/...` page.
2. A floating panel appears bottom-right.
3. If the listing has an existing successful estimation, the panel
   shows its yield + the editable scenario.
4. If not, the panel shows a **Run estimation** button. Clicking it
   `POST`s to `/estimations` and polls until the run completes.

Edits to the three input fields debounce by 500ms and PATCH the
scenario back to the API. The SPA's `/estimation/:id` page picks
up the same state on next load.

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
