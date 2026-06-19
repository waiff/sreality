/* Wire shapes mirroring the public views from migration 008. Only the columns
 * the UI actually reads are typed here; expand as Parts B–E need more. */

import type { DistrictChip, ListingFilters, PresetSpec } from './filters';

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

/* Migration 134 — MF Cenová mapa reference-rent formula breakdown stored on
 * sale apartments (listings.mf_reference_rent). */
export interface MfReferenceRentAdjustment {
  attribute: string;
  czk_per_m2: number;
}

export interface MfReferenceRent {
  territory: {
    ruian_code: number;
    level: 'ku' | 'obec';
    name: string;
    kraj: string | null;
  };
  vk: number;
  is_novostavba: boolean;
  source_revision: number;
  base_per_m2: number;
  adjustments: MfReferenceRentAdjustment[];
  adjustments_sum_per_m2: number;
  total_per_m2: number;
  area_m2: number;
  monthly_rent_czk: number;
}

export interface ListingPublic {
  sreality_id: number;
  first_seen_at: string;
  last_seen_at: string;
  is_active: boolean;
  /* Migration 091 — source portal (sreality, bazos, idnes, …). */
  source: string;
  category_main: string | null;
  category_type: string | null;
  price_czk: number | null;
  price_unit: string | null;
  area_m2: number | null;
  disposition: Disposition | null;
  locality: string | null;
  district: string | null;
  obec: string | null;
  okres: string | null;
  street: string | null;
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
  /* Migration 052 — "turned in" (TOM) in whole days. now() -
   * first_seen_at for active listings (right-censored, growing);
   * last_seen_at - first_seen_at for delisted. */
  tom_days: number | null;
  /* Migration 084 — original sreality "Popis" free-text description.
   * Promoted from raw_json->'text'->>'value' to a typed column +
   * exposed on listings_public. May be null for older rows that
   * haven't been re-fetched and had no description in their last
   * snapshot. */
  description: string | null;
  /* Migration 133/134 — MF Cenová mapa secondary rent reference (sale
   * apartments only; null otherwise). `_czk` is the monthly reference rent,
   * `_pct` the gross yield, and `mf_reference_rent` the formula breakdown
   * behind both. */
  mf_reference_rent_czk: number | null;
  mf_gross_yield_pct: number | null;
  mf_reference_rent: MfReferenceRent | null;
}

