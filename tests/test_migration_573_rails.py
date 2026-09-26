"""Migration 573's predicates ARE the parser rails, literally (PR #1630 review).

573 NULLs the stored values the ingest rails decline, and its header asserts that each
predicate is exactly one of them. The CI schema replay runs the file only on an empty
schema, so nothing else would notice a later change to a band constant or the room regex
letting the file NULL values the parser still accepts. The live half, over seeded hits and
misses, is tests/test_migration_573_live.py.
"""

from __future__ import annotations

import re
from pathlib import Path

from scraper import area, floor
from scraper.attribute_contract import CONTRACT, source_value

_SQL = (Path(__file__).resolve().parent.parent / "migrations"
        / "573_portal_contract_leftovers_floor_area.sql").read_text(encoding="utf-8")
_BODY = _SQL[_SQL.index("set lock_timeout"):]
_HEADER = _SQL[:_SQL.index("set lock_timeout")]


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _header_text() -> str:
    return _squash(" ".join(line.lstrip("-") for line in _HEADER.splitlines()))


def _hit_predicates() -> list[str]:
    return [_squash(m) for m in re.findall(
        r"from listings l\s+where (.*?)\s+for update of l skip locked", _BODY, re.DOTALL)]


def _categories(listed: str) -> frozenset[str]:
    return frozenset(c.strip().strip("'") for c in listed.split(","))


def test_the_file_has_one_hit_predicate_per_column():
    assert len(_hit_predicates()) == 3


def test_the_floor_band_is_the_parsers_band():
    m = re.search(r"l\.floor < (-?\d+) or l\.floor > (-?\d+)", _BODY)
    assert m and (int(m[1]), int(m[2])) == (floor._FLOOR_MIN, floor._FLOOR_MAX)
    # The band bounds the CANONICAL storey — the value `listings.floor` stores — on either
    # scale: a ground1 portal's 41 is storey 40, its 42 is out.
    assert floor.floor_from_portal("ground0", str(floor._FLOOR_MAX)) == floor._FLOOR_MAX
    assert floor.floor_from_portal("ground0", str(floor._FLOOR_MAX + 1)) is None
    assert floor.floor_from_portal("ground0", str(floor._FLOOR_MIN)) == floor._FLOOR_MIN
    assert floor.floor_from_portal("ground0", str(floor._FLOOR_MIN - 1)) is None
    assert floor.floor_from_portal("ground1", str(floor._FLOOR_MAX + 1)) == floor._FLOOR_MAX
    assert floor.floor_from_portal("ground1", str(floor._FLOOR_MAX + 2)) is None
    assert floor.floor_from_portal("ground1", str(floor._FLOOR_MIN - 1)) is None


def test_the_storey_count_band_is_the_parsers_band():
    m = re.search(r"l\.total_floors < (\d+) or l\.total_floors > (\d+)", _BODY)
    assert m
    low, high = int(m[1]), int(m[2])
    assert high == floor._FLOOR_MAX
    assert floor.total_floors_from_portal(low - 1) is None
    assert floor.total_floors_from_portal(low) == low
    assert floor.total_floors_from_portal(high) == high
    assert floor.total_floors_from_portal(high + 1) is None


def test_f1_is_the_idnes_placeholder_the_contract_declares():
    assert "l.source = 'idnes' and l.floor = 20" in _BODY
    placeholder = "20. patro a vyšší"
    assert CONTRACT["idnes"]["floor"].sentinels == (placeholder,)
    assert source_value("idnes", "floor", {"podlaží": placeholder}) is None
    # Without the sentinel the placeholder reads as storey 20: the value F1 targets.
    assert floor.normalize_floor(placeholder) == 20
    # The header's pre-apply count splits F1 on exactly that raw label.
    assert f"'{placeholder}'" in _HEADER


def test_the_area_band_literals_are_the_resolvers_constants():
    m = re.search(r"l\.category_main in \(([^)]*)\) and l\.area_m2 < (\d+(?:\.\d+)?)\)", _BODY)
    assert m
    assert _categories(m[1]) == area.BOUNDED_CATEGORIES
    assert float(m[2]) == area.MIN_AREA_M2

    m = re.search(
        r"l\.category_main in \(([^)]*)\)\s+and l\.area_m2 < (\d+) \* "
        r"\(case when l\.disposition ~ '([^']*)'\s+then left\(l\.disposition, 1\)::int end\)",
        _BODY)
    assert m
    assert _categories(m[1]) == area.ROOMED_CATEGORIES
    assert float(m[2]) == area.MIN_AREA_PER_ROOM_M2

    m = re.search(r"l\.category_main = '(\w+)' and l\.area_m2 >= (\d+)", _BODY)
    assert m and float(m[2]) == area.MAX_FLAT_AREA_M2
    # The ceiling is a flat's alone for a LABELLED measure, which is what 573 cannot tell
    # apart from any other stored headline.
    assert area.dwelling_area_band(m[1])[1] == area.MAX_FLAT_AREA_M2
    for other in (area.BOUNDED_CATEGORIES | area.ROOMED_CATEGORIES) - {m[1]}:
        assert area.dwelling_area_band(other)[1] == area.MAX_AREA_M2


def test_the_sql_room_regex_reads_the_same_rooms_as_the_resolver():
    """`disposition ~ '^[1-9]\\+(kk|1)$'` + `left(disposition, 1)::int` is spelled apart from
    `area._ROOMS_RE`, so the two are compared by what they read, over every code the
    vocabulary emits and the near misses around them."""
    m = re.search(r"l\.disposition ~ '([^']*)'", _BODY)
    assert m
    sql_rooms = re.compile(m[1])
    codes = {f"{n}+{tail}" for n in range(0, 11) for tail in ("kk", "1", "2", "3")}
    codes |= {"atypicky", "pokoj", "", "3+kk ", " 3+kk", "3+KK", "10+kk", "3 + kk", "3+k"}
    for code in sorted(codes):
        ours = area._ROOMS_RE.match(code)
        theirs = sql_rooms.match(code)
        assert bool(ours) == bool(theirs), code
        if ours:
            assert int(code[:1]) == int(ours.group(1)), code


def test_the_band_declines_exactly_what_the_predicate_hits():
    """The area predicate, evaluated in Python over a grid, against the resolver on a
    LABELLED measure (573 cannot see the basis a stored headline came from)."""
    rooms_re = re.compile(re.search(r"l\.disposition ~ '([^']*)'", _BODY)[1])

    def hit(category: str, disposition: str | None, value: float) -> bool:
        if category in ("byt", "dum", "komercni") and value < 5:
            return True
        if category in ("byt", "dum") and disposition and rooms_re.match(disposition):
            if value < 8 * int(disposition[:1]):
                return True
        return category == "byt" and value >= 1000

    for category in ("byt", "dum", "komercni", "pozemek", "ostatni", None):
        for disposition in (None, "atypicky", "1+kk", "3+1", "4+kk", "9+1", "3+2"):
            for value in (0.5, 4.9, 5.0, 7.9, 8.0, 23.9, 24.0, 71.9, 72.0, 999.9, 1000.0,
                          1800.0):
                declined = area.derive_headline_area(
                    category_main=category, usable=value, disposition=disposition,
                ) == (None, None)
                assert declined == hit(category, disposition, value), (
                    category, disposition, value)


def test_the_pre_apply_count_query_uses_the_files_own_predicates():
    header = _header_text()
    for predicate in _hit_predicates():
        assert predicate.replace("l.", "") in header, predicate
    assert "group by rail, source, is_active" in header
