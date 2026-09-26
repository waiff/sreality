"""`katastr_kod` — FILL's one KÚ rule (MF program PR-B, resolver v5.4).

**The single KÚ of the BOUND registry entity, else NULL.** Every case below is one bound
entity kind: an address point (its own KÚ), a KÚ or ZSJ unit (the KÚ on its chain), an obec
(its one KÚ child, else nothing), a street or část obce (the KÚ holding every one of its
RÚIAN doors — operator Q7, 2026-09-25). What is NOT an entity is a portal pin: 66,165 idnes
`no_exact_address` and 13,176 sreality `not_address` pins read as "precise"
(`core.pin_is_precise`), so the B1 regression below puts such a pin exactly ON a door and
still expects NULL.

Praha is the multi-KÚ town (Vokovice + Veleslavín here), Krásný Les u Frýdlantu the one-KÚ
one. A fixture point's `katastr_kod` stands in for the SQL point-in-KÚ.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from location_data.resolver import bind as step_bind
from location_data.resolver import core
from location_data.resolver import fill as step_fill
from location_data.resolver.types import AddressPoint, Binding, Street
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm

VOKOVICE_KU = 729418
VELESLAVIN_KU = 729353
KRASNY_LES_KU = 673986


def _mirror() -> mm.MiniMirror:
    base = mm.default_mirror()
    units = [
        *base.units,
        mm._unit(40, "katastralni_uzemi", VOKOVICE_KU, "Vokovice", "vokovice",
                 "k19.o3100.b554782", parent=13),
        mm._unit(41, "katastralni_uzemi", VELESLAVIN_KU, "Veleslavín", "veleslavin",
                 "k19.o3100.b554782", parent=13),
        mm._unit(42, "zsj", 1234501, "Vokovice-střed", "vokovice stred",
                 "k19.o3100.b554782", parent=40),
        mm._unit(43, "cast_obce", 490211, "Veleslavín", "veleslavin",
                 "k19.o3100.b554782.c490211", parent=13),
        mm._unit(44, "cast_obce", 490999, "Prázdná", "prazdna",
                 "k19.o3100.b554782.c490999", parent=13),
    ]
    points = [
        replace(p, katastr_kod=VOKOVICE_KU) if p.ulice_kod == 101 else p
        for p in base.points
    ]
    # Livornská straddles the KÚ border: one door each side, and both in část Veleslavín.
    points += [
        AddressPoint(kod_adm=21690301, obec_unit_id=13, obec_kod=554782, psc="16200",
                     lat=50.0990, lon=14.3200, ulice_kod=111, street_name_norm="livornska",
                     street_name="Livornská", cislo_domovni=401, cast_obce_unit_id=43,
                     cast_obce_kod=490211, katastr_kod=VOKOVICE_KU),
        AddressPoint(kod_adm=21690302, obec_unit_id=13, obec_kod=554782, psc="16200",
                     lat=50.0980, lon=14.3150, ulice_kod=111, street_name_norm="livornska",
                     street_name="Livornská", cislo_domovni=402, cast_obce_unit_id=43,
                     cast_obce_kod=490211, katastr_kod=VELESLAVIN_KU),
    ]
    return replace(base, units=units, points=points)


def _resolve(claims, mirror=None):
    return core.resolve(
        claims, mm.context(mirror or _mirror()), resolver_version=RESOLVER_VERSION,
        registry_version="ruian:2026-08-31",
    )


# ------------------------------------------------------------------ the bound entity


def test_an_address_point_is_in_its_own_ku():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
    ])
    assert resolution.granularity == "address_point"
    assert resolution.katastr_kod == VOKOVICE_KU


def test_a_portal_registry_key_binds_the_same_ku():
    resolution = _resolve([mm.claim(1, "address_point_id", value_text="21690278")])
    assert resolution.ruian_adm_kod == 21690278
    assert resolution.katastr_kod == VOKOVICE_KU


@pytest.mark.parametrize(("unit_id", "expected"), [(40, VOKOVICE_KU), (42, VOKOVICE_KU)])
def test_a_bound_ku_is_itself_and_a_zsj_is_the_ku_it_lies_in(unit_id, expected):
    """BIND does not bind a KÚ by name today; FILL's rule is written for the entity, so a KÚ
    unit (or a ZSJ, a child of exactly one KÚ) answers off its own chain."""
    binding = Binding(target_kind="admin_unit", granularity="cast_obce_or_quarter", rung="R4",
                      admin_unit_id=unit_id, obec_kod=554782)
    filled = step_fill.fill(binding, step_bind.Constraints(), _mirror())
    assert filled.katastr_kod == expected


def test_a_one_ku_obec_is_in_that_ku():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Krásný Les"),
        mm.claim(2, "psc", value_text="463 46"),
    ])
    assert (resolution.obec_kod, resolution.granularity) == (563943, "obec")
    assert resolution.katastr_kod == KRASNY_LES_KU


def test_a_multi_ku_obec_has_no_ku():
    resolution = _resolve([mm.claim(1, "obec_name", value_text="Praha")])
    assert (resolution.obec_kod, resolution.granularity) == (554782, "obec")
    assert resolution.katastr_kod is None


def test_a_street_whose_every_door_is_in_one_ku_is_in_that_ku():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou"),
    ])
    assert (resolution.ulice_kod, resolution.granularity) == (101, "street")
    assert resolution.katastr_kod == VOKOVICE_KU


def test_a_street_spanning_two_ku_has_none():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Livornská"),
    ])
    assert resolution.ulice_kod == 111
    assert resolution.katastr_kod is None


def test_a_street_without_doors_has_none():
    """The Q7 residual stated as a rule: no registered door, no evidence, no KÚ."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="28. října"),
    ])
    assert resolution.ulice_kod == 102
    assert resolution.katastr_kod is None


