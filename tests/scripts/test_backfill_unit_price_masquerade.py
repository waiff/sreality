"""decide() rules of the unit-price quarantine — confirmation, never magnitude.

The true total was never on the page, so this backfill cannot repair; it can
only stop a rate being read as a total. That makes the false-positive cost the
whole design: a cheap-but-real listing must survive, and a row whose price cell
cannot be read must be LEFT and counted, never guessed at from its magnitude.
"""

from __future__ import annotations

import pytest

from scripts.backfill_unit_price_masquerade import (
    KEEP,
    QUARANTINE,
    UNCONFIRMED,
    decide,
    price_text_from_fragment,
)

# The three portals' real cell shapes, straight off production staged pages.
PER_AREA_CELLS = [
    ("ceskereality", "100 Kč za m²/měsíc"),
    ("ceskereality", "105 Kč za m²/rok"),
    ("ceskereality", "105 533 Kč za m² Spočítat hypotéku"),
    ("realitymix", "45 Kč / (za m 2 ) Nabídněte cenu"),
    ("realitymix", "850 Kč / (za m 2 )"),
]

# Cheap but REAL: a garage, a storage unit, a small monthly rent. Magnitude puts
# these squarely inside the damaged band; the cell is what saves them.
CHEAP_BUT_REAL_CELLS = [
    ("ceskereality", "968 Kč za měsíc"),
    ("ceskereality", "833 Kč za rok"),
    ("ceskereality", "700 Kč za měsíc"),
    ("bazos", "250 Kč"),
    ("bazos", "170 Kč"),
    ("realitymix", "900 Kč / (za měsíc)"),
]


@pytest.mark.parametrize("source,cell", PER_AREA_CELLS)
def test_per_area_cell_is_quarantined(source: str, cell: str) -> None:
    assert decide(source, cell, 100)[0] == QUARANTINE


@pytest.mark.parametrize("source,cell", CHEAP_BUT_REAL_CELLS)
def test_cheap_but_real_cell_survives(source: str, cell: str) -> None:
    assert decide(source, cell, 250)[0] == KEEP


def test_a_total_with_a_per_area_note_is_still_a_total():
    # The marker test is ANCHORED to the text right after the amount, so a
    # parenthesised Kč/m² NOTE beside a real total never convicts the total.
    assert decide("ceskereality", "4 990 000 Kč (4 008 Kč/m²)", 4_990_000)[0] == KEEP


def test_magnitude_alone_never_quarantines():
    # The largest confirmed ceskereality masquerade is 979,620 Kč "za m²" and
    # the smallest surviving real price is double digits: the band overlaps
    # completely, which is why only the cell decides.
    assert decide("ceskereality", "979 620 Kč za m²/měsíc", 979_620)[0] == QUARANTINE
    assert decide("ceskereality", "10 Kč za měsíc", 10)[0] == KEEP


@pytest.mark.parametrize("cell", [None, "", "   ", "Cena dohodou"])
def test_an_unreadable_cell_is_left_alone_not_guessed(cell: str | None) -> None:
    verdict, _reason = decide("realitymix", cell, 136)
    assert verdict == UNCONFIRMED


def test_bazos_bare_cell_confirms_nothing():
    # Measured across all 26,592 priced active bazos rows: ZERO carry any
    # per-area marker. The basis lives in prose, so bazos is left whole.
    assert decide("bazos", "379 Kč", 379)[0] == KEEP


def test_a_row_with_no_stored_price_is_a_no_op():
    assert decide("ceskereality", "100 Kč za m²/měsíc", None)[0] == KEEP


def test_realitymix_fragment_round_trips_through_the_portal_extractor():
    # The staged page is 72 kB; only this 379-byte <tr> is fetched, so it has to
    # survive being re-wrapped in a <table> before the portal's own selector.
    fragment = (
        '<tr class="advert-description__short-props-price">\n\t\t\t\t<td>Cena:</td>\n'
        '\t\t\t\t<td>45 Kč  / <span>(za m<sup>2</sup>)</span>&nbsp;'
        '<button class="x" data-toggle-form-propose-price-detail>Nabídněte cenu</button>\n'
        "\t\t\t\t</td>\n\t\t\t</tr>"
    )
    text = price_text_from_fragment("realitymix", fragment)
    assert text is not None and "45" in text
    assert decide("realitymix", text, 45)[0] == QUARANTINE


def test_a_missing_fragment_yields_no_text():
    assert price_text_from_fragment("realitymix", None) is None
    assert price_text_from_fragment("ceskereality", "<tr><td>x</td></tr>") is None
