"""CHECK — step 4 of 4: which country this is, and whether the row disagrees with itself.

Two halves, and they were two separate stages before W2-a. The country determination was S2
(310 lines, a four-signal ladder with a method and a confidence of its own); the
self-consistency comparison was S9, the reconciler — eight rules, a contradiction ledger, a
disposition table and an auto-close engine. They collapse into one function and one nullable
`disputed` column whose value IS the reason.

Every rejection tested here is a verified corpus trap that would otherwise flag EVERY
listing on at least one portal: `Zahraniční nemovitosti` is site nav on 100 % of mmreality
and ceskereality pages, the REMAX footer lists twelve countries, EUR is standard practice on
CZ commercial rent, and the Regus boilerplate advertises a "global network of thousands of
branches" on two Karlín/Nusle listings.
"""

from __future__ import annotations

import pytest

from location_data.resolver import check as step_check
from location_data.resolver import core
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm


def _resolve(claims, mirror=None):
    return core.resolve(
        claims, mm.context(mirror), resolver_version=RESOLVER_VERSION,
        registry_version="ruian:2026-07-31",
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
    assert step_check.is_rejected_country_evidence(text) == trap


def test_a_nav_country_claim_never_reaches_a_determination():
    claims = [
        mm.claim(1, "country", value_text="Zahraniční nemovitosti", source="mmreality",
                 extraction_method="html_selector_parse"),
    ]
    assert _resolve(claims).country_status == "undetermined"


def test_a_subject_scoped_false_claim_is_inadmissible_by_construction():
    claims = [
        mm.claim(1, "country", value_text="Španělsko", subject_scoped=False,
                 extraction_method="regex_text"),
    ]
    assert _resolve(claims).country_status == "undetermined"


def test_a_eur_price_sentence_never_makes_a_listing_foreign():
    claims = [
        mm.claim(1, "address_line_verbatim",
                 value_text="Kancelář Praha 8 - Karlín, nájem 18 EUR/m2, Španělsko",
                 extraction_method="regex_text"),
    ]
    assert _resolve(claims).country_status != "foreign"


def test_a_country_named_mid_sentence_is_not_an_address_tail():
    assert step_check.country_from_text("byt jako ve Španělsko u moře, Praha 6") is None


# ------------------------------------------------------------------- the determination


def test_a_trailing_country_token_is_foreign_and_nothing_else():
    """idnes ships the country in Czech, in a stored text column: 22 545 active rows. A
    foreign row carries the code and NO Czech hierarchy — there is none to carry."""
    resolution = _resolve([
        mm.claim(1, "address_line_verbatim", value_text="Benahavís, Španělsko",
                 source="idnes", extraction_method="portal_structured_field"),
        mm.claim(2, "coordinate", lat=36.5090, lon=-4.8856, source="idnes"),
    ])
    assert (resolution.country_status, resolution.country_code) == ("foreign", "ES")
    assert resolution.obec_kod is None and resolution.obec_name is None
    assert resolution.granularity == "country"
    # The pin is KEPT — it is geographically correct, it is simply not in Czechia.
    assert (resolution.lat, resolution.lon) == (36.5090, -4.8856)


def test_the_bazos_psc_buckets_are_country_signals_not_postcodes():
    resolution = _resolve([mm.claim(1, "psc", value_text="987 65", source="bazos")])
    assert (resolution.country_status, resolution.country_code) == ("foreign", "SK")


def test_the_zahranici_bucket_is_foreign_with_no_code():
    resolution = _resolve([mm.claim(1, "obec_name", value_text="Zahraničí", source="bazos")])
    assert (resolution.country_status, resolution.country_code) == ("foreign", None)
    assert resolution.granularity == "unknown"


def test_the_bbox_never_determines_foreign():
    """The Wisła hotel sits 0.008° outside the box and the Italian row's coordinates are
    geographically correct for Scalea: those pins are *uncountried, not wrong*. Outside the
    bbox with no other signal is `undetermined`, never `foreign`."""
    mirror = mm.default_mirror()
    mirror.cz_polygon = None
    mirror.obec_polygons = {}
    resolution = _resolve([mm.claim(1, "coordinate", lat=49.65, lon=19.20)], mirror)
    assert resolution.country_status == "undetermined"
    assert resolution.granularity == "unknown"


def test_inside_the_bbox_with_no_boundary_pack_is_still_czech():
    """Degraded mode only: with the state polygon unloaded the bbox is the last signal
    standing, and it may say CZ — it may never say foreign."""
    mirror = mm.default_mirror()
    mirror.cz_polygon = None
    mirror.obec_polygons = {}
    resolution = _resolve([mm.claim(1, "coordinate", lat=50.0755, lon=14.4378)], mirror)
    assert resolution.country_status == "cz"


def test_registry_containment_is_authoritative_for_cz():
    resolution = _resolve([mm.claim(1, "coordinate", lat=50.0755, lon=14.4378)])
    assert (resolution.country_status, resolution.country_code) == ("cz", "CZ")


def test_a_listing_with_no_claims_at_all_is_undetermined_never_foreign():
    """Rule 25: foreign is a determination the resolver MAKES. The row exists and says
    `undetermined`, which is how coverage counts it."""
    resolution = core.resolve(
        [], mm.context(), resolver_version=RESOLVER_VERSION,
        registry_version="ruian:2026-07-31", listing_id=4242, source="remax",
    )
    assert resolution.country_status == "undetermined"
    assert resolution.granularity == "unknown"
    assert resolution.listing_id == 4242 and resolution.source == "remax"


# -------------------------------------------------------------------- the three reasons


def test_a_clean_row_is_not_disputed():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "coordinate", lat=50.0755, lon=14.4378),
    ])
    assert resolution.disputed is None


