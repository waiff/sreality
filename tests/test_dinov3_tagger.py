"""scraper/dinov3_tagger.py — the three preprocessing arms, the precision rail, the
mandatory eval(), the pooling dispatch and the revision pin.

Every one of these guards a SILENT failure (docs/design/new-dedup/ENCODER-DECISION.md
§6): a wrong transform, a wrong CLS, a train-mode module or an unpinned checkpoint all
produce vectors of the right shape and the right dtype that simply belong to a
different population. There is no runtime error to catch afterwards.

Hermetic: PIL only (a base dep), no torch, no transformers, no HF hub, no DB.
"""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest
from PIL import Image

from scraper import dinov3_tagger
from scraper.dinov3_tagger import (
    Dinov3Tagger,
    apply_preprocessing,
    check_supported,
    letterbox_pad,
    resize_center_crop,
    resolve_torch_dtype,
    square_squash,
)

COMPLETE = {
    "model": "facebook/dinov3-vitb16-pretrain-lvd1689m",
    "revision": "a" * 40,
    "library": "transformers",
    "pooling": "cls",
    "resolution": 224,
    "preprocessing": "letterbox_pad",
    "dtype": "fp32",
}

RES = 224
GREEN = (0, 200, 0)
RED = (200, 0, 0)


def _watermarked(width: int = 400, height: int = 200):
    """A 2:1 frame with a green band down the LEFT edge — standing in for the portal
    watermark ENCODER-DECISION §3.2 says centre-cropping throws away."""
    img = Image.new("RGB", (width, height), RED)
    for x in range(width // 10):
        for y in range(height):
            img.putpixel((x, y), GREEN)
    return img


def _is_greenish(px) -> bool:
    return px[1] > px[0] + 40


def _is_pad(px) -> bool:
    return max(px) < 40


# --- geometry: shape ---------------------------------------------------------


@pytest.mark.parametrize("transform", [square_squash, resize_center_crop, letterbox_pad])
def test_every_transform_returns_a_resolution_square(transform):
    assert transform(_watermarked(), RES).size == (RES, RES)


@pytest.mark.parametrize("size", [(400, 200), (200, 400), (300, 300), (37, 900)])
@pytest.mark.parametrize("transform", [square_squash, resize_center_crop, letterbox_pad])
def test_transforms_square_any_aspect_ratio(transform, size):
    assert transform(Image.new("RGB", size, RED), RES).size == (RES, RES)


# --- geometry: the property each arm exists for -------------------------------


def test_letterbox_pad_preserves_aspect_ratio_by_padding():
    out = letterbox_pad(_watermarked(400, 200), RES)
    # 2:1 source -> content is 224x112 centred, so the top and bottom bands are pad.
    assert _is_pad(out.getpixel((RES // 2, 4)))
    assert _is_pad(out.getpixel((RES // 2, RES - 5)))
    assert not _is_pad(out.getpixel((RES // 2, RES // 2)))


def test_square_squash_does_not_preserve_aspect_ratio():
    # The distortion IS the definition: the 2:1 frame is stretched to fill the square,
    # so no pixel anywhere is padding.
    out = square_squash(_watermarked(400, 200), RES)
    corners = [(0, 0), (RES - 1, 0), (0, RES - 1), (RES - 1, RES - 1)]
    assert not any(_is_pad(out.getpixel(c)) for c in corners)


def test_letterbox_pad_keeps_the_edge_watermark_that_center_crop_discards():
    # The single most decision-relevant difference between the two arms.
    letterboxed = letterbox_pad(_watermarked(400, 200), RES)
    assert _is_greenish(letterboxed.getpixel((4, RES // 2)))

    cropped = resize_center_crop(_watermarked(400, 200), RES)
    row = RES // 2
    assert not any(_is_greenish(cropped.getpixel((x, row))) for x in range(RES))


def test_square_squash_also_keeps_the_edge_watermark():
    out = square_squash(_watermarked(400, 200), RES)
    assert _is_greenish(out.getpixel((4, RES // 2)))


def test_resize_center_crop_preserves_aspect_ratio_before_cropping():
    # A square source is unchanged in proportion, so a centred marker stays centred.
    src = Image.new("RGB", (300, 300), RED)
    for x in range(140, 160):
        for y in range(140, 160):
            src.putpixel((x, y), GREEN)
    out = resize_center_crop(src, RES)
    assert _is_greenish(out.getpixel((RES // 2, RES // 2)))


# --- geometry: dispatch -------------------------------------------------------


@pytest.mark.parametrize("mode", ["square_squash", "resize_center_crop", "letterbox_pad"])
def test_apply_preprocessing_dispatches_every_named_mode(mode):
    assert apply_preprocessing(_watermarked(), mode, RES).size == (RES, RES)


def test_apply_preprocessing_rejects_an_unknown_mode():
    with pytest.raises(RuntimeError) as exc:
        apply_preprocessing(_watermarked(), "centre_crop_probably", RES)
    assert "unknown preprocessing" in str(exc.value)


def test_apply_preprocessing_converts_to_rgb():
    grey = Image.new("L", (400, 200), 128)
    assert apply_preprocessing(grey, "letterbox_pad", RES).mode == "RGB"


# --- precision ----------------------------------------------------------------


def test_fp32_resolves_to_the_library_default_without_importing_torch():
    assert resolve_torch_dtype("fp32") is None


@pytest.mark.parametrize("bad", ["fp16", "float16", "int8", "", "bfloat16"])
def test_unsupported_precision_raises_rather_than_falling_back(bad):
    with pytest.raises(RuntimeError) as exc:
        resolve_torch_dtype(bad)
    assert "dtype" in str(exc.value)


def test_bf16_maps_to_torch_bfloat16():
    torch = pytest.importorskip("torch")
    assert resolve_torch_dtype("bf16") is torch.bfloat16


# --- the supported-values rail -------------------------------------------------


def test_check_supported_accepts_a_complete_config():
    check_supported(COMPLETE)


@pytest.mark.parametrize(
    "override",
    [
        {"library": "timm"},          # timm's DINOv3 config declares global_pool='avg'
        {"pooling": "mean"},
        {"preprocessing": "resize"},
        {"dtype": "fp16"},            # documented NaN risk
        {"resolution": 0},
        {"resolution": -224},
        {"resolution": "224"},
        {"resolution": 224.0},
    ],
)
def test_check_supported_refuses_a_value_this_loader_cannot_honour(override):
    with pytest.raises(RuntimeError):
        check_supported({**COMPLETE, **override})


# --- load(): eval() and the revision stamp -------------------------------------


class _FakeModel:
    def __init__(self, commit_hash: str | None = None) -> None:
        self.eval_calls = 0
        self.config = types.SimpleNamespace(_commit_hash=commit_hash)

    def eval(self):
        self.eval_calls += 1
        return self


def _patch_loader(monkeypatch, model, processor=object()):
    monkeypatch.setattr(
        dinov3_tagger, "_load_model_and_processor",
        lambda *a, **k: (model, processor),
    )


def test_load_puts_the_model_in_eval_mode(monkeypatch):
    # Not hygiene: DINOv3's positional augmentations run in TRAIN mode, so a module
    # left in train mode returns a different vector per forward pass for one image.
    model = _FakeModel()
    _patch_loader(monkeypatch, model)
    tagger = Dinov3Tagger.load(config=dict(COMPLETE))
    assert model.eval_calls == 1
    assert tagger.resolution == 224
    assert tagger.preprocessing == "letterbox_pad"
    assert tagger.identity["dtype"] == "fp32"


def test_load_stamps_the_revision_the_weights_actually_came_from(monkeypatch):
    _patch_loader(monkeypatch, _FakeModel(commit_hash=COMPLETE["revision"]))
    assert Dinov3Tagger.load(config=dict(COMPLETE)).revision == COMPLETE["revision"]


def test_load_refuses_when_the_loaded_checkpoint_is_not_the_pinned_one(monkeypatch):
    _patch_loader(monkeypatch, _FakeModel(commit_hash="b" * 40))
    with pytest.raises(RuntimeError) as exc:
        Dinov3Tagger.load(config=dict(COMPLETE))
    assert "refusing to write vectors" in str(exc.value)


def test_load_falls_back_to_the_pin_when_the_config_reports_nothing(monkeypatch):
    _patch_loader(monkeypatch, _FakeModel(commit_hash=None))
    assert Dinov3Tagger.load(config=dict(COMPLETE)).revision == COMPLETE["revision"]


def test_load_refuses_a_provisional_config(monkeypatch):
    _patch_loader(monkeypatch, _FakeModel())
    with pytest.raises(RuntimeError) as exc:
        Dinov3Tagger.load(config={**COMPLETE, "revision": None})
    assert "revision" in str(exc.value)


def test_load_refuses_an_unsupported_precision_before_touching_the_hub(monkeypatch):
    def _never(*a, **k):
        raise AssertionError("the model must not be loaded for an invalid config")

    monkeypatch.setattr(dinov3_tagger, "_load_model_and_processor", _never)
    with pytest.raises(RuntimeError):
        Dinov3Tagger.load(config={**COMPLETE, "dtype": "fp16"})


# --- pooling: the post-LayerNorm CLS, not the raw one --------------------------


def _tagger(**overrides) -> Dinov3Tagger:
    return Dinov3Tagger(object(), object(), {**COMPLETE, **overrides})


def test_pooling_takes_pooler_output():
    sentinel = object()
    outputs = types.SimpleNamespace(pooler_output=sentinel, last_hidden_state="raw-cls")
    assert _tagger()._pool(outputs) is sentinel


def test_pooling_refuses_a_model_output_without_pooler_output():
    with pytest.raises(RuntimeError) as exc:
        _tagger()._pool(types.SimpleNamespace(last_hidden_state="raw-cls"))
    assert "POST-LayerNorm" in str(exc.value)


def test_pooling_refuses_an_unimplemented_mode():
    with pytest.raises(RuntimeError):
        _tagger(pooling="mean")._pool(types.SimpleNamespace(pooler_output=object()))


def test_module_never_reaches_for_the_raw_cls_token():
    # `last_hidden_state[:, 0]` is the pre-LayerNorm CLS: same shape, same dtype,
    # different population. A static guard, because the runtime difference is silent.
    # AST, not text: the docstring names the anti-pattern in order to warn about it.
    tree = ast.parse(Path(dinov3_tagger.__file__).read_text())
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "last_hidden_state"
    ]
    assert not offenders, f"raw pre-LN CLS indexed at line(s) {offenders}"


# --- from_pretrained: the pin and the gated-weights token ----------------------


def _fake_hub(monkeypatch, model):
    calls: list[tuple[str, str, dict]] = []

    class _Base:
        @classmethod
        def from_pretrained(cls, model_id, **kwargs):
            calls.append((cls.__name__, model_id, kwargs))
            return model if cls.__name__ == "AutoModel" else object()

    transformers = types.ModuleType("transformers")
    transformers.AutoModel = type("AutoModel", (_Base,), {})
    transformers.AutoImageProcessor = type("AutoImageProcessor", (_Base,), {})
    torch = types.ModuleType("torch")
    torch.set_num_threads = lambda _n: None
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setitem(sys.modules, "torch", torch)
    return calls


def test_loader_pins_the_revision_and_forwards_hf_token(monkeypatch):
    calls = _fake_hub(monkeypatch, _FakeModel())
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    dinov3_tagger._load_model_and_processor("facebook/dinov3-vitb16", "c" * 40, None)
    assert {c[0] for c in calls} == {"AutoModel", "AutoImageProcessor"}
    for _name, model_id, kwargs in calls:
        assert model_id == "facebook/dinov3-vitb16"
        assert kwargs["revision"] == "c" * 40
        # `facebook/dinov3-*` is gated: manual — even config.json 401s without this.
        assert kwargs["token"] == "hf_secret"


def test_loader_passes_the_dtype_to_the_model_only(monkeypatch):
    calls = _fake_hub(monkeypatch, _FakeModel())
    monkeypatch.delenv("HF_TOKEN", raising=False)
    sentinel = object()
    dinov3_tagger._load_model_and_processor("m", "d" * 40, sentinel)
    by_name = {name: kwargs for name, _mid, kwargs in calls}
    assert by_name["AutoModel"]["dtype"] is sentinel
    assert "dtype" not in by_name["AutoImageProcessor"]
    assert by_name["AutoModel"]["token"] is None


def test_loader_omits_the_dtype_for_fp32(monkeypatch):
    calls = _fake_hub(monkeypatch, _FakeModel())
    dinov3_tagger._load_model_and_processor("m", "e" * 40, None)
    assert all("dtype" not in kwargs for _n, _m, kwargs in calls)
