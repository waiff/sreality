"""A/B a candidate image embedding (DINOv2) against the stored CLIP embeddings on the
LABELLED same-disposition dedup set — does the candidate separate same-property from
different-unit-same-development where CLIP collapsed?

The measured CLIP failure: floor-plan-confirmed DIFFERENT units (same development, shared
renders / identical developer finish) score max same-room cosine >= same-property pairs
(negatives 0.992 vs positives 0.925 at p10) — so no cosine threshold can auto-merge. CLIP
is semantic ("a kitchen in this style"); DINOv2 is instance-biased (each image is its own
class). This reports the max same-room cosine distribution by is_same for BOTH embeddings
on the SAME pairs, so the margin is directly comparable.

READ-ONLY: no merges, no writes — just embeds + cosine + percentiles. Run:
`python -m scripts.embedding_ab` (env: SUPABASE_DB_URL, R2_*, the `clip` extra). Negatives =
floor-plan `different_layout` byt pairs (the engine's real same-disposition hard negatives);
positives = `dedup_golden_pairs` same-property byt.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

LOG = logging.getLogger("embedding_ab")

# Per-pair candidate same-room image pairs: interior rooms, render-excluded (matches the
# byt merge signal), only rooms present on BOTH sides. The pair's CLIP max-same-room cosine
# is computed here in SQL from the stored embeddings; DINOv2 is computed in Python on the
# same (image_a, image_b, tag) candidate pairs.
_PAIRS_SQL = """
WITH labelled AS (
  (SELECT left_sreality_id AS la, right_sreality_id AS lb, true AS is_same
     FROM dedup_golden_pairs WHERE is_same AND category_main='byt' ORDER BY id LIMIT %(npos)s)
  UNION ALL
  (SELECT fp.sreality_id_a, fp.sreality_id_b, false
     FROM listing_floor_plan_matches fp
     JOIN listings l ON l.sreality_id=fp.sreality_id_a AND l.category_main='byt'
     WHERE fp.verdict='different_layout' LIMIT %(nneg)s)
)
SELECT g.la, g.lb, g.is_same,
       ia.id AS ia_id, ia.storage_path AS ia_key,
       ib.id AS ib_id, ib.storage_path AS ib_key,
       ta.logical_tag,
       (1 - (ea.embedding <=> eb.embedding)) AS clip_cos
FROM labelled g
-- Resolve each labelled sreality-id to its surrogate listings.id, then join images on
-- the NOT-NULL images.listing_id — sreality_id goes NULL for non-sreality portals once
-- Gate 2 flips, so joining images directly on sreality_id would drop those rows.
JOIN listings l_a ON l_a.sreality_id=g.la
JOIN listings l_b ON l_b.sreality_id=g.lb
JOIN images ia ON ia.listing_id=l_a.id AND ia.storage_path IS NOT NULL
JOIN images ib ON ib.listing_id=l_b.id AND ib.storage_path IS NOT NULL
JOIN image_clip_tags ta ON ta.image_id=ia.id
JOIN image_clip_tags tb ON tb.image_id=ib.id AND tb.logical_tag=ta.logical_tag
JOIN image_clip_embeddings ea ON ea.image_id=ia.id AND ea.model=ta.model
JOIN image_clip_embeddings eb ON eb.image_id=ib.id AND eb.model=tb.model
WHERE ta.logical_tag IN ('kitchen','bathroom','toilet','living_room','bedroom','hallway')
  AND coalesce(ta.render_score,0) < %(rmin)s AND coalesce(tb.render_score,0) < %(rmin)s
