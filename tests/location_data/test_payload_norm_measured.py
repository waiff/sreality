"""The measured volatile profiles, against real refetches (W2a-3b, W2a-3c).

Every fixture here is a body `scripts/location_payload_diff_probe.py` actually
fetched from the live portal — idnes / ceskereality / realitymix on 2026-08-13,
mmreality / remax on 2026-08-14: `*_a1` and `*_a2` are the SAME detail page seconds
apart, `*_b1` is a different listing on the same portal. (`<style>` and `<svg>` were
dropped to halve the weight — presentation-only, and `<style>` is already in the
profile's strip set, so neither can carry a hash relation; the trim was verified to
leave every assertion below unchanged. Inline `<script>` is kept: ceskereality and
realitymix carry map configuration there, and mmreality's whole payload is one.)

The mmreality and remax pairs were fetched across SEPARATE HTTP SESSIONS, because
remax's churn does not exist within one: three fetches eight seconds apart are
byte-identical, and the tokens only re-roll for the next session. The live drain is
a fresh process per run, so cross-session is what production measures.

TWO edits to those bytes, both deliberate. The trim above, and a contact scrub:
this repo is PUBLIC, so the brokers' phone numbers, e-mail addresses and names
were replaced with the house placeholders (`+420 XXX XXX XXX`, `agent@example.cz`,
`Jan Novák`) by `scripts/fetch_and_anonymize_fixtures.py --scrub-contacts`. It is
contact-scoped rather than that script's blanket 9-digit sweep, which would have
rewritten the coordinates and photo ids these fixtures exist to protect; it is
deterministic and was applied identically to a1/a2/b1, so all three hash relations
below hold exactly as they did on the untouched bodies.
`test_no_committed_fixture_carries_contact_details` keeps it that way.

mmreality needed two shapes that scrub did not previously reach, both added with
it: a phone under an embedded-JSON key (`&quot;phone&quot;:&quot;731404040&quot;` —
no `+420`, no grouping, no tel: href, so every seed rule walked past it), and
Cloudflare's obfuscated e-mail payload, which is reversible with a two-line XOR and
would have published the address as plainly as the text would. The Cloudflare
payloads are re-encoded rather than blanked, under the page's OWN key — which is
why a1 and a2 still carry different ciphertexts of the same placeholder, and why
this fixture pair still demonstrates the churn it was fetched to demonstrate.

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
    MEASURED_VOLATILE_PROFILES,
    PAGE_KIND_DETAIL,
    VolatileProfile,
    normalise,
    selector_is_safe,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "location_w2a_refetch"

# What --scrub-contacts substitutes for a real person, in the spellings a page can
# carry it in (plain, and JSON-escaped as the embedded Vue/JSON props do).
_PLACEHOLDER_NAMES = frozenset(
    {"Jan Novák", "Jan Nov\\u00e1k", "Jan Novak", "agent@example.cz"}
)
_HTML = "text/html; charset=utf-8"

# Every portal whose profile is measured rather than guessed: W2a-3b's three, plus
# W2a-3c's two. New entries get the three parametrised assertions for free.
_MEASURED = ("idnes", "ceskereality", "realitymix", "mmreality", "remax")


def _body(name: str) -> bytes:
    return (_FIXTURES / f"{name}.html").read_bytes()


def _norm(name: str, source: str) -> bytes:
    return normalise(
        _body(name), content_type=_HTML,
        volatile=MEASURED_VOLATILE_PROFILES[source][PAGE_KIND_DETAIL],
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
        volatile=MEASURED_VOLATILE_PROFILES[source][PAGE_KIND_DETAIL],
    ).norm_bytes

    assert b"Prodej" in normalised or b"prodej" in normalised
    assert len(normalised) > 10_000


def test_idnes_strips_the_contact_form_antispam_and_the_similar_offers_rail() -> None:
    normalised = normalise(
        _body("idnes_a1"), content_type=_HTML,
        volatile=MEASURED_VOLATILE_PROFILES["idnes"][PAGE_KIND_DETAIL],
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
        volatile=MEASURED_VOLATILE_PROFILES["ceskereality"][PAGE_KIND_DETAIL],
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
        volatile=MEASURED_VOLATILE_PROFILES["realitymix"][PAGE_KIND_DETAIL],
    ).norm_bytes

    assert b"data-gps-lat" in normalised
    assert b"data-gps-lon" in normalised
    # The badge is the portal's entire measured churn; nothing else may go with it.
    assert b"bottom-2 right-2" not in normalised
    assert b"<footer" in normalised


def test_mmreality_strips_the_cloudflare_email_payload_and_keeps_the_coordinates() -> None:
    """mmreality's whole measured churn is Cloudflare re-obfuscating one constant
    address under a fresh random key per response. Both carriers go; the Vue prop
    that holds the listing's own latitude/longitude must not."""
    normalised = normalise(
        _body("mmreality_a1"), content_type=_HTML,
        volatile=MEASURED_VOLATILE_PROFILES["mmreality"][PAGE_KIND_DETAIL],
    ).norm_bytes

    for gone in (b"__cf_email__", b"data-cfemail", b"email-protection"):
        assert gone not in normalised
    # The embedded JSON is the location signal W2 mines here — mmreality has no
    # data-gps attributes, its coordinates live in the Vue prop.
    assert b"latitude" in normalised and b"longitude" in normalised
    assert b"49.500513957" in normalised
    # Only the two obfuscated anchors go, not the sections that hold them: the
    # agent contact form and the footer are both structure the parser reads.
    assert b"rds-agent-contact-form" in normalised
    assert b"rds-footer-contacts" in normalised


