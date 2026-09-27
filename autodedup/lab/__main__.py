"""`python3 -m autodedup.lab` — build a cohort's evidence cache, run experiment configs over it,
verify the reference arm against a stored generation, and print the leaderboard.

    python3 -m autodedup.lab build trial --workers 4
    python3 -m autodedup.lab verify trial
    python3 -m autodedup.lab run autodedup/lab/experiments/*.json --cohort trial
    python3 -m autodedup.lab board
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from autodedup.lab import board as lab_board
from autodedup.lab import metrics, page
from autodedup.lab.cache import open_cohort, registry

REFERENCE = Path(__file__).parent / "experiments" / "w31_reference.json"


def _board_path(reg: dict[str, Any], given: str | None) -> Path:
    return Path(given or reg["leaderboard"])


def cmd_build(args: argparse.Namespace) -> int:
    clock = time.perf_counter()
    cohort = open_cohort(args.cohort, args.cohorts, workers=args.workers)
    print(json.dumps({"cohort": cohort.name, "version": cohort.version, "pairs": cohort.n,
                      "reachable": int(cohort.done["gate"].sum()), "timings": cohort.timings,
                      "wall_s": round(time.perf_counter() - clock, 1)}, indent=1))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    reg = registry(args.cohorts)
    cohort = open_cohort(args.cohort, args.cohorts, workers=args.workers)
    outcome = lab_board.run(cohort, lab_board.load_config(args.config or REFERENCE))
    spec = reg["cohorts"][args.cohort]
    report: dict[str, Any] = {"cohort": args.cohort, "cache": cohort.version,
                              "timings": outcome.timings}
    if spec.get("stored"):
        report["rows"] = metrics.verify(cohort, outcome, spec["stored"])
    if spec.get("harness_run"):
        report["groups"] = metrics.groups_identical(outcome, spec["harness_run"])
    print(json.dumps(report, indent=1))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    reg = registry(args.cohorts)
    labels = metrics.load_labels(reg)
    board = _board_path(reg, args.board)
    configs = [arm for path in args.configs for arm in lab_board.expand(lab_board.load_config(path))]
    base_config = lab_board.load_config(args.base or REFERENCE)
    for name in args.cohort:
        clock = time.perf_counter()
        cohort = open_cohort(name, args.cohorts, workers=args.workers)
        print(f"[{name}] cache {cohort.version} ready in {time.perf_counter() - clock:.1f}s "
              f"({cohort.n} pairs)", flush=True)
        base = lab_board.run(cohort, base_config)
        metrics.append(board, metrics.row(cohort, base, None, labels))
        for config in configs:
            if lab_board.config_id(config) == lab_board.config_id(base_config):
                continue
            outcome = lab_board.run(cohort, config)
            entry = metrics.row(cohort, outcome, base, labels)
            metrics.append(board, entry)
            vs, r = entry["vs_base"], entry["rulings"]
            print(f"[{name}] {entry['experiment']}: merge {entry['zones']['merge']} groups "
                  f"{entry['groups']} co +{vs['copairs_gained']}/-{vs['copairs_lost']} "
                  f"op same {r['same_together']}/{r['same_n']} diff {r['diff_together']}/"
                  f"{r['diff_n']}  {entry['timings']}", flush=True)
            if args.review:
                out = Path(args.review) / name / f"{entry['experiment']}.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(metrics.review(cohort, outcome, base, labels),
                                          default=str), encoding="utf-8")
    print(metrics.render(board, args.cohort))
    return 0


def cmd_lost(args: argparse.Namespace) -> int:
    reg = registry(args.cohorts)
    cohort = open_cohort(args.cohort, args.cohorts, workers=args.workers)
    outcome = lab_board.run(cohort, lab_board.load_config(args.config or REFERENCE))
    labels = metrics.load_labels(reg)
    report: dict[str, Any] = {"cohort": args.cohort, "experiment": outcome.config.get("name"),
                              "lost": metrics.where_lost(cohort, outcome, labels)}
    if args.why:
        report["why_refused"] = metrics.why_refused(cohort, outcome, labels)
    print(json.dumps(report, indent=1))
    return 0


def cmd_page(args: argparse.Namespace) -> int:
    review = json.loads(Path(args.review).read_text(encoding="utf-8"))
    out = Path(args.out or Path(args.review).with_suffix(".html"))
    out.write_text(page.render(review, args.n, args.seed), encoding="utf-8")
    print(out)
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    reg = registry(args.cohorts)
    table = metrics.render(_board_path(reg, args.board), args.cohort or None)
    if args.out:
        Path(args.out).write_text(table, encoding="utf-8")
    print(table)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autodedup.lab")
    parser.add_argument("--cohorts", help="cohort registry JSON (or AUTODEDUP_LAB_COHORTS)")
    parser.add_argument("--workers", type=int, default=4)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("cohort")
    build.set_defaults(fn=cmd_build)
    verify = sub.add_parser("verify")
    verify.add_argument("cohort")
    verify.add_argument("--config")
    verify.set_defaults(fn=cmd_verify)
    run = sub.add_parser("run")
    run.add_argument("configs", nargs="+")
    run.add_argument("--cohort", action="append", required=True)
    run.add_argument("--base")
    run.add_argument("--board")
    run.add_argument("--review", help="directory for each arm's changed-groups review file")
    run.set_defaults(fn=cmd_run)
    lost = sub.add_parser("lost", help="where each labelled pair the arm misses was lost")
    lost.add_argument("cohort")
    lost.add_argument("--config")
    lost.add_argument("--why", action="store_true",
                      help="for merge edges the group step refused: the invariant and the facts")
    lost.set_defaults(fn=cmd_lost)
    review = sub.add_parser("page", help="one review page from an arm's review file")
    review.add_argument("review")
    review.add_argument("--out")
    review.add_argument("--n", type=int, default=60, help="groups in the seeded sample (0 = all)")
    review.add_argument("--seed", type=int, default=1)
    review.set_defaults(fn=cmd_page)
    show = sub.add_parser("board")
    show.add_argument("--cohort", action="append")
    show.add_argument("--board")
    show.add_argument("--out")
    show.set_defaults(fn=cmd_board)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
