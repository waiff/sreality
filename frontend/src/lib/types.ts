/* Wire shapes mirroring the public views from migration 008. Only the columns
 * the UI actually reads are typed here; expand as Parts B–E need more. */

export type Disposition =
  | '1+kk' | '1+1'
  | '2+kk' | '2+1'
  | '3+kk' | '3+1'
  | '4+kk' | '4+1'
  | '5+kk' | '5+1';

export interface ListingPublic {
  sreality_id: number;
  first_seen_at: string;
  last_seen_at: string;
  is_active: boolean;
  category_main: number | null;
  category_type: number | null;
  price_czk: number | null;
  price_unit: string | null;
  area_m2: number | null;
  disposition: Disposition | null;
  locality: string | null;
  district: string | null;
  locality_district_id: number | null;
  locality_region_id: number | null;
  lat: number | null;
  lng: number | null;
  floor: number | null;
  total_floors: number | null;
  has_balcony: boolean | null;
  has_parking: boolean | null;
  has_lift: boolean | null;
  building_type: string | null;
  condition: string | null;
  energy_rating: string | null;
}

export interface ListingSnapshotPublic {
  id: number;
  sreality_id: number;
  scraped_at: string;
  price_czk: number | null;
}

export interface ListingFreshnessCheckPublic {
  id: number;
  sreality_id: number;
  checked_at: string;
  outcome: string;
}

export interface ListingFetchFailurePublic {
  sreality_id: number;
  attempts: number;
  first_failure_at: string;
  last_failure_at: string;
  given_up: boolean;
}

export interface ImagePublic {
  id: number;
  sreality_id: number;
  sequence: number | null;
  sreality_url: string;
  storage_path: string | null;
}

/* Region page payloads — shape mirrors migration 012 RPCs. */

export interface PercentileTriple {
  p25: number;
  p50: number;
  p75: number;
}

export interface RegionDispositionRow {
  disposition: string;
  n: number;
  median_price: number | null;
  median_ppm2: number | null;
  median_area: number | null;
}

export interface RegionStats {
  total_active: number;
  total_ever: number;
  last_new_first_seen: string | null;
  price: PercentileTriple | null;
  ppm2: PercentileTriple | null;
  dispositions: RegionDispositionRow[];
  tom_median_days: number | null;
  tom_n: number;
}

export interface ActiveByDayRow {
  day: string;
  active: number;
  new: number;
}

/* Health dashboard payload — shape mirrors migration 013 health_summary RPC. */

export interface HealthDayCount {
  day: string;
  n: number;
}

export interface HealthSnapBucket {
  bucket: '1' | '2' | '3' | '4+' | string;
  n: number;
}

export interface HealthFreshnessRow {
  outcome: string;
  n: number;
}

export interface HealthFailureRow {
  sreality_id: number;
  attempts: number;
  first_failure_at: string;
  last_failure_at: string | null;
  given_up: boolean;
}

export interface HealthSummary {
  last_scrape_at: string | null;
  active_now: number;
  active_7d_ago: number;
  flipped_inactive_7d: number;
  new_per_day_14d: HealthDayCount[];
  flipped_per_day_7d: HealthDayCount[];
  snapshot_density: HealthSnapBucket[];
  freshness_24h: HealthFreshnessRow[];
  failures_given_up: number;
  failures_total: number;
  failures_top10: HealthFailureRow[];
}

/* -------------------------------------------------------------------------- */
/* Estimations (U2). Wire shapes mirror api/schemas.py and api/estimation_runs */
/* — backend is authoritative.                                                */
/* -------------------------------------------------------------------------- */

export type EstimationStatus = 'pending' | 'running' | 'success' | 'failed';
export type EstimationSource = 'ui' | 'api' | 'clickup';
export type EstimationMode = 'deterministic' | 'agent';
export type Confidence = 'low' | 'medium' | 'high';
export type DispositionMatch = 'exact' | 'loose' | 'any';

export interface TargetSpecIn {
  lat: number;
  lng: number;
  area_m2: number | null;
  disposition: Disposition | null;
  floor: number | null;
  exclude_ids: number[];
}

