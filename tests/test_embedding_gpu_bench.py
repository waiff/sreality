"""Pure-math tests for scripts/embedding_gpu_bench.py (no torch/transformers needed)."""

from scripts.embedding_gpu_bench import (
    auc,
    hamming64,
    is_shared_photo,
    recall_at_precision,
    score_pairs,
    summarize,
)


def test_hamming64_identical_and_negative_bigints():
    assert hamming64(12345, 12345) == 0
    assert hamming64(0, 1) == 1
    # pHash is stored as a SIGNED bigint; sign must not distort the distance
    assert hamming64(-1, -1) == 0
    assert hamming64(-1, 0) == 64
    assert hamming64(-9223372036854775808, 0) == 1  # only the sign bit differs


def test_auc_perfect_and_random():
    assert auc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert auc([0.1, 0.2], [0.9, 0.8]) == 0.0
    assert auc([0.5], [0.5]) == 0.5  # tie -> average rank
    assert auc([], [0.5]) is None


def test_recall_at_precision_thresholds():
    pos = [0.9, 0.8, 0.7]
    neg = [0.75, 0.1]
    # at t=0.8: 2 TP, 0 FP -> precision 1.0, recall 2/3
    r, t = recall_at_precision(pos, neg, 1.0)
    assert r == 2 / 3 and t == 0.8
    # allowing precision >= 0.75: t=0.7 gives 3 TP, 1 FP -> precision 0.75, recall 1.0
    r, t = recall_at_precision(pos, neg, 0.75)
    assert r == 1.0 and t == 0.7


def _fixture():
    images = {
        "1": {"phash": 100, "render_score": None},
        "2": {"phash": 100, "render_score": 0.1},   # phash-identical to 1
        "3": {"phash": 500, "render_score": 0.99},  # render
        "4": {"phash": None, "render_score": 0.2},
    }
    pairs = [
        {"pair_id": "p1", "is_same": True, "category": "byt", "source": "s",
         "image_pairs": [
             {"a": 1, "b": 2, "tag": "kitchen", "clip_cos": 0.95},   # shared via phash
             {"a": 1, "b": 4, "tag": "kitchen", "clip_cos": 0.80},
             {"a": 1, "b": 3, "tag": "bedroom", "clip_cos": 0.90},   # render side
         ]},
        {"pair_id": "p2", "is_same": False, "category": "byt", "source": "s",
         "image_pairs": [
             {"a": 4, "b": 2, "tag": "kitchen", "clip_cos": 0.9995},  # shared via clip ceiling
         ]},
    ]
    return images, pairs


def test_is_shared_photo_rules():
    images, pairs = _fixture()
    assert is_shared_photo(pairs[0]["image_pairs"][0], images, 2)       # phash equal
    assert not is_shared_photo(pairs[0]["image_pairs"][1], images, 2)   # one phash NULL
    assert is_shared_photo(pairs[1]["image_pairs"][0], images, 2)       # clip_cos >= 0.999


def test_score_pairs_render_shared_and_tag_filters():
    images, pairs = _fixture()
    cos = lambda a, b, ip: ip.get("clip_cos")  # noqa: E731
    # render filter alone: p1 bedroom row (render side) drops, max = 0.95
    all_scores = score_pairs(pairs, images, cos, rmin=0.95, exclude_shared=False, hamming_max=2)
    assert all_scores == {"p1": 0.95, "p2": 0.9995}
    # shared exclusion: p1 falls back to 0.80; p2 loses its only row -> no score
    ns = score_pairs(pairs, images, cos, rmin=0.95, exclude_shared=True, hamming_max=2)
    assert ns == {"p1": 0.80}
    # tag filter: bedroom-only with rmin off -> only the bedroom row survives
    bed = score_pairs(pairs, images, cos, rmin=1.01, exclude_shared=False,
                      hamming_max=2, tag="bedroom")
    assert bed == {"p1": 0.90}
    # encoder returning None for an image pair must not score it
    none_cos = lambda a, b, ip: None  # noqa: E731
    assert score_pairs(pairs, images, none_cos, rmin=0.95,
                       exclude_shared=False, hamming_max=2) == {}


def test_summarize_has_operating_points():
    labels = {f"p{i}": i < 8 for i in range(12)}
    scores = {f"p{i}": (0.9 - 0.01 * i if i < 8 else 0.5 + 0.01 * i) for i in range(12)}
    out = summarize(scores, labels)
    assert out["n_pos"] == 8 and out["n_neg"] == 4
    assert out["auc"] == 1.0
    assert out["recall@p1.0"]["recall"] == 1.0
    assert "separation" in out and "pos" in out and "neg" in out
