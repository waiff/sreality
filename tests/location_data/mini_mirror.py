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
distance, the ČástObce point lookup). What is left is the NINE questions the protocol still
declares — the sliver fallback among them, because rule 25 does not allow a border pin to
have no town.
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
    StreetPoint,
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

    def street_point(self, street: Street) -> StreetPoint | None:
        """The same derivation `_STREET_POINT_SQL` makes, in python: the centroid of the
        street's own address points, and the distance from it to the FARTHEST of them. Keyed
        on `ulice_kod` here because the fixture points carry that and not a `street_id` —
        the two are 1:1 on every row of the real mirror."""
        points = [p for p in self.points
                  if p.ulice_kod == street.code and p.lat is not None and p.lon is not None]
        if not points:
            return None
        lat = sum(p.lat for p in points) / len(points)
        lon = sum(p.lon for p in points) / len(points)
        extent = max(haversine_m(lat, lon, p.lat, p.lon) for p in points)
        return StreetPoint(lat=lat, lon=lon, extent_m=extent, point_count=len(points))

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

    def nearest_obec_within(
        self, lat: float, lon: float, max_m: float
    ) -> tuple[AdminUnit, float] | None:
        best: tuple[AdminUnit, float] | None = None
        for code, (clat, clon, radius) in sorted(self.obec_polygons.items()):
            distance = max(0.0, haversine_m(lat, lon, clat, clon) - radius)
            unit = next((u for u in self.units if u.level == "obec" and u.code == code), None)
            if unit is None or distance > max_m:
                continue
            if best is None or distance < best[1]:
                best = (unit, distance)
        return best

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
    """One kraj/okres tree per regression case, plus a fully addressed obec.

    Unit POINTS are modelled as the mirror actually has them (W2-a3): stát, kraj, okres and
    obec carry one, a část obce or a městský obvod does NOT — `ruian_boundaries.LAYERS` draws
    no polygon at those levels and `ruian_admin_units.definition_point` is a column the
    loader has never written. A fixture that gave Vokovice a point would test a registry we
    do not have.
    """
    units = [
        # --- Liberecký kraj / okres Liberec / Krásný Les (the RIGHT one)
        _unit(1, "kraj", 51, "Liberecký kraj", "liberecky kraj", "k51",
              lat=50.6500, lon=14.9000),
        _unit(2, "okres", 3506, "Liberec", "liberec", "k51.o3506", parent=1,
              lat=50.7663, lon=15.0562),
        _unit(3, "obec", 563943, "Krásný Les", "krasny les", "k51.o3506.b563943", parent=2,
              lat=50.9330, lon=15.1500, psc_set=("46346",), homonym_count=2),
        _unit(4, "katastralni_uzemi", 673986, "Krásný Les u Frýdlantu",
              "krasny les u frydlantu", "k51.o3506.b563943", parent=3),
        # --- Ústecký kraj / okres Ústí nad Labem / Krásný Les (the WRONG one, ~100 km west)
        _unit(5, "kraj", 42, "Ústecký kraj", "ustecky kraj", "k42",
              lat=50.5000, lon=13.8000),
        _unit(6, "okres", 3805, "Ústí nad Labem", "usti nad labem", "k42.o3805", parent=5,
              lat=50.6607, lon=14.0328),
        _unit(7, "obec", 567931, "Krásný Les", "krasny les", "k42.o3805.b567931", parent=6,
              lat=50.7676, lon=13.9353, psc_set=("40302",), homonym_count=2),
        # --- Moravskoslezský kraj / okres Nový Jičín / Bílovec
        _unit(8, "kraj", 80, "Moravskoslezský kraj", "moravskoslezsky kraj", "k80",
              lat=49.7500, lon=18.0000),
        _unit(9, "okres", 3804, "Nový Jičín", "novy jicin", "k80.o3804", parent=8,
              lat=49.5944, lon=18.0103),
        _unit(10, "obec", 599212, "Bílovec", "bilovec", "k80.o3804.b599212", parent=9,
              lat=49.7573, lon=18.0158, psc_set=("74301",)),
        # --- Praha (street-name-contains-village trap) + Bořislav village
        _unit(11, "kraj", 19, "Hlavní město Praha", "hlavni mesto praha", "k19",
              lat=50.0755, lon=14.4378),
        _unit(12, "okres", 3100, "Hlavní město Praha", "hlavni mesto praha", "k19.o3100",
              parent=11, lat=50.0755, lon=14.4378),
        _unit(13, "obec", 554782, "Praha", "praha", "k19.o3100.b554782", parent=12,
              lat=50.0755, lon=14.4378, psc_set=("16000", "18000")),
        # No point: RÚIAN draws no ČástObce polygon at all, so a quarter-bound row takes
        # its town's point.
        _unit(14, "cast_obce", 490067, "Vokovice", "vokovice", "k19.o3100.b554782.c490067",
              parent=13),
        _unit(15, "obec", 567639, "Bořislav", "borislav", "k42.o3805.b567639", parent=6,
              lat=50.5794, lon=13.9200, psc_set=("41502",)),
        # --- W18's operator case, as the live register holds it: Mladá Boleslav and the
        # street of bazos ad 223293822. The town's own point is deliberately NOT the
        # street's — that difference is the whole of "a bound street decides the point".
        _unit(30, "kraj", 27, "Středočeský kraj", "stredocesky kraj", "k27",
              lat=50.0000, lon=14.5000),
        _unit(31, "okres", 3204, "Mladá Boleslav", "mlada boleslav", "k27.o3204", parent=30,
              lat=50.4114, lon=14.9030),
        _unit(32, "obec", 535419, "Mladá Boleslav", "mlada boleslav",
              "k27.o3204.b535419", parent=31, lat=50.4114, lon=14.9030,
              psc_set=("29301",)),
        # Kladno and its ČástObce Dubí — the dash-split line "Kladno - Dubí, Ke Křížku",
        # which is the shape `bazos_parser._trailer_street_quarter` writes.
        _unit(33, "okres", 3201, "Kladno", "kladno", "k27.o3201", parent=30,
              lat=50.1477, lon=14.1028),
        _unit(34, "obec", 532053, "Kladno", "kladno", "k27.o3201.b532053", parent=33,
              lat=50.1477, lon=14.1028, psc_set=("27201",)),
        _unit(35, "cast_obce", 71773, "Dubí", "dubi", "k27.o3201.b532053.c71773",
              parent=34),
        # The 76-times-over collision, as one case: a ČástObce of Ostrava spelled exactly
        # like a street of the same town. On a LINE there is nothing to tell them apart, so
        # the segment is not a street candidate at all.
        _unit(36, "okres", 3807, "Ostrava-město", "ostrava mesto", "k80.o3807", parent=8,
              lat=49.8209, lon=18.2625),
        _unit(37, "obec", 554821, "Ostrava", "ostrava", "k80.o3807.b554821", parent=36,
              lat=49.8209, lon=18.2625, psc_set=("70200",)),
        _unit(38, "cast_obce", 554791, "Zábřeh", "zabreh", "k80.o3807.b554821.c554791",
              parent=37),
    ]
    streets = [
        Street(code=101, name="Nad Bořislavkou", name_norm="nad borislavkou", obec_kod=554782),
        Street(code=102, name="28. října", name_norm="28 rijna", obec_kod=554782),
        Street(code=103, name="Slunečná", name_norm="slunecna", obec_kod=599212),
        # The dropped-prefix class: the portal says `Budovatelů`, RÚIAN says `nám.
        # Budovatelů` — same street, and `name_norm` keeps the type word RÚIAN spells.
        Street(code=104, name="nám. Budovatelů", name_norm="nam budovatelu", obec_kod=599212),
        # W18: a street with a SPREAD, and an ASYMMETRIC one. The three points below are
        # shaped so the derived centroid is the live one (POINT(14.91365 50.42247), measured
        # over Jiráskova's 63 address points on 2026-09-16) and its farthest door is the live
        # 1,288 m away — while HALF THE BOUNDING DIAGONAL of the same set is only ~985 m. The
        # asymmetry is the fixture's whole job: it is what makes a test able to tell the two
        # definitions of `extent_m` apart, and a real door of the street falls outside the
        # wrong one.
        Street(code=105, name="Jiráskova", name_norm="jiraskova", obec_kod=535419),
        # W18's line cases. `Ke Křížku` is the street a dash-split line ends on; `Sokolovská`
        # gives the same line a SECOND street, which is what fail-closed means. `Zábřeh` is a
        # street spelled like a část obce of its own town, and `Livornská` is the register's
        # spelling of a name bazos writes inflected ("Livornské ulici").
        Street(code=106, name="Ke Křížku", name_norm="ke krizku", obec_kod=532053),
        Street(code=107, name="Sokolovská", name_norm="sokolovska", obec_kod=532053),
        Street(code=108, name="náměstí Míru", name_norm="namesti miru", obec_kod=535419),
        Street(code=109, name="Zábřeh", name_norm="zabreh", obec_kod=554821),
        Street(code=110, name="28. října", name_norm="28 rijna", obec_kod=554821),
        Street(code=111, name="Livornská", name_norm="livornska", obec_kod=554782),
        # The WIDENED-KEY COLLISION, as the register actually holds it: 45 keys across 32
        # obce collide once the type word is dropped, and Kladno is one of them — both of
        # these fold to `svobody`. It is why an exact full-name match has to win outright.
        Street(code=112, name="náměstí Svobody", name_norm="namesti svobody", obec_kod=532053),
        Street(code=113, name="Svobody", name_norm="svobody", obec_kod=532053),
        # The register spelling the generic word INTO the name (`Nová ulice` ×8, `V Ulici`,
        # `Na Ulici`, `I. ulice`…`IX. ulice`): folding it off leaves a key that can never bind.
        Street(code=114, name="Nová ulice", name_norm="nova ulice", obec_kod=532053),
        Street(code=115, name="Na Ulici", name_norm="na ulici", obec_kod=532053),
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
        AddressPoint(
            kod_adm=55000001, obec_unit_id=32, obec_kod=535419, psc="29301",
            lat=50.41090, lon=14.91200, ulice_kod=105,
            street_name_norm="jiraskova", street_name="Jiráskova", cislo_domovni=1,
        ),
        AddressPoint(
            kod_adm=55000002, obec_unit_id=32, obec_kod=535419, psc="29301",
            lat=50.42800, lon=14.91400, ulice_kod=105,
            street_name_norm="jiraskova", street_name="Jiráskova", cislo_domovni=40,
        ),
        AddressPoint(
            kod_adm=55000003, obec_unit_id=32, obec_kod=535419, psc="29301",
            lat=50.42851, lon=14.91495, ulice_kod=105,
            street_name_norm="jiraskova", street_name="Jiráskova", cislo_domovni=86,
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
            535419: (50.42247, 14.91365, 6000.0),
            532053: (50.1477, 14.1028, 5000.0),
            554821: (49.8209, 18.2625, 9000.0),
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


def statutory_city_mirror() -> MiniMirror:
    """The eight statutory cities as RÚIAN actually holds them (W9), plus the homonyms that
    make the fail-closed rule mean something.

    Codes, names and parentage are the live register's (`ruian_admin_units`,
    `ruian:2026-08-31`, read 2026-09-14): Praha's quarters are MOMC *and* ČástObce rows with
    different codes, "Poruba" is a ČástObce of Ostrava AND a MOMC of Ostrava AND a ČástObce
    of Orlová, there is a village called Podolí in okres Brno-venkov, and "Frýdek-Místek" and
    "Brno-venkov" are an obec and an okres spelled like a městský obvod. Every one of those
    is a case the deleted regex had to be told about by hand.

    No unit points: RÚIAN draws no polygon at ČástObce or MOMC level, and the towns here are
    never asked to place a row.
    """
    units = [
        # --- Jihomoravský kraj: Brno, its okres namesake, and the village called Podolí
        _unit(100, "kraj", 116, "Jihomoravský kraj", "jihomoravsky kraj", "k116",
              lat=49.1951, lon=16.6068),
        _unit(101, "okres", 3702, "Brno-město", "brno mesto", "k116.o3702", parent=100,
              lat=49.1951, lon=16.6068),
        _unit(102, "okres", 3703, "Brno-venkov", "brno venkov", "k116.o3703", parent=100,
              lat=49.1951, lon=16.6068),
        _unit(103, "obec", 582786, "Brno", "brno", "k116.o3702.b582786", parent=101,
              lat=49.1951, lon=16.6068),
        _unit(104, "cast_obce", 12114, "Dolní Heršpice", "dolni herspice",
              "k116.o3702.b582786.c12114", parent=103),
        _unit(105, "momc", 550973, "Brno-střed", "brno stred", "k116.o3702.b582786.m550973",
              parent=103),
        _unit(106, "obec", 583634, "Podolí", "podoli", "k116.o3703.b583634", parent=102,
              lat=49.2211, lon=16.7530),
        _unit(107, "cast_obce", 124257, "Podolí", "podoli", "k116.o3703.b583634.c124257",
              parent=106),
        # --- Praha: no okres at all, and the quarter at BOTH levels
        _unit(110, "kraj", 19, "Hlavní město Praha", "hlavni mesto praha", "k19",
              lat=50.0755, lon=14.4378),
        _unit(111, "obec", 554782, "Praha", "praha", "k19.b554782", parent=110,
              lat=50.0755, lon=14.4378),
        _unit(112, "momc", 500119, "Praha 4", "praha 4", "k19.b554782.m500119", parent=111),
        _unit(113, "momc", 539635, "Praha-Řeporyje", "praha reporyje",
              "k19.b554782.m539635", parent=111),
        _unit(114, "cast_obce", 400190, "Podolí", "podoli", "k19.b554782.c400190",
              parent=111),
        _unit(115, "cast_obce", 490270, "Řeporyje", "reporyje", "k19.b554782.c490270",
              parent=111),
        # --- Plzeň
        _unit(120, "kraj", 32, "Plzeňský kraj", "plzensky kraj", "k32",
              lat=49.7384, lon=13.3736),
        _unit(121, "okres", 3404, "Plzeň-město", "plzen mesto", "k32.o3404", parent=120,
              lat=49.7384, lon=13.3736),
        _unit(122, "obec", 554791, "Plzeň", "plzen", "k32.o3404.b554791", parent=121,
              lat=49.7384, lon=13.3736),
        _unit(123, "cast_obce", 406368, "Jižní Předměstí", "jizni predmesti",
              "k32.o3404.b554791.c406368", parent=122),
        _unit(124, "momc", 546003, "Plzeň 3", "plzen 3", "k32.o3404.b554791.m546003",
              parent=122),
        # --- Moravskoslezský kraj: Ostrava (Poruba twice over), Orlová, Opava, Frýdek-Místek
        _unit(130, "kraj", 132, "Moravskoslezský kraj", "moravskoslezsky kraj", "k132",
              lat=49.8209, lon=18.2625),
        _unit(131, "okres", 3807, "Ostrava-město", "ostrava mesto", "k132.o3807", parent=130,
              lat=49.8209, lon=18.2625),
        _unit(132, "obec", 554821, "Ostrava", "ostrava", "k132.o3807.b554821", parent=131,
              lat=49.8209, lon=18.2625),
        _unit(133, "cast_obce", 414085, "Poruba", "poruba", "k132.o3807.b554821.c414085",
              parent=132),
        _unit(134, "momc", 546224, "Poruba", "poruba", "k132.o3807.b554821.m546224",
              parent=132),
        _unit(135, "okres", 3802, "Karviná", "karvina", "k132.o3802", parent=130,
              lat=49.8543, lon=18.5421),
        _unit(136, "obec", 507292, "Orlová", "orlova", "k132.o3802.b507292", parent=135,
              lat=49.8654, lon=18.4302),
        _unit(137, "cast_obce", 413488, "Poruba", "poruba", "k132.o3802.b507292.c413488",
              parent=136),
        _unit(138, "okres", 3805, "Opava", "opava", "k132.o3805", parent=130,
              lat=49.9387, lon=17.9026),
        _unit(139, "obec", 505927, "Opava", "opava", "k132.o3805.b505927", parent=138,
              lat=49.9387, lon=17.9026),
        _unit(140, "cast_obce", 413909, "Kateřinky", "katerinky",
              "k132.o3805.b505927.c413909", parent=139),
        _unit(141, "okres", 3803, "Frýdek-Místek", "frydek mistek", "k132.o3803",
              parent=130, lat=49.6833, lon=18.3486),
        _unit(142, "obec", 598003, "Frýdek-Místek", "frydek mistek", "k132.o3803.b598003",
              parent=141, lat=49.6833, lon=18.3486),
        _unit(143, "cast_obce", 33235, "Místek", "mistek", "k132.o3803.b598003.c33235",
              parent=142),
        # --- Liberec: one MOMC, and it is spelled city-plus-name
        _unit(150, "kraj", 51, "Liberecký kraj", "liberecky kraj", "k51",
              lat=50.7663, lon=15.0562),
        _unit(151, "okres", 3506, "Liberec", "liberec", "k51.o3506", parent=150,
              lat=50.7663, lon=15.0562),
        _unit(152, "obec", 563889, "Liberec", "liberec", "k51.o3506.b563889", parent=151,
              lat=50.7663, lon=15.0562),
        _unit(153, "momc", 556891, "Liberec-Vratislavice nad Nisou",
              "liberec vratislavice nad nisou", "k51.o3506.b563889.m556891", parent=152),
        # --- Ústí nad Labem: the quarter as a ČástObce and as a city-prefixed MOMC
        _unit(160, "kraj", 42, "Ústecký kraj", "ustecky kraj", "k42",
              lat=50.6607, lon=14.0328),
        _unit(161, "okres", 3809, "Ústí nad Labem", "usti nad labem", "k42.o3809",
              parent=160, lat=50.6607, lon=14.0328),
        _unit(162, "obec", 554804, "Ústí nad Labem", "usti nad labem", "k42.o3809.b554804",
              parent=161, lat=50.6607, lon=14.0328),
        _unit(163, "cast_obce", 409448, "Střekov", "strekov", "k42.o3809.b554804.c409448",
              parent=162),
        _unit(164, "momc", 502316, "Ústí nad Labem-Střekov", "usti nad labem strekov",
              "k42.o3809.b554804.m502316", parent=162),
        # --- Pardubice: the Roman-numeral MOMC
        _unit(170, "kraj", 53, "Pardubický kraj", "pardubicky kraj", "k53",
              lat=50.0343, lon=15.7812),
        _unit(171, "okres", 3603, "Pardubice", "pardubice", "k53.o3603", parent=170,
              lat=50.0343, lon=15.7812),
        _unit(172, "obec", 555134, "Pardubice", "pardubice", "k53.o3603.b555134",
              parent=171, lat=50.0343, lon=15.7812),
        _unit(173, "cast_obce", 410632, "Polabiny", "polabiny", "k53.o3603.b555134.c410632",
              parent=172),
        _unit(174, "momc", 555151, "Pardubice II", "pardubice ii",
              "k53.o3603.b555134.m555151", parent=172),
    ]
    streets = [
        # The third token of "Brno - Dolní Heršpice, Bernáčkova": a street, and the register
        # holds it at no admin level at all, which is how the binder comes to ignore it.
        Street(code=200, name="Bernáčkova", name_norm="bernackova", obec_kod=582786),
    ]
    return MiniMirror(units=units, streets=streets, points=[], obec_polygons={})
