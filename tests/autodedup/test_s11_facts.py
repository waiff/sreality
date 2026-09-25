"""S11's readings (E260, E261, E262, E263, E264), each built from the advert that named it.

Every body below is a real advert out of cohort 13. Dolní Břežany / Krátká is the whole of
E260/E261: ONE plot let as two rows of one priced plan — the fenced 585 m² part at 5,000 Kč
against the whole `celkem tedy až 827 m²` at 7,000 → 8,000 Kč — posted the same second, live
together 64 days on idnes and 56 on ceskereality, with byte-identical bodies on five portals
and only the portals' own slugs (`komercniho-pozemku` against `stavebni-parcely`) beside the
price to part them. Průhonice / Pod Valem II is E264: one house re-posted twenty times on
idnes at 75,000 and then 70,000, whose tail 504940 was last SIGHTED on 07-09 while its
successor 547142 was first sighted on 07-16 — the two never met, and only the DETECTION stamp
of 08-05 makes them look co-live. E262 and E263 are the clusterer's half of the same defect.
"""

from __future__ import annotations

import json
from pathlib import Path

from autodedup.d43 import relation_for
from autodedup.dataset import Listing
from autodedup.indistinguishable import CLUSTER, GATE, distinguishing_facts
from autodedup.repartition import Edge, partition
from autodedup.settings import Settings
from autodedup.text_facts import offered_extent_menu

SETTINGS = Path(__file__).resolve().parents[2] / "autodedup/settings"
S10 = Settings.from_json(SETTINGS / "w25.json")
S11 = Settings.from_json(SETTINGS / "w26.json")


def variant(**kwargs: object) -> Settings:
    raw = json.loads((SETTINGS / "w26.json").read_text(encoding="utf-8"))
    raw.update(kwargs)
    return Settings.from_dict(raw)


def stamp(day: int) -> str:
    return f"2026-0{1 + day // 28}-{1 + day % 28:02d}T00:00:00+00:00"


def names(a: Listing, b: Listing, cfg: Settings, mode: str = GATE) -> list[str]:
    return [fact.name for fact in distinguishing_facts(a, b, None, cfg, mode)]


# --- E260: the priced plan the body itself prints --------------------------------------------
# ceskereality's spelling. `m²` survives `fact_text`'s fold as `m2`.
KRATKA = (
    "Hledáte dostupný pozemek u Prahy, kde můžete parkovat auta, techniku, skladovat "
    "materiál nebo mít praktické zázemí pro Vaše podnikání?\n\nV Dolních Břežanech nabízíme "
    "k pronájmu rovinatý pozemek s dojezdem do Prahy do 15 minut autem.\n\nPlně oplocená "
    "část pozemku má výměru 585 m². Po dohodě je možné pronajmout také navazující "
    "neoplocenou část, celkem tedy až 827 m². Vyberete si variantu, která Vám bude dávat "
    "větší smysl.\n\nHlavní výhody pozemku:\n plně oplocená část o výměře 585 m²\n možnost "
    "pronájmu až 827 m² včetně navazující neoplocené části\n Dolní Břežany u Prahy"
)
# idnes and realitymix carry the HTML entity half-decoded. Same advert, same seller, same day.
KRATKA_SUP2 = KRATKA.replace("m²", "m and sup2;")
# The same land let with ONE extent: no menu, so no plan, so no row.
KRATKA_NO_MENU = KRATKA.split("Po dohodě")[0] + "Vyberete si variantu podle svého záměru."