def test_a_street_in_a_one_ku_obec_is_in_that_ku_even_without_doors():
    """The hierarchy answers first: everything inside a one-KÚ obec lies in its KÚ."""
    mirror = _mirror()
    mirror.streets.append(Street(code=120, name="Lesní", name_norm="lesni", obec_kod=563943))
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Krásný Les"),
        mm.claim(2, "psc", value_text="463 46"),
        mm.claim(3, "street_name", value_text="Lesní"),
    ], mirror)
    assert (resolution.ulice_kod, resolution.granularity) == (120, "street")
    assert resolution.katastr_kod == KRASNY_LES_KU


@pytest.mark.parametrize(
    ("part", "expected"),
    [("Vokovice", VOKOVICE_KU), ("Veleslavín", None), ("Prázdná", None)],
    ids=["one-ku", "spanning", "no-doors"],
)
def test_a_cast_obce_follows_the_door_rule(part, expected):
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "cast_obce_name", value_text=part),
    ])
    assert resolution.granularity == "cast_obce_or_quarter"
    assert resolution.katastr_kod == expected


# ------------------------------------------------------------------ never a pin (B1)


@pytest.mark.parametrize("label", ["no_exact_address", "not_address", "gps"])
def test_a_pin_on_a_door_never_decides_the_ku(label):
    """The B1 regression. The pin sits ON a Vokovice door and the portal may even call it
    precise — but nothing BOUND a door, so the town-level row has no KÚ."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "coordinate", lat=50.10100, lon=14.34800, declared_precision_label=label),
    ])
    assert resolution.obec_kod == 554782
    assert resolution.ruian_adm_kod is None
    assert resolution.katastr_kod is None


def test_a_pin_alone_gets_its_town_and_no_ku():
    resolution = _resolve([
        mm.claim(1, "coordinate", lat=50.10100, lon=14.34800, declared_precision_label="gps"),
    ])
    assert resolution.obec_kod == 554782
    assert resolution.katastr_kod is None


# ------------------------------------------------------------------ masking


def test_a_foreign_row_has_no_ku():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Benahavís"),
        mm.claim(2, "country", value_text="Španělsko"),
    ])
    assert resolution.country_status == "foreign"
    assert resolution.katastr_kod is None


def test_a_disputed_row_keeps_its_bound_entitys_ku():
    """No special case: the dispute is about the PIN, the KÚ is the bound street's."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou"),
        mm.claim(3, "coordinate", lat=50.0600, lon=14.4200, declared_precision_label="gps"),
    ])
    assert resolution.disputed == "pin_off_street"
    assert resolution.katastr_kod == VOKOVICE_KU


