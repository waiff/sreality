"""Validation harness for CLIP zero-shot RENDER (visualization) detection.

The operator's #3: can we confidently TAG an image as a 3D render / visualization vs a
real photo, so a development's shared interior RENDERS stop driving byt merges? This is
the VALIDATE-FIRST gate — it does NOT touch the engine. It embeds the target listings'
images fresh from R2 with the same CLIP model the tagger uses (so it works regardless of
the kraj-prioritized embedding coverage), scores each image render-vs-photo against text
anchors, and reports whether KNOWN renders (e.g. the "Rezidence Na Bradle" development
units) score high while KNOWN amateur photos (a control set) score low.

The "vizualizace" caption is NOT used — verified absent on the Na Bradle units, the
actual case. The signal is the IMAGE: how render-like vs photo-like it is (the operator's
"is it realistic enough to be a real photo?").

Runnable as `python -m scripts.validate_render_detection --sreality-ids a,b,c
--control-sreality-ids d,e`. Required env: SUPABASE_DB_URL + R2_* (download bytes).
Install: CPU torch + `.[clip]` (same as clip_tag).
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
from typing import Any

from scraper import image_storage

LOG = logging.getLogger("validate_render")

# Multiple anchors per class (averaged in the softmax) for robustness — a single prompt
# is brittle. render = CGI/visualization; photo = a real camera capture.
RENDER_ANCHORS = [
    "a 3D architectural rendering of an interior",
    "a computer-generated interior visualization",
    "a CGI rendered apartment room",
    "an architectural visualization, not a real photograph",
]
PHOTO_ANCHORS = [
    "a real photograph of an interior taken with a camera",
    "an amateur smartphone photo of a room",
    "a real-estate listing photograph of an apartment",
    "a real photo of a room with natural lighting and imperfections",
]


def _load_clip() -> tuple[Any, Any, Any, Any]:
    """(model, processor, _project, torch) — the same model the tagger uses."""
    import torch
    from transformers import CLIPModel, CLIPProcessor

    from scraper.clip_tagger import _project, load_taxonomy

    model_id = load_taxonomy()["model"]
    model = CLIPModel.from_pretrained(model_id)
    processor = CLIPProcessor.from_pretrained(model_id)
    model.eval()
    return model, processor, _project, torch


def _encode_text(model: Any, processor: Any, project: Any, torch: Any, texts: list[str]) -> Any:
    inp = processor(text=texts, return_tensors="pt", padding=True)
    with torch.no_grad():
        out = model.text_model(input_ids=inp["input_ids"], attention_mask=inp.get("attention_mask"))
        emb = model.text_projection(project(out))
    return emb / emb.norm(dim=-1, keepdim=True)


def _embed_images(model: Any, processor: Any, project: Any, torch: Any, images: list[Any]) -> Any:
    inp = processor(images=images, return_tensors="pt")
    with torch.no_grad():
        out = model.vision_model(pixel_values=inp["pixel_values"])
        feats = model.visual_projection(project(out))
    return feats / feats.norm(dim=-1, keepdim=True)


def _image_rows(conn: Any, sreality_ids: list[int]) -> list[tuple[int, int, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.sreality_id, i.id, i.storage_path FROM images i "
            "WHERE i.sreality_id = ANY(%s) AND i.storage_path IS NOT NULL "
            "ORDER BY i.sreality_id, i.sequence ASC NULLS LAST, i.id ASC",
            (sreality_ids,),
        )
        return [(int(r[0]), int(r[1]), r[2]) for r in cur.fetchall()]


def _score(conn: Any, r2: Any, clip: tuple, anchor_emb: Any, n_render: int,
           sreality_ids: list[int], threshold: float, label: str) -> None:
    from PIL import Image
    model, processor, project, torch = clip
    rows = _image_rows(conn, sreality_ids)
    LOG.info("=== %s: %d listings, %d images ===", label, len(sreality_ids), len(rows))
    scale = model.logit_scale.exp()
    per_listing: dict[int, list[float]] = {}
    for sid, image_id, key in rows:
        try:
            data = r2.download_bytes(key)
            img = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("skip image_id=%d (%s): %s", image_id, key, exc)
            continue
        emb = _embed_images(model, processor, project, torch, [img])
        probs = (scale * emb @ anchor_emb.T).softmax(dim=-1)
        render_score = float(probs[0, :n_render].sum().item())
        per_listing.setdefault(sid, []).append(render_score)
        flag = "RENDER" if render_score >= threshold else "photo "
        LOG.info("  %s sid=%d image=%d render_score=%.3f", flag, sid, image_id, render_score)
    LOG.info("--- %s per-listing summary (threshold=%.2f) ---", label, threshold)
    for sid in sreality_ids:
        scores = per_listing.get(sid, [])
        if not scores:
            LOG.info("  sid=%d: no images scored", sid)
            continue
        n_hi = sum(1 for s in scores if s >= threshold)
        LOG.info("  sid=%d: n=%d render-flagged=%d max=%.3f mean=%.3f",
                 sid, len(scores), n_hi, max(scores), sum(scores) / len(scores))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sreality-ids", required=True,
                   help="Comma-separated target listing ids (suspected renders).")
    p.add_argument("--control-sreality-ids", default="",
                   help="Comma-separated control listing ids (known amateur photos).")
    p.add_argument("--threshold", type=float, default=0.5,
                   help="render_score >= this is flagged RENDER (default 0.5).")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    if not image_storage.is_configured():
        print("ERROR: R2 is not configured (need image bytes).", file=sys.stderr)
        return 2

    targets = [int(x) for x in args.sreality_ids.split(",") if x.strip()]
    controls = [int(x) for x in args.control_sreality_ids.split(",") if x.strip()]

    import psycopg

    clip = _load_clip()
    model, processor, project, torch = clip
    anchors = RENDER_ANCHORS + PHOTO_ANCHORS
    anchor_emb = _encode_text(model, processor, project, torch, anchors)
    n_render = len(RENDER_ANCHORS)
    LOG.info("RENDER-DETECT anchors render=%d photo=%d threshold=%.2f",
             n_render, len(PHOTO_ANCHORS), args.threshold)

    r2 = image_storage.R2Client.from_env()
    with psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True,
                         prepare_threshold=None) as conn:
        _score(conn, r2, clip, anchor_emb, n_render, targets, args.threshold, "TARGET (suspected renders)")
        if controls:
            _score(conn, r2, clip, anchor_emb, n_render, controls, args.threshold, "CONTROL (real photos)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
