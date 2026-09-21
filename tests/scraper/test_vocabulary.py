"""The label → canonical mappings the nine deleted `_norm_*` functions used to assert.

Those assertions guarded a real fact — what a portal's own word means — not a planted
fixture, so they are migrated here rather than dropped. They belong in ONE table now,
because the nine functions they covered are one registry.

The census-driven gate (A3) proves no LIVE value is missing; this proves the ones the
parsers have always mapped still map, including the inflections a substring matcher used
to catch and an exact table can only catch by naming (`Nevybaveno`).
"""

from __future__ import annotations

import pytest

from scraper import vocabulary

CASES: tuple[tuple[str, str, str, str | None], ...] = (
    # condition — ceskereality / realitymix / idnes / mmreality labels
    ("condition", "ceskereality", "Bezvadný", "velmi_dobry"),
    ("condition", "ceskereality", "K rekonstrukci", "pred_rekonstrukci"),
    ("condition", "ceskereality", "Rozestavěný", "ve_vystavbe"),
    ("condition", "ceskereality", "Dobrý", "dobry"),
    ("condition", "ceskereality", "Po rekonstrukci", "po_rekonstrukci"),
    ("condition", "realitymix", "Novostavba", "novostavba"),
    ("condition", "mmreality", "velmi dobrý", "velmi_dobry"),
    ("condition", "idnes", "Velmi dobrý stav", "velmi_dobry"),
    ("condition", "mmreality", "neuvedeno", None),
    # building_type
    ("building_type", "ceskereality", "Zděná", "cihla"),
    ("building_type", "ceskereality", "Cihlová", "cihla"),
    ("building_type", "ceskereality", "Panelová", "panel"),
    ("building_type", "ceskereality", "Jiná", "jina"),
    ("building_type", "mmreality", "Smíšená", "smisena"),
    ("building_type", "mmreality", "neuvedeno", None),
    # ownership — the canonical-only rule the four `_norm_ownership` copies enforced
    ("ownership", "ceskereality", "Státní, obecní, jiné", "statni"),
    ("ownership", "ceskereality", "soukromé", "osobni"),
    ("ownership", "ceskereality", "Družstevní", "druzstevni"),
    ("ownership", "mmreality", "Obecní", "statni"),
    ("ownership", "idnes", "Jiné", None),
    ("ownership", "idnes", "s.r.o.", None),
    ("ownership", "idnes", "Podílové", None),
    ("ownership", "mmreality", "Ostatní", None),
    # furnished — both stems, both genders: `_norm_furnished` matched on substrings
    ("furnished", "remax", "Ano", "ano"),
    ("furnished", "remax", "Ne", "ne"),
    ("furnished", "remax", "Nevybaveno", "ne"),
    ("furnished", "remax", "Vybaveno", "ano"),
    ("furnished", "remax", "Částečně", "castecne"),
    ("furnished", "idnes", "zařízený", "ano"),
    ("furnished", "idnes", "nezařízený", "ne"),
    ("furnished", "realitymix", "částečně vybaveno", "castecne"),
)


@pytest.mark.parametrize("field,portal,label,expected", CASES)
def test_label_maps_to_its_canonical_value(
    field: str, portal: str, label: str, expected: str | None
) -> None:
    vocabulary.take_unmapped()
    assert vocabulary.canonical(field, portal, label) == expected
    # A `None` here is a decision (a refusal), never a label nothing names.
    assert vocabulary.take_unmapped() == []


def test_the_llm_schema_offers_every_value_the_parsers_may_emit() -> None:
    """The on-demand URL parser writes the SAME columns the nine scrapers write.

    Its tool schema is generated from `known_values`, so until W5 collapses the legacy
    spellings the two producers of one column cannot disagree about its value space."""
    from scraper.source_parsers.common import RECORD_LISTING_TOOL

    properties = RECORD_LISTING_TOOL["input_schema"]["properties"]
    for field in ("condition", "building_type", "energy_rating"):
        offered = set(properties[field]["properties"]["value"]["enum"]) - {None}
        assert offered == set(vocabulary.known_values(field))
    # `disposition` is a grammar, not a list: the filter pill list stops at 5+1 and would
    # make a 6+1 house unnameable.
    assert "enum" not in properties["disposition"]["properties"]["value"]
