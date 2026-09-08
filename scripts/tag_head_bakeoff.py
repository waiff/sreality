"""Train and score the tagging bake-off's arm x mode x head cross product, on CPU.

    python -m scripts.tag_head_bakeoff --run-id 3 --dry-run
    python -m scripts.tag_head_bakeoff --run-id 3
    python -m scripts.tag_head_bakeoff --run-id 3 --arms dinov3-b16@768 --modes pos_neg

The run and its arms are rows the operator (or the manifest lane) inserted into
`dedup_sim.tag_head_bakeoff_runs` / `_arms`; the GPU job fills
`dedup_sim.tag_head_bakeoff_vectors` for each arm BEFORE this runs. This step is
pure CPU — logistic regressions over a few hundred vectors apiece — and needs
`pip install -e ".[training]"` (scikit-learn) and nothing else.

RESUMABLE, because the cross product is long. Every (arm, mode, head) that
already has a metrics row is skipped unless `--force`; a cell that could not be
trained is RECORDED as failed rather than skipped, so a resume does not retry it
forever and the page can say why it is missing.

HEADS ARE CHOSEN BY A FLAG, NOT A LIST. `--min-train-positives` (default 100) is
the whole rule; the dry run prints every tag with its admitted counts and whether
it made the cut, so the scope of a run is inspectable before it costs an hour.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from toolkit import tag_head_bakeoff as bo
from toolkit import tag_heads as th

LOG = logging.getLogger("tag_head_bakeoff")


def _report_plan(conn, args: argparse.Namespace) -> int:
    run = bo.get_run(conn, run_id=args.run_id)
    if run is None:
        LOG.error("BAKEOFF run %d does not exist", args.run_id)
        return 1
    floor = (args.min_train_positives if args.min_train_positives is not None
             else run["min_train_positives"])
    arms = bo.list_arms(conn, run_id=args.run_id, names=args.arms)
    plans = bo.select_heads(conn, min_train_positives=floor)
    selected = [p for p in plans if p.selected]
    sitting = bo.load_exam(conn, cohort=args.exam_cohort, set_name=args.exam_set)
    done = bo.done_keys(conn, run_id=args.run_id)

    LOG.info("BAKEOFF run=%d %r status=%s min_train_positives=%d",
             run["id"], run["label"], run["status"], floor)
    for arm in arms:
        LOG.info("BAKEOFF arm=%s id=%d dim=%s vectors=%d %s",
                 arm.arm, arm.id, arm.dim, bo.arm_vector_count(conn, arm_id=arm.id),
                 json.dumps(arm.encoder.as_dict(), sort_keys=True))
    if not arms:
        LOG.warning("BAKEOFF this run has no arms — insert them first")
    LOG.info("BAKEOFF %-38s %7s %7s  %s", "head", "pos", "neg", "selected")
    for plan in sorted(plans, key=lambda p: (-p.n_pos, p.tag_id)):
        LOG.info("BAKEOFF %-38s %7d %7d  %s", plan.label[:38], plan.n_pos,
                 plan.n_neg, "yes" if plan.selected else f"no ({plan.reason})")
    LOG.info("BAKEOFF exam=%s", "none" if sitting is None else
             f"{args.exam_cohort}/{args.exam_set} "
             f"{len(sitting.rows)} answered images over {len(sitting.tag_ids)} tags")
    cells = len(arms) * len(args.modes) * len(selected)
    LOG.info("BAKEOFF plan: %d arm(s) x %d mode(s) x %d head(s) = %d cell(s); "
             "%d already have metrics", len(arms), len(args.modes), len(selected),
             cells, len(done))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", type=int, required=True,
                    help="dedup_sim.tag_head_bakeoff_runs.id to execute.")
    ap.add_argument("--min-train-positives", type=int, default=None,
                    help="Head selection floor; defaults to the run's own value.")
    ap.add_argument("--modes", nargs="+", default=list(th.MODES), choices=list(th.MODES),
                    help="Training modes to run (default: all three).")
    ap.add_argument("--arms", nargs="+", default=None,
                    help="Arm names to run (default: every arm of the run).")
    ap.add_argument("--n-splits", type=int, default=th.DEFAULT_N_SPLITS)
    ap.add_argument("--C", type=float, default=th.DEFAULT_C)
    ap.add_argument("--threshold", type=float, default=th.DEFAULT_THRESHOLD,
                    help="Cut for the logistic modes; the centroid mode picks "
                         "its own on the out-of-fold scores and reports it.")
    ap.add_argument("--seed", type=int, default=th.DEFAULT_SEED)
    ap.add_argument("--exam-cohort", default=bo.DEFAULT_EXAM_COHORT)
    ap.add_argument("--exam-set", default=bo.DEFAULT_EXAM_SET)
    ap.add_argument("--force", action="store_true",
                    help="Retrain cells that already have metrics.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report the plan; train nothing, write nothing.")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from scraper import db

    with db.connect() as conn:
        if args.dry_run:
            return _report_plan(conn, args)
        try:
            outcomes = bo.run_bakeoff(
                conn, run_id=args.run_id, modes=args.modes, arm_names=args.arms,
                min_train_positives=args.min_train_positives,
                n_splits=args.n_splits, C=args.C, threshold=args.threshold,
                seed=args.seed, exam_cohort=args.exam_cohort,
                exam_set=args.exam_set, force=args.force)
        except bo.BakeoffError as exc:
            LOG.error("BAKEOFF %s", exc)
            return 1

    ok = [o for o in outcomes if o.status == "ok"]
    failed = [o for o in outcomes if o.status != "ok"]
    LOG.info("BAKEOFF done: %d cell(s) trained, %d could not be graded, "
             "%d score rows written", len(ok), len(failed),
             sum(o.n_scores for o in ok))
    for outcome in failed:
        LOG.warning("BAKEOFF arm_id=%d mode=%s tag=%d not graded: %s",
                    outcome.arm_id, outcome.mode, outcome.tag_id, outcome.note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
