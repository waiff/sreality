"""What W13 built: the honest clock priced by E84 alone (E95), and the numbers behind it.

The two pairs that refused the arm in W11 — `522698 x 13221982` and its dev-side twin
`555448 x 18626270`, both inside the ceskereality Rezidence K Botiči families — were ruled
`same` by the OPERATOR on 2026-09-20. Operator labels outrank gold, so the sealed read W11
already took of exactly this arm (347 of 406 against g6's 300, gained 47, lost 0) becomes the
price E87/E89 kept asking for. `settings/w13.json` is that row.

The wave PROMOTES (D36): generation g7 is `settings/w13.json` + `models/w6_gold.json`,
evidenced by `settings/w13_strata.json`, on the verifier's single sealed read of `510db099…`
(seed 20260927, now registered SPENT). E96 turns E95's revoking event into a check something
runs rather than a sentence somebody remembers.
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
STRATA = json.loads((ROOT / "settings/w13_strata.json").read_text(encoding="utf-8"))
# The verifier's SINGLE sealed read of 510db099 (seed 20260927), 342 reliable labelled pairs on
# the test side, 250 of them duplicates. Read once, never re-read: these are the numbers.
SEALED = {"g6": {"merged": 164, "recall": 0.656}, "g7": {"merged": 204, "recall": 0.816}}
SEALED_MCNEMAR = {"gained": 40, "lost": 0, "p": 1.82e-12}
SEALED_FALSE_MERGES = {"g6": (0, 0, 0, 0), "g7": (0, 0, 0, 0)}
# The two pairs owed to the validation UI before anyone calls the question closed (D36 iv).
OWED = ((145827, 11648789), (16438, 92824))


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


def test_g7_is_the_promoted_generation_and_the_defaults_are_untouched() -> None:
    """D36. A generation is a settings FILE plus a model, never the dataclass defaults: g6 was
    `w8.json` and the defaults were never w8's either. Nothing new is fitted."""
    assert (ROOT / "settings/w13.json").exists()
    assert (ROOT / "settings/w13_strata.json").exists(), "a promotion carries its evidence"
    assert (ROOT / "models/w6_gold.json").exists() and not (ROOT / "models/w13_gold.json").exists()
    assert W8.live_window_from_sighting is False, "the incumbent row is not edited in place"
    assert Settings().live_window_from_sighting is False
    assert not (ROOT / "settings/refuted/w13.json").exists(), "the promoted row is not refuted"


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


def test_the_fresh_seal_is_committed_named_by_map_and_seed_and_now_SPENT() -> None:
    """The build cut it unread; the verification opened it ONCE to adjudicate D36, so it is
    registered spent in the same wave that promoted on it. A later number on it confirms."""
    groups = seals.load(W13_SEAL)
    assert len(groups) == 4456 and len(set(groups.values())) == 664
    assert seals.seed_for(W13_SEAL) == 20260927
    assert seals.seal_id(groups, 20260927) == W13_SEAL
    assert split_seal(groups)["sha256"] == W11_SEAL, "same map, different seed, different seal"
    spent = seals.spent(W13_SEAL)
    assert spent and "D36" in spent and "204 of 250" in spent


