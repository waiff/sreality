"""S4's readings (E190-E193), each built from the adverts that named it.

Every case here is a real pair of cohort 6 — the total-floors cases are certain duplicates of
the Karlovy Vary corpus (shared exact frames), the promotion cases are the bazos re-post tower
whose 2,118 pairs S3 leaves unrecovered, and the sequential-price case is the Penzion
"Hámerská jizba" halves that carry one evidenční číslo and no area on the bazos side. Nothing
is invented: where a body is quoted it is quoted from the advert.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from autodedup.dataset import Listing
from autodedup.demonstrate import strong_corroboration
from autodedup.floor_convention import joint_convention_shift, total_convention_shift
from autodedup.indistinguishable import (
    CLUSTER,
    GATE,
    PROMOTE,
    agreeing_attributes,
    distinguishing_facts,
    promotion_warrant,
    unit_grade_warrant,
)
from autodedup.repartition import Edge, partition
from autodedup.settings import Settings

SETTINGS = Path(__file__).resolve().parents[2] / "autodedup/settings"
S3 = Settings.from_json(SETTINGS / "w18.json")
S4 = Settings.from_json(SETTINGS / "w19.json")

# The developer's own body, quoted from bazos 185625 and 236175 — the SAME 1,592 characters on
# both rows. Neither row states an area, a price, a storey or a street.
TOWER_BODY = (
    "Cena vč. právních služeb a provize RK, Dumrealit.cz Vám zprostředkuje prodej "
    "atraktivního DEVELOPERSKÉHO PROJEKTU pro výstavbu bytových domů s aktuálně platným "
    "stavebním povolením (vydaným 9/25). Nabízíme k prodeji atraktivní developerský projekt "
    "výstavby bytových domů ve fázi připravené k okamžité realizaci. Projekt se nachází ve "
    "městě Františkovy Lázně, v rozvojové části Slatina na strategickém místě. Základní "
    "informace projektu: 1) Lokalita: Františkovy Lázně - Slatina, Karlovarský kraj. "
    "2) Předmět prodeje: Stavební pozemek určený k výstavbě včetně platného stavební "
    "povolení a kompletní prováděcí projektové dokumentace."
)


def stamp(day: int) -> str:
    return f"2026-0{1 + day // 28}-{1 + day % 28:02d}T00:00:00+00:00"


def listing(listing_id: int, first: int = 0, last: int = 20, **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "source": "sreality",
        "category_main": "byt",
        "category_type": "prodej",
        "first_seen_at": stamp(first),
        "last_seen_at": stamp(last),
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def names(a: Listing, b: Listing, cfg: Settings, mode: str = GATE) -> list[str]:
    return [fact.name for fact in distinguishing_facts(a, b, None, cfg, mode)]


def variant(base: Settings = S4, **kwargs: object) -> Settings:
    raw = json.loads((SETTINGS / "w19.json").read_text(encoding="utf-8"))
    raw.update(kwargs)
    return Settings.from_dict(raw)


# --- E190: the camps read the BUILDING's storey count too --------------------------------


def kv_nadrazni(cfg_floor: int | None, total: int, source: str, listing_id: int) -> Listing:
    """One 3+1 of 65 m² on Nádražní in Karlovy Vary, carried by two portals at 3,190,000."""
    return listing(listing_id, source=source, disposition="3+1", area_m2=65.0,
                   floor=cfg_floor, total_floors=total, price=3_190_000.0)


def test_idnes_counts_the_building_one_storey_lower_than_sreality() -> None:
    """idnes 117424 and sreality 17298 are one advert — same 3+1, same 65 m², same 3,190,000,
    same street, four exact frames in common. idnes prints the building as 3 storeys and
    sreality as 4, and BOTH place the flat on floor 0, so `joint_convention_shift` has nothing
    to move with. Measured over cohorts 3-6, idnes states one storey fewer than sreality on 386
    of 5,073 cross-portal certain duplicates and one MORE on 15."""
    a = kv_nadrazni(0, 3, "idnes", 117424)
    b = kv_nadrazni(0, 4, "sreality", 17298)
    assert "total_floors" in names(a, b, S3)
    assert "total_floors" not in names(a, b, S4)
    assert not joint_convention_shift(S4.floor_camps, "idnes", 0, 3, "sreality", 0, 4)
    assert total_convention_shift(S4.floor_camps, "idnes", 0, 3, "sreality", 0, 4)


def test_a_house_states_no_storey_at_all_and_the_camps_still_place_it() -> None:
    """idnes 116059 and realitymix 392657: one 150 m² house at 9,000,000, three storeys against
    four, and neither advert fills the floor column at all."""
    a = listing(116059, source="idnes", category_main="dum", disposition=None,
                area_m2=150.0, floor=None, total_floors=3, price=9_000_000.0)
    b = listing(392657, source="realitymix", category_main="dum", disposition=None,
                area_m2=150.0, floor=None, total_floors=4, price=9_000_000.0)
    assert "total_floors" in names(a, b, S3)
    assert "total_floors" not in names(a, b, S4)


def test_the_gap_the_convention_does_not_predict_is_still_a_fact() -> None:
    """The excuse is SIGNED. idnes counts LOWER, so an idnes row printing one storey MORE than
    sreality is not the convention — and that is 15 pairs against 386."""
    a = kv_nadrazni(0, 5, "idnes", 117424)
    b = kv_nadrazni(0, 4, "sreality", 17298)
    assert "total_floors" in names(a, b, S4)


def test_two_storeys_apart_is_never_the_convention() -> None:
    a = kv_nadrazni(0, 2, "idnes", 117424)
    b = kv_nadrazni(0, 4, "sreality", 17298)
    assert "total_floors" in names(a, b, S4)


def test_one_portal_has_no_convention_to_blame() -> None:
    """Two sreality rows share a vocabulary by construction, so a storey between them is two
    statements about the building."""
    a = kv_nadrazni(0, 3, "sreality", 17298)
    b = kv_nadrazni(0, 4, "sreality", 17299)
    assert "total_floors" in names(a, b, S4)


def test_a_contradicting_storey_closes_the_excuse() -> None:
    """Where both adverts DO place the unit, the two numbers must tell one story: a floor gap
    that is neither zero nor the same offset is a statement the convention cannot explain."""
    a = kv_nadrazni(5, 3, "idnes", 117424)
    b = kv_nadrazni(1, 4, "sreality", 17298)
    assert "total_floors" in names(a, b, S4)


def test_the_camped_total_is_read_in_every_mode() -> None:
    """A vocabulary is not a fact in any reading — promotion included, exactly as the joint
    shift already is."""
    a = kv_nadrazni(0, 3, "idnes", 117424)
    b = kv_nadrazni(0, 4, "sreality", 17298)
    for mode in (PROMOTE, GATE, CLUSTER):
        assert "total_floors" not in names(a, b, S4, mode)


def test_the_camped_total_counts_as_an_agreeing_attribute() -> None:
    a = kv_nadrazni(0, 3, "idnes", 117424)
    b = kv_nadrazni(0, 4, "sreality", 17298)
    assert "total_floors" not in agreeing_attributes(a, b, None, S3)
    assert "total_floors" in agreeing_attributes(a, b, None, S4)


def test_an_unplaced_portal_is_not_camped() -> None:
    """bazos posts both ways and the fit refuses it, so there is no offset to read."""
    assert not total_convention_shift(S4.floor_camps, "bazos", None, 3, "sreality", None, 4)


# --- E191: the promotion rail reads unit-grade evidence ------------------------------------


def tower_row(listing_id: int, first: int, last: int) -> Listing:
    """One bazos posting of the Františkovy Lázně - Slatina project: no area, no price, no
    storey, no street — its whole content is the body."""
    return listing(listing_id, source="bazos", category_main="dum", category_type="prodej",
                   disposition=None, area_m2=None, floor=None, total_floors=None, price=None,
                   description=TOWER_BODY, first=first, last=last)


BODY_ONLY = {"containment_max": (1.0, True), "phash_tight_matches": (0.0, False),
             "ref_code_shared": (0.0, False)}


def test_a_repost_whose_whole_content_is_its_body_is_warranted() -> None:
    """bazos 185625 (4-5 June) and 236175 (6 June), the same 1,592 characters. S3's rail counts
    the public attributes both adverts state — there are none — and refuses; the body is what
    makes them one advert."""
    a, b = tower_row(185625, 4, 5), tower_row(236175, 6, 7)
    assert agreeing_attributes(a, b, BODY_ONLY, S4) == []
    assert not distinguishing_facts(a, b, BODY_ONLY, S4, PROMOTE)
    assert promotion_warrant(a, b, BODY_ONLY, S3) is None
    assert promotion_warrant(a, b, BODY_ONLY, S4) == "unit:body"


def test_a_body_shared_by_two_adverts_on_sale_together_is_a_template() -> None:
    """The one thing the limb may not promote on: the Černovírské zahrady shape, where the
    body is the developer's and not the unit's."""
    a, b = tower_row(185625, 4, 40), tower_row(236175, 6, 42)
    assert unit_grade_warrant(a, b, BODY_ONLY, S4) is None
    assert promotion_warrant(a, b, BODY_ONLY, S4) is None


def test_the_sellers_own_order_code_needs_no_re_post() -> None:
    """A shared rare code names ONE object, so it carries co-live adverts as well."""
    feats = {"containment_max": (0.0, False), "phash_tight_matches": (0.0, False),
             "ref_code_shared": (1.0, True)}
    a, b = tower_row(185625, 4, 40), tower_row(236175, 6, 42)
    assert unit_grade_warrant(a, b, feats, S4) == "code"


def test_three_tight_photo_files_carry_the_warrant() -> None:
    feats = {"containment_max": (0.0, False), "phash_tight_matches": (4.0, True),
             "ref_code_shared": (0.0, False), "tag_room_clip_min2": (0.97, True)}
    a, b = tower_row(185625, 4, 40), tower_row(236175, 6, 42)
    assert unit_grade_warrant(a, b, feats, S4) == "photos:4"


def test_a_headline_demonstrates_nothing() -> None:
    """E164's floor travels with the limb: an advert that states nothing has said nothing."""
    a = tower_row(185625, 4, 5)
    b = tower_row(236175, 6, 7)
    a.description = "Prodej pozemku Františkovy Lázně"
    b.description = "Prodej pozemku Františkovy Lázně"
    assert strong_corroboration(a, b, BODY_ONLY, S4) is None
    assert promotion_warrant(a, b, BODY_ONLY, S4) is None


