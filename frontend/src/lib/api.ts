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
    const detail =
      body && typeof body === 'object' && body !== null && 'detail' in body
        ? String((body as { detail: unknown }).detail)
        : res.statusText || `HTTP ${res.status}`;
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
  flags: { priority?: boolean; ready_for_training?: boolean },
): Promise<{ data: NewDedupTag }> =>
  request<{ data: NewDedupTag }>(`/new-dedup/labeling/taxonomy/${tagId}/flags`, {
    method: 'PATCH',
    json: flags,
    jwt: true,
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
    json: { image_id: imageId, state, excluded_reason: excludedReason ?? null },
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
}

export interface ExamState {
  cohort: { name: string; sealed: boolean };
  /* The eight routing tags, server-resolved. Never a copy in the client: the
   * buttons must be the same set the answers are written against. */
  tags: ExamTag[];
  progress: { total: number; answered: number; remaining: number };
  /* Null when every image has a verdict on every tag. Deliberately carries NO
   * machine suggestion — a pre-ticked answer would anchor the operator, and an
   * exam the machine helped answer cannot grade the machine. */
  question: ExamQuestion | null;
}

export const getExamState = (cohort: string): Promise<{ data: ExamState }> =>
  request<{ data: ExamState }>(`/new-dedup/labeling/exam/${cohort}`, { jwt: true });

/* Practice images from OUTSIDE the exam — they settle the hand without spending
 * real exam images, and answers for them are refused server-side. */
export const getExamWarmup = (
  cohort: string, limit = 10,
): Promise<{ data: Array<{ image_id: number; storage_path: string | null }> }> =>
  request<{ data: Array<{ image_id: number; storage_path: string | null }> }>(
    `/new-dedup/labeling/exam/${cohort}/warmup?limit=${limit}`, { jwt: true },
  );

/* Every routing tag in NEITHER list becomes a NEGATIVE — that is what lets one
 * answer measure precision and recall at once. skipped_tag_ids is the brief's
 * per-tag leave-out (excluded/'pruned'): subject clearly present, photo of
 * something else; the cell trains nothing and grades nothing. */
export const answerExamQuestion = (
  cohort: string,
  body: { image_id: number; picked_tag_ids: number[]; skipped_tag_ids: number[]; cant_tell: boolean },
): Promise<{ data: { image_id: number; cells_written: number } }> =>
  request<{ data: { image_id: number; cells_written: number } }>(
    `/new-dedup/labeling/exam/${cohort}/answer`,
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