def test_the_spent_seals_stay_spent() -> None:
    for seal in (W13_SEAL,
                 "00e2cb2fe4fe1e8ee9886729ef3420ebaa6aec059934b20e30630751500032b4",
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


def test_the_sealed_read_is_the_one_the_verification_took_and_it_promotes() -> None:
    """M132. The bar: 0 reliable false merges at every grain, recall above g6 with McNemar in
    the candidate's favour, and a band no larger. All four clauses, on a fresh seal."""
    assert SEALED_FALSE_MERGES["g7"] == (0, 0, 0, 0) == SEALED_FALSE_MERGES["g6"]
    assert SEALED["g7"]["merged"] > SEALED["g6"]["merged"]
    assert SEALED_MCNEMAR["lost"] == 0
    assert SEALED_MCNEMAR["gained"] == SEALED["g7"]["merged"] - SEALED["g6"]["merged"]
    bar = STRATA["promotion_bar"]
    assert all(clause["met"] for clause in bar.values())
    sealed = bar["sealed_recall_above_g6_with_mcnemar_in_its_favour"]
    assert sealed["g7"]["merged"] == SEALED["g7"]["merged"]
    assert sealed["seal"] == W13_SEAL
    # The candidate wins under GOLD alone — the tier whose negative refused it in W11.
    gold = sealed["by_tier"]["gold_only"]
    assert gold["g7"] > gold["g6"] and gold["lost"] == 0


def test_the_hazard_read_found_no_different_unit_merge_and_says_so_as_a_bound() -> None:
    """The fourth clause is a READING, not a label count, so it is recorded with its Wilson
    bound: 0 of 68 is absence of evidence at a measured precision, never proof (D36 v)."""
    clause = STRATA["promotion_bar"]["zero_different_unit_merges_in_the_verifier_reading"]
    assert clause["different_unit_merges_found"] == 0 and clause["read_by_hand"] == 68
    assert clause["wilson95_upper"]["pair"] > 0.0, "a bound, not a proof"
    risk = STRATA["owed"]["d30_vii_residual_risk"]
    assert risk.startswith("UNCHANGED"), "this wave's clean read does not soften D30 (vii)"


def test_exactly_one_merge_is_lost_and_the_claim_is_scoped_to_labelled_duplicates() -> None:
    """M133. 'Nothing is lost anywhere' is true of LABELLED RELIABLE duplicates and of nothing
    wider: `531470 x 18717549` falls out of the merge zone and no tier labels it."""
    losses = STRATA["losses"]
    assert losses["labelled_reliable_duplicates_lost_vs_g6"] == 0
    assert losses["merges_lost_vs_g6"] == 1
    assert losses["the_one"]["pair"] == [531470, 18717549]
    assert losses["the_one"]["labelled_by"] == "no tier"


def test_the_two_pairs_owed_to_the_operator_are_committed_as_a_pair_list() -> None:
    """D36 (iv). E95 is revoked by an operator NEGATIVE inside a K-B family, and these are the
    only two nameable candidates in the cohort — one that g7 merges as K-B and reads as ONE
    unit (M130), one that merges in BOTH arms and is therefore an incumbent question."""
    owed = json.loads((ROOT / "pairs/w13_operator_owed.json").read_text(encoding="utf-8"))
    assert tuple(tuple(pair) for pair in owed) == OWED
    for lo, hi in owed:
        assert lo < hi, "a pair list is ordered, so a lookup cannot miss it"
    recorded = STRATA["owed"]["operator_pairs"]
    assert [tuple(row["pair"]) for row in recorded["pairs"]] == list(OWED)
    assert recorded["file"] == "autodedup/pairs/w13_operator_owed.json"


def test_e96_counts_the_event_that_revokes_e95_and_it_reads_zero_today() -> None:
    """M134. The rule names its own revoking event; the check is what keeps that from being a
    sentence nobody re-derives. It reports — revoking E95 is a settings change, not a code one."""
    from autodedup import revocation

    today = STRATA["e96_revocation_check"]["today"]
    assert today["negatives"] == 0 and today["revoked"] is False
    assert today["operator_labelled_pairs_inside"] == 196
    # And the check that produced it still refuses to be fooled by the tier E95 overturned.
    report = revocation.check_rows([{"lo": 1, "hi": 2, "certificate": "K-B"}], {})
    assert not report.revoked and report.families == 1


def test_the_real_time_lane_carries_g7_without_a_new_rail() -> None:
    """D36 (vi). The honest clock is a clock and a PAIR-level rail: no cohort-wide index, so
    replay equivalence holds exactly and order-insensitively on the promoted row."""
    lane = STRATA["real_time_lane"]
    assert lane["w13_sets_all_three_to_the_accepted_value"] is True
    assert lane["replay_equivalence"]["pairs"]["differing"] == 0
    assert lane["replay_equivalence"]["clusters"]["member_sets_identical"] is True
    assert lane["order_insensitivity_shuffle_seed_7"]["pairs_differing"] == 0


def test_the_evidence_row_and_these_tests_quote_the_same_numbers() -> None:
    """A strata file nobody checks is a press release. Every headline number appears twice."""
    assert STRATA["zones"]["g7"]["merge"] == ZONES["w13"]["merge"]
    assert STRATA["zones"]["g7"]["band"] == ZONES["w13"]["band"]
    assert STRATA["zones"]["g6"] == {**STRATA["zones"]["g6"], "merge": ZONES["g6"]["merge"]}
    assert STRATA["zones"]["g7"]["clusters"] == GROUPS["w13"]
    assert STRATA["zones"]["g7"]["certificates"] == CERTIFICATES["w13"]
    assert STRATA["recall"]["reliable"]["g7"]["hit"] == RELIABLE_RECALL["w13"][0]
    assert STRATA["recall"]["reliable"]["mcnemar"]["gained"] == MCNEMAR_RELIABLE["gained"]
    assert STRATA["new_merges"]["by_source"]["model"] == NON_KB_NEW_MERGES["model"]
    assert STRATA["provenance"]["model"].startswith("autodedup/models/w6_gold.json")
    assert STRATA["provenance"]["spend_usd"] == 0.0 and STRATA["provenance"]["judge_calls"] == 0
    assert STRATA["seal"]["id"] == W13_SEAL and STRATA["seal"]["spent"] is True


def test_the_operator_ruling_is_recorded_with_its_date() -> None:
    ruling = STRATA["operator_ruling"]
    assert ruling["decided_at_utc"].startswith("2026-09-20T19:30")
    assert [tuple(row["pair"]) for row in ruling["pairs"]] == list(OPERATOR_RULED)
    assert all(row["verdict"] == "same" for row in ruling["pairs"])
    assert ruling["source"] == "explicit" and ruling["grain"] == "pair"
    # The sensitivity of the whole safety case to those two clicks, stated rather than hidden.
    reverted = STRATA["sensitivity"]["gold_wins_on_the_two_ruled_pairs"]["g7"]
    assert reverted["pair"] == 2, "E95 names exactly this, and E96 counts it"
