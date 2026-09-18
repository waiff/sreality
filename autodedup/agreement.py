"""Operator vs judge agreement — the D6 gate, computed from one statement's rows.

D6 stops the program if the **gold** judge and the operator agree on less than 0.95 of a
~200-pair session run through the validation UI. That number is meaningless without saying
exactly how it is computed, so the whole computation lives here, in Python, over the rows
`ui_sql.AGREEMENT_PAIRS_SQL` returns — a Wilson interval spelled in SQL is a formula nobody
can check by hand, and this one decides whether the program continues.

THE COMPARISON IS BINARY, because the two vocabularies are not the same vocabulary. The
operator has five verdicts (migration 532), the judge four, and only one distinction is common
to both and load-bearing: **one property, or not one property**. `same` is the operator's
"one"; `same_property` is the judge's. Everything else the operator can say — `different`,
`same_building_different_unit`, `same_project_different_unit` — is "not one", and so are the
judge's `different_property` and `same_building_different_unit`. `unsure` is not a label (the
statement drops it) and `insufficient_evidence` is not a judgement (dropped here, and COUNTED,
because a judge that abstains on half the sample is a fact about the judge).

The interval is **Wilson**, not the normal approximation: at n≈200 and p≈0.97 the normal
interval runs past 1.0, and an upper bound above 1 on a page that reports a gate is the kind
of number that makes an operator stop trusting the page.
"""

from __future__ import annotations

from math import sqrt
from typing import Any, Iterable

# The operator's "these two adverts are one property", and the judge's.
OPERATOR_SAME = "same"
JUDGE_SAME = "same_property"
# Not a verdict about the pair — the judge saying the digests do not settle it (§9).
JUDGE_ABSTAIN = "insufficient_evidence"

# gold > vision > text. `oss` never reaches here (the statement excludes it).
TIERS: tuple[str, ...] = ("gold", "vision", "text")

# The tier D6 is measured on, and the session size it names.
GATE_TIER = "gold"
GATE_BAR = 0.95
GATE_TARGET_N = 200

# 95%, two-sided.
Z_95 = 1.959963984540054

# A disagreement table is evidence, not a data dump: enough rows to see the error CLASS,
# few enough that the panel stays a panel.
MAX_DISAGREEMENTS = 50


def wilson_interval(k: int, n: int, z: float = Z_95) -> tuple[float | None, float | None]:
    """The Wilson score interval for k successes in n trials; (None, None) for n = 0."""
    if n <= 0:
        return (None, None)
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return (max(0.0, center - half), min(1.0, center + half))


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    n_agree = sum(1 for r in rows if r["agree"])
    low, high = wilson_interval(n_agree, n)
    return {
        "n": n,
        "n_agree": n_agree,
        "agreement": (n_agree / n) if n else None,
        "ci_low": low,
        "ci_high": high,
        # The two directions of the same number: an engine that over-merges and a judge that
        # under-calls duplicates are different failures and are never summed into one "4 wrong".
        "n_judge_different_operator_same": sum(
            1 for r in rows if r["operator_same"] and not r["judge_same"]
        ),
        "n_judge_same_operator_different": sum(
            1 for r in rows if not r["operator_same"] and r["judge_same"]
        ),
        "n_explicit": sum(1 for r in rows if r["operator_source"] == "explicit"),
        "n_implied": sum(1 for r in rows if r["operator_source"] == "implied"),
    }


def summarize(raw_rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """One row per comparable pair in, the whole panel out.

    `raw_rows` are `AGREEMENT_PAIRS_SQL`'s rows: `listing_lo`, `listing_hi`,
    `operator_verdict`, `operator_source`, `judge_verdict`, `judge_tier`, `judge_model`.
    """
    compared: list[dict[str, Any]] = []
    abstained: dict[str, int] = {}
    n_abstained = 0
    for row in raw_rows:
        tier = str(row["judge_tier"])
        if row["judge_verdict"] == JUDGE_ABSTAIN:
            n_abstained += 1
            abstained[tier] = abstained.get(tier, 0) + 1
            continue
        operator_same = row["operator_verdict"] == OPERATOR_SAME
        judge_same = row["judge_verdict"] == JUDGE_SAME
        compared.append(
            {
                "listing_lo": int(row["listing_lo"]),
                "listing_hi": int(row["listing_hi"]),
                "operator_verdict": row["operator_verdict"],
                "operator_source": row["operator_source"],
                "judge_verdict": row["judge_verdict"],
                "judge_tier": tier,
                "judge_model": row["judge_model"],
                "operator_same": operator_same,
                "judge_same": judge_same,
                "agree": operator_same == judge_same,
            }
        )

    by_tier = [
        {"tier": tier, **_stats([r for r in compared if r["judge_tier"] == tier])}
        for tier in TIERS
    ]
    disagreements = [
        {k: v for k, v in row.items() if k not in ("operator_same", "judge_same", "agree")}
        for row in compared
        if not row["agree"]
    ]
    return {
        "overall": _stats(compared),
        # Every tier is reported, including the ones with no pair: "gold has judged none of
        # these yet" is the answer that tells the operator what to run next, and an absent row
        # reads as a page that forgot to say.
        "tiers": by_tier,
        "n_insufficient_evidence": n_abstained,
        "insufficient_by_tier": abstained,
        "disagreements": disagreements[:MAX_DISAGREEMENTS],
        "n_disagreements": len(disagreements),
        "gate": {"tier": GATE_TIER, "bar": GATE_BAR, "target_n": GATE_TARGET_N},
    }
