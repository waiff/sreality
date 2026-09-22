"""The UNIT IDENTITY CARD: one LLM extraction per LISTING, compared by plain code.

The engine's free regex readers keep losing to Czech prose. Four unseen-cohort confirmations
each found a form nobody had written a pattern for — unit codes (`Byt B1.2.1`, `byt s
označením B2.2.1`, `F2.103`, `5.13`, `Vila II`), floors written out (`2. NP` vs `3. NP`, `1.
patro`), printed areas contradicting a degenerate stored column (bazos stores the terrace as
`area_m2`), parcels of one parcelling, serviced-office tiers (`pro 1 osobu` vs `pro 2 pracovní
místa`), offered extent (`pokoj` vs `2 spojené pokoje`), accessory numbers (`stání č. 47` vs
`32`). Writing the next regex has lost four times running.

The hypothesis here is NOT the judge's. An LLM asked "same or different?" per PAIR was measured
and refused — it says `same` across development twins, and the cost is O(pairs). This asks the
model the one thing it is reliably good at: EXTRACTION. One call per LISTING (O(listings),
cacheable for ever on listing + content hash + prompt version) fills a small structured card
of what the advert PRINTS, every field null when not stated and never guessed. Two cards are
then compared field by field by `card_conflicts` — plain code, no model. The LLM never decides
a merge; it only reads. A conflict needs BOTH sides non-null, which is the same
absence-is-not-disagreement rule the whole engine runs on.

E28: the text sent is scrubbed by `autodedup.judge.scrub_for_prompt` — ONE scrubber, the
judge's, not a second copy of the PII rules.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

from autodedup.judge import scrub_for_prompt

TOOL_NAME: str = "record_unit_identity_card"

# The judge caps the description at 1,200 characters because it pays that token budget per
# PAIR, on every pair. A card is paid ONCE per listing and cached for ever, and the fact that
# tells two developer units apart is as often in the last paragraph as the first — the exact
# reason `judge.scrubbed_text` exists uncapped. So the cap here is a runaway guard, not a
# budget: generous enough that essentially no advert is cut.
TEXT_MAX_CHARS: int = 6000

# Two printed areas of one unit differ by rounding and by which plocha a portal quotes.
# 2% is the independent-signature probe's own threshold (confirm_region), chosen there
# because it separated real unit-vs-unit fusions from rounding without firing on either.
AREA_TOL: float = 0.02

# Fields whose disagreement is a STATED, NUMBERED fact about the unit itself — a designator,
# a count, a measured quantity. A conflict here is what the operator means by "a
# distinguishing fact", and it is the tier a merge decision could one day be built on.
STRONG_FIELDS: tuple[str, ...] = (
    "unit_code",
    "parcel_numbers",
    "plot_number",
    "floor",
    "area_m2",
    "capacity_persons",
    "rooms_offered",
)

# Disagreement here is real but softer: a NAME the two portals spell differently, or a fact
# about the BUILDING rather than the unit. Measured on the fc1 cards, the weak tier is where
# the model's variance lives — `locality` alone split 69 of 461 certain duplicates on nothing
# but Czech inflection. Reported separately so each field can be priced before anything is
# promoted; `street`, `house_number`, `accessories` and `other_areas` sat in the strong tier
# in the first pass and were moved here when the numbers came back.
WEAK_FIELDS: tuple[str, ...] = (
    "street",
    "house_number",
    "locality",
    "project_name",
    "building_block",
    "total_floors",
    "disposition",
    "orientation",
    "accessories",
    "other_areas",
)

CONFLICT_FIELDS: tuple[str, ...] = STRONG_FIELDS + WEAK_FIELDS

# A menu advert ("tři samostatné parcely", "nabízíme byty 2+kk až 4+kk", plots 7/8/9) states
# MANY units' facts at once, so its unit-level values do not belong to one unit and cannot
# contradict a single-unit advert. Place-level facts still can: a menu of three parcels in
# Karlovice still is not a parcel in Veselá.
MENU_SUPPRESSED: frozenset[str] = frozenset({
    "unit_code", "floor", "area_m2", "rooms_offered", "capacity_persons",
    "accessories", "other_areas", "disposition", "orientation", "plot_number",
    "total_floors",
})

_ACCENTS = re.compile(r"[̀-ͯ]")
_CODE_SEP = re.compile(r"[\s._\-/]+")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def fold(text: str) -> str:
    """Deaccented lowercase — `text_facts.fold`'s rule, applied to card values."""
    return _ACCENTS.sub("", unicodedata.normalize("NFKD", text)).lower()


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    out = str(value).strip()
    return out or None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def _whole(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return int(round(number))


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "ano"}
    return bool(value)


def _strings(value: Any) -> list[str]:
    if value is None or isinstance(value, (str, bytes)):
        return [text] if (text := _text(value)) else []
    if isinstance(value, dict):
        value = list(value.values())
    out: list[str] = []
    for entry in value if isinstance(value, Iterable) else ():
        text = _text(entry)
        if text and text not in out:
            out.append(text)
    return out


def _labelled_numbers(value: Any, label_key: str, value_key: str) -> dict[str, float]:
    """`[{"label": "terasa", "m2": 7.9}]` -> `{"terasa": 7.9}`, folded, first wins.

    Also accepts the already-folded map, so a card survives a `to_json` / `from_json` round
    trip — the artifact is re-read by the analysis and must come back the same card.
    """
    out: dict[str, float] = {}
    if isinstance(value, dict):
        for label, raw in value.items():
            text, number = _text(label), _number(raw)
            if text is not None and number is not None:
                out.setdefault(fold(text), number)
        return out
    for entry in value if isinstance(value, (list, tuple)) else ():
        if not isinstance(entry, dict):
            continue
        label = _text(entry.get(label_key))
        number = _number(entry.get(value_key))
        if label is None or number is None:
            continue
        key = fold(label)
        out.setdefault(key, number)
    return out


def _labelled_strings(value: Any, label_key: str, value_key: str) -> dict[str, list[str]]:
    """`[{"kind": "stání", "designator": "47"}]` -> `{"stani": ["47"]}`, or the folded map."""
    out: dict[str, list[str]] = {}
    if isinstance(value, dict):
        for label, raw in value.items():
            text = _text(label)
            designators = _strings(raw)
            if text is not None and designators:
                out.setdefault(fold(text), []).extend(designators)
        return out
    for entry in value if isinstance(value, (list, tuple)) else ():
        if not isinstance(entry, dict):
            continue
        label = _text(entry.get(label_key))
        designator = _text(entry.get(value_key))
        if label is None or designator is None:
            continue
        out.setdefault(fold(label), []).append(designator)
    return out


def _evidence(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): text for key, raw in value.items() if (text := _text(raw)) is not None
    }


