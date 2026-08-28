"""Vision screening for the exam's stratified frame (migrations 458 + 459).

    python -m scripts.screen_exam_cohort --cohort exam_v1 --calibrate 25
    python -m scripts.screen_exam_cohort --cohort exam_v1 --screen 4000 --max-usd 8
    python -m scripts.screen_exam_cohort --cohort exam_v1 --stratify 150

Three phases, deliberately separate acts.

CALIBRATE FIRST, ALWAYS. The repo has no measured per-image vision cost, and
gpt-5-mini bills reasoning tokens as OUTPUT at $2.00/M — this codebase has already
been bitten once by reasoning consuming a whole budget. A cap extrapolated from
input tokens alone would pass a run that overspends severalfold, which is a cap in
name only. So `--calibrate` screens a small sample, measures what was ACTUALLY
billed, and every later run sizes its pre-flight estimate from that number.

THE CAP IS PRE-FLIGHT. api.llm_client's daily-cost check only LOGS, and on a long
run it notices after the money is gone. `--max-usd` refuses to start.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any

LOG = logging.getLogger("screen_exam_cohort")

CALLED_FOR = "screen_exam_image"
# The screener answers with a short JSON object. A generous ceiling still bounds a
# runaway reasoning trace, which is the failure mode that actually costs money.
MAX_TOKENS = 300


_MEASURED_COST_SQL = """
    SELECT count(*)::int, COALESCE(avg(cost_usd), 0)::double precision
    FROM llm_calls
    WHERE called_for = %(called_for)s AND model = %(model)s AND cost_usd IS NOT NULL
"""

_UNSCREENED_PROBE_SQL = """
    WITH bounds AS (SELECT min(id) AS lo, max(id) AS hi FROM images),
    probes AS (
      SELECT DISTINCT (b.lo + floor(random() * (b.hi - b.lo + 1)))::bigint AS id
      FROM bounds b, generate_series(1, %(probes)s)
    )
    SELECT i.id, i.storage_path
    FROM probes p
    JOIN images i ON i.id = p.id
    WHERE i.storage_path IS NOT NULL
      AND EXISTS (
            SELECT 1 FROM image_clip_embeddings e
            WHERE e.image_id = i.id AND e.model = %(model)s::text
          )
      AND NOT EXISTS (
            SELECT 1 FROM tag_exam_members m WHERE m.image_id = i.id
          )
      AND NOT EXISTS (
            SELECT 1 FROM tag_exam_screens s
            WHERE s.cohort_id = %(cohort_id)s AND s.image_id = i.id
          )
    LIMIT %(count)s
"""

_ROUTING_TAGS_SQL = """
    SELECT id, label FROM tag_taxonomy
    WHERE routing_categories IS NOT NULL AND active
    ORDER BY id
