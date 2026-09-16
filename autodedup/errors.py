"""Where the engine is wrong, and on WHICH feature (W4b).

`evaluate` answers "how often is the merge zone right"; this module answers the two questions
that come next and that no aggregate rate can answer: *which* pairs it got wrong, and what
separates them from the pairs it got right. Three error surfaces, one shape:

* FALSE MERGES — zone `merge`, label 0 — grouped by certificate x cohort block x same/cross
  source, because a certificate is a structural promise (E24) and a certificate that only leaks
  in one block against one source pair is a fixable certificate, not a broken layer.
* FALSE REJECTS — zone `reject`/`veto`, label 1 — grouped by block x same/cross, carrying the
  NAME of the rule that refused each pair (a guard veto and a model reject are different bugs).
* BAND composition — zone `band` — by block, positives against negatives, because the band is
  the review budget (E25) and its composition says whether widening it would buy anything.

Every group carries a per-feature CONTRAST: for each feature of `FEATURE_ORDER`, the mean of
its PRESENT values on each side, the presence rate on each side, and the standardised
difference between the two means — sorted so the top of the list is what actually separates
the classes. Absence is reported next to the mean rather than folded into it (E12): a feature
that is simply missing on the wrong side is a different finding from one whose value differs,
and averaging an absent feature as zero hides both. The RANK therefore reads both halves —
a feature one class never has has no standardised difference at all, and ranking on the value
gap alone would bury the most separating feature in the table — and discounts both by the
number of observations behind them, because a one-against-one comparison standardises to a
constant +-2.0 that is an artefact of the pooling rather than a measurement.

Note on the reject denominator: this module pools zone `reject` WITH zone `veto`, which is
deliberately wider than `evaluate.REJECT_ZONE` (`reject` alone). A guard veto is a refusal the
engine made and has to answer for here; the two modules' "reject precision" will therefore
differ on any run that stores vetoed pairs, and that is not a bug in either.

The candidate-rule tables are the decision aid this was built for: a certificate can only be
tightened on evidence, so `rule_table` re-prices K-A (and the model merges at a ladder of score
cuts) under every conjunction of four proposed extra clauses, and prints n, precision, the
Wilson lower bound and how much of the base set's true merges the clause keeps. A clause that
buys precision by discarding the positives is visible in the same row that flatters it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Callable, Iterable, Mapping, Sequence

from autodedup.evaluate import wilson_lower
from autodedup.features import FAMILY_OF, FEATURE_ORDER, TXT_RARE_TOKENS
from autodedup.labels import (
    EMPTY_SAMPLE,
    JudgementRow,
    Label,
    PairKey,
    Sample,
    pair_key,
)

MERGE_ZONE: str = "merge"
BAND_ZONE: str = "band"
REJECT_ZONES: tuple[str, ...] = ("reject", "veto")

DEFAULT_TOP: int = 5
TOP_FEATURES: int = 8
MARKDOWN_CONTRAST_ROWS: int = 12
MODEL_THRESHOLDS: tuple[float, ...] = (0.97, 0.98, 0.99, 0.995)
MAX_COMBO: int = 2
TINY: float = 1e-12
MIN_CONTRAST_N: int = 3
PRESENCE_SCALE: float = 2.0
ALL_BLOCKS: str = "(all)"

ERRORS_STEM: str = "errors"


# --- rows ------------------------------------------------------------------------------------


@dataclass(slots=True)
class PairRow:
    """One stored pair joined to its label and to the judgement that produced the label."""

    lo: int
    hi: int
    zone: str
    score: float
    certificate: str | None
    veto: str | None
    reason: str
    block: str
    side: str
    families: tuple[str, ...]
    feats: dict[str, tuple[float, bool]]
    labelled: bool = False
    y: int | None = None
    tier: str | None = None
    confidence: float = 0.0
    stratum: str = "(none)"
    weight: float = 1.0
    verdict: str | None = None
    unit_discriminator: str | None = None
    key_evidence: str | None = None

    @property
    def key(self) -> PairKey:
        return (self.lo, self.hi)

    @property
    def refusal(self) -> str:
        """The name of whatever refused the pair: a guard veto outranks the decision reason."""
        if self.veto:
            return f"guard_veto:{self.veto}"
        return self.reason or "-"

    def value(self, name: str) -> float | None:
        """The feature's value when PRESENT, else None — absence is unknown, never zero (E12)."""
        entry = self.feats.get(name)
        if not entry or not entry[1]:
            return None
        return float(entry[0])

    def to_json(self) -> dict[str, Any]:
        return {
            "lo": self.lo,
            "hi": self.hi,
            "zone": self.zone,
            "score": self.score,
            "certificate": self.certificate,
            "reason": self.reason,
            "veto": self.veto,
            "refusal": self.refusal,
            "block": self.block,
            "side": self.side,
            "families": list(self.families),
            "y": self.y,
            "tier": self.tier,
            "confidence": self.confidence,
            "stratum": self.stratum,
            "weight": self.weight,
            "verdict": self.verdict,
            "unit_discriminator": self.unit_discriminator,
            "key_evidence": self.key_evidence,
        }