def test_remax_strips_the_share_popover_and_keeps_the_forms_map_and_canonical() -> None:
    """The remax token that mattered is INSIDE an attribute value, so the fix is a
    node strip, not a wider input[name] rule. Everything around it stays."""
    normalised = normalise(
        _body("remax_a1"), content_type=_HTML,
        volatile=MEASURED_VOLATILE_PROFILES["remax"][PAGE_KIND_DETAIL],
    ).norm_bytes

    # The escaped <form> lived in `data-content`; no popover payload may survive.
    assert b"data-content" not in normalised
    assert b"dalten_web_send_listing_form" not in normalised
    # ...but only the two buttons go. Their container, the real contact forms and
    # the listing's own geography all stay.
    assert b"pd-share__buttons" in normalised
    assert b"listing-detail-contact-form" in normalised
    assert b"data-gps" in normalised
    assert b"rel=\"canonical\"" in normalised


def test_remax_is_stable_within_a_session_which_is_why_the_pair_is_cross_session() -> None:
    """The methodology assertion, and the reason these two fixtures exist at all.

    A same-session refetch of a remax page is byte-identical — three fetches eight
    seconds apart, 5/5 listings — so a probe that reuses one HTTP session measures
    ZERO churn on a portal production measured at 100%. What re-rolls is minted per
    session, and the live drain is a fresh process every run. This pins the two
    tokens whose values differ between the committed a1 and a2, so a future reader
    cannot mistake this pair for a seconds-apart one.
    """
    a1 = _body("remax_a1").decode("utf-8")
    a2 = _body("remax_a2").decode("utf-8")

    tokens = re.compile(r"name='dalten_web_send_listing_form\[_token\]' value='([^']+)'")
    assert tokens.search(a1) and tokens.search(a2)
    assert tokens.findall(a1) != tokens.findall(a2)

    dom_tokens = re.compile(r'name="mortgage_contact_form\[_token\]" value="([^"]+)"')
    assert dom_tokens.search(a1) and dom_tokens.search(a2)
    assert dom_tokens.findall(a1) != dom_tokens.findall(a2)


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

    profile = MEASURED_VOLATILE_PROFILES["ceskereality"][PAGE_KIND_DETAIL]
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
    profile = MEASURED_VOLATILE_PROFILES["ceskereality"][PAGE_KIND_DETAIL]
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
        # "telephone", a reveal-on-click attribute, or under an embedded-JSON key
        # that says phone (mmreality's Vue prop spells it
        # `&quot;phone&quot;:&quot;731404040&quot;` — no +420, no grouping, no
        # tel: href, so nothing above would have caught it). A bare 9-digit run
        # elsewhere is a coordinate or an id and must stay (the normaliser is
        # measured on those bytes).
        contexts = re.findall(
            r'(?:tel:|"telephone"\s*:\s*"|-on-click=")([^"\']{0,30})', text,
        ) + re.findall(
            r'(?:&quot;|\\?")(?:phone_number|mobile_phone|phone|mobile)'
            r'(?:&quot;|\\?")\s*:\s*(?:&quot;|\\?")([^"&\\]{0,25})',
            text,
        )
        for context in contexts:
            assert not phone_like.search(context), f"{path.name}: {context}"


