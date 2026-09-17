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

`--gold-votes` answers a different question off the same files: the gold tier casts THREE
votes per pair — two from the paid vision model and one from a rented open-weights arm — and
stores their aggregate. That mode reads the individual votes instead, so the rented arm can be
scored against the paid one it was bought to be independent of, with the paid model's own
two-vote disagreement as the baseline any arm has to beat.

Reads files only: no database, no network, no spend.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from autodedup.evaluate import wilson_interval

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


# --- gold votes ------------------------------------------------------------------------
#
# The gold tier writes FOUR rows per pair: three individual votes and the aggregate built from
# them. `load_rows` above deliberately collapses that to the aggregate, because a label is what
# the fit reads. This section reads the votes instead, to answer the one question the aggregate
# hides: how good was the rented open-weights arm, on its own, against the paid one.
#
# Every agreement is reported under TWO views, because a verdict passes through one more rule
# after the model has spoken. `parse_verdict` downgrades a non-same verdict that named no
# `unit_discriminator` to `insufficient_evidence` (E27), and records what it was in
# `downgraded_from`. An arm that judges well but fills the schema badly therefore lands in the
# jsonl as an abstainer. `stored` is the label the lane kept — the only one the program has ever
# acted on. `raw` is `downgraded_from or verdict` — what the model actually said. Reporting only
# the first calls a formatting defect a reasoning defect; reporting only the second credits an
# arm for a verdict the lane refused to use.

GOLD_VOTES_JSON: str = "gold_votes.json"
GOLD_VOTES_MD: str = "gold_votes.md"
SUMMARY_NAME: str = "summary.json"
VIEWS: tuple[str, ...] = ("stored", "raw")
MISSING_DISCRIMINATOR: str = "no discriminator named for a non-same verdict"
ERROR_RE = re.compile(
    r"^(?P<lo>\d+)x(?P<hi>\d+)\s+(?P<tier>[^/\s]+)/(?P<model>\S+?):\s+"
    r"(?P<kind>\w+):\s*(?P<message>.*)$"
)
MESSAGE_CLIP: int = 70


@dataclass(slots=True)
class GoldVote:
    model: str
    strategy: str | None
    weaker: bool
    verdict: str
    raw_verdict: str
    downgraded_from: str | None
    no_discriminator: bool
    confidence: float | None
    cost_usd: float | None
    latency_s: float | None

    def label(self, view: str) -> str:
        return self.raw_verdict if view == "raw" else self.verdict


@dataclass(slots=True)
class GoldPair:
    lo: int
    hi: int
    stratum: str
    votes: list[GoldVote]
    aggregate: str | None = None
    aggregate_cost_usd: float | None = None
    incomplete: bool = False


def load_gold_pairs(path: Path) -> list[GoldPair]:
    """Every gold pair in one judgements.jsonl, votes kept apart from the aggregate.

    Vote rows arrive in plan order (G1, G2, then the third vote), which is the order this keeps:
    `primary_votes(...)[0]` is the matched-first vote and `[1]` the sequence-first one."""
    if not path.is_file():
        raise SystemExit(f"no judgements file at {path}")
    pairs: dict[tuple[int, int], GoldPair] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        try:
            key = (int(record["lo"]), int(record["hi"]))
        except (KeyError, TypeError, ValueError):
            continue
        pair = pairs.setdefault(
            key, GoldPair(key[0], key[1], str(record.get("stratum") or "(none)"), [])
        )
        verdict_obj = record.get("verdict")
        verdict_obj = verdict_obj if isinstance(verdict_obj, dict) else {}
        verdict = str(verdict_obj.get("verdict") or "")
        if record.get("n_votes") is not None:
            # The aggregate row. A short plan writes it with `incomplete` and no verdict at all,
            # so an aborted run's partial pairs are visible as such instead of silently absent.
            pair.aggregate_cost_usd = _float(record.get("cost_usd"))
            pair.incomplete = bool(record.get("incomplete"))
            pair.aggregate = verdict or None
            continue
        if not verdict:
            continue
        downgraded = verdict_obj.get("downgraded_from") or None
        pair.votes.append(GoldVote(
            model=str(record.get("model") or "(unnamed)"),
            strategy=record.get("strategy"),
            weaker=bool(record.get("weaker")),
            verdict=verdict,
            raw_verdict=str(downgraded) if downgraded else verdict,
            downgraded_from=str(downgraded) if downgraded else None,
            no_discriminator=(
                str(verdict_obj.get("unit_discriminator") or "") == MISSING_DISCRIMINATOR
            ),
            confidence=_float(verdict_obj.get("confidence")),
            cost_usd=_float(record.get("cost_usd")),
            latency_s=_float(record.get("latency_s")),
        ))
    if not pairs:
        raise SystemExit(f"{path} holds no gold votes")
    return [pairs[key] for key in sorted(pairs)]