def _feats_of(row: Mapping[str, Any]) -> dict[str, tuple[float, bool]]:
    out: dict[str, tuple[float, bool]] = {}
    for name, entry in (row.get("feats") or {}).items():
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            try:
                out[str(name)] = (float(entry[0]), bool(entry[1]))
            except (TypeError, ValueError):
                continue
    return out


def judgements_by_key(
    judgements: Sequence[JudgementRow],
) -> dict[str, dict[PairKey, JudgementRow]]:
    """The raw judgement behind each (tier, pair), mirroring `labels.labels_by_tier` exactly.

    The label says the class; only the judgement carries the free text (`unit_discriminator`,
    `key_evidence`) an error listing is read for, so the two have to be indexed the same way —
    last row wins, gold counted only through its aggregate."""
    out: dict[str, dict[PairKey, JudgementRow]] = {}
    for row in judgements:
        if not row.usable:
            continue
        out.setdefault(row.tier, {})[row.key] = row
    return out


def _first_evidence(body: Mapping[str, Any]) -> str | None:
    evidence = body.get("key_evidence")
    if isinstance(evidence, (list, tuple)) and evidence:
        return str(evidence[0])
    return str(evidence) if isinstance(evidence, str) and evidence else None


def build_rows(
    run_pairs: Sequence[Mapping[str, Any]],
    labels: Mapping[PairKey, Label],
    judgements: Sequence[JudgementRow],
    sample: Sample | None,
) -> list[PairRow]:
    """Join stored pairs, labels and judgements; weights follow `evaluate`'s own HT contract."""
    draw = sample if sample is not None else EMPTY_SAMPLE
    index = judgements_by_key(judgements)
    out: list[PairRow] = []
    for row in run_pairs:
        key = pair_key(row["lo"], row["hi"])
        label = labels.get(key)
        stratum = (label.stratum if label and label.stratum else draw.stratum_of(key)) or "(none)"
        weight = (
            draw.weight_for_name(stratum)
            if (label is not None and label.stratum)
            else draw.weight_of(key)
        )
        judged = index.get(label.tier, {}).get(key) if label is not None else None
        body = (judged.raw.get("verdict") if judged is not None else None) or {}
        body = body if isinstance(body, Mapping) else {}
        out.append(PairRow(
            lo=key[0],
            hi=key[1],
            zone=str(row.get("zone") or "?"),
            score=float(row.get("score") or 0.0),
            certificate=(str(row["certificate"]) if row.get("certificate") else None),
            veto=(str(row["veto"]) if row.get("veto") else None),
            reason=str(row.get("reason") or ""),
            block=str(row.get("block") or "(none)"),
            side=("cross" if row.get("cross_source") else "same"),
            families=tuple(str(name) for name in (row.get("families") or ())),
            feats=_feats_of(row),
            labelled=label is not None,
            y=(label.y if label is not None else None),
            tier=(label.tier if label is not None else None),
            confidence=(label.confidence if label is not None else 0.0),
            stratum=stratum,
            weight=weight,
            verdict=(label.verdict if label is not None else None),
            unit_discriminator=(
                str(body["unit_discriminator"]) if body.get("unit_discriminator") else None
            ),
            key_evidence=_first_evidence(body),
        ))
    return out


# --- feature statistics ------------------------------------------------------------------


@dataclass(slots=True)
class FeatureStats:
    """One feature over one set of pairs: how often present, and the mean/sd of PRESENT values."""

    n: int = 0
    n_present: int = 0
    mean: float | None = None
    sd: float | None = None

    @property
    def presence_rate(self) -> float:
        return (self.n_present / self.n) if self.n else 0.0


def feature_stats(rows: Sequence[PairRow], name: str) -> FeatureStats:
    values = [value for value in (row.value(name) for row in rows) if value is not None]
    if not values:
        return FeatureStats(n=len(rows))
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return FeatureStats(len(rows), len(values), mean, math.sqrt(variance))


def stats_table(rows: Sequence[PairRow]) -> dict[str, FeatureStats]:
    return {name: feature_stats(rows, name) for name in FEATURE_ORDER}


def _standardised(
    positive: FeatureStats, negative: FeatureStats, union: FeatureStats
) -> float | None:
    """(mean_pos - mean_neg) standardised by the sd of the two classes POOLED.

    Pooling over the union rather than within a class is what keeps a perfectly separating
    feature reportable: `same_source` at 1.0 on one side and 0.0 on the other has zero variance
    inside each class, and a within-class denominator would divide by zero exactly where the
    separation is total."""
    if positive.mean is None or negative.mean is None:
        return None
    if union.sd is None or union.sd <= TINY:
        return 0.0
    return (positive.mean - negative.mean) / union.sd