"""


def _measured_cost(conn: Any, *, model: str) -> tuple[int, float]:
    with conn.cursor() as cur:
        cur.execute(_MEASURED_COST_SQL, {"called_for": CALLED_FOR, "model": model})
        row = cur.fetchone()
    return (int(row[0]), float(row[1])) if row else (0, 0.0)


def _routing_tags(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(_ROUTING_TAGS_SQL)
        return [{"id": int(r[0]), "label": r[1]} for r in cur.fetchall()]


def _draw_unscreened(conn: Any, *, cohort_id: int, model: str, count: int) -> list[tuple[int, str]]:
    with conn.cursor() as cur:
        cur.execute(_UNSCREENED_PROBE_SQL, {
            "probes": min(60_000, count * 12), "count": count,
            "model": model, "cohort_id": cohort_id,
        })
        return [(int(r[0]), r[1]) for r in cur.fetchall()]


def _screen_batch(
    conn: Any, llm, r2, *, cohort_id: int, rows: list[tuple[int, str]],
    tags: list[dict[str, Any]], model: str, max_usd: float, max_seconds: int,
) -> dict[str, Any]:
    from toolkit import exam_screening as es
    from toolkit.vision_images import COMPARISON_MAX_EDGE, image_block

    prompt = es.build_prompt(tags)
    valid = {t["id"] for t in tags}
    stats = {"ok": 0, "errors": 0, "hits": 0, "spent": 0.0, "aborted": False}
    started = time.monotonic()

    for image_id, storage_path in rows:
        if max_usd > 0 and stats["spent"] >= max_usd:
            LOG.warning("SCREEN cost ceiling $%.2f reached; stopping cleanly", max_usd)
            stats["aborted"] = True
            break
        if max_seconds > 0 and time.monotonic() - started >= max_seconds:
            LOG.info("SCREEN time budget %ds reached; stopping cleanly", max_seconds)
            stats["aborted"] = True
            break
        try:
            block = image_block(r2, storage_path, COMPARISON_MAX_EDGE)
            res = llm.call(
                called_for=CALLED_FOR, model=model, max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": [block, {"type": "text", "text": prompt}]}],
            )
            stats["spent"] += float(getattr(res, "cost_usd", 0.0) or 0.0)
            ids = es.parse_guess(getattr(res, "text", "") or "", valid_ids=valid)
        except Exception as exc:  # noqa: BLE001 - one image must not kill the pass
            # Recorded as an ERROR, never as an empty guess. The two look identical
            # downstream and mean opposite things: "saw nothing" is evidence,
            # "failed" is the absence of it, and binning failures into screen_none
            # would fill that stratum with model errors.
            stats["errors"] += 1
            es.record_screen(conn, cohort_id=cohort_id, image_id=image_id,
                             guess_tag_ids=None, model=model, error=str(exc)[:500])
            continue
        stats["ok"] += 1
        stats["hits"] += 1 if ids else 0
        es.record_screen(conn, cohort_id=cohort_id, image_id=image_id,
                         guess_tag_ids=ids, model=model, error=None)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cohort", required=True)
    ap.add_argument("--calibrate", type=int, default=0,
                    help="Screen N images and report the MEASURED cost per image.")
    ap.add_argument("--screen", type=int, default=0,
                    help="Screen N images, refusing to start above --max-usd.")
    ap.add_argument("--stratify", type=int, default=0,
                    help="Draw N stratified members from the recorded screen.")
    ap.add_argument("--max-usd", type=float, default=8.0,
                    help="Hard pre-flight ceiling for this run.")
    ap.add_argument("--max-seconds", type=int, default=1500)
    ap.add_argument("--model", default="gpt-5-mini")
    ap.add_argument("--max-error-rate", type=float, default=0.05,
                    help="Refuse to stratify above this screen error rate.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s",
    )

    if sum(1 for x in (args.calibrate, args.screen, args.stratify) if x) != 1:
        LOG.error("SCREEN pass exactly one of --calibrate / --screen / --stratify")
        return 1

    from scraper import db
    from toolkit import exam_screening as es
    from toolkit import tag_exam, tag_holdout

    with db.connect() as conn:
        cohort = tag_holdout.get_cohort(conn, name=args.cohort)
        if cohort is None:
            LOG.error("SCREEN cohort %r does not exist; draw it first", args.cohort)
            return 1
        if cohort["sealed_at"] is not None:
            LOG.error("SCREEN cohort %r is sealed", args.cohort)
            return 1
        cohort_id = cohort["id"]

        if args.stratify:
            err = es.screen_error_rate(conn, cohort_id=cohort_id)
            LOG.info("SCREEN errors=%d/%d rate=%.3f", err["errors"], err["total"], err["rate"])
            if err["rate"] > args.max_error_rate:
                # Drawing around a broken screen would bury model failures inside
                # the screen_none stratum and bias every later recall estimate.
                LOG.error("SCREEN error rate %.3f exceeds %.3f; fix the screen before "
                          "stratifying", err["rate"], args.max_error_rate)
                return 1
            screens = es.screened_rows(conn, cohort_id=cohort_id)
            stratum_of, sizes = es.assign_strata(screens)
            quota = es.allocate_stratified(stratum_of, sizes, total=args.stratify)
            probs = es.inclusion_probabilities(quota, sizes)
            for s in sorted(quota):
                LOG.info("SCREEN stratum=%-20s size=%-5d take=%-4d p=%.4f",
                         s, sizes[s], quota[s], probs[s])
            if args.dry_run:
                LOG.info("SCREEN dry-run: would add %d members", sum(quota.values()))
                return 0
            by_stratum: dict[str, list[int]] = {}
            for image_id, stratum in stratum_of.items():
                by_stratum.setdefault(stratum, []).append(image_id)
            guesses = dict(screens)
            rows = []
            for s, n in quota.items():
                for image_id in sorted(by_stratum[s])[:n]:
                    rows.append({
                        "image_id": image_id, "frame": "stratified", "stratum": s,
                        "inclusion_probability": probs[s],
                        "screen_guess_tag_ids": guesses.get(image_id) or None,
                    })
            written = tag_exam.add_members(conn, cohort_id=cohort_id, rows=rows)
            LOG.info("SCREEN stratify added=%d of %d", written, len(rows))
            return 0

        count = args.calibrate or args.screen
        n_measured, avg = _measured_cost(conn, model=args.model)
        if args.screen:
            if n_measured < 10:
                LOG.error("SCREEN refusing to run %d images on an UNMEASURED cost: "
                          "only %d prior calls. Run --calibrate 25 first — an "
                          "estimate from input tokens alone ignores reasoning "
                          "tokens, which bill as output at $2.00/M.",
                          count, n_measured)
                return 1
            estimate = avg * count
            LOG.info("SCREEN pre-flight measured_per_image=$%.5f (n=%d) "
                     "estimate=$%.2f ceiling=$%.2f", avg, n_measured, estimate, args.max_usd)
            if estimate > args.max_usd:
                LOG.error("SCREEN refusing to start: $%.2f estimated over $%.2f ceiling",
                          estimate, args.max_usd)
                return 1

        tags = _routing_tags(conn)
        if not tags:
            LOG.error("SCREEN no routing tags; nothing to screen for")
            return 1
        rows = _draw_unscreened(conn, cohort_id=cohort_id, model=cohort["model"], count=count)
        LOG.info("SCREEN cohort=%r tags=%d to_screen=%d model=%s",
                 args.cohort, len(tags), len(rows), args.model)
        if args.dry_run:
            LOG.info("SCREEN dry-run: would screen %d images", len(rows))
            return 0

        from api.llm_client import LLMClient
        from api.providers.openai import OpenAIProvider
        from scraper.image_storage import R2Client

        llm = LLMClient(conn, providers={"openai": OpenAIProvider()})
        r2 = R2Client.from_env()
        stats = _screen_batch(
            conn, llm, r2, cohort_id=cohort_id, rows=rows, tags=tags,
            model=args.model, max_usd=args.max_usd, max_seconds=args.max_seconds,
        )
        per_image = stats["spent"] / stats["ok"] if stats["ok"] else 0.0
        LOG.info("SCREEN done ok=%d errors=%d with_hits=%d spent=$%.4f "
                 "measured_per_image=$%.5f aborted=%s",
                 stats["ok"], stats["errors"], stats["hits"], stats["spent"],
                 per_image, stats["aborted"])
        if args.calibrate and stats["ok"]:
            LOG.info("SCREEN calibration: 4000 images would cost about $%.2f",
                     per_image * 4000)
        return 1 if stats["errors"] and not stats["ok"] else 0


if __name__ == "__main__":
    sys.exit(main())
