"""scraper.sreality_url — the ONE assembly site for sreality's listings.source_url.

Pins the closed, sreality-sourced codebook (never a slugified display label), the
type-slug remap the stored vocabulary needs (drazba -> drazby, podil -> podily), sreality's
own locality shape (repeated city, trailing hyphen), the literal '+', every counted
decline reason, and that the parser stores exactly what the assembler derives.
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
    # The stored vocabulary says 'drazba' / 'podil'; both 404 in a URL.
    assert su.TYPE_SLUG == {1: "prodej", 2: "pronajem", 3: "drazby", 4: "podily"}
    assert set(su.TYPE_SLUG.values()) & set(parser.CATEGORY_TYPE.values()) == {"prodej", "pronajem"}


def test_vocabularies_stay_in_lockstep_with_the_parser() -> None:
    # The drift alarm: a code the parser learns must be here too, and vice versa.
    assert set(su.TYPE_SLUG) == set(parser.CATEGORY_TYPE)
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


# --- the parser contract + reading a stored URL back apart ---------------------------------


def test_the_parser_stores_exactly_what_the_assembler_derives() -> None:
    raw = json.loads((FIXTURES / "sample_listing.json").read_text("utf-8"))
    row = parser.parse_listing(raw)
    url, reason = su.from_payload(raw)
    assert reason is None
    assert url == row["source_url"]
    assert url.endswith("/prodej/byt/3+kk/ostrava-petrkovice-/3292504140")


def test_critical_segments_ignores_the_locality_and_rejects_a_foreign_url() -> None:
    canon = "https://www.sreality.cz/detail/prodej/dum/rodinny/praha-michle-/1915215948"
    assert su.critical_segments(canon) == ("prodej", "dum", "rodinny", "1915215948")
    other_locality = canon.replace("/praha-michle-/", "/praha-michle-pod-sychrovem-i/")
    assert su.critical_segments(other_locality) == su.critical_segments(canon)
    assert su.critical_segments("https://reality.bazos.cz/inzerat/1/x.php") is None
    assert su.critical_segments("https://www.sreality.cz/detail/x/1") is None
    assert su.critical_segments(None) is None
