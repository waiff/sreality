"""The value objects the resolver passes between stages, and the registry interface.

Everything here is plain data. The registry is a PROTOCOL, not a connection: the pure core
asks `RegistryView` questions ("which address point is kód ADM 21690278?", "which obec
polygon covers this point?") and the two implementations answer them from psycopg
(`resolve_db.SqlRegistryView`) or from fixtures (`tests/location_data/mini_mirror.py`).
That is what keeps S1-S7 runnable with no database and makes the replay gate hermetic.

Vocabularies are the enums of migration 380 and are never re-spelled here: `granularity`
is a `location_granularity` label, `position_source` a `position_source` label, and so on.
Ordinal comparisons go through `GranularityRank` (the `location_granularity_rank` table),
never through Python string ordering and never through the enum's own ordinality
(01 §0.4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

# Mirrors migration 380's location_granularity_rank seed. The DB table is the authority;
# this is the offline default the pure core and the fixtures use, and `GranularityRank`
# always carries whichever mapping was loaded (01 §0.4: a persisted comparison is a rank
# comparison, never an enum-ordinality one).
DEFAULT_GRANULARITY_RANK: dict[str, int] = {
    "unknown": 0,
    "country": 10,
    "kraj": 20,
    "okres": 30,
    "obec": 40,
    "cast_obce_or_quarter": 50,
    "street": 60,
    "street_segment": 70,
    "parcel": 80,
    "building": 90,
    "address_point": 100,
}

MATCH_CONFIDENCE_ORDER: tuple[str, ...] = ("low", "medium", "high", "exact")


class GranularityRank:
    """`location_granularity_rank` as a comparator. The ONE way a rank comparison is made."""

    def __init__(self, ranks: dict[str, int] | None = None) -> None:
        self._ranks = dict(ranks or DEFAULT_GRANULARITY_RANK)

    def rank(self, granularity: str) -> int:
        try:
            return self._ranks[granularity]
        except KeyError as exc:  # a rung with no rank row is a schema bug, never a default
            raise KeyError(f"no location_granularity_rank row for {granularity!r}") from exc

    def at_least(self, granularity: str, floor: str) -> bool:
        return self.rank(granularity) >= self.rank(floor)

    def coarser_of(self, a: str, b: str) -> str:
        return a if self.rank(a) <= self.rank(b) else b

    def finer_of(self, a: str, b: str) -> str:
        return a if self.rank(a) >= self.rank(b) else b


# --------------------------------------------------------------------------- claims


@dataclass(frozen=True, slots=True)
class Claim:
    """One `location_claims_live` row, as the resolver consumes it (01 §4.2)."""

    id: int
    listing_id: int
    source: str
    claim_type: str
    surface: str
    extraction_method: str
    extractor_id: str
    licence_class: str
    observed_at: datetime
    value_text: str | None = None
    value_num: float | None = None
    lat: float | None = None
    lon: float | None = None
    value_jsonb: dict[str, Any] = field(default_factory=dict)
    declared_precision_label: str | None = None
    declared_confidence: str | None = None
    declared_radius_m: float | None = None
    blur_evidence: str = "none"
    claim_confidence: str | None = None
    subject_scoped: bool | None = None
    page_kind: str = "none"
    snapshot_id: int | None = None
    distance_m: int | None = None
    target_text: str | None = None

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass(frozen=True, slots=True)
class NormalizedClaim:
    """S1's derived sidecar — one per claim, never written back onto the claim (03 §3.3)."""

    claim_id: int
    claim_type: str
    value_verbatim: str | None
    value_cf: str | None
    value_ascii: str | None
    typed_slots: dict[str, Any]
    rejections: tuple[str, ...] = ()

    @property
    def rejected(self) -> bool:
        return bool(self.rejections)


# --------------------------------------------------------------------------- registry


@dataclass(frozen=True, slots=True)
class AdminUnit:
    unit_id: int
    level: str
    code: int
    name: str
    name_norm: str
    path: str
    display_path: str
    parent_id: int | None = None
    lat: float | None = None
    lon: float | None = None
    okres_kod: int | None = None
    kraj_kod: int | None = None
    obec_kod: int | None = None
    qualifier: str | None = None
    homonym_count: int = 1
    psc_set: tuple[str, ...] = ()
    containment_radius_m: float | None = None


