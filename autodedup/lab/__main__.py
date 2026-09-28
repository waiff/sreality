"""`python3 -m autodedup.lab` — read a cohort's `harness run --evidence` artefact, verify the
reference arm reproduces it, run experiment configs over it, print the leaderboard and apply the
pre-registered keep and cut rules.

    python3 -m autodedup.harness run cohort.jsonl.gz --out RUN --settings w31 --model w6_gold \\
        --evidence --evidence-workers 4                  # the cache: once per export and code
    python3 -m autodedup.lab verify trial --rungs 3000   # no number counts before this passes
    python3 -m autodedup.lab fit --train trial --cohort c17 --cohort c18 --out MODELS
    python3 -m autodedup.lab ground trial --out GROUNDS/trial.npz  # the challenger's training rows
    python3 -m autodedup.lab mf-fit --ground trial=GROUNDS/trial.npz ... --score trial --out MF
    python3 -m autodedup.lab run autodedup/lab/experiments/*.json --cohort trial --why
    python3 -m autodedup.lab board --out LEADERBOARD.md
    python3 -m autodedup.lab keep ARM --incumbent w31_reference [--alias c18=c18_w31r1]
    python3 -m autodedup.lab cut ARM --param t_merge --incumbent w31_reference
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from autodedup.lab import board as lab_board
from autodedup.lab import fit, metrics, page, rules, verify
from autodedup.lab.cache import open_cohort, registry

REFERENCE = Path(__file__).parent / "experiments" / "w31_reference.json"


def _board_path(reg: dict[str, Any], given: str | None) -> Path:
    return Path(given or reg["leaderboard"])


def _root(reg: dict[str, Any]) -> Path:
    return Path(reg["cache_root"])


def cmd_load(args: argparse.Namespace) -> int:
    clock = time.perf_counter()
    cohort = open_cohort(args.cohort, args.cohorts, workers=args.workers)
    print(json.dumps({"cohort": cohort.name, "version": cohort.version, "pairs": cohort.n,
                      "engine": cohort.code_digest, "reachable": int(cohort.done["gate"].sum()),
                      "timings": {k: round(v, 2) for k, v in cohort.timings.items()},
                      "wall_s": round(time.perf_counter() - clock, 1)}, indent=1))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    reg = registry(args.cohorts)
    clock = time.perf_counter()
    cohort = open_cohort(args.cohort, args.cohorts, workers=args.workers)
    load_s = time.perf_counter() - clock
    outcome = lab_board.run(cohort, lab_board.load_config(args.config or REFERENCE))
    spec = reg["cohorts"][args.cohort]
    report: dict[str, Any] = {"cohort": args.cohort, "cache": cohort.version,
                              "engine": cohort.code_digest, "load_s": round(load_s, 1),
                              "timings": outcome.timings,
                              "rows": verify.rows(cohort, outcome),
                              "groups": verify.groups(outcome, cohort.run.dir)}
    if spec.get("stored"):
        report["stored_generation"] = verify.stored_generation(cohort, outcome, spec["stored"])
    if args.rungs:
        idx = None if args.rungs < 0 else np.sort(np.random.default_rng(args.seed).choice(
            cohort.n, size=min(args.rungs, cohort.n), replace=False))
        clock = time.perf_counter()
        report["rung_equivalence"] = verify.rung_equivalence(cohort, idx)
        report["rung_equivalence"]["seconds"] = round(time.perf_counter() - clock, 1)
    ok = verify.passed(report)
    report["ok"] = ok
    if args.config is None:
        verify.record(_root(reg), cohort, report, ok)
    print(json.dumps(report, indent=1, default=str))
    return 0 if ok else 1


def verified_state(stamp_ok: bool, outcome: lab_board.Outcome, cache: str,
                   fits: dict[str, dict[str, Any]]) -> bool | str:
    """True when the stamp is verified and the score column is the one path's; a verified
    cache under any other score file is 'cache verified, scorer external'."""
    if not stamp_ok:
        return False
    return True if fit.scorer_state(outcome.scorer, cache, fits) else \
        "cache verified, scorer external"


def cmd_run(args: argparse.Namespace) -> int:
    reg = registry(args.cohorts)
    labels = metrics.load_labels(reg)
    board = _board_path(reg, args.board)
    configs = [arm for path in args.configs for arm in lab_board.expand(lab_board.load_config(path))]
    base_config = lab_board.load_config(args.base or REFERENCE)
    ok = verify.verified(_root(reg))
    fits = fit.recorded(_root(reg))
    for name in args.cohort:
        clock = time.perf_counter()
        cohort = open_cohort(name, args.cohorts, workers=args.workers)
        stamp = verify.stamp(cohort.version)
        print(f"[{name}] cache {cohort.version} ready in {time.perf_counter() - clock:.1f}s "
              f"({cohort.n} pairs){'' if stamp in ok else ' NOT VERIFIED'}", flush=True)
        base = lab_board.run(cohort, base_config)
        why = (metrics.why_summary(metrics.why_refused(cohort, base, labels))
               if args.why else None)
        metrics.append(board, metrics.row(cohort, base, None, labels, why,
                                          verified_state(stamp in ok, base, cohort.version, fits),
                                          stamp))
        for config in configs:
            if lab_board.config_id(config) == lab_board.config_id(base_config):
                continue
            outcome = lab_board.run(cohort, config)
            why = (metrics.why_summary(metrics.why_refused(cohort, outcome, labels))
                   if args.why else None)
            entry = metrics.row(cohort, outcome, base, labels, why,
                                verified_state(stamp in ok, outcome, cohort.version, fits), stamp)
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
    out.write_text(page.render(review, args.n, args.seed, m45=args.m45), encoding="utf-8")
    print(out)
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    reg = registry(args.cohorts)
    table = metrics.render(_board_path(reg, args.board), args.cohort or None)
    if args.out:
        Path(args.out).write_text(table, encoding="utf-8")
    print(table)
    return 0


def _rows_of(latest: dict[tuple[str, str, str], dict[str, Any]], experiment: str,
             like: dict[str, dict[str, Any]] | None = None, aliased: frozenset[str] = frozenset()
             ) -> dict[str, dict[str, Any]]:
    """The newest row per cohort of one experiment (by name); with `like`, the newest row that
    is comparable with `like`'s row for that cohort when there is one (`rules.comparable`)."""
    def rank(cohort: str, entry: dict[str, Any]) -> tuple[bool, str]:
        mate = (like or {}).get(cohort)
        return (mate is None or rules.comparable(mate, entry, cohort in aliased) is None,
                entry["at"])
    out: dict[str, dict[str, Any]] = {}
    for (cohort, name, _), entry in latest.items():
        if name == experiment and (cohort not in out
                                   or rank(cohort, entry) >= rank(cohort, out[cohort])):
            out[cohort] = entry
    return out


