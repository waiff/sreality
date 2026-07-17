"""GPU embedding bake-off — DINOv2 variants vs stored CLIP on the labelled dedup set.

Consumes the manifest built by scripts/build_embedding_manifest.py (presigned URLs,
labels, per-image-pair stored CLIP cosine) so it runs ANYWHERE with a GPU and no
credentials — designed for a throwaway RunPod pod. Self-contained on purpose: no
repo imports; deps are torch (preinstalled on the pod image), transformers, pillow,
requests.

What it measures, per encoder (stored CLIP baseline + each --models entry):
  - per-pair max same-family cosine → pos/neg percentiles, separation
    (pos_p50 - neg_p90), ROC-AUC, recall at >=100%/99%/95% precision
  - the same with SHARED-PHOTO image pairs excluded (phash hamming <= --hamming-max
    OR stored clip_cos >= 0.999) — encoder-independent rule, so every encoder is
    scored on the identical surviving subset. This is the June-26 A/B's known
    confound: same-development negatives sharing literal marketing renders sit at
    cosine 1.0 under ANY encoder.
  - per-category and per-family breakdowns
  - embed throughput (img/s incl. decode) and $/1M images at --gpu-cost-per-hr

The June-26 CPU A/B (scripts/embedding_ab.py, dinov2-small, byt-only) is the
baseline this harness supersedes: bigger variants, all categories, exclusion
analysis, proper threshold metrics.

Run on the pod:
  pip install transformers pillow requests
  python3 embedding_gpu_bench.py --manifest embedding_manifest.json \\
      --models facebook/dinov2-base,facebook/dinov2-large --out results.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

LOG = logging.getLogger("embedding_gpu_bench")

SHARED_CLIP_COS = 0.999  # stored-CLIP cosine at/above which an image pair counts as the same photo


# ---------------------------------------------------------------------------
# Pure scoring/metric helpers (no torch — unit-tested in tests/test_embedding_gpu_bench.py)
# ---------------------------------------------------------------------------

def hamming64(a: int, b: int) -> int:
    """Hamming distance between two 64-bit pHashes stored as SIGNED bigints."""
    return ((a & 0xFFFFFFFFFFFFFFFF) ^ (b & 0xFFFFFFFFFFFFFFFF)).bit_count()


def is_shared_photo(ip: dict, images: dict, hamming_max: int) -> bool:
    """Encoder-independent 'same literal photo' rule: near-identical pHash OR
    stored CLIP cosine at ceiling. `ip` is a manifest image_pair dict."""
    cc = ip.get("clip_cos")
    if cc is not None and cc >= SHARED_CLIP_COS:
        return True
    pa = images[str(ip["a"])].get("phash")
    pb = images[str(ip["b"])].get("phash")
    return pa is not None and pb is not None and hamming64(pa, pb) <= hamming_max


def score_pairs(
    pairs: list[dict],
    images: dict,
    cos,
    *,
    rmin: float,
    exclude_shared: bool,
    hamming_max: int,
    tag: str | None = None,
) -> dict[str, float]:
    """{pair_id: max cosine} over eligible image pairs. cos(a_id, b_id, image_pair)
    returns float | None (None = encoder has no value for this image pair). Eligible:
    both render_scores < rmin (NULL counts as photo), optionally not a shared photo,
    optionally a single family."""
    out: dict[str, float] = {}
    for p in pairs:
        best = None
        for ip in p["image_pairs"]:
            if tag is not None and ip["tag"] != tag:
                continue
            ra = images[str(ip["a"])].get("render_score")
            rb = images[str(ip["b"])].get("render_score")
            if (ra or 0.0) >= rmin or (rb or 0.0) >= rmin:
                continue
            if exclude_shared and is_shared_photo(ip, images, hamming_max):
                continue
            c = cos(ip["a"], ip["b"], ip)
            if c is not None and (best is None or c > best):
                best = c
        if best is not None:
            out[p["pair_id"]] = best
    return out


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


def recall_at_precision(pos: list[float], neg: list[float], floor: float) -> tuple[float, float | None]:
    """(best recall, threshold) over all thresholds where precision >= floor.
    Threshold semantics: score >= t predicts same-property."""
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


# ---------------------------------------------------------------------------
# IO + torch (pod side)
# ---------------------------------------------------------------------------

def download_images(images: dict, cache_dir: str, workers: int) -> dict[int, str]:
    """{image_id: local path} for every manifest image; resumable (skips existing)."""
    import requests

    os.makedirs(cache_dir, exist_ok=True)
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_maxsize=workers)
    session.mount("https://", adapter)

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
            except Exception:  # noqa: BLE001 - retry then drop; a lost image just drops out
                time.sleep(1.0)
        return int(iid), None

    out: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for iid, path in pool.map(_one, images.items()):
            if path is not None:
                out[iid] = path
    return out


def embed_images(model_id: str, paths: dict[int, str], batch_size: int,
                 device: str, fp16: bool) -> tuple[dict, float, int]:
    """Embed every image; returns ({image_id: row index}, embeddings tensor via
    closure-free tuple, img/s). CLS token, L2-normalized, float32 on CPU."""
    import torch
    from PIL import Image
    from transformers import AutoImageProcessor, AutoModel

    proc = AutoImageProcessor.from_pretrained(model_id, use_fast=True)
    dtype = torch.float16 if (fp16 and device == "cuda") else None
    model = AutoModel.from_pretrained(model_id, torch_dtype=dtype)
    model.eval().to(device)

    ids = sorted(paths)

    def _decode(iid: int):
        try:
            return Image.open(paths[iid]).convert("RGB")
        except Exception:  # noqa: BLE001 - corrupt download; the image just drops out
            return None

    chunks: list[list[int]] = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
    kept_ids: list[int] = []
    embs: list = []
    t0 = time.monotonic()
    # Per-image decode futures, one chunk ahead: the pool decodes chunk N+1 in
    # parallel while the main thread preprocesses + forwards chunk N — otherwise
    # single-threaded decode dominates and every model reports the same img/s.
    with ThreadPoolExecutor(max_workers=min(16, os.cpu_count() or 8)) as pool:
        def _submit(ci: int) -> list:
            return [(iid, pool.submit(_decode, iid)) for iid in chunks[ci]]

        pending = _submit(0) if chunks else []
        for ci in range(len(chunks)):
            current, pending = pending, (_submit(ci + 1) if ci + 1 < len(chunks) else [])
            good = [(iid, f.result()) for iid, f in current]
            good = [(iid, im) for iid, im in good if im is not None]
            if not good:
                continue
            inp = proc(images=[im for _, im in good], return_tensors="pt").to(device)
            if dtype is not None:
                inp["pixel_values"] = inp["pixel_values"].to(dtype)
            with torch.no_grad():
                res = model(**inp)
            cls = res.last_hidden_state[:, 0]
            cls = cls / cls.norm(dim=-1, keepdim=True)
            kept_ids.extend(iid for iid, _ in good)
            embs.append(cls.float().cpu())
            if ci % 20 == 0:
                LOG.info("%s: embedded %d/%d", model_id, len(kept_ids), len(ids))
    elapsed = time.monotonic() - t0
    emb = torch.cat(embs) if embs else torch.empty(0, 1)
    idx = {iid: i for i, iid in enumerate(kept_ids)}
    return {"index": idx, "emb": emb}, len(kept_ids) / max(elapsed, 1e-9), emb.shape[1]


def cosine_lookup(embedded: dict, pairs: list[dict]):
    """Precompute cosines for every manifest image pair in one vectorized gather;
    returns cos(a, b, ip) -> float | None."""
    import torch

    idx, emb = embedded["index"], embedded["emb"]
    ipairs = [(ip["a"], ip["b"]) for p in pairs for ip in p["image_pairs"]
              if ip["a"] in idx and ip["b"] in idx]
    table: dict[tuple[int, int], float] = {}
    if ipairs:
        ia = torch.tensor([idx[a] for a, _ in ipairs])
        ib = torch.tensor([idx[b] for _, b in ipairs])
        cos = (emb[ia] * emb[ib]).sum(-1)
        table = {ab: float(c) for ab, c in zip(ipairs, cos)}

    def cos_fn(a: int, b: int, ip: dict) -> float | None:
        return table.get((a, b))

    return cos_fn


def clip_lookup(a: int, b: int, ip: dict) -> float | None:
    return ip.get("clip_cos")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def analyze(pairs: list[dict], images: dict, encoders: dict, args) -> dict:
    labels = {p["pair_id"]: p["is_same"] for p in pairs}
    category = {p["pair_id"]: (p.get("category") or "unknown") for p in pairs}
    tags = sorted({ip["tag"] for p in pairs for ip in p["image_pairs"]})
    results: dict = {}
    for name, cos in encoders.items():
        enc: dict = {}
        for variant, exclude in (("all", False), ("noshared", True)):
            scores = score_pairs(pairs, images, cos, rmin=args.rmin,
                                 exclude_shared=exclude, hamming_max=args.hamming_max)
            block = summarize(scores, labels)
            block["coverage"] = round(len(scores) / max(len(pairs), 1), 4)
            block["by_category"] = {}
            for cat in sorted(set(category.values())):
                sub = {k: v for k, v in scores.items() if category[k] == cat}
                if sub:
                    block["by_category"][cat] = summarize(sub, labels)
            if exclude:
                block["by_family"] = {}
                for t in tags:
                    sub = score_pairs(pairs, images, cos, rmin=args.rmin,
                                      exclude_shared=True, hamming_max=args.hamming_max, tag=t)
                    if len(sub) >= 20:
                        block["by_family"][t] = summarize(sub, labels)
            enc[variant] = block
        results[name] = enc
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--models", default="facebook/dinov2-base,facebook/dinov2-large")
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--workers", type=int, default=32, help="Parallel image downloads.")
    ap.add_argument("--cache-dir", default="./imgcache")
    ap.add_argument("--out", default="results.json")
    ap.add_argument("--device", default=None, help="cuda|cpu (default: auto)")
    ap.add_argument("--no-fp16", action="store_true")
    ap.add_argument("--rmin", type=float, default=0.95,
                    help="Exclude images with render_score >= this (probable 3D renders).")
    ap.add_argument("--hamming-max", type=int, default=2,
                    help="pHash distance at/below which an image pair counts as the same photo.")
    ap.add_argument("--limit-pairs", type=int, default=0, help="Smoke-test on N pairs.")
    ap.add_argument("--gpu-cost-per-hr", type=float, default=0.70)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    with open(args.manifest, encoding="utf-8") as fh:
        manifest = json.load(fh)
    pairs = manifest["pairs"]
    if args.limit_pairs:
        pairs = pairs[: args.limit_pairs]
    images = manifest["images"]
    used = {str(ip[k]) for p in pairs for ip in p["image_pairs"] for k in ("a", "b")}
    images = {k: v for k, v in images.items() if k in used}
    LOG.info("pairs=%d images=%d", len(pairs), len(images))

    paths = download_images(images, args.cache_dir, args.workers)
    LOG.info("downloaded=%d/%d", len(paths), len(images))

    device = args.device
    if device is None:
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"
    LOG.info("device=%s", device)

    encoders: dict = {"clip-stored": clip_lookup}
    throughput: dict = {}
    for model_id in [m.strip() for m in args.models.split(",") if m.strip()]:
        embedded, img_per_s, dims = embed_images(
            model_id, paths, args.batch_size, device, not args.no_fp16)
        cost_per_m = (1e6 / max(img_per_s, 1e-9)) / 3600.0 * args.gpu_cost_per_hr
        throughput[model_id] = {"img_per_s": round(img_per_s, 1), "dims": dims,
                                "usd_per_1m_images": round(cost_per_m, 2)}
        LOG.info("%s: %.1f img/s, %d-d, $%.2f per 1M images",
                 model_id, img_per_s, dims, cost_per_m)
        encoders[model_id] = cosine_lookup(embedded, pairs)

    results = {
        "manifest_generated_at": manifest.get("generated_at"),
        "n_pairs": len(pairs),
        "n_images": len(images),
        "n_downloaded": len(paths),
        "args": {"rmin": args.rmin, "hamming_max": args.hamming_max,
                 "device": device, "batch_size": args.batch_size},
        "throughput": throughput,
        "encoders": analyze(pairs, images, encoders, args),
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=1)
    LOG.info("wrote %s", args.out)

    for name, enc in results["encoders"].items():
        for variant, block in enc.items():
            if "auc" in block:
                LOG.info("== %s [%s] cov=%.0f%% auc=%.4f sep=%+.4f recall@p1.0=%.3f @p0.99=%.3f",
                         name, variant, 100 * block["coverage"], block["auc"],
                         block["separation"], block["recall@p1.0"]["recall"],
                         block["recall@p0.99"]["recall"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