@dataclass(frozen=True)
class UnitCard:
    """What ONE advert PRINTS about the unit it offers. Every field null when not stated."""

    listing_id: int
    unit_code: str | None = None
    floor_raw: str | None = None
    floor: int | None = None
    total_floors: int | None = None
    area_m2: float | None = None
    other_areas: dict[str, float] = field(default_factory=dict)
    parcel_numbers: list[str] = field(default_factory=list)
    plot_number: str | None = None
    street: str | None = None
    house_number: str | None = None
    locality: str | None = None
    project_name: str | None = None
    building_block: str | None = None
    disposition: str | None = None
    rooms_offered: int | None = None
    capacity_persons: int | None = None
    accessories: dict[str, list[str]] = field(default_factory=dict)
    orientation: str | None = None
    is_menu: bool = False
    menu_units: list[str] = field(default_factory=list)
    is_new_development: bool = False
    evidence: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "listing_id": self.listing_id,
            "unit_code": self.unit_code,
            "floor_raw": self.floor_raw,
            "floor": self.floor,
            "total_floors": self.total_floors,
            "area_m2": self.area_m2,
            "other_areas": dict(self.other_areas),
            "parcel_numbers": list(self.parcel_numbers),
            "plot_number": self.plot_number,
            "street": self.street,
            "house_number": self.house_number,
            "locality": self.locality,
            "project_name": self.project_name,
            "building_block": self.building_block,
            "disposition": self.disposition,
            "rooms_offered": self.rooms_offered,
            "capacity_persons": self.capacity_persons,
            "accessories": {key: list(value) for key, value in self.accessories.items()},
            "orientation": self.orientation,
            "is_menu": self.is_menu,
            "menu_units": list(self.menu_units),
            "is_new_development": self.is_new_development,
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any], listing_id: int | None = None) -> "UnitCard":
        return cls(
            listing_id=int(payload.get("listing_id") or listing_id or 0),
            unit_code=_text(payload.get("unit_code")),
            floor_raw=_text(payload.get("floor_raw")),
            floor=_whole(payload.get("floor")),
            total_floors=_whole(payload.get("total_floors")),
            area_m2=_number(payload.get("area_m2")),
            other_areas=_labelled_numbers(payload.get("other_areas"), "label", "m2"),
            parcel_numbers=_strings(payload.get("parcel_numbers")),
            plot_number=_text(payload.get("plot_number")),
            street=_text(payload.get("street")),
            house_number=_text(payload.get("house_number")),
            locality=_text(payload.get("locality")),
            project_name=_text(payload.get("project_name")),
            building_block=_text(payload.get("building_block")),
            disposition=_text(payload.get("disposition")),
            rooms_offered=_whole(payload.get("rooms_offered")),
            capacity_persons=_whole(payload.get("capacity_persons")),
            accessories=_labelled_strings(payload.get("accessories"), "kind", "designator"),
            orientation=_text(payload.get("orientation")),
            is_menu=_flag(payload.get("is_menu")),
            menu_units=_strings(payload.get("menu_units")),
            is_new_development=_flag(payload.get("is_new_development")),
            evidence=_evidence(payload.get("evidence")),
        )

    def filled(self) -> tuple[str, ...]:
        """Which comparable fields this card actually carries — the fill-rate numerator."""
        return tuple(name for name in CONFLICT_FIELDS if _value_of(self, name) is not None)


def parse_card(payload: Any, listing_id: int) -> UnitCard:
    """The model's tool input -> a card. A non-dict input is a refusal to answer, not a card."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise CardParseError(f"tool input is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CardParseError(f"tool input is {type(payload).__name__}, expected an object")
    return UnitCard.from_json(payload, listing_id)


class CardParseError(ValueError):
    """The model's tool call did not satisfy the card contract."""


# --- comparison --------------------------------------------------------------------------


def _value_of(card: UnitCard, name: str) -> Any:
    value = getattr(card, name, None)
    if isinstance(value, (list, dict)):
        return value or None
    return value


# --- value hygiene -----------------------------------------------------------------------
#
# Measured on the first real run (gpt-5-nano, prompt fc1, 1,276 cards): 51% of 461
# certain-duplicate pairs carried a "conflict", and almost none of it was the model finding a
# real difference. It was the model writing things a value slot cannot hold —
#
#   * the literal STRING "null" (233 unit_codes, 71 floor_raws) — which is a stated value to
#     any comparison that only checks `is not None`, hence "unit_code: garáž vs null";
#   * a GUESS, marked as one: "1+kk?", "B1.2.?", "Botič II?", "3+kk? no";
#   * its own commentary: "Plzeň - Skvrny? text says Plzeň Skvrňany. The locality should be…";
#   * a DISPOSITION or an object word where a designator belongs: "3+1", "2+kk", "garáž",
#     "Pozemek", "parcela s chatou?".
#
# Three rails catch all four shapes before any comparison sees them, and they are rails a
# prompt cannot be trusted to enforce: a question mark, a prose-length value, or the word
# "null" means the model did not read a value, so the field is null.

VALUE_MAX_CHARS: int = 40

_NULL_WORDS = re.compile(
    r"(?:^|\b)(?:null|none|nan|n/a|na/|not\s+(?:provided|stated|specified|given)|unknown|"
    r"neuvedeno|nezadano|neni\s+uvedeno)(?:\b|$)"
)


def clean_value(raw: Any) -> str | None:
    """A string field as the comparison may use it, or None when the model did not read one."""
    value = _text(raw)
    if value is None:
        return None
    if "?" in value or len(value) > VALUE_MAX_CHARS:
        return None
    if _NULL_WORDS.search(fold(value)):
        return None
    return value


# --- places: deaccented, de-prepositioned, and Czech-inflection tolerant -----------------
#
# "Plzeň" and "Plzni", "Plzeň - Skvrňany" and "Plzeň Skvrňany", "ulici Univerzitní" and
# "Univerzitní ulice", "Touškov" and "Město Touškov" are each ONE place printed twice. Raw
# string comparison called every one of them a distinguishing fact.

_PLACE_STOPWORDS: frozenset[str] = frozenset({
    "ulice", "ulici", "ulicce", "ulic", "ul", "v", "ve", "na", "mesto", "mesta", "meste",
    "obec", "obce", "obci", "cast", "casti", "ctvrt", "ctvrti", "okres", "okresu", "k",
    "kat", "uzemi", "kraj", "kraji",
})

_VOWELS: frozenset[str] = frozenset("aeiouy")

# A stem must keep three quarters of its word and at least four characters. Both halves
# matter: without the proportion "Olomouc" stems to "olom" and swallows "Olomučany"; without
# the floor a three-letter town matches everything.
STEM_MIN: int = 4
STEM_KEEP: float = 0.75


def place_tokens(value: Any) -> tuple[str, ...]:
    """`"Plzeň – Skvrňany"` -> `('plzen', 'skvrnany')`; stop-words and punctuation gone."""
    text = clean_value(value)
    if text is None:
        return ()
    return tuple(
        token for token in _NON_ALNUM.sub(" ", fold(text)).split()
        if token and token not in _PLACE_STOPWORDS
    )


