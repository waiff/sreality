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
    assert ev.truth(["neg_fused", "pos_rule"]) == "pos"          # the operator overrides C7
    assert ev.truth(["neg_mnl", "pos_merge"]) == "conflict"
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


def test_head_rooms_come_from_the_registry_label_not_the_id():
    heads = [{"tag_id": 22, "label": "interier - kuchyně"}, {"tag_id": 25, "label": "interier - koupelna"},
             {"tag_id": 39, "label": "podklad - 3d plán"}, {"tag_id": 46, "label": "podklad - půdorys"},
             {"tag_id": 77, "label": "something new"}]
    rooms = pod.head_room_map(heads)
    assert rooms[22] == "kitchen" and rooms[25] == "bathroom"      # swapped ids, right rooms
    assert rooms[39] == "plan_3d" and rooms[46] == "floor_plan"
    assert 77 not in rooms and rooms[28] == "living_room"            # unknown label: id map stays


# --- the pod's GPU: an ordered ladder over both clouds (2026-09-27) ---------------------
# GitHub run 36316992243 prepared run 2 and then found no COMMUNITY capacity for any of the
# three allowed cards; the lane never looked in the secure cloud or at another card.

from scripts import g1_image_stack_dispatch as g1d  # noqa: E402
from scripts import pod_bootstrap  # noqa: E402
from scripts import tagging_bakeoff_dispatch as tb  # noqa: E402
from scripts.runpod_client import RunPodClient, RunPodError  # noqa: E402

_CATALOG = [
    # id, displayName, GB, community $/h, secure $/h, in community, in secure
    ("NVIDIA GeForce RTX 3090", "RTX 3090", 24, 0.22, 0.43, True, True),
    ("NVIDIA GeForce RTX 3090 Ti", "RTX 3090 Ti", 24, 0.27, 0.0, True, False),
    ("NVIDIA RTX A5000", "RTX A5000", 24, 0.16, 0.27, True, True),
    ("NVIDIA L4", "L4", 24, 0.0, 0.43, False, True),
    ("NVIDIA L40S", "L40S", 48, 0.79, 0.86, True, True),
    ("NVIDIA GeForce RTX 3070", "RTX 3070", 8, 0.13, 0.0, True, False),
    ("NVIDIA A100 80GB PCIe", "A100 PCIe", 80, 1.19, 1.64, True, True),
]


class _Resp:
    def __init__(self, status: int = 200, body=None, text: str = "") -> None:
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def iter_lines(self, decode_unicode: bool = True):
        return iter(())

    def close(self) -> None:
        pass


class _Session:
    """RunPod's two APIs, faked: the GraphQL catalogue and REST pod CRUD. `no_capacity`
    names the (card, cloud) rungs that answer RunPod's real no-instances 500."""

    def __init__(self, no_capacity=()) -> None:
        self.headers: dict[str, str] = {}
        self.no_capacity = set(no_capacity)
        self.launches: list[dict] = []
        self.deleted: list[str] = []

    def post(self, url, json=None, timeout=30):
        if url.endswith("/pods"):
            self.launches.append(json)
            if (json["gpuTypeIds"][0], json["cloudType"]) in self.no_capacity:
                return _Resp(500, text='{"error":"create pod: There are no instances '
                                       'currently available","status":500}')
            return _Resp(201, {"id": f"pod{len(self.launches)}", "costPerHr": 0.3})
        return _Resp(200, {"data": {"gpuTypes": [
            {"id": i, "displayName": d, "memoryInGb": m, "communityPrice": c, "securePrice": s,
             "communityCloud": ic, "secureCloud": isc} for i, d, m, c, s, ic, isc in _CATALOG]}})

    def get(self, url, timeout=30, stream=False):
        return _Resp(200, {"desiredStatus": "EXITED"})

    def delete(self, url, timeout=30):
        self.deleted.append(url.rsplit("/", 1)[-1])
        return _Resp(204)


def _rungs(ladder):
    return [(g.id.replace("NVIDIA ", "").replace("GeForce ", ""), g.cloud_type[0]) for g in ladder]


def test_g1_ladder_walks_the_allowlist_in_order_each_card_community_then_secure():
    client = RunPodClient("k", session=_Session())
    allow = ("NVIDIA GeForce RTX 3090", "NVIDIA RTX A5000", "NVIDIA GeForce RTX 3090 Ti", "NVIDIA L4")
    assert _rungs(g1d.gpu_ladder(client, allow, g1d.CLOUD_TYPES["ANY"])) == [
        ("RTX 3090", "C"), ("RTX 3090", "S"), ("RTX A5000", "C"), ("RTX A5000", "S"),
        ("RTX 3090 Ti", "C"), ("L4", "S")]
    assert _rungs(g1d.gpu_ladder(client, allow, g1d.CLOUD_TYPES["COMMUNITY"])) == [
        ("RTX 3090", "C"), ("RTX A5000", "C"), ("RTX 3090 Ti", "C")]
    assert _rungs(g1d.gpu_ladder(client, allow, g1d.CLOUD_TYPES["SECURE"])) == [
        ("RTX 3090", "S"), ("RTX A5000", "S"), ("L4", "S")]


