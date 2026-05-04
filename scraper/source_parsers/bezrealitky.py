"""Bezrealitky.cz per-source parser."""

from __future__ import annotations

from typing import Any

from scraper.source_parsers import common

_PROMPT = """\
Source URL: {url}
Source domain: bezrealitky.cz

Layout notes specific to bezrealitky:
- The spec table uses <ul> / <li> with class names like "ParamsList"
  and "param" — keys in <span class="paramName">, values in
  <span class="paramValue"> or similar. Czech labels.
- Rent figures may be split: "Cena nájmu" (base rent) versus "Poplatky"
  / "Energie" (fees). The price_czk you return is the BASE rent unless
  only a "Včetně poplatků" (inclusive) total is given — in that case
  use the total and add a warning naming the fees inclusion.
- Many bezrealitky pages embed a JSON-LD <script type="application/ld+json">
  block with the canonical price and address. When present, prefer it
  for price_czk and locality.
- "Dispoziční řešení" is the disposition. The listing title also
  usually contains it.
- "Stav nemovitosti" is the condition. "Konstrukce" or "Typ stavby"
  is the building_type.
- Bezrealitky lists category_type as "Pronájem" / "Prodej" — map to
  "pronajem" / "prodej".

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
