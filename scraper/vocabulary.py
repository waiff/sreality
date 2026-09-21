"""The PRODUCER side of the one canonical vocabulary (field-capture R5).

The canon itself — what a `condition` or a `building_type` may BE — lives in
`toolkit.filter_registry`, which already generates the SPA's option lists and the API
schema under a CI drift gate. This module never restates it; it imports it and owns the
other half: turning what a portal WROTE into one of those values.

  * `fold` — one diacritic/whitespace/`… stav` normalisation, replacing the nine
    near-identical `_strip_diacritics(...).lower()` chains.
  * `canonical` — ONE `(field, portal label) -> canonical` registry. A label no entry
    names is NULL **and a counted event** (`unmapped_events`), never a passthrough: the
    passthrough `.get(key, key)` idiom is how 13 `condition` spellings and 15
    `building_type` values reached a column whose filter offers 6 and 8.
  * `disposition` / `energy_rating` — ONE grammar each, replacing nine disposition
    regexes and seven energy-class regexes.
  * `yes_no` / `present` / `states` / `contains` / `mentions` — the five boolean
    readings a portal actually uses, from an `Ano` cell to a set of accessory names.
    They differ in what SILENCE means, which is a live fact per portal and not a thing to
    unify by fiat: W4 is where the readings converge. What a MISSING KEY means is not
    decided here at all — absence is a per-(portal, field) fact the attribute contract
    declares, because only sreality and bezrealitky ever write an explicit `false`.

**Identity, until W5.** Every value a parser emits today is still emitted today. The
off-canon spellings live rows carry (`ve_vystavbe_(hruba_stavba)`, `urceny_k_demolici`,
building_type `jina`) are declared as LEGACY entries mapped to themselves, with the
collapse W5 will apply recorded beside them — so this wave moves no stored value and W5
is one table edit plus one counted batch.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Mapping
from unicodedata import combining, normalize

from toolkit.filter_registry import (
    BUILDING_TYPE_OPTIONS,
    CONDITION_OPTIONS,
    DISPOSITION_OPTIONS,
    ENERGY_RATING_OPTIONS,
    FURNISHED_CANONICAL,
    OWNERSHIP_CANONICAL,
)

# The canon, read from the consumer-side registry — one list, never a second spelling.
CANON: dict[str, frozenset[str]] = {
    "condition": frozenset(o.value for o in CONDITION_OPTIONS),
    "building_type": frozenset(o.value for o in BUILDING_TYPE_OPTIONS),
    "ownership": frozenset(OWNERSHIP_CANONICAL),
    "furnished": frozenset(FURNISHED_CANONICAL),
    "energy_rating": frozenset(o.value for o in ENERGY_RATING_OPTIONS),
    "disposition": frozenset(o.value for o in DISPOSITION_OPTIONS),
}

# Values live rows carry that the canon has no slot for, mapped here to THEMSELVES so
# this wave moves nothing, with the collapse W5 applies to stored rows recorded beside
# each. A `None` target means "the operator rules on it in W5" (R11 adds the real ones as
# canonical members rather than NULLing a stated fact).
LEGACY_COLLAPSES: dict[str, dict[str, str | None]] = {
    "condition": {
        "ve_vystavbe_(hruba_stavba)": "ve_vystavbe",  # a slugified portal label, parens and all
        "urceny_k_demolici": "k_demolici",
        "ve_vystavbe": None,      # R11: becomes a canonical member
        "projekt": None,
        "v_rekonstrukci": None,
        "udrzovany": None,
        "spatny": None,
    },
    "building_type": {
        "zdena, kamenna": "cihla",    # a comma-joined ceskereality cell nothing splits
        "drevena, zdena": "cihla",
        "jina": None,                 # 8,230 ceskereality rows; R11 rules
        "ocelova": None,
        "roubena": None,
        "modularni": None,
    },
    "ownership": {"jine": None},      # 73 realitymix rows, semantically absent
}

_LEGACY_VALUES: dict[str, frozenset[str]] = {
    field: frozenset(values) for field, values in LEGACY_COLLAPSES.items()
}


def known_values(field: str) -> frozenset[str]:
    """Every value this module may emit for a field: the canon plus today's legacy."""
    return CANON.get(field, frozenset()) | _LEGACY_VALUES.get(field, frozenset())


# --- the one fold ----------------------------------------------------------

_WS = re.compile(r"[\s,/]+")
_STATE_SUFFIX = re.compile(r"\s+stav$")


def fold(text: str | None) -> str:
    """Diacritics stripped, lower-cased, `… stav` suffix dropped, separators to `_`.

    The `… stav` strip is what lets idnes's "velmi dobrý stav" and sreality's "Velmi
    dobrý" be one entry instead of two; the `,` and `/` in the separator class are what
    let "Státní/obecní" and "státní, obecní, jiné" fold onto one key."""
    if not text:
        return ""
    plain = "".join(c for c in normalize("NFD", str(text)) if not combining(c))
    return _WS.sub("_", _STATE_SUFFIX.sub("", plain.lower().strip())).strip("_")


# --- the registry ----------------------------------------------------------