def _support(n: int) -> float:
    """How much a separation resting on `n` observations per side is worth for RANKING."""
    return math.sqrt(n / (n + MIN_CONTRAST_N)) if n > 0 else 0.0


def _separation(
    std_diff: float | None,
    presence_diff: float,
    *,
    n_value: int,
    n_rows: int,
) -> tuple[float, int]:
    """The magnitude to rank on, and the number of observations that magnitude rests on.

    Both halves are read, because `std_diff` is None exactly when one class NEVER has the
    feature — total separation, which a value-only key would sort to the bottom. The presence
    gap is scaled by `PRESENCE_SCALE` to put it on the same footing: a perfectly separating
    binary already standardises to +-2.0 under this pooling. Both halves are discounted by the
    observations behind them (`n_value` = the smaller side's PRESENT count, `n_rows` = the
    smaller side's row count), and the winning half's support is returned so a thin reading can
    be demoted outright: a 1-vs-1 `std_diff` is a constant +-2.0, an artefact of the
    denominator rather than a finding, and must not buy the top of the table."""
    value = abs(std_diff or 0.0) * _support(n_value)
    presence = PRESENCE_SCALE * abs(presence_diff) * _support(n_rows)
    if presence > value:
        return presence, n_rows
    return value, n_value


def contrast(
    positives: Sequence[PairRow], negatives: Sequence[PairRow]
) -> list[dict[str, Any]]:
    """Per-feature positive-vs-negative contrast, most separating feature first."""
    union = list(positives) + list(negatives)
    rows_min = min(len(positives), len(negatives))
    entries: list[dict[str, Any]] = []
    for name in FEATURE_ORDER:
        pos = feature_stats(positives, name)
        neg = feature_stats(negatives, name)
        both = feature_stats(union, name)
        std_diff = _standardised(pos, neg, both)
        separation, support = _separation(
            std_diff,
            pos.presence_rate - neg.presence_rate,
            n_value=min(pos.n_present, neg.n_present),
            n_rows=rows_min,
        )
        entries.append({
            "feature": name,
            "family": FAMILY_OF.get(name, "?"),
            "mean_pos": pos.mean,
            "sd_pos": pos.sd,
            "n_present_pos": pos.n_present,
            "presence_pos": pos.presence_rate,
            "mean_neg": neg.mean,
            "sd_neg": neg.sd,
            "n_present_neg": neg.n_present,
            "presence_neg": neg.presence_rate,
            "std_diff": std_diff,
            "presence_diff": pos.presence_rate - neg.presence_rate,
            "separation": separation,
            "support": support,
            "supported": support >= MIN_CONTRAST_N,
        })
    # Thin readings sort below every well-supported one, however large they look: a separation
    # measured on one observation per side is a property of the denominator, not of the data.
    entries.sort(
        key=lambda entry: (
            not entry["supported"],
            -float(entry["separation"]),
            -abs(entry["std_diff"] or 0.0),
            -abs(entry["presence_diff"]),
            entry["feature"],
        )
    )
    return entries


def top_features(
    row: PairRow, reference: Mapping[str, FeatureStats], limit: int = TOP_FEATURES
) -> list[dict[str, Any]]:
    """This pair's most informative features, measured AGAINST the class it was confused with.

    A present feature scores |value - reference mean| / reference sd; an absent one scores the
    reference's presence rate, so "missing where the other class always has it" can still
    surface instead of being silently dropped for having no value to compare."""
    scored: list[dict[str, Any]] = []
    for name in FEATURE_ORDER:
        stats = reference.get(name)
        value = row.value(name)
        if value is None:
            weight = stats.presence_rate if stats is not None else 0.0
        elif stats is None or stats.mean is None:
            weight = 1.0
        elif stats.sd and stats.sd > TINY:
            weight = abs(value - stats.mean) / stats.sd
        else:
            weight = 0.0 if abs(value - stats.mean) <= TINY else 1.0
        scored.append({
            "feature": name,
            "family": FAMILY_OF.get(name, "?"),
            "value": value,
            "present": value is not None,
            "informativeness": weight,
            "reference_mean": (stats.mean if stats is not None else None),
            "reference_presence": (stats.presence_rate if stats is not None else None),
        })
    scored.sort(key=lambda entry: (-float(entry["informativeness"]), entry["feature"]))
    return scored[:limit]


# --- grouped error sections ---------------------------------------------------------------


def _counts(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items(), key=lambda item: (-item[1], item[0])))


