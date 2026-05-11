"""Best-effort fallback parser for unsupported listing sources.

Used when classify_url returns 'unsupported'. We have no layout
knowledge, so the prompt asks the LLM to do its best and warn loudly
about anything inferred from prose. The dispatcher always tags these
runs with parse_confidence='best_effort' regardless of the per-field
confidences the LLM returns.
"""

from __future__ import annotations

from typing import Any

from scraper.source_parsers import common

_PROMPT = """\
Source URL: {url}
Source domain: UNKNOWN — we have no layout knowledge of this site.

Do your best. The HTML may be a Czech real-estate listing in any
layout. Look for:
- A spec table or definition list with Czech labels (Užitná plocha,
  Dispozice, Cena, Stav, Konstrukce, Patro).
- A JSON-LD <script type="application/ld+json"> block.
- Open Graph meta tags (og:title, og:description) for the headline.

Be conservative. Mark any field as "low" confidence if you inferred
it from prose rather than a labelled value. Add a warning naming the
unknown source and noting fields you could not extract reliably.

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
