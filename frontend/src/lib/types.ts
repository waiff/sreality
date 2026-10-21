/* Wire shapes mirroring the public views from migration 008. Only the columns
 * the UI actually reads are typed here; expand as Parts B–E need more. */

import type { DistrictChip, ListingFilters, MapBounds, PresetSpec } from './filters';
import type { ReferenceRent } from './mfReference';

/* The whole Czech grammar, 1 through 9 — the same span
 * `filter_registry.DISPOSITION_OPTIONS` generates. It used to stop at 5+1,
 * which made the type a lie about 708 active rows. */
export type Disposition =
  | '1+kk' | '1+1'
  | '2+kk' | '2+1'
  | '3+kk' | '3+1'
  | '4+kk' | '4+1'
  | '5+kk' | '5+1'
  | '6+kk' | '6+1'
  | '7+kk' | '7+1'
  | '8+kk' | '8+1'
  | '9+kk' | '9+1';

/* Promoted from raw_json via migration 022. See parser.FURNISHED /
 * parser.OWNERSHIP for the int→text mapping. NULL when sreality didn't
 * report a value. */
export type Furnished = 'ano' | 'ne' | 'castecne';
export type Ownership = 'osobni' | 'druzstevni' | 'statni' | 'jine';

/* The MF reference-rent result and the one rule for rendering it live in a
 * zero-dependency module the Chrome extension imports too. */
export type {
  ReferenceRent,
  ReferenceRentAdjustment,
  ReferenceRentRange,
} from './mfReference';

export interface ListingPublic {
  /* Surrogate PK (migration 312/334) — the stable identifier the R2 resolver-chain
   * cutover keys child loaders on instead of sreality_id where it's available. */
  id: number;
  /* NULLABLE, and typed honestly: migration 311's sign check makes a positive
   * sreality_id impossible off sreality, and post-Gate-2 non-sreality rows carry
   * NULL. Typing this non-null is what let `[listing.sreality_id]` compile into
   * an array holding one null. Key on `id` above. */
  sreality_id: number | null;
  first_seen_at: string;
  last_seen_at: string;
  is_active: boolean;
  /* Migration 091 — source portal (sreality, bazos, idnes, …). */
  source: string;
  /* Migration 420 (W9b). The portal's own advert id — the second half of the
   * canonical /listing/{source}/{source_id_native} route. On the row itself now,
   * so canonicalizing a legacy URL no longer waits on the multi-portal source
   * list to find this listing's own native id in it. */
  source_id_native: string | null;
  /* Migration 420 (W9b). The `properties` parent (rule #15) this listing hangs
   * off — NULL only inside the ~5-min pre-attach window after a scrape, which is
   * exactly the window property_sources_public also cannot answer for (it filters
   * `property_id is not null`). Every property-grain read on the page keys on it:
   * MF, the status-event log, the pipeline funnel. */
  property_id: number | null;
  category_main: string | null;
  category_type: string | null;
  price_czk: number | null;
  price_unit: string | null;
  area_m2: number | null;
  disposition: Disposition | null;
  /* Portal-agnostic property sub-type (migration 152): the meaningful "kind"
   * for commercial/houses where disposition is NULL. NULL for apartments. */
  subtype: string | null;
  /* The ONE place string, composed server-side by location_display_label from
   * the listing's `listing_location` row (migration 503): street + house number
   * + town, else part-of-town + town, else the town, else the country for a
   * foreign listing, else NULL. No surface assembles a place out of parts any
   * more -- there are no parts on the wire to assemble. */
  display_label: string | null;
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
  /* Migration 494. The listing's page on its own portal — stored by the portal's
   * parser at ingest (sreality included since the URL-contract sprint). NEVER
   * reconstruct one from the category triple: sreality's sub slugs are its own
   * closed vocabulary, not our display labels. NULL → in-app view, not a guess. */
  source_url: string | null;
  furnished: Furnished | null;
  terrace: boolean | null;
  cellar: boolean | null;
  garage: boolean | null;
  parking_lots: number | null;
  ownership: Ownership | null;
  /* Time on market in whole days, the one measure (measure_tom_days, migration
   * 617): now() - first_seen_at for active ads (right-censored, growing);
   * last_seen_at - first_seen_at for inactive. */
  tom_days: number | null;
  /* Migration 084 — original sreality "Popis" free-text description.
   * Promoted from raw_json->'text'->>'value' to a typed column +
   * exposed on listings_public. May be null for older rows that
   * haven't been re-fetched and had no description in their last
   * snapshot. */
  description: string | null;
  /* Migration 425 — THE per-m² measure and its published basis label, straight
   * off listings_public. Every detail surface reads this pair instead of
   * dividing price_czk by area_m2: the measure is basis-resolved and floored, so
   * a re-derivation would print figures the platform elsewhere says do not
   * exist. `price_per_m2_basis` is one of sale_capital_czk_m2 /
   * rent_monthly_czk_m2 / land_capital_czk_m2, or null when undecidable. */
  price_per_m2: number | null;
  price_per_m2_basis: string | null;
}

