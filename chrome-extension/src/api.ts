/* Background-worker-side fetch wrapper. Only this file talks to the
 * FastAPI service directly; the content script proxies via runtime
 * messages so the network call runs in a context with host_permissions
 * and isn't subject to sreality.cz's CORS posture. */

import { getAccessToken } from './auth';
import type {
  ApiResult,
  CollectionWriteResult,
  EstimationRun,
  ExtCollection,
  ExtNote,
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

if (!BASE_URL) {
  console.warn(
    '[sreality-ext] VITE_API_BASE_URL is empty — extension cannot ' +
    'reach the FastAPI service. Set it in .env and rebuild.',
  );
}

/* Every extension-touched route now runs on verify_jwt + the tenant pool
 * (Wave 1 W1-1), so a request with no session is a clean, expected "please
 * sign in" — not a network failure. `status: 401` + this sentinel `detail`
 * lets the panel show a sign-in prompt instead of a raw error string. */
export const NOT_SIGNED_IN_DETAIL = 'not_signed_in';

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<ApiResult<T>> {
  if (!BASE_URL) {
    return { ok: false, status: 0, detail: 'API base URL not configured' };
  }
  const token = await getAccessToken();
  if (!token) {
    return { ok: false, status: 401, detail: NOT_SIGNED_IN_DETAIL };
  }
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...((init.headers as Record<string, string> | undefined) ?? {}),
  };
  if (init.body !== undefined) headers['Content-Type'] = 'application/json';
  headers.Authorization = `Bearer ${token}`;

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
      source: 'extension',
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

/* GET /collections — the operator-curated collections (rule #18). The panel
 * reads `monitoring_enabled` + `is_system` to pick a monitoring target for its
 * one-click toggle. Returns `{data:[...]}`; we unwrap to the array. */
export async function listCollections(): Promise<ApiResult<ExtCollection[]>> {
  const res = await request<{ data: ExtCollection[] }>('/collections');
  if (!res.ok) return res;
  return { ok: true, data: res.data.data };
}

/* POST /collections/:id/properties — add the property to a collection (rule #18).
 * The SAME bearer-gated route the SPA's Collection page uses; idempotent
 * server-side (ON CONFLICT DO NOTHING). Returns `{added, skipped}`. */
export async function addToCollection(
  collection_id: number,
  property_id: number,
): Promise<ApiResult<CollectionWriteResult>> {
  return request<CollectionWriteResult>(`/collections/${collection_id}/properties`, {
    method: 'POST',
    body: JSON.stringify({ property_ids: [property_id] }),
  });
}

/* DELETE /collections/:id/properties/:property_id — remove the property from a
 * collection. Returns `{removed}`. */
export async function removeFromCollection(
  collection_id: number,
  property_id: number,
): Promise<ApiResult<CollectionWriteResult>> {
  return request<CollectionWriteResult>(
    `/collections/${collection_id}/properties/${property_id}`,
    { method: 'DELETE' },
  );
}

/* GET /properties/:id/notes — the property's operator notes (rule #18),
 * most-recent-first. Returns `{data:[...]}`; we unwrap to the array. */
export async function listNotes(property_id: number): Promise<ApiResult<ExtNote[]>> {
  const res = await request<{ data: ExtNote[] }>(`/properties/${property_id}/notes`);
  if (!res.ok) return res;
  return { ok: true, data: res.data.data };
}

/* POST /properties/:id/notes — add an operator note to the property. The SAME
 * bearer-gated route the SPA's CurationBlock uses; `origin_listing_ref_id` records
 * the advert being viewed (display provenance only). Returns the created note. */
export async function addNote(
  property_id: number,
  body: string,
  origin_listing_ref_id: number | null,
): Promise<ApiResult<ExtNote>> {
  return request<ExtNote>(`/properties/${property_id}/notes`, {
    method: 'POST',
    body: JSON.stringify({ body, origin_listing_ref_id }),
  });
}
