/* Fetch wrapper for the Railway FastAPI service.
 *
 * Two auth shapes, matching the backend gate each route actually uses:
 *  - `jwt: true` (require_admin / verify_jwt routes — Settings, labeling,
 *    property merge mechanics, Outreach, broker-review, skill-refinements,
 *    Collections list, Pipeline, Watchdog subscriptions, /estimations,
 *    and every `/brokers/*` read since 2026-08-12) sends
 *    the caller's real Supabase session access_token. The backend no longer
 *    accepts anything else here (api/dependencies.py:verify_jwt) — admin
 *    status rides in the JWT's app_metadata.is_admin claim, never a shared
 *    secret.
 *  - default (require_token routes) sends VITE_API_TOKEN, a static secret
 *    inlined into the JS bundle at build time and therefore extractable by
 *    anyone with browser devtools. That's fine for this gate: it only proves
 *    "loaded the SPA past its password gate", never an identity or admin
 *    claim. Server-side enforcement is api/dependencies.py:require_token.
 *    See frontend/README.md.
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
  MergesResponse,
  MergedPropertiesResponse,
} from './types';
import type { PresetSpec } from './filters';
import type { WaterfallRow } from './locationWaterfall';
import { supabase } from './supabase';

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

export type QueryScalar = string | number | boolean;
/* An array value is serialized as REPEATED params (ids=1&ids=2) — the shape a
 * FastAPI `list[int] = Query(default=[])` route parses. Comma-joining would
 * arrive server-side as one unparseable value, so callers must not hand-roll it. */
export type QueryValue = QueryScalar | readonly QueryScalar[] | undefined | null;

interface RequestOptions extends Omit<RequestInit, 'body'> {
  query?: Record<string, QueryValue>;
  json?: unknown;
  /* True for require_admin / verify_jwt-gated routes — see the file-header
   * comment. Sends the caller's real Supabase JWT instead of VITE_API_TOKEN. */
  jwt?: boolean;
}

/* Resolves to the caller's real Supabase JWT for `jwt: true` requests, falling
 * back to the static token when logged out (shouldn't happen behind
 * RequireAuth/RequireAdmin in normal operation, but a request made during
 * that brief window must not silently claim a capability it doesn't have). */
async function authHeader(useJwt: boolean | undefined): Promise<Record<string, string>> {
  if (useJwt) {
    const { data } = await supabase.auth.getSession();
    const accessToken = data.session?.access_token;
    if (accessToken) return { Authorization: `Bearer ${accessToken}` };
  }
  return TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {};
}

/* FastAPI answers a 422 with `detail` as a LIST of {loc, msg, type} objects.
 * String()-ing that array renders "[object Object]" in the error banner, which
 * tells the operator nothing about which parameter was rejected. */
function detailText(detail: unknown): string {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      if (typeof item === 'string') return item;
      if (item && typeof item === 'object') {
        const rec = item as { loc?: unknown; msg?: unknown };
        const loc = Array.isArray(rec.loc) ? rec.loc.join('.') : null;
        const msg = typeof rec.msg === 'string' ? rec.msg : null;
        if (msg) return loc ? `${loc}: ${msg}` : msg;
      }
      return JSON.stringify(item);
    });
    return parts.join('; ');
  }
  return String(detail);
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  if (!BASE_URL) {
    throw new ApiError(
      'API base URL is not configured',
      0,
      { detail: 'VITE_API_BASE_URL is empty' },
    );
  }

  const { query, json, headers, jwt, ...rest } = opts;
  const url = new URL(BASE_URL + path);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      /* Drop nullish AND empty-string values. An empty string is not a filter —
       * sending `?listing_ids=` once meant "no filter" server-side and paged the
       * whole table. No surface may emit a meaningless parameter. */
      if (v == null || v === '') continue;
      if (Array.isArray(v)) {
        for (const item of v as readonly QueryScalar[]) {
          if (item != null) url.searchParams.append(k, String(item));
        }
      } else {
        url.searchParams.set(k, String(v));
      }
    }
  }

  const finalHeaders: Record<string, string> = {
    Accept: 'application/json',
    ...(json !== undefined ? { 'Content-Type': 'application/json' } : {}),
    ...(await authHeader(jwt)),
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
    const raw =
      body && typeof body === 'object' && body !== null && 'detail' in body
        ? (body as { detail: unknown }).detail
        : null;
    const detail =
      raw == null ? res.statusText || `HTTP ${res.status}` : detailText(raw);
    throw new ApiError(detail, res.status, body);
  }

  return body as T;
}

/* Generic verbs used by lib/maps.ts (and any other future module that
 * needs raw GET/POST without going through a feature-specific wrapper).
 * `jwt` defaults to false — pass true for a require_admin/verify_jwt route
 * (see the file-header comment). */
export const apiGet = <T>(
  path: string,
  params?: Record<string, QueryValue>,
  signal?: AbortSignal,
  jwt?: boolean,
): Promise<T> => request<T>(path, { query: params, signal, jwt });

export const apiPost = <T>(
  path: string,
  body: unknown,
  signal?: AbortSignal,
  jwt?: boolean,
): Promise<T> => request<T>(path, { method: 'POST', json: body, signal, jwt });

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
  request<EstimationRun>('/estimations', { method: 'POST', json: input, jwt: true });

export const getEstimation = (id: number): Promise<EstimationRun> =>
  request<EstimationRun>(`/estimations/${id}`, { jwt: true });

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
    jwt: true,
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
    jwt: true,
  });

export const listEstimations = (
  params: EstimationListParams = {},
): Promise<EstimationListResponse> =>
  request<EstimationListResponse>('/estimations', {
    query: params as Record<string, QueryValue>,
    /* Account-scoped read (deps.account_scope): the static token is not an
     * identity, so it would narrow the operator to SYSTEM-owned runs only. */
    jwt: true,
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
        { query: { sreality_ids: ids.join(',') }, signal, jwt: true },
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
  /* The unit those boxes are in — pass `BrowseStats.ppm2_basis` straight
   * through, spelled in migration 425's own vocabulary. The annotator states it
   * in the prompt instead of guessing, and REFUSES a `'mixed'` cohort outright
   * (no LLM call, an explicit note): a box plot stacking monthly rents on
   * purchase prices has no describable shape. Omitting it makes the annotator
   * treat the unit as unknown, so it can no longer name Kč/m² even when the
   * cohort is a plain sale cohort. It is also part of the server's per-day
   * cache identity beside region_hash, so two bases never share one entry. */
  ppm2_basis?: string | null;
}

export interface RegionDispositionAnnotationsResult {
  data: {
    region_key: string;
    annotations: Record<string, string>;
    model: string;
    cost_usd: number | null;
    cache_hit: boolean;
    /* Echo of the basis, and its Czech unit — null for a cohort with no
     * single one, which is exactly when nothing may name a unit. */
    ppm2_basis?: string | null;
    ppm2_unit?: string | null;
  };
  /* `notes` says WHY `annotations` is empty when the server declined to write
   * any (today: a mixed cohort). Rendering it is the difference between an
   * explained refusal and a blank panel. */
  metadata: Record<string, unknown> & { notes?: string[] };
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
    const raw =
      body && typeof body === 'object' && body !== null && 'detail' in body
        ? (body as { detail: unknown }).detail
        : null;
    const detail =
      raw == null ? res.statusText || `HTTP ${res.status}` : detailText(raw);
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
    jwt: true,
  });

export const getSkill = (name: string): Promise<Skill> =>
  request<Skill>(`/admin/skills/${encodeURIComponent(name)}`, { jwt: true });

export const updateSkill = (
  name: string,
  patch: SkillUpdate,
): Promise<Skill> =>
  request<Skill>(`/admin/skills/${encodeURIComponent(name)}`, {
    method: 'PUT',
    json: patch,
    jwt: true,
  });

export const listAppSettings = (): Promise<{ data: AppSetting[] }> =>
  request<{ data: AppSetting[] }>('/admin/app_settings', { jwt: true });

export const updateAppSetting = (
  key: string,
  value: unknown,
): Promise<AppSetting> =>
  request<AppSetting>(`/admin/app_settings/${encodeURIComponent(key)}`, {
    method: 'PUT',
    json: { value },
    jwt: true,
  });

export interface NewDedupSetting {
  key: string;
  category: string;
  value_type: 'integer' | 'numeric' | 'boolean' | 'text';
  value: unknown;
  default: unknown;
  is_override: boolean;
  decided: boolean;
  explanation: string;
  enum_choices: string[] | null;
  minimum: number | null;
  maximum: number | null;
}

export const listNewDedupSettings = (): Promise<{ data: NewDedupSetting[] }> =>
  request<{ data: NewDedupSetting[] }>('/new-dedup/settings', { jwt: true });

export const updateNewDedupSetting = (
  key: string,
  value: unknown,
): Promise<NewDedupSetting> =>
  request<NewDedupSetting>(`/new-dedup/settings/${encodeURIComponent(key)}`, {
    method: 'PUT',
    json: { value },
    jwt: true,
  });

export const resetNewDedupSetting = (key: string): Promise<NewDedupSetting> =>
  request<NewDedupSetting>(`/new-dedup/settings/${encodeURIComponent(key)}`, {
    method: 'DELETE',
    jwt: true,
  });

/* NEW DEDUP Level 0 · candidate audit (api/routes/new_dedup_candidates.py;
 * docs/design/new-dedup/PROGRAM.md Wave 2, ledger 2026-09-10 (a)/(b)).
 *
 * A GENERATION is one run of the candidate lane under one PARAMETER SET (its
 * `fingerprint`). Its audit numbers are computed once, at the end of that run,
 * onto the generation row — so the page reads `stats` and never scans pairs.
 * Every one of those numbers is produced by
 * scripts/dedup_candidates_generate.py:generation_stats; the shapes below
 * mirror that function field for field, which is why several are optional:
 * a run that failed before the statistics step carries `stats: null`, and the
 * page must render the gap rather than a zero. */

export interface NewDedupCandidateRung {
  code: string;
  label: string;
  needs: string[];
  explanation: string;
}

export interface NewDedupCandidatePath {
  code: string;
  label: string;
  /* false for the paths the program has not built (A, and B until Wave 3).
   * They still arrive, with no rungs — an empty column is the honest answer. */
  built: boolean;
  block_key: string | null;
  explanation: string;
  rungs: NewDedupCandidateRung[];
}

export interface NewDedupCandidateGeneration {
  id: number;
  simulation_run_id: number;
  inputs_id: number;
  path: string;
  fingerprint: string;
  status: string;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  inputs: Record<string, unknown>;
  progress: Record<string, unknown> | null;
  error_message: string | null;
}

/* One (rung × the pair's two property types × deal) cell of the pair table.
 * `floor_checked` counts the pairs in that cell where the apartment floor rule
 * could actually be applied (both sides byt with a known floor). */
export interface NewDedupCandidateMatrixRow {
  rung: string;
  category_main_lo: string | null;
  category_main_hi: string | null;
  category_type: string | null;
  pairs: number;
  floor_checked: number;
}

/* The listing-side funnel, one row per (portal, property type, deal) — the SHARED
 * step vocabulary since W16 (location_data/location_steps.py). `with_verdict` was
 * `with_projection` and `located_town` was `with_town`; `located`,
 * `located_foreign` and `located_no_town` are new, and they are what turned one
 * misleading "lost 99,889" into three honest readings. A run generated before W16
 * carries the old keys and no `waterfall`, so every one of these is optional and
 * the page renders a gap rather than a zero. */
export interface NewDedupCandidateFunnelRow {
  source: string;
  category_main: string | null;
  category_type: string | null;
  listings: number;
  active: number;
  with_verdict?: number;
  located?: number;
  located_town?: number;
  located_foreign?: number;
  located_no_town?: number;
  with_disposition: number;
  with_area: number;
  byt: number;
  byt_with_floor: number;
  c1_eligible: number;
  c3_eligible: number;
  town_no_attribute: number;
}

/* `listings` is only carried by the lane's estimate mode; a generation's own
 * top-town rows have the two rung counts and the name. Optional, so the page
 * renders an em dash instead of inventing a number. */
export interface NewDedupCandidateTownRow {
  block_key: string;
  obec_name?: string | null;
  C1: number;
  C3: number;
  listings?: number | null;
}

export interface NewDedupCandidateBucketRow {
  obec_kod: string;
  obec_name: string | null;
  disposition: string | null;
  listings: number;
  active: number;
}

/* One band of the "how many towns produced this many pairs" histogram.
 * `pairs_to: null` is the open-ended top band. */
export interface NewDedupCandidateDistributionRow {
  pairs_from: number;
  pairs_to: number | null;
  towns: number;
}

export interface NewDedupCandidateTypeCount {
  category_main: string | null;
  listings: number;
}

export interface NewDedupCandidateStats {
  matrix: NewDedupCandidateMatrixRow[];
  pairs: { C1: number; C3: number; total: number };
  listings_with_candidates: NewDedupCandidateTypeCount[];
  towns_with_pairs: number;
  top_towns: NewDedupCandidateTownRow[];
  distribution: NewDedupCandidateDistributionRow[];
  funnel: NewDedupCandidateFunnelRow[];
  /* W16 — THE CHAIN, stamped by the lane with the SHARED step keys, the same
   * shape `location_audit_waterfall` writes hourly for the audit page. One row
   * type for both readouts; the browser sums and subtracts nothing. Optional
   * because a run generated before W16 has none. */
  waterfall?: WaterfallRow[];
  /* When the lane COUNTED those rows — the funnel re-reads `listings` live at the
   * end of a run, so this, and not the generation row's completed_at, is what the
   * page's as-of line can honestly claim. */
  computed_at?: string;
  top_buckets: NewDedupCandidateBucketRow[];
  /* Stamped onto the stats after the statistics step, so a run that predates a
   * field (or failed early) simply has none. */
  partial?: boolean;
  only?: string[];
  stale_deleted?: number;
  chunks_done?: number;
  pairs_upserted?: number;
  seconds?: number;
  scope?: string;
}

/* The run picker's rows — three facts read straight off `stats`, NULL while a
 * run has none yet. Column order is the backend's `_RECENT_COLUMNS` contract. */
export interface NewDedupCandidateRecentGeneration {
  id: number;
  status: string;
  created_at: string | null;
  completed_at: string | null;
  fingerprint: string;
  scope: string | null;
  partial: boolean | null;
  pairs_total: number | null;
}

export interface NewDedupCandidateOverview {
  /* false until migration 492 is applied — the page says "not created yet"
   * instead of failing. */
  store_ready: boolean;
  paths: NewDedupCandidatePath[];
  generation: NewDedupCandidateGeneration | null;
  stats: NewDedupCandidateStats | null;
  recent: NewDedupCandidateRecentGeneration[];
}

/* The whole audit page in one call. Without `generationId`, the newest
 * SUCCESSFUL path C run; with one, that run (any status) so two parameter sets
 * can be compared. */
export const getNewDedupCandidateOverview = (
  generationId?: number | null,
): Promise<{ data: NewDedupCandidateOverview }> =>
  request<{ data: NewDedupCandidateOverview }>('/new-dedup/candidates/overview', {
    query: { generation_id: generationId ?? null },
    jwt: true,
  });

/* THE DRILL-DOWN behind a figure on the Candidates page. `bucket` is a KEY the
 * backend looks up (`toolkit.dedup_candidates_sql.AUDIT_BUCKETS`) — never a
 * predicate — so an unknown one is a 400 rather than a query.
 *
 * LIVE, unlike every other number on that page. The funnel's counts were frozen
 * when the run executed; this reads the database now, so the two are not
 * expected to tally and the API returns no total to invite the comparison. The
 * page says which is which. */
export interface NewDedupCandidateAuditRow {
  listing_id: number;
  property_id: number | null;
  sreality_id: number | null;
  source: string;
  source_id_native: string | null;
  source_url: string | null;
  category_main: string | null;
  category_type: string | null;
  disposition: string | null;
  area_m2: number | null;
  floor: number | null;
  price_czk: number | null;
  is_active: boolean;
  first_seen_at: string | null;
  last_seen_at: string | null;
  display_label: string | null;
  obec_kod: string | null;
  granularity: string | null;
  country_status: string | null;
}

export interface NewDedupCandidateAuditPage {
  store_ready: boolean;
  data: NewDedupCandidateAuditRow[];
  has_more: boolean;
  next_after_id: number | null;
}

export const getNewDedupCandidateListings = (q: {
  bucket: string;
  source?: string | null;
  category_main?: string | null;
  category_type?: string | null;
  active_only?: boolean;
  after_id?: number | null;
  limit?: number;
}): Promise<NewDedupCandidateAuditPage> =>
  request<NewDedupCandidateAuditPage>('/new-dedup/candidates/listings', {
    query: {
      bucket: q.bucket,
      source: q.source ?? null,
      category_main: q.category_main ?? null,
      category_type: q.category_type ?? null,
      active_only: q.active_only ? true : null,
      after_id: q.after_id ?? null,
      limit: q.limit ?? null,
    },
    jwt: true,
  });

export const listNewDedupCandidateGenerations = (): Promise<{
  data: NewDedupCandidateRecentGeneration[];
}> =>
  request<{ data: NewDedupCandidateRecentGeneration[] }>(
    '/new-dedup/candidates/generations',
    { jwt: true },
  );