def _aliases(given: Sequence[str] | None) -> dict[str, str]:
    """`--alias c18=c18_w31r1`: the arm's rows on a sibling artefact of one export stand for
    that cohort's."""
    out: dict[str, str] = {}
    for item in given or ():
        cohort, _, sibling = item.partition("=")
        if not cohort or not sibling:
            raise SystemExit(f"--alias wants COHORT=SIBLING, got {item!r}")
        out[cohort] = sibling
    return out


def _arm_rows(latest: dict[tuple[str, str, str], dict[str, Any]], experiment: str,
              aliases: dict[str, str]) -> dict[str, dict[str, Any]]:
    rows = _rows_of(latest, experiment)
    siblings = set(aliases.values())
    out = {cohort: row for cohort, row in rows.items() if cohort not in siblings
           and cohort not in aliases}
    for cohort, sibling in aliases.items():
        if sibling in rows:
            out[cohort] = rows[sibling]
    return out


def cmd_keep(args: argparse.Namespace) -> int:
    reg = registry(args.cohorts)
    latest = metrics.latest_rows(_board_path(reg, args.board))
    aliases = _aliases(args.alias)
    arm = _arm_rows(latest, args.arm, aliases)
    if not arm:
        raise SystemExit(f"no leaderboard row for {args.arm}")
    incumbent = _rows_of(latest, args.incumbent, arm, frozenset(aliases))
    result = rules.keep(arm, incumbent, verify.verified(_root(reg)), args.validate,
                        accepted=args.accept or (), fixtures=metrics.load_fixtures(),
                        aliased=aliases)
    if args.readers_added is not None:
        result["rule_b"] = rules.readers(result, args.readers_added)
        result["stop_rules"] = rules.stop_rules(result["verdict"], result["rule_b"]["verdict"])
    print(json.dumps({"arm": args.arm, "incumbent": args.incumbent, "aliases": aliases,
                      "rule_names": rules.RULE_INCUMBENT,
                      "stands_for_it": args.incumbent != rules.RULE_INCUMBENT, **result},
                     indent=1))
    return 0