def test_a_pin_outside_the_resolved_obec_keeps_the_pin_and_drops_to_the_admin_level():
    """The pin says Bílovec, the name says Praha. Both are kept — the pin because it is the
    only position there is, the town because the name is the stronger claim — and the row
    says so in one word."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou"),
        mm.claim(3, "coordinate", lat=49.7573, lon=18.0158),
    ])
    assert resolution.disputed == "pin_outside_obec"
    assert resolution.obec_kod == 554782
    assert (resolution.lat, resolution.lon) == (49.7573, 18.0158)
    # BIND reached the street rung; the disagreement drops it back to the admin level.
    assert resolution.granularity == "obec"


def test_a_pin_outside_czechia_with_a_czech_town_is_disputed_not_foreign():
    """Almost always a geocoder artifact; the town is the trustworthy half, so the row stays
    Czech and carries the reason."""
    mirror = mm.default_mirror()
    mirror.cz_polygon = (49.8, 15.5, 1000.0)  # a state polygon that covers almost nothing
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "coordinate", lat=50.0755, lon=14.4378),
    ], mirror)
    assert resolution.country_status == "cz"
    assert resolution.disputed == "pin_outside_cz"


def test_text_that_says_foreign_over_a_czech_town_is_a_conflict_never_a_silent_flip():
    """remax 442804 is genuinely in Poland and the portal files it as Opava; three of the
    five corpus `foreign_suspect` rows are the inverse — pure geocoder artifacts. Trusting
    either side unconditionally is wrong in both directions."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "foreign_indicator", value_text="Polsko", extraction_method="llm_text",
                 source="remax"),
    ])
    assert resolution.country_status == "cz"
    assert resolution.disputed == "country_conflict"


def test_a_registry_point_can_never_be_outside_its_own_town():
    """Only a PORTAL PIN can fall outside the town the row names; a registry point is inside
    by construction, so the comparison is not even made for it."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
        mm.claim(3, "coordinate", lat=49.7573, lon=18.0158),
    ])
    assert resolution.ruian_adm_kod == 21690278
    assert resolution.disputed is None