// Tag annotation matrix (docs/design/tag-annotation-matrix.md) — the
// operator-curated tag taxonomy, the relabel sample, and the tri-state
// (positive/negative/excluded) ground truth every per-tag classifier head
// trains from. `image_tag_labels` is the single source of truth; a proposal
// (below) is just a machine SUGGESTION pending review, never written to
// image_clip_tags (gallery-flip hazard).
export type TagState = 'positive' | 'negative' | 'excluded';
export const TAG_STATES: readonly TagState[] = ['positive', 'negative', 'excluded'];

/* Only meaningful on an excluded cell. Same effect on training, opposite
 * diagnostic meaning: 'ambiguous' means nobody could decide (and a tag with too
 * many of them has a DEFINITION problem); 'pruned' means the operator
 * deliberately removed the image from the training set. Pruned exclusions are
 * excluded from the ambiguity rate's numerator AND denominator, so pruning can
 * never dilute the signal. */
export type TagExcludedReason = 'ambiguous' | 'pruned';
export const TAG_EXCLUDED_REASONS: readonly TagExcludedReason[] = ['ambiguous', 'pruned'];

/* Who or what decided a cell. 'human_confirmed' (machine proposed, human
 * affirmed) is deliberately NOT the same evidence as 'machine' (nobody checked).
 * 'backfill_442' is migration 442's manufactured one-hot fiction — 72,058 rows
 * awaiting a separate gated deletion; never treat one as a decision. */
export type TagSource = 'human' | 'human_confirmed' | 'machine' | 'backfill_442';

export interface NewDedupTag {
  id: number;
  label: string;
  family: string | null;
  active: boolean;
  /* Needs attention now — pins this tag to the top of the "Modify labels"
   * popup and marks it there. */
  priority: boolean;
  /* Operator's own call that this tag's set is solid enough for the (not
   * yet built) per-tag trainer — independent of Gate 1, which only says a
   * tag is LABELED enough, not reviewed. */
  ready_for_training: boolean;
  /* Property types this head serves (byt, dum, komercni, pozemek, ostatni).
   * Null = NOT a head: the training-set page, the heads read and the labeler
   * all key on this. Set from the Taxonomy page. */
  routing_categories?: string[] | null;
  created_at: string;
  /* Positive annotations for this tag — the inventory number (what a tag
   * REMOVE deletes). It is `gate_count + border_case_count`. */
  positive_count: number;
  /* What GATE 1 counts: positive annotations NOT flagged as border cases. An
   * image nobody could classify is not evidence a tag is learnable, so it
   * doesn't move the tag toward its target — clearing the flag makes it
   * count again, with no relabelling. Every coverage surface (bar, ≤N
   * ceiling, picker counts) reads THIS, never positive_count. */
  gate_count: number;
  /* The parked remainder, excluded from gate_count. */
  border_case_count: number;
  negative_count: number;
  excluded_count: number;
  pending_count: number;
  dismissed_count: number;
  /* This tag's own review queue (migration 450) — images CLIP retrieval drew
   * for somebody to LOOK at, and how many of them are still undecided. Never a
   * label: being a candidate says nothing about whether the tag applies, and an
   * image nobody has reviewed is never trained as a negative. */
  candidate_count: number;
  candidate_open_count: number;
  last_drawn_at: string | null;
  /* Provenance inventory. These sum to the tag's full row count and are the
   * honest breakdown of the positive/negative/excluded counts above, which
   * deliberately still include backfill rows. */
  human_count: number;
  machine_count: number;
  backfill_count: number;
  /* Every ambiguous exclusion on the tag (an excluded cell with no reason at
   * all counts here — a deliberate prune always names itself). */
  ambiguous_count: number;
  /* The ambiguity rate's NUMERATOR — the human-decided slice of the above.
   * Render this against decided_count; ambiguous_count is a different
   * population and pairing the two would state a fraction nobody computed. */
  ambiguous_decided_count: number;
  pruned_count: number;
  /* The ambiguity rate's denominator: positive + negative + ambiguous
   * exclusions, counting only what a human decided — the rate measures human
   * indecision, so unverified machine rows and backfill rows are both out. */
  decided_count: number;
  /* null (never 0) when decided_count is 0 — no decisions means unknown, not
   * healthy. */
  ambiguity_rate: number | null;
  /* Computed SERVER-side against ambiguity_threshold and
   * ambiguity_min_decisions, so the threshold has one definition. Never
   * recompute it in the SPA. */
  ambiguity_alert: boolean;
}
export interface NewDedupLabelingOverview {
  /* Distinct images queued as a candidate for at least ONE tag. It REPLACES
   * sample_size rather than renaming it: sample_size counted the single pool
   * every tag shared, and nothing in the candidate world means that — queues
   * are per tag, so this is a different quantity over a different denominator.
   * Keeping the old name with a new meaning is how a number drifts. */
  candidate_image_count: number;
  /* 0.15 today. Echoed so the SPA renders "above 15 percent" without a second
   * hardcoded copy of the number. */
  ambiguity_threshold: number;
  ambiguity_min_decisions: number;
  tags: NewDedupTag[];
}
export const getNewDedupLabelingOverview = (): Promise<{ data: NewDedupLabelingOverview }> =>
  request<{ data: NewDedupLabelingOverview }>('/new-dedup/labeling/overview', { jwt: true });

export const addNewDedupTag = (
  label: string,
  family?: string | null,
): Promise<{ data: NewDedupTag }> =>
  request<{ data: NewDedupTag }>('/new-dedup/labeling/taxonomy', {
    method: 'POST',
    json: { label, family: family ?? null },
    jwt: true,
  });

/* The rename/add/flags routes return tag_annotations._tag_dict — identity and
 * flags only, never the derived counts. Typed honestly so a cache patch can
 * only ever merge onto a cached row, never replace it with undefined counts.
 * (addNewDedupTag and setNewDedupTagFlags carry the same inaccuracy; both
 * callers invalidate instead of patching, so they are left as they are.) */
export type NewDedupTagIdentity = Pick<
  NewDedupTag,
  'id' | 'label' | 'family' | 'active' | 'priority' | 'ready_for_training' | 'created_at'
>;

export const renameNewDedupTag = (
  tagId: number,
  label: string,
): Promise<{ data: NewDedupTagIdentity }> =>
  request<{ data: NewDedupTagIdentity }>(`/new-dedup/labeling/taxonomy/${tagId}`, {
    method: 'PUT',
    json: { label },
    jwt: true,
  });

export const removeNewDedupTag = (
  tagId: number,
): Promise<{ data: { label: string; deleted_annotations: number } }> =>
  request<{ data: { label: string; deleted_annotations: number } }>(
    `/new-dedup/labeling/taxonomy/${tagId}`,
    { method: 'DELETE', jwt: true },
  );

/* Sets one or both operator flags — only the fields actually passed, so
 * toggling one from the Modify labels popup never clobbers the other. */
export const setNewDedupTagFlags = (
  tagId: number,
  flags: { priority?: boolean; ready_for_training?: boolean; review_state?: ReviewState },
): Promise<{ data: NewDedupTag }> =>
  request<{ data: NewDedupTag }>(`/new-dedup/labeling/taxonomy/${tagId}/flags`, {
    method: 'PATCH',
    json: flags,
    jwt: true,
  });

export const ROUTING_CATEGORIES = ['byt', 'dum', 'komercni', 'pozemek', 'ostatni'] as const;

/* Make a tag a head (non-empty) or stop it being one (empty). */
export const setNewDedupTagRouting = (
  tagId: number, categories: string[],
): Promise<{ data: NewDedupTag }> =>
  request<{ data: NewDedupTag }>(`/new-dedup/labeling/taxonomy/${tagId}/routing`, {
    method: 'PATCH', json: { categories }, jwt: true,
  });

export const growNewDedupSample = (
  count: number,
  categoryMain?: string | null,
): Promise<{ data: { added: number } }> =>
  request<{ data: { added: number } }>('/new-dedup/labeling/sample/grow', {
    method: 'POST',
    json: { count, category_main: categoryMain ?? null },
    jwt: true,
  });

export interface NewDedupLabelProposal {
  image_id: number;
  model: string;
  label: string;
  confidence: number | null;
  proposed_at: string;
  status: 'pending' | 'confirmed' | 'dismissed';
  reviewed_at: string | null;
  reviewed_by: string | null;
  /* This image's tri-state decision for the proposal's OWN label, or null
   * when untouched (defaults to negative for display/training). Not the
   * same as `label`: a pending row's label is the model's suggestion. */
  current_state: TagState | null;
  /* Why that cell is excluded, when it is — so the grid can render the reason
   * chip on an already-excluded tile instead of making the operator guess. */
  current_excluded_reason: TagExcludedReason | null;
}
/* `status` is 'all' | 'pending' | 'confirmed' | 'dismissed'. An unknown
 * value is a 422, not a silent unfiltered listing. */
export const listNewDedupProposals = (params: {
  status?: string;
  label?: string;
  /* The production CLIP tagger's own fine_tag — a different, fixed
   * vocabulary from `label` (Taxonomy v1). Only meaningful in the
   * "Original tag" view; mutually exclusive with `label` in practice. */
  original_tag?: string;
  limit?: number;
}): Promise<{ data: NewDedupLabelProposal[] }> =>
  request<{ data: NewDedupLabelProposal[] }>('/new-dedup/labeling/proposals', {
    query: params,
    jwt: true,
  });

/* The production CLIP tagger's fixed fine-tag vocabulary (data/clip_taxonomy.json's
 * prompt anchors) — the option list for the "Original tag" view's own tag filter. */
export const listNewDedupOriginalTags = (): Promise<{ data: string[] }> =>
  request<{ data: string[] }>('/new-dedup/labeling/original-tags', { jwt: true });

/* Echoes back what actually landed: `label` is the tag it was decided
 * against (the corrected one if any), `proposed_label` what the model
 * suggested, `corrected` true when the operator overrode it. */
export interface NewDedupProposalStateResult {
  image_id: number;
  model: string;
  label: string;
  state: TagState;
  status: 'confirmed' | 'dismissed';
  proposed_label: string;
  corrected: boolean;
  excluded_reason: TagExcludedReason | null;
}

/* `label` corrects a wrong suggestion before deciding — the decision lands on
 * that tag instead of the proposed one (the proposal row keeps the model's
 * own prediction either way). Omit to decide against the proposal as-is.
 * `excludedReason` is only meaningful with state='excluded'; the server 422s a
 * reason sent with any other state rather than silently dropping it. */
export const setNewDedupProposalState = (
  imageId: number,
  model: string,
  state: TagState,
  label?: string,
  excludedReason?: TagExcludedReason | null,
): Promise<{ data: NewDedupProposalStateResult }> =>
  request<{ data: NewDedupProposalStateResult }>('/new-dedup/labeling/proposals/state', {
    method: 'POST',
    json: {
      image_id: imageId,
      model,
      state,
      label: label ?? null,
      excluded_reason: excludedReason ?? null,
    },
    jwt: true,
  });

export const bulkSetNewDedupProposalState = (
  model: string,
  imageIds: number[],
  state: TagState,
  excludedReason?: TagExcludedReason | null,
): Promise<{
  data: {
    updated: number;
    model: string;
    state: TagState;
    excluded_reason: TagExcludedReason | null;
    image_ids: number[];
  };
}> =>
  request('/new-dedup/labeling/proposals/bulk-state', {
    method: 'POST',
    json: { model, image_ids: imageIds, state, excluded_reason: excludedReason ?? null },
    jwt: true,
  });

export interface NewDedupImageTag {
  id: number;
  label: string;
  family: string | null;
  state: TagState | 'untouched';
  updated_at: string | null;
  source: TagSource | null;
  excluded_reason: TagExcludedReason | null;
}
/* Image-centric view for the detail panel: every active tag with this
 * image's current state, grouped by family — the mirror of
 * listNewDedupTagImages, for the "one photo, several tags at once" case. */
export const listNewDedupImageTags = (
  imageId: number,
): Promise<{ data: NewDedupImageTag[] }> =>
  request<{ data: NewDedupImageTag[] }>(`/new-dedup/labeling/images/${imageId}/tags`, {
    jwt: true,
  });

/* Sets many tags on ONE image to the same state at once — the detail
 * panel's "set selected" action (the mirror of bulkSetNewDedupTagAnnotation,
 * which fixes the tag and varies the image). */
export const bulkSetNewDedupImageTags = (
  imageId: number,
  tagIds: number[],
  state: TagState,
  excludedReason?: TagExcludedReason | null,
): Promise<{
  data: {
    updated: number;
    image_id: number;
    state: TagState;
    excluded_reason: TagExcludedReason | null;
    tag_ids: number[];
  };
}> =>
  request(`/new-dedup/labeling/images/${imageId}/tags/bulk`, {
    method: 'POST',
    json: { tag_ids: tagIds, state, excluded_reason: excludedReason ?? null },
    jwt: true,
  });

export interface NewDedupPositiveTag {
  image_id: number;
  tag_id: number;
  label: string;
}
/* Every positive tag on each of several images, one call for a whole visible
 * grid — the "what's already assigned" line under each tile. A tile only
 * shows the one tag it's reviewing; with multi-label images that's not the
 * same as everything the image is already positive on. */
export const listNewDedupPositiveTagsForImages = (
  imageIds: number[],
): Promise<{ data: NewDedupPositiveTag[] }> =>
  request<{ data: NewDedupPositiveTag[] }>('/new-dedup/labeling/images/tags/batch', {
    method: 'POST',
    json: { image_ids: imageIds },
    jwt: true,
  });

/* Which band of a tag's ranked pool drew a candidate (migration 450).
 * centroid_head = the nearest neighbours to the tag's centroid; centroid_mid =
 * a random sample from just below the head, where the confusion clusters live;
 * random = an unranked sample of the whole pool. A pure top-N would produce a
 * prototypical training set that fails on odd cases, so the bands are mixed on
 * purpose — and sustained positives out of the random band mean the centroid is
 * missing a mode. */
export type TagCandidateDraw = 'centroid_head' | 'centroid_mid' | 'random';

/* One bucket of a tag's queue — `key` is a TagCandidateDraw in by_draw and a
 * listings.category_main value in by_category. Empty buckets are omitted.
 *
 * `positive` / `negative` are the bucket's YIELD, derived by joining the label
 * store; they are what makes the random band's honesty rail readable — an
 * unranked sample of the pool that keeps coming back positive means the centroid
 * is missing a mode. total - open - positive - negative is the excluded remainder. */
export interface NewDedupCandidateBucket {
  key: string;
  total: number;
  open: number;
  positive: number;
  negative: number;
}

export interface NewDedupCandidateSummary {
  tag_id: number;
  total: number;
  /* Candidates with no decision yet for this tag — the work left. */
  open: number;
  reviewed: number;
  last_drawn_at: string | null;
  /* Human-verified positives that carry a CLIP vector — the centroid's
   * population. Not the overview's positive_count, which includes backfill. */
  verified_positive_count: number;
  min_verified_positives: number;
  can_draw: boolean;
  model: string;
  /* Property types this tag serves (migration 457); [] = no scope, draw the full
   * mix. Server-resolved — a draw that covers three of five categories has to SAY
   * so, or a deliberately narrow draw reads as a thin corpus. */
  routing_categories: string[];
  by_draw: NewDedupCandidateBucket[];
  by_category: NewDedupCandidateBucket[];
}

export interface NewDedupCandidateDrawResult {
  tag_id: number;
  status: 'drawn' | 'insufficient_positives';
  requested: number;
  inserted: number;
  verified_positive_count: number;
  min_verified_positives: number;
  model: string;
  by_draw: Record<TagCandidateDraw, number>;
  by_category: Record<string, number>;
  dropped_near_dup: number;
  dropped_property_cap: number;
  categories: Array<{
    category_main: string;
    status: 'drawn' | 'timeout' | 'empty_pool' | 'skipped_budget';
    requested: number;
    pool_size: number;
    inserted: number;
    dropped_near_dup: number;
    dropped_property_cap: number;
    elapsed_ms: number;
  }>;
}

export const getNewDedupTagCandidates = (
  tagId: number,
): Promise<{ data: NewDedupCandidateSummary }> =>
  request<{ data: NewDedupCandidateSummary }>(
    `/new-dedup/labeling/tags/${tagId}/candidates`, { jwt: true },
  );

/* `categoryMain` null = the full category mix (the skew-correcting default);
 * a value scopes the whole draw to that one property type. */
export const drawNewDedupTagCandidates = (
  tagId: number,
  count: number,
  categoryMain?: string | null,
): Promise<{ data: NewDedupCandidateDrawResult }> =>
  request<{ data: NewDedupCandidateDrawResult }>(
    `/new-dedup/labeling/tags/${tagId}/candidates`,
    { method: 'POST', json: { count, category_main: categoryMain ?? null }, jwt: true },
  );