def test_the_warrant_never_reaches_past_a_stated_fact() -> None:
    """A warrant is not a merge and it is not an override: the predicate is read first."""
    a, b = tower_row(185625, 4, 5), tower_row(236175, 6, 7)
    a.area_m2, b.area_m2 = 300.0, 180.0
    assert promotion_warrant(a, b, BODY_ONLY, S4) is None


def test_the_body_limb_can_be_taken_off_its_re_post_condition() -> None:
    a, b = tower_row(185625, 4, 40), tower_row(236175, 6, 42)
    loose = variant(d43_promote_unit_body_sequential=False)
    assert unit_grade_warrant(a, b, BODY_ONLY, loose) == "body"


# --- E192: the sequential price path where one side states no area -------------------------


def hamerska(listing_id: int, source: str, area: float | None, price: float,
             first: int, last: int) -> Listing:
    """Penzion "Hámerská jizba", ev. č. 655820 — one object, two postings, two prices."""
    return listing(listing_id, source=source, category_main="dum", disposition=None,
                   area_m2=area, floor=None, total_floors=None, price=price,
                   description=TOWER_BODY, first=first, last=last)


def test_a_bazos_row_with_no_area_but_one_order_code_is_still_one_advert() -> None:
    a = hamerska(49634, "bazos", None, 12_900_000.0, 0, 10)
    b = hamerska(362390, "sreality", 430.0, 11_900_000.0, 14, 40)
    feats = {"phash_tight_matches": (0.0, False), "containment_max": (0.0, False),
             "ref_code_shared": (1.0, True)}
    assert "price" in [fact.name for fact in
                       distinguishing_facts(a, b, feats, S3, PROMOTE)]
    assert "price" not in [fact.name for fact in
                           distinguishing_facts(a, b, feats, S4, PROMOTE)]


