"""Ensemble rules over verdicts that are already stored — decisions, not calls.

W6 ruled that no single judge arm may be given merge authority on the band. The obvious next
question is whether a COMBINATION may be, and the honest way to ask it is for free: four arms
judged the same pairs, so every rule that reads their four verdicts can be scored without one
extra token. Nothing here calls a model.

A rule is a sequence of STAGES. Every arm in a stage must say `same_property` for the rule to
reach the next one; the rule merges when the last stage passes. Stage 0 is always paid for;
stage k is paid only on the pairs stages 0..k-1 let through — which is the whole point of a
cascade and the reason its cost cannot be read off a per-arm price list. `veto_arms` are
consulted in stage 0 (so they are always paid) and refuse the merge when ANY of them abstains
or calls the pair `same_building_different_unit`: the two verdicts that mean "I am looking at a
development", which is the shape the operator's standing ruling protects.

Coverage is explicit. A pair whose consulted arm has no verdict is NOT covered, and is dropped
from that rule's denominators rather than scored as a no-merge — an arm that died mid-pass
would otherwise look like a safety feature.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, permutations
from typing import Iterable, Mapping, Sequence

MERGE: str = "same_property"
ABSTAIN: str = "insufficient_evidence"
SAME_BUILDING: str = "same_building_different_unit"
VETO_VERDICTS: frozenset[str] = frozenset({ABSTAIN, SAME_BUILDING})
VETO_SUFFIX: str = "|veto"


@dataclass(frozen=True, slots=True)
class ArmRow:
    """One arm's answer on one pair, with what it cost to get it."""

    verdict: str
    cost_usd: float | None = None
    latency_s: float | None = None


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    kind: str
    stages: tuple[tuple[str, ...], ...]
    veto_arms: tuple[str, ...] = ()

    @property
    def arms(self) -> tuple[str, ...]:
        seen: list[str] = []
        for stage in self.stages:
            for arm in stage:
                if arm not in seen:
                    seen.append(arm)
        for arm in self.veto_arms:
            if arm not in seen:
                seen.append(arm)
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class Decision:
    covered: bool
    merge: bool
    consulted: tuple[str, ...]
    cost_usd: float
    latency_s: float
    priced: bool


def decide(rule: Rule, rows: Mapping[str, ArmRow]) -> Decision:
    """One rule on one pair: does it merge, which arms did it have to pay for, how long."""
    consulted: list[str] = []
    cost = 0.0
    priced = True
    latency = 0.0
    merge = True
    for index, stage in enumerate(rule.stages):
        wanted = tuple(stage) + (rule.veto_arms if index == 0 else ())
        stage_latency = 0.0
        for arm in wanted:
            row = rows.get(arm)
            if row is None:
                return Decision(False, False, tuple(consulted), 0.0, 0.0, False)
            if arm in consulted:
                continue
            consulted.append(arm)
            if row.cost_usd is None:
                priced = False
            else:
                cost += row.cost_usd
            if row.latency_s is not None:
                stage_latency = max(stage_latency, row.latency_s)
        # Arms within a stage are independent calls and run side by side; stages are serial.
        latency += stage_latency
        if any(rows[arm].verdict != MERGE for arm in stage):
            merge = False
            break
    if merge and rule.veto_arms:
        if any(rows[arm].verdict in VETO_VERDICTS for arm in rule.veto_arms):
            merge = False
    return Decision(True, merge, tuple(consulted), cost, latency, priced)


def _with_veto(rule: Rule, arms: Sequence[str]) -> Rule:
    return Rule(
        name=rule.name + VETO_SUFFIX,
        kind=rule.kind + "_veto",
        stages=rule.stages,
        veto_arms=tuple(arms),
    )


def catalogue(arms: Sequence[str], veto: bool = True) -> list[Rule]:
    """Every rule the stored verdicts can answer: each arm alone, unanimity of 2/3/…/n, every
    ordered two-stage cascade, and — when `veto` — each of those under the all-arm veto.

    Ordered cascades are generated in both directions on purpose: `A` proposing and `B`
    confirming is a different rule from the reverse, both in what it merges and in what it
    costs, and W6 was surprised by exactly that asymmetry."""
    names = list(dict.fromkeys(arms))
    rules: list[Rule] = []
    for arm in names:
        rules.append(Rule(arm, "single", ((arm,),)))
    for size in range(2, len(names) + 1):
        for combo in combinations(names, size):
            rules.append(Rule(f"unan({'+'.join(combo)})", f"unanimity{size}", (combo,)))
    for first, second in permutations(names, 2):
        rules.append(Rule(f"casc({first}>{second})", "cascade2", ((first,), (second,))))
    if veto:
        rules = rules + [_with_veto(rule, names) for rule in rules]
    return rules


