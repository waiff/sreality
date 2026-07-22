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
  CollectionWithProperties,
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
  PipelineStage,
  SkillRefinement,
  SourceKind,
  Tag,
  TagColor,
  NotificationSourceKind,
  NotificationUnreadCount,
  WatchdogDispatch,
  WatchdogDispatchesResponse,
  WatchdogFilterSpec,
  WatchdogSeenFilter,
  WatchdogSubscription,
  FilterPreset,
  DedupCandidatesResponse,
  DedupSummaryResponse,
  MergesResponse,
  MergedPropertiesResponse,
  DecisionFeedback,
  AuditRung,
} from './types';
import type { DistrictChip, PresetSpec } from './filters';
import { districtChipsToCsvParams } from './filters';

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
  renovation_czk?: number | null;
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

// The dedup-engine knob registry (one typed source of truth, backend-defined).
export type DedupSetting = {
  key: string;
  kind: 'bool' | 'float' | 'model';
  default: unknown;
  label: string;
  group: string;
  help: string;
  min: number | null;
  max: number | null;
  value: unknown;
  is_default: boolean;
};

export const getDedupSettings = (): Promise<{ data: DedupSetting[] }> =>
  request<{ data: DedupSetting[] }>('/admin/dedup-settings');

export const updateDedupSetting = (
  key: string,
  value: unknown,
): Promise<{ key: string; value: unknown; is_default: boolean }> =>
  request<{ key: string; value: unknown; is_default: boolean }>(
    `/admin/dedup-settings/${encodeURIComponent(key)}`,
    { method: 'PUT', json: { value } },
  );

export type DedupTagPriority = {
  family: string;
  order: string[];
  default_order: string[];
  is_default: boolean;
};

export const getDedupTagPriorities = (): Promise<{ data: DedupTagPriority[] }> =>
  request<{ data: DedupTagPriority[] }>('/admin/dedup-tag-priorities');

export const updateDedupTagPriority = (
  family: string,
  order: string[],
): Promise<DedupTagPriority> =>
  request<DedupTagPriority>(
    `/admin/dedup-tag-priorities/${encodeURIComponent(family)}`,
    { method: 'PUT', json: { order } },
  );

// The unified Decision history feed: every terminal dedup decision (merged /
// dismissed), engine AND operator, with the undo handle + factor detail.
export type DedupAuditRow = {
  audit_id: number;
  run_at: string;
  left_sreality_id: number | null;
  right_sreality_id: number | null;
  left_property_id: number | null;
  right_property_id: number | null;
  category_main: string | null;
  stage: string;
  outcome: 'merged' | 'dismissed' | string;
  source: 'engine' | 'operator' | string | null;
  merge_group_id: string | null;
  detail: Record<string, unknown> | null;
  undone: boolean;
  feedback: DecisionFeedback | null;
  audit_breakdown: AuditRung[];
};

export const getDedupAudit = (
  params: {
    outcome?: string;
    category_main?: string;
    source?: string;
    stage?: string;
    factor?: string; // phash | cosine | visual | address | floor_plan
    factor_min?: number;
    factor_max?: number;
    verdict?: string; // High | Medium | Low
    room_type?: string; // the compared room/plan tag (detail.room_type)
    property_id?: number; // scope to one property's merge decisions
    property_id_in?: ReadonlyArray<number>; // batched form of property_id
    flagged?: boolean; // only decisions the operator flagged as incorrect
    // Matches a decision if EITHER side of its pair touches the picked place —
    // the same `DistrictChip[]` widget Browse/Watchdog use (LocationTypeahead),
    // serialised via the shared `districtChipsToCsvParams` wire format.
    districts?: ReadonlyArray<DistrictChip> | null;
    limit?: number;
    offset?: number;
  } = {},
): Promise<{ data: DedupAuditRow[]; total: number; returned: number }> => {
  const q = new URLSearchParams();
  if (params.outcome) q.set('outcome', params.outcome);
  if (params.category_main) q.set('category_main', params.category_main);
  if (params.source) q.set('source', params.source);
  if (params.stage) q.set('stage', params.stage);
  if (params.factor) q.set('factor', params.factor);
  if (params.factor_min != null) q.set('factor_min', String(params.factor_min));
  if (params.factor_max != null) q.set('factor_max', String(params.factor_max));
  if (params.verdict) q.set('verdict', params.verdict);
  if (params.room_type) q.set('room_type', params.room_type);
  if (params.property_id != null) q.set('property_id', String(params.property_id));
  if (params.property_id_in?.length) {
    q.set('property_id_in', params.property_id_in.join(','));
  }
  if (params.flagged) q.set('flagged', 'true');
  for (const [k, v] of Object.entries(districtChipsToCsvParams(params.districts ?? []))) {
    q.set(k, v);
  }
  q.set('limit', String(params.limit ?? 100));
  if (params.offset) q.set('offset', String(params.offset));
  return request<{ data: DedupAuditRow[]; total: number; returned: number }>(
    `/dedup/audit?${q.toString()}`,
  );
};

