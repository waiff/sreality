"""DINOv3 bake-off — the pod-side harness for ENCODER-DECISION §5 Set 2 (job b).

Consumes the manifest built by scripts/dinov3_bakeoff_manifest.py (presigned R2 URLs,
the five §5.2 populations, stored-CLIP cosines, the synthetic-transform recipes, the
weights-canary fixture) and measures NEAR-DUPLICATE SEPARATION per encoder arm.

SELF-CONTAINED ON PURPOSE — no repo imports. It must run on a bare GPU pod holding
nothing but this one file, the manifest, and pip-installed packages:

    pip install torch transformers pillow requests huggingface_hub
    python3 dinov3_bakeoff.py --manifest dinov3_manifest.json --out results.json

SCOPE, STATED PLAINLY. Nothing launches a pod. The manifest is built by a GitHub
Actions workflow that holds the DB and R2 credentials a bare pod does not; running
THIS file against that manifest on a real GPU is a MANUAL step the operator performs
by hand, when they choose to spend the ~$2 §5.3 estimates. There is no pod-launch
automation here and there never should be in this file — a harness that can spend
money unattended is a different artifact with a different review.

IT WRITES NO DATABASE. Not `image_clip_embeddings`, not `image_dinov3_embeddings`
(that table belongs to the production embedding job). Results go to one JSON file.

A VECTOR'S IDENTITY IS SIX FACTS — model, revision (the resolved HF commit sha),
library, pooling, resolution, preprocessing, dtype — and every arm's result row
carries all of them. A revision is RESOLVED AT RUN TIME through the Hub API and
passed explicitly to both `from_pretrained` calls; an arm whose revision cannot be
resolved (gated weights, no token, licence not accepted) is SKIPPED with the reason
recorded. It is never loaded unpinned and a sha is never invented.

WHAT IT MEASURES, per arm (§5.4's readouts 1, 2, 3, 6, 7):
  * P1b (synthetic same-photo, transforms applied HERE with Pillow) against P3
    (different property, same tag) — the headline: percentiles, separation, ROC-AUC,
    recall at >=100%/99%/95% precision. Per transform and pooled.
  * P1a as the pHash control — how much of the repost population pHash already gets.
  * P2 (same listing, different photo) and P4 (documents, INSPECTION ONLY — never a
    scored positive set, because dHash collapses distinct floor plans).
  * The twenty worst P3 pairs, so mislabelled negatives can be told from real confusion.
  * The knob arms: resolution, precision drift (bf16 vs fp32), preprocessing, and the
    timm-vs-gated weights canary.
  * Measured end-to-end throughput (decode + preprocess + forward) and $/1M images.

THE EXCLUSION IS TWO INDEPENDENT KNOBS. `--p1a-hamming` (what the manifest called a
POSITIVE) and `--exclusion-hamming` (the contamination filter applied to the P3 side)
are separate parameters, because the predecessor harness defaulted them to the same
value and would therefore have produced an empty positive set by construction. The
exclusion's CLIP-DERIVED limbs (stored cosine at ceiling, render_score floor) are
reported both ON and OFF: baking the incumbent's opinion into the evaluation set is a
confound when one of the arms IS the incumbent.

The pure math below (hamming64, auc, recall_at_precision, _pctl, summarize) is lifted
close to verbatim from the deleted `scripts/embedding_gpu_bench.py` (git show
74bf82b2:scripts/embedding_gpu_bench.py); only the unit of analysis changed, from a
labelled LISTING pair reduced by max-cosine to a plain IMAGE pair.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Sequence

LOG = logging.getLogger("dinov3_bakeoff")

# Stored-CLIP cosine at/above which a pair counts as literally the same render. Kept
# from the predecessor harness, but now only ONE limb of the exclusion and switchable.
SHARED_CLIP_COS = 0.999


# ---------------------------------------------------------------------------
# Pure scoring/metric helpers — lifted from scripts/embedding_gpu_bench.py
# (74bf82b2). No torch: unit-tested offline.
# ---------------------------------------------------------------------------

def hamming64(a: int, b: int) -> int:
    """Hamming distance between two 64-bit pHashes stored as SIGNED bigints."""
    return ((a & 0xFFFFFFFFFFFFFFFF) ^ (b & 0xFFFFFFFFFFFFFFFF)).bit_count()


def auc(pos: list[float], neg: list[float]) -> float | None:
    """ROC-AUC via the rank statistic, average ranks on ties."""
    if not pos or not neg:
        return None
    ranked = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    rank_sum, i = 0.0, 0
    while i < len(ranked):
        j = i
        while j < len(ranked) and ranked[j][0] == ranked[i][0]:
            j += 1
        avg_rank = (i + j + 1) / 2.0  # 1-based average rank of the tie block
        rank_sum += avg_rank * sum(1 for k in range(i, j) if ranked[k][1] == 1)
        i = j
    n_pos, n_neg = len(pos), len(neg)
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def recall_at_precision(pos: list[float], neg: list[float],
                        floor: float) -> tuple[float, float | None]:
    """(best recall, threshold) over all thresholds where precision >= floor.
    Threshold semantics: score >= t predicts same-photo."""
    if not pos or not neg:
        return 0.0, None
    scored = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg], reverse=True)
    best_recall, best_t = 0.0, None
    tp = fp = 0
    i = 0
    while i < len(scored):
        j = i
        while j < len(scored) and scored[j][0] == scored[i][0]:
            tp += scored[j][1]
            fp += 1 - scored[j][1]
            j += 1
        precision = tp / (tp + fp)
        recall = tp / len(pos)
        if precision >= floor and recall > best_recall:
            best_recall, best_t = recall, scored[i][0]
        i = j
    return best_recall, best_t


def _pctl(vals: list[float], q: float) -> float:
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))]


def summarize(scores: dict[str, float], labels: dict[str, bool]) -> dict:
    """Percentiles, separation, AUC and recall-at-precision for one scored population.

    Unchanged from the predecessor except that a key is now an IMAGE-pair id.
    """
    pos = [v for k, v in scores.items() if labels[k]]
    neg = [v for k, v in scores.items() if not labels[k]]
    out: dict = {"n_pos": len(pos), "n_neg": len(neg)}
    if pos:
        out["pos"] = {f"p{int(q * 100)}": round(_pctl(pos, q), 4) for q in (0.10, 0.50, 0.90, 0.99)}
    if neg:
        out["neg"] = {f"p{int(q * 100)}": round(_pctl(neg, q), 4) for q in (0.10, 0.50, 0.90, 0.99)}
    if pos and neg:
        out["separation"] = round(_pctl(pos, 0.50) - _pctl(neg, 0.90), 4)
        out["auc"] = round(auc(pos, neg), 4)
        for floor in (1.0, 0.99, 0.95):
            r, t = recall_at_precision(pos, neg, floor)
            out[f"recall@p{floor}"] = {"recall": round(r, 4),
                                       "threshold": round(t, 4) if t is not None else None}
    return out


def worst_pairs(scores: dict[str, float], labels: dict[str, bool], k: int = 20
                ) -> list[dict[str, Any]]:
    """§5.4 readout 3: the k highest-scoring NEGATIVES, so mislabelled negatives can
    be told apart from an encoder genuinely confusing two flats."""
    negs = [(v, key) for key, v in scores.items() if not labels[key]]
    negs.sort(reverse=True)
    return [{"pair": key, "score": round(v, 4)} for v, key in negs[:k]]


# ---------------------------------------------------------------------------
# The contamination filter — two INDEPENDENT knobs
# ---------------------------------------------------------------------------

def is_shared_photo(
    pair: dict,
    images: dict,
    *,
    hamming_max: int,
    clip_limbs: bool,
    clip_ceiling: float = SHARED_CLIP_COS,
    render_max: float = 1.01,
) -> bool:
    """'These two are literally the same picture' — the rule that strips shared
    marketing renders out of the NEGATIVE side.

    `hamming_max` is this filter's OWN parameter and must be set independently of the
    Hamming distance that DEFINES P1a; the predecessor defaulted both to 2, which made
    the positive population empty by construction (ENCODER-DECISION §5.2 fix 1).

    `clip_limbs=False` drops the CLIP-derived limbs (stored cosine at ceiling, and the
    render_score floor), leaving a purely encoder-independent rule. Both readings are
    reported, because deciding which pairs survive with the incumbent's own opinion is
    a confound when the incumbent is one of the arms (§5.2 fix 2, §5.1 gap 5).
    """
    a = images.get(str(pair["a"])) or {}
    b = images.get(str(pair["b"])) or {}
    if clip_limbs:
        cos = pair.get("clip_cos")
        if cos is not None and cos >= clip_ceiling:
            return True
        ra, rb = a.get("render_score"), b.get("render_score")
        if (ra or 0.0) >= render_max or (rb or 0.0) >= render_max:
            return True
    pa, pb = a.get("phash"), b.get("phash")
    return pa is not None and pb is not None and hamming64(pa, pb) <= hamming_max


# ---------------------------------------------------------------------------
# P1b — the synthetic transforms, applied HERE (§5.2). Pure PIL functions.
# ---------------------------------------------------------------------------

def t_crop10(im):
    """Centre crop to 90% of each side — breaks pHash, keeps human identity."""
    w, h = im.size
    dx, dy = int(round(w * 0.05)), int(round(h * 0.05))
    return im.crop((dx, dy, w - dx, h - dy))


def t_resize_half(im):
    """Downscale to 50% and back up: resampling loss at the original dimensions."""
    from PIL import Image

    w, h = im.size
    small = im.resize((max(1, w // 2), max(1, h // 2)), Image.BICUBIC)
    return small.resize((w, h), Image.BICUBIC)


def t_rejpeg_q60(im):
    """Re-encode as JPEG quality 60. Same dimensions, different bytes."""
    from PIL import Image

    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=60)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def t_watermark_band(im):
    """Opaque bands over the LEFT and RIGHT thirds, never the centre.

    Placement is the whole point: a centred watermark is exactly what a
    shortest-side-resize + centre-crop preprocessing would throw away, so a centred
    one would measure the preprocessing rather than the encoder. Portal watermarks
    live at the edges, and §3.2 flags that centre-cropping discards them.
    """
    from PIL import ImageDraw

    out = im.convert("RGB").copy()
    w, h = out.size
    draw = ImageDraw.Draw(out)
    y0, y1 = int(h * 0.40), int(h * 0.60)
    draw.rectangle([int(w * 0.02), y0, int(w * 0.30), y1], fill=(255, 255, 255))
    draw.rectangle([int(w * 0.70), y0, int(w * 0.98), y1], fill=(255, 255, 255))
    return out


def t_letterbox(im):
    """Pad to square with black bars. Aspect ratio preserved; nothing cropped."""
    from PIL import Image

    w, h = im.size
    side = max(w, h)
    canvas = Image.new("RGB", (side, side), (0, 0, 0))
    canvas.paste(im.convert("RGB"), ((side - w) // 2, (side - h) // 2))
    return canvas


def t_crop10_rejpeg_q60(im):
    """The composed case: geometry AND compression at once."""
    return t_rejpeg_q60(t_crop10(im))


TRANSFORMS: dict[str, Callable[[Any], Any]] = {
    "crop10": t_crop10,
    "resize_half": t_resize_half,
    "rejpeg_q60": t_rejpeg_q60,
    "watermark_band": t_watermark_band,
    "letterbox": t_letterbox,
    "crop10_rejpeg_q60": t_crop10_rejpeg_q60,
}


# ---------------------------------------------------------------------------
# Preprocessing arms (§3.2: MEASURED, not locked)
# ---------------------------------------------------------------------------

def pp_square_squash(im, size: int):
    """The HF processor default: resize to size x size, ignoring aspect ratio."""
    from PIL import Image

    return im.convert("RGB").resize((size, size), Image.BICUBIC)


def pp_shortest_side_center_crop(im, size: int):
    """Resize the shortest side to `size`, then centre-crop a square.

    Discards the left and right edges of a 4:3 portal photo — where the watermarks
    live — which is precisely why P1b's watermark transform is scored against it.
    """
    from PIL import Image

    im = im.convert("RGB")
    w, h = im.size
    scale = size / min(w, h)
    im = im.resize((max(size, int(round(w * scale))), max(size, int(round(h * scale)))),
                   Image.BICUBIC)
    w, h = im.size
    left, top = (w - size) // 2, (h - size) // 2
    return im.crop((left, top, left + size, top + size))


def pp_letterbox_pad(im, size: int):
    """Fit the whole image inside size x size and pad the remainder black. Nothing
    is discarded and the aspect ratio survives; the cost is wasted pixels."""
    from PIL import Image

    im = im.convert("RGB")
    w, h = im.size
    scale = min(size / w, size / h)
    resized = im.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))),
                        Image.BICUBIC)
    canvas = Image.new("RGB", (size, size), (0, 0, 0))
    canvas.paste(resized, ((size - resized.size[0]) // 2, (size - resized.size[1]) // 2))
    return canvas


PREPROCESSORS: dict[str, Callable[[Any, int], Any]] = {
    "square_squash": pp_square_squash,
    "shortest_side_center_crop": pp_shortest_side_center_crop,
    "letterbox_pad": pp_letterbox_pad,
}


# ---------------------------------------------------------------------------
# Pooling — per encoder family, NOT one hardcoded slice
# ---------------------------------------------------------------------------

def _attr(outputs: Any, name: str) -> Any:
    value = getattr(outputs, name, None)
    if value is None and isinstance(outputs, dict):
        value = outputs.get(name)
    if value is None:
        raise ValueError(f"model output has no usable `{name}`")
    return value


# The predecessor hardcoded `last_hidden_state[:, 0]` for every model. For SigLIP2
# that slice is the top-left image PATCH — SigLIP2 has no CLS token at all, its
# summary comes from a learned attention-pooling head — and for the DINO family it is
# the PRE-LayerNorm raw CLS rather than the post-LN `pooler_output` DINOv3's own
# retrieval protocol uses. Both distinctions change the vector, so pooling is declared
# per arm and dispatched here.
POOLERS: dict[str, Callable[[Any], Any]] = {
    # DINOv2 / DINOv3: post-LayerNorm CLS.
    "cls_post_ln": lambda out: _attr(out, "pooler_output"),
    # The predecessor's slice, kept as an explicit arm rather than a default.
    "cls_pre_ln": lambda out: _attr(out, "last_hidden_state")[:, 0],
    # SigLIP2: its own learned attention-pooling head.
    "attention_pool": lambda out: _attr(out, "pooler_output"),
    # CLIP / LAION-CLIP: the projected, normalized image embedding.
    "image_embeds": lambda out: _attr(out, "image_embeds"),
}


def pool(outputs: Any, mode: str) -> Any:
    if mode not in POOLERS:
        raise ValueError(f"unknown pooling mode {mode!r}; known: {sorted(POOLERS)}")
    return POOLERS[mode](outputs)


def snap_to_patch(size: int, patch: int) -> int:
    """Largest multiple of `patch` at or below `size`.

    DINOv2-with-registers is patch 14, so a requested 512 is silently treated as 504
    — the trap §2.7 flags. Snapping makes the effective resolution a recorded fact
    instead of a surprise, and it is part of the vector's identity.
    """
    if patch <= 0:
        return size
    return max(patch, (size // patch) * patch)


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Arm:
    name: str
    repo_id: str
    model_class: str          # "AutoModel" | "CLIPModel" | "SiglipVisionModel"
    pooling: str
    resolution: int
    patch: int
    preprocess: str = "square_squash"
    dtype: str = "fp32"
    gated: bool = False
    library: str = "transformers"
    note: str = ""

    @property
    def effective_resolution(self) -> int:
        return snap_to_patch(self.resolution, self.patch)

    def identity(self, revision: str | None) -> dict[str, Any]:
        """The six facts that make this vector comparable to another one."""
        return {
            "model": self.repo_id,
            "revision": revision,
            "library": self.library,
            "pooling": self.pooling,
            "resolution": self.effective_resolution,
            "resolution_requested": self.resolution,
            "preprocessing": self.preprocess,
            "dtype": self.dtype,
        }


# SigLIP2 CHECKPOINT CHOICE, recorded where the choice is made. §5.3's arm list names
# "SigLIP2-B/16 (tag-side control)", and §2.4's load-bearing Table-14 citation is the
# SigLIP2-B row (Oxford-Hard 20.2) read token-matched against DINOv3-B. §2.5's
# so400m/16@256 is a DIFFERENT option (option C, a 427.9M vision tower), not the arm
# §5.3 asks for. `google/siglip2-base-patch16-256` is therefore the defensible pick:
# B/16 at 256, matched in patch size and resolution to the DINOv3-B/16 arm it controls.
SIGLIP2_CHECKPOINT = "google/siglip2-base-patch16-256"

BASE_ARMS: list[Arm] = [
    # CLIPVisionModelWithProjection, not CLIPModel: the projection head is the half
    # that produces `image_embeds`, and calling CLIPModel with pixel_values alone
    # would demand input_ids it has no text for.
    Arm(name="laion-clip-b32", repo_id="laion/CLIP-ViT-B-32-laion2B-s34B-b79K",
        model_class="CLIPVisionModelWithProjection", pooling="image_embeds",
        resolution=224, patch=32,
        note="the FAIR CLIP baseline (MIT, ungated) — tells 'the CLIP family fails "
             "at near-duplicate' apart from 'this 2021 checkpoint fails'"),
    Arm(name="dinov3-b16", repo_id="facebook/dinov3-vitb16-pretrain-lvd1689m",
        model_class="AutoModel", pooling="cls_post_ln", resolution=224, patch=16,
        gated=True,
        note="the recommendation. 224 because DINOv3's own retrieval numbers "
             "(Oxford-H 58.5) were produced at 224, not 512 (§3.2)"),
    Arm(name="dinov3-l16", repo_id="facebook/dinov3-vitl16-pretrain-lvd1689m",
        model_class="AutoModel", pooling="cls_post_ln", resolution=224, patch=16,
        gated=True),
    Arm(name="dinov2-l14-reg", repo_id="facebook/dinov2-with-registers-large",
        model_class="AutoModel", pooling="cls_post_ln", resolution=224, patch=14,
        note="option E, Apache-2.0 — the licence fallback. Patch 14, so every "
             "resolution snaps down to a multiple of 14"),
    Arm(name="siglip2-b16-256", repo_id=SIGLIP2_CHECKPOINT,
        model_class="SiglipVisionModel", pooling="attention_pool",
        resolution=256, patch=16,
        note="tag-side control. NO CLS token — pooling is a learned attention head"),
]

# The mirror the canary checks the gated weights against.
CANARY_GATED = "facebook/dinov3-vitb16-pretrain-lvd1689m"
CANARY_MIRROR = "timm/vit_base_patch16_dinov3.lvd1689m"


def build_arms(
    *,
    knob_arm: str,
    resolutions: Sequence[int],
    preprocess_arms: Sequence[str],
    precision_arms: Sequence[str],
    only: Sequence[str] = (),
) -> list[Arm]:
    """Base arms plus one-knob-at-a-time variants of the leading DINO arm.

    Deliberately not a cross product: §5.3 asks for resolution, precision and
    preprocessing as SEPARATE arms, and a full grid would multiply a ~$2 run by
    an order of magnitude for readouts nobody asked for.
    """
    arms: list[Arm] = [a for a in BASE_ARMS if not only or a.name in set(only)]
    base = next((a for a in BASE_ARMS if a.name == knob_arm), None)
    if base is None or (only and base.name not in set(only)):
        return arms
    for res in resolutions:
        if res != base.resolution:
            arms.append(Arm(**{**asdict(base), "name": f"{base.name}@{res}",
                               "resolution": res}))
    for pp in preprocess_arms:
        if pp != base.preprocess:
            arms.append(Arm(**{**asdict(base), "name": f"{base.name}+{pp}",
                               "preprocess": pp}))
    for dt in precision_arms:
        if dt != base.dtype:
            arms.append(Arm(**{**asdict(base), "name": f"{base.name}+{dt}", "dtype": dt}))
    seen: set[tuple] = set()
    out: list[Arm] = []
    for a in arms:
        key = (a.repo_id, a.pooling, a.effective_resolution, a.preprocess, a.dtype)
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def resolve_revision(repo_id: str, *, token: str | None = None, api: Any = None) -> str:
    """The HF commit sha for `repo_id`, resolved at run time.

    A sha is never hardcoded (nothing here can verify one) and never omitted: an arm
    whose sha will not resolve — a gated repo with no token, or a licence not yet
    accepted — is skipped by the caller rather than loaded from an unpinned `main`.
    """
    if api is None:
        from huggingface_hub import HfApi

        api = HfApi()
    sha = api.model_info(repo_id, token=token).sha
    if not sha:
        raise ValueError(f"{repo_id}: hub returned no commit sha")
    return str(sha)


# ---------------------------------------------------------------------------
# IO + torch (pod side)
# ---------------------------------------------------------------------------

def download_images(images: dict, cache_dir: str, workers: int) -> dict[int, str]:
    """{image_id: local path}; resumable (skips what is already on disk).

    Fine at 20k, structurally wrong at 10.4M — the production corpus pass must stream
    (§5.5). This is a measurement harness, and the distinction is deliberate.
    """
    import requests

    os.makedirs(cache_dir, exist_ok=True)
    session = requests.Session()

    def _one(item: tuple[str, dict]) -> tuple[int, str | None]:
        iid, meta = item
        path = os.path.join(cache_dir, f"{iid}.img")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return int(iid), path
        for _ in range(3):
            try:
                resp = session.get(meta["url"], timeout=30)
                resp.raise_for_status()
                with open(path, "wb") as fh:
                    fh.write(resp.content)
                return int(iid), path
            except Exception:  # noqa: BLE001 — retry then drop; a lost image drops out
                time.sleep(1.0)
        return int(iid), None

    out: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool_:
        for iid, path in pool_.map(_one, images.items()):
            if path is not None:
                out[iid] = path
    return out


def _load_model(arm: Arm, revision: str, device: str):
    """Model + processor, both pinned to the SAME resolved revision.

    `model.eval()` is mandatory for DINOv3: its config carries pos_embed_shift /
    jitter / rescale, positional augmentations that apply only in training mode
    (§2.4). Leaving the model in train mode would make every vector nondeterministic.
    """
    import torch
    import transformers

    dtype = torch.bfloat16 if arm.dtype == "bf16" else torch.float32
    cls = getattr(transformers, arm.model_class, None) or transformers.AutoModel
    model = cls.from_pretrained(arm.repo_id, revision=revision, torch_dtype=dtype)
    model.eval().to(device)
    # The processor's own size is overridden explicitly rather than left at the
    # checkpoint default (§5.1 gap 2: the resolution arm cannot be run otherwise).
    # This harness shapes every image to size x size ITSELF before the processor sees
    # it, so the processor's resize and centre-crop are then identities — which is
    # what makes the preprocessing arm a real arm instead of a suggestion.
    size = arm.effective_resolution
    kwargs = {"size": {"height": size, "width": size},
              "crop_size": {"height": size, "width": size}}
    try:
        proc = transformers.AutoImageProcessor.from_pretrained(
            arm.repo_id, revision=revision, **kwargs)
    except (TypeError, ValueError):
        proc = transformers.AutoImageProcessor.from_pretrained(
            arm.repo_id, revision=revision, size={"height": size, "width": size})
    return model, proc


def embed(
    arm: Arm,
    revision: str,
    items: list[tuple[str, Any]],
    *,
    device: str,
    batch_size: int,
) -> tuple[dict[str, int], Any, float]:
    """Embed `items` ((key, PIL.Image) pairs) under one arm.

    Returns ({key: row index}, an L2-normalized float32 CPU tensor, seconds elapsed).
    The timer wraps preprocessing AND the forward pass, so the throughput number is
    end-to-end rather than a synthetic-tensor GPU figure (§5.3 calls that out).
    """
    import torch

    model, proc = _load_model(arm, revision, device)
    shape = PREPROCESSORS[arm.preprocess]
    size = arm.effective_resolution
    keys: list[str] = []
    chunks: list[Any] = []
    t0 = time.monotonic()
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        shaped = [shape(im, size) for _, im in batch]
        inp = proc(images=shaped, return_tensors="pt").to(device)
        if arm.dtype == "bf16":
            inp["pixel_values"] = inp["pixel_values"].to(torch.bfloat16)
        with torch.no_grad():
            out = model(**inp)
        vec = pool(out, arm.pooling).float()
        vec = vec / vec.norm(dim=-1, keepdim=True)
        keys.extend(k for k, _ in batch)
        chunks.append(vec.cpu())
        if start % (batch_size * 20) == 0:
            LOG.info("%s: %d/%d", arm.name, len(keys), len(items))
    elapsed = time.monotonic() - t0
    emb = torch.cat(chunks) if chunks else torch.empty(0, 1)
    return {k: i for i, k in enumerate(keys)}, emb, elapsed


def cosines_for(index: dict[str, int], emb: Any,
                pairs: Sequence[tuple[str, str]]) -> dict[tuple[str, str], float]:
    """Every requested pair's cosine in one vectorized gather."""
    import torch

    usable = [(a, b) for a, b in pairs if a in index and b in index]
    if not usable:
        return {}
    ia = torch.tensor([index[a] for a, _ in usable])
    ib = torch.tensor([index[b] for _, b in usable])
    cos = (emb[ia] * emb[ib]).sum(-1)
    return {ab: float(c) for ab, c in zip(usable, cos)}