def _precision(right: Sequence[PairRow], wrong: Sequence[PairRow]) -> dict[str, Any]:
    """Share of the judged pairs the engine got right, unweighted and Horvitz-Thompson."""
    judged = len(right) + len(wrong)
    weight_right = sum(row.weight for row in right)
    weight_total = weight_right + sum(row.weight for row in wrong)
    return {
        "n_judged": judged,
        "n_right": len(right),
        "n_wrong": len(wrong),
        "precision": (len(right) / judged) if judged else None,
        "wilson_lb": wilson_lower(len(right), judged) if judged else None,
        "precision_ht": (weight_right / weight_total) if weight_total > 0 else None,
    }


def _grouped(
    scope: Sequence[PairRow], key_fn: Callable[[PairRow], str]
) -> dict[str, list[PairRow]]:
    grouped: dict[str, list[PairRow]] = {}
    for row in scope:
        grouped.setdefault(key_fn(row), []).append(row)
    return grouped


def clean_groups(
    scope: Sequence[PairRow], key_fn: Callable[[PairRow], str], *, wrong_y: int
) -> list[dict[str, Any]]:
    """The judged groups with NO error, compactly — a promise that held is evidence too.

    `group_errors` shows only where the engine leaked, and a certificate absent from that table
    reads identically whether it never erred or was never judged. This is the other half."""
    out: list[dict[str, Any]] = []
    for key, members in _grouped(scope, key_fn).items():
        wrong = [row for row in members if row.y == wrong_y]
        right = [row for row in members if row.y == 1 - wrong_y]
        if wrong or not right:
            continue
        out.append({
            "group": key,
            "n_pairs": len(members),
            "n_abstain": sum(1 for row in members if row.labelled and row.y is None),
            "n_unlabelled": sum(1 for row in members if not row.labelled),
            **_precision(right, wrong),
        })
    out.sort(key=lambda entry: (-entry["n_judged"], entry["group"]))
    return out


def group_errors(
    scope: Sequence[PairRow],
    key_fn: Callable[[PairRow], str],
    *,
    wrong_y: int,
    top: int = DEFAULT_TOP,
    worst_first: bool = True,
) -> list[dict[str, Any]]:
    """Group one zone's pairs, keep the groups that contain an error, and describe each.

    `wrong_y` is the label the ENGINE contradicted: 0 inside the merge zone (a false merge), 1
    inside the reject zone (a false reject). The reference for the per-pair feature ranking is
    always the other class of the same group — the pairs the engine treated identically and was
    right about — falling back to the whole scope when a group has none of them. The groups
    WITHOUT an error are reported by `clean_groups`, not dropped on the floor."""
    fallback = stats_table([row for row in scope if row.y == 1 - wrong_y])
    grouped = _grouped(scope, key_fn)

    out: list[dict[str, Any]] = []
    for key, members in grouped.items():
        wrong = [row for row in members if row.y == wrong_y]
        if not wrong:
            continue
        right = [row for row in members if row.y == 1 - wrong_y]
        reference = stats_table(right) if right else fallback
        listing = sorted(
            wrong, key=lambda row: (row.score, row.lo, row.hi), reverse=worst_first
        )[:top]
        positives, negatives = (right, wrong) if wrong_y == 0 else (wrong, right)
        out.append({
            "group": key,
            "n_pairs": len(members),
            "n_abstain": sum(1 for row in members if row.labelled and row.y is None),
            "n_unlabelled": sum(1 for row in members if not row.labelled),
            "rules": _counts(row.refusal for row in wrong),
            "certificates": _counts((row.certificate or "model") for row in members),
            **_precision(right, wrong),
            "pairs": [
                {**row.to_json(), "top_features": top_features(row, reference)}
                for row in listing
            ],
            "contrast": contrast(positives, negatives),
        })
    out.sort(key=lambda entry: (-entry["n_wrong"], entry["group"]))
    return out


def band_composition(
    band: Sequence[PairRow], *, per_block: bool = True
) -> list[dict[str, Any]]:
    """What the review budget is actually holding, by block: positives, negatives, contrast.

    The pooled row is keyed on None rather than on its label, so a cohort block that happens to
    be NAMED `(all)` gets its own row instead of being folded into the total."""
    grouped: dict[str | None, list[PairRow]] = {None: list(band)}
    if per_block:
        for row in band:
            grouped.setdefault(row.block, []).append(row)
    out: list[dict[str, Any]] = []
    for group_key, members in grouped.items():
        key = ALL_BLOCKS if group_key is None else group_key
        positives = [row for row in members if row.y == 1]
        negatives = [row for row in members if row.y == 0]
        judged = len(positives) + len(negatives)
        out.append({
            "block": key,
            "n_pairs": len(members),
            "n_judged": judged,
            "n_positive": len(positives),
            "n_negative": len(negatives),
            "n_abstain": sum(1 for row in members if row.labelled and row.y is None),
            "n_unlabelled": sum(1 for row in members if not row.labelled),
            "positive_share": (len(positives) / judged) if judged else None,
            "certificates": _counts((row.certificate or "model") for row in members),
            "pooled": group_key is None,
            "contrast": contrast(positives, negatives),
        })
    out.sort(key=lambda entry: (not entry["pooled"], -entry["n_pairs"], entry["block"]))
    return out


