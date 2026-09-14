"""W9 — a composite locality name is bound against the register and resolved up.

The operator's ruling, 2026-09-14: *"I do not like this string work, it could be ambiguous.
We need to make this deterministic based on some registry with appropriate hierarchy and
apply for all the statutory cities. We should be able to match the quarter name in a
registry and then resolve up to the town name. All based on an official set of names."*

What these assert is the WHOLE contract of `resolver.composite`, in the order the rules run:
the whole string first (so a two-part town is never cut in half), then the parts scoped by
the anchoring town (so a quarter is found in its own city and nowhere else), and nothing at
all when neither settles it. The mirror is the live register's own rows —
`mini_mirror.statutory_city_mirror` names its provenance — because a test that invents a
gazetteer would prove only that the code agrees with the test.

The deletion this replaces is `claims_common.statutory_city_obec` and its two regexes over
eight hand-typed city names; `test_page_reader_canon.py` holds the reader half.
"""

from __future__ import annotations

import pytest

from location_data.resolver import bind as step_bind
from location_data.resolver import core, normalize
from location_data.resolver.composite import resolve_locality
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm


@pytest.fixture()
def registry():
    return mm.statutory_city_mirror()


def _resolve(claims, mirror):
    return core.resolve(
        claims, mm.context(mirror), resolver_version=RESOLVER_VERSION,
        registry_version="ruian:2026-08-31",
    )


def _bind(claims, mirror):
    binding, _ = step_bind.bind(claims, normalize.normalize_all(claims), mm.context(mirror))
    return binding


# ------------------------------------------------------------ rule 1: the whole string


def test_a_two_part_town_binds_whole_and_is_never_split(registry):
    """"Frýdek-Místek" is an obec, an okres AND a town whose second half is one of its own
    ČástObce rows. Splitting it published "Frýdek", which is nothing; the whole-string rung
    is what makes the eight-city list unnecessary rather than merely shorter."""
    found = resolve_locality("Frýdek-Místek", registry)
    assert (found.obec.name, found.part, found.reason) == ("Frýdek-Místek", None, "whole_string")


def test_a_city_prefixed_momc_binds_whole_and_resolves_up(registry):
    """"Praha-Řeporyje" is the official name of a městská část. It is matched as itself and
    the TOWN comes off its parent — "resolve up to the town name", and the reason the
    reader no longer has to know which of Prague's 57 quarters are spelled this way."""
    found = resolve_locality("Praha-Řeporyje", registry)
    assert found.obec.name == "Praha"
    assert (found.part.name, found.part.level) == ("Praha-Řeporyje", "momc")
    assert found.reason == "whole_string"


def test_a_bare_numbered_obvod_binds_whole_because_the_number_is_its_name(registry):
    """"Praha 4" and "Pardubice II" are what RÚIAN calls those units. The old regex stripped
    the number to reach the city; matching it keeps the quarter as well as the town."""
    for line, town, quarter in (("Praha 4", "Praha", "Praha 4"),
                                ("Pardubice II", "Pardubice", "Pardubice II"),
                                ("Plzeň 3", "Plzeň", "Plzeň 3")):
        found = resolve_locality(line, registry)
        assert (found.obec.name, found.part.name) == (town, quarter), line


def test_an_okres_spelled_like_an_obvod_is_refused_before_anything_is_split(registry):
    """"Brno-venkov" is a rural district and folding it claimed the second-largest city in
    the country as the town of any village in its hinterland. The rail is now a LEVEL test —
    the line matches an okres, so it names no locality — instead of the nine hyphenated
    okres names the regex had to carry by hand."""
    found = resolve_locality("Brno-venkov", registry)
    assert found.bound is False
    assert found.reason == "region_not_locality"


# ------------------------------------------- rule 2: the parts, scoped by their town


@pytest.mark.parametrize("line,town,quarter", [
    ("Praha 4 - Podolí", "Praha", "Podolí"),
    ("Plzeň - Jižní Předměstí", "Plzeň", "Jižní Předměstí"),
    ("Brno - Dolní Heršpice", "Brno", "Dolní Heršpice"),
    ("Ostrava - Poruba", "Ostrava", "Poruba"),
    ("Liberec - Vratislavice nad Nisou", "Liberec", "Liberec-Vratislavice nad Nisou"),
    ("Ústí nad Labem - Střekov", "Ústí nad Labem", "Ústí nad Labem-Střekov"),
    ("Pardubice - Polabiny", "Pardubice", "Polabiny"),
    ("Opava - Kateřinky", "Opava", "Kateřinky"),
    # The unspaced spelling and the en dash are the same line.
    ("Praha 4-Podolí", "Praha", "Podolí"),
    ("Plzeň – Jižní Předměstí", "Plzeň", "Jižní Předměstí"),
])
def test_the_eight_statutory_cities_bind_town_and_quarter_from_one_line(
        registry, line, town, quarter):
    """One rule for all eight, which is what the ruling asked for. Two of them come back with
    the city-prefixed MOMC as the quarter because that is the name the register gives the
    unit that matched whole — the answer is the register's spelling, never the portal's."""
    found = resolve_locality(line, registry)
    assert found.bound, found.reason
    assert (found.obec.name, found.part.name) == (town, quarter)


