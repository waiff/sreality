/* Background-worker-side fetch wrapper. Only this file talks to the
 * FastAPI service directly; the content script proxies via runtime
 * messages so the network call runs in a context with host_permissions
 * and isn't subject to sreality.cz's CORS posture. */

import type {
  ApiResult,
  EstimationRun,
  PipelineCardResult,
  PipelineStage,
  PortalListing,
  PortalLookupItem,
  PortalLookupResponse,
  YieldScenarioUpdate,
} from './types';

/* Tolerate an operator entering the API URL without a scheme (e.g. just
 * `api.up.railway.app`) — fetch() needs an absolute URL, so default to https.
 * Also strip any trailing slash so `BASE_URL + path` doesn't double up.
 *
 * NB: this is duplicated as a tiny inline in content.ts — MV3 content scripts
 * are classic scripts that can't `import`, so the content + service-worker
 * bundles cannot share a runtime module. A 5-line pure helper is the cheapest
 * thing to copy across that boundary. */
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

/* POST /pipeline/cards — bookmark a property into the deal pipeline (rule #22),
 * landing it at the entry stage. The SAME bearer-gated endpoint the SPA's
 * BookmarkButton / PipelineToggle use — one write path, idempotent server-side
 * (ON CONFLICT DO NOTHING). Returns the card incl. its entry-stage label. */
export async function addPipelineCard(
  property_id: number,
): Promise<ApiResult<PipelineCardResult>> {
  return request<PipelineCardResult>('/pipeline/cards', {
    method: 'POST',
    body: JSON.stringify({ property_id }),
  });
}

/* DELETE /pipeline/cards/:property_id — un-bookmark (drop the card, ledger-logged). */
export async function removePipelineCard(
  property_id: number,
): Promise<ApiResult<PipelineCardResult>> {
  return request<PipelineCardResult>(`/pipeline/cards/${property_id}`, {
    method: 'DELETE',
  });
}

/* PATCH /pipeline/cards/:property_id — change the deal stage. The SAME audited
 * write the SPA's PipelineToggle + the kanban use: it stamps `entered_stage_at`
 * and logs a `moved` event to property_pipeline_events. Returns the moved card. */
export async function movePipelineCard(
  property_id: number,
  stage_id: number,
): Promise<ApiResult<PipelineCardResult>> {
  return request<PipelineCardResult>(`/pipeline/cards/${property_id}`, {
    method: 'PATCH',
    body: JSON.stringify({ stage_id }),
  });
}

/* GET /pipeline/stages — the operator-curated stage list, to populate the stage
 * `<select>`. Returns `{data:[...]}`; we unwrap to the array. */
export async function listPipelineStages(): Promise<ApiResult<PipelineStage[]>> {
  const res = await request<{ data: PipelineStage[] }>('/pipeline/stages');
  if (!res.ok) return res;
  return { ok: true, data: res.data.data };
}