def test_a_missing_area_with_no_identity_beside_it_still_refuses() -> None:
    """E164's rule stands where nothing replaces the area: two adverts that state neither an
    area nor a code nor a body in common are not reconciled by the clock alone."""
    a = hamerska(49634, "bazos", None, 12_900_000.0, 0, 10)
    b = hamerska(362390, "sreality", 430.0, 11_900_000.0, 14, 40)
    feats = {"phash_tight_matches": (6.0, True), "containment_max": (0.0, False),
             "ref_code_shared": (0.0, False)}
    assert "price" in [fact.name for fact in
                       distinguishing_facts(a, b, feats, S4, PROMOTE)]


def test_two_stated_areas_that_differ_are_untouched_by_the_identity_limb() -> None:
    a = hamerska(49634, "bazos", 380.0, 12_900_000.0, 0, 10)
    b = hamerska(362390, "sreality", 430.0, 11_900_000.0, 14, 40)
    feats = {"phash_tight_matches": (0.0, False), "containment_max": (0.0, False),
             "ref_code_shared": (1.0, True)}
    assert "price" in [fact.name for fact in
                       distinguishing_facts(a, b, feats, S4, PROMOTE)]


# --- E193: the re-partitioner offers every cut edge its whole-cell join --------------------


def conflicting(pairs: set[tuple[int, int]]):
    def invariants(members):
        for index, left in enumerate(sorted(members)):
            for right in sorted(members)[index + 1:]:
                if (left, right) in pairs:
                    return "fact"
        return None
    return invariants


