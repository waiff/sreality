"""The challenger's score (GLOBAL_SEARCH 1.4, rung 2): one boosted pair model, read from a JSON model
file and evaluated in pure Python, because the worker image has no numpy and no scikit-learn (rule
7). The lab's exporter writes the file from a fitted HistGradientBoostingClassifier (the training
extra); `tests/autodedup/test_challenger.py` holds this evaluation to scikit-learn's predict_proba
bit for bit, calibration included.

The file (`format` FORMAT): `feature_order` (the vector a caller passes), `inputs` (the vector index
each tree input reads), `fill` (vector index -> the value a MISSING entry reads as: a column that is
constant when present splits on its presence), `baseline` (the raw start), `trees` (one flat node
list per tree: [input, threshold, missing_left, left, right, value, leaf]; a value at or below the
threshold goes left, a missing one where `missing_left` says), `calibration` (the isotonic map: knots
`x`, `y` and the clip range `lo`, `hi`; null = the raw probability) and `card` (what trained it).

The isotonic map is read as `numpy.interp` reads it (scipy's linear interp1d delegates there): a
knot reads its own value and a segment `slope * (t - x0) + y0`, so a plateau returns its knot value
exactly. p equals scikit-learn's bit for bit (the committed fixture holds 1,000 vectors to that),
and two pairs on one plateau tie exactly whatever the last bits of `exp`."""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

FORMAT: str = "autodedup-gbt/1"

Scorer = Callable[[Sequence["float | None"]], float]


def scorer(model: str | Path | Mapping[str, Any]) -> Scorer:
    """The model file (a path or its parsed JSON) as `p(x)`: `x` follows the file's
    `feature_order`, None or NaN is missing; returns the calibrated probability of one property."""
    spec = model if isinstance(model, Mapping) else json.loads(Path(model).read_text("utf-8"))
    if spec.get("format") != FORMAT:
        raise ValueError(f"not a {FORMAT} model file: {spec.get('format')!r}")
    inputs = [int(j) for j in spec["inputs"]]
    fill = {int(j): float(v) for j, v in (spec.get("fill") or {}).items()}
    baseline = float(spec["baseline"])
    trees = [[(int(n[0]), float(n[1]), bool(n[2]), int(n[3]), int(n[4]), float(n[5]), bool(n[6]))
              for n in tree] for tree in spec["trees"]]
    knots = spec.get("calibration")
    xs = [float(v) for v in knots["x"]] if knots else []
    ys = [float(v) for v in knots["y"]] if knots else []
    lo, hi = (float(knots["lo"]), float(knots["hi"])) if knots else (0.0, 0.0)

    def p(x: Sequence[float | None]) -> float:
        row: list[float | None] = []
        for j in inputs:
            value = x[j]
            if value is None or value != value:
                value = fill.get(j)
            row.append(value)
        raw = baseline
        for nodes in trees:
            node = nodes[0]
            while not node[6]:
                value = row[node[0]]
                if value is None:
                    node = nodes[node[3] if node[2] else node[4]]
                else:
                    node = nodes[node[3] if value <= node[1] else node[4]]
            raw += node[5]
        try:
            prob = 1.0 / (1.0 + math.exp(-raw))
        except OverflowError:
            prob = 0.0
        return _isotonic(prob, xs, ys, lo, hi) if knots else prob

    return p


def _isotonic(t: float, xs: list[float], ys: list[float], lo: float, hi: float) -> float:
    """scikit-learn's IsotonicRegression.predict on one value: clip, then `numpy.interp` (which
    scipy's linear interp1d delegates to) step for step: a knot reads its own value, a segment
    `slope * (t - x0) + y0`."""
    t = min(max(t, lo), hi)
    j = bisect_right(xs, t) - 1
    if j < 0:
        return ys[0]
    if j >= len(xs) - 1:
        return ys[-1]
    if xs[j] == t:
        return ys[j]
    return (ys[j + 1] - ys[j]) / (xs[j + 1] - xs[j]) * (t - xs[j]) + ys[j]
