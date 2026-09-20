"""E85 — the K-B family guard, and what each of its clauses is allowed to refuse.

The guard is the one rule in this engine that decides a pair on evidence the pair does not
carry, so every test here is about the seam: what a family IS (and that it cannot move under
the guard's own verdict), what a cell is, what each clause costs, and that a refusal removes
evidence rather than manufacturing a contradiction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodedup import family, seals
from autodedup.dataset import Listing
from autodedup.decide import Decision, certificate_of
from autodedup.settings import Settings

ROOT = Path(__file__).resolve().parents[2] / "autodedup"
DAY = 86400.0


def _stamp(day: int, hour: int = 0) -> str:
    return f"2026-0{1 + day // 28}-{1 + day % 28:02d}T{hour:02d}:00:00+00:00"


def listing(
    listing_id: int,
    *,
    first: str,
    last: str,
    area: float | None = 27.0,
    body: str = "Prodej bytu 1+kk o podlahové ploše 27,2 m2 v projektu K Botiči.",
    price: float | None = 5_499_000.0,
    disposition: str | None = "1+kk",
) -> Listing:
    return Listing(
        id=listing_id, block="praha", source="ceskereality", broker_key="b1",
        category_main="byt", category_type="prodej", disposition=disposition,
        area_m2=area, price=price, description=body,
        first_seen_at=first, last_seen_at=last, is_active=False,
    )


def honest() -> Settings:
    settings = Settings()
    settings.live_window_from_sighting = True
    settings.family_guard_mode = "cell"
    settings.certificate_b_min_gap_days = 1.0 / 1440.0
    settings.validate()
    return settings


def kb(lo: int, hi: int) -> Decision:
    return Decision(lo, hi, "merge", 1.0, {"TXT", "ATTR"}, "K-B", None, "certificate:K-B")


# --- what a family is -------------------------------------------------------------------


def test_a_family_is_the_certificate_not_the_zone() -> None:
    """A K-B pair the gates BANDED is still a family edge: read the zone instead and the guard
    would judge a different family every time it demoted something."""
    decisions = [
        kb(1, 2),
        Decision(2, 3, "band", 0.9, set(), "K-B", None, "certificate:K-B:developer_colive"),
        Decision(3, 4, "merge", 1.0, set(), "K-C", None, "certificate:K-C"),
    ]
    assert family.kb_families(decisions) == {1: [1, 2, 3]}


def test_a_family_is_keyed_by_its_smallest_member() -> None:
    assert family.kb_families([kb(9, 4), kb(4, 7)]) == {4: [4, 7, 9]}


def test_no_families_means_no_refusals_and_an_empty_report() -> None:
    refused, report = family.refusals([], {}, honest())
    assert refused == {} and report["n_families"] == 0 and report["n_refused"] == 0


def test_the_guard_is_off_by_default_and_reads_nothing() -> None:
    refused, report = family.refusals([kb(1, 2)], {}, Settings())
    assert refused == {} and report["mode"] == "off"


# --- cells ------------------------------------------------------------------------------


def test_a_serial_repost_chain_is_one_cell_and_nothing_is_refused() -> None:
    """The one-unit shape: one size, one price, windows in strict succession."""
    settings = honest()
    listings = {
        i: listing(i, first=_stamp(2 * i), last=_stamp(2 * i + 1)) for i in (1, 2, 3)
    }
    decisions = [kb(1, 2), kb(2, 3), kb(1, 3)]
    refused, report = family.refusals(decisions, listings, settings)
    assert refused == {}
    assert report["n_families"] == 1 and report["n_families_impure"] == 0


def test_a_different_printed_size_opens_a_second_cell_and_only_cross_edges_are_refused() -> None:
    """F518656: 19 adverts printing 27,2 m² and 2 printing 28,6 m², one price, one broker."""
    settings = honest()
    listings = {
        1: listing(1, first=_stamp(0), last=_stamp(1),
                   body="Prodej bytu 1+kk o ploše 28,6 m2."),
        2: listing(2, first=_stamp(2), last=_stamp(3),
                   body="Prodej bytu 1+kk o ploše 28,6 m2."),
        3: listing(3, first=_stamp(4), last=_stamp(5)),
        4: listing(4, first=_stamp(6), last=_stamp(7)),
    }
    decisions = [kb(a, b) for a in (1, 2, 3, 4) for b in (1, 2, 3, 4) if a < b]
    refused, report = family.refusals(decisions, listings, settings)
    assert report["families"][0]["cells"] == [[1, 2], [3, 4]]
    assert set(refused) == {(1, 3), (1, 4), (2, 3), (2, 4)}
    assert set(refused.values()) == {"printed_area"}


def test_a_price_cut_in_time_stays_one_unit_and_a_rise_does_not() -> None:
    """F399658 dips 19,999,000 -> 17,700,000 -> 18,481,000 and is one flat; F186168 rises
    27,490 -> 37,490 and is a different rent package in one serviced-office building."""
    settings = honest()
    cut = {
        1: listing(1, first=_stamp(0), last=_stamp(1), price=19_999_000.0),
        2: listing(2, first=_stamp(2), last=_stamp(3), price=17_700_000.0),
        3: listing(3, first=_stamp(4), last=_stamp(5), price=18_481_000.0),
    }
    refused, _ = family.refusals([kb(1, 2), kb(2, 3), kb(1, 3)], cut, settings)
    assert refused == {}
    rise = {
        1: listing(1, first=_stamp(0), last=_stamp(1), price=27_490.0),
        2: listing(2, first=_stamp(2), last=_stamp(3), price=37_490.0),
    }
    refused, _ = family.refusals([kb(1, 2)], rise, settings)
    assert refused == {(1, 2): "price"}


def test_two_adverts_live_at_once_are_two_units() -> None:
    settings = honest()
    listings = {
        1: listing(1, first=_stamp(0), last=_stamp(6)),
        2: listing(2, first=_stamp(3), last=_stamp(9)),
    }
    refused, _ = family.refusals([kb(1, 2)], listings, settings)
    assert refused == {(1, 2): "concurrent"}


def test_a_cell_is_a_clique_so_one_lax_edge_cannot_fuse_two_chains() -> None:
    """Two chains running side by side, one asking 8,525,000 and one 3,399,000. A 60 % price
    CUT is compatible on its own, so connected components would fuse all four adverts and then
    lose the whole family; first-fit cells keep the two chains apart and refuse only the edges
    between them."""
    settings = honest()
    listings = {
        1: listing(1, first=_stamp(0), last=_stamp(3), price=8_525_000.0),
        2: listing(2, first=_stamp(1), last=_stamp(4), price=3_399_000.0),
        3: listing(3, first=_stamp(5), last=_stamp(8), price=8_525_000.0),
        4: listing(4, first=_stamp(6), last=_stamp(9), price=3_399_000.0),
    }
    decisions = [kb(a, b) for a in (1, 2, 3, 4) for b in (1, 2, 3, 4) if a < b]
    refused, report = family.refusals(decisions, listings, settings)
    assert report["families"][0]["cells"] == [[1, 3], [2, 4]]
    assert set(refused) == {(1, 2), (1, 4), (2, 3), (3, 4)}


def test_an_absent_fact_is_never_a_mismatch() -> None:
    """E12: a body that prints no size and a listing that carries no price cannot refuse."""
    settings = honest()
    listings = {
        1: listing(1, first=_stamp(0), last=_stamp(1), body="Prodej bytu.", price=None),
        2: listing(2, first=_stamp(2), last=_stamp(3), price=None),
    }
    assert family.refusals([kb(1, 2)], listings, settings)[0] == {}


# --- clause costs and the clauses that are OFF --------------------------------------------


def test_the_area_window_keeps_a_half_house_repost_together() -> None:
    """478609 x 536412: one advert prints 103 and 206 against a stored 150, the other only 206.
    Without the window the nearest-match reads 103 against 206 and refuses a true re-post."""
    settings = honest()
    body_a = ("Nabízíme k prodeji 1/2 rodinného domu o ploše 103 m2, "
              "celý dům má 206 m2 užitné plochy.")
    body_b = "Nabízíme k prodeji 1/2 rodinného domu, celý dům má 206 m2 užitné plochy."
    listings = {
        1: listing(1, first=_stamp(0), last=_stamp(1), area=150.0, body=body_a,
                   price=6_000_000.0, disposition=None),
        2: listing(2, first=_stamp(2), last=_stamp(3), area=150.0, body=body_b,
                   price=6_000_000.0, disposition=None),
    }
    assert family.refusals([kb(1, 2)], listings, settings)[0] == {}
    wide = honest()
    wide.family_guard_area_window = 0.9
    assert family.refusals([kb(1, 2)], listings, wide)[0] == {(1, 2): "printed_area"}


def test_the_reference_code_clause_is_off_because_e60_already_ruled_on_it() -> None:
    """E60: a DIFFERING agency order code is evidence of nothing in either direction — W7
    refuted the conflict rule on one 43,3 m² flat at 7 974 910 Kč carrying N115815 and N118731.
    The clause exists as a row and is OFF, so the engine cannot refuse E60's own example."""
    assert Settings().family_guard_ref_code_clause is False
    settings = honest()
    body = "Rezidence Pod Parukařkou, byt 2+kk o podlahové ploše 43,3 m2. Ev. číslo: {code}"
    listings = {
        1: listing(1, first=_stamp(0), last=_stamp(1), area=43.0, price=7_974_910.0,
                   body=body.format(code="N115815"), disposition="2+kk"),
        2: listing(2, first=_stamp(2), last=_stamp(3), area=43.0, price=7_974_910.0,
                   body=body.format(code="N118731"), disposition="2+kk"),
    }
    assert family.refusals([kb(1, 2)], listings, settings)[0] == {}
    loud = honest()
    loud.family_guard_ref_code_clause = True
    assert family.refusals([kb(1, 2)], listings, loud)[0] == {(1, 2): "ref_code"}


