# Limen Reality — Chrome extension (Realitní výnos / MF panel)

Overlays our **MF reference rent** (`mf_reference_rent_czk`) and the
**"Výnos MF"** gross yield (`mf_gross_yield_pct`) — the same precomputed
figures the SPA's Browse cards show for sale apartments — on listing pages
across **every portal we scrape** (sreality, bazos, bezrealitky, idnes,
maxima, remax, mmreality, ceskereality).

- **Detail pages** get a floating panel (closed shadow root). For **any**
  listing we have, it shows a **deal-pipeline control** (bookmark, then change
  stage / remove), a monitoring/collection toggle, **operator notes** (see the
  existing notes + add a new one), and an **"Otevřít v aplikaci"** deep-link to
  that listing's page in our app (`/listing/{sreality_id}`) plus its subject
  facts. For **apartments for sale** it additionally shows the Výnos MF headline +
  MF reference rent, with a comparables-based estimation as the deeper tool /
  fallback. (MF + estimation are gated to byt+prodej; the rest are not.) Listings
  not in our DB show a short "není v databázi" note.
  - The **notes** are property-grain (rule #18), the same notes the SPA's
    listing-detail `CurationBlock` shows. The panel lists the property's existing
    notes (most-recent-first) and an add box ("Přidat poznámku…" → **Uložit
    poznámku**). Writes go through the same bearer-gated `POST /properties/{id}/notes`
    the SPA uses, recording the viewed advert's `sreality_id` as the note's
    `origin_listing_id` ("written while viewing this advert"); the panel fetches
    existing notes lazily via `GET /properties/{id}/notes` on open.
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

## Sign-in (Wave 1)

The extension runs its **own** Supabase session — a separate sign-in from
the SPA's, never the SPA's refresh token (Supabase rotates refresh tokens
with reuse-detection, so sharing one between two independently-refreshing
sessions would eventually log both out). Every API route now requires a
real per-user session; the panel shows a **"Přihlásit se přes Google"**
prompt until you sign in, and a compact "signed in as … · Odhlásit" line
in the panel once you are.

Under the hood: `chrome.identity.launchWebAuthFlow` opens the Google
consent screen via Supabase GoTrue's PKCE flow; the code exchange and the
periodic silent refresh (every ~30 min, via `chrome.alarms` — MV3 kills
any in-memory timer when the service worker is evicted) happen entirely in
the background worker. No secret ships in the bundle for this — the
Supabase anon key is a public client key, same posture as the SPA.

**One-time setup for a NEW deployment** (already done for the operator's
existing Railway + Supabase project — skip unless standing up a fresh one):

1. **Pin the redirect URL.** This build's extension ID is fixed by the
   `key` field in `manifest.json` (so "Load unpacked" gives the same ID on
   every machine/CI download): `eibnegoankipleeegjilnjhpnbaedpjd`. Its auth
   redirect URL is `https://eibnegoankipleeegjilnjhpnbaedpjd.chromiumapp.org/`.
2. **Supabase dashboard** → your project → **Authentication** → **URL
   Configuration** → **Redirect URLs** → add that exact URL to
   **Additional Redirect URLs** → **Save**.
3. **Google Cloud Console** → your OAuth client (the same one Supabase's
   Google provider uses) → **APIs & Services** → **Credentials** → open the
   OAuth 2.0 Client → **Authorized redirect URIs** → add the SAME URL →
   **Save**. (This is the step that actually breaks sign-in if skipped —
   Google rejects the redirect before Supabase ever sees it.)
4. **Supabase dashboard** → **Authentication** → **Providers** → confirm
   **Google** is enabled.

If you ever need a *different* stable extension ID (e.g. a second
deployment), generate a new keypair and repeat steps 1–3 with the new ID:

```sh
openssl genrsa -out extension-key.pem 2048
openssl rsa -in extension-key.pem -pubout -outform DER | openssl base64 -A
```

Paste that output into manifest.json's `"key"` field. **Keep `extension-key.pem`
out of git** (already gitignored) — it isn't needed again unless you locally
pack a signed `.crx`; the committed `key` field (a public key) is all Chrome
needs to keep deriving the same ID.

## Distribution policy

No secret ships in the bundle — the Supabase anon key + FastAPI base URL are
both non-sensitive public client config (same posture as the SPA). The
extension is safe to distribute broadly, including via the public Chrome Web
Store, once the Chrome Web Store readiness items in
`docs/design/waves-1-4-public-features.md` (privacy policy, single-purpose
statement, staged rollout) are done.

## Build option A — GitHub Actions (recommended)

No local toolchain required. The repo has a workflow at
`.github/workflows/build-extension.yml` that builds the extension
in CI and uploads the result as a downloadable artifact.

One-time setup — repository secrets:
1. GitHub repo → **Settings** → **Secrets and variables** → **Actions**.
2. Add `EXT_API_BASE_URL` = the Railway FastAPI URL (no trailing slash).
3. Add `EXT_SUPABASE_URL` = the same value as the SPA's `VITE_SUPABASE_URL`.
4. Add `EXT_SUPABASE_ANON_KEY` = the same value as the SPA's
   `VITE_SUPABASE_ANON_KEY` (the anon/publishable key — not a secret key).
5. (Optional) Add `EXT_APP_BASE_URL` = the SPA (browser app) URL, no trailing
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
# edit .env: set VITE_API_BASE_URL, VITE_SUPABASE_URL, VITE_SUPABASE_ANON_KEY
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
5. Confirm the extension card shows "Limen Reality — výnos & pipeline" with
   the copper "%" icon.

The `key` field pinned in `manifest.json` means the extension ID is always
`eibnegoankipleeegjilnjhpnbaedpjd` (same on every machine, every CI
download) — the "one-time setup" steps above already registered it with
both Supabase and Google, and the origin below is already correct.

## Allow the extension's origin in CORS (Railway)

The extension's fetches run from the background service worker under the
origin `chrome-extension://eibnegoankipleeegjilnjhpnbaedpjd`. The FastAPI
service must allow that origin or every request will be blocked.

1. Open the **Railway** dashboard.
2. Pick the FastAPI service (the one running `api/main.py`).
3. Open the **Variables** tab.
4. Find `CORS_ALLOW_ORIGINS`. (If it doesn't exist yet, click
   **+ New Variable** and add it.)
5. Set the value to a comma-separated list including the extension's
   origin. Example:
   ```
   https://your-spa.up.railway.app,chrome-extension://eibnegoankipleeegjilnjhpnbaedpjd
   ```
   Keep any existing origins (your SPA's URL) and just append the
   `chrome-extension://...` one. **No spaces** around the comma.
6. Save. Railway auto-redeploys the service. Wait ~30 seconds for
   the new deploy to come up.

## Use

**First run:** the floating panel shows **"Přihlásit se přes Google"** —
click it, complete the Google consent screen in the popup window that
opens, and the panel reloads signed in. A compact "you@example.com ·
Odhlásit" line then appears at the top of the panel on every listing; click
**Odhlásit** to sign out again (any page will then prompt to sign back in).

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

## Chrome Web Store submission — what's left

The bundle itself carries no secret and every write is per-user + RLS-scoped
(Wave 1), so the remaining gap to a public listing is process, not code:

- `sourcemap:false` + confirm `minify:'esbuild'` for the store build
  (`sourcemap:true` today is a dev/debug convenience — flip it off before
  packaging a store submission).
- A privacy policy URL + per-permission justifications (`identity`,
  `alarms`, `storage`, the two `host_permissions` origins) + a single-purpose
  statement in the Developer Dashboard listing.
- Staged rollout, with the API kept backward-compatible across at least two
  extension versions (the dual-auth window in `verify_jwt` already covers
  this for the auth switch itself — older installed builds keep working
  against a static `API_TOKEN` until that's rotated platform-wide, a
  separate, deliberately-sequenced cutover per
  `docs/design/waves-1-4-public-features.md`).

Full detail: `docs/design/waves-1-4-public-features.md`, "Chrome Web Store
readiness".
