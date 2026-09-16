"""Judge output -> training and evaluation labels (PROGRAM.md §9).

A judgement is what one tier said about one pair; a **label** is what the label store believes
about that pair after precedence is applied — gold outranks vision outranks text, and a tier is
never averaged with another (a cheap verdict cannot dilute the oracle). Three verdicts map onto
two classes: `same_property` is the positive, `different_property` and
`same_building_different_unit` are both negatives, and `insufficient_evidence` is an abstention
that must stay OUT of every denominator rather than counting as a negative — an abstention
denominator is how an evaluation quietly inflates its own precision.

`same_building_different_unit` additionally carries `must_not_link`: it is the developer-project
negative E33's clustering is forbidden to union, and it is a stronger statement than
`different_property`, not a weaker one.

The sample file is the other half of the arithmetic. Pairs are drawn with per-stratum quotas, so
a judged pair stands for `n_total / n_selected` cohort pairs; every cohort-level number in
`evaluate` is Horvitz-Thompson weighted by exactly that ratio, and a sample-less run falls back
to weight 1.0 (which makes every cohort estimate a sample estimate, and says so).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

PairKey = tuple[int, int]

TIER_PRECEDENCE: tuple[str, ...] = ("gold", "vision", "text")
CHEAP_TIERS: tuple[str, ...] = ("vision", "text")

POSITIVE_VERDICT: str = "same_property"
NEGATIVE_VERDICTS: tuple[str, ...] = ("different_property", "same_building_different_unit")
ABSTAIN_VERDICT: str = "insufficient_evidence"
MUST_NOT_LINK_VERDICT: str = "same_building_different_unit"

WEIGHT_GOLD_UNANIMOUS: float = 1.0
WEIGHT_GOLD_MAJORITY: float = 0.67
WEIGHT_VISION: float = 0.6
WEIGHT_TEXT: float = 0.3
TIER_WEIGHTS: dict[str, float] = {
    "gold": WEIGHT_GOLD_UNANIMOUS,
    "vision": WEIGHT_VISION,
    "text": WEIGHT_TEXT,
}


def pair_key(lo: Any, hi: Any) -> PairKey:
    left, right = int(lo), int(hi)
    return (left, right) if left <= right else (right, left)


@dataclass(slots=True)
class JudgementRow:
    """One line of `judgements.jsonl`, whatever tier wrote it."""

    lo: int
    hi: int
    tier: str
    model: str
    stratum: str | None
    verdict: str | None
    confidence: float
    unanimous: bool | None
    flagged: bool
    n_votes: int | None
    developer_project_suspected: bool
    downgraded_from: str | None
    incomplete: bool
    cost_usd: float
    llm_call_id: int | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def key(self) -> PairKey:
        return pair_key(self.lo, self.hi)

    @property
    def is_aggregate(self) -> bool:
        """A gold row that AGGREGATES its votes, as opposed to one of the votes itself.

        The lane emits both; only the aggregate is ground truth, because `aggregate_gold` is
        where unanimity, the majority downgrade and the no-majority abstention are decided."""
        return self.n_votes is not None

    @property
    def usable(self) -> bool:
        if self.incomplete or not self.verdict:
            return False
        return self.is_aggregate if self.tier == "gold" else True


def _as_bool(value: Any) -> bool:
    return bool(value) if value is not None else False


def parse_judgement(payload: Mapping[str, Any]) -> JudgementRow:
    verdict = payload.get("verdict")
    body: Mapping[str, Any] = verdict if isinstance(verdict, Mapping) else {}
    n_votes = payload.get("n_votes")
    return JudgementRow(
        lo=int(payload["lo"]),
        hi=int(payload["hi"]),
        tier=str(payload.get("tier") or "?"),
        model=str(payload.get("model") or payload.get("models") or "?"),
        stratum=(str(payload["stratum"]) if payload.get("stratum") else None),
        verdict=(str(body["verdict"]) if body.get("verdict") else None),
        confidence=float(body.get("confidence") or 0.0),
        unanimous=(None if body.get("unanimous") is None else bool(body["unanimous"])),
        flagged=_as_bool(body.get("flagged")),
        n_votes=(int(n_votes) if n_votes is not None else None),
        developer_project_suspected=_as_bool(body.get("developer_project_suspected")),
        downgraded_from=(str(body["downgraded_from"]) if body.get("downgraded_from") else None),
        incomplete=_as_bool(payload.get("incomplete")),
        cost_usd=float(payload.get("cost_usd") or 0.0),
        llm_call_id=(
            int(payload["llm_call_id"]) if payload.get("llm_call_id") is not None else None
        ),
        raw=dict(payload),
    )


def load_judgements(path: str | Path) -> list[JudgementRow]:
    """Read one `judgements.jsonl`; a blank or malformed-empty line is skipped, not fatal."""
    rows: list[JudgementRow] = []
    with Path(path).open("rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(parse_judgement(json.loads(line)))
    return rows


def load_all_judgements(paths: Iterable[str | Path]) -> list[JudgementRow]:
    out: list[JudgementRow] = []
    for path in paths:
        out.extend(load_judgements(path))
    return out


@dataclass(slots=True)
class Label:
    """The label store's belief about one pair: the class, who said so, and how much it counts."""

    lo: int
    hi: int
    y: int | None
    verdict: str
    tier: str
    confidence: float
    weight: float
    must_not_link: bool
    developer_project_suspected: bool
    stratum: str | None = None
    n_votes: int | None = None
    unanimous: bool | None = None

    @property
    def key(self) -> PairKey:
        return pair_key(self.lo, self.hi)

    @property
    def judged(self) -> bool:
        return self.y is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "lo": self.lo,
            "hi": self.hi,
            "y": self.y,
            "verdict": self.verdict,
            "tier": self.tier,
            "confidence": self.confidence,
            "weight": self.weight,
            "must_not_link": self.must_not_link,
            "developer_project_suspected": self.developer_project_suspected,
            "stratum": self.stratum,
            "n_votes": self.n_votes,
            "unanimous": self.unanimous,
        }


