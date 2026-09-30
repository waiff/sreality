"""The plain-text location reading: what the text lane asks for, and the one reader of it.

The text lane (`toolkit/description_extraction.py`) asks for this block in the SAME call that
reads the attribute fields, and stores the answer RAW under `location` in
`listing_description_enrichments.extracted`. Nothing here writes. The claim lane hands a
listing's current reading to `read_claims` — the reader `text_reading`, the lane's third
substrate (W3) — which applies V1–V4 and builds claims through `_base`; whether a value names
a real place is the register's question, asked by the resolver.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from location_data.claims_common import Claim, Entry, ListingRow, _base

AD_KINDS = ("offer", "exchange", "wanted", "not_property")
ADMITTED_AD_KINDS = frozenset({"offer", "exchange"})
# The register is Czech: a reading that places the property abroad admits no slot. The pilot
# (run 36679622378) saw a Croatian apartment and a Slovak cottage answered with a town.
_COUNTRY = ("ISO 3166-1 alpha-2 of the country the property is in, from the text: 'CZ' when a "
            "Czech town, okres or kraj is named; 'HR' for 'Chorvátsko, ostrov Vir', 'SK' for "
            "'Slovensko'; null when nothing says. A quote is welcome, not required.")

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
    "A house number needs the advert's own marker, the form 'Street 123/4', or a VILLAGE name "
    "followed by its number ('Hodoviz 13', 'Smolná 28' -> č.p.); never a city district number "
    "('Praha 8', 'Brno 2'), never 'Street 12' or 'č. 12' alone, never a parcel, LV, k.ú., unit "
    "or reference number, never a negated one ('bez č.p.').\n"
    "A property outside the Czech Republic, or an advert not written in Czech (e.g. Slovak): "
    "set `country` and leave every other location slot null.\n"
    "Every location quote obeys the same rule: a VERBATIM span of the advert text, or null."
)


def location_block(cell: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """The tool-schema property; `cell` is the lane's own `{value, evidence_quote}` wrapper."""
    slots = {"ad_kind": cell({"type": "string", "enum": list(AD_KINDS), "description": _AD_KIND}),
             "country": cell({"type": ["string", "null"], "description": _COUNTRY})}
    for slot, description in _SLOT_SPEC.items():
        slots[slot] = cell({"type": ["string", "null"], "description": description})
    return {"type": "object", "additionalProperties": False,
            "description": "Where the advertised property itself is.",
            "properties": slots, "required": list(slots)}


def read_location(payload: Mapping[str, Any] | None, advert_text: str,
                  slots: Sequence[str] = SLOTS) -> dict[str, dict[str, Any]]:
    """`slot -> {value, quote}` for `ad_kind`, `country` and `slots`; a slot that fails V1
    (quote not in the text) or V3 (not an offered property, or one placed abroad) is null."""
    # Lazy: toolkit imports this module, and the quote check must stay the lane's ONE check.
    from toolkit.description_extraction import quote_supports

    block = (payload or {}).get("location")
    block = block if isinstance(block, Mapping) else {}
    kind, quote = _cell(block.get("ad_kind"))
    kind = kind if kind in AD_KINDS else None
    out = {"ad_kind": {"value": kind, "quote": quote if kind else None}}
    country, quote = _cell(block.get("country"))
    country = country.strip().upper() if isinstance(country, str) and country.strip() else None
    out["country"] = {"value": country, "quote": quote if country else None}
    admitted = kind in ADMITTED_AD_KINDS and country in (None, "CZ")
    for slot in slots:
        value, quote = _cell(block.get(slot))
        ok = (admitted and isinstance(value, str) and value.strip()
              and isinstance(quote, str) and quote_supports(advert_text, quote))
        out[slot] = ({"value": value.strip(), "quote": quote} if ok
                     else {"value": None, "quote": None})
    return out


def _cell(raw: Any) -> tuple[Any, Any]:
    raw = raw if isinstance(raw, Mapping) else {}
    return raw.get("value"), raw.get("evidence_quote")


