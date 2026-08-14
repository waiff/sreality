"""Tests for scripts/fetch_and_anonymize_fixtures.py.

Hermetic — no live HTTP. Covers the anonymization regexes only;
the fetch/write orchestration is exercised through the workflow.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# The script lives outside any package, so import via spec.
_SCRIPT = (
    Path(__file__).parent.parent.parent
    / "scripts" / "fetch_and_anonymize_fixtures.py"
)
spec = importlib.util.spec_from_file_location(
    "fetch_and_anonymize_fixtures", _SCRIPT,
)
assert spec is not None and spec.loader is not None
faaf = importlib.util.module_from_spec(spec)
sys.modules["fetch_and_anonymize_fixtures"] = faaf
spec.loader.exec_module(faaf)


def test_anonymize_strips_email():
    out = faaf.anonymize("Contact: jana.novakova@bezrealitky.cz pls")
    assert "jana.novakova" not in out
    assert "agent@example.cz" in out


@pytest.mark.parametrize("phone", [
    "+420 605 123 456",
    "+420605123456",
    "605 123 456",
    "605 123 456",  # NBSP separators
])
def test_anonymize_strips_phone(phone):
    out = faaf.anonymize(f"Tel: {phone}, ulice")
    assert "605" not in out
    assert "+420 XXX XXX XXX" in out


def test_anonymize_strips_street_number():
    out = faaf.anonymize("Anglická 846/1, Praha")
    assert "846/1" not in out
    assert "XXX/YY" in out


def test_anonymize_preserves_html_structure():
    html = (
        '<html><body><div class="spec">'
        '<dl><dt>Užitná plocha</dt><dd>65 m²</dd></dl>'
        '</div></body></html>'
    )
    out = faaf.anonymize(html)
    assert "<dl>" in out
    assert "Užitná plocha" in out
    assert "65 m²" in out


def test_anonymize_prepends_warning_banner():
    out = faaf.anonymize("<html/>")
    assert out.startswith("<!-- ANONYMIZED FIXTURE")


def test_anonymize_does_not_touch_year_or_isbn_like_numbers():
    """A 4-digit year should not look like a phone fragment."""
    out = faaf.anonymize("Rok výstavby 1923, Stav: po rekonstrukci")
    assert "1923" in out
    assert "rekonstrukci" in out


# --- anonymize_contacts(): the contact-scoped variant, for byte-kept fixtures ---

_CONTACT_PAGE = (
    '<html><body>'
    '<div id="print-map" data-gps-lon="14.362793888889" data-gps-lat="50.069672777778"></div>'
    '<img src="https://st.example.cz/i/66685238/makleri/makler_1881361.jpg" alt="Petra Dvořáková">'
    '<a href="/profil-makleru/petra-dvorakova-1881361">Petra Dvořáková</a>'
    '<a class="c" href="tel:720503014" data-hidden-content-on-click="720 503 014">telefon</a>'
    '<a href="tel:+420603363935">+420 603 363 935</a>'
    '<a rel="nofollow" href="/trackredir/8650343/call/detail">800100164</a>'
    '<a href="mailto:petra.dvorakova@example-realiy.cz">petra.dvorakova@example-realiy.cz</a>'
    '<script type="application/ld+json">{"name":"Petra Dvo\\u0159\\u00e1kov\\u00e1",'
    '"telephone":"720503014"}</script>'
    '<td style="--size:0.17813245890041">graf</td>'
    '</body></html>'
)


def _scrubbed():
    return faaf.anonymize_contacts(_CONTACT_PAGE, names=["Petra Dvořáková"])


def test_scrub_contacts_masks_every_rendering_of_a_phone():
    out = _scrubbed()
    for digits in ("720503014", "720 503 014", "603363935", "603 363 935", "800100164"):
        assert digits not in out
    assert out.count(faaf.PHONE_PLACEHOLDER) == 6  # 3x tel:, on-click, +420 text, bare text
    assert 'href="tel:+420 XXX XXX XXX"' in out


def test_scrub_contacts_leaves_the_bytes_a_payload_fixture_is_kept_for():
    """Why this exists instead of anonymize(): its blanket 9-digit sweep rewrites
    coordinates, photo ids and CSS custom properties — the very bytes a
    payload-normaliser fixture is committed to measure."""
    out = _scrubbed()
    assert 'data-gps-lon="14.362793888889"' in out
    assert 'data-gps-lat="50.069672777778"' in out
    assert "makler_1881361.jpg" in out
    assert "--size:0.17813245890041" in out

    blanket = faaf.anonymize(_CONTACT_PAGE)
    assert 'data-gps-lat="50.069672777778"' not in blanket  # the regression it avoids


def test_scrub_contacts_masks_emails_and_every_form_of_a_name():
    out = _scrubbed()
    assert "petra.dvorakova" not in out
    assert "Petra Dvořáková" not in out
    assert "petra-dvorakova" not in out          # the profile-URL slug
    assert "Dvo\\u0159\\u00e1kov\\u00e1" not in out  # the JSON-escaped form
    assert faaf.EMAIL_PLACEHOLDER in out
    assert "petra-dvorakova-1881361" not in out
    assert "jan-novak-1881361" in out


def test_scrub_contacts_is_idempotent_and_banners_once():
    once = _scrubbed()
    twice = faaf.anonymize_contacts(once, names=["Petra Dvořáková"])

    assert twice == once
    assert once.count(faaf._BANNER_MARK) == 1
    assert once.startswith("<!-- ANONYMIZED FIXTURE")


def test_scrub_contacts_only_seeds_from_phone_context():
    """A 9-digit run that no markup calls a phone is left alone; that is the whole
    difference from anonymize(), so it is pinned rather than assumed."""
    assert faaf.phone_seeds('<span data-x="123456789">50.069672777778</span>') == set()
    assert faaf.phone_seeds('<a href="tel:123456789">x</a>') == {"123456789"}
    assert faaf.phone_seeds('{"telephone":"+420 123 456 789"}') == {"123456789"}