export interface ListingSnapshotPublic {
  id: number;
  sreality_id: number;
  scraped_at: string;
  price_czk: number | null;
  /* Migration 084 — projected from listing_snapshots.raw_json so the
   * HistoryBlock can flag description changes between snapshots
   * without us materialising another typed column on the history
   * table. Null on snapshots whose raw_json lacks a text.value. */
  description: string | null;
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

/* Distributional shapes — used by EstimationDetail's RangeStrip and by
 * the per-disposition Kč/m² box plots on Browse > Stats. */

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
  // When the pg_cron loop last refreshed the Health matviews (migration 176).
  // Absent on payloads generated before that migration.
  generated_at?: string | null;
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

/* Per-category source-scoped health (migrations 118/119): category_trends RPC.
   One trend point per index run — "portal" = portal-reported total, "db" = our
   active count. The rest are source-scoped per-category aggregates. */

export interface CategoryTrendPoint {
  t: string;
  portal: number | null;
  db: number | null;
}

export interface CategoryTrend {
  category_main: string;
  category_type: string;
  total_in_db: number;
  active_now: number;
  new_today: number;
  new_7d: number;
  flipped_today: number;
  flipped_7d: number;
  failures_total: number;
  failures_given_up: number;
  portal_total: number | null;
  collected: number | null;
  hourly: CategoryTrendPoint[];
  daily: CategoryTrendPoint[];
}

/* Per-scrape stats (migration 086): scrape_runs table + recent_scrape_runs RPC. */

export interface ScrapeRunCategory {
  category_main: string | null;
  category_type: string | null;
  listings_found_new: number;
  listings_scraped_new: number;
  listings_inactive: number;
  images_discovered: number;
  images_stored: number;
  // Reconciliation (recorded by the region-split scraper; absent on older runs).
  sreality_result_size?: number | null;
  collected?: number | null;
  active_db?: number | null;
}

export interface ScrapeRun {
  id: number;
  started_at: string;
  ended_at: string | null;
  // 'full'/'delta' are the legacy monolithic scraper; 'index'/'detail' are the
  // Phase-2 cadence split (migration 105).
  run_type: 'full' | 'delta' | 'index' | 'detail';
  index_pages: number;
  listings_found_new: number;
  listings_scraped_new: number;
  listings_updated: number;
  listings_inactive: number;
  images_discovered: number;
  images_stored: number;
  errors: number;
  by_category: ScrapeRunCategory[];
  // Which portal this run belongs to (migration 100). Defaults to 'sreality'
  // on rows recorded before the column existed.
  source: string;
}

/* Per-portal health (migration 100): the portals registry joined with each
 * source's activity. `kind` decides which metrics are meaningful — scrapers
 * report listing + scrape-run stats, on-demand URL parsers report parse
 * activity. Both metric families are always present (zeroed for the other
 * kind) so the dashboard renders uniformly. */

export type PortalKind = 'scraper' | 'parser';
export type PortalStage = 'live' | 'pilot' | 'on_demand' | 'planned';

export interface PortalHealth {
  source: string;
  label: string;
  kind: PortalKind;
  stage: PortalStage;
  home_url: string | null;
  listings_total: number;
  listings_active: number;
  listings_active_7d: number;
  parses_total: number;
  parses_30d: number;
  last_scrape_at: string | null;
  runs_7d: number;
  scraped_new_7d: number;
  inactive_7d: number;
  errors_7d: number;
  last_parsed_at: string | null;
  last_activity_at: string | null;
}

export interface ImageStorageCategory {
  category_main: string | null;
  category_type: string | null;
  total: number;
  stored: number;
  // Active-listing subset (migration 109): the *closeable* gap — inactive
  // listings' CDN photos are mostly expired and unrecoverable.
  total_active: number;
  stored_active: number;
}

export interface ImageStorageOverview {
  total_images: number;
  stored_images: number;
  total_active_images: number;
  stored_active_images: number;
  by_category: ImageStorageCategory[];
}

/* Image-download failure rollup (migration 177): one row per
 * (source, bucket, detail). `detail` is the unavailable_reason or coarse
 * last_error class ('HTTP 404', 'other'); '' when not applicable. */

export type ImageFailureBucket = 'stored' | 'unavailable' | 'exhausted' | 'pending';

export interface ImageFailureRow {
  source: string;
  bucket: ImageFailureBucket;
  detail: string;
  n: number;
}

/* -------------------------------------------------------------------------- */
/* Estimations (U2). Wire shapes mirror api/schemas.py and api/estimation_runs */
/* — backend is authoritative.                                                */
/* -------------------------------------------------------------------------- */

export type EstimationStatus = 'pending' | 'running' | 'success' | 'failed';
export type EstimationSource = 'ui' | 'api' | 'clickup';
export type EstimationMode = 'deterministic' | 'agent';
export type EstimationProvider = 'anthropic' | 'gemini';
export type Lifecycle = 'active' | 'delisted' | 'all';
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
  /* Migration 048 — per-comparable inclusion reason emitted by the
   * agent in record_estimate.comparable_decisions. Null on
   * deterministic runs and on agent runs predating migration 048. */
  reason?: string | null;
}

/* Migration 048 — listings the agent considered and chose not to
 * include in the cohort. Mirrors comparables_used but with only the
 * id + a one-sentence reason. Null when the agent didn't emit
 * comparable_decisions (legacy rows or deterministic runs). */
