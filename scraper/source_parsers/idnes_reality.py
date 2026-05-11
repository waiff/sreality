"""reality.idnes.cz per-source parser."""

from __future__ import annotations

from typing import Any

from scraper.source_parsers import common

_PROMPT = """\
Source URL: {url}
Source domain: reality.idnes.cz

Layout notes specific to reality.idnes.cz:
- The spec section uses <table class="list"> with paired <th>/<td>
  rows. Czech labels — "Užitná plocha", "Dispozice", "Patro",
  "Stav objektu", "Konstrukce", "Energetická náročnost".
- Price is in a header element, often <span class="price"> or in
  a banner labelled "Celková cena". Strip "Kč", "kč", whitespace.
- Some idnes listings show the price as "Info o ceně" or "Dohodou"
  (price on inquiry / by agreement). Return price_czk=null with a
  warning explaining the page hides the price.
- Address blocks live in <h2> headings or breadcrumb-style strings;
  district appears as "Praha-Vinohrady" / "Brno-Bohunice" patterns.
- category_type from breadcrumb / URL: "/prodej" → "prodej",
  "/pronajem" → "pronajem".
- "Patro" values like "1. NP" mean ground floor (floor=0), "2. NP" =
  floor 1, "1. PP" = floor -1.

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
