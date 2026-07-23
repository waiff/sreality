"""Pydantic request bodies for the FastAPI service.

Responses are returned as plain dicts (the toolkit envelope) so we
don't have to re-encode every field in a Pydantic response model.
FastAPI's jsonable_encoder handles datetimes and Decimals on the way
out.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, model_validator


def _as_str_list(v: Any) -> Any:
    """Coerce a bare string into a single-element list (backward-compat for
    callers that still send `furnished="ano"` now that the filter is
    multi-select). None and lists pass through unchanged."""
    if isinstance(v, str):
        return [v]
    return v


# Multi-select enum filter that also accepts a single bare string.
EnumListFilter = Annotated[list[str] | None, BeforeValidator(_as_str_list)]


class TargetIn(BaseModel):
    lat: float
    lng: float
    area_m2: float | None = None
    disposition: str | None = None
    floor: int | None = None
    exclude_ids: list[int] = Field(default_factory=list)
    exclude_listing_ids: list[int] = Field(default_factory=list)


class FindComparablesIn(BaseModel):
    target: TargetIn
    radius_m: int = 1000
    area_band_pct: float = 0.20
    disposition_match: Literal["exact", "loose", "any"] = "exact"
    # No implicit freshness gate. Pass max_age_days + lifecycle='active'
    # explicitly when you want only fresh active listings; lifecycle
    # also unlocks 'delisted' / 'all' cohorts.
    max_age_days: int | None = None
    lifecycle: Literal["active", "delisted", "all"] | None = None
    floor_band: int | None = None
    portals: list[str] | None = None
    condition_match: list[str] | None = None
    building_type_match: list[str] | None = None
    energy_rating_match: list[str] | None = None
    has_balcony: bool | None = None
    has_lift: bool | None = None
    has_parking: bool | None = None
    min_price_czk: int | None = None
    max_price_czk: int | None = None
    # Required, no silent default. A missing category used to mean
    # "apartments for rent", which made house/commercial cohorts
    # impossible to drive cleanly. Pass `null` explicitly to search every
    # category. See ComparableFilters in toolkit/comparables.py.
    category_main: str | None = Field(...)
    category_type: str | None = Field(...)
    category_sub_cb: int | None = None
    locality_district_id: int | None = None
    locality_region_id: int | None = None
    include_unreliable: bool = False
    furnished: EnumListFilter = None
    terrace: bool | None = None
    cellar: bool | None = None
    garage: bool | None = None
    ownership: EnumListFilter = None
    min_estate_area: float | None = None
    max_estate_area: float | None = None
    min_usable_area: float | None = None
    max_usable_area: float | None = None
    min_parking_lots: int | None = None
    building_condition_level_min: int | None = None
    building_condition_level_max: int | None = None
    apartment_condition_level_min: int | None = None
    apartment_condition_level_max: int | None = None
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
    sreality_id: int | None = None
    since_days: int | None = None
    listing_id: int | None = None


class DescribeNeighborhoodIn(BaseModel):
    lat: float
    lng: float
    radius_m: int = 1000
    # No implicit freshness gate; pass explicitly when needed.
    max_age_days: int | None = None
    # Required, no silent apartment-rental default. Pass `null` to span
    # every category.
    category_main: str | None = Field(...)
    category_type: str | None = Field(...)


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
    portals: list[str] | None = None
    condition_match: list[str] | None = None
    building_type_match: list[str] | None = None
    energy_rating_match: list[str] | None = None
    has_balcony: bool | None = None
    has_lift: bool | None = None
    has_parking: bool | None = None
    min_price_czk: int | None = None
    max_price_czk: int | None = None
    # Required, no silent apartment-rental default. Pass `null` to span
    # every category.
    category_main: str | None = Field(...)
    category_type: str | None = Field(...)
    category_sub_cb: int | None = None
    locality_district_id: int | None = None
    locality_region_id: int | None = None
    include_unreliable: bool = False
    furnished: EnumListFilter = None
    terrace: bool | None = None
    cellar: bool | None = None
    garage: bool | None = None
    ownership: EnumListFilter = None
    min_estate_area: float | None = None
    max_estate_area: float | None = None
    min_usable_area: float | None = None
    max_usable_area: float | None = None
    min_parking_lots: int | None = None
    building_condition_level_min: int | None = None
    building_condition_level_max: int | None = None
    apartment_condition_level_min: int | None = None
    apartment_condition_level_max: int | None = None
    tom_days_min: int | None = None
    tom_days_max: int | None = None
    last_seen_min_days: int | None = None
    last_seen_max_days: int | None = None
    first_seen_min_days: int | None = None
    first_seen_max_days: int | None = None
    lifecycle: Literal["active", "delisted", "all"] = "all"
    trend_split_days: int = 7


class ComputeListingVelocityIn(BaseModel):
    sreality_id: int
    radius_m: int = 1000
    disposition_match: Literal["exact", "loose", "any"] = "exact"
    lifecycle: Literal["active", "delisted", "all"] = "all"


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


class Ppm2BoxIn(BaseModel):
    n: int
    min: float
    p25: float
    median: float
    p75: float
    max: float


class RegionDispositionIn(BaseModel):
    disposition: str
    n: int
    ppm2_box: Ppm2BoxIn | None = None


class SummarizeRegionDispositionsIn(BaseModel):
    """Annotate the per-disposition Kč/m² box plots in Browse > Stats.

    `region_key` is the caller's deterministic serialization of the active
    filter set; the server hashes it and caches the annotations per
    (region, calendar day) so repeat browser sessions don't re-bill.
    `dispositions` is the same `ppm2_box` payload that drives the chart.
    """
    region_key: str = Field(min_length=1, max_length=8192)
    dispositions: list[RegionDispositionIn] = Field(default_factory=list, max_length=40)
    ppm2_overall: dict[str, Any] | None = None
    region_label: str | None = Field(default=None, max_length=256)
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

    Exactly one of `url`, `spec`, or `sreality_id` must be set. When `url`
    is set, the URL is parsed via scraper.url_parser and the resulting spec
    is used as the target; `spec_overrides` (a partial dict) is merged on
    top to let the caller adjust individual fields. When `spec` is set
    directly, it is used verbatim. When `sreality_id` is set, the target is
    built from the already-scraped `listings` row by id (no URL parse, no
    LLM) — the path the Browse cards' on-card estimate uses.

    `estimate_kind` selects rent vs sale. When omitted, defaults to
    'rent' for backwards compatibility with callers built before
    migration 029. The default `category_type` follows the kind
    (`pronajem` for rent, `prodej` for sale) unless the caller
    explicitly overrides it.

    The comparable-search knobs (radius_m, area_band_pct,
    disposition_match, max_age_days) were removed from this schema:
    agent-mode runs choose them per-iteration, and deterministic runs
    use the built-in defaults in `api.estimation_runs._build_filters`.
    `lifecycle` is the one cohort knob kept here (optional override of
    `default_lifecycle`). UI callers default to
    `mode='agent'`; direct API callers may still pass
    `mode='deterministic'` for a single-shot estimate with the
    built-in filter defaults.
    """
    source: Literal["ui", "api", "clickup", "extension"] = "api"
    mode: Literal["deterministic", "agent"] = "deterministic"

    # Agent-mode only; ignored when mode == 'deterministic'.
    provider: Literal["anthropic", "gemini"] = "anthropic"
    skill: str = "rental_estimator_full_v1"

    estimate_kind: Literal["rent", "sale"] = "rent"

    # Cohort lifecycle. None (default) inherits `default_lifecycle`
    # ("active and recently-seen"); "delisted" restricts to
    # is_active=false (likely closed deals); "all" applies no filter.
    lifecycle: Literal["active", "delisted", "all"] | None = None

    url: str | None = None
    spec: TargetIn | None = None
    sreality_id: int | None = None
    spec_overrides: dict[str, Any] | None = None

    purchase_price_czk: int | None = None
    expected_monthly_rent_czk: int | None = None

    floor_band: int | None = None
    portals: list[str] | None = None
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
    furnished: EnumListFilter = None
    terrace: bool | None = None
    cellar: bool | None = None
    garage: bool | None = None
    ownership: EnumListFilter = None
    min_estate_area: float | None = None
    max_estate_area: float | None = None
    min_usable_area: float | None = None
    max_usable_area: float | None = None
    min_parking_lots: int | None = None
    building_condition_level_min: int | None = None
    building_condition_level_max: int | None = None
    apartment_condition_level_min: int | None = None
    apartment_condition_level_max: int | None = None
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
        targets = (self.url, self.spec, self.sreality_id)
        if sum(t is not None for t in targets) != 1:
            raise ValueError(
                "Provide exactly one of 'url', 'spec', or 'sreality_id'"
            )
        if self.category_type is None:
            self.category_type = (
                "pronajem" if self.estimate_kind == "rent" else "prodej"
            )
        return self


