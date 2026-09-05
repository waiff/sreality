"""The pod-side bake-off harness, exercised entirely offline.

No network, no HuggingFace Hub, no RunPod, no GPU and no database — the harness's
torch/transformers imports all live inside functions, so everything below runs on
Pillow and the standard library alone. The two things the tests are most careful
about are the two that were WRONG in the predecessor: the exclusion filter sharing a
threshold with the positive population, and one hardcoded pooling slice standing in
for four different encoder families.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from scripts import dinov3_bakeoff as bo


# ---------------------------------------------------------------------------
# The lifted math — verified against hand-computed examples, not assumed correct
# ---------------------------------------------------------------------------

def test_hamming64_identical_and_negative_bigints() -> None:
    assert bo.hamming64(12345, 12345) == 0
    assert bo.hamming64(0, 1) == 1
    # pHash is stored as a SIGNED bigint; the sign must not distort the distance.
    assert bo.hamming64(-1, -1) == 0
    assert bo.hamming64(-1, 0) == 64
    assert bo.hamming64(-9223372036854775808, 0) == 1  # only the sign bit differs


def test_auc_perfect_reversed_and_tied() -> None:
    assert bo.auc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert bo.auc([0.1, 0.2], [0.9, 0.8]) == 0.0
    assert bo.auc([0.5], [0.5]) == 0.5  # a tie takes the average rank
    assert bo.auc([], [0.5]) is None
    assert bo.auc([0.5], []) is None


def test_auc_hand_computed_half_overlap() -> None:
    # pos {0.4, 0.6}, neg {0.5, 0.7}. Of the 4 pos/neg comparisons exactly one
    # (0.6 > 0.5) puts a positive above a negative, so AUC = 1/4.
    assert bo.auc([0.4, 0.6], [0.5, 0.7]) == 0.25


def test_recall_at_precision_thresholds() -> None:
    pos = [0.9, 0.8, 0.7]
    neg = [0.75, 0.1]
    # at t=0.8: 2 TP, 0 FP -> precision 1.0, recall 2/3
    r, t = bo.recall_at_precision(pos, neg, 1.0)
    assert r == 2 / 3 and t == 0.8
    # allowing precision >= 0.75: t=0.7 gives 3 TP, 1 FP -> precision 0.75, recall 1.0
    r, t = bo.recall_at_precision(pos, neg, 0.75)
    assert r == 1.0 and t == 0.7
    assert bo.recall_at_precision([], neg, 1.0) == (0.0, None)


def test_pctl_picks_the_documented_index() -> None:
    vals = [0.1, 0.2, 0.3, 0.4, 0.5]
    assert bo._pctl(vals, 0.0) == 0.1
    assert bo._pctl(vals, 0.5) == 0.3
    assert bo._pctl(vals, 0.99) == 0.5  # clamps at the last element


def test_summarize_separates_and_counts() -> None:
    scores = {"p1": 0.95, "p2": 0.93, "n1": 0.20, "n2": 0.25}
    labels = {"p1": True, "p2": True, "n1": False, "n2": False}
    out = bo.summarize(scores, labels)
    assert out["n_pos"] == 2 and out["n_neg"] == 2
    assert out["auc"] == 1.0
    # separation is pos p50 minus neg p90, both by the same index rule as _pctl.
    assert out["separation"] == pytest.approx(0.95 - 0.25, abs=1e-9)
    assert out["recall@p1.0"]["recall"] == 1.0


def test_summarize_with_only_negatives_reports_no_auc() -> None:
    # P2 and P4 are inspection sets: percentiles are meaningful, AUC is not.
    out = bo.summarize({"a": 0.4, "b": 0.5}, {"a": False, "b": False})
    assert out["n_pos"] == 0 and "auc" not in out and "neg" in out


def test_worst_pairs_returns_the_top_scoring_negatives() -> None:
    scores = {"P3:1:2": 0.91, "P3:3:4": 0.99, "P1b:x": 1.0, "P3:5:6": 0.10}
    labels = {"P3:1:2": False, "P3:3:4": False, "P1b:x": True, "P3:5:6": False}
    worst = bo.worst_pairs(scores, labels, k=2)
    assert [w["pair"] for w in worst] == ["P3:3:4", "P3:1:2"]


# ---------------------------------------------------------------------------
# The exclusion: two INDEPENDENT knobs (the predecessor's fatal default)
# ---------------------------------------------------------------------------

def _images() -> dict:
    return {
        "1": {"phash": 0b1010, "render_score": None},
        "2": {"phash": 0b1010, "render_score": 0.1},    # pHash-identical to 1
        "3": {"phash": 0b1011, "render_score": 0.99},   # 1 bit from 1; a render
        "4": {"phash": None, "render_score": 0.2},
        "5": {"phash": (1 << 40), "render_score": 0.0},  # far from everything
    }


def test_the_exclusion_hamming_is_independent_of_the_p1a_definition() -> None:
    """The predecessor's bug, pinned.

    P1a is DEFINED as pHash Hamming <= 2. If the contamination filter reuses that same
    threshold, every P1a positive is also 'a shared photo' and the positive set is
    empty by construction — ENCODER-DECISION §5.2 fix 1. Here the same pair is a
    positive at the definition and survives at a filter set independently.
    """
    images = _images()
    p1a_pair = {"a": 1, "b": 3, "clip_cos": 0.5}  # Hamming 1 -> a P1a POSITIVE
    # The predecessor's shape: filter threshold == the definition. Everything drops.
    assert bo.is_shared_photo(p1a_pair, images, hamming_max=2, clip_limbs=False)
    # This harness sets the filter separately; the population survives.
    assert not bo.is_shared_photo(p1a_pair, images, hamming_max=0, clip_limbs=False)

    positives = [p for p in [p1a_pair]
                 if not bo.is_shared_photo(p, images, hamming_max=0, clip_limbs=False)]
    assert positives, "the positive population must not be empty by construction"


def test_clip_derived_limbs_are_switchable() -> None:
    images = _images()
    at_ceiling = {"a": 4, "b": 5, "clip_cos": 0.9995}
    assert bo.is_shared_photo(at_ceiling, images, hamming_max=0, clip_limbs=True)
    # With the incumbent's opinion off, the same pair survives — which is the point:
    # one of the arms IS the incumbent.
    assert not bo.is_shared_photo(at_ceiling, images, hamming_max=0, clip_limbs=False)

    render_pair = {"a": 3, "b": 5, "clip_cos": 0.1}
    assert bo.is_shared_photo(render_pair, images, hamming_max=0, clip_limbs=True,
                              render_max=0.95)
    assert not bo.is_shared_photo(render_pair, images, hamming_max=0, clip_limbs=False)


def test_a_missing_phash_is_never_a_shared_photo() -> None:
    images = _images()
    assert not bo.is_shared_photo({"a": 1, "b": 4, "clip_cos": None}, images,
                                  hamming_max=64, clip_limbs=False)


# ---------------------------------------------------------------------------
# The synthetic transforms (P1b) — each one's meaningful property
# ---------------------------------------------------------------------------

def _photo(width: int = 120, height: int = 90):
    """A deliberately noisy 4:3 image: a flat colour would JPEG round-trip exactly
    and make the re-encode test vacuous."""
    from PIL import Image

    im = Image.new("RGB", (width, height))
    px = im.load()
    for x in range(width):
        for y in range(height):
            px[x, y] = ((x * 37 + y * 11) % 256, (x * 5 + y * 71) % 256, (x ^ y) % 256)
    return im


def test_crop10_removes_a_tenth_of_each_side() -> None:
    out = bo.t_crop10(_photo(120, 90))
    assert out.size == (108, 82)  # 120 - 2*6, 90 - 2*4 (rounded)


def test_resize_half_keeps_dimensions_but_loses_detail() -> None:
    src = _photo()
    out = bo.t_resize_half(src)
    assert out.size == src.size
    assert out.tobytes() != src.tobytes()


def test_rejpeg_changes_bytes_and_keeps_dimensions() -> None:
    src = _photo()
    out = bo.t_rejpeg_q60(src)
    assert out.size == src.size
    assert out.tobytes() != src.tobytes(), "q60 must actually re-encode"
    # and it really is a JPEG round trip, not a copy
    buf = io.BytesIO()
    src.save(buf, format="JPEG", quality=60)
    assert buf.getbuffer().nbytes > 0


def test_watermark_sits_in_the_left_and_right_bands_not_the_centre() -> None:
    """The placement is load-bearing: a CENTRED watermark would be thrown away by
    shortest-side + centre-crop preprocessing, so it would measure the preprocessing
    rather than the encoder. Portal watermarks live at the edges (§3.2)."""
    src = _photo(120, 90)
    out = bo.t_watermark_band(src)
    assert out.size == src.size
    mid_y = 45
    assert out.getpixel((12, mid_y)) != src.getpixel((12, mid_y)), "left band untouched"
    assert out.getpixel((108, mid_y)) != src.getpixel((108, mid_y)), "right band untouched"
    assert out.getpixel((60, mid_y)) == src.getpixel((60, mid_y)), "centre must be clean"
    # and the outer thirds are where the change is, not a whole-image overlay
    assert out.getpixel((60, 5)) == src.getpixel((60, 5))


def test_letterbox_pads_rather_than_crops_and_keeps_the_aspect_ratio() -> None:
    src = _photo(120, 60)
    out = bo.t_letterbox(src)
    assert out.size == (120, 120)
    assert out.getpixel((60, 2)) == (0, 0, 0), "top must be padding"
    assert out.getpixel((60, 118)) == (0, 0, 0), "bottom must be padding"
    # the original pixels survive, un-cropped, at the pasted offset
    assert out.getpixel((0, 30)) == src.getpixel((0, 0))
    assert out.getpixel((119, 89)) == src.getpixel((119, 59))


def test_composed_transform_applies_both_stages() -> None:
    src = _photo()
    out = bo.t_crop10_rejpeg_q60(src)
    assert out.size == bo.t_crop10(src).size
    assert out.tobytes() != bo.t_crop10(src).tobytes()


def test_every_recipe_name_the_manifest_can_ask_for_is_implemented() -> None:
    from scripts import dinov3_bakeoff_manifest as mf

    assert {r["name"] for r in mf.TRANSFORM_RECIPES} == set(bo.TRANSFORMS)


# ---------------------------------------------------------------------------
# Preprocessing arms
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", sorted(bo.PREPROCESSORS))
def test_every_preprocessor_yields_the_requested_square(mode: str) -> None:
    out = bo.PREPROCESSORS[mode](_photo(120, 90), 64)
    assert out.size == (64, 64) and out.mode == "RGB"


def test_centre_crop_discards_the_edges_letterbox_pad_keeps_them() -> None:
    """The whole reason preprocessing is an ARM: centre-cropping throws away the left
    and right columns of a 4:3 portal photo, which is where portal watermarks live
    (§3.2). Letterbox pad keeps the whole frame."""
    src = _photo(120, 90)
    # A band, not a hairline: a 1px marker is resampled away by any downscale, which
    # would make this test pass for the wrong reason.
    edge = (7, 7, 7)  # a colour that appears NOWHERE else in the noise pattern
    for y in range(90):
        for x in list(range(12)) + list(range(108, 120)):
            src.putpixel((x, y), edge)

    def _row(im) -> list:
        return [im.getpixel((x, 32)) for x in range(im.size[0])]

    assert edge not in _row(bo.pp_shortest_side_center_crop(src, 64)), (
        "centre-cropping must discard the outer columns — where watermarks live")
    assert edge in _row(bo.pp_letterbox_pad(src, 64)), (
        "letterbox pad must keep the whole frame, watermark band included")
    assert edge in _row(bo.pp_square_squash(src, 64)), (
        "square-squash distorts the aspect ratio but discards nothing")


# ---------------------------------------------------------------------------
# Pooling dispatch — per family, with mocked model outputs
# ---------------------------------------------------------------------------

class _FakeSeq:
    """Just enough tensor to answer `[:, 0]` with a recognisable value."""

    def __init__(self, token0: str) -> None:
        self.token0 = token0

    def __getitem__(self, key):
        assert key == (slice(None), 0), f"unexpected slice {key!r}"
        return self.token0


def _outputs() -> SimpleNamespace:
    return SimpleNamespace(pooler_output="POOLED",
                           last_hidden_state=_FakeSeq("TOP_LEFT_PATCH_OR_CLS"),
                           image_embeds="PROJECTED")


@pytest.mark.parametrize("mode,expected", [
    ("cls_post_ln", "POOLED"),            # DINOv2 / DINOv3
    ("attention_pool", "POOLED"),         # SigLIP2's learned head — NOT a CLS slice
    ("cls_pre_ln", "TOP_LEFT_PATCH_OR_CLS"),
    ("image_embeds", "PROJECTED"),        # CLIP / LAION-CLIP
])
def test_pooling_reads_the_right_field_per_family(mode: str, expected: str) -> None:
    assert bo.pool(_outputs(), mode) == expected


def test_siglip2_is_never_pooled_by_slicing_the_first_token() -> None:
    """SigLIP2 has no CLS token at all: `last_hidden_state[:, 0]` is the top-left
    image PATCH. The predecessor hardcoded exactly that slice for every model."""
    siglip = next(a for a in bo.BASE_ARMS if "siglip2" in a.name)
    assert siglip.pooling == "attention_pool"
    assert bo.pool(_outputs(), siglip.pooling) == "POOLED"


def test_pool_rejects_an_unknown_mode_and_a_missing_field() -> None:
    with pytest.raises(ValueError, match="unknown pooling mode"):
        bo.pool(_outputs(), "nope")
    with pytest.raises(ValueError, match="no usable"):
        bo.pool(SimpleNamespace(pooler_output=None), "cls_post_ln")


def test_pooling_dispatch_reads_a_dict_output_too() -> None:
    assert bo.pool({"pooler_output": "POOLED"}, "cls_post_ln") == "POOLED"


# ---------------------------------------------------------------------------
# Arms, identity, resolution snapping
# ---------------------------------------------------------------------------

def test_snap_to_patch_handles_the_dinov2_multiple_of_14_trap() -> None:
    assert bo.snap_to_patch(512, 14) == 504  # silently 504, so it is recorded as 504
    assert bo.snap_to_patch(224, 14) == 224
    assert bo.snap_to_patch(256, 16) == 256
    assert bo.snap_to_patch(10, 16) == 16


def test_identity_carries_all_six_facts() -> None:
    arm = next(a for a in bo.BASE_ARMS if a.name == "dinov3-b16")
    ident = arm.identity("deadbeef")
    for fact in ("model", "revision", "library", "pooling", "resolution",
                 "preprocessing", "dtype"):
        assert fact in ident and ident[fact] is not None
    assert ident["revision"] == "deadbeef"


def test_build_arms_varies_one_knob_at_a_time_and_dedupes() -> None:
    arms = bo.build_arms(knob_arm="dinov3-b16", resolutions=[224, 256, 512],
                         preprocess_arms=["square_squash", "letterbox_pad"],
                         precision_arms=["fp32", "bf16"])
    names = [a.name for a in arms]
    assert "dinov3-b16" in names
    assert "dinov3-b16@512" in names and "dinov3-b16+letterbox_pad" in names
    assert "dinov3-b16+bf16" in names
    # the base resolution/preprocess/dtype are not re-added as knob variants
    assert "dinov3-b16@224" not in names and "dinov3-b16+square_squash" not in names
    assert "dinov3-b16+fp32" not in names
    assert len(names) == len(set(names))


def test_build_arms_only_filter_scopes_the_run() -> None:
    arms = bo.build_arms(knob_arm="dinov3-b16", resolutions=[224], preprocess_arms=[],
                         precision_arms=[], only=["siglip2-b16-256"])
    assert [a.name for a in arms] == ["siglip2-b16-256"]


def test_the_siglip2_checkpoint_is_the_b16_arm_the_doc_names() -> None:
    # §5.3's arm list says "SigLIP2-B/16 (tag-side control)" and §2.4's load-bearing
    # Table-14 citation is the SigLIP2-B row; §2.5's so400m is a different OPTION.
    assert bo.SIGLIP2_CHECKPOINT == "google/siglip2-base-patch16-256"


# ---------------------------------------------------------------------------
# Revision resolution — mocked Hub, never a real call
# ---------------------------------------------------------------------------

class _FakeApi:
    def __init__(self, sha: str | None = "abc123", raises: Exception | None = None):
        self.sha = sha
        self.raises = raises
        self.calls: list[tuple[str, str | None]] = []

    def model_info(self, repo_id, token=None):
        self.calls.append((repo_id, token))
        if self.raises:
            raise self.raises
        return SimpleNamespace(sha=self.sha)


def test_resolve_revision_returns_the_hub_sha_and_passes_the_token() -> None:
    api = _FakeApi("f" * 40)
    assert bo.resolve_revision("facebook/x", token="tok", api=api) == "f" * 40
    assert api.calls == [("facebook/x", "tok")]


def test_resolve_revision_refuses_an_empty_sha() -> None:
    with pytest.raises(ValueError, match="no commit sha"):
        bo.resolve_revision("facebook/x", api=_FakeApi(sha=None))


def test_a_gated_repo_without_a_token_raises_so_the_arm_can_be_skipped() -> None:
    api = _FakeApi(raises=PermissionError("401 GatedRepo"))
    with pytest.raises(PermissionError):
        bo.resolve_revision("facebook/dinov3-vitb16-pretrain-lvd1689m", api=api)


# ---------------------------------------------------------------------------
# Readouts over a synthetic manifest
# ---------------------------------------------------------------------------

def _manifest() -> dict:
    return {
        "generated_at": "2026-09-05T00:00:00Z",
        "stored_clip": {"model": "openai/clip-vit-base-patch32", "provenance": []},
        "populations": {
            "P1a": {"pairs": [{"a": 1, "b": 2, "hamming": 0, "clip_cos": 0.98},
                              {"a": 3, "b": 4, "hamming": 2, "clip_cos": 0.97}]},
            "P1b": {"images": [10, 11], "transforms": ["crop10", "rejpeg_q60"]},
            "P2": {"pairs": [{"a": 5, "b": 6, "group": [1, 2], "clip_cos": 0.6}]},
            "P3": {"pairs": [{"a": 7, "b": 8, "tag_id": 22, "clip_cos": 0.30},
                             {"a": 8, "b": 9, "tag_id": 22, "clip_cos": 0.40}]},
            "P4": {"pairs": [{"a": 12, "b": 13, "tag_id": 3, "clip_cos": 0.99}],
                   "tags": [], "patterns": []},
        },
        "canary": {"image_ids": [1, 2]},
        "images": {str(i): {"phash": i, "render_score": 0.0} for i in range(1, 14)},
    }


def test_readouts_produce_the_headline_and_the_control() -> None:
    manifest = _manifest()
    synth = {"crop10": {"P1b:crop10:10": 0.99, "P1b:crop10:11": 0.98},
             "rejpeg_q60": {"P1b:rejpeg_q60:10": 0.95}}
    out = bo.readouts(manifest, manifest["images"],
                      lambda a, b, pair: pair.get("clip_cos"), synth,
                      exclusion_hamming=0, render_max=0.95, worst_n=5)
    assert set(out) == {"all", "excl_phash_only", "excl_with_clip"}
    head = out["all"]["P1b_vs_P3"]
    assert head["n_pos"] == 3 and head["n_neg"] == 2 and head["auc"] == 1.0
    assert set(out["all"]["P1b_vs_P3_by_transform"]) == {"crop10", "rejpeg_q60"}
    # readout 2: the pHash control is scored against the same negatives
    assert out["all"]["P1a_vs_P3"]["n_pos"] == 2
    # P4 is never a scored positive set
    assert out["all"]["P4_documents_inspection_only"]["n_pos"] == 0
    assert out["all"]["coverage"] == {"P1a": 2, "P1b": 3, "P2": 1, "P3": 2, "P4": 1}


def test_the_exclusion_never_touches_the_positive_population() -> None:
    """P1a is DEFINED as pHash Hamming <= 2. The contamination filter runs at >= that
    threshold, so applying it to P1a would delete every positive — §5.2's fix 1, in the
    place it would actually bite. It is a NEGATIVE-side filter and only that."""
    manifest = _manifest()
    # every image pair here is pHash-close enough for any filter to fire on
    for i in manifest["images"]:
        manifest["images"][i]["phash"] = 0
    out = bo.readouts(manifest, manifest["images"],
                      lambda a, b, pair: pair.get("clip_cos"),
                      {"crop10": {"P1b:crop10:10": 0.99}},
                      exclusion_hamming=6, render_max=0.95, worst_n=5)
    assert out["excl_phash_only"]["coverage"]["P1a"] == 2, "P1a must survive intact"
    assert out["excl_phash_only"]["coverage"]["P3"] == 0, "P3 is the filtered side"
    assert out["excl_phash_only"]["coverage"]["P2"] == 1, "P2 is an inspection set"
    assert out["excl_phash_only"]["coverage"]["P4"] == 1, "P4 is an inspection set"
    assert out["excl_phash_only"]["P1b_vs_P3"]["n_pos"] == 1


def test_the_clip_limbs_variant_drops_the_pairs_the_incumbent_calls_identical() -> None:
    manifest = _manifest()
    manifest["populations"]["P3"]["pairs"][0]["clip_cos"] = 0.9999
    out = bo.readouts(manifest, manifest["images"],
                      lambda a, b, pair: pair.get("clip_cos"), {},
                      exclusion_hamming=0, render_max=0.95, worst_n=5)
    assert out["all"]["coverage"]["P3"] == 2
    assert out["excl_phash_only"]["coverage"]["P3"] == 2, (
        "the encoder-independent limb must not consult the incumbent's cosine")
    assert out["excl_with_clip"]["coverage"]["P3"] == 1


def test_synthetic_scores_key_each_transform_separately() -> None:
    manifest = _manifest()
    table = {("10", "10|crop10"): 0.9, ("11", "11|crop10"): 0.8,
             ("10", "10|rejpeg_q60"): 0.7}
    out = bo.synthetic_scores(manifest, lambda a, b: table.get((a, b)))
    assert out["crop10"] == {"P1b:crop10:10": 0.9, "P1b:crop10:11": 0.8}
    assert out["rejpeg_q60"] == {"P1b:rejpeg_q60:10": 0.7}  # 11 had no vector


def test_score_population_skips_pairs_the_encoder_has_no_value_for() -> None:
    pairs = [{"a": 1, "b": 2, "clip_cos": 0.5}, {"a": 3, "b": 4, "clip_cos": None}]
    out = bo.score_population(pairs, {}, lambda a, b, pair: pair.get("clip_cos"),
                              name="P3")
    assert out == {"P3:1:2": 0.5}


# ---------------------------------------------------------------------------
# Precision arm and canary
# ---------------------------------------------------------------------------

def test_precision_drift_reports_movement_and_order_stability() -> None:
    fp32 = {"a": 0.90, "b": 0.80, "c": 0.70}
    bf16 = {"a": 0.9001, "b": 0.7999, "c": 0.7002}
    out = bo.precision_drift(fp32, bf16)
    assert out["n"] == 3 and out["ordering_stable"] is True
    assert out["max_abs_cosine_delta"] == pytest.approx(0.0002, abs=1e-9)


def test_precision_drift_notices_a_reordering() -> None:
    fp32 = {"a": 0.90, "b": 0.899}
    bf16 = {"a": 0.899, "b": 0.90}
    out = bo.precision_drift(fp32, bf16)
    assert out["ordering_stable"] is False and out["rank_positions_changed"] == 2


def test_precision_drift_on_disjoint_inputs_says_nothing_rather_than_zero() -> None:
    assert bo.precision_drift({"a": 1.0}, {"b": 1.0}) == {"n": 0}


def test_canary_passes_only_on_a_real_six_decimal_match() -> None:
    gated = {"1": [1.0, 0.0], "2": [0.0, 1.0]}
    assert bo.canary_verdict(gated, dict(gated))["status"] == "ok"
    off = {"1": [1.0, 0.0], "2": [0.01, 1.0]}   # cosine ~0.99995: inside 4dp, not 6
    assert bo.canary_verdict(gated, off)["status"] == "MISMATCH"
    assert bo.canary_verdict(gated, off)["min_cosine"] < 1.0


def test_canary_reports_a_pooling_mismatch_instead_of_a_false_equality() -> None:
    """timm declares global_pool=avg. If the mirror's CLS-equivalent token cannot be
    reached, the harness must SAY so — never assert equality it did not test."""
    out = bo.canary_verdict({"1": [1.0]}, {"1": [1.0]}, pooling_comparable=False)
    assert out["status"] == "pooling_mismatch"
    assert out["timm_declared_global_pool"] == "avg"
    assert "min_cosine" not in out


def test_canary_skips_cleanly_when_the_gated_weights_are_unavailable() -> None:
    out = bo.canary_verdict(None, None, reason="no HF_TOKEN")
    assert out["status"] == "skipped" and "no HF_TOKEN" in out["reason"]


def test_the_canary_names_both_the_gated_repo_and_the_ungated_mirror() -> None:
    assert bo.CANARY_GATED == "facebook/dinov3-vitb16-pretrain-lvd1689m"
    assert bo.CANARY_MIRROR == "timm/vit_base_patch16_dinov3.lvd1689m"


# ---------------------------------------------------------------------------
# End to end, with no GPU, no weights and no network
# ---------------------------------------------------------------------------

def test_main_produces_the_stored_clip_arm_with_no_weights_at_all(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zero-GPU path all the way through: read a manifest, score the incumbent
    off the cosines it already carries, skip every model arm, write JSON."""
    import json

    manifest_path = tmp_path / "m.json"
    manifest_path.write_text(json.dumps(_manifest()), encoding="utf-8")
    out_path = tmp_path / "r.json"

    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.setattr(bo, "download_images", lambda images, cache, workers: {})
    monkeypatch.setattr(bo, "build_arms", lambda **kw: [])

    assert bo.main(["--manifest", str(manifest_path), "--out", str(out_path)]) == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "dinov3-bakeoff-results/1"
    assert "No pod was launched" in payload["scope_note"]
    assert [a["name"] for a in payload["arms"]] == ["clip-stored"]
    baseline = payload["arms"][0]
    assert baseline["identity"]["model"] == "openai/clip-vit-base-patch32"
    # The stored arm has no transformed twin, so P1b is empty and P1a is the readout.
    assert baseline["readouts"]["all"]["P1a_vs_P3"]["n_pos"] == 2
    assert payload["precision_drift"]["status"] == "skipped"
    assert payload["weights_canary"]["status"] == "skipped"


# ---------------------------------------------------------------------------
# Standing rails
# ---------------------------------------------------------------------------

def test_the_harness_imports_nothing_from_this_repo() -> None:
    """It has to run on a bare pod holding this one file. A repo import would make
    the file undeployable in exactly the situation it exists for."""
    src = (bo.__file__ or "")
    text = open(src, encoding="utf-8").read()
    for banned in ("from scraper", "from toolkit", "from api ", "from location_data",
                   "import scraper", "import toolkit"):
        assert banned not in text, f"pod-side harness must not {banned!r}"


def test_the_harness_never_writes_a_database() -> None:
    text = open(bo.__file__, encoding="utf-8").read()
    for banned in ("psycopg", "INSERT", "UPDATE ", "image_dinov3_embeddings ("):
        assert banned not in text, f"the harness must never {banned!r}"


def test_the_harness_never_talks_to_runpod() -> None:
    text = open(bo.__file__, encoding="utf-8").read().lower()
    assert "runpod_client" not in text
    assert "api.runpod" not in text
