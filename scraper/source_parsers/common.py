"""Shared utilities for the LLM-driven per-source parsers.

Contains:
- The `record_listing` tool schema. Every per-source parser uses the
  same schema so the dispatcher can read fields uniformly. Fields
  follow the {value, confidence} envelope described in the seeded
  system prompt (app_settings.llm_parse_system_prompt).
- An HTML fetch helper with reasonable defaults.
- A truncation helper so we never exceed the LLM context window.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

LOG = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "sreality-tracker/0.2 (+https://github.com/waiff/sreality)"
)
DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs,en;q=0.7",
}

# 200_000 chars is a conservative cap. Sonnet 4.5 has 200K-token context
# but we want to leave headroom for the (long) system prompt + tool
# schema + output. ~200K chars is roughly 50K tokens of HTML — plenty
# for a single listing page after stripping nav/footer noise. Listings
# with bigger pages get truncated; the LLM is told the truncation is
# at the end and to use what's available.
HTML_CHAR_CAP = 200_000


def _field(value_type: str | list[str], description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "value": {"type": value_type, "description": description},
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
        },
        "required": ["value", "confidence"],
    }


# Field semantics mirror the seeded system prompt verbatim. Keep this
# in sync with app_settings.llm_parse_system_prompt; if you change the
# enums or field set, edit the seed via the operator's Settings UI
# (which writes a history row), not by re-seeding through a migration.
RECORD_LISTING_TOOL: dict[str, Any] = {
    "name": "record_listing",
    "description": (
        "Record the structured listing data extracted from the page. "
        "Call exactly once. Every field uses the {value, confidence} "
        "envelope; use null for value when the page does not state it."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "area_m2": _field(["number", "null"],
                "Useful area in m² (užitná plocha preferred over celková)."),
            "disposition": _field(["string", "null"],
                "Czech disposition: 1+kk, 1+1, 2+kk, ..., 6+1."),
            "price_czk": _field(["integer", "null"],
                "Headline price in CZK. Strip thousands separators. "
                "Null if not stated or not in CZK."),
            "price_unit": _field(["string", "null"],
                "'měsíc' for monthly rent; 'celkem' for total/sale."),
            "locality": _field(["string", "null"],
                "Most specific human-readable address suitable for geocoding."),
            "district": _field(["string", "null"],
                "City or city-district (e.g. 'Praha 2', 'Brno-střed')."),
            "category_main": _field(["string", "null"],
                "byt | dum | pozemek | komercni | ostatni"),
            "category_type": _field(["string", "null"],
                "prodej | pronajem | drazba"),
            "floor": _field(["integer", "null"],
                "Floor number, ground = 0, suterén = -1."),
            "total_floors": _field(["integer", "null"],
                "Total floors in the building."),
            "has_balcony": _field(["boolean", "null"],
                "True if balcony, loggia, or terrace mentioned."),
            "has_lift": _field(["boolean", "null"], "Elevator / výtah."),
            "has_parking": _field(["boolean", "null"],
                "Garage, parking lot, or parkovací stání."),
            "building_type": _field(["string", "null"],
                "cihla | panel | smisena | skelet | drevo | kamen | "
                "montovana | nizkoenergeticka."),
            "condition": _field(["string", "null"],
                "novostavba | po rekonstrukci | velmi dobrý stav | "
                "dobrý stav | před rekonstrukcí | ve výstavbě | k demolici."),
            "energy_rating": _field(["string", "null"],
                "Single capital letter A through G."),
            "description": _field(["string", "null"],
                "Seller's free-text description, verbatim, up to 8000 chars."),
            "warnings": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Notes about ambiguity, conflicts, or missing data. "
                    "Always present, may be empty."
                ),
            },
        },
        "required": [
            "area_m2", "disposition", "price_czk", "price_unit",
            "locality", "district", "category_main", "category_type",
            "floor", "total_floors", "has_balcony", "has_lift",
            "has_parking", "building_type", "condition",
            "energy_rating", "description", "warnings",
        ],
    },
}


def fetch_html(
    url: str,
    *,
    timeout_s: float = 30.0,
    session: requests.Session | None = None,
) -> str:
    """Fetch a single listing page. Raises requests.HTTPError on non-2xx."""
    sess = session or requests.Session()
    r = sess.get(url, headers=DEFAULT_HEADERS, timeout=timeout_s)
    r.raise_for_status()
    return r.text


def truncate_html(html: str, cap: int = HTML_CHAR_CAP) -> tuple[str, bool]:
    """Return (truncated_html, was_truncated)."""
    if len(html) <= cap:
        return html, False
    return html[:cap], True


def render_messages(prompt: str, html: str) -> list[dict[str, Any]]:
    """Wrap a per-source rendered prompt into the Anthropic messages format."""
    truncated, was_truncated = truncate_html(html)
    note = (
        "\n\n[NOTE: the HTML above was truncated to the first "
        f"{HTML_CHAR_CAP} chars; use what is available.]"
        if was_truncated else ""
    )
    return [{
        "role": "user",
        "content": prompt + truncated + note,
    }]