def test_g1_ladder_prices_each_rung_in_its_own_cloud_under_the_lane_cap():
    client = RunPodClient("k", session=_Session())
    ladder = g1d.gpu_ladder(client, ("NVIDIA RTX A5000", "NVIDIA L40S"), ("COMMUNITY", "SECURE"))
    assert [(g.cloud_type, g.price_per_hr()) for g in ladder] == [
        ("COMMUNITY", 0.16), ("SECURE", 0.27), ("COMMUNITY", 0.79), ("SECURE", 0.86)]
    assert all(g.price_per_hr() <= tb.MAX_PRICE_PER_HR for g in ladder)


def test_g1_ladder_matches_whole_words_and_exact_ids():
    client = RunPodClient("k", session=_Session())
    # Shorthand keeps working ("3090" is both 3090s, cheaper first); a full id is that card
    # only; "NVIDIA L4" is never the L40S.
    assert _rungs(g1d.gpu_ladder(client, ("3090", "NVIDIA L4"), ("COMMUNITY", "SECURE"))) == [
        ("RTX 3090", "C"), ("RTX 3090", "S"), ("RTX 3090 Ti", "C"), ("L4", "S")]
    assert _rungs(g1d.gpu_ladder(client, ("NVIDIA GeForce RTX 3090",), ("COMMUNITY",))) == [
        ("RTX 3090", "C")]


def test_g1_ladder_drops_small_and_over_cap_cards_and_refuses_an_empty_ladder():
    client = RunPodClient("k", session=_Session())
    assert _rungs(g1d.gpu_ladder(client, ("3070", "a100", "a5000"), ("COMMUNITY", "SECURE"))) == [
        ("RTX A5000", "C"), ("RTX A5000", "S")]
    with pytest.raises(RunPodError):
        g1d.gpu_ladder(client, ("3070", "a100"), ("COMMUNITY", "SECURE"))


def test_g1_ladder_survives_one_cloud_whose_catalogue_read_fails():
    class _Half(RunPodClient):
        def eligible_gpus(self, *, max_price_per_hr=None, cloud_type="COMMUNITY"):
            if cloud_type == "COMMUNITY":
                raise RunPodError("no COMMUNITY GPU type available under the given price cap")
            return super().eligible_gpus(max_price_per_hr=max_price_per_hr, cloud_type=cloud_type)

    ladder = g1d.gpu_ladder(_Half("k", session=_Session()), ("a5000",), ("COMMUNITY", "SECURE"))
    assert _rungs(ladder) == [("RTX A5000", "S")]


def test_g1_widens_its_own_default_and_leaves_the_tagging_lane_alone():
    assert tb.DEFAULT_GPU_ALLOWLIST == ("3090", "a5000")
    assert g1d.G1_GPU_ALLOWLIST[:3] == ("NVIDIA GeForce RTX 3090", "NVIDIA RTX A5000",
                                        "NVIDIA GeForce RTX 3090 Ti")
    assert len(set(g1d.G1_GPU_ALLOWLIST)) == len(g1d.G1_GPU_ALLOWLIST) == 19
    assert g1d.MIN_GPU_MEMORY_GB == 16
    # bf16 arms (no Volta/Turing) on a cu118 torch (no Blackwell).
    assert not any(b in name for name in g1d.G1_GPU_ALLOWLIST
                   for b in ("V100", "Tesla T4", "RTX 5080", "RTX 5090", "RTX PRO", "Blackwell"))


def test_the_default_ladder_on_the_live_catalogue_of_2026_09_27():
    # RunPod's public gpuTypes answer (no key) on the day run 36316992243 found no
    # community capacity, trimmed to the fields eligible_gpus reads.
    live = [
        ("NVIDIA GeForce RTX 3090", "RTX 3090", 24, 0.22, 0.5, True, True),
        ("NVIDIA RTX A5000", "RTX A5000", 24, 0.16, 0.27, True, True),
        ("NVIDIA GeForce RTX 3090 Ti", "RTX 3090 Ti", 24, 0.27, 0.46, True, False),
        ("NVIDIA L40", "L40", 48, 0.69, 0.82, True, True),
        ("NVIDIA L40S", "L40S", 48, 0.79, 1.09, True, True),
        ("NVIDIA L4", "L4", 24, 0.44, 0.49, False, True),
        ("NVIDIA GeForce RTX 5090", "RTX 5090", 32, 0.69, 0.99, True, True),
        ("Tesla V100-PCIE-16GB", "Tesla V100", 16, 0.19, 0.0, True, False),
        ("NVIDIA GeForce RTX 3080 Ti", "RTX 3080 Ti", 12, 0.18, 0.0, True, False),
    ]
    session = _Session()
    session.post = lambda url, json=None, timeout=30: _Resp(200, {"data": {"gpuTypes": [
        {"id": i, "displayName": d, "memoryInGb": m, "communityPrice": c, "securePrice": s,
         "communityCloud": ic, "secureCloud": isc} for i, d, m, c, s, ic, isc in live]}})
    ladder = g1d.gpu_ladder(RunPodClient("k", session=session), g1d.G1_GPU_ALLOWLIST,
                            g1d.CLOUD_TYPES["ANY"])
    assert [(g.id, g.cloud_type[0], g.price_per_hr()) for g in ladder] == [
        ("NVIDIA GeForce RTX 3090", "C", 0.22), ("NVIDIA GeForce RTX 3090", "S", 0.5),
        ("NVIDIA RTX A5000", "C", 0.16), ("NVIDIA RTX A5000", "S", 0.27),
        ("NVIDIA GeForce RTX 3090 Ti", "C", 0.27),       # not offered in secure
        ("NVIDIA L40", "C", 0.69), ("NVIDIA L40", "S", 0.82),
        ("NVIDIA L40S", "C", 0.79),                      # $1.09 secure is over the cap
        ("NVIDIA L4", "S", 0.49)]                        # secure only


