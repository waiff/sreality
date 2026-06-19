/* Fetch wrapper for the Railway FastAPI service.
 *
 * SECURITY NOTE: VITE_API_TOKEN is inlined into the JS bundle at build
 * time and is therefore extractable by anyone with browser devtools.
 * This is the same security category as the password gate — it keeps
 * casual visitors out, the real protection is the password gate plus
 * the URL not being shared publicly. Do NOT treat this token as a
 * meaningful secret. Server-side enforcement sits at
 * api/dependencies.py:require_token. See frontend/README.md.
 */

import type {
  BuildingAttachment,
  BuildingListResponse,
  BuildingRun,
  Collection,
  CollectionWithListings,
  ConfirmBuildingUnitsIn,
  CreateBuildingFromUrlIn,
  UpdateBuildingInputsIn,
  CreateEstimationIn,
  EstimationFeedback,
  EstimationListParams,
  EstimationListResponse,
  EstimationRun,
  ListingEstimate,
  ListingSummaryBatchRow,
  Ppm2Box,
  ManualRentalEstimate,
  CreateManualEstimateIn,
  UpdateManualEstimateIn,
  Note,
  ParseResult,
  SkillRefinement,
  SourceKind,
  Tag,
  TagColor,
  WatchdogDispatch,
  WatchdogDispatchesResponse,
  WatchdogFilterSpec,
  WatchdogSeenFilter,
  WatchdogSubscription,
  FilterPreset,
  DedupCandidatesResponse,
  DedupSummaryResponse,
  MergesResponse,
} from './types';
import type { PresetSpec } from './filters';

/* Sources the backend allowlists for high-confidence parsing.
 * Anything else falls through to a best-effort parse. The order is
 * the order shown in the UI's "Supported:" tip line. Keep in sync
 * with scraper/source_dispatcher._KIND_SUFFIXES on the backend. */
export const SUPPORTED_SOURCES: ReadonlyArray<{
  kind: SourceKind;
  label: string;
  hostHint: string;
}> = [
  { kind: 'sreality',      label: 'sreality',      hostHint: 'sreality.cz' },
  { kind: 'bezrealitky',   label: 'bezrealitky',   hostHint: 'bezrealitky.cz' },
  { kind: 'idnes_reality', label: 'idnes-reality', hostHint: 'reality.idnes.cz' },
  { kind: 'remax',         label: 'remax',         hostHint: 'remax-czech.cz' },
];

/* Display label for a source kind. Falls back to the raw kind so
 * unknown future kinds surface visibly rather than silently. */
export const sourceKindLabel = (kind: SourceKind | null): string => {
  if (kind == null) return '—';
  if (kind === 'unsupported') return 'unsupported';
  const found = SUPPORTED_SOURCES.find((s) => s.kind === kind);
  return found ? found.label : kind;
};

/* Quick host-based classification — used by the URL input to choose
 * the right loading copy ("Fetching listing…" vs "Reading listing
 * with Claude…") before the request goes out. The backend re-classifies
 * authoritatively; this is a UX optimisation, not a security boundary. */
export const classifyUrlHost = (url: string): SourceKind => {
  let host: string;
  try {
    host = new URL(url.trim()).hostname.toLowerCase();
  } catch {
    return 'unsupported';
  }
  for (const { kind, hostHint } of SUPPORTED_SOURCES) {
    if (host === hostHint || host.endsWith('.' + hostHint)) return kind;
  }
  return 'unsupported';
};

const BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '');
const TOKEN = import.meta.env.VITE_API_TOKEN ?? '';

export const isApiConfigured = (): boolean => Boolean(BASE_URL);

if (!BASE_URL) {
  console.warn(
    'API env vars missing. Set VITE_API_BASE_URL (and VITE_API_TOKEN for prod).',
  );
}

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly body: unknown,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export type QueryValue = string | number | boolean | undefined | null;

interface RequestOptions extends Omit<RequestInit, 'body'> {
  query?: Record<string, QueryValue>;
  json?: unknown;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  if (!BASE_URL) {
    throw new ApiError(
      'API base URL is not configured',
      0,
      { detail: 'VITE_API_BASE_URL is empty' },
    );
  }

  const { query, json, headers, ...rest } = opts;
  const url = new URL(BASE_URL + path);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v != null) url.searchParams.set(k, String(v));
    }
  }

  const finalHeaders: Record<string, string> = {
    Accept: 'application/json',
    ...(json !== undefined ? { 'Content-Type': 'application/json' } : {}),
    ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
    ...((headers as Record<string, string> | undefined) ?? {}),
  };

  let res: Response;
  try {
    res = await fetch(url.toString(), {
      ...rest,
      headers: finalHeaders,
      body: json !== undefined ? JSON.stringify(json) : undefined,
    });
  } catch (err) {
    throw new ApiError(
      err instanceof Error ? err.message : 'Network error',
      0,
      null,
    );
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
    throw new ApiError(detail, res.status, body);
  }

  return body as T;
}

/* Generic verbs used by lib/maps.ts (and any other future module that
 * needs raw GET/POST without going through a feature-specific wrapper). */
export const apiGet = <T>(
  path: string,
  params?: Record<string, string | number | undefined>,
  signal?: AbortSignal,
): Promise<T> =>
  request<T>(path, { query: params as Record<string, QueryValue> | undefined, signal });

export const apiPost = <T>(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): Promise<T> => request<T>(path, { method: 'POST', json: body, signal });

/* ----- estimations ------------------------------------------------------- */

/* POST /estimations/preview — generic URL parser (sreality fast path
 * + LLM-driven per-source parser for everything else, dispatched on
 * the backend). When force_refresh is true the 7-day URL cache is
 * bypassed and a fresh parse is performed (the cache row is also
 * upserted on success). */
