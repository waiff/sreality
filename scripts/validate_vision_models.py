"""A/B a candidate (model, max_edge) for the dedup vision tools against the
historical ground truth, so a model or resolution change can't silently lose
auto-merge recall.

Two checks, both READ-ONLY (re-runs the LLM but writes no cache / no app_settings;
the only writes are the standard llm_calls audit rows):

  1. COMPARE recall — every historical 'High' verdict in listing_visual_matches drove
     (or could drive) an auto-merge. Re-run each with the candidate (model, max_edge)
     and require it stays 'High'. A drop here = lost recall on real duplicates.
  2. CLASSIFY agreement — re-label a sample of already-classified listings and measure
     per-image room-label agreement. Classify never merges on its own, so this is a
     softer gate (mislabels only mis-pair like rooms / the pHash interior gate).

Exits non-zero when a threshold is missed, so it gates the Haiku+768 model flip.
Run via .github/workflows/validate_vision_models.yml (needs ANTHROPIC_API_KEY, R2_*,
SUPABASE_DB_URL).

  python -m scripts.validate_vision_models \
      --candidate-model claude-haiku-4-5 --max-edge 768
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from scraper import db, image_storage
from toolkit import image_classification as ic
from toolkit import visual_match as vm
from toolkit.vision_images import COMPARISON_MAX_EDGE, image_block

LOG = logging.getLogger("validate_vision_models")


def _room_images(
    conn: Any, sreality_id: int, room_type: str, prod_model: str, limit: int,
) -> list[str]:
    """Storage paths of one listing's images classified as room_type (prod model)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.storage_path FROM images i "
            "JOIN image_room_classifications c ON c.image_id = i.id "
            "WHERE i.sreality_id = %s AND i.storage_path IS NOT NULL "
            "  AND c.model = %s AND c.room_type = %s "
            "ORDER BY i.sequence ASC NULLS LAST, i.id ASC LIMIT %s",
            (sreality_id, prod_model, room_type, limit),
        )
        return [r[0] for r in cur.fetchall()]


