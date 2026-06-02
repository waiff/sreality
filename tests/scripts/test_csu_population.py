"""Hermetic tests for scripts/csu_population.py — the ČSÚ DataStat JSON-stat
population parser + curated-city matcher. No network, no DB; reads a trimmed
JSON-stat fixture committed alongside.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts import csu_population

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "csu_population_sample.json"
)


def _doc() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_parse_latest_year_and_drops_kraj_aggregates() -> None:
    parsed = csu_population.parse_population_jsonstat(_doc())
    # Kraj-level rows (CZ010 / CZ064) are aggregates and must be dropped.
    assert ("Hlavní město Praha", "Hlavní město Praha") not in parsed
    assert ("Jihomoravský kraj", "Jihomoravský kraj") not in parsed
    # Municipalities keyed (name, kraj) with the LATEST year (2026) value.
    assert parsed[("Praha", "Hlavní město Praha")] == (1407084, 2026)
    assert parsed[("Brno", "Jihomoravský kraj")] == (400000, 2026)


def test_duplicate_name_within_kraj_keeps_max_population() -> None:
    parsed = csu_population.parse_population_jsonstat(_doc())
    # Two "Březina" in Jihomoravský kraj (800 vs 1200) collapse to the larger.
    assert parsed[("Březina", "Jihomoravský kraj")] == (1200, 2026)


def test_load_from_file() -> None:
    parsed = csu_population.load_population_jsonstat(FIXTURE)
    assert parsed[("Praha", "Hlavní město Praha")][0] == 1407084


def test_match_to_curated_with_misses_and_diacritics() -> None:
    parsed = csu_population.parse_population_jsonstat(_doc())
    curated = [
        ("Praha", "Hlavní město Praha"),
        ("Brno", "Jihomoravsky kraj"),     # diacritics-insensitive join
        ("Plzeň", "Plzeňský kraj"),         # absent → miss
    ]
    matched, misses = csu_population.match_to_curated(parsed, curated)
    assert matched[("Praha", "Hlavní město Praha")] == (1407084, 2026)
    # Keyed by the CURATED spelling, even though the join was slug-based.
    assert matched[("Brno", "Jihomoravsky kraj")] == (400000, 2026)
    assert ("Plzeň", "Plzeňský kraj") not in matched
    assert misses == [("Plzeň", "Plzeňský kraj")]


def test_slugify_normalises_nbsp_and_diacritics() -> None:
    # The curated CSV uses a non-breaking space in multi-word names
    # ("Kralupy nad\xa0Vltavou"); ČSÚ uses a normal space. Both must slug
    # to the same key, diacritics-insensitively.
    assert csu_population.slugify("Kralupy nad\xa0Vltavou") == "kralupy nad vltavou"
    assert csu_population.slugify("Plzeň") == csu_population.slugify("Plzen")
    assert csu_population.slugify("  Brno   ") == "brno"


def test_match_joins_across_nbsp() -> None:
    parsed = {("Kralupy nad Vltavou", "Středočeský kraj"): (18000, 2026)}
    # curated side carries the NBSP variant — must still match.
    matched, misses = csu_population.match_to_curated(
        parsed, [("Kralupy nad\xa0Vltavou", "Středočeský kraj")]
    )
    assert matched == {("Kralupy nad\xa0Vltavou", "Středočeský kraj"): (18000, 2026)}
    assert misses == []


def test_missing_child_map_raises() -> None:
    doc = _doc()
    doc["dimension"]["UZ25"]["category"].pop("child")
    try:
        csu_population.parse_population_jsonstat(doc)
    except ValueError:
        return
    raise AssertionError("expected ValueError when `child` map is missing")


def test_sparse_value_object_supported() -> None:
    # JSON-stat allows a sparse {index: value} object instead of a dense list.
    doc = _doc()
    dense = doc["value"]
    doc["value"] = {str(i): v for i, v in enumerate(dense)}
    parsed = csu_population.parse_population_jsonstat(doc)
    assert parsed[("Praha", "Hlavní město Praha")] == (1407084, 2026)
