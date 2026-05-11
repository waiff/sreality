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
  CreateEstimationIn,
  EstimationListParams,
  EstimationListResponse,
  EstimationRun,
  ListingSummaryBatchRow,
  ParseResult,
  PreviewResponse,
  SourceKind,
} from './types';

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

// TODO(estimation-5 Part B): delete `previewListing` + the
// `fetchEstimationPreview` re-export in lib/queries.ts once
// UrlScrapeStep.tsx is migrated to previewListingUrl + useUrlPreview.
// The new POST /estimations/preview routes sreality through the same
// dispatcher, so the legacy GET endpoint becomes dead code at that point.
export const previewListing = (url: string): Promise<PreviewResponse> =>
  request<PreviewResponse>('/estimations/preview', { query: { url } });

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

export const listEstimations = (
  params: EstimationListParams = {},
): Promise<EstimationListResponse> =>
  request<EstimationListResponse>('/estimations', {
    query: params as Record<string, QueryValue>,
  });

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

/* ----- admin / Settings page --------------------------------------------
 *
 * The /admin/* prefix is exempted from the API_TOKEN bearer gate per
 * CLAUDE.md rule #8 (same exemption category as /health). The private
 * Railway URL is the security perimeter for these routes.
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

export const listSkills = (): Promise<{ data: Skill[] }> =>
  request<{ data: Skill[] }>('/admin/skills');

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

/* Download the skill row as a SKILL.md document (Anthropic skill
 * folder convention). Returns the raw body as a Blob so the caller
 * can trigger a browser download. */
export const exportSkill = async (name: string): Promise<Blob> => {
  if (!BASE_URL) {
    throw new ApiError('API base URL is not configured', 0, null);
  }
  const res = await fetch(
    `${BASE_URL}/admin/skills/${encodeURIComponent(name)}/export`,
    { headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {} },
  );
  if (!res.ok) {
    const text = await res.text();
    throw new ApiError(text || `HTTP ${res.status}`, res.status, text);
  }
  return res.blob();
};

/* Upload a SKILL.md (or zip containing one) and overwrite the matching
 * row (or auto-create if the name is new). Returns the resulting Skill. */
export const importSkill = async (file: File): Promise<Skill> => {
  if (!BASE_URL) {
    throw new ApiError('API base URL is not configured', 0, null);
  }
  const form = new FormData();
  form.append('file', file);
  const res = await fetch(`${BASE_URL}/admin/skills/import`, {
    method: 'POST',
    headers: TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {},
    body: form,
  });
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
  return body as Skill;
};

export const listAppSettings = (): Promise<{ data: AppSetting[] }> =>
  request<{ data: AppSetting[] }>('/admin/app_settings');

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