export interface NewDedupTagImage {
  image_id: number;
  storage_path: string | null;
  state: TagState | 'untouched';
  updated_at: string | null;
  created_by: string | null;
  source: TagSource | null;
  excluded_reason: TagExcludedReason | null;
  /* WHY this image is in front of you: which rank band drew it, which category
   * quota it was drawn under, and where it sat in that ranked pool. All three
   * are null for an image decided for this tag but never drawn as a candidate
   * — the legacy positives, which the browse still lists so their labels don't
   * read as vanished. */
  draw: TagCandidateDraw | null;
  category_main: string | null;
  pool_rank: number | null;
}
/* The operator's reasons for changing marks on a head — raw material for its
 * next definition revision. Absorbed notes carry the version that took them. */
export interface TagLabelNote {
  id: number;
  image_id: number;
  storage_path: string;
  from_state: TagState | null;
  to_state: TagState;
  note: string;
  created_at: string | null;
}

export const listTagLabelNotes = (
  tagId: number, params: { include_absorbed?: boolean; limit?: number } = {},
): Promise<{ data: TagLabelNote[] }> =>
  request<{ data: TagLabelNote[] }>(`/new-dedup/labeling/tags/${tagId}/notes`, {
    query: params, jwt: true,
  });

export const getOpenNoteCounts = (): Promise<{ data: Record<string, number> }> =>
  request<{ data: Record<string, number> }>('/new-dedup/labeling/notes/open-counts', {
    jwt: true,
  });

export const absorbTagLabelNotes = (
  tagId: number, body: { definition_id: number; note_ids: number[] },
): Promise<{ data: { tag_id: number; absorbed: number[]; requested: number } }> =>
  request<{ data: { tag_id: number; absorbed: number[]; requested: number } }>(
    `/new-dedup/labeling/tags/${tagId}/notes/absorb`,
    { method: 'POST', json: body, jwt: true },
  );

/* Reviewing the TRAINING SET: what the model built, per head, with the
 * operator's own labels beside it. A separate read from the tag-centric browse
 * below, which is built around the (now empty) candidate queue and can neither
 * page nor tell the two apart. The holdout is excluded server-side. */
export interface TrainingSetHead {
  id: number;
  label: string;
  /* The trays. `positive`/`negative` are what the head TRAINS on (admitted);
   * `reserve` is positives the operator has not admitted (migration 484). */
  positive: number;
  positive_reserve: number;
  negative: number;
  negative_reserve: number;
  excluded: number;
  /* The operator's marker of whether they have been through this head
   * (tag_taxonomy.review_state, migration 487). Three-valued because "set aside
   * on purpose" is a decision and must not look like "nobody has said". Since
   * the ruling of 2026-09-08 'ready' also SELECTS the heads a tagging bake-off
   * run trains, so this toggle decides scope — it is no longer only bookkeeping. */
  review_state: ReviewState;
}

export type ReviewState = 'not_ready' | 'ready' | 'skipped';

export interface TrainingSetRow {
  image_id: number;
  storage_path: string;
  state: TagState;
  source: TagSource;
  excluded_reason: TagExcludedReason | null;
  updated_at: string | null;
  definition_version: number | null;
  /* Written under wording that has since been replaced — not wrong, but it
   * describes a rule that has changed, which is the one thing a reviewer
   * cannot see in the photo. */
  definition_stale: boolean;
  /* The open (unabsorbed) note on this image for this head, so it can be read
   * and changed later. Null when there is none. */
  note_id: number | null;
  note: string | null;
  /* Does the head train on this label, or is it waiting in the reserve? */
  in_training: boolean;
}

/* Move labels into a head's training set, or back to the reserve. */
export const setTrainingMembership = (
  tagId: number, imageIds: number[], inTraining: boolean,
): Promise<{ data: { tag_id: number; in_training: boolean; moved: number[]; requested: number } }> =>
  request<{ data: { tag_id: number; in_training: boolean; moved: number[]; requested: number } }>(
    `/new-dedup/labeling/tags/${tagId}/training-membership`,
    { method: 'POST', json: { image_ids: imageIds, in_training: inTraining }, jwt: true },
  );

export const listTrainingSetHeads = (): Promise<{ data: TrainingSetHead[] }> =>
  request<{ data: TrainingSetHead[] }>('/new-dedup/labeling/training-set/heads', {
    jwt: true,
  });

/* Where one image sits in a head's trays — so a link can name a head and a photo
 * and the page can land on it. The server resolves it against the SAME order and
 * the same storage_path join the page uses, because a rank computed any other
 * way pages to a different photo. */
/* Admit N labels of one sign at random and return the rest of that sign to its
 * reserve. The drawn set IS what the head trains on (migration 486). */
export const drawTrainingSet = (
  tagId: number, opts: { state?: TagState; size?: number } = {},
): Promise<{ data: { tag_id: number; state: string; drawn: number } }> =>
  request(`/new-dedup/labeling/tags/${tagId}/draw`, {
    method: 'POST', json: opts, jwt: true,
  });

export const locateTrainingImage = (
  tagId: number, imageId: number,
): Promise<{ data: {
  tag_id: number; image_id: number;
  tray: 'positive' | 'positive_reserve' | 'negative' | 'negative_reserve' | 'excluded';
  state: 'positive' | 'negative' | 'excluded';
  in_training: boolean; rank: number;
} }> =>
  request('/new-dedup/labeling/training-set/locate', {
    query: { tag_id: tagId, image_id: imageId }, jwt: true,
  });

export const listTrainingSet = (params: {
  tag_id: number;
  state?: 'positive' | 'negative' | 'excluded';
  in_training?: boolean;
  limit?: number;
  offset?: number;
}): Promise<{
  data: {
    rows: TrainingSetRow[];
    counts: Omit<TrainingSetHead, 'id' | 'label'>;
    limit: number;
    offset: number;
  };
}> =>
  request<{
    data: {
      rows: TrainingSetRow[];
      counts: Omit<TrainingSetHead, 'id' | 'label'>;
      limit: number;
      offset: number;
    };
  }>('/new-dedup/labeling/training-set', { query: params, jwt: true });

/* Change or drop a note. Only an OPEN note: an absorbed one already shaped a
 * definition version, and the server answers 404 rather than rewriting it. */
export const editTagLabelNote = (
  noteId: number, note: string,
): Promise<{ data: TagLabelNote }> =>
  request<{ data: TagLabelNote }>(`/new-dedup/labeling/notes/${noteId}`, {
    method: 'PATCH', json: { note }, jwt: true,
  });

export const deleteTagLabelNote = (
  noteId: number,
): Promise<{ data: { id: number; image_id: number; tag_id: number } }> =>
  request<{ data: { id: number; image_id: number; tag_id: number } }>(
    `/new-dedup/labeling/notes/${noteId}`, { method: 'DELETE', jwt: true },
  );

/* Tag-centric browse: this tag's candidate queue (migration 450) plus every
 * image already decided for the tag, each with its state — reaches images the
 * model never proposed this tag for, and backs "kitchen = excluded" filtering.
 * state='untouched' is the undecided part of the queue, which is the work
 * left; it is NOT a set of negatives. */
export const listNewDedupTagImages = (
  tagId: number,
  params: { state?: TagState | 'untouched'; limit?: number } = {},
): Promise<{ data: NewDedupTagImage[] }> =>
  request<{ data: NewDedupTagImage[] }>(`/new-dedup/labeling/tags/${tagId}/images`, {
    query: params,
    jwt: true,
  });

export const setNewDedupTagAnnotation = (
  tagId: number,
  imageId: number,
  state: TagState,
  excludedReason?: TagExcludedReason | null,
  /* Why the mark changed, with what the tile showed before. Recorded beside
   * the write (migration 473) so the mark and its reason cannot drift apart;
   * it is the raw material for the head's next definition revision. */
  note?: { text: string; from_state: TagState | null },
): Promise<{
  data: {
    image_id: number;
    tag_id: number;
    state: TagState;
    source: TagSource;
    excluded_reason: TagExcludedReason | null;
    definition_id: number | null;
    verified_at: string | null;
    updated_at: string;
    /* false when a machine write was refused because a human had already
     * decided this cell. Always true for writes from this UI. */
    applied: boolean;
  };
}> =>
  request(`/new-dedup/labeling/tags/${tagId}/annotations`, {
    method: 'POST',
    json: { image_id: imageId, state, excluded_reason: excludedReason ?? null, ...(note ? { note: note.text, from_state: note.from_state } : {}) },
    jwt: true,
  });

export const bulkSetNewDedupTagAnnotation = (
  tagId: number,
  imageIds: number[],
  state: TagState,
  excludedReason?: TagExcludedReason | null,
): Promise<{
  data: {
    updated: number;
    tag_id: number;
    state: TagState;
    excluded_reason: TagExcludedReason | null;
    image_ids: number[];
  };
}> =>
  request(`/new-dedup/labeling/tags/${tagId}/annotations/bulk`, {
    method: 'POST',
    json: { image_ids: imageIds, state, excluded_reason: excludedReason ?? null },
    jwt: true,
  });

export const clearNewDedupTagAnnotation = (
  tagId: number,
  imageId: number,
): Promise<{ data: { image_id: number; tag_id: number; deleted: boolean } }> =>
  request<{ data: { image_id: number; tag_id: number; deleted: boolean } }>(
    `/new-dedup/labeling/tags/${tagId}/annotations/${imageId}`,
    { method: 'DELETE', jwt: true },
  );

/* Tag definitions (migration 446) — the versioned written meaning of a
 * tag_taxonomy row. Supersede, never overwrite: every save creates a new
 * version and retires the previous one, so `version` only ever goes up and old
 * versions are readable forever.
 *
 * Named without the NewDedup prefix on purpose: the taxonomy these define is
 * permanent and not dedup-scoped. The ROUTES still live under
 * /new-dedup/labeling — renaming that prefix is separately flagged debt. */
export interface TagDefinitionDoesNotCount {
  case: string;
  /* The tag this case belongs to instead, when there is one. Always an id —
   * never label text, so a rename can't rot a definition. */
  goes_to_tag_id: number | null;
}

export interface TagDefinitionConfusable {
  tag_id: number;
  /* The visual tell that separates the two ("mailboxes/intercom = shared"). */
  tell: string;
}

/* Every other tag this definition points at, resolved to a label server-side.
 * Ids that no longer exist are simply absent — the definition document is a
 * denormalized snapshot and is resolved leniently on read. */
export interface TagDefinitionReferencedTag {
  tag_id: number;
  label: string;
}

export interface TagDefinition {
  id: number;
  tag_id: number;
  version: number;
  means: string;
  counts: string[];
  does_not_count: TagDefinitionDoesNotCount[];
  confusable_with: TagDefinitionConfusable[];
  leave_out_when: string | null;
  example_image_ids: number[];
  status: 'active' | 'superseded';
  created_at: string;
  created_by: string;
  referenced_tags: TagDefinitionReferencedTag[];
}

/* Version metadata only — no document body. */
export interface TagDefinitionVersion {
  id: number;
  version: number;
  status: 'active' | 'superseded';
  means: string;
  created_at: string;
  created_by: string;
}

/* One row per tag that HAS an active definition; a tag absent from the list has
 * none yet (the future "no definition = cannot enter the pipeline" gate). */
export interface TagDefinitionStatus {
  tag_id: number;
  definition_id: number;
  version: number;
  means: string;
  created_at: string;
}

export type TagContentsOrder = 'recent' | 'outlier_first';

export interface TagPositiveImage {
  image_id: number;
  storage_path: string | null;
  sreality_url: string;
  updated_at: string;
  /* Cosine DISTANCE from this tag's own centroid — 0 = identical, and only ever
   * meaningful as a rank INSIDE this one tag (measured inter-tag centroid
   * distances span ~0.01 to ~0.42, so an absolute value transfers nowhere).
   * Present only on an order='outlier_first' read; null when the image carries
   * no CLIP embedding, which makes it unplaceable, not an outlier. */
  centroid_distance?: number | null;
  /* 1 = farthest from the centroid. Server-assigned, so patching a row out of
   * the cached list cannot renumber the ones that stay. */
  distance_rank?: number | null;
}

export interface TagPositiveImagesResponse {
  data: TagPositiveImage[];
  /* The order the server ACTUALLY applied — a tag under the centroid floor
   * comes back 'recent' however it was asked. The UI reads this and never
   * re-derives the verdict from the counts. */
  order: TagContentsOrder;
  /* Embedded human-verified positives behind the centroid: null = not computed
   * (recent read), 0 = computed and there are none. */
  centroid_positives: number | null;
  min_positives: number;
}

/* `cosine_distance` is a DISTANCE (0 = identical), not a similarity — pgvector's
 * `<=>`. Lower is closer; the list arrives sorted ascending.
 * `embedded_positive_count` counts positives that actually have a CLIP
 * embedding, so it can be lower than the overview's positive_count. */
export interface TagNeighbour {
  tag_id: number;
  label: string;
  family: string | null;
  embedded_positive_count: number;
  cosine_distance: number;
}

export interface SaveTagDefinitionIn {
  means: string;
  counts: string[];
  does_not_count: TagDefinitionDoesNotCount[];
  confusable_with: TagDefinitionConfusable[];
  leave_out_when: string | null;
  example_image_ids: number[];
  /* The version this edit was written against — null when the editor opened a
   * tag with no definition. The server refuses (422) a save whose base_version
   * is no longer the active one, so a stale second tab cannot revert the
   * definition; send what the form was loaded from, never the newest known. */
  base_version: number | null;
}

export const listTagDefinitionStatus = (): Promise<{ data: TagDefinitionStatus[] }> =>
  request<{ data: TagDefinitionStatus[] }>('/new-dedup/labeling/definitions', { jwt: true });

/* Null body when the tag exists but has no definition yet; a 404 (ApiError) when
 * the tag itself is unknown. */
export const getTagDefinition = (
  tagId: number,
): Promise<{ data: TagDefinition | null }> =>
  request<{ data: TagDefinition | null }>(
    `/new-dedup/labeling/tags/${tagId}/definition`,
    { jwt: true },
  );

/* The definition as a PERSON reads it while labeling. Every string is rendered by
 * toolkit/tag_definition_render.py — the browser lays it out and assembles nothing,
 * so there is exactly one answer anywhere to "what does this tag mean". */
export interface TagHandbookCard {
  tag_label: string;
  headline: string;
  count_it: string[];
  dont_count_it: string[];
  cant_tell: string[];
}

/* Null body when the tag has no definition yet: a card invented from nothing
 * would be a labeling guide nobody wrote. */
export const getTagDefinitionCard = (
  tagId: number,
): Promise<{ data: { card: TagHandbookCard; prompt: string; definition_id: number; version: number } | null }> =>
  request<{ data: { card: TagHandbookCard; prompt: string; definition_id: number; version: number } | null }>(
    `/new-dedup/labeling/tags/${tagId}/definition/card`,
    { jwt: true },
  );

/* Renders an UNSAVED draft. Writes nothing.
 *
 * The editor cannot preview by re-reading the saved definition — there are no
 * server-side drafts, so it would always show the PREVIOUS version. The other way
 * round would be a TypeScript copy of the renderer, i.e. two implementations of
 * the same meaning, free to drift. A debounced round-trip is the cheaper price. */
export const previewTagDefinitionCard = (
  tagId: number,
  body: SaveTagDefinitionIn,
): Promise<{ data: { card: TagHandbookCard } }> =>
  request<{ data: { card: TagHandbookCard } }>(
    `/new-dedup/labeling/tags/${tagId}/definition/card/preview`,
    { method: 'POST', json: body, jwt: true },
  );

/* --- the sealed exam (migrations 458 + 459) ------------------------------- */

export interface ExamTag { id: number; label: string }

export interface ExamQuestion {
  image_id: number;
  position: number;
  storage_path: string | null;
  /* The machine's pre-answer for THIS image against THIS sitting's question
   * list (scripts/suggest_exam_answers.py). null = nothing worth showing (not
   * computed, errored, or computed against a different question list); [] =
   * the machine genuinely suggests none. Rendered as a subtle mark, never a
   * pre-filled verdict — the stored suggestion vs the final answer is the
   * standing anchoring audit. */
  suggested_tag_ids?: number[] | null;
}

export interface ExamState {
  cohort: { name: string; sealed: boolean };
  /* Which iteration's question list this sitting asks — a named tag_exam_sets row,
   * or 'routing' for the flag-derived fallback set_1 was seeded to match. */
  set: string;
  /* The eight routing tags, server-resolved. Never a copy in the client: the
   * buttons must be the same set the answers are written against. */
  tags: ExamTag[];
  progress: { total: number; answered: number; remaining: number };
  /* Null when every image has a verdict on every tag. Carries the machine's
   * suggestion since 2026-08-30 (the operator's ruling, reversing the original
   * no-suggestion posture) — see ExamQuestion.suggested_tag_ids. */
  question: ExamQuestion | null;
}

export interface ExamCohortRow {
  name: string;
  /* holdout = the graded yardstick (excluded from training); curated = your
   * marked images re-labeled carefully, feeding training. */
  purpose: 'holdout' | 'curated';
  sealed: boolean;
  members: number;
}

export const getExamSets = (): Promise<{ data: Array<{ name: string; tag_count: number }> }> =>
  request<{ data: Array<{ name: string; tag_count: number }> }>(
    '/new-dedup/labeling/exam-sets', { jwt: true },
  );