def _stems(word: str) -> set[str]:
    """The word and the stems a Czech case ending could have been added to."""
    out = {word}
    shortest = max(STEM_MIN, -(-len(word) * 3 // 4))
    for cut in range(len(word) - 1, shortest - 1, -1):
        out.add(word[:cut])
    # Plzeň -> Plzni elides the stem's last vowel, so no suffix strip reaches the shared stem.
    if len(word) >= 4 and word[-1] not in _VOWELS and word[-2] in _VOWELS:
        out.add(word[:-2] + word[-1])
    return {stem for stem in out if len(stem) >= STEM_MIN}


def tokens_agree(left: str, right: str, stem_tolerance: bool = True) -> bool:
    """One place name in two inflections is not two places."""
    if left == right:
        return True
    if not stem_tolerance:
        return False
    for one in _stems(left):
        for other in _stems(right):
            if one.startswith(other) or other.startswith(one):
                return True
    return False


def places_agree(
    left: tuple[str, ...], right: tuple[str, ...], stem_tolerance: bool = True
) -> bool:
    """Do two printed places name one place? Absence agrees; a finer name agrees with a coarser.

    Set containment, not position: "Bohdalovice, Velké Hamry" and "Velké Hamry" are one place
    named twice, and "Plzeň" beside "Plzeň - Skvrňany" is the same place said coarsely — which
    is exactly the shape a false split would be built from. Two DIFFERENT districts of one
    city ("Plzeň - Skvrňany" vs "Plzeň - Slovany") still conflict, because neither token set
    is contained in the other.
    """
    if not left or not right:
        return True
    if set(left) == set(right):
        return True
    return _contains(left, right, stem_tolerance) or _contains(right, left, stem_tolerance)


def _contains(small: tuple[str, ...], big: tuple[str, ...], stem_tolerance: bool) -> bool:
    return all(
        any(tokens_agree(one, other, stem_tolerance) for other in big) for one in small
    )


# --- floors: an integer, not a string ----------------------------------------------------
#
# "7. patro" beside "7. patře" and "6. nadzemní podlaží" beside "6. nadzemním podlaží" are
# the same floor declined twice. The model's own `floor` integer got them wrong; the printed
# string is the reliable half, so it is parsed here and the model's integer is only the
# fallback. The raw string survives as EVIDENCE.

_FLOOR_WORDS: dict[str, int] = {
    "prvni": 1, "prvnim": 1, "druhe": 2, "druhem": 2, "druhy": 2, "druhym": 2,
    "treti": 3, "tretim": 3, "ctvrte": 4, "ctvrtem": 4, "ctvrty": 4, "ctvrtym": 4,
    "pate": 5, "patem": 5, "paty": 5, "patym": 5, "seste": 6, "sestem": 6,
    "sedme": 7, "sedmem": 7, "osme": 8, "osmem": 8, "devate": 9, "devatem": 9,
    "desate": 10, "desatem": 10,
}
_ROMAN_FLOOR: dict[str, int] = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
    "ix": 9, "x": 10,
}
_ARABIC = re.compile(r"-?\d+")
_ROMAN_PRINTED = re.compile(r"\b(?:I{1,3}|IV|VI{0,3}|IX|X|V)\b")
_ABOVE_GROUND = re.compile(r"\bnp\b|\dnp\b|nadzemn")
_BELOW_GROUND = re.compile(r"\bpp\b|\dpp\b|podzemn")
_GROUND_WORDS = re.compile(r"prizem|parter")
_CELLAR_WORDS = re.compile(r"suteren|sklep|podkladn")


def parse_floor(raw: Any) -> int | None:
    """The printed floor as an integer with GROUND = 0, or None when it is not a floor.

    Czech portals count two ways and print both: `1. NP` / `přízemí` / `parter` are 0, `1.
    patro` is 1. A maisonette gives its lowest floor (the first number printed).
    """
    text = clean_value(raw)
    if text is None:
        return None
    folded = fold(text)
    number: int | None = None
    if match := _ARABIC.search(folded):
        number = int(match.group())
    elif match := _ROMAN_PRINTED.search(text):
        # Only an UPPERCASE numeral: `v` is the preposition "in" far more often than five.
        number = _ROMAN_FLOOR[match.group().lower()]
    else:
        for token in _NON_ALNUM.sub(" ", folded).split():
            if token in _FLOOR_WORDS:
                number = _FLOOR_WORDS[token]
                break
    if _ABOVE_GROUND.search(folded):
        return (number - 1) if number is not None else 0
    if _BELOW_GROUND.search(folded):
        return -abs(number) if number else -1
    if "patr" in folded or "poschod" in folded or "podlaz" in folded:
        return number
    if _GROUND_WORDS.search(folded):
        return 0
    if _CELLAR_WORDS.search(folded):
        return -1
    return None


# --- dispositions: the canonical grammar, or nothing -------------------------------------

_DISPOSITION = re.compile(r"(\d)\s*\+\s*(kk|1)\b|(\d)\s*(kk)\b")


def canonical_disposition(raw: Any) -> str | None:
    """`"3+1 (přízemí, 1. patro, podkroví)"` -> `"3+1"`; `"2+1 (3kk)"` -> None.

    `N+kk` or `N+1` and nothing else. A layout the grammar does not cover ("garsoniéra",
    "atypický", "4 pokoje") is not a fact two cards can be compared on, and an advert printing
    TWO layouts ("1kk a 2kk", "4+1 a 3+1 v domě") states neither about one unit.
    """
    text = clean_value(raw)
    if text is None:
        return None
    found = {
        f"{match[0] or match[2]}+{match[1] or match[3]}"
        for match in _DISPOSITION.findall(fold(text))
    }
    return found.pop() if len(found) == 1 else None


# --- designators: a printed code, never a layout and never an object ---------------------

_CODE_NOUNS: frozenset[str] = frozenset({
    "byt", "bytu", "bytem", "byty", "bytova", "bytovy", "bytove", "bytoveho", "bytovem",
    "jednotka", "jednotky", "jednotce", "jednotku", "apartman", "apartmanu", "apartmany",
    "vila", "vily", "vile", "dum", "domu", "dome", "domy", "budova", "budovy", "budove",
    "objekt", "objektu", "vchod", "vchodu", "sekce", "sekci", "blok", "bloku", "etapa",
    "etapy", "garaz", "garaze", "garazi", "garazovy", "garazove", "garazoveho", "stani",
    "koje", "sklep", "sklepni", "pozemek", "pozemku", "parcela", "parcely", "parcele",
    "chata", "chaty", "kancelar", "kancelare", "atelier", "prostor", "prostory", "rodinny",
    "novostavba", "novostavby", "s", "se", "v", "ve", "na", "oznacenim", "oznaceni",
    "cislo", "cislem", "cislu", "c", "cis", "no", "nr", "id", "lamela",
})
_ROMAN_CODE = re.compile(r"^(?:i{1,3}|iv|vi{0,3}|ix|x|v)$")
_DESIGNATOR = re.compile(r"^[a-z]{0,3}\d+[a-z]?(?:[./-]\d+[a-z]?)*$")
_ANY_DISPOSITION = re.compile(r"\d\s*\+\s*(?:kk|\d)|\d\s*kk\b")


def designator(raw: Any) -> str | None:
    """The printed code of ONE unit, folded for comparison, or None when this is not a code.

    fc1's `unit_code` came back holding a disposition 149 times ("3+1", "2+kk"), an object
    word ("garáž", "Pozemek"), a street name ("Dřevařská"), a guess ("B1.2.?") or the string
    "null" — and `unit_code` was the single biggest source of false splits (95 pairs split on
    it alone). A designator is letters and digits with separators, optionally behind its noun
    ("Byt B1.2.1", "byt s označením B2.2.1"), or a bare block letter or Roman numeral
    ("jednotka A", "Vila II"). Everything else is not a designator.
    """
    value = clean_value(raw)
    if value is None:
        return None
    folded = fold(value)
    if _ANY_DISPOSITION.search(folded):
        return None
    tokens = [token for token in folded.split() if _NON_ALNUM.sub("", token)]
    kept = [
        token for token in tokens if _NON_ALNUM.sub("", token) not in _CODE_NOUNS
    ]
    # `č.` is a noun beside a code and a code on its own ("jednotka C"), so a value that is
    # ONLY nouns is re-read as itself rather than as nothing.
    if not kept:
        kept = tokens
    if len(kept) != 1:
        return None
    token = kept[0].strip("./-,;:")
    if _DESIGNATOR.match(token) or _ROMAN_CODE.match(token) or re.fullmatch(r"[a-z]", token):
        return token
    return None


def _alnum(raw: Any) -> str | None:
    """A number-ish value reduced to its characters — `"č.p. 467"` -> `"cp467"`."""
    value = clean_value(raw)
    if value is None:
        return None
    return _NON_ALNUM.sub("", fold(value)) or None


_HOUSE_PART = re.compile(r"\d+[a-z]?")


def _house_parts(raw: Any) -> tuple[str, ...] | None:
    """`"2842/1"` -> `('2842', '1')`. Two house numbers agree when the parts they SHARE agree.

    A portal prints `č.p. 2842` where another prints `2842/1` (č.p. / č.or.) — the same
    house said with one part missing, not two houses.
    """
    value = clean_value(raw)
    if value is None:
        return None
    parts = tuple(
        match.group() for raw_part in fold(value).split("/")
        if (match := _HOUSE_PART.search(raw_part))
    )
    return parts or None


def _houses_conflict(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return any(one != other for one, other in zip(left, right))


# --- areas -------------------------------------------------------------------------------

# Two portals print one flat's area as 74.5 and 75. Below half a square metre, or across one
# rounding boundary, there is no stated difference to act on.
AREA_ABS_TOL: float = 0.5

# No unit's HEADLINE area is two square metres. A number that small is the cellar, the lodge
# or a misread, so it is not the field's value — it is a null. (The gap is the point: the
# rejected reading is what produced "area_m2: 50.0 vs 2.0" on a confirmed duplicate.)
AREA_MIN_M2: float = 5.0


def _areas_conflict(left: float, right: float, tol: float) -> bool:
    """2% of the larger area, or half a square metre, whichever is more forgiving.

    A third clause — "and they round to the same whole metre" — was measured on the fc1 cards
    and removed: it bought ZERO false splits and cost one confirmed fused group and one
    confirmed DIFFERENT pair (16.8 m² beside 17.4 m²). The absolute floor already does
    everything rounding tolerance was meant to do.
    """
    return _relative_gap(left, right) > tol and abs(left - right) > AREA_ABS_TOL


# --- orientation -------------------------------------------------------------------------

_COMPASS: dict[str, str] = {
    "jih": "j", "jizni": "j", "jizne": "j", "jiz": "j", "j": "j",
    "sever": "s", "severni": "s", "severne": "s", "s": "s",
    "vychod": "v", "vychodni": "v", "vychodne": "v", "v": "v",
    "zapad": "z", "zapadni": "z", "zapadne": "z", "z": "z",
    "jihozapad": "jz", "jihozapadni": "jz", "jihozapadne": "jz", "jz": "jz",
    "jihovychod": "jv", "jihovychodni": "jv", "jihovychodne": "jv", "jv": "jv",
    "severozapad": "sz", "severozapadni": "sz", "severozapadne": "sz", "sz": "sz",
    "severovychod": "sv", "severovychodni": "sv", "severovychodne": "sv", "sv": "sv",
}
_ORIENTATION_NOISE: frozenset[str] = frozenset({"orientace", "orientovany", "orientovana",
                                                 "na", "do", "k", "ke", "strana", "stranu"})


def canonical_orientation(raw: Any) -> str | None:
    """`"jiho-západ"` / `"JZ"` / `"jihozápad"` -> `"jz"`; `"sever jih"` -> None (two answers)."""
    value = clean_value(raw)
    if value is None:
        return None
    tokens = [
        token for token in _NON_ALNUM.sub(" ", fold(value)).split()
        if token not in _ORIENTATION_NOISE
    ]
    joined = "".join(tokens)
    if joined in _COMPASS:
        return _COMPASS[joined]
    found = {_COMPASS[token] for token in tokens if token in _COMPASS}
    return found.pop() if len(found) == 1 else None


# --- the normalised card -----------------------------------------------------------------


def normalise(card: UnitCard) -> UnitCard:
    """The card as the COMPARISON sees it: every field a typed, hygienic, comparable value.

    Returned as a `UnitCard` so one shape carries both halves of the experiment, but it is
    NOT the model's answer: string fields hold folded comparison keys, `unit_code` holds a
    designator or nothing, `floor` is parsed from the printed string, and anything the model
    guessed or commented on is gone. The printed card stays untouched — it is the record of
    what the model said, and it is what a conflict is REPORTED with.
    """
    return UnitCard(
        listing_id=card.listing_id,
        unit_code=designator(card.unit_code),
        floor_raw=clean_value(card.floor_raw),
        # The PRINTED floor decides, and the model's own integer is not a fallback: it
        # disagreed with the string beside it on 232 of the 458 fc1 cards that carried both
        # ("3. patro" -> 1, "4.NP" -> 1), which is where the floor false splits came from.
        # A floor no advert printed is a floor this comparison does not have.
        floor=parse_floor(card.floor_raw),
        total_floors=card.total_floors if (card.total_floors or 0) > 0 else None,
        area_m2=card.area_m2 if (card.area_m2 or 0) >= AREA_MIN_M2 else None,
        other_areas={
            fold(label): value for label, value in card.other_areas.items()
            if clean_value(label) is not None and value > 0
        },
        parcel_numbers=[
            key for value in card.parcel_numbers if (key := _alnum(value)) is not None
        ],
        plot_number=_alnum(card.plot_number),
        street=" ".join(place_tokens(card.street)) or None,
        house_number="/".join(_house_parts(card.house_number) or ()) or None,
        locality=" ".join(place_tokens(card.locality)) or None,
        project_name=" ".join(place_tokens(card.project_name)) or None,
        building_block=designator(card.building_block),
        disposition=canonical_disposition(card.disposition),
        # A count of zero rooms or zero persons is not a count the advert offered.
        rooms_offered=card.rooms_offered if (card.rooms_offered or 0) > 0 else None,
        capacity_persons=card.capacity_persons if (card.capacity_persons or 0) > 0 else None,
        # `accessories` is defined as the things the advert NUMBERS, so a designator with no
        # digit in it is not one: fc1 returned "skříň v předsíni" and a garage space
        # designated "ne", and each split a confirmed duplicate.
        accessories={
            kind: [
                key for value in values
                if (key := _alnum(value)) is not None and any(c.isdigit() for c in key)
            ]
            for kind, values in card.accessories.items()
        },
        orientation=canonical_orientation(card.orientation),
        is_menu=card.is_menu,
        menu_units=[
            key for value in card.menu_units if (key := designator(value)) is not None
        ],
        is_new_development=card.is_new_development,
        evidence=card.evidence,
    )


def _code_key(code: str) -> tuple[str, ...]:
    """`Byt B1.2.1` / `B1-2-1` / `b1 2 1` all fold to `('b1','2','1')`.

    Segments, not one string, because segment COUNT is what tells a coarse designation
    (`budova B`) from a unit code (`B1.2.1`), and a coarser code must never split a pair."""
    stripped = _NON_ALNUM.sub(" ", fold(code)).strip()
    return tuple(part for part in _CODE_SEP.split(stripped) if part)


def _codes_conflict(left: str, right: str) -> bool:
    a, b = _code_key(left), _code_key(right)
    if not a or not b:
        return False
    if a == b:
        return False
    # One code is the other's prefix: `B1` beside `B1.2.1` is the same unit named coarsely on
    # one side, which is exactly the shape a false split would be built from.
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if long[: len(short)] == short:
        return False
    return True


def _relative_gap(left: float, right: float) -> float:
    scale = max(abs(left), abs(right))
    return abs(left - right) / scale if scale else 0.0


def _menu_covers(card: UnitCard, other_code: str | None) -> bool:
    if not card.is_menu or not other_code:
        return False
    wanted = _code_key(other_code)
    return any(_code_key(entry) == wanted for entry in card.menu_units)


def card_conflicts(
    a: UnitCard,
    b: UnitCard,
    *,
    area_tol: float = AREA_TOL,
    floor_slack: int = 0,
    stem_tolerance: bool = True,
) -> list[str]:
    """Every STATED fact that tells these two adverts' units apart, as `field: left vs right`.

    A conflict needs BOTH sides non-null AND normalised-different. Absence is unknown, never
    disagreement (the rule the whole engine runs on, and the only reason an extraction with
    honest nulls is safe to act on) — and so is a value the model did not really read, which
    `normalise` has already turned back into absence. The LLM does not appear here: it filled
    the two cards, and this is arithmetic.

    The decision is taken on the NORMALISED cards; the reason is printed from the cards the
    model actually wrote, so a conflict reads as the two adverts read.

    Menus are handled rather than excluded. An advert offering several units at once states
    many units' facts, so its unit-level values cannot contradict a single-unit advert —
    place-level facts (street, house number, parcel, locality) still can, because a menu of
    three parcels in one village is still not a parcel in another.
    """
    reasons: list[str] = []
    left, right = normalise(a), normalise(b)
    suppressed = MENU_SUPPRESSED if (left.is_menu or right.is_menu) else frozenset()

    def add(name: str, printed_left: Any, printed_right: Any) -> None:
        if name not in suppressed:
            reasons.append(f"{name}: {printed_left} vs {printed_right}")

    if left.unit_code and right.unit_code:
        if not (_menu_covers(left, right.unit_code) or _menu_covers(right, left.unit_code)):
            if _codes_conflict(left.unit_code, right.unit_code):
                add("unit_code", a.unit_code, b.unit_code)

    if left.floor is not None and right.floor is not None \
            and abs(left.floor - right.floor) > floor_slack:
        add("floor", a.floor_raw or left.floor, b.floor_raw or right.floor)

    if left.total_floors is not None and right.total_floors is not None \
            and left.total_floors != right.total_floors:
        add("total_floors", left.total_floors, right.total_floors)

    if left.area_m2 is not None and right.area_m2 is not None \
            and _areas_conflict(left.area_m2, right.area_m2, area_tol):
        add("area_m2", left.area_m2, right.area_m2)

    for label in sorted(set(left.other_areas) & set(right.other_areas)):
        if _areas_conflict(left.other_areas[label], right.other_areas[label], area_tol):
            add("other_areas", f"{label} {left.other_areas[label]}",
                f"{label} {right.other_areas[label]}")

    if left.parcel_numbers and right.parcel_numbers \
            and not (set(left.parcel_numbers) & set(right.parcel_numbers)):
        add("parcel_numbers", ",".join(sorted(set(left.parcel_numbers))),
            ",".join(sorted(set(right.parcel_numbers))))

    if left.plot_number and right.plot_number and left.plot_number != right.plot_number:
        add("plot_number", a.plot_number, b.plot_number)

    if not places_agree(
        place_tokens(a.street), place_tokens(b.street), stem_tolerance
    ):
        add("street", a.street, b.street)

    if left.house_number and right.house_number and _houses_conflict(
        tuple(left.house_number.split("/")), tuple(right.house_number.split("/"))
    ):
        add("house_number", a.house_number, b.house_number)

    if not places_agree(
        place_tokens(a.locality), place_tokens(b.locality), stem_tolerance
    ):
        add("locality", a.locality, b.locality)

    if not places_agree(
        place_tokens(a.project_name), place_tokens(b.project_name), stem_tolerance
    ):
        add("project_name", a.project_name, b.project_name)

    if left.building_block and right.building_block \
            and _codes_conflict(left.building_block, right.building_block):
        add("building_block", a.building_block, b.building_block)

    if left.disposition and right.disposition and left.disposition != right.disposition:
        add("disposition", left.disposition, right.disposition)

    if left.rooms_offered is not None and right.rooms_offered is not None \
            and left.rooms_offered != right.rooms_offered:
        add("rooms_offered", left.rooms_offered, right.rooms_offered)

    if left.capacity_persons is not None and right.capacity_persons is not None \
            and left.capacity_persons != right.capacity_persons:
        add("capacity_persons", left.capacity_persons, right.capacity_persons)

    for kind in sorted(set(left.accessories) & set(right.accessories)):
        one, other = set(left.accessories[kind]), set(right.accessories[kind])
        if one and other and not (one & other):
            add("accessories", f"{kind} {','.join(sorted(one))}",
                f"{kind} {','.join(sorted(other))}")

    if left.orientation and right.orientation and left.orientation != right.orientation:
        add("orientation", left.orientation, right.orientation)

    return reasons


def conflict_field(reason: str) -> str:
    """`"floor: 2. NP vs 3. NP"` -> `"floor"`."""
    return reason.split(":", 1)[0]


def strong_conflicts(reasons: Iterable[str]) -> list[str]:
    strong = set(STRONG_FIELDS)
    return [reason for reason in reasons if conflict_field(reason) in strong]


# --- the prompt --------------------------------------------------------------------------
#
# A prompt version is a FROZEN artifact: a card is cached for ever on (listing_id, content
# hash, prompt version), so an edit to the text a card was read under is a new version, never
# a patch of the old one. fc1's string below is byte-identical to what run 35672879363 was
# billed for; fc2 is a separate string, and both stay available so a re-run can be compared
# against the cards already paid for.


_FC1_SYSTEM: str = """\
You are reading ONE Czech property advert (title and description, as the portal printed it)
and filling a small structured card describing the unit the advert offers.

You are an EXTRACTOR, not a judge. You never decide whether two adverts are the same
property, you are never shown a second advert, and nothing you write is a similarity or an
opinion. Another program compares two cards field by field.

THE ONE RULE THAT MATTERS
Write a value ONLY when the advert states it in words or figures. Everything you are not
told is null. Never infer, never average, never complete a pattern, never carry a number
from one field into another, never translate a guess into a value. A card that is mostly
null and entirely true is exactly what this task wants; a plausible invented value is the
one failure that cannot be recovered downstream, because the program that reads your card
treats two stated values that differ as PROOF the adverts are different units.

EVIDENCE
For every non-null field, put the shortest VERBATIM span of the advert that states it into
the `evidence` object under that field's name. Copy the characters exactly, Czech
diacritics and all. If you cannot quote it, you did not read it, and the field is null.

THE FIELDS

unit_code — the designator of THIS unit exactly as printed, without the noun. Czech adverts
write it many ways: "Byt B1.2.1", "byt s označením B2.2.1", "jednotka A", "F2.103",
"5.13", "Vila II", "apartmán č. 4", "H2-304", "A4/15", "JE24.000". Copy the code itself
("B1.2.1", "A", "II"). An agency's order or reference number ("č. zakázky 1234/OL",
"ID: 98765") is NOT a unit code — leave it out. If the advert gives only a building or
block letter and no unit designator, leave unit_code null and use building_block.

floor_raw — the floor of the unit exactly as printed: "2. NP", "3. nadzemní podlaží",
"1. patro", "přízemí", "zvýšené přízemí", "suterén", "mezonet 4./5. patro".
floor — the same floor as an integer with GROUND = 0. Czech portals count two ways and you
must reconcile them: "přízemí"/"parter"/"1. NP"/"1. nadzemní podlaží" = 0; "2. NP" = 1;
"1. patro"/"1. poschodí" = 1; "3. NP" = 2; "suterén"/"sklep"/"1. PP" = -1. For a maisonette
give the LOWEST floor of the unit. If the advert states a floor only for the building and
not for the unit, both fields are null.

total_floors — how many floors the BUILDING has, if printed ("v 5patrovém domě", "budova má
4 nadzemní podlaží").

area_m2 — the headline floor area OF THE UNIT ITSELF as the advert prints it, in m². This is
the number a reader would call "how big is it": užitná plocha, podlahová plocha, celková
plocha of the flat/house/space, or the area of the land when the advert offers land. Ignore
what any structured field elsewhere might say — read the text. If the advert prints several
areas, this is the one belonging to the unit as a whole; the others go in other_areas.
If the advert offers several units with several areas, leave this null and fill the menu
fields instead.

other_areas — every OTHER area the advert prints, each with the Czech label it was printed
under: terasa, balkon, lodžie, sklep, sklepní kóje, zahrada, pozemek, garáž, půda,
komora. One entry per label, `m2` in square metres.

parcel_numbers — every cadastral parcel number printed for THIS offer: "parc. č. 934/11",
"p.č. 2150/1", "st. p. 123", "pozemky parc. č. 7, 8 a 9". Copy each number as printed
("934/11", "2150/1"). Not the plot's marketing number.
plot_number — the lot's number WITHIN a parcelling or development ("pozemek č. 7",
"parcela B3"), when the advert numbers its plots that way.

street — the street the unit is ON, as named in the text ("ul. Litovelská", "na
Podhorské"). A street the advert merely says the unit is NEAR ("nedaleko Krapkovy",
"kousek od zastávky") is not this unit's street: leave it out.
house_number — the house number printed with that street ("101/17", "č.p. 467").
locality — the village, town, town district or cadastral area the advert names as where the
unit IS ("Olbramice u Náměště na Hané", "Bohdalovice", "Praha 9 - Vysočany").

project_name — the development or residence the advert markets ("Harfa Living", "Rokytná
Resort", "Slavonínské zahrady", "Chalet Benecko").
building_block — the building, block, entrance or section within a project ("budova B",
"vchod 2", "dům F", "sekce A"), when named separately from the unit code.

disposition — the layout exactly as printed ("2+kk", "3+1", "garsoniéra", "atypický").
rooms_offered — how many rooms of a larger whole THIS advert offers, when the advert says
so: "pokoj" = 1, "dva spojené pokoje" / "2 propojené kanceláře" = 2. Null unless the advert
offers rooms as a count.
capacity_persons — a serviced office's or accommodation's stated capacity: "pro 1 osobu" =
1, "pro 2 pracovní místa" = 2, "kapacita 6 osob" = 6.

accessories — numbered things that come WITH the unit, each as kind + designator:
"garážové stání č. 47" -> kind "stání", designator "47"; "sklepní kóje č. 25" -> kind
"kóje", designator "25"; "garáž č. 3". Only when the advert NUMBERS them.
orientation — the compass orientation printed for the unit ("orientace na jih",
"jihozápad").

is_menu — true when this ONE advert offers SEVERAL units at once rather than a single unit:
"tři samostatné parcely", "nabízíme byty 2+kk až 4+kk", "pozemky č. 7, 8 a 9",
"k dispozici jsou jednotky A a B", "poslední 3 volné byty". A single unit described with
several rooms is NOT a menu.
menu_units — when is_menu is true, the designators or descriptions of the units offered, as
printed ("7", "8", "9"; "A", "B"; "2+kk", "3+kk").
is_new_development — true when the advert markets a NEW BUILD development or project (a
developer selling units of a building under or just after construction), false otherwise.

Call the tool record_unit_identity_card exactly once.
"""


# fc2. Same task, same tool, same fields — every change below is an answer to something the
# fc1 cards actually did on 1,276 adverts (run 35672879363):
#
#   * `unit_code` came back holding a DISPOSITION 149 times ("3+1", "2+kk", "1+kk?"), an
#     object word ("garáž", "Pozemek"), a street name ("Dřevařská") or the string "null" —
#     the single biggest source of false splits.
#   * 233 `unit_code`s and 71 `floor_raw`s were the four characters n-u-l-l inside a string.
#   * guesses arrived marked as guesses ("B1.2.?", "Botič II?", "3+kk? no") — the model knew
#     it did not know and wrote a value anyway.
#   * commentary leaked into value slots ("Plzeň - Skvrny? text says Plzeň Skvrňany. The
#     locality should be…", "2+kk? wait 1+1 is. The text says byt 1+1.").
#   * 22% of evidence spans were not verbatim in the advert, and thousands of filled fields
#     carried no span at all.
#
# So fc2 says what a value slot is NOT, in the model's own failure vocabulary, and the schema
# carries the same rails where JSON Schema can express them.
_FC2_SYSTEM: str = """\
You are reading ONE Czech property advert (title and description, as the portal printed it)
and filling a small structured card describing the unit the advert offers.

You are an EXTRACTOR, not a judge. You never decide whether two adverts are the same
property, you are never shown a second advert, and nothing you write is a similarity or an
opinion. Another program compares two cards field by field.

THE ONE RULE THAT MATTERS
Write a value ONLY when the advert states it in words or figures. Everything you are not
told is null. Never infer, never average, never complete a pattern, never carry a number
from one field into another, never translate a guess into a value. A card that is mostly
null and entirely true is exactly what this task wants; a plausible invented value is the
one failure that cannot be recovered downstream, because the program that reads your card
treats two stated values that differ as PROOF the adverts are different units.

A FIELD IS A VALUE OR IT IS null — FOUR THINGS THAT ARE NOT VALUES
1. A GUESS. If you are not sure, the field is null. Never write a question mark. "B1.2.?",
   "Botič II?", "1+kk?", "Plzeň - Skvrny?" are all null. A question mark anywhere in a value
   means the whole field should have been null.
2. YOUR REASONING. No commentary, no alternatives, no corrections, no explanation of your
   choice. Never "text says…", never "wait", never "likely", never "the locality should
   be…", never "not provided". If you are writing a sentence, the answer is null.
3. THE WORD "null". Use the JSON value null, never the four characters n-u-l-l inside a
   string, and never "N/A", "none" or "neuvedeno".
4. A VALUE OF THE WRONG KIND. Each field below says what kind it holds. A layout is never a
   unit code; a floor is never a locality; a street is never a project.

EVIDENCE
For every non-null field, put the shortest VERBATIM span of the advert that states it into
the `evidence` object under that field's name. Copy the characters exactly, Czech
diacritics and all — a span that is not a substring of the advert is a fabricated field. If
you cannot quote it, you did not read it, and the field is null. Every field of `evidence`
is required: write the span, or write null there too.

THE FIELDS

unit_code — the printed alphanumeric DESIGNATOR of ONE unit, exactly as printed, without the
noun. Czech adverts write it many ways: "Byt B1.2.1", "byt s označením B2.2.1", "jednotka A",
"F2.103", "5.13", "Vila II", "apartmán č. 4", "H2-304", "A4/15", "JE24.000". Copy the code
itself ("B1.2.1", "A", "II", "4"). It contains no spaces and no question mark.
  NEVER a layout: "3+1", "2+kk", "1+kk", "garsoniéra" are dispositions — that is the
  `disposition` field, and unit_code stays null.
  NEVER a kind of thing: "garáž", "byt", "pozemek", "parcela", "chata", "rodinný dům",
  "nebytový prostor" name what is offered, not which one it is.
  NEVER a place: a street, village or project name is not a unit code.
  NEVER an agency's order or reference number ("č. zakázky 1234/OL", "ID: 98765").
  If the advert gives only a building or block letter and no unit designator, leave unit_code
  null and use building_block. If the advert prints no designator at all — which is the
  normal case — unit_code is null.

floor_raw — the floor of the unit exactly as printed, and nothing else: "2. NP", "3.
nadzemní podlaží", "1. patro", "přízemí", "zvýšené přízemí", "suterén", "mezonet 4./5.
patro". Copy the printed words; do not rewrite them.
floor — the same floor as an integer with GROUND = 0. Czech portals count two ways and you
must reconcile them: "přízemí"/"parter"/"1. NP"/"1. nadzemní podlaží" = 0; "2. NP" = 1;
"1. patro"/"1. poschodí" = 1; "3. NP" = 2; "suterén"/"sklep"/"1. PP" = -1. For a maisonette
give the LOWEST floor of the unit. If the advert states a floor only for the building and
not for the unit, both fields are null.

total_floors — how many floors the BUILDING has, if printed ("v 5patrovém domě", "budova má
4 nadzemní podlaží").

area_m2 — the headline floor area OF THE UNIT ITSELF as the advert prints it, in m². This is
the number a reader would call "how big is it": užitná plocha, podlahová plocha, celková
plocha of the flat/house/space, or the area of the land when the advert offers land. Ignore
what any structured field elsewhere might say — read the text. If the advert prints several
areas, this is the one belonging to the unit as a whole; the others go in other_areas.
If the advert offers several units with several areas, leave this null and fill the menu
fields instead.

other_areas — every OTHER area the advert prints, each with the Czech label it was printed
under: terasa, balkon, lodžie, sklep, sklepní kóje, zahrada, pozemek, garáž, půda,
komora. One entry per label, `m2` in square metres.

parcel_numbers — every cadastral parcel number printed for THIS offer: "parc. č. 934/11",
"p.č. 2150/1", "st. p. 123", "pozemky parc. č. 7, 8 a 9". Copy each number as printed
("934/11", "2150/1"). Not the plot's marketing number, and not an area in m².
plot_number — the lot's number WITHIN a parcelling or development ("pozemek č. 7",
"parcela B3"), when the advert numbers its plots that way.

street — the street name the unit is ON and nothing more: "Litovelská", "Krapkova",
"Podhorská". Drop the noun and the preposition ("ul.", "ulice", "v", "na"). A street the
advert merely says the unit is NEAR ("nedaleko Krapkovy", "kousek od zastávky") is not this
unit's street: leave it out. Two streets named for one offer means null, not both.
house_number — the house number printed with that street ("101/17", "č.p. 467").
locality — the village, town, town district or cadastral area the advert names as where the
unit IS, as printed and nothing more: "Olomouc", "Plzeň - Skvrňany", "Olbramice u Náměště na
Hané", "Praha 9 - Vysočany". Not a description of the surroundings.

project_name — the development or residence the advert markets ("Harfa Living", "Rokytná
Resort", "Slavonínské zahrady", "Chalet Benecko"). A sentence about a project is not its
name; if no name is printed, null.
building_block — the building, block, entrance or section within a project ("budova B",
"vchod 2", "dům F", "sekce A"), when named separately from the unit code. "bytový dům" with
no letter or number names no block: null.

disposition — the layout in the canonical Czech form ONLY: "2+kk", "3+1", "1+kk", "4+1".
Anything else is null — "garsoniéra", "atypický", "4 pokoje", "nebytový prostor", and any
advert printing two layouts ("1kk a 2kk", "4+1 a 3+1") all give null. No parentheses, no
extra words, no room list.
rooms_offered — how many rooms of a larger whole THIS advert offers, when the advert says
so: "pokoj" = 1, "dva spojené pokoje" / "2 propojené kanceláře" = 2. Null unless the advert
offers rooms as a count. This is NOT the room count of a normal flat: a 3+1 offered whole
gives null here.
capacity_persons — a serviced office's or accommodation's stated capacity: "pro 1 osobu" =
1, "pro 2 pracovní místa" = 2, "kapacita 6 osob" = 6.

accessories — numbered things that come WITH the unit, each as kind + designator:
"garážové stání č. 47" -> kind "stání", designator "47"; "sklepní kóje č. 25" -> kind
"kóje", designator "25"; "garáž č. 3". Only when the advert NUMBERS them.
orientation — the compass orientation printed for the unit, as one of exactly these words:
jih, sever, východ, západ, jihozápad, jihovýchod, severozápad, severovýchod. Anything else,
including a view or a courtyard, is null.

is_menu — true when this ONE advert offers SEVERAL units at once rather than a single unit:
"tři samostatné parcely", "nabízíme byty 2+kk až 4+kk", "pozemky č. 7, 8 a 9",
"k dispozici jsou jednotky A a B", "poslední 3 volné byty". A single unit described with
several rooms is NOT a menu.
menu_units — when is_menu is true, the designators or descriptions of the units offered, as
printed ("7", "8", "9"; "A", "B"; "2+kk", "3+kk").
is_new_development — true when the advert markets a NEW BUILD development or project (a
developer selling units of a building under or just after construction), false otherwise.

Call the tool record_unit_identity_card exactly once.
"""

USER_HEADER: str = "LISTING {listing_id} — advert text (Czech, contact details removed{note}):"
TRUNCATED_NOTE: str = ", truncated"
USER_TASK: str = (
    "Fill the card from this text alone. Every field you were not told is null, and every "
    "field you fill carries its verbatim quote in `evidence`."
)

_NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"]}
_NULLABLE_NUMBER: dict[str, Any] = {"type": ["number", "null"]}
_NULLABLE_INT: dict[str, Any] = {"type": ["integer", "null"]}

# `evidence` carries one quote per field it can quote. Declared as named properties rather
# than a free map so the model is told WHICH fields owe a quote — an open object gets one
# summary sentence back instead of per-field spans.
_EVIDENCE_FIELDS: tuple[str, ...] = (
    "unit_code", "floor_raw", "total_floors", "area_m2", "other_areas", "parcel_numbers",
    "plot_number", "street", "house_number", "locality", "project_name", "building_block",
    "disposition", "rooms_offered", "capacity_persons", "accessories", "orientation",
    "is_menu", "is_new_development",
)

_FC1_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Record what ONE Czech property advert prints about the unit it offers. Call exactly "
        "once. Every field not stated in the advert is null."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "unit_code": _NULLABLE_STRING | {
                "description": "The unit's designator exactly as printed, without the noun.",
            },
            "floor_raw": _NULLABLE_STRING | {
                "description": "The unit's floor exactly as printed, e.g. '2. NP', '1. patro'.",
            },
            "floor": _NULLABLE_INT | {
                "description": "The same floor normalised, ground = 0, cellar = -1.",
            },
            "total_floors": _NULLABLE_INT | {
                "description": "How many floors the building has, if printed.",
            },
            "area_m2": _NULLABLE_NUMBER | {
                "description": "The unit's own headline floor area in m² as printed.",
            },
            "other_areas": {
                "type": "array",
                "description": "Every other printed area, under its Czech label.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "label": {"type": "string"},
                        "m2": {"type": "number"},
                    },
                    "required": ["label", "m2"],
                },
            },
            "parcel_numbers": {
                "type": "array",
                "description": "Cadastral parcel numbers printed for this offer.",
                "items": {"type": "string"},
            },
            "plot_number": _NULLABLE_STRING | {
                "description": "The lot's number within a parcelling or development.",
            },
            "street": _NULLABLE_STRING | {
                "description": "The street the unit is on (not one it is merely near).",
            },
            "house_number": _NULLABLE_STRING | {
                "description": "The house number printed with that street.",
            },
            "locality": _NULLABLE_STRING | {
                "description": "The village, town or district the unit is in.",
            },
            "project_name": _NULLABLE_STRING | {
                "description": "The development or residence the advert markets.",
            },
            "building_block": _NULLABLE_STRING | {
                "description": "The building, block, entrance or section within a project.",
            },
            "disposition": _NULLABLE_STRING | {
                "description": "The layout exactly as printed, e.g. '2+kk'.",
            },
            "rooms_offered": _NULLABLE_INT | {
                "description": "How many rooms of a larger whole this advert offers.",
            },
            "capacity_persons": _NULLABLE_INT | {
                "description": "A stated capacity in persons or workplaces.",
            },
            "accessories": {
                "type": "array",
                "description": "Numbered things included with the unit.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string"},
                        "designator": {"type": "string"},
                    },
                    "required": ["kind", "designator"],
                },
            },
            "orientation": _NULLABLE_STRING | {
                "description": "The compass orientation printed for the unit.",
            },
            "is_menu": {
                "type": "boolean",
                "description": "True when this advert offers SEVERAL units at once.",
            },
            "menu_units": {
                "type": "array",
                "description": "The units offered, as printed, when is_menu is true.",
                "items": {"type": "string"},
            },
            "is_new_development": {
                "type": "boolean",
                "description": "True when the advert markets a new-build development.",
            },
            "evidence": {
                "type": "object",
                "additionalProperties": False,
                "description": "One verbatim span of the advert per field filled.",
                "properties": {name: {"type": "string"} for name in _EVIDENCE_FIELDS},
            },
        },
        "required": [
            "unit_code", "floor_raw", "floor", "total_floors", "area_m2", "other_areas",
            "parcel_numbers", "plot_number", "street", "house_number", "locality",
            "project_name", "building_block", "disposition", "rooms_offered",
            "capacity_persons", "accessories", "orientation", "is_menu", "menu_units",
            "is_new_development", "evidence",
        ],
    },
}