@dataclass(frozen=True, slots=True)
class AddressPoint:
    kod_adm: int
    obec_unit_id: int
    obec_kod: int
    psc: str
    lat: float | None
    lon: float | None
    street_id: int | None = None
    ulice_kod: int | None = None
    street_name_norm: str | None = None
    cislo_domovni: int | None = None
    cislo_orientacni: int | None = None
    znak_orientacniho: str | None = None
    stavebni_objekt_code: int | None = None
    cast_obce_unit_id: int | None = None
    cast_obce_kod: int | None = None
    momc_unit_id: int | None = None


@dataclass(frozen=True, slots=True)
class Street:
    street_id: int
    code: int
    name: str
    name_norm: str
    obec_unit_id: int
    obec_kod: int


@dataclass(frozen=True, slots=True)
class Parcel:
    parcel_id: int
    code: int
    katuz_unit_id: int
    parcel_label_norm: str
    lat: float | None
    lon: float | None
    obec_kod: int | None = None


class RegistryView(Protocol):
    """The RÚIAN mirror as the resolver sees it, pinned to one `registry_version_id`."""

    def address_point(self, kod_adm: int) -> AddressPoint | None: ...

    def address_points_by_number(
        self,
        *,
        obec_kod: int,
        street_name_norm: str | None,
        cislo_domovni: int | None,
        cislo_orientacni: int | None,
    ) -> Sequence[AddressPoint]: ...

    def streets_in_obec(self, obec_kod: int) -> Sequence[Street]: ...

    def admin_units_by_name(
        self, name_norm: str, *, levels: Sequence[str] = ()
    ) -> Sequence[AdminUnit]: ...

    def admin_unit_by_code(self, level: str, code: int) -> AdminUnit | None: ...

    def admin_unit(self, unit_id: int) -> AdminUnit | None: ...

    def admin_chain(self, unit_id: int) -> Sequence[AdminUnit]: ...

    def obec_codes_for_psc(self, psc: str) -> Sequence[int]: ...

    def parcels(self, *, katuz_name_norm: str, parcel_label_norm: str) -> Sequence[Parcel]: ...

    def containing_obec(self, lat: float, lon: float) -> AdminUnit | None:
        """`ST_Covers` against the AUTHORITATIVE polygon (never the simplified one)."""

    def nearest_obec_within(
        self, lat: float, lon: float, max_m: float
    ) -> tuple[AdminUnit, float] | None:
        """The `pip_nearest_within_n_m` sliver fallback (01 §2.2, 250 m)."""

    def distance_to_admin_boundary_m(self, unit_id: int, lat: float, lon: float) -> float | None: ...

    def cast_obce_for_point(self, lat: float, lon: float) -> AdminUnit | None:
        """ČástObce has NO polygon in RÚIAN (03 §3.7.4) — membership is a code predicate
        over the address-point set, never a polygon test and never a faked hull."""

    def cast_obce_extent_m(self, cast_obce_kod: int) -> float | None: ...

    def in_czechia_polygon(self, lat: float, lon: float) -> bool | None:
        """Containment in the RÚIAN state polygon. `None` = not loaded, so no signal."""


# --------------------------------------------------------------------------- policy


@dataclass(frozen=True, slots=True)
class FieldPolicyRow:
    """`location_field_policy` (01 §6.2). `may_overwrite_non_null` and
    `requires_independent_agreement` are D7's graded write-back guard — never dropped."""

    policy_version: str
    field: str
    source_pattern: str
    method_pattern: str
    rank: int
    min_granularity: str | None = None
    min_confidence: str | None = None
    max_age_days: int | None = None
    may_fill_null: bool = True
    may_overwrite_non_null: bool = False
    requires_independent_agreement: bool = False
    tie_breaker: str = "granularity_then_rank_then_recency"


@dataclass(frozen=True, slots=True)
class UncertaintyPolicyRow:
    policy_version: str
    position_source: str
    granularity: str
    source: str
    r95_m: float | None
    radius_semantics: str
    derivation: str