// Flag a dedup decision / candidate PAIR as incorrect (with direction + note).
// PROPERTY-pair-keyed, so the same flag shows on both the history feed and the queue and
// never orphans on a repr-listing recompute.
export type DecisionFeedbackInput = {
  left_property_id: number;
  right_property_id: number;
  is_incorrect?: boolean;
  expected_outcome?: 'should_merge' | 'should_dismiss' | 'unsure' | null;
  note?: string | null;
  category_main?: string | null;
};
export const setDecisionFeedback = (
  body: DecisionFeedbackInput,
): Promise<{ data: Record<string, unknown> }> =>
  request<{ data: Record<string, unknown> }>('/dedup/feedback', {
    method: 'POST',
    json: body,
  });
export const deleteDecisionFeedback = (
  left_property_id: number,
  right_property_id: number,
): Promise<{ data: { deleted: boolean } }> =>
  request<{ data: { deleted: boolean } }>('/dedup/feedback', {
    method: 'DELETE',
    query: { a: left_property_id, b: right_property_id },
  });

// The SPECIFIC pictures behind a decision, resolved at read time: the pHash matched
// PAIRS (with Hamming), the compared plans, or the deciding room.
export type DedupEvidenceImage = {
  image_id?: number;
  sreality_url: string | null;
  storage_path: string | null;
};
export type DedupEvidenceSide = {
  sreality_id: number;
  images: DedupEvidenceImage[];
  fallback: boolean;
};
export type DedupEvidencePair = {
  hamming: number;
  left: DedupEvidenceImage;
  right: DedupEvidenceImage;
};
export type DedupDecisionEvidence = {
  pairs: DedupEvidencePair[] | null;
  room_type: string | null;
  left: DedupEvidenceSide;
  right: DedupEvidenceSide;
};
export const getDedupDecisionEvidence = (params: {
  a: number;
  b: number;
  stage?: string | null;
  reason?: string | null;
  room_type?: string | null;
  category_main?: string | null;
  per_side?: number;
}): Promise<{ data: DedupDecisionEvidence }> => {
  const q = new URLSearchParams();
  q.set('a', String(params.a));
  q.set('b', String(params.b));
  if (params.stage) q.set('stage', params.stage);
  if (params.reason) q.set('reason', params.reason);
  if (params.room_type) q.set('room_type', params.room_type);
  if (params.category_main) q.set('category_main', params.category_main);
  if (params.per_side) q.set('per_side', String(params.per_side));
  return request<{ data: DedupDecisionEvidence }>(
    `/dedup/decision-evidence?${q.toString()}`,
  );
};

// /clip-audit: flag one image's CLIP tag and/or render score as wrong, with a note.
export type ImageAnnotation = {
  image_id: number;
  tag_flagged: boolean;
  render_flagged: boolean;
  note: string | null;
  updated_at: string;
};
export const setImageAnnotation = (body: {
  image_id: number;
  tag_flagged?: boolean;
  render_flagged?: boolean;
  note?: string | null;
}): Promise<{ data: ImageAnnotation }> =>
  request<{ data: ImageAnnotation }>('/dedup/image-annotation', {
    method: 'POST',
    json: body,
  });
export const deleteImageAnnotation = (
  image_id: number,
): Promise<{ data: { deleted: boolean } }> =>
  request<{ data: { deleted: boolean } }>('/dedup/image-annotation', {
    method: 'DELETE',
    query: { image_id },
  });

// /phash-audit: a note on one image pair.
export type PhashNote = {
  image_id_a: number;
  image_id_b: number;
  note: string | null;
  updated_at: string;
};
export const setPhashNote = (body: {
  image_id_a: number;
  image_id_b: number;
  note?: string | null;
}): Promise<{ data: PhashNote }> =>
  request<{ data: PhashNote }>('/dedup/phash-note', { method: 'POST', json: body });
export const deletePhashNote = (
  image_id_a: number,
  image_id_b: number,
): Promise<{ data: { deleted: boolean } }> =>
  request<{ data: { deleted: boolean } }>('/dedup/phash-note', {
    method: 'DELETE',
    query: { a: image_id_a, b: image_id_b },
  });