def _listing_images(conn: Any, sreality_id: int, limit: int) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, storage_path FROM images "
            "WHERE sreality_id = %s AND storage_path IS NOT NULL "
            "ORDER BY sequence ASC NULLS LAST, id ASC LIMIT %s",
            (sreality_id, limit),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def _prod_labels(conn: Any, image_ids: list[int], prod_model: str) -> dict[int, str]:
    if not image_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT image_id, room_type FROM image_room_classifications "
            "WHERE model = %s AND image_id = ANY(%s)",
            (prod_model, image_ids),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def run_compare_ab(
    conn: Any, llm: Any, r2: Any, *,
    candidate_model: str, max_edge: int, prod_model: str, limit: int,
) -> tuple[int, int, list[str]]:
    """Re-run historical High verdicts; return (still_high, evaluated, misses)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT sreality_id_a, sreality_id_b, room_type "
            "FROM listing_visual_matches WHERE verdict = 'High' AND model = %s "
            "ORDER BY 1, 2, 3 LIMIT %s",
            (prod_model, limit),
        )
        rows = cur.fetchall()

    still_high = 0
    evaluated = 0
    misses: list[str] = []
    for a, b, room in rows:
        paths_a = _room_images(conn, a, room, prod_model, 4)
        paths_b = _room_images(conn, b, room, prod_model, 4)
        if not paths_a or not paths_b:
            LOG.info("compare skip a=%s b=%s room=%s (room images gone)", a, b, room)
            continue
        content: list[dict[str, Any]] = [
            {"type": "text", "text": f"Listing A — {room} ({len(paths_a)} image(s)):"}
        ]
        content.extend(image_block(r2, p, max_edge) for p in paths_a)
        content.append({"type": "text", "text": f"Listing B — {room} ({len(paths_b)} image(s)):"})
        content.extend(image_block(r2, p, max_edge) for p in paths_b)
        content.append({
            "type": "text",
            "text": (
                "Both sets show the same room type. Decide whether they depict the "
                "same physical property, then call record_visual_match once."
            ),
        })
        try:
            resp = llm.call(
                called_for="compare_listings_visually",
                messages=[{"role": "user", "content": content}],
                system=llm.resolve_system_prompt(vm._PROMPT_KEY),
                tools=[vm.RECORD_VISUAL_MATCH_TOOL],
                model=candidate_model,
            )
            verdict, _ = vm._extract(resp.tool_calls)
        except Exception as exc:  # noqa: BLE001 - one bad pair must not abort the gate
            LOG.warning("compare a=%s b=%s room=%s failed: %s", a, b, room, exc)
            misses.append(f"{a}/{b} {room}: ERROR {exc}")
            evaluated += 1
            continue
        evaluated += 1
        if verdict == "High":
            still_high += 1
        else:
            misses.append(f"{a}/{b} {room}: High -> {verdict}")
    return still_high, evaluated, misses


def run_classify_ab(
    conn: Any, llm: Any, r2: Any, *,
    candidate_model: str, max_edge: int, prod_model: str, sample: int,
) -> tuple[int, int]:
    """Re-classify a sample; return (agreeing_labels, total_labels)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT i.sreality_id FROM images i "
            "JOIN image_room_classifications c ON c.image_id = i.id "
            "WHERE c.model = %s ORDER BY i.sreality_id LIMIT %s",
            (prod_model, sample),
        )
        sids = [r[0] for r in cur.fetchall()]

    agree = 0
    total = 0
    for sid in sids:
        imgs = _listing_images(conn, sid, 12)  # mirrors classify_listing_images n_images default
        if not imgs:
            continue
        prod = _prod_labels(conn, [i for i, _ in imgs], prod_model)
        if not prod:
            continue
        content: list[dict[str, Any]] = [
            {"type": "text", "text": f"{len(imgs)} listing images, in order (index 0..N):"}
        ]
        for idx, (_, path) in enumerate(imgs):
            content.append({"type": "text", "text": f"Image index {idx}:"})
            content.append(image_block(r2, path, max_edge))
        try:
            resp = llm.call(
                called_for="classify_listing_images",
                messages=[{"role": "user", "content": content}],
                system=llm.resolve_system_prompt(ic._PROMPT_KEY),
                tools=[ic.RECORD_ROOM_TYPES_TOOL],
                model=candidate_model,
            )
            rooms = ic._extract_rooms(resp.tool_calls)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("classify sid=%s failed: %s", sid, exc)
            continue
        cand = {}
        for entry in rooms:
            i = entry.get("index")
            if isinstance(i, int) and 0 <= i < len(imgs):
                cand[imgs[i][0]] = entry.get("room_type")
        for image_id, prod_rt in prod.items():
            if image_id in cand:
                total += 1
                if cand[image_id] == prod_rt:
                    agree += 1
    return agree, total


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="A/B a candidate vision (model, max_edge).")
    ap.add_argument("--candidate-model", default="claude-haiku-4-5")
    ap.add_argument("--max-edge", type=int, default=COMPARISON_MAX_EDGE)
    ap.add_argument("--compare-limit", type=int, default=200)
    ap.add_argument("--classify-sample", type=int, default=40)
    ap.add_argument("--min-compare-recall", type=float, default=1.0)
    ap.add_argument("--min-classify-agreement", type=float, default=0.85)
    ap.add_argument("--skip-classify", action="store_true")
    args = ap.parse_args()

    # Build the LLMClient without api.dependencies (which pulls in FastAPI) — mirrors
    # scripts.backfill_condition_scores so the harness runs under the minimal scoring deps.
    from api.llm_client import LLMClient
    from api.providers.anthropic import AnthropicProvider

    if not image_storage.is_configured():
        LOG.error("R2 is not configured; cannot fetch image bytes. Aborting.")
        return 2

    conn = db.connect()
    try:
        r2 = image_storage.R2Client.from_env()
        llm = LLMClient(conn, providers={"anthropic": AnthropicProvider()})
        prod_compare_model = llm.resolve_model(vm._MODEL_KEY)
        prod_classify_model = llm.resolve_model(ic._MODEL_KEY)

        LOG.info(
            "A/B candidate model=%s max_edge=%d vs prod compare=%s classify=%s",
            args.candidate_model, args.max_edge, prod_compare_model, prod_classify_model,
        )

        still_high, evaluated, misses = run_compare_ab(
            conn, llm, r2,
            candidate_model=args.candidate_model, max_edge=args.max_edge,
            prod_model=prod_compare_model, limit=args.compare_limit,
        )
        compare_recall = (still_high / evaluated) if evaluated else 1.0

        agreement = 1.0
        if not args.skip_classify:
            agree, total = run_classify_ab(
                conn, llm, r2,
                candidate_model=args.candidate_model, max_edge=args.max_edge,
                prod_model=prod_classify_model, sample=args.classify_sample,
            )
            agreement = (agree / total) if total else 1.0
            LOG.info("CLASSIFY agreement %d/%d = %.1f%%", agree, total, 100 * agreement)

        LOG.info(
            "COMPARE recall %d/%d = %.1f%% (still High at candidate)",
            still_high, evaluated, 100 * compare_recall,
        )
        for m in misses:
            LOG.warning("COMPARE miss: %s", m)

        ok_compare = compare_recall >= args.min_compare_recall
        ok_classify = args.skip_classify or agreement >= args.min_classify_agreement
        print(
            f"\n=== VISION A/B RESULT ===\n"
            f"candidate model: {args.candidate_model} @ {args.max_edge}px\n"
            f"compare recall:  {100 * compare_recall:.1f}%  (gate >= {100 * args.min_compare_recall:.0f}%)  "
            f"{'PASS' if ok_compare else 'FAIL'}\n"
            f"classify agree:  {100 * agreement:.1f}%  (gate >= {100 * args.min_classify_agreement:.0f}%)  "
            f"{'PASS' if ok_classify else 'FAIL'}\n"
            f"verdict: {'ADOPT — flip the model(s)' if (ok_compare and ok_classify) else 'DO NOT ADOPT'}\n"
        )
        return 0 if (ok_compare and ok_classify) else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
