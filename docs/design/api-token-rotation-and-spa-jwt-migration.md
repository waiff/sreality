# API_TOKEN rotation & the SPA → user-JWT migration

**Status:** planned, operator-sequenced. This is the runbook for retiring the shared
static bearer token — the last piece of the public-release auth story. It has a
code half (the SPA must send per-user JWTs) and an operator half (rotate the secret).
Written 2026-07-23 after a two-account pen-test surfaced the live symptom below.

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

Crucially, `verify_jwt` treats a request bearing this token as a **synthetic platform
admin**: `{"sub": None, "role": "operator", "is_admin": True, "legacy": True}`. And
`tenant_pool.tenant_conn` routes a `legacy` caller to the **unscoped service-role DB
connection** — RLS is bypassed. So *the static token is a god-token*: it authenticates as
admin and reads/writes every account's data.

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

**Interim posture until Part A ships:** treat the SPA as an **operator-only** console and
keep it behind its password gate. The **extension** is the per-user-safe public surface
(Wave 1). Do not onboard non-operator tenants onto the SPA yet.

## The cutover — two parts

### Part A — SPA sends per-user JWTs (code; the real fix for the leak)

The SPA must send the logged-in user's Supabase `access_token` instead of the static token.
The extension already does exactly this; the SPA is the last static-token client.

1. **`frontend/src/lib/api.ts` `request()`** — when a Supabase session exists, send
   `Authorization: Bearer <session.access_token>`; fall back to the static token only when
   logged out (public/anon calls) — or drop the static fallback entirely once every
   SPA-called route accepts a JWT. The session is already available via the auth context
   (`frontend/src/lib/auth.tsx`).
2. **Route audit — every route the SPA calls must accept a JWT, not only the static token.**
   `verify_jwt` accepts *both* the static token (→ legacy admin) and a real user JWT, so
   migrating a route from `require_token` → `verify_jwt` never breaks existing callers.
   Per-account data routes additionally move onto `tenant_pool.tenant_conn` (RLS) — the same
   pattern Wave 1/2 already applied to `/pipeline/*`, `/collections` (GET), `/estimations/*`,
   notes, and `/listings/lookup`. Routes still on `require_token` that the SPA calls (e.g.
   `POST /collections`, `GET/PATCH/DELETE /collections/{id}`, and other curation/estimation
   writes) need this migration or they will 401 once the SPA stops sending the static token.
3. **Admin continuity is already satisfied:** the operator's Supabase user
   (`hejtmanekp@gmail.com`) carries `app_metadata.is_admin = true`, which Supabase includes
   in the JWT, so `require_admin` (which checks `app_metadata.is_admin`) keeps passing under
   the operator's real JWT. The second account has no such flag → correctly non-admin.
4. **Verify** with the two real accounts: the non-admin account sees only its own
   collections/tags/notes/pipeline through the SPA; the operator still reaches every admin
   page; no route the SPA uses 401s.

Part A is a self-contained engineering PR (SPA `request()` change + a bounded route-auth
audit). It closes the leak **without** needing the secret rotated. Recommend shipping it as
its own focused change with the operator watching, since it touches auth on the live console.

### Part B — rotate the secret (operator; after Part A)

Once no legitimate caller *needs* the old shared token, rotate its value so any leaked copy
(every SPA bundle ever shipped contains it) becomes useless.

1. **Inventory the remaining legitimate holders** of the old token and give each a plan:
   - SPA — after Part A it no longer needs it (drop `VITE_API_TOKEN` from the build).
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
- **Before rotating:** confirm Part A has shipped and the ClickUp/automation token list is
  complete (Part B step 1).
- **At rotation:** change `API_TOKEN` on Railway, update ClickUp + any automation to the new
  value, redeploy the API and the SPA. (These are dashboard/Railway actions a session can't do.)
- **Until Part A ships:** keep the SPA operator-only behind its password gate; route new
  tenants through the extension.

## Relationship to public signup

Enabling public **email/password signup** is safe for the **extension** (per-user JWTs, RLS).
It does **not** grant SPA access — the SPA is separately gated by its password gate — but a
new signup who somehow reaches the SPA would inherit the god-token problem above, which is
exactly why the SPA stays operator-only until Part A. Signup and this migration are
independent; do signup now, sequence the rotation deliberately.
