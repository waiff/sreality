/* Wire shapes mirroring the public views from migration 008. Only the columns
 * the UI actually reads are typed here; expand as Parts B–E need more. */

export type Disposition =
  | '1+kk' | '1+1'
  | '2+kk' | '2+1'
  | '3+kk' | '3+1'
  | '4+kk' | '4+1'
  | '5+kk' | '5+1';

/* Promoted from raw_json via migration 022. See parser.FURNISHED /
 * parser.OWNERSHIP for the int→text mapping. NULL when sreality didn't
 * report a value. */
export type Furnished = 'ano' | 'ne' | 'castecne';
export type Ownership = 'osobni' | 'druzstevni' | 'statni';

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
  /* Migration 022 — category-relevant fields. Older rows that haven't
   * been re-fetched since the migration may have null for any of
   * these even when the legacy columns are populated. */
  estate_area: number | null;
  usable_area: number | null;
  garden_area: number | null;
  category_sub_cb: number | null;
  furnished: Furnished | null;
  terrace: boolean | null;
  cellar: boolean | null;
  garage: boolean | null;
  parking_lots: number | null;
  ownership: Ownership | null;
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

export interface Ppm2Box {
  n: number;
  min: number;
  p25: number;
  median: number;
  p75: number;
  max: number;
}