def verdict_class(verdict: str) -> int | None:
    """same_property -> 1, the two negatives -> 0, insufficient_evidence -> None (abstain)."""
    if verdict == POSITIVE_VERDICT:
        return 1
    if verdict in NEGATIVE_VERDICTS:
        return 0
    return None


def label_weight(row: JudgementRow) -> float:
    """Gold splits on unanimity (§9's oracle weight vs the flagged 2-1 majority); cheap is flat."""
    if row.tier == "gold":
        return WEIGHT_GOLD_UNANIMOUS if row.unanimous else WEIGHT_GOLD_MAJORITY
    return TIER_WEIGHTS.get(row.tier, WEIGHT_TEXT)


def _label_of(row: JudgementRow) -> Label:
    verdict = str(row.verdict)
    return Label(
        lo=row.key[0],
        hi=row.key[1],
        y=verdict_class(verdict),
        verdict=verdict,
        tier=row.tier,
        confidence=row.confidence,
        weight=label_weight(row),
        must_not_link=verdict == MUST_NOT_LINK_VERDICT,
        developer_project_suspected=row.developer_project_suspected,
        stratum=row.stratum,
        n_votes=row.n_votes,
        unanimous=row.unanimous,
    )


def labels_by_tier(judgements: Sequence[JudgementRow]) -> dict[str, dict[PairKey, Label]]:
    """One label per (tier, pair). A later row for the same key wins — a re-judge is a correction.

    Gold keeps only the aggregate; a lone surviving gold vote is deliberately NOT ground truth."""
    out: dict[str, dict[PairKey, Label]] = {}
    for row in judgements:
        if not row.usable:
            continue
        out.setdefault(row.tier, {})[row.key] = _label_of(row)
    return out


def effective_precedence(
    tiers: Iterable[str], precedence: Sequence[str] = TIER_PRECEDENCE
) -> list[str]:
    """The ordering actually applied: the caller's list first, then the REMAINING tiers in
    §9's own order, then anything unknown alphabetically.

    A partial `--precedence gold` must not silently invert vision and text: alphabetical order
    puts text above vision, which is the opposite of what their weights (0.6 vs 0.3) say."""
    present = list(dict.fromkeys(tiers))
    ranked = [tier for tier in precedence if tier in present]
    ranked.extend(
        tier for tier in TIER_PRECEDENCE if tier in present and tier not in ranked
    )
    ranked.extend(sorted(tier for tier in present if tier not in ranked))
    return ranked


