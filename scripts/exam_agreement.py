"""Per-head agreement of the machine review against the human exam answers.

    python -m scripts.exam_agreement --cohort exam_v1 --set all
    python -m scripts.exam_agreement --cohort exam_v1 --set all --json

THE GATE BEFORE SPENDING AT SCALE. The whole point of a labeled exam is to
answer one question per head: can the model, reading the definition, be trusted
to label images we will train on? This prints that answer — precision, recall
and the graded denominator per tag — so bulk labeling is switched on head by
head on evidence, never wholesale on a hunch.

exam_v1 is the HOLDOUT and the honest yardstick: those images are excluded from
training, so measuring on them is not measuring on the model's own homework.
gold_v1 is curated (rare heads over-represented by construction), so its numbers
describe those heads better and the population worse — read it per head, never
as an overall score.

A cell grades only when both sides said yes or no. A "left out" on either side
is an abstention: it trains nothing and grades nothing, and scoring it as a no
would punish the model for obeying the rule. Abstentions are reported, not
folded in. Reads only; spends nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

LOG = logging.getLogger("exam_agreement")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cohort", required=True)
    ap.add_argument("--set", required=True, dest="set_name")
    ap.add_argument("--json", action="store_true", help="Machine-readable output.")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from scraper import db
    from toolkit import exam_machine_review as mr
    from toolkit import exam_suggestions as sugg
    from toolkit import tag_exam, tag_holdout

    with db.connect() as conn:
        cohort = tag_holdout.get_cohort(conn, name=args.cohort)
        if cohort is None:
            LOG.error("AGREEMENT cohort %r does not exist", args.cohort)
            return 1
        exam_set = sugg.get_set(conn, name=args.set_name)
        if exam_set is None:
            LOG.error("AGREEMENT set %r does not exist", args.set_name)
            return 1
        tag_ids = exam_set["tag_ids"]
        labels = {t["id"]: t["label"] for t in sugg.set_tags(conn, tag_ids=tag_ids)}
        rows = tag_exam.answers(conn, cohort_id=cohort["id"], tag_ids=tag_ids,
                                set_id=exam_set["id"])
        reviews = mr.reviews_for_answers(conn, cohort_id=cohort["id"], tag_ids=tag_ids)

    counts = mr.agreement(rows=rows, reviews=reviews, tag_ids=tag_ids)
    scored = {tag_id: mr.scored(c) for tag_id, c in counts.items()}
    reviewed = len({r["image_id"] for r in rows if reviews.get(r["image_id"])})

    if args.json:
        print(json.dumps({
            "cohort": args.cohort, "set": args.set_name,
            "answered": len(rows), "reviewed": reviewed,
            "heads": {str(k): {**v, "label": labels.get(k, str(k))}
                      for k, v in scored.items()},
        }, indent=2))
        return 0

    LOG.info("AGREEMENT cohort=%s set=%s answered=%d reviewed=%d",
             args.cohort, args.set_name, len(rows), reviewed)
    if not reviewed:
        LOG.warning("No current machine review for this sitting — run the review "
                    "lane first (action=review), or the definitions changed since.")
        return 0
    LOG.info("%-38s %6s %6s %5s %5s %5s %5s %7s %7s",
             "head", "human+", "graded", "tp", "fp", "fn", "abst", "prec", "recall")
    def _sort_key(item: tuple[int, dict]) -> tuple:
        return (-(item[1]["human_positives"]), item[0])
    for tag_id, s in sorted(scored.items(), key=_sort_key):
        abstain = s["human_skip"] + s["machine_skip"]
        LOG.info("%-38s %6d %6d %5d %5d %5d %5d %7s %7s",
                 labels.get(tag_id, str(tag_id))[:38], s["human_positives"],
                 s["graded"], s["tp"], s["fp"], s["fn"], abstain,
                 "-" if s["precision"] is None else f"{s['precision']:.2f}",
                 "-" if s["recall"] is None else f"{s['recall']:.2f}")
    unreviewed = max((s["unreviewed"] for s in scored.values()), default=0)
    if unreviewed:
        LOG.info("(%d answered images carry no current review — re-run the lane)",
                 unreviewed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