// /phash-audit: matching-photo image pairs within a Hamming-distance range, from pairs
// the engine already decided (dedup_pair_audit) — read-only evidence, no engine change.
export type PhashAuditImageRef = {
  image_id: number;
  sreality_url: string | null;
  storage_path: string | null;
  room_type: string | null;
  fine_tag: string | null;
  confidence: number | null;
  render_score: number | null;
};
export type PhashAuditRow = {
  audit_id: number;
  left_sreality_id: number | null;
  right_sreality_id: number | null;
  left_property_id: number | null;
  right_property_id: number | null;
  outcome: string;
  category_main: string | null;
  run_at: string;
  // What ACTUALLY decided this pair — may be a different signal than the Hamming
  // number this page sorts by (e.g. phash found nothing, forensic vision dismissed
  // it). Same shape/source as DedupAuditRow.audit_breakdown, so DedupBreakdown
  // renders both identically.
  stage: string;
  audit_breakdown: AuditRung[];
  left_image: PhashAuditImageRef;
  right_image: PhashAuditImageRef;
  hamming: number;
};
export const getPhashAudit = (
  params: {
    hamming_min?: number;
    hamming_max?: number;
    category_main?: string;
    outcome?: string;
    // Both images in a returned pair must share the SAME tag, which must be one of
    // these — not "either side is one of these" (see phash_audit's docstring).
    room_types?: ReadonlyArray<string>;
    // Only pairs where at least one of the two shown images already has a
    // linear-probe training-set label.
    training_only?: boolean;
    // Narrows training_only to one SPECIFIC label (implies training_only).
    training_label?: string;
    // Inverse of training_only — pairs where NEITHER shown image is in the
    // training set yet. Takes priority if both are set.
    training_exclude?: boolean;
    limit?: number;
    // Opaque cursor — pass back the previous response's next_scan_offset.
    scan_offset?: number;
  } = {},
): Promise<{
  data: PhashAuditRow[];
  returned: number;
  scanned_pairs: number;
  scan_cap: number;
  scanned_so_far: number;
  // Pagination is over the scan SCOPE, not the joined result (see the backend
  // docstring) — a short `data` with next_scan_offset != null means "nothing more in
  // this window, but the ceiling/population isn't exhausted yet, keep scrolling";
  // null means truly done.
  next_scan_offset: number | null;
}> => {
  const q = new URLSearchParams();
  if (params.hamming_min != null) q.set('hamming_min', String(params.hamming_min));
  if (params.hamming_max != null) q.set('hamming_max', String(params.hamming_max));
  if (params.category_main) q.set('category_main', params.category_main);
  if (params.outcome) q.set('outcome', params.outcome);
  if (params.room_types?.length) q.set('room_types', params.room_types.join(','));
  if (params.training_only) q.set('training_only', 'true');
  if (params.training_label) q.set('training_label', params.training_label);
  if (params.training_exclude) q.set('training_exclude', 'true');
  q.set('limit', String(params.limit ?? 100));
  if (params.scan_offset) q.set('scan_offset', String(params.scan_offset));
  return request(`/dedup/phash-audit?${q.toString()}`);
};

// /phash-audit "Train": one image's linear-probe training-set label (migration 309).
// Data-collection only — nothing reads this table yet.
export type TrainingExample = {
  image_id: number;
  label: string;
  updated_at: string;
};
export const setTrainingExample = (body: {
  image_id: number;
  label: string;
}): Promise<{ data: TrainingExample }> =>
  request<{ data: TrainingExample }>('/dedup/training-example', {
    method: 'POST',
    json: body,
  });
export const deleteTrainingExample = (
  image_id: number,
): Promise<{ data: { deleted: boolean } }> =>
  request<{ data: { deleted: boolean } }>('/dedup/training-example', {
    method: 'DELETE',
    query: { image_id },
  });

// /clip-audit summary-chip trash: remove EVERY training example under one label.
// Only the training assignments go — the images stay. A custom label disappears
// with its rows; a taxonomy label just drops to zero coverage.
export const deleteTrainingLabel = (
  label: string,
): Promise<{ data: { deleted: number; label: string } }> =>
  request<{ data: { deleted: number; label: string } }>(
    '/dedup/training-examples/by-label',
    { method: 'DELETE', query: { label } },
  );

// /clip-audit batch relabel: move a whole checked selection under one label in a
// single statement (server-side dedupe + a 500-per-batch cap). Same upsert
// semantics as setTrainingExample — an image not yet in the set gets added.
export const bulkSetTrainingExamples = (body: {
  image_ids: number[];
  label: string;
}): Promise<{ data: { updated: number; label: string; image_ids: number[] } }> =>
  request<{ data: { updated: number; label: string; image_ids: number[] } }>(
    '/dedup/training-examples/bulk',
    { method: 'POST', json: body },
  );