# ---------------------------------------------------------------------------
# Readouts
# ---------------------------------------------------------------------------

def precision_drift(a: dict[str, float], b: dict[str, float]) -> dict[str, Any]:
    """bf16 vs fp32 on the same pairs: how far cosines moved, and whether the ORDER
    changed — the second is what actually decides whether a threshold survives."""
    shared = sorted(set(a) & set(b))
    if not shared:
        return {"n": 0}
    deltas = [abs(a[k] - b[k]) for k in shared]
    rank_a = [k for k in sorted(shared, key=lambda k: a[k], reverse=True)]
    rank_b = [k for k in sorted(shared, key=lambda k: b[k], reverse=True)]
    changes = sum(1 for x, y in zip(rank_a, rank_b) if x != y)
    return {
        "n": len(shared),
        "max_abs_cosine_delta": round(max(deltas), 6),
        "mean_abs_cosine_delta": round(sum(deltas) / len(deltas), 6),
        "rank_positions_changed": changes,
        "ordering_stable": changes == 0,
    }


def canary_verdict(
    gated: dict[str, list[float]] | None,
    mirror: dict[str, list[float]] | None,
    *,
    decimals: int = 6,
    pooling_comparable: bool = True,
    reason: str = "",
) -> dict[str, Any]:
    """gated vs ungated-mirror weights on the fixed canary images.

    timm's config declares `global_pool: "avg"`, so a naive mirror swap pools the
    AVERAGE of the patch tokens where the gated HF checkpoint pools CLS — two
    different, incomparable populations that would look like a weights difference.
    When the mirror's CLS-equivalent token cannot be reached, this reports the
    MISMATCH instead of asserting an equality that was never tested.
    """
    if gated is None or mirror is None:
        return {"status": "skipped",
                "reason": reason or "one side unavailable (gated weights or token)"}
    if not pooling_comparable:
        return {"status": "pooling_mismatch", "reason": reason or
                "timm mirror declares global_pool=avg and no CLS-equivalent token was "
                "reachable; cosines below are NOT a weights comparison",
                "timm_declared_global_pool": "avg"}
    shared = sorted(set(gated) & set(mirror))
    tol = 10.0 ** (-decimals)
    per_image = {}
    worst = 1.0
    for key in shared:
        va, vb = gated[key], mirror[key]
        dot = sum(x * y for x, y in zip(va, vb))
        na = sum(x * x for x in va) ** 0.5 or 1.0
        nb = sum(x * x for x in vb) ** 0.5 or 1.0
        cos = dot / (na * nb)
        per_image[key] = round(cos, decimals)
        worst = min(worst, cos)
    return {
        "status": "ok" if shared and (1.0 - worst) <= tol else "MISMATCH",
        "n": len(shared),
        "min_cosine": round(worst, decimals) if shared else None,
        "matches_to_decimals": decimals,
        "per_image": per_image,
    }