export interface ComparableExcluded {
  sreality_id: number;
  reason: string;
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

export interface YieldScenario {
  rent_czk: number | null;
  fond_per_m2_czk: number | null;
  price_czk: number | null;
  /* ISO-8601 UTC string written by the API on each PATCH. */
  updated_at: string;
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
  /* Migration 048 — agent's per-listing decision log for candidates
   * not included in comparables_used. Null on deterministic runs and
   * on agent runs predating the migration. */
  comparables_excluded: ComparableExcluded[] | null;
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
   * (LLM path only) and is large — GET /estimations list rows omit it;
   * only GET /estimations/:id returns it. */
  source_kind: SourceKind | null;
  parse_confidence: Confidence | null;
  parse_confidence_per_field: Record<string, Confidence> | null;
  source_html?: string | null;
  /* Migration 138 — typed subject attributes (mirroring listings_public field
   * names) for a parsed subject with no resolved listings row, so the UI can
   * render its facts grid. NULL when input_sreality_id is set (read the
   * listings row instead). */
  subject_attributes: Record<string, unknown> | null;
  /* Sum of llm_calls.cost_usd rows linked to this run via
   * estimation_run_id. The backend uses COALESCE(..., 0) so the
   * value is never null in practice, but the type tolerates it for
   * forward compatibility. */
  cost_usd_total: number | null;
  /* Migration 052 — snapshot of the skill that produced this run.
   * Null on deterministic runs and on pre-AI agent runs. Survives
   * later edits to the live skill; the /estimations list reads
   * these directly rather than re-parsing trace.steps. */
  skill_name: string | null;
  skill_version: number | null;
  /* Migration 052 — true when at least one estimation_feedback row
   * references this run. Drives the "Feedback" button enable state
   * on the /estimations list. */
  has_feedback: boolean;
  /* Migration 042 — operator-supplied free-text inputs. Both null on
   * runs created before the column existed and on runs the operator
   * didn't fill in. Immutable on a terminal run — editing them is
   * a re-run. */
  special_instructions: string | null;
  contextual_text: string | null;
  /* Migration 085 — operator-tunable yield scenario shared between
   * the SPA's YieldBlock and the Chrome extension's yield panel.
   * Null means "no overrides yet — render defaults" (estimated rent,
   * 10 CZK/m² fond, subject sale price). Mutated through PATCH
   * /estimations/:id/scenario, latest-wins. */
  scenario: YieldScenario | null;
  /* Migration 131 — secondary rent reference from the MF "Cenová mapa
   * nájemného". Populated only on rent runs whose subject resolved to a
   * mapped territory; null on sale runs, territory misses, and runs
   * created before a rent-map revision was ingested. Read-only — never
   * overrides the comparables-based primary estimate. */
  reference_rent: ReferenceRent | null;
  /* Server-derived display string emitted only by GET /estimations:
   * listings.district for sreality runs, else the latest
   * parsed_url_cache extraction.locality.value for the run's input_url.
   * Null when neither path resolves. Not returned by GET /estimations/:id. */
  locality_display?: string | null;
}

/* Compact latest-rent-estimate summary per listing, served by
 * GET /estimations/latest-by-listing for the Browse cards' on-card estimate
 * chip. Distinct from the card's statistical mf_gross_yield_pct — this is the
 * result of an actual estimation run. */
export interface ListingEstimate {
  sreality_id: number;
  run_id: number;
  status: EstimationStatus;
  estimate_kind: 'rent' | 'sale' | null;
  gross_yield_pct: number | null;
  estimated_monthly_rent_czk: number | null;
  created_at: string | null;
}

/* Migration 131 — the MF Cenová mapa secondary rent reference breakdown
 * stored on estimation_runs.reference_rent. */
export interface ReferenceRentAdjustment {
  attribute: string;
  czk_per_m2: number;
}

export interface ReferenceRent {
  territory: {
    ruian_code: number;
    level: 'ku' | 'obec';
    name: string;
    kraj: string | null;
  };
  vk: number;
  is_novostavba: boolean;
  source_revision: number;
  source_date: string | null;
  base_per_m2: number;
  adjustments: ReferenceRentAdjustment[];
  total_per_m2: number;
  area_m2: number;
  monthly_rent_czk: number;
}

/* Phase AI slice B — one row per operator feedback submission on
 * an estimation run. Status walks the lifecycle in migration 049:
 * submitted -> refining -> proposed -> applied | dismissed | failed.
 * `refinement_id` is null until slice C's refiner produces a draft. */
export type FeedbackStatus =
  | 'submitted'
  | 'refining'
  | 'proposed'
  | 'applied'
  | 'dismissed'
  | 'failed';

export interface EstimationFeedback {
  id: number;
  estimation_run_id: number;
  feedback_text: string;
  submitted_at: string;
  status: FeedbackStatus;
  refinement_id: number | null;
}

/* Phase AI slice C — refiner-proposed prompt edit pending operator
 * approval. The diff between `original_prompt` and `proposed_prompt`
 * is what gets rendered for review. */
export type RefinementStatus = 'proposed' | 'applied' | 'dismissed';

export interface SkillRefinement {
  id: number;
  skill_name: string;
  original_prompt: string;
  proposed_prompt: string;
  refiner_explanation: string;
  source_feedback_id: number;
  status: RefinementStatus;
  created_at: string;
  applied_at: string | null;
}

/* Filter half of the POST /estimations body — mirrors ComparableFilters
 * via api/schemas.CreateEstimationIn. Only fields the UI actually exposes.
 *
 * The cohort-search knobs (radius_m, area_band_pct, disposition_match,
 * max_age_days) intentionally do NOT appear here: the agent decides them
 * per-iteration and deterministic runs use the backend's built-in
 * defaults. `lifecycle` rides on CreateEstimationIn directly. */
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
  min_price_per_m2: number | null;
  max_price_per_m2: number | null;
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
  building_condition_level_min: number | null;
  apartment_condition_level_min: number | null;
}

