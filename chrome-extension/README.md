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

## Build

```sh
cd chrome-extension
cp .env.example .env
# edit .env: set VITE_API_BASE_URL and VITE_API_TOKEN
npm install
npm run build
```

The output lands in `chrome-extension/dist/` with three files —
`manifest.json`, `content.js`, `background.js` — plus a sourcemap.

## Install (unpacked, recommended for internal distribution)

1. Open `chrome://extensions` in Chrome.
2. Toggle **Developer mode** on (top-right).
3. Click **Load unpacked**.
4. Pick the `chrome-extension/dist/` folder.
5. Confirm the extension shows "Sreality Yield Panel".

## Allow the extension's origin in CORS

After loading the unpacked extension Chrome assigns it a stable ID
shown on the `chrome://extensions` page (a 32-character hash). The
extension's fetches run from the background service worker under
the origin `chrome-extension://<id>`.

Add that origin to the FastAPI service's `CORS_ALLOW_ORIGINS` env
var on Railway:

```
CORS_ALLOW_ORIGINS=https://your-spa.up.railway.app,chrome-extension://abcd…ef
```

Multiple origins are comma-separated. Restart the Railway service
after the env-var change.

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
