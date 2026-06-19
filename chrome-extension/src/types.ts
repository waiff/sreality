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
  gross_yield_pct: number | null;
  confidence: 'high' | 'medium' | 'low' | null;
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

/* Deal-pipeline membership for the listing's property (rule #22). Null when the
 * listing has no property yet (not in our DB, or the ~5-min straggler-attach
 * lag). `in_pipeline` is the bookmark state; stage_* describe the current stage
 * when bookmarked. */
export interface PipelineMembership {
  in_pipeline: boolean;
  stage_key: string | null;
  stage_label: string | null;
}

/* Response of POST /pipeline/cards (add) and DELETE /pipeline/cards/{id}
 * (remove). Add returns the card (incl. its entry-stage label); remove returns
 * `{removed}`. We only read the few fields the toggle reflects. */
export interface PipelineCardResult {
  property_id?: number;
  stage_key?: string | null;
  stage_label?: string | null;
  added?: boolean;
  removed?: boolean;
}

/* One entry from POST /listings/lookup — our scraped facts for a portal
 * listing keyed by (source, native id), including the precomputed MF
 * reference rent + "Výnos MF" gross yield (the same figures Browse cards
 * show), a handle on any existing successful estimation, and the property's
 * deal-pipeline membership. */
export interface PortalListing {
  source: string;
  source_id: string;
  found: boolean;
  /* App-wide listing identity (negative for non-sreality portals); the SPA
   * page is /listing/{sreality_id}. Null when not in our DB (no app page). */
  sreality_id: number | null;
  /* The grouping property (the pipeline + dedup grain). Null when not in our
   * DB or not yet attached to a property. */
  property_id: number | null;
  category_main: string | null;
  category_type: string | null;
  area_m2: number | null;
  price_czk: number | null;
  disposition: string | null;
  district: string | null;
  locality: string | null;
  is_active: boolean | null;
  last_seen_at: string | null;
  mf_reference_rent_czk: number | null;
  mf_gross_yield_pct: number | null;
  latest_estimation: {
    estimation_id: number;
    estimate_kind: 'rent' | 'sale' | null;
    gross_yield_pct: number | null;
  } | null;
  pipeline: PipelineMembership | null;
}

export interface PortalLookupResponse {
  data: PortalListing[];
}

export interface PortalLookupItem {
  source: string;
  source_id: string;
}

/* Message protocol between content script and background worker.
 * The background worker is the only context allowed to make API
 * fetches — host_permissions covers it without CORS getting in the
 * way. Content scripts post messages, get the typed result back. */
export type ApiMessage =
  | { type: 'lookup_listings'; items: PortalLookupItem[] }
  | { type: 'patch_scenario'; run_id: number; body: YieldScenarioUpdate }
  | { type: 'create_estimation'; url: string }
  | { type: 'get_estimation'; run_id: number }
  | { type: 'add_pipeline_card'; property_id: number }
  | { type: 'remove_pipeline_card'; property_id: number };

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
