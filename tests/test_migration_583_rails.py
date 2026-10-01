"""Migration 583's predicate IS the plot rule, literally (the 573 pattern).

583 NULLs the stored `estate_area` values `scraper.area.stated_plot` declines, and its header
asserts that each arm is exactly that rule. The CI schema replay runs the file only on an
empty schema, so nothing else would notice a later change to the category sets or the
labelled bases letting the file NULL plots the parser still keeps. The live half, over
seeded hits and misses, is tests/test_migration_583_live.py.
"""

from __future__ import annotations

import itertools
import re
from pathlib import Path

from scraper import area

_SQL = (Path(__file__).resolve().parent.parent / "migrations"
        / "583_plot_column_echo_heal.sql").read_text(encoding="utf-8")
_BODY = _SQL[_SQL.index("set lock_timeout"):]
_HEADER = _SQL[:_SQL.index("set lock_timeout")]


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _header_text() -> str:
    return _squash(" ".join(line.lstrip("-") for line in _HEADER.splitlines()))


def _hit_predicates() -> list[str]:
    return [_squash(m) for m in re.findall(
        r"from listings l\s+where (.*?)\s+for update of l skip locked", _BODY, re.DOTALL)]


def _in_list(listed: str) -> frozenset[str]:
    return frozenset(c.strip().strip("'") for c in listed.split(","))


def test_the_file_has_one_hit_predicate_for_the_one_column():
    assert len(_hit_predicates()) == 1
    assert "set estate_area = null" in _BODY
    # area_basis belongs to area_m2, which is correct on every hit: never touched here.
    assert "area_basis = null" not in _BODY
    assert "listing_snapshots" not in _BODY and "last_seen_at" not in _BODY


def test_the_category_literals_are_the_rules_sets():
    predicate = _hit_predicates()[0]
    flat = re.search(r"l\.category_main = '(\w+)' or", predicate)
    echo = re.search(r"\(l\.category_main = '(\w+)' and \(l\.estate_area = l\.usable_area",
                     predicate)
    assert flat and frozenset({flat[1]}) == area.PLOT_FREE_CATEGORIES
    assert echo and frozenset({echo[1]}) == area.PLOT_ECHO_CATEGORIES
    bases = re.search(r"l\.area_basis in \(([^)]*)\)", predicate)
    assert bases and _in_list(bases[1]) == area.PLOT_ECHO_BASES
    assert area.PLOT_ECHO_BASES < area.AREA_BASES
    assert "unknown" not in area.PLOT_ECHO_BASES and "plot" not in area.PLOT_ECHO_BASES


def test_the_predicate_declines_exactly_what_the_rule_declines():
    """The hit predicate, evaluated in Python over a grid, against `stated_plot`: every
    category the vocabulary emits, every basis, a plot that equals the usable measure,
    the headline, both, or neither, and NULLs on each side. SQL three-valued logic is
    spelled out: a comparison against NULL is NULL, and `or` with NULL is NULL unless the
    other arm is true, so the row is NOT selected."""
    predicate = _hit_predicates()[0]
    assert "l.estate_area is not null" in predicate

    def sql_hit(category: str | None, plot: float | None, usable: float | None,
                headline: float | None, basis: str | None) -> bool:
        if plot is None:
            return False
        if category == "byt":
            return True
        if category != "komercni":
            return False
        usable_eq = None if usable is None else plot == usable
        headline_eq = (None if headline is None else plot == headline) and (
            basis in ("usable", "floor", "total"))
        return bool(usable_eq) or bool(headline_eq)

    figures = (None, 10.0, 852.0, 6841.0)
    for category, plot, usable, headline, basis in itertools.product(
            ("byt", "dum", "komercni", "pozemek", "ostatni", None),
            figures, figures, figures, tuple(area.AREA_BASES) + (None,)):
        case = (category, plot, usable, headline, basis)
        declined = plot is not None and area.stated_plot(
            category, plot, usable=usable, headline=headline, headline_basis=basis) is None
        assert declined == sql_hit(*case), case


def test_the_rule_reads_at_the_columns_scale_so_sql_equality_is_the_same_test():
    """numeric(9,1) rounds half up on the write: 100.04 is stored as 100.0 and equals a
    100.0 usable measure in SQL; the Python rule quantizes the same way before comparing,
    so a live parse and this file agree on the hit."""
    assert area.stated_plot("komercni", 100.04, usable=100.0) is None
    assert area.stated_plot("komercni", 100.05, usable=100.0) == 100.05


def test_the_pre_apply_count_query_uses_the_files_own_predicate():
    header = _header_text()
    assert _hit_predicates()[0].replace("l.", "") in header
    assert "group by rail, source, category_main, is_active, area_basis" in header


def test_the_restore_names_the_one_column_and_only_fills_a_still_null_cell():
    restore = _squash(re.search(r"RESTORE.*?;", _HEADER, re.DOTALL)[0])
    assert "set estate_area = b.old_value" in restore
    assert "b.column_name = 'estate_area'" in restore
    assert "l.estate_area is null" in restore


def test_the_backup_row_carries_its_own_rail_tags():
    assert "'estate_area', rail, estate_area from hit" in _BODY
    assert "then 'P1' else 'P2'" in _BODY
