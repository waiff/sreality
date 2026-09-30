"""The plain-text location reading: what the text lane asks for, and the one reader of it.

The text lane (`toolkit/description_extraction.py`) asks for this block in the SAME call that
reads the attribute fields, and stores the answer RAW under `location` in
`listing_description_enrichments.extracted`. Nothing here writes. `read_location` applies only
the checks that need nothing but the advert itself — V1 (the quote is verbatim in it) and V3
(the advert offers a property); whether a value names a real place is the register's question,
asked by the claim reader (W3).
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

AD_KINDS = ("offer", "exchange", "wanted", "not_property")
ADMITTED_AD_KINDS = frozenset({"offer", "exchange"})

_SLOT_SPEC: dict[str, str] = {
    "town": "The municipality (obec) the PROPERTY is in, as the register spells it: nominative, "
            "keeping a qualifier such as 'Lhota u Příbramě'. A city district ('Praha 5', "
            "'Brno-Židenice') is the city ('Praha', 'Brno'); the district goes to part_of_town.",
    "part_of_town": "The část obce / městská část / čtvrť / sídliště the property is in, "
                    "nominative.",
    "street": "The property's own street, its official nominative name, without 'ul.'/'ulice' "
              "but with a type word that is part of the name (náměstí Míru, třída Kpt. Jaroše, "
              "nábřeží …, sídliště …).",
    "house_number_cp": "Digits only: the číslo popisné ('č.p.', 'čp', 'číslo popisné'), or 123 "
                       "in the form 'Street 123/4'.",
    "house_number_co": "Digits only: the číslo orientační ('č.o.', 'orientační'), or 4 in the "
                       "form 'Street 123/4'.",
    "house_number_ev": "Digits only: the číslo evidenční of the BUILDING ('chata č.ev. 13'). "
                       "'Evidenční číslo: 928457' or 'ev.č.: 7630' is the broker's reference -> null.",
}
SLOTS = tuple(_SLOT_SPEC)
_AD_KIND = ("offer: sells or lets the property described (also 'hledáme nájemníka'); exchange: "
            "offers their own for another; wanted: seeks one; not_property: no real estate at a fixed "
            "place (a prefab or mobile garage, a mobile home, a container, materials, a service). "
            "Always set, even with no quote.")

LOCATION_PROMPT = (
    "`location` records where the advertised property ITSELF is — never a place near it (a "
    "tram stop, a park, a shop, '500 m od ulice X', 'pod ulicí X', a town given by distance: "
    "'10 km od Brna', 'nedaleko Kolína'; 'Lhota u Příbramě' is a name, not a distance).\n"
    "Never a desired location: for an exchange advert record ONLY the property offered, never "
    "the one wanted; for a wanted advert leave every location slot null.\n"
    "Never a broker's office address, never a secondary street (a rear entrance, a corner "
    "'roh ulic X a Y' -> null), never a project or building name.\n"
    "A house number needs the advert's own marker or the form 'Street 123/4' — never a bare "
    "number ('Street 12', 'č. 12', 'Praha 8'), never a parcel, LV, k.ú., unit or reference "
    "number, never a negated one ('bez č.p.').\n"
    "A property outside the Czech Republic, or an advert not written in Czech (e.g. Slovak): "
    "every location slot null.\n"
    "Every location quote obeys the same rule: a VERBATIM span of the advert text, or null."
)


def location_block(cell: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """The tool-schema property; `cell` is the lane's own `{value, evidence_quote}` wrapper."""
    slots = {"ad_kind": cell({"type": "string", "enum": list(AD_KINDS), "description": _AD_KIND})}
    for slot, description in _SLOT_SPEC.items():
        slots[slot] = cell({"type": ["string", "null"], "description": description})
    return {"type": "object", "additionalProperties": False,
            "description": "Where the advertised property itself is.",
            "properties": slots, "required": list(slots)}


def read_location(payload: Mapping[str, Any] | None,
                  advert_text: str) -> dict[str, dict[str, Any]]:
    """`slot -> {value, quote}` for `ad_kind` and every slot; a slot that fails V1 or V3 is
    null."""
    # Lazy: toolkit imports this module, and the quote check must stay the lane's ONE check.
    from toolkit.description_extraction import quote_supports

    block = (payload or {}).get("location")
    block = block if isinstance(block, Mapping) else {}
    kind, quote = _cell(block.get("ad_kind"))
    kind = kind if kind in AD_KINDS else None
    out = {"ad_kind": {"value": kind, "quote": quote if kind else None}}
    for slot in SLOTS:
        value, quote = _cell(block.get(slot))
        ok = (kind in ADMITTED_AD_KINDS and isinstance(value, str) and value.strip()
              and isinstance(quote, str) and quote_supports(advert_text, quote))
        out[slot] = ({"value": value.strip(), "quote": quote} if ok
                     else {"value": None, "quote": None})
    return out


def _cell(raw: Any) -> tuple[Any, Any]:
    raw = raw if isinstance(raw, Mapping) else {}
    return raw.get("value"), raw.get("evidence_quote")
