"""An in-memory RÚIAN mini-mirror for the resolver tests.

No database, no network: the four steps all run against this. That is the whole point of
`types.RegistryView` being a protocol.

The gazetteer content is the design's own named regression material: two Krásný Les obce
~100 km apart, Bílovec vs its de-accented form, and a Prague street whose name merely
CONTAINS a village name (the Bořislav 40 case).

W2-a shrank this file by more than half, and the deletions say what the wave did: the four
CONFIG fixtures are gone (`location_field_policy`, `location_uncertainty_policy`,
`location_collision_policy`, `location_constants` — policy is code now), and so are the
registry answers whose questions went with them (parcels, the pin clusters, the boundary
distance, the nearest-obec sliver, the ČástObce point lookup). What is left is the EIGHT
questions the protocol still declares.
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
    ResolverContext,
    Street,
)

# The Czech bounding box the resolver carries as a code constant (`check.CZ_BBOX`).
CZ_BBOX = (12.0, 48.0, 19.0, 51.5)


@dataclass
class MiniMirror:
    """A `RegistryView` over python dicts."""

    units: list[AdminUnit] = field(default_factory=list)
    streets: list[Street] = field(default_factory=list)
    points: list[AddressPoint] = field(default_factory=list)
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

    def admin_chain(self, unit_id: int) -> list[AdminUnit]:
        """The unit ITSELF first, then its ancestors — the shape `_ADMIN_CHAIN_SQL` returns
        now that `admin_unit` and `admin_unit_by_code` are folded into it."""
        unit = self._unit_by_id(unit_id)
        if unit is None:
            return []
        chain = [unit]
        while unit is not None and unit.parent_id is not None:
            unit = self._unit_by_id(unit.parent_id)
            if unit is None:
                break
            chain.append(unit)
        return chain

    def admin_chain_by_code(self, level: str, code: int) -> list[AdminUnit]:
        unit = next((u for u in self.units if u.level == level and u.code == code), None)
        return [] if unit is None else self.admin_chain(unit.unit_id)

    def obec_codes_for_psc(self, psc: str) -> list[int]:
        return sorted({p.obec_kod for p in self.points if p.psc == psc})

    def containing_obec(self, lat: float, lon: float) -> AdminUnit | None:
        for code, (clat, clon, radius) in sorted(self.obec_polygons.items()):
            if haversine_m(lat, lon, clat, clon) <= radius:
                return next(
                    (u for u in self.units if u.level == "obec" and u.code == code), None
                )
        return None

    def in_czechia_polygon(self, lat: float, lon: float) -> bool | None:
        if self.cz_polygon is None:
            return None
        clat, clon, radius = self.cz_polygon
        return haversine_m(lat, lon, clat, clon) <= radius

    # ---- fixture helper, not part of the protocol
    def _unit_by_id(self, unit_id: int) -> AdminUnit | None:
        return next((u for u in self.units if u.unit_id == unit_id), None)


# --------------------------------------------------------------------------- fixtures


def _unit(unit_id, level, code, name, name_norm, path, parent=None, lat=None, lon=None,
          psc_set=(), qualifier=None, homonym_count=1) -> AdminUnit:
    return AdminUnit(
        unit_id=unit_id, level=level, code=code, name=name, name_norm=name_norm, path=path,
        parent_id=parent, lat=lat, lon=lon, psc_set=tuple(psc_set),
        qualifier=qualifier, homonym_count=homonym_count,
    )


def default_mirror() -> MiniMirror:
    """One kraj/okres tree per regression case, plus a fully addressed obec."""
    units = [
        # --- Liberecký kraj / okres Liberec / Krásný Les (the RIGHT one)
        _unit(1, "kraj", 51, "Liberecký kraj", "liberecky kraj", "k51"),
        _unit(2, "okres", 3506, "Liberec", "liberec", "k51.o3506", parent=1),
        _unit(3, "obec", 563943, "Krásný Les", "krasny les", "k51.o3506.b563943", parent=2,
              lat=50.9330, lon=15.1500, psc_set=("46346",), homonym_count=2),
        _unit(4, "katastralni_uzemi", 673986, "Krásný Les u Frýdlantu",
              "krasny les u frydlantu", "k51.o3506.b563943", parent=3),
        # --- Ústecký kraj / okres Ústí nad Labem / Krásný Les (the WRONG one, ~100 km west)
        _unit(5, "kraj", 42, "Ústecký kraj", "ustecky kraj", "k42"),
        _unit(6, "okres", 3805, "Ústí nad Labem", "usti nad labem", "k42.o3805", parent=5),
        _unit(7, "obec", 567931, "Krásný Les", "krasny les", "k42.o3805.b567931", parent=6,
              lat=50.7676, lon=13.9353, psc_set=("40302",), homonym_count=2),
        # --- Moravskoslezský kraj / okres Nový Jičín / Bílovec
        _unit(8, "kraj", 80, "Moravskoslezský kraj", "moravskoslezsky kraj", "k80"),
        _unit(9, "okres", 3804, "Nový Jičín", "novy jicin", "k80.o3804", parent=8),
        _unit(10, "obec", 599212, "Bílovec", "bilovec", "k80.o3804.b599212", parent=9,
              lat=49.7573, lon=18.0158, psc_set=("74301",)),
        # --- Praha (street-name-contains-village trap) + Bořislav village
        _unit(11, "kraj", 19, "Hlavní město Praha", "hlavni mesto praha", "k19"),
        _unit(12, "okres", 3100, "Hlavní město Praha", "hlavni mesto praha", "k19.o3100", parent=11),
        _unit(13, "obec", 554782, "Praha", "praha", "k19.o3100.b554782", parent=12,
              lat=50.0755, lon=14.4378, psc_set=("16000", "18000")),
        _unit(14, "cast_obce", 490067, "Vokovice", "vokovice", "k19.o3100.b554782.c490067",
              parent=13, lat=50.1010, lon=14.3480),
        _unit(15, "obec", 567639, "Bořislav", "borislav", "k42.o3805.b567639", parent=6,
              lat=50.5794, lon=13.9200, psc_set=("41502",)),
    ]
    streets = [
        Street(code=101, name="Nad Bořislavkou", name_norm="nad borislavkou", obec_kod=554782),
        Street(code=102, name="28. října", name_norm="28 rijna", obec_kod=554782),
        Street(code=103, name="Slunečná", name_norm="slunecna", obec_kod=599212),
        # The dropped-prefix class: the portal says `Budovatelů`, RÚIAN says `nám.
        # Budovatelů` — same street, and `name_norm` keeps the type word RÚIAN spells.
        Street(code=104, name="nám. Budovatelů", name_norm="nam budovatelu", obec_kod=599212),
    ]
    points = [
        AddressPoint(
            kod_adm=21690278, obec_unit_id=13, obec_kod=554782, psc="16000",
            lat=50.10100, lon=14.34800, ulice_kod=101,
            street_name_norm="nad borislavkou", street_name="Nad Bořislavkou",
            cislo_domovni=487, cislo_orientacni=40,
            cast_obce_unit_id=14, cast_obce_kod=490067,
        ),
        AddressPoint(
            kod_adm=21690279, obec_unit_id=13, obec_kod=554782, psc="16000",
            lat=50.10110, lon=14.34810, ulice_kod=101,
            street_name_norm="nad borislavkou", street_name="Nad Bořislavkou",
            cislo_domovni=488, cislo_orientacni=41,
            cast_obce_unit_id=14, cast_obce_kod=490067,
        ),
        AddressPoint(
            kod_adm=33000001, obec_unit_id=10, obec_kod=599212, psc="74301",
            lat=49.75740, lon=18.01590, ulice_kod=103,
            street_name_norm="slunecna", street_name="Slunečná", cislo_domovni=12,
        ),
        AddressPoint(
            kod_adm=33000002, obec_unit_id=10, obec_kod=599212, psc="74301",
            lat=49.75750, lon=18.01600, ulice_kod=104,
            street_name_norm="nam budovatelu", street_name="nám. Budovatelů",
            cislo_domovni=5,
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


def context(mirror: MiniMirror | None = None) -> ResolverContext:
    return ResolverContext(registry=mirror or default_mirror())


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
