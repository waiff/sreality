/* Subset of the SPA's EstimationRun shape — we only consume the
 * fields needed by the yield panel. Mirroring frontend/src/lib/types.ts
 * but kept local so the extension build doesn't reach across into the
 * SPA's path aliases. */

export interface YieldScenario {
  rent_czk: number | null;
  fond_per_m2_czk: number | null;
  price_czk: number | null;
  /* Migration 213 — flat one-off renovation budget added to price_czk to form
   * the total acquisition cost (the yield denominator). Null/absent = none. */
  renovation_czk: number | null;
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
  renovation_czk?: number | null;
}

/* Deal-pipeline membership for the listing's property (rule #22). Null when the
 * listing has no property yet (not in our DB, or the ~5-min straggler-attach
 * lag). `in_pipeline` is the bookmark state; stage_* describe the current stage
 * when bookmarked. */
export interface PipelineMembership {
  in_pipeline: boolean;
  /* The current stage's id — the `<select>` value when changing stage. */
  stage_id: number | null;
  stage_key: string | null;
  stage_label: string | null;
}

/* One operator-curated pipeline stage (GET /pipeline/stages). Mirrors the SPA's
 * stage shape; the extension reads id + label to populate the stage `<select>`. */
export interface PipelineStage {
  id: number;
  key: string;
  label: string;
  position: number;
  color: string | null;
  is_terminal: boolean;
  is_entry: boolean;
}

/* Response of the pipeline-card writes: POST /pipeline/cards (add) and
 * PATCH /pipeline/cards/{id} (move) both return the card (incl. its stage);
 * DELETE returns `{removed}`. We only read the few fields the control reflects. */
export interface PipelineCardResult {
  property_id?: number;
  stage_id?: number;
  stage_key?: string | null;
  stage_label?: string | null;
  added?: boolean;
  removed?: boolean;
}

/* One operator-curated collection (GET /collections). Property-grain
 * (rule #18); `monitoring_enabled` marks it as a watchlist, `is_system` marks
 * the default "monitoring" collection. The panel reads these to pick a single
 * monitoring target for its one-click toggle. */
export interface ExtCollection {
  id: number;
  name: string;
  monitoring_enabled: boolean;
  is_system: boolean;
}

/* Response of the collection writes: POST /collections/{id}/properties (add)
 * returns `{added, skipped}`; DELETE /collections/{id}/properties/{property_id}
 * returns `{removed}`. We only read these to confirm the write landed. */
export interface CollectionWriteResult {
  added?: number;
  skipped?: number;
  removed?: boolean;
}

/* One operator note about a PROPERTY (rule #18). `origin_listing_id` is the
 * advert the operator was viewing when they wrote it — display provenance only,
 * not a grouping key. Read via GET /properties/{id}/notes (most-recent-first). */
export interface ExtNote {
  id: number;
  property_id: number;
  body: string;
  origin_listing_id: number | null;
  created_at: string;
}

/* One entry from POST /listings/lookup — our scraped facts for a portal
 * listing keyed by (source, native id), including the precomputed MF
 * reference rent + "Výnos MF" gross yield (the same figures Browse cards
 * show), a handle on any existing successful estimation, the property's
 * deal-pipeline membership, and its collection memberships. */
export interface PortalListing {
  source: string;
  source_id: string;
  found: boolean;
  /* LEGACY app-wide listing identity (negative for non-sreality portals).
   * NULL for a post-Gate-2 listing — use `found` to test DB membership and
   * `listing_id` to reference the listing in a write. The deep link uses the
   * canonical /listing/{source}/{source_id} natural-key route regardless. */
  sreality_id: number | null;
  /* Surrogate listing identity — present for every listing in our DB. */
  listing_id: number | null;
  /* The grouping property (the pipeline + dedup grain). Null when not in our
   * DB or not yet attached to a property. */
  property_id: number | null;
  category_main: string | null;
  category_type: string | null;
  area_m2: number | null;
  price_czk: number | null;
  disposition: string | null;
  /* Portal-agnostic property sub-type (migration 152): the meaningful "kind"
   * for commercial/houses where disposition is NULL. */
  subtype: string | null;
  /* The display "kind": the Czech subtype label (commercial/houses) else the
   * disposition (apartments). Computed server-side so the extension carries no
   * slug→label dictionary of its own. */
  kind_label: string | null;
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
  /* Collection memberships of the listing's property (rule #18). Null when the
   * listing has no property yet (same posture as `pipeline`). */
  collection_ids: number[] | null;
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
  | { type: 'remove_pipeline_card'; property_id: number }
  | { type: 'move_pipeline_card'; property_id: number; stage_id: number }
  | { type: 'list_pipeline_stages' }
  | { type: 'list_collections' }
  | { type: 'add_to_collection'; collection_id: number; property_id: number }
  | { type: 'remove_from_collection'; collection_id: number; property_id: number }
  | { type: 'list_notes'; property_id: number }
  | { type: 'add_note'; property_id: number; body: string; origin_listing_ref_id: number | null }
  | { type: 'sign_in' }
  | { type: 'sign_out' }
  | { type: 'get_auth_state' };

/* The extension's own Supabase session state (Wave 1) — read by the panel
 * to decide whether to show data or a "please sign in" prompt, and to show
 * the signed-in email + a sign-out control. */
export interface AuthState {
  signedIn: boolean;
  email: string | null;
}

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
