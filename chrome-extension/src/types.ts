/* Subset of the SPA's EstimationRun shape — we only consume the
 * fields needed by the yield panel. Mirroring frontend/src/lib/types.ts
 * but kept local so the extension build doesn't reach across into the
 * SPA's path aliases. */

export interface YieldScenario {
  rent_czk: number | null;
  fond_per_m2_czk: number | null;
  price_czk: number | null;
  updated_at: string;
}

/* MF Cenová mapa secondary rent reference (migration 131). Null on sale
 * runs, territory misses, and runs predating a rent-map revision. */
export interface ReferenceRent {
  territory: { name: string; kraj: string | null };
  vk: number;
  is_novostavba: boolean;
  base_per_m2: number;
  total_per_m2: number;
  monthly_rent_czk: number;
  source_date: string | null;
}

export interface EstimationRun {
  id: number;
  status: 'pending' | 'running' | 'success' | 'failed';
  estimate_kind: 'rent' | 'sale' | null;
  input_sreality_id: number | null;
  input_purchase_price_czk: number | null;
  estimated_monthly_rent_czk: number | null;
  estimated_sale_price_czk: number | null;
  input_spec: { area_m2?: number | null } | null;
  scenario: YieldScenario | null;
  reference_rent: ReferenceRent | null;
  error_message: string | null;
}

export interface EstimationListResponse {
  data: EstimationRun[];
  total: number;
  limit: number;
  offset: number;
}

export interface YieldScenarioUpdate {
  rent_czk?: number | null;
  fond_per_m2_czk?: number | null;
  price_czk?: number | null;
}

/* Message protocol between content script and background worker.
 * The background worker is the only context allowed to make API
 * fetches — host_permissions covers it without CORS getting in the
 * way. Content scripts post messages, get the typed result back. */
export type ApiMessage =
  | { type: 'find_run_by_sreality_id'; sreality_id: number }
  | { type: 'patch_scenario'; run_id: number; body: YieldScenarioUpdate }
  | { type: 'create_estimation'; url: string }
  | { type: 'get_estimation'; run_id: number };

export interface ApiResponse<T> {
  ok: true;
  data: T;
}

export interface ApiError {
  ok: false;
  status: number;
  detail: string;
}

export type ApiResult<T> = ApiResponse<T> | ApiError;
