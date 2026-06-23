"""CLIP feasibility trial — the go/no-go gate for dedup v2's visual stage.

Answers three questions on real data, in priority order, from ONE embedding pass:

  1. FEASIBILITY  — does a self-hosted CLIP run at all on a free GitHub Actions
     runner (torch-free at runtime would be the production path; this trial uses
     transformers+torch for first-run robustness — its img/s is the conservative
     FLOOR, production int8-ONNX is faster).
  2. THROUGHPUT   — measured img/s → real wall-clock to push ~1M images, the
     number that decides whether the backfill is affordable.
  3. TAG ACCURACY — CLIP zero-shot tag vs the existing Haiku
     image_room_classifications (agreement %). High agreement = CLIP can do the
     tagging for FREE, replacing the paid classifier, which is the whole point:
     accurate tags let pHash/cosine run over LIKE-FOR-LIKE rooms.

Plus, opportunistically (the "cherry on top"): COSINE DISCRIMINATION — do
same-property image pairs (cross-listing within one merged property) carry a
higher cosine than different-property pairs? That separation, if clean, is the
calibration for the v2 stage-4b CLIP band thresholds. It is best-effort: a thin
sample just reports fewer pairs, never fails the trial.

Read-only. No DB writes, no R2 writes. Usage:
    python -m scripts.clip_trial --n-labeled 1500 --n-multi 600 --out /tmp/clip_trial.json
Required: SUPABASE_DB_URL + the R2_* set.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from scraper import image_storage

LOG = logging.getLogger("clip_trial")

_TAXONOMY_PATH = Path(__file__).resolve().parent.parent / "data" / "clip_taxonomy.json"

# Labeled-image sample: images carrying a Haiku room_type (the accuracy +
# throughput set). `random() < frac` + LIMIT bounds the scan so the pooler's
# statement timeout is never at risk (no full-table sort).
_SAMPLE_LABELED_SQL = """
    SELECT i.id, i.storage_path, irc.room_type, i.sreality_id, l.property_id,
           l.category_main
    FROM image_room_classifications irc
    JOIN images i ON i.id = irc.image_id AND i.storage_path IS NOT NULL
    LEFT JOIN listings l ON l.sreality_id = i.sreality_id
    WHERE irc.room_type IS NOT NULL AND random() < %(frac)s
    LIMIT %(n)s
"""

# Multi-listing (merged, cross-portal) properties: guarantees same-property
# image pairs for the cosine-discrimination probe. room_type via LATERAL (may be
# NULL — these need no label for the cosine math).
_SAMPLE_MULTI_SQL = """
    SELECT i.id, i.storage_path, irc.room_type, i.sreality_id, l.property_id,
           l.category_main
    FROM properties p
    JOIN listings l ON l.property_id = p.id
    JOIN images i ON i.sreality_id = l.sreality_id AND i.storage_path IS NOT NULL
    LEFT JOIN LATERAL (
        SELECT room_type FROM image_room_classifications c
        WHERE c.image_id = i.id LIMIT 1
    ) irc ON TRUE
    WHERE p.source_count >= 2 AND random() < %(frac)s
    LIMIT %(n)s
"""

# Per-category sample of UNTAGGED images (no classification join) — for the
# categories that have zero Haiku labels (dum/pozemek/komercni). No ground truth,
# so the metric is CLIP's tag DISTRIBUTION (sanity) + same-property consistency,
# not vs-Haiku agreement. Active listings only (the dedup-relevant ones).
_SAMPLE_CATEGORY_SQL = """
    SELECT i.id, i.storage_path, NULL::text, i.sreality_id, l.property_id,
           l.category_main
    FROM listings l
    JOIN images i ON i.sreality_id = l.sreality_id AND i.storage_path IS NOT NULL
    WHERE l.category_main = %(cat)s AND l.is_active AND random() < %(frac)s
    LIMIT %(n)s