# The compass words `canonical_orientation` can read. An enum because the fc1 cards came back
# with "již", "jiho-západ", "Severní orientace" and one whole sentence about a courtyard.
ORIENTATIONS: tuple[str, ...] = (
    "jih", "sever", "východ", "západ", "jihozápad", "jihovýchod", "severozápad",
    "severovýchod",
)

# fc2's schema deltas, keyed by property. `pattern`, `enum`, `maxLength` and the numeric
# bounds are not enforced by either provider (nothing here sends OpenAI's `strict: true`, and
# DashScope validates nothing), so they are instructions in the place a model is most likely
# to read them — beside the field, not in the prose. The rails that MUST hold are in
# `clean_value` / `designator` / `canonical_disposition`, in code.
_FC2_CONSTRAINTS: dict[str, dict[str, Any]] = {
    "unit_code": {
        "pattern": r"^[A-Za-z0-9]+(?:[./-][A-Za-z0-9]+)*$",
        "maxLength": 20,
        "description": (
            "The printed alphanumeric designator of ONE unit, without its noun ('B1.2.1', "
            "'A', 'II', '5.13'). NEVER a layout ('3+1', '2+kk' — that is disposition), never "
            "an object word ('garáž', 'pozemek'), never a place name, never a guess, no "
            "question mark, no spaces. Null when the advert prints no designator."
        ),
    },
    "floor_raw": {
        "maxLength": 40,
        "description": (
            "The unit's floor exactly as printed and nothing else, e.g. '2. NP', '1. patro', "
            "'přízemí'."
        ),
    },
    "floor": {"minimum": -5, "maximum": 60},
    "total_floors": {"minimum": 1, "maximum": 60},
    "area_m2": {"exclusiveMinimum": 0, "maximum": 100000},
    "plot_number": {"maxLength": 20},
    "street": {
        "maxLength": 40,
        "description": (
            "The street name the unit is on, without 'ul.'/'ulice' and without a "
            "preposition. Not a street it is merely near. Null if two streets are named."
        ),
    },
    "house_number": {"pattern": r"^[0-9]+[A-Za-z]?(?:/[0-9]+[A-Za-z]?)?$", "maxLength": 12},
    "locality": {
        "maxLength": 40,
        "description": (
            "The village, town, district or cadastral area the unit IS in, as printed "
            "('Olomouc', 'Plzeň - Skvrňany'). Not a description of the surroundings."
        ),
    },
    "project_name": {
        "maxLength": 40,
        "description": (
            "The name of the development or residence the advert markets. A sentence about a "
            "project is not its name."
        ),
    },
    "building_block": {
        "maxLength": 20,
        "description": (
            "The building, block, entrance or section within a project ('budova B', 'vchod "
            "2'). 'bytový dům' with no letter or number is null."
        ),
    },
    "disposition": {
        "pattern": r"^[0-9]+\+(?:kk|1)$",
        "maxLength": 8,
        "description": (
            "The layout in canonical form ONLY: '2+kk', '3+1'. Anything else "
            "('garsoniéra', 'atypický', '4 pokoje', two layouts at once) is null."
        ),
    },
    "rooms_offered": {
        "minimum": 1,
        "maximum": 20,
        "description": (
            "How many rooms of a larger whole this advert offers, when the advert says so. "
            "Null for a normal flat offered whole."
        ),
    },
    "capacity_persons": {"minimum": 1, "maximum": 500},
    "orientation": {
        "enum": [*ORIENTATIONS, None],
        "description": "One printed compass word, or null.",
    },
}