@dataclass(frozen=True, slots=True)
class CollisionPolicyRow:
    policy_version: str
    source: str
    obec_kod: int | None
    threshold_n: int
    radius_m: int
    min_distinct_streets: int
    pin_collision_semantics: str


@dataclass(frozen=True, slots=True)
class LocationConstants:
    """`location_constants` (01 §2.2). The CZ bbox lives HERE and nowhere else — the repo's
    six independent copies are exactly what this row exists to collapse."""

    cz_bbox: tuple[float, float, float, float]  # (lon_min, lat_min, lon_max, lat_max)
    cz_bbox_trigger_buffer_deg: float = 0.05
    pip_sliver_tolerance_m: float = 250.0
    registry_pin_conflict_m: float = 300.0
    # UNCALIBRATED (03 OQ3): position_quality_class's two cuts have no measurement behind
    # them and no seed row in migration 380. Conservative defaults, overridable as data.
    precise_r95_m: float = 30.0
    approx_r95_m: float = 300.0

    def in_bbox(self, lat: float, lon: float) -> bool:
        lon_min, lat_min, lon_max, lat_max = self.cz_bbox
        return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max

    def near_bbox(self, lat: float, lon: float) -> bool:
        lon_min, lat_min, lon_max, lat_max = self.cz_bbox
        b = self.cz_bbox_trigger_buffer_deg
        return (lat_min - b) <= lat <= (lat_max + b) and (lon_min - b) <= lon <= (lon_max + b)


@dataclass(frozen=True, slots=True)
class ClusterEvidence:
    """One `pin_clusters` row as S6 consumes it, plus the two wider radii."""

    cluster_id: int | None
    source: str
    cell_key: str
    listing_count: int
    distinct_streets: int
    distinct_obec_kods: int
    classification: str
    n_25m: int = 1
    n_100m: int = 1
    distance_to_admin_centroid_m: float | None = None
    declared_blur_share: float | None = None

    @property
    def heterogeneity(self) -> int:
        return self.distinct_streets

    @property
    def heterogeneity_ok(self) -> bool:
        """≤1 distinct normalized street key in the cluster (00 §7.3)."""
        return self.distinct_streets <= 1


class CollisionEvidenceView(Protocol):
    def for_point(self, source: str, lat: float, lon: float) -> ClusterEvidence | None: ...


@dataclass(frozen=True, slots=True)
class ResolverContext:
    """Everything the pure resolver reads besides the claims themselves."""

    registry: RegistryView
    constants: LocationConstants
    field_policy: tuple[FieldPolicyRow, ...]
    uncertainty_policy: tuple[UncertaintyPolicyRow, ...]
    collision_policy: tuple[CollisionPolicyRow, ...]
    collision: CollisionEvidenceView | None = None
    granularity_rank: GranularityRank = field(default_factory=GranularityRank)
    previous_position: "Position | None" = None  # for position_source='carried_forward'

    def collision_threshold(self, source: str, obec_kod: int | None) -> CollisionPolicyRow:
        """`threshold_from(location_collision_policy, source, obec_kod)` (00 §7.3).
        Most specific first: (source, obec) > (source, *) > (*, obec) > (*, *)."""
        best: CollisionPolicyRow | None = None
        best_score = -1
        for row in self.collision_policy:
            if row.source not in ("*", source):
                continue
            if row.obec_kod is not None and row.obec_kod != obec_kod:
                continue
            score = (2 if row.source == source else 0) + (1 if row.obec_kod is not None else 0)
            if score > best_score:
                best, best_score = row, score
        if best is None:
            raise LookupError("location_collision_policy has no matching row, not even ('*', NULL)")
        return best


# --------------------------------------------------------------------------- results


