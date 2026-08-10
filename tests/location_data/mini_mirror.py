"""An in-memory RÚIAN mini-mirror + policy fixtures for the resolver tests.

No database, no network: the pure core (S1-S7), the projection builders and the reconciler
all run against this. That is the whole point of `types.RegistryView` being a protocol —
the deterministic replay gate (06 §6.4 W1) has to be runnable in the normal pytest job.

The gazetteer content is the design's own named regression material: two Krásný Les obce
~100 km apart, Bílovec vs its de-accented form, and a Prague street whose name merely
CONTAINS a village name (the Bořislav 40 case).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from location_data.resolver.geo import haversine_m
from location_data.resolver.types import (
    AddressPoint,
    AdminUnit,
    Claim,
    ClusterEvidence,
    CollisionPolicyRow,
    FieldPolicyRow,
    LocationConstants,
    Parcel,
    ResolverContext,
    Street,
    UncertaintyPolicyRow,
)

# The seeded `location_constants.cz_bbox` row, as `load_constants` would return it.
CZ_BBOX = (12.0, 48.0, 19.0, 51.5)


@dataclass
class MiniMirror:
    """A `RegistryView` over python dicts."""

    units: list[AdminUnit] = field(default_factory=list)
    streets: list[Street] = field(default_factory=list)
    points: list[AddressPoint] = field(default_factory=list)
    parcels_: list[Parcel] = field(default_factory=list)
    # obec_kod -> (lat, lon, radius_m) polygon stand-in: a circle around the centre.
    obec_polygons: dict[int, tuple[float, float, float]] = field(default_factory=dict)
    cz_polygon: tuple[float, float, float] | None = (49.8, 15.5, 300_000.0)

    # ---- protocol
    def address_point(self, kod_adm: int) -> AddressPoint | None:
        return next((p for p in self.points if p.kod_adm == kod_adm), None)

    def address_points_by_number(
        self, *, obec_kod: int, street_name_norm: str | None,
        cislo_domovni: int | None, cislo_orientacni: int | None,
    ) -> list[AddressPoint]:
        return [
            p
            for p in self.points
            if p.obec_kod == obec_kod
            and (street_name_norm is None or p.street_name_norm == street_name_norm)
            and (cislo_domovni is None or p.cislo_domovni == cislo_domovni)
            and (cislo_orientacni is None or p.cislo_orientacni == cislo_orientacni)
        ]

    def streets_in_obec(self, obec_kod: int) -> list[Street]:
        return [s for s in self.streets if s.obec_kod == obec_kod]

    def admin_units_by_name(self, name_norm: str, *, levels: Sequence[str] = ()) -> list[AdminUnit]:
        return [
            u
            for u in self.units
            if u.name_norm == name_norm and (not levels or u.level in levels)
        ]

    def admin_unit_by_code(self, level: str, code: int) -> AdminUnit | None:
        return next((u for u in self.units if u.level == level and u.code == code), None)

    def admin_unit(self, unit_id: int) -> AdminUnit | None:
        return next((u for u in self.units if u.unit_id == unit_id), None)

    def admin_chain(self, unit_id: int) -> list[AdminUnit]:
        chain: list[AdminUnit] = []
        unit = self.admin_unit(unit_id)
        while unit is not None and unit.parent_id is not None:
            unit = self.admin_unit(unit.parent_id)
            if unit is None:
                break
            chain.append(unit)
        return chain

    def obec_codes_for_psc(self, psc: str) -> list[int]:
        return sorted({p.obec_kod for p in self.points if p.psc == psc})

    def parcels(self, *, katuz_name_norm: str, parcel_label_norm: str) -> list[Parcel]:
        katuz_ids = {
            u.unit_id
            for u in self.units
            if u.level == "katastralni_uzemi" and u.name_norm == katuz_name_norm
        }
        return [
            p
            for p in self.parcels_
            if p.katuz_unit_id in katuz_ids and p.parcel_label_norm == parcel_label_norm
        ]

    def containing_obec(self, lat: float, lon: float) -> AdminUnit | None:
        for code, (clat, clon, radius) in sorted(self.obec_polygons.items()):
            if haversine_m(lat, lon, clat, clon) <= radius:
                return self.admin_unit_by_code("obec", code)
        return None

    def nearest_obec_within(
        self, lat: float, lon: float, max_m: float
    ) -> tuple[AdminUnit, float] | None:
        best: tuple[AdminUnit, float] | None = None
        for code, (clat, clon, radius) in sorted(self.obec_polygons.items()):
            distance = max(0.0, haversine_m(lat, lon, clat, clon) - radius)
            unit = self.admin_unit_by_code("obec", code)
            if unit is None or distance > max_m:
                continue
            if best is None or distance < best[1]:
                best = (unit, distance)
        return best

    def distance_to_admin_boundary_m(self, unit_id: int, lat: float, lon: float) -> float | None:
        unit = self.admin_unit(unit_id)
        if unit is None or unit.code not in self.obec_polygons:
            return None
        clat, clon, radius = self.obec_polygons[unit.code]
        return abs(radius - haversine_m(lat, lon, clat, clon))

    def cast_obce_for_point(self, lat: float, lon: float) -> AdminUnit | None:
        nearest: tuple[float, AdminUnit] | None = None
        for point in self.points:
            if point.cast_obce_unit_id is None or point.lat is None or point.lon is None:
                continue
            distance = haversine_m(lat, lon, point.lat, point.lon)
            if distance > 250.0:
                continue
            unit = self.admin_unit(point.cast_obce_unit_id)
            if unit is not None and (nearest is None or distance < nearest[0]):
                nearest = (distance, unit)
        return None if nearest is None else nearest[1]

    def cast_obce_extent_m(self, cast_obce_kod: int) -> float | None:
        return None

    def in_czechia_polygon(self, lat: float, lon: float) -> bool | None:
        if self.cz_polygon is None:
            return None
        clat, clon, radius = self.cz_polygon
        return haversine_m(lat, lon, clat, clon) <= radius


class StaticCollision:
    """A `CollisionEvidenceView` keyed on the 4-dp cell, as one stamped epoch would be."""

    def __init__(self, clusters: dict[tuple[str, str], ClusterEvidence] | None = None) -> None:
        self._clusters = clusters or {}

    def for_point(self, source: str, lat: float, lon: float) -> ClusterEvidence | None:
        from location_data.resolver.collision import cell_of

        return self._clusters.get((source, cell_of(lat, lon)))


# --------------------------------------------------------------------------- policies

FIELD_POLICY: tuple[FieldPolicyRow, ...] = tuple(
    FieldPolicyRow(
        policy_version="v1", field=f, source_pattern=sp, method_pattern=mp, rank=rank,
        min_confidence=min_conf, may_fill_null=True, may_overwrite_non_null=overwrite,
        requires_independent_agreement=agree,
    )
    for f in (
        "coordinate", "address_point_id", "street_name", "house_number_cp", "house_number_co",
        "psc", "obec_name", "cast_obce_name", "okres_name", "kraj_name", "postal_town",
        "evidencni", "development_name", "cadastral_territory_name", "parcel_number",
    )
    for sp, mp, rank, min_conf, overwrite, agree in (
        ("ruian", "registry_derived", 100, None, True, False),
        ("portal:*", "portal_structured_field", 300, None, True, False),
        ("portal:*", "html_selector_parse", 400, None, True, False),
        ("portal:*", "url_slug_parse", 450, None, True, False),
        ("portal:*", "breadcrumb_parse", 450, None, True, False),
        ("llm_text", "llm_text", 900, "high", False, True),
    )
)

UNCERTAINTY_POLICY: tuple[UncertaintyPolicyRow, ...] = (
    UncertaintyPolicyRow("v1", "registry_point", "address_point", "*", 10, "geometric_bound", "constant"),
    UncertaintyPolicyRow("v1", "registry_point", "building", "*", 15, "geometric_bound", "constant"),
    UncertaintyPolicyRow("v1", "registry_point", "parcel", "*", 25, "geometric_bound", "constant"),
    UncertaintyPolicyRow("v1", "portal_pin", "address_point", "*", 15, "geometric_bound", "constant"),
    UncertaintyPolicyRow("v1", "portal_pin", "building", "*", 30, "geometric_bound", "constant"),
    UncertaintyPolicyRow("v1", "portal_pin", "street_segment", "*", 100, "geometric_bound", "constant"),
    UncertaintyPolicyRow("v1", "portal_pin", "street", "*", 300, "geometric_bound", "constant"),
    UncertaintyPolicyRow("v1", "portal_pin", "obec", "*", 1000, "geometric_bound", "constant"),
    UncertaintyPolicyRow("v1", "portal_pin", "cast_obce_or_quarter", "*", 750, "geometric_bound", "constant"),
    UncertaintyPolicyRow("v1", "portal_pin", "unknown", "*", 5000, "geometric_bound", "constant"),
    UncertaintyPolicyRow("v1", "portal_pin_blurred", "obec", "*", 1000, "declared", "declared_shape"),
    UncertaintyPolicyRow("v1", "portal_pin_blurred", "cast_obce_or_quarter", "*", 750, "declared", "declared_shape"),
    UncertaintyPolicyRow("v1", "portal_pin_blurred", "street", "*", 500, "declared", "declared_shape"),
    UncertaintyPolicyRow("v1", "portal_pin_blurred", "unknown", "*", 5000, "declared", "declared_shape"),
    UncertaintyPolicyRow("v1", "portal_pin_blurred", "building", "*", 300, "declared", "declared_shape"),
    UncertaintyPolicyRow("v1", "portal_pin_blurred", "street_segment", "*", 400, "declared", "declared_shape"),
    UncertaintyPolicyRow("v1", "admin_centroid", "obec", "*", None, "geometric_bound", "admin_containment_radius"),
    UncertaintyPolicyRow("v1", "admin_centroid", "cast_obce_or_quarter", "*", None, "geometric_bound", "admin_containment_radius"),
    UncertaintyPolicyRow("v1", "admin_centroid", "okres", "*", None, "geometric_bound", "admin_containment_radius"),
    UncertaintyPolicyRow("v1", "admin_centroid", "kraj", "*", None, "geometric_bound", "admin_containment_radius"),
    UncertaintyPolicyRow("v1", "carried_forward", "address_point", "*", None, "geometric_bound", "max_of_inputs"),
    UncertaintyPolicyRow("v1", "carried_forward", "building", "*", None, "geometric_bound", "max_of_inputs"),
    UncertaintyPolicyRow("v1", "carried_forward", "obec", "*", None, "geometric_bound", "max_of_inputs"),
    UncertaintyPolicyRow("v1", "carried_forward", "unknown", "*", None, "geometric_bound", "max_of_inputs"),
    UncertaintyPolicyRow("v1", "none", "unknown", "*", 250000, "geometric_bound", "constant"),
)

COLLISION_POLICY: tuple[CollisionPolicyRow, ...] = (
    CollisionPolicyRow("v1", "*", None, 4, 0, 2, "suspect"),
    CollisionPolicyRow("v1", "bezrealitky", None, 12, 0, 2, "legitimate_multiunit"),
)

CONSTANTS = LocationConstants(cz_bbox=CZ_BBOX)


# --------------------------------------------------------------------------- fixtures


def _unit(unit_id, level, code, name, name_norm, path, parent=None, lat=None, lon=None,
          psc_set=(), qualifier=None, homonym_count=1, radius=None) -> AdminUnit:
    return AdminUnit(
        unit_id=unit_id, level=level, code=code, name=name, name_norm=name_norm, path=path,
        display_path=name, parent_id=parent, lat=lat, lon=lon, psc_set=tuple(psc_set),
        qualifier=qualifier, homonym_count=homonym_count, containment_radius_m=radius,
    )


def default_mirror() -> MiniMirror:
    """One kraj/okres tree per regression case, plus a fully addressed obec."""
    units = [
        # --- Liberecký kraj / okres Liberec / Krásný Les (the RIGHT one)
        _unit(1, "kraj", 51, "Liberecký kraj", "liberecky kraj", "k51"),
        _unit(2, "okres", 3506, "Liberec", "liberec", "k51.o3506", parent=1),
        _unit(3, "obec", 563943, "Krásný Les", "krasny les", "k51.o3506.b563943", parent=2,
              lat=50.9330, lon=15.1500, psc_set=("46346",), homonym_count=2, radius=3000.0),
        _unit(4, "katastralni_uzemi", 673986, "Krásný Les u Frýdlantu",
              "krasny les u frydlantu", "k51.o3506.b563943", parent=3),
        # --- Ústecký kraj / okres Ústí nad Labem / Krásný Les (the WRONG one, ~100 km west)
        _unit(5, "kraj", 42, "Ústecký kraj", "ustecky kraj", "k42"),
        _unit(6, "okres", 3805, "Ústí nad Labem", "usti nad labem", "k42.o3805", parent=5),
        _unit(7, "obec", 567931, "Krásný Les", "krasny les", "k42.o3805.b567931", parent=6,
              lat=50.7676, lon=13.9353, psc_set=("40302",), homonym_count=2, radius=3000.0),
        # --- Moravskoslezský kraj / okres Nový Jičín / Bílovec
        _unit(8, "kraj", 80, "Moravskoslezský kraj", "moravskoslezsky kraj", "k80"),
        _unit(9, "okres", 3804, "Nový Jičín", "novy jicin", "k80.o3804", parent=8),
        _unit(10, "obec", 599212, "Bílovec", "bilovec", "k80.o3804.b599212", parent=9,
              lat=49.7573, lon=18.0158, psc_set=("74301",), radius=4000.0),
        # --- Praha (street-name-contains-village trap) + Bořislav village
        _unit(11, "kraj", 19, "Hlavní město Praha", "hlavni mesto praha", "k19"),
        _unit(12, "okres", 3100, "Hlavní město Praha", "hlavni mesto praha", "k19.o3100", parent=11),
        _unit(13, "obec", 554782, "Praha", "praha", "k19.o3100.b554782", parent=12,
              lat=50.0755, lon=14.4378, psc_set=("16000", "18000"), radius=12000.0),
        _unit(14, "cast_obce", 490067, "Vokovice", "vokovice", "k19.o3100.b554782.c490067",
              parent=13, lat=50.1010, lon=14.3480),
        _unit(15, "obec", 567639, "Bořislav", "borislav", "k42.o3805.b567639", parent=6,
              lat=50.5794, lon=13.9200, psc_set=("41502",), radius=2000.0),
    ]
    streets = [
        Street(street_id=1, code=101, name="Nad Bořislavkou", name_norm="nad borislavkou",
               obec_unit_id=13, obec_kod=554782),
        Street(street_id=2, code=102, name="28. října", name_norm="28 rijna",
               obec_unit_id=13, obec_kod=554782),
        Street(street_id=3, code=103, name="Slunečná", name_norm="slunecna",
               obec_unit_id=10, obec_kod=599212),
    ]
    points = [
        AddressPoint(
            kod_adm=21690278, obec_unit_id=13, obec_kod=554782, psc="16000",
            lat=50.10100, lon=14.34800, street_id=1, ulice_kod=101,
            street_name_norm="nad borislavkou", cislo_domovni=487, cislo_orientacni=40,
            stavebni_objekt_code=555001, cast_obce_unit_id=14, cast_obce_kod=490067,
        ),
        AddressPoint(
            kod_adm=21690279, obec_unit_id=13, obec_kod=554782, psc="16000",
            lat=50.10110, lon=14.34810, street_id=1, ulice_kod=101,
            street_name_norm="nad borislavkou", cislo_domovni=488, cislo_orientacni=41,
            stavebni_objekt_code=555002, cast_obce_unit_id=14, cast_obce_kod=490067,
        ),
        AddressPoint(
            kod_adm=33000001, obec_unit_id=10, obec_kod=599212, psc="74301",
            lat=49.75740, lon=18.01590, street_id=3, ulice_kod=103,
            street_name_norm="slunecna", cislo_domovni=12,
            stavebni_objekt_code=556001,
        ),
    ]
    return MiniMirror(
        units=units,
        streets=streets,
        points=points,
        obec_polygons={
            563943: (50.9330, 15.1500, 3000.0),
            567931: (50.7676, 13.9353, 3000.0),
            599212: (49.7573, 18.0158, 4000.0),
            554782: (50.0755, 14.4378, 12000.0),
            567639: (50.5794, 13.9200, 2000.0),
        },
    )


def context(
    mirror: MiniMirror | None = None,
    *,
    collision=None,
    constants: LocationConstants | None = None,
    previous_position=None,
) -> ResolverContext:
    return ResolverContext(
        registry=mirror or default_mirror(),
        constants=constants or CONSTANTS,
        field_policy=FIELD_POLICY,
        uncertainty_policy=UNCERTAINTY_POLICY,
        collision_policy=COLLISION_POLICY,
        collision=collision,
        previous_position=previous_position,
    )


_T0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def claim(
    claim_id: int,
    claim_type: str,
    *,
    listing_id: int = 900001,
    source: str = "sreality",
    value_text: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    extraction_method: str = "portal_structured_field",
    surface: str = "api_json",
    licence_class: str = "portal",
    subject_scoped: bool | None = True,
    declared_precision_label: str | None = None,
    declared_radius_m: float | None = None,
    blur_evidence: str = "none",
    claim_confidence: str | None = "high",
    minutes: int = 0,
) -> Claim:
    return Claim(
        id=claim_id, listing_id=listing_id, source=source, claim_type=claim_type,
        surface=surface, extraction_method=extraction_method, extractor_id=f"fx.{claim_type}",
        licence_class=licence_class,
        observed_at=_T0.replace(minute=_T0.minute) if minutes == 0 else _T0,
        value_text=value_text, lat=lat, lon=lon, subject_scoped=subject_scoped,
        declared_precision_label=declared_precision_label,
        declared_radius_m=declared_radius_m, blur_evidence=blur_evidence,
        claim_confidence=claim_confidence,
    )