# --- candidate rules -----------------------------------------------------------------------


def _at_least(row: PairRow, name: str, threshold: float) -> bool:
    value = row.value(name)
    return value is not None and value >= threshold


def _is_zero(row: PairRow, name: str) -> bool:
    value = row.value(name)
    return value is not None and abs(value) <= TINY


def _non_catalog_images(row: PairRow) -> bool:
    """At least one non-catalogue photo on BOTH sides.

    `n_images_min` is already the smaller side's count AFTER catalogue subtraction (E9), so on
    every pair observed so far the count alone decides the clause and the ratio changes nothing.
    The ratio is kept as an explicit second reading for the day the count stops being
    catalogue-net — and, unlike the count, it is read FAIL-OPEN on purpose: an unknown
    catalogue ratio is "not known to be all stock", which is the defensible reading when the
    first condition has already proved there are photos to compare."""
    catalog = row.value("catalog_ratio_max")
    return _at_least(row, "n_images_min", 1.0) and (catalog is None or catalog < 1.0)


@dataclass(slots=True)
class Rule:
    code: str
    text: str
    test: Callable[[PairRow], bool]


CANDIDATE_RULES: tuple[Rule, ...] = (
    Rule("a", "interior_match_ratio >= 0.5",
         lambda row: _at_least(row, "interior_match_ratio", 0.5)),
    Rule("b", f"rare_token_overlap >= {TXT_RARE_TOKENS:g}",
         lambda row: _at_least(row, "rare_token_overlap", TXT_RARE_TOKENS)),
    Rule("c", "non-catalog images >= 1 on both sides", _non_catalog_images),
    Rule("d", "overlap_days == 0 or same_source == 0",
         lambda row: _is_zero(row, "overlap_days") or _is_zero(row, "same_source")),
)


def rule_table(
    rows: Sequence[PairRow],
    *,
    rules: Sequence[Rule] = CANDIDATE_RULES,
    max_combo: int = MAX_COMBO,
    base_true: int | None = None,
) -> list[dict[str, Any]]:
    """Re-price a merge set under every conjunction of up to `max_combo` extra clauses.

    Precision alone would rank a clause that keeps three pairs above one that keeps three
    hundred, so every row also carries the Wilson lower bound and `kept_true_share` — the share
    of the TRUE merges the clause still admits.

    `base_true` is that denominator, and a caller that FILTERS its rows before calling (the
    score ladder does) must pass the unfiltered count: measured against each rung's own base,
    raising the threshold reads as free recall — the share rises while the absolute number of
    true merges falls. `kept_true_share_local` keeps the per-rung figure beside it, and
    `n_positive` puts the absolute count on the page so neither can be misread."""
    judged = [row for row in rows if row.y is not None]
    local_true = sum(1 for row in judged if row.y == 1)
    denominator = local_true if base_true is None else base_true
    out: list[dict[str, Any]] = []
    for size in range(0, max_combo + 1):
        for combo in combinations(rules, size):
            kept = [row for row in judged if all(rule.test(row) for rule in combo)]
            positives = sum(1 for row in kept if row.y == 1)
            total = len(kept)
            weight_pos = sum(row.weight for row in kept if row.y == 1)
            weight_all = sum(row.weight for row in kept)
            out.append({
                "rule": "+".join(rule.code for rule in combo) or "(base)",
                "clauses": [rule.text for rule in combo],
                "n": total,
                "n_positive": positives,
                "n_false": total - positives,
                "precision": (positives / total) if total else None,
                "wilson_lb": wilson_lower(positives, total) if total else None,
                "precision_ht": (weight_pos / weight_all) if weight_all > 0 else None,
                "kept_true_share": (positives / denominator) if denominator else None,
                "kept_true_share_local": (positives / local_true) if local_true else None,
            })
    return out


def threshold_ladder(
    t_hi: float | None, thresholds: Sequence[float] = MODEL_THRESHOLDS
) -> list[float]:
    """The score cuts worth showing: the run's own live cut, then the rungs ABOVE it.

    A rung below `t_hi` is not a stricter cut, it is the live merge set under another name —
    printing it invites reading a duplicate as a choice."""
    if t_hi is None:
        return list(thresholds)
    return sorted({float(t_hi), *(value for value in thresholds if value > t_hi)})


def model_threshold_tables(
    model_merges: Sequence[PairRow],
    *,
    thresholds: Sequence[float] = MODEL_THRESHOLDS,
    max_combo: int = MAX_COMBO,
) -> list[dict[str, Any]]:
    """The same table for the model-decided merges, at a ladder of score cuts.

    Every rung is priced against ONE denominator — the true merges of the unfiltered set — so
    `keepsTP` falls as the cut rises, which is what raising a threshold actually costs."""
    base_true = sum(1 for row in model_merges if row.y == 1)
    return [
        {
            "threshold": threshold,
            "rows": rule_table(
                [row for row in model_merges if row.score >= threshold],
                max_combo=max_combo,
                base_true=base_true,
            ),
        }
        for threshold in thresholds
    ]


