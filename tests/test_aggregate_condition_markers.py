"""Hermetic tests for scripts/aggregate_condition_markers.py.

Exercises the clustering / dedup logic over hand-crafted near-duplicate
Czech marker phrases. No DB connection — we call
`cluster_markers` directly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def _load_aggregate_module() -> Any:
    """Load scripts/aggregate_condition_markers.py as a module.

    The scripts/ folder is not a package, so we have to load it via
    spec_from_file_location rather than `from scripts.foo import bar`.
    """
    if "aggregate_condition_markers" in sys.modules:
        return sys.modules["aggregate_condition_markers"]
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "aggregate_condition_markers.py"
    spec = importlib.util.spec_from_file_location(
        "aggregate_condition_markers", path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["aggregate_condition_markers"] = module
    spec.loader.exec_module(module)
    return module


def _row(text: str, *, scope: str = "building",
         sentiment: str = "positive",
         level_hint: str = "high",
         source: str = "text") -> dict[str, Any]:
    return {
        "marker_text": text,
        "scope": scope,
        "evidence_quote": f"... {text} ...",
        "sentiment": sentiment,
        "suggested_level_implication": level_hint,
        "source": source,
    }


def test_identical_phrases_cluster_into_one():
    agg = _load_aggregate_module()
    rows = [_row("zateplená budova") for _ in range(5)]
    clusters = agg.cluster_markers(
        rows, similarity_threshold=0.85, token_jaccard_threshold=0.7,
    )
    assert len(clusters) == 1
    assert clusters[0]["count"] == 5
    assert clusters[0]["canonical"] == "zateplená budova"


def test_near_duplicates_cluster_together():
    agg = _load_aggregate_module()
    rows = [
        _row("zateplená budova"),
        _row("zateplená budova"),
        _row("Zateplená budova"),
        _row("zateplena budova"),
        _row("zateplená fasáda"),
    ]
    clusters = agg.cluster_markers(
        rows, similarity_threshold=0.85, token_jaccard_threshold=0.7,
    )
    canonicals = {c["canonical"] for c in clusters}
    # First four normalise to identical or near-identical strings →
    # one cluster. "zateplená fasáda" differs in the second token but
    # shares "zateplená" — token Jaccard < 0.7, ratio < 0.85, so it
    # should be its own cluster.
    assert len(clusters) == 2
    top = max(clusters, key=lambda c: c["count"])
    assert top["count"] == 4
    assert top["canonical"] in {"zateplená budova", "Zateplená budova"}
    assert "zateplená fasáda" in canonicals


def test_distinct_phrases_stay_separate():
    agg = _load_aggregate_module()
    rows = [
        _row("zateplená budova"),
        _row("nová střecha"),
        _row("po kompletní rekonstrukci", scope="apartment"),
        _row("původní jádro", scope="apartment", sentiment="negative"),
    ]
    clusters = agg.cluster_markers(
        rows, similarity_threshold=0.85, token_jaccard_threshold=0.7,
    )
    assert len(clusters) == 4
    counts = {c["canonical"]: c["count"] for c in clusters}
    assert all(v == 1 for v in counts.values())


def test_diacritic_insensitive_clustering():
    """"po kompletni rekonstrukci" and "po kompletní rekonstrukci"
    should normalise to the same key after NFKD diacritic strip."""
    agg = _load_aggregate_module()
    rows = [
        _row("po kompletní rekonstrukci", scope="apartment"),
        _row("po kompletni rekonstrukci", scope="apartment"),
        _row("Po Kompletní Rekonstrukci", scope="apartment"),
    ]
    clusters = agg.cluster_markers(
        rows, similarity_threshold=0.85, token_jaccard_threshold=0.7,
    )
    assert len(clusters) == 1
    assert clusters[0]["count"] == 3


def test_majority_sentiment_picked():
    agg = _load_aggregate_module()
    rows = [
        _row("původní okna", scope="apartment", sentiment="negative"),
        _row("původní okna", scope="apartment", sentiment="negative"),
        _row("původní okna", scope="apartment", sentiment="neutral"),
    ]
    clusters = agg.cluster_markers(
        rows, similarity_threshold=0.85, token_jaccard_threshold=0.7,
    )
    assert clusters[0]["sentiment_majority"] == "negative"
    assert clusters[0]["sentiment_counts"]["negative"] == 2
    assert clusters[0]["sentiment_counts"]["neutral"] == 1


def test_normalise_strips_diacritics_and_lowercases():
    agg = _load_aggregate_module()
    assert agg._normalise("Zateplená BUDOVA") == "zateplena budova"
    assert agg._normalise("  Po   kompletní    rekonstrukci  ") == "po kompletni rekonstrukci"
    assert agg._normalise("") == ""


def test_empty_marker_text_is_skipped():
    agg = _load_aggregate_module()
    rows = [_row(""), _row("nová střecha")]
    clusters = agg.cluster_markers(
        rows, similarity_threshold=0.85, token_jaccard_threshold=0.7,
    )
    assert len(clusters) == 1
    assert clusters[0]["canonical"] == "nová střecha"
