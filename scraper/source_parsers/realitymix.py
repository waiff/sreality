"""realitymix.cz per-source parser (on-demand estimation-preview path)."""

from __future__ import annotations

from typing import Any

from scraper.source_parsers import common

_PROMPT = """\
Source URL: {url}
Source domain: realitymix.cz

Layout notes specific to realitymix.cz (a structured server-rendered page):
- Category: a <script type="application/ld+json"> @type "BreadcrumbList"
  carries the category path — position 2 is the family (Byty/Domy/Pozemky/
  Chaty/Komerce/Ostatní), position 3 the offer (Prodej/Pronájem). Use it for
  category_type (prodej = sale, pronajem = rent).
- Coordinates + address are already on the page in
  <div id="print-map" data-gps-lat="…" data-gps-lon="…"
  data-address="Street, Obec, okres Okres"> — use them directly (lat first).
  The first comma-segment of data-address is the street, the next the obec.
- The spec section is a list of
  <li class="detail-information__data-item"><span>Label:</span>
  <span>Value</span></li> rows. Czech labels:
  "Dispozice bytu"/"Dispozice", "Celková podlahová plocha"/"Užitná plocha"
  (the usable/floor area in m²), "Číslo podlaží v domě" (floor, "přízemí" → 0),
  "Druh objektu" (construction), "Stav objektu", "Vlastnictví"
  ("osobní" → osobni, "družstevní" → druzstevni), "Energetická náročnost budovy".
- Building type values: "Cihlová" → cihla, "Panelová" → panel, "Smíšená" →
  smisena, "Dřevěná" → drevo. Lowercase, no diacritics on the enum.
- Price is in <tr class="advert-description__short-props-price"> after "Cena:".
  MANY listings show "Cena na vyžádání", "Rezervováno" or "info v RK" → no
  numeric price; report price null (do NOT invent one). Strip "Kč" and the
  thousands separators (NBSP \\u00A0) from a numeric price.

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
