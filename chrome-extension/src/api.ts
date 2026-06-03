/* Background-worker-side fetch wrapper. Only this file talks to the
 * FastAPI service directly; the content script proxies via runtime
 * messages so the network call runs in a context with host_permissions
 * and isn't subject to sreality.cz's CORS posture. */

import type {
  ApiResult,
  EstimationRun,
  PortalListing,
  PortalLookupItem,
  PortalLookupResponse,
  YieldScenarioUpdate,
} from './types';

/* Tolerate an operator entering the API URL without a scheme (e.g. just
 * `api.up.railway.app`) — fetch() needs an absolute URL, so default to https.
 * Also strip any trailing slash so `BASE_URL + path` doesn't double up. */
function normalizeBaseUrl(raw: string): string {
  const trimmed = raw.trim();
  if (trimmed === '') return '';
  const withScheme = /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
  return withScheme.replace(/\/$/, '');
}

const BASE_URL = normalizeBaseUrl(import.meta.env.VITE_API_BASE_URL ?? '');
const TOKEN = import.meta.env.VITE_API_TOKEN ?? '';

if (!BASE_URL) {
  console.warn(
    '[sreality-ext] VITE_API_BASE_URL is empty — extension cannot ' +
    'reach the FastAPI service. Set it in .env and rebuild.',
  );
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<ApiResult<T>> {
  if (!BASE_URL) {
    return { ok: false, status: 0, detail: 'API base URL not configured' };
  }
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...((init.headers as Record<string, string> | undefined) ?? {}),
  };
  if (init.body !== undefined) headers['Content-Type'] = 'application/json';
  if (TOKEN) headers.Authorization = `Bearer ${TOKEN}`;

  let res: Response;
  try {
    res = await fetch(BASE_URL + path, { ...init, headers });
  } catch (err) {
    return {
      ok: false,
      status: 0,
      detail: err instanceof Error ? err.message : 'network error',
    };
  }

  const text = await res.text();
  let body: unknown = null;
  if (text) {
    try { body = JSON.parse(text); } catch { body = text; }
  }

  if (!res.ok) {
    const detail =
      body && typeof body === 'object' && body !== null && 'detail' in body
        ? String((body as { detail: unknown }).detail)
        : res.statusText || `HTTP ${res.status}`;
    return { ok: false, status: res.status, detail };
  }

  return { ok: true, data: body as T };
}

/* POST /listings/lookup — batch (source, native id) → our scraped facts +
 * precomputed MF reference rent / "Výnos MF" yield + latest estimate. One
 * request resolves every visible card on an index page; the detail panel
 * sends a single item. Returns one entry per requested item, in order. */
export async function lookupListings(
  items: PortalLookupItem[],
): Promise<ApiResult<PortalListing[]>> {
  if (items.length === 0) return { ok: true, data: [] };
  const res = await request<PortalLookupResponse>('/listings/lookup', {
    method: 'POST',
    body: JSON.stringify({ items }),
  });
  if (!res.ok) return res;
  return { ok: true, data: res.data.data };
}

/* PATCH /estimations/:id/scenario — write the operator's yield
 * overrides. A body with all fields null clears the column. */
export async function patchScenario(
  run_id: number,
  body: YieldScenarioUpdate,
): Promise<ApiResult<EstimationRun>> {
  return request<EstimationRun>(`/estimations/${run_id}/scenario`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

/* POST /estimations — kicks off a new estimation for the listing the
 * operator is currently viewing. Defaults match the SPA's "rent" path
 * since that's the most common ask from the listing page; we surface
 * the resulting row id and the panel polls until it lands. */
export async function createEstimation(
  url: string,
): Promise<ApiResult<EstimationRun>> {
  return request<EstimationRun>('/estimations', {
    method: 'POST',
    body: JSON.stringify({
      url,
      source: 'ui',
      estimate_kind: 'rent',
      mode: 'deterministic',
    }),
  });
}

/* GET /estimations/:id — used by the polling loop after we trigger a
 * new estimation. Returns when the row reaches a terminal status. */
export async function getEstimation(
  run_id: number,
): Promise<ApiResult<EstimationRun>> {
  return request<EstimationRun>(`/estimations/${run_id}`);
}
