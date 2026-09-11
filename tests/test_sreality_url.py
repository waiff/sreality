"""scraper.sreality_url — the ONE assembly site for sreality's listings.source_url.

Pins the closed, sreality-sourced codebook (never a slugified display label), the
type-slug remap the stored vocabulary needs (drazba -> drazby, podil -> podily), sreality's
own locality shape (repeated city, trailing hyphen), the literal '+', every counted
decline reason, and that the two input adapters agree on one payload.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scraper import parser
from scraper import sreality_url as su

FIXTURES = Path(__file__).parent / "fixtures"


def _payload(**overrides):
    base = {
        "hash_id": 1915215948,
        "category_type_cb": {"name": "Prodej", "value": 1},
        "category_main_cb": {"name": "Domy", "value": 2},
        "category_sub_cb": {"name": "Rodinný", "value": 37},
        "locality": {
            "city_seo_name": "praha",
            "citypart_seo_name": "michle",
            "street_seo_name": "pod-sychrovem-i",
        },
    }
    base.update(overrides)
    return base


# --- the regression case -----------------------------------------------------------------


def test_operator_reported_house_resolves_to_srealitys_own_slug() -> None:
    url, reason = su.from_payload(_payload())
    assert reason is None
    assert url == (
        "https://www.sreality.cz/detail/prodej/dum/rodinny/praha-michle-pod-sychrovem-i/1915215948"
    )
    assert "rodinny-dum" not in url  # the label-derived slug that 404s


# --- the codebook ------------------------------------------------------------------------


def test_codebook_is_closed_and_well_formed() -> None:
    assert len(su.SUB_SLUG) == 48
    for cb, (main, slug) in su.SUB_SLUG.items():
        assert isinstance(cb, int) and cb > 0
        assert main in su.MAIN_SLUG.values()
        assert re.fullmatch(r"[a-z0-9+-]+", slug), slug


@pytest.mark.parametrize(
    "cb,expected",
    [
        (37, "rodinny"), (28, "obchodni-prostor"), (26, "sklad"), (27, "vyrobni-prostor"),
        (32, "ostatni-komercni-prostory"), (35, "pamatka"), (57, "apartman"),
        (31, "zemedelsky"), (36, "jine-nemovitosti"), (24, "ostatni-pozemky"),
        (21, "les"), (22, "louka"), (23, "zahrada"), (46, "rybnik"), (53, "mobilni-domek"),
        (12, "6-a-vice"), (38, "cinzovni-dum"),
    ],
)
def test_slugs_that_a_label_slugify_gets_wrong(cb: int, expected: str) -> None:
    # Each of these was verified live (2026-09-10/11): the label-derived slug 404s,
    # sreality's own slug resolves.
    assert su.SUB_SLUG[cb][1] == expected


def test_type_slugs_are_srealitys_plurals_not_the_stored_vocabulary() -> None:
    assert su.TYPE_SLUG == {1: "prodej", 2: "pronajem", 3: "drazby", 4: "podily"}
    assert su.STORED_TYPE_SLUG["drazba"] == "drazby"
    assert su.STORED_TYPE_SLUG["podil"] == "podily"


def test_vocabularies_stay_in_lockstep_with_the_parser() -> None:
    # The drift alarm: a code the parser learns must be here too, and vice versa.
    assert set(su.TYPE_SLUG) == set(parser.CATEGORY_TYPE)
    assert set(su.STORED_TYPE_SLUG) == set(parser.CATEGORY_TYPE.values())
    assert set(su.MAIN_SLUG.values()) == set(parser.CATEGORY_MAIN.values())
    assert set(parser.SUBTYPE) <= set(su.SUB_SLUG)


def test_plus_in_flat_dispositions_is_literal() -> None:
    url, _ = su.from_payload(_payload(
        category_main_cb={"value": 1}, category_sub_cb={"value": 5},
    ))
    assert "/byt/2+1/" in url  # '%2B' also resolves live, '%20' 404s — store the literal


# --- the locality rule -------------------------------------------------------------------


def test_locality_repeats_the_city_when_citypart_is_missing_and_keeps_trailing_hyphen() -> None:
    url, _ = su.from_payload(_payload(locality={"city_seo_name": "prestavlky"}))
    assert url.endswith("/rodinny/prestavlky-prestavlky-/1915215948")


def test_locality_repeats_the_city_even_when_citypart_equals_it() -> None:
    url, _ = su.from_payload(_payload(
        locality={"city_seo_name": "karlovy-vary", "citypart_seo_name": "karlovy-vary"},
    ))
    assert "/karlovy-vary-karlovy-vary-/" in url


def test_locality_missing_declines_rather_than_guessing() -> None:
    assert su.from_payload(_payload(locality={})) == (None, su.Declined.LOCALITY_NULL)


# --- declines, never raises --------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"hash_id": None}, su.Declined.ID_MISSING),
        ({"category_type_cb": {"value": 0}}, su.Declined.TYPE_NULL),
        ({"category_type_cb": {"value": 9}}, su.Declined.TYPE_NULL),
        ({"category_main_cb": None}, su.Declined.MAIN_NULL),
        ({"category_sub_cb": {"value": 0}}, su.Declined.SUB_NULL),
        ({"category_sub_cb": {"value": 41}}, su.Declined.SUB_UNKNOWN),
        ({"category_main_cb": {"value": 1}}, su.Declined.SUB_MAIN_MISMATCH),  # 37 is a dum code
    ],
)
def test_every_non_derivation_is_a_counted_reason(overrides, reason) -> None:
    assert su.from_payload(_payload(**overrides)) == (None, reason)


def test_legacy_v2_and_index_shaped_payloads_decline_without_raising() -> None:
    # The legacy v2 detail shape (pre-2026-05-26 raw_json) has no category_*_cb and a
    # free-text locality; freshness + snapshot diffs run parse_listing over it.
    legacy = {"_embedded": {}, "seo": {"category_sub_cb": 4, "locality": "olomouc-slavonin-jizni"},
              "locality": {"name": "Adresa", "value": "Jižní, Olomouc - Slavonín"}}
    assert su.from_payload(legacy)[0] is None
    assert su.from_payload({})[0] is None
    assert su.from_payload("not a dict")[0] is None  # type: ignore[arg-type]


# --- the reconciler adapter ---------------------------------------------------------------


def test_from_columns_maps_the_stored_type_vocabulary() -> None:
    url, reason = su.from_columns(
        category_type="drazba", category_main="komercni", category_sub_cb=31,
        locality="Týček", street=None, street_source=None, sreality_id=3147342668,
    )
    assert reason is None
    # Verified live: sreality's own 301 target for this listing.
    assert url == "https://www.sreality.cz/detail/drazby/komercni/zemedelsky/tycek-tycek-/3147342668"


def test_from_columns_declines_an_unmapped_stored_type() -> None:
    assert su.from_columns(
        category_type="ostatni", category_main="dum", category_sub_cb=37,
        locality="Praha - Michle", street=None, street_source=None, sreality_id=1,
    ) == (None, su.Declined.TYPE_UNMAPPED)


def test_from_columns_uses_the_street_unless_the_resolver_wrote_it() -> None:
    kw = dict(category_type="prodej", category_main="dum", category_sub_cb=37,
              locality="Praha - Michle", street="Pod Sychrovem I", sreality_id=1915215948)
    # parser-stamped and pre-stamping (NULL provenance) streets are the portal's own;
    # only a RÚIAN-resolved street is left out (it was never on sreality's page).
    assert su.from_columns(street_source="parser", **kw)[0].endswith("/praha-michle-pod-sychrovem-i/1915215948")
    assert su.from_columns(street_source=None, **kw)[0].endswith("/praha-michle-pod-sychrovem-i/1915215948")
    assert su.from_columns(street_source="resolver", **kw)[0].endswith("/praha-michle-/1915215948")


def test_from_columns_recovers_the_street_from_the_legacy_locality_prefix() -> None:
    url, _ = su.from_columns(
        category_type="pronajem", category_main="byt", category_sub_cb=4,
        locality="Jižní, Olomouc - Slavonín", street=None, street_source=None, sreality_id=2836292428,
    )
    assert url == "https://www.sreality.cz/detail/pronajem/byt/2+kk/olomouc-slavonin-jizni/2836292428"
    assert su.legacy_street("Jižní, Olomouc - Slavonín") == "Jižní"
    assert su.legacy_street("Olomouc - Slavonín") is None


@pytest.mark.parametrize(
    "street,expected",
    [("Z. M. Kuděje", "z-m-kudeje"), ("nábřeží Svazu protifašistických bojovníků",
      "nabrezi-svazu-protifasistickych-bojovniku"), ("U Dálnice", "u-dalnice")],
)
def test_slugify_matches_srealitys_street_seo_names_from_the_first_live_sample(street, expected) -> None:
    assert su.slugify(street) == expected


def test_adapters_agree_on_the_fixture_payload() -> None:
    raw = json.loads((FIXTURES / "sample_listing.json").read_text("utf-8"))
    row = parser.parse_listing(raw)
    from_payload = su.from_payload(raw)
    from_columns = su.from_columns(
        category_type=row["category_type"], category_main=row["category_main"],
        category_sub_cb=row["category_sub_cb"], locality=row["locality"],
        street=row["street"], street_source="parser" if row["street"] else None,
        sreality_id=row["sreality_id"],
    )
    assert from_payload == from_columns
    assert from_payload[0] == row["source_url"]  # and the parser emits exactly this
    assert from_payload[0].endswith("/prodej/byt/3+kk/ostrava-petrkovice-/3292504140")


@pytest.mark.parametrize(
    "locality,expected",
    [
        ("Olomouc - Slavonín", ("Olomouc", "Slavonín")),
        ("Olomouc", ("Olomouc", None)),
        ("Jižní, Olomouc - Slavonín", ("Olomouc", "Slavonín")),   # legacy v2 shape
        ("Praha - Praha 6", ("Praha", "Praha 6")),
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_split_locality_handles_both_stored_shapes(locality, expected) -> None:
    assert su.split_locality(locality) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Praha", "praha"), ("Mladá Boleslav II", "mlada-boleslav-ii"),
        ("Pod Sychrovem I", "pod-sychrovem-i"), ("K Šedivce", "k-sedivce"),
        ("Praha 10", "praha-10"), ("Привет", None), ("", None), (None, None),
    ],
)
def test_slugify_reproduces_srealitys_seo_names(text, expected) -> None:
    assert su.slugify(text) == expected