def test_family_mode_refuses_the_whole_family_where_cell_mode_splits_it() -> None:
    settings = honest()
    settings.family_guard_mode = "family"
    listings = {
        1: listing(1, first=_stamp(0), last=_stamp(1),
                   body="Prodej bytu 1+kk o ploše 28,6 m2."),
        2: listing(2, first=_stamp(2), last=_stamp(3)),
        3: listing(3, first=_stamp(4), last=_stamp(5)),
    }
    decisions = [kb(1, 2), kb(2, 3), kb(1, 3)]
    refused, _ = family.refusals(decisions, listings, settings)
    assert set(refused) == {(1, 2), (2, 3), (1, 3)}


# --- what a refusal does to a decision ----------------------------------------------------


def _feats(**values: float) -> dict[str, tuple[float, bool]]:
    return {name: (value, True) for name, value in values.items()}


def test_a_refused_pair_loses_k_b_and_keeps_every_other_certificate() -> None:
    """The guard removes evidence; it never manufactures a contradiction. A pair that also
    earns K-C keeps it, and one that earns nothing else falls to the model."""
    settings = honest()
    la = listing(1, first=_stamp(0), last=_stamp(1))
    lb = listing(2, first=_stamp(4), last=_stamp(5))
    both = _feats(same_source=1.0, same_broker_key=1.0, containment_max=0.95,
                  area_rel_diff=0.0, phash_tight_matches=6.0, seq_monotone_ratio=1.0,
                  dispo_equal=1.0, catalog_ratio_max=0.0)
    assert certificate_of(both, la, lb, settings) == "K-B"
    assert certificate_of(both, la, lb, settings, kb_refused=True) == "K-C"
    only_b = _feats(same_source=1.0, same_broker_key=1.0, containment_max=0.95,
                    area_rel_diff=0.0)
    assert certificate_of(only_b, la, lb, settings) == "K-B"
    assert certificate_of(only_b, la, lb, settings, kb_refused=True) is None