def label_pairs(
    judgements: Sequence[JudgementRow],
    *,
    precedence: Sequence[str] = TIER_PRECEDENCE,
) -> dict[PairKey, Label]:
    """Collapse every tier onto one label per pair, highest-precedence tier wins outright."""
    per_tier = labels_by_tier(judgements)
    out: dict[PairKey, Label] = {}
    for tier in reversed(effective_precedence(per_tier, precedence)):
        out.update(per_tier[tier])
    return out


@dataclass(slots=True)
class Stratum:
    key: str
    n_selected: int
    n_total: int

    @property
    def weight(self) -> float:
        """Horvitz-Thompson inflation: one judged pair stands for this many cohort pairs."""
        return (self.n_total / self.n_selected) if self.n_selected > 0 else 0.0

    def to_json(self) -> dict[str, Any]:
        return {"n_selected": self.n_selected, "n_total": self.n_total, "weight": self.weight}


@dataclass(slots=True)
class Sample:
    strata: dict[str, Stratum] = field(default_factory=dict)
    pair_stratum: dict[PairKey, str] = field(default_factory=dict)
    seed: int | None = None
    tier: str | None = None
    judge_version: str | None = None
    n_requested: int | None = None
    n_selected: int | None = None
    path: str | None = None
    stratum_fn: str | None = None

    def stratum_of(self, key: PairKey) -> str | None:
        return self.pair_stratum.get(key)

    def inflates(self, name: str | None) -> bool:
        """Whether this stratum actually stands for more of the cohort than it contains."""
        if name is None:
            return False
        stratum = self.strata.get(name)
        return bool(stratum and stratum.n_selected and stratum.n_total > stratum.n_selected)

    def weight_for_name(self, name: str | None, default: float = 1.0) -> float:
        stratum = self.strata.get(name) if name is not None else None
        return stratum.weight if stratum is not None and stratum.n_selected else default

    def weight_of(self, key: PairKey, default: float = 1.0) -> float:
        """The pair's HT weight; an unsampled or unknown pair counts once, never zero."""
        return self.weight_for_name(self.pair_stratum.get(key), default)

    def weight_for(self, key: PairKey, stratum: str | None = None,
                   default: float = 1.0) -> float:
        """Prefer the stratum the JUDGEMENT row carries — it is the sampler's own stamp — and
        fall back to this file's per-pair map only when the label has none."""
        if stratum:
            return self.weight_for_name(stratum, default)
        return self.weight_of(key, default)

    @property
    def is_weighted(self) -> bool:
        """True only when some stratum really inflates. `Stratum(name, n, n)` everywhere means
        every weight is 1.0, and a report built on it is a sample rate, not a cohort estimate."""
        return any(
            stratum.n_selected and stratum.n_total > stratum.n_selected
            for stratum in self.strata.values()
        )

    @property
    def n_cohort(self) -> int:
        return sum(stratum.n_total for stratum in self.strata.values())

    def to_json(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "tier": self.tier,
            "judge_version": self.judge_version,
            "n_requested": self.n_requested,
            "n_selected": self.n_selected,
            "n_strata": len(self.strata),
            "n_cohort": self.n_cohort,
            "path": self.path,
            "stratum_fn": self.stratum_fn,
            "is_weighted": self.is_weighted,
        }


EMPTY_SAMPLE: Sample = Sample()


def _stratum_counts(payload: Mapping[str, Any]) -> tuple[int, int]:
    selected = payload.get("selected", payload.get("n_selected", 0))
    total = payload.get("population", payload.get("n_total", 0))
    return int(selected or 0), int(total or 0)