export const getExamCohorts = (): Promise<{ data: ExamCohortRow[] }> =>
  request<{ data: ExamCohortRow[] }>('/new-dedup/labeling/exam-cohorts', { jwt: true });

export const getExamState = (cohort: string, set?: string): Promise<{ data: ExamState }> =>
  request<{ data: ExamState }>(
    `/new-dedup/labeling/exam/${cohort}${set ? `?set=${encodeURIComponent(set)}` : ''}`,
    { jwt: true },
  );

export type MachineVerdict = 'yes' | 'no' | 'skip';

export interface ExamMachineReview {
  verdicts: Record<string, MachineVerdict>;
  dismissed_tag_ids: number[];
  reviewed_at: string | null;
}

export interface ExamAnswerRow {
  image_id: number;
  position: number;
  picked_tag_ids: number[];
  skipped_tag_ids: number[];
  cant_tell: boolean;
  /* Cells written by migration 466's bulk default (created_by backfill:*) —
   * declared negatives the operator has not personally confirmed yet. The
   * review page fences these buttons off until the row is re-answered. */
  auto_tag_ids?: number[];
  /* The definition-driven machine review (migration 467): one verdict per
   * tag against the ACTIVE definitions. The page derives proposals from the
   * row's live state; null/absent = no current review for this image. */
  machine?: ExamMachineReview | null;
  /* The machine's pre-answer for this image against the current question
   * list, beside your final — the anchoring/disagreement audit made visible.
   * null/absent = no current suggestion. */
  suggested_tag_ids?: number[] | null;
}

/* Every fully-answered image of one sitting with its current verdicts, draw
 * order. Corrections go back through answerExamQuestion — the same single write
 * path the exam uses. */
export const getExamAnswers = (
  cohort: string, set?: string,
): Promise<{ data: { set: string; tags: ExamTag[]; rows: ExamAnswerRow[] } }> =>
  request<{ data: { set: string; tags: ExamTag[]; rows: ExamAnswerRow[] } }>(
    `/new-dedup/labeling/exam/${cohort}/answers${set ? `?set=${encodeURIComponent(set)}` : ''}`,
    { jwt: true },
  );

/* Every routing tag in NEITHER list becomes a NEGATIVE — that is what lets one
 * answer measure precision and recall at once. skipped_tag_ids is the brief's
 * per-tag leave-out (excluded/'pruned'): subject clearly present, photo of
 * something else; the cell trains nothing and grades nothing. */
export const answerExamQuestion = (
  cohort: string,
  body: {
    image_id: number; picked_tag_ids: number[]; skipped_tag_ids: number[];
    cant_tell: boolean; set?: string;
  },
): Promise<{ data: { image_id: number; cells_written: number } }> =>
  request<{ data: { image_id: number; cells_written: number } }>(
    `/new-dedup/labeling/exam/${cohort}/answer`,
    { method: 'POST', json: body, jwt: true },
  );

/* "Keep mine" on one machine proposal. Accepting one is NOT a separate call:
 * it is answerExamQuestion with that cell changed — the single write path. */
export const dismissExamMachineProposal = (
  cohort: string, body: { image_id: number; tag_id: number },
): Promise<{ data: { image_id: number; dismissed_tag_ids: number[] } }> =>
  request<{ data: { image_id: number; dismissed_tag_ids: number[] } }>(
    `/new-dedup/labeling/exam/${cohort}/machine-review/dismiss`,
    { method: 'POST', json: body, jwt: true },
  );

/* Writes a NEW version and retires the previous one. There is no draft state
 * server-side — batch every edit into one call. */
export const saveTagDefinition = (
  tagId: number,
  body: SaveTagDefinitionIn,
): Promise<{ data: TagDefinition }> =>
  request<{ data: TagDefinition }>(`/new-dedup/labeling/tags/${tagId}/definition`, {
    method: 'PUT',
    json: body,
    jwt: true,
  });

export const listTagDefinitionVersions = (
  tagId: number,
): Promise<{ data: TagDefinitionVersion[] }> =>
  request<{ data: TagDefinitionVersion[] }>(
    `/new-dedup/labeling/tags/${tagId}/definition/versions`,
    { jwt: true },
  );

export const getTagDefinitionVersion = (
  tagId: number,
  version: number,
): Promise<{ data: TagDefinition }> =>
  request<{ data: TagDefinition }>(
    `/new-dedup/labeling/tags/${tagId}/definition/versions/${version}`,
    { jwt: true },
  );

/* What the tag ACTUALLY contains — every image currently positive on it. Not
 * listNewDedupTagImages: that one lists the tag's REVIEW QUEUE (whatever its
 * state), which is a question about work left, not about what the tag holds. */
export const listTagPositiveImages = (
  tagId: number,
  limit = 200,
  order: TagContentsOrder = 'recent',
): Promise<TagPositiveImagesResponse> =>
  request<TagPositiveImagesResponse>(
    `/new-dedup/labeling/tags/${tagId}/positive-images`,
    { query: { limit, order }, jwt: true },
  );

/* Empty — never an error — when the tag has fewer than 5 positives carrying a
 * CLIP embedding, i.e. too few to have a meaningful centroid. */
export const listTagNeighbours = (
  tagId: number,
  limit = 8,
): Promise<{ data: TagNeighbour[] }> =>
  request<{ data: TagNeighbour[] }>(`/new-dedup/labeling/tags/${tagId}/neighbours`, {
    query: { limit },
    jwt: true,
  });

/* ----- the tagging bake-off (api/new_dedup_bakeoff.py) --------------------
 *
 * READ-ONLY. One yes/no classifier (a HEAD) per photo tag, trained on the
 * embeddings of one encoder configuration (an ARM) under one training MODE,
 * then scored over every labelled photo. The page these four calls back is the
 * comparison surface: which arm should NEW DEDUP buy into, and what does the
 * number look like as photographs.
 *
 * TWO CONVENTIONS TRAVEL WITH EVERY SHAPE HERE, and both are load-bearing:
 *  - a `null` precision / recall / f1 means NOTHING WAS PROPOSED. It is not
 *    zero, and it must never render as one. `*_graded_n` sits beside every rate
 *    for exactly that reason and belongs beside it on screen too.
 *  - `label: null` (exam split only) is a human ABSTENTION. Those rows carry a
 *    real score and a real prediction, and are in no bucket and no rate.
 *
 * Raw scores compare only WITHIN one mode: the two logistic modes score in
 * [0, 1], `pos_only_centroid` is a cosine. Compare outcomes and metrics across
 * modes, never the numbers themselves. */

export type BakeoffMode = 'pos_neg' | 'pos_only_free_neg' | 'pos_only_centroid';
export type BakeoffSplit = 'cv' | 'exam';
export type BakeoffOutcome = 'tp' | 'fp' | 'fn' | 'tn';

export interface BakeoffArm {
  id: number;
  run_id: number;
  /* The short human name, e.g. "dinov3-b16@768/bf16". */
  arm: string;
  dim: number | null;
  status: string;
  note: string | null;
  model: string;
  revision: string | null;
  library: string | null;
  pooling: string | null;
  resolution: number | null;
  preprocessing: string | null;
  dtype: string | null;
}

export interface BakeoffRun {
  id: number;
  created_at: string;
  label: string | null;
  note: string | null;
  status: string;
  manifest_key: string | null;
  /* Tag ids only — the human labels arrive with the metrics rows. */
  heads: number[];
  min_train_positives: number | null;
  arms: BakeoffArm[];
}

export interface BakeoffMetric {
  arm_id: number;
  arm: string;
  mode: BakeoffMode;
  tag_id: number;
  tag_label: string;
  n_pos: number;
  n_neg: number;
  n_groups: number;
  cv_precision: number | null;
  cv_recall: number | null;
  cv_f1: number | null;
  cv_graded_n: number;
  cv_tp: number;
  cv_fp: number;
  cv_tn: number;
  cv_fn: number;
  exam_precision: number | null;
  exam_recall: number | null;
  exam_f1: number | null;
  exam_graded_n: number;
  exam_abstained_n: number;
  exam_tp: number;
  exam_fp: number;
  exam_tn: number;
  exam_fn: number;
  threshold: number | null;
  dataset_hash: string | null;
  /* 'failed' is a DECIDED cell, not a missing one — its note says why. */
  status: string;
  note: string | null;
  trained_at: string | null;
}

export interface BakeoffImageScore {
  arm_id: number;
  arm: string;
  mode: BakeoffMode;
  tag_id: number;
  tag_label: string;
  split: BakeoffSplit;
  fold: number | null;
  label: 1 | 0 | null;
  score: number;
  predicted: boolean;
  outcome: BakeoffOutcome | 'abstained';
}

export interface BakeoffImage {
  image_id: number;
  listing_id: number | null;
  storage_path: string | null;
  scores: BakeoffImageScore[];
}

export interface BakeoffTile {
  image_id: number;
  listing_id: number | null;
  storage_path: string | null;
  score: number;
  label: 1 | 0 | null;
  predicted: boolean;
  fold: number | null;
}

export interface BakeoffBuckets {
  arm_id: number;
  mode: BakeoffMode;
  tag_id: number;
  split: BakeoffSplit;
  abstained_count: number;
  buckets: Record<BakeoffOutcome, { count: number; tiles: BakeoffTile[] }>;
  histogram: {
    bins: number;
    /* The MEASURED range of this cell, not an assumed axis — the logistic modes
     * live in [0, 1] and the centroid mode in cosine space. */
    lo: number;
    hi: number;
    positive: number[];
    negative: number[];
    abstained: number[];
  };
}

export const listBakeoffRuns = (
  limit = 25,
): Promise<{ data: BakeoffRun[] }> =>
  request<{ data: BakeoffRun[] }>('/new-dedup/tagging-bakeoff/runs', {
    query: { limit }, jwt: true,
  });

/* The whole arm x mode x head table in one payload — small by construction, so
 * the page pivots it client-side rather than asking the API to. */
export const getBakeoffMetrics = (
  runId: number,
): Promise<{ data: BakeoffMetric[] }> =>
  request<{ data: BakeoffMetric[] }>(
    `/new-dedup/tagging-bakeoff/runs/${runId}/metrics`, { jwt: true },
  );

/* VIEW A. `arms` is a COMMA-JOINED string here, not a repeated param — the
 * route parses it itself (`arms: str`), so the request helper's array
 * serialization would arrive as an unparseable value. `outcome` requires
 * `tag_id`: an outcome is one head's verdict. */
export const getBakeoffImages = (
  runId: number,
  params: {
    split?: BakeoffSplit;
    arms?: string;
    mode?: BakeoffMode;
    tag_id?: number;
    outcome?: BakeoffOutcome;
    after_image_id?: number;
    limit?: number;
  } = {},
): Promise<{ data: { images: BakeoffImage[]; next_after_image_id: number | null } }> =>
  request<{ data: { images: BakeoffImage[]; next_after_image_id: number | null } }>(
    `/new-dedup/tagging-bakeoff/runs/${runId}/images`, { query: params, jwt: true },
  );

/* VIEW B. One head under one arm and mode. Each bucket pages INDEPENDENTLY on
 * the same limit/offset, so "next page" advances all four together. */
export const getBakeoffBuckets = (
  runId: number,
  params: {
    arm_id: number;
    mode: BakeoffMode;
    tag_id: number;
    split?: BakeoffSplit;
    limit?: number;
    offset?: number;
  },
): Promise<{ data: BakeoffBuckets }> =>
  request<{ data: BakeoffBuckets }>(
    `/new-dedup/tagging-bakeoff/runs/${runId}/buckets`, { query: params, jwt: true },
  );

export interface BakeoffScoreRow {
  image_id: number;
  listing_id: number | null;
  storage_path: string | null;
  score: number;
  label: 1 | 0 | null;
  predicted: boolean;
  fold: number | null;
  outcome: BakeoffOutcome | 'abstained';
}

export interface BakeoffScores {
  arm_id: number;
  mode: BakeoffMode;
  tag_id: number;
  split: BakeoffSplit;
  /* The size of the whole cell, not of this page. */
  total: number;
  /* The window that was actually served, echoed back. */
  limit: number;
  offset: number;
  rows: BakeoffScoreRow[];
}

/* VIEW C. The same cell as View B, UNBUCKETED: every photo the head scored, in
 * score order. Paged by limit/offset like the training-set grid (the API orders
 * by score with image_id as the unique tiebreaker, so an offset is a stable
 * position even though scores tie); rank in the cell = offset + place on the
 * page. `limit` up to 10,000, the training-set page's widest page. */
export const getBakeoffScores = (
  runId: number,
  params: {
    arm_id: number;
    mode: BakeoffMode;
    tag_id: number;
    split?: BakeoffSplit;
    limit?: number;
    offset?: number;
  },
): Promise<{ data: BakeoffScores }> =>
  request<{ data: BakeoffScores }>(
    `/new-dedup/tagging-bakeoff/runs/${runId}/scores`, { query: params, jwt: true },
  );

export interface BakeoffImageDetailScore {
  arm_id: number;
  arm: string;
  resolution: number | null;
  mode: BakeoffMode;
  tag_id: number;
  tag_label: string | null;
  split: BakeoffSplit;
  fold: number | null;
  label: 1 | 0 | null;
  score: number;
  predicted: boolean;
  outcome: BakeoffOutcome | 'abstained';
}

export interface BakeoffImageDetail {
  image_id: number;
  listing_id: number | null;
  storage_path: string | null;
  scores: BakeoffImageDetailScore[];
}

/* VIEW D. One photograph, every score the run gave it. This is RAW MODEL
 * OUTPUT — the probability each head assigned this photo — not a metric, which
 * is why the modal built on it never shows an F1. Ranking heads against each
 * other is meaningful only WITHIN one (arm, mode, split): the scales differ
 * across modes and two arms are two different models. */