# --- report ---------------------------------------------------------------------------------


@dataclass(slots=True)
class ErrorReport:
    sections: dict[str, Any] = field(default_factory=dict)
    title: str = "autodedup error analysis"

    def to_json(self) -> dict[str, Any]:
        return {"title": self.title, **self.sections}

    def headline(self) -> list[str]:
        return _headline(self)

    def to_markdown(self) -> str:
        return _markdown(self)


def analyse(
    run_pairs: Sequence[Mapping[str, Any]],
    labels: Mapping[PairKey, Label],
    judgements: Sequence[JudgementRow],
    sample: Sample | None,
    *,
    top: int = DEFAULT_TOP,
    t_hi: float | None = None,
    thresholds: Sequence[float] | None = None,
) -> ErrorReport:
    """The whole W4b readout: false merges, false rejects, the band, and the rule tables."""
    rows = build_rows(run_pairs, labels, judgements, sample)
    draw = sample if sample is not None else EMPTY_SAMPLE
    merges = [row for row in rows if row.zone == MERGE_ZONE]
    rejects = [row for row in rows if row.zone in REJECT_ZONES]
    band = [row for row in rows if row.zone == BAND_ZONE]
    ka_merges = [row for row in merges if row.certificate == "K-A"]
    model_merges = [row for row in merges if row.certificate is None]
    ladder = list(thresholds) if thresholds else threshold_ladder(t_hi)

    def merge_key(row: PairRow) -> str:
        return f"{row.certificate or 'model'}|{row.block}|{row.side}"

    def reject_key(row: PairRow) -> str:
        return f"{row.block}|{row.side}"

    sections: dict[str, Any] = {
        "summary": {
            "n_rows": len(rows),
            "n_labelled": sum(1 for row in rows if row.labelled),
            "n_judged": sum(1 for row in rows if row.y is not None),
            "n_abstain": sum(1 for row in rows if row.labelled and row.y is None),
            "top": top,
            "t_hi": t_hi,
            "thresholds": ladder,
            "sample_weighted": bool(sample is not None and sample.is_weighted),
            # A judged pair whose stratum does not inflate carries weight 1.0: the header can
            # read "HT-weighted" while part of the draw is not weighted at all (evaluate tracks
            # the same count as `n_labelled_without_sample_weight`).
            "n_labelled_without_sample_weight": sum(
                1 for row in rows if row.y is not None and not draw.inflates(row.stratum)
            ),
            "zones": _counts(row.zone for row in rows),
            "merge": _precision(
                [row for row in merges if row.y == 1], [row for row in merges if row.y == 0]
            ),
            "reject": _precision(
                [row for row in rejects if row.y == 0], [row for row in rejects if row.y == 1]
            ),
            "band": {
                "n_pairs": len(band),
                "n_positive": sum(1 for row in band if row.y == 1),
                "n_negative": sum(1 for row in band if row.y == 0),
            },
        },
        "false_merges": group_errors(merges, merge_key, wrong_y=0, top=top),
        "clean_merge_groups": clean_groups(merges, merge_key, wrong_y=0),
        "false_rejects": group_errors(
            rejects, reject_key, wrong_y=1, top=top, worst_first=False
        ),
        "clean_reject_groups": clean_groups(rejects, reject_key, wrong_y=1),
        "band_composition": band_composition(band),
        "candidate_rules_ka": rule_table(ka_merges),
        "model_thresholds": model_threshold_tables(model_merges, thresholds=ladder),
    }
    return ErrorReport(sections=sections)


# --- rendering -------------------------------------------------------------------------------


def _pct(value: float | None, digits: int = 1) -> str:
    return "-" if value is None else f"{value * 100:.{digits}f}%"