export const previewListingUrl = (
  url: string,
  options: { force_refresh?: boolean } = {},
): Promise<ParseResult> =>
  request<ParseResult>('/estimations/preview', {
    method: 'POST',
    json: { url, force_refresh: options.force_refresh ?? false },
  });

export const createEstimation = (
  input: CreateEstimationIn,
): Promise<EstimationRun> =>
  request<EstimationRun>('/estimations', { method: 'POST', json: input });

export const getEstimation = (id: number): Promise<EstimationRun> =>
  request<EstimationRun>(`/estimations/${id}`);

/* PATCH /estimations/:id/scenario — shared yield-scenario state.
 * Used by YieldBlock and the Chrome extension. All three fields are
 * optional; sending the body with every field null clears overrides
 * back to defaults. */
export interface YieldScenarioUpdate {
  rent_czk?: number | null;
  fond_per_m2_czk?: number | null;
  price_czk?: number | null;
}

export const patchEstimationScenario = (
  id: number,
  body: YieldScenarioUpdate,
): Promise<EstimationRun> =>
  request<EstimationRun>(`/estimations/${id}/scenario`, {
    method: 'PATCH',
    json: body,
  });

export interface TracePayload {
  step_n: number;
  full_output: unknown;
  captured_at: string | null;
}

export const getTracePayload = (
  runId: number,
  stepN: number,
): Promise<TracePayload> =>
  request<TracePayload>(`/estimations/${runId}/trace/${stepN}/payload`);

/* Phase AI slice B — feedback capture. POST inserts a new
 * `estimation_feedback` row and (default) fires the slice C
 * refiner inline; the response carries the (feedback, refinement)
 * pair so the UI can show the proposed prompt without a second
 * round-trip. */
export interface CreateFeedbackIn {
  feedback_text: string;
  kick_off_refinement?: boolean;
}

export interface FeedbackResponse {
  feedback: EstimationFeedback;
  refinement: SkillRefinement | null;
}

export const listEstimationFeedback = (
  runId: number,
): Promise<{ data: EstimationFeedback[] }> =>
  request<{ data: EstimationFeedback[] }>(
    `/estimations/${runId}/feedback`,
  );

export const submitEstimationFeedback = (
  runId: number,
  input: CreateFeedbackIn,
): Promise<FeedbackResponse> =>
  request<FeedbackResponse>(`/estimations/${runId}/feedback`, {
    method: 'POST',
    json: input,
  });

export const decideRefinement = (
  refinementId: number,
  decision: 'apply' | 'dismiss',
): Promise<SkillRefinement> =>
  request<SkillRefinement>(`/skill-refinements/${refinementId}/decision`, {
    method: 'POST',
    json: { decision },
  });

export const listEstimations = (
  params: EstimationListParams = {},
): Promise<EstimationListResponse> =>
  request<EstimationListResponse>('/estimations', {
    query: params as Record<string, QueryValue>,
  });

/* GET /estimations/latest-by-listing — latest rent estimate per listing id,
 * for the Browse cards' on-card estimate chip. Returns a map keyed by
 * sreality_id (string keys after JSON); ids with no rent run are absent. */
export const latestEstimationsByListing = (
  ids: ReadonlyArray<number>,
  signal?: AbortSignal,
): Promise<Record<number, ListingEstimate>> =>
  ids.length === 0
    ? Promise.resolve({})
    : request<{ estimates: Record<number, ListingEstimate> }>(
        '/estimations/latest-by-listing',
        { query: { sreality_ids: ids.join(',') }, signal },
      ).then((r) => r.estimates);

/* POST /listings/summaries — batch wrapper around the
 * summarize_listing toolkit function. The backend cache means
 * repeat calls for the same (sreality_id, snapshot_id) pairs are
 * effectively free. Per-item failures surface inline; one bad id
 * never fails the whole request. */
export const fetchListingSummaries = (
  items: ReadonlyArray<{ sreality_id: number; snapshot_id: number | null }>,
): Promise<{ data: ListingSummaryBatchRow[] }> =>
  request<{ data: ListingSummaryBatchRow[] }>('/listings/summaries', {
    method: 'POST',
    json: { items },
  });

/* POST /tools/summarize_region_dispositions — one-to-two-sentence
 * natural-language annotation per per-disposition Kč/m² box plot in
 * Browse > Stats. Generated server-side from the same ppm2_box payload
 * that drives the chart. Cached server-side per (region, calendar day):
 * the first viewer of a region today pays for the LLM call, everyone
 * else hits the cache. `region_key` is the caller's deterministic
 * serialization of the active filter set (see regionKeyFromFilters). */
export interface RegionDispositionAnnotationsInput {
  region_key: string;
  dispositions: ReadonlyArray<{
    disposition: string;
    n: number;
    ppm2_box: Ppm2Box | null;
  }>;
  ppm2_overall?: { p25: number; p50: number; p75: number } | null;
  region_label?: string | null;
}

export interface RegionDispositionAnnotationsResult {
  data: {
    region_key: string;
    annotations: Record<string, string>;
    model: string;
    cost_usd: number | null;
    cache_hit: boolean;
  };
  metadata: Record<string, unknown>;
}

export const fetchRegionDispositionAnnotations = (
  input: RegionDispositionAnnotationsInput,
  signal?: AbortSignal,
): Promise<RegionDispositionAnnotationsResult> =>
  request<RegionDispositionAnnotationsResult>(
    '/tools/summarize_region_dispositions',
    { method: 'POST', json: input, signal },
  );