def _fc2_schema() -> dict[str, Any]:
    """fc1's schema with the constraints above, and an `evidence` object that owes an answer.

    `evidence` goes nullable-and-required for the same reason the card's own fields did: an
    optional quote is a quote the model can quietly skip, and fc1 skipped 376 of them on
    `locality` alone. Required + nullable puts no pressure on the model to invent one — null
    is a legal answer — while making "filled without evidence" impossible to hide.
    """
    schema: dict[str, Any] = json.loads(json.dumps(_FC1_SCHEMA))
    properties = schema["input_schema"]["properties"]
    for name, constraints in _FC2_CONSTRAINTS.items():
        properties[name].update(constraints)
    properties["evidence"] = {
        "type": "object",
        "additionalProperties": False,
        "description": (
            "One VERBATIM span of the advert per field filled, copied character for "
            "character, or null for a field left null."
        ),
        "properties": {
            name: {"type": ["string", "null"], "maxLength": 200}
            for name in _EVIDENCE_FIELDS
        },
        "required": list(_EVIDENCE_FIELDS),
    }
    return schema


@dataclass(frozen=True)
class Prompt:
    """One frozen (system prompt, tool schema) pair, named by the version a card is keyed on."""

    version: str
    system: str
    tool_schema: dict[str, Any]


