"""Bulk machine labeling — the LLM builds the training set for named heads.

    python -m scripts.label_images --tags 22,25,28 --count 200 --max-usd 2 --dry-run
    python -m scripts.label_images --tags 22,25,28 --count 200 --max-usd 2
    python -m scripts.label_images --tags 22 --ids-file ids.txt --count 500 --max-usd 3
    python -m scripts.label_images --tags 22,25 --status

One call per image carries the ACTIVE definitions of the named heads and the
three-tier rule; the verdicts land in image_tag_labels as source='machine'
cells, stamped with the definition that produced them.

HEADS ARE NAMED EXPLICITLY, always. There is no "label everything" switch,
because bulk labeling is only justified for a head the agreement gate has shown
the model can read (`python -m scripts.exam_agreement`), and that threshold is
the operator's judgment, not a constant in a script.

Exam members are never labeled — the holdout has to stay unseen by training and
a curated member is the operator's to answer. Resume is by provenance: an image
is done for a head once it carries a machine cell stamped with that head's
currently-active definition, so an interrupted run costs nothing to repeat and
a definition edit re-opens exactly the heads whose wording moved.

Spending rails are the programme's: a pre-flight estimate from this pass's own
measured cost (or the review pass's, scaled, until it has ten calls of its own),
a budget checked before each call, and errors recorded rather than written as
negatives.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

LOG = logging.getLogger("label_images")

MAX_TOKENS = 4096
DEFAULT_WORKERS = 8
WORKERS_MAX = 16
MIN_MEASURED = 10
# The review pass is the same shape (one photo, every definition, a small JSON
# reply), so its measured cost is a sound prior at parity until this pass has
# ten calls of its own.
PRIOR_MULTIPLE = 1.0


def _parse_tags(raw: str) -> list[int]:
    ids = [int(part) for part in raw.replace(" ", "").split(",") if part]
    if not ids:
        raise ValueError("--tags names no head")
    return list(dict.fromkeys(ids))


def _label_batch(
    r2: Any, *, rows: list[tuple[int, str]], definitions: list[dict[str, Any]],
    model: str, max_usd: float, max_seconds: int, workers: int,
) -> dict[str, Any]:
    from toolkit import exam_machine_review as mr
    from toolkit import machine_labeling as ml
    from toolkit import vision_batch

    prompt = mr.build_prompt(definitions)
    valid = {e["tag_id"] for e in definitions}

    def _record(wconn: Any, image_id: int, verdicts: dict[int, str] | None,
                error: str | None) -> None:
        if error is not None or not verdicts:
            # An unusable reply is the absence of evidence. Writing negatives
            # here would poison the training set with confident-looking rows
            # nobody ever produced; the image simply stays eligible.
            LOG.warning("LABEL image=%s unusable reply: %s", image_id, (error or "")[:160])
            return
        ml.record_labels(wconn, image_id=image_id, verdicts=verdicts, model=model)

    return vision_batch.run_vision_batch(
        r2, rows=rows, prompt=prompt,
        parse=lambda text: mr.parse_verdicts(text, valid_ids=valid),
        record=_record, model=model, called_for=ml.CALLED_FOR,
        max_tokens=MAX_TOKENS, max_usd=max_usd, max_seconds=max_seconds,
        workers=max(1, min(workers, WORKERS_MAX)),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tags", required=True,
                    help="Comma-separated tag ids to label. Named explicitly on purpose.")
    ap.add_argument("--count", type=int, default=100,
                    help="At most this many images this run.")
    ap.add_argument("--ids-file",
                    help="Label these image ids (one per line) instead of a random "
                         "sample — the hook for a targeted draw.")
    ap.add_argument("--sample-pct", type=float, default=1.0,
                    help="Block-sample percentage for the random/near-tag draw.")
    ap.add_argument("--near-tag", type=int,
                    help="Draw images that look like this head's known positives "
                         "(CLIP centroid over a sampled slice) instead of at random. "
                         "For rare heads that a random draw cannot reach — the draw "
                         "inherits CLIP's blind spots, so use it BESIDE the random "
                         "one, never instead of it.")
    ap.add_argument("--max-usd", type=float, default=1.0)
    ap.add_argument("--max-seconds", type=int, default=1500)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--model", default="gpt-5-mini")
    ap.add_argument("--status", action="store_true",
                    help="Print the training set as it stands and exit.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    try:
        tag_ids = _parse_tags(args.tags)
    except ValueError as exc:
        LOG.error("LABEL %s", exc)
        return 1

    from scraper import db
    from toolkit import exam_machine_review as mr
    from toolkit import machine_labeling as ml

    with db.connect() as conn:
        try:
            definitions = mr.active_definitions(conn, tag_ids=tag_ids)
        except ValueError as exc:
            LOG.error("LABEL refusing to run: %s — labeling against a bare tag name "
                      "is what the definitions exist to replace", exc)
            return 1
        labels = {e["tag_id"]: e["label"] for e in definitions}

        if args.status:
            counts = ml.labelled_counts(conn, tag_ids=tag_ids)
            LOG.info("%-38s %9s %9s %9s", "head", "positive", "negative", "left out")
            for tag_id in tag_ids:
                c = counts.get(tag_id, {})
                LOG.info("%-38s %9d %9d %9d", labels.get(tag_id, str(tag_id))[:38],
                         c.get("positive", 0), c.get("negative", 0), c.get("excluded", 0))
            return 0

        if args.near_tag and args.near_tag not in tag_ids:
            LOG.error("LABEL --near-tag %d must be one of the heads being labeled",
                      args.near_tag)
            return 1

        if args.near_tag:
            rows = ml.near_tag_candidates(
                conn, seed_tag_id=args.near_tag, tag_ids=tag_ids,
                limit=max(1, args.count), pct=args.sample_pct or 5.0)
            LOG.info("LABEL near-tag=%d (%s) drew=%d",
                     args.near_tag, labels.get(args.near_tag, "?"), len(rows))
            if not rows:
                LOG.warning("LABEL no candidates — the head may have too few embedded "
                            "positives for a centroid, or the sampled slice held none")
        elif args.ids_file:
            with open(args.ids_file, encoding="utf-8") as fh:
                image_ids = [int(line) for line in fh.read().split() if line.strip()]
            rows = ml.candidates_by_ids(conn, tag_ids=tag_ids, image_ids=image_ids,
                                        limit=max(1, args.count))
            LOG.info("LABEL ids-file named=%d eligible=%d", len(image_ids), len(rows))
        else:
            rows = ml.sample_candidates(conn, tag_ids=tag_ids,
                                        limit=max(1, args.count), pct=args.sample_pct)
        LOG.info("LABEL heads=%d to_label=%d model=%s",
                 len(definitions), len(rows), args.model)
        if not rows:
            LOG.info("LABEL nothing eligible: every sampled image already carries a "
                     "machine cell under the current definitions")
            return 0

        n_own, avg_own = ml.measured_cost(conn, model=args.model,
                                          called_for=ml.CALLED_FOR)
        if n_own >= MIN_MEASURED:
            avg, basis = avg_own, f"own n={n_own}"
        else:
            n_prior, avg_prior = ml.measured_cost(conn, model=args.model,
                                                  called_for=mr.CALLED_FOR)
            if n_prior < MIN_MEASURED:
                LOG.error("LABEL refusing to run on an UNMEASURED cost: %d own calls, "
                          "%d review calls. Run the exam review first — it is the same "
                          "call shape and it is how this pass learns its price.",
                          n_own, n_prior)
                return 1
            avg, basis = avg_prior * PRIOR_MULTIPLE, f"review n={n_prior}"
        estimate = avg * len(rows)
        LOG.info("LABEL pre-flight per_image=$%.5f (%s) estimate=$%.2f ceiling=$%.2f",
                 avg, basis, estimate, args.max_usd)
        if estimate > args.max_usd:
            LOG.error("LABEL refusing to start: $%.2f estimated over $%.2f ceiling",
                      estimate, args.max_usd)
            return 1
        if args.dry_run:
            LOG.info("LABEL dry-run: would label %d images across %d heads",
                     len(rows), len(definitions))
            return 0

        from scraper.image_storage import R2Client

        stats = _label_batch(
            R2Client.from_env(), rows=rows, definitions=definitions,
            model=args.model, max_usd=args.max_usd, max_seconds=args.max_seconds,
            workers=args.workers,
        )
        per_image = stats["spent"] / stats["ok"] if stats["ok"] else 0.0
        LOG.info("LABEL done ok=%d errors=%d spent=$%.4f per_image=$%.5f aborted=%s",
                 stats["ok"], stats["errors"], stats["spent"], per_image,
                 stats["aborted"])
        return 1 if stats["errors"] and not stats["ok"] else 0


if __name__ == "__main__":
    sys.exit(main())
