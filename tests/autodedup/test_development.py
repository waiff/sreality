"""E88: the new-development HOLD — the family, the markers, the two definitions, the rails.

Every number this file pins was measured on the W12 build (dev + validation of seal
ebc141fa…, model `w6_gold` unrefitted); the sealed side was never opened by the build.
"""

from __future__ import annotations

import random

import pytest

from autodedup import development as D
from autodedup.dataset import Listing, Location
from autodedup.decide import Decision
from autodedup.settings import Settings

TEMPLATE = (
    "Jako developer vam nabizime byt 1+kk (27,2 m2) v projektu Rezidence K Botici. "
    "Druzstevni vlastnictvi, podil 3 399 000 Kc, anuita 5 126 000 Kc."
)
PLAIN = "Prodej bytu 1+kk 27 m2, zrekonstruovany, blizko metra, volny ihned."


def _listing(lid: int, text: str, *, block: str = "ruian:1", broker: str = "b1",
             source: str = "ceskereality", category: str = "byt|prodej") -> Listing:
    main, _, kind = category.partition("|")
    return Listing(
        id=lid, block="b", source=source, broker_key=broker, description=text,
        category_main=main, category_type=kind, disposition="1+kk", area_m2=27.0,
        price=5_499_000.0, first_seen_at="2026-07-10T00:00:00+00:00",
        last_seen_at="2026-07-13T00:00:00+00:00", is_active=False,
        location=Location(ruian_adm_kod=int(block.split(":")[1])) if block.startswith("ruian:")
        else Location(obec_kod=1),
    )


def _kb(lo: int, hi: int) -> Decision:
    return Decision(lo, hi, "merge", 1.0, {"text"}, "K-B", None, "certificate:K-B")


def _settings(mode: str) -> Settings:
    return Settings(live_window_from_sighting=True, certificate_b_min_gap_days=1.0 / 1440.0,
                    development_hold_mode=mode)


# --- the family -------------------------------------------------------------------------

def test_a_family_is_one_source_one_broker_one_block() -> None:
    listings = {
        1: _listing(1, TEMPLATE), 2: _listing(2, TEMPLATE),
        3: _listing(3, TEMPLATE, block="ruian:2"),          # another building
        4: _listing(4, TEMPLATE, broker="b2"),              # another broker
    }
    families = D.kb_project_families([_kb(1, 2), _kb(2, 3), _kb(1, 4)], listings)
    members = sorted(sorted(group) for group in families.values())
    assert members == [[1, 2], [3], [4]], (
        "a K-B edge that leaves the block or the broker joins nothing — the component is the "
        "project, not the broker's whole portfolio"
    )


def test_the_partition_is_an_invariant_of_the_member_set_not_of_arrival_order() -> None:
    """E86's defect cannot reach E88: a connected component is confluent, a first-fit cell is not."""
    listings = {lid: _listing(lid, TEMPLATE) for lid in range(1, 7)}
    edges = [_kb(1, 2), _kb(2, 3), _kb(3, 4), _kb(4, 5), _kb(5, 6)]
    settings = _settings("narrow")
    held, _ = D.holds(edges, listings, settings)
    rng = random.Random(20260925)
    for _ in range(20):
        shuffled = list(edges)
        rng.shuffle(shuffled)
        assert D.holds(shuffled, listings, settings)[0] == held


def test_a_family_of_one_is_what_a_cross_block_certificate_leaves_behind() -> None:
    """A K-B pair whose two sides sit in different blocks has no family, and WIDE then holds it
    on one advert's vocabulary alone. Measured, and recorded as a defect of the definition:
    the W12 WIDE arm holds 116 families of which several have one or two members."""
    listings = {1: _listing(1, TEMPLATE), 2: _listing(2, TEMPLATE, block="ruian:2")}
    held, report = D.holds([_kb(1, 2)], listings, _settings("wide"))
    assert held == {(1, 2): "vocabulary"}
    assert report["n_families"] == 2 and all(row["size"] == 1 for row in report["families"])


# --- the markers ------------------------------------------------------------------------

def test_vocabulary_is_a_family_property_not_one_advert_s_word() -> None:
    listings = {1: _listing(1, TEMPLATE), 2: _listing(2, PLAIN), 3: _listing(3, PLAIN)}
    marks = D.markers_of("F1", [1, 2, 3], listings, D.block_population(listings),
                         _settings("narrow"))
    assert marks.n_project_members == 1 and not marks.vocabulary, (
        "one member naming a project next door is not a development family"
    )


def test_cooperative_vocabulary_needs_a_multi_advert_family() -> None:
    coop = "Druzstevni byt, podil 3 399 000 Kc, anuita 5 126 000 Kc."
    small = {1: _listing(1, coop), 2: _listing(2, coop)}
    settings = _settings("narrow")
    assert not D.markers_of("F1", [1, 2], small, D.block_population(small), settings).vocabulary
    big = {lid: _listing(lid, coop) for lid in (1, 2, 3)}
    assert D.markers_of("F1", [1, 2, 3], big, D.block_population(big), settings).vocabulary


