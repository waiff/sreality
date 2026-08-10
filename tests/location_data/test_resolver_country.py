"""S2 — country determination, and the §3.4.2 false-positive rejections.

Every rejection here is a verified corpus trap that would otherwise flag EVERY listing on
at least one portal: `Zahraniční nemovitosti` is site nav on 100 % of mmreality and
ceskereality pages, the REMAX footer lists twelve countries, EUR is standard practice on CZ
commercial rent, and the Regus boilerplate advertises a "global network of thousands of
branches" on two Karlín/Nusle listings.
"""

from __future__ import annotations

import pytest

from location_data.resolver import country as s2
from location_data.resolver import normalize
from tests.location_data import mini_mirror as mm


def _determine(claims, *, pin=None, mirror=None):
    normalized = normalize.normalize_all(claims)
    return s2.determine_country(
        claims, normalized, registry=mirror or mm.default_mirror(),
        constants=mm.CONSTANTS, pin=pin,
    )


# --------------------------------------------------------------- mandatory rejections


@pytest.mark.parametrize(
    "text, trap",
    [
        ("Zahraniční nemovitosti", "site_nav_zahranicni_nemovitosti"),
        ("Prodej bytu, cena 250 000 EUR", "eur_denomination"),
        ("Cena 4 500 €/měsíc", "eur_denomination"),
        ("Regus — globální síť tisíců poboček", "regus_boilerplate"),
        (
            "Austria Belgium Bulgaria Croatia Czechia France Germany Italy Poland Spain",
            "country_list_footer",
        ),
    ],
)
def test_the_four_named_traps_are_rejected_by_content(text, trap):
    assert s2.is_rejected_country_evidence(text) == trap


def test_a_nav_country_claim_never_reaches_a_determination():
    claims = [
        mm.claim(1, "country", value_text="Zahraniční nemovitosti", source="mmreality",
                 extraction_method="html_selector_parse"),
    ]
    assert _determine(claims).status == "undetermined"


def test_a_subject_scoped_false_claim_is_inadmissible_by_construction():
    claims = [
        mm.claim(1, "country", value_text="Španělsko", subject_scoped=False,
                 extraction_method="regex_text"),
    ]
    assert _determine(claims).status == "undetermined"


def test_a_eur_price_sentence_never_makes_a_listing_foreign():
    claims = [
        mm.claim(1, "address_line_verbatim",
                 value_text="Kancelář Praha 8 - Karlín, nájem 18 EUR/m2, Španělsko",
                 extraction_method="regex_text"),
    ]
    assert _determine(claims).status != "foreign"


# ------------------------------------------------------------------- signal ladder


def test_signal_1b_reads_the_trailing_country_token():
    """idnes ships the country in Czech, in a stored text column: 22 545 active rows."""
    claims = [
        mm.claim(1, "address_line_verbatim", value_text="Benahavís, Španělsko",
                 source="idnes", extraction_method="portal_structured_field"),
    ]
    determination = _determine(claims)
    assert (determination.status, determination.country_code) == ("foreign", "ES")
    assert determination.method == "portal_field"


def test_a_country_named_mid_sentence_is_not_an_address_tail():
    assert s2.country_from_text("byt jako ve Španělsko u moře, Praha 6") is None


def test_the_bazos_psc_buckets_are_country_signals_not_postcodes():
    claims = [mm.claim(1, "psc", value_text="987 65", source="bazos")]
    determination = _determine(claims)
    assert (determination.status, determination.country_code) == ("foreign", "SK")
    assert determination.method == "portal_bucket"


def test_the_zahranici_bucket_is_foreign_with_no_code():
    claims = [mm.claim(1, "obec_name", value_text="Zahraničí", source="bazos")]
    determination = _determine(claims)
    assert (determination.status, determination.country_code) == ("foreign", None)


def test_text_versus_pin_produces_disputed_never_a_silent_flip():
    """remax 442804 is genuinely in Poland and the portal files it as Opava; three of the
    five corpus `foreign_suspect` rows are the inverse — pure geocoder artifacts."""
    claims = [
        mm.claim(1, "foreign_indicator", value_text="Polsko", extraction_method="llm_text",
                 source="remax"),
    ]
    determination = _determine(claims, pin=(49.9, 17.9))
    assert determination.status == "disputed"
    assert determination.country_code is None
    assert determination.conflicting


def test_the_bbox_never_determines_foreign():
    """The Wisła hotel sits 0.008° outside the box and the Italian row's coordinates are
    geographically correct for Scalea: those pins are *uncountried, not wrong*. Outside the
    bbox with no other signal is `undetermined`, never `foreign`."""
    mirror = mm.default_mirror()
    mirror.cz_polygon = None
    outside = _determine([], pin=(49.65, 19.20), mirror=mirror)
    assert outside.status == "undetermined"
    assert outside.method == "unknown"


def test_inside_the_bbox_with_no_boundary_pack_is_a_labelled_weak_assumption():
    """Degraded mode only: with the state polygon unloaded the bbox yields an explicitly
    low-confidence `assumed_default`, never a high-confidence determination."""
    mirror = mm.default_mirror()
    mirror.cz_polygon = None
    determination = _determine([], pin=(50.0755, 14.4378), mirror=mirror)
    assert determination.status == "cz"
    assert determination.method == "assumed_default"
    assert determination.confidence == "low"


def test_registry_containment_is_authoritative_for_cz():
    determination = _determine([], pin=(50.0755, 14.4378))
    assert (determination.status, determination.method) == ("cz", "registry_containment")