class ScenarioUpdateIn(BaseModel):
    """Operator-tunable yield scenario for an estimation_runs row.

    All numeric fields are optional. Send only the fields the operator
    actually overrode; missing keys remain at the default (estimated
    rent, 10 CZK/m², subject sale price, no renovation). Send a body
    with every field null to clear overrides and re-render defaults.

    `renovation_czk` is a flat one-off renovation budget added to the
    listing price to form the total acquisition cost (the yield
    denominator).
    """
    rent_czk: float | None = None
    fond_per_m2_czk: float | None = None
    price_czk: float | None = None
    renovation_czk: float | None = None


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
    lifecycle: Literal["active", "delisted", "all"] | None = None
    floor_band: int | None = None
    portals: list[str] | None = None
    condition_match: list[str] | None = None
    building_type_match: list[str] | None = None
    energy_rating_match: list[str] | None = None
    has_balcony: bool | None = None
    has_lift: bool | None = None
    has_parking: bool | None = None
    min_price_czk: int | None = None
    max_price_czk: int | None = None
    # category_main is required (no silent apartment default). category_type
    # follows estimate_kind when omitted — same smart pattern as
    # CreateEstimationIn (rent -> pronajem, sale -> prodej). Pass it
    # explicitly to override.
    category_main: str | None = Field(...)
    category_type: str | None = None
    category_sub_cb: int | None = None
    locality_district_id: int | None = None
    locality_region_id: int | None = None
    include_unreliable: bool = False
    furnished: EnumListFilter = None
    terrace: bool | None = None
    cellar: bool | None = None
    garage: bool | None = None
    ownership: EnumListFilter = None
    min_estate_area: float | None = None
    max_estate_area: float | None = None
    min_usable_area: float | None = None
    max_usable_area: float | None = None
    min_parking_lots: int | None = None
    building_condition_level_min: int | None = None
    building_condition_level_max: int | None = None
    apartment_condition_level_min: int | None = None
    apartment_condition_level_max: int | None = None
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
        # listings now pass max_age_days + lifecycle='active' explicitly.
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
    name:               str = Field(min_length=1, max_length=200)
    description:        str | None = None
    monitoring_enabled: bool = False
    notify_channels:    list[str] = Field(default_factory=list)