export const getBakeoffImageDetail = (
  runId: number,
  imageId: number,
): Promise<{ data: BakeoffImageDetail }> =>
  request<{ data: BakeoffImageDetail }>(
    `/new-dedup/tagging-bakeoff/runs/${runId}/images/${imageId}`, { jwt: true },
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
  request<{ data: BorderCase }>('/labeling/border-case', {
    method: 'POST',
    json: { image_id },
    jwt: true,
  });
export const deleteBorderCase = (
  image_id: number,
): Promise<{ data: { deleted: boolean } }> =>
  request<{ data: { deleted: boolean } }>('/labeling/border-case', {
    method: 'DELETE',
    query: { image_id },
    jwt: true,
  });

export const listAgentTools = (): Promise<{ data: AgentTool[] }> =>
  request<{ data: AgentTool[] }>('/admin/tools', { jwt: true });

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
  request<{ data: PortalAdminRow[] }>('/admin/portals', { jwt: true });

export const updatePortalLimits = (
  source: string,
  patch: PortalLimitValues,
): Promise<{ source: string; overrides: PortalLimitValues; effective: PortalLimitValues }> =>
  request(`/admin/portals/${encodeURIComponent(source)}/limits`, {
    method: 'PUT',
    json: patch,
    jwt: true,
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
  request<{ current: RentMapRevision | null }>('/admin/rent-map', { jwt: true });

export const listRentMapRevisions = (): Promise<{ data: RentMapRevision[] }> =>
  request<{ data: RentMapRevision[] }>('/admin/rent-map/revisions', { jwt: true });

export const triggerRentMapFetch = (): Promise<RentMapIngestResult> =>
  request<RentMapIngestResult>('/admin/rent-map/fetch', { method: 'POST', jwt: true });

export async function uploadRentMapFile(
  file: File,
): Promise<RentMapIngestResult> {
  const form = new FormData();
  form.append('file', file);
  const res = await fetch(`${BASE_URL}/admin/rent-map/revisions`, {
    method: 'POST',
    headers: {
      Accept: 'application/json',
      ...(await authHeader(true)),
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
    { jwt: true },
  );

export const updateConditionScoringRegions = (
  enabledRegionIds: number[],
): Promise<{ data: ConditionScoringRegionsPayload }> =>
  request<{ data: ConditionScoringRegionsPayload }>(
    '/admin/condition-scoring/regions',
    { method: 'PUT', json: { enabled_region_ids: enabledRegionIds }, jwt: true },
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
  request<{ data: ClipTaggingRegionsPayload }>('/admin/clip-tagging/regions', { jwt: true });

export const updateClipTaggingRegions = (
  priorityRegionIds: number[],
): Promise<{ data: ClipTaggingRegionsPayload }> =>
  request<{ data: ClipTaggingRegionsPayload }>(
    '/admin/clip-tagging/regions',
    { method: 'PUT', json: { priority_region_ids: priorityRegionIds }, jwt: true },
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
  request<FilterSchemaPayload>('/admin/filter-schema', { jwt: true });

export const getFilterVisibility = (): Promise<{ data: FilterVisibilityRow[] }> =>
  request<{ data: FilterVisibilityRow[] }>('/admin/filter-visibility', { jwt: true });

export const setFilterVisibility = (
  agenda: Agenda,
  filterId: string,
  enabled: boolean,
): Promise<FilterVisibilityRow> =>
  request<FilterVisibilityRow>(
    `/admin/filter-visibility/${encodeURIComponent(agenda)}/${encodeURIComponent(filterId)}`,
    { method: 'PUT', json: { enabled }, jwt: true },
  );

/* ----- curation (U2.6) ---------------------------------------------------
 *
 * Collections, tags, and notes — all PROPERTY-grain (a property groups one
 * real-world listing across portals). Reads of `which tags / which
 * collections does property X belong to` go through the *_public Supabase
 * views (see lib/queries.ts) — there is no per-property GET on the API for
 * those. Notes are read via the API only. Everything else (list-by-domain,
 * create, update, delete, attach, detach) goes through the FastAPI endpoints
 * wrapped below.
 *
 * EVERY wrapper here passes `jwt: true`. The routes run on the tenant pool
 * (verify_jwt + RLS-scoped connection), which rejects the static API_TOKEN
 * outright — it is not an identity, it ships inside this bundle. Dropping the
 * flag does not degrade to "unscoped but working"; it 401s.
 */

/* Collections */

export const listCollections = (): Promise<{ data: Collection[]; total: number }> =>
  request<{ data: Collection[]; total: number }>('/collections', { jwt: true });

export const getCollection = (id: number): Promise<CollectionWithProperties> =>
  request<CollectionWithProperties>(`/collections/${id}`, { jwt: true });

export const createCollection = (input: {
  name: string;
  description?: string | null;
  monitoring_enabled?: boolean;
  notify_channels?: string[];
}): Promise<Collection> =>
  request<Collection>('/collections', { method: 'POST', json: input, jwt: true });

export const updateCollection = (
  id: number,
  input: {
    name?: string | null;
    description?: string | null;
    monitoring_enabled?: boolean;
    notify_channels?: string[];
  },
): Promise<Collection> =>
  request<Collection>(`/collections/${id}`, {
    method: 'PATCH',
    json: input,
    jwt: true,
  });

export const deleteCollection = (id: number): Promise<{ deleted: true }> =>
  request<{ deleted: true }>(`/collections/${id}`, { method: 'DELETE', jwt: true });

export const addPropertiesToCollection = (
  id: number,
  property_ids: number[],
): Promise<{ added: number; skipped: number }> =>
  request<{ added: number; skipped: number }>(`/collections/${id}/properties`, {
    method: 'POST',
    json: { property_ids },
    jwt: true,
  });

export const removePropertyFromCollection = (
  id: number,
  property_id: number,
): Promise<{ removed: boolean }> =>
  request<{ removed: boolean }>(
    `/collections/${id}/properties/${property_id}`,
    { method: 'DELETE', jwt: true },
  );

/* Tags */

export const listTags = (): Promise<{ data: Tag[] }> =>
  request<{ data: Tag[] }>('/tags', { jwt: true });

export const createTag = (input: { name: string; color: TagColor }): Promise<Tag> =>
  request<Tag>('/tags', { method: 'POST', json: input, jwt: true });

export const updateTag = (
  id: number,
  patch: { name?: string | null; color?: TagColor | null },
): Promise<Tag> =>
  request<Tag>(`/tags/${id}`, { method: 'PATCH', json: patch, jwt: true });

export const deleteTag = (id: number): Promise<{ deleted: true }> =>
  request<{ deleted: true }>(`/tags/${id}`, { method: 'DELETE', jwt: true });

export const attachTag = (
  property_id: number,
  tag_id: number,
): Promise<{ attached: boolean }> =>
  request<{ attached: boolean }>(`/properties/${property_id}/tags`, {
    method: 'POST',
    json: { tag_id },
    jwt: true,
  });

export const detachTag = (
  property_id: number,
  tag_id: number,
): Promise<{ detached: boolean }> =>
  request<{ detached: boolean }>(
    `/properties/${property_id}/tags/${tag_id}`,
    { method: 'DELETE', jwt: true },
  );

/* Notes (per-property journal) */

export const listPropertyNotes = (
  property_id: number,
): Promise<{ data: Note[] }> =>
  request<{ data: Note[] }>(`/properties/${property_id}/notes`, { jwt: true });

export const createPropertyNote = (
  property_id: number,
  body: string,
  origin_listing_id?: number,
  // Surrogate twin (migration 323/R2). origin_listing_id is the legacy
  // sreality_id, NULL for a post-Gate-2 listing — pass the surrogate too so
  // the note's provenance survives even when the legacy id is unavailable
  // (api.create_note COALESCEs one from the other server-side).
  origin_listing_ref_id?: number,
): Promise<Note> =>
  request<Note>(`/properties/${property_id}/notes`, {
    method: 'POST',
    json: {
      body,
      ...(origin_listing_id != null ? { origin_listing_id } : {}),
      ...(origin_listing_ref_id != null ? { origin_listing_ref_id } : {}),
    },
    jwt: true,
  });

export const updatePropertyNote = (
  property_id: number,
  note_id: number,
  body: string,
): Promise<Note> =>
  request<Note>(`/properties/${property_id}/notes/${note_id}`, {
    method: 'PATCH',
    json: { body },
    jwt: true,
  });

export const deletePropertyNote = (
  property_id: number,
  note_id: number,
): Promise<{ deleted: true }> =>
  request<{ deleted: true }>(`/properties/${property_id}/notes/${note_id}`, {
    method: 'DELETE',
    jwt: true,
  });

/* Dismissals (migration 536) — "never show me this property again", and its
 * undo. State is read via property_dismissals_public. */

export const dismissProperty = (
  property_id: number,
): Promise<{ property_id: number; added: boolean }> =>
  request<{ property_id: number; added: boolean }>('/dismissals', {
    method: 'POST',
    json: { property_id },
    jwt: true,
  });

export const undismissProperty = (
  property_id: number,
): Promise<{ property_id: number; removed: boolean }> =>
  request<{ property_id: number; removed: boolean }>(`/dismissals/${property_id}`, {
    method: 'DELETE',
    jwt: true,
  });

/* Deal pipeline (migration 205) — bookmark a property into the pipeline
 * (entry stage) / remove it. Membership is read via property_pipeline_public. */

export const addPipelineCard = (
  property_id: number,
): Promise<{ property_id: number; stage_key: string; added: boolean }> =>
  request<{ property_id: number; stage_key: string; added: boolean }>(
    '/pipeline/cards',
    { method: 'POST', json: { property_id }, jwt: true },
  );

export const removePipelineCard = (
  property_id: number,
): Promise<{ removed: boolean }> =>
  request<{ removed: boolean }>(`/pipeline/cards/${property_id}`, {
    method: 'DELETE',
    jwt: true,
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
      jwt: true,
    },
  );

/* Stage management — operator-curated kanban columns (rename / recolor / add /
 * reorder / archive). The `key` slug is derived server-side from the label. */

export const createPipelineStage = (input: {
  label: string;
  color?: TagColor | null;
  is_terminal?: boolean;
  /* Short funnel badge (migration 377); omit to fall back to the ordinal. */
  code?: string | null;
}): Promise<PipelineStage> =>
  request<PipelineStage>('/pipeline/stages', { method: 'POST', json: input, jwt: true });

export const updatePipelineStage = (
  stage_id: number,
  patch: {
    label?: string;
    color?: TagColor | null;
    is_terminal?: boolean;
    is_entry?: boolean;
    /* Explicit null clears the badge back to the ordinal fallback. */
    code?: string | null;
  },
): Promise<PipelineStage> =>
  request<PipelineStage>(`/pipeline/stages/${stage_id}`, {
    method: 'PATCH',
    json: patch,
    jwt: true,
  });

export const reorderPipelineStages = (
  ordered_ids: number[],
): Promise<{ data: PipelineStage[] }> =>
  request<{ data: PipelineStage[] }>('/pipeline/stages/reorder', {
    method: 'POST',
    json: { ordered_ids },
    jwt: true,
  });

export const archivePipelineStage = (
  stage_id: number,
): Promise<{ archived: boolean; stage_id: number }> =>
  request<{ archived: boolean; stage_id: number }>(
    `/pipeline/stages/${stage_id}`,
    { method: 'DELETE', jwt: true },
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
    { query: { include_inactive: options.includeInactive ?? true }, jwt: true },
  );

export const getWatchdogSubscription = (
  id: string,
): Promise<WatchdogSubscription> =>
  request<WatchdogSubscription>(
    `/notifications/subscriptions/${encodeURIComponent(id)}`,
    { jwt: true },
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
    jwt: true,
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
    { method: 'PUT', json: patch, jwt: true },
  );

export const deleteWatchdogSubscription = (
  id: string,
): Promise<{ deleted: true }> =>
  request<{ deleted: true }>(
    `/notifications/subscriptions/${encodeURIComponent(id)}`,
    { method: 'DELETE', jwt: true },
  );

export const listWatchdogDispatches = (
  params: ListWatchdogDispatchesParams = {},
): Promise<WatchdogDispatchesResponse> =>
  request<WatchdogDispatchesResponse>('/notifications/dispatches', {
    query: params as Record<string, QueryValue>,
    jwt: true,
  });

export const markWatchdogDispatchSeen = (
  dispatchId: string,
): Promise<WatchdogDispatch> =>
  request<WatchdogDispatch>(
    `/notifications/dispatches/${encodeURIComponent(dispatchId)}/mark-seen`,
    { method: 'POST', jwt: true },
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
    jwt: true,
  });

export const getNotificationUnreadCount = (
  source_kind: NotificationSourceKind | 'all' = 'all',
): Promise<NotificationUnreadCount> =>
  request<NotificationUnreadCount>('/notifications/unread-count', {
    query: { source_kind },
    jwt: true,
  });

export const markAllNotificationsSeen = (
  source_kind: NotificationSourceKind | 'all' = 'all',
): Promise<{ updated: number }> =>
  request<{ updated: number }>('/notifications/mark-all-seen', {
    method: 'POST',
    query: { source_kind },
    jwt: true,
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

/* ----- Operator merge mechanics (multi-portal) ----------------------------
 * Mounted under `/properties/*` since the NEW DEDUP cutoff (docs/design/
 * new-dedup/CUTOFF.md §2/S5) — the mechanics that survived the decision-layer
 * removal. Every route is `require_admin`, so each call sends `jwt: true`. */

export interface UnmergeResult {
  data: {
    merge_group_id: string;
    survivor_id: number;
    retired_ids: number[];
    listings_moved_back: number;
    conflicts: number[];
  };
}

export interface ClusterMergeResult {
  merge_group_id: string;
  survivor_id: number;
  retired_ids: number[];
  listings_moved: number;
  candidates_resolved: number;
}

/* Merge an operator-checked SET of properties (Browse mergeMode) into one
 * survivor under one reversible merge group. */
export const mergePropertySet = (
  propertyIds: number[],
): Promise<ClusterMergeResult> =>
  request<ClusterMergeResult>('/properties/merge', {
    method: 'POST',
    json: { property_ids: propertyIds },
    jwt: true,
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
  request<AssetLinkResult>('/properties/assets/link', {
    method: 'POST',
    json: { property_ids: propertyIds, note: note ?? null },
    jwt: true,
  });

export const unlinkAssetProperty = (
  propertyId: number,
): Promise<{ data: { asset_id: number; asset_dissolved: boolean } }> =>
  request<{ data: { asset_id: number; asset_dissolved: boolean } }>(
    '/properties/assets/unlink',
    { method: 'POST', json: { property_id: propertyId }, jwt: true },
  );

/* Merge ledger (list / browse-results / unmerge). Retained without a UI caller on
 * purpose: the buttons lived on the deleted Dedup page, and until the new production
 * wave gives them a permanent home unmerge is API-only. These three wrap the surviving
 * `/properties/*` mechanics routes — do not delete them as "dead". */
export const listPropertyMerges = (
  params: { limit?: number; offset?: number } = {},
): Promise<MergesResponse> =>
  request<MergesResponse>('/properties/merges', {
    query: params as Record<string, QueryValue>,
    jwt: true,
  });

/* Browse the RESULTS of merging: already-merged properties whose child-listing
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
  request<MergedPropertiesResponse>('/properties/merged', {
    query: params as Record<string, QueryValue>,
    jwt: true,
  });

export const unmergeMergeGroup = (
  mergeGroupId: string,
): Promise<UnmergeResult> =>
  request<UnmergeResult>(
    `/properties/merges/${encodeURIComponent(mergeGroupId)}/unmerge`,
    { method: 'POST', jwt: true },
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
  apiPost('/price-stats/datasets', input, undefined, true);

export const deletePriceStatDataset = (
  id: number,
): Promise<{ id: number; is_active: boolean }> =>
  request(`/price-stats/datasets/${id}`, { method: 'DELETE', jwt: true });

export const updatePriceStatDataset = (
  id: number,
  patch: Partial<PriceStatDatasetInput> & { is_active?: boolean },
): Promise<import('./priceStats').PriceStatDataset> =>
  request(`/price-stats/datasets/${id}`, { method: 'PATCH', json: patch, jwt: true });

export const runPriceStatDataset = (
  id: number,
): Promise<{ dispatched: boolean; run_url?: string; detail?: string }> =>
  apiPost(`/price-stats/datasets/${id}/run`, {}, undefined, true);

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
  request<{ campaigns: OutreachCampaign[] }>('/outreach/campaigns', { jwt: true });

export const getOutreachCampaign = (id: number): Promise<OutreachCampaign> =>
  request<OutreachCampaign>(`/outreach/campaigns/${id}`, { jwt: true });

export const createOutreachCampaign = (input: {
  name: string;
  goal?: string | null;
  guidance?: string | null;
  target?: OutreachTargetSpec | null;
}): Promise<OutreachCampaign> =>
  request<OutreachCampaign>('/outreach/campaigns', { method: 'POST', json: input, jwt: true });

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
  request<OutreachCampaign>(`/outreach/campaigns/${id}`, { method: 'PATCH', json: patch, jwt: true });

export const previewOutreachTargets = (
  id: number,
  limit = 50,
): Promise<{ targets: OutreachTarget[]; count: number }> =>
  request<{ targets: OutreachTarget[]; count: number }>(
    `/outreach/campaigns/${id}/targets`,
    { query: { limit }, jwt: true },
  );

export const generateOutreachDrafts = (
  id: number,
  limit = 25,
): Promise<{ generated: number; targets: number }> =>
  request<{ generated: number; targets: number }>(
    `/outreach/campaigns/${id}/generate`,
    { method: 'POST', query: { limit }, jwt: true },
  );

export const listOutreachMessages = (
  id: number,
  status?: string,
): Promise<{ messages: OutreachMessage[] }> =>
  request<{ messages: OutreachMessage[] }>(
    `/outreach/campaigns/${id}/messages`,
    { query: status ? { status } : undefined, jwt: true },
  );

export const updateOutreachMessage = (
  messageId: number,
  patch: { status?: string; subject?: string; body?: string; notes?: string },
): Promise<OutreachMessage> =>
  request<OutreachMessage>(`/outreach/messages/${messageId}`, {
    method: 'PATCH',
    json: patch,
    jwt: true,
  });

export const regenerateOutreachMessage = (
  messageId: number,
): Promise<OutreachMessage> =>
  request<OutreachMessage>(`/outreach/messages/${messageId}/regenerate`, {
    method: 'POST',
    jwt: true,
  });

export const listOutreachSuppressions = (): Promise<{ suppressions: OutreachSuppression[] }> =>
  request<{ suppressions: OutreachSuppression[] }>('/outreach/suppressions', { jwt: true });

export const addOutreachSuppression = (
  broker_id: number,
  reason?: string,
): Promise<OutreachSuppression> =>
  request<OutreachSuppression>('/outreach/suppressions', {
    method: 'POST',
    json: { broker_id, reason },
    jwt: true,
  });

export const removeOutreachSuppression = (
  broker_id: number,
): Promise<{ removed: number }> =>
  request<{ removed: number }>(`/outreach/suppressions/${broker_id}`, {
    method: 'DELETE',
    jwt: true,
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
  evidence: {
    name?: string;
    firm_name?: string | null;
    firm_domain?: string | null;
    broker_count?: number;
    // reason='contact_bridge_review' instead carries the pair that bridged them
    names?: (string | null)[];
    sources?: (string | null)[];
    bridges?: string[];
    // reason='name_cross_firm' carries the two firms + per-firm activity windows
    firms?: string[];
    tenure?: { overlap?: boolean } & Record<string, [string, string] | boolean | undefined>;
    // why the engine did NOT auto-merge this card (all reasons since 2026-08-24)
    hold?: {
      code: string;
      firms?: string[];
      values?: string[];
      identities?: number;
    };
  };
  status: string;
  created_at: string | null;
  brokers: BrokerMergeBroker[];
}

export interface BrokerMergeCandidatePage {
  candidates: BrokerMergeCandidate[];
  count: number;
  reason_counts: Record<string, number>;
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
  reason?: string,
  offset = 0,
): Promise<BrokerMergeCandidatePage> =>
  request<BrokerMergeCandidatePage>(
    '/broker-review/candidates',
    { query: { limit, offset, ...(reason ? { reason } : {}) }, jwt: true },
  );

export const mergeBrokerCandidate = (
  candidateId: number,
  brokerIds?: number[],
): Promise<{ merge_group_id: string; survivor_broker_id: number; retired_broker_ids: number[] }> =>
  request('/broker-review/candidates/' + candidateId + '/merge', {
    method: 'POST',
    json: { broker_ids: brokerIds ?? null },
    jwt: true,
  });

export const dismissBrokerCandidate = (
  candidateId: number,
): Promise<{ id: number; status: string }> =>
  request('/broker-review/candidates/' + candidateId + '/dismiss', { method: 'POST', jwt: true });

export const listBrokerMerges = (
  limit = 50,
): Promise<{ merges: BrokerMergeRecord[] }> =>
  request<{ merges: BrokerMergeRecord[] }>('/broker-review/merges', { query: { limit }, jwt: true });

export const unmergeBrokers = (
  mergeGroupId: string,
): Promise<{ merge_group_id: string; survivor_broker_id: number; restored_broker_ids: number[] }> =>
  request('/broker-review/merges/' + encodeURIComponent(mergeGroupId) + '/unmerge', {
    method: 'POST',
    jwt: true,
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
  request('/admin/plans', { jwt: true });

export const adminCreatePlan = (body: {
  key: string;
  name: string;
  position?: number;
  agendas?: Record<string, boolean>;
}): Promise<Plan> => request('/admin/plans', { method: 'POST', json: body, jwt: true });

export const adminUpdatePlan = (
  key: string,
  body: Partial<Pick<Plan, 'name' | 'position' | 'agendas' | 'is_default'>>,
): Promise<Plan> =>
  request(`/admin/plans/${encodeURIComponent(key)}`, { method: 'PATCH', json: body, jwt: true });

export const adminDeletePlan = (key: string): Promise<{ deleted: boolean }> =>
  request(`/admin/plans/${encodeURIComponent(key)}`, { method: 'DELETE', jwt: true });

export const adminListEntitlements = (): Promise<{ data: EntitlementRow[] }> =>
  request('/admin/entitlements', { jwt: true });

export const adminSetEntitlement = (
  accountId: string,
  body: { plan: string; status?: string },
): Promise<EntitlementRow> =>
  request(`/admin/entitlements/${encodeURIComponent(accountId)}`, {
    method: 'PUT',
    json: body,
    jwt: true,
  });

/* ------------------------------------------------------------- AUTODEDUP */

/* The autonomous cross-portal dedup engine's progress ledger
 * (docs/design/autodedup/PROGRAM.md §12/§13). One row per ITERATION — a unit of
 * work the operator can read as a story — written by the lane itself into
 * `autodedup.iterations`. That schema is unreachable from the browser (the
 * Supabase client is pinned to `public`), so both reads below go through the
 * admin-gated API: `api/routes/autodedup.py`, the shape of which these types
 * mirror field for field.
 *
 * `cost_usd` is READ BACK from `llm_calls` by the lane, never forecast (E32). */
export interface AutodedupIteration {
  id: number;
  wave: string;
  title: string;
  status: 'running' | 'done' | 'failed' | 'skipped';
  approach: string | null;
  tools: string[];
  sample_stats: Record<string, unknown>;
  metrics: Record<string, unknown>;
  cost_usd: number | null;
  /* `artifacts jsonb` carries no shape constraint (migration 528), so the type
   * must not promise one: the render site filters for http(s) strings. */
  artifacts: Record<string, unknown> | null;
  run_id: number | null;
  notes: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

/* Keyset page, newest first: `next_after_id` is the last id of this page and is
 * null on the final one. `data` is null — and `store_ready` false — until
 * migration 528 is applied, so the page renders an empty state instead of
 * failing (the `new_dedup_candidates` posture). */
export interface AutodedupIterationPage {
  store_ready: boolean;
  data: {
    items: AutodedupIteration[];
    has_more: boolean;
    next_after_id: number | null;
  } | null;
}

/* Per-wave rollup, ordered by the wave's most recent iteration. `last_status`
 * is the status of that newest iteration, not an aggregate. */
export interface AutodedupWaveRollup {
  wave: string;
  n: number;
  last_status: string | null;
  cost_usd: number;
}

/* The header strip. Every figure is summed from the same per-wave rollup the
 * `waves` list carries, so the headline and the breakdown cannot disagree. */
/* One row of a `group by` rollup the engine half carries. Each is a count over
 * a store table, not a derived rate, so the page can add them up itself. */
export interface AutodedupGenerationRollup {
  generation: string;
  n_clusters: number;
  n_members: number;
  n_conflicted: number;
  last_changed_at: string | null;
}

export interface AutodedupVerdictRollup {
  kind: 'pair' | 'cluster';
  verdict: AutodedupVerdictValue;
  n: number;
}

export interface AutodedupJudgementRollup {
  tier: string;
  verdict: string;
  n: number;
}

/* The `autodedup.runs` row of the latest SCORE pass. `cohort` is null while the
 * run is still `running` — the lane writes it on the terminal UPDATE, because
 * the block count is only knowable once the dataset has loaded. */
export interface AutodedupScoreRun {
  id: number;
  status: string;
  fingerprint: string | null;
  cohort: Record<string, unknown> | null;
  params: Record<string, unknown> | null;
  stats: Record<string, unknown> | null;
  started_at: string | null;
  finished_at: string | null;
}

/* The engine half of the strip, added with the validation views: what the store
 * holds right now. Optional because the ledger half predates it and a database
 * with no score run yet answers without it — a missing `engine` means "no score
 * run", which the page prints as words rather than as zeros.
 *
 * The counts are keyed the way the store keys them: `pairs_by_zone` and
 * `certificates` are maps because the server aggregates them into one, and the
 * three rollups stay ARRAYS because each row is identified by a compound key
 * (kind+verdict, tier+verdict) that no flat record can hold without inventing a
 * separator. `latest_generation` is the first of `generations`, which the
 * statement already returns newest-first. */
/* One bucket of the reason histogram (migration 533): how often the operator
 * picked one reason code, at one verdict grain. Pair and cluster are separate
 * rows because they answer different questions — a chip on a pair names the
 * discriminator one edge missed, the same chip on a cluster names why a whole
 * proposal was wrong — so the page never sums them. */
export interface AutodedupReasonRollup {
  kind: 'pair' | 'cluster';
  reason: string;
  n: number;
}

export interface AutodedupEngineStats {
  pairs_by_zone: Partial<Record<AutodedupZone, number>>;
  n_pairs: number;
  certificates: Record<string, number>;
  generations: AutodedupGenerationRollup[];
  latest_generation: string | null;
  verdicts: AutodedupVerdictRollup[];
  n_verdicts: number;
  /* Absent against a store that predates 533 — the read degrades, it does not
   * 500 — so the table above it says "not yet" rather than "none". */
  verdict_reasons?: AutodedupReasonRollup[];
  judgements: AutodedupJudgementRollup[];
  n_judgements: number;
  last_score_run: AutodedupScoreRun | null;
}

export interface AutodedupStats {
  n_iterations: number;
  total_cost_usd: number;
  /* D2's spend gate travels with the spend, so the page never retypes the caps. */
  run_cap_usd?: number;
  program_cap_usd?: number;
  last_iteration_at: string | null;
  waves: AutodedupWaveRollup[];
  engine?: AutodedupEngineStats | null;
}

export const getAutodedupIterations = (q?: {
  limit?: number | null;
  after?: number | null;
}): Promise<AutodedupIterationPage> =>
  request<AutodedupIterationPage>('/autodedup/iterations', {
    query: { limit: q?.limit ?? null, after: q?.after ?? null },
    jwt: true,
  });

export const getAutodedupStats = (): Promise<{
  store_ready: boolean;
  data: AutodedupStats | null;
}> =>
  request<{ store_ready: boolean; data: AutodedupStats | null }>('/autodedup/stats', {
    jwt: true,
  });

/* ---------------------------------------------------------------------------
 * AUTODEDUP · validation UI (PROGRAM.md §12, W5)
 *
 * Three read surfaces and one write, all admin-gated and all served by
 * `api/routes/autodedup.py` — the `autodedup` schema is unreachable from the
 * browser (the Supabase client is pinned to `public`), and `listings` is RLS
 * deny-all, so there is no second path to this data.
 *
 * SHADOW MODE (D4). Nothing here applies a merge. A verdict is the operator's
 * opinion recorded against a pair or a cluster; a negative PAIR verdict also
 * writes a permanent must-not-link server-side. The UI never says "merged".
 *
 * Every filter below is a KEY the server validates against its own registry —
 * these are the names, never a predicate.
 * ------------------------------------------------------------------------- */

export type AutodedupVerdictValue =
  | 'same'
  | 'different'
  | 'same_building_different_unit'
  /* E49 (migration 532): a different BUILDING of the same development project.
   * `different` throws the project away and `same_building_different_unit`
   * claims a building the adverts do not share — both lose the one fact the
   * operator established. Like them, it is a permanent must-not-link. */
  | 'same_project_different_unit'
  | 'unsure';

export type AutodedupZone = 'merge' | 'band' | 'reject';

/* What `lib/imageUrl.imageSrc` needs, and nothing else. */
export interface AutodedupImageRef {
  storage_path: string | null;
  sreality_url: string;
}

/* One listing as every validation surface shows it. `listing_id` is
 * `listings.id`; `sreality_id` / `source_id_native` are what an in-app listing
 * link is built from (lib/listingUrl) and are optional because a payload that
 * omits them still renders — with the portal link only. */
export interface AutodedupMember {
  listing_id: number;
  source: string;
  source_url: string | null;
  category_main: string | null;
  category_type: string | null;
  disposition: string | null;
  area_m2: number | null;
  floor: number | null;
  price_czk: number | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  is_active: boolean | null;
  cover: AutodedupImageRef | null;
  n_images: number;
  /* The residual queue and the pair digests carry the storey count; the group
   * members' select list does not. Optional rather than nullable, so a surface
   * that HAS it can show "2 / 5" — floor-within-building is a real unit
   * discriminator — and one that does not simply shows the floor. */
  total_floors?: number | null;
  sreality_id?: number | null;
  source_id_native?: string | null;
  /* The first frames of the album, in the SAME order the cover is picked from —
   * so `images[0]` IS `cover`. Both queues ship 12 per advert — a group card's
   * member and a residual row's side — so the card can be paged; `n_images`
   * still counts the whole album, which is how the card says how many more the
   * dialog would show. Absent on a surface that selects only the cover, which
   * is why the card falls back rather than assuming. */
  images?: AutodedupMemberImage[];
}

export interface AutodedupMemberImage extends AutodedupImageRef {
  image_id?: number | null;
  /* Which side of the pair this frame belongs to — the route selects images for
   * both listings in one statement and stamps each row, so the client never has
   * to infer it from which array it was found in. */
  listing_id?: number | null;
  sequence: number | null;
  /* A 64-bit dHash: JSON cannot carry it as a safe integer, so a string is the
   * honest wire shape and a number is accepted for the small ones. Optional: the
   * QUEUE gallery (12 frames on a group card) selects no hash — nothing on that
   * card computes a distance, and shipping one per frame per member would cost
   * the page a column it never reads. */
  phash?: string | number | null;
}

export interface AutodedupMemberDetail extends AutodedupMember {
  images: AutodedupMemberImage[];
  /* THE ADVERT'S OWN WORDS, detail route only. A developer project's units share
   * the photos and the attribute row and differ only in what the text says — so
   * the dialog carries it and the 20-card queue does not (it would drag a TOASTed
   * description per member of per card across the wire). Both fields arrive
   * PII-scrubbed (E28) and the description is NOT cut at the judge's token cap:
   * `description_truncated` is there so one component renders this text and the
   * judge digest, which IS cut. */
  title?: string | null;
  description?: string | null;
  description_truncated?: boolean;
  description_chars?: number;
}

/* The per-image best match on the OTHER side of the pair (§12: "both image
 * lists with per-image Hamming"). */
export interface AutodedupPairImage extends AutodedupMemberImage {
  best_hamming: number | null;
  best_match_image_id: number | null;
}

/* `features` is the compact store shape: only the PRESENT features, each as
 * [value, present]. A missing key means the feature could not be computed —
 * which is a different statement from a zero. */
export interface AutodedupPairRow {
  listing_lo: number;
  listing_hi: number;
  score: number | null;
  zone: AutodedupZone | null;
  decision: string | null;
  guard_veto: string | null;
  certificate: string | null;
  families: number;
  probes: string[];
  features?: Record<string, [number, boolean]> | null;
  cluster_key?: number | null;
  /* The provenance the row was stored with. Optional because the two QUEUE
   * routes answer with a summary, while the group-detail pair table and the
   * pair page carry the whole stored row — which is where "scored by which
   * model, against which feature set, when" belongs. */
  feature_version?: number | null;
  model_version?: string | null;
  decided_at?: string | null;
  /* Decoded and worded by the server on the routes that send the whole row.
   * Nullable as well as optional: the residual wire form spells an absent
   * decode as null, and `decodeFamilies` reads both as "no families". */
  family_names?: string[] | null;
  why_not_merged?: string;
}

export interface AutodedupEdgeSummary {
  n_edges: number;
  min_score: number | null;
  mean_score: number | null;
  n_certificates: number;
  n_judged?: number;
  /* The union of the member pairs' evidence families, already decoded. */
  family_names?: string[];
}

/* The stored operator verdict. The DETAIL routes return the whole row; the two
 * QUEUE routes return only the four fields a badge needs — so everything the
 * badge does not read is optional here rather than promised and absent. */
export interface AutodedupVerdictRow {
  verdict: AutodedupVerdictValue;
  note: string | null;
  /* WHY, in the registry's codes (migration 533). Optional only because a row
   * read back from a store without the column carries none; `[]` and absent
   * mean the same thing to every reader. */
  reasons?: string[] | null;
  decided_by: string;
  decided_at: string;
  id?: number;
  kind?: 'pair' | 'cluster';
  cluster_key?: number | null;
  listing_lo?: number | null;
  listing_hi?: number | null;
  weight?: number | null;
  /* WHICH PASS the ruling was taken on, and WHICH SET OF ADVERTS it binds
   * (migration 538, rule E58). Null on a pair verdict by the table's own check
   * — a pair ruling is about two listings and belongs to no generation — and
   * null on a legacy cluster row taken before the store recorded the set. */
  generation?: string | null;
  member_ids?: number[] | null;
}

/* A CLUSTER RULING THAT NO LONGER APPLIES. The group the operator confirmed is
 * not the group on screen: a bridge grew it, or a promotion absorbed it into
 * another key. The server sends the earlier ruling with the adverts that arrived
 * and the ones that left, the card reads as UNREVIEWED, and the notice asks for
 * the ruling again rather than inheriting a claim about adverts nobody looked
 * at. Null on every group whose membership has not moved. */
export interface AutodedupStaleVerdict extends AutodedupVerdictRow {
  added: number[];
  removed: number[];
}

/* The judge's ruling. Only the verdict is guaranteed: the residual queue carries
 * a three-field summary (verdict, confidence, tier) and the pair page the whole
 * transcript, and one type for both keeps the chip identical on every surface. */
export interface AutodedupJudgementRow {
  verdict:
    | 'same_property'
    | 'different_property'
    | 'same_building_different_unit'
    | 'insufficient_evidence';
  confidence?: number | null;
  tier?: 'text' | 'vision' | 'gold' | 'oss' | null;
  model?: string | null;
  judge_version?: string | null;
  listing_lo?: number;
  listing_hi?: number;
  unit_discriminator?: string | null;
  key_evidence?: string[] | null;
  contradicting_evidence?: string[] | null;
  developer_project_suspected?: boolean | null;
  cost_usd?: number | null;
  created_at?: string | null;
}

export interface AutodedupConflictRow {
  id: number;
  kind: 'invariant' | 'must_not_link' | 'oversize' | 'bridge';
  cluster_key_a: number | null;
  cluster_key_b: number | null;
  listing_lo: number | null;
  listing_hi: number | null;
  invariant: string | null;
  detail: Record<string, unknown> | null;
  created_at: string;
}

/* One row of `autodedup.clusters`. `status` is never 'applied' in this program
 * (D4) and `property_id` never set. */
export interface AutodedupClusterRow {
  cluster_key: number;
  generation: string;
  size: number;
  block_key: number | null;
  cat_group: string | null;
  category_main: string | null;
  category_type: string | null;
  area_min: number | null;
  area_max: number | null;
  sources: string[];
  medoid_listing_id: number | null;
  min_edge_score: number | null;
  mean_edge_score: number | null;
  n_judged_edges: number;
  n_certificate_edges: number;
  evidence_families?: number | null;
  evidence_family_names?: string[] | null;
  max_gap_days: number | null;
  shared_photo_warning: boolean;
  status: string;
  property_id?: number | null;
  model_version: string | null;
  feature_version: number | null;
  first_built_at?: string | null;
  last_changed_at?: string | null;
}

/* The cluster as the page renders it: the row, flattened together with its
 * members, its edge rollup and the operator's latest verdict. The server sends
 * those as four sibling objects (`{cluster, members, edges, verdict}`); the
 * flattening happens once, in `normalizeGroup` below, so no component has to
 * know which of the two spellings it was handed. */
export interface AutodedupGroup extends AutodedupClusterRow {
  members: AutodedupMember[];
  edges: AutodedupEdgeSummary | null;
  verdict: AutodedupVerdictRow | null;
  /* The operator's own rulings on the members' PAIRS — what a stored split
   * actually is. A card that cannot read them shows "A" over every member while
   * the badge says the group was split, and the next save silently retracts the
   * permanent must-not-links the first one wrote. */
  member_verdicts: AutodedupVerdictRow[];
  /* The ruling that no longer applies, and what moved under it (E58). */
  stale_verdict: AutodedupStaleVerdict | null;
  /* Decoded once — from the server's names when it sends them, from the bitmask
   * otherwise. The chips read this and never the raw smallint. */
  family_names: string[];
}

export interface AutodedupGroupDetail {
  cluster: AutodedupGroup;
  members: AutodedupMemberDetail[];
  pairs: AutodedupPairRow[];
  judgements: AutodedupJudgementRow[];
  conflicts: AutodedupConflictRow[];
  verdicts: AutodedupVerdictRow[];
  /* The PAIR verdicts among the members, including the pairs the engine never
   * scored — a split rules on those too, and keying the read on the scored
   * edges would hide exactly the rulings the dialog has to show back. */
  member_verdicts?: AutodedupVerdictRow[];
  /* The ruling that no longer applies to this group (E58). */
  stale_verdict?: AutodedupStaleVerdict | null;
}

/* What the wire actually carries for one queue item. Every field the server may
 * spell two ways is optional here, and `normalizeGroup` picks. */
interface WireGroupItem {
  cluster?: AutodedupClusterRow;
  members?: AutodedupMember[] | AutodedupMemberDetail[];
  edges?: (AutodedupEdgeSummary & { families?: number | string[] | null }) | null;
  verdict?: AutodedupVerdictRow | null;
  stale_verdict?: AutodedupStaleVerdict | null;
  member_verdicts?: AutodedupVerdictRow[] | null;
}

/* Decode an evidence-family value that may arrive as the stored bitmask or as
 * the server's already-decoded names. The bit table mirrors
 * `autodedup/score_lane.py::FAMILY_BITS` and `api/routes/autodedup.py`. */
const FAMILY_BITS: ReadonlyArray<readonly [number, string]> = [
  [1, 'ATTR'],
  [2, 'PRICE'],
  [4, 'TXT'],
  [8, 'BRK'],
  [16, 'LOC'],
  [32, 'IMG'],
  [64, 'TIME'],
];

export function decodeFamilies(value: number | string[] | null | undefined): string[] {
  if (Array.isArray(value)) return value;
  if (value == null) return [];
  return FAMILY_BITS.filter(([bit]) => (value & bit) !== 0).map(([, name]) => name);
}

export function normalizeGroup(item: WireGroupItem & Partial<AutodedupClusterRow>): AutodedupGroup {
  const cluster = (item.cluster ?? (item as AutodedupClusterRow)) as AutodedupClusterRow;
  const edges = item.edges ?? null;
  return {
    ...cluster,
    members: item.members ?? [],
    edges,
    verdict: item.verdict ?? null,
    stale_verdict: item.stale_verdict ?? null,
    member_verdicts: item.member_verdicts ?? [],
    family_names: decodeFamilies(
      cluster.evidence_family_names ?? edges?.family_names ?? edges?.families ??
        cluster.evidence_families,
    ),
  };
}

/* One log-odds contribution of the linear model, or — when the model exposes
 * none — one present feature. `contribution` null means the second case. */
export interface AutodedupContribution {
  name: string;
  value: number | null;
  present: boolean;
  contribution: number | null;
}

/* A pair the engine did NOT join into one cluster, above the display floor.
 * `why_not_merged` is the precondition that failed, in words — §12's "that last
 * field is what turns a review session into design feedback". */
export interface AutodedupResidualRow extends AutodedupPairRow {
  lo: AutodedupMember;
  hi: AutodedupMember;
  why_not_merged: string;
  contributions: AutodedupContribution[];
  judgement: AutodedupJudgementRow | null;
  verdict: AutodedupVerdictRow | null;
  family_names: string[];
  block_key?: number | null;
}

/* The wire form: the two sides are `a`/`b`, the breakdown is `top_features` and
 * the judge summary is `judge`. Both spellings are accepted so the page does not
 * break on whichever one the server settles at. */
interface WireResidualRow extends Omit<AutodedupPairRow, 'families'> {
  families?: number | string[] | null;
  family_names?: string[] | null;
  block_key?: number | null;
  why_not_merged: string;
  a?: AutodedupMember;
  b?: AutodedupMember;
  lo?: AutodedupMember;
  hi?: AutodedupMember;
  top_features?: AutodedupContribution[] | null;
  contributions?: AutodedupContribution[] | null;
  judge?: AutodedupJudgementRow | null;
  judgement?: AutodedupJudgementRow | null;
  verdict?: AutodedupVerdictRow | null;
}

export function normalizeResidual(row: WireResidualRow): AutodedupResidualRow {
  const lo = row.lo ?? row.a;
  const hi = row.hi ?? row.b;
  if (!lo || !hi) throw new Error(`residual pair ${row.listing_lo}/${row.listing_hi} has no sides`);
  const families = row.family_names ?? row.families ?? null;
  return {
    ...row,
    lo,
    hi,
    /* Kept as the numeric mask when that is what arrived, so a caller that wants
     * the raw value still has it; the chips read `family_names`. */
    families: typeof row.families === 'number' ? row.families : 0,
    family_names: decodeFamilies(families),
    contributions: row.contributions ?? row.top_features ?? [],
    judgement: row.judgement ?? row.judge ?? null,
    verdict: row.verdict ?? null,
  };
}

/* The judge's own digest of a listing — PII-free by construction (no broker
 * field of any kind, description scrubbed). `attributes` is a label→text map
 * the extractor owns, so it is read as data and never keyed on here. */
export interface AutodedupDigest {
  listing_id: number;
  portal: string | null;
  deal: string | null;
  category: string | null;
  subtype: string | null;
  disposition: string | null;
  area_m2: number | null;
  floor: number | null;
  total_floors: number | null;
  price: number | null;
  price_unit: string | null;
  /* `_digest` does not emit a price trail today (the route builds its response
   * dict explicitly), so this is optional rather than promised-and-absent —
   * a required key the wire never sends is a render-time TypeError. */
  price_history?: Array<[string, number | null]> | null;
  attributes: Record<string, string | null>;
  first_seen: string | null;
  last_seen: string | null;
  active: boolean;
  description: string | null;
  description_truncated: boolean;
  absent: string[];
  /* The per-row portal URL captured at ingest — the digest DOES carry it, so
   * the pair page has a link even without a listing summary. */
  source_url?: string | null;
}

export interface AutodedupFeatureRow {
  name: string;
  value: number | null;
  present: boolean;
  contribution: number | null;
}

export interface AutodedupPairDetail {
  pair: AutodedupPairRow | null;
  listings: { lo: AutodedupMember | null; hi: AutodedupMember | null };
  digests: { lo: AutodedupDigest | null; hi: AutodedupDigest | null };
  images: { lo: AutodedupPairImage[]; hi: AutodedupPairImage[] };
  features: AutodedupFeatureRow[];
  judgements: AutodedupJudgementRow[];
  verdicts: AutodedupVerdictRow[];
  family_names: string[];
  /* The clustering pass this evidence was opened against, as the server
   * resolved it — a link that named none still lands on a real one. */
  generation: string | null;
}

interface WireSided<T> {
  lo?: T;
  hi?: T;
  a?: T;
  b?: T;
}

interface WirePairDetail {
  pair?: (AutodedupPairRow & { families?: number | string[] | null; family_names?: string[] }) | null;
  listings?: WireSided<AutodedupMember | null>;
  digests?: WireSided<AutodedupDigest | null>;
  /* The route also reports how many frames it showed vs hashed, alongside the
   * two sides; carried on the type so the literal payload checks. */
  images?: WireSided<WirePairImage[]> & {
    n_frames_shown?: Record<string, number>;
    n_hashed_frames?: Record<string, number>;
  };
  /* The route sends {name, value, present} only — the log-odds breakdown
   * arrives apart, in `top_features`, and is joined on the name below. */
  features?: Array<Omit<AutodedupFeatureRow, 'contribution'> & { contribution?: number | null }>;
  top_features?: AutodedupContribution[] | null;
  judgements?: AutodedupJudgementRow[];
  verdicts?: AutodedupVerdictRow[];
  generation?: string | null;
}

const side = <T,>(s: WireSided<T> | undefined, which: 'lo' | 'hi'): T | undefined =>
  which === 'lo' ? (s?.lo ?? s?.a) : (s?.hi ?? s?.b);

/* The route nests the per-frame answer as `best_match: {image_id, hamming} |
 * null` (a NULL phash has no distance to anything, so there is no match rather
 * than a fabricated zero). The UI wants it flat, and reading the nested shape
 * as a flat one silently prints "no match" on every photo — evidence AGAINST a
 * duplicate that nobody produced. Flattened once, here. */
interface WirePairImage extends AutodedupMemberImage {
  best_hamming?: number | null;
  best_match_image_id?: number | null;
  best_match?: { image_id: number | null; hamming: number | null } | null;
}

function flattenImages(images: WirePairImage[] | undefined): AutodedupPairImage[] {
  return (images ?? []).map((img) => ({
    ...img,
    best_hamming: img.best_hamming ?? img.best_match?.hamming ?? null,
    best_match_image_id: img.best_match_image_id ?? img.best_match?.image_id ?? null,
  }));
}

/* A digest is not a listing row, but it carries every attribute the diff table
 * compares — so when the payload has no listing summaries the diff is built from
 * the digests rather than dropped. Nothing is invented: what the digest does not
 * carry (the portal URL, the photo count) stays empty. */
function memberFromDigest(d: AutodedupDigest | null | undefined): AutodedupMember | null {
  if (!d) return null;
  return {
    listing_id: d.listing_id,
    source: d.portal ?? '',
    source_url: d.source_url ?? null,
    category_main: d.category,
    category_type: d.deal,
    disposition: d.disposition,
    area_m2: d.area_m2,
    floor: d.floor,
    total_floors: d.total_floors,
    price_czk: d.price,
    first_seen_at: d.first_seen,
    last_seen_at: d.last_seen,
    is_active: d.active,
    cover: null,
    n_images: 0,
  };
}

export function normalizePairDetail(raw: WirePairDetail): AutodedupPairDetail {
  const digests = {
    lo: side(raw.digests, 'lo') ?? null,
    hi: side(raw.digests, 'hi') ?? null,
  };
  /* The model's log-odds breakdown arrives apart from the feature list; joined
   * on the name here so the table has one row per feature, not two lists. */
  const byName = new Map((raw.top_features ?? []).map((c) => [c.name, c.contribution]));
  const features = (raw.features ?? []).map((f) => ({
    ...f,
    contribution: f.contribution ?? byName.get(f.name) ?? null,
  }));
  const pair = raw.pair ?? null;
  return {
    pair,
    listings: {
      lo: side(raw.listings, 'lo') ?? memberFromDigest(digests.lo),
      hi: side(raw.listings, 'hi') ?? memberFromDigest(digests.hi),
    },
    digests,
    images: {
      lo: flattenImages(side(raw.images, 'lo')),
      hi: flattenImages(side(raw.images, 'hi')),
    },
    features,
    judgements: raw.judgements ?? [],
    verdicts: raw.verdicts ?? [],
    family_names: decodeFamilies(pair?.family_names ?? pair?.families ?? null),
    generation: raw.generation ?? null,
  };
}

/* Keyset page. The cursor is opaque — `(min_edge_score, cluster_key)` for the
 * groups list, `(score, lo, hi)` for the residual one — so nothing here reads
 * it as a number. */
export interface AutodedupKeysetPage<T> {
  items: T[];
  has_more: boolean;
  next_after: string | null;
  /* How many rows the CURRENT filter selects, for "20 of N". The server counts
   * on the FIRST page only, so a continuation page carries null — "not counted
   * here", never zero; the page keeps the number the first read gave it. */
  total?: number | null;
  /* WHICH clustering pass the rows are from. The request may name none — "the
   * newest", resolved against the store — so the reply is the only place the
   * page learns which generation it is actually reviewing. */
  generation?: string | null;
}

export type AutodedupEnvelope<T> = { store_ready: boolean; data: T | null };

export interface AutodedupGroupFilters {
  generation?: string | null;
  after?: string | null;
  limit?: number | null;
  /* A block is a CODE AND A GRAIN. `block` is the RUIAN code (an INT server-side)
   * and `block_grain` says whose vocabulary it belongs to — `o` a town, `c` a
   * quarter (migration 529). They share one number space, so the code alone can
   * name two different blocks; a null grain means "either", which is what a link
   * written before the grain existed says. */
  block?: number | null;
  block_grain?: string | null;
  source?: string | null;
  category_main?: string | null;
  category_type?: string | null;
  min_size?: number | null;
  max_size?: number | null;
  min_score?: number | null;
  max_score?: number | null;
  verdict?: string | null;
  shared_photo?: 0 | 1 | null;
  has_judgement?: 0 | 1 | null;
  /* `random` is the SEEDED sample order (D6) — stable across pages and reloads
   * for one seed, and uncorrelated with anything the engine did, which is what
   * makes an error rate measured on it an error rate about the engine rather
   * than about the top of a working queue. */
  sort?: 'weakest' | 'newest' | 'largest' | 'random' | null;
  /* WHICH sample. Sent with `sort=random`; the server defaults it to `v1` and
   * echoes back the seed it drew, so a session can be resumed tomorrow. */
  seed?: string | null;
}

export interface AutodedupResidualFilters {
  generation?: string | null;
  after?: string | null;
  limit?: number | null;
  block?: number | null;
  block_grain?: string | null;
  zone?: AutodedupZone | null;
  min_score?: number | null;
  source_pair?: string | null;
  has_judgement?: 0 | 1 | null;
  verdict?: string | null;
  sort?: 'score_desc' | 'random' | null;
  seed?: string | null;
}

/* One block of a generation: the stored blocking key, the grain it was keyed at
 * (`o` = obec/town, `c` = cast obce/quarter — migration 529, and the two share
 * their number space) and the RUIAN name resolved off `listing_location`. The
 * name is NULL when the block's grain predates that migration: a code with no
 * grain cannot be named without guessing whose vocabulary it belongs to.
 *
 * `n_clusters` / `n_listings` are what this generation's clusters say about the
 * block. There is deliberately no pair count: `autodedup.pairs` carries no block
 * column, and a number read off the unwritten fingerprint table would be a
 * confident zero.
 *
 * The list is the busiest blocks first and CAPPED server-side — a select with
 * thousands of options is not a control. A block outside the cap still filters:
 * the picker keeps whatever key the URL arrived with. */
export interface AutodedupBlock {
  block_key: number;
  block_grain: string | null;
  name: string | null;
  n_clusters: number;
  n_listings: number;
}

/* Every clustering pass the store holds, newest first, with the current one
 * named. The validation views used to default to `g1` — the first hand-prior
 * pass, over-merging developer units, superseded twice — so the operator
 * reviewed proposals the live engine had already stopped making. The default is
 * now the server's answer and this is where the page learns which pass that is,
 * so it can say out loud when it is showing an older one. */
export const getAutodedupGenerations = async (): Promise<
  AutodedupEnvelope<{ items: AutodedupGenerationRollup[]; latest: string | null }>
> =>
  request<AutodedupEnvelope<{ items: AutodedupGenerationRollup[]; latest: string | null }>>(
    '/autodedup/generations',
    { jwt: true },
  );

/* The BLOCK filter's vocabulary. It replaces a free-text field for a numeric
 * RUIAN code, where a typed town name silently dropped the parameter and the
 * queue answered with the unfiltered list. */
export const getAutodedupBlocks = async (
  generation?: string | null,
): Promise<AutodedupEnvelope<{ items: AutodedupBlock[]; generation: string }>> =>
  request<AutodedupEnvelope<{ items: AutodedupBlock[]; generation: string }>>(
    '/autodedup/blocks',
    { query: { generation: generation ?? null }, jwt: true },
  );

/* Each read normalizes ONCE, here, so every page downstream sees one shape.
 * `store_ready: false` short-circuits with `data: null` — an un-migrated store
 * is an empty page, never an exception. */
export const getAutodedupGroups = async (
  f: AutodedupGroupFilters = {},
): Promise<AutodedupEnvelope<AutodedupKeysetPage<AutodedupGroup>>> => {
  const res = await request<AutodedupEnvelope<AutodedupKeysetPage<WireGroupItem>>>(
    '/autodedup/groups',
    { query: { ...f } as Record<string, QueryValue>, jwt: true },
  );
  if (!res.data) return { store_ready: res.store_ready, data: null };
  return {
    store_ready: res.store_ready,
    data: { ...res.data, items: (res.data.items ?? []).map(normalizeGroup) },
  };
};

export const getAutodedupGroup = async (
  clusterKey: number,
  generation?: string | null,
): Promise<AutodedupEnvelope<AutodedupGroupDetail>> => {
  const res = await request<
    AutodedupEnvelope<Omit<AutodedupGroupDetail, 'cluster'> & WireGroupItem>
  >(`/autodedup/groups/${encodeURIComponent(String(clusterKey))}`, {
    query: { generation: generation ?? null },
    jwt: true,
  });
  if (!res.data) return { store_ready: res.store_ready, data: null };
  return {
    store_ready: res.store_ready,
    data: {
      ...res.data,
      cluster: normalizeGroup(res.data),
      members: res.data.members ?? [],
      pairs: res.data.pairs ?? [],
      judgements: res.data.judgements ?? [],
      conflicts: res.data.conflicts ?? [],
      verdicts: res.data.verdicts ?? [],
    } as AutodedupGroupDetail,
  };
};

export const getAutodedupResidual = async (
  f: AutodedupResidualFilters = {},
): Promise<AutodedupEnvelope<AutodedupKeysetPage<AutodedupResidualRow>>> => {
  const res = await request<AutodedupEnvelope<AutodedupKeysetPage<WireResidualRow>>>(
    '/autodedup/residual',
    { query: { ...f } as Record<string, QueryValue>, jwt: true },
  );
  if (!res.data) return { store_ready: res.store_ready, data: null };
  return {
    store_ready: res.store_ready,
    data: { ...res.data, items: (res.data.items ?? []).map(normalizeResidual) },
  };
};

export const getAutodedupPair = async (
  lo: number,
  hi: number,
  generation?: string | null,
): Promise<AutodedupEnvelope<AutodedupPairDetail>> => {
  const res = await request<AutodedupEnvelope<WirePairDetail>>(
    `/autodedup/pair/${encodeURIComponent(String(lo))}/${encodeURIComponent(String(hi))}`,
    { query: { generation: generation ?? null }, jwt: true },
  );
  if (!res.data) return { store_ready: res.store_ready, data: null };
  return { store_ready: res.store_ready, data: normalizePairDetail(res.data) };
};

/* The one write of the whole program, and it writes into `autodedup` only. A
 * negative PAIR verdict also lands a permanent must-not-link server-side; a
 * CLUSTER verdict flags the group and splits nothing (pair-level splits are
 * made on the pair view). */
export interface AutodedupVerdictInput {
  kind: 'pair' | 'cluster';
  verdict: AutodedupVerdictValue;
  listing_lo?: number | null;
  listing_hi?: number | null;
  cluster_key?: number | null;
  /* REQUIRED on a cluster ruling, refused on a pair one (E58). A cluster key
   * names one set of adverts only inside one pass, so a ruling that does not
   * say which pass is a ruling about nothing; the SERVER then resolves the
   * member set for that (generation, cluster_key) and stamps it on the row. */
  generation?: string | null;
  note?: string | null;
  /* Reason CODES, never labels: the label is the registry's rendering of the
   * code and changing one must not change what a past verdict recorded. */
  reasons?: string[];
}

/* The route answers `{data: {verdict, must_not_link}}` — the stored row is
 * NESTED, and taking `data` verbatim hands the badge an object where it expects
 * a verdict string, so a SUCCESSFUL write un-presses the button it just set.
 * Unwrapped once, here; `must_not_link` rides alongside so a caller can say
 * that the pair is now permanently un-linkable. */
export interface AutodedupVerdictResult {
  verdict: AutodedupVerdictRow | null;
  must_not_link: boolean;
  /* Pairs whose operator veto this verdict dropped — a cluster `same` retracts
   * every one inside the group, which is a thing the operator should be told. */
  must_not_link_retracted?: number;
}

/* THE REASON VOCABULARY, SERVED (PROGRAM.md §9). The codes live in
 * `autodedup/verdict_reasons.py` and the SPA hard-codes NONE of them: a chip
 * list copied into the browser is a second vocabulary that drifts the first
 * time a review session names a shape. No `store_ready` — the registry is code,
 * so it answers against a database that has not been migrated at all. */
export interface AutodedupVerdictReason {
  code: string;
  label: string;
}

export const getAutodedupVerdictReasons = (): Promise<AutodedupVerdictReason[]> =>
  request<{ data: { reasons: AutodedupVerdictReason[] } }>('/autodedup/verdict-reasons', {
    jwt: true,
  }).then((res) => res.data?.reasons ?? []);

export const postAutodedupVerdict = async (
  body: AutodedupVerdictInput,
): Promise<
  AutodedupEnvelope<AutodedupVerdictRow> & {
    must_not_link: boolean;
    must_not_link_retracted?: number;
  }
> => {
  const res = await request<AutodedupEnvelope<AutodedupVerdictResult>>(
    '/autodedup/verdict',
    { method: 'POST', json: body, jwt: true },
  );
  return {
    store_ready: res.store_ready,
    data: res.data?.verdict ?? null,
    must_not_link: res.data?.must_not_link ?? false,
    must_not_link_retracted: res.data?.must_not_link_retracted ?? 0,
  };
};

/* THE UNIT SPLIT (E49). One proposed group, ruled unit by unit: every member
 * carries a unit label, members sharing a label are one property, and members
 * in different labels are `relation` — the same building, the same development
 * project, or unrelated — each pair of them taking a PERMANENT must-not-link.
 * The assignment must name every member of the cluster exactly once; the server
 * validates against cluster MEMBERSHIP, not against the scored pairs (a cluster
 * is a union of edges, so two members can share one with no edge between them). */
export interface AutodedupSplitUnit {
  listing_id: number;
  unit: string;
}

export type AutodedupSplitRelation =
  | 'same_building_different_unit'
  | 'same_project_different_unit'
  | 'different';

/* The relation between TWO units. One value for a whole split cannot describe
 * the group the operator meets — A and B two units of one BUILDING, C a
 * different building of the same development — and stamping either statement
 * onto the other pair records a building the adverts do not share. Both land as
 * permanent must-not-links and as calibration labels. */
export interface AutodedupSplitRelationEntry {
  unit_a: string;
  unit_b: string;
  relation: AutodedupSplitRelation;
}

export interface AutodedupSplitInput {
  cluster_key: number;
  generation: string;
  units: AutodedupSplitUnit[];
  /* The fill for any unit pair `relations` does not name. */
  relation: AutodedupSplitRelation;
  relations?: AutodedupSplitRelationEntry[];
  /* A split that drops a veto the operator wrote earlier is refused with a 409
   * until this says the operator meant it. */
  confirm_retract?: boolean;
  note?: string | null;
  /* ONE set for the whole split — it is one ruling — stamped on every pair row
   * it writes and on the cluster row. */
  reasons?: string[];
}

/* What the one write reports back: the stored CLUSTER verdict (`same` when the
 * operator used one unit, the relation otherwise) and the fan-out counts, so the
 * page can say "3 pairs separated" rather than "saved". */
export interface AutodedupSplitResult {
  cluster_verdict: AutodedupVerdictRow | null;
  n_pairs_same: number;
  n_pairs_negative: number;
  must_not_link_written: number;
  must_not_link_retracted: number;
  /* The pairs this save took back: the operator had ruled them negative and the
   * split re-ruled them as one unit. */
  reversed_pairs?: number[][];
}

export const postAutodedupSplitVerdict = (
  body: AutodedupSplitInput,
): Promise<AutodedupEnvelope<AutodedupSplitResult>> =>
  request<AutodedupEnvelope<AutodedupSplitResult>>('/autodedup/verdict/split', {
    method: 'POST',
    json: body,
    jwt: true,
  });

/* ---------------------------------------------------------------------------
 * CANDIDATE GROUPS (PROGRAM.md §12, E56) — the residual cohort, packed.
 *
 * WHY. The pair queue asks one question per PAIR, and the pairs of one
 * generation are not independent: one advert against each member of a merged
 * group is the same question five times. So the server lifts the residual pairs
 * to UNIT level — an existing cluster of that generation (all its members,
 * locked together) or a lone advert — and packs the units into cards of at most
 * eight adverts. Every residual pair lands in exactly one card, so nothing stops
 * being asked, and one save rules many pairs.
 *
 * THE CARD IS THE GROUPS CARD. Same member shape, same galleries, same unit
 * letters, same relation-per-unit-pair split. Two differences, both because the
 * engine did NOT merge these: the adverts of one already-merged group are locked
 * to one letter, and the letters start apart rather than all on A.
 * ------------------------------------------------------------------------- */

/* One thing the engine already treats as a single property. `cluster_key` is the
 * merged group it belongs to, or null for a lone advert — and it is what makes a
 * letter lockable on the card. */
export interface AutodedupCandidateUnit {
  unit_key: string;
  cluster_key: number | null;
  listing_ids: number[];
}

/* A card member is a queue member plus WHICH UNIT it belongs to. `unit_lock` is
 * the merged group's key or null; members sharing one must share a letter, and
 * the server refuses a save that separates them (that ruling belongs on the
 * Groups page, where it writes a cluster verdict). */
export interface AutodedupCandidateMember extends AutodedupMember {
  unit_key?: string;
  unit_lock?: number | null;
}

/* The header facts. NO judge artefact among them by construction: this surface
 * is blind by default (E55) and a chip that leaked the judge's word would defeat
 * it before the operator had said anything. */
export interface AutodedupCandidateHeader {
  candidate_key: string;
  generation: string;
  /* Adverts, not units — the number the card's own grid shows. */
  size: number;
  n_units: number;
  score_min: number | null;
  score_max: number | null;
  zones: Partial<Record<AutodedupZone, number>>;
  families: number;
  family_names: string[];
  block_key: number | null;
  block_grain: string | null;
  locked_cluster_keys: number[];
  units: AutodedupCandidateUnit[];
  /* REVIEWED IS DERIVED, never stored: a candidate group is not a row anywhere,
   * so the card is reviewed when every residual pair inside it carries an
   * operator pair verdict (by any operator — one operator, one platform). */
  n_pairs: number;
  n_pairs_reviewed: number;
  n_pairs_not_same: number;
  reviewed: boolean;
}

export interface AutodedupCandidate extends AutodedupCandidateHeader {
  sources: string[];
  members: AutodedupCandidateMember[];
  /* The operator's own rulings on the members' pairs, so the unit letters
   * hydrate after a reload rather than reading "all one unit" over a card that
   * was partitioned last week (E50). */
  member_verdicts: AutodedupVerdictRow[];
}

export interface AutodedupCandidatePair extends AutodedupPairRow {
  why_not_merged?: string;
  /* Whether this scored edge is one of the questions THIS card asks. An edge
   * inside a locked group is evidence here, never a question. */
  residual?: boolean;
}

export interface AutodedupCandidateDetail {
  candidate: AutodedupCandidateHeader;
  members: Array<AutodedupMemberDetail & { unit_key?: string; unit_lock?: number | null }>;
  pairs: AutodedupCandidatePair[];
  judgements: AutodedupJudgementRow[];
  member_verdicts: AutodedupVerdictRow[];
}

export interface AutodedupCandidateFilters {
  generation?: string | null;
  /* The cursor is the last card's KEY — the order is a total order over a
   * structure the server holds whole, so the page boundary is a name rather
   * than a tuple of sort values. */
  after?: string | null;
  limit?: number | null;
  block?: number | null;
  block_grain?: string | null;
  zone?: AutodedupZone | null;
  /* Two values only: this surface has no verdict of its own to filter on. */
  verdict?: 'reviewed' | 'unreviewed' | null;
  sort?: 'weakest' | 'strongest' | 'largest' | 'random' | null;
  seed?: string | null;
}

export const getAutodedupCandidates = async (
  f: AutodedupCandidateFilters = {},
): Promise<AutodedupEnvelope<AutodedupKeysetPage<AutodedupCandidate>>> =>
  request<AutodedupEnvelope<AutodedupKeysetPage<AutodedupCandidate>>>(
    '/autodedup/candidates',
    { query: { ...f } as Record<string, QueryValue>, jwt: true },
  );

export const getAutodedupCandidate = async (
  candidateKey: string,
  generation?: string | null,
): Promise<AutodedupEnvelope<AutodedupCandidateDetail>> =>
  request<AutodedupEnvelope<AutodedupCandidateDetail>>(
    `/autodedup/candidates/${encodeURIComponent(candidateKey)}`,
    { query: { generation: generation ?? null }, jwt: true },
  );

/* THE CANDIDATE SPLIT. The cluster split's body minus the cluster: the same unit
 * assignment, the same relation per unit pair, the same 409 when it would take
 * back a veto the operator wrote earlier.
 *
 * It carries NO `reasons`. A split stamps its reason chips on the cluster row,
 * and there is no cluster row here — stamping them on the pairwise fan-out
 * instead would post one reason row per pair from a single click, so the §9
 * histogram would measure card size rather than what the operator saw. The
 * server answers 400 rather than dropping them silently; the note stays. */
export interface AutodedupCandidateSplitInput {
  candidate_key: string;
  generation: string;
  units: AutodedupSplitUnit[];
  relation: AutodedupSplitRelation;
  relations?: AutodedupSplitRelationEntry[];
  confirm_retract?: boolean;
  note?: string | null;
}

export interface AutodedupCandidateSplitResult extends AutodedupSplitResult {
  candidate_key?: string;
  /* Pairs inside one already-merged group: the Groups page's ruling, untouched. */
  n_pairs_locked?: number;
}

export const postAutodedupCandidateSplitVerdict = (
  body: AutodedupCandidateSplitInput,
): Promise<AutodedupEnvelope<AutodedupCandidateSplitResult>> =>
  request<AutodedupEnvelope<AutodedupCandidateSplitResult>>(
    '/autodedup/verdict/candidate-split',
    { method: 'POST', json: body, jwt: true },
  );

/* HOW FAR THROUGH THE VALIDATION SESSION (D6). Two counts at one grain: the
 * whole generation, and the first `sample_size` of the seeded random order —
 * the draw the program's gate is measured on. `n_not_same` counts every ruling
 * that is not "one property" (including `unsure`), which on the groups queue is
 * the engine's error count on an unbiased sample.
 *
 * The sample is NOT narrowed by the filter bar: a sample that moved with the
 * filters would mean a different thing on every page of one session. */
export interface AutodedupValidationCounts {
  n: number;
  n_reviewed: number;
  n_not_same: number;
}

export type AutodedupSurface = 'groups' | 'residual' | 'candidates';

export interface AutodedupValidationProgress {
  generation: string | null;
  surface: AutodedupSurface;
  seed: string;
  sample_size: number;
  /* `cluster` on the groups queue, `pair` on the residual one, `candidate` on
   * the candidate-group view — three different units of work, never added
   * together on a page. A candidate card is reviewed when every residual pair
   * inside it is, so its counter is deliberately not the pair counter. */
  grain: 'cluster' | 'pair' | 'candidate';
  sample: AutodedupValidationCounts;
  total: AutodedupValidationCounts;
}

export const getAutodedupValidationProgress = (q: {
  surface: AutodedupSurface;
  generation?: string | null;
  seed?: string | null;
  min_score?: number | null;
}): Promise<AutodedupEnvelope<AutodedupValidationProgress>> =>
  request<AutodedupEnvelope<AutodedupValidationProgress>>('/autodedup/validation-progress', {
    query: {
      surface: q.surface,
      generation: q.generation ?? null,
      seed: q.seed ?? null,
      min_score: q.min_score ?? null,
    },
    jwt: true,
  });

/* OPERATOR vs JUDGE (D6, the gate that can stop the program). One binary
 * question — one property, or not — asked of both, over the pairs where both
 * have spoken. `ci_low`/`ci_high` are a Wilson 95% interval computed
 * server-side; `agreement` is null when nothing is comparable yet, which is a
 * different statement from zero. */
export interface AutodedupAgreementStats {
  n: number;
  n_agree: number;
  agreement: number | null;
  ci_low: number | null;
  ci_high: number | null;
  /* The two directions, never summed: an engine the operator over-ruled and a
   * judge that under-calls duplicates are different failures. */
  n_judge_different_operator_same: number;
  n_judge_same_operator_different: number;
  /* Where the operator's label came from: a verdict on THAT pair, or a pair
   * implied by a confirmed group. */
  n_explicit: number;
  n_implied: number;
}

export interface AutodedupAgreementTier extends AutodedupAgreementStats {
  tier: string;
}

export interface AutodedupDisagreement {
  listing_lo: number;
  listing_hi: number;
  operator_verdict: AutodedupVerdictValue;
  operator_source: 'explicit' | 'implied';
  judge_verdict: AutodedupJudgementRow['verdict'];
  judge_tier: string;
  judge_model: string | null;
}

export interface AutodedupAgreement {
  generation: string | null;
  overall: AutodedupAgreementStats;
  tiers: AutodedupAgreementTier[];
  n_insufficient_evidence: number;
  insufficient_by_tier: Record<string, number>;
  disagreements: AutodedupDisagreement[];
  n_disagreements: number;
  /* The bar this number is measured against, served rather than hard-coded, so
   * the page and the program document cannot drift apart. */
  gate: { tier: string; bar: number; target_n: number };
  max_cluster_size: number;
  n_clusters_over_cap: number;
}

export const getAutodedupAgreement = (
  generation?: string | null,
): Promise<AutodedupEnvelope<AutodedupAgreement>> =>
  request<AutodedupEnvelope<AutodedupAgreement>>('/autodedup/agreement', {
    query: { generation: generation ?? null },
    jwt: true,
  });