/* ----- freshness (Phase U2.5) -------------------------------------------- *
 *
 * POST /tools/verify_listing_freshness — on-demand re-fetch of one listing.
 * The endpoint logs to listing_freshness_checks and may write a new
 * listing_snapshots row and/or flip listings.is_active (the explicit
 * write-allowed exception per CLAUDE.md). max_age_hours defaults to 0 here
 * so an operator clicking the button always triggers a real check rather
 * than the throttle's `cached` short-circuit.
 */

export type FreshnessOutcome =
  | 'unchanged'
  | 'updated'
  | 'gone'
  | 'fetch_error'
  | 'cached';

export interface VerifyFreshnessResult {
  data: {
    sreality_id: number;
    outcome: FreshnessOutcome;
    verified: boolean;
    cached: boolean;
    age_hours: number | null;
    what_changed: string[];
    snapshot_id: number | null;
    current: Record<string, unknown> | null;
  };
  metadata: {
    tool: string;
    filters_used: Record<string, unknown>;
    result_count: number;
    queried_at: string;
    data_freshness: string | null;
  };
}

export const verifyListingFreshness = (
  sreality_id: number,
  options: { max_age_hours?: number } = {},
): Promise<VerifyFreshnessResult> =>
  request<VerifyFreshnessResult>('/tools/verify_listing_freshness', {
    method: 'POST',
    json: { sreality_id, max_age_hours: options.max_age_hours ?? 0 },
  });

/* ----- buildings (Phase B1) ---------------------------------------------- */

export const createBuildingFromUrl = (
  input: CreateBuildingFromUrlIn,
): Promise<BuildingRun> =>
  request<BuildingRun>('/buildings/from_url', {
    method: 'POST',
    json: input,
  });

export const getBuilding = (id: number): Promise<BuildingRun> =>
  request<BuildingRun>(`/buildings/${id}`);

export const listBuildings = (
  params: { source?: string; status?: string; limit?: number; offset?: number } = {},
): Promise<BuildingListResponse> =>
  request<BuildingListResponse>('/buildings', {
    query: params as Record<string, QueryValue>,
  });

export const confirmBuildingUnits = (
  id: number,
  input: ConfirmBuildingUnitsIn,
): Promise<BuildingRun> =>
  request<BuildingRun>(`/buildings/${id}/confirm_units`, {
    method: 'POST',
    json: input,
  });

export const reExtractBuilding = (id: number): Promise<BuildingRun> =>
  request<BuildingRun>(`/buildings/${id}/re_extract`, { method: 'POST' });

export const updateBuildingInputs = (
  id: number,
  input: UpdateBuildingInputsIn,
): Promise<BuildingRun> =>
  request<BuildingRun>(`/buildings/${id}/inputs`, {
    method: 'PATCH',
    json: input,
  });

/* Multipart upload — bypasses the JSON helper. Each call uploads ONE
 * file; the caller fans out for multi-file pickers. The server replies
 * with the inserted BuildingAttachment row. */
export const uploadBuildingAttachment = async (
  buildingId: number,
  file: File,
): Promise<BuildingAttachment> => {
  if (!BASE_URL) {
    throw new ApiError(
      'API base URL is not configured', 0,
      { detail: 'VITE_API_BASE_URL is empty' },
    );
  }
  const url = new URL(`${BASE_URL}/buildings/${buildingId}/attachments`);
  url.searchParams.set('source', 'ui');
  const form = new FormData();
  form.append('file', file, file.name);
  let res: Response;
  try {
    res = await fetch(url.toString(), {
      method: 'POST',
      body: form,
      headers: {
        Accept: 'application/json',
        ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
      },
    });
  } catch (err) {
    throw new ApiError(
      err instanceof Error ? err.message : 'Network error', 0, null,
    );
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
    throw new ApiError(detail, res.status, body);
  }
  return body as BuildingAttachment;
};

export const listBuildingAttachments = (
  buildingId: number,
): Promise<{ data: BuildingAttachment[] }> =>
  request<{ data: BuildingAttachment[] }>(
    `/buildings/${buildingId}/attachments`,
  );

export const deleteBuildingAttachment = (
  buildingId: number,
  attachmentId: number,
): Promise<{ ok: true }> =>
  request<{ ok: true }>(
    `/buildings/${buildingId}/attachments/${attachmentId}`,
    { method: 'DELETE' },
  );

/* Build a fetch URL for one attachment's raw bytes. The route is
 * bearer-gated, so callers that want to render the image in <img> tags
 * must either fetch via this helper and convert to a blob URL, or
 * include the token in a query param (we use the fetch + blob path,
 * which keeps the token out of the URL). */
export const buildingAttachmentRawUrl = (
  buildingId: number,
  attachmentId: number,
): string => {
  if (!BASE_URL) return '';
  return `${BASE_URL}/buildings/${buildingId}/attachments/${attachmentId}/raw`;
};

export const fetchBuildingAttachmentBlob = async (
  buildingId: number,
  attachmentId: number,
): Promise<Blob> => {
  if (!BASE_URL) {
    throw new ApiError('API base URL is not configured', 0, null);
  }
  const url = buildingAttachmentRawUrl(buildingId, attachmentId);
  const res = await fetch(url, {
    headers: {
      ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
    },
  });
  if (!res.ok) {
    throw new ApiError(
      `HTTP ${res.status} fetching attachment`,
      res.status,
      null,
    );
  }
  return res.blob();
};

/* ----- admin / Settings page --------------------------------------------
 *
 * The /admin/* prefix is bearer-gated like every other write surface per
 * CLAUDE.md rule #8. These calls go through `request()`, which already
 * attaches `Authorization: Bearer <VITE_API_TOKEN>`, so no extra wiring is
 * needed here.
 */

export interface AgentTool {
  name: string;
  description: string;
}

export interface SkillLimits {
  max_iterations: number;
  max_cost_usd: number;
  wall_clock_timeout_s: number;
}