export interface ListingSnapshotPublic {
  id: number;
  /* The owning listing's SURROGATE id (listing_snapshots_public.listing_id,
   * migration 334/343). Group/join snapshots on this, not sreality_id — a
   * post-Gate-2 non-sreality listing's snapshots all carry NULL sreality_id
   * and would otherwise collide onto one shared bucket. */
  listing_id: number;
  scraped_at: string;
  price_czk: number | null;
}

export interface ListingFreshnessCheckPublic {
  id: number;
  sreality_id: number;
  checked_at: string;
  outcome: string;
}

export interface ImagePublic {
  id: number;
  sreality_id: number;
  sequence: number | null;
  sreality_url: string;
  storage_path: string | null;
  /* CLIP per-image tag (image_clip_tags via images_public, migration 236).
   * clip_fine_tag is the raw winning anchor we display; clip_logical_tag is the
   * collapsed engine vocabulary; clip_confidence is the softmax score 0..1.
   * All NULL until the image is CLIP-tagged. */
  clip_fine_tag: string | null;
  clip_logical_tag: string | null;
  clip_confidence: number | null;
  /* CLIP render-vs-photo score 0..1 (migration 239): >= ~0.65 is a 3D render /
   * visualization (excluded from the byt merge signal). NULL until scored. */
  clip_render_score: number | null;
  /* 64-bit dHash perceptual hash (migration 308), as Postgres's signed bigint —
   * a display value for image-audit surfaces. NULL until hashed. */
  phash: number | null;
  /* The ACTIVE DINOv3 tag model's per-head scores, {"<tag_taxonomy.id>": 0..1}
   * (migration 591). Only fetchImagesForListingIds selects it (the Browse card
   * cover); NULL when the active model has not scored the photo. */
  tag_head_scores?: Record<string, number> | null;
}
/* A photo for the shared lightbox without an image row's CLIP/pHash facts (its badges self-guard on null):
 * a stored copy by `storage_path`, or an outside photo by its own URL. */
export const bareImage = (id: number, storage_path: string | null, sreality_url = ''): ImagePublic => ({
  id, sreality_id: 0, sequence: null, sreality_url, storage_path,
  clip_fine_tag: null, clip_logical_tag: null, clip_confidence: null, clip_render_score: null, phash: null,
});

/* -------------------------------------------------------------------------- */
/* Estimations (U2). Wire shapes mirror api/schemas.py and api/estimation_runs */
/* — backend is authoritative.                                                */
/* -------------------------------------------------------------------------- */

export type EstimationStatus = 'pending' | 'running' | 'success' | 'failed';
export type EstimationSource = 'ui' | 'api' | 'clickup';
export type EstimationMode = 'deterministic' | 'agent';
export type EstimationProvider = 'anthropic' | 'gemini';
export type Lifecycle = 'active' | 'delisted' | 'all';
export type Confidence = 'low' | 'medium' | 'high';