def named(rules: Iterable[Rule], name: str) -> Rule:
    for rule in rules:
        if rule.name == name:
            return rule
    raise KeyError(name)


# --- scoring ----------------------------------------------------------------------------

Key = tuple[int, int]
SAME: str = "same"
NOT_SAME: str = "not_same"


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def _rate(k: int, n: int) -> dict[str, object]:
    from autodedup.evaluate import wilson_interval

    low, high = wilson_interval(k, n) if n else (None, None)
    return {
        "k": k,
        "n": n,
        "rate": round(k / n, 4) if n else None,
        "wilson_low": None if low is None else round(low, 4),
        "wilson_high": None if high is None else round(high, 4),
    }


def score(
    rule: Rule,
    arms: Mapping[str, Mapping[Key, ArmRow]],
    reference: Mapping[Key, str],
    blocks: Mapping[Key, str] | None = None,
) -> dict[str, object]:
    """One rule against one binary reference — the four numbers a rollout is decided on.

    The false-merge denominator is the reference's `not_same` pairs and the recall denominator
    its `same` pairs, never pooled: an arm cannot trade one for the other and keep a headline.
    `blocks` turns the first into a second, harder reading — how many DISTINCT developments
    produced an error — because a rate measured inside one development is not a rate."""
    blocks = blocks or {}
    false_merges = 0
    negatives = 0
    recalled = 0
    positives = 0
    escalated = 0
    covered = 0
    priced = 0
    costs: list[float] = []
    latencies: list[float] = []
    error_blocks: set[str] = set()
    negative_blocks: set[str] = set()
    missed_blocks: set[str] = set()
    positive_blocks: set[str] = set()
    for key, truth in reference.items():
        rows = {name: table[key] for name, table in arms.items() if key in table}
        decision = decide(rule, rows)
        if not decision.covered:
            continue
        covered += 1
        if decision.priced:
            priced += 1
            costs.append(decision.cost_usd)
        latencies.append(decision.latency_s)
        if len(decision.consulted) > len(rule.stages[0]) + len(rule.veto_arms):
            escalated += 1
        block = blocks.get(key)
        if truth == NOT_SAME:
            negatives += 1
            if block:
                negative_blocks.add(block)
            if decision.merge:
                false_merges += 1
                if block:
                    error_blocks.add(block)
        else:
            positives += 1
            if block:
                positive_blocks.add(block)
            if decision.merge:
                recalled += 1
            elif block:
                missed_blocks.add(block)
    return {
        "rule": rule.name,
        "kind": rule.kind,
        "arms": list(rule.arms),
        "n_covered": covered,
        "n_reference": len(reference),
        "false_merge": _rate(false_merges, negatives),
        "recall": _rate(recalled, positives),
        "blocks": {
            "negative_blocks": len(negative_blocks),
            "false_merge_blocks": len(error_blocks),
            "false_merge_block_names": sorted(error_blocks),
            "positive_blocks": len(positive_blocks),
            "missed_blocks": len(missed_blocks),
        },
        "cost": {
            "n_priced": priced,
            "per_pair_usd": round(sum(costs) / len(costs), 6) if costs else None,
            "total_usd": round(sum(costs), 6) if costs else None,
        },
        "escalation_rate": round(escalated / covered, 4) if covered else None,
        "latency": {
            "p50_s": _percentile(latencies, 0.50),
            "p95_s": _percentile(latencies, 0.95),
        },
    }


def score_all(
    arms: Mapping[str, Mapping[Key, ArmRow]],
    reference: Mapping[Key, str],
    blocks: Mapping[Key, str] | None = None,
    rules: Sequence[Rule] | None = None,
) -> list[dict[str, object]]:
    catalogue_ = list(rules) if rules is not None else catalogue(list(arms))
    return [score(rule, arms, reference, blocks) for rule in catalogue_]


def project_monthly_usd(per_pair_usd: float | None, band_pairs: int,
                        recall_dial: float = 1.0) -> float | None:
    """M20's production shape: `band_pairs` a month at the full dial, scaled by a recall dial.

    D15's 75 %-recall dial is a 0.386 share of the band, so the dial is a multiplier on the
    POPULATION, not a discount on the price — a cheaper rule does not become safer by seeing
    fewer pairs."""
    if per_pair_usd is None:
        return None
    return round(per_pair_usd * band_pairs * recall_dial, 2)