export interface Skill {
  name: string;
  description: string;
  system_prompt: string;
  allowed_tools: string[];
  preferred_model: Record<string, string>;
  limits: SkillLimits;
  updated_at: string | null;
  /* Migration 051 — non-null when this skill row has been archived.
   * Archived skills are hidden from the Settings list by default;
   * pass `?include_archived=true` to the GET /admin/skills endpoint
   * to see them. */
  archived_at: string | null;
}

export interface SkillUpdate {
  description?: string;
  system_prompt?: string;
  allowed_tools?: string[];
  preferred_model?: Record<string, string>;
  limits?: SkillLimits;
}

export interface AppSetting {
  key: string;
  value: unknown;
  description: string | null;
  updated_at: string | null;
}

export const listSkills = (
  options: { includeArchived?: boolean } = {},
): Promise<{ data: Skill[] }> =>
  request<{ data: Skill[] }>('/admin/skills', {
    query: { include_archived: options.includeArchived ?? false },
  });

export const getSkill = (name: string): Promise<Skill> =>
  request<Skill>(`/admin/skills/${encodeURIComponent(name)}`);

export const updateSkill = (
  name: string,
  patch: SkillUpdate,
): Promise<Skill> =>
  request<Skill>(`/admin/skills/${encodeURIComponent(name)}`, {
    method: 'PUT',
    json: patch,
  });

export const listAppSettings = (): Promise<{ data: AppSetting[] }> =>
  request<{ data: AppSetting[] }>('/admin/app_settings');

export const getAppSetting = (key: string): Promise<AppSetting> =>
  request<AppSetting>(`/admin/app_settings/${encodeURIComponent(key)}`);

export const updateAppSetting = (
  key: string,
  value: unknown,
): Promise<AppSetting> =>
  request<AppSetting>(`/admin/app_settings/${encodeURIComponent(key)}`, {
    method: 'PUT',
    json: { value },
  });

export const listAgentTools = (): Promise<{ data: AgentTool[] }> =>
  request<{ data: AgentTool[] }>('/admin/tools');

/* ----- per-portal operational limits (Scrapers dashboard, migration 114) ---
 * Each portal's limits resolve as CLI override > per-portal DB > global
 * (app_settings.scraper_limits_global, edited via updateAppSetting) > code
 * default. `overrides` is the raw per-portal jsonb; `effective` is the resolved
 * value the scraper would use today; `baked_default` is the code floor. */

export interface PortalLimitValues {
  index_rate?: number | null;
  detail_workers?: number | null;
  detail_rate?: number | null;
  max_detail_per_run?: number | null;
  max_detail_per_category?: number | null;
  image_workers?: number | null;
  max_image_downloads?: number | null;
  suspicious_stop_window?: number | null;
  suspicious_stop_threshold?: number | null;
}

export interface PortalAdminRow {
  source: string;
  label: string;
  kind: 'scraper' | 'parser';
  stage: string;
  sort_order: number;
  is_enabled: boolean;
  supports_complete_walk: boolean;
  overrides: PortalLimitValues | null;
  effective: PortalLimitValues | null;
  baked_default: PortalLimitValues | null;
}

export const listPortals = (): Promise<{ data: PortalAdminRow[] }> =>
  request<{ data: PortalAdminRow[] }>('/admin/portals');

export const updatePortalLimits = (
  source: string,
  patch: PortalLimitValues,
): Promise<{ source: string; overrides: PortalLimitValues; effective: PortalLimitValues }> =>
  request(`/admin/portals/${encodeURIComponent(source)}/limits`, {
    method: 'PUT',
    json: patch,
  });

/* ----- rent map: MF Cenová mapa nájemného (migration 132) ------------------
 * Revision history + manual upload + on-demand fetch, all on the bearer-gated
 * /admin/* surface. The same data also auto-grabs monthly via fetch_rent_map.yml. */

export interface RentMapRevision {
  source_revision: number;
  source_date: string | null;
  source_filename: string;
  row_count: number;
  uploaded_by: string | null;
  uploaded_at: string | null;
}

export interface RentMapIngestResult {
  ingested: boolean;
  source_revision: number | null;
  source_date: string | null;
  source_filename: string;
  file_sha256: string;
  territory_count: number;
  adjustment_count: number;
}

export const getRentMapStatus = (): Promise<{ current: RentMapRevision | null }> =>
  request<{ current: RentMapRevision | null }>('/admin/rent-map');

export const listRentMapRevisions = (): Promise<{ data: RentMapRevision[] }> =>
  request<{ data: RentMapRevision[] }>('/admin/rent-map/revisions');

export const triggerRentMapFetch = (): Promise<RentMapIngestResult> =>
  request<RentMapIngestResult>('/admin/rent-map/fetch', { method: 'POST' });

export async function uploadRentMapFile(
  file: File,
): Promise<RentMapIngestResult> {
  const form = new FormData();
  form.append('file', file);
  const res = await fetch(`${BASE_URL}/admin/rent-map/revisions`, {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
    },
    body: form,
  });
  const text = await res.text();
  const body: unknown = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail =
      typeof body === 'object' && body && 'detail' in body
        ? String((body as { detail: unknown }).detail)
        : `upload failed (${res.status})`;
    throw new ApiError(detail, res.status, body);
  }
  return body as RentMapIngestResult;
}

/* ----- condition scoring: per-kraj enablement ------------------------------
 * GET returns every kraj (admin_boundaries level='kraj') with its enabled
 * flag + count of unscored active listings; PUT replaces the full enabled
 * list (app_settings.condition_scoring_enabled_region_ids) and returns the
 * same payload. The scheduled batch job reads the same key. */

export interface ConditionScoringRegion {
  id: number;
  name: string;
  enabled: boolean;
  unscored_active: number;
}