export interface CreateEstimationIn extends Partial<EstimationFilters> {
  source?: EstimationSource;
  mode?: EstimationMode;
  provider?: EstimationProvider;
  skill?: string;
  lifecycle?: Lifecycle;
  estimate_kind?: 'rent' | 'sale';
  url?: string;
  spec?: TargetSpecIn;
  /* Estimate an already-scraped listing by internal id (the Browse cards'
   * on-card estimate). Exactly one of url / spec / sreality_id is set. */
  sreality_id?: number;
  spec_overrides?: Partial<TargetSpecIn>;
  purchase_price_czk?: number | null;
  expected_monthly_rent_czk?: number | null;
  parent_run_id?: number | null;
  rerun_reason?: string | null;
  /* Migration 042 — operator-supplied free-text inputs persisted on the
   * row and appended into the agent's first user message inside fenced
   * <operator_instructions> / <contextual_text> sections. */
  special_instructions?: string | null;
  contextual_text?: string | null;
}

export interface EstimationListParams {
  source?: EstimationSource;
  status?: EstimationStatus;
  sreality_id?: number;
  /* CSV of listing ids — the property-grain fetch the Listing Detail
   * estimations section uses (every run on any of the property's
   * child listings). */
  sreality_ids?: string;
  source_kind?: SourceKind;
  limit?: number;
  offset?: number;
  /* Keyset cursor (the prior page's next_cursor). Newest-first feed paged
   * on (created_at, id) — dup/skip-free under live inserts. */
  cursor?: string;
}

export interface EstimationListResponse {
  data: EstimationRun[];
  /* Cohort total — present on the first page only (null on cursor'd pages). */
  total: number | null;
  limit: number;
  offset: number;
  next_cursor: string | null;
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
  property_id: number;
  body: string;
  /* The advert the operator was viewing when the note was written, kept for
   * provenance. Null on notes created outside a specific listing context. */
  origin_listing_id: number | null;
  created_at: string;
}

/* Deal pipeline (migration 205). A property is in the pipeline iff it has a
 * card; "bookmarked / interested" is the entry stage. Read from
 * property_pipeline_public; single-valued (one card per property). */
export interface PipelineCard {
  property_id: number;
  stage_key: string;
  stage_label: string;
  stage_color: TagColor | null;
  is_terminal: boolean;
  stage_position: number;
}

/* The kanban columns (pipeline_stages_public, operator-curated). */
export interface PipelineStage {
  id: number;
  key: string;
  label: string;
  position: number;
  color: TagColor | null;
  is_terminal: boolean;
  is_entry: boolean;
}

/* The canonical (resolved) broker for a card, with the contact the hover box
 * shows. broker_id links to the broker page. NULL when the listing has no
 * resolved broker (e.g. private bazos sellers). */
export interface PipelineCardBroker {
  broker_id: number;
  display_name: string | null;
  firm_label: string | null;
  email: string | null;
  phone: string | null;
}

/* A board card = the property_pipeline row joined to its property's display
 * fields (from properties_public) for rendering on the kanban. */
export interface PipelineBoardCard {
  property_id: number;
  stage_id: number;
  board_position: number;
  entered_stage_at: string;
  sreality_id: number | null;
  street: string | null;
  district: string | null;
  disposition: string | null;
  area_m2: number | null;
  price_czk: number | null;
  mf_gross_yield_pct: number | null;
  image_url: string | null;
  broker: PipelineCardBroker | null;
}

/* GET /collections/{id} embeds a slimmer property projection than
 * ListingPublic — see api/curation.get_collection. Each row carries the
 * property_id plus its representative listing's sreality_id (for
 * /listing/{sreality_id} links). Renderers expand with fetchListingsByIds()
 * when more detail is needed. */
export interface CollectionPropertyRow {
  property_id: number;
  sreality_id: number;
  district: string | null;
  disposition: string | null;
  area_m2: number | null;
  price_czk: number | null;
  last_seen_at: string;
  is_active: boolean;
  added_at: string;
}

export interface CollectionWithProperties {
  collection: Collection;
  properties: CollectionPropertyRow[];
}

/* --------------------------------------------------------------------
 * Buildings (Phase B0)
 *
 * Type stubs for the building-decomposition flow. B0 ships persistence
 * + read endpoints only; B1 adds the URL ingest + the agent extractor
 * and a `/building/:id` page that consumes these types.
 * ------------------------------------------------------------------ */

export type BuildingStatus =
  | 'pending'
  | 'extracting'
  | 'awaiting_input'
  | 'estimating'
  | 'success'
  | 'failed';