EDGES = [Edge(1, 2, 0.99, False), Edge(2, 3, 0.98, False), Edge(3, 4, 0.97, False),
         Edge(4, 5, 0.96, False), Edge(5, 6, 0.95, False), Edge(6, 7, 0.94, False),
         Edge(7, 8, 0.93, False)]


def test_no_two_cells_an_invariant_would_hold_together_are_left_apart() -> None:
    """E193's contract. With one round the reconcile can walk one member per cell, so a cut
    between two cells of four stays cut; the whole-cell join closes it in one step."""
    members = list(range(1, 9))
    invariants = conflicting({(1, 8)})
    cut = partition(members, EDGES, invariants, max_rounds=1, keep_factless=True)
    joined = partition(members, EDGES, invariants, max_rounds=1, keep_factless=True,
                       rejoin_cells=True)
    home = {m: index for index, cell in enumerate(joined) for m in cell}
    for edge in EDGES:
        if home[edge.lo] == home[edge.hi]:
            continue
        merged = sorted(joined[home[edge.lo]] + joined[home[edge.hi]])
        assert invariants(merged) is not None, f"{edge.lo}x{edge.hi} left apart for nothing"
    assert sum(len(cell) for cell in joined) == len(members)
    assert len(joined) <= len(cut)


def test_the_join_is_a_function_of_the_edge_set_not_of_its_order() -> None:
    members = list(range(1, 9))
    invariants = conflicting({(1, 8), (2, 7)})
    base = partition(members, EDGES, invariants, max_rounds=4, keep_factless=True,
                     rejoin_cells=True)
    rng = random.Random(20260922)
    for _ in range(12):
        shuffled = list(EDGES)
        rng.shuffle(shuffled)
        assert partition(members, shuffled, invariants, max_rounds=4, keep_factless=True,
                         rejoin_cells=True) == base


def test_a_join_an_invariant_refuses_is_never_made() -> None:
    members = list(range(1, 9))
    invariants = conflicting({(left, right) for left in range(1, 5)
                              for right in range(5, 9)})
    cells = partition(members, EDGES, invariants, max_rounds=4, keep_factless=True,
                      rejoin_cells=True)
    for cell in cells:
        assert invariants(cell) is None


# --- the ladder: nothing here may change an arm that does not ask for it --------------------


@pytest.mark.parametrize("arm", ["w13.json", "w15.json", "w16_s.json", "w17.json", "w18.json"])
def test_every_earlier_arm_keeps_the_S3_reading(arm: str) -> None:
    cfg = Settings.from_json(SETTINGS / arm)
    for name in ("d43_total_floors_camp", "d43_promote_unit_evidence",
                 "d43_price_sequential_identity", "repartition_rejoin_cells"):
        assert getattr(cfg, name) is False, name


def test_s4_is_s3_plus_exactly_the_four_limbs() -> None:
    before = json.loads((SETTINGS / "w18.json").read_text(encoding="utf-8"))
    after = json.loads((SETTINGS / "w19.json").read_text(encoding="utf-8"))
    changed = {key for key in set(before) | set(after)
               if before.get(key) != after.get(key)}
    assert changed == {"d43_total_floors_camp", "d43_promote_unit_evidence",
                       "d43_promote_unit_body_sequential", "d43_price_sequential_identity",
                       "repartition_rejoin_cells"}


def test_a_dependent_field_cannot_be_set_alone() -> None:
    with pytest.raises(ValueError):
        variant(d43_promote_unit_evidence=True, d43_promote=False)
    with pytest.raises(ValueError):
        variant(d43_total_floors_camp=True, floor_camps={})
    with pytest.raises(ValueError):
        variant(d43_price_sequential_identity=True, d43_price_sequential_path=False)
    with pytest.raises(ValueError):
        variant(repartition_rejoin_cells=True, repartition=False)
