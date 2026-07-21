/* The extension's OWN Supabase session — hand-rolled PKCE against GoTrue
 * (no supabase-js in the vanilla-TS bundle). Runs in the background service
 * worker only (chrome.identity + the token fetches need that context).
 *
 * Never reuse the SPA's refresh token: Supabase rotates refresh tokens with
 * reuse-detection, so a token shared between two independently-refreshing
 * sessions would eventually revoke both. This is a fully separate sign-in. */

function normalizeBaseUrl(raw: string): string {
  const trimmed = raw.trim();
  if (trimmed === '') return '';
  const withScheme = /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
  return withScheme.replace(/\/$/, '');
}

const SUPABASE_URL = normalizeBaseUrl(import.meta.env.VITE_SUPABASE_URL ?? '');
const SUPABASE_ANON_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY ?? '';
/* Every GoTrue REST endpoint lives under /auth/v1 — Supabase's Kong gateway
 * 404s ("requested path is invalid") on anything hit at the bare project
 * root, so this prefix is load-bearing, not cosmetic. */
const AUTH_BASE = SUPABASE_URL + '/auth/v1';

if (!SUPABASE_URL || !SUPABASE_ANON_KEY) {
  console.warn(
    '[sreality-ext] VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY are empty — ' +
    'sign-in is unavailable. Set them in .env and rebuild.',
  );
}

const SESSION_KEY = 'authSession';
const VERIFIER_KEY = 'pkceVerifier';
/* Refresh a little before actual expiry so a request never races a token
 * that's valid-on-read but expired-on-arrival at the API. */
const EXPIRY_SKEW_SECONDS = 60;

interface StoredSession {
  access_token: string;
  refresh_token: string;
  /* Epoch seconds. */
  expires_at: number;
}

interface GoTrueTokenResponse {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  expires_at?: number;
  token_type: string;
}

export interface AuthState {
  signedIn: boolean;
  email: string | null;
}

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = '';
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

async function sha256(input: string): Promise<Uint8Array> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(input));
  return new Uint8Array(digest);
}

/* RFC 7636 verifier — 43 base64url chars from 32 random bytes, well within
 * the 43-128 char spec range. */
function randomVerifier(): string {
  return base64UrlEncode(crypto.getRandomValues(new Uint8Array(32)));
}

async function challengeFor(verifier: string): Promise<string> {
  return base64UrlEncode(await sha256(verifier));
}

/* Best-effort JWT payload decode for display only (email in the sign-in
 * strip) — never used for authorization, the API independently verifies
 * the token via JWKS. */
function decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const [, payload] = token.split('.');
    if (payload == null) return null;
    const padded = payload.replace(/-/g, '+').replace(/_/g, '/').padEnd(
      payload.length + ((4 - (payload.length % 4)) % 4), '=',
    );
    return JSON.parse(atob(padded)) as Record<string, unknown>;
  } catch {
    return null;
  }
}

/* GoTrue error responses come in two shapes depending on the project's Auth
 * server version: the older `{error, error_description}` and the current
 * `{code, error_code, msg}`. Checking only the old shape silently swallowed
 * every real error into a generic fallback — surface whichever is present. */
function extractGoTrueError(body: unknown): string | null {
  if (body == null || typeof body !== 'object') return null;
  const b = body as Record<string, unknown>;
  if (typeof b.msg === 'string') {
    return typeof b.error_code === 'string' ? `${b.msg} (${b.error_code})` : b.msg;
  }
  if (typeof b.error_description === 'string') return b.error_description;
  if (typeof b.error === 'string') return b.error;
  return null;
}

async function gotrueFetch(path: string, init: RequestInit & { accessToken?: string }): Promise<{
  ok: boolean;
  status: number;
  body: unknown;
}> {
  const { accessToken, ...rest } = init;
  const headers: Record<string, string> = {
    apikey: SUPABASE_ANON_KEY,
    Authorization: `Bearer ${accessToken ?? SUPABASE_ANON_KEY}`,
    'Content-Type': 'application/json',
    ...((rest.headers as Record<string, string> | undefined) ?? {}),
  };
  let res: Response;
  try {
    res = await fetch(AUTH_BASE + path, { ...rest, headers });
  } catch {
    return { ok: false, status: 0, body: null };
  }
  const text = await res.text();
  let body: unknown = null;
  if (text) {
    try { body = JSON.parse(text); } catch { body = text; }
  }
  return { ok: res.ok, status: res.status, body };
}

async function getStoredSession(): Promise<StoredSession | null> {
  const r = await chrome.storage.local.get([SESSION_KEY]);
  const s = r[SESSION_KEY] as StoredSession | undefined;
  return s ?? null;
}

async function storeSession(token: GoTrueTokenResponse): Promise<void> {
  const expires_at = token.expires_at
    ?? Math.floor(Date.now() / 1000) + token.expires_in;
  const session: StoredSession = {
    access_token: token.access_token,
    refresh_token: token.refresh_token,
    expires_at,
  };
  await chrome.storage.local.set({ [SESSION_KEY]: session });
}

async function clearSession(): Promise<void> {
  await chrome.storage.local.remove(SESSION_KEY);
}

/* Sign-in via chrome.identity.launchWebAuthFlow + PKCE against GoTrue. Opens
 * a focused auth window for the Google consent screen; GoTrue's PKCE
 * callback redirects to our chromiumapp.org URL with `?code=...`, which we
 * exchange for a session. The code_verifier lives in chrome.storage.session
 * (survives a service-worker restart mid-flow, cleared on browser close —
 * exactly the lifetime a one-shot sign-in attempt needs). */