@dataclass(frozen=True, slots=True)
class Reading:
    """A listing's CURRENT reading, as the claim lane selects it: the row id, the raw block
    (`{"location": …}`) and the advert text it was read from, composed in SQL."""
    id: int
    payload: Mapping[str, Any]
    advert_text: str


# A č.ev. rides the č.p. claim type MARKED, and resolver S1 types it (`normalize._EVIDENCNI`).
_CLAIM_VALUE = {"house_number_ev": "č.ev. {}"}
_NUMBER = {"house_number_cp": r"\d{1,6}", "house_number_co": r"\d{1,5}[a-z]?",
           "house_number_ev": r"\d{1,6}"}
# V4, on the lane's fold. A marker, or "Street N[/M]" (a word of two letters or more first,
# so "č. 12" is no street); a parcel, LV, k.ú., GPS or negation token anywhere drops it.
_MARKER = {"house_number_cp": r"c\.?\s*p\.?|cislo\s+popisne",
           "house_number_co": r"c\.?\s*o\.?|cislo\s+orientacni",
           "house_number_ev": r"c\.?\s*ev\.?|ev\.?\s*c\.?|cislo\s+evidencni|evidencni\s+cislo"}
_FORM = {"house_number_cp": r"[^\W\d_]{{2}}\.?\s+{n}(?:\s*/\s*\d+[a-z]?)?(?![\d/])",
         "house_number_co": r"[^\W\d_]{{2}}\.?\s+\d+\s*/\s*{n}(?![\da-z])"}
_REFUSED_NUMBER = re.compile(
    r"parc|\bp\.\s*c\.|\blv\b|vlastnictvi|\bk\.\s*u\.|katastr|gps|\bbez\b|\bnema\b|\bneni\b")


def read_claims(entry: Entry, row: ListingRow, reading: Reading) -> list[Claim]:
    """THE reader `text_reading`: the entry's `slot`, then each `fallback` slot, through V1
    and V3 (`read_location`), V2 for a name and V4 for a number. Pure: the reading is an input."""
    slots = [str(entry.locator["slot"])] + [
        str(alt["slot"]) for alt in entry.locator.get("fallback") or () if isinstance(alt, Mapping)]
    read = read_location(reading.payload, reading.advert_text, slots)
    for slot in slots:
        value, quote = read[slot]["value"], read[slot]["quote"]
        if value is None or not (_numbered(slot, value, quote) if slot in _NUMBER
                                 else _grounded(value, quote)):
            continue
        return [_base(entry, row, value_text=_CLAIM_VALUE.get(slot, "{}").format(value))]
    return []


def _fold(text: str) -> str:
    # Lazy for the same reason as `read_location`: the lane's fold, plus ů->o (Dvůr/Dvoře).
    from toolkit.description_extraction import _flat
    return _flat(text.replace("ů", "o").replace("Ů", "O"))


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", _fold(text))


def _grounded(value: str, quote: str) -> bool:
    """V2: a value word of 3+ letters shares its first three with a quote word (Brno/Brně,
    Plzeň/Plzni, Hora/Hoře); a name with no such word ("Aš") must stand in the quote whole."""
    said = _words(quote)
    named = _words(value)
    long = [w[:3] for w in named if len(w) >= 3]
    if not long:
        return bool(named) and all(w in said for w in named)
    return bool(set(long) & {w[:3] for w in said if len(w) >= 3})


def _numbered(slot: str, value: str, quote: str) -> bool:
    """V4: the number stands in its quote behind its marker or in the "Street N[/M]" form."""
    number, folded = value.strip().lower(), _fold(quote)
    if not re.fullmatch(_NUMBER[slot], number) or _REFUSED_NUMBER.search(folded):
        return False
    n = re.escape(number)
    marked = rf"\b(?:{_MARKER[slot]})\s*[:.]?\s*{n}(?![\d/])"
    form = _FORM.get(slot)
    return bool(re.search(marked, folded) or (form and re.search(form.format(n=n), folded)))
