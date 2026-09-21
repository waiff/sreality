"""D50 / E157-E159: identity is DEMONSTRATED, and the three arms are nested by construction.

g8b promotes a band pair unless a reader FINDS a distinguishing fact. That is fail-open: its
safety is bounded by how much Czech prose the readers cover, and three cohorts in a row have
produced a form none of them knew. S and M ask the opposite question instead — do the two
adverts POSITIVELY say the same thing — and L keeps g8b's question with every reader fix.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodedup.dataset import Listing, Location
from autodedup.decide import demonstration_refusal
from autodedup.demonstrate import (
    price_conflict,
    area_demonstrated,
    corroboration_warrant,
    demonstration_gap,
    disposition_demonstrated,
    obec_demonstrated,
    price_demonstrated,
)
from autodedup.settings import Settings

ROOT = Path("autodedup/settings")
L = Settings.from_json(ROOT / "w16_l.json")
M = Settings.from_json(ROOT / "w16_m.json")
S = Settings.from_json(ROOT / "w16_s.json")
READERS = ("d43_body_align", "d43_prose_street", "d43_prose_obec", "d43_printed_area",
           "floor_feed_unknown_closed", "d43_parcel_forms_wide", "repartition_keep_factless")


def listing(listing_id: int, **kwargs: object) -> Listing:
    fields: dict[str, object] = {
        "id": listing_id, "block": "b", "source": "sreality",
        "category_main": "byt", "category_type": "prodej", "disposition": "2+kk",
        "area_m2": 58.0, "price": 5_000_000.0,
        "description": "Byt 2+kk o výměře 58 m² v cihlovém domě po rekonstrukci.",
        "location": Location(obec_kod=554782, obec_name="Olomouc"),
        "first_seen_at": "2026-05-01T00:00:00+00:00",
        "last_seen_at": "2026-09-01T00:00:00+00:00",
    }
    fields.update(kwargs)
    return Listing(**fields)  # type: ignore[arg-type]


# --------------------------------------------------------------------- E157 (A) the key facts
def test_an_area_neither_side_states_demonstrates_nothing() -> None:
    a = listing(1, area_m2=None, description="Byt 2+kk v cihlovém domě.")
    b = listing(2, area_m2=None, description="Byt 2+kk v cihlovém domě.")
    assert not area_demonstrated(a, b)
    assert demonstration_gap(a, b, M, False, None) == "area"


def test_an_area_both_sides_state_and_agree_is_demonstrated() -> None:
    assert area_demonstrated(listing(1), listing(2))


def test_a_degenerate_stored_column_is_dropped_for_what_the_body_prints() -> None:
    """bazos stores the terrace. The column then has nothing to say and the body decides."""
    body = ("Apartmán s podlahovou plochou 76,1 m² a terasou o velikosti 10 m² "
            "v posledním patře.")
    a = listing(1, source="bazos", area_m2=10.0, description=body)
    b = listing(2, source="bazos", area_m2=10.0,
                description=body.replace("76,1 m²", "77,8 m²"))
    assert not area_demonstrated(a, b)


def test_two_co_live_prices_are_two_prices() -> None:
    """260846 x 261012: two Okružní garages first sighted seven minutes apart at 1,190,000 and
    1,240,000, and the cheaper one's whole life was inside the other's."""
    a = listing(1, price=1_190_000.0, source="idnes",
                first_seen_at="2026-06-07T12:02:00+00:00",
                last_seen_at="2026-06-09T02:18:00+00:00")
    b = listing(2, price=1_240_000.0, source="idnes",
                first_seen_at="2026-06-07T12:09:00+00:00",
                last_seen_at="2026-09-21T13:34:00+00:00")
    assert not price_demonstrated(a, b, M, False, 1.59)


def test_a_cut_between_two_sequential_postings_is_one_unit() -> None:
    a = listing(1, price=4_000_000.0, first_seen_at="2026-05-01T00:00:00+00:00",
                last_seen_at="2026-07-01T00:00:00+00:00",
                inactive_at="2026-07-02T00:00:00+00:00")
    b = listing(2, price=3_600_000.0, first_seen_at="2026-07-01T12:00:00+00:00",
                last_seen_at="2026-09-01T00:00:00+00:00")
    assert price_demonstrated(a, b, M, False, 0.5)


def test_a_price_no_advert_states_demonstrates_nothing() -> None:
    a, b = listing(1, price=None), listing(2)
    assert not price_demonstrated(a, b, M, False, 0.0)
    assert demonstration_gap(a, b, M, False, 0.0) == "price"


def test_two_towns_are_two_objects() -> None:
    """224071 x 15604418: two bazos rows of three words each, one in Olomouc-Nemilany and one
    in Velký Týnec. Nothing either body states tells them apart, and they are not one flat."""
    a = listing(1, location=Location(obec_kod=554782, obec_name="Olomouc"))
    b = listing(2, location=Location(obec_kod=505285, obec_name="Velký Týnec"))
    assert not obec_demonstrated(a, b)
    assert demonstration_gap(a, b, M, False, 0.0) == "obec"


def test_land_has_no_layout_to_state() -> None:
    a = listing(1, category_main="pozemek", disposition=None)
    b = listing(2, category_main="pozemek", disposition=None)
    assert disposition_demonstrated(a, b)


def test_a_layout_only_one_side_states_is_not_agreement() -> None:
    assert not disposition_demonstrated(listing(1), listing(2, disposition=None))


# ------------------------------------------------------------- E158 (B) unit-grade corroboration
def _feats(**kwargs: float) -> dict[str, tuple[float, bool]]:
    return {name: (value, True) for name, value in kwargs.items()}


def test_a_photo_file_in_common_is_unit_grade() -> None:
    feats = _feats(phash_tight_matches=1.0)
    assert corroboration_warrant(listing(1), listing(2), feats, S) == "photo"


def test_agreement_on_public_attributes_alone_is_not() -> None:
    assert corroboration_warrant(listing(1), listing(2), _feats(same_exact_pin=1.0), S) is None


def test_m_accepts_two_of_the_wider_list_where_s_accepts_none() -> None:
    feats = _feats(same_exact_pin=1.0, price_path_event_match=1.0)
    assert corroboration_warrant(listing(1), listing(2), feats, S) is None
    assert corroboration_warrant(listing(1), listing(2), feats, M) is not None


def test_every_corroboration_mode_answers_unit_first() -> None:
    """What makes S ⊆ M an identity rather than a measurement (E159)."""
    feats = _feats(phash_tight_matches=1.0)
    for mode in ("unit", "two_of", "development_only"):
        settings = Settings(demonstrate_identity=True, corroboration=mode)
        assert corroboration_warrant(listing(1), listing(2), feats, settings) == "photo"


def test_a_shared_body_is_not_unit_grade_inside_a_development() -> None:
    """Černovírské zahrady: three idnes adverts posted two seconds apart, three distinct detail
    URLs, BYTE-IDENTICAL bodies, each 282 m² at 1,580,000, for a project the body itself calls
    `tři samostatné parcely`. The body agrees because it is the developer's, not the plot's."""
    project = ("Nabízíme k prodeji rekreační pozemek o výměře cca 282 m² v rámci projektu "
               "Černovírské zahrady v Olomouci-Černovíře. Pozemek je součástí komorního "
               "projektu zahrnujícího tři samostatné parcely s vlastní komunikací.")
    plain = "Prodej pozemku 282 m² v klidné části obce, veškeré sítě na hranici pozemku."
    feats = _feats(containment_max=1.0)
    a, b = listing(1, description=project), listing(2, description=project)
    assert corroboration_warrant(a, b, feats, S) is None
    assert corroboration_warrant(a, b, feats, M) is None
    assert corroboration_warrant(a, b, _feats(phash_tight_matches=1.0), S) == "photo"
    outside = listing(3, description=plain), listing(4, description=plain)
    assert corroboration_warrant(*outside, feats, S) == "body"


def test_l_asks_for_no_demonstration_at_all() -> None:
    assert demonstration_refusal(listing(1), listing(2, price=None), {}, L) is None
    assert demonstration_refusal(listing(1), listing(2, price=None), {}, M) == "A:price"


def test_corroboration_cannot_be_asked_for_without_the_demonstration_it_refines() -> None:
    with pytest.raises(ValueError, match="corroboration needs demonstrate_identity"):
        Settings(corroboration="unit")
    with pytest.raises(ValueError, match="demonstrate_cluster_price needs"):
        Settings(demonstrate_cluster_price=True)


def test_a_small_co_live_price_gap_is_two_units_and_a_large_one_is_two_prices() -> None:
    """D49 refused the co-live price CONTRADICTION on a mechanism: one advert legitimately
    carries a freehold price and a co-operative SHARE at the same time. Every one of its
    counter-examples is a RATIO. Three Hlubočky houses are 2 % apart for 82 days."""
    settings = Settings(demonstrate_identity=True, demonstrate_cluster_price=True)
    near = listing(1, price=9_650_000.0, source="ceskereality")
    same_project = listing(2, price=9_850_000.0, source="ceskereality")
    coop_share = listing(3, price=1_670_000.0, source="sreality")
    assert price_conflict(near, same_project, settings, False, 82.4)
    assert not price_conflict(listing(4), coop_share, settings, False, 82.4)


# --------------------------------------------------------------- E159 the ladder, as it ships
def test_the_three_arms_are_one_reader_set_and_two_extra_questions() -> None:
    raw = {name: json.loads((ROOT / f"w16_{name}.json").read_text(encoding="utf-8"))
           for name in ("l", "m", "s")}
    for reader in READERS:
        assert all(row[reader] is True for row in raw.values()), reader
    assert raw["l"]["demonstrate_identity"] is False
    assert raw["m"]["demonstrate_identity"] is True
    assert raw["m"]["corroboration"] == "development_only"
    assert raw["s"]["demonstrate_identity"] is True and raw["s"]["corroboration"] == "unit"
    changed = {key for key in set(raw["m"]) | set(raw["s"])
               if raw["m"].get(key) != raw["s"].get(key)}
    assert changed == {"corroboration"}
    # L asks neither extra question, so the cluster-grain price limb is M and S's alone.
    assert raw["l"]["demonstrate_cluster_price"] is False
    assert raw["m"]["demonstrate_cluster_price"] is True


def test_w16_is_w15_plus_the_readers_and_nothing_else() -> None:
    w15 = json.loads((ROOT / "w15.json").read_text(encoding="utf-8"))
    w16 = json.loads((ROOT / "w16_l.json").read_text(encoding="utf-8"))
    changed = {key for key in set(w15) | set(w16) if w15.get(key) != w16.get(key)}
    # The readers, plus the rows L states explicitly at their defaults so the arm files name
    # every question the ladder asks instead of leaving two of them to be inferred.
    assert changed == set(READERS) | {
        "d43_body_align_min_ratio", "demonstrate_identity", "demonstrate_price_colive_days",
        "demonstrate_price_colive_fraction", "demonstrate_require_disposition",
        "demonstrate_require_obec", "corroboration", "corroboration_body_containment",
        "corroboration_min", "demonstrate_cluster_price",
    }
    assert Settings.from_json(ROOT / "w16_l.json").demonstrate_identity is False


def test_the_earlier_generations_read_none_of_it() -> None:
    """g7, g8 and g8b must keep replaying byte-for-byte, so every W16 row is off in all three."""
    for name in ("w13", "w14", "w15"):
        settings = Settings.from_json(ROOT / f"{name}.json")
        for reader in READERS:
            assert getattr(settings, reader) is False, (name, reader)
        assert settings.demonstrate_identity is False
        assert settings.corroboration == "off"
        assert settings.demonstrate_cluster_price is False


def test_the_defaults_are_off_so_an_unset_row_is_g8bs_reading() -> None:
    default = Settings()
    for reader in READERS:
        assert getattr(default, reader) is False, reader
    assert default.demonstrate_identity is False
    assert default.corroboration == "off"
    assert default.demonstrate_cluster_price is False
