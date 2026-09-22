"""The PRODUCER side of the one canonical vocabulary (field-capture R5).

The canon itself — what a `condition` or a `building_type` may BE — lives in
`toolkit.filter_registry`, which already generates the SPA's option lists and the API
schema under a CI drift gate. This module never restates it; it imports it and owns the
other half: turning what a portal WROTE into one of those values.

  * `fold` — one diacritic/whitespace/`… stav` normalisation, replacing the nine
    near-identical `_strip_diacritics(...).lower()` chains.
  * `canonical` — ONE `(field, portal label) -> canonical` registry. A label no entry
    names is NULL **and a counted event** (`take_unmapped`), never a passthrough: the
    passthrough `.get(key, key)` idiom is how 13 `condition` spellings and 15
    `building_type` values reached a column whose filter offers 6 and 8.
  * `disposition` / `energy_rating` — ONE grammar each, replacing nine disposition
    regexes and seven energy-class regexes.
  * `yes_no` / `present` / `contains` / `mentions` / `parking` — the five boolean readings
    a portal actually uses, from an `Ano` cell to a set of accessory names. They differ in what
    SILENCE means, which is a live fact per portal and not a thing to unify by fiat. What
    a MISSING KEY means is not decided here at all — absence is a per-(portal, field) fact
    the attribute contract declares. SIX portals can write an explicit `false` after W4:
    sreality and bezrealitky from a real payload boolean, ceskereality and realitymix from
    a stated list that does not name the thing (`contains` / `parking`), idnes from a cross
    icon or a parking cell naming only the street, and mmreality from a stated group.
  * `any_true` — the ONE union over those readings (W4). `has_balcony` is balcony OR
    loggia and `has_parking` is a space or right BELONGING to the property, on all nine
    portals; which KEYS carry those facts is the contract's business, and combining them
    was previously four separate definitions (`parser._any_of`, `idnes._any_true`, and the
    `or` chains in maxima and remax that turned an explicit `False` into NULL).

**The canon is the whole value space (W5).** There is no legacy tier any more: every
value a live row carries is either a canonical member (`ve_vystavbe`, `projekt`,
`udrzovany`, building_type `jina`, ownership `jine`, a 6+kk disposition — all real facts
the filters had no slot for) or a spelling of one, collapsed here. So `CANON` is exactly
what this module may emit, and the LLM tool schema generated from it offers exactly that.
"""

from __future__ import annotations

import re
from collections import Counter
from threading import Lock
from typing import Iterable, Mapping
from unicodedata import combining, normalize

from toolkit.filter_registry import COLUMN_CANONICAL_VALUES

# The canon, read from the consumer-side registry — one list, never a second spelling.
CANON: dict[str, frozenset[str]] = {
    field: frozenset(COLUMN_CANONICAL_VALUES[field])
    for field in ("condition", "building_type", "ownership", "furnished",
                  "energy_rating", "disposition", "price_unit")
}


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
        # realitymix and remax slugify the whole dropdown label, parentheses and all.
        "ve_vystavbe_(hruba_stavba)": "ve_vystavbe",
        "projekt": "projekt", "project": "projekt",
        "v_rekonstrukci": "v_rekonstrukci", "in_reconstruction": "v_rekonstrukci",
        "spatny": "spatny", "bad": "spatny",
        "udrzovany": "udrzovany",
        "urceny_k_demolici": "k_demolici",
    },
    "building_type": {
        "cihlova": "cihla", "cihla": "cihla", "zdena": "cihla", "brick": "cihla",
        "panelova": "panel", "panel": "panel",
        "smisena": "smisena", "mixed": "smisena",
        # ceskereality has no "smíšená" option and states two materials in one cell
        # instead ("Zděná, kamenná"); two materials IS mixed construction, and folding
        # the pair onto its first member would throw the second fact away.
        "zdena_kamenna": "smisena", "drevena_zdena": "smisena",
        "skeletova": "skelet", "skelet": "skelet",
        "drevena": "drevo", "drevostavba": "drevo", "wood": "drevo",
        "kamenna": "kamen", "stone": "kamen",
        "montovana": "montovana", "prefab": "montovana",
        "nizkoenergeticka": "nizkoenergeticka",
        "jina": "jina", "jine": "jina", "ostatni": "jina",
        "ocelova": "ocelova", "roubena": "roubena", "modularni": "modularni",
    },
    "ownership": {
        "osobni": "osobni", "soukrome": "osobni",
        "druzstevni": "druzstevni",
        "statni": "statni", "obecni": "statni", "statni_obecni": "statni",
        "statni_obecni_jine": "statni",
        # Every regime outside the three. idnes names two of them outright ("s.r.o.",
        # "podílové") and four portals used to drop the bucket entirely.
        "jine": "jine", "ostatni": "jine", "s.r.o.": "jine", "podilove": "jine",
    },
    # Both stems in both genders: the five deleted `_norm_furnished` matchers keyed on the
    # substrings "zariz" / "vybav" / "castec", so every inflection a portal renders had to
    # land here or a label that used to map would start writing NULL.
    "furnished": {
        "ano": "ano", "zarizeny": "ano", "zarizeno": "ano",
        "vybaveny": "ano", "vybaveno": "ano",
        "ne": "ne", "nezarizeny": "ne", "nezarizeno": "ne",
        "nevybaveny": "ne", "nevybaveno": "ne",
        "castecne": "castecne", "castecne_zarizeny": "castecne",
        "castecne_zarizeno": "castecne", "castecne_vybaveny": "castecne",
        "castecne_vybaveno": "castecne",
    },
}

