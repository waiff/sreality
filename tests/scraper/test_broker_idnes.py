"""Unit tests for the idnes broker-block parser."""

from __future__ import annotations

import pytest

from scraper.broker_idnes import parse_idnes_broker


def _ent(s: str) -> str:
    """Encode a string as numeric HTML entities (how idnes obfuscates the mailto)."""
    return "".join(f"&#{ord(c)};" for c in s)


OID = "6644a3f8a7571b50b90a8d4a"
_MAILTO = _ent(f"mailto:info@ibero-casa.com?subject=Dotaz na inzerát")

PAGE = f"""
<html><body>
<div class="content" id="f-contact">
  <h3 class="h2">Kontaktujte prodejce</h3>
  <div class="b-author mb-20">
    <div class="avatar b-author__avatar">
      <a href="https://reality.idnes.cz/makler/detail/jiri-svancara-mba/{OID}/" class="avatar__img">
        <img src="x.jpg" alt="Jiří Švancara MBA">
      </a>
    </div>
    <div class="b-author__content">
      <h2 class="b-author__title mb-7">
        <a href="https://reality.idnes.cz/makler/detail/jiri-svancara-mba/{OID}/">Jiří Švancara MBA</a>
      </h2>
      <p class="b-author__info mb-7"><span class="items">
        <strong class="items__item">
          <a onclick="stat.count('clickEmail', ['articleDetail', 'clickEmail', &quot;estate&quot;, [null, &quot;account.{OID}&quot;], [null, &quot;estate.69b5&quot;]]);"
             href="{_MAILTO}">info@…</a>
        </strong>
        <strong class="items__item">
          <a onclick="stat.count('clickPhone', [null, &quot;account.{OID}&quot;]);"
             href="tel:+420777044796">+420 777 044 796</a>
        </strong>
      </span></p>
    </div>
    <div class="b-author__logo"><img src="logo.png" alt="IBERO CASA Real Estate"></div>
  </div>
</div>
</body></html>
"""


def test_parse_full_block():
    b = parse_idnes_broker(PAGE)
    assert b is not None
    assert b["account_oid"] == OID
    assert b["name"] == "Jiří Švancara MBA"
    assert b["email"] == "info@ibero-casa.com"  # decoded + ?subject stripped
    assert b["phone"] == "420777044796"
    assert b["agency_name"] == "IBERO CASA Real Estate"


def test_name_falls_back_to_avatar_alt():
    page = PAGE.replace(
        '<h2 class="b-author__title mb-7">\n        <a href="https://reality.idnes.cz/makler/detail/jiri-svancara-mba/'
        + OID
        + '/">Jiří Švancara MBA</a>\n      </h2>',
        "",
    )
    b = parse_idnes_broker(page)
    assert b is not None
    assert b["name"] == "Jiří Švancara MBA"  # from avatar img alt


def test_no_account_oid_returns_none():
    # A page with a contact box but no account.<oid> anywhere → not attributable.
    page = '<div id="f-contact"><div class="b-author"><h2 class="b-author__title">X</h2></div></div>'
    assert parse_idnes_broker(page) is None


def test_no_contact_box_returns_none():
    assert parse_idnes_broker("<html><body>nope</body></html>") is None
    assert parse_idnes_broker("") is None


def test_phone_national_form_gets_country_code():
    page = PAGE.replace('href="tel:+420777044796"', 'href="tel:777044796"')
    b = parse_idnes_broker(page)
    assert b is not None
    assert b["phone"] == "420777044796"


@pytest.mark.parametrize("missing", ["email", "phone", "agency"])
def test_partial_blocks(missing: str):
    page = PAGE
    if missing == "email":
        page = page.replace(f'href="{_MAILTO}"', 'href="#"')
    elif missing == "phone":
        page = page.replace('href="tel:+420777044796"', 'href="#"')
    elif missing == "agency":
        page = page.replace('<img src="logo.png" alt="IBERO CASA Real Estate">', "")
    b = parse_idnes_broker(page)
    assert b is not None
    assert b["account_oid"] == OID
    if missing == "email":
        assert b["email"] is None
    if missing == "phone":
        assert b["phone"] is None
    if missing == "agency":
        # falls back to eventLabel from the data-layer-json (none here) → None
        assert b["agency_name"] is None