export interface RegionDispositionRow {
  disposition: string;
  n: number;
  median_price: number | null;
  median_ppm2: number | null;
  median_area: number | null;
  ppm2_box: Ppm2Box | null;
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

export interface HealthCategoryBlock {
  category_main: 'byt' | 'dum' | 'komercni' | string;
  category_type: 'pronajem' | 'prodej' | string;
  active_now: number;
  flipped_inactive_7d: number;
  new_per_day_14d: HealthDayCount[];
  flipped_per_day_7d: HealthDayCount[];
  failures_total: number;
  failures_given_up: number;
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
  by_category: HealthCategoryBlock[];
}

/* -------------------------------------------------------------------------- */
/* Estimations (U2). Wire shapes mirror api/schemas.py and api/estimation_runs */
/* — backend is authoritative.                                                */
/* -------------------------------------------------------------------------- */

export type EstimationStatus = 'pending' | 'running' | 'success' | 'failed';
export type EstimationSource = 'ui' | 'api' | 'clickup';
export type EstimationMode = 'deterministic' | 'agent';
export type EstimationProvider = 'anthropic' | 'gemini';
export type Population = 'active' | 'delisted' | 'all';
/* Result confidence (estimate_yield) only ever returns the first three;
 * parse confidence (URL parser) can additionally be 'best_effort'. The
 * widened union covers both call sites. */
export type Confidence = 'low' | 'medium' | 'high' | 'best_effort';

/* Mirrors the CHECK constraint on estimation_runs.source_kind (migration 020). */
export type SourceKind =
  | 'sreality'
  | 'bezrealitky'
  | 'idnes_reality'
  | 'remax'
  | 'unsupported';

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

/* Output of toolkit/summaries.py:summarize_listing — the five legacy
 * fields plus the three sections added in migration 031. Older cached
 * rows may be missing the new fields; renderers must tolerate
 * absence and show "—" rather than crashing. */
export interface ListingSummaryBody {
  headline: string;
  key_highlights: string[];
  concerns: string[];
  condition_assessment: 'excellent' | 'good' | 'average' | 'poor' | 'unknown' | string;
  target_audience:
    | 'family' | 'couple' | 'single_professional'
    | 'investor' | 'student' | 'general' | string;
  location_summary?: string | null;
  building_summary?: string | null;
  apartment_summary?: string | null;
}

/* Persisted on estimation_runs.subject_summary by the backend after a
 * successful run. Carries the snapshot id so the UI can deep-link to
 * the exact snapshot the summary was generated against. */
export interface SubjectSummary {
  snapshot_id: number | null;
  summary: ListingSummaryBody;
}

/* POST /listings/summaries response shape — one row per requested
 * (sreality_id, snapshot_id) pair. Per-item failures surface as
 * `summary: null` + `error: <reason>`; a single bad id never fails
 * the whole request. */
export interface ListingSummaryBatchRow {
  sreality_id: number;
  snapshot_id: number | null;
  summary: ListingSummaryBody | null;
  error: string | null;
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
  /* Discriminator added in migration 029. Null on legacy rows
   * predating the migration — readers should treat null as 'rent'. */
  estimate_kind: 'rent' | 'sale' | null;
  input_purchase_price_czk: number | null;
  estimated_monthly_rent_czk: number | null;
  rent_p25_czk: number | null;
  rent_p75_czk: number | null;
  estimated_sale_price_czk: number | null;
  sale_p25_czk: number | null;
  sale_p75_czk: number | null;
  gross_yield_pct: number | null;
  confidence: Confidence | null;
  comparables_used: ComparableUsed[] | null;
  trace: Trace | null;
  warnings: string[] | null;
  error_message: string | null;
  parent_run_id: number | null;
  rerun_reason: string | null;
  /* Estimation-4 audit fields (added in migration 020). All null for
   * runs created before estimation-4 shipped, all null for spec-mode
   * runs (no URL parse happened), and parse_confidence_per_field is
   * also null for sreality runs (the deterministic parser doesn't
   * track per-field confidences). source_html is the raw page bytes
   * (LLM path only) and is large — only fetched on the detail page. */
  source_kind: SourceKind | null;
  parse_confidence: Confidence | null;
  parse_confidence_per_field: Record<string, Confidence> | null;
  source_html: string | null;
  /* Sum of llm_calls.cost_usd rows linked to this run via
   * estimation_run_id. The backend uses COALESCE(..., 0) so the
   * value is never null in practice, but the type tolerates it for
   * forward compatibility. */
  cost_usd_total: number | null;
  /* Migration 031 — structured Czech-real-estate summary of the
   * subject listing, generated server-side at estimation time when
   * input_sreality_id was set. Null on legacy runs and on runs
   * where no listing was resolved (spec-only inputs, parse failures). */
  subject_summary: SubjectSummary | null;
  /* Migration 034 — first-class agent provenance. Both null on
   * deterministic runs and on pre-slice-2 agent runs that crashed
   * before logging the summary line (backfill couldn't recover
   * them). Renderers should treat null as "unknown" rather than
   * crashing or hiding the column entirely. */
  provider: EstimationProvider | null;
  skill_name: string | null;
}

/* Filter half of the POST /estimations body — mirrors ComparableFilters
 * via api/schemas.CreateEstimationIn. Only fields the UI actually exposes.
 *
 * The five cohort-search knobs (radius_m, area_band_pct,
 * disposition_match, max_age_days, active_only) intentionally do NOT
 * appear here: the agent decides them per-iteration and deterministic
 * runs use the backend's built-in defaults. */
export interface EstimationFilters {
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
  category_sub_cb: number | null;
  locality_district_id: number | null;
  locality_region_id: number | null;
  include_unreliable: boolean;
  furnished: Furnished | null;
  terrace: boolean | null;
  cellar: boolean | null;
  garage: boolean | null;
  ownership: Ownership | null;
  min_estate_area: number | null;
  max_estate_area: number | null;
  min_usable_area: number | null;
  max_usable_area: number | null;
  min_parking_lots: number | null;
}

export interface CreateEstimationIn extends Partial<EstimationFilters> {
  source?: EstimationSource;
  mode?: EstimationMode;
  provider?: EstimationProvider;
  skill?: string;
  population?: Population;
  estimate_kind?: 'rent' | 'sale';
  url?: string;
  spec?: TargetSpecIn;
  spec_overrides?: Partial<TargetSpecIn>;
  purchase_price_czk?: number | null;
  expected_monthly_rent_czk?: number | null;
  parent_run_id?: number | null;
  rerun_reason?: string | null;
}

export interface EstimationListParams {
  source?: EstimationSource;
  status?: EstimationStatus;
  sreality_id?: number;
  source_kind?: SourceKind;
  limit?: number;
  offset?: number;
}

export interface EstimationListResponse {
  data: EstimationRun[];
  total: number;
  limit: number;
  offset: number;
}

/* POST /estimations/preview response (estimation-4). Routes any URL
 * through the source-kind dispatcher: sreality fast-path or LLM-driven
 * per-source parser. The `spec` block is the same TargetSpecIn shape
 * that gets POSTed back to /estimations. The `listing` block is the
 * informational sidecar. Provenance fields document where the data
 * came from and how confident the parser was. */
export interface ParseListing {
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
  /* Migration 022 — same fields as PreviewListing. The LLM-driven
   * per-source parsers don't yet extract these, so they're null for
   * non-sreality URLs until the per-source prompts learn them. */
  estate_area: number | null;
  usable_area: number | null;
  garden_area: number | null;
  category_sub_cb: number | null;
  furnished: Furnished | null;
  terrace: boolean | null;
  cellar: boolean | null;
  garage: boolean | null;
  parking_lots: number | null;
  ownership: Ownership | null;
}

export interface ParseResult {
  spec: TargetSpecIn;
  listing: ParseListing;
  source_kind: SourceKind;
  source_url: string;
  /* ISO-8601 timestamp. For sreality, this is when the API was queried;
   * for LLM-parsed sources, this is the cache row's parsed_at on a hit
   * or "now" on a fresh parse. May be null for sreality if the upstream
   * scraper didn't record a fetched_at. */
  fetched_at: string | null;
  parse_confidence: Confidence;
  parse_confidence_per_field: Record<string, Confidence> | null;
  warnings: string[];
  from_cache: boolean;
  cost_usd: number | null;
  sreality_id: number | null;
}

/* -------------------------------------------------------------------------- */
/* Curation (U2.6). Wire shapes mirror api/curation.py and migrations 022-025. */
/* -------------------------------------------------------------------------- */

/* Mirrors the 024 CHECK constraint + api/schemas.TagColor. Adding a new
 * colour requires a migration + a globals.css token + a bump here. */
export type TagColor =
  | 'copper' | 'sage' | 'brick' | 'ochre'
  | 'slate'  | 'plum' | 'teal'  | 'sand';

export const TAG_COLORS: ReadonlyArray<TagColor> = [
  'copper', 'sage', 'brick', 'ochre',
  'slate',  'plum', 'teal',  'sand',
];

export interface Collection {
  id: number;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
  listing_count: number;
}

export interface Tag {
  id: number;
  name: string;
  color: TagColor;
  created_at: string;
  listing_count: number;
}

export interface Note {
  id: number;
  sreality_id: number;
  body: string;
  created_at: string;
}

/* GET /collections/{id} embeds a slimmer listing projection than
 * ListingPublic — see api/curation.get_collection. Renderers expand
 * with fetchListingsByIds() when more detail is needed. */
export interface CollectionListingRow {
  sreality_id: number;
  district: string | null;
  disposition: string | null;
  area_m2: number | null;
  price_czk: number | null;
  last_seen_at: string;
  is_active: boolean;
  added_at: string;
}

export interface CollectionWithListings {
  collection: Collection;
  listings: CollectionListingRow[];
}
