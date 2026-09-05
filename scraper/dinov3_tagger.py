"""Self-hosted DINOv3 image embedder — one 768-d unit vector per image.

The encoder the operator accepted on 2026-09-05 for the NEW DEDUP tag heads, Level-3
similarity and candidate path B (docs/design/new-dedup/ENCODER-DECISION.md §3.1),
conditional on a licence review the operator owns. Pure image -> vector (no DB, no
R2), so it is reusable and unit-testable; `scripts/dinov3_embed_backfill.py` is the
production driver.

Mirrors `scraper/clip_tagger.py`'s shape, including its refuse-to-run rail — with the
rail widened from one fact to six. `data/dinov3_config.json` ships with four of them
null (revision, resolution, preprocessing, dtype) because the bake-off has not run,
so every load raises until they are filled in. That is the design: an under-specified
encoder writes vectors that are silently incomparable with every other row in the
table, and nothing at runtime can detect it afterwards.

Three things this module is deliberately fussy about, each because getting it wrong is
SILENT (ENCODER-DECISION §6):

  * `pooler_output`, not `last_hidden_state[:, 0]`. The first is the post-LayerNorm
    CLS token — DINOv3's own retrieval protocol; the second is the raw pre-LN CLS.
    Same shape, same dtype, different population.
  * `model.eval()` before every forward. DINOv3 applies positional augmentations
    (pos_embed_shift / jitter / rescale) in TRAIN mode only, so a module left in train
    mode returns non-deterministic embeddings.
  * The geometry transform is ours, not the processor's. All three arms
    ENCODER-DECISION §3.2 names are implemented here and the config picks one; the
    processor is then asked for normalization + tensor conversion ONLY
    (`do_resize=False`, `do_center_crop=False`) so its own 224-square-squash default
    cannot quietly override the configured resolution.

transformers/torch are the optional `clip` extra — imported lazily so this module
loads without them (a --help, a config lint, or the offline test suite).
"""

from __future__ import annotations

import os
import re
from typing import Any

from scraper.dinov3_config import load_dinov3_config, validate_config

# Geometry arms of the bake-off (ENCODER-DECISION §3.2). All three are implemented
# now so the code is ready the moment one is picked — none is a default.
PREPROCESSING_MODES: tuple[str, ...] = (
    "square_squash",       # resize to res x res; aspect ratio DISTORTED (the HF default's geometry)
    "resize_center_crop",  # shortest side -> res, centre-crop; DISCARDS the left/right edges
    "letterbox_pad",       # longest side -> res, pad the rest; aspect ratio and every edge PRESERVED
)
POOLING_MODES: tuple[str, ...] = ("cls",)
LIBRARIES: tuple[str, ...] = ("transformers",)
# fp16 is OUT: a documented NaN risk on this family (ENCODER-DECISION §3.2). An
# unrecognised value raises rather than silently falling back to fp32 — a silent
# fallback would write a second, incomparable population under the configured name.
DTYPES: tuple[str, ...] = ("bf16", "fp32")

# The resample filter is itself part of the preprocessing identity. One filter across
# all three arms, so the arms differ ONLY in geometry — which is what the bake-off is
# measuring. (Four "defaults" circulate upstream: HF processor bilinear, timm bicubic,
# Meta README, paper probes. Ours is bicubic, recorded here, and changing it is a new
# population like any other identity change.)
_PAD_COLOR = (0, 0, 0)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DOC = "docs/design/new-dedup/ENCODER-DECISION.md"


def _resample():
    from PIL import Image  # base dep

    return Image.Resampling.BICUBIC


def square_squash(image, resolution: int):
    """Resize to resolution x resolution, ignoring aspect ratio. The HF image
    processor's own geometry — every pixel survives, distorted."""
    return image.resize((resolution, resolution), _resample())


def resize_center_crop(image, resolution: int):
    """Shortest side -> resolution (aspect preserved), then a centre crop.

    Note what this throws away: on a ~4:3 portal photo it discards the left and right
    bands — exactly where portal watermarks live, i.e. the evidence a near-duplicate
    check most wants (ENCODER-DECISION §3.2)."""
    width, height = image.size
    scale = resolution / min(width, height)
    scaled = (max(resolution, round(width * scale)), max(resolution, round(height * scale)))
    resized = image.resize(scaled, _resample())
    left = (scaled[0] - resolution) // 2
    top = (scaled[1] - resolution) // 2
    return resized.crop((left, top, left + resolution, top + resolution))