def population_pairs(manifest: dict, name: str) -> list[dict]:
    return list(((manifest.get("populations") or {}).get(name) or {}).get("pairs") or [])


def _pair_key(name: str, pair: dict) -> str:
    return f"{name}:{pair['a']}:{pair['b']}"


def score_population(
    pairs: Sequence[dict],
    images: dict,
    cos: Callable[[int, int, dict], float | None],
    *,
    name: str,
    exclusion: dict[str, Any] | None = None,
) -> dict[str, float]:
    """{pair key: cosine} over the eligible image pairs of one population.

    The unit of analysis is the IMAGE PAIR. The predecessor reduced a labelled
    LISTING pair to its max same-family cosine; §5.2's populations are image pairs, so
    the reduction is gone rather than reinterpreted.
    """
    out: dict[str, float] = {}
    for pair in pairs:
        if exclusion is not None and is_shared_photo(pair, images, **exclusion):
            continue
        value = cos(int(pair["a"]), int(pair["b"]), pair)
        if value is not None:
            out[_pair_key(name, pair)] = float(value)
    return out


def synthetic_scores(
    manifest: dict,
    cos: Callable[[str, str], float | None],
) -> dict[str, dict[str, float]]:
    """P1b: {transform: {key: cosine(x, T(x))}} keyed so it can join P3's negatives."""
    p1b = (manifest.get("populations") or {}).get("P1b") or {}
    out: dict[str, dict[str, float]] = {}
    for transform in p1b.get("transforms") or []:
        scores: dict[str, float] = {}
        for iid in p1b.get("images") or []:
            value = cos(str(iid), f"{iid}|{transform}")
            if value is not None:
                scores[f"P1b:{transform}:{iid}"] = float(value)
        out[transform] = scores
    return out


