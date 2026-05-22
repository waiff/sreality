"""Pydantic request bodies for the FastAPI service.

Responses are returned as plain dicts (the toolkit envelope) so we
don't have to re-encode every field in a Pydantic response model.
FastAPI's jsonable_encoder handles datetimes and Decimals on the way
out.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class TargetIn(BaseModel):
    lat: float
    lng: float
    area_m2: float | None = None
    disposition: str | None = None
    floor: int | None = None
    exclude_ids: list[int] = Field(default_factory=list)


class FindComparablesIn(BaseModel):
    target: TargetIn
    radius_m: int = 1000
    area_band_pct: float = 0.20
    disposition_match: Literal["exact", "loose", "any"] = "exact"
    # No implicit freshness gate. Pass max_age_days / active_only
    # explicitly when you want only fresh active listings.
    max_age_days: int | None = None
    active_only: bool = False
    floor_band: int | None = None
    condition_match: list[str] | None = None
    building_type_match: list[str] | None = None
    energy_rating_match: list[str] | None = None
    has_balcony: bool | None = None
    has_lift: bool | None = None
    has_parking: bool | None = None
    min_price_czk: int | None = None
    max_price_czk: int | None = None
    category_main: str | None = "byt"
    category_type: str | None = "pronajem"
    category_sub_cb: int | None = None
    locality_district_id: int | None = None
    locality_region_id: int | None = None
    include_unreliable: bool = False
    furnished: str | None = None
    terrace: bool | None = None
    cellar: bool | None = None
    garage: bool | None = None
    ownership: str | None = None
    min_estate_area: float | None = None
    max_estate_area: float | None = None
    min_usable_area: float | None = None
    max_usable_area: float | None = None
    min_parking_lots: int | None = None
    building_condition_level_min: int | None = None
    apartment_condition_level_min: int | None = None
    tom_days_min: int | None = None
    tom_days_max: int | None = None
    last_seen_min_days: int | None = None
    last_seen_max_days: int | None = None
    first_seen_min_days: int | None = None
    first_seen_max_days: int | None = None


class AnalyzeDistributionIn(BaseModel):
    listings: list[dict[str, Any]]
    field: Literal["price_czk", "price_per_m2", "area_m2"] = "price_per_m2"


class ClusterComparablesIn(BaseModel):
    listings: list[dict[str, Any]]
    axes: list[
        Literal["price_per_m2", "price_czk", "area_m2", "distance_m"]
    ] = Field(default_factory=lambda: ["price_per_m2"])
    n_clusters: int = Field(default=3, ge=2, le=8)
    seed: int = 42
    n_restarts: int = Field(default=5, ge=1, le=20)


class FindComparablesRelaxedIn(FindComparablesIn):
    min_results: int = Field(default=5, ge=1, le=50)
    relaxation_ladder: list[str] | None = None


class FindComparablesAlongAxisIn(FindComparablesIn):
    transport_types: list[Literal["tram", "subway", "bus"]] | None = None
    anchor_radius_m: int = Field(default=800, ge=50, le=5000)
    corridor_m: int = Field(default=300, ge=50, le=2000)
    cache_ttl_days: int = Field(default=30, ge=1, le=365)


class VerifyFreshnessIn(BaseModel):
    sreality_id: int
    max_age_hours: int = 24


class CompareSnapshotsIn(BaseModel):
    sreality_id: int
    since_days: int | None = None


class DescribeNeighborhoodIn(BaseModel):
    lat: float
    lng: float
    radius_m: int = 1000
    # No implicit freshness gate; pass explicitly when needed.
    max_age_days: int | None = None
    category_main: str | None = "byt"
    category_type: str | None = "pronajem"


class FindDistributionOutliersIn(BaseModel):
    listings: list[dict[str, Any]]
    field: Literal["price_per_m2", "price_czk"] = "price_per_m2"
    iqr_multiplier: float = 1.5
    investigate_history: bool = True


class ComputeMarketVelocityIn(BaseModel):
    target: TargetIn
    radius_m: int = 1000
    area_band_pct: float = 0.20
    disposition_match: Literal["exact", "loose", "any"] = "exact"
    floor_band: int | None = None
    condition_match: list[str] | None = None
    building_type_match: list[str] | None = None
    energy_rating_match: list[str] | None = None
    has_balcony: bool | None = None
    has_lift: bool | None = None
    has_parking: bool | None = None
    min_price_czk: int | None = None
    max_price_czk: int | None = None
    category_main: str | None = "byt"
    category_type: str | None = "pronajem"
    category_sub_cb: int | None = None
    locality_district_id: int | None = None
    locality_region_id: int | None = None
    include_unreliable: bool = False
    furnished: str | None = None
    terrace: bool | None = None
    cellar: bool | None = None
    garage: bool | None = None
    ownership: str | None = None
    min_estate_area: float | None = None
    max_estate_area: float | None = None
    min_usable_area: float | None = None
    max_usable_area: float | None = None
    min_parking_lots: int | None = None
    building_condition_level_min: int | None = None
    apartment_condition_level_min: int | None = None
    tom_days_min: int | None = None
    tom_days_max: int | None = None
    last_seen_min_days: int | None = None
    last_seen_max_days: int | None = None
    first_seen_min_days: int | None = None
    first_seen_max_days: int | None = None
    population: Literal["active", "delisted", "all"] = "all"
    trend_split_days: int = 7


class ComputeListingVelocityIn(BaseModel):
    sreality_id: int
    radius_m: int = 1000
    disposition_match: Literal["exact", "loose", "any"] = "exact"
    population: Literal["active", "delisted", "all"] = "all"


class FindAnchorAmenitiesIn(BaseModel):
    lat: float
    lng: float
    radius_m: int = 1000
    categories: list[str] | None = None
    cache_ttl_days: int = 30


class ComputeWalkabilityIn(BaseModel):
    lat: float
    lng: float
    radius_m: int = 1000
    categories: list[str] | None = None
    weights: dict[str, float] | None = None
    cache_ttl_days: int = 30


class ComputeAmenitySupplyIn(BaseModel):
    lat: float
    lng: float
    radius_m: int = 1000
    categories: list[str] | None = None
    target_counts: dict[str, int] | None = None
    cache_ttl_days: int = 30


class SummarizeListingIn(BaseModel):
    sreality_id: int
    snapshot_id: int | None = None
    force_refresh: bool = False


class SummarizeListingsBatchItem(BaseModel):
    sreality_id: int
    snapshot_id: int | None = None


class SummarizeListingsBatchIn(BaseModel):
    """Batch wrapper around summarize_listing for the Estimate page.

    Returns one row per requested item — `{summary, snapshot_id}` on
    success or `{error}` on failure. Cache hits skip the LLM, so
    repeat calls for the same pairs are effectively free.
    """
    items: list[SummarizeListingsBatchItem] = Field(default_factory=list, max_length=50)


class CompareListingImagesIn(BaseModel):
    sreality_id_a: int
    sreality_id_b: int
    n_images: int = Field(default=6, ge=1, le=20)
    force_refresh: bool = False


class PreviewEstimationIn(BaseModel):
    """POST /estimations/preview body.

    Resolves the URL through the source-kind dispatcher and returns the
    parsed spec + provenance fields without creating an estimation_runs
    row. Used by the UI to show "what we extracted" before the user
    commits to running the full estimate.
    """
    url: str
    spec_overrides: dict[str, Any] | None = None
    force_refresh: bool = False


class CreateEstimationIn(BaseModel):
    """POST /estimations request body.

    Either `url` or `spec` must be set, not both. When `url` is set,
    the URL is parsed via scraper.url_parser and the resulting spec is
    used as the target; `spec_overrides` (a partial dict) is merged on
    top to let the caller adjust individual fields. When `spec` is set
    directly, it is used verbatim.

    `estimate_kind` selects rent vs sale. When omitted, defaults to
    'rent' for backwards compatibility with callers built before
    migration 029. The default `category_type` follows the kind
    (`pronajem` for rent, `prodej` for sale) unless the caller
    explicitly overrides it.

    The five comparable-search knobs (radius_m, area_band_pct,
    disposition_match, max_age_days, active_only) were removed from
    this schema: agent-mode runs choose them per-iteration, and
    deterministic runs use the built-in defaults in
    `api.estimation_runs._build_filters`. UI callers default to
    `mode='agent'`; direct API callers may still pass
    `mode='deterministic'` for a single-shot estimate with the
    built-in filter defaults.
    """
    source: Literal["ui", "api", "clickup"] = "api"
    mode: Literal["deterministic", "agent"] = "deterministic"

    # Agent-mode only; ignored when mode == 'deterministic'.
    provider: Literal["anthropic", "gemini"] = "anthropic"
    skill: str = "rental_estimator_full_v1"

    estimate_kind: Literal["rent", "sale"] = "rent"

    # Cohort population. None (default) preserves the legacy
    # "active and recently-seen" filter; "delisted" restricts to
    # is_active=false (likely closed deals); "all" applies no filter.
    population: Literal["active", "delisted", "all"] | None = None

    url: str | None = None
    spec: TargetIn | None = None
    spec_overrides: dict[str, Any] | None = None

    purchase_price_czk: int | None = None
    expected_monthly_rent_czk: int | None = None

    floor_band: int | None = None
    condition_match: list[str] | None = None
    building_type_match: list[str] | None = None
    energy_rating_match: list[str] | None = None
    has_balcony: bool | None = None
    has_lift: bool | None = None
    has_parking: bool | None = None
    min_price_czk: int | None = None
    max_price_czk: int | None = None
    category_main: str | None = "byt"
    category_type: str | None = None
    category_sub_cb: int | None = None
    locality_district_id: int | None = None
    locality_region_id: int | None = None
    include_unreliable: bool = False
    furnished: str | None = None
    terrace: bool | None = None
    cellar: bool | None = None
    garage: bool | None = None
    ownership: str | None = None
    min_estate_area: float | None = None
    max_estate_area: float | None = None
    min_usable_area: float | None = None
    max_usable_area: float | None = None
    min_parking_lots: int | None = None
    building_condition_level_min: int | None = None
    apartment_condition_level_min: int | None = None
    tom_days_min: int | None = None
    tom_days_max: int | None = None
    last_seen_min_days: int | None = None
    last_seen_max_days: int | None = None
    first_seen_min_days: int | None = None
    first_seen_max_days: int | None = None

    parent_run_id: int | None = None
    rerun_reason: str | None = None

    special_instructions: str | None = Field(default=None, max_length=10_000)
    contextual_text: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def _apply_kind_defaults(self) -> "CreateEstimationIn":
        if (self.url is None) == (self.spec is None):
            raise ValueError(
                "Provide exactly one of 'url' or 'spec'"
            )
        if self.category_type is None:
            self.category_type = (
                "pronajem" if self.estimate_kind == "rent" else "prodej"
            )
        return self


class ScenarioUpdateIn(BaseModel):
    """Operator-tunable yield scenario for an estimation_runs row.

    All three numeric fields are optional. Send only the fields the
    operator actually overrode; missing keys remain at the default
    (estimated rent, 10 CZK/m², subject sale price). Send a body
    with all three set to null to clear overrides and re-render
    defaults.
    """
    rent_czk: float | None = None
    fond_per_m2_czk: float | None = None
    price_czk: float | None = None


class ResolveLocationIn(BaseModel):
    """A Mapy.cz suggestion item picked by the user.

    Field names mirror Mapy.cz's `/v1/suggest` item shape so the frontend
    can pass through the relevant subset without re-mapping. `position`
    is collapsed into `lat`/`lng`. `regional_structure` is the
    `regionalStructure` array used to match admin polygons. `raw` is
    optional and round-tripped to the response for debugging.
    """
    label: str
    lat: float | None = None
    lng: float | None = None
    type: str | None = None
    regional_structure: list[dict[str, Any]] | None = None
    raw: dict[str, Any] | None = None


class EstimateYieldIn(BaseModel):
    target: TargetIn
    estimate_kind: Literal["rent", "sale"] = "rent"
    purchase_price_czk: int | None = None
    expected_monthly_rent_czk: int | None = None
    radius_m: int = 1000
    area_band_pct: float = 0.20
    disposition_match: Literal["exact", "loose", "any"] = "exact"
    max_age_days: int | None = None
    active_only: bool = False
    floor_band: int | None = None
    condition_match: list[str] | None = None
    building_type_match: list[str] | None = None
    energy_rating_match: list[str] | None = None
    has_balcony: bool | None = None
    has_lift: bool | None = None
    has_parking: bool | None = None
    min_price_czk: int | None = None
    max_price_czk: int | None = None
    category_main: str | None = "byt"
    category_type: str | None = None
    category_sub_cb: int | None = None
    locality_district_id: int | None = None
    locality_region_id: int | None = None
    include_unreliable: bool = False
    furnished: str | None = None
    terrace: bool | None = None
    cellar: bool | None = None
    garage: bool | None = None
    ownership: str | None = None
    min_estate_area: float | None = None
    max_estate_area: float | None = None
    min_usable_area: float | None = None
    max_usable_area: float | None = None
    min_parking_lots: int | None = None
    building_condition_level_min: int | None = None
    apartment_condition_level_min: int | None = None
    tom_days_min: int | None = None
    tom_days_max: int | None = None
    last_seen_min_days: int | None = None
    last_seen_max_days: int | None = None
    first_seen_min_days: int | None = None
    first_seen_max_days: int | None = None

    @model_validator(mode="after")
    def _apply_kind_defaults(self) -> "EstimateYieldIn":
        if self.category_type is None:
            self.category_type = (
                "pronajem" if self.estimate_kind == "rent" else "prodej"
            )
        # No implicit freshness gate. Callers that want only fresh active
        # listings now pass max_age_days / active_only explicitly.
        return self


# --- curation -------------------------------------------------------------
# Collections (named lists of listings), free-text journal notes, and
# free-form coloured tags. The colour palette is mirrored in the
# 024_listing_tags.sql CHECK constraint and in the frontend's
# tag-palette tokens.

TagColor = Literal[
    "copper", "sage", "brick", "ochre",
    "slate",  "plum", "teal",  "sand",
]


class CreateCollectionIn(BaseModel):
    name:        str = Field(min_length=1, max_length=200)
    description: str | None = None


class UpdateCollectionIn(BaseModel):
    name:        str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class AddListingsToCollectionIn(BaseModel):
    sreality_ids: list[int] = Field(min_length=1, max_length=500)


class CreateNoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class CreateTagIn(BaseModel):
    name:  str = Field(min_length=1, max_length=50)
    color: TagColor


class UpdateTagIn(BaseModel):
    name:  str | None = Field(default=None, min_length=1, max_length=50)
    color: TagColor | None = None


class AttachTagIn(BaseModel):
    tag_id: int


# --- manual rental estimates ---------------------------------------------
# Operator-recorded point estimates attached to a listing (Phase U-ME).
# See migration 046 and CLAUDE.md "Operator workflow track".

ManualEstimateSourceKind = Literal[
    "broker", "gut", "external_comp", "portfolio", "other",
]


class CreateManualEstimateIn(BaseModel):
    rent_czk:    int = Field(ge=1000, le=1000000)
    author:      str = Field(min_length=1, max_length=120)
    source_kind: ManualEstimateSourceKind
    notes:       str | None = Field(default=None, min_length=1, max_length=4000)
    updated_by:  str | None = Field(default=None, max_length=120)


class UpdateManualEstimateIn(BaseModel):
    rent_czk:    int | None = Field(default=None, ge=1000, le=1000000)
    author:      str | None = Field(default=None, min_length=1, max_length=120)
    source_kind: ManualEstimateSourceKind | None = None
    notes:       str | None = Field(default=None, max_length=4000)
    updated_by:  str | None = Field(default=None, max_length=120)


class GetManualRentalEstimatesIn(BaseModel):
    sreality_id: int


# --- buildings ------------------------------------------------------------
# Phase B0 of the building-decomposition track. See ROADMAP.md
# "Building decomposition track" and CLAUDE.md architectural rule #13.
# B0 ships schemas + read endpoints only; B1 adds URL ingest + the
# extractor; B2 fans out per-unit estimations; B3 layers the
# business case.

BuildingStatus = Literal[
    "pending", "extracting", "awaiting_input",
    "estimating", "success", "failed",
]

BuildingUnitSource = Literal[
    "description", "floor_plan", "both", "user_added",
]


class BuildingUnit(BaseModel):
    """One apartment unit within a building.

    Lives inside `building_runs.units_proposal` (agent output) and
    `building_runs.units` (operator-confirmed) as a JSONB array entry.
    `unit_id` is a stable string ('u1', 'u2', ...) assigned by the
    extractor (B1) and preserved across edits so child estimations
    stay linked to the same conceptual unit.
    """
    unit_id: str = Field(min_length=1, max_length=50)
    label: str | None = None
    floor: str | None = None
    area_m2: float | None = None
    disposition: str | None = None
    condition: str | None = None
    is_potential: bool = False
    source: BuildingUnitSource | None = None
    notes: str | None = None


class CreateBuildingIn(BaseModel):
    """Minimal building-row creation for B0.

    Inserts a `status='pending'` shell so the read endpoints can be
    exercised end-to-end. B1 replaces this with `POST /buildings/from_url`
    that parses the URL, fills `input_*`/`source_*`, and kicks off the
    extractor synchronously.
    """
    source: Literal["ui", "api", "clickup"] = "api"
    input_url: str | None = None


class CreateBuildingFromUrlIn(BaseModel):
    """Phase B1 paste-a-building entry.

    Routes the URL through scraper.source_dispatcher (the existing
    per-source parser fleet), rejects category_main='byt' (apartments
    don't decompose), then runs the building-unit extractor
    synchronously and persists the row in `status='awaiting_input'`.
    """
    source: Literal["ui", "api", "clickup"] = "api"
    url: str = Field(min_length=1)
    force_refresh: bool = False

    special_instructions: str | None = Field(default=None, max_length=10_000)
    contextual_text: str | None = Field(default=None, max_length=20_000)


class UpdateBuildingInputsIn(BaseModel):
    """Patch operator-supplied text on a building_runs row.

    Allowed only while the row is editable (status in
    pending / extracting / awaiting_input). Once estimation starts the
    inputs are frozen for audit. Use only to correct text inputs;
    attachments have their own endpoints.
    """
    special_instructions: str | None = Field(default=None, max_length=10_000)
    contextual_text: str | None = Field(default=None, max_length=20_000)


class BuildingAttachmentOut(BaseModel):
    """One operator-supplied attachment row on a building_run."""
    id: int
    building_run_id: int
    filename: str
    mime_type: str
    byte_size: int
    width_px: int | None = None
    height_px: int | None = None
    storage_key: str
    sha256_hex: str
    uploaded_by: str | None = None
    created_at: str


class ConfirmBuildingUnitsIn(BaseModel):
    """Operator-confirmed unit list. Transitions awaiting_input -> estimating."""
    units: list[BuildingUnit] = Field(min_length=1, max_length=30)


# --- Feedback (Phase AI slice B) ------------------------------------------

class CreateFeedbackIn(BaseModel):
    """Operator note on one estimation run.

    `kick_off_refinement` flips on the slice C refiner the moment the
    feedback lands, so the operator gets a same-session prompt
    proposal. Set false to stash feedback without spending LLM credit.
    """
    feedback_text:        str = Field(min_length=1, max_length=4000)
    kick_off_refinement:  bool = True


# --- Skill refinements (Phase AI slice C) ---------------------------------

FeedbackStatus = Literal[
    "submitted", "refining", "proposed", "applied", "dismissed", "failed",
]

RefinementStatus = Literal["proposed", "applied", "dismissed"]


class RefinementDecisionIn(BaseModel):
    """Operator decides what to do with a proposed skill refinement."""
    decision: Literal["apply", "dismiss"]