def letterbox_pad(image, resolution: int):
    """Longest side -> resolution (aspect preserved), padded to a square. Keeps the
    full frame including the edge bands, at the cost of spending pixels on padding."""
    from PIL import Image

    width, height = image.size
    scale = resolution / max(width, height)
    scaled = (
        max(1, min(resolution, round(width * scale))),
        max(1, min(resolution, round(height * scale))),
    )
    resized = image.resize(scaled, _resample())
    canvas = Image.new("RGB", (resolution, resolution), _PAD_COLOR)
    canvas.paste(resized, ((resolution - scaled[0]) // 2, (resolution - scaled[1]) // 2))
    return canvas


_TRANSFORMS = {
    "square_squash": square_squash,
    "resize_center_crop": resize_center_crop,
    "letterbox_pad": letterbox_pad,
}


def apply_preprocessing(image, preprocessing: str, resolution: int):
    """Config's `preprocessing` string -> the transform, as an RGB resolution-square."""
    transform = _TRANSFORMS.get(preprocessing)
    if transform is None:
        raise RuntimeError(
            f"unknown preprocessing {preprocessing!r} — expected one of "
            f"{', '.join(PREPROCESSING_MODES)}. The transform is part of the vector's "
            f"identity ({_DOC} §3.2); guessing one is not an option."
        )
    if getattr(image, "mode", None) != "RGB":
        image = image.convert("RGB")
    return transform(image, int(resolution))


def resolve_torch_dtype(dtype: str):
    """`bf16` -> torch.bfloat16, `fp32` -> None (the library default). Anything else
    raises — notably fp16, which NaNs on this family."""
    if dtype not in DTYPES:
        raise RuntimeError(
            f"unsupported dtype {dtype!r} — expected one of {', '.join(DTYPES)}. "
            f"fp16 is excluded on purpose (documented NaN risk, {_DOC} §3.2); "
            "refusing to silently fall back to another precision."
        )
    if dtype == "fp32":
        return None
    import torch

    return torch.bfloat16


def check_supported(identity: dict[str, Any]) -> None:
    """The six facts are present (validate_config) AND this module can honour them."""
    if identity["library"] not in LIBRARIES:
        raise RuntimeError(
            f"unsupported library {identity['library']!r} — this loader implements "
            f"{', '.join(LIBRARIES)}. timm's DINOv3 config declares global_pool='avg', "
            f"so a library swap is a DIFFERENT population, not a different loader ({_DOC} §6)."
        )
    if identity["pooling"] not in POOLING_MODES:
        raise RuntimeError(
            f"unsupported pooling {identity['pooling']!r} — this loader implements "
            f"{', '.join(POOLING_MODES)}."
        )
    if identity["preprocessing"] not in PREPROCESSING_MODES:
        raise RuntimeError(
            f"unknown preprocessing {identity['preprocessing']!r} — expected one of "
            f"{', '.join(PREPROCESSING_MODES)}."
        )
    resolution = identity["resolution"]
    if not isinstance(resolution, int) or isinstance(resolution, bool) or resolution <= 0:
        raise RuntimeError(f"resolution must be a positive integer, got {resolution!r}")
    resolve_torch_dtype(identity["dtype"])  # raises on fp16 / anything unknown


def _load_model_and_processor(model_id: str, revision: str, torch_dtype, threads: int = 0):
    """AutoModel + AutoImageProcessor at a PINNED revision, with the same
    retry-on-transient-hub-error loop clip_tagger.py uses (a sharded backfill's
    concurrent downloads of the weights occasionally 503 / time out).

    `AutoModel`, not `Dinov3Model`: the auto class resolves the architecture from the
    pinned config, so the loader does not carry a second, hardcoded claim about which
    architecture that sha holds — and it is one import that exists across the whole
    supported transformers range.

    HF_TOKEN comes from the environment (a GitHub Actions secret at real run time);
    `facebook/dinov3-*` is `gated: manual`, so even config.json 401s without it.
    """
    import time

    import torch
    from transformers import AutoImageProcessor, AutoModel

    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
    torch.set_num_threads(threads or (os.cpu_count() or 4))
    token = os.environ.get("HF_TOKEN")
    # `revision=` stays an EXPLICIT keyword at the call site, not a **kwargs member:
    # tests/test_clip_encoder_pin.py's AST sweep reads the call, and a pin it cannot
    # see is a pin nothing enforces. Only the optional dtype travels in the dict.
    dtype_kwargs: dict[str, Any] = {"dtype": torch_dtype} if torch_dtype is not None else {}

    model = processor = None
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            model = AutoModel.from_pretrained(
                model_id, revision=revision, token=token, **dtype_kwargs
            )
            processor = AutoImageProcessor.from_pretrained(
                model_id, revision=revision, token=token
            )
            break
        except Exception as exc:  # noqa: BLE001 - transient HF hub error -> retry
            last_exc = exc
            time.sleep(5 * (attempt + 1))
    if model is None or processor is None:
        raise RuntimeError(f"DINOv3 model load failed after retries: {last_exc}")
    return model, processor


def _loaded_revision(model, pinned: str) -> str:
    """The sha the weights actually came from, not the one we asked for.

    transformers stamps the resolved hub commit onto the config as `_commit_hash`.
    When it is present and the pin is a full sha, a mismatch means the pin did not
    take — the exact integrity failure this rail exists to prevent, so it raises
    rather than stamping a revision the vectors were not made with."""
    resolved = getattr(getattr(model, "config", None), "_commit_hash", None)
    if resolved and _SHA_RE.match(pinned) and resolved != pinned:
        raise RuntimeError(
            f"pinned revision {pinned} but the loaded checkpoint reports {resolved} — "
            "refusing to write vectors stamped with a revision they were not made with."
        )
    return resolved or pinned


class Dinov3Tagger:
    """A loaded, eval-mode DINOv3 encoder plus the six facts that identify its output."""

    def __init__(self, model, processor, identity: dict[str, Any], torch_dtype=None) -> None:
        self._model = model
        self._processor = processor
        self._torch_dtype = torch_dtype
        self.identity: dict[str, Any] = dict(identity)
        self.model_id: str = identity["model"]
        self.revision: str = identity["revision"]
        self.library: str = identity["library"]
        self.pooling: str = identity["pooling"]
        self.resolution: int = int(identity["resolution"])
        self.preprocessing: str = identity["preprocessing"]
        self.dtype: str = identity["dtype"]

    @classmethod
    def load(cls, *, config: dict[str, Any] | None = None, threads: int = 0) -> "Dinov3Tagger":
        identity = validate_config(config if config is not None else load_dinov3_config())
        check_supported(identity)
        torch_dtype = resolve_torch_dtype(identity["dtype"])
        model, processor = _load_model_and_processor(
            identity["model"], identity["revision"], torch_dtype, threads
        )
        # MANDATORY, not hygiene: DINOv3's positional augmentations run in train mode,
        # so a module left in train mode returns a different vector each forward pass
        # for the same image (ENCODER-DECISION §6).
        model.eval()
        return cls(
            model,
            processor,
            {**identity, "revision": _loaded_revision(model, identity["revision"])},
            torch_dtype=torch_dtype,
        )

    def _pool(self, outputs):
        """Pooling dispatch. One arm today; adding another is a new branch here plus a
        new value in POOLING_MODES, not a rewrite."""
        if self.pooling != "cls":
            raise RuntimeError(f"unsupported pooling {self.pooling!r}")
        pooled = getattr(outputs, "pooler_output", None)
        if pooled is None:
            raise RuntimeError(
                "model output has no pooler_output — pooling='cls' means the POST-LayerNorm "
                "CLS token, NOT last_hidden_state[:, 0] (the pre-LN raw CLS). Same shape, "
                f"different population ({_DOC} §6)."
            )
        return pooled

    def embed(self, images: list, batch_size: int = 32):
        """L2-normalized 768-d embeddings, one row per image, in input order."""
        import torch

        chunks = []
        for i in range(0, len(images), batch_size):
            batch = [
                apply_preprocessing(img, self.preprocessing, self.resolution)
                for img in images[i:i + batch_size]
            ]
            # do_resize/do_center_crop off: the geometry is already ours, and leaving
            # them on would silently re-impose the checkpoint's 224 square default over
            # the configured resolution.
            inp = self._processor(
                images=batch, return_tensors="pt", do_resize=False, do_center_crop=False
            )
            pixel_values = inp["pixel_values"]
            if self._torch_dtype is not None:
                pixel_values = pixel_values.to(self._torch_dtype)
            with torch.no_grad():
                out = self._model(pixel_values=pixel_values)
            # Cast BEFORE normalizing: under bf16 the norm itself would be computed at
            # ~3 decimal digits, so the stored unit vector would not be unit.
            feats = self._pool(out).float()
            feats = feats / feats.norm(dim=-1, keepdim=True)
            chunks.append(feats)
        return torch.cat(chunks) if chunks else None