def cmd_cut(args: argparse.Namespace) -> int:
    reg = registry(args.cohorts)
    latest = metrics.latest_rows(_board_path(reg, args.board))
    sweep = {t: _rows_of(latest, f"{args.arm}[{args.param}={t}]") for t in rules.CUTS}
    like = next((rows for rows in sweep.values() if rows), None)
    incumbent = _rows_of(latest, args.incumbent, like)
    ok = verify.verified(_root(reg))
    result = rules.cut(sweep, incumbent, ok, args.validate, args.read)
    if result.get("cut") is not None:
        rows = sweep[result["cut"]]
        result["keep"] = rules.keep(rows, _rows_of(latest, args.incumbent, rows), ok,
                                    args.validate, accepted=args.accept or (),
                                    fixtures=metrics.load_fixtures())
    print(json.dumps({"arm": args.arm, "param": args.param, "incumbent": args.incumbent,
                      "rule_names": rules.RULE_INCUMBENT,
                      "stands_for_it": args.incumbent != rules.RULE_INCUMBENT, **result},
                     indent=1))
    return 0


def cmd_fit(args: argparse.Namespace) -> int:
    reg = registry(args.cohorts)
    labels = metrics.load_labels(reg)
    train = open_cohort(args.train, args.cohorts, workers=args.workers)
    others = (open_cohort(name, args.cohorts, workers=args.workers)
              for name in args.cohort or () if name != args.train)
    report = fit.fit(train, others, labels, Path(args.out), _root(reg))
    print(json.dumps(report, indent=1))
    return 0


def cmd_ground(args: argparse.Namespace) -> int:
    from autodedup.lab import ground

    reg = registry(args.cohorts)
    cache = (open_cohort(args.cohort, args.cohorts, workers=args.workers)
             if reg["cohorts"][args.cohort].get("run") else None)
    built = ground.build(reg, args.cohort, cache, args.settings, args.model)
    ground.write(args.out, built)
    print(str(built["meta"]))
    return 0