"""


def _load_dino(model_id: str):
    import torch
    from transformers import AutoImageProcessor, AutoModel

    proc = AutoImageProcessor.from_pretrained(model_id)
    model = AutoModel.from_pretrained(model_id)
    model.eval()
    torch.set_num_threads(os.cpu_count() or 4)
    return proc, model


def _embed_dino(proc, model, images: list, batch_size: int) -> dict:
    """image -> L2-normalized DINOv2 CLS embedding. Returns {pos_index: tensor}."""
    import torch

    out: list = []
    for i in range(0, len(images), batch_size):
        batch = images[i:i + batch_size]
        inp = proc(images=batch, return_tensors="pt")
        with torch.no_grad():
            res = model(**inp)
        cls = res.last_hidden_state[:, 0]  # CLS token = image descriptor
        cls = cls / cls.norm(dim=-1, keepdim=True)
        out.append(cls)
    return torch.cat(out) if out else None


def _download_decode(r2, keys: list[str], workers: int) -> dict:
    from PIL import Image

    def _one(key: str):
        try:
            data = r2.download_bytes(key)
            return key, Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:  # noqa: BLE001 - skip a bad/transient image; it just drops out
            return key, None

    out: dict = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for key, img in pool.map(_one, keys):
            if img is not None:
                out[key] = img
    return out


def _pctl(vals: list[float]) -> str:
    if not vals:
        return "n=0"
    s = sorted(vals)
    def p(q: float) -> float:
        return s[min(len(s) - 1, int(q * len(s)))]
    return (f"n={len(s)} p10={p(0.10):.4f} p50={p(0.50):.4f} "
            f"p90={p(0.90):.4f} p99={p(0.99):.4f} max={s[-1]:.4f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="facebook/dinov2-small")
    ap.add_argument("--npos", type=int, default=400)
    ap.add_argument("--nneg", type=int, default=400)
    ap.add_argument("--rmin", type=float, default=0.95, help="render_score exclusion floor")
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL not set", file=sys.stderr)
        return 2

    import psycopg

    from scraper import image_storage

    if not image_storage.is_configured():
        print("ERROR: R2 not configured (need R2_* env vars)", file=sys.stderr)
        return 2

    with psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True,
                         prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(_PAIRS_SQL, {"npos": args.npos, "nneg": args.nneg, "rmin": args.rmin})
            rows = cur.fetchall()
    LOG.info("candidate same-room image pairs=%d", len(rows))
    if not rows:
        LOG.info("no rows — nothing to compare")
        return 0

    # row layout: 0 la,1 lb,2 is_same,3 ia_id,4 ia_key,5 ib_id,6 ib_key,7 tag,8 clip_cos
    keys: dict[str, int] = {}  # storage_path -> embed index
    for r in rows:
        for k in (r[4], r[6]):
            if k not in keys:
                keys[k] = len(keys)
    key_list = list(keys.keys())
    LOG.info("unique images to embed=%d", len(key_list))

    r2 = image_storage.R2Client.from_env(max_pool_connections=args.workers + 4)
    decoded = _download_decode(r2, key_list, args.workers)
    LOG.info("downloaded+decoded=%d/%d", len(decoded), len(key_list))

    proc, model = _load_dino(args.model)
    ordered_keys = [k for k in key_list if k in decoded]
    imgs = [decoded[k] for k in ordered_keys]
    emb = _embed_dino(proc, model, imgs, args.batch_size)
    idx = {k: i for i, k in enumerate(ordered_keys)}
    LOG.info("DINOv2 embedded=%d", len(ordered_keys))

    # Per-pair: max same-room CLIP cosine (from SQL) and max same-room DINOv2 cosine.
    clip_max: dict[tuple, float] = {}
    dino_max: dict[tuple, float] = {}
    is_same: dict[tuple, bool] = {}
    for r in rows:
        pid = (int(r[0]), int(r[1]))
        is_same[pid] = bool(r[2])
        c = float(r[8])
        clip_max[pid] = max(clip_max.get(pid, -1.0), c)
        ka, kb = r[4], r[6]
        if ka in idx and kb in idx:
            d = float((emb[idx[ka]] * emb[idx[kb]]).sum())
            dino_max[pid] = max(dino_max.get(pid, -1.0), d)

    for label, store in (("CLIP (stored)", clip_max), ("DINOv2", dino_max)):
        pos = [v for k, v in store.items() if is_same.get(k)]
        neg = [v for k, v in store.items() if not is_same.get(k)]
        LOG.info("== %s ==", label)
        LOG.info("  POSITIVES (same property):   %s", _pctl(pos))
        LOG.info("  NEGATIVES (different unit):  %s", _pctl(neg))
        # Margin = how much LOWER negatives sit vs positives at the discriminating tail.
        if pos and neg:
            sp, sn = sorted(pos), sorted(neg)
            neg_p90 = sn[min(len(sn) - 1, int(0.90 * len(sn)))]
            pos_p50 = sp[min(len(sp) - 1, int(0.50 * len(sp)))]
            LOG.info("  separation: pos_p50 - neg_p90 = %.4f (want POSITIVE; CLIP ~<=0)",
                     pos_p50 - neg_p90)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