"""


def _sample_rows(conn: Any, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [
            {
                "image_id": r[0], "key": r[1], "haiku_room": r[2],
                "sreality_id": r[3], "property_id": r[4], "category_main": r[5],
            }
            for r in cur.fetchall()
        ]


def _download_decode(r2: image_storage.R2Client, rows: list[dict[str, Any]],
                     workers: int):
    """Fetch bytes from R2 (threaded) and decode to RGB PIL — drop failures."""
    from PIL import Image  # base dep
    import io

    def _one(row: dict[str, Any]):
        try:
            data = r2.download_bytes(row["key"])
            img = Image.open(io.BytesIO(data)).convert("RGB")
            return row, img
        except Exception as exc:  # noqa: BLE001 - one bad image must not kill the run
            return row, None

    out = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row, img in pool.map(_one, rows):
            if img is not None:
                out.append((row, img))
    return out


def _load_clip(threads: int):
    import torch  # noqa: F401 - presence check; clear error if the extra is missing
    from transformers import CLIPModel, CLIPProcessor

    torch.set_num_threads(threads)
    model_id = json.loads(_TAXONOMY_PATH.read_text())["model"]
    LOG.info("CLIP loading model=%s threads=%d", model_id, threads)
    model = CLIPModel.from_pretrained(model_id)
    model.eval()
    processor = CLIPProcessor.from_pretrained(model_id)
    return model, processor


# Explicit submodel + projection rather than get_text_features/get_image_features:
# across transformers versions those convenience methods sometimes return a raw
# BaseModelOutputWithPooling instead of the projected tensor. text_model /
# vision_model / *_projection are stable attributes and reproduce exactly what the
# convenience methods do internally (pooler_output -> projection -> shared space).
def _project(out):
    return out if hasattr(out, "shape") else out.pooler_output


def _embed_text(model, processor, prompts: dict[str, str]):
    import torch

    labels = list(prompts)
    with torch.no_grad():
        inputs = processor(text=[prompts[k] for k in labels],
                           return_tensors="pt", padding=True)
        out = model.text_model(input_ids=inputs["input_ids"],
                               attention_mask=inputs.get("attention_mask"))
        feats = model.text_projection(_project(out))
    feats = feats / feats.norm(dim=-1, keepdim=True)
    return labels, feats


def _embed_images(model, processor, images: list, batch_size: int):
    """Return (normalized_embeddings_tensor, encode_seconds)."""
    import torch

    chunks = []
    encode_s = 0.0
    for i in range(0, len(images), batch_size):
        batch = images[i:i + batch_size]
        inputs = processor(images=batch, return_tensors="pt")
        t0 = time.perf_counter()
        with torch.no_grad():
            out = model.vision_model(pixel_values=inputs["pixel_values"])
            feats = model.visual_projection(_project(out))
        encode_s += time.perf_counter() - t0
        feats = feats / feats.norm(dim=-1, keepdim=True)
        chunks.append(feats)
        LOG.info("ENCODE %d/%d", min(i + batch_size, len(images)), len(images))
    return torch.cat(chunks), encode_s


# Dedup-relevant coarse buckets: same-tag matching only needs these distinctions,
# not the fine 12-way labels. Most fine confusions (living_room<->bedroom,
# toilet<->bathroom, balcony<->facade) collapse WITHIN a bucket, so coarse
# agreement is the number that actually governs whether CLIP tags well enough.
# floor_plan and site_plan stay SEPARATE — site_plan is the development guard.
COARSE_BUCKET = {
    "kitchen": "kitchen",
    "bathroom": "sanitary", "toilet": "sanitary",
    "living_room": "living", "bedroom": "living", "hallway": "living",
    "exterior_facade": "exterior", "balcony_terrace": "exterior", "garden": "exterior",
    "floor_plan": "floor_plan", "site_plan": "site_plan",
    "other": "other",
}


def _coarse(tag: str) -> str:
    return COARSE_BUCKET.get(tag, "other")


def _tag_accuracy(labels: list[str], img_tags: list[str],
                  haiku: list[str | None]) -> dict[str, Any]:
    pairs = [(c, h) for c, h in zip(img_tags, haiku) if h]
    if not pairs:
        return {"n": 0, "note": "no Haiku-labeled images in sample"}
    agree = sum(1 for c, h in pairs if c == h)
    no_other = [(c, h) for c, h in pairs if h != "other"]
    agree_no_other = sum(1 for c, h in no_other if c == h)
    # Coarse-bucket agreement (the dedup-relevant granularity) — excl 'other'.
    coarse_agree = sum(1 for c, h in no_other if _coarse(c) == _coarse(h))
    coarse_per: dict[str, dict[str, Any]] = {}
    for b in {_coarse(h) for _, h in no_other}:
        cls = [(c, h) for c, h in no_other if _coarse(h) == b]
        coarse_per[b] = {
            "n": len(cls),
            "agreement": round(
                sum(1 for c, _ in cls if _coarse(c) == b) / len(cls), 3),
        }
    # Per-Haiku-class fine agreement + the most common confusion per class.
    per_class: dict[str, dict[str, Any]] = {}
    for h in {h for _, h in pairs}:
        cls = [(c, hh) for c, hh in pairs if hh == h]
        conf: dict[str, int] = {}
        for c, _ in cls:
            if c != h:
                conf[c] = conf.get(c, 0) + 1
        top = max(conf.items(), key=lambda kv: kv[1])[0] if conf else None
        per_class[h] = {
            "n": len(cls),
            "agreement": round(sum(1 for c, _ in cls if c == h) / len(cls), 3),
            "top_confusion": top,
        }
    return {
        "n": len(pairs),
        "agreement": round(agree / len(pairs), 3),
        "agreement_excl_other": (
            round(agree_no_other / len(no_other), 3) if no_other else None
        ),
        "coarse_agreement_excl_other": (
            round(coarse_agree / len(no_other), 3) if no_other else None
        ),
        "n_excl_other": len(no_other),
        "coarse_per_bucket": dict(sorted(coarse_per.items(),
                                         key=lambda kv: -kv[1]["n"])),
        "per_class": dict(sorted(per_class.items(),
                                 key=lambda kv: -kv[1]["n"])),
    }


def _tag_consistency(meta: list[dict[str, Any]],
                     img_tags: list[str]) -> dict[str, Any]:
    """The dedup-truest metric: across two listings of the SAME property showing
    the same Haiku room type, does CLIP assign them the SAME tag? Consistency
    (not vs-Haiku correctness) is what makes same-tag matching work."""
    by_prop: dict[int, list[int]] = {}
    for idx, m in enumerate(meta):
        if m["property_id"] is not None and m["haiku_room"] and m["haiku_room"] != "other":
            by_prop.setdefault(m["property_id"], []).append(idx)
    fine_n = fine_ok = coarse_ok = 0
    for idxs in by_prop.values():
        for a in idxs:
            for b in idxs:
                if (a < b
                        and meta[a]["sreality_id"] != meta[b]["sreality_id"]
                        and meta[a]["haiku_room"] == meta[b]["haiku_room"]):
                    fine_n += 1
                    fine_ok += img_tags[a] == img_tags[b]
                    coarse_ok += _coarse(img_tags[a]) == _coarse(img_tags[b])
    return {
        "n_same_room_cross_listing_pairs": fine_n,
        "fine_consistency": round(fine_ok / fine_n, 3) if fine_n else None,
        "coarse_consistency": round(coarse_ok / fine_n, 3) if fine_n else None,
        "note": ("CLIP assigns the same tag to genuinely-same-room cross-listing "
                 "pairs at this rate; high = same-tag matching is reliable "
                 "regardless of vs-Haiku label correctness."),
    }


def _cosine_discrimination(emb, meta: list[dict[str, Any]],
                           img_tags: list[str], max_pairs: int) -> dict[str, Any]:
    """Same-property (cross-listing) vs different-property cosine separation."""
    import torch

    by_prop: dict[int, list[int]] = {}
    for idx, m in enumerate(meta):
        pid = m["property_id"]
        if pid is not None:
            by_prop.setdefault(pid, []).append(idx)

    def _cos(a: int, b: int) -> float:
        return float(torch.dot(emb[a], emb[b]))

    # Positives: pairs of images from the SAME property but DIFFERENT listings.
    pos: list[float] = []
    pos_same_tag: list[float] = []
    for pid, idxs in by_prop.items():
        listings: dict[int, list[int]] = {}
        for i in idxs:
            listings.setdefault(meta[i]["sreality_id"], []).append(i)
        if len(listings) < 2:
            continue
        ls = list(listings.values())
        for a_group in ls:
            for b_group in ls:
                if a_group is b_group:
                    continue
                for a in a_group[:4]:
                    for b in b_group[:4]:
                        if a < b:
                            c = _cos(a, b)
                            pos.append(c)
                            if (img_tags[a] == img_tags[b]
                                    and img_tags[a] not in ("other", "floor_plan")):
                                pos_same_tag.append(c)
        if len(pos) >= max_pairs:
            break

    # Negatives: random images from DIFFERENT properties (deterministic stride,
    # no RNG — keeps the trial reproducible across resumes).
    props = list(by_prop)
    neg: list[float] = []
    if len(props) >= 2:
        for k in range(min(max_pairs, len(props) * 2)):
            pa = by_prop[props[k % len(props)]]
            pb = by_prop[props[(k * 7 + 3) % len(props)]]
            if props[k % len(props)] == props[(k * 7 + 3) % len(props)]:
                continue
            a = pa[k % len(pa)]
            b = pb[(k * 3) % len(pb)]
            neg.append(_cos(a, b))

    def _stats(xs: list[float]) -> dict[str, Any]:
        if not xs:
            return {"n": 0}
        s = sorted(xs)
        q = lambda p: round(s[min(len(s) - 1, int(p * len(s)))], 4)
        return {"n": len(xs), "median": q(0.5), "p25": q(0.25),
                "p75": q(0.75), "p95": q(0.95), "min": round(s[0], 4),
                "max": round(s[-1], 4)}

    # Rank-AUC: P(cosine(same-property) > cosine(different-property)).
    auc = None
    if pos and neg:
        merged = sorted([(c, 1) for c in pos] + [(c, 0) for c in neg])
        rank_sum = 0.0
        i = 0
        while i < len(merged):
            j = i
            while j < len(merged) and merged[j][0] == merged[i][0]:
                j += 1
            avg_rank = (i + j - 1) / 2 + 1
            for k in range(i, j):
                if merged[k][1] == 1:
                    rank_sum += avg_rank
            i = j
        auc = round((rank_sum - len(pos) * (len(pos) + 1) / 2)
                    / (len(pos) * len(neg)), 4)

    return {
        "same_property": _stats(pos),
        "same_property_same_tag": _stats(pos_same_tag),
        "different_property": _stats(neg),
        "auc_same_gt_diff": auc,
        "note": ("AUC ~0.5 = no separation (CLIP useless as a recall tier); "
                 "approaching 1.0 = clean separation (usable). Read the "
                 "same_property median vs different_property p95 gap for the "
                 "stage-4b band thresholds."),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n-labeled", type=int, default=1500)
    p.add_argument("--n-multi", type=int, default=600)
    p.add_argument("--frac", type=float, default=0.05,
                   help="Per-row random sampling fraction (bounds the DB scan).")
    p.add_argument("--batch-size", type=int, default=32)
    p.add_argument("--workers", type=int, default=16, help="Parallel R2 downloads.")
    p.add_argument("--threads", type=int, default=0,
                   help="torch CPU threads (0 = os.cpu_count()).")
    p.add_argument("--max-cosine-pairs", type=int, default=4000)
    p.add_argument("--category-sample", type=int, default=0,
                   help="Images sampled PER category in --categories (0 = off). "
                        "For the untagged dum/pozemek/komercni categories: no "
                        "ground truth, so reports CLIP's tag distribution.")
    p.add_argument("--categories", type=str, default="dum,pozemek,komercni,byt",
                   help="Comma-separated category_main values for --category-sample.")
    p.add_argument("--out", type=str, default="", help="Write JSON summary here.")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    if not image_storage.is_configured():
        print("ERROR: R2 env vars missing.", file=sys.stderr)
        return 2

    import psycopg

    threads = args.threads or (os.cpu_count() or 4)
    taxonomy = json.loads(_TAXONOMY_PATH.read_text())
    prompts = taxonomy["prompts"]
    collapse = taxonomy.get("collapse", {})

    # 1. Sample (dedupe by image id; labeled rows win so haiku_room is kept).
    cats = [c.strip() for c in args.categories.split(",") if c.strip()]
    with psycopg.connect(db_url, prepare_threshold=None) as conn:
        labeled = _sample_rows(conn, _SAMPLE_LABELED_SQL,
                               {"frac": args.frac, "n": args.n_labeled})
        multi = _sample_rows(conn, _SAMPLE_MULTI_SQL,
                             {"frac": args.frac, "n": args.n_multi})
        cat_rows: list[dict[str, Any]] = []
        if args.category_sample:
            for cat in cats:
                cr = _sample_rows(conn, _SAMPLE_CATEGORY_SQL,
                                  {"cat": cat, "frac": args.frac,
                                   "n": args.category_sample})
                LOG.info("SAMPLE category=%s n=%d", cat, len(cr))
                cat_rows += cr
    by_id: dict[int, dict[str, Any]] = {}
    for row in cat_rows + multi + labeled:  # labeled last → its haiku_room wins
        by_id[row["image_id"]] = row
    rows = list(by_id.values())
    LOG.info("SAMPLE labeled=%d multi=%d category=%d unique=%d",
             len(labeled), len(multi), len(cat_rows), len(rows))
    if not rows:
        print("ERROR: empty sample (no classified images?).", file=sys.stderr)
        return 1

    # 2. Download + decode (timed separately from encode).
    r2 = image_storage.R2Client.from_env(max_pool_connections=args.workers + 4)
    t0 = time.perf_counter()
    decoded = _download_decode(r2, rows, args.workers)
    download_s = time.perf_counter() - t0
    LOG.info("DOWNLOAD ok=%d/%d in %.1fs", len(decoded), len(rows), download_s)
    if not decoded:
        print("ERROR: no images decoded.", file=sys.stderr)
        return 1

    meta = [row for row, _ in decoded]
    images = [img for _, img in decoded]

    # 3. CLIP: text + image embeddings (image encode is the throughput metric).
    model, processor = _load_clip(threads)
    labels, text_emb = _embed_text(model, processor, prompts)
    emb, encode_s = _embed_images(model, processor, images, args.batch_size)

    # 4. Tag = argmax cosine vs the prompt matrix (fine), then collapse to the
    # engine's logical labels. Metrics use logical tags; the per-category
    # distribution keeps the fine tag so plot sub-types (cadastral/aerial) show.
    import torch
    sims = emb @ text_emb.T
    fine_tags = [labels[int(i)] for i in sims.argmax(dim=1)]
    img_tags = [collapse.get(t, t) for t in fine_tags]

    # Per-category tag distribution (the only signal for the unlabeled
    # dum/pozemek/komercni — sanity: do plot categories tag as site/plot/exterior?).
    cat_dist: dict[str, Any] = {}
    for cat in cats:
        idxs = [i for i, m in enumerate(meta) if m["category_main"] == cat]
        if not idxs:
            continue
        logical_c: dict[str, int] = {}
        fine_c: dict[str, int] = {}
        for i in idxs:
            logical_c[img_tags[i]] = logical_c.get(img_tags[i], 0) + 1
            fine_c[fine_tags[i]] = fine_c.get(fine_tags[i], 0) + 1
        cat_dist[cat] = {
            "n": len(idxs),
            "logical": dict(sorted(logical_c.items(), key=lambda kv: -kv[1])),
            "fine_plan_aerial": {k: v for k, v in sorted(fine_c.items(),
                                 key=lambda kv: -kv[1])
                                 if k in ("situation_plan", "cadastral_map",
                                          "aerial_plot", "location_map", "floor_plan")},
        }

    n = len(images)
    summary = {
        "sample": {"labeled": len(labeled), "multi": len(multi),
                   "category": len(cat_rows), "unique": len(rows), "decoded": n},
        "throughput": {
            "encode_img_per_s": round(n / encode_s, 2) if encode_s else None,
            "end_to_end_img_per_s": round(n / (download_s + encode_s), 2),
            "encode_seconds": round(encode_s, 1),
            "download_seconds": round(download_s, 1),
            "threads": threads,
            "projected_1M_encode_hours": (
                round(1_000_000 / (n / encode_s) / 3600, 1) if encode_s else None
            ),
        },
        "tag_accuracy": _tag_accuracy(labels, img_tags,
                                      [m["haiku_room"] for m in meta]),
        "tag_consistency": _tag_consistency(meta, img_tags),
        "category_tag_distribution": cat_dist,
        "cosine_discrimination": _cosine_discrimination(
            emb, meta, img_tags, args.max_cosine_pairs),
        "model": taxonomy["model"],
    }

    print("\n===== CLIP TRIAL SUMMARY =====")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        LOG.info("wrote %s", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