def land(listing_id: int, price: float, first: int = 0, last: int = 60,
         body: str = KRATKA, **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "source": "ceskereality",
        "category_main": "pozemek",
        "category_type": "pronajem",
        "area_m2": 585.0,
        "price": price,
        "description": body,
        "first_seen_at": stamp(first),
        "last_seen_at": stamp(last),
        "price_history": [[stamp(first), price]],
        "location": {"obec_kod": 539104, "street_key": "kratka",
                     "granularity": "address_point", "granularity_rank": 100,
                     "is_address_grain": True},
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def test_the_menu_is_read_in_both_spellings_the_portals_store() -> None:
    assert offered_extent_menu(KRATKA) == frozenset({827.0})
    assert offered_extent_menu(KRATKA_SUP2) == frozenset({827.0})


def test_one_extent_is_no_menu() -> None:
    assert offered_extent_menu(KRATKA_NO_MENU) == frozenset()


def test_the_two_rows_of_the_plan_are_parted_on_one_portal() -> None:
    a, b = land(363920, 5000.0), land(363921, 8000.0)
    assert "extent_variant" in names(a, b, S11)
    assert "extent_variant" not in names(a, b, S10)


def test_the_row_travels_with_the_price_path_through_a_re_post() -> None:
    # 363920 (5,000, ceskereality) died before 18362921 (8,000, its row's re-post) appeared.
    a = land(363920, 5000.0, first=0, last=56)
    b = land(18362921, 8000.0, first=58, last=60)
    assert "extent_variant" in names(a, b, S11)
    colive_only = variant(d43_extent_variant_sequential=False)
    assert "extent_variant" not in names(a, b, colive_only)


def test_a_re_post_of_ONE_row_is_still_that_row() -> None:
    # 363921 walked 7,000 -> 8,000 and 18362921 is its re-post at 8,000: the paths MEET.
    a = land(363921, 8000.0, first=0, last=56,
             price_history=[[stamp(0), 7000.0], [stamp(30), 8000.0]])
    b = land(18362921, 8000.0, first=58, last=60)
    assert "extent_variant" not in names(a, b, S11)


def test_two_portals_carrying_ONE_row_are_not_parted() -> None:
    a = land(280716, 5000.0, source="sreality")
    b = land(435320, 5000.0, source="realitymix", body=KRATKA_SUP2)
    assert "extent_variant" not in names(a, b, S11)


def test_the_bare_price_is_still_not_a_fact_without_the_menu() -> None:
    """D49 stands: what lifts the Krátká reading is the MENU, never the price alone."""
    a = land(1, 5000.0, body=KRATKA_NO_MENU)
    b = land(2, 8000.0, body=KRATKA_NO_MENU)
    assert "extent_variant" not in names(a, b, S11)


def test_a_menu_that_restates_the_headline_is_one_extent() -> None:
    same = KRATKA.replace("827 m²", "600 m²")
    a, b = land(1, 5000.0, body=same), land(2, 8000.0, body=same)
    assert "extent_variant" not in names(a, b, S11)


def test_two_adverts_leading_with_DIFFERENT_extents_are_E204s_and_not_this_limb() -> None:
    a, b = land(1, 5000.0), land(2, 8000.0, area_m2=827.0)
    assert "extent_variant" not in names(a, b, S11)


def test_a_price_move_too_small_to_be_a_row_is_not_a_row() -> None:
    a, b = land(1, 5000.0), land(2, 5400.0)
    assert "extent_variant" not in names(a, b, S11)


def test_the_kratka_plan_comes_apart_into_exactly_two_rows() -> None:
    rows_low = [land(243583, 5000.0, source="idnes", body=KRATKA_SUP2, first=0, last=8),
                land(321272, 5000.0, source="idnes", body=KRATKA_SUP2, first=9, last=60),
                land(280716, 5000.0, source="sreality"),
                land(363920, 5000.0),
                land(435320, 5000.0, source="realitymix", body=KRATKA_SUP2)]
    rows_high = [land(280665, 8000.0, source="sreality"),
                 land(321271, 8000.0, source="idnes", body=KRATKA_SUP2, first=9, last=60),
                 land(363921, 8000.0),
                 land(18362921, 8000.0, first=62, last=64)]
    listings = {row.id: row for row in rows_low + rows_high}
    relation = relation_for(S11, listings)
    assert relation is not None
    for low in rows_low:
        for high in rows_high:
            assert not relation.ok(low.id, high.id), (low.id, high.id)
    for row in (rows_low, rows_high):
        for index, left in enumerate(row):
            for right in row[index + 1:]:
                assert relation.ok(left.id, right.id), (left.id, right.id)


# --- E264: the cluster-grain price limb reads W8's honest clock ------------------------------
VALEM = (
    "Rodinný dům Pod Valem II Nabízíme k pronájmu samostatně stojící nezařízený rodinný dům "
    "5+kk o velikosti 190 m² situovaný na pozemku 811 m² disponující zahradou (691 m²). "
    "Nemovitost se nachází ve vyhledávané lokalitě v blízkosti mezinárodních škol - Průhonice."
)


def house(listing_id: int, price: float, first: str, last: str,
          inactive: str | None = None, **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "source": "idnes",
        "category_main": "dum",
        "category_type": "pronajem",
        "disposition": "5+kk",
        "area_m2": 190.0,
        "price": price,
        "description": VALEM,
        "first_seen_at": first,
        "last_seen_at": last,
        "inactive_at": inactive,
        "price_history": [[first, price]],
        "location": {"obec_kod": 539881, "street_key": "pod-valem",
                     "granularity": "address_point", "granularity_rank": 100,
                     "is_address_grain": True},
    }
    fields.update(kwargs)
    return Listing.from_json({"id": listing_id, "block": "b", **fields})


def valem_pair() -> tuple[Listing, Listing]:
    # The real stamps. 504940's delisting was DETECTED on 08-05, 27 days after its last sighting.
    return (
        house(504940, 75_000.0, "2026-07-07T14:26:30+00:00", "2026-07-09T10:55:19+00:00",
              "2026-08-05T03:42:20+00:00"),
        house(547142, 70_000.0, "2026-07-16T18:23:40+00:00", "2026-07-17T11:29:23+00:00",
              "2026-07-18T04:13:32+00:00"),
    )


def test_no_stated_fact_separates_the_two_re_posts() -> None:
    a, b = valem_pair()
    assert distinguishing_facts(a, b, None, S11, CLUSTER) == []
    assert distinguishing_facts(a, b, None, S10, CLUSTER) == []


def test_the_detection_stamp_alone_made_the_two_re_posts_co_live() -> None:
    a, b = valem_pair()
    listings = {a.id: a, b.id: b}
    assert relation_for(S10, listings).ok(a.id, b.id) is False  # type: ignore[union-attr]
    assert relation_for(S11, listings).ok(a.id, b.id) is True  # type: ignore[union-attr]


def test_two_adverts_really_on_sale_together_are_still_two_prices() -> None:
    a = house(1, 75_000.0, "2026-07-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00")
    b = house(2, 70_000.0, "2026-07-02T00:00:00+00:00", "2026-08-02T00:00:00+00:00")
    listings = {1: a, 2: b}
    assert relation_for(S11, listings).ok(1, 2) is False  # type: ignore[union-attr]


# --- E262: the shed may not carve a cell's own members off --------------------------------
def shed_case(blockers, **kwargs):
    """Průhonice's shape: a cell of one object's re-posts, and a cell it conflicts with."""
    train = [1, 2, 3, 4, 5]
    other = [11, 12, 13]
    members = train + other
    edges = [Edge(lo, hi, 0.95, True) for i, lo in enumerate(train) for hi in train[i + 1:]]
    edges += [Edge(lo, hi, 0.95, True) for i, lo in enumerate(other) for hi in other[i + 1:]]
    # 5 is the train's tail: it carries a fact against the OTHER cell and nothing else does.
    edges += [Edge(lo, hi, 0.95, True) for lo in (1, 2, 3, 4, 5) for hi in other]
    return partition(members, edges, **kwargs, keep_factless=True, rejoin_cells=True,
                     shed_blockers=blockers, shed_max=1, outer_rounds=3)


def test_the_train_keeps_its_tail_when_the_tail_is_the_cells_own() -> None:
    def blockers(group):
        group = set(group)
        return [(5, other) for other in (11, 12, 13) if 5 in group and other in group]

    carved = shed_case(blockers, invariants=lambda g: "blocked" if blockers(g) else None,
                       shed_factless_guard="off")
    assert [5] in carved
    held = shed_case(blockers, invariants=lambda g: "blocked" if blockers(g) else None,
                     shed_factless_guard="core")
    assert [5] not in held
    assert sorted(len(cell) for cell in held) == [3, 5]


def test_a_stranger_in_the_cell_is_still_shed() -> None:
    """E253's own case: the absorbed third advert conflicts with many and holds few edges."""
    members = [1, 2, 3, 4, 9]
    edges = [Edge(1, 4, 0.90, False), Edge(1, 2, 0.85, False), Edge(1, 3, 0.85, False),
             Edge(1, 9, 0.80, False), Edge(2, 9, 0.80, False), Edge(3, 9, 0.80, False)]

    def blockers(group):
        group = set(group)
        return [(4, 9)] if 4 in group and 9 in group else []

    for mode in ("off", "core"):
        shed = partition(members, edges,
                         lambda g: "blocked" if blockers(g) else None,
                         keep_factless=True, rejoin_cells=True, shed_blockers=blockers,
                         shed_max=1, outer_rounds=3, shed_factless_guard=mode)
        assert sorted(shed) == [[1, 2, 3, 9], [4]], mode


def test_the_guard_is_off_for_every_generation_up_to_S10() -> None:
    assert S10.repartition_shed_factless_guard == "off"
    assert S11.repartition_shed_factless_guard == "core"
    assert S10.demonstrate_cluster_price_honest_clock is False
    assert S11.demonstrate_cluster_price_honest_clock is True
    assert S10.d43_extent_variant is False
    assert S11.d43_extent_variant is True
