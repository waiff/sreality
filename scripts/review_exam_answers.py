"""The definition-driven machine review of one exam cohort (migration 467).

    python -m scripts.review_exam_answers --cohort exam_v1 --set all
    python -m scripts.review_exam_answers --cohort gold_v1 --set all --count 100

Runs every member whose review is missing, errored or STALE (asked list or
definition versions differ from the current ones) through the model with ALL
of the set's definitions in one call, and stores a yes / no / skip verdict per
tag. The review page shows each disagreement with the human's answer as a
proposal. Re-dispatch after any definition edit: that is the refresh.

Runs on sealed cohorts — reviews happen after sittings. Writes only
tag_exam_machine_reviews, never a label.

Spending rails are the screen lane's (toolkit.vision_batch): a pre-flight
estimate, a budget checked before every call, errors recorded as errors. The
prior is this pass's OWN measured cost once it has ten calls; before that the
suggest/screen cost times PRIOR_MULTIPLE, because this call carries eighteen
definitions where those carried a list of names.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

LOG = logging.getLogger("review_exam_answers")

# Same reasoning-token trap as the screen and suggest lanes: gpt-5-mini spends
# its output budget on reasoning first and returns '' (billed) when it runs
# out. Size for the reasoning, not the reply.
MAX_TOKENS = 4096
DEFAULT_WORKERS = 8
WORKERS_MAX = 16
# Until this pass has ten measured calls of its own, price it at this multiple
# of the name-only calls: the prompt is ~15x longer and the reply ~18x.
PRIOR_MULTIPLE = 3.0
MIN_MEASURED = 10


def _review_batch(
    r2: Any, *, cohort_id: int, rows: list[tuple[int, str]],
    definitions: list[dict[str, Any]], model: str, max_usd: float,
    max_seconds: int, workers: int,
) -> dict[str, Any]:
    from toolkit import exam_machine_review as mr
    from toolkit import vision_batch

    prompt = mr.build_prompt(definitions)
    asked = [e["tag_id"] for e in definitions]
    valid = set(asked)
    versions = mr.versions_of(definitions)

    def _record(wconn: Any, image_id: int, verdicts: dict[int, str] | None,
                error: str | None) -> None:
        mr.record_review(
            wconn, cohort_id=cohort_id, image_id=image_id, asked_tag_ids=asked,
            versions=versions, verdicts=verdicts, model=model, error=error,
        )

    return vision_batch.run_vision_batch(
        r2, rows=rows, prompt=prompt,
        parse=lambda text: mr.parse_verdicts(text, valid_ids=valid),
        record=_record, model=model, called_for=mr.CALLED_FOR,
        max_tokens=MAX_TOKENS, max_usd=max_usd, max_seconds=max_seconds,
        workers=max(1, min(workers, WORKERS_MAX)),
    )


def _exit_code(stats: dict[str, Any], tag: str) -> int:
    """A run that mostly failed must not report success. The live case: 461 of
    2,500 images landed and the pass exited 0, because the old test only asked
    whether ANY image had succeeded — so an exhausted API key read as a green
    run."""
    if stats.get("fatal"):
        LOG.error("REVIEW FAILED: %s", stats["fatal"])
        return 1
    if stats["errors"] > stats["ok"]:
        LOG.error("REVIEW FAILED: %d errors against %d successes",
                  stats["errors"], stats["ok"])
        return 1
    if stats["errors"]:
        LOG.warning("REVIEW %d images errored and stay eligible for the next run",
                    stats["errors"])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cohort", required=True)
    ap.add_argument("--set", required=True, dest="set_name",
                    help="The tag_exam_sets row whose tags (and definitions) to review against.")
    ap.add_argument("--count", type=int, default=600,
                    help="At most this many members this run (default: a whole cohort).")
    ap.add_argument("--max-usd", type=float, default=2.0,
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
    from toolkit import exam_machine_review as mr
    from toolkit import exam_suggestions as sugg
    from toolkit import tag_holdout

    with db.connect() as conn:
        cohort = tag_holdout.get_cohort(conn, name=args.cohort)
        if cohort is None:
            LOG.error("REVIEW cohort %r does not exist", args.cohort)
            return 1
        exam_set = sugg.get_set(conn, name=args.set_name)
        if exam_set is None:
            LOG.error("REVIEW set %r does not exist", args.set_name)
            return 1
        try:
            definitions = mr.active_definitions(conn, tag_ids=exam_set["tag_ids"])
        except ValueError as exc:
            LOG.error("REVIEW refusing to run: %s — a review against a name alone is "
                      "what this pass exists to replace", exc)
            return 1
        versions = mr.versions_of(definitions)
        rows = mr.members_needing_review(
            conn, cohort_id=cohort["id"], tag_ids=exam_set["tag_ids"],
            versions=versions, limit=max(1, args.count),
        )
        LOG.info("REVIEW cohort=%r set=%r tags=%d versions=%s to_review=%d model=%s",
                 args.cohort, args.set_name, len(definitions), versions, len(rows),
                 args.model)
        if not rows:
            LOG.info("REVIEW nothing to do: every member has a current review")
            return 0

        n_own, avg_own = mr.measured_cost(conn, model=args.model)
        if n_own >= MIN_MEASURED:
            avg, basis = avg_own, f"own n={n_own}"
        else:
            n_prior, avg_prior = sugg.measured_cost(
                conn, model=args.model,
                called_fors=["suggest_exam_answer", "screen_exam_image"])
            if n_prior < MIN_MEASURED:
                LOG.error("REVIEW refusing to run on an UNMEASURED cost: %d own calls, "
                          "%d suggest/screen calls. Calibrate the screen lane first.",
                          n_own, n_prior)
                return 1
            avg, basis = avg_prior * PRIOR_MULTIPLE, f"suggest/screen n={n_prior} x{PRIOR_MULTIPLE}"
        estimate = avg * len(rows)
        LOG.info("REVIEW pre-flight per_image=$%.5f (%s) estimate=$%.2f ceiling=$%.2f",
                 avg, basis, estimate, args.max_usd)
        if estimate > args.max_usd:
            LOG.error("REVIEW refusing to start: $%.2f estimated over $%.2f ceiling",
                      estimate, args.max_usd)
            return 1
        if args.dry_run:
            LOG.info("REVIEW dry-run: would review %d images", len(rows))
            return 0

        from scraper.image_storage import R2Client

        r2 = R2Client.from_env()
        stats = _review_batch(
            r2, cohort_id=cohort["id"], rows=rows, definitions=definitions,
            model=args.model, max_usd=args.max_usd, max_seconds=args.max_seconds,
            workers=args.workers,
        )
        per_image = stats["spent"] / stats["ok"] if stats["ok"] else 0.0
        LOG.info("REVIEW done ok=%d errors=%d spent=$%.4f per_image=$%.5f aborted=%s",
                 stats["ok"], stats["errors"], stats["spent"], per_image,
                 stats["aborted"])
        return _exit_code(stats, "REVIEW")


if __name__ == "__main__":
    sys.exit(main())