class _Cur:
    def __init__(self, log: list) -> None:
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def execute(self, sql, params=None) -> None:
        self.log.append((" ".join(sql.split())[:40], params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, log: list) -> None:
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def cursor(self):
        return _Cur(self.log)


def _dispatch_pod(monkeypatch, argv, session):
    sql: list = []
    monkeypatch.setenv("RUNPOD_API_KEY", "k")
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://fake")
    monkeypatch.setattr(tb, "_connect", lambda url: _Conn(sql))
    monkeypatch.setattr(tb, "RunPodClient", lambda key: RunPodClient(key, session=session))
    # pod() swaps this module attribute for the run; monkeypatch restores it afterwards.
    monkeypatch.setattr(tb, "_log_arm_reset", tb._log_arm_reset)
    return g1d.main(["--stage", "pod", "--run-id", "2", "--ref", "main", *argv]), sql


def test_the_pod_stage_passes_the_allowlist_through_and_falls_back_to_secure(monkeypatch):
    session = _Session(no_capacity={("NVIDIA RTX A5000", "COMMUNITY")})
    rc, sql = _dispatch_pod(monkeypatch, ["--gpu-allowlist", "NVIDIA RTX A5000,3090",
                                          "--cloud-type", "ANY"], session)
    assert rc == 0
    assert [(b["gpuTypeIds"][0], b["cloudType"]) for b in session.launches] == [
        ("NVIDIA RTX A5000", "COMMUNITY"), ("NVIDIA RTX A5000", "SECURE")]
    assert session.deleted == ["pod2"]          # the rented pod was torn down
    assert any("UPDATE dedup_sim.tag_head_bakeoff_arms" in s for s, _ in sql)


def test_the_pod_stage_rents_nothing_when_no_rung_has_capacity(monkeypatch):
    rungs = {(i, c) for i, *_ in _CATALOG for c in ("COMMUNITY", "SECURE")}
    session = _Session(no_capacity=rungs)
    rc, _ = _dispatch_pod(monkeypatch, ["--cloud-type", "COMMUNITY"], session)
    assert rc == 1
    # The default ladder, community only: the cards this catalogue lists under the cap.
    assert [b["gpuTypeIds"][0] for b in session.launches] == [
        "NVIDIA GeForce RTX 3090", "NVIDIA RTX A5000", "NVIDIA GeForce RTX 3090 Ti", "NVIDIA L40S"]
    assert {b["cloudType"] for b in session.launches} == {"COMMUNITY"}
    assert session.deleted == []


def test_an_empty_allowlist_means_the_g1_default(monkeypatch):
    session = _Session(no_capacity={("NVIDIA GeForce RTX 3090", "COMMUNITY")})
    rc, _ = _dispatch_pod(monkeypatch, ["--gpu-allowlist", ""], session)
    assert rc == 0
    assert [(b["gpuTypeIds"][0], b["cloudType"]) for b in session.launches] == [
        ("NVIDIA GeForce RTX 3090", "COMMUNITY"), ("NVIDIA GeForce RTX 3090", "SECURE")]


def test_the_workflow_hands_g1_its_allowlist_and_cloud_and_the_embed_stage_neither():
    import pathlib

    import yaml

    wf = yaml.safe_load((pathlib.Path(__file__).resolve().parents[2]
                         / ".github/workflows/tagging_bakeoff.yml").read_text())
    inputs = (wf.get("on") or wf[True])["workflow_dispatch"]["inputs"]
    assert inputs["gpu_allowlist"]["default"] == ""
    assert inputs["cloud_type"]["default"] == "ANY"
    assert inputs["cloud_type"]["options"] == ["ANY", "COMMUNITY", "SECURE"]
    steps = {s.get("name"): s for s in wf["jobs"]["bakeoff"]["steps"]}
    g1 = steps["G1 image-stack bake-off"]
    assert g1["env"]["GPU_ALLOWLIST"] == "${{ inputs.gpu_allowlist }}"
    assert g1["env"]["CLOUD_TYPE"] == "${{ inputs.cloud_type }}"
    assert '--gpu-allowlist "${GPU_ALLOWLIST}"' in g1["run"]
    assert '--cloud-type "${CLOUD_TYPE:-ANY}"' in g1["run"]
    embed = steps["Dispatch the embedding pass to a GPU pod"]
    assert "gpu-allowlist" not in embed["run"] and "cloud-type" not in embed["run"]


# --- the pod sizes its CPU pools from what RunPod gave it ---------------------------------


def test_pod_vcpus_reads_runpod_then_the_cgroup_quota_then_affinity(tmp_path, monkeypatch):
    monkeypatch.setattr(pod.os, "sched_getaffinity", lambda _pid: set(range(64)), raising=False)
    empty = tmp_path / "none"
    assert pod.pod_vcpus({"RUNPOD_CPU_COUNT": "6"}, str(empty)) == 6
    v2 = tmp_path / "v2"
    v2.mkdir()
    (v2 / "cpu.max").write_text("900000 100000\n")
    assert pod.pod_vcpus({}, str(v2)) == 9
    (v2 / "cpu.max").write_text("max 100000\n")
    assert pod.pod_vcpus({}, str(v2)) == 64
    v1 = tmp_path / "v1"
    (v1 / "cpu").mkdir(parents=True)
    (v1 / "cpu" / "cpu.cfs_quota_us").write_text("1600000\n")
    (v1 / "cpu" / "cpu.cfs_period_us").write_text("100000\n")
    assert pod.pod_vcpus({"RUNPOD_CPU_COUNT": "junk"}, str(v1)) == 16
    assert pod.pod_vcpus({}, str(empty)) == 64


def test_cpu_bound_pools_never_exceed_the_pods_vcpus():
    assert pod.cpu_workers(16, 6) == 6       # a 4090 pod
    assert pod.cpu_workers(16, 32) == 16     # the requested ceiling still holds
    assert pod.cpu_workers(0, 9) == 9        # 0 = one per vCPU
    assert pod.cpu_workers(4, 0) == 1


def _rooms(kitchen, bathroom, private, n=100):
    out = {}
    for room, (clip, new) in (("kitchen", kitchen), ("bathroom", bathroom), ("private", private)):
        out[f"clip@clip:{room}"] = {"recall@labFMR0.05": clip, "n_pos": n}
        out[f"dinov3@head:{room}"] = {"recall@labFMR0.05": new, "n_pos": n}
    return out


def test_stop_rests_on_the_pooled_private_bar_not_on_one_room():
    # kitchen misses its +0.10 margin and the 0.75 floor, but the pooled private rooms clear +0.08
    report = {"arms_new": ["dinov3"], "rooms": _rooms((0.717, 0.74), (0.538, 0.70), (0.631, 0.73))}
    v = ev.verdict(report)
    assert v["B2_same_room"]["pass"] is True
    assert v["B2_same_room"]["per_room_partial"] == ["kitchen", "kitchen_floor_0.75"]
    assert not v["decision"].startswith("STOP")
    half = v["B2_same_room"]["kitchen"]["ci95_half_width"]
    assert half == pytest.approx(1.96 * math.sqrt(0.74 * 0.26 / 100))


def test_stop_when_the_pooled_private_bar_fails_even_if_the_rooms_pass():
    report = {"arms_new": ["dinov3"], "rooms": _rooms((0.717, 0.83), (0.538, 0.65), (0.631, 0.70))}
    v = ev.verdict(report)
    assert v["B2_same_room"]["pass"] is False
    assert v["decision"].startswith("STOP")
    assert "M1 +2" in v["engine_adoption_bar"]


# --- the 2026-09-27 OOM: bounded decode, shards, resume, partial results -----------------
# Run 2 (pod udlpld675b3zwp, $2.17) was OOM-killed at DINOv3 image 34,848 and restarted ten
# times: every decoded batch stayed alive for the whole arm, the arm was written only at its
# end, and nothing bounded the restarts.


class _Decoded:
    """A stand-in for a decoded image; weakref-able, so the test can count the live ones."""

    def __init__(self, key) -> None:
        self.key = key


class _Vec:
    def __init__(self, a) -> None:
        self.a = a

    def numpy(self):
        return self.a


class _Encoder:
    """The encoder interface embed_arm calls: a batch in, one row per image out."""

    def __init__(self) -> None:
        self.seen: list = []

    def embed(self, imgs, batch_size=32):
        self.seen.extend(i.key for i in imgs)
        return _Vec(np.array([[float(i.key), 1.0] for i in imgs], np.float32))


def test_decoded_batches_keeps_only_the_batches_in_flight_alive():
    # The leak: a finished future holds its result, and the old list held every future.
    import weakref

    live: weakref.WeakSet = weakref.WeakSet()
    peak = 0

    def loader(src):
        img = _Decoded(src)
        live.add(img)
        return img

    items = [(i, i) for i in range(400)]
    got = 0
    for ids, imgs in pod.decoded_batches(items, batch=8, workers=4, loader=loader, prefetch=3):
        got += len(ids)
        del imgs
        peak = max(peak, len(live))
    assert got == 400
    # the batch being consumed + at most `prefetch` decoded ahead (+1 racing its submit)
    assert peak <= 8 * (3 + 2), peak


def test_decoded_batches_blocks_the_decoder_while_the_consumer_is_behind():
    calls = []
    items = [(i, i) for i in range(1000)]
    stream = pod.decoded_batches(items, batch=10, workers=8,
                                 loader=lambda s: calls.append(s) or _Decoded(s), prefetch=2)
    next(stream)
    import time as _t

    _t.sleep(0.3)
    # 1000 images are waiting; backpressure means only the next `prefetch` batches decode.
    assert len(calls) <= 10 * (1 + 2 + 1)
    stream.close()


def test_plan_decode_sizes_the_queue_from_the_pods_memory():
    gb = 2**30
    # a 3090 pod (~60 GB cgroup limit), DINOv3 @768: the full batch, one batch per worker
    assert pod.plan_decode(60 * gb, 16, 32, 768 * 768 * 4) == (32, 16)
    # a 2 GB box, SSCD @320: the batch holds, the prefetch shrinks to what 10 % of RAM holds
    batch, prefetch = pod.plan_decode(2 * gb, 16, 64, 320 * 320 * 4)
    assert batch == 64 and 1 <= prefetch < 16
    assert (prefetch + 1) * batch * 320 * 320 * 4 <= 0.1 * 2 * gb
    # so little RAM that two batches would not fit: the batch shrinks, never below one
    assert pod.plan_decode(64 * 2**20, 8, 64, 768 * 768 * 4) == (1, 1)


def test_pod_memory_reads_meminfo_and_the_cgroup_limit(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       263842516 kB\nMemFree:  1 kB\n")
    own = tmp_path / "self_cgroup"
    own.write_text("0::/\n")                       # a container with its own namespace
    v2 = tmp_path / "v2"
    v2.mkdir()
    (v2 / "memory.max").write_text(f"{62 * 2**30}\n")
    got = pod.pod_memory(str(meminfo), str(v2), str(own))
    assert got["mem_total"] == 263842516 * 1024
    assert got["cgroup_limit"] == 62 * 2**30 and got["limit"] == 62 * 2**30
    (v2 / "memory.max").write_text("max\n")
    assert pod.pod_memory(str(meminfo), str(v2), str(own))["limit"] == 263842516 * 1024
    # A process in a nested cgroup (a systemd scope, a host-namespace container): the
    # tightest limit along its path wins.
    own.write_text("0::/user.slice/run-1.scope\n")
    (v2 / "user.slice" / "run-1.scope").mkdir(parents=True)
    (v2 / "user.slice" / "memory.max").write_text(f"{8 * 2**30}\n")
    (v2 / "user.slice" / "run-1.scope" / "memory.max").write_text(f"{3 * 2**30}\n")
    assert pod.pod_memory(str(meminfo), str(v2), str(own))["cgroup_limit"] == 3 * 2**30
    v1 = tmp_path / "v1"
    (v1 / "memory").mkdir(parents=True)
    (v1 / "memory" / "memory.limit_in_bytes").write_text("9223372036854771712\n")   # unlimited
    assert pod.pod_memory(str(meminfo), str(v1), str(tmp_path / "none"))["cgroup_limit"] is None
    (v1 / "memory" / "memory.limit_in_bytes").write_text(f"{16 * 2**30}\n")
    assert pod.pod_memory(str(meminfo), str(v1), str(tmp_path / "none"))["limit"] == 16 * 2**30


def test_fs_type_names_the_filesystem_under_the_cache(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("overlay / overlay rw 0 0\ntmpfs /dev/shm tmpfs rw 0 0\n"
                      "/dev/sda1 /opt/podboot ext4 rw 0 0\n")
    assert pod.fs_type("/opt/podboot/img", str(mounts)) == "ext4"
    assert pod.fs_type("/dev/shm/cache", str(mounts)) == "tmpfs"
    assert pod.fs_type("/root", str(mounts)) == "overlay"


def test_embed_arm_writes_shards_and_a_restart_resumes_after_the_last_one(tmp_path):
    import os as _os
    import time as _t

    out = str(tmp_path / "emb_x.npz")
    sdir = pod.shard_dir(out)
    items = [(i, i) for i in range(1, 11)]
    shards: list = []

    def loader(src):
        if src == 7:
            raise OSError("bytes that will never decode")
        return _Decoded(src)

    # A kill (here: a deadline already past) after the first batch leaves one shard and no
    # arm file — the state a pass dies in.
    killed = _Encoder()
    cut = pod.embed_arm("x", killed, items, batch=2, workers=2, loader=loader, out_path=out,
                        beat=lambda m: None, deadline=_t.monotonic() - 1, prefetch=1,
                        shard_size=2, on_shard=shards.append)
    assert cut["partial"] is True and not _os.path.exists(out)
    assert len(pod.shard_files(sdir)) == 1 and killed.seen == [1, 2]
    # The next pass embeds only what no shard holds, then assembles the arm.
    resumed = _Encoder()
    done = pod.embed_arm("x", resumed, items, batch=2, workers=2, loader=loader, out_path=out,
                         beat=lambda m: None, deadline=None, prefetch=1, shard_size=2,
                         on_shard=shards.append)
    assert resumed.seen == [3, 4, 5, 6, 8, 9, 10]      # 1-2 came from the shard, 7 is bad
    assert done["n"] == 9 and done["resumed"] == 2
    keys, vecs = pod.load_vecs(out)
    assert sorted(keys.tolist()) == [1, 2, 3, 4, 5, 6, 8, 9, 10]
    assert vecs.shape == (9, 2) and not _os.path.exists(sdir)   # no shards left behind
    assert shards and all(s.startswith("x shard") for s in shards)
    # A finished arm is never embedded again.
    assert pod.embed_arm("x", _Encoder(), items, batch=2, workers=2, loader=loader,
                         out_path=out, beat=lambda m: None, deadline=None) == {
        "arm": "x", "skipped": "exists"}


def test_an_undecodable_image_is_recorded_and_never_retried(tmp_path):
    out = str(tmp_path / "emb_y.npz")
    items = [(i, i) for i in range(1, 5)]
    tries: list = []

    def loader(src):
        tries.append(src)
        if src == 2:
            raise OSError("truncated")
        return _Decoded(src)

    pod.embed_arm("y", _Encoder(), items, batch=4, workers=1, loader=loader, out_path=out,
                  beat=lambda m: None, deadline=0.0000001, prefetch=1, shard_size=100)
    done, bad = pod.embed_shard_keys(pod.shard_dir(out))
    assert done == {1, 3, 4} and bad == {2}
    tries.clear()
    pod.embed_arm("y", _Encoder(), items, batch=4, workers=1, loader=loader, out_path=out,
                  beat=lambda m: None, deadline=None, prefetch=1, shard_size=100)
    assert tries == []                              # nothing left to try, 2 included


def test_synthetic_string_keys_survive_the_shard_round_trip(tmp_path):
    out = str(tmp_path / "syn_z.npz")
    items = [(f"{i}:crop10", i) for i in range(1, 6)]

    class _Enc(_Encoder):
        def embed(self, imgs, batch_size=32):
            self.seen.extend(i.key for i in imgs)
            return _Vec(np.ones((len(imgs), 3), np.float32))

    import time as _t

    pod.embed_arm("z", _Enc(), items, batch=2, workers=1, loader=_Decoded, out_path=out,
                  beat=lambda m: None, deadline=_t.monotonic() - 1, prefetch=1, shard_size=2)
    rest = _Enc()
    pod.embed_arm("z", rest, items, batch=2, workers=1, loader=_Decoded, out_path=out,
                  beat=lambda m: None, deadline=None, prefetch=1, shard_size=2)
    assert rest.seen == [3, 4, 5]                   # the sources of 3..5:crop10 only
    keys, _ = pod.load_vecs(out)
    assert sorted(keys.tolist()) == [f"{i}:crop10" for i in range(1, 6)]


def test_restorable_takes_results_and_shards_and_nothing_that_climbs_out():
    assert pod.restorable("emb_sscd.npz") and pod.restorable("synthetic.json")
    assert pod.restorable("emb_dinov3.shards/000012.npz")
    assert pod.restorable("lg_disk.shards/legacy.npz")
    for bad in ("report.json", "../x.npz", "a/b/c.npz", "emb_dinov3.shards/../x.npz",
                "notshards/000001.npz", "emb_x.shards/000001.npz.tmp.npz", "x.txt"):
        assert not pod.restorable(bad), bad


def test_restore_brings_back_shards_of_an_unfinished_arm(tmp_path, monkeypatch):
    import io as _io
    import tarfile as _tar

    src = tmp_path / "src"
    (src / "emb_dinov3.shards").mkdir(parents=True)
    np.savez(src / "emb_sscd.npz", key=np.array([1]), vec=np.ones((1, 2), np.float16))
    np.savez(src / "emb_dinov3.shards" / "000000.npz", key=np.array([1]),
             vec=np.ones((1, 2), np.float16), bad=np.array([]))
    (src / "report.json").write_text("{}")
    blob = _io.BytesIO()
    with _tar.open(fileobj=blob, mode="w") as tar:
        tar.add(src, arcname="g1_results")

    class _R2:
        def object_size(self, key):
            return len(blob.getvalue())

        def download_file(self, key, path):
            with open(path, "wb") as fh:
                fh.write(blob.getvalue())

    from scraper import image_storage

    monkeypatch.setattr(image_storage.R2Client, "from_env", classmethod(lambda cls, **kw: _R2()))
    out = tmp_path / "run-2"
    out.mkdir()
    restored = pod.restore_checkpoint(str(out), 2)
    assert sorted(restored) == ["emb_dinov3.shards/000000.npz", "emb_sscd.npz"]
    assert pod.embed_shard_keys(pod.shard_dir(str(out / "emb_dinov3.npz")))[0] == {1}
    assert not (tmp_path / "run-2.restore.tar").exists()      # streamed, then removed


def test_durable_units_move_only_when_work_lands(tmp_path):
    out = tmp_path / "run"
    (out / "emb_dinov3.shards").mkdir(parents=True)
    (out / "report.json").write_text("{}")
    assert pod.durable_units(str(out)) == 0          # a report rewrite is not progress
    (out / "emb_sscd.npz").write_bytes(b"x")
    (out / "emb_dinov3.shards" / "000000.npz").write_bytes(b"x")
    (out / "emb_dinov3.shards" / "000001.npz.tmp.npz").write_bytes(b"x")
    assert pod.durable_units(str(out)) == 2


def test_reporter_alive_lines_carry_units_and_rss(caplog):
    rep = pod.Reporter(None, 2, units_fn=lambda: 17)
    with caplog.at_level("INFO"):
        rep.run_note(alive="embed dinov3 4096/98578")
    assert "units=17" in caplog.text and "rss=" in caplog.text


def test_finalize_ships_what_is_on_disk_and_closes_every_open_arm(tmp_path):
    out = tmp_path / "run-2"
    (out / "emb_dinov3.shards").mkdir(parents=True)
    np.savez(out / "emb_sscd.npz", key=np.array([1]), vec=np.ones((1, 2), np.float16))
    (out / "emb_dinov3.shards" / "000000.npz").write_bytes(b"x")
    log: list = []
    assert pod.finalize(str(out), 2, "code=137 failures=1 pass=1", local_only=True,
                        connect=lambda: _Conn(log), units_fn=lambda: 2) == 0
    import json as _json
    import tarfile as _tar

    with _tar.open(str(out) + ".tar") as tar:
        names = tar.getnames()
    assert "g1_results/emb_sscd.npz" in names
    assert "g1_results/emb_dinov3.shards/000000.npz" in names
    assert _json.loads((out / "report.json").read_text())["gave_up"]["reason"].startswith("code=137")
    closed = [p for s, p in log if s.startswith("UPDATE dedup_sim.tag_head_bakeoff_arms")]
    assert closed and "payload gave up (code=137" in closed[0]["note"]
    assert {"run_id": 2, "status": "failed"} in [p for _s, p in log]


class _R2Log:
    """A fake R2 whose uploads land in a shared event log, failing the first `fails`."""

    def __init__(self, events: list, fails: int = 0) -> None:
        self.events = events
        self.fails = fails

    def upload_file(self, key, path, content_type=None):
        if self.fails:
            self.fails -= 1
            self.events.append(("upload-error", key))
            raise ConnectionError("R2 503")
        self.events.append(("upload", key))


class _EventConn(_Conn):
    def cursor(self):
        return _Cur(self.log)


def _gave_up_dir(tmp_path):
    out = tmp_path / "run-2"
    (out / "emb_dinov3.shards").mkdir(parents=True)
    np.savez(out / "emb_sscd.npz", key=np.array([1]), vec=np.ones((1, 2), np.float16))
    return out


def test_finalize_uploads_before_it_touches_the_database(tmp_path, monkeypatch):
    # Review of 17bea70d: --finalize connected FIRST, so an unreachable database raised
    # before the upload that needs only R2. And closing the arms is the watchdog's
    # all-terminal cue, so it must come after the upload, never before.
    from scraper import image_storage

    events: list = []
    r2 = _R2Log(events)
    monkeypatch.setattr(image_storage.R2Client, "from_env", classmethod(lambda cls, **kw: r2))

    def connect():
        events.append(("connect", None))
        return _EventConn(events)

    assert pod.finalize(str(_gave_up_dir(tmp_path)), 2, "code=137", local_only=False,
                        connect=connect) == 0
    kinds = [e[0] for e in events]
    assert kinds.index("upload") < kinds.index("connect")
    closed = [i for i, e in enumerate(events)
              if str(e[0]).startswith("UPDATE dedup_sim.tag_head_bakeoff_arms")]
    assert closed and closed[0] > kinds.index("upload")


def test_finalize_still_uploads_when_the_database_is_unreachable(tmp_path, monkeypatch):
    from scraper import image_storage

    events: list = []
    monkeypatch.setattr(image_storage.R2Client, "from_env",
                        classmethod(lambda cls, **kw: _R2Log(events)))

    def connect():
        raise OSError("connection refused")

    assert pod.finalize(str(_gave_up_dir(tmp_path)), 2, "code=137", local_only=False,
                        connect=connect) == 0
    assert events == [("upload", "bakeoff/g1-image-stack/2/results.tar")]


def test_the_final_upload_is_retried_in_process(tmp_path, monkeypatch):
    # A transient R2 failure of the one final upload used to exit 1 with every arm already
    # terminal: the watchdog's teardown beat the restart that would have retried.
    from scraper import image_storage

    events: list = []
    r2 = _R2Log(events, fails=2)
    monkeypatch.setattr(image_storage.R2Client, "from_env", classmethod(lambda cls, **kw: r2))
    slept: list = []
    key = pod.upload_with_retry(str(_gave_up_dir(tmp_path)), 2, False, sleep=slept.append)
    assert key == "bakeoff/g1-image-stack/2/results.tar"
    assert [e[0] for e in events] == ["upload-error", "upload-error", "upload"]
    assert slept == list(pod.UPLOAD_BACKOFF_S[:2])
    # Past the backoff the failure is real: it raises, and a finalize says so.
    r2.fails = 99
    with pytest.raises(ConnectionError):
        pod.upload_with_retry(str(_gave_up_dir(tmp_path / "b")), 2, False, sleep=slept.append)
    assert pod.finalize(str(_gave_up_dir(tmp_path / "c")), 2, "code=137", local_only=False,
                        backoff=(0.0,)) == 1


def test_the_payloads_final_upload_goes_through_the_retry():
    import inspect

    src = inspect.getsource(pod.main)
    tail = src[src.index("finally:"):]
    assert "upload_with_retry(" in tail and "upload_results(" not in tail


# --- the evaluator reads finished arms only, and says PARTIAL -------------------------


def test_arm_status_counts_an_arm_finished_only_by_its_own_file(tmp_path):
    np.savez(tmp_path / "emb_sscd.npz", key=np.array([1]), vec=np.ones((1, 2)))
    np.savez(tmp_path / "emb_dinov2.npz", key=np.array([1]), vec=np.ones((1, 2)))
    (tmp_path / "emb_dinov3.shards").mkdir()
    for i in range(3):
        (tmp_path / "emb_dinov3.shards" / f"{i:06d}.npz").write_bytes(b"x")
    finished, unfinished = ev.arm_status(str(tmp_path))
    assert finished == ["sscd", "dinov2"]
    assert unfinished["dinov3"] == "3 shards on disk"
    assert unfinished["lg_aliked"] == "not started" and "lg_superpoint" not in unfinished
    assert unfinished["synthetic"].startswith("0/6 files")


def test_lightglue_shards_are_read_only_on_request(tmp_path):
    (tmp_path / "lg_disk.shards").mkdir()
    pod.save_match_rows(str(tmp_path / "lg_disk.shards" / "000000.npz"),
                        [(1, 2, 100, 90, 40, 20, 0.5, 30, 25)])
    assert ev.load_lg(str(tmp_path)) == {}
    assert ev.load_lg(str(tmp_path), include_partial=True)["disk"][(1, 2)] == (40, 30, 25)


def test_a_partial_run_reports_bars_on_finished_arms_only_and_never_stops():
    # The pooled private bar FAILS on the finished arm; with DINOv3 and LightGlue unfinished
    # that is a readout, not a STOP.
    report = {"arms_new": ["sscd"], "arms_finished": ["sscd"],
              "arms_unfinished": {"dinov3": "3 shards on disk", "lg_aliked": "not started"},
              "rooms": {f"{a}@hc:{r}": {"recall@labFMR0.05": v, "n_pos": 100}
                        for r in ("kitchen", "bathroom", "private")
                        for a, v in (("clip", 0.63), ("sscd", 0.60), ("dinov3", 0.95))}}
    report["rooms"].update({f"clip@clip:{r}": {"recall@labFMR0.05": 0.63, "n_pos": 100}
                            for r in ("kitchen", "bathroom", "private")})
    v = ev.verdict(report)
    assert v["decision"].startswith("PARTIAL RUN") and v["partial"] is True
    assert "dinov3 (3 shards on disk)" in v["decision"]
    assert v["B2_same_room"]["private"]["best"].startswith("sscd")   # dinov3 is not read
    assert v["B2_same_room"]["arms_read"] == ["sscd"]
    assert v["B3_geometry"]["pass"] is None and "no finished arm" in v["B3_geometry"]["not_reported"]
    assert v["B1_copy"]["not_reported"]


def test_a_complete_run_is_not_partial():
    report = {"arms_new": ["dinov3"], "arms_finished": list(ev.EXPECTED_ARMS),
              "arms_unfinished": {}, "rooms": _rooms((0.717, 0.83), (0.538, 0.65), (0.631, 0.70))}
    v = ev.verdict(report)
    assert v["partial"] is False and v["decision"].startswith("STOP")


# --- the dispatcher wires the give-up call and the units marker ------------------------


def test_the_g1_pod_is_launched_with_a_finalize_and_a_units_watchdog(monkeypatch):
    seen = {}

    def fake_run_pod(plan, args, select=None, units=False, max_passes=None):
        seen.update(start=plan.start_cmd[-1], units=units, max_passes=max_passes)
        return 0

    monkeypatch.setattr(tb, "_run_pod", fake_run_pod)
    monkeypatch.setattr(tb, "_log_arm_reset", tb._log_arm_reset)
    assert g1d.main(["--stage", "pod", "--run-id", "2", "--ref", "main", "--dry-run"]) == 0
    assert seen["units"] is True
    # G1 alone opts in to the restart bound, and its watchdog to the pass rail behind it.
    assert seen["max_passes"] == pod_bootstrap.MAX_PAYLOAD_FAILURES
    assert "python -m scripts.g1_image_stack_pod --run-id=2" in seen["start"]
    assert "--finalize" in seen["start"] and "payload gave-up" in seen["start"]
    assert f'-ge {pod_bootstrap.MAX_PAYLOAD_FAILURES} ]' in seen["start"]


def test_collect_evaluates_whatever_arms_finished(tmp_path, monkeypatch):
    import gzip as _gzip
    import io as _io
    import json as _json
    import tarfile as _tar

    src = tmp_path / "pod"
    src.mkdir()
    np.savez(src / "emb_sscd.npz", key=np.array([1, 2]), vec=np.eye(2, dtype=np.float16))
    (src / "emb_dinov3.shards").mkdir()
    np.savez(src / "emb_dinov3.shards" / "000000.npz", key=np.array([1]),
             vec=np.ones((1, 2), np.float16), bad=np.array([]))
    blob = _io.BytesIO()
    with _tar.open(fileobj=blob, mode="w") as tar:
        tar.add(src, arcname="g1_results")
    manifest = {"images": [{"image_id": 1, "listing_id": 10, "seq": 0, "phash": 0, "tag": "kitchen"},
                           {"image_id": 2, "listing_id": 11, "seq": 0, "phash": 7, "tag": "kitchen"}],
                "pairs": [{"a": 10, "b": 11, "classes": ["pos_rule"], "stratum": "h0",
                           "n_tight": 0, "n_tight_all": 0}],
                "frame_pairs": [], "neighbours": {}}
    mbuf = _io.BytesIO()
    with _gzip.GzipFile(fileobj=mbuf, mode="wb") as fh:
        fh.write(_json.dumps(manifest).encode())
    store = {"bakeoff/g1-image-stack/2/results.tar": blob.getvalue(),
             "bakeoff/g1-image-stack/2/manifest.json.gz": mbuf.getvalue()}

    class _R2:
        def object_size(self, key):
            return len(store[key]) if key in store else None

        def download_file(self, key, path):
            with open(path, "wb") as fh:
                fh.write(store[key])

    from scraper import image_storage

    monkeypatch.setattr(image_storage.R2Client, "from_env", classmethod(lambda cls, **kw: _R2()))
    out = tmp_path / "g1_out"
    assert g1d.main(["--stage", "collect", "--run-id", "2", "--out", str(out)]) == 0
    report = _json.loads((out / "eval.json").read_text())
    assert report["arms_finished"] == ["sscd"] and report["partial"] is True
    assert report["verdict"]["decision"].startswith("PARTIAL RUN")
    assert (out / "eval.md").read_text().startswith("**PARTIAL RUN**")
    assert not (out / "results.tar").exists()