export interface ConditionScoringRegionsPayload {
  regions: ConditionScoringRegion[];
  parked_no_geo: number;
  enabled_region_ids: number[];
}

export const getConditionScoringRegions = (): Promise<{
  data: ConditionScoringRegionsPayload;
}> =>
  request<{ data: ConditionScoringRegionsPayload }>(
    '/admin/condition-scoring/regions',
  );

export const updateConditionScoringRegions = (
  enabledRegionIds: number[],
): Promise<{ data: ConditionScoringRegionsPayload }> =>
  request<{ data: ConditionScoringRegionsPayload }>(
    '/admin/condition-scoring/regions',
    { method: 'PUT', json: { enabled_region_ids: enabledRegionIds } },
  );

/* ----- filter registry + visibility (PR 1 / migration 059) ----------------
 * The canonical filter list lives in toolkit/filter_registry.py. `getFilterSchema`
 * returns the live registry plus the agenda × filter visibility matrix.
 * `getFilterVisibility` is the same matrix without the registry — convenient
 * when the SPA already has the static codegen output and only needs the
 * operator's overrides. `setFilterVisibility` toggles one cell. */

import type {
  Agenda,
  FilterDef,
  UiControl,
  FilterType,
} from '@/lib/filterRegistry.generated';

export type { Agenda, FilterDef, UiControl, FilterType };

export interface FilterSchemaEntry extends FilterDef {
  visibility: Record<Agenda, boolean>;
}

export interface FilterSchemaPayload {
  agendas: Agenda[];
  categories: string[];
  ui_controls: UiControl[];
  filters: FilterSchemaEntry[];
}

export interface FilterVisibilityRow {
  agenda: Agenda;
  filter_id: string;
  enabled: boolean;
}

export const getFilterSchema = (): Promise<FilterSchemaPayload> =>
  request<FilterSchemaPayload>('/admin/filter-schema');

export const getFilterVisibility = (): Promise<{ data: FilterVisibilityRow[] }> =>
  request<{ data: FilterVisibilityRow[] }>('/admin/filter-visibility');

export const setFilterVisibility = (
  agenda: Agenda,
  filterId: string,
  enabled: boolean,
): Promise<FilterVisibilityRow> =>
  request<FilterVisibilityRow>(
    `/admin/filter-visibility/${encodeURIComponent(agenda)}/${encodeURIComponent(filterId)}`,
    { method: 'PUT', json: { enabled } },
  );

/* ----- curation (U2.6) ---------------------------------------------------
 *
 * Collections, tags, and notes. Reads of `which tags / which collections
 * does listing X belong to` go through the *_public Supabase views (see
 * lib/queries.ts) — there is no per-listing GET on the API. Everything
 * else (list-by-domain, create, update, delete, attach, detach) goes
 * through the bearer-gated FastAPI endpoints wrapped below.
 */

/* Collections */

export const listCollections = (): Promise<{ data: Collection[]; total: number }> =>
  request<{ data: Collection[]; total: number }>('/collections');

export const getCollection = (id: number): Promise<CollectionWithListings> =>
  request<CollectionWithListings>(`/collections/${id}`);

export const createCollection = (input: {
  name: string;
  description?: string | null;
}): Promise<Collection> =>
  request<Collection>('/collections', { method: 'POST', json: input });

export const updateCollection = (
  id: number,
  input: { name?: string | null; description?: string | null },
): Promise<Collection> =>
  request<Collection>(`/collections/${id}`, { method: 'PATCH', json: input });

export const deleteCollection = (id: number): Promise<{ deleted: true }> =>
  request<{ deleted: true }>(`/collections/${id}`, { method: 'DELETE' });

export const addListingsToCollection = (
  id: number,
  sreality_ids: number[],
): Promise<{ added: number; skipped: number }> =>
  request<{ added: number; skipped: number }>(`/collections/${id}/listings`, {
    method: 'POST',
    json: { sreality_ids },
  });

export const removeListingFromCollection = (
  id: number,
  sreality_id: number,
): Promise<{ removed: boolean }> =>
  request<{ removed: boolean }>(
    `/collections/${id}/listings/${sreality_id}`,
    { method: 'DELETE' },
  );

/* Tags */

export const listTags = (): Promise<{ data: Tag[] }> =>
  request<{ data: Tag[] }>('/tags');

export const createTag = (input: { name: string; color: TagColor }): Promise<Tag> =>
  request<Tag>('/tags', { method: 'POST', json: input });

export const updateTag = (
  id: number,
  patch: { name?: string | null; color?: TagColor | null },
): Promise<Tag> =>
  request<Tag>(`/tags/${id}`, { method: 'PATCH', json: patch });

export const deleteTag = (id: number): Promise<{ deleted: true }> =>
  request<{ deleted: true }>(`/tags/${id}`, { method: 'DELETE' });

export const attachTag = (
  sreality_id: number,
  tag_id: number,
): Promise<{ attached: boolean }> =>
  request<{ attached: boolean }>(`/listings/${sreality_id}/tags`, {
    method: 'POST',
    json: { tag_id },
  });

export const detachTag = (
  sreality_id: number,
  tag_id: number,
): Promise<{ detached: boolean }> =>
  request<{ detached: boolean }>(
    `/listings/${sreality_id}/tags/${tag_id}`,
    { method: 'DELETE' },
  );

/* Notes (per-listing journal) */

export const listListingNotes = (
  sreality_id: number,
): Promise<{ data: Note[] }> =>
  request<{ data: Note[] }>(`/listings/${sreality_id}/notes`);

export const createListingNote = (
  sreality_id: number,
  body: string,
): Promise<Note> =>
  request<Note>(`/listings/${sreality_id}/notes`, {
    method: 'POST',
    json: { body },
  });

