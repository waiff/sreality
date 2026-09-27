"""G1 image-stack bake-off: the pure parts (no torch, no DB, no R2)."""

from __future__ import annotations

import math
import random

import numpy as np
import pytest

from scripts import g1_image_pairs as gp
from scripts import g1_image_stack_eval as ev
from scripts import g1_image_stack_pod as pod


def _img(iid, lid, seq, phash, tag="kitchen", pop=1):
    return gp.Img(image_id=iid, listing_id=lid, seq=seq, key=f"img/{lid}/{iid}.jpg",
                  phash=phash, pop=pop, tag=tag, tag_conf=0.9)


def _adv(i, **kw):
    base = dict(id=i, export="t", source="sreality", category_main="byt", category_type="prodej",
                disposition="2+kk", area_m2=50.0, floor=2, total_floors=5, price=5_000_000.0,
                broker_key="b", obec=1, street="s", house_number="1", first_seen="2026-05-01",
                last_seen="2026-06-01", is_active=True, prices=[5_000_000.0])
    base.update(kw)
    return gp.Advert(**base)


def test_tight_matches_is_one_to_one_and_symmetric():
    a = [_img(1, 10, 1, 0b1111), _img(2, 10, 2, 0b1111)]
    b = [_img(3, 11, 1, 0b1110)]
    hits = gp.tight_matches(a, b)
    assert len(hits) == 1
    assert len(gp.tight_matches(b, a)) == 1


def test_stratum_separates_stock_only_identity():
    assert gp.stratum(0, 0) == "h0"
    assert gp.stratum(0, 3) == "hs"
    assert gp.stratum(2, 2) == "h1"
    assert gp.stratum(5, 5) == "h4"


def test_stock_frames_count_only_in_the_all_frames_gallery():
    a = [_img(1, 10, 1, 7, pop=9)]
    b = [_img(2, 11, 1, 7, pop=9)]
    assert gp.tight_matches(gp.phash_gallery(a), gp.phash_gallery(b)) == []
    assert len(gp.tight_matches(gp.phash_gallery(a, stock=True),
                                gp.phash_gallery(b, stock=True))) == 1


def test_distinguishing_reads_stated_unit_facts_only():
    assert gp.distinguishing(_adv(1), _adv(2)) == []
    assert gp.distinguishing(_adv(1), _adv(2, floor=4)) == ["floor"]
    assert gp.distinguishing(_adv(1), _adv(2, disposition="3+kk"))[0] == "disposition"
    assert gp.distinguishing(_adv(1), _adv(2, floor=20)) == []  # the idnes placeholder


def test_assemble_routes_same_room_frames_and_counts_strata():
    adverts = {10: _adv(10), 11: _adv(11), 12: _adv(12, area_m2=80.0)}
    images = {
        10: [_img(1, 10, 1, 0xF0F0, "kitchen"), _img(2, 10, 2, 0x1, "bathroom")],
        11: [_img(3, 11, 1, 0xF0F0, "kitchen"), _img(4, 11, 2, 0xFFFFFFFF, "bathroom")],
        12: [_img(5, 12, 1, 0xABCDEF0123, "kitchen")],
    }
    pairs = [{"a": 10, "b": 11, "classes": ["pos_rule"], "notes": []},
             {"a": 10, "b": 12, "classes": ["neg_sib_rnd"], "notes": []},
             {"a": 10, "b": 99, "classes": ["pos_rule"], "notes": []}]
    manifest, counts = gp.assemble(pairs, adverts, images)
    by = {(p["a"], p["b"]): p for p in manifest["pairs"]}
    assert by[(10, 11)]["stratum"] == "h1"
    assert by[(10, 12)]["stratum"] == "h0"
    assert by[(10, 12)]["facts"] and by[(10, 12)]["facts"][0].startswith("area")
    assert counts["pairs_missing_images_by_class"] == {"pos_rule": 1}
    assert [1, 3, "kitchen"] in manifest["frame_pairs"]
    assert [2, 4, "bathroom"] in manifest["frame_pairs"]


