"""The value objects the four steps pass between themselves, and the registry interface.

Everything here is plain data. The registry is a PROTOCOL, not a connection: the pure core
asks `RegistryView` questions ("which address point is kód ADM 21690278?", "which obec
polygon covers this point?") and the two implementations answer them from psycopg
(`resolve_db.SqlRegistryView`) or from fixtures (`tests/location_data/mini_mirror.py`).
That is what keeps BIND → FILL → GRADE → CHECK runnable with no database.

The protocol is NINE questions (W2-a, down from fifteen). Four went with the engines they
served — parcels (the rung was unreachable), the pin-collision clusters, the boundary
distance and the ČástObce point lookup — and two folded into `admin_chain`, which now
returns the unit itself ahead of its ancestors so "give me this unit" and "give me its
chain" are one round trip instead of two.

Vocabularies are the enums of migration 380 and are never re-spelled here: `granularity`
is a `location_granularity` label and `match_confidence` a `match_confidence` one. Ordinal
comparisons go through `GranularityRank`, never through Python string ordering and never
through the enum's own ordinality (01 §0.4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

# Mirrors migration 380's location_granularity_rank seed. The TABLE survives (it is read by
# `toolkit/dedup_candidates_sql.py`), but the resolver no longer spends a round trip loading
# a mapping that is fixed by the enum's own declaration order.
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


def cap_confidence(value: str | None, ceiling: str) -> str:
    """The ONE confidence comparison. `low` for anything the vocabulary does not know."""
    current = value if value in MATCH_CONFIDENCE_ORDER else "low"
    if MATCH_CONFIDENCE_ORDER.index(current) <= MATCH_CONFIDENCE_ORDER.index(ceiling):
        return current
    return ceiling


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
    """One `location_claims` row, as the resolver consumes it (01 §4.2).

    The last six fields are no longer SELECTed (W1-b, migration 498). They keep their names
    and defaults so a fixture can still spell them without a DB column.
    """

    id: int
    listing_id: int
    source: str
    claim_type: str
    surface: str
    extraction_method: str
    licence_class: str
    observed_at: datetime
    value_text: str | None = None
    value_num: float | None = None
    lat: float | None = None
    lon: float | None = None
    value_jsonb: dict[str, Any] = field(default_factory=dict)
    declared_precision_label: str | None = None
    declared_radius_m: float | None = None
    blur_evidence: str = "none"
    claim_confidence: str | None = None
    subject_scoped: bool | None = None
    extractor_id: str = ""
    declared_confidence: str | None = None
    page_kind: str = "none"
    snapshot_id: int | None = None
    distance_m: int | None = None
    target_text: str | None = None

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass(frozen=True, slots=True)
class NormalizedClaim:
    """The normalizer's derived sidecar — one per claim, never written back (03 §3.3)."""

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
    parent_id: int | None = None
    lat: float | None = None
    lon: float | None = None
    okres_kod: int | None = None
    kraj_kod: int | None = None
    obec_kod: int | None = None
    qualifier: str | None = None
    homonym_count: int = 1
    psc_set: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AddressPoint:
    kod_adm: int
    obec_unit_id: int
    obec_kod: int
    psc: str
    lat: float | None
    lon: float | None
    ulice_kod: int | None = None
    street_name_norm: str | None = None
    # The OFFICIAL display form (`ruian_streets.name`), not the match key: on a
    # registry-bound row it is what gets served (03 §3.9.3).
    street_name: str | None = None
    cislo_domovni: int | None = None
    cislo_orientacni: int | None = None
    znak_orientacniho: str | None = None
    cast_obce_unit_id: int | None = None
    cast_obce_kod: int | None = None


@dataclass(frozen=True, slots=True)
class Street:
    code: int
    name: str
    name_norm: str
    obec_kod: int


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

    def admin_chain(self, unit_id: int) -> Sequence[AdminUnit]:
        """The unit ITSELF, then its ancestors coarsest-last. Empty = no such unit."""

    def admin_chain_by_code(self, level: str, code: int) -> Sequence[AdminUnit]:
        """`admin_chain` addressed by (level, code) — the shape a portal's own obec code
        arrives in. Same one statement, so a code costs one trip, not two."""

    def obec_codes_for_psc(self, psc: str) -> Sequence[int]: ...

    def containing_obec(self, lat: float, lon: float) -> AdminUnit | None:
        """`ST_Covers` against the AUTHORITATIVE polygon (never the simplified one). It does
        double duty: BIND's reverse-geocode rung and CHECK's pin-inside-the-town test."""

    def nearest_obec_within(
        self, lat: float, lon: float, max_m: float
    ) -> tuple[AdminUnit, float] | None:
        """The sliver fallback: the obec whose polygon comes closest to a point that is
        inside none of them, with its distance, or None past `max_m`. BIND's LAST rung — it
        is what keeps a border pin from having no town at all."""

    def in_czechia_polygon(self, lat: float, lon: float) -> bool | None:
        """Containment in the RÚIAN state polygon. `None` = not loaded, so no signal."""