# Labels a portal states that mean "no value", not a value. Refused here rather than
# NULLed silently, so they never count as unmapped: the operator has decided about them.
_REFUSED: dict[str, frozenset[str]] = {
    "condition": frozenset({"undefined", "neuvedeno", "rezervovano", "prodano"}),
    "building_type": frozenset({"undefined", "neuvedeno", "rezervovano", "prodano"}),
    "ownership": frozenset({"undefined", "neuvedeno"}),
    "furnished": frozenset({"undefined", "neuvedeno"}),
}

# Where one portal reads a label differently from the rest.
_PORTAL_LABELS: dict[tuple[str, str], dict[str, str]] = {
    # mmreality states `equipment` as 1|2|3 and renders no "Vybavení" row on the page, so
    # the codebook was settled against the ads' own words on the rental slice (n=1,246):
    # code 1 reads "plně/kompletně vybaven" on 29.8% of its rows, code 2 "nevybaven" on
    # 4.9%, code 3 "částečně vybaven" on 9.0% — each code's modal cue, and each the
    # maximum for that cue across the three codes.
    ("mmreality", "furnished"): {"1": "ano", "2": "ne", "3": "castecne"},
}

# Per-PASS, not per-process: `take_unmapped` drains it, because the always-on worker calls
# `run_detail_drain` in one long-lived process, once per source, every pass — a counter that
# only ever grew would report portal A's stale label on portal B's summary forever. The lock
# is the drain's parse fan-out (`ThreadPoolExecutor`), where `+= 1` can lose an increment.
UNMAPPED: Counter[str] = Counter()
_UNMAPPED_LOCK = Lock()


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
        with _UNMAPPED_LOCK:
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


def refuse(field: str, portal: str, label: str) -> None:
    """Count a stated value the parse REFUSES rather than stores.

    The same channel as an unmapped label, because it is the same event: a value the
    portal published that reaches no column, visible in the run summary instead of
    silently gone. bezrealitky's EUR rents are the first customer (W4) — a non-CZK amount
    in `price_czk` reads ~25x low, and converting it would invent an exchange rate."""
    with _UNMAPPED_LOCK:
        UNMAPPED[f"{field}/{portal}/{fold(label)}"] += 1


def take_unmapped() -> list[tuple[str, int]]:
    """`(field/portal/label, count)` since the last call, worst first, then reset."""
    with _UNMAPPED_LOCK:
        events = UNMAPPED.most_common()
        UNMAPPED.clear()
    return events


# --- the two grammars ------------------------------------------------------

# One disposition grammar, and it is the Czech one: N rooms plus a kitchenette (`kk`) or
# a separate kitchen (`1`). The second term can be nothing else and the first cannot be
# zero, which the `\d\+(kk|\d)` every portal used to carry did not say — so bazos's
# free-text mining wrote 0+1, 4+2, 8+7 and 9+5 into the column (230 active rows across 28
# impossible values). A near-miss is now refused and COUNTED, not stored.
_DISPOSITION_RE = re.compile(r"\b([1-9])\s*\+\s*(kk|1)\b", re.IGNORECASE)
_DISPOSITION_NEAR_MISS_RE = re.compile(r"\b(\d)\s*\+\s*(kk|\d)\b", re.IGNORECASE)
# bezrealitky states its disposition as a code, not as text.
_DISP_CODE_RE = re.compile(r"DISP_(\d)_(KK|1|IZB)")
_ENERGY_RE = re.compile(r"\b([A-G])\b")


