"""Self-hosted CLIP image tagger — zero-shot room/plot tagging for dedup.

ONE tagger shared by the trial (scripts/clip_trial.py) and the production backfill
(scripts/clip_tag_backfill.py). Loads CLIP (transformers, CPU), embeds the taxonomy
prompts once, and tags an image by argmax cosine — collapsing the fine visual
anchors to the engine's logical labels per data/clip_taxonomy.json. Pure image ->
tag (no DB, no R2), so it is reusable and unit-testable. Validated free replacement
for the paid room classifier on the coarse, dedup-relevant distinctions, and the
first tagger for the non-apartment categories (dum/pozemek/komercni).

transformers/torch are the optional `clip` extra — imported lazily so this module
loads without them (e.g. for a --help or a taxonomy lint).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

_TAXONOMY_PATH = Path(__file__).resolve().parent.parent / "data" / "clip_taxonomy.json"


@dataclass(frozen=True)
class TagResult:
    fine_tag: str       # the CLIP anchor that won (e.g. 'cadastral_map')
    logical_tag: str    # collapsed to the engine's label space (e.g. 'site_plan')
    confidence: float   # softmax probability of the winning anchor (0..1)


def load_taxonomy() -> dict:
    return json.loads(_TAXONOMY_PATH.read_text())


def _project(out):
    """get_text_features/get_image_features return a wrapper under some
    transformers versions; take pooler_output when so, else the tensor itself."""
    return out if hasattr(out, "shape") else out.pooler_output


class Tagger:
    """Loaded CLIP model + precomputed taxonomy text embeddings."""

    def __init__(self, model, processor, labels, text_emb, collapse, model_id):
        self._model = model
        self._processor = processor
        self._labels = labels
        self._text_emb = text_emb
        self._collapse = collapse
        self.model_id = model_id

    @classmethod
    def load(cls, threads: int = 0) -> "Tagger":
        import time

        import torch
        from transformers import CLIPModel, CLIPProcessor

        os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
        torch.set_num_threads(threads or (os.cpu_count() or 4))
        tax = load_taxonomy()
        model_id = tax["model"]
        # The sharded backfill runs 4 jobs in parallel; concurrent HF downloads of
        # the ~600 MB weights occasionally 503 / time out (the single-job trial
        # never did). Retry with backoff; the workflow also caches
        # ~/.cache/huggingface so every run after the first restores warm.
        model = processor = None
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                model = CLIPModel.from_pretrained(model_id)
                processor = CLIPProcessor.from_pretrained(model_id)
                break
            except Exception as exc:  # noqa: BLE001 - transient HF hub error -> retry
                last_exc = exc
                time.sleep(5 * (attempt + 1))
        if model is None or processor is None:
            raise RuntimeError(f"CLIP model load failed after retries: {last_exc}")
        model.eval()
        prompts = tax["prompts"]
        labels = list(prompts)
        with torch.no_grad():
            inp = processor(text=[prompts[k] for k in labels],
                            return_tensors="pt", padding=True)
            out = model.text_model(input_ids=inp["input_ids"],
                                   attention_mask=inp.get("attention_mask"))
            text_emb = model.text_projection(_project(out))
        text_emb = text_emb / text_emb.norm(dim=-1, keepdim=True)
        return cls(model, processor, labels, text_emb, tax.get("collapse", {}),
                   model_id)

    def embed(self, images: list, batch_size: int = 32):
        """L2-normalized image embeddings (for tagging AND the cosine tier)."""
        import torch

        chunks = []
        for i in range(0, len(images), batch_size):
            inp = self._processor(images=images[i:i + batch_size],
                                  return_tensors="pt")
            with torch.no_grad():
                out = self._model.vision_model(pixel_values=inp["pixel_values"])
                feats = self._model.visual_projection(_project(out))
            feats = feats / feats.norm(dim=-1, keepdim=True)
            chunks.append(feats)
        return torch.cat(chunks) if chunks else None

    def tags_from_emb(self, emb) -> list[TagResult]:
        """Tag precomputed embeddings — proper CLIP zero-shot (logit_scale +
        softmax), so confidence is the winning anchor's probability."""
        scale = self._model.logit_scale.exp()
        probs = (scale * emb @ self._text_emb.T).softmax(dim=-1)
        conf, idx = probs.max(dim=-1)
        out = []
        for i, c in zip(idx.tolist(), conf.tolist()):
            fine = self._labels[i]
            out.append(TagResult(fine, self._collapse.get(fine, fine),
                                 round(float(c), 4)))
        return out

    def tag(self, images: list, batch_size: int = 32) -> list[TagResult]:
        emb = self.embed(images, batch_size)
        return self.tags_from_emb(emb) if emb is not None else []