export type BuildingUnitSource =
  | 'description'
  | 'floor_plan'
  | 'both'
  | 'user_added';

export interface BuildingUnit {
  unit_id: string;
  label: string | null;
  floor: string | null;
  area_m2: number | null;
  disposition: string | null;
  condition: string | null;
  is_potential: boolean;
  source: BuildingUnitSource | null;
  notes: string | null;
}

/* What the extractor returns (B1). Stored as-is on
 * building_runs.units_proposal; the operator-confirmed array lands on
 * building_runs.units. */
export interface BuildingSummary {
  floor_count: number | null;
  has_attic: boolean | null;
  year_built: number | null;
  construction_type:
    | 'cihla' | 'panel' | 'skelet' | 'drevostavba' | 'smiseny' | 'unknown'
    | null;
  total_area_m2: number | null;
  condition:
    | 'novostavba' | 'po_rekonstrukci' | 'velmi_dobry' | 'dobry'
    | 'pred_rekonstrukci' | 'k_demolici' | 'unknown'
    | null;
  notes: string | null;
}

export interface BuildingUnitsProposal {
  units: BuildingUnit[];
  building: BuildingSummary;
  confidence: 'high' | 'medium' | 'low';
  warnings: string[];
  n_images: number;
  model: string;
  cost_usd: number | null;
  snapshot_id: number;
}

/* Embedded child-estimation projection on GET /buildings/{id}.
 * Slimmer than EstimationRun — full detail lives at /estimation/:id. */
export interface BuildingChildRun {
  id: number;
  created_at: string;
  status: EstimationStatus;
  estimate_kind: 'rent' | 'sale' | null;
  building_unit_id: string | null;
  estimated_monthly_rent_czk: number | null;
  rent_p25_czk: number | null;
  rent_p75_czk: number | null;
  estimated_sale_price_czk: number | null;
  sale_p25_czk: number | null;
  sale_p75_czk: number | null;
  confidence: Confidence | null;
  error_message: string | null;
}

export interface BuildingRun {
  id: number;
  created_at: string;
  source: EstimationSource;
  status: BuildingStatus;
  input_url: string | null;
  input_sreality_id: number | null;
  input_spec: TargetSpecIn | null;
  source_kind: SourceKind | null;
  parse_confidence: Confidence | null;
  parse_confidence_per_field: Record<string, Confidence> | null;
  source_html: string | null;
  /* Building-flow display payload ({ source_url, fields, building }) — a
   * different shape from the (removed) estimation subject summary; the
   * BuildingDetail SubjectBlock casts it to the fields it reads. */
  subject_summary: Record<string, unknown> | null;
  units_proposal: BuildingUnitsProposal | null;
  units: BuildingUnit[] | null;
  total_rent_p25_czk: number | null;
  total_rent_p50_czk: number | null;
  total_rent_p75_czk: number | null;
  total_sale_p25_czk: number | null;
  total_sale_p50_czk: number | null;
  total_sale_p75_czk: number | null;
  /* Phase B3 — operator-tunable spreadsheet inputs + cached outputs. */
  business_case: Record<string, unknown> | null;
  warnings: string[] | null;
  error_message: string | null;
  /* Migration 042 — operator-supplied free-text inputs on the building
   * row. The unit extractor consumes them directly in its vision payload;
   * per-unit child estimations inherit them via the future B2 orchestrator. */
  special_instructions: string | null;
  contextual_text: string | null;
  /* Only present on GET /buildings/{id}, not on the list endpoint. */
  children?: BuildingChildRun[];
  /* Migration 042 — operator-uploaded images (photos / floor plans /
   * technical drawings). Only present on the detail endpoint. */
  attachments?: BuildingAttachment[];
}

/* Migration 042 — one operator-uploaded image attached to a building_run. */
export interface BuildingAttachment {
  id: number;
  building_run_id: number;
  filename: string;
  mime_type: 'image/png' | 'image/jpeg' | 'image/webp';
  byte_size: number;
  width_px: number | null;
  height_px: number | null;
  storage_key: string;
  sha256_hex: string;
  uploaded_by: EstimationSource | null;
  created_at: string;
}

export interface CreateBuildingIn {
  source: EstimationSource;
  input_url?: string | null;
}

export interface CreateBuildingFromUrlIn {
  source: EstimationSource;
  url: string;
  force_refresh?: boolean;
  special_instructions?: string | null;
  contextual_text?: string | null;
}

export interface UpdateBuildingInputsIn {
  special_instructions?: string | null;
  contextual_text?: string | null;
}