/* Manual rental estimates (Phase U-ME).
 *
 * Reads can also come from the manual_rental_estimates_public Supabase
 * view via the anon key; the API endpoint is included here for
 * symmetry and direct API callers. Writes always go through the API. */

export const listManualEstimates = (
  sreality_id: number,
): Promise<{ data: ManualRentalEstimate[] }> =>
  request<{ data: ManualRentalEstimate[] }>(
    `/listings/${sreality_id}/manual_estimates`,
  );

export const createManualEstimate = (
  sreality_id: number,
  body: CreateManualEstimateIn,
): Promise<ManualRentalEstimate> =>
  request<ManualRentalEstimate>(
    `/listings/${sreality_id}/manual_estimates`,
    { method: 'POST', json: body },
  );

export const updateManualEstimate = (
  estimate_id: number,
  body: UpdateManualEstimateIn,
): Promise<ManualRentalEstimate> =>
  request<ManualRentalEstimate>(
    `/manual_estimates/${estimate_id}`,
    { method: 'PATCH', json: body },
  );

export const deleteManualEstimate = (
  estimate_id: number,
): Promise<{ deleted: true }> =>
  request<{ deleted: true }>(`/manual_estimates/${estimate_id}`, {
    method: 'DELETE',
  });

/* ----- Watchdog notifications (Phase U2.7) ------------------------------- */

export interface ListWatchdogDispatchesParams {
  subscription_id?: string;
  seen?: WatchdogSeenFilter;
  limit?: number;
  offset?: number;
}

export const listWatchdogSubscriptions = (
  options: { includeInactive?: boolean } = {},
): Promise<{ data: WatchdogSubscription[]; total: number }> =>
  request<{ data: WatchdogSubscription[]; total: number }>(
    '/notifications/subscriptions',
    { query: { include_inactive: options.includeInactive ?? true } },
  );

export const getWatchdogSubscription = (
  id: string,
): Promise<WatchdogSubscription> =>
  request<WatchdogSubscription>(
    `/notifications/subscriptions/${encodeURIComponent(id)}`,
  );

export const createWatchdogSubscription = (input: {
  name: string;
  filter_spec: WatchdogFilterSpec;
  is_active?: boolean;
}): Promise<WatchdogSubscription> =>
  request<WatchdogSubscription>('/notifications/subscriptions', {
    method: 'POST',
    json: input,
  });

export const updateWatchdogSubscription = (
  id: string,
  patch: {
    name?: string;
    filter_spec?: WatchdogFilterSpec;
    is_active?: boolean;
  },
): Promise<WatchdogSubscription> =>
  request<WatchdogSubscription>(
    `/notifications/subscriptions/${encodeURIComponent(id)}`,
    { method: 'PUT', json: patch },
  );

export const deleteWatchdogSubscription = (
  id: string,
): Promise<{ deleted: true }> =>
  request<{ deleted: true }>(
    `/notifications/subscriptions/${encodeURIComponent(id)}`,
    { method: 'DELETE' },
  );

export const listWatchdogDispatches = (
  params: ListWatchdogDispatchesParams = {},
): Promise<WatchdogDispatchesResponse> =>
  request<WatchdogDispatchesResponse>('/notifications/dispatches', {
    query: params as Record<string, QueryValue>,
  });

export const markWatchdogDispatchSeen = (
  dispatchId: string,
): Promise<WatchdogDispatch> =>
  request<WatchdogDispatch>(
    `/notifications/dispatches/${encodeURIComponent(dispatchId)}/mark-seen`,
    { method: 'POST' },
  );

export const kickoffWatchdogDispatchEstimate = (
  dispatchId: string,
): Promise<WatchdogDispatch> =>
  request<WatchdogDispatch>(
    `/notifications/dispatches/${encodeURIComponent(dispatchId)}/estimate`,
    { method: 'POST' },
  );

export const runWatchdogMatcher = (): Promise<{
  data: {
    subscriptions_evaluated: number;
    matches_inserted: number;
    listings_in_window: number;
  };
}> =>
  request<{
    data: {
      subscriptions_evaluated: number;
      matches_inserted: number;
      listings_in_window: number;
    };
  }>('/notifications/matcher/run', { method: 'POST' });

/* ----- Saved Browse filter presets (migration 151) ---------------------- */

export const listFilterPresets = (): Promise<{
  data: FilterPreset[];
  total: number;
}> =>
  request<{ data: FilterPreset[]; total: number }>('/filter-presets');

export const createFilterPreset = (input: {
  name: string;
  filter_spec: PresetSpec;
}): Promise<FilterPreset> =>
  request<FilterPreset>('/filter-presets', { method: 'POST', json: input });

export const updateFilterPreset = (
  id: string,
  patch: { name?: string; filter_spec?: PresetSpec },
): Promise<FilterPreset> =>
  request<FilterPreset>(`/filter-presets/${encodeURIComponent(id)}`, {
    method: 'PUT',
    json: patch,
  });