PROMPTS: dict[str, Prompt] = {
    "fc1": Prompt("fc1", _FC1_SYSTEM, _FC1_SCHEMA),
    "fc2": Prompt("fc2", _FC2_SYSTEM, _fc2_schema()),
}

PROMPT_VERSION: str = "fc2"
SYSTEM_PROMPT: str = PROMPTS[PROMPT_VERSION].system
TOOL_SCHEMA: dict[str, Any] = PROMPTS[PROMPT_VERSION].tool_schema


def prompt_for(version: str) -> Prompt:
    if version not in PROMPTS:
        raise SystemExit(
            f"unknown prompt_version {version!r}; known versions: {', '.join(PROMPTS)}"
        )
    return PROMPTS[version]


def prompt_text(text: str | None, max_chars: int = TEXT_MAX_CHARS) -> tuple[str | None, bool]:
    """E28's scrub, then the runaway cap. Returns the text and whether it was cut."""
    scrubbed = scrub_for_prompt(text)
    if not (scrubbed or "").strip():
        return None, False
    whole = scrubbed or ""
    if len(whole) > max_chars:
        return whole[:max_chars], True
    return whole, False


def build_messages(
    listing_id: int, text: str, truncated: bool = False
) -> list[dict[str, Any]]:
    """One user message in the legacy dict shape `LLMClient.call` accepts."""
    header = USER_HEADER.format(
        listing_id=listing_id, note=TRUNCATED_NOTE if truncated else ""
    )
    return [{
        "role": "user",
        "content": [{"type": "text", "text": f"{header}\n{text}\n\n{USER_TASK}"}],
    }]


def content_key(text: str) -> str:
    """The cache key's text half: the scrubbed text a card was read from, hashed.

    A production `autodedup_facts` cache is keyed (listing_id, this, prompt_version) — a card
    is valid for ever while all three hold, which is what makes the whole approach affordable.
    """
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