export interface ConfirmBuildingUnitsIn {
  units: BuildingUnit[];
}

export interface BuildingListResponse {
  data: BuildingRun[];
  total: number;
  limit: number;
  offset: number;
}

/* -------------------------------------------------------------------------- */
/* Watchdog / new-listing notifications (Phase U2.7).                         */
/*                                                                            */
/* Subscriptions are saved filter specs; the backend matcher writes one       */
/* dispatch per (subscription, listing) match. Wire shapes mirror             */
/* api/notifications.py + migrations 056 / 057.                               */
/* -------------------------------------------------------------------------- */

export interface WatchdogFilterSpec {
  category_main: string | null;
  category_type: string | null;
  category_sub_cb: number | null;
  // Portal-agnostic property sub-type (multi-select; matches any).
  subtype: string[] | null;
  dispositions: string[] | null;
  lat: number | null;
  lng: number | null;
  radius_m: number | null;
  locality_district_id: number | null;
  locality_region_id: number | null;
  /* Each chip is `{name, context}` — context narrows the name match
   * to a parent municipality so the watchdog matcher (api/
   * notifications.py) doesn't fire on streets of the same name in
   * other cities. See migration 075 for the one-shot lift of legacy
   * `string[]` entries to this shape. */
  districts: DistrictChip[] | null;
  min_price_czk: number | null;
  max_price_czk: number | null;
  // Price per m² bounds (price_czk / NULLIF(area_m2, 0)).
  min_price_per_m2: number | null;
  max_price_per_m2: number | null;
  // MF gross rental yield % bounds (migration 133). Sale apartments only;
  // the matcher honours these (api/notifications.py _build_match_clauses).
  min_mf_gross_yield_pct: number | null;
  max_mf_gross_yield_pct: number | null;
  min_area_m2: number | null;
  max_area_m2: number | null;
  min_usable_area: number | null;
  max_usable_area: number | null;
  min_estate_area: number | null;
  max_estate_area: number | null;
  has_balcony: boolean | null;
  has_lift: boolean | null;
  has_parking: boolean | null;
  terrace: boolean | null;
  cellar: boolean | null;
  garage: boolean | null;
  // Multi-select enums; the '__unknown__' element matches NULL / non-canonical.
  furnished: string[] | null;
  ownership: string[] | null;
  // Raw sreality condition enum (Stav objektu). Matches l.condition = ANY(...);
  // null / empty = any. Honoured by the matcher (_build_match_clauses).
  condition_match: string[] | null;
  // Source portals (migration 091). A listing matches if its source is
  // in the list; null / empty = all portals.
  portals: string[] | null;
  min_parking_lots: number | null;
  // Derived condition scores (migrations 072 / 073). Same NULL-excluding
  // `>= N` / `<= N` semantics as the Browse filter.
  building_condition_level_min: number | null;
  building_condition_level_max: number | null;
  apartment_condition_level_min: number | null;
  apartment_condition_level_max: number | null;
  // Price-history aggregates (migration 173). Property grain; the matcher
  // reads them off properties_public. The window (30/90/365, null = all
  // time) picks which precomputed count column the count min reads;
  // total_price_change_pct is signed (negative = total drop of at least
  // that much, positive = total rise).
  price_change_count_min: number | null;
  price_change_window_days: 30 | 90 | 365 | null;
  total_price_change_pct: number | null;
  // Added with migration 060 / PR 2: backend now honours these,
  // matching the Browse sidebar filter set. The Watchdog form
  // surfaces them in a later PR — until then, API callers can set
  // them directly through POST/PUT /notifications/subscriptions.
  building_material: Array<'cihla' | 'panel' | 'smisena' | 'ostatni'> | null;
  min_garden_area: number | null;
  max_garden_area: number | null;
  tags: number[] | null;
  /* Phase QUAL — curated-city quality predicates. Mirrors the
   * Python `WatchdogFilterSpec` fields in `api/notifications.py`.
   * Same wire shape as Browse's `ListingFilters.cityIndexRules`. */
  city_index_rules: Array<{
    index_name: string;
    op?: '>=' | '<=' | '==' | '!=' | '>' | '<';
    value: number;
  }> | null;
  min_city_population: number | null;
  max_city_population: number | null;
  near_city_proximity: {
    index_rules: Array<{
      index_name: string;
      op?: '>=' | '<=' | '==' | '!=' | '>' | '<';
      value: number;
    }>;
    population_min: number | null;
    radius_km: number;
  } | null;
  /* Fast polygon-edge proximity (migration 142). Precomputed-column
   * predicates the matcher reads off properties_public — `>= value`. */
  near_pop_5km_min: number | null;
  near_pop_15km_min: number | null;
  near_jobs_5km_min: number | null;
  near_jobs_15km_min: number | null;
  near_youth_5km_min: number | null;
  near_youth_15km_min: number | null;
  near_overall_5km_min: number | null;
  near_overall_15km_min: number | null;
}