def load_run_summary(path: Path) -> dict[str, Any]:
    """The `summary.json` beside a judgements file, or {}. It carries the two facts the jsonl
    cannot: calls that were BILLED and then failed to parse (they leave no row), and the error
    strings that say why the third arm fell back."""
    candidate = path.parent / SUMMARY_NAME
    if not candidate.is_file():
        return {}
    try:
        loaded = json.loads(candidate.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    result = loaded.get("result")
    return result if isinstance(result, dict) else {}


def resolve_arms(pairs: Sequence[GoldPair]) -> tuple[str, list[str]]:
    """Primary = the model that casts the most non-weaker votes (gold takes two of them);
    every other model is a secondary arm. Named by the data, never hardcoded to one vendor."""
    counts = Counter(
        vote.model for pair in pairs for vote in pair.votes if not vote.weaker
    )
    if not counts:
        raise SystemExit("gold file holds no non-weaker votes")
    primary = counts.most_common(1)[0][0]
    return primary, sorted(model for model in counts if model != primary)


def primary_votes(pair: GoldPair, primary: str) -> list[GoldVote]:
    return [v for v in pair.votes if v.model == primary and not v.weaker]


def secondary_vote(pair: GoldPair, primary: str) -> GoldVote | None:
    return next((v for v in pair.votes if v.model != primary and not v.weaker), None)


def fallback_votes(pair: GoldPair) -> list[GoldVote]:
    return [v for v in pair.votes if v.weaker]


def covered_pairs(pairs: Sequence[GoldPair], primary: str) -> list[GoldPair]:
    """Pairs the comparison can use: two primary votes AND a secondary vote."""
    return [
        pair for pair in pairs
        if len(primary_votes(pair, primary)) == 2
        and secondary_vote(pair, primary) is not None
    ]


# --- gold-vote statistics ----------------------------------------------------------------


def rate(k: int, n: int) -> dict[str, Any]:
    low, high = wilson_interval(k, n) if n else (None, None)
    return {
        "k": k,
        "n": n,
        "rate": _round(k / n) if n else None,
        "ci95": [_round(low), _round(high)] if n else None,
    }


def _agreement_entry(pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    entry = rate(sum(1 for left, right in pairs if left == right), len(pairs))
    entry["agreement"] = entry.pop("rate")
    entry["kappa"] = _round(cohen_kappa(pairs))
    return entry


def agreement_stats(pairs: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """Four-way and binary, the binary one dropping every pair either side abstained on —
    the same exclusion `compare_arm` makes, for the same reason."""
    binary = [
        (BINARY[left], BINARY[right])
        for left, right in pairs
        if left in BINARY and right in BINARY
    ]
    binary_entry = _agreement_entry(binary)
    binary_entry["excluded_insufficient"] = len(pairs) - len(binary)
    return {
        "four_way": _agreement_entry(pairs),
        "binary": binary_entry,
        "confusion": confusion(pairs),
    }


def verdict_mix(verdicts: Sequence[str]) -> dict[str, Any]:
    counts = Counter(verdicts)
    n = len(verdicts)
    return {
        "n": n,
        "counts": {verdict: counts.get(verdict, 0) for verdict in VERDICTS},
        "same_property": rate(counts.get("same_property", 0), n),
        "insufficient": rate(counts.get(INSUFFICIENT, 0), n),
        "mean_confidence": None,
    }


def paired_bias(pairs: Sequence[tuple[str, str]], label: str) -> dict[str, Any]:
    """McNemar's discordant counts for one label: of the pairs where exactly one side used it,
    how often was that side the arm. 0.5 inside the interval = no measurable lean; a lower bound
    above 0.5 is the arm reaching for `label` where the reference does not."""
    arm_only = sum(1 for ref, arm in pairs if arm == label and ref != label)
    ref_only = sum(1 for ref, arm in pairs if ref == label and arm != label)
    out = rate(arm_only, arm_only + ref_only)
    out["label"] = label
    out["arm_only"] = arm_only
    out["reference_only"] = ref_only
    out["share_arm"] = out.pop("rate")
    return out


def two_vote_aggregate(verdicts: Sequence[str]) -> str:
    """`judge.aggregate_gold` on two votes: unanimous wins, anything else is no majority."""
    return verdicts[0] if len(set(verdicts)) == 1 else INSUFFICIENT


def _stats(values: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "mean": _round(statistics.fmean(values), 6) if values else None,
        "p50": _round(_percentile(values, 0.50), 6),
        "p95": _round(_percentile(values, 0.95), 6),
        "total": _round(sum(values), 6) if values else None,
    }


def view_block(pairs: Sequence[GoldPair], primary: str, secondary: str | None,
               view: str) -> dict[str, Any]:
    """(a), (b) and (c) of the gold-vote question, under one view of the verdicts."""
    covered = covered_pairs(pairs, primary)
    unanimous: list[tuple[str, str]] = []
    split = 0
    split_matched = 0
    for pair in covered:
        first, second = primary_votes(pair, primary)
        arm = secondary_vote(pair, primary)
        assert arm is not None
        left, right, mine = first.label(view), second.label(view), arm.label(view)
        if left == right:
            unanimous.append((left, mine))
        else:
            split += 1
            split_matched += int(mine in {left, right})

    same = "same_property"
    true_positive = sum(1 for ref, arm in unanimous if ref == same and arm == same)
    arm_positive = sum(1 for _, arm in unanimous if arm == same)
    reference_positive = sum(1 for ref, _ in unanimous if ref == same)

    vs_aggregate = [
        (pair.aggregate, arm.label(view))
        for pair in covered
        if pair.aggregate and (arm := secondary_vote(pair, primary)) is not None
    ]
    inter_rater = [
        (votes[0].label(view), votes[1].label(view))
        for pair in pairs
        if len(votes := primary_votes(pair, primary)) == 2
    ]
    inter_rater_shared = [
        (primary_votes(pair, primary)[0].label(view),
         primary_votes(pair, primary)[1].label(view))
        for pair in covered
    ]

    first_verdicts = [primary_votes(pair, primary)[0].label(view) for pair in covered]
    second_verdicts = [primary_votes(pair, primary)[1].label(view) for pair in covered]
    arm_verdicts = [
        arm.label(view) for pair in covered
        if (arm := secondary_vote(pair, primary)) is not None
    ]
    confidences = {
        primary: [
            vote.confidence for pair in covered for vote in primary_votes(pair, primary)
            if vote.confidence is not None
        ],
        secondary: [
            arm.confidence for pair in covered
            if (arm := secondary_vote(pair, primary)) is not None
            and arm.confidence is not None
        ],
    }
    mixes: dict[str, dict[str, Any]] = {}
    for name, model, verdicts in (
        (f"{primary} (vote 1, matched_first)", primary, first_verdicts),
        (f"{primary} (vote 2, sequence_first)", primary, second_verdicts),
        (f"{primary} (both votes pooled)", primary, first_verdicts + second_verdicts),
        *([(secondary, secondary, arm_verdicts)] if secondary else []),
    ):
        mix = verdict_mix(verdicts)
        values = confidences.get(model) or []
        mix["mean_confidence"] = _round(statistics.fmean(values), 3) if values else None
        mixes[name] = mix

    return {
        "a_secondary_vs_primary_majority": {
            "subset": "pairs with two primary votes AND a secondary vote",
            "n": len(covered),
            "primary_unanimous": {"n": len(unanimous), **agreement_stats(unanimous)},
            "primary_split": {
                "n": split,
                "secondary_matched_one_vote": rate(split_matched, split),
            },
            # The band judge's whole job is to say "merge these two" and never be wrong about
            # it. Precision here is that: of the pairs this arm called same_property, how many
            # did the paid pair also call same_property. Recall is what it would cost — the
            # duplicates it would leave in Browse.
            "secondary_same_property": {
                "precision": rate(true_positive, arm_positive),
                "recall": rate(true_positive, reference_positive),
            },
        },
        "b_secondary_vs_aggregate": {
            "note": "the STORED aggregate contains this vote — agreement here is inflated",
            **agreement_stats(vs_aggregate),
        },
        "c_primary_vs_primary": {
            "all_pairs": {"n": len(inter_rater), **agreement_stats(inter_rater)},
            "shared_subset": {
                "n": len(inter_rater_shared),
                **agreement_stats(inter_rater_shared),
            },
        },
        "d_verdict_mix": {
            "subset_n": len(covered),
            "arms": mixes,
            "paired_same_bias": paired_bias(unanimous, "same_property"),
            "paired_insufficient_bias": paired_bias(unanimous, INSUFFICIENT),
            "paired_note": "reference = the unanimous primary label, arm = the secondary vote",
        },
    }


def aggregate_influence(pairs: Sequence[GoldPair], primary: str) -> dict[str, Any]:
    """What the third vote actually bought: how often the STORED gold label differs from the
    two paid votes on their own. This is the only place the secondary arm touched the program."""
    changed: list[dict[str, Any]] = []
    seen = 0
    for pair in covered_pairs(pairs, primary):
        if not pair.aggregate:
            continue
        seen += 1
        without = two_vote_aggregate([v.verdict for v in primary_votes(pair, primary)])
        if without != pair.aggregate:
            changed.append({
                "lo": pair.lo, "hi": pair.hi, "stratum": pair.stratum,
                "without_secondary": without, "stored": pair.aggregate,
            })
    return {
        "n": seen,
        "changed": rate(len(changed), seen),
        "transitions": dict(Counter(
            f"{row['without_secondary']} -> {row['stored']}" for row in changed
        )),
        "examples": changed[:10],
    }


def downgrade_report(pairs: Sequence[GoldPair]) -> dict[str, dict[str, Any]]:
    """Per model: how often E27's caller-side downgrade rewrote the verdict, and how often the
    schema field that triggers it was left empty. A high number here is a PROMPT finding."""
    out: dict[str, dict[str, Any]] = {}
    by_model: dict[str, list[GoldVote]] = {}
    for pair in pairs:
        for vote in pair.votes:
            by_model.setdefault(vote.model, []).append(vote)
    for model, votes in sorted(by_model.items()):
        out[model] = {
            "votes": len(votes),
            "downgraded": rate(sum(1 for v in votes if v.downgraded_from), len(votes)),
            "no_unit_discriminator": rate(
                sum(1 for v in votes if v.no_discriminator), len(votes)
            ),
            "transitions": dict(Counter(
                f"{v.downgraded_from} -> {v.verdict}" for v in votes if v.downgraded_from
            )),
        }
    return out


def cost_by_model(pairs: Sequence[GoldPair]) -> dict[str, dict[str, Any]]:
    costs: dict[str, list[float]] = {}
    latencies: dict[str, list[float]] = {}
    counts: Counter[str] = Counter()
    weaker: Counter[str] = Counter()
    for pair in pairs:
        for vote in pair.votes:
            counts[vote.model] += 1
            weaker[vote.model] += int(vote.weaker)
            if vote.cost_usd is not None:
                costs.setdefault(vote.model, []).append(vote.cost_usd)
            if vote.latency_s is not None:
                latencies.setdefault(vote.model, []).append(vote.latency_s)
    return {
        model: {
            "votes": counts[model],
            "weaker_votes": weaker[model],
            "cost_usd": _stats(costs.get(model, [])),
            "latency_s": _stats(latencies.get(model, [])),
        }
        for model in sorted(counts)
    }


def fallback_reasons(summaries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The `errors` strings a run kept, grouped. The lane caps that list, so this reports what
    the cap let through beside the run's own `errors_total` — never a share of the total."""
    grouped: Counter[tuple[str, str, str]] = Counter()
    for summary in summaries:
        for error in summary.get("errors") or []:
            match = ERROR_RE.match(str(error))
            if match is None:
                grouped[("(unparsed)", "(unparsed)", str(error)[:MESSAGE_CLIP])] += 1
                continue
            grouped[(match.group("model"), match.group("kind"),
                     match.group("message")[:MESSAGE_CLIP])] += 1
    return [
        {"model": model, "kind": kind, "message": message, "n_listed": count}
        for (model, kind, message), count in sorted(
            grouped.items(), key=lambda item: (-item[1], item[0])
        )
    ]


def _file_row(path: Path, pairs: Sequence[GoldPair], summary: dict[str, Any],
              primary: str, secondary: str | None) -> dict[str, Any]:
    covered = covered_pairs(pairs, primary)
    row: dict[str, Any] = {
        "path": str(path),
        "run": path.parent.name or str(path),
        "n_pairs": len(pairs),
        "n_votes": sum(len(pair.votes) for pair in pairs),
        "n_with_secondary": len(covered),
        "n_fallback": sum(1 for pair in pairs if fallback_votes(pair)),
        "n_incomplete": sum(1 for pair in pairs if pair.incomplete),
        "billed_unparsed": summary.get("billed_unparsed"),
        "errors_total": summary.get("errors_total"),
        "arm_disabled_reason": summary.get("qwen_arm_disabled_reason"),
        "arm_disabled_at_pair": summary.get("qwen_arm_disabled_at_pair"),
        "views": {},
    }
    for view in VIEWS:
        block = view_block(pairs, primary, secondary, view)
        unanimous = block["a_secondary_vs_primary_majority"]["primary_unanimous"]
        arm_mix = block["d_verdict_mix"]["arms"].get(secondary or "", {})
        row["views"][view] = {
            "binary": unanimous["binary"],
            "four_way": unanimous["four_way"],
            "insufficient": arm_mix.get("insufficient"),
            "same_property": arm_mix.get("same_property"),
        }
    return row


def build_gold_votes_report(
    files: Sequence[tuple[Path, list[GoldPair], dict[str, Any]]],
) -> dict[str, Any]:
    pairs = [pair for _, run_pairs, _ in files for pair in run_pairs]
    primary, secondaries = resolve_arms(pairs)
    secondary = secondaries[0] if secondaries else None
    covered = covered_pairs(pairs, primary)

    costs = cost_by_model(pairs)
    primary_cost = costs.get(primary, {}).get("cost_usd", {}).get("mean")
    secondary_cost = (
        costs.get(secondary, {}).get("cost_usd", {}).get("mean") if secondary else None
    )
    billed_unparsed = sum(int(summary.get("billed_unparsed") or 0) for _, _, summary in files)
    n_secondary_votes = costs.get(secondary, {}).get("votes", 0) if secondary else 0
    effective = None
    if secondary_cost is not None and n_secondary_votes:
        # Every unparsed response was paid for and left no row. Charging those wasted calls at
        # the arm's own mean price is the honest per-USABLE-vote number; it is true only of runs
        # whose failures were all this arm's, hence the note the report carries beside it.
        effective = _round(
            secondary_cost * (n_secondary_votes + billed_unparsed) / n_secondary_votes, 6
        )
    fallback_pairs = [pair for pair in pairs if fallback_votes(pair)]

    return {
        "arms": {"primary": primary, "secondary": secondaries},
        "views": {view: view_block(pairs, primary, secondary, view) for view in VIEWS},
        "view_note": (
            "stored = the label the lane kept; raw = downgraded_from or verdict, what the "
            "model said before E27's missing-unit_discriminator downgrade"
        ),
        "b2_aggregate_influence": aggregate_influence(pairs, primary),
        "downgrades": downgrade_report(pairs),
        "totals": {
            "n_pairs": len(pairs),
            "n_with_secondary": len(covered),
            "n_fallback_pairs": len(fallback_pairs),
            "n_incomplete": sum(1 for pair in pairs if pair.incomplete),
        },
        "files": [
            _file_row(path, run_pairs, summary, primary, secondary)
            for path, run_pairs, summary in files
        ],
        "e_cost": {
            "per_model": costs,
            "ratio_secondary_over_primary": (
                _round(secondary_cost / primary_cost, 4)
                if secondary_cost and primary_cost else None
            ),
            "billed_unparsed_calls": billed_unparsed,
            "effective_per_usable_secondary_vote_usd": effective,
            "effective_note": (
                "charges every billed-but-unparsed call in these runs to the secondary arm"
            ),
        },
        "f_fallback": {
            "n_pairs": len(pairs),
            "fallback": rate(len(fallback_pairs), len(pairs)),
            "reasons_listed": fallback_reasons([summary for _, _, summary in files]),
            "reasons_note": (
                "the lane caps the stored error list; `errors_total` per file is the true count"
            ),
        },
    }


# --- gold-vote markdown --------------------------------------------------------------------


def _pct(value: Any) -> str:
    return "–" if value is None else f"{float(value) * 100:.2f}%"


def _ci(entry: dict[str, Any] | None) -> str:
    if not entry or not entry.get("ci95"):
        return "–"
    low, high = entry["ci95"]
    return f"{_pct(low)}–{_pct(high)}"


def _agreement_rows(report: dict[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for view in VIEWS:
        block = report["views"][view]
        unanimous = block["a_secondary_vs_primary_majority"]["primary_unanimous"]
        aggregate = block["b_secondary_vs_aggregate"]
        shared = block["c_primary_vs_primary"]["shared_subset"]
        for label, entry in (
            ("(a) secondary vs unanimous primary", unanimous),
            ("(b) secondary vs gold aggregate", aggregate),
            ("(c) primary vote 1 vs vote 2", shared),
        ):
            for kind in ("four_way", "binary"):
                cell = entry[kind]
                rows.append([
                    f"{label} — {kind.replace('_', '-')}", view, cell["n"],
                    _pct(cell["agreement"]), _ci(cell), cell["kappa"],
                ])
    return rows


def render_gold_votes_markdown(report: dict[str, Any]) -> str:
    arms = report["arms"]
    primary = arms["primary"]
    secondary = arms["secondary"][0] if arms["secondary"] else "–"
    totals = report["totals"]
    influence = report["b2_aggregate_influence"]
    same_block = (report["views"]["stored"]["a_secondary_vs_primary_majority"]
                  ["secondary_same_property"])
    same_precision = same_block["precision"]
    same_recall = same_block["recall"]
    cost = report["e_cost"]
    fallback = report["f_fallback"]

    lines = [
        f"# Gold votes — `{secondary}` against `{primary}`",
        "",
        f"{totals['n_pairs']} gold pair(s) over {len(report['files'])} run(s); "
        f"{totals['n_with_secondary']} carry a secondary vote, "
        f"{totals['n_fallback_pairs']} fell back to a weaker {primary} vote.",
        "",
        f"Views: {report['view_note']}.",
        "",
        "## Agreement",
        "",
    ]
    lines += _table(
        ["question", "view", "n", "agreement", "95% CI", "kappa"], _agreement_rows(report)
    )
    lines += [
        "",
        "`binary` folds `same_building_different_unit` into not-same and drops every pair "
        "either side abstained on.",
        "",
        f"(b) is inflated — {report['views']['stored']['b_secondary_vs_aggregate']['note']}. "
        f"The secondary vote moved the stored gold label on {influence['changed']['k']} of "
        f"{influence['changed']['n']} pair(s) ({_pct(influence['changed']['rate'])}): "
        f"{', '.join(f'{k} ×{v}' for k, v in influence['transitions'].items()) or 'none'}.",
        "",
        f"When `{secondary}` says same_property (stored view) the unanimous paid pair agrees "
        f"{_pct(same_precision['rate'])} of the time "
        f"({same_precision['k']}/{same_precision['n']}, CI {_ci(same_precision)}); it finds "
        f"{_pct(same_recall['rate'])} of the same_property pairs the paid pair agreed on "
        f"({same_recall['k']}/{same_recall['n']}, CI {_ci(same_recall)}).",
        "",
        "## Per run",
        "",
    ]
    lines += _table(
        ["run", "pairs", "with secondary", "fallback", "(a) binary stored", "n",
         "(a) binary raw", "n", "abstain stored", "abstain raw"],
        [
            [
                row["run"], row["n_pairs"], row["n_with_secondary"], row["n_fallback"],
                _pct(row["views"]["stored"]["binary"]["agreement"]),
                row["views"]["stored"]["binary"]["n"],
                _pct(row["views"]["raw"]["binary"]["agreement"]),
                row["views"]["raw"]["binary"]["n"],
                _pct((row["views"]["stored"]["insufficient"] or {}).get("rate")),
                _pct((row["views"]["raw"]["insufficient"] or {}).get("rate")),
            ]
            for row in report["files"]
        ],
    )
    lines += ["", "## Schema downgrades (E27)", ""]
    lines += _table(
        ["model", "votes", "downgraded", "no unit_discriminator", "transitions"],
        [
            [
                model, stats["votes"],
                f"{stats['downgraded']['k']} ({_pct(stats['downgraded']['rate'])})",
                f"{stats['no_unit_discriminator']['k']} "
                f"({_pct(stats['no_unit_discriminator']['rate'])})",
                ", ".join(f"{k} ×{v}" for k, v in stats["transitions"].items()) or "–",
            ]
            for model, stats in report["downgrades"].items()
        ],
    )

    for view in VIEWS:
        mix_block = report["views"][view]["d_verdict_mix"]
        lines += ["", f"## (d) Verdict mix and abstention — {view}", ""]
        lines += _table(
            ["arm", "n", "same", "diff", "same_bldg", "insufficient", "same %", "insuff %",
             "conf"],
            [
                [
                    name, mix["n"],
                    mix["counts"]["same_property"],
                    mix["counts"]["different_property"],
                    mix["counts"]["same_building_different_unit"],
                    mix["counts"][INSUFFICIENT],
                    _pct(mix["same_property"]["rate"]),
                    _pct(mix["insufficient"]["rate"]),
                    mix["mean_confidence"],
                ]
                for name, mix in mix_block["arms"].items()
            ],
        )
        same_bias = mix_block["paired_same_bias"]
        none_bias = mix_block["paired_insufficient_bias"]
        lines += [
            "",
            f"Paired lean ({mix_block['paired_note']}): `same_property` on "
            f"{same_bias['arm_only']} pair(s) the primary did not, against "
            f"{same_bias['reference_only']} the other way "
            f"({_pct(same_bias['share_arm'])} of the discordant, CI {_ci(same_bias)}); "
            f"`insufficient_evidence` {none_bias['arm_only']} vs {none_bias['reference_only']} "
            f"({_pct(none_bias['share_arm'])}, CI {_ci(none_bias)}).",
        ]

    lines += ["", "## (e) Cost and latency per vote", ""]
    lines += _table(
        ["model", "votes", "weaker", "$/vote", "$ total", "mean s", "p95 s"],
        [
            [
                model, stats["votes"], stats["weaker_votes"],
                _usd(stats["cost_usd"]["mean"]), _usd(stats["cost_usd"]["total"]),
                stats["latency_s"]["mean"], stats["latency_s"]["p95"],
            ]
            for model, stats in cost["per_model"].items()
        ],
    )
    lines += [
        "",
        f"Price ratio secondary / primary: {_cell(cost['ratio_secondary_over_primary'])}. "
        f"{cost['billed_unparsed_calls']} billed-but-unparsed call(s) leave no row; charged to "
        f"the secondary arm that is "
        f"{_usd(cost['effective_per_usable_secondary_vote_usd'])} per usable vote.",
        "",
        "## (f) Fallback to the weaker vote",
        "",
        f"{fallback['fallback']['k']} of {fallback['fallback']['n']} pair(s) "
        f"({_pct(fallback['fallback']['rate'])}, CI {_ci(fallback['fallback'])}) took a third "
        f"{primary} vote instead of the secondary arm. {fallback['reasons_note']}.",
        "",
    ]
    lines += _table(
        ["run", "pairs", "with secondary", "fallback", "billed unparsed", "errors", "arm down"],
        [
            [
                row["run"], row["n_pairs"], row["n_with_secondary"], row["n_fallback"],
                row["billed_unparsed"], row["errors_total"], row["arm_disabled_reason"] or "no",
            ]
            for row in report["files"]
        ],
    )
    lines += ["", "Reasons, as far as the stored error list reaches:", ""]
    lines += _table(
        ["model", "kind", "message", "listed"],
        [[row["model"], row["kind"], row["message"], row["n_listed"]]
         for row in fallback["reasons_listed"]] or [["–", "–", "none stored", 0]],
    )

    for view in VIEWS:
        unanimous = (report["views"][view]["a_secondary_vs_primary_majority"]
                     ["primary_unanimous"])
        lines += ["", f"## Confusion — secondary vs unanimous primary, {view} "
                      "(rows = primary)", ""]
        columns = sorted({col for row in unanimous["confusion"].values() for col in row})
        lines += _table(
            ["primary \\ secondary", *columns],
            [
                [label, *[unanimous["confusion"][label].get(col, 0) for col in columns]]
                for label in sorted(unanimous["confusion"])
            ],
        )
    lines += [""]
    return "\n".join(lines)


def run_gold_votes(paths: Sequence[Path], out_dir: Path) -> int:
    files = [(path, load_gold_pairs(path), load_run_summary(path)) for path in paths]
    report = build_gold_votes_report(files)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / GOLD_VOTES_JSON).write_text(
        json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
    )
    markdown = render_gold_votes_markdown(report)
    (out_dir / GOLD_VOTES_MD).write_text(markdown, encoding="utf-8")
    print(markdown)
    if not report["arms"]["secondary"]:
        print("gold file holds one arm only: nothing to compare", file=sys.stderr)
        return 1
    return 0


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
    parser.add_argument("--gold", required=True, type=Path, action="append",
                        help="judgements.jsonl from a gold run; repeatable with --gold-votes")
    parser.add_argument("--arm", action="append", metavar="NAME=PATH",
                        help="an arm's judgements.jsonl; repeatable")
    parser.add_argument("--gold-votes", action="store_true",
                        help="report the gold tier's own arms against each other instead")
    parser.add_argument("--out", required=True, type=Path, help="report directory")
    args = parser.parse_args(argv)

    if args.gold_votes:
        return run_gold_votes(args.gold, Path(args.out))
    if not args.arm:
        parser.error("--arm is required unless --gold-votes is given")
    if len(args.gold) != 1:
        parser.error("arm comparison takes exactly one --gold")
    gold_path = args.gold[0]

    gold = load_rows(gold_path)
    arms: list[tuple[str, Path, Pairs]] = []
    for spec in args.arm:
        name, path = parse_arm(spec)
        arms.append((name, path, load_rows(path)))

    report = build_report(gold_path, gold, arms)
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