// "Border case" flag (migration 310): even a human isn't confident about this
// image's classification. Independent of image_training_examples — no label
// required, may coexist with one (a best-guess flagged as uncertain).
export type BorderCase = {
  image_id: number;
  created_at: string;
};
export const setBorderCase = (
  image_id: number,
): Promise<{ data: BorderCase }> =>
  request<{ data: BorderCase }>('/dedup/border-case', {
    method: 'POST',
    json: { image_id },
  });
export const deleteBorderCase = (
  image_id: number,
): Promise<{ data: { deleted: boolean } }> =>
  request<{ data: { deleted: boolean } }>('/dedup/border-case', {
    method: 'DELETE',
    query: { image_id },
  });

// CLIP backfill progress (listing-grain), for the /dedup tracker.
export type DedupCoverageTier = {
  key: string;
  label: string;
  tagged: number;
  total: number;
};
export type DedupClipCoverage = {
  total_tags: number;
  total_embeddings: number;
  priority_region_id: number;
  grain: string;
  tiers: DedupCoverageTier[];
};
export const getDedupClipCoverage = (): Promise<{ data: DedupClipCoverage }> =>
  request<{ data: DedupClipCoverage }>('/dedup/clip-coverage');

// Top-of-page dedup funnel: per-stage count + last-24h movement.
export type DedupPipelineOverview = {
  tagging: { total: number; delta_24h: number; embeddings: number };
  eligible: { total: number; flagged_location: number; flagged_disposition: number };
  candidates: { total: number; delta_24h: number };
  decisions: { total: number; delta_24h: number; merged: number; dismissed: number };
  last_run: {
    started_at: string;
    /* which lane wrote the row ('full' | 'candidates' | 'dirty' | 'geo'; null pre-262) */
    run_kind: string | null;
    auto_merged: number;
    auto_dismissed: number;
    queued: number;
    clip_classified: number;
    routed_haiku: number;
    routed_sonnet: number;
    vision_calls: number;
  } | null;
};
export const getDedupPipelineOverview = (): Promise<{ data: DedupPipelineOverview }> =>
  request<{ data: DedupPipelineOverview }>('/dedup/pipeline-overview');

// Dedup-funnel throughput per bucket (hour | day), for the overview's timeline chart.
export type DedupTimelinePoint = {
  bucket: string; // ISO timestamp of the bucket start
  tagged: number;
  candidates: number;
  merged: number;
  dismissed: number;
};
export const getDedupPipelineTimeline = (
  bucket: 'hour' | 'day' = 'day',
): Promise<{ grain: string; data: DedupTimelinePoint[] }> =>
  request<{ grain: string; data: DedupTimelinePoint[] }>(
    `/dedup/pipeline-timeline?bucket=${bucket}`,
  );

export const archiveResetDedupCandidates = (): Promise<{
  archived: number;
  deleted: number;
  batch: string;
}> =>
  request<{ archived: number; deleted: number; batch: string }>(
    '/dedup/candidates/archive-reset',
    { method: 'POST' },
  );

/* POST /dedup/model-compare — convene every connected vision model on undecided pairs
 * (decision support). candidate_ids omitted = the oldest-undecided top-`limit`; a list =
 * exactly those proposed candidates (the per-card button). Verdicts land on /model-testing
 * under the returned run_label. */
export interface ModelCompareResponse {
  dispatched: boolean;
  run_label: string;
  pair_count: number;
  models: string[];
  model_testing_url: string;
  run_url: string;
}

export const requestModelCompare = (
  body: { candidate_ids?: number[]; limit?: number },
): Promise<ModelCompareResponse> =>
  request<ModelCompareResponse>('/dedup/model-compare', { method: 'POST', json: body });

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

/* Per-kraj CLIP-tagging drain priority. GET every kraj with its priority flag +
 * active-listing volume; PUT replaces the full priority list
 * (app_settings.clip_tagging_priority_region_ids). The scheduled clip_tag runs read
 * the same key — a priority kraj is drained (tags + embeddings) before the global sweep. */
export interface ClipTaggingRegion {
  id: number;
  name: string;
  priority: boolean;
  active_listings: number;
}
export interface ClipTaggingRegionsPayload {
  regions: ClipTaggingRegion[];
  parked_no_geo: number;
  priority_region_ids: number[];
}
export const getClipTaggingRegions = (): Promise<{
  data: ClipTaggingRegionsPayload;
}> =>
  request<{ data: ClipTaggingRegionsPayload }>('/admin/clip-tagging/regions');