def readouts(
    manifest: dict,
    images: dict,
    pair_cos: Callable[[int, int, dict], float | None],
    synth: dict[str, dict[str, float]],
    *,
    exclusion_hamming: int,
    render_max: float,
    worst_n: int,
) -> dict[str, Any]:
    """§5.4's readouts 1-3 for one arm, at three exclusion settings.

    `all` applies no contamination filter; `excl_phash_only` applies the
    encoder-independent limb; `excl_with_clip` adds the CLIP-derived limbs. Reporting
    all three is the answer to §5.1's gap 5 — with only the last one, the incumbent's
    opinion silently decides which pairs every OTHER arm is judged on.
    """
    variants = {
        "all": None,
        "excl_phash_only": {"hamming_max": exclusion_hamming, "clip_limbs": False},
        "excl_with_clip": {"hamming_max": exclusion_hamming, "clip_limbs": True,
                           "render_max": render_max},
    }
    out: dict[str, Any] = {}
    for variant, exclusion in variants.items():
        # THE EXCLUSION IS A NEGATIVE-SIDE FILTER, AND ONLY THAT. Applying it to P1a
        # would delete the positive population outright — P1a is DEFINED as pHash
        # Hamming <= 2 and the filter's own threshold is >= that — which is precisely
        # the "zero positives by construction" failure §5.2 fix 1 describes. P2 and P4
        # are inspection sets and stay whole for the same reason: a low-Hamming pair
        # inside one listing is a burst shot, and inside P4 it is two floor plans that
        # dHash collapsed, not contamination.
        p1a = score_population(population_pairs(manifest, "P1a"), images, pair_cos,
                               name="P1a")
        p2 = score_population(population_pairs(manifest, "P2"), images, pair_cos,
                              name="P2")
        p3 = score_population(population_pairs(manifest, "P3"), images, pair_cos,
                              name="P3", exclusion=exclusion)
        p4 = score_population(population_pairs(manifest, "P4"), images, pair_cos,
                              name="P4")

        block: dict[str, Any] = {}
        pooled_p1b = {k: v for scores in synth.values() for k, v in scores.items()}
        labels = {k: True for k in pooled_p1b} | {k: False for k in p3}
        # READOUT 1 — the headline. Every P1b transform against P3.
        block["P1b_vs_P3"] = summarize(pooled_p1b | p3, labels)
        block["P1b_vs_P3_by_transform"] = {
            t: summarize(scores | p3,
                         {k: True for k in scores} | {k: False for k in p3})
            for t, scores in synth.items() if scores
        }
        # READOUT 2 — P1a as the pHash control: what the cheap signal already gets.
        block["P1a_vs_P3"] = summarize(
            p1a | p3, {k: True for k in p1a} | {k: False for k in p3})
        # P2 is a hard-negative INSPECTION set (burst shots are legitimately in it),
        # and P4 is never a scored positive set — percentiles only for both.
        block["P2_hard_negatives"] = summarize(p2, {k: False for k in p2})
        block["P4_documents_inspection_only"] = summarize(p4, {k: False for k in p4})
        # READOUT 3 — the twenty worst P3 pairs.
        block["worst_P3_pairs"] = worst_pairs(
            pooled_p1b | p3, labels, k=worst_n)
        block["coverage"] = {
            "P1a": len(p1a), "P1b": len(pooled_p1b), "P2": len(p2),
            "P3": len(p3), "P4": len(p4),
        }
        out[variant] = block
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@dataclass
class ArmResult:
    name: str
    identity: dict[str, Any]
    status: str = "ok"
    skip_reason: str = ""
    note: str = ""
    throughput: dict[str, Any] = field(default_factory=dict)
    readouts: dict[str, Any] = field(default_factory=dict)