export const deleteFilterPreset = (id: string): Promise<{ deleted: true }> =>
  request<{ deleted: true }>(`/filter-presets/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });

/* ----- Cross-source dedup review (multi-portal PR3b) --------------------- */

export interface ListDedupCandidatesParams {
  status?: string;
  tier?: string;
  reason?: string;
  verdict?: string;
  limit?: number;
  offset?: number;
}

export interface MergeResult {
  data: {
    merge_group_id: string;
    survivor_id: number;
    retired_id: number;
    listings_moved: number;
  };
}

export interface UnmergeResult {
  data: {
    merge_group_id: string;
    survivor_id: number;
    retired_ids: number[];
    listings_moved_back: number;
    conflicts: number[];
  };
}

export const listDedupCandidates = (
  params: ListDedupCandidatesParams = {},
): Promise<DedupCandidatesResponse> =>
  request<DedupCandidatesResponse>('/dedup/candidates', {
    query: params as Record<string, QueryValue>,
  });

export const getDedupSummary = (
  status = 'proposed',
): Promise<DedupSummaryResponse> =>
  request<DedupSummaryResponse>('/dedup/summary', { query: { status } });

export const mergeDedupCandidate = (candidateId: number): Promise<MergeResult> =>
  request<MergeResult>(
    `/dedup/candidates/${candidateId}/merge`,
    { method: 'POST' },
  );

export const dismissDedupCandidate = (
  candidateId: number,
): Promise<{ id: number; status: string }> =>
  request<{ id: number; status: string }>(
    `/dedup/candidates/${candidateId}/dismiss`,
    { method: 'POST' },
  );

export interface ClusterMergeResult {
  merge_group_id: string;
  survivor_id: number;
  retired_ids: number[];
  listings_moved: number;
  candidates_resolved: number;
}

/* Merge a whole cluster of candidates (A-B, B-C, ...) into one property under
 * one reversible merge group. */
export const mergeDedupCluster = (
  candidateIds: number[],
): Promise<ClusterMergeResult> =>
  request<ClusterMergeResult>('/dedup/clusters/merge', {
    method: 'POST',
    json: { candidate_ids: candidateIds },
  });

export const dismissDedupCluster = (
  candidateIds: number[],
): Promise<{ dismissed: number[]; status: string }> =>
  request<{ dismissed: number[]; status: string }>('/dedup/clusters/dismiss', {
    method: 'POST',
    json: { candidate_ids: candidateIds },
  });

/* Merge an explicit operator-checked SUBSET of a cluster by property id; the
 * unchecked rest stays in the proposal queue (re-pointed server-side). */
export const mergeDedupPropertySet = (
  propertyIds: number[],
): Promise<ClusterMergeResult> =>
  request<ClusterMergeResult>('/dedup/properties/merge', {
    method: 'POST',
    json: { property_ids: propertyIds },
  });

export const listDedupMerges = (
  params: { limit?: number; offset?: number } = {},
): Promise<MergesResponse> =>
  request<MergesResponse>('/dedup/merges', {
    query: params as Record<string, QueryValue>,
  });

export const unmergeMergeGroup = (
  mergeGroupId: string,
): Promise<UnmergeResult> =>
  request<UnmergeResult>(
    `/dedup/merges/${encodeURIComponent(mergeGroupId)}/unmerge`,
    { method: 'POST' },
  );

/* ----- price-stats datasets ---------------------------------------------- */

export interface PriceStatDatasetInput {
  slug: string;
  name: string;
  description?: string | null;
  category_main_cb?: number;
  building_condition?: string | null;
  building_type?: string | null;
  ownership?: string | null;
  usable_area_from?: number | null;
  usable_area_to?: number | null;
  distance?: number;
  start_ym?: string | null;
  end_ym?: string | null;
  obec_ids?: number[] | null;
  min_population?: number | null;
  max_population?: number | null;
}

export const createPriceStatDataset = (
  input: PriceStatDatasetInput,
): Promise<import('./priceStats').PriceStatDataset> =>
  apiPost('/price-stats/datasets', input);

export const deletePriceStatDataset = (
  id: number,
): Promise<{ id: number; is_active: boolean }> =>
  request(`/price-stats/datasets/${id}`, { method: 'DELETE' });

export const updatePriceStatDataset = (
  id: number,
  patch: Partial<PriceStatDatasetInput> & { is_active?: boolean },
): Promise<import('./priceStats').PriceStatDataset> =>
  request(`/price-stats/datasets/${id}`, { method: 'PATCH', json: patch });

export const runPriceStatDataset = (
  id: number,
): Promise<{ dispatched: boolean; run_url?: string; detail?: string }> =>
  apiPost(`/price-stats/datasets/${id}/run`, {});

/* ----- broker outreach CRM (Phase 4) ------------------------------------- *
 *
 * Human-in-the-loop: the operator creates a campaign, the LLM drafts a
 * message per targeted broker, the operator reviews/edits/approves and sends
 * MANUALLY (mailto/copy) then marks it sent. No automated email send in v1.
 * All endpoints are bearer-gated (PII). */

export interface OutreachTargetSpec {
  region_ids?: number[];
  okres_ids?: number[];
  obec_ids?: number[];
  category_main?: string | null;
  category_type?: string | null;
  metric?: string;
}

export interface OutreachCampaign {
  id: number;
  name: string;
  goal: string | null;
  guidance: string | null;
  status: 'draft' | 'active' | 'archived';
  target: OutreachTargetSpec;
  created_at: string | null;
  updated_at: string | null;
  message_count?: number;
  sent_count?: number;
  approved_count?: number;
  draft_count?: number;
  message_stats?: Record<string, number>;
}

export type OutreachMessageStatus =
  | 'draft' | 'approved' | 'sent' | 'skipped' | 'replied' | 'bounced';

export interface OutreachMessage {
  id: number;
  campaign_id: number;
  broker_id: number;
  broker_name: string | null;
  firm_name: string | null;
  channel: string;
  to_email: string | null;
  to_phone: string | null;
  subject: string | null;
  body: string | null;
  status: OutreachMessageStatus;
  model: string | null;
  cost_usd: number | null;
  generated_at: string | null;
  approved_at: string | null;
  sent_at: string | null;
  sent_via: string | null;
  notes: string | null;
}

export interface OutreachTarget {
  broker_id: number;
  display_name: string | null;
  primary_email: string | null;
  primary_phone: string | null;
  firm_name: string | null;
  firm_domain: string | null;
  active_property_count: number;
  property_count: number;
}

export interface OutreachSuppression {
  broker_id: number;
  broker_name: string | null;
  reason: string | null;
  suppressed_at: string | null;
}

export const listOutreachCampaigns = (): Promise<{ campaigns: OutreachCampaign[] }> =>
  request<{ campaigns: OutreachCampaign[] }>('/outreach/campaigns');

export const getOutreachCampaign = (id: number): Promise<OutreachCampaign> =>
  request<OutreachCampaign>(`/outreach/campaigns/${id}`);

export const createOutreachCampaign = (input: {
  name: string;
  goal?: string | null;
  guidance?: string | null;
  target?: OutreachTargetSpec | null;
}): Promise<OutreachCampaign> =>
  request<OutreachCampaign>('/outreach/campaigns', { method: 'POST', json: input });

export const updateOutreachCampaign = (
  id: number,
  patch: {
    name?: string;
    goal?: string | null;
    guidance?: string | null;
    status?: string;
    target?: OutreachTargetSpec;
  },
): Promise<OutreachCampaign> =>
  request<OutreachCampaign>(`/outreach/campaigns/${id}`, { method: 'PATCH', json: patch });

export const previewOutreachTargets = (
  id: number,
  limit = 50,
): Promise<{ targets: OutreachTarget[]; count: number }> =>
  request<{ targets: OutreachTarget[]; count: number }>(
    `/outreach/campaigns/${id}/targets`,
    { query: { limit } },
  );

export const generateOutreachDrafts = (
  id: number,
  limit = 25,
): Promise<{ generated: number; targets: number }> =>
  request<{ generated: number; targets: number }>(
    `/outreach/campaigns/${id}/generate`,
    { method: 'POST', query: { limit } },
  );

export const listOutreachMessages = (
  id: number,
  status?: string,
): Promise<{ messages: OutreachMessage[] }> =>
  request<{ messages: OutreachMessage[] }>(
    `/outreach/campaigns/${id}/messages`,
    { query: status ? { status } : undefined },
  );

export const updateOutreachMessage = (
  messageId: number,
  patch: { status?: string; subject?: string; body?: string; notes?: string },
): Promise<OutreachMessage> =>
  request<OutreachMessage>(`/outreach/messages/${messageId}`, {
    method: 'PATCH',
    json: patch,
  });

export const regenerateOutreachMessage = (
  messageId: number,
): Promise<OutreachMessage> =>
  request<OutreachMessage>(`/outreach/messages/${messageId}/regenerate`, {
    method: 'POST',
  });

export const listOutreachSuppressions = (): Promise<{ suppressions: OutreachSuppression[] }> =>
  request<{ suppressions: OutreachSuppression[] }>('/outreach/suppressions');

export const addOutreachSuppression = (
  broker_id: number,
  reason?: string,
): Promise<OutreachSuppression> =>
  request<OutreachSuppression>('/outreach/suppressions', {
    method: 'POST',
    json: { broker_id, reason },
  });

export const removeOutreachSuppression = (
  broker_id: number,
): Promise<{ removed: number }> =>
  request<{ removed: number }>(`/outreach/suppressions/${broker_id}`, {
    method: 'DELETE',
  });

/* ----- broker merge review (Phase 5) ------------------------------------- *
 *
 * The auto-merge engine leaves corporate/role-inbox accounts apart (no personal
 * bridge). This queue surfaces "same name + same firm" groups for one-click
 * reversible operator merge. All bearer-gated. */

export interface BrokerMergeBroker {
  broker_id: number;
  display_name: string | null;
  firm_name: string | null;
  firm_domain: string | null;
  primary_email: string | null;
  primary_phone: string | null;
  source_count: number;
  distinct_source_count: number;
  active_property_count: number;
  property_count: number;
}

export interface BrokerMergeCandidate {
  id: number;
  group_key: string;
  broker_ids: number[];
  reason: string;
  evidence: { name?: string; firm_name?: string | null; firm_domain?: string | null; broker_count?: number };
  status: string;
  created_at: string | null;
  brokers: BrokerMergeBroker[];
}

export interface BrokerMergeRecord {
  merge_group_id: string;
  survivor_broker_id: number;
  survivor_name: string | null;
  retired_broker_ids: number[];
  reason: string | null;
  source: string | null;
  merged_at: string | null;
}

export const listBrokerMergeCandidates = (
  limit = 100,
): Promise<{ candidates: BrokerMergeCandidate[]; count: number }> =>
  request<{ candidates: BrokerMergeCandidate[]; count: number }>(
    '/broker-review/candidates',
    { query: { limit } },
  );

export const mergeBrokerCandidate = (
  candidateId: number,
  brokerIds?: number[],
): Promise<{ merge_group_id: string; survivor_broker_id: number; retired_broker_ids: number[] }> =>
  request('/broker-review/candidates/' + candidateId + '/merge', {
    method: 'POST',
    json: { broker_ids: brokerIds ?? null },
  });

export const dismissBrokerCandidate = (
  candidateId: number,
): Promise<{ id: number; status: string }> =>
  request('/broker-review/candidates/' + candidateId + '/dismiss', { method: 'POST' });

export const listBrokerMerges = (
  limit = 50,
): Promise<{ merges: BrokerMergeRecord[] }> =>
  request<{ merges: BrokerMergeRecord[] }>('/broker-review/merges', { query: { limit } });

export const unmergeBrokers = (
  mergeGroupId: string,
): Promise<{ merge_group_id: string; survivor_broker_id: number; restored_broker_ids: number[] }> =>
  request('/broker-review/merges/' + encodeURIComponent(mergeGroupId) + '/unmerge', {
    method: 'POST',
  });
