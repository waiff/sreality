"""What W12 shipped, and — louder — what it did not.

The wave pre-registered E88's two definitions, measured them on a fresh seal's dev +
validation with `w6_gold` unrefitted, and the pre-stated choice rule named NO candidate: both
holds cost more reliable dev duplicates than the honest clock gains. The verification then
opened the seal ONCE and measured the NARROW row strictly WORSE than the shipped engine (145
of 238 sealed reliable duplicates against g6's 177, McNemar gained 7 / lost 39, p < 1e-5, 0
false merges removed), so E89 takes back the payment `validate` briefly accepted from the hold
and the three W12 rows move to `settings/refuted/`.

Nothing is promoted; g6 (`settings/w8.json` + `models/w6_gold.json`) stays the shipped
generation. These assertions are the rail against a later session reading any of it as one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodedup import development as D
from autodedup import seals
from autodedup.evaluate import split_seal
from autodedup.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "autodedup"
W8 = Settings.from_json(ROOT / "settings/w8.json")
W12_SEAL = "ebc141fa51555bf7e2dd1757d84fb912e907c377e927e1b5c6de6132844d1b24"
W11_SEAL = "00e2cb2fe4fe1e8ee9886729ef3420ebaa6aec059934b20e30630751500032b4"

# dev + validation of the W12 seal, `w6_gold` unrefitted, reliable tier = operator + gold minus
# the DISPUTED and CONTESTED pairs. The sealed side was never opened by the build.
HONEST_GAIN_OVER_G6 = 165
NARROW_COST = 212
WIDE_COST = 297
COST_BUDGET = HONEST_GAIN_OVER_G6 / 3.0

# The W12 seal's TEST side, opened once by the verification (M110). Reliable labelled pairs:
# 238 positives, 85 negatives, 0 contested.
SEALED_DUPS = 238
SEALED = {"g6": 177, "candidate": 145, "honest": 189}
SEALED_MCNEMAR = {"candidate": (7, 39), "honest": (12, 0)}
# pair / block / family / cluster, every arm, contested in or out — the bar, met by all three.
SEALED_FALSE_MERGES = (0, 0, 0, 0)


def test_the_shipped_generation_is_still_g6_and_the_hold_is_off() -> None:
    assert W8.development_hold_mode == "off"
    assert W8.live_window_from_sighting is False
    assert Settings().development_hold_mode == "off"
    assert not (ROOT / "settings/w12.json").exists(), "W12 promoted nothing"
    assert not (ROOT / "settings/w12_strata.json").exists(), "and has no promoted strata"
    assert not (ROOT / "models/w12_gold.json").exists(), "the model stays w6_gold, unrefitted"


def test_the_pre_stated_choice_rule_named_no_candidate() -> None:
    """Both pre-registered holds cost more true duplicates than the honest clock buys."""
    assert NARROW_COST > COST_BUDGET and WIDE_COST > COST_BUDGET
    assert NARROW_COST > HONEST_GAIN_OVER_G6 and WIDE_COST > HONEST_GAIN_OVER_G6, (
        "each hold gives back more than the whole gain it was meant to protect"
    )


def test_the_sealed_read_refuses_the_candidate_the_dev_rule_already_refused() -> None:
    """M110: the holdout agrees with the wave's own rule, and says it louder.

    The candidate is DOMINATED — it pays 32 net true merges and removes no false one — so no
    threshold or scope makes promoting it better than leaving g6 alone, which is why this wave
    names no conditions under which the hold could still ship."""
    gained, lost = SEALED_MCNEMAR["candidate"]
    assert SEALED["candidate"] < SEALED["g6"] and gained - lost == -32
    assert SEALED_FALSE_MERGES == (0, 0, 0, 0), "both arms already met the safety bar"
    honest_gained, honest_lost = SEALED_MCNEMAR["honest"]
    assert SEALED["honest"] > SEALED["g6"] and honest_lost == 0 and honest_gained == 12, (
        "the clock the hold was built to pay for is the arm that wins the holdout"
    )


def test_the_three_w12_rows_are_records_outside_settings_and_do_not_build() -> None:
    """E87's precedent, applied to E89's refutation: `settings/` holds rows the engine can
    build, and a refuted arm keeps its record beside it under `settings/refuted/`."""
    for name in ("w12_candidate", "w12_narrow", "w12_wide"):
        assert not (ROOT / f"settings/{name}.json").exists(), f"{name} is not a shipped row"
        record = ROOT / f"settings/refuted/{name}.json"
        raw = json.loads(record.read_text(encoding="utf-8"))
        assert raw["live_window_from_sighting"] is True
        assert raw["certificate_b_min_gap_days"] > 0.0, "E84 is required under the honest clock"
        assert raw.get("certificate_b_min_images", 0.0) == 0.0, "no E65 floor — the hold paid"
        assert raw.get("family_guard_mode", "off") == "off", "E85 stays off (D30 ii)"
        assert raw.get("catalog_carrier_aware", False) is False, "E83 stays off (D28 ii)"
        with pytest.raises(ValueError, match="E89"):
            Settings.from_json(record)
    narrow = json.loads(
        (ROOT / "settings/refuted/w12_candidate.json").read_text(encoding="utf-8")
    )
    assert narrow["development_hold_mode"] == "narrow", "the candidate row IS the NARROW arm"
    wide = json.loads((ROOT / "settings/refuted/w12_wide.json").read_text(encoding="utf-8"))
    assert wide["development_hold_mode"] == "wide"


def test_every_row_under_settings_is_still_constructible() -> None:
    for path in sorted((ROOT / "settings").glob("*.json")):
        if path.name.endswith("_strata.json"):
            continue
        Settings.from_json(path)


def test_the_pre_registered_vocabulary_and_caps_are_what_was_measured() -> None:
    """A term list or a cap that moved after the measurement makes the measurement a fit."""
    assert D.PROJECT_TERMS == ("rezidenc", "projekt", "developer", "novostavb", "etap",
                               "cenik", "kolaudac", "dokonceni")
    assert D.COOP_TERMS == ("podil", "anuit", "druzstevn")
    defaults = Settings()
    assert defaults.development_vocab_min_share == 0.50
    assert defaults.development_coop_min_size == 3
    assert defaults.development_size_min_narrow == 3
    assert defaults.development_size_min_wide == 4
    assert defaults.development_block_density_min == 20


def test_the_fresh_seal_is_committed_carries_its_seed_and_is_named_by_both() -> None:
    """W12's map reproduces W11's to the listing, so the fresh holdout is a fresh SEED and the
    name has to carry it — otherwise the only place to commit it is on top of a SPENT seal."""
    groups = seals.load(W12_SEAL)
    assert len(groups) == 4456 and len(set(groups.values())) == 664
    assert seals.seed_for(W12_SEAL) == 20260925
    assert seals.seal_id(groups, 20260925) == W12_SEAL
    assert split_seal(groups)["sha256"] == W11_SEAL, "same map, different seed, different seal"
    assert seals.seed_for(W11_SEAL) == 20260923, "the spent seal's own seed is untouched"


def test_the_w12_seal_is_registered_spent_and_says_what_it_is_silent_about() -> None:
    """M112: no contested pair landed on its test side, so 0 sealed false merges there is
    SILENCE about the hazard that refused W11, not an acquittal of it."""
    reason = seals.spent(W12_SEAL)
    assert reason and "145 of 238" in reason
    assert "NO contested pair" in reason and "silent" in reason


def test_the_seal_that_decided_W11_stays_registered_spent() -> None:
    reason = seals.spent(W11_SEAL)
    assert reason and "FIXPOINT" in reason and W12_SEAL[:8] in reason


def test_the_honest_clock_still_cannot_run_for_free() -> None:
    with pytest.raises(ValueError, match="needs a price"):
        Settings(live_window_from_sighting=True, certificate_b_min_gap_days=1.0 / 1440.0)


def test_lifting_the_hold_is_a_settings_change_and_the_lane_is_not_part_of_it() -> None:
    """The hold ships as DATA: `development.holds` is live code any row can turn on, and the
    real-time lane keeps g6's rules until it carries a family index (E88's rail, E64-shaped)."""
    from autodedup import incremental

    assert hasattr(D, "holds") and set(D.MODES) == {"off", "narrow", "wide"}
    settings = Settings()
    settings.development_hold_mode = "narrow"
    settings.certificate_b_min_gap_days = 1.0 / 1440.0
    with pytest.raises(NotImplementedError, match="development_hold_mode"):
        incremental.run_pass(
            None, None, None, settings, None, None,  # type: ignore[arg-type]
        )