def _decode(path: str):
    from PIL import Image

    try:
        return Image.open(path).convert("RGB")
    except Exception:  # noqa: BLE001 — a corrupt download drops out of every arm
        return None


def _build_items(manifest: dict, paths: dict[int, str]) -> list[tuple[str, Any]]:
    """Every (key, image) the arms embed: each downloaded image once, plus one
    transformed copy per P1b recipe. Keys are strings so a transform variant
    ('1234|crop10') and an original ('1234') live in the same index."""
    items: list[tuple[str, Any]] = []
    p1b = set((manifest.get("populations") or {}).get("P1b", {}).get("images") or [])
    transforms = (manifest.get("populations") or {}).get("P1b", {}).get("transforms") or []
    for iid, path in sorted(paths.items()):
        im = _decode(path)
        if im is None:
            continue
        items.append((str(iid), im))
        if iid in p1b:
            for t in transforms:
                fn = TRANSFORMS.get(t)
                if fn is None:
                    LOG.warning("manifest asks for unknown transform %r — skipped", t)
                    continue
                items.append((f"{iid}|{t}", fn(im)))
    return items


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", default="dinov3_bakeoff_results.json")
    ap.add_argument("--cache-dir", default="./imgcache")
    ap.add_argument("--workers", type=int, default=32, help="Parallel image downloads.")
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--device", default=None, help="cuda|cpu (default: auto)")
    ap.add_argument("--arms", default="", help="Comma list; default = every arm.")
    ap.add_argument("--knob-arm", default="dinov3-b16",
                    help="The arm the resolution/precision/preprocessing knobs vary.")
    ap.add_argument("--resolutions", default="224,256,512")
    ap.add_argument("--preprocess-arms", default="square_squash,"
                                                 "shortest_side_center_crop,letterbox_pad")
    ap.add_argument("--precision-arms", default="fp32,bf16")
    ap.add_argument("--exclusion-hamming", type=int, default=6,
                    help="THE CONTAMINATION FILTER's pHash threshold, independent of "
                         "the manifest's P1a definition. Same value for both is the "
                         "predecessor's bug: it empties the positive set.")
    ap.add_argument("--render-max", type=float, default=0.95,
                    help="CLIP-derived render_score floor, one of the limbs reported "
                         "both on and off.")
    ap.add_argument("--worst-pairs", type=int, default=20)
    ap.add_argument("--gpu-cost-per-hr", type=float, default=0.22,
                    help="RTX 3090 list price per §5.3; used for the $/1M readout.")
    ap.add_argument("--skip-canary", action="store_true")
    ap.add_argument("--limit-images", type=int, default=0, help="Smoke test on N images.")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    with open(args.manifest, encoding="utf-8") as fh:
        manifest = json.load(fh)
    images = manifest.get("images") or {}
    if args.limit_images:
        images = dict(sorted(images.items())[: args.limit_images])
    LOG.info("manifest images=%d populations=%s", len(images),
             {k: len(v.get("pairs") or v.get("images") or [])
              for k, v in (manifest.get("populations") or {}).items()})

    paths = download_images(images, args.cache_dir, args.workers)
    LOG.info("downloaded=%d/%d", len(paths), len(images))
    items = _build_items(manifest, paths)
    LOG.info("embeddable items (originals + transforms)=%d", len(items))

    device = args.device
    if device is None:
        try:
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"
    LOG.info("device=%s", device)

    hf_token = os.environ.get("HF_TOKEN") or None
    if hf_token is None:
        LOG.warning("HF_TOKEN absent — every GATED arm and the weights canary will be "
                    "SKIPPED with a reason, never silently run unpinned")

    arms = build_arms(
        knob_arm=args.knob_arm,
        resolutions=[int(r) for r in args.resolutions.split(",") if r.strip()],
        preprocess_arms=[p.strip() for p in args.preprocess_arms.split(",") if p.strip()],
        precision_arms=[d.strip() for d in args.precision_arms.split(",") if d.strip()],
        only=[a.strip() for a in args.arms.split(",") if a.strip()],
    )
    LOG.info("arms: %s", [a.name for a in arms])

    def _pair_lookup(table: dict[tuple[str, str], float]):
        def cos(a: int, b: int, _pair: dict) -> float | None:
            return table.get((str(a), str(b)), table.get((str(b), str(a))))
        return cos

    results: list[ArmResult] = []

    # The incumbent, at zero GPU cost: cosines the manifest already carries.
    stored = manifest.get("stored_clip") or {}
    baseline = ArmResult(
        name="clip-stored",
        identity={"model": stored.get("model"), "revision": "mixed — see provenance",
                  "library": "pgvector (stored)", "pooling": "image_embeds",
                  "resolution": 224, "preprocessing": "as written by scraper/clip_tagger.py",
                  "dtype": "fp32", "provenance": stored.get("provenance")},
    )
    baseline.readouts = readouts(
        manifest, images,
        lambda a, b, pair: pair.get("clip_cos"),
        synth={},  # a stored vector has no transformed twin to compare against
        exclusion_hamming=args.exclusion_hamming, render_max=args.render_max,
        worst_n=args.worst_pairs)
    baseline.note = ("P1b is absent for the stored arm by construction: no vector "
                     "exists for a transform that was never embedded. Read this arm "
                     "through P1a_vs_P3 — it is the pHash control's own baseline.")
    results.append(baseline)

    per_arm_scores: dict[str, dict[str, float]] = {}
    for arm in arms:
        try:
            revision = resolve_revision(arm.repo_id, token=hf_token)
        except Exception as exc:  # noqa: BLE001 — gated repo, no token, or hub down
            LOG.warning("SKIP %s: cannot resolve revision (%s)", arm.name, exc)
            results.append(ArmResult(name=arm.name, identity=arm.identity(None),
                                     status="skipped",
                                     skip_reason=f"revision unresolvable: {exc}"))
            continue
        try:
            index, emb, elapsed = embed(arm, revision, items,
                                        device=device, batch_size=args.batch_size)
        except Exception as exc:  # noqa: BLE001 — one bad arm must not end the run
            LOG.warning("SKIP %s: embed failed (%s)", arm.name, exc)
            results.append(ArmResult(name=arm.name, identity=arm.identity(revision),
                                     status="failed", skip_reason=str(exc)))
            continue

        wanted: list[tuple[str, str]] = []
        for name in ("P1a", "P2", "P3", "P4"):
            wanted += [(str(p["a"]), str(p["b"])) for p in population_pairs(manifest, name)]
        p1b = (manifest.get("populations") or {}).get("P1b") or {}
        for iid in p1b.get("images") or []:
            for t in p1b.get("transforms") or []:
                wanted.append((str(iid), f"{iid}|{t}"))
        table = cosines_for(index, emb, wanted)

        synth = synthetic_scores(manifest, lambda a, b: table.get((a, b)))
        # Every cosine this arm produced, str-keyed — the precision arm compares the
        # whole surface, not just P1b, so a drift that moves only the negatives shows.
        per_arm_scores[arm.name] = {f"{a}|{b}": v for (a, b), v in table.items()}
        img_per_s = len(index) / max(elapsed, 1e-9)
        results.append(ArmResult(
            name=arm.name,
            identity=arm.identity(revision),
            throughput={
                "img_per_s": round(img_per_s, 1),
                "n_embedded": len(index),
                "seconds": round(elapsed, 1),
                "dims": int(emb.shape[1]) if len(emb.shape) > 1 else None,
                "gpu_cost_per_hr": args.gpu_cost_per_hr,
                # The predecessor's cost accounting, verbatim.
                "usd_per_1m_images": round(
                    (1e6 / max(img_per_s, 1e-9)) / 3600.0 * args.gpu_cost_per_hr, 2),
            },
            readouts=readouts(manifest, images, _pair_lookup(table), synth,
                              exclusion_hamming=args.exclusion_hamming,
                              render_max=args.render_max, worst_n=args.worst_pairs),
        ))
        LOG.info("%s: %.1f img/s, $%.2f/1M", arm.name, img_per_s,
                 results[-1].throughput["usd_per_1m_images"])

    # §5.4 readout 6: precision drift, measured between the two same-model arms.
    knob = args.knob_arm
    drift: dict[str, Any] = {"status": "not measured"}
    if knob in per_arm_scores and f"{knob}+bf16" in per_arm_scores:
        drift = precision_drift(per_arm_scores[knob], per_arm_scores[f"{knob}+bf16"])
    elif knob not in per_arm_scores:
        drift = {"status": "skipped", "reason": f"{knob} arm did not run"}

    canary: dict[str, Any] = {"status": "skipped", "reason": "--skip-canary"}
    if not args.skip_canary:
        canary = run_canary(manifest, paths, device=device, token=hf_token,
                            batch_size=args.batch_size)

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "manifest_generated_at": manifest.get("generated_at"),
        "schema": "dinov3-bakeoff-results/1",
        "scope_note": ("harness output only. No pod was launched by this file; no "
                       "vectors were written to any database."),
        "args": {"device": device, "batch_size": args.batch_size,
                 "exclusion_hamming": args.exclusion_hamming,
                 "render_max": args.render_max,
                 "gpu_cost_per_hr": args.gpu_cost_per_hr},
        "arms": [asdict(r) for r in results],
        "precision_drift": drift,
        "weights_canary": canary,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    LOG.info("wrote %s", args.out)

    for r in results:
        head = (r.readouts.get("excl_phash_only") or {}).get("P1b_vs_P3") or {}
        if "auc" in head:
            LOG.info("== %-24s P1b-vs-P3 auc=%.4f sep=%+.4f recall@p0.99=%.3f",
                     r.name, head["auc"], head["separation"],
                     head["recall@p0.99"]["recall"])
        elif r.status != "ok":
            LOG.info("== %-24s %s: %s", r.name, r.status, r.skip_reason)
    return 0


