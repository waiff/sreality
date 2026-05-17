"""Hermetic tests for the Phase A curator + rubric integrity check.

Two concerns:
  1. The curator's whitelist rule applies correctly across the four
     combinations of count / level_hint / sentiment.
  2. Every marker_id referenced by `data/condition_rubric_v1.json`
     actually exists in `data/condition_markers_curated.json` —
     prevents silent rubric drift if either file is regenerated.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def _load_curator() -> Any:
    if "curate_condition_markers" in sys.modules:
        return sys.modules["curate_condition_markers"]
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "curate_condition_markers.py"
    spec = importlib.util.spec_from_file_location(
        "curate_condition_markers", path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["curate_condition_markers"] = module
    spec.loader.exec_module(module)
    return module


def _cluster(
    *, marker_id: str, count: int, sentiment: str, level_hint: str,
) -> dict[str, Any]:
    return {
        "marker_id": marker_id,
        "canonical": f"test {marker_id}",
        "count": count,
        "sentiment_majority": sentiment,
        "level_hint_majority": level_hint,
    }


def test_whitelist_keeps_high_count_regardless_of_level_or_sentiment():
    curator = _load_curator()
    # count >= 10 wins even for neutral / low
    assert curator.passes_whitelist(
        _cluster(marker_id="A1", count=100, sentiment="neutral", level_hint="low"),
        min_count=10,
    ) is True


def test_whitelist_keeps_rare_high_signal_negative():
    """The crucial rare-but-critical case: 'umakartové jádro' = 7 hits."""
    curator = _load_curator()
    assert curator.passes_whitelist(
        _cluster(marker_id="A1", count=7, sentiment="negative", level_hint="high"),
        min_count=10,
    ) is True


def test_whitelist_keeps_rare_high_signal_positive():
    curator = _load_curator()
    assert curator.passes_whitelist(
        _cluster(marker_id="B1", count=5, sentiment="positive", level_hint="high"),
        min_count=10,
    ) is True


def test_whitelist_drops_rare_high_signal_neutral():
    """High level_hint with neutral sentiment is not enough — these
    tend to be structure types like 'cihlová stavba' rather than
    condition signals."""
    curator = _load_curator()
    assert curator.passes_whitelist(
        _cluster(marker_id="B1", count=5, sentiment="neutral", level_hint="high"),
        min_count=10,
    ) is False


def test_whitelist_drops_rare_medium_signal():
    curator = _load_curator()
    assert curator.passes_whitelist(
        _cluster(marker_id="A1", count=5, sentiment="positive", level_hint="medium"),
        min_count=10,
    ) is False


def test_curate_preserves_marker_ids():
    """Curator must NOT renumber IDs — rubric stability depends on it."""
    curator = _load_curator()
    raw = {
        "schema_version": 1,
        "total_extractions": 100,
        "building": [
            _cluster(marker_id="B003", count=50, sentiment="positive", level_hint="high"),
            _cluster(marker_id="B007", count=4, sentiment="neutral", level_hint="low"),  # dropped
            _cluster(marker_id="B099", count=2, sentiment="negative", level_hint="high"),  # kept
        ],
        "apartment": [],
    }
    out = curator.curate(raw, min_count=10)
    kept_ids = [c["marker_id"] for c in out["building"]]
    assert kept_ids == ["B003", "B099"]


# --- Rubric integrity ------------------------------------------------------


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_rubric_marker_ids_exist_in_curated_dictionary():
    root = _project_root()
    rubric_path = root / "data" / "condition_rubric_v1.json"
    curated_path = root / "data" / "condition_markers_curated.json"
    if not rubric_path.is_file() or not curated_path.is_file():
        # Files may be absent in a partial checkout; skip rather than
        # fail so the test passes on branches that don't ship the data.
        import pytest
        pytest.skip("rubric or curated dictionary missing in this checkout")

    rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
    curated = json.loads(curated_path.read_text(encoding="utf-8"))

    bld_ids = {c["marker_id"] for c in curated["building"]}
    apt_ids = {c["marker_id"] for c in curated["apartment"]}

    for lvl in rubric["building_levels"]:
        for mid in lvl["required_marker_ids"] + lvl["disqualifying_marker_ids"]:
            assert mid in bld_ids, (
                f"rubric building level {lvl['level']} references unknown "
                f"marker_id {mid!r}"
            )
    for lvl in rubric["apartment_levels"]:
        for mid in lvl["required_marker_ids"] + lvl["disqualifying_marker_ids"]:
            assert mid in apt_ids, (
                f"rubric apartment level {lvl['level']} references unknown "
                f"marker_id {mid!r}"
            )


def test_rubric_level_count_matches_arrays():
    root = _project_root()
    rubric_path = root / "data" / "condition_rubric_v1.json"
    if not rubric_path.is_file():
        import pytest
        pytest.skip("rubric missing in this checkout")
    rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
    assert len(rubric["building_levels"]) == rubric["level_count"]
    assert len(rubric["apartment_levels"]) == rubric["level_count"]


def test_rubric_levels_are_consecutive_and_descending():
    root = _project_root()
    rubric_path = root / "data" / "condition_rubric_v1.json"
    if not rubric_path.is_file():
        import pytest
        pytest.skip("rubric missing in this checkout")
    rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
    n = rubric["level_count"]
    for arr in (rubric["building_levels"], rubric["apartment_levels"]):
        levels = [lvl["level"] for lvl in arr]
        assert levels == list(range(n, 0, -1)), (
            f"rubric levels not in descending {n}..1 order: {levels}"
        )


def test_rubric_confidence_policy_pins_silent_default():
    """Silent listings with no fallback signal MUST land on level 3 with
    confidence < 0.2. Guards against accidental drift in either the
    forced level or the upper confidence bound."""
    root = _project_root()
    rubric_path = root / "data" / "condition_rubric_v1.json"
    if not rubric_path.is_file():
        import pytest
        pytest.skip("rubric missing in this checkout")
    rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
    policy = rubric.get("confidence_policy")
    assert policy is not None, "rubric missing confidence_policy section"
    bands = {b["name"]: b for b in policy["bands"]}
    silent = bands.get("silent_no_fallback")
    assert silent is not None, "confidence_policy missing silent_no_fallback band"
    assert silent["forced_level"] == 3, (
        f"silent_no_fallback forced_level must be 3, got {silent['forced_level']}"
    )
    lo, hi = silent["confidence_range"]
    assert lo == 0.0 and hi <= 0.20, (
        f"silent_no_fallback confidence_range must be [0.0, <=0.20], got [{lo}, {hi}]"
    )


def test_rubric_fallback_chain_has_expected_steps():
    """Fallback chain is the contract the Phase B scorer reads. Pin the
    four steps so a casual edit can't drop one (e.g. accidentally
    removing the listings.condition fallback would silently change scorer
    behaviour for ~20% of listings with no apartment markers)."""
    root = _project_root()
    rubric_path = root / "data" / "condition_rubric_v1.json"
    if not rubric_path.is_file():
        import pytest
        pytest.skip("rubric missing in this checkout")
    rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
    chain = rubric.get("fallback_chain")
    assert isinstance(chain, list) and len(chain) == 4, (
        f"fallback_chain must have exactly 4 steps, got {len(chain) if chain else None}"
    )
    joined = " ".join(chain).lower()
    assert "curated markers" in joined
    assert "listings.condition" in joined
    assert "hard default" in joined or "default" in joined