def test_a_refusal_never_reaches_k_r() -> None:
    settings = honest()
    la = listing(1, first=_stamp(0), last=_stamp(1))
    lb = listing(2, first=_stamp(4), last=_stamp(5))
    feats = _feats(same_source=1.0, same_broker_key=1.0, containment_max=0.95,
                   area_rel_diff=0.0, ref_code_shared=1.0)
    assert certificate_of(feats, la, lb, settings, kb_refused=True) == "K-R"


# --- the settings contract ----------------------------------------------------------------


def test_the_honest_clock_may_pay_its_price_with_the_guard_instead_of_the_floor() -> None:
    naked = Settings()
    naked.live_window_from_sighting = True
    naked.certificate_b_min_gap_days = 1.0 / 1440.0
    with pytest.raises(ValueError, match="E65 image floor or the E85 family guard"):
        naked.validate()
    naked.family_guard_mode = "cell"
    naked.validate()


def test_the_gap_rail_is_still_required_under_the_honest_clock() -> None:
    settings = Settings()
    settings.live_window_from_sighting = True
    settings.family_guard_mode = "cell"
    with pytest.raises(ValueError, match="E84 gap rail"):
        settings.validate()


def test_an_unknown_mode_fails_at_load() -> None:
    with pytest.raises(ValueError, match="family_guard_mode"):
        Settings.from_dict({"family_guard_mode": "sometimes"})