def test_block_density_counts_the_OTHER_listings_at_the_address() -> None:
    listings = {lid: _listing(lid, PLAIN) for lid in range(1, 26)}
    marks = D.markers_of("F1", [1, 2, 3], listings, D.block_population(listings),
                         _settings("wide"))
    assert marks.block_density == 22 and marks.wide, "25 at the block, 3 of them the family"
    other_category = {**listings, 26: _listing(26, PLAIN, category="dum|prodej")}
    assert D.markers_of("F1", [1, 2, 3], other_category,
                        D.block_population(other_category), _settings("wide")
                        ).block_density == 22, "a different category is a different market"


def test_the_two_definitions_are_exactly_as_pre_registered() -> None:
    listings = {lid: _listing(lid, TEMPLATE) for lid in (1, 2)}
    pop = D.block_population(listings)
    two_with_vocab = D.markers_of("F1", [1, 2], listings, pop, _settings("narrow"))
    assert two_with_vocab.vocabulary and not two_with_vocab.narrow, "NARROW needs size >= 3"
    assert two_with_vocab.wide, "WIDE takes vocabulary on its own"
    plain = {lid: _listing(lid, PLAIN) for lid in (1, 2, 3)}
    three_plain = D.markers_of("F1", [1, 2, 3], plain, D.block_population(plain),
                               _settings("wide"))
    assert not three_plain.vocabulary and not three_plain.narrow and not three_plain.wide, (
        "no vocabulary, three adverts, an empty block: neither definition fires"
    )


def test_the_unit_fact_marker_is_reported_and_never_a_predicate() -> None:
    listings = {1: _listing(1, PLAIN), 2: _listing(2, PLAIN)}
    listings[2].disposition = "2+kk"
    marks = D.markers_of("F1", [1, 2], listings, D.block_population(listings), _settings("wide"))
    assert "disposition" in marks.unit_fact_disagreement
    assert not marks.wide, "a unit-fact disagreement is measured, not held on (M99)"


# --- the hold ---------------------------------------------------------------------------

def test_the_hold_reaches_K_B_and_nothing_else() -> None:
    listings = {lid: _listing(lid, TEMPLATE) for lid in (1, 2, 3)}
    decisions = [_kb(1, 2), _kb(2, 3),
                 Decision(1, 3, "merge", 1.0, {"text"}, "K-R", None, "certificate:K-R")]
    held, _ = D.holds(decisions, listings, _settings("narrow"))
    assert set(held) == {(1, 2), (2, 3)}, (
        "K-B is the only merge path that rests on the honest clock's disjoint windows; K-R is "
        "a broker's own statement and no census can decay it (E64)"
    )


def test_off_is_inert() -> None:
    listings = {lid: _listing(lid, TEMPLATE) for lid in (1, 2, 3)}
    held, report = D.holds([_kb(1, 2), _kb(2, 3)], listings, Settings())
    assert held == {} and report["n_held"] == 0 and report["mode"] == "off"


# --- the gates ---------------------------------------------------------------------------

def test_the_honest_clock_may_be_paid_for_by_the_floor_or_by_the_hold_never_by_neither() -> None:
    with pytest.raises(ValueError, match="needs a price"):
        Settings(live_window_from_sighting=True, certificate_b_min_gap_days=1.0 / 1440.0)
    Settings(live_window_from_sighting=True, certificate_b_min_gap_days=1.0 / 1440.0,
             certificate_b_min_images=1.0)
    Settings(live_window_from_sighting=True, certificate_b_min_gap_days=1.0 / 1440.0,
             development_hold_mode="narrow")


def test_the_hold_may_not_run_without_the_E84_gap_rail() -> None:
    with pytest.raises(ValueError, match="needs the E84 gap rail"):
        Settings(development_hold_mode="wide")


def test_a_vacuous_size_bar_is_refused() -> None:
    for name in ("development_size_min_narrow", "development_size_min_wide",
                 "development_coop_min_size"):
        with pytest.raises(ValueError, match="must be at least 2"):
            Settings(**{name: 1})


def test_the_lane_refuses_the_setting_until_it_carries_a_family_index() -> None:
    """Like E83 and E85: a pass that sees one claim cannot read the whole family or its block
    census. What is new is that the hold is MONOTONE, so the rail owed is nameable — E64's,
    triggered by a family gaining a member or a marker."""
    from pathlib import Path as _Path

    from autodedup import incremental

    with pytest.raises(NotImplementedError, match="development_hold_mode"):
        incremental.run_pass(
            None, None, None, _settings("narrow"), None, None,  # type: ignore[arg-type]
        )
    source = _Path(incremental.__file__).read_text(encoding="utf-8")
    assert "development_hold_mode (E88) has no incremental family index" in source
    assert "the hold is monotone" in source
