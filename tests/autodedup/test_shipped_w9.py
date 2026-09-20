"""What W9 ships, and what it does NOT (D24/D25).

g7 is a REFUTED candidate, not a promotion: 5 sealed reliable false merges against g6's 0,
reliable merge recall 0.6659 against 0.7588, band 4,913 against 4,907 — and at the INCUMBENT's
own cuts (arm x1 against f2) the refitted model still loses recall while adding false merges, so
the refit is a source of the damage and not only the stratum row chosen on top of it.

So the shipped generation stays g6 = `settings/w8.json` + `models/w6_gold.json`. These tests pin
the three things W9 leaves behind — the E65 floor's pairing with the honest clock, the seal
discipline, and the fact that NO settings file carries a cut the refit chose.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodedup import seals
from autodedup.model import LogisticModel
from autodedup.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "autodedup"
W8 = Settings.from_json(ROOT / "settings/w8.json")
ARM = Settings.from_json(ROOT / "settings/w9_honest_arm.json")
CANDIDATE = Settings.from_json(ROOT / "settings/w9_g7_candidate.json")
W9_SEAL = "fb9df2ea9fd773bf0eda256d00894924ba4b8491cc7559181f2c48da75e59884"
STRATA = json.loads((ROOT / "settings/w9_strata.json").read_text(encoding="utf-8"))


def test_the_shipped_row_is_still_g6_on_the_detection_clock() -> None:
    """W9 promotes no generation. w8.json is untouched, and the honest clock stays off there."""
    assert W8.live_window_from_sighting is False
    assert W8.certificate_b_min_images == 0.0
    assert Settings().live_window_from_sighting is False


def test_the_honest_clock_arm_carries_the_floor_and_the_incumbents_cuts() -> None:
    assert ARM.live_window_from_sighting is True
    assert ARM.certificate_b_min_images == 1.0
    assert ARM.certificate_b_min_matched_images == 1.0
    # Every cut is the incumbent's (D21's 0.9788, `model|same` propose-only): the one clean arm
    # W9 produced changes the CLOCK and the certificate, never a threshold.
    assert ARM.t_hi_by_stratum == W8.t_hi_by_stratum
    for name in ("certificate_kr_enabled", "unit_designator_veto", "context_rule_enabled",
                 "bridge_apply", "bridge_min_score", "store_floor", "t_hi", "t_lo"):
        assert getattr(ARM, name) == getattr(W8, name), name


def test_the_pairing_of_the_honest_clock_and_the_floor_is_enforced_not_remembered() -> None:
    with pytest.raises(ValueError, match="needs the E65 image floor"):
        Settings.from_dict({**ARM.to_dict(), "certificate_b_min_images": 0.0,
                            "certificate_b_min_matched_images": 0.0})


def test_no_shipped_settings_file_carries_a_cut_the_refit_chose() -> None:
    """E68/D25. The two W9 dev-chosen cuts sit 1.4e-5 and 1.0e-4 above a labelled must-not-link;
    they survive only in the candidate file, which exists to keep g7 reproducible."""
    for path in sorted((ROOT / "settings").glob("*.json")):
        if path.name.endswith("_strata.json") or path.name == "w9_g7_candidate.json":
            continue
        row = Settings.from_json(path).t_hi_by_stratum or {}
        assert row.get("model|same") != 0.999188, path.name
        assert row.get("model|cross") != 0.967765, path.name
    assert CANDIDATE.t_hi_by_stratum["model|same"] == 0.999188
    assert CANDIDATE.t_hi_by_stratum["model|cross"] == 0.967765


def test_the_candidate_is_recorded_as_refuted_with_the_numbers_that_refute_it() -> None:
    bar = STRATA["promotion_bar"]
    assert all(value.startswith("FAILED") for value in bar.values())
    arms = STRATA["verification"]["confirmed"]["the refit loses at MATCHED clock, floor and cuts"]
    assert arms["x1 (w9_gold, same everything else)"]["recall_reliable"] < \
        arms["f2 (w6_gold, honest clock, floor, g6 cuts)"]["recall_reliable"]
    assert arms["x1 (w9_gold, same everything else)"]["reliable_neg"] == 2
    assert STRATA["settings_files"]["autodedup/settings/w9_honest_arm.json"]


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


def test_the_seal_w9_chose_on_is_now_spent_too_and_still_committed() -> None:
    """It was clean when W9 cut it; W10's verification was its second read (D28 v), so W11
    registered it spent and opened a fresh one. The map stays: `w9_gold` names it."""
    assert seals.spent(W9_SEAL)
    assert seals.committed(W9_SEAL)
    assert seals.spent("37c8771fda6b06db2ead790fcf7728e0ccb0e80c60cad5358c2905ed39be52cc")


def test_the_sealed_read_log_names_every_read_including_the_audits() -> None:
    """A read the log omits is a read that happened anyway (W9a's correction to Track A)."""
    log = " ".join(STRATA["sealed_reads"])
    assert "kb_audit" in log and "no split filter" in log
    assert "harness fit" in log