def _candidate_stratum_fns() -> list[tuple[str, Callable[[Mapping[str, Any]], str]]]:
    """Every key function the samplers in `harness` actually use, in the order to try them.

    `sample_pairs` (the judge lane) keys on `judge_stratum`; `judge-sample` on the CLI keys on
    `_stratum`. The two name shapes never intersect, so recomputing with the wrong one silently
    collapses every Horvitz-Thompson weight to 1.0 — which is why a recomputed name is CHECKED
    against the file's own strata rather than trusted."""
    from autodedup import harness

    return [("judge_stratum", harness.judge_stratum), ("stratum", harness._stratum)]


def _resolve_stratum_fn(
    rows: Sequence[Mapping[str, Any]],
    names: Mapping[str, Any],
    stratum_fn: Callable[[Mapping[str, Any]], str] | None,
) -> tuple[str, Callable[[Mapping[str, Any]], str]]:
    candidates = (
        [("explicit", stratum_fn)] if stratum_fn is not None else _candidate_stratum_fns()
    )
    best: tuple[int, str, Callable[[Mapping[str, Any]], str]] = (-1, "none", lambda row: "")
    for label, fn in candidates:
        hits = 0
        for row in rows:
            try:
                hits += 1 if fn(dict(row)) in names else 0
            except Exception:  # a row this key function cannot read is simply not a hit
                continue
        if hits > best[0]:
            best = (hits, label, fn)
    if best[0] < len(rows):
        raise ValueError(
            f"sample.json strata do not match any known sampler key: {best[0]} of {len(rows)} "
            f"pairs resolved with `{best[1]}`; the file's strata look like "
            f"{sorted(names)[:2]}. Pass the sample the judgements were actually drawn from."
        )
    return best[1], best[2]


def load_sample(
    path: str | Path,
    *,
    stratum_fn: Callable[[Mapping[str, Any]], str] | None = None,
) -> Sample:
    """Read a lane/harness `sample.json` into the per-pair stratum map the HT weights need.

    The file stores strata counts but not, on every sampler, the per-pair key. A recomputed key
    that is not one of the file's own strata is a HARD ERROR: keeping it as an empty stratum
    would hand every pair weight 1.0 while the report still claimed to be cohort-level."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    strata = {
        str(name): Stratum(str(name), *_stratum_counts(body))
        for name, body in (data.get("strata") or {}).items()
    }
    rows = [row for row in (data.get("pairs") or ()) if not row.get("stratum")]
    if rows and not strata:
        raise ValueError(f"{path}: no strata in the sample file, so no weight can be recovered")
    resolved = _resolve_stratum_fn(rows, strata, stratum_fn) if rows else None
    pair_stratum: dict[PairKey, str] = {}
    for row in data.get("pairs") or ():
        key = pair_key(row["lo"], row["hi"])
        name = str(row["stratum"]) if row.get("stratum") else str(resolved[1](dict(row)))
        pair_stratum[key] = name
        if name not in strata:
            strata[name] = Stratum(name, 0, 0)
    return Sample(
        stratum_fn=(resolved[0] if resolved else "stamped"),
        strata=strata,
        pair_stratum=pair_stratum,
        seed=(int(data["seed"]) if data.get("seed") is not None else None),
        tier=(str(data["tier"]) if data.get("tier") else None),
        judge_version=(str(data["judge_version"]) if data.get("judge_version") else None),
        n_requested=(int(data["n_requested"]) if data.get("n_requested") is not None else None),
        n_selected=(int(data["n_selected"]) if data.get("n_selected") is not None else None),
        path=str(path),
    )


def sample_from_judgements(judgements: Sequence[JudgementRow]) -> Sample:
    """A degenerate sample recovered from the judgement rows alone: strata are known, the
    populations are not, so every weight is 1.0 and no number built on it is cohort-level."""
    pair_stratum: dict[PairKey, str] = {}
    for row in judgements:
        if row.stratum:
            pair_stratum[row.key] = row.stratum
    counts: dict[str, int] = {}
    for name in pair_stratum.values():
        counts[name] = counts.get(name, 0) + 1
    return Sample(
        strata={name: Stratum(name, n, n) for name, n in counts.items()},
        pair_stratum=pair_stratum,
        stratum_fn="stamped",
    )