export interface ComparableUsed {
  sreality_id: number;
  snapshot_id: number | null;
  snapshot_date: string | null;
  data_age_days: number | null;
  verified_during_estimate: boolean;
}

export type TraceStepKind = 'tool_call' | 'computation' | 'reasoning';

interface TraceStepBase {
  n: number;
  kind: TraceStepKind;
  started_at: string;
  duration_ms: number;
  output_summary: Record<string, unknown>;
}

export interface TraceStepToolCall extends TraceStepBase {
  kind: 'tool_call';
  tool: string;
  input: Record<string, unknown>;
}

export interface TraceStepComputation extends TraceStepBase {
  kind: 'computation';
  label: string;
}

/* Reserved for U4 — shape locked at the agent layer, treated opaquely here. */
export interface TraceStepReasoning extends TraceStepBase {
  kind: 'reasoning';
}

export type TraceStep =
  | TraceStepToolCall
  | TraceStepComputation
  | TraceStepReasoning;

export interface Trace {
  version: number;
  summary: string;
  steps: TraceStep[];
}

export interface EstimationRun {
  id: number;
  created_at: string;
  source: EstimationSource;
  mode: EstimationMode;
  status: EstimationStatus;
  input_url: string | null;
  input_sreality_id: number | null;
  input_spec: TargetSpecIn | null;
  input_purchase_price_czk: number | null;
  estimated_monthly_rent_czk: number | null;
  rent_p25_czk: number | null;
  rent_p75_czk: number | null;
  gross_yield_pct: number | null;
  confidence: Confidence | null;
  comparables_used: ComparableUsed[] | null;
  trace: Trace | null;
  warnings: string[] | null;
  error_message: string | null;
  parent_run_id: number | null;
  rerun_reason: string | null;
}

/* Filter half of the POST /estimations body — mirrors ComparableFilters
 * via api/schemas.CreateEstimationIn. Only fields the UI actually exposes. */
export interface EstimationFilters {
  radius_m: number;
  area_band_pct: number;
  disposition_match: DispositionMatch;
  max_age_days: number;
  active_only: boolean;
  floor_band: number | null;
  condition_match: string[] | null;
  building_type_match: string[] | null;
  energy_rating_match: string[] | null;
  has_balcony: boolean | null;
  has_lift: boolean | null;
  has_parking: boolean | null;
  min_price_czk: number | null;
  max_price_czk: number | null;
  category_main: string | null;
  category_type: string | null;
  locality_district_id: number | null;
  locality_region_id: number | null;
  include_unreliable: boolean;
}

export interface CreateEstimationIn extends Partial<EstimationFilters> {
  source?: EstimationSource;
  mode?: EstimationMode;
  url?: string;
  spec?: TargetSpecIn;
  spec_overrides?: Partial<TargetSpecIn>;
  purchase_price_czk?: number | null;
  parent_run_id?: number | null;
  rerun_reason?: string | null;
}

export interface EstimationListParams {
  source?: EstimationSource;
  status?: EstimationStatus;
  sreality_id?: number;
  limit?: number;
  offset?: number;
}

export interface EstimationListResponse {
  data: EstimationRun[];
  total: number;
  limit: number;
  offset: number;
}

/* GET /estimations/preview response. The `spec` field is shaped so it
 * can be POSTed back to /estimations as `spec` verbatim. The `listing`
 * block is informational + drives filter pre-fill. */
export interface PreviewListing {
  price_czk: number | null;
  price_unit: string | null;
  category_main: string | null;
  category_type: string | null;
  locality: string | null;
  district: string | null;
  locality_district_id: number | null;
  locality_region_id: number | null;
  total_floors: number | null;
  has_balcony: boolean | null;
  has_lift: boolean | null;
  has_parking: boolean | null;
  building_type: string | null;
  condition: string | null;
  energy_rating: string | null;
  image_count: number;
}

export interface PreviewResponse {
  url: string;
  sreality_id: number;
  in_database: boolean;
  fetched_at: string;
  spec: TargetSpecIn;
  listing: PreviewListing;
}