def test_truth_drops_conflicts_and_names_the_hardest_negative():
    assert ev.truth(["pos_rule", "pos_merge"]) == "pos"
    assert ev.truth(["neg_mnl", "neg_rule"]) == "neg_rule"
    assert ev.truth(["neg_fused", "pos_rule"]) == "conflict"
    assert ev.truth([]) is None


def test_auc_ranks_missing_scores_lowest():
    assert ev.auc(np.array([0.9, 0.8]), np.array([0.1, 0.2])) == pytest.approx(1.0)
    assert ev.auc(np.array([math.nan]), np.array([0.1])) == pytest.approx(0.0)
    assert ev.auc(np.array([0.5]), np.array([0.5])) == pytest.approx(0.5)


def test_threshold_at_fmr_holds_the_false_match_rate():
    neg = np.arange(100, dtype=float)
    t = ev.threshold_at_fmr(neg, 0.05)
    assert ev.rate_above(neg, t) == pytest.approx(0.05)


def test_pod_routes_union_carries_every_route_bit():
    manifest = {
        "images": [{"image_id": i, "listing_id": 10 if i < 3 else 11, "seq": i, "pop": 1,
                    "tag": "kitchen"} for i in range(1, 5)],
        "pairs": [{"a": 10, "b": 11, "tight": [[1, 3, 0]]}],
        "frame_pairs": [[1, 3, "kitchen"]],
    }
    ids = np.array([1, 2, 3, 4])
    vecs = np.eye(4, dtype=np.float32)
    vecs[3] = vecs[1]
    heads = {"key": ids, "winner": np.array([25, 25, 25, 22]),
             "winner_score": np.array([0.9, 0.9, 0.9, 0.9])}
    routes = pod.build_routes(manifest, dino=(ids, vecs), sscd=None, heads=heads,
                              topk_dino=1, topk_sscd=0)
    assert routes[(1, 3)] & pod.ROUTE_CLIP and routes[(1, 3)] & pod.ROUTE_DHASH
    assert routes[(1, 3)] & pod.ROUTE_HEAD
    assert routes[(2, 4)] & pod.ROUTE_DINO


def test_pod_head_scoring_is_the_logistic_of_the_artifact():
    heads = [{"tag_id": 25, "artifact": {"kind": "tag_head_logreg", "weights": [1.0, 0.0],
                                         "bias": 0.0}},
             {"tag_id": 22, "artifact": {"kind": "tag_head_logreg", "weights": [0.0, 1.0],
                                         "bias": 0.0}}]
    out = pod.score_heads(np.array([7]), np.array([[2.0, 0.0]], dtype=np.float32), heads)
    assert int(out["winner"][0]) == 25
    assert float(out["winner_score"][0]) == pytest.approx(1 / (1 + math.exp(-2.0)), rel=1e-3)


def test_pod_transforms_keep_an_image():
    from PIL import Image

    img = Image.new("RGB", (160, 120), (120, 80, 40))
    rng = random.Random(1)
    for name in pod.SYNTH_TRANSFORMS:
        out = pod.transform(img, name, rng)
        assert out.size[0] > 0 and out.size[1] > 0


def test_superpoint_is_never_a_default_phase():
    assert "match-superpoint" not in pod.PHASES
    assert pod.PHASES[-1] == "upload"


def test_fused_negative_reads_facts_then_colive_same_portal():
    a = _adv(1, source="sreality", broker_key="b", area_m2=23.0)
    b = _adv(2, source="sreality", broker_key="b", area_m2=24.0)
    c = _adv(3, source="remax", broker_key="r", area_m2=24.0)
    assert gp.fused_negative(a, b) == "colive-same-portal"
    assert gp.fused_negative(a, c) is None          # a cross-portal copy may be one unit
    assert gp.fused_negative(a, _adv(4, area_m2=23.0, floor=5)) == "floor"


