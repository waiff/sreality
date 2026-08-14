"""W2a-3b: the idnes / ceskereality / realitymix profiles, against real refetches.

Every fixture here is a body `scripts/location_payload_diff_probe.py` actually
fetched from the live portal on 2026-08-13: `*_a1` and `*_a2` are the SAME detail
page seconds apart, `*_b1` is a different listing on the same portal. (`<style>`
and `<svg>` were dropped to halve the weight — presentation-only, and `<style>` is
already in the profile's strip set, so neither can carry a hash relation; the trim
was verified to leave every assertion below unchanged. Inline `<script>` is kept:
ceskereality and realitymix carry map configuration there.)

TWO edits to those bytes, both deliberate. The trim above, and a contact scrub:
this repo is PUBLIC, so the brokers' phone numbers, e-mail addresses and names
were replaced with the house placeholders (`+420 XXX XXX XXX`, `agent@example.cz`,
`Jan Novák`) by `scripts/fetch_and_anonymize_fixtures.py --scrub-contacts`. It is
contact-scoped rather than that script's blanket 9-digit sweep, which would have
rewritten the coordinates and photo ids these fixtures exist to protect; it is
deterministic and was applied identically to a1/a2/b1, so all three hash relations
below hold exactly as they did on the untouched bodies.
`test_no_committed_fixture_carries_contact_details` keeps it that way.

Two assertions per portal, and the second is the one that matters:

  * a1 == a2 after normalisation says the profile covers what actually churns;
  * a1 != b1 after normalisation says it does not cover so much that two DIFFERENT
    listings collapse onto one hash. An over-broad selector is a far worse failure
    than a change rate that is a few points high — it would make the payload archive
    silently drop a real body — and that is exactly what this half catches.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from location_data.payload_norm import (
    DEFAULT_VOLATILE_PROFILES,
    VolatileProfile,
    normalise,
    selector_is_safe,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "location_w2a_refetch"
_HTML = "text/html; charset=utf-8"

# The three portals whose profiles W2a-3b replaced with measured ones.
_MEASURED = ("idnes", "ceskereality", "realitymix")


def _body(name: str) -> bytes:
    return (_FIXTURES / f"{name}.html").read_bytes()


def _norm(name: str, source: str) -> bytes:
    return normalise(
        _body(name), content_type=_HTML, volatile=DEFAULT_VOLATILE_PROFILES[source],
    ).norm_sha256


def _raw(name: str) -> bytes:
    return normalise(
        _body(name), content_type=_HTML, volatile=VolatileProfile(),
    ).raw_sha256


@pytest.mark.parametrize("source", _MEASURED)
def test_two_fetches_of_one_page_differ_raw_but_normalise_alike(source: str) -> None:
    assert _raw(f"{source}_a1") != _raw(f"{source}_a2"), "fixture pair is not a refetch"

    assert _norm(f"{source}_a1", source) == _norm(f"{source}_a2", source)


@pytest.mark.parametrize("source", _MEASURED)
def test_a_different_listing_still_normalises_differently(source: str) -> None:
    """The over-stripping guard: profiles must not collapse distinct listings."""
    assert _norm(f"{source}_a1", source) != _norm(f"{source}_b1", source)


@pytest.mark.parametrize("source", _MEASURED)
def test_the_listing_itself_survives_normalisation(source: str) -> None:
    """A profile that stripped the page down to chrome would pass the two hash
    assertions and be useless. Anchor on what W2 extracts: the listing's own text."""
    normalised = normalise(
        _body(f"{source}_a1"), content_type=_HTML,
        volatile=DEFAULT_VOLATILE_PROFILES[source],
    ).norm_bytes

    assert b"Prodej" in normalised or b"prodej" in normalised
    assert len(normalised) > 10_000


def test_idnes_strips_the_contact_form_antispam_and_the_similar_offers_rail() -> None:
    normalised = normalise(
        _body("idnes_a1"), content_type=_HTML,
        volatile=DEFAULT_VOLATILE_PROFILES["idnes"],
    ).norm_bytes

    # The observed values, and the ELEMENTS that carried them. The bare word
    # `schpeck` survives in the form's static JS and in the visible answer input,
    # which is why these markers are attribute-shaped rather than substrings.
    for gone in (
        b"tshee",
        b'name="schpeckc"',
        b"62da6c7c15cfa1aedbf7d2e2ecc84c54",  # the captcha hash this fetch served
        b'id="schpeckIn"',
        b"grid-similar-offers",
        b"c-grid-products__item",  # the rail's cards, i.e. other listings
    ):
        assert gone not in normalised
    # The contact form itself stays — only its per-response anti-spam material goes.
    assert b"frm-s-result-reactionBox-reactionBoxForm" in normalised


def test_ceskereality_strips_the_bug_report_token_and_keeps_the_map() -> None:
    normalised = normalise(
        _body("ceskereality_a1"), content_type=_HTML,
        volatile=DEFAULT_VOLATILE_PROFILES["ceskereality"],
    ).norm_bytes

    assert b"bug-report-token" not in normalised
    assert b"s-estates-slide" not in normalised
    # The <iframe> src is the listing's coordinates — the artefact W2 exists to mine.
    assert b"google.com/maps/embed" in normalised
    # The listing's own gallery is a SIBLING of the stripped rail, not part of it.
    assert b"s-estate-detail-intro__slider" in normalised