export const DEFAULT_WATCHDOG_FILTER_SPEC: WatchdogFilterSpec = {
  category_main: 'byt',
  category_type: 'pronajem',
  category_sub_cb: null,
  subtype: null,
  dispositions: null,
  lat: null,
  lng: null,
  radius_m: null,
  locality_district_id: null,
  locality_region_id: null,
  districts: null,
  min_price_czk: null,
  max_price_czk: null,
  min_price_per_m2: null,
  max_price_per_m2: null,
  min_mf_gross_yield_pct: null,
  max_mf_gross_yield_pct: null,
  min_area_m2: null,
  max_area_m2: null,
  min_usable_area: null,
  max_usable_area: null,
  min_estate_area: null,
  max_estate_area: null,
  has_balcony: null,
  has_lift: null,
  has_parking: null,
  terrace: null,
  cellar: null,
  garage: null,
  furnished: null,
  ownership: null,
  condition_match: null,
  portals: null,
  min_parking_lots: null,
  building_condition_level_min: null,
  building_condition_level_max: null,
  apartment_condition_level_min: null,
  apartment_condition_level_max: null,
  price_change_count_min: null,
  price_change_window_days: null,
  total_price_change_pct: null,
  building_material: [],
  min_garden_area: null,
  max_garden_area: null,
  tags: null,
  city_index_rules: null,
  min_city_population: null,
  max_city_population: null,
  near_city_proximity: null,
  near_pop_5km_min: null,
  near_pop_15km_min: null,
  near_jobs_5km_min: null,
  near_jobs_15km_min: null,
  near_youth_5km_min: null,
  near_youth_15km_min: null,
  near_overall_5km_min: null,
  near_overall_15km_min: null,
};

export interface WatchdogSubscription {
  id: string;
  name: string;
  filter_spec: WatchdogFilterSpec;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  dispatch_count: number;
}

/* Saved Browse filter preset (filter_presets table, migration 151).
 * Unlike a Watchdog, a preset never fires — it just restores the view
 * client-side, so `filter_spec` is an opaque blob to the API: the current
 * `{ filters, sort }` shape (PresetSpec), or the legacy bare ListingFilters
 * for presets saved before sort was captured. Read it via `readPresetSpec`. */
export interface FilterPreset {
  id: string;
  name: string;
  filter_spec: PresetSpec | ListingFilters;
  created_at: string;
  updated_at: string;
  /* Operator-controlled display order (ascending, 0 = first). The list
   * endpoint already returns rows in this order, so the UI relies on array
   * order; this field is the canonical source it persists via reorder. */
  position: number;
  /* Optional chip colour from the shared tag palette; null = neutral default. */
  color: TagColor | null;
}

export interface WatchdogDispatch {
  id: string;
  subscription_id: string;
  subscription_name: string;
  sreality_id: number;
  /* Property grain (Slice 2b). `property_id` is the canonical property;
   * `change_kind` is why this dispatch fired ('new' = newly matched the
   * filter, 'price_drop' = a recent price decrease). `sreality_id` is the
   * property's representative listing (what the feed links to). */
  property_id: number | null;
  change_kind: string;
  dispatched_at: string;
  seen_at: string | null;
  estimation_run_id: number | null;
  estimation_status: EstimationStatus | null;
  estimation_kind: 'rent' | 'sale' | null;
  estimated_monthly_rent_czk: number | null;
  estimated_sale_price_czk: number | null;
  gross_yield_pct: number | null;
  confidence: Confidence | null;
  /* Listing-side fields joined via the matcher's LEFT JOIN. Null when
   * the listing has been hard-deleted (shouldn't happen — architectural
   * rule #3 forbids deletes — but render defensively). */
  category_main: string | null;
  category_type: string | null;
  price_czk: number | null;
  price_unit: string | null;
  area_m2: number | null;
  disposition: Disposition | null;
  locality: string | null;
  district: string | null;
  is_active: boolean | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  /* MF (Ministry of Finance) reference gross rental yield % — the
   * deterministic Cenová-mapa figure carried on the listing (migration 133).
   * Sale apartments only; null on rentals / non-apartments / no territory.
   * Shown alongside the comparables-based estimation yield. */
  mf_gross_yield_pct: number | null;
  /* The portal the property was last seen on (`listings.source`, e.g.
   * 'sreality') + the listing's URL on that portal (`source_url`, may be null
   * for older sreality rows). Drives the Portal column's clickable chip. */
  source: string | null;
  source_url: string | null;
}