export interface TargetSpecIn {
  lat: number;
  lng: number;
  area_m2: number | null;
  disposition: Disposition | null;
  floor: number | null;
  /* The subject's advert(s): every advert of their property leaves the cohort (decision 13). */
  exclude_listing_ids?: number[];
}

export interface ComparableUsed {
  /* Surrogate `listing_id` (api/estimate_yield.py:_used_entry, since #879/#892).
   * Present on every run finalised after that ship; ABSENT (not null — the key
   * itself is missing) on the ~600 frozen pre-cutover rows, which carry
   * sreality_id only (estimation_runs rows are immutable, rule 12). A comparable
   * always has at least one of {listing_id, sreality_id} — prefer listing_id for
   * identity (React keys, map marker ids, batch-fetch routing) since it survives
   * the post-flip NULL sreality_id case a non-sreality-portal comparable can hit. */
  listing_id?: number | null;
  /* NULL for a post-Gate-2-flip non-sreality-portal comparable (flip not live
   * yet, so always populated today) — never assume non-null. */
  sreality_id: number | null;
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
  /* Migration 213 — flat one-off renovation budget added to price_czk to form
   * the total acquisition cost (the yield denominator). Null/absent on rows
   * predating the field = no renovation. */
  renovation_czk: number | null;
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
  /* Kept for older clients; nothing here routes by it (sreality ads only). */
  input_sreality_id: number | null;
  /* The ad the run estimates, set at submit. Null only on the historic runs that
   * never had an ad (a pasted URL before 2026-10-20, or a bare spec). */
  input_listing_id: number | null;
  /* That ad's natural key (a join on input_listing_id): the run lives on
   * `/listing/{subject_source}/{subject_source_id_native}` (lib/runLinks). */
  subject_source: string | null;
  subject_source_id_native: string | null;
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
  /* Server-derived display string emitted only by GET /estimations: the
   * resolved town (listing_location.obec_name) of the run's ad. Null for a run
   * with no ad or an ad with no town. Not returned by GET /estimations/:id. */
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
  lifecycle?: Lifecycle;
  estimate_kind?: 'rent' | 'sale';
  /* The ad to estimate (listings.id). Exactly one of listing_id / sreality_id /
   * spec is set; sreality_id is the legacy handle of the same ad (the Browse
   * cards' on-card estimate). A pasted link becomes an ad first (POST
   * /listings/ingest); there is no URL arm. */
  listing_id?: number;
  sreality_id?: number;
  spec?: TargetSpecIn;
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
  /* CSV of listings.id surrogates — the property-grain fetch the Listing
   * Detail estimations section uses (every run on any of the property's child
   * listings). Fails CLOSED server-side: supplied-but-empty is a 400, never
   * "no filter". Never send the legacy sreality_id here — it is NULL for every
   * non-sreality listing. */
  listing_ids?: string;
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
  /* Monitored collections raise change alerts on their member properties
   * (price moves, delisting). `is_system` marks the protected default
   * "monitoring" collection (can't be renamed or deleted). */
  monitoring_enabled: boolean;
  is_system: boolean;
  /* Sharing (migration 610). `owner_email` is null exactly when the caller owns
   * the collection (lib/collectionCache `isMine`); on a shared row monitoring
   * reads off — the alerts are the owner's. */
  owner_email: string | null;
  /* The owner sees every recipient; a recipient sees only its own grant, with
   * `email: null` (= you) — that account_id is the key to leave. */
  shared_with: CollectionShare[];
}

