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
from autodedup.text_facts import streets_agree

PROMPT_VERSION: str = "fc1"
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

# Fields whose disagreement is a STATED fact about the unit itself. A conflict here is what
# the operator means by "a distinguishing fact".
STRONG_FIELDS: tuple[str, ...] = (
    "unit_code",
    "floor",
    "area_m2",
    "parcel_numbers",
    "plot_number",
    "street",
    "house_number",
    "rooms_offered",
    "capacity_persons",
    "accessories",
    "other_areas",
)

# Disagreement here is real but softer — spelling variance (a project markets itself under
# three names), or a fact about the BUILDING rather than the unit. Reported separately so the
# experiment can price each field's false splits before anything is promoted.
WEAK_FIELDS: tuple[str, ...] = (
    "total_floors",
    "locality",
    "project_name",
    "building_block",
    "disposition",
    "orientation",
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
) -> list[str]:
    """Every STATED fact that tells these two adverts' units apart, as `field: left vs right`.

    A conflict needs BOTH sides non-null — absence is unknown, never disagreement (the rule
    the whole engine runs on, and the only reason an extraction with honest nulls is safe to
    act on). The LLM does not appear here: it filled the two cards, and this is arithmetic.

    Menus are handled rather than excluded. An advert offering several units at once states
    many units' facts, so its unit-level values cannot contradict a single-unit advert —
    place-level facts (street, house number, parcel, locality) still can, because a menu of
    three parcels in one village is still not a parcel in another.
    """
    reasons: list[str] = []
    menu = a.is_menu or b.is_menu
    suppressed = MENU_SUPPRESSED if menu else frozenset()

    def add(name: str, left: Any, right: Any) -> None:
        if name not in suppressed:
            reasons.append(f"{name}: {left} vs {right}")

    if a.unit_code and b.unit_code:
        if not (_menu_covers(a, b.unit_code) or _menu_covers(b, a.unit_code)):
            if _codes_conflict(a.unit_code, b.unit_code):
                add("unit_code", a.unit_code, b.unit_code)

    if a.floor is not None and b.floor is not None and abs(a.floor - b.floor) > floor_slack:
        add("floor", a.floor_raw or a.floor, b.floor_raw or b.floor)

    if a.total_floors is not None and b.total_floors is not None \
            and a.total_floors != b.total_floors:
        add("total_floors", a.total_floors, b.total_floors)

    if a.area_m2 is not None and b.area_m2 is not None \
            and _relative_gap(a.area_m2, b.area_m2) > area_tol:
        add("area_m2", a.area_m2, b.area_m2)

    for label in sorted(set(a.other_areas) & set(b.other_areas)):
        left, right = a.other_areas[label], b.other_areas[label]
        if _relative_gap(left, right) > area_tol:
            add("other_areas", f"{label} {left}", f"{label} {right}")

    if a.parcel_numbers and b.parcel_numbers:
        left = {fold(value) for value in a.parcel_numbers}
        right = {fold(value) for value in b.parcel_numbers}
        if not (left & right):
            add("parcel_numbers", ",".join(sorted(left)), ",".join(sorted(right)))

    if a.plot_number and b.plot_number and fold(a.plot_number) != fold(b.plot_number):
        add("plot_number", a.plot_number, b.plot_number)

    if a.street and b.street and not streets_agree([fold(a.street)], [fold(b.street)]):
        add("street", a.street, b.street)

    if a.house_number and b.house_number \
            and _NON_ALNUM.sub("", fold(a.house_number)) \
            != _NON_ALNUM.sub("", fold(b.house_number)):
        add("house_number", a.house_number, b.house_number)

    if a.locality and b.locality and fold(a.locality) != fold(b.locality):
        add("locality", a.locality, b.locality)

    if a.project_name and b.project_name and fold(a.project_name) != fold(b.project_name):
        add("project_name", a.project_name, b.project_name)

    if a.building_block and b.building_block \
            and _codes_conflict(a.building_block, b.building_block):
        add("building_block", a.building_block, b.building_block)

    if a.disposition and b.disposition \
            and _NON_ALNUM.sub("", fold(a.disposition)) \
            != _NON_ALNUM.sub("", fold(b.disposition)):
        add("disposition", a.disposition, b.disposition)

    if a.rooms_offered is not None and b.rooms_offered is not None \
            and a.rooms_offered != b.rooms_offered:
        add("rooms_offered", a.rooms_offered, b.rooms_offered)

    if a.capacity_persons is not None and b.capacity_persons is not None \
            and a.capacity_persons != b.capacity_persons:
        add("capacity_persons", a.capacity_persons, b.capacity_persons)

    for kind in sorted(set(a.accessories) & set(b.accessories)):
        left = {_NON_ALNUM.sub("", fold(value)) for value in a.accessories[kind]}
        right = {_NON_ALNUM.sub("", fold(value)) for value in b.accessories[kind]}
        if left and right and not (left & right):
            add("accessories", f"{kind} {','.join(sorted(left))}",
                f"{kind} {','.join(sorted(right))}")

    if a.orientation and b.orientation and fold(a.orientation) != fold(b.orientation):
        add("orientation", a.orientation, b.orientation)

    return reasons


def conflict_field(reason: str) -> str:
    """`"floor: 2. NP vs 3. NP"` -> `"floor"`."""
    return reason.split(":", 1)[0]


def strong_conflicts(reasons: Iterable[str]) -> list[str]:
    strong = set(STRONG_FIELDS)
    return [reason for reason in reasons if conflict_field(reason) in strong]


# --- the prompt --------------------------------------------------------------------------


SYSTEM_PROMPT: str = """\
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

TOOL_SCHEMA: dict[str, Any] = {
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