class UpdateCollectionIn(BaseModel):
    name:               str | None = Field(default=None, min_length=1, max_length=200)
    description:        str | None = None
    monitoring_enabled: bool | None = None
    notify_channels:    list[str] | None = None


class AddPropertiesToCollectionIn(BaseModel):
    property_ids: list[int] = Field(min_length=1, max_length=500)


class CreateNoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    # The advert the operator was viewing when they wrote the note — display
    # provenance only ("written while viewing this advert"), not a grouping key.
    origin_listing_id: int | None = None
    # Surrogate twin (R2). Preferred when present; origin_listing_id is the
    # legacy handle and is NULL for a post-Gate-2 listing.
    origin_listing_ref_id: int | None = None


class CreateTagIn(BaseModel):
    name:  str = Field(min_length=1, max_length=50)
    color: TagColor


class UpdateTagIn(BaseModel):
    name:  str | None = Field(default=None, min_length=1, max_length=50)
    color: TagColor | None = None


class AttachTagIn(BaseModel):
    tag_id: int


# --- deal pipeline (migration 205) ----------------------------------------
# A property is "bookmarked / interested" iff it has a property_pipeline row
# (starting at the entry stage). Single-valued; property grain.

class AddPipelineCardIn(BaseModel):
    property_id: int


class MoveCardIn(BaseModel):
    # Move to another stage and/or reorder within a stage. Both optional; an
    # empty body is a no-op that returns the current card.
    stage_id: int | None = None
    board_position: float | None = None