def test_no_committed_fixture_carries_a_real_persons_name() -> None:
    """The gate the phone/e-mail checks did not cover, added after it failed in
    production: PR #1064 merged with two mortgage advisers' real names still in the
    mmreality fixtures (`&quot;mortgageAdviser&quot;:{...&quot;name&quot;:&quot;Franti\\u0161ek
    Jaro\\u0161&quot;}`). Their e-mails HAD been scrubbed, so every assertion above
    passed, and `--scrub-contacts` takes names as a hand-supplied list — the listing
    agent's name was passed and the adviser's, in a sibling block on the same page,
    was not.

    A name cannot be recognised by shape the way a phone or an address can. What IS
    checkable is that every person-bearing JSON key carries the house placeholder,
    which is exactly the shape that got through: the omission is a missed INPUT, and
    this asserts on the output instead."""
    person_keys = re.compile(
        r"(?:&quot;|\\?\")(?:mortgageAdviser|adviser|advisor|agent|broker|realtor|"
        r"seller|contact|owner|user)(?:&quot;|\\?\")\s*:\s*\{([^{}]{0,400})",
        re.IGNORECASE,
    )
    # Lazy up to the closing quote so a \uXXXX escape is captured whole: the
    # placeholder itself renders as `Jan Novák` in these embedded props, and a
    # class that excluded the backslash would truncate it to "Jan Nov" and fail on
    # a correctly-scrubbed file.
    name_value = re.compile(
        r"(?:&quot;|\\?\")name(?:&quot;|\\?\")\s*:\s*(?:&quot;|\\?\")(.{2,60}?)(?:&quot;|\\?\")"
    )

    for path in sorted(_FIXTURES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for block in person_keys.findall(text):
            for name in name_value.findall(block):
                assert name in _PLACEHOLDER_NAMES, (
                    f"{path.name}: person-bearing block carries {name!r}, which is "
                    f"not a placeholder — re-run --scrub-contacts with --name "
                    f"{name!r} (see this test's docstring)"
                )


def test_no_committed_fixture_carries_an_obfuscated_email() -> None:
    """Cloudflare's e-mail obfuscation is NOT anonymisation: `data-cfemail` and the
    /cdn-cgi/l/email-protection# fragment are the address XOR'd with their own
    leading byte, so the two lines below recover it. Committing one would publish a
    real address while passing every plaintext check in the test above.

    The scrub re-encodes the PLACEHOLDER under each payload's own key, so this
    decodes to `agent@example.cz` while the per-response keys — mmreality's entire
    measured churn — still differ between a1 and a2. That is asserted here too, so
    a future 'fix' that blanked the payloads would fail loudly rather than quietly
    turn the fixture pair into a tautology.
    """
    payload_re = re.compile(
        r'(?:data-cfemail="|/cdn-cgi/l/email-protection#)([0-9a-fA-F]{4,})'
    )

    def decode(payload: str) -> str:
        raw = bytes.fromhex(payload)
        return "".join(chr(byte ^ raw[0]) for byte in raw[1:])

    seen_keys: dict[str, set[str]] = {}
    for path in sorted(_FIXTURES.glob("*.html")):
        payloads = payload_re.findall(path.read_text(encoding="utf-8"))
        for payload in payloads:
            assert decode(payload) == "agent@example.cz", f"{path.name}: {payload}"
        seen_keys[path.name] = {p[:2].lower() for p in payloads}

    assert seen_keys["mmreality_a1.html"], "the obfuscated-email fixture lost its payloads"
    assert seen_keys["mmreality_a1.html"].isdisjoint(seen_keys["mmreality_a2.html"])
