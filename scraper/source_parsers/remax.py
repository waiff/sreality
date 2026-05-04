"""remax-czech.cz per-source parser."""

from __future__ import annotations

from typing import Any

from scraper.source_parsers import common

_PROMPT = """\
Source URL: {url}
Source domain: remax-czech.cz

Layout notes specific to remax-czech.cz:
- The spec section uses a definition list (<dl>) under a "Detaily
  nemovitosti" / "Parameters" header. Czech labels.
- Price appears as "Cena" with currency suffix; strip "Kč" and any
  thousands separators (NBSP characters are common — \\u00A0).
- Most Remax listings are sales (category_type = "prodej") but
  pronajem listings exist too — verify against the page rather
  than defaulting.
- Address has a city + district format like "Praha 4 - Krč".
- Floor values like "Přízemí" → 0, "1. patro" → 1, "Suterén" → -1.
- Building type values: "Cihlová" → cihla, "Panelová" → panel,
  "Smíšená" → smisena, etc. Lowercase, no diacritics on the
  category enum.
- A JSON-LD <script type="application/ld+json"> block sometimes
  carries the canonical schema.org/Place data — prefer it for
  price, locality, and lat/lng when present (lat/lng is the only
  source where coordinates may already be on the page).

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
