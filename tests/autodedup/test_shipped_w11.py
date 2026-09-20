"""What W11 ships: NOTHING, and the three rails its verification bought (E86, E87, D30).

The wave built the K-B family guard (E85), cut a fresh seal, and then the verifier read that
seal once: the honest clock gains 47 sealed labelled duplicates over g6 and loses none, while
the guard itself changes NOTHING on the holdout (gained 0, lost 0, every demotion unlabelled).
Both candidate arms carry the same sealed reliable false merge, gold `522698 x 13221982`, so
both miss the promotion bar's first clause — zero reliable false merges at pair, block and
family grain — and g6 stays the shipped generation.

What survives the wave is not a promotion but three rails: the guard is DATA and OFF with its
structural case withdrawn (M99/M100), its verdict must be an invariant of the family and not of
the order the family arrived in (E86), and a gate may only take a price a sealed read has
measured (E87).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodedup import family, seals
from autodedup.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "autodedup"
W8 = Settings.from_json(ROOT / "settings/w8.json")
W11_SEAL = "00e2cb2fe4fe1e8ee9886729ef3420ebaa6aec059934b20e30630751500032b4"


def test_w11_promotes_nothing_so_there_is_no_promoted_settings_row() -> None:
    """D30: a `w11.json` would BE the promotion. The shipped generation is still g6."""
    assert not (ROOT / "settings/w11.json").exists()
    assert not (ROOT / "settings/w11_strata.json").exists()
    assert not (ROOT / "models/w11_gold.json").exists()
    assert (ROOT / "settings/w8.json").exists()
    assert (ROOT / "models/w6_gold.json").exists()


def test_the_shipped_row_runs_the_detection_clock_and_no_guard() -> None:
    assert W8.family_guard_mode == "off"
    assert W8.live_window_from_sighting is False
    assert Settings().family_guard_mode == "off"


def test_every_row_under_settings_is_constructible_and_the_refuted_arm_is_not_one() -> None:
    """E87: `settings/` holds rows the engine can build; the refuted arm is a record beside it."""
    for path in sorted((ROOT / "settings").glob("*.json")):
        if path.name.endswith("_strata.json"):
            continue
        Settings.from_json(path)
    record = ROOT / "settings/refuted/w11_candidate.json"
    assert json.loads(record.read_text(encoding="utf-8"))["family_guard_mode"] == "cell"
    with pytest.raises(ValueError, match="E87"):
        Settings.from_json(record)


def test_the_sealed_split_that_decided_the_wave_is_registered_spent() -> None:
    reason = seals.spent(W11_SEAL)
    assert reason and "gained 0, lost 0" in reason


def test_the_guard_is_forbidden_in_the_real_time_lane_in_every_mode() -> None:
    """The lane keeps g6's rules until it carries a family index: E85 raises, whatever the mode.

    `pair` is the one mode E86 leaves buildable there (its verdict reads only the edge's own two
    adverts), but buildable is not measured, so the raise covers it too."""
    from autodedup import incremental

    source = Path(incremental.__file__).read_text(encoding="utf-8")
    assert 'settings.family_guard_mode != "off"' in source
    assert "family_guard_mode (E85) has no incremental family index" in source
    assert set(family.MODES) - {"off"} == {"family", "cell", "pair"}