def _num(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _cell(text: str) -> str:
    """A group key inside a GFM table cell: the `|` separating its parts must be escaped.

    A code span does NOT protect a pipe in GFM — an unescaped one splits the cell, the header
    then carries more cells than the delimiter row, and the whole table renders as a paragraph
    of pipe characters."""
    return str(text).replace("|", "\\|")


def _group_lines(groups: Sequence[Mapping[str, Any]], label: str) -> list[str]:
    lines = [f"{label:<44}{'wrong':>6}{'right':>6}{'prec':>8}{'wilsonLB':>10}{'abst':>6}"]
    for group in groups:
        lines.append(
            f"  {str(group['group']):<42}{group['n_wrong']:>6}{group['n_right']:>6}"
            f"{_pct(group['precision']):>8}{_pct(group['wilson_lb']):>10}"
            f"{group['n_abstain']:>6}"
        )
    if not groups:
        lines.append("  (none)")
    return lines


def _clean_lines(groups: Sequence[Mapping[str, Any]], label: str) -> list[str]:
    """The groups with no error at all — printed so silence cannot be mistaken for absence."""
    lines = [f"{label:<44}{'judged':>6}{'prec':>8}{'wilsonLB':>10}"]
    for group in groups:
        lines.append(
            f"  {str(group['group']):<42}{group['n_judged']:>6}"
            f"{_pct(group['precision']):>8}{_pct(group['wilson_lb']):>10}"
        )
    if not groups:
        lines.append("  (none)")
    return lines


def _rule_lines(rows: Sequence[Mapping[str, Any]], prefix: str = "") -> list[str]:
    lines: list[str] = []
    for row in rows:
        lines.append(
            f"  {prefix}{str(row['rule']):<10}{row['n']:>6}{row['n_positive']:>6}"
            f"{row['n_false']:>6}{_pct(row['precision']):>9}{_pct(row['wilson_lb']):>10}"
            f"{_pct(row['kept_true_share']):>9}"
        )
    return lines


RULE_HEADER: str = (
    f"  {'rule':<10}{'n':>6}{'true':>6}{'false':>6}"
    f"{'prec':>9}{'wilsonLB':>10}{'keepsTP':>9}"
)


def _headline(report: ErrorReport) -> list[str]:
    sections = report.sections
    summary = sections["summary"]
    merge, reject = summary["merge"], summary["reject"]
    lines = [
        f"rows {summary['n_rows']}  judged {summary['n_judged']}"
        f"  abstain {summary['n_abstain']}  HT-weighted {summary['sample_weighted']}"
        f" (unweighted labels {summary['n_labelled_without_sample_weight']})",
        f"merge  judged {merge['n_judged']}  false {merge['n_wrong']}"
        f"  precision {_pct(merge['precision'])} (HT {_pct(merge['precision_ht'])})",
        f"reject+veto judged {reject['n_judged']}  false {reject['n_wrong']}"
        f"  precision {_pct(reject['precision'])} (HT {_pct(reject['precision_ht'])})",
        f"band   pairs {summary['band']['n_pairs']}"
        f"  positive {summary['band']['n_positive']}"
        f"  negative {summary['band']['n_negative']}",
        "",
        *_group_lines(sections["false_merges"], "false merges  certificate|block|side"),
        "",
        *_clean_lines(sections["clean_merge_groups"], "clean merge groups (no false merge)"),
        "",
        *_group_lines(sections["false_rejects"], "false rejects  block|side"),
        "",
        *_clean_lines(sections["clean_reject_groups"], "clean reject groups (no false reject)"),
        "",
        "K-A merges under candidate rules",
        *[f"  {rule.code} = {rule.text}" for rule in CANDIDATE_RULES],
        RULE_HEADER,
        *_rule_lines(sections["candidate_rules_ka"]),
        "",
        "model-only merges by score cut (base + single clauses;"
        " keepsTP is against ALL judged model merges)",
        RULE_HEADER.replace("  rule", "  t     rule"),
    ]
    for table in sections["model_thresholds"]:
        singles = [row for row in table["rows"] if len(row["clauses"]) <= 1]
        lines.extend(_rule_lines(singles, prefix=f"{table['threshold']:<6}"))
    return lines


def _contrast_rows(entries: Sequence[Mapping[str, Any]], limit: int) -> list[str]:
    lines = [
        "| feature | family | mean + | n + | present + | mean - | n - | present - |"
        " std diff |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for entry in entries[:limit]:
        lines.append(
            f"| {entry['feature']} | {entry['family']} | {_num(entry['mean_pos'])} |"
            f" {entry['n_present_pos']} | {_pct(entry['presence_pos'], 0)} |"
            f" {_num(entry['mean_neg'])} | {entry['n_present_neg']} |"
            f" {_pct(entry['presence_neg'], 0)} | {_num(entry['std_diff'], 2)} |"
        )
    return lines


def _pair_lines(pairs: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    for pair in pairs:
        lines.append(
            f"- **{pair['lo']} x {pair['hi']}** score {pair['score']:.4f}"
            f" · {pair['refusal']} · families {', '.join(pair['families']) or '(none)'}"
            f" · {pair['tier']} says *{pair['verdict']}*"
        )
        if pair.get("unit_discriminator"):
            lines.append(f"  - discriminator: {pair['unit_discriminator']}")
        if pair.get("key_evidence"):
            lines.append(f"  - evidence: {pair['key_evidence']}")
        lines.append(f"  - features: {', '.join(_feature_bits(pair))}")
    return lines


def _feature_bits(pair: Mapping[str, Any]) -> list[str]:
    """Each top feature next to the reference it was scored against, or the value says nothing.

    `top_features` ranks by distance from the TRUE pairs of the same group; without that mean on
    the page, `attr_contradictions=1.000` is a number the reader cannot place."""
    bits: list[str] = []
    for entry in pair.get("top_features", ()):
        name = entry["feature"]
        if entry["present"]:
            reference = entry.get("reference_mean")
            tail = "" if reference is None else f" (vs {_num(reference)})"
            bits.append(f"{name}={_num(entry['value'])}{tail}")
        else:
            presence = entry.get("reference_presence")
            tail = "" if presence is None else f" (present {_pct(presence, 0)})"
            bits.append(f"{name}=absent{tail}")
    return bits


def _markdown(report: ErrorReport) -> str:
    sections = report.sections
    summary = sections["summary"]
    lines = [f"# {report.title}", "", "## Summary", ""]
    lines.extend(f"- {line}" for line in _headline(report)[:4])
    for title, key, clean_key, label, verb in (
        ("False merges (zone merge, label 0)", "false_merges", "clean_merge_groups",
         "certificate / block / side", "merged by"),
        ("False rejects (zone reject/veto, label 1)", "false_rejects", "clean_reject_groups",
         "block / side", "refused by"),
    ):
        lines.extend(["", f"## {title}", "",
                      f"| {label} | wrong | right | precision | wilson LB | HT precision |"
                      f" abstain | unlabelled |",
                      "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
        for group in sections[key]:
            lines.append(
                f"| {_cell(group['group'])} | {group['n_wrong']} | {group['n_right']} |"
                f" {_pct(group['precision'])} | {_pct(group['wilson_lb'])} |"
                f" {_pct(group['precision_ht'])} | {group['n_abstain']} |"
                f" {group['n_unlabelled']} |"
            )
        lines.extend(["", f"Groups with no error ({label})", "",
                      f"| {label} | judged | precision | wilson LB | HT precision |",
                      "| --- | ---: | ---: | ---: | ---: |"])
        for group in sections[clean_key]:
            lines.append(
                f"| {_cell(group['group'])} | {group['n_judged']} |"
                f" {_pct(group['precision'])} | {_pct(group['wilson_lb'])} |"
                f" {_pct(group['precision_ht'])} |"
            )
        for group in sections[key]:
            rules = ", ".join(f"{name} x{count}" for name, count in group["rules"].items())
            lines.extend(["", f"### {group['group']}", "",
                          f"{verb}: {rules or '(none)'}", ""])
            lines.extend(_pair_lines(group["pairs"]))
            lines.extend(["", "Feature contrast (positives vs negatives in this group)", ""])
            lines.extend(_contrast_rows(group["contrast"], MARKDOWN_CONTRAST_ROWS))

    lines.extend(["", "## Band composition", "",
                  "| block | pairs | judged | positive | negative | positive share |"
                  " abstain | unlabelled |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for entry in sections["band_composition"]:
        lines.append(
            f"| {entry['block']} | {entry['n_pairs']} | {entry['n_judged']} |"
            f" {entry['n_positive']} | {entry['n_negative']} |"
            f" {_pct(entry['positive_share'])} | {entry['n_abstain']} |"
            f" {entry['n_unlabelled']} |"
        )
    for entry in sections["band_composition"]:
        if entry["n_judged"]:
            lines.extend(["", f"### band · {entry['block']}", ""])
            lines.extend(_contrast_rows(entry["contrast"], MARKDOWN_CONTRAST_ROWS))

    lines.extend(["", "## Candidate rules on K-A merges", ""])
    lines.extend(f"- `{rule.code}` — {rule.text}" for rule in CANDIDATE_RULES)
    lines.extend(["", "| rule | n | true | false | precision | wilson LB | HT precision |"
                  " keeps true merges |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for row in sections["candidate_rules_ka"]:
        lines.append(
            f"| {row['rule']} | {row['n']} | {row['n_positive']} | {row['n_false']} |"
            f" {_pct(row['precision'])} | {_pct(row['wilson_lb'])}"
            f" | {_pct(row['precision_ht'])} | {_pct(row['kept_true_share'])} |"
        )
    lines.extend(["", "## Model-only merges by score cut", "",
                  "`keeps true merges` is measured against ALL judged model merges, one fixed"
                  " denominator for every rung; `at this cut` re-bases on the rung itself.", "",
                  "| t | rule | n | true | false | precision | wilson LB |"
                  " keeps true merges | at this cut |",
                  "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for table in sections["model_thresholds"]:
        for row in table["rows"]:
            lines.append(
                f"| {table['threshold']} | {row['rule']} | {row['n']} | {row['n_positive']} |"
                f" {row['n_false']} | {_pct(row['precision'])} | {_pct(row['wilson_lb'])} |"
                f" {_pct(row['kept_true_share'])} | {_pct(row['kept_true_share_local'])} |"
            )
    lines.append("")
    return "\n".join(lines)
