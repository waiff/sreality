"""The identity rail: no stored listing value moves when the parsers are refactored.

The goldens in `tests/fixtures/field_capture/golden/` were recorded from the nine parsers
as they stood before `scraper/vocabulary.py` and `scraper/attribute_contract.py` existed,
over three real corpora (see `tests/scraper/attribute_probes`). Every later wave that
touches a parser answers to them: a diff here is a value that moved, and a value that
moves is either the point of that wave (then it is blessed with numbers in the PR) or a
regression.

This is deliberately NOT a set of per-parser assertions about labels. A hand-authored
fixture can only assert back what the test itself planted — which is how remax's
`balkon`/`lodzie` reads stayed green for the whole time the portal emitted neither.
"""

from __future__ import annotations

import pytest

from tests.scraper import attribute_probes


@pytest.mark.parametrize("corpus", sorted(attribute_probes.CORPORA))
def test_attributes_unchanged(corpus: str) -> None:
    golden = attribute_probes.load_golden(corpus)
    live = attribute_probes.record(corpus)
    assert sorted(live) == sorted(golden), (
        f"the {corpus} corpus gained or lost probes — re-bless with "
        f"`{attribute_probes.BLESS_COMMAND}` once the change is reviewed"
    )
    moved = {
        name: {"was": golden[name], "now": live[name]}
        for name in sorted(golden)
        if live[name] != golden[name]
    }
    assert not moved, (
        f"{len(moved)} probe(s) in the {corpus} corpus emit a different value than the "
        f"blessed parsers did: {list(moved)[:8]} — re-bless with "
        f"`{attribute_probes.BLESS_COMMAND}` ONLY when the move is the point of the wave"
    )


def test_every_portal_is_probed() -> None:
    """A portal missing from a corpus is the failure mode the goldens cannot see."""
    labels = {name.split("::", 1)[0] for name in attribute_probes.load_golden("labels")}
    assert labels == set(attribute_probes.DRIVERS)
