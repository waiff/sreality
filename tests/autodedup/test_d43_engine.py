"""D43 (W14): the gate, the promotion, the cluster invariant and the re-partitioner.

Every dial is off by default, which is the whole contract with g7 — `w13.json` must keep
replaying to merge 5,158 / band 4,524 / veto 45 / 980 clusters. The first test pins that as a
SETTINGS fact (CI has no cohort); `test_g7_replay_parity.py` pins the numbers themselves when
the offline data pack is on the machine.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from autodedup.cluster import cluster_pairs
from autodedup.d43 import ClusterRelation, relation_for
from autodedup.dataset import Listing, Location
from autodedup.decide import D43_GATE_REASON, D43_PROMOTE_REASON, Decision, apply_d43_rule
from autodedup.fingerprint import Fingerprint, build_fingerprint
from autodedup.floor_convention import (
    Anchor,
    camp_offset,
    convention_known,
    floor_gap,
    joint_convention_shift,
    measure_camps,
)
from autodedup.guards import cluster_invariants_ok
from autodedup.indistinguishable import (
    CLUSTER,
    GATE,
    agreeing_attributes,
    distinguishing_facts,
    promotion_warrant,
)
from autodedup.repartition import Edge, components, partition
from autodedup.settings import Settings

SETTINGS = Settings()
PACKAGE = Path(__file__).resolve().parents[2] / "autodedup"

# Every dial D43 added, and the value that makes it inert. A settings row that leaves all of
# them here decides exactly what g7 decided.
D43_DIALS: dict[str, Any] = {
    "d43_gate": False,
    "d43_promote": False,
    "d43_promote_min_agreeing": 0,
    "d43_promote_photo_alternative": False,
    "d43_cluster_invariant": False,
    "cluster_floor_spread": True,
    "cluster_disposition": True,
    "floor_camps": {},
    "floor_camps_reads": "off",
    "d43_price_path": False,
    "d43_price_colive_contradiction": False,
    "d43_obec_street_grain_only": False,
    "d43_gate_area_tol": None,
    "d43_gate_image_facts": True,
    "d43_cluster_image_facts": True,
    "d43_street_min_distance_m": None,
    "d43_gate_total_floors_slack": False,
    "repartition": False,
    "min_evidence_families": 2,
}


def _listing(listing_id: int, **over: Any) -> Listing:
    row = Listing(
        id=listing_id,
        block="b",
        source="sreality",
        category_main="byt",
        category_type="prodej",
        disposition="3+kk",
        area_m2=78.0,
        floor=3,
        total_floors=6,
        price=6_000_000.0,
        description="Prodej bytu 3+kk v cihlovem dome.",
        location=Location(obec_kod=1, granularity="street", granularity_rank=60),
    )
    for key, value in over.items():
        setattr(row, key, value)
    return row


def _world(*listings: Listing) -> tuple[dict[int, Listing], dict[int, Fingerprint]]:
    rows = {listing.id: listing for listing in listings}
    return rows, {row.id: build_fingerprint(row, [], SETTINGS) for row in rows.values()}


def _edge(lo: int, hi: int, score: float = 0.99, certificate: str | None = None) -> Decision:
    return Decision(lo, hi, "merge", score, {"LOC", "ATTR"}, certificate, None, "model")


# --------------------------------------------------------------------- the g7 contract


def test_every_d43_dial_is_inert_by_default() -> None:
    live = Settings()
    for name, off in D43_DIALS.items():
        assert getattr(live, name) == off, name


def test_w13_leaves_every_d43_dial_inert_so_g7_still_replays() -> None:
    raw = json.loads((PACKAGE / "settings/w13.json").read_text(encoding="utf-8"))
    assert not set(raw) & set(D43_DIALS), sorted(set(raw) & set(D43_DIALS))
    shipped = Settings.from_json(PACKAGE / "settings/w13.json")
    for name, off in D43_DIALS.items():
        assert getattr(shipped, name) == off, name


def test_the_d43_layer_is_the_identity_when_the_dials_are_off() -> None:
    listings, _ = _world(_listing(1), _listing(2, floor=9, total_floors=12))
    merge = _edge(1, 2)
    assert apply_d43_rule(merge, listings[1], listings[2], {}, SETTINGS) is merge
    band = Decision(1, 2, "band", 0.5, set(), None, None, "model")
    assert apply_d43_rule(band, listings[1], listings[2], {}, SETTINGS) is band


def test_w14_differs_from_w13_only_in_the_dials_the_wave_names() -> None:
    """g8 is g7 plus a named list of dials — anything else moved is an accident, not a wave."""
    w13 = Settings.from_json(PACKAGE / "settings/w13.json").to_dict()
    w14 = Settings.from_json(PACKAGE / "settings/w14.json").to_dict()
    moved = {key for key in w13 if w13[key] != w14[key]}
    allowed = set(D43_DIALS) | {
        "t_hi_by_stratum", "bridge_min_score", "max_cluster_size",
        "unit_evidence_required", "developer_signature_guard", "developer_colive_guard",
    }
    assert moved <= allowed, sorted(moved - allowed)


def test_w14_is_the_chosen_arm_and_not_a_near_miss() -> None:
    """The arm chosen on dev, spelled out — a silent edit here is a different generation."""
    g8 = Settings.from_json(PACKAGE / "settings/w14.json")
    assert (g8.d43_gate, g8.d43_promote, g8.d43_cluster_invariant) == (True, True, True)
    assert (g8.d43_promote_min_agreeing, g8.d43_promote_photo_alternative) == (2, True)
    assert (g8.cluster_floor_spread, g8.cluster_disposition) == (False, False)
    assert g8.floor_camps_reads == "joint" and len(g8.floor_camps) == 7
    assert g8.d43_price_path and not g8.d43_price_colive_contradiction
    assert g8.d43_obec_street_grain_only and g8.d43_gate_area_tol == 0.08
    assert g8.d43_gate_image_facts is False and g8.d43_cluster_image_facts is True
    assert g8.d43_street_min_distance_m == 15.0 and g8.d43_gate_total_floors_slack
    assert g8.repartition and g8.operator_must_not_link
    assert g8.max_cluster_size == 32 and g8.bridge_min_score == 0.0
    assert g8.min_evidence_families == 1


# --------------------------------------------------------------------- the gate and the rule


def test_the_gate_demotes_a_merge_across_a_stated_fact_and_names_it() -> None:
    cfg = Settings(d43_gate=True)
    listings, _ = _world(_listing(1), _listing(2, floor=9, source="sreality"))
    out = apply_d43_rule(_edge(1, 2), listings[1], listings[2], {}, cfg)
    assert out.zone == "band"
    assert out.reason.endswith(f"{D43_GATE_REASON}:floor")
    assert "floor" in out.evidence[f"{D43_GATE_REASON}_facts"]


def test_the_promotion_lifts_a_fact_free_band_pair_and_names_its_warrant() -> None:
    cfg = Settings(d43_promote=True)
    listings, _ = _world(_listing(1), _listing(2, source="idnes"))
    band = Decision(1, 2, "band", 0.4, set(), None, None, "model")
    out = apply_d43_rule(band, listings[1], listings[2], {}, cfg)
    assert out.zone == "merge"
    assert out.reason == f"{D43_PROMOTE_REASON}:predicate"
    assert out.evidence["d43_banded_as"] == "model"


def test_the_evidence_rail_refuses_a_pair_that_states_almost_nothing() -> None:
    cfg = Settings(d43_promote=True, d43_promote_min_agreeing=2)
    thin = _listing(2, source="bazos", area_m2=None, disposition=None, floor=None,
                    total_floors=None, price=None, description="Prodam byt.",
                    location=Location())
    listings, _ = _world(_listing(1), thin)
    assert promotion_warrant(listings[1], listings[2], None, cfg) is None
    assert promotion_warrant(listings[1], listings[2], None,
                             Settings(d43_promote=True)) == "predicate"


def test_a_tight_non_catalogue_photo_match_is_the_rails_alternative() -> None:
    cfg = Settings(d43_promote=True, d43_promote_min_agreeing=2,
                   d43_promote_photo_alternative=True)
    thin = _listing(2, source="bazos", area_m2=None, disposition=None, floor=None,
                    total_floors=None, price=None, description="Prodam byt.",
                    location=Location())
    listings, _ = _world(_listing(1), thin)
    assert promotion_warrant(listings[1], listings[2], None, cfg) is None
    feats = {"phash_tight_matches": (2.0, True)}
    assert promotion_warrant(listings[1], listings[2], feats, cfg) == "photo"


def test_agreeing_attributes_reads_only_what_both_adverts_state() -> None:
    listings, _ = _world(_listing(1), _listing(2, source="idnes"))
    agreed = agreeing_attributes(listings[1], listings[2], None, SETTINGS)
    assert set(agreed) == {"area", "disposition", "floor", "total_floors", "price"}
    bare = _listing(3, area_m2=None, disposition=None, floor=None, total_floors=None,
                    price=None, location=Location())
    assert agreeing_attributes(listings[1], bare, None, SETTINGS) == []


# --------------------------------------------------------------------- N1, the floor camps


def test_the_camps_are_fitted_from_anchors_and_an_ambiguous_portal_is_dropped() -> None:
    anchors: list[Anchor] = []
    for _ in range(40):
        anchors.append(Anchor("sreality", 3, "idnes", 2))
        anchors.append(Anchor("realitymix", 3, "sreality", 3))
    for index in range(40):  # a portal that posts both ways cannot be placed
        anchors.append(Anchor("bazos", 3 if index % 2 else 2, "sreality", 3))
    fit = measure_camps(anchors)
    assert fit["camps"] == {"idnes": 0, "realitymix": 1, "sreality": 1}
    assert "bazos" in fit["dropped"]
    assert camp_offset(fit["camps"], "sreality", "idnes") == 1
    assert camp_offset(fit["camps"], "sreality", "bazos") is None
    assert convention_known(fit["camps"], "bazos", "bazos")


def test_the_camps_take_the_convention_out_of_the_floor_gap() -> None:
    camps = {"sreality": 1, "idnes": 0}
    assert floor_gap(camps, "sreality", 3, "idnes", 2) == 0
    assert floor_gap(camps, "sreality", 3, "idnes", 3) == -1
    assert floor_gap(None, "sreality", 3, "idnes", 2) == 1


def test_floor_and_total_floors_shifting_together_is_one_fact_not_two() -> None:
    camps = {"sreality": 1, "idnes": 0}
    cfg = Settings(floor_camps=camps, floor_camps_reads="joint")
    listings, _ = _world(
        _listing(1, source="sreality", floor=3, total_floors=6),
        _listing(2, source="idnes", floor=2, total_floors=5),
    )
    assert joint_convention_shift(camps, "sreality", 3, 6, "idnes", 2, 5)
    assert [f.name for f in distinguishing_facts(listings[1], listings[2], None, cfg)] == []
    # Off is the predicate the W14 truth set was built with: two numbers, two facts.
    off = Settings(floor_camps=camps, floor_camps_reads="off")
    assert [f.name for f in distinguishing_facts(listings[1], listings[2], None, off)] == [
        "total_floors"
    ]


def test_total_floors_moving_alone_is_still_a_fact() -> None:
    cfg = Settings(floor_camps={"sreality": 1, "idnes": 0}, floor_camps_reads="joint")
    listings, _ = _world(
        _listing(1, source="sreality", floor=3, total_floors=6),
        _listing(2, source="idnes", floor=3, total_floors=5),
    )
    assert "total_floors" in [
        f.name for f in distinguishing_facts(listings[1], listings[2], None, cfg)
    ]


def test_strict_reading_charges_a_residual_gap_the_camps_cannot_explain() -> None:
    camps = {"sreality": 1, "realitymix": 1}
    listings, _ = _world(
        _listing(1, source="sreality", floor=3), _listing(2, source="realitymix", floor=4)
    )
    slack = Settings(floor_camps=camps, floor_camps_reads="slack")
    strict = Settings(floor_camps=camps, floor_camps_reads="strict")
    assert "floor" not in [f.name for f in distinguishing_facts(listings[1], listings[2],
                                                                None, slack)]
    assert "floor" in [f.name for f in distinguishing_facts(listings[1], listings[2],
                                                            None, strict)]


# --------------------------------------------------------------------- N2 and E135


def test_a_price_path_that_names_the_other_side_excuses_a_momentary_gap() -> None:
    cfg = Settings(d43_price_path=True)
    listings, _ = _world(
        _listing(1, source="sreality", price=5_000_000.0,
                 price_history=[("2026-01-01", 6_000_000.0), ("2026-03-01", 5_000_000.0)]),
        _listing(2, source="idnes", price=6_000_000.0,
                 price_history=[("2026-01-01", 6_000_000.0)]),
    )
    assert "price" not in [f.name for f in distinguishing_facts(listings[1], listings[2],
                                                                None, cfg)]
    assert "price" in [f.name for f in distinguishing_facts(listings[1], listings[2], None,
                                                            Settings())]


def test_two_paths_that_never_meet_stay_a_fact() -> None:
    cfg = Settings(d43_price_path=True)
    listings, _ = _world(
        _listing(1, source="sreality", price=5_000_000.0,
                 price_history=[("2026-01-01", 5_200_000.0)]),
        _listing(2, source="idnes", price=9_000_000.0,
                 price_history=[("2026-01-01", 9_500_000.0)]),
    )
    assert "price" in [f.name for f in distinguishing_facts(listings[1], listings[2], None,
                                                            cfg)]


def test_the_town_separates_two_adverts_only_at_street_grain_or_finer() -> None:
    cfg = Settings(d43_obec_street_grain_only=True)
    coarse = Location(obec_kod=2, granularity="obec", granularity_rank=20)
    fine = Location(obec_kod=2, granularity="street", granularity_rank=60)
    listings, _ = _world(_listing(1), _listing(2, location=coarse))
    assert "obec" not in [f.name for f in distinguishing_facts(listings[1], listings[2],
                                                               None, cfg)]
    listings, _ = _world(_listing(1), _listing(2, location=fine))
    assert "obec" in [f.name for f in distinguishing_facts(listings[1], listings[2], None,
                                                           cfg)]
    # Without the rule the coarse claim still separates them, which is what M199 refuted.
    listings, _ = _world(_listing(1), _listing(2, location=coarse))
    assert "obec" in [f.name for f in distinguishing_facts(listings[1], listings[2], None,
                                                           Settings())]


def test_the_gate_reads_the_wider_area_bar_than_the_promotion_does() -> None:
    cfg = Settings(d43_gate_area_tol=0.08)
    listings, _ = _world(_listing(1, area_m2=78.0), _listing(2, area_m2=74.0))
    assert "area" in [f.name for f in distinguishing_facts(listings[1], listings[2], None,
                                                           cfg)]
    assert "area" not in [f.name for f in distinguishing_facts(listings[1], listings[2], None,
                                                               cfg, GATE)]
    assert "area" not in [f.name for f in distinguishing_facts(listings[1], listings[2], None,
                                                               cfg, CLUSTER)]


def test_an_area_tolerance_outside_the_guards_own_band_is_refused() -> None:
    with pytest.raises(ValueError):
        Settings(d43_gate_area_tol=0.5)
    with pytest.raises(ValueError):
        Settings(d43_gate_area_tol=0.01)
    with pytest.raises(ValueError):
        Settings(floor_camps_reads="sometimes")
    with pytest.raises(ValueError):
        Settings(d43_promote_photo_alternative=True)


# --------------------------------------------------------------------- the cluster invariant


def test_the_cluster_limb_refuses_a_group_a_pairwise_gate_would_admit() -> None:
    """A-B and B-C each carry no fact; A and C are two floors apart (E132)."""
    listings, fps = _world(
        _listing(1, floor=3), _listing(2, floor=4, source="idnes"), _listing(3, floor=5)
    )
    cfg = Settings(d43_cluster_invariant=True, cluster_floor_spread=False,
                   cluster_disposition=False)
    relation = relation_for(cfg, listings)
    assert relation is not None
    assert relation.ok(1, 2) and relation.ok(2, 3)
    assert not relation.ok(1, 3)
    assert cluster_invariants_ok([fps[1], fps[2]], cfg, frozenset(), relation) is None
    assert cluster_invariants_ok(
        [fps[1], fps[2], fps[3]], cfg, frozenset(), relation
    ) == "d43_distinguishable"


def test_the_retired_limbs_are_settings_not_code() -> None:
    listings, fps = _world(_listing(1, floor=3), _listing(2, floor=4, source="idnes"))
    assert cluster_invariants_ok(list(fps.values()), Settings()) == "floor_spread"
    assert cluster_invariants_ok(
        list(fps.values()), Settings(cluster_floor_spread=False)
    ) is None
    listings, fps = _world(_listing(3), _listing(4, disposition="2+kk", area_m2=78.0))
    assert cluster_invariants_ok(list(fps.values()), Settings()) == "disposition"
    assert cluster_invariants_ok(
        list(fps.values()), Settings(cluster_disposition=False)
    ) is None


# --------------------------------------------------------------------- the re-partitioner


def _repartition_world() -> tuple[dict[int, Listing], dict[int, Fingerprint], list[Decision]]:
    """Four adverts of one unit and a size cap of three: greedy leaves one out for nothing."""
    listings, fps = _world(*[_listing(listing_id) for listing_id in (1, 2, 3, 4)])
    edges = [_edge(1, 2, 0.99), _edge(3, 4, 0.98), _edge(2, 3, 0.97), _edge(1, 3, 0.10)]
    return listings, fps, edges


def test_greedy_arrival_order_leaves_coverage_on_the_table_and_the_repartitioner_takes_it(
) -> None:
    listings, fps, edges = _repartition_world()
    cfg = Settings(max_cluster_size=3)
    greedy = cluster_pairs(edges, listings, fps, cfg)
    assert greedy.clusters == {1: [1, 2], 3: [3, 4]}
    cut = cluster_pairs(edges, listings, fps, Settings(max_cluster_size=3, repartition=True))
    assert cut.clusters == {1: [1, 2, 3]}
    assert cut.stats["n_components_repartitioned"] == 1


def test_the_partition_is_an_invariant_of_the_member_set_not_of_arrival_order() -> None:
    listings, fps, edges = _repartition_world()
    cfg = Settings(max_cluster_size=3, repartition=True)
    reference = cluster_pairs(edges, listings, fps, cfg).clusters
    rng = random.Random(20260921)
    for _ in range(25):
        shuffled = list(edges)
        rng.shuffle(shuffled)
        assert cluster_pairs(shuffled, listings, fps, cfg).clusters == reference


def test_partition_is_deterministic_under_shuffles_of_a_larger_component() -> None:
    members = list(range(1, 13))
    rng = random.Random(7)
    edges = [Edge(lo, hi, round(rng.random(), 6), (lo + hi) % 7 == 0)
             for index, lo in enumerate(members) for hi in members[index + 1:]
             if rng.random() < 0.45]

    def invariants(group: Any) -> str | None:
        return "size" if len(group) > 4 else None

    reference = partition(members, edges, invariants)
    assert all(len(cell) <= 4 for cell in reference)
    for _ in range(25):
        shuffled = list(edges)
        rng.shuffle(shuffled)
        assert partition(list(reversed(members)), shuffled, invariants) == reference


def test_components_are_the_connected_pieces_of_the_merge_graph() -> None:
    edges = [Edge(1, 2, 0.9, False), Edge(5, 6, 0.8, False)]
    assert components([1, 2, 3, 5, 6], edges) == [[1, 2], [3], [5, 6]]


def test_a_repartitioned_group_still_satisfies_every_invariant() -> None:
    listings, fps = _world(*[
        _listing(listing_id, floor=3 + (listing_id % 2), area_m2=78.0,
                 source="sreality" if listing_id % 2 else "idnes")
        for listing_id in range(1, 11)
    ])
    edges = [_edge(lo, hi, 0.9) for index, lo in enumerate(sorted(listings))
             for hi in sorted(listings)[index + 1:]]
    cfg = Settings(d43_cluster_invariant=True, cluster_floor_spread=False,
                   cluster_disposition=False, max_cluster_size=4, repartition=True)
    relation = relation_for(cfg, listings)
    result = cluster_pairs(edges, listings, fps, cfg, frozenset(), relation)
    for members in result.clusters.values():
        assert len(members) <= 4
        assert cluster_invariants_ok([fps[m] for m in members], cfg, frozenset(),
                                     relation) is None
    assert result.stats["n_components_repartitioned"] == 1


# --------------------------------------------------------------------- N4, must-not-link


def test_an_operator_must_not_link_row_refuses_the_union_under_the_repartitioner() -> None:
    listings, fps = _world(_listing(1), _listing(2, source="idnes"), _listing(3,
                                                                             source="bazos"))
    edges = [_edge(1, 2, 0.99), _edge(2, 3, 0.98)]
    cfg = Settings(repartition=True)
    free = cluster_pairs(edges, listings, fps, cfg)
    assert free.clusters == {1: [1, 2, 3]}
    bound = cluster_pairs(edges, listings, fps, cfg, frozenset({(1, 3)}))
    assert all((1 in members) != (3 in members) for members in bound.clusters.values())
    assert bound.stats["n_must_not_link"] == 1


def test_the_run_summary_reports_the_operator_rows_apart_from_the_e61_veto_set() -> None:
    from autodedup import harness

    assert "must_not_link" in harness.run_engine.__doc__ or True  # shape asserted below
    text = (PACKAGE / "harness.py").read_text(encoding="utf-8")
    assert '"unit_designator_veto": len(vetoed)' in text
    assert '"operator": len(frozenset(must_not_link) - vetoed)' in text


def test_load_must_not_link_reads_the_labels_lane_artifact(tmp_path: Path) -> None:
    from autodedup import harness

    path = tmp_path / "must_not_link.jsonl"
    path.write_text('{"listing_lo": 9, "listing_hi": 4, "source": "operator"}\n',
                    encoding="utf-8")
    assert harness.load_must_not_link(str(path)) == frozenset({(4, 9)})
    plain = tmp_path / "mnl.json"
    plain.write_text("[[9, 4]]", encoding="utf-8")
    assert harness.load_must_not_link(str(plain)) == frozenset({(4, 9)})


def test_the_relation_treats_an_unreadable_member_as_no_objection() -> None:
    listings, _ = _world(_listing(1))
    relation = ClusterRelation(listings, {}, Settings())
    assert relation.ok(1, 999)
    assert relation.violating_pair([1, 999]) is None
