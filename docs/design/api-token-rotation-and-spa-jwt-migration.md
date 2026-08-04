# API_TOKEN rotation & the SPA → user-JWT migration

**Status:** Part A's admin/tenant slice **shipped 2026-08-04**; the `require_token`-only
route audit and Part B (secret rotation) remain operator-sequenced. This is the runbook for
retiring the shared static bearer token — the last piece of the public-release auth story.
It has a code half (the SPA must send per-user JWTs) and an operator half (rotate the
secret). Written 2026-07-23 after a two-account pen-test surfaced the live symptom below.

**2026-08-04 update:** `verify_jwt`'s legacy branch (quoted below) is now **fully removed**,
not just de-privileged — the static token no longer authenticates *anything* through
`verify_jwt`, closing both the `require_admin` god-token bypass and the `tenant_conn`
RLS-bypass for every route that already used it. `frontend/src/lib/api.ts` sends the
caller's real Supabase JWT on every `require_admin`/`verify_jwt`/`tenant_conn`-gated call
(a per-call `jwt: true` request option, not a blanket switch — see Part A below). **Not**
done by this change: the `require_token`-only route audit (step 2) — `POST /collections`,
single-collection CRUD, tags, buildings, manual estimates, filter-presets, and (pre-#940)
brokers still authenticate via the shared static token and are not yet per-account scoped.
The "treat the SPA as operator-only" interim posture stays in force until that audit lands.

## What `API_TOKEN` is

`API_TOKEN` is a **single shared static bearer token** — one secret string, the same for
everyone. It exists in three places:

- **Railway, API service env var `API_TOKEN`** — the server's copy. `api/dependencies.py`'s
  `require_token` accepts a request only if its `Authorization: Bearer …` equals this value.
- **The SPA bundle, build-time `VITE_API_TOKEN`** — inlined into the JavaScript at build,
  so it is **extractable by anyone who loads the SPA** (browser devtools → it's in the JS).
  `frontend/src/lib/api.ts` sends it on *every* API call.
- **Any other trusted caller** that talks to the API directly (e.g. a ClickUp automation,
  CI scripts) — each holds its own copy of the same string.

Crucially, `verify_jwt` **used to** treat a request bearing this token as a **synthetic
platform admin**: `{"sub": None, "role": "operator", "is_admin": True, "legacy": True}`. And
`tenant_pool.tenant_conn` routed a `legacy` caller to the **unscoped service-role DB
connection** — RLS was bypassed. So *the static token was a god-token*: it authenticated as
admin and read/wrote every account's data. (Retired 2026-08-04 — see the status note above.
`require_token`, the separate simpler gate for non-identity routes, is unaffected and still
accepts this token by design.)

## Why it must change (the live symptom)

Because `VITE_API_TOKEN` ships inside the SPA bundle and grants admin, **the SPA is not a
per-tenant-safe surface today.** Concretely, from the 2026-07-23 two-account pen-test:

- The new second account (`petr.hejtmanek@limenventures.com`) logged into the SPA and saw
  **three "monitoring" collections** — one per account on the platform — instead of only
  their own.
- Root cause: the SPA sends the static `API_TOKEN` on `GET /collections` (and every other
  API read). The API sees the god-token → legacy admin → service-role connection → RLS
  bypassed → the query returns **all accounts'** rows.
- This is **not** an RLS or data-model bug. The pen-test confirmed the database is correctly
  scoped: reading the same tables under a real user JWT (`SET ROLE authenticated` + the
  user's claims) returns exactly the caller's own one collection. The **Chrome extension is
  already safe** — since Wave 1 it runs its own Supabase session and sends a per-user JWT
  (`chrome-extension/src/auth.ts`), so its `GET /collections` is RLS-scoped.

So the leak is entirely: **the SPA authenticates to the API with the shared admin token
instead of the logged-in user's JWT.** Anyone who can reach the SPA (past its password gate)
also holds admin API access via the embedded token.

**Interim posture — still in force for the `require_token` surface:** treat the SPA as an
**operator-only** console and keep it behind its password gate for anything not yet migrated
off `require_token` (step 2 below). The **extension** is the per-user-safe public surface
(Wave 1). Do not onboard non-operator tenants onto the SPA yet.

## The cutover — two parts

### Part A — SPA sends per-user JWTs (code; the real fix for the leak)

The SPA must send the logged-in user's Supabase `access_token` instead of the static token.
The extension already does exactly this; the SPA was the last static-token client.

1. **SHIPPED 2026-08-04 — `frontend/src/lib/api.ts` `request()`.** Every call site that
   maps to a `require_admin`/`verify_jwt`/`tenant_conn`-gated backend route passes a new
   `jwt: true` request option (see the file's header comment for the full route list); the
   shared `request()`/`apiGet`/`apiPost` helpers then send
   `Authorization: Bearer <session.access_token>` (via `supabase.auth.getSession()`) instead
   of the static token for that call. Calls without the flag (routes still on `require_token`)
   are unaffected. This is a per-call opt-in, not a blanket "always send the JWT" switch —
   the blanket approach would also send a JWT to `require_token`-only routes, which reject
   anything that isn't a literal match on the static secret and would 401 the SPA on those
   calls (tags, single-collection CRUD, buildings, manual estimates, filter-presets). Backing
   this up, `verify_jwt`'s legacy branch is now fully removed (not just de-privileged) — see
   the status note at the top of this doc — so a `require_admin`/`verify_jwt` route rejects
   the static token outright regardless of what the frontend sends.
2. **STILL OPEN — route audit: every route the SPA calls must accept a JWT, not only the
   static token.** Routes still on `require_token` that the SPA calls (`POST /collections`,
   `GET/PATCH/DELETE /collections/{id}`, tags, buildings, manual estimates, filter-presets,
   and pre-#940 brokers) still authenticate via the shared static token and are not yet
   per-account scoped. Migrating one means moving it onto `verify_jwt`/`tenant_conn` (the
   pattern already used by `/pipeline/*`, `/collections` GET, `/estimations/*` create/read,
   notes, and `/listings/lookup`) *and* updating its `frontend/src/lib/api.ts` call site to
   add `jwt: true` in the same change — do both together, not one then the other, or the
   route either 401s (JWT added, frontend not updated) or silently keeps accepting the
   shared secret (frontend updated, JWT not required server-side).
3. **Admin continuity is confirmed working:** the operator's Supabase user
   (`hejtmanekp@gmail.com`) carries `app_metadata.is_admin = true` (verified live against
   the `admins` table and `auth.users.raw_app_meta_data` on 2026-08-04 — no Custom Access
   Token Hook is configured; Supabase includes `app_metadata` in every issued JWT by
   default), so `require_admin` keeps passing under the operator's real JWT. The one other
   account on the platform has no such flag → correctly non-admin.
4. **Verified 2026-08-04:** the full backend + frontend test suites pass with the legacy
   branch removed (`tests/api/test_verify_jwt.py`, `test_admin_routes.py`, `test_auth.py`
   all assert the old static token now gets 401/403, not 200, on every route it used to
   silently pass).

Step 1 shipped as a self-contained PR alongside the `verify_jwt` legacy-branch removal. It
closes the `require_admin` god-token bypass and the `tenant_conn` RLS-bypass for every route
that already used those gates, **without** needing the secret rotated. Step 2 (the
`require_token` route audit) is unstarted — do it as its own follow-up, since each route
needs a real backend gate change, not just a frontend header change.

### Part B — rotate the secret (operator; after Part A)

Once no legitimate caller *needs* the old shared token, rotate its value so any leaked copy
(every SPA bundle ever shipped contains it) becomes useless.

1. **Inventory the remaining legitimate holders** of the old token and give each a plan:
   - SPA — still needs `VITE_API_TOKEN` for its `require_token`-only calls until Part A step 2
     (the route audit) lands; can only drop it once every SPA-called route accepts a JWT.
   - Extension — already off it (Wave 1).
   - **ClickUp / any automation / CI** — if these call the API with the static token, they
     need the new value (or their own dedicated credential). Confirm the full list before
     rotating; a missed caller breaks at rotation.
2. **Force old SPA bundles out** (optional but recommended): because the *old* bundle carries
   the *old* token, users on a stale tab keep working until you rotate. Either accept that
   rotation logs them out (they reload → new bundle) or add a min-version check first.
3. **Generate a new random `API_TOKEN`** (e.g. `openssl rand -hex 32`).
4. **Update it everywhere it's legitimately used, in one window:** Railway API service env
   `API_TOKEN`; ClickUp's stored token; any CI/script secret. (Railway redeploys the API on
   env change.)
5. **Rebuild + redeploy the SPA without a privileged `VITE_API_TOKEN`** (Part A means it
   doesn't need one; if the anon/public path still wants a token, use a non-admin one).
6. The old token is now dead — every leaked copy is inert.

## What the operator needs to do

- **Decide the sequencing/date.** Rotation is disruptive: it invalidates old SPA bundles and
  breaks any automation still on the old token — so it's a dated cutover, not a background task.
- **Before rotating:** confirm Part A step 2 (the `require_token` route audit) has shipped
  and the ClickUp/automation token list is complete (Part B step 1). Step 1 (this PR) alone
  isn't enough — the SPA still depends on the static token for its `require_token` calls.
- **At rotation:** change `API_TOKEN` on Railway, update ClickUp + any automation to the new
  value, redeploy the API and the SPA. (These are dashboard/Railway actions a session can't do.)
- **Until Part A step 2 ships:** keep the SPA operator-only behind its password gate; route
  new tenants through the extension.

## Relationship to public signup

Enabling public **email/password signup** is safe for the **extension** (per-user JWTs, RLS).
It does **not** grant SPA access — the SPA is separately gated by its password gate — but a
new signup who somehow reaches the SPA would inherit the god-token problem above, which is
exactly why the SPA stays operator-only until Part A. Signup and this migration are
independent; do signup now, sequence the rotation deliberately.
