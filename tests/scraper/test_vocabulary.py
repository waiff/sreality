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
    ("building_type", "mmreality", "Ostatní", "jina"),
    ("building_type", "mmreality", "Smíšená", "smisena"),
    ("building_type", "mmreality", "neuvedeno", None),
    # The two collapses W5 applied: a slugified dropdown label, and the comma-joined
    # multi-material cell ceskereality states because it has no "smíšená" option.
    ("condition", "realitymix", "ve výstavbě (hrubá stavba)", "ve_vystavbe"),
    ("condition", "realitymix", "Určený k demolici", "k_demolici"),
    ("building_type", "ceskereality", "Zděná, kamenná", "smisena"),
    ("building_type", "ceskereality", "Dřevěná, zděná", "smisena"),
    # ownership — three regimes plus the portals' own "other", which is a stated fact
    ("ownership", "ceskereality", "Státní, obecní, jiné", "statni"),
    ("ownership", "ceskereality", "soukromé", "osobni"),
    ("ownership", "ceskereality", "Družstevní", "druzstevni"),
    ("ownership", "mmreality", "Obecní", "statni"),
    ("ownership", "idnes", "Jiné", "jine"),
    ("ownership", "idnes", "s.r.o.", "jine"),
    ("ownership", "idnes", "Podílové", "jine"),
    ("ownership", "mmreality", "Ostatní", "jine"),
    ("ownership", "bezrealitky", "OSTATNI", "jine"),
    ("ownership", "sreality", "- vyber vlastnictví", None),
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


def test_a_negated_member_never_states_the_thing_it_names() -> None:
    """`contains` reads negation per MEMBER, not per cell.

    Per cell — the shape the deleted `states()` had — "Bezbarierový přístup, Výtah"
    (138 live realitymix rows) would read as "no lift", because the folded cell starts
    with `bez`. Per member, "Bez balkonu" still cannot become a balcony."""
    assert vocabulary.contains("Bez balkonu", "balk", "lod") is False
    assert vocabulary.contains("Bez výtahu", "vytah") is False
    assert vocabulary.contains("Bezbarierový přístup, Výtah", "vytah") is True
    assert vocabulary.contains("Bezbarierový přístup", "vytah") is False
    assert vocabulary.contains("Balkon, Lodžie, Terasa", "balk", "lod") is True
    assert vocabulary.contains(None, "balk") is None


def test_a_numeric_zero_is_a_stated_absence_like_the_string() -> None:
    """The surface arrives as a JSON number on bezrealitky and mmreality, and `fold`
    swallows a falsy value — so 0 has to be read before the fold, not after."""
    assert vocabulary.present(0) is False
    assert vocabulary.present(0.0) is False
    assert vocabulary.present("0") is False
    assert vocabulary.present(4) is True
    assert vocabulary.present(None) is None


def test_the_llm_schema_offers_every_value_the_parsers_may_emit() -> None:
    """The on-demand URL parser writes the SAME columns the nine scrapers write.

    Its tool schema is generated from the canon, which since W5 IS the whole value space
    — so the two producers of one column cannot disagree about it. `disposition` and
    `price_unit` are enumerated too: the pill list now spans the entire grammar."""
    from scraper.source_parsers.common import RECORD_LISTING_TOOL

    properties = RECORD_LISTING_TOOL["input_schema"]["properties"]
    for field in ("condition", "building_type", "energy_rating", "disposition",
                  "price_unit"):
        offered = set(properties[field]["properties"]["value"]["enum"]) - {None}
        assert offered == set(vocabulary.CANON[field])


def test_the_grammar_refuses_a_disposition_that_cannot_exist() -> None:
    """`N+M` with M outside {kk, 1}, or no rooms at all, is a digit pair the bazos
    free-text scan found — not a flat. Refused, counted, and never stored."""
    vocabulary.take_unmapped()
    for text in ("prodam byt 4+2", "0+1", "8+7 novostavba", "byt 1+0"):
        assert vocabulary.disposition("bazos", text) is None
    assert sorted(key for key, _ in vocabulary.take_unmapped()) == [
        "disposition/bazos/0+1", "disposition/bazos/1+0",
        "disposition/bazos/4+2", "disposition/bazos/8+7",
    ]
    # A grammatical pair beside an impossible one still wins, and costs no event.
    assert vocabulary.disposition("bazos", "4+2 patro, byt 3+kk") == "3+kk"
    assert vocabulary.take_unmapped() == []
