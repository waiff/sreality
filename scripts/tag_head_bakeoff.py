"""Train and score the tagging bake-off's arm x mode x head cross product, on CPU.

    python -m scripts.tag_head_bakeoff --run-id 3 --dry-run
    python -m scripts.tag_head_bakeoff --run-id 3
    python -m scripts.tag_head_bakeoff --run-id 3 --arms dinov3-b16@768/bf16
    python -m scripts.tag_head_bakeoff --run-id 3 --modes pos_neg pos_only_centroid

The run and its arms are rows the operator (or the manifest lane) inserted into
`dedup_sim.tag_head_bakeoff_runs` / `_arms`; the GPU job fills
`dedup_sim.tag_head_bakeoff_vectors` for each arm BEFORE this runs. This step is
pure CPU — logistic regressions over a few hundred vectors apiece — and needs
`pip install -e ".[training]"` (scikit-learn) and nothing else.

RESUMABLE, because the cross product is long. Every (arm, mode, head) that
already has a metrics row is skipped unless `--force`; a cell that could not be
trained is RECORDED as failed rather than skipped, so a resume does not retry it
forever and the page can say why it is missing.

THE DEFAULT MODE SET IS `pos_neg` ALONE (ruling 2026-09-09). The two positive-only
modes measured 0.05-0.07 mean F1 below it in run 1, so they no longer run by
themselves; `--modes pos_only_free_neg` still runs one when the operator asks for it,
and `tag_heads.MODES` is still the whole vocabulary the flag validates against.

HEADS ARE CHOSEN BY THE OPERATOR'S READY FLAG (ruling 2026-09-08) — the Ready /
Not ready / Skip toggle on `/new-dedup/training-set`, read through
`tag_head_bakeoff.ready_heads`. `--heads` overrides it with a named list. The
admitted positive/negative counts are still printed for every tag, but they are
INFORMATION: a ready head with too few rows trains, fails, and records why.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Sequence

from toolkit import tag_head_bakeoff as bo
from toolkit import tag_heads as th

LOG = logging.getLogger("tag_head_bakeoff")


def split_list(values: Sequence[str] | None) -> list[str]:
    """Flatten `--arms a b`, `--arms a,b` and `--arms "a, b"` into the same list.

    A workflow_dispatch input is ONE string, so the dispatcher hands these flags a single
    comma-joined token — and `nargs="+"` alone read that token as one arm name. The train
    stage of run 1 then died with `has no arms named ['a,b,c']` and wrote nothing
    (2026-09-08 (i)). The embed payload has always split on commas; this is that same
    reading, applied to every list-shaped flag here.
    """
    out: list[str] = []
    for value in values or ():
        out.extend(part.strip() for part in value.split(",") if part.strip())
    return out


def _report_plan(conn, args: argparse.Namespace) -> int:
    run = bo.get_run(conn, run_id=args.run_id)
    if run is None:
        LOG.error("BAKEOFF run %d does not exist", args.run_id)
        return 1
    arms = bo.list_arms(conn, run_id=args.run_id, names=args.arms)
    plans = bo.select_heads(conn, tag_ids=args.heads or None)
    selected = [p for p in plans if p.selected]
    sitting = bo.load_exam(conn, cohort=args.exam_cohort, set_name=args.exam_set)
    done = bo.done_keys(conn, run_id=args.run_id)

    LOG.info("BAKEOFF run=%d %r status=%s selection=%s",
             run["id"], run["label"], run["status"],
             f"explicit --heads {args.heads}" if args.heads
             else "tag_taxonomy ready flag")
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


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", type=int, required=True,
                    help="dedup_sim.tag_head_bakeoff_runs.id to execute.")
    # The three list flags take space- OR comma-separated values and are normalised in
    # parse_args(), never by argparse: `type=int` and `choices=` both run per TOKEN, so
    # either would reject a comma-joined workflow input before it could be split.
    ap.add_argument("--heads", nargs="+", default=None,
                    help="Tag ids to run, overriding the operator's ready flag. "
                         "A named list is itself an operator decision.")
    ap.add_argument("--modes", nargs="+", default=list(th.DEFAULT_MODES),
                    help=f"Training modes to run, from {', '.join(th.MODES)} "
                         f"(default: {', '.join(th.DEFAULT_MODES)}). The "
                         "positive-only modes are retired from the default set "
                         "(ruling 2026-09-09) and run only when named here.")
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
    return ap


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse and normalise: after this the three list flags are lists, whatever shape
    the caller spelled them in, and an empty selection is None ("everything")."""
    ap = build_parser()
    args = ap.parse_args(argv)
    args.arms = split_list(args.arms) or None
    args.modes = split_list(args.modes) or list(th.DEFAULT_MODES)
    unknown = [m for m in args.modes if m not in th.MODES]
    if unknown:
        ap.error(f"--modes: unknown mode(s) {', '.join(unknown)}; "
                 f"choose from {', '.join(th.MODES)}")
    heads = split_list(args.heads)
    if any(not h.lstrip("-").isdigit() for h in heads):
        ap.error(f"--heads: tag ids must be integers, got {' '.join(heads)}")
    args.heads = [int(h) for h in heads] or None
    return args


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from scraper import db

    with db.connect() as conn:
        if args.dry_run:
            return _report_plan(conn, args)
        try:
            outcomes = bo.run_bakeoff(
                conn, run_id=args.run_id, modes=args.modes, arm_names=args.arms,
                tag_ids=args.heads or None,
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