export async function signInWithGoogle(): Promise<{ ok: true } | { ok: false; detail: string }> {
  if (!SUPABASE_URL || !SUPABASE_ANON_KEY) {
    return { ok: false, detail: 'Přihlášení není nakonfigurováno (chybí Supabase URL/klíč).' };
  }

  const verifier = randomVerifier();
  await chrome.storage.session.set({ [VERIFIER_KEY]: verifier });
  const challenge = await challengeFor(verifier);
  const redirectUri = chrome.identity.getRedirectURL();

  const authUrl = `${AUTH_BASE}/authorize?${new URLSearchParams({
    provider: 'google',
    code_challenge: challenge,
    code_challenge_method: 's256',
    redirect_to: redirectUri,
  }).toString()}`;

  let responseUrl: string;
  try {
    responseUrl = await new Promise<string>((resolve, reject) => {
      chrome.identity.launchWebAuthFlow({ url: authUrl, interactive: true }, (redirectUrl) => {
        if (chrome.runtime.lastError || !redirectUrl) {
          reject(new Error(chrome.runtime.lastError?.message ?? 'přihlášení zrušeno'));
          return;
        }
        resolve(redirectUrl);
      });
    });
  } catch (err) {
    await chrome.storage.session.remove(VERIFIER_KEY);
    return { ok: false, detail: err instanceof Error ? err.message : 'přihlášení selhalo' };
  }

  const stored = await chrome.storage.session.get([VERIFIER_KEY]);
  const codeVerifier = stored[VERIFIER_KEY] as string | undefined;
  await chrome.storage.session.remove(VERIFIER_KEY);

  const returned = new URL(responseUrl);
  const code = returned.searchParams.get('code');
  const errorDescription = returned.searchParams.get('error_description');
  if (code == null) {
    return { ok: false, detail: errorDescription ?? 'GoTrue nevrátilo autorizační kód.' };
  }
  if (codeVerifier == null) {
    return { ok: false, detail: 'PKCE ověřovací kód vypršel, zkuste to znovu.' };
  }

  const res = await gotrueFetch('/token?grant_type=pkce', {
    method: 'POST',
    body: JSON.stringify({ auth_code: code, code_verifier: codeVerifier }),
  });
  if (!res.ok) {
    const detail = extractGoTrueError(res.body)
      ?? `výměna kódu za relaci selhala (HTTP ${res.status})`;
    return { ok: false, detail };
  }
  await storeSession(res.body as GoTrueTokenResponse);
  return { ok: true };
}

/* At most one refresh in flight at a time — the lazy (per-request) and the
 * chrome.alarms-periodic refresh paths both call this, and Supabase's
 * refresh-token reuse-detection would log the user out if two refreshes
 * raced on the same (now-rotated) token. */
let refreshInFlight: Promise<boolean> | null = null;

function refreshSingleFlight(): Promise<boolean> {
  if (refreshInFlight == null) {
    refreshInFlight = doRefresh().finally(() => { refreshInFlight = null; });
  }
  return refreshInFlight;
}

async function doRefresh(): Promise<boolean> {
  const s = await getStoredSession();
  if (s == null) return false;
  const res = await gotrueFetch('/token?grant_type=refresh_token', {
    method: 'POST',
    body: JSON.stringify({ refresh_token: s.refresh_token }),
  });
  if (!res.ok) {
    console.warn('[sreality-ext] session refresh failed:', extractGoTrueError(res.body) ?? res.status);
    await clearSession();
    return false;
  }
  await storeSession(res.body as GoTrueTokenResponse);
  return true;
}

/* A valid access token, refreshing first if it's near/past expiry. Returns
 * null when signed out or the refresh failed (session cleared either way —
 * callers surface a "please sign in" state). */
export async function getAccessToken(): Promise<string | null> {
  const s = await getStoredSession();
  if (s == null) return null;
  const now = Math.floor(Date.now() / 1000);
  if (s.expires_at - now > EXPIRY_SKEW_SECONDS) return s.access_token;
  const ok = await refreshSingleFlight();
  if (!ok) return null;
  const fresh = await getStoredSession();
  return fresh?.access_token ?? null;
}

/* Called on a chrome.alarms tick (~30 min) so a long-idle service worker
 * still keeps the refresh token rotating — MV3 SW eviction kills any
 * in-memory auto-refresh timer, this is the periodic backstop for it. */
export async function refreshIfSignedIn(): Promise<void> {
  const s = await getStoredSession();
  if (s == null) return;
  const now = Math.floor(Date.now() / 1000);
  if (s.expires_at - now > EXPIRY_SKEW_SECONDS) return;
  await refreshSingleFlight();
}

export async function getAuthState(): Promise<AuthState> {
  const s = await getStoredSession();
  if (s == null) return { signedIn: false, email: null };
  const claims = decodeJwtPayload(s.access_token);
  const email = typeof claims?.email === 'string' ? claims.email : null;
  return { signedIn: true, email };
}

/* Best-effort server-side revocation (design doc: "server-side revocation on
 * sign-out" mitigates chrome.storage.local being unencrypted-at-rest) — the
 * local session is cleared regardless of whether the network call succeeds. */
export async function signOut(): Promise<void> {
  const s = await getStoredSession();
  await clearSession();
  if (s != null) {
    void gotrueFetch('/logout?scope=local', {
      method: 'POST', accessToken: s.access_token,
    }).catch(() => { /* best-effort */ });
  }
}
