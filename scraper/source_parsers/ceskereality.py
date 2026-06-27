"""ceskereality.cz per-source parser."""

from __future__ import annotations

from typing import Any

from scraper.source_parsers import common

_PROMPT = """\
Source URL: {url}
Source domain: ceskereality.cz

Layout notes specific to ceskereality.cz:
- A <script type="application/ld+json"> block of @type
  "individualProduct" carries the canonical schema.org data —
  PREFER it: offers.price (an integer, currency CZK) for the price,
  offers.areaServed.address for the locality, and the title (name)
  for the disposition + area. Ignore offers.offeredby.address — that
  is the AGENT's office, not the listing's location.
- Coordinates are already on the page in `data-coord-lat` /
  `data-coord-lng` attributes and in a Google-Maps "?q=lat,lng"
  link — use them directly (lat first).
- The spec section is a list of
  <div class="i-info"><span class="i-info__title">…</span>
  <span class="i-info__value">…</span></div> rows. Czech labels:
  "Plocha užitná" (usable area, prefer over "Plocha obytná"),
  "Dispozice", "Patro" ("přízemí" → 0), "Konstrukce", "Vlastnictví"
  ("soukromé" → osobni, "Družstevní" → druzstevni), "Stav
  nemovitosti", "Energetická náročnost".
- Building type values: "Cihlová" → cihla, "Panelová" → panel,
  "Smíšená" → smisena. Lowercase, no diacritics on the enum.
- The detail URL path is /{{prodej|pronajem}}/{{category}}/… — use it
  to set category_type (prodej = sale, pronajem = rent).
- Strip "Kč" and thousands separators (NBSP \\u00A0) from any price
  taken from text rather than the JSON-LD integer.

HTML:
"""


def build_messages(url: str, html: str) -> list[dict[str, Any]]:
    prompt = _PROMPT.format(url=url)
    return common.render_messages(prompt, html)


def post_process(
    extraction: dict[str, Any],
    warnings: list[str],
) -> tuple[dict[str, Any], list[str]]:
    return extraction, warnings
