"""What generation g7 ships — and what it does NOT (D24).

g7 is a CANDIDATE, not a promotion: it carries 5 sealed reliable false merges against g6's 0,
its reliable merge recall is 0.6659 against g6's 0.7588, and its band is 4,913 against 4,907.
These tests pin the artifacts so the candidate stays reproducible and so the two things that
DID hold — the E65 floor's pairing with the honest clock, and the seal discipline — cannot be
loosened by accident.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodedup import seals
from autodedup.model import LogisticModel
from autodedup.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "autodedup"
W9 = Settings.from_json(ROOT / "settings/w9.json")
W8 = Settings.from_json(ROOT / "settings/w8.json")
W9_SEAL = "fb9df2ea9fd773bf0eda256d00894924ba4b8491cc7559181f2c48da75e59884"


def test_w9_runs_the_honest_clock_and_therefore_carries_the_floor() -> None:
    assert W9.live_window_from_sighting is True
    assert W9.certificate_b_min_images == 1.0
    assert W9.certificate_b_min_matched_images == 1.0
    # The pairing is enforced, not remembered (E65).
    with pytest.raises(ValueError, match="needs the E65 image floor"):
        Settings.from_dict({**W9.to_dict(), "certificate_b_min_images": 0.0,
                            "certificate_b_min_matched_images": 0.0})


def test_w9_keeps_every_w8_rule_switch() -> None:
    for name in ("certificate_kr_enabled", "unit_designator_veto", "context_rule_enabled",
                 "bridge_apply", "bridge_min_score", "store_floor", "t_hi"):
        assert getattr(W9, name) == getattr(W8, name), name


def test_the_stratum_row_is_spelled_out_cell_by_cell() -> None:
    """An ABSENT key runs on the global cut; W9 states every cell so the row can be read."""
    row = W9.t_hi_by_stratum
    assert row["K-C|same"] is None and row["K-A|same"] is None and row["K-A|cross"] is None
    assert row["K-B|same"] == 1.0 and row["K-R|same"] == 1.0 and row["K-R|cross"] == 1.0
    # The two cells the dev-side rule opened, and the five sealed false merges they bought.
    assert row["model|cross"] is not None and row["model|same"] is not None


def test_w9_gold_names_the_fresh_seal_and_the_map_is_committed() -> None:
    model = LogisticModel.from_json(
        json.loads((ROOT / "models/w9_gold.json").read_text(encoding="utf-8"))
    )
    provenance = model.provenance
    assert (provenance["seal"] or {})["sha256"] == W9_SEAL
    assert seals.committed(W9_SEAL)
    assert seals.seed_for(W9_SEAL) == provenance["training"]["seed"] == 20260922
    # The briefed map lost on validation and the model says which map it actually carries (M60).
    assert provenance["training"]["calibration"] == "platt"
    assert "isotonic_pav" in provenance["calibration_note"]
    assert provenance["label_run_excluded"].startswith("35205840437")


def test_the_spent_seal_is_not_the_one_w9_chose_on() -> None:
    assert seals.spent(W9_SEAL) is None
    assert seals.spent("37c8771fda6b06db2ead790fcf7728e0ccb0e80c60cad5358c2905ed39be52cc")