# One entry per label any portal states, folded. The value is what the column gets; a
# label absent from the table is NULL + a counted event, and the A3 gate proves no live
# value is missing. Ordered canonical-first, legacy last, with the portals that emit each
# label named only where it is not obvious.
_LABELS: dict[str, dict[str, str]] = {
    "condition": {
        "velmi_dobry": "velmi_dobry", "bezvadny": "velmi_dobry", "very_good": "velmi_dobry",
        "dobry": "dobry", "good": "dobry", "v_puvodnim_stavu": "dobry",
        "novostavba": "novostavba", "new": "novostavba",
        "po_rekonstrukci": "po_rekonstrukci", "after_reconstruction": "po_rekonstrukci",
        "after_partial_reconstruction": "po_rekonstrukci",
        "pred_rekonstrukci": "pred_rekonstrukci", "k_rekonstrukci": "pred_rekonstrukci",
        "before_reconstruction": "pred_rekonstrukci",
        "k_demolici": "k_demolici", "demolition": "k_demolici",
        "ve_vystavbe": "ve_vystavbe", "rozestaveny": "ve_vystavbe",
        "construction": "ve_vystavbe",
        "projekt": "projekt", "project": "projekt",
        "v_rekonstrukci": "v_rekonstrukci", "in_reconstruction": "v_rekonstrukci",
        "spatny": "spatny", "bad": "spatny",
        "udrzovany": "udrzovany",
        "ve_vystavbe_(hruba_stavba)": "ve_vystavbe_(hruba_stavba)",
        "urceny_k_demolici": "urceny_k_demolici",
    },
    "building_type": {
        "cihlova": "cihla", "cihla": "cihla", "zdena": "cihla", "brick": "cihla",
        "panelova": "panel", "panel": "panel",
        "smisena": "smisena", "mixed": "smisena",
        "skeletova": "skelet", "skelet": "skelet",
        "drevena": "drevo", "drevostavba": "drevo", "wood": "drevo",
        "kamenna": "kamen", "stone": "kamen",
        "montovana": "montovana", "prefab": "montovana",
        "nizkoenergeticka": "nizkoenergeticka",
        "jina": "jina", "ocelova": "ocelova", "roubena": "roubena",
        "modularni": "modularni",
        "zdena_kamenna": "zdena, kamenna", "drevena_zdena": "drevena, zdena",
    },
    "ownership": {
        "osobni": "osobni", "soukrome": "osobni",
        "druzstevni": "druzstevni",
        "statni": "statni", "obecni": "statni", "statni_obecni": "statni",
        "statni_obecni_jine": "statni",
    },
    "furnished": {
        "ano": "ano", "zarizeny": "ano", "zarizeno": "ano", "vybaveny": "ano",
        "ne": "ne", "nezarizeny": "ne", "nezarizeno": "ne", "nevybaveny": "ne",
        "castecne": "castecne", "castecne_zarizeny": "castecne",
        "castecne_zarizeno": "castecne", "castecne_vybaveny": "castecne",
    },
}

# Labels a portal states that mean "no value", not a value. Refused here rather than
# NULLed silently, so they never count as unmapped: the operator has decided about them.
_REFUSED: dict[str, frozenset[str]] = {
    "condition": frozenset({"undefined", "neuvedeno", "rezervovano", "prodano"}),
    "building_type": frozenset({"undefined", "neuvedeno", "ostatni", "jine",
                                "rezervovano", "prodano"}),
    "ownership": frozenset({"undefined", "ostatni", "jine", "s.r.o.", "podilove",
                            "neuvedeno"}),
    "furnished": frozenset({"undefined", "neuvedeno"}),
}

# Where one portal reads a label differently from the rest. `jine` is the whole list: on
# idnes and mmreality an "other" ownership has always been dropped, and on realitymix it
# has always been stored as `jine` (73 active rows). W5 is where those two agree.
_PORTAL_LABELS: dict[tuple[str, str], dict[str, str]] = {
    ("realitymix", "ownership"): {"jine": "jine"},
}

UNMAPPED: Counter[str] = Counter()


def canonical(field: str, portal: str, label: str | None) -> str | None:
    """The canonical value a portal's label carries, or None.

    None on three different grounds, only one of which is a defect: the label is absent;
    the label is a refusal the operator already ruled on ("neuvedeno", a sold/reserved
    status overlay sreality paints over the condition name); or NOTHING maps it — which
    is counted under `{field}/{portal}/{label}` so a portal relabelling a dropdown shows
    up in the run summary instead of quietly emptying a column."""
    key = fold(label)
    if not key:
        return None
    override = _PORTAL_LABELS.get((portal, field))
    if override and key in override:
        return override[key]
    table = _LABELS.get(field)
    if table is None:
        raise KeyError(f"no vocabulary for field {field!r}")
    value = table.get(key)
    if value is not None:
        return value
    if not _is_refusal(field, key):
        UNMAPPED[f"{field}/{portal}/{key}"] += 1
    return None