def test_neighbourhood_keeps_stated_different_colive_siblings_only():
    adverts = {1: _adv(1), 2: _adv(2, floor=5), 3: _adv(3),
               4: _adv(4, floor=6, first_seen="2025-01-01", last_seen="2025-02-01"),
               5: _adv(5, floor=7, broker_key="other")}
    nb = gp.neighbourhood([1], adverts)
    assert [s for s, _ in nb[1]] == [2]              # 3 states nothing, 4 is not co-live, 5 other broker
    assert nb[1][0][1] == "floor"
    many = {i: _adv(i, floor=i % 19) for i in range(1, 40)}
    assert len(gp.neighbourhood([1], many, cap=5)[1]) == 5
    assert gp.neighbourhood([1], many, cap=5) == gp.neighbourhood([1], many, cap=5)


def test_assemble_takes_only_unit_room_frames_of_a_neighbour():
    adverts = {10: _adv(10), 11: _adv(11), 20: _adv(20, floor=6)}
    images = {
        10: [_img(1, 10, 1, 0xF0F0, "kitchen")],
        11: [_img(2, 11, 1, 0xF0F0, "kitchen")],
        20: [_img(5, 20, 1, 1, "kitchen"), _img(6, 20, 2, 2, "kitchen"), _img(7, 20, 3, 3, "kitchen"),
             _img(8, 20, 4, 4, "hallway"), _img(9, 20, 5, 5, "floor_plan")],
    }
    pairs = [{"a": 10, "b": 11, "classes": ["pos_rule"], "notes": []}]
    manifest, counts = gp.assemble(pairs, adverts, images, {"10": [[20, "floor"]]})
    nb_rows = [r for r in manifest["images"] if r["nb"]]
    assert sorted(r["image_id"] for r in nb_rows) == [5, 6, 9]   # 2 kitchens, the plan; no hallway
    assert manifest["neighbours"]["10"] == [[20, "floor", [5, 6, 9]]]
    assert counts["nb_images"] == 3 and counts["images_needed"] == 2


def _pod_manifest():
    imgs = [(1, 10, "kitchen"), (2, 10, "hallway"), (3, 11, "kitchen"), (4, 11, "hallway"),
            (5, 12, "kitchen"), (6, 13, "kitchen"), (7, 20, "kitchen")]
    return {
        "images": [{"image_id": i, "listing_id": l, "seq": i, "pop": 1, "tag": t, "nb": l == 20}
                   for i, l, t in imgs],
        "pairs": [{"a": 10, "b": 11, "classes": ["pos_rule"], "stratum": "h4", "tight": []},
                  {"a": 12, "b": 13, "classes": ["neg_rule"], "stratum": "h0", "tight": []}],
        "frame_pairs": [[1, 3, "kitchen"], [2, 4, "hallway"], [5, 6, "kitchen"]],
        "neighbours": {"10": [[20, "floor", [7]]]},
    }


def test_lg_plan_drops_unit_less_rooms_and_puts_informative_pairs_first():
    manifest = _pod_manifest()
    ids = np.array([1, 2, 3, 4, 5, 6, 7])
    vecs = np.eye(7, dtype=np.float32)
    vecs[6] = vecs[0]                                # the sibling carries 10's kitchen
    routes = pod.build_routes(manifest, dino=(ids, vecs), sscd=None, heads=None,
                              topk_dino=0, topk_sscd=0)
    assert routes[(1, 7)] & pod.ROUTE_NB
    plan = pod.lg_plan(routes, manifest, pod.rooms_of(manifest, None))
    assert (2, 4) not in plan                        # hallway: descriptors only
    assert plan.index((5, 6)) < plan.index((1, 7)) < plan.index((1, 3))   # tier 0, 1, 2


def test_match_rows_round_trip_and_partial_name(tmp_path):
    rows = [(1, 2, 100, 90, 40, 20, 0.5, 30, 25), (3, 4, 10, 10, 2, 0, 0.1, 0, 0)]
    path = str(tmp_path / "lg_aliked.npz")
    assert pod.partial_of(path).endswith("lg_aliked.partial.npz")
    pod.save_match_rows(path, rows)
    back = pod.load_match_rows(path)
    assert [r[:2] for r in back] == [(1, 2), (3, 4)] and back[0][7] == 30
    pod.save_match_rows(path, [])
    assert pod.load_match_rows(path) == []


