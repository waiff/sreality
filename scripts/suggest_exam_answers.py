"""Machine pre-answers for one exam sitting (migration 461).

    python -m scripts.suggest_exam_answers --cohort exam_v1 --set set_1
    python -m scripts.suggest_exam_answers --cohort exam_v1 --set set_2 --count 50

Runs each still-unsuggested exam member through the model with the SET's own
question list and stores which tags the model would press — served on the exam
screen as a subtle mark, never a pre-filled answer. Ordered by the operator
2026-08-30, reversing the exam's original no-suggestion posture; the stored
suggestion vs the final human answer is the standing anchoring audit.

Unlike the screen lane this one RUNS ON A SEALED COHORT — that is the normal
case: suggestions assist the sitting, and sittings happen after sealing. It
writes only tag_exam_suggestions, never a label, so the seal has nothing to
fear from it.

The spending rails are the screen lane's, shared via toolkit.vision_batch: a
pre-flight estimate from MEASURED cost (the suggest call is the screener's
shape, so screen_exam_image's measured cost is the prior until suggest has ten
calls of its own), a budget checked in the worker before each call, and errors
recorded as errors — an errored row is offered again next run.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

LOG = logging.getLogger("suggest_exam_answers")

CALLED_FOR = "suggest_exam_answer"

# Same model, same image tier, same ~30-token JSON reply as the screen lane —
# and the same reasoning-token trap, so the same ceiling (see that script).
MAX_TOKENS = 4096
DEFAULT_WORKERS = 8
WORKERS_MAX = 16


def _suggest_batch(
    r2: Any, *, cohort_id: int, set_id: int, rows: list[tuple[int, str]],
    tags: list[dict[str, Any]], model: str, max_usd: float, max_seconds: int,
    workers: int,
) -> dict[str, Any]:
    """The engine with the suggestion sink: parse with the screener's parser,
    record with the set's frozen question list."""
    from toolkit import exam_screening as es
    from toolkit import exam_suggestions as sugg
    from toolkit import vision_batch

    prompt = sugg.build_prompt(tags)
    valid = {t["id"] for t in tags}
    asked = [t["id"] for t in tags]

    def _record(wconn: Any, image_id: int, ids: list[int] | None,
                error: str | None) -> None:
        sugg.record_suggestion(
            wconn, cohort_id=cohort_id, image_id=image_id, set_id=set_id,
            asked_tag_ids=asked, suggested_tag_ids=ids, model=model, error=error,
        )

    return vision_batch.run_vision_batch(
        r2, rows=rows, prompt=prompt,
        parse=lambda text: es.parse_guess(text, valid_ids=valid),
        record=_record, model=model, called_for=CALLED_FOR,
        max_tokens=MAX_TOKENS, max_usd=max_usd, max_seconds=max_seconds,
        workers=max(1, min(workers, WORKERS_MAX)),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cohort", required=True)
    ap.add_argument("--set", required=True, dest="set_name",
                    help="The tag_exam_sets row whose question list to pre-answer.")
    ap.add_argument("--count", type=int, default=300,
                    help="At most this many members this run (default: the whole cohort).")
    ap.add_argument("--max-usd", type=float, default=1.0,
                    help="Hard pre-flight ceiling for this run.")
    ap.add_argument("--max-seconds", type=int, default=1500)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--model", default="gpt-5-mini")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s",
    )

    from scraper import db
    from toolkit import exam_suggestions as sugg
    from toolkit import tag_holdout

    with db.connect() as conn:
        cohort = tag_holdout.get_cohort(conn, name=args.cohort)
        if cohort is None:
            LOG.error("SUGGEST cohort %r does not exist", args.cohort)
            return 1
        exam_set = sugg.get_set(conn, name=args.set_name)
        if exam_set is None:
            LOG.error("SUGGEST set %r does not exist", args.set_name)
            return 1
        tags = sugg.set_tags(conn, tag_ids=exam_set["tag_ids"])
        if not tags:
            LOG.error("SUGGEST set %r resolves to no active tags", args.set_name)
            return 1

        rows = sugg.unsuggested_members(
            conn, cohort_id=cohort["id"], set_id=exam_set["id"],
            limit=max(1, args.count),
        )
        LOG.info("SUGGEST cohort=%r set=%r tags=%d to_suggest=%d model=%s",
                 args.cohort, args.set_name, len(tags), len(rows), args.model)
        if not rows:
            LOG.info("SUGGEST nothing to do: every member has a suggestion for this set")
            return 0

        n_measured, avg = sugg.measured_cost(
            conn, model=args.model, called_fors=[CALLED_FOR, "screen_exam_image"])
        if n_measured < 10:
            LOG.error("SUGGEST refusing to run on an UNMEASURED cost: only %d prior "
                      "calls across suggest+screen. Calibrate the screen lane first "
                      "— reasoning tokens bill as output at $2.00/M and an estimate "
                      "from input tokens alone is a cap in name only.", n_measured)
            return 1
        estimate = avg * len(rows)
        LOG.info("SUGGEST pre-flight measured_per_image=$%.5f (n=%d) estimate=$%.2f "
                 "ceiling=$%.2f", avg, n_measured, estimate, args.max_usd)
        if estimate > args.max_usd:
            LOG.error("SUGGEST refusing to start: $%.2f estimated over $%.2f ceiling",
                      estimate, args.max_usd)
            return 1
        if args.dry_run:
            LOG.info("SUGGEST dry-run: would pre-answer %d images", len(rows))
            return 0

        from scraper.image_storage import R2Client

        r2 = R2Client.from_env()
        stats = _suggest_batch(
            r2, cohort_id=cohort["id"], set_id=exam_set["id"], rows=rows,
            tags=tags, model=args.model, max_usd=args.max_usd,
            max_seconds=args.max_seconds, workers=args.workers,
        )
        per_image = stats["spent"] / stats["ok"] if stats["ok"] else 0.0
        LOG.info("SUGGEST done ok=%d errors=%d with_hits=%d spent=$%.4f "
                 "per_image=$%.5f aborted=%s",
                 stats["ok"], stats["errors"], stats["hits"], stats["spent"],
                 per_image, stats["aborted"])
        return 1 if stats["errors"] and not stats["ok"] else 0


if __name__ == "__main__":
    sys.exit(main())