def test_realitymix_strips_the_footer_stamp_and_keeps_the_gps_attributes() -> None:
    normalised = normalise(
        _body("realitymix_a1"), content_type=_HTML,
        volatile=DEFAULT_VOLATILE_PROFILES["realitymix"],
    ).norm_bytes

    assert b"data-gps-lat" in normalised
    assert b"data-gps-lon" in normalised
    # The badge is the portal's entire measured churn; nothing else may go with it.
    assert b"bottom-2 right-2" not in normalised
    assert b"<footer" in normalised


def test_ceskereality_relative_insertion_date_is_the_known_residue() -> None:
    """A "Datum vložení" of "před 22 minutami" re-renders every minute, and no CSS
    selector can pick that row out (it is identified by its LABEL TEXT, and
    selectolax has no text predicate — see selector_is_safe). So it is deliberately
    NOT stripped, which under-states the profile and over-states the change rate:
    the safe direction. ceskereality switches the field to an absolute date once a
    listing is ~2 weeks old, so this only touches fresh inventory.

    Modelled rather than fixtured: committing a second 230KB pair to assert one
    <span> would not teach the next reader more than this does.
    """
    def page(rendered: str) -> bytes:
        return (
            '<html><body><dl class="g-info"><div class="g-info__col">'
            '<div class="i-info"><span class="i-info__title">Datum vložení</span>'
            f'<span class="i-info__value">{rendered}</span></div>'
            '<div class="i-info"><span class="i-info__title">Cena</span>'
            '<span class="i-info__value">2 100 000 Kč</span></div>'
            '</div></dl></body></html>'
        ).encode("utf-8")

    profile = DEFAULT_VOLATILE_PROFILES["ceskereality"]
    minutes = normalise(page("před 22 minutami"), content_type=_HTML, volatile=profile)
    later = normalise(page("před 23 minutami"), content_type=_HTML, volatile=profile)
    absolute = normalise(page("10. března 2026"), content_type=_HTML, volatile=profile)

    assert minutes.norm_sha256 != later.norm_sha256  # the residue, documented
    assert absolute.norm_sha256 == normalise(
        page("10. března 2026"), content_type=_HTML, volatile=profile,
    ).norm_sha256


def test_unsupported_pseudo_class_selectors_are_skipped_not_executed() -> None:
    """`:contains()` SEGFAULTS selectolax on a full-size page (exit 139, reproducible
    on the ceskereality fixture, selectolax 0.4.10). A segfault is not catchable, so
    `normalise` must never hand one to the CSS engine — it has to be refused by
    syntax, before the call. Verified here on the real body that crashes it."""
    assert not selector_is_safe('span.i-info__title:contains("Datum")')
    assert not selector_is_safe("div:is(.a)")
    assert selector_is_safe("div.i-info")
    assert selector_is_safe('input[name="tshee"]')
    assert selector_is_safe("div.a:not(.b)")

    unsafe = VolatileProfile(css_selectors=('span.i-info__title:contains("Datum")',))
    result = normalise(_body("ceskereality_a1"), content_type=_HTML, volatile=unsafe)

    assert result.norm_byte_size > 10_000


def test_a_malformed_selector_does_not_degrade_a_real_body_to_raw() -> None:
    """The full-size half of test_payload_norm's malformed-selector case: on a
    three-node document the raw fallback and the normalised form are nearly the
    same bytes, so only a real page shows what one typo used to cost — the whole
    body, and with it that portal's measured change rate."""
    profile = DEFAULT_VOLATILE_PROFILES["ceskereality"]
    with_typo = VolatileProfile(
        css_selectors=profile.css_selectors + ("div..a", ""),
        strip_attributes=profile.strip_attributes,
    )

    clean = normalise(_body("ceskereality_a1"), content_type=_HTML, volatile=profile)
    typo = normalise(_body("ceskereality_a1"), content_type=_HTML, volatile=with_typo)
    raw_fallback = normalise(
        _body("ceskereality_a1"), content_type=_HTML, volatile=VolatileProfile(),
    )

    assert typo.norm_sha256 == clean.norm_sha256
    assert typo.norm_byte_size < raw_fallback.norm_byte_size


def test_no_committed_fixture_carries_contact_details() -> None:
    """This repo is PUBLIC and these are real portal pages: a broker's mobile
    number or work e-mail committed here would be published permanently, with no
    takedown short of a history rewrite. The scrub that produced these files
    (`--scrub-contacts`, see the module docstring) is idempotent, so this gate is
    also the check that a REFRESHED fixture went through it."""
    phone_like = re.compile(r"(?<![\d/])\+?\d{3}[\s \-]?\d{3}[\s \-]?\d{3}(?!\d)")
    email_like = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

    for path in sorted(_FIXTURES.glob("*.html")):
        text = path.read_text(encoding="utf-8")

        assert set(email_like.findall(text)) <= {"agent@example.cz"}, path.name
        for href in re.findall(r'(?:tel|mailto):[^"\'\s>]*', text):
            assert href.startswith(("tel:+420", "mailto:agent@example.cz")), (
                f"{path.name}: {href}"
            )
        # Digits presented AS a phone number: inside a tel: href, a schema.org
        # "telephone", or a reveal-on-click attribute. A bare 9-digit run
        # elsewhere is a coordinate or an id and must stay (the normaliser is
        # measured on those bytes).
        contexts = re.findall(
            r'(?:tel:|"telephone"\s*:\s*"|-on-click=")([^"\']{0,30})', text,
        )
        for context in contexts:
            assert not phone_like.search(context), f"{path.name}: {context}"
