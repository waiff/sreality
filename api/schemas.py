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
    max_age_days: int = 7
    active_only: bool = True
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
    locality_district_id: int | None = None
    locality_region_id: int | None = None
    include_unreliable: bool = False


class AnalyzeDistributionIn(BaseModel):
    listings: list[dict[str, Any]]
    field: Literal["price_czk", "price_per_m2", "area_m2"] = "price_per_m2"


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
    max_age_days: int = 30
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
    locality_district_id: int | None = None
    locality_region_id: int | None = None
    include_unreliable: bool = False
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

    The remaining filter fields forward into ComparableFilters
    one-to-one, mirroring EstimateYieldIn.
    """
    source: Literal["ui", "api", "clickup"] = "api"
    mode: Literal["deterministic"] = "deterministic"

    url: str | None = None
    spec: TargetIn | None = None
    spec_overrides: dict[str, Any] | None = None

    purchase_price_czk: int | None = None

    radius_m: int = 1000
    area_band_pct: float = 0.20
    disposition_match: Literal["exact", "loose", "any"] = "exact"
    max_age_days: int = 7
    active_only: bool = True
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
    locality_district_id: int | None = None
    locality_region_id: int | None = None
    include_unreliable: bool = False

    parent_run_id: int | None = None
    rerun_reason: str | None = None

    @model_validator(mode="after")
    def _validate_url_xor_spec(self) -> "CreateEstimationIn":
        if (self.url is None) == (self.spec is None):
            raise ValueError(
                "Provide exactly one of 'url' or 'spec'"
            )
        return self


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
    purchase_price_czk: int | None = None
    radius_m: int = 1000
    area_band_pct: float = 0.20
    disposition_match: Literal["exact", "loose", "any"] = "exact"
    max_age_days: int = 7
    active_only: bool = True
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
    locality_district_id: int | None = None
    locality_region_id: int | None = None
    include_unreliable: bool = False


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


class AttachTagIn(BaseModel):
    tag_id: int
