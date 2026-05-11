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