export interface CollectionShare {
  account_id: string;
  email: string | null;
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
  created_at: string;
  /* Null until the author edits the body; marking shared is not an edit. */
  updated_at: string | null;
  /* The author's consent for the people they share this property with to read
   * it (migration 610); a foreign note is always shared. */
  shared: boolean;
  /* Null iff the caller wrote it; else the author's email (read-only here). */
  owner_email: string | null;
  /* The note's latest delegated edit (migration 641): who wrote it acting as
   * the owner, and when; null when none, or when the owner edited it since. */
  edited_by_email?: string | null;
  edited_at?: string | null;
  /* Its files, oldest first (migration 592). Only the list read carries them. */
  attachments?: NoteAttachment[];
}

/* A property's one operator-supplied link (migration 599). */
export interface ArtifactLink {
  property_id: number;
  url: string;
  created_at: string;
  updated_at: string;
}

export interface NoteAttachment {
  id: number;
  note_id: number;
  filename: string;
  mime_type: string;
  byte_size: number;
  created_at: string;
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
  /* Short operator-owned badge shown inside the funnel (migration 377). NULL =
   * fall back to the stage's 1-based ordinal among live stages. */
  code: string | null;
  /* The stage's latest delegated edit (migration 641's view columns; null when
   * none). Only the view read carries them — the API's stage writes do not. */
  edited_by_account_id?: string | null;
  edited_at?: string | null;
}

/* A pipeline share (migration 641): the OTHER party and the grant's mode. In
 * `shared_with` it is a recipient of my board; in `shared_with_me` the owner
 * whose board I may open (read) or act on (edit). */
export type PipelineShareMode = 'read' | 'edit';

export interface PipelineShare {
  account_id: string;
  email: string | null;
  mode: PipelineShareMode;
}

/* GET /pipeline/shares (and the POST's answer): both of my lists. */
export interface PipelineShares {
  shared_with: PipelineShare[];
  shared_with_me: PipelineShare[];
}

/* A board card = the property_pipeline row joined to its property's display
 * fields (from properties_public) for rendering on the kanban. */
export interface PipelineBoardCard {
  property_id: number;
  stage_id: number;
  board_position: number;
  /* Stamped by the API on a real stage change only (a pure within-stage
   * reorder is not deal history) — so "how long has this sat where it is".
   * Audited against property_pipeline_events: for every live card the newest
   * reason='operator' event matches this to the microsecond. */
  entered_stage_at: string;
  /* When the property entered the pipeline at all. Defaulted at INSERT and
   * never updated, EXCEPT by an unmerge restore, which re-INSERTs the card and
   * resets it to now() — see the sort-option docstring in lib/pipelineSort. */
  added_at: string;
  sreality_id: number | null;
  /* Representative listing's portal + native id (migration 091) — builds the
   * canonical `/listing/{source}/{native}` link so the card never flashes the
   * negative synthetic id; null on pre-091 rows → legacy/property fallback. */
  source: string | null;
  source_id_native: string | null;
  /* The representative listing's SURROGATE id (properties_public.listing_id,
   * migration 343) — never null in practice. Use this for the card's link and
   * for keying image/broker lookups; sreality_id may be NULL for a
   * post-Gate-2 non-sreality representative. */
  listing_id: number | null;
  category_main: string | null;
  /* One label -- see ListingPublic.display_label. It replaced `street`, which
   * the board carried only to feed the deleted placePrimary(). */
  display_label: string | null;
  disposition: string | null;
  subtype: string | null;
  area_m2: number | null;
  price_czk: number | null;
  mf_gross_yield_pct: number | null;
  /* Signed (last − first) percent across the REPRESENTATIVE listing's own price
   * series — the same listing whose price is `price_czk`, so the two always
   * describe one series. NULL when that listing has fewer than two priced
   * snapshots, which is NOT the same claim as "the price never moved"; the
   * <PriceDelta> component renders nothing in that case. */
  total_price_change_pct: number | null;
  /* Consecutive-step price changes within each child listing, summed across
   * children. Distinguishes "observed twice, never moved" (0) from "moved and
   * came back" (>0) when total_price_change_pct is exactly 0. */
  price_change_count: number | null;
  /* The five RÚIAN codes the ONE place predicate tests (lib/districtCodes) —
   * the board filters its cards in the browser, so it carries the same codes
   * the server-side predicate uses. `obec_id` doubles as the join key to the
   * curated-city index strip (= curated_cities.admin_boundary_id; see
   * lib/useCityQuality). */
  obec_id: number | null;
  cast_obce_id: number | null;
  ulice_id: number | null;
  okres_id: number | null;
  region_id: number | null;
  /* The municipality NAME, and the only place text left on a card (W3 S4,
   * migration 506 dropped `district`, `locality`, `okres`, `region` and
   * `place_search_text` from pipeline_board_public). The card's place line is
   * `display_label`; `obec` stays because the "Město A–Ž" sort orders by the
   * TOWN, which is the tail of the label rather than its head (a label that
   * starts with a street would sort by house number). */
  obec: string | null;
  /* properties.is_active rollup (bool_or over child listings, rule #15/#20) —
   * the same property-grain liveness signal Browse filters on. */
  is_active: boolean;
  /* Migration 425 — the deal type, THE per-m² measure and its published basis
   * label, all three added to pipeline_board_public for the board. The board
   * spans deal types by rule 22, so the price line needs category_type for its
   * monthly marker and the per-m² figure needs its own basis per card. */
  category_type: string | null;
  price_per_m2: number | null;
  price_per_m2_basis: string | null;
  /* The card's latest delegated edit (migration 641's view columns): the
   * account that last moved it acting as the board's owner, and when; null
   * when none, or when the owner changed it since. */
  edited_by_account_id?: string | null;
  edited_at?: string | null;
  /* No image_url and no broker here on purpose. Both are DECORATIONS: they are
   * fetched by lib/hydration keyed on `listing_id` and reach the card through
   * CardHydrationProvider, so nothing on the board's critical path waits for
   * them. Putting either back on this interface would re-couple the board's
   * structural read to a thumbnail — the exact shape that made the board take
   * six serialized round trips to paint. */
}