export const updateClipTaggingRegions = (
  priorityRegionIds: number[],
): Promise<{ data: ClipTaggingRegionsPayload }> =>
  request<{ data: ClipTaggingRegionsPayload }>(
    '/admin/clip-tagging/regions',
    { method: 'PUT', json: { priority_region_ids: priorityRegionIds } },
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
 * Collections, tags, and notes — all PROPERTY-grain (a property groups one
 * real-world listing across portals). Reads of `which tags / which
 * collections does property X belong to` go through the *_public Supabase
 * views (see lib/queries.ts) — there is no per-property GET on the API for
 * those. Notes are read via the API only. Everything else (list-by-domain,
 * create, update, delete, attach, detach) goes through the bearer-gated
 * FastAPI endpoints wrapped below.
 */

/* Collections */

export const listCollections = (): Promise<{ data: Collection[]; total: number }> =>
  request<{ data: Collection[]; total: number }>('/collections');

export const getCollection = (id: number): Promise<CollectionWithProperties> =>
  request<CollectionWithProperties>(`/collections/${id}`);

export const createCollection = (input: {
  name: string;
  description?: string | null;
  monitoring_enabled?: boolean;
  notify_channels?: string[];
}): Promise<Collection> =>
  request<Collection>('/collections', { method: 'POST', json: input });

export const updateCollection = (
  id: number,
  input: {
    name?: string | null;
    description?: string | null;
    monitoring_enabled?: boolean;
    notify_channels?: string[];
  },
): Promise<Collection> =>
  request<Collection>(`/collections/${id}`, { method: 'PATCH', json: input });

export const deleteCollection = (id: number): Promise<{ deleted: true }> =>
  request<{ deleted: true }>(`/collections/${id}`, { method: 'DELETE' });

export const addPropertiesToCollection = (
  id: number,
  property_ids: number[],
): Promise<{ added: number; skipped: number }> =>
  request<{ added: number; skipped: number }>(`/collections/${id}/properties`, {
    method: 'POST',
    json: { property_ids },
  });

export const removePropertyFromCollection = (
  id: number,
  property_id: number,
): Promise<{ removed: boolean }> =>
  request<{ removed: boolean }>(
    `/collections/${id}/properties/${property_id}`,
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
  property_id: number,
  tag_id: number,
): Promise<{ attached: boolean }> =>
  request<{ attached: boolean }>(`/properties/${property_id}/tags`, {
    method: 'POST',
    json: { tag_id },
  });

export const detachTag = (
  property_id: number,
  tag_id: number,
): Promise<{ detached: boolean }> =>
  request<{ detached: boolean }>(
    `/properties/${property_id}/tags/${tag_id}`,
    { method: 'DELETE' },
  );

/* Notes (per-property journal) */

export const listPropertyNotes = (
  property_id: number,
): Promise<{ data: Note[] }> =>
  request<{ data: Note[] }>(`/properties/${property_id}/notes`);

export const createPropertyNote = (
  property_id: number,
  body: string,
  origin_listing_id?: number,
): Promise<Note> =>
  request<Note>(`/properties/${property_id}/notes`, {
    method: 'POST',
    json:
      origin_listing_id != null
        ? { body, origin_listing_id }
        : { body },
  });

/* Deal pipeline (migration 205) — bookmark a property into the pipeline
 * (entry stage) / remove it. Membership is read via property_pipeline_public. */

export const addPipelineCard = (
  property_id: number,
): Promise<{ property_id: number; stage_key: string; added: boolean }> =>
  request<{ property_id: number; stage_key: string; added: boolean }>(
    '/pipeline/cards',
    { method: 'POST', json: { property_id } },
  );

export const removePipelineCard = (
  property_id: number,
): Promise<{ removed: boolean }> =>
  request<{ removed: boolean }>(`/pipeline/cards/${property_id}`, {
    method: 'DELETE',
  });

export const movePipelineCard = (
  property_id: number,
  stage_id: number,
  board_position?: number,
): Promise<{ property_id: number; stage_id: number; stage_key: string }> =>
  request<{ property_id: number; stage_id: number; stage_key: string }>(
    `/pipeline/cards/${property_id}`,
    {
      method: 'PATCH',
      json: board_position != null ? { stage_id, board_position } : { stage_id },
    },
  );

/* Stage management — operator-curated kanban columns (rename / recolor / add /
 * reorder / archive). The `key` slug is derived server-side from the label. */

export const createPipelineStage = (input: {
  label: string;
  color?: TagColor | null;
  is_terminal?: boolean;
}): Promise<PipelineStage> =>
  request<PipelineStage>('/pipeline/stages', { method: 'POST', json: input });

export const updatePipelineStage = (
  stage_id: number,
  patch: {
    label?: string;
    color?: TagColor | null;
    is_terminal?: boolean;
    is_entry?: boolean;
  },
): Promise<PipelineStage> =>
  request<PipelineStage>(`/pipeline/stages/${stage_id}`, {
    method: 'PATCH',
    json: patch,
  });

export const reorderPipelineStages = (
  ordered_ids: number[],
): Promise<{ data: PipelineStage[] }> =>
  request<{ data: PipelineStage[] }>('/pipeline/stages/reorder', {
    method: 'POST',
    json: { ordered_ids },
  });

export const archivePipelineStage = (
  stage_id: number,
): Promise<{ archived: boolean; stage_id: number }> =>
  request<{ archived: boolean; stage_id: number }>(
    `/pipeline/stages/${stage_id}`,
    { method: 'DELETE' },
  );

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
  /* Scope to one producer. The Watchdog page passes 'watchdog' so the unified
   * feed's collection_monitor rows (subscription_id NULL) don't leak onto it. */
  source_kind?: NotificationSourceKind | 'all';
  seen?: WatchdogSeenFilter;
  limit?: number;
  offset?: number;
  /* Keyset cursor (the prior page's next_cursor). */
  cursor?: string;
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
  channels?: string[];
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
    channels?: string[];
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

/* ----- Unified notifications feed (Sprint C) ---------------------------- */

export interface ListNotificationsParams {
  source_kind?: NotificationSourceKind | 'all';
  change_kind?: string;
  collection_id?: number;
  seen?: WatchdogSeenFilter;
  limit?: number;
  cursor?: string;
}

/* The unified feed: watchdog matches AND collection-monitor change events.
 * Same endpoint + row shape as the watchdog dispatches, just unscoped by
 * source (the LEFT-join feed serves both). */
export const listNotifications = (
  params: ListNotificationsParams = {},
): Promise<WatchdogDispatchesResponse> =>
  request<WatchdogDispatchesResponse>('/notifications/dispatches', {
    query: params as Record<string, QueryValue>,
  });

export const getNotificationUnreadCount = (
  source_kind: NotificationSourceKind | 'all' = 'all',
): Promise<NotificationUnreadCount> =>
  request<NotificationUnreadCount>('/notifications/unread-count', {
    query: { source_kind },
  });

export const markAllNotificationsSeen = (
  source_kind: NotificationSourceKind | 'all' = 'all',
): Promise<{ updated: number }> =>
  request<{ updated: number }>('/notifications/mark-all-seen', {
    method: 'POST',
    query: { source_kind },
  });

/* ----- Saved Browse filter presets (migration 151) ---------------------- */

export const listFilterPresets = (): Promise<{
  data: FilterPreset[];
  total: number;
}> =>
  request<{ data: FilterPreset[]; total: number }>('/filter-presets');

export const createFilterPreset = (input: {
  name: string;
  filter_spec: PresetSpec;
  color?: TagColor | null;
}): Promise<FilterPreset> =>
  request<FilterPreset>('/filter-presets', { method: 'POST', json: input });

export const updateFilterPreset = (
  id: string,
  patch: { name?: string; filter_spec?: PresetSpec; color?: TagColor | null },
): Promise<FilterPreset> =>
  request<FilterPreset>(`/filter-presets/${encodeURIComponent(id)}`, {
    method: 'PUT',
    json: patch,
  });

export const deleteFilterPreset = (id: string): Promise<{ deleted: true }> =>
  request<{ deleted: true }>(`/filter-presets/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });

/* Persist a new display order (full ordered id-list, 0 = first). Returns the
 * canonical list so the caller can adopt the server's view. */
export const reorderFilterPresets = (
  ids: string[],
): Promise<{ data: FilterPreset[]; total: number }> =>
  request<{ data: FilterPreset[]; total: number }>('/filter-presets/reorder', {
    method: 'PUT',
    json: { ids },
  });

/* ----- Cross-source dedup review (multi-portal PR3b) --------------------- */

export interface ListDedupCandidatesParams {
  status?: string;
  tier?: string;
  reason?: string;
  verdict?: string;
  // Matches a pair if EITHER candidate property is that type — the same
  // property-type tabs Decision history uses (a pair can legitimately span
  // two types, e.g. the sanctioned dům↔komerční cross-type merge).
  category_main?: string;
  // Matches a pair if EITHER candidate property touches the picked place —
  // lets the operator prioritise the manual review backlog by location.
  districts?: ReadonlyArray<DistrictChip> | null;
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
): Promise<DedupCandidatesResponse> => {
  const { districts, ...rest } = params;
  return request<DedupCandidatesResponse>('/dedup/candidates', {
    query: {
      ...(rest as Record<string, QueryValue>),
      ...districtChipsToCsvParams(districts ?? []),
    },
  });
};

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

/* Asset links (migration 224): group properties that are the same physical
 * building across category cohorts WITHOUT collapsing them — the cross-category
 * sameness a merge correctly refuses. Both rows + both category facets survive. */
export interface AssetLinkResult {
  data: {
    asset_id: number;
    member_property_ids: number[];
    newly_linked_property_ids: number[];
    dissolved_asset_ids: number[];
  };
}

export const linkAssetProperties = (
  propertyIds: number[],
  note?: string,
): Promise<AssetLinkResult> =>
  request<AssetLinkResult>('/dedup/assets/link', {
    method: 'POST',
    json: { property_ids: propertyIds, note: note ?? null },
  });

export const unlinkAssetProperty = (
  propertyId: number,
): Promise<{ data: { asset_id: number; asset_dissolved: boolean } }> =>
  request<{ data: { asset_id: number; asset_dissolved: boolean } }>(
    '/dedup/assets/unlink',
    { method: 'POST', json: { property_id: propertyId } },
  );

export interface BulkMergeResult {
  data: {
    merged: number;
    skipped: number;
    merge_group_ids: string[];
  };
}

/* Scoped bulk-approve: merge many proposed candidates as INDEPENDENT reversible
 * pairs (per-pair tolerant). The /dedup surface sends the loaded STRONG ids. */
export const bulkMergeDedupCandidates = (
  candidateIds: number[],
): Promise<BulkMergeResult> =>
  request<BulkMergeResult>('/dedup/candidates/bulk-merge', {
    method: 'POST',
    json: { candidate_ids: candidateIds },
  });

export const listDedupMerges = (
  params: { limit?: number; offset?: number } = {},
): Promise<MergesResponse> =>
  request<MergesResponse>('/dedup/merges', {
    query: params as Record<string, QueryValue>,
  });

/* Browse the RESULTS of dedup: already-merged properties whose child-listing
 * count (`source_count`) is in [min_listings, max_listings], biggest groups
 * first. `max_listings`/`category_main` omitted => no upper bound / any type
 * (null query params are dropped by `request`). Admin-gated. */
export const listMergedProperties = (
  params: {
    min_listings?: number;
    max_listings?: number | null;
    category_main?: string | null;
    limit?: number;
    offset?: number;
  } = {},
): Promise<MergedPropertiesResponse> =>
  request<MergedPropertiesResponse>('/dedup/merged-properties', {
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

/* ----- billing: tiers + agenda visibility (admin) ------------------------- */

export type Plan = {
  key: string;
  name: string;
  position: number;
  agendas: Record<string, boolean>;
  is_default: boolean;
  updated_at: string | null;
};

export type EntitlementRow = {
  account_id: string;
  email: string | null;
  plan: string;
  status: string;
  current_period_end: string | null;
  is_explicit: boolean;
};

export const adminListPlans = (): Promise<{ data: Plan[] }> =>
  request('/admin/plans');

export const adminCreatePlan = (body: {
  key: string;
  name: string;
  position?: number;
  agendas?: Record<string, boolean>;
}): Promise<Plan> => request('/admin/plans', { method: 'POST', json: body });

export const adminUpdatePlan = (
  key: string,
  body: Partial<Pick<Plan, 'name' | 'position' | 'agendas' | 'is_default'>>,
): Promise<Plan> =>
  request(`/admin/plans/${encodeURIComponent(key)}`, { method: 'PATCH', json: body });

export const adminDeletePlan = (key: string): Promise<{ deleted: boolean }> =>
  request(`/admin/plans/${encodeURIComponent(key)}`, { method: 'DELETE' });

export const adminListEntitlements = (): Promise<{ data: EntitlementRow[] }> =>
  request('/admin/entitlements');

export const adminSetEntitlement = (
  accountId: string,
  body: { plan: string; status?: string },
): Promise<EntitlementRow> =>
  request(`/admin/entitlements/${encodeURIComponent(accountId)}`, {
    method: 'PUT',
    json: body,
  });

/* ----- location audit ---------------------------------------------------- */
/* /location-audit — read-only per-listing inventory of every address / geo /
 * coordinate field, with the acquisition method for the two fields whose
 * provenance varies per row (coordinate + street). Backed by
 * api/routes/location_audit.py, admin-gated. See lib/locationAudit.ts for the
 * field glossary + method labels the page renders. */
export type LocationAuditRow = {
  sreality_id: number;
  source: string;
  source_id_native: string | null;
  source_url: string | null;
  category_main: string | null;
  category_type: string | null;
  category_sub_cb: number | null;
  is_active: boolean;
  last_seen_at: string | null;
  inactive_at: string | null;
  lat: number | null;
  lon: number | null;
  street: string | null;
  house_number: string | null;
  zip: string | null;
  street_id: number | null;
  street_name_key: string | null;
  street_source: string | null;
  // Dedup-eligibility inputs (for the per-row "why unreachable" breakdown).
  disposition: string | null;
  area_m2: number | null;
  estate_area: number | null;
  usable_area: number | null;
  locality: string | null;
  district: string | null;
  obec: string | null;
  okres: string | null;
  region: string | null;
  obec_id: number | null;
  okres_id: number | null;
  region_id: number | null;
  locality_district_id: number | null;
  locality_region_id: number | null;
  locality_municipality_id: number | null;
  locality_quarter_id: number | null;
  locality_ward_id: number | null;
  geo_cell_key: string | null;
  geocode_attempted_at: string | null;
  coord_street_attempt_version: number | null;
  coords_source: string | null;
  inaccuracy_type: string | null;
  accurate: boolean | null;
  geom_method: string | null;
  street_method: string | null;
  // Dedup reachability: dedup_reachable = the engine can reach this listing via SOME
  // pass; the three arm booleans show which (street+disposition / geo+area / byt-geo).
  dedup_reachable: boolean;
  elig_street: boolean;
  elig_geo: boolean;
  elig_byt_geo: boolean;
};

export type LocationAuditPage = {
  data: LocationAuditRow[];
  // null on non-first pages: the count is computed once (offset 0) and read from the
  // first page only — re-counting on every infinite-scroll fetch is wasteful (~1s each).
  total: number | null;
  returned: number;
  limit: number;
  offset: number;
};

export const getLocationAudit = (
  params: {
    source?: string;
    category_main?: string;
    active?: 'active' | 'inactive';
    dedup?: 'reachable' | 'unreachable';
    // Narrow to ONE dedup pass's domain, optionally split into that pass's own
    // eligible / ineligible halves — how an eligibility-matrix cell drills down.
    path?: DedupPathKey;
    path_state?: 'eligible' | 'ineligible';
    has?: ReadonlyArray<string>;
    missing?: ReadonlyArray<string>;
    limit?: number;
    offset?: number;
  } = {},
): Promise<LocationAuditPage> => {
  const q = new URLSearchParams();
  if (params.source) q.set('source', params.source);
  if (params.category_main) q.set('category_main', params.category_main);
  if (params.active) q.set('active', params.active);
  if (params.dedup) q.set('dedup', params.dedup);
  if (params.path) q.set('path', params.path);
  if (params.path_state) q.set('path_state', params.path_state);
  if (params.has?.length) q.set('has', params.has.join(','));
  if (params.missing?.length) q.set('missing', params.missing.join(','));
  q.set('limit', String(params.limit ?? 50));
  q.set('offset', String(params.offset ?? 0));
  return request(`/location-audit?${q.toString()}`);
};

export type DedupPathKey = 'street' | 'geo' | 'byt_geo';

/* One bucket of listings sharing an eligibility signature. The API returns the joint
 * distribution rather than a pre-pivoted table, so every pass × scope × breakdown the
 * matrix offers is a client-side pivot of ONE 0.8s scan. `elig_*` are the ENGINE's own
 * predicates (nullable: a NULL category_main makes the two category-gated arms NULL —
 * treat non-true as ineligible, never `!elig`). */
export type EligibilityBucket = {
  source: string;
  category_main: string | null;
  is_active: boolean;
  has_street: boolean;
  has_disposition: boolean;
  has_geom: boolean;
  has_obec: boolean;
  has_area: boolean;
  elig_street: boolean | null;
  elig_geo: boolean | null;
  elig_byt_geo: boolean | null;
  n: number;
};

export type EligibilityMatrix = {
  buckets: EligibilityBucket[];
  /* Each pass's domain + active gate, DERIVED server-side from the same constants the
   * SQL renders — so the client pivot can't drift from the predicate it pivots. */
  paths: Array<{
    key: DedupPathKey;
    domain_categories: string[] | null;
    active_only: boolean;
  }>;
  total: number;
};

export const getEligibilityMatrix = (): Promise<EligibilityMatrix> =>
  request('/location-audit/eligibility-matrix');

export type LocationAuditRaw = {
  sreality_id: number;
  source: string;
  source_id_native: string | null;
  source_url: string | null;
  category_main: string | null;
  category_type: string | null;
  last_seen_at: string | null;
  raw_json: unknown;
};

export const getLocationAuditRaw = (
  srealityId: number,
): Promise<LocationAuditRaw> =>
  // sreality_id is a QUERY param, not a path segment: non-sreality PKs are
  // negative and the int path convertor would 404 on the leading minus.
  request('/location-audit/raw', { query: { sreality_id: srealityId } });