@dataclass(frozen=True, slots=True)
class CountryDetermination:
    country_code: str | None
    status: str
    confidence: str
    method: str
    driving_claim_ids: tuple[int, ...] = ()
    conflicting: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class Candidate:
    """A `location_resolution_candidates` row (01 §6.1). The set is stored COMPLETE — a
    non-winning POSITION is a candidate row too (00 §11.2: there is no place_position)."""

    rung: str
    rank: int
    score: float
    target_kind: str
    granularity: str
    position_source: str
    match_confidence: str
    uncertainty_radius_m: float
    radius_semantics: str
    licence_class: str = "cc_by_ruian"
    blur_evidence: str = "none"
    lat: float | None = None
    lon: float | None = None
    ruian_adm_kod: int | None = None
    stavebni_objekt_kod: int | None = None
    parcela_id: int | None = None
    # BOTH currencies (00 §7.1): `ulice_kod` is the stable code the projection serves,
    # `ulice_id` the mirror surrogate the candidate row's FK needs.
    ulice_kod: int | None = None
    ulice_id: int | None = None
    admin_unit_id: int | None = None
    component_match: dict[str, str] = field(default_factory=dict)
    distance_to_pin_m: float | None = None
    rejected_reason: str | None = None
    source_claim_ids: tuple[int, ...] = ()
    relaxations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CandidateSet:
    candidates: tuple[Candidate, ...]
    ambiguity_status: str  # resolved | ambiguous | unmatched
    top_granularity: str
    constraining: dict[str, Any] = field(default_factory=dict)
    rung_trace: tuple[dict[str, Any], ...] = ()
    runner_up_score_gap: float | None = None


@dataclass(frozen=True, slots=True)
class Position:
    lat: float | None
    lon: float | None
    position_source: str
    blur_evidence: str
    licence_class: str
    granularity: str
    match_confidence: str
    uncertainty_radius_m: float
    radius_semantics: str
    winner_candidate_rank: int | None = None
    source_claim_ids: tuple[int, ...] = ()
    cross_check: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdminAssignment:
    method: str
    position_source: str
    sliver_distance_m: float | None = None
    distance_to_nearest_boundary_m: float | None = None
    obec_kod: int | None = None
    obec_unit_id: int | None = None
    obec_name: str | None = None
    cast_obce_kod: int | None = None
    cast_obce_unit_id: int | None = None
    cast_obce_name: str | None = None
    momc_kod: int | None = None
    ku_kod: int | None = None
    pou_kod: int | None = None
    orp_kod: int | None = None
    okres_kod: int | None = None
    okres_unit_id: int | None = None
    okres_name: str | None = None
    kraj_kod: int | None = None
    kraj_unit_id: int | None = None
    kraj_name: str | None = None
    admin_path: str | None = None
    display_path: str | None = None


@dataclass(frozen=True, slots=True)
class Precision:
    granularity: str
    position_source: str
    match_confidence: str
    blur_evidence: str
    uncertainty_radius_m: float
    radius_semantics: str
    position_quality_class: str
    match_components: dict[str, str] = field(default_factory=dict)
    collision: dict[str, Any] = field(default_factory=dict)
    declared_caps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FieldWinner:
    field: str
    value: Any
    source_claim_ids: tuple[int, ...]
    rule: str
    method: str
    granularity: str | None = None
    confidence: str | None = None


@dataclass(frozen=True, slots=True)
class ContradictionSignal:
    """S4/S6 hand these to S9; the resolver itself never writes the ledger (03 §3.11)."""

    rule: str
    field: str
    severity: str
    stored: Any = None
    claimed: Any = None
    distance_m: float | None = None
    evidence_claim_ids: tuple[int, ...] = ()
    auto_action: str = "none"
    evidence_quote: str | None = None


@dataclass(frozen=True, slots=True)
class Resolution:
    """The S7 -> S8 payload: exactly one `location_resolutions` row plus its candidates."""

    listing_id: int
    source: str
    status: str
    as_of: datetime | None
    claim_set_hash: str
    content_hash: str
    resolver_version: str
    registry_version_id: int
    policy_version: str
    collision_epoch_id: int
    country: CountryDetermination
    position: Position
    admin: AdminAssignment
    precision: Precision
    candidates: tuple[Candidate, ...]
    chosen_rank: int | None
    chosen_rule: str
    runner_up_score_gap: float | None
    fields: dict[str, FieldWinner]
    input_claim_ids: tuple[int, ...]
    position_licence_class: str
    contradiction_signals: tuple[ContradictionSignal, ...] = ()
    rung_trace: tuple[dict[str, Any], ...] = ()