def test_operating_point_credits_the_veto_before_the_cut():
    y = np.array([True, True, True, False, False, False, False])
    sv = np.array([0.99, 0.97, 0.95, 0.99, 0.98, 0.50, 0.40])
    cat = np.array([-np.inf, -np.inf, -np.inf, 0.99, 0.985, -np.inf, -np.inf])
    raw = ev.operating_point(y, sv, None, 0.0)
    veto = ev.operating_point(y, sv, cat, 0.0)
    assert raw["recall"] == pytest.approx(0.0)      # the two catalogue negatives top the ranking
    assert veto["recall"] == pytest.approx(1.0) and veto["fmr"] == 0.0


def test_catalogue_witness_is_never_the_pair_partner():
    manifest = {
        "images": [{"image_id": 1, "listing_id": 10, "seq": 0}, {"image_id": 2, "listing_id": 11, "seq": 0},
                   {"image_id": 3, "listing_id": 12, "seq": 0, "nb": True}],
        "pairs": [{"a": 10, "b": 11, "classes": ["neg_sib_hard"]}],
        "neighbours": {"10": [[11, "floor", []]]},
    }
    arm = ev.Arm("x", np.array([1, 2, 3]), np.array([[1, 0], [1, 0], [0, 1]], np.float32))
    router = {1: "kitchen", 2: "kitchen", 3: "kitchen"}
    out = ev.pair_room_scores(manifest, arm.sims, router, "kitchen")
    sv, cat, has = out[(10, 11)]
    assert sv == pytest.approx(1.0) and cat == -np.inf and has is False
    manifest["neighbours"]["10"].append([12, "floor", [3]])
    sv, cat, has = ev.pair_room_scores(manifest, arm.sims, router, "kitchen")[(10, 11)]
    assert has is True and cat == pytest.approx(0.0)


def test_dhash_arm_is_64_minus_hamming():
    arm = ev.DhashArm({1: 0b1011, 2: 0b1000, 3: 0})
    _, _, s = arm.sims([1], [2, 3])
    assert s.tolist() == [[62.0, 61.0]]


def test_retrieval_finds_the_planted_partner_among_siblings():
    manifest = {
        "images": [{"image_id": 1, "listing_id": 10, "seq": 0, "phash": 0},
                   {"image_id": 2, "listing_id": 11, "seq": 0, "phash": 2 ** 40 - 1},
                   {"image_id": 3, "listing_id": 12, "seq": 0, "phash": 2 ** 20 - 1, "nb": True}],
        "pairs": [{"a": 10, "b": 11, "classes": ["pos_rule"]}],
    }
    vecs = np.array([[1, 0.1], [1, 0.12], [0.2, 1]], np.float32)
    arm = ev.Arm("x", np.array([1, 2, 3]), vecs)
    out = ev.retrieval_eval(manifest, {"x": arm}, {1: "kitchen", 2: "kitchen", 3: "kitchen"},
                            rooms=("kitchen",))
    assert out["x:kitchen:all"]["R@1"] == 1.0
    assert out["x:kitchen:interesting"]["n"] == 2   # no identical frame: both directions count


def test_public_pairs_doc_cuts_case_notes_to_cohort_and_reason():
    doc = {"pairs": [{"a": 1, "b": 2, "classes": ["neg_fused"],
                      "notes": ["c17:floor:Ruska 137/47: FOUR sreality adverts", "a5:showflat floors 2 vs 5",
                                "a5195e0f-225b-4f01-b86f-46a5a22ed56e"]}], "listing_ids": [1, 2]}
    pub = gp.public_pairs_doc(doc)
    assert pub["pairs"][0]["notes"] == ["c17:floor", "a5:showflat floors 2 vs 5",
                                        "a5195e0f-225b-4f01-b86f-46a5a22ed56e"]
    assert doc["pairs"][0]["notes"][0].startswith("c17:floor:Ruska")   # the input is untouched