def test_the_shipped_row_still_carries_no_family_guard() -> None:
    assert Settings.from_json(ROOT / "settings/w8.json").family_guard_mode == "off"
    assert Settings().family_guard_mode == "off"


def test_the_w11_candidate_is_the_shipped_row_plus_the_clock_the_rail_and_the_guard() -> None:
    raw = json.loads((ROOT / "settings/w11_candidate.json").read_text(encoding="utf-8"))
    base = json.loads((ROOT / "settings/w8.json").read_text(encoding="utf-8"))
    assert {key for key in raw if raw[key] != base.get(key)} == {
        "live_window_from_sighting", "certificate_b_min_gap_days", "family_guard_mode",
        "family_guard_area_tol", "family_guard_area_window", "family_guard_price_tol",
        "family_guard_price_rise_max", "family_guard_overlap_days",
        "family_guard_concurrency_clause", "family_guard_price_clause",
        "family_guard_ref_code_clause", "family_guard_unit_designator_clause",
        "family_guard_disposition_clause",
    }
    candidate = Settings.from_json(ROOT / "settings/w11_candidate.json")
    assert candidate.family_guard_mode == "cell"
    assert candidate.certificate_b_min_images == 0.0, "the guard replaces the E65 floor"
    assert candidate.family_guard_ref_code_clause is False, "E60 forbids it"
    assert candidate.catalog_carrier_aware is False, "E83 stays off (D28 ii)"


def test_the_incremental_lane_refuses_the_guard_until_its_rail_exists() -> None:
    from autodedup import incremental

    source = (Path(incremental.__file__)).read_text(encoding="utf-8")
    assert "family_guard_mode (E85) has no incremental family index" in source


# --- the fresh seal ------------------------------------------------------------------------


W11_SEAL = "00e2cb2fe4fe1e8ee9886729ef3420ebaa6aec059934b20e30630751500032b4"
W9_SEAL = "fb9df2ea9fd773bf0eda256d00894924ba4b8491cc7559181f2c48da75e59884"


def test_the_fresh_seal_is_committed_unspent_and_carries_its_seed() -> None:
    assert seals.committed(W11_SEAL)
    assert seals.seed_for(W11_SEAL) == 20260923
    assert seals.spent(W11_SEAL) is None


def test_the_seal_w11_replaces_is_registered_spent() -> None:
    assert seals.spent(W9_SEAL) and seals.committed(W9_SEAL)