@dataclass(frozen=True, slots=True)
class ResolverContext:
    """Everything the pure resolver reads besides the claims themselves.

    Two fields, and that is the point: every policy TABLE the old context carried
    (`location_field_policy`, `location_uncertainty_policy`, `location_collision_policy`,
    `location_constants`, `location_granularity_rank`) is either a code constant now or
    deleted with the engine that read it.
    """

    registry: RegistryView
    granularity_rank: GranularityRank = field(default_factory=GranularityRank)


# --------------------------------------------------------------------------- the steps


@dataclass(frozen=True, slots=True)
class Binding:
    """BIND's answer: the FINEST registry entity the claims justify, and what justified it.

    `agreed` names the INDEPENDENT fields that matched the entity — it is GRADE's whole
    input. `relaxations` names the qualifiers that had to be applied to get here; some of
    them (a coordinate tie-break, a post-town guess) cap the answer at `low` however many
    fields agreed.
    """

    target_kind: str  # address_point | street | admin_unit | none
    granularity: str
    rung: str
    ruian_adm_kod: int | None = None
    ulice_kod: int | None = None
    admin_unit_id: int | None = None
    obec_kod: int | None = None
    street_name: str | None = None
    lat: float | None = None
    lon: float | None = None
    cast_obce_unit_id: int | None = None
    agreed: tuple[str, ...] = ()
    relaxations: tuple[str, ...] = ()
    ambiguous: bool = False
    source_claim_ids: tuple[int, ...] = ()

    @property
    def bound(self) -> bool:
        return self.target_kind != "none"

    @property
    def pin_derived(self) -> bool:
        """The town came from the PIN (reverse geocode, or the sliver fallback) rather than
        from a claim. CHECK's pin-inside-the-town comparison is circular on such a row — the
        pin IS where the town came from — so it is not made."""
        return self.rung in ("R7", "R8")


@dataclass(frozen=True, slots=True)
class Position:
    """The one coordinate the row publishes, and where it came from.

    `origin` is internal to the resolver — the answer table stores the point and its radius,
    not a provenance vocabulary. It survives here because GRADE reads it (a blurred portal
    pin cannot grade above `medium`) and CHECK reads it (only a PIN can fall outside the
    town it claims; a registry point is inside by construction).
    """

    lat: float | None
    lon: float | None
    origin: str  # registry_point | portal_pin | admin_centroid | none
    blurred: bool = False
    registry_pin_distance_m: float | None = None
    source_claim_ids: tuple[int, ...] = ()

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass(frozen=True, slots=True)
class Fill:
    """FILL's answer: the hierarchy, joined off the bound ids. Names are ALWAYS the
    registry's own spelling; only čp/čo/PSČ/street may fall back to a claim."""

    kraj_kod: int | None = None
    okres_kod: int | None = None
    obec_kod: int | None = None
    cast_obce_kod: int | None = None
    ulice_kod: int | None = None
    ruian_adm_kod: int | None = None
    kraj_name: str | None = None
    okres_name: str | None = None
    obec_name: str | None = None
    cast_obce_name: str | None = None
    street_name: str | None = None
    house_number_cp: str | None = None
    house_number_co: str | None = None
    psc: str | None = None


@dataclass(frozen=True, slots=True)
class Grade:
    """GRADE's answer: one granularity, one confidence, one radius."""

    granularity: str
    match_confidence: str
    uncertainty_radius_m: float


@dataclass(frozen=True, slots=True)
class Verdict:
    """CHECK's answer: which country this is, and whether anything about the row disagrees
    with itself. `disputed` is ONE lower_snake word, or None for a clean row."""

    country_status: str  # cz | foreign | undetermined
    country_code: str | None = None
    disputed: str | None = None
    granularity: str | None = None  # set when the check coarsens the grade


@dataclass(frozen=True, slots=True)
class Resolution:
    """ONE `listing_location` row. `source` rides along for the log line and is NOT a
    column — it is one primary-key join away on `listings`."""

    listing_id: int
    source: str
    lat: float | None
    lon: float | None
    country_code: str | None
    kraj_name: str | None
    okres_name: str | None
    obec_name: str | None
    cast_obce_name: str | None
    street_name: str | None
    house_number_cp: str | None
    house_number_co: str | None
    psc: str | None
    kraj_kod: int | None
    okres_kod: int | None
    obec_kod: int | None
    cast_obce_kod: int | None
    ulice_kod: int | None
    ruian_adm_kod: int | None
    match_confidence: str
    granularity: str
    uncertainty_radius_m: float
    country_status: str
    disputed: str | None
    resolver_version: str
    claim_set_hash: str
    registry_version: str