export interface WatchdogDispatchesResponse {
  data: WatchdogDispatch[];
  /* Cohort total — present on the first page only (null on cursor'd pages). */
  total: number | null;
  limit: number;
  offset: number;
  next_cursor: string | null;
}

export type WatchdogSeenFilter = 'all' | 'seen' | 'unseen';

/* Manual rental estimates (Phase U-ME).
 * Operator-recorded point-estimate rentals attached to a listing.
 * Read path: manual_rental_estimates_public view via anon key. */

export type ManualEstimateSourceKind =
  | 'broker'
  | 'gut'
  | 'external_comp'
  | 'portfolio'
  | 'other';

export const MANUAL_ESTIMATE_SOURCE_KINDS: ReadonlyArray<ManualEstimateSourceKind> = [
  'broker',
  'gut',
  'external_comp',
  'portfolio',
  'other',
];

export const manualEstimateSourceLabel = (kind: ManualEstimateSourceKind): string => {
  switch (kind) {
    case 'broker':        return 'Broker';
    case 'gut':           return 'Gut';
    case 'external_comp': return 'External comp';
    case 'portfolio':     return 'Portfolio';
    case 'other':         return 'Other';
  }
};

export interface ManualRentalEstimate {
  id: number;
  sreality_id: number;
  rent_czk: number;
  author: string;
  source_kind: ManualEstimateSourceKind;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface CreateManualEstimateIn {
  rent_czk: number;
  author: string;
  source_kind: ManualEstimateSourceKind;
  notes?: string | null;
  updated_by?: string | null;
}

export interface UpdateManualEstimateIn {
  rent_czk?: number;
  author?: string;
  source_kind?: ManualEstimateSourceKind;
  notes?: string | null;
  updated_by?: string | null;
}

// scraper_health_checks() RPC (migration 088)
export type HealthCheckStatus = 'pass' | 'warn' | 'fail';

export interface ScraperHealthCheck {
  key: string;
  label: string;
  status: HealthCheckStatus;
  value: string;
  detail: string;
}

export interface ScraperHealthChecks {
  generated_at: string;
  source?: string;
  checks: ScraperHealthCheck[];
}

/* ----- Cross-source dedup review (multi-portal PR3b) --------------------- */

export interface DedupPropertySide {
  property_id: number;
  status: string;
  sreality_id: number | null;
  price_czk: number | null;
  area_m2: number | null;
  disposition: string | null;
  district: string | null;
  category_main: string | null;
  category_type: string | null;
  source_count: number | null;
  distinct_site_count: number | null;
  first_seen_at: string | null;
  lat: number | null;
  lng: number | null;
}

export interface DedupCandidate {
  id: number;
  tier: string;
  status: string;
  confidence: number | null;
  markers_matched: Record<string, unknown> | null;
  auto_merged: boolean;
  merge_group_id: string | null;
  created_at: string;
  reviewed_at: string | null;
  left_property: DedupPropertySide;
  right_property: DedupPropertySide;
}

export interface DedupCandidatesResponse {
  data: DedupCandidate[];
  total: number;       // total matching the filter (the page is capped by `limit`)
  returned?: number;   // rows on this page
}

export interface DedupSummaryBucket {
  reason: string;          // markers_matched.reason, or "(legacy)" for pre-reason rows
  verdict: string | null;  // markers_matched.verdict (visual buckets)
  count: number;
}

export interface DedupSummaryResponse {
  data: {
    status: string;
    total: number;
    buckets: DedupSummaryBucket[];
  };
}

export interface MergeGroup {
  merge_group_id: string;
  merged_at: string;
  survivor_property_id: number;
  retired_count: number;
  listings_moved: number;
  source: 'auto' | 'operator';
  reason: string;
  fully_undone: boolean;
}

export interface MergesResponse {
  data: MergeGroup[];
  total: number;
}

/* One row of property_sources_public — a property's per-portal observations
 * (multi-portal dedup). Drives the Listing Detail "listed on N sites" panel. */
export interface PropertySource {
  property_id: number;
  sreality_id: number;
  source: string;
  source_url: string | null;
  source_id_native: string | null;
  is_active: boolean;
  price_czk: number | null;
  first_seen_at: string;
  last_seen_at: string;
}