def cmd_mf_fit(args: argparse.Namespace) -> int:
    from autodedup.lab import mf_fit

    reg = registry(args.cohorts)
    grounds = []
    for item in args.ground:
        name, _, path = item.partition("=")
        if not name or not path:
            raise SystemExit(f"--ground wants NAME=PATH, got {item!r}")
        grounds.append(mf_fit.load_ground(name, path))
    mf_fit.check_seals(grounds, reg)
    report = mf_fit.sealed(grounds, args.score, args.source, args.calibration, Path(args.out))
    print(json.dumps(report, indent=1))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autodedup.lab")
    parser.add_argument("--cohorts", help="cohort registry JSON (or AUTODEDUP_LAB_COHORTS)")
    parser.add_argument("--workers", type=int, default=4)
    sub = parser.add_subparsers(dest="command", required=True)
    load = sub.add_parser("load", help="read a cohort's evidence artefact and print its timings")
    load.add_argument("cohort")
    load.set_defaults(fn=cmd_load)
    check = sub.add_parser("verify", help="the reference arm against the harness run it reads")
    check.add_argument("cohort")
    check.add_argument("--config", help="another arm (not recorded as a verify)")
    check.add_argument("--rungs", type=int, default=0,
                       help="rung-by-rung against the engine functions on N seeded pairs (-1 all)")
    check.add_argument("--seed", type=int, default=20260927)
    check.set_defaults(fn=cmd_verify)
    learn = sub.add_parser("fit", help="the challenger's GBM on the artefacts' own V/P and keys")
    learn.add_argument("--train", default="trial")
    learn.add_argument("--cohort", action="append", help="a cohort to score (repeat)")
    learn.add_argument("--out", required=True, help="directory for gbm_<cohort>.npz")
    learn.set_defaults(fn=cmd_fit)
    gr = sub.add_parser("ground", help="one cohort's labelled pairs as the challenger's training rows")
    gr.add_argument("cohort")
    gr.add_argument("--out", required=True, help="the ground file (.npz)")
    gr.add_argument("--settings", default="w31",
                    help="the settings row of a cohort with no cache (a cached one uses its own)")
    gr.add_argument("--model", default="w6_gold", help="likewise, the model row")
    gr.set_defaults(fn=cmd_ground)
    mf = sub.add_parser("mf-fit", help="the challenger's model files, sealed by town")
    mf.add_argument("--ground", action="append", required=True,
                    help="NAME=PATH of a `lab ground` file (repeat; the order is the training row "
                         "order)")
    mf.add_argument("--score", action="append", required=True, help="a cohort to write models for")
    mf.add_argument("--source", choices=("all", "judge"), default="all")
    mf.add_argument("--calibration", choices=("loco", "trial", "pooled"), default="loco",
                    help="loco: a map per scored cohort off the other grounds (default); trial / "
                         "pooled: G2's s6 / s5 maps, which read the scored cohort's labels")
    mf.add_argument("--out", required=True, help="directory for <source>/<cohort>/<town>.json")
    mf.set_defaults(fn=cmd_mf_fit)
    run = sub.add_parser("run")
    run.add_argument("configs", nargs="+")
    run.add_argument("--cohort", action="append", required=True)
    run.add_argument("--base")
    run.add_argument("--board")
    run.add_argument("--why", action="store_true", help="M8's `lost --why` summary in each row")
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
    review.add_argument("--m45", action="store_true",
                        help="M4/M5's page: 40 arm-only + 40 base-only groups, seed 20260927")
    review.set_defaults(fn=cmd_page)
    show = sub.add_parser("board")
    show.add_argument("--cohort", action="append")
    show.add_argument("--board")
    show.add_argument("--out")
    show.set_defaults(fn=cmd_board)
    for name, fn, text in (("keep", cmd_keep, "rule 6.1(a): KEEP or DROP an arm"),
                           ("cut", cmd_cut, "rule 6.1(a): the cut over a t_merge sweep")):
        cmd = sub.add_parser(name, help=text)
        cmd.add_argument("arm", help="the experiment name (for cut: the sweep's base name)")
        cmd.add_argument("--incumbent", required=True)
        cmd.add_argument("--board")
        cmd.add_argument("--validate", default=rules.VALIDATE)
        cmd.add_argument("--accept", action="append",
                         help="a fixture case the operator read as one property at an RP")
        if name == "keep":
            cmd.add_argument("--alias", action="append",
                             help="COHORT=SIBLING: the arm's rows on a sibling artefact of "
                                  "COHORT's export (another settings row) stand for COHORT's")
            cmd.add_argument("--readers-added", type=int,
                             help="rule 6.1 (b) for a challenger arm: readers or tolerances "
                                  "it adds back over MF-P14")
        if name == "cut":
            cmd.add_argument("--param", default="t_merge")
            cmd.add_argument("--read", default=rules.READ)
        cmd.set_defaults(fn=fn)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