def run_canary(manifest: dict, paths: dict[int, str], *, device: str,
               token: str | None, batch_size: int) -> dict[str, Any]:
    """The gated-vs-mirror weights check on the fixed canary fixture.

    THE POOLING TRAP, handled explicitly. timm's `vit_base_patch16_dinov3.lvd1689m`
    config declares `global_pool: "avg"`, so the timm wrapper's `pooler_output` is the
    MEAN of the patch tokens — a second, incomparable population that a naive mirror
    swap would produce silently. Both sides are therefore pooled to the same thing:
    the post-final-LayerNorm CLS token, read as `pooler_output` on the HF checkpoint
    and as `last_hidden_state[:, 0]` on the timm wrapper (timm applies its final norm
    inside forward_features, so token 0 there is the post-norm CLS). If the wrapper
    exposes no token sequence to slice, the verdict is `pooling_mismatch` — the
    equality is reported as untested, never as passed.
    """
    ids = ((manifest.get("canary") or {}).get("image_ids")) or []
    items = [(str(i), _decode(paths[i])) for i in ids if i in paths]
    items = [(k, im) for k, im in items if im is not None]
    if not items:
        return {"status": "skipped", "reason": "no canary image bytes available"}

    def _vectors(repo_id: str, pooling: str) -> tuple[dict[str, list[float]] | None, str]:
        arm = Arm(name="canary", repo_id=repo_id, model_class="AutoModel",
                  pooling=pooling, resolution=224, patch=16)
        try:
            revision = resolve_revision(repo_id, token=token)
        except Exception as exc:  # noqa: BLE001 — gated, no token, or hub down
            return None, f"revision unresolvable: {exc}"
        try:
            index, emb, _ = embed(arm, revision, items, device=device,
                                  batch_size=batch_size)
        except Exception as exc:  # noqa: BLE001
            return None, f"{revision}: embed failed: {exc}"
        return {k: [float(x) for x in emb[i]] for k, i in index.items()}, revision

    gated_vecs, gated_note = _vectors(CANARY_GATED, "cls_post_ln")
    mirror_vecs, mirror_note = _vectors(CANARY_MIRROR, "cls_pre_ln")

    mirror_pooling_failed = mirror_vecs is None and "no usable" in mirror_note
    if mirror_pooling_failed:
        return canary_verdict(
            gated_vecs, gated_vecs, pooling_comparable=False,
            reason="the timm mirror exposed no token sequence to slice a CLS from; "
                   "its declared global_pool=avg pooler_output is NOT comparable to "
                   f"the gated checkpoint's CLS ({mirror_note})")
    if gated_vecs is None or mirror_vecs is None:
        return canary_verdict(None, None,
                              reason=f"gated={gated_note}; mirror={mirror_note}")
    verdict = canary_verdict(gated_vecs, mirror_vecs, pooling_comparable=True)
    verdict["gated"] = {"repo": CANARY_GATED, "revision": gated_note,
                        "pooled_as": "pooler_output (post-LayerNorm CLS)"}
    verdict["mirror"] = {"repo": CANARY_MIRROR, "revision": mirror_note,
                         "declared_global_pool": "avg",
                         "pooled_as": "last_hidden_state[:, 0] (CLS) — deliberately "
                                      "NOT the wrapper's avg pooler_output"}
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