def disposition(portal: str, *texts: str | None) -> str | None:
    """The first grammatical `N+kk` / `N+1` any of the texts states, in the caller's order.

    A digit pair that is not a disposition ("4+2", "0+1") is refused and counted rather
    than stored: on bazos the haystack is the advert's own prose, where a bare "8+7" is
    far more likely to be a phone number or a dimension than a flat."""
    for text in texts:
        if not text:
            continue
        match = _DISPOSITION_RE.search(str(text))
        if match:
            return f"{match.group(1)}+{match.group(2).lower()}"
        near = _DISPOSITION_NEAR_MISS_RE.search(str(text))
        if near:
            refuse("disposition", portal, near.group(0))
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
    measurement — but NO portal marks which (W5 measured all nine: every one spells its
    G as the ordinary class label, "G - Mimořádně nehospodárná" or idnes's decree
    citation). So it cannot be split here, and stays a measure-validity fact the fill
    matrix reports."""
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


def present(value: object) -> bool | None:
    """A cell whose LABEL is the amenity and whose value is its size ("Balkon: 4 m²").

    Present means true unless the value negates it; absent is the contract's call. The
    size arrives as text on the HTML portals and as a JSON number on bezrealitky and
    mmreality, which is why this takes an object and folds it rather than a `str`: a
    numeric 0 is a stated absence like the string "0", which `fold` would swallow."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value > 0
    key = fold(value)
    if not key:
        return None
    if key.startswith(_NEGATION) or key in ("0", "0_m2"):
        return False
    return True


def contains(text: str | None, *needles: str) -> bool | None:
    """A STATED list read for one of its members ("garáž , parkování na ulici").

    Unlike `mentions`, a present list that does not name the thing is the portal saying
    it is absent — which is how idnes states a garage. Negation is read per MEMBER, not
    per cell: "Bez balkonu" negates the needle it carries, while "Bezbarierový přístup,
    Výtah" (138 live realitymix rows) states a lift beside a member that merely begins
    like a negation, and a whole-cell prefix test would read it as no lift."""
    if not text:
        return None
    return any(
        any(n in key for n in needles) and not key.startswith(_NEGATION)
        for key in (fold(part) for part in str(text).split(","))
    )


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


# R11's ONE reading of `has_parking`: a space or right BELONGING to the property. Every
# portal states its parking as a facility OF THE LISTING, so the discriminator is not a
# per-portal inclusion list but the explicit not-ours qualifier the live vocabularies
# share — the street and a car park merely nearby. mmreality's literal "Není" (51 live
# rows) needs no qualifier: it names no parking at all, so the inclusion test already
# drops it; `neni` covers a member that names parking AND negates it in one cell.
_PARKING_WORDS = ("parkov", "garaz", "stani", "pristresek")
_PARKING_NOT_OURS = ("na_ulici", "pobliz", "v_okoli", "neni")


def parking(members: Iterable[str] | str | None) -> bool | None:
    """Whether the property's OWN parking is among the stated facilities.

    `members` is one portal's stated parking: a multi-value cell ("Garáž, Parkování na
    ulici"), a set of accessory names, or a single label. Split on the comma BEFORE
    folding, because `fold` turns both the separator and the spaces into `_`. None when
    the portal stated nothing; False when it listed its facilities and none of them comes
    with the unit — which is the only way six of the nine portals can say "no parking"."""
    if members is None:
        return None
    parts = members.split(",") if isinstance(members, str) else list(members)
    listed = [key for key in (fold(p) for p in parts) if key]
    if not listed:
        return None
    return any(
        any(w in key for w in _PARKING_WORDS)
        and not any(q in key for q in _PARKING_NOT_OURS)
        for key in listed
    )


def any_true(*values: bool | None) -> bool | None:
    """The ONE union of related boolean signals: None only while every one is silent.

    A `False` among them is a stated absence and must survive — `a or b` collapses
    `False or None` to None, which is how remax and maxima turned "Parkování: Ne" into
    "nobody said"."""
    if all(v is None for v in values):
        return None
    return any(v is True for v in values)


def accessory_names(
    groups: Iterable[Mapping[str, object]] | None, *, group: str | None = None,
) -> set[str] | None:
    """mmreality's `accessoryGroups[].accessories[].name`, optionally ONE group's.

    The group is half the fact: the portal files "Parkování na ulici" and "Parkoviště
    poblíž" (street / a nearby car park) under `Parkování`, and "Parkety" (parquet
    FLOORING) under `Podlahy` — a flattened name search read all three as parking. With a
    group named, None means the portal did not state that group at all, and an empty set
    means it stated the group and listed nothing our reading recognises."""
    if groups is None:
        return None
    names: set[str] = set()
    seen_group = group is None
    for entry in groups or []:
        if group is not None and fold((entry or {}).get("name")) != fold(group):
            continue
        seen_group = True
        for accessory in (entry or {}).get("accessories") or []:  # type: ignore[union-attr]
            name = fold((accessory or {}).get("name"))  # type: ignore[union-attr]
            if name:
                names.add(name)
    return names if seen_group else None