def test_the_part_question_is_asked_only_for_a_part_bind_in_a_multi_ku_town():
    """The one round trip the rule adds, and only where nothing already read answers it."""
    asked: list[int] = []
    mirror = _mirror()
    original = mirror.part_katastr_kod

    def counting(unit_id: int) -> int | None:
        asked.append(unit_id)
        return original(unit_id)

    mirror.part_katastr_kod = counting  # type: ignore[method-assign]
    _resolve([mm.claim(1, "obec_name", value_text="Praha"),
              mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40")], mirror)
    _resolve([mm.claim(1, "obec_name", value_text="Praha")], mirror)
    assert asked == []
    _resolve([mm.claim(1, "obec_name", value_text="Praha"),
              mm.claim(2, "cast_obce_name", value_text="Vokovice")], mirror)
    assert asked == [14]


# ------------------------------------------------------------------ the SQL side


def _flat(sql: str) -> str:
    return " ".join(sql.split()).lower()


def test_every_ku_answer_rides_a_read_fill_already_makes():
    """Round trips are the drain's cost model: the address point, the street point and the
    chain answer the KÚ in the SAME statement, and each binds the version it needs."""
    from location_data.resolver import resolve_db

    for sql, placeholders in (
        (resolve_db._ADDRESS_POINT_SQL, 2),             # version, kod_adm
        (resolve_db._ADDRESS_POINTS_BY_NUMBER_SQL, 8),  # version + the seven it had
        (resolve_db._STREET_POINT_SQL, 3),              # street id, version x2
        (resolve_db._PART_KATASTR_SQL, 3),              # unit id, version x2
        (resolve_db._ADMIN_CHAIN_SQL, 2),               # unchanged: the sole KÚ binds none
    ):
        assert sql.count("%s") == placeholders, _flat(sql)
    assert "katastralni_uzemi" in _flat(resolve_db._ADMIN_CHAIN_SQL)
    assert "ku.code" in _flat(resolve_db._ADDRESS_POINT_SQL)


def test_the_ku_is_only_ever_asked_of_a_registry_geometry():
    """No portal coordinate reaches a KÚ question: the point-keyed statements (the ones that
    take the listing's own lat/lon arrays) never name the KÚ level."""
    from location_data.resolver import resolve_db

    for sql in (resolve_db._CONTAINING_OBEC_SQL, resolve_db._NEAREST_OBEC_SQL,
                resolve_db._IN_CZ_SQL):
        assert "katastralni_uzemi" not in _flat(sql)
    for sql in (resolve_db._ADDRESS_POINT_SQL, resolve_db._STREET_POINT_SQL,
                resolve_db._PART_KATASTR_SQL):
        assert "unnest(" not in _flat(sql)


def test_the_door_rule_reads_the_polygon_by_unit_and_version():
    """One GiST probe for the first door, then ONE covers test against the KÚ's
    authoritative polygon on `ruian_aug_unique_nonpip (unit_id, registry_version_id,
    purpose)` — 22 ms for a 9,193-door část where a probe per door took 1.7 s."""
    from location_data.resolver import resolve_db

    for sql in (resolve_db._STREET_POINT_SQL, resolve_db._PART_KATASTR_SQL):
        flat = _flat(sql)
        assert ("a.unit_id = k.id and a.registry_version_id = %s "
                "and a.purpose = 'authoritative'") in flat
        assert "st_covers(a.geom, (select st_collect(d.geom)" in flat
        assert "order by d.kod_adm limit 1" in flat
