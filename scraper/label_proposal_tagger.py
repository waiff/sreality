"""Secondary CLIP encoder for the NEW DEDUP Labeling program (Wave 1,
docs/design/new-dedup/PROGRAM.md) — proposes Taxonomy v1 tags for operator
review on the Labeling page.

Deliberately separate from the production tagger (scraper/clip_tagger.py),
which owns the live gallery badge (`image_clip_tags` -> `images_public`).
This module never touches that table or its taxonomy file: labels here are
whatever the operator currently has active in `dedup_sim.taxonomy_labels`
(open vocabulary, growing over time), scored with a simple "a photo of
{label}" zero-shot prompt per label — there's no fine/logical collapse
layer, just one flat proposal per image.

transformers/torch are the optional `clip` extra — imported lazily so this
module loads without them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProposalResult:
    label: str
    confidence: float  # softmax probability of the winning label, 0..1


def _project(out):
    """get_text_features/get_image_features return a wrapper under some
    transformers versions; take pooler_output when so, else the tensor itself."""
    return out if hasattr(out, "shape") else out.pooler_output


class ProposalTagger:
    """Loaded secondary CLIP model + precomputed taxonomy-label text embeddings."""

    def __init__(self, model, processor, labels: list[str], text_emb, model_id: str) -> None:
        self._model = model
        self._processor = processor
        self._labels = labels
        self._text_emb = text_emb
        self.model_id = model_id

    @classmethod
    def load(cls, model_id: str, labels: list[str], threads: int = 0) -> "ProposalTagger":
        import os
        import time

        import torch
        from transformers import CLIPModel, CLIPProcessor

        if not labels:
            raise ValueError("no active taxonomy labels to propose against")
        os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
        torch.set_num_threads(threads or (os.cpu_count() or 4))
        # Mirrors clip_tagger.Tagger.load's retry: concurrent sharded jobs
        # downloading the same weights occasionally 503 / time out.
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
        prompts = [f"a photo of {label}" for label in labels]
        with torch.no_grad():
            inp = processor(text=prompts, return_tensors="pt", padding=True)
            out = model.text_model(
                input_ids=inp["input_ids"], attention_mask=inp.get("attention_mask")
            )
            text_emb = model.text_projection(_project(out))
        text_emb = text_emb / text_emb.norm(dim=-1, keepdim=True)
        return cls(model, processor, list(labels), text_emb, model_id)

    def tag(self, images: list, batch_size: int = 16) -> list[ProposalResult]:
        """Zero-shot argmax over the loaded taxonomy labels, one result per
        input image, same order."""
        import torch

        results: list[ProposalResult] = []
        scale = self._model.logit_scale.exp()
        # Softmax over exactly one label is mathematically always 1.0
        # regardless of match quality (a real, likely bootstrap-time state —
        # the operator adds Taxonomy v1 labels one at a time) — cosine
        # similarity itself is the only signal left in that case, so use it
        # directly instead of a constant, meaningless "100% confidence".
        single_label = self._text_emb.shape[0] == 1
        for i in range(0, len(images), batch_size):
            batch = images[i : i + batch_size]
            inp = self._processor(images=batch, return_tensors="pt")
            with torch.no_grad():
                out = self._model.vision_model(pixel_values=inp["pixel_values"])
                feats = self._model.visual_projection(_project(out))
            feats = feats / feats.norm(dim=-1, keepdim=True)
            sims = feats @ self._text_emb.T
            if single_label:
                conf, idx = sims.clamp(-1, 1).max(dim=-1)
            else:
                conf, idx = (scale * sims).softmax(dim=-1).max(dim=-1)
            for c, ix in zip(conf.tolist(), idx.tolist()):
                results.append(ProposalResult(self._labels[ix], round(float(c), 4)))
        return results
