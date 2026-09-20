"""Labels that come from structure, not from a model (W7): what each rule may and may not claim."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodedup import structural_truth as st
from autodedup.dataset import Listing, Location
from autodedup.structural_truth import (
    LABEL_DIFFERENT,
    LABEL_SAME,
    MIN_CODE_LEN,
    NEGATIVE_RULES,
    POSITIVE_RULES,
    RULES,
    StructuralLabel,
    address_block_key,
    areas_disjoint,
    build_index,
    label_pair,
    label_pairs,
    overlap_days,
    reference_codes,
    reference_context,
    stated_areas,
    stratify,
    unit_numbers,
)

PAIRS_DIR = Path(__file__).resolve().parents[2] / "autodedup" / "pairs"


def make(
    listing_id: int,
    *,
    source: str = "sreality",
    native: str | None = None,
    description: str = "",
    area: float | None = 50.0,
    price: float | None = 5_000_000.0,
    floor: int | None = 2,
    ruian: int | None = 1001,
    first: str = "2026-05-01T00:00:00+00:00",
    inactive: str | None = None,
    active: bool = True,
    last_seen: str | None = "2026-09-01T00:00:00+00:00",
    broker: int | None = 7,
    category_main: str = "byt",
    category_type: str = "prodej",
) -> Listing:
    return Listing(
        id=listing_id,
        block="jablonec",
        source=source,
        source_id_native=native if native is not None else str(listing_id),
        category_main=category_main,
        category_type=category_type,
        area_m2=area,
        floor=floor,
        price=price,
        description=description,
        first_seen_at=first,
        last_seen_at=last_seen,
        inactive_at=inactive,
        is_active=active,
        broker_identity_id=broker,
        location=Location(obec_kod=563510, ruian_adm_kod=ruian),
    )


def label(a: Listing, b: Listing) -> StructuralLabel | None:
    return label_pair(a, b, build_index({a.id: a, b.id: b}))


# --- extraction -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Pro komunikaci uvádějte evidenční číslo zakázky N115423.", {"N115423"}),
        ("Ev. číslo: 657349.", {"657349"}),
        ("Ev.č. 2026-08-20-100017 kancelář", {"2026-08-20-100017"}),
        ("Hartigova 1116/83, Žižkov [ID 84553] byt", {"84553"}),
        ("evidenční číslo / ID zakázky: R2256 nabízí", {"R2256"}),
        # Too short to be an order key, and a bare number with no keyword is a price or a year.
        ("Ev. číslo: 371.", set()),
        ("Byt byl postaven v roce 2018 za 3 500 000 Kč.", set()),
    ],
)
def test_reference_codes(text: str, expected: set[str]) -> None:
    assert reference_codes(text) == expected


def test_reference_code_is_not_truncated() -> None:
    """A clipped tail would let two different orders collide on one key."""
    code = next(iter(reference_codes("Ev.č. 2026-08-20-100017")))
    assert len(code) > MIN_CODE_LEN


def test_reference_context_is_scrubbed() -> None:
    text = "Volejte makléře Jana Nováka, tel. 777 123 456. Ev. číslo: 657349."
    context = reference_context(text, "657349")
    assert "657349" in context
    assert "777" not in context


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Představujeme vám světlý byt (č.3) o dispozici 2+kk", {"3"}),
        ("Prodej bytu, jednotka č. 12, druhé patro", {"12"}),
        ("byt s označením B36 o dispozici", {"B36"}),
        # A disposition is not a unit number.
        ("Nabízíme nadstandardní bytovou jednotku 4+kk", set()),
    ],
)
def test_unit_numbers(text: str, expected: set[str]) -> None:
    assert unit_numbers(text) == expected


def test_stated_areas_reads_the_text_not_the_column() -> None:
    assert stated_areas("byt 3+1 o velikosti 72m2 se zahrádkou 20,2 m²") == {72.0, 20.2}


def test_areas_disjoint_tolerates_one_flat_measured_twice() -> None:
    assert not areas_disjoint({47.6}, {46.6})
    assert areas_disjoint({39.3}, {43.2})
    assert not areas_disjoint({50.7, 20.2}, {49.8, 20.2})
    assert not areas_disjoint(set(), {50.0})


def test_address_block_key_falls_back_through_the_grains() -> None:
    assert address_block_key(make(1, ruian=555)) == "ruian:555"
    loose = make(2, ruian=None)
    loose.location = Location(obec_kod=9, street_key="hartigova", house_number="1116/83")
    assert address_block_key(loose) == "addr:9:hartigova:1116/83"
    bare = make(3, ruian=None)
    bare.location = Location(obec_kod=9)
    assert address_block_key(bare) == "obec:9"


def test_overlap_days_ends_an_active_advert_at_its_last_sighting() -> None:
    a = make(1, first="2026-05-01T00:00:00+00:00", last_seen="2026-06-01T00:00:00+00:00")
    b = make(2, first="2026-05-20T00:00:00+00:00", last_seen="2026-07-01T00:00:00+00:00")
    assert overlap_days(a, b) == pytest.approx(12.0)
    gone = make(3, first="2026-01-01T00:00:00+00:00", last_seen="2026-01-28T00:00:00+00:00",
                inactive="2026-02-01T00:00:00+00:00", active=False)
    assert overlap_days(a, gone) == 0.0


def test_overlap_days_ends_an_inactive_advert_at_its_last_sighting_not_the_stamp() -> None:
    """W8: `inactive_at` is the DETECTION stamp, and the detection lag reaches 70 days.

    Listing 412650's shape: last seen 2026-08-12, stamped inactive 2026-09-08. Its successor
    started the day before the last sighting, so the two ran together for ONE day — the 27.4
    days the old clock reported sailed past the 21-day re-post guard."""
    gone = make(1, first="2026-06-01T00:00:00+00:00", last_seen="2026-08-12T00:00:00+00:00",
                inactive="2026-09-08T00:00:00+00:00", active=False)
    successor = make(2, first="2026-08-11T00:00:00+00:00",
                     last_seen="2026-09-17T00:00:00+00:00")
    assert overlap_days(gone, successor) == pytest.approx(1.0)


def test_overlap_days_falls_back_to_the_stamp_when_there_is_no_sighting() -> None:
    gone = make(1, first="2026-01-01T00:00:00+00:00", last_seen=None,
                inactive="2026-02-01T00:00:00+00:00", active=False)
    other = make(2, first="2026-01-20T00:00:00+00:00", last_seen="2026-03-01T00:00:00+00:00")
    assert overlap_days(gone, other) == pytest.approx(12.0)


# --- positives --------------------------------------------------------------------------


def test_shared_code_across_portals_is_one_order() -> None:
    body = "Pro komunikaci uvádějte evidenční číslo zakázky N115423."
    result = label(make(1, source="sreality", description=body),
                   make(2, source="ceskereality", description=body))
    assert result is not None
    assert (result.label, result.rule) == (LABEL_SAME, "pos_ref_cross")
    assert result.evidence["code"] == "N115423"


def test_shared_code_on_one_portal_is_a_re_post() -> None:
    body = "Ev. číslo: 657349."
    result = label(make(1, native="a", description=body), make(2, native="b", description=body))
    assert result is not None
    assert (result.label, result.rule) == (LABEL_SAME, "pos_ref_relist")


def test_a_crowded_code_certifies_nothing() -> None:
    """A per-broker sequence number carried by a crowd is not an order key."""
    body = "Ev. číslo: 03888."
    listings = {i: make(i, description=body) for i in range(1, 12)}
    index = build_index(listings)
    assert label_pair(listings[1], listings[2], index) is None


def test_same_unit_number_in_one_block_at_one_area() -> None:
    result = label(
        make(1, description="byt (č.7) o dispozici 2+kk", area=53.0),
        make(2, source="bazos", description="prodej bytu č. 7 v projektu", area=53.2),
    )
    assert result is not None
    assert (result.label, result.rule) == (LABEL_SAME, "pos_unit_in_project")


def test_a_positive_never_crosses_the_deal_type() -> None:
    body = "Ev. číslo: 657349."
    assert label(make(1, description=body, category_type="prodej"),
                 make(2, description=body, category_type="pronajem")) is None
    assert label(make(1, description=body, category_main="byt"),
                 make(2, description=body, category_main="komercni")) is None


# --- negatives --------------------------------------------------------------------------


def test_different_unit_numbers_in_one_block() -> None:
    result = label(
        make(1, description="světlý byt (č.3) o dispozici 2+kk"),
        make(2, description="světlý byt (č.5) o dispozici 2+kk"),
    )
    assert result is not None
    assert (result.label, result.rule) == (LABEL_DIFFERENT, "neg_unit_number_conflict")


def test_unit_numbers_only_conflict_inside_one_block() -> None:
    assert label(
        make(1, description="byt (č.3)", ruian=1001),
        make(2, description="byt (č.5)", ruian=2002),
    ) is None


def test_co_live_adverts_printing_different_areas_are_two_flats() -> None:
    result = label(
        make(1, description="byt 2+kk o podlahové ploše 39,3 m2", price=6_910_000.0),
        make(2, description="byt 2+kk o podlahové ploše 43,2 m2", price=7_560_000.0),
    )
    assert result is not None
    assert (result.label, result.rule) == (LABEL_DIFFERENT, "neg_stated_area_conflict")


def test_an_identical_price_makes_the_area_gap_unreadable() -> None:
    """The K Botiči abstention: 27,2 and 28,6 m² both at 5 499 000 Kč decide nothing."""
    assert label(
        make(1, description="byt 1+kk (27,2 m2)", price=5_499_000.0),
        make(2, description="byt 1+kk (28,6 m2)", price=5_499_000.0),
    ) is None


def test_areas_that_never_ran_together_are_a_re_post_not_a_second_flat() -> None:
    assert label(
        make(1, description="byt o ploše 39,3 m2", inactive="2026-05-10T00:00:00+00:00",
             active=False, last_seen="2026-05-10T00:00:00+00:00", price=1.0),
        make(2, description="byt o ploše 43,2 m2", first="2026-06-01T00:00:00+00:00", price=2.0),
    ) is None


def test_a_pair_two_rules_claim_at_once_is_returned_to_nobody() -> None:
    body_a = "Ev. číslo: 657349. byt (č.3) o ploše 39,3 m2"
    body_b = "Ev. číslo: 657349. byt (č.5) o ploše 39,3 m2"
    assert label(make(1, native="a", description=body_a),
                 make(2, native="b", description=body_b)) is None


# --- plumbing ---------------------------------------------------------------------------


def test_label_pairs_is_order_free_and_skips_absent_listings() -> None:
    body = "Ev. číslo: 657349."
    listings = {1: make(1, native="a", description=body), 2: make(2, native="b", description=body)}
    forward = label_pairs(listings, [(1, 2)])
    backward = label_pairs(listings, [(2, 1)])
    assert [item.to_json() for item in forward] == [item.to_json() for item in backward]
    assert label_pairs(listings, [(1, 99)]) == []


def test_stratify_spreads_before_it_deepens() -> None:
    labels = [
        StructuralLabel(i, i + 1000, LABEL_DIFFERENT, "neg_unit_number_conflict", f"b{i % 3}")
        for i in range(12)
    ]
    picked = stratify(labels, cap_per_block=3, limit=100)
    assert len(picked) == 9
    assert {block: sum(1 for p in picked if p.block == block) for block in ("b0", "b1", "b2")} == {
        "b0": 3, "b1": 3, "b2": 3,
    }
    assert [p.block for p in picked[:3]] == ["b0", "b1", "b2"]
    assert len(stratify(labels, cap_per_block=3, limit=4)) == 4


def test_rule_names_are_partitioned() -> None:
    assert set(RULES) == set(POSITIVE_RULES) | set(NEGATIVE_RULES)
    assert not set(POSITIVE_RULES) & set(NEGATIVE_RULES)


@pytest.mark.parametrize("name", ["w7_struct_pos.json", "w7_struct_neg.json"])
def test_committed_pair_lists_are_what_the_judge_lane_reads(name: str) -> None:
    """`judge_lane.load_pair_list` wants a bare JSON list of two-integer pairs, lo < hi."""
    payload = json.loads((PAIRS_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(payload, list) and payload
    seen = set()
    for entry in payload:
        assert isinstance(entry, list) and len(entry) == 2
        lo, hi = entry
        assert isinstance(lo, int) and isinstance(hi, int) and lo < hi
        assert (lo, hi) not in seen
        seen.add((lo, hi))


def test_mask_codes_redacts_every_order_code_the_rules_certify_on() -> None:
    text = (
        "Nabizime byt 2+kk, evidencni cislo zakazky N115815, dale ev. c.: 657349. "
        "Kontakt v inzeratu [ID 84553]."
    )
    assert st.reference_codes(text) == {"N115815", "657349", "84553"}
    masked = st.mask_codes(text)
    assert st.reference_codes(masked) == set()
    assert "N115815" not in masked and "657349" not in masked and "84553" not in masked
    assert masked.count(st.CODE_MASK) == 3
    # The keyword survives: the judge still sees that an order number was printed, only not
    # WHICH one, so the arm cannot match two adverts on the string itself.
    assert "evidencni cislo zakazky" in masked


def test_mask_codes_is_a_no_op_on_text_with_no_code() -> None:
    assert st.mask_codes(None) is None
    assert st.mask_codes("") == ""
    plain = "Slunny byt 3+1 o vymere 72 m2 v cihlovem dome."
    assert st.mask_codes(plain) == plain


def test_mask_codes_follows_whitespace_the_capture_removed() -> None:
    text = "evidencni cislo zakazky N 115815 konec"
    codes = st.reference_codes(text)
    assert codes == {"N115815"}
    assert st.mask_codes(text) == "evidencni cislo zakazky [KOD] konec"
