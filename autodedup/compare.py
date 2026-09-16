"""Compare judge arms against gold on the SAME pairs — `python3 -m autodedup.compare`.

The question this answers is the only one that matters before the vision step is handed to a
model this program rents instead of buys: over the pairs gold has already ruled on, how often
does each arm say the same thing, and what did each answer cost. Every arm is one
`judgements.jsonl` from a judge run (any tier), gold is the three-vote aggregate from a gold
run, and the comparison is restricted to their intersection — an arm judged on pairs gold never
saw contributes nothing but a `missing` count.

Two agreement numbers, deliberately:

  * FOUR-WAY, over the judge's own vocabulary, which is the label the program stores.
  * BINARY same / not-same, with `insufficient_evidence` EXCLUDED on either side and counted
    separately. Folding an abstention into "not the same property" is how a timid model scores
    as a careful one: a cheap arm can buy agreement simply by refusing to decide, so the
    abstention rate is reported beside the agreement it would otherwise inflate.

Both carry Cohen's kappa, because raw agreement over a corpus that is 90% `different_property`
is ~90% for a model that always says `different_property`.

Reads files only: no database, no network, no spend.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

VERDICTS: tuple[str, ...] = (
    "same_property",
    "different_property",
    "same_building_different_unit",
    "insufficient_evidence",
)
INSUFFICIENT: str = "insufficient_evidence"
BINARY: dict[str, str] = {
    "same_property": "same",
    "different_property": "not_same",
    "same_building_different_unit": "not_same",
}

REPORT_JSON: str = "compare.json"
REPORT_MD: str = "compare.md"


@dataclass(slots=True)
class Row:
    lo: int
    hi: int
    verdict: str
    tier: str | None = None
    model: str | None = None
    stratum: str | None = None
    confidence: float | None = None
    cost_usd: float | None = None
    latency_s: float | None = None
    aggregate: bool = False


Pairs = dict[tuple[int, int], Row]


# --- loading ---------------------------------------------------------------------------


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_row(record: dict[str, Any]) -> Row | None:
    """One judgements.jsonl line into a Row, or None when it carries no verdict.

    A gold pair whose plan came up short is written with `incomplete: true` and NO verdict
    (the lane refuses to call one surviving vote unanimous); those lines are skipped here for
    the same reason they were never stored."""
    verdict_obj = record.get("verdict")
    if not isinstance(verdict_obj, dict):
        return None
    verdict = str(verdict_obj.get("verdict") or "")
    if not verdict:
        return None
    try:
        lo, hi = int(record["lo"]), int(record["hi"])
    except (KeyError, TypeError, ValueError):
        return None
    return Row(
        lo=lo,
        hi=hi,
        verdict=verdict,
        tier=record.get("tier"),
        model=record.get("model"),
        stratum=record.get("stratum"),
        confidence=_float(verdict_obj.get("confidence")),
        cost_usd=_float(record.get("cost_usd")),
        latency_s=_float(record.get("latency_s")),
        aggregate="n_votes" in record,
    )


def load_rows(path: Path) -> Pairs:
    """Latest row per pair, except that a gold AGGREGATE always beats the individual votes it
    was built from — both are written to the same file under tier `gold`."""
    rows: Pairs = {}
    if not path.is_file():
        raise SystemExit(f"no judgements file at {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        row = parse_row(record)
        if row is None:
            continue
        key = (row.lo, row.hi)
        seen = rows.get(key)
        if seen is not None and seen.aggregate and not row.aggregate:
            continue
        rows[key] = row
    if not rows:
        raise SystemExit(f"{path} holds no verdicts")
    return rows


# --- statistics ------------------------------------------------------------------------


def agreement(pairs: Sequence[tuple[str, str]]) -> float | None:
    if not pairs:
        return None
    return sum(1 for left, right in pairs if left == right) / len(pairs)


def cohen_kappa(pairs: Sequence[tuple[str, str]]) -> float | None:
    """(po - pe) / (1 - pe) over the labels actually observed.

    Perfect agreement on a single label leaves pe = 1 and the ratio undefined: that is chance
    agreement of 100%, so kappa is reported as 0.0 — a one-label corpus proves nothing about
    an arm, and 1.0 would claim the opposite."""
    n = len(pairs)
    if not n:
        return None
    labels = {label for pair in pairs for label in pair}
    po = sum(1 for left, right in pairs if left == right) / n
    pe = 0.0
    for label in labels:
        left = sum(1 for a, _ in pairs if a == label) / n
        right = sum(1 for _, b in pairs if b == label) / n
        pe += left * right
    if pe >= 1.0:
        return 0.0
    return (po - pe) / (1.0 - pe)


def confusion(pairs: Sequence[tuple[str, str]]) -> dict[str, dict[str, int]]:
    matrix: dict[str, dict[str, int]] = {}
    for gold_label, arm_label in pairs:
        matrix.setdefault(gold_label, {})
        matrix[gold_label][arm_label] = matrix[gold_label].get(arm_label, 0) + 1
    return matrix


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


# --- one arm ---------------------------------------------------------------------------


def compare_arm(name: str, path: Path, arm: Pairs, gold: Pairs) -> dict[str, Any]:
    keys = [key for key in gold if key in arm]
    four_way = [(gold[key].verdict, arm[key].verdict) for key in keys]
    binary = [
        (BINARY[left], BINARY[right])
        for left, right in four_way
        if left in BINARY and right in BINARY
    ]

    strata: dict[str, list[tuple[str, str]]] = {}
    for key in keys:
        stratum = gold[key].stratum or arm[key].stratum or "(none)"
        strata.setdefault(stratum, []).append((gold[key].verdict, arm[key].verdict))

    confidences = [arm[key].confidence for key in keys if arm[key].confidence is not None]
    costs = [arm[key].cost_usd for key in keys if arm[key].cost_usd is not None]
    latencies = [arm[key].latency_s for key in keys if arm[key].latency_s is not None]
    models = sorted({arm[key].model for key in keys if arm[key].model})

    return {
        "name": name,
        "path": str(path),
        "models": models,
        "tier": next((arm[key].tier for key in keys if arm[key].tier), None),
        "n_gold": len(gold),
        "n_judged": len(arm),
        "n_compared": len(keys),
        "n_missing": len(gold) - len(keys),
        "four_way": {
            "n": len(four_way),
            "agreement": _round(agreement(four_way)),
            "kappa": _round(cohen_kappa(four_way)),
        },
        "binary": {
            "n": len(binary),
            "excluded_insufficient": len(four_way) - len(binary),
            "agreement": _round(agreement(binary)),
            "kappa": _round(cohen_kappa(binary)),
        },
        "confusion": confusion(four_way),
        "per_stratum": {
            stratum: {
                "n": len(rows),
                "agreement": _round(agreement(rows)),
            }
            for stratum, rows in sorted(strata.items())
        },
        "insufficient_rate": _round(
            sum(1 for _, right in four_way if right == INSUFFICIENT) / len(four_way)
            if four_way else None
        ),
        "gold_insufficient_rate": _round(
            sum(1 for left, _ in four_way if left == INSUFFICIENT) / len(four_way)
            if four_way else None
        ),
        "mean_confidence": _round(
            statistics.fmean(confidences) if confidences else None
        ),
        "cost": {
            "n_priced": len(costs),
            "total_usd": _round(sum(costs), 6) if costs else None,
            "per_pair_usd": _round(statistics.fmean(costs), 6) if costs else None,
        },
        "latency": {
            "n": len(latencies),
            "mean_s": _round(statistics.fmean(latencies), 2) if latencies else None,
            "p50_s": _round(_percentile(latencies, 0.50), 2),
            "p95_s": _round(_percentile(latencies, 0.95), 2),
        },
    }


def compare_arms(arms: dict[str, Pairs]) -> list[dict[str, Any]]:
    """Every arm against every other arm, on their own intersection — an arm can agree with
    gold as often as another and still disagree with it pair for pair."""
    out: list[dict[str, Any]] = []
    names = list(arms)
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            keys = [key for key in arms[left] if key in arms[right]]
            pairs = [(arms[left][key].verdict, arms[right][key].verdict) for key in keys]
            out.append({
                "a": left,
                "b": right,
                "n": len(pairs),
                "agreement": _round(agreement(pairs)),
                "kappa": _round(cohen_kappa(pairs)),
            })
    return out


def build_report(gold_path: Path, gold: Pairs,
                 arms: list[tuple[str, Path, Pairs]]) -> dict[str, Any]:
    return {
        "gold": {
            "path": str(gold_path),
            "n_pairs": len(gold),
            "verdicts": {
                verdict: sum(1 for row in gold.values() if row.verdict == verdict)
                for verdict in VERDICTS
                if any(row.verdict == verdict for row in gold.values())
            },
        },
        "arms": [compare_arm(name, path, rows, gold) for name, path, rows in arms],
        "between_arms": compare_arms({name: rows for name, _, rows in arms}),
    }


# --- markdown --------------------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _usd(value: Any) -> str:
    """Money gets five decimals, not the generic three. A rented pod's share is ~$0.0004 a pair
    and the paid vision arm ~$0.002: at three decimals a free arm and a cheap arm both render
    `0.000`, and the cost ratio this whole comparison exists to expose disappears from the one
    table the operator actually reads."""
    if value is None:
        return "–"
    return f"{float(value):.5f}"


def _table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows]
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    gold = report["gold"]
    lines = [
        "# Judge arms vs gold",
        "",
        f"Gold: `{gold['path']}` — {gold['n_pairs']} pair(s) "
        f"({', '.join(f'{name} {count}' for name, count in gold['verdicts'].items())}).",
        "",
        "## Agreement",
        "",
    ]
    lines += _table(
        ["arm", "model", "n", "missing", "4-way", "kappa", "binary", "kappa",
         "abstained", "insufficient", "conf", "$/pair", "s/pair"],
        [
            [
                arm["name"],
                ", ".join(arm["models"]) or "–",
                arm["n_compared"],
                arm["n_missing"],
                arm["four_way"]["agreement"],
                arm["four_way"]["kappa"],
                arm["binary"]["agreement"],
                arm["binary"]["kappa"],
                arm["binary"]["excluded_insufficient"],
                arm["insufficient_rate"],
                arm["mean_confidence"],
                _usd(arm["cost"]["per_pair_usd"]),
                arm["latency"]["mean_s"],
            ]
            for arm in report["arms"]
        ],
    )
    lines += [
        "",
        "`binary` excludes every pair either side called insufficient_evidence; "
        "`abstained` counts them.",
        "",
    ]

    for arm in report["arms"]:
        lines += [f"## {arm['name']}", "", "Confusion (rows = gold, columns = arm):", ""]
        columns = sorted({
            column for row in arm["confusion"].values() for column in row
        })
        lines += _table(
            ["gold \\ arm", *columns],
            [
                [gold_label, *[arm["confusion"][gold_label].get(col, 0) for col in columns]]
                for gold_label in sorted(arm["confusion"])
            ],
        )
        lines += ["", "Per stratum:", ""]
        lines += _table(
            ["stratum", "n", "4-way agreement"],
            [
                [stratum, stats["n"], stats["agreement"]]
                for stratum, stats in arm["per_stratum"].items()
            ],
        )
        lines += [""]

    if report["between_arms"]:
        lines += ["## Between arms", ""]
        lines += _table(
            ["a", "b", "n", "agreement", "kappa"],
            [
                [row["a"], row["b"], row["n"], row["agreement"], row["kappa"]]
                for row in report["between_arms"]
            ],
        )
        lines += [""]
    return "\n".join(lines)


# --- cli -------------------------------------------------------------------------------


def parse_arm(spec: str) -> tuple[str, Path]:
    name, _, path = spec.partition("=")
    if not name.strip() or not path.strip():
        raise SystemExit(f"--arm takes name=path, got {spec!r}")
    return name.strip(), Path(path.strip())


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m autodedup.compare",
        description="Agreement, cost and latency of judge arms against gold, same pairs.",
    )
    parser.add_argument("--gold", required=True, type=Path,
                        help="judgements.jsonl from a gold run")
    parser.add_argument("--arm", required=True, action="append", metavar="NAME=PATH",
                        help="an arm's judgements.jsonl; repeatable")
    parser.add_argument("--out", required=True, type=Path, help="report directory")
    args = parser.parse_args(argv)

    gold = load_rows(args.gold)
    arms: list[tuple[str, Path, Pairs]] = []
    for spec in args.arm:
        name, path = parse_arm(spec)
        arms.append((name, path, load_rows(path)))

    report = build_report(args.gold, gold, arms)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / REPORT_JSON).write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    markdown = render_markdown(report)
    (out_dir / REPORT_MD).write_text(markdown, encoding="utf-8")
    print(markdown)

    if any(arm["n_compared"] == 0 for arm in report["arms"]):
        empty = [arm["name"] for arm in report["arms"] if arm["n_compared"] == 0]
        # An arm that shares no pair with gold is not a result, it is a mis-wired comparison:
        # the report is written either way, but the run says so.
        print(f"no pair in common with gold: {', '.join(empty)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
