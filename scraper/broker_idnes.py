"""Extract the broker/agency contact block from an idnes detail page.

The contact section (`#f-contact` / `.b-author`) carries: the individual broker's
iDNES account id (`account.<oid>` in the stat.count calls — the SAME id as the
`/makler/detail/{slug}/{oid}/` link, so it's per-broker, not per-agency), the broker
name, an entity-encoded mailto, a tel: link, and the agency name (logo alt /
data-layer eventLabel). Pure HTML→dict; the resolver attributes from raw_json.broker.
"""

from __future__ import annotations

import html as _html
import re
from typing import Any

from selectolax.parser import HTMLParser, Node

_OID_RE = re.compile(r"account\.([0-9a-f]{24})")
_MAILTO_RE = re.compile(r"mailto:([^?\"'&]+)", re.IGNORECASE)


def parse_idnes_broker(page_html: str) -> dict[str, Any] | None:
    """Return {account_oid, name, email, phone, agency_name} or None."""
    if not page_html:
        return None
    tree = HTMLParser(page_html)
    box = tree.css_first("#f-contact") or tree.css_first(".b-author")
    if box is None:
        return None

    # The broker's stable iDNES account id — without it there's no key to attribute.
    oid = _OID_RE.search(page_html)
    if not oid:
        return None
    account_oid = oid.group(1)

    title = box.css_first(".b-author__title")
    name = _clean(title.text()) if title else None
    if not name:
        avatar = box.css_first(".b-author__avatar img") or box.css_first(".avatar__img img")
        name = _clean(avatar.attributes.get("alt")) if avatar else None

    return {
        "account_oid": account_oid,
        "name": name,
        "email": _email(box),
        "phone": _phone(box),
        "agency_name": _agency(box),
    }


def _clean(s: str | None) -> str | None:
    if not s:
        return None
    return " ".join(s.split()) or None


def _email(box: Node) -> str | None:
    for a in box.css("a[href]"):
        href = _html.unescape(a.attributes.get("href") or "")
        if href.lower().startswith("mailto:"):
            m = _MAILTO_RE.match(href)
            if m:
                em = m.group(1).strip().lower()
                if "@" in em and "." in em.rsplit("@", 1)[-1]:
                    return em
    return None


def _phone(box: Node) -> str | None:
    for a in box.css("a[href]"):
        href = a.attributes.get("href") or ""
        if href.lower().startswith("tel:"):
            digits = re.sub(r"\D", "", href)
            if len(digits) == 9:
                return "420" + digits
            if len(digits) >= 9:
                return digits
    return None


def _agency(box: Node) -> str | None:
    logo = box.css_first(".b-author__logo img")
    if logo:
        alt = _clean(logo.attributes.get("alt"))
        if alt:
            return alt
    for el in box.css("[data-layer-json]"):
        raw = _html.unescape(el.attributes.get("data-layer-json") or "")
        m = re.search(r'"eventLabel"\s*:\s*"([^"]+)"', raw)
        if m:
            return _clean(m.group(1))
    return None