def test_the_quarter_is_searched_inside_the_anchoring_town_only(registry):
    """Thirteen places in the country are called Podolí and one of them is an obec. Scoping
    the search to Praha is the whole of the hierarchy requirement: without it the line binds
    to a village 240 km away, and with it the wrong Podolí is unreachable."""
    found = resolve_locality("Praha 4 - Podolí", registry)
    assert found.part.code == 400190           # Praha's ČástObce, not Brno-venkov's village
    assert found.obec.code == 554782
    assert found.reason == "anchored_part"


def test_a_street_token_is_ignored_rather_than_bound(registry):
    """"Brno - Dolní Heršpice, Bernáčkova" carries three tokens and the register places two.
    A token it holds at no admin level contributes nothing — never a bind, never a refusal."""
    found = resolve_locality("Brno - Dolní Heršpice, Bernáčkova", registry)
    assert (found.obec.name, found.part.name) == ("Brno", "Dolní Heršpice")


def test_a_town_with_no_placeable_second_token_still_binds_its_town(registry):
    """Rule 25's floor: the town is the answer that matters, so a line whose other half the
    register cannot place is still a bound town, not an unresolved row."""
    found = resolve_locality("Opava, Nákladní 14", registry)
    assert (found.obec.name, found.part, found.reason) == ("Opava", None, "anchor_only")


# --------------------------------------------------------------- rule 3: fail closed


def test_an_ambiguous_part_with_nothing_to_anchor_it_binds_nothing(registry):
    """"Poruba" is a ČástObce of Ostrava, a MOMC of Ostrava and a ČástObce of Orlová. With no
    town beside it the line names two places, so it names none: the row stays unresolved
    rather than being guessed into the bigger city."""
    found = resolve_locality("Poruba", registry)
    assert found.bound is False
    assert found.reason.startswith("ambiguous_whole_string")


def test_a_village_named_like_a_prague_quarter_is_never_read_as_prague(registry):
    """There is an obec called Podolí. Alone, the line names THAT — the town level is tried
    first and a name that is a town is never read as somebody else's quarter."""
    found = resolve_locality("Podolí", registry)
    assert (found.obec.name, found.obec.code) == ("Podolí", 583634)
    assert found.part is None


def test_a_quarter_of_a_town_that_is_not_named_binds_nothing(registry):
    """Two tokens, neither of which the register can anchor: no obec, no unique part, no
    bind. "A location is never guessed" is the rule this is."""
    found = resolve_locality("Poruba - Poruba", registry)
    assert found.bound is False
    assert found.reason == "no_anchor"


def test_a_space_glued_pair_is_not_split_and_stays_unbound(registry):
    """"Praha Stodůlky" (ceskereality's one committed Prague body) matches no unit whole and
    carries no separator. Splitting on the space too would read any two-word line as
    town-plus-quarter, so it is left alone and the row says it does not know."""
    assert resolve_locality("Praha Stodůlky", registry).bound is False


# ------------------------------------------------------- through BIND, FILL and the row


def test_the_bound_row_takes_both_names_from_the_register(registry):
    """End to end, and the point of the wave: the portal wrote one string, the answer row
    carries two RÚIAN names and two RÚIAN codes, and neither was ever a substring."""
    row = _resolve([mm.claim(1, "obec_name", value_text="Praha 4 - Podolí")], registry)
    assert (row.obec_name, row.obec_kod) == ("Praha", 554782)
    assert (row.cast_obce_name, row.cast_obce_kod) == ("Podolí", 400190)
    assert row.granularity == "cast_obce_or_quarter"
    assert row.match_confidence == "high"


def test_the_composite_town_still_constrains_a_street_claim(registry):
    """The anchoring town joins the constraining obec set, so a street stated on the same
    listing is looked up in Brno — the rung the fold used to reach only because it had
    already published a bare "Brno"."""
    binding = _bind([
        mm.claim(1, "obec_name", value_text="Brno - Dolní Heršpice"),
        mm.claim(2, "street_name", value_text="Bernáčkova"),
    ], registry)
    assert binding.target_kind == "street"
    assert (binding.ulice_kod, binding.obec_kod) == (200, 582786)


def test_the_bind_is_stamped_as_a_composite_for_the_diagnostics(registry):
    """Why a row bound the way it did has to be readable off the answer: the town candidate
    carries `composite_locality`, which is not one of the low-confidence qualifiers and so
    ranks and grades exactly like a named obec."""
    binding = _bind([mm.claim(1, "obec_name", value_text="Opava, Nákladní 14")], registry)
    assert binding.granularity == "obec"
    assert "composite_locality" in binding.relaxations
    assert binding.rung == "R4"


def test_an_unbindable_line_leaves_the_row_undetermined(registry):
    """Fail-closed, at the row: no town, no position, no invented hierarchy."""
    row = _resolve([mm.claim(1, "obec_name", value_text="Poruba")], registry)
    assert row.obec_name is None and row.cast_obce_name is None
    assert row.granularity == "unknown"