# Stage management (operator-curated columns; the curated-index precedent). The
# `key` is derived server-side from the label — operators name a column, not a slug.
PIPELINE_STAGE_COLORS = (
    "copper", "sage", "brick", "ochre", "slate", "plum", "teal", "sand",
)


class CreateStageIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    color: str | None = None
    is_terminal: bool = False


class UpdateStageIn(BaseModel):
    # All optional; an absent field is left unchanged. `is_entry` may only be
    # set True (move the entry crown to this stage) — you re-home the entry by
    # crowning another, never by un-crowning the only one.
    label: str | None = Field(default=None, min_length=1, max_length=80)
    color: str | None = None
    is_terminal: bool | None = None
    is_entry: bool | None = None


class ReorderStagesIn(BaseModel):
    # The complete set of non-archived stage ids in their new left-to-right order.
    ordered_ids: list[int]


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


# --- Price-stats datasets (ceny-nemovitosti) ------------------------------

class PriceStatDatasetIn(BaseModel):
    """Create a price-stats dataset (one named filter set)."""
    slug:               str = Field(min_length=1, max_length=120)
    name:               str = Field(min_length=1, max_length=200)
    description:        str | None = None
    category_main_cb:   int = 1
    building_condition: str | None = None
    building_type:      str | None = None
    ownership:          str | None = None
    usable_area_from:   int | None = Field(default=None, ge=0)
    usable_area_to:     int | None = Field(default=None, ge=0)
    distance:           int = Field(default=0, ge=0)
    # scrape window (YYYY-MM) + city selection
    start_ym:           str | None = None
    end_ym:             str | None = None
    obec_ids:           list[int] | None = None
    min_population:     int | None = Field(default=None, ge=0)
    max_population:     int | None = Field(default=None, ge=0)


class PriceStatDatasetUpdateIn(BaseModel):
    """Patch a dataset; every field optional."""
    name:               str | None = Field(default=None, min_length=1, max_length=200)
    description:        str | None = None
    category_main_cb:   int | None = None
    building_condition: str | None = None
    building_type:      str | None = None
    ownership:          str | None = None
    usable_area_from:   int | None = Field(default=None, ge=0)
    usable_area_to:     int | None = Field(default=None, ge=0)
    distance:           int | None = Field(default=None, ge=0)
    is_active:          bool | None = None
    start_ym:           str | None = None
    end_ym:             str | None = None
    obec_ids:           list[int] | None = None
    min_population:     int | None = Field(default=None, ge=0)
    max_population:     int | None = Field(default=None, ge=0)


class PortalLookupItem(BaseModel):
    """One portal listing keyed by its native id. For sreality, source_id is the
    numeric id as a string (matches listings.source_id_native)."""
    source: str = Field(min_length=1, max_length=40)
    source_id: str = Field(min_length=1, max_length=128)


class PortalLookupIn(BaseModel):
    """Batch lookup for the Chrome extension's detail panel + index-card overlay."""
    items: list[PortalLookupItem] = Field(min_length=1, max_length=50)