/* GET /collections/{id} embeds a slimmer property projection than
 * ListingPublic — see api/curation.get_collection. Each row carries the
 * property_id plus its representative listing's sreality_id (for
 * /listing/{sreality_id} links). Renderers expand with fetchListingsByIds()
 * when more detail is needed. */
export interface CollectionPropertyRow {
  property_id: number;
  sreality_id: number;
  /** Source portal of the representative listing; null if none joined. Drives
   * whether sreality_id is shown as a meaningful "ID" (sreality only). */
  source: string | null;
  /* One label -- see ListingPublic.display_label. */
  display_label: string | null;
  disposition: string | null;
  subtype: string | null;
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

export interface UpdateBuildingInputsIn {
  special_instructions?: string | null;
  contextual_text?: string | null;
}

export interface ConfirmBuildingUnitsIn {
  units: BuildingUnit[];
}

/* -------------------------------------------------------------------------- */
/* Watchdog / new-listing notifications (Phase U2.7).                         */
/*                                                                            */
/* Subscriptions are saved filter specs; the backend matcher writes one       */
/* dispatch per (subscription, listing) match. Wire shapes mirror             */
/* api/notifications.py + migrations 056 / 057.                               */
/* -------------------------------------------------------------------------- */

export interface WatchdogFilterSpec {
  // Multi-select category (matches any). Deal type stays single-valued.
  category_main_in: string[] | null;
  category_type: string | null;
  category_sub_cb: number | null;
  // Portal-agnostic property sub-type (multi-select; matches any).
  subtype: string[] | null;
  dispositions: string[] | null;
  /* The map box (rule 16): the ONE spatial shape, what Browse filters on — its
   * viewport, or the square around a centre+radius pick (filters.effectiveBbox)
   * — so a watchdog saved from Browse watches the rows Browse showed. The
   * matcher renders it as the same lat/lng band (api/location_filter
   * .bounds_where). Null = no map area. */
  bounds: MapBounds | null;
  /* Place, as chips. Each chip is a LEVEL plus a RÚIAN CODE — the one
   * predicate Browse, Stats, the map and the watchdog matcher share
   * (rule 16, lib/districtCodes + api/location_filter). `context` is
   * carried for the read-time name lookup that rescues chips saved
   * before codes existed; it is not a predicate. See migration 075 for
   * the one-shot lift of legacy `string[]` entries to this shape. */
  districts: DistrictChip[] | null;
  min_price_czk: number | null;
  max_price_czk: number | null;
  // When true AND a price bound is set, keep no-price (price_czk IS NULL)
  // listings instead of dropping them. No-op when no bound is set.
  include_no_price: boolean;
  // Price per m² bounds, matched against THE measure — properties_public
  // .price_per_m2, i.e. measure_price_per_m2 (migration 425), basis-resolved
  // and floored. Rule 16: the same column Browse filters on, so a saved
  // watchdog and the Browse cohort it was built from cannot disagree. It is
  // NOT price_czk / area_m2, which is what this said before the measure existed.
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
  // The canonical condition (Stav objektu). Matches l.condition = ANY(...), and
  // '__unknown__' NULL / non-canonical like furnished; null / empty = any.
  // Honoured by the matcher (_build_match_clauses).
  condition_match: string[] | null;
  // The portal rule's two sides (MS19 + migration 597): a property with an ad on
  // one of `portals` (null / empty = all) and none on `portals_excluded` (any ad,
  // or only active ones under `portals_excluded_active_only`).
  portals: string[] | null;
  portals_excluded: string[] | null;
  portals_excluded_active_only: boolean;
  min_parking_lots: number | null;
  // Price-history aggregates (migration 173). Property grain; the matcher
  // reads them off properties_public. The window (30/90/365, null = all
  // time) picks which precomputed count column the count min reads;
  // total_price_change_pct is signed (negative = total drop of at least
  // that much, positive = total rise).
  price_change_count_min: number | null;
  price_change_window_days: 30 | 90 | 365 | null;
  total_price_change_pct: number | null;
  // Restored in fix/watchdog-dropped-filters (lost by merge f2d7b359):
  // the matcher honours them as Browse does, and an equality test pins
  // the Python spec to the WATCHDOG agenda so a filter can't drop again.
  building_material: Array<'cihla' | 'panel' | 'smisena' | 'ostatni'> | null;
  energy_rating_match: string[] | null;
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
  category_main_in: null,
  category_type: 'pronajem',
  category_sub_cb: null,
  subtype: null,
  dispositions: null,
  bounds: null,
  districts: null,
  min_price_czk: null,
  max_price_czk: null,
  include_no_price: false,
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
  portals_excluded: null,
  portals_excluded_active_only: false,
  min_parking_lots: null,
  price_change_count_min: null,
  price_change_window_days: null,
  total_price_change_pct: null,
  building_material: [],
  energy_rating_match: [],
  min_garden_area: null,
  max_garden_area: null,
  tags: null,
  city_index_rules: null,
  min_city_population: null,
  max_city_population: null,
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

/* A named Browse › Stats dashboard (stats_dashboards, migration 647): the
 * filter preset's twin in the one saved-set primitive (docs/design/
 * saved-dashboards.md). `charts` is its stored chart list — read it through
 * lib/stats `sanitizeCharts`; `filter_spec` is the preset blob, null = the
 * dashboard carries no filter. Sharing as a collection's: `owner_email` is null
 * exactly when the caller owns it; the owner sees every recipient, a recipient
 * only its own grant (the account_id it leaves with). */
export interface SavedDashboard {
  id: string;
  name: string;
  charts: unknown;
  filter_spec: PresetSpec | null;
  position: number;
  color: TagColor | null;
  created_at: string;
  updated_at: string;
  owner_email: string | null;
  shared_with: CollectionShare[];
}

export type NotificationSourceKind =
  | 'watchdog'
  | 'collection_monitor'
  /* Something was shared with the caller (migrations 610, 641, 647): a collection
   * (collection_id set), a pipeline or a dashboard (collection_id null); change_kind
   * names which (migration 652), no listing subject — the sharer and the thing's name. */
  | 'share';

/* Why a dispatch fired: a detector's event, or what a share opens (the change_kind
 * CHECK of migration 652, minus the kinds no feed row carries). */
export type DispatchChangeKind =
  | 'new'
  | 'price_drop'
  | 'price_rise'
  | 'inactive'
  | 'reactivated'
  | 'new_source'
  | 'shared_collection'
  | 'shared_dashboard'
  | 'shared_pipeline';

export interface WatchdogDispatch {
  id: string;
  /* Unified event model (migration 206). `source_kind` says which producer
   * fired this row; exactly one of subscription_id / collection_id is set. */
  source_kind: NotificationSourceKind;
  subscription_id: string | null;
  subscription_name: string | null;
  collection_id: number | null;
  collection_name: string | null;
  /* Property grain (Slice 2b). `property_id` is the property the event is
   * about — set on every watchdog / collection_monitor row, null on a share;
   * the row opens it (lib/dispatchTarget). `change_kind` is why this dispatch
   * fired ('new' = newly matched the filter, 'price_drop' = a recent price
   * decrease). */
  property_id: number | null;
  change_kind: DispatchChangeKind;
  /* What a `share` row shares: the kind (the API reads it off change_kind,
   * migration 652), and its id off the row's own columns — the collection id, the
   * owner's account uuid for a pipeline (the actor), the dashboard uuid (the
   * event_ref). Null on every other producer. */
  share_kind: 'collection' | 'pipeline' | 'dashboard' | null;
  share_ref: string | null;
  /* A share stores no text (migration 656): the bell composes its sentence from
   * the sharer's email (null once no share joins the two accounts), the shared
   * collection's or dashboard's live name (null once it cannot be read) and a
   * pipeline share's mode. All three null on every other producer. */
  actor_email: string | null;
  share_name: string | null;
  share_mode: 'read' | 'edit' | null;
  dispatched_at: string;
  seen_at: string | null;
  /* Change provenance — price events carry these so "why was I pinged" survives
   * latest-wins: the new and the previous price. */
  trigger_price_czk: number | null;
  prev_price_czk: number | null;
  /* Listing-side fields joined via the matcher's LEFT JOIN. Null when
   * the listing has been hard-deleted (shouldn't happen — architectural
   * rule #3 forbids deletes — but render defensively). */
  category_main: string | null;
  category_type: string | null;
  price_czk: number | null;
  price_unit: string | null;
  area_m2: number | null;
  disposition: Disposition | null;
  /* Portal-agnostic property sub-type (migration 152): the meaningful "kind"
   * for commercial/houses where disposition is NULL. NULL for apartments. */
  subtype: string | null;
  /* One label, composed by the API from the dispatch's listing (migration 503).
   * It replaced a `locality ?? district` pair that the SPA fell back through in
   * one order and the extension in the other. */
  display_label: string | null;
  is_active: boolean | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  /* MF (Ministry of Finance) reference gross rental yield % — the
   * deterministic Cenová-mapa figure carried on the listing (migration 133).
   * Sale apartments only; null on rentals / non-apartments / no territory. */
  mf_gross_yield_pct: number | null;
  /* The portal the property was last seen on (`listings.source`, e.g.
   * 'sreality') + the listing's URL on that portal (`source_url`, may be null
   * for older sreality rows). */
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

/* The caller's unseen notifications (GET /notifications/unread-count): the
 * one number the nav badge and the feed's header show. */
export interface NotificationUnreadCount {
  unread_count: number;
}

export type WatchdogSeenFilter = 'all' | 'seen' | 'unseen';

/* One row of property_sources_public — one advert of a property: a row of the
 * property page's merged-adverts section. */
export interface PropertySource {
  property_id: number;
  /* The child listing's SURROGATE id (property_sources_public.id = listings.id,
   * migration 334). NEVER null — per-advert loaders key on it, never on
   * sreality_id, which a post-Gate-2 non-sreality row may not have. */
  id: number;
  sreality_id: number;
  source: string;
  source_url: string | null;
  source_id_native: string | null;
  is_active: boolean;
  price_czk: number | null;
  first_seen_at: string;
  last_seen_at: string;
}

/* One registered sale, as `sold_comparables(p_lat, p_lng, p_radius_m)` answers
 * it (migration 545). NOT a listing and never joined to one: a sale is an
 * account-less external fact under its own cadastral identity, so the key is
 * `(source, source_record_id)` — the cadastre transfer id — and there is no
 * `listing_id`, no `property_id` and no `is_active`.
 *
 * The attribute columns are spelled exactly as `listings` spells them, which is
 * what lets `price_per_m2` / `price_per_m2_basis` be migration 425's measure
 * rather than a second per-m² definition for sold data. `distance_m` is the
 * function's own derivation: metres from the point it was asked about.
 *
 * These are the columns the block RENDERS, not every column the function
 * returns. The rest — the RÚIAN codes, the sale's own point, `sold_age_days`,
 * the two secondary areas — exist to be FILTERED on, and a PostgREST predicate
 * does not need its column selected. */
export interface SoldComparable {
  source: string;
  source_record_id: string;
  /* A DATE: the source's time component is its batch clock, not a legal time. */
  sold_at: string;
  price_czk: number;
  /* The last ASKING price. Only ever rendered as a discount against the sale
   * price — it is never itself a comparable. */
  asking_last_czk: number | null;
  listed_at: string | null;
  category_main: string;
  category_type: string;
  subtype: string | null;
  disposition: Disposition | null;
  area_m2: number | null;
  area_basis: string | null;
  address_text: string | null;
  photo_urls: string[] | null;
  source_url: string | null;
  fetched_at: string;
  distance_m: number;
  price_per_m2: number | null;
  price_per_m2_basis: string | null;
}

/* Coverage of the MUNICIPALITY containing a point — `sold_coverage` (migration
 * 545). Not of the fetched box: cells are obec envelopes expanded by 5 km and
 * overlap heavily, so only the point's own obec is a statement about the point.
 * Four answers, and the block says which:
 *   - no row: the point is in no municipality we hold a boundary for;
 *   - `fetched_at` and `last_attempt_at` both null: nobody has ever looked;
 *   - `fetched_at` null with a failed `last_attempt_at`: we tried and could
 *     not — a broken lane, NOT operator inaction;
 *   - `fetched_at` set: we looked that day. `record_count` is what the source
 *     published for this municipality inside its 24-month window; `source_total`
 *     is what it says has EVER been registered there. Two populations, not a
 *     numerator over a denominator. */
export interface SoldCoverage {
  obec_kod: number;
  obec_name: string;
  fetched_at: string | null;
  record_count: number | null;
  source_total: number | null;
  truncated: boolean | null;
  last_attempt_at: string | null;
  last_attempt_status: string | null;
}

/* POST /listings/ingest and GET /listings/ingest/{source}/{native_id}
 * (docs/design/new-property-from-link.md §3.2): ONE state shape for a pasted ad
 * link. `held` = we hold the ad (`property_id` null until its property is born);
 * `queued` = the worker will fetch its page; `refused` / `gone` / `failed` carry
 * one Czech sentence in `message_cs`. Market data only — no per-account field. */
export type IngestStateName = 'refused' | 'held' | 'queued' | 'gone' | 'failed';

export interface IngestQueue {
  attempts: number;
  claimed_at: string | null;
  given_up: boolean;
}

export interface IngestState {
  state: IngestStateName;
  message_cs: string | null;
  source: string | null;
  source_id_native: string | null;
  listing_id: number | null;
  is_active: boolean | null;
  property_id: number | null;
  queue: IngestQueue | null;
}