def _is_refusal(field: str, key: str) -> bool:
    """A label that means "not specified" rather than a value.

    sreality spells the empty option of every dropdown as a leading dash and the words
    "vyber"/"nezadano" ("choose…"/"not entered"), and paints the sale STATUS over the
    condition and building-type names on a reserved or sold advert. Those are absence,
    already ruled on, and must not be counted as an unmapped label."""
    return (key in _REFUSED.get(field, frozenset())
            or key.startswith("-") or "vyber" in key or "nezadano" in key)


def unmapped_events() -> list[tuple[str, int]]:
    """`(field/portal/label, count)` for this process, worst first."""
    return UNMAPPED.most_common()


# --- the two grammars ------------------------------------------------------

# One disposition grammar. The spaced form is the superset: every portal's own regex was
# either this or the unspaced `\d\+(kk|\d)` sreality used, which this matches too.
_DISPOSITION_RE = re.compile(r"\b(\d)\s*\+\s*(kk|\d)\b", re.IGNORECASE)
# bezrealitky states its disposition as a code, not as text.
_DISP_CODE_RE = re.compile(r"DISP_(\d)_(KK|1|IZB)")
_ENERGY_RE = re.compile(r"\b([A-G])\b")


def disposition(*texts: str | None) -> str | None:
    """The first `N+kk` / `N+M` any of the texts states, in the caller's order."""
    for text in texts:
        if not text:
            continue
        match = _DISPOSITION_RE.search(str(text))
        if match:
            return f"{match.group(1)}+{match.group(2).lower()}"
    return None


def disposition_code(value: str | None) -> str | None:
    """bezrealitky's `DISP_3_KK` / `GARSONIERA` enum."""
    if not value:
        return None
    if value == "GARSONIERA":
        return "1+kk"
    match = _DISP_CODE_RE.fullmatch(value)
    if not match:
        return None
    return f"{match.group(1)}+kk" if match.group(2) == "KK" else f"{match.group(1)}+1"


def energy_rating(*texts: str | None) -> str | None:
    """The PENB class: the first standalone A–G letter any of the texts states.

    A stated `G` is very often the statutory placeholder for an UNASSESSED building
    (67.4% of every rated row, uniformly across all nine portals) rather than a
    measurement. That is a measure-validity fact, recorded by the fill matrix and ruled
    on in W5 — not something to silently drop here."""
    for text in texts:
        if not text:
            continue
        match = _ENERGY_RE.search(str(text))
        if match:
            return match.group(1).upper()
    return None


# --- the three boolean readings -------------------------------------------

_NEGATION = ("ne", "bez", "zadn", "none", "false", "0")


def yes_no(value: object) -> bool | None:
    """An `Ano` / `Ne` cell, or a payload's own boolean. Anything else is unknown, never
    a guessed False — the three JSON portals state a real `false` and the six HTML ones
    mostly cannot, which is what the contract's absence axis is for."""
    if isinstance(value, bool):
        return value
    key = fold(value if isinstance(value, str) else (None if value is None else str(value)))
    if key.startswith("ano") or key in ("true", "1", "yes"):
        return True
    if key.startswith("ne") or key in ("false", "0"):
        return False
    return None


def present(value: str | None) -> bool | None:
    """A cell whose LABEL is the amenity and whose value is its size ("Balkon: 4 m²").

    Present means true unless the value negates it; absent is the contract's call."""
    if value is None:
        return None
    key = fold(value)
    if not key:
        return None
    if key.startswith(_NEGATION) or key in ("0", "0_m2"):
        return False
    return True


def states(text: str | None, *needles: str) -> bool | None:
    """A multi-value cell ("Balkon, Lodžie, Terasa") read for any of `needles`."""
    key = fold(text)
    if not key:
        return None
    # Negation FIRST: "Bez balkonu" states the absence of the very thing the needle
    # matches, and reading the needle first would turn it into a True.
    if key.startswith(_NEGATION):
        return False
    return True if any(n in key for n in needles) else None


def contains(text: str | None, *needles: str) -> bool | None:
    """A STATED list read for one of its members ("garáž , parkování na ulici").

    Unlike `mentions`, a present list that does not name the thing is the portal saying
    it is absent — which is how idnes states a garage, and the only reason `garage` has
    a real `false` anywhere outside sreality and bezrealitky."""
    if not text:
        return None
    key = fold(text)
    return any(n in key for n in needles)


def mentions(names: Iterable[str] | str | None, *needles: str) -> bool | None:
    """Any of `needles` inside a set of stated names (mmreality's accessories) or a
    free-text blob (realitymix's `ostatní`). True or unknown — a name list that does not
    mention a thing is not the portal saying the thing is absent."""
    if names is None:
        return None
    folded = fold(names) if isinstance(names, str) else " ".join(fold(n) for n in names)
    if not folded:
        return None
    return True if any(n in folded for n in needles) else None


def accessory_names(groups: Iterable[Mapping[str, object]] | None) -> set[str]:
    """mmreality states its amenities as `accessoryGroups[].accessories[].name`."""
    names: set[str] = set()
    for group in groups or []:
        for accessory in (group or {}).get("accessories") or []:  # type: ignore[union-attr]
            name = fold((accessory or {}).get("name"))  # type: ignore[union-attr]
            if name:
                names.add(name)
    return names
