"""What W13 built: the honest clock priced by E84 alone (E95), and the numbers behind it.

The two pairs that refused the arm in W11 — `522698 x 13221982` and its dev-side twin
`555448 x 18626270`, both inside the ceskereality Rezidence K Botiči families — were ruled
`same` by the OPERATOR on 2026-09-20. Operator labels outrank gold, so the sealed read W11
already took of exactly this arm (347 of 406 against g6's 300, gained 47, lost 0) becomes the
price E87/E89 kept asking for. `settings/w13.json` is that row.

Nothing here promotes a generation: that is the assembler's call on the fresh seal
`510db099…`, which this build cut and did not open.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodedup import seals
from autodedup.evaluate import split_seal
from autodedup.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "autodedup"
W8 = Settings.from_json(ROOT / "settings/w8.json")
W13 = Settings.from_json(ROOT / "settings/w13.json")
GAP = 1.0 / 1440.0
W13_SEAL = "510db099bed2d473827257ebc2211f00bbbf0beceff23906d3567e3c7f0dd6c1"
W11_SEAL = "00e2cb2fe4fe1e8ee9886729ef3420ebaa6aec059934b20e30630751500032b4"

OPERATOR_RULED = ((522698, 13221982), (555448, 18626270))
# dev + validation of seal 510db099, `w6_gold` unrefitted, reliable tier = operator + gold
# minus DISPUTED and minus the gold-only labels still inside the two K Botiči families (M127).
ZONES = {"g6": {"merge": 4690, "band": 4907}, "w13": {"merge": 5158, "band": 4524}}
GROUPS = {"g6": 939, "w13": 980}
CERTIFICATES = {"g6": {"K-B": 1110, "K-C": 1993, "K-R": 189},
                "w13": {"K-B": 1597, "K-C": 1973, "K-R": 189}}
RELIABLE_RECALL = {"g6": (839, 1175), "w13": (979, 1175)}
# pair / block / family / cluster. g6 is the arm carrying one, not the candidate.
FALSE_MERGES = {"g6": (0, 0, 0, 1), "w13": (0, 0, 0, 0)}
MCNEMAR_RELIABLE = {"gained": 140, "lost": 0}
NON_KB_NEW_MERGES = {"model": 13, "E63": 7, "K-R": 1}


def test_w13_is_w8_plus_the_honest_clock_and_the_e84_rail_and_nothing_else() -> None:
    assert W13.live_window_from_sighting is True
    assert W13.certificate_b_min_gap_days == pytest.approx(GAP)
    # Every guard the arm deliberately does NOT pay with, spelled out in the row rather than
    # inherited, so a later default change cannot silently re-price it.
    raw = json.loads((ROOT / "settings/w13.json").read_text(encoding="utf-8"))
    for name in ("certificate_b_min_images", "certificate_b_min_matched_images"):
        assert raw[name] == 0.0, name
    assert raw["family_guard_mode"] == "off" and raw["development_hold_mode"] == "off"
    assert raw["catalog_carrier_aware"] is False
    # Every cut, every stratum and every other switch is the incumbent's: the arm moves the
    # CLOCK, never a threshold, and the model is `w6_gold` unrefitted.
    assert W13.t_hi_by_stratum == W8.t_hi_by_stratum
    for name in ("certificate_kr_enabled", "unit_designator_veto", "context_rule_enabled",
                 "bridge_apply", "bridge_min_score", "store_floor", "t_hi", "t_lo"):
        assert getattr(W13, name) == getattr(W8, name), name
    assert not (ROOT / "models/w13_gold.json").exists(), "no refit"


def test_e95_the_gate_asks_for_e84_and_no_longer_for_the_e65_floor() -> None:
    """E87 and E89 both refused a price no sealed read had measured. W11's seal HAD measured
    this arm; what refused it was one gold negative the operator has since overturned."""
    Settings(live_window_from_sighting=True, certificate_b_min_gap_days=GAP)
    with pytest.raises(ValueError, match="E84 gap rail"):
        Settings(live_window_from_sighting=True)
    with pytest.raises(ValueError, match="E84 gap rail"):
        Settings(live_window_from_sighting=True, certificate_b_min_images=4.0)
    # The floor is not withdrawn — it is optional. Restoring the W9 arm is one number.
    floored = Settings(live_window_from_sighting=True, certificate_b_min_gap_days=GAP,
                       certificate_b_min_images=1.0, certificate_b_min_matched_images=1.0)
    assert floored.certificate_b_min_images == 1.0
    assert Settings().certificate_b_min_images == 0.0, "and the default never carried one"


def test_the_shipped_generation_is_still_g6_and_the_clock_is_still_off_by_default() -> None:
    assert W8.live_window_from_sighting is False
    assert Settings().live_window_from_sighting is False
    assert not (ROOT / "settings/w13_strata.json").exists(), "no stratum row is promoted"


def test_the_arm_carries_zero_reliable_false_merges_at_every_grain() -> None:
    """M127. The promotion bar's first clause is 0 at pair, block, family and cluster grain —
    the clause that refused W11 when the count was 1. It is now g6 that carries one."""
    assert FALSE_MERGES["w13"] == (0, 0, 0, 0)
    assert FALSE_MERGES["g6"][3] == 1, "445691 x 15427866, the card M56 reported"


def test_the_arm_beats_the_incumbent_on_recall_and_loses_nothing() -> None:
    hit_w13, total = RELIABLE_RECALL["w13"]
    hit_g6, total_g6 = RELIABLE_RECALL["g6"]
    assert total == total_g6, "the same labelled duplicates are stored by both arms"
    assert hit_w13 > hit_g6 and MCNEMAR_RELIABLE["lost"] == 0
    assert MCNEMAR_RELIABLE["gained"] == hit_w13 - hit_g6
    assert ZONES["w13"]["band"] < ZONES["g6"]["band"], "and the LLM band shrinks with it"
    assert CERTIFICATES["w13"]["K-B"] > CERTIFICATES["g6"]["K-B"]
    assert CERTIFICATES["w13"]["K-R"] == CERTIFICATES["g6"]["K-R"], "K-R is clock-independent"


def test_the_honest_clock_is_not_only_a_k_b_channel() -> None:
    """M111's boundary, re-measured (M128): 21 of the new merges arrive through the model, E63
    and K-R, so any successor to E88 must act on the SCORE and not only on the certificate."""
    assert sum(NON_KB_NEW_MERGES.values()) == 21
    assert NON_KB_NEW_MERGES["model"] == 13, "every one at the model|cross cut 0.9788"


def test_the_two_contested_pairs_are_operator_positives_now() -> None:
    """M125: the W13 labels export carries both rulings, explicit, verdict `same`. They are the
    whole of what E95 rests on, which is why the rule names what revokes it."""
    export = (Path("/home/hejtm/autodedup-artifacts/w13/labels/"
                   "autodedup-labels-35532695459/operator_labels.jsonl"))
    if not export.is_file():  # the artifact lives outside the repo
        pytest.skip("W13 label export not present in this checkout")
    ruled = {}
    for line in export.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        key = (min(row["listing_lo"], row["listing_hi"]),
               max(row["listing_lo"], row["listing_hi"]))
        if key in OPERATOR_RULED:
            ruled[key] = row
    assert set(ruled) == set(OPERATOR_RULED)
    for row in ruled.values():
        assert row["verdict"] == "same" and row["source"] == "explicit"
        assert row["relation"] == "same_property"


def test_the_fresh_seal_is_committed_named_by_map_and_seed_and_unread() -> None:
    groups = seals.load(W13_SEAL)
    assert len(groups) == 4456 and len(set(groups.values())) == 664
    assert seals.seed_for(W13_SEAL) == 20260927
    assert seals.seal_id(groups, 20260927) == W13_SEAL
    assert split_seal(groups)["sha256"] == W11_SEAL, "same map, different seed, different seal"
    assert seals.spent(W13_SEAL) is None, "W13 cut it and did not open it"


def test_the_spent_seals_stay_spent() -> None:
    for seal in ("00e2cb2fe4fe1e8ee9886729ef3420ebaa6aec059934b20e30630751500032b4",
                 "ebc141fa51555bf7e2dd1757d84fb912e907c377e927e1b5c6de6132844d1b24",
                 "fb9df2ea9fd773bf0eda256d00894924ba4b8491cc7559181f2c48da75e59884",
                 "37c8771fda6b06db2ead790fcf7728e0ccb0e80c60cad5358c2905ed39be52cc"):
        assert seals.spent(seal), seal


def test_every_row_under_settings_is_constructible() -> None:
    for path in sorted((ROOT / "settings").glob("*.json")):
        if path.name.endswith("_strata.json"):
            continue
        Settings.from_json(path)


def test_the_real_time_lane_still_refuses_every_guard_this_wave_did_not_ship() -> None:
    """E95 changes the gate, not the lane: `incremental.run_pass` still raises on E83, E85 and
    E88, because none of them has the family index its rail needs."""
    from autodedup import incremental

    for field, value in (("family_guard_mode", "cell"),
                         ("development_hold_mode", "narrow"),
                         ("catalog_carrier_aware", True)):
        settings = Settings.from_json(ROOT / "settings/w13.json")
        setattr(settings, field, value)
        if field == "catalog_carrier_aware":
            settings.catalog_min_broker_carriers = 2
        settings.validate()
        with pytest.raises(NotImplementedError):
            incremental.run_pass(
                None, None, None, settings, None, None,  # type: ignore[arg-type]
            )
