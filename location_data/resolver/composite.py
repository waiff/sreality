"""Composite locality names — bound against the register, resolved up (W9).

The operator's ruling, 2026-09-14: *"I do not like this string work, it could be ambiguous.
We need to make this deterministic based on some registry with appropriate hierarchy and
apply for all the statutory cities. We should be able to match the quarter name in a
registry and then resolve up to the town name. All based on an official set of names."*

What it replaces is `claims_common.statutory_city_obec`: a regex over a hand-typed list of
eight city names that folded "Praha 4 - Podolí" to "Praha" inside the READER and dropped
the quarter on the floor. A reader states what the portal said; deciding what that names is
the resolver's job, and the resolver is the half that holds the register. The claim now
carries the portal's line verbatim and every answer here is a RÚIAN row, never a substring.

Three rules, in order:

1. **Whole string first.** The normalised line is looked up at obec, část obce and MOMC. An
   exact match at any level wins and nothing is split — which is why "Frýdek-Místek" stays the
   town it is and "Praha-Řeporyje" anchors Praha, with no special-case list anywhere.
2. **Then parts, scoped by the anchoring obec.** Only when nothing matched whole is the line
   split on the separators the portals use. A token naming exactly one obec anchors the
   search; failing that, a token naming exactly one part anchors it through its own parent
   ("Praha 4" is not an obec — it is a MOMC whose parent is Praha). The remaining tokens are
   then matched INSIDE that obec, so "Podolí" is found under Praha and not in one of the
   twelve other villages that share the name. A token matching nothing there — a street, a
   house number — is ignored.
3. **Fail closed.** An ambiguous name with nothing to anchor it binds NOTHING: the row stays
   unresolved rather than guessed into the wrong town. A line naming an okres or a kraj is a
   region, not a locality, and is refused before any split — the rail that keeps a village in
   "Brno-venkov" from being published as Brno, and it is a LEVEL test rather than the list of
   hyphenated okres names the regex had to carry.

Numbers are part of an official name here, not a shape to strip: "Praha 4", "Plzeň 3" and
"Pardubice II" are what RÚIAN calls those units, so they match as themselves. What a match
BINDS is `part_of`'s answer: a MOMC anchors its town and is never the quarter (W5, ruling Q1).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from location_data.name_index import split_qualifier
from location_data.resolver.normalize import (
    case_fold,
    deaccent,
    house_number,
    normalize_match_key,
    split_street_and_number,
    split_street_type,
    strip_street_generic,
)
from location_data.resolver.types import AdminUnit, RegistryView, Street

# The levels BELOW the town a place name is matched at, finest first (W5, ruling Q1). Every
# match anchors its town; only a část obce is ever bound (`part_of`). MOMC stays a key: 1,607
# remax ads write their town "Praha N". Správní obvod and ZSJ are no level.
MATCH_LEVELS: tuple[str, ...] = ("cast_obce", "momc")
# The whole-string ladder. The town is tried first: a name that IS a town is never read as
# somebody else's quarter.
WHOLE_LEVELS: tuple[str, ...] = ("obec", *MATCH_LEVELS)
# Regions, and never a locality. RÚIAN spells nine okresy exactly like a městský obvod
# ("Brno-venkov", "Plzeň-sever", "Praha-východ"), which is why this test exists at all.
REGION_LEVELS: tuple[str, ...] = ("okres", "kraj")

# The separators the nine portals write a composite locality with. Every Unicode dash is
# accepted because `normalize.case_fold` has usually — not always — already folded them.
_SEPARATORS = re.compile(r"[-‐-―−,]")


@dataclass(frozen=True, slots=True)
class CompositeBind:
    """What a locality line names. `reason` is the diagnostic, bound or not."""

    obec: AdminUnit | None = None
    part: AdminUnit | None = None
    reason: str = "no_match"

    @property
    def bound(self) -> bool:
        return self.obec is not None


def resolve_locality(value: str, registry: RegistryView) -> CompositeBind:
    """-> the town the line names and, where it names one, the part inside it."""
    key = normalize_match_key(value)
    if not key:
        return CompositeBind(reason="empty")

    level, units = _first_match(registry, key, WHOLE_LEVELS)
    if units:
        if len(units) > 1:
            return CompositeBind(reason=f"ambiguous_whole_string:{level}")
        return _resolve_up(units[0], registry, "whole_string")

    if _lookup(registry, key, REGION_LEVELS):
        return CompositeBind(reason="region_not_locality")

    tokens = _tokens(value)
    if len(tokens) < 2:
        return CompositeBind(reason="no_match")

    town_only: AdminUnit | None = None
    for index, unit in _anchor_candidates(tokens, registry):
        obec = obec_of(unit, registry)
        if obec is None:
            continue
        part = _part_inside(tokens, index, obec, registry) or part_of(unit, obec.code, registry)
        if part is not None:
            return CompositeBind(obec=obec, part=part, reason="anchored_part")
        if town_only is None:
            town_only = obec
    if town_only is None:
        return CompositeBind(reason="no_anchor")
    return CompositeBind(obec=town_only, reason="anchor_only")


def _resolve_up(unit: AdminUnit, registry: RegistryView, reason: str) -> CompositeBind:
    obec = obec_of(unit, registry)
    if obec is None:
        return CompositeBind(reason="no_obec_ancestor")
    return CompositeBind(obec=obec, part=part_of(unit, obec.code, registry), reason=reason)


def _anchor_candidates(
    tokens: Sequence[str], registry: RegistryView
) -> list[tuple[int, AdminUnit]]:
    """Every token that fixes a town on its own, obec tokens first and then part tokens,
    each in the order the line writes them.

    A token matching SEVERAL obce settles nothing and is dropped rather than aborting the
    line — a token naming thirteen places is not evidence, and the rest of the line may
    still be. A part token is a candidate only when its name is unique across the whole
    register at that level, which is rule (c): with nothing to anchor it, a part binds only
    if there is exactly one place it can mean.

    The caller prefers the candidate under which ANOTHER token also resolves inside the same
    town, and that preference is what "Praha 4 - Podolí" needs: there is a village called
    Podolí, so the second token names exactly one obec and would otherwise anchor the line
    240 km from the listing. Reading the first token as the MOMC it is explains both halves;
    reading the second as the village explains one.
    """
    obec_hits: list[tuple[int, AdminUnit]] = []
    part_hits: list[tuple[int, AdminUnit]] = []
    for index, token in enumerate(tokens):
        key = normalize_match_key(token)
        towns = _lookup(registry, key, ("obec",))
        if len(towns) == 1:
            obec_hits.append((index, towns[0]))
            continue
        if towns:
            continue
        _, parts = _first_match(registry, key, MATCH_LEVELS)
        if len(parts) == 1:
            part_hits.append((index, parts[0]))
    return [*obec_hits, *part_hits]


def _part_inside(
    tokens: Sequence[str], anchor_index: int, obec: AdminUnit, registry: RegistryView
) -> AdminUnit | None:
    """The one část obce another token names INSIDE the anchoring town (two settle nothing)."""
    for index, token in enumerate(tokens):
        if index == anchor_index:
            continue
        inside = [unit for unit in _lookup(registry, normalize_match_key(token), ("cast_obce",))
                  if town_code(unit, registry) == obec.code]
        if len(inside) == 1:
            return inside[0]
    return None


def part_of(unit: AdminUnit, town: int, registry: RegistryView) -> AdminUnit | None:
    """The část a match binds (W5, Q1/Q2): a část itself; a KÚ the one část of its obec its
    qualifier-free name spells ("Podolí u Mohelnice" -> Podolí); a MOMC or the obec nothing."""
    if unit.level == "cast_obce":
        return unit
    if unit.level != "katastralni_uzemi":
        return None
    key = normalize_match_key(split_qualifier(unit.name)[0])
    spelled = [u for u in _lookup(registry, key, ("cast_obce",))
               if town_code(u, registry) == town]
    return spelled[0] if len(spelled) == 1 else None


def town_code(unit: AdminUnit, registry: RegistryView) -> int | None:
    """The unit's obec code: the ltree path's for free, the chain's where a view has none."""
    return unit.obec_kod or getattr(obec_of(unit, registry), "code", None)


def obec_of(unit: AdminUnit, registry: RegistryView) -> AdminUnit | None:
    if unit.level == "obec":
        return unit
    for ancestor in registry.admin_chain(unit.unit_id):
        if ancestor.level == "obec":
            return ancestor
    return None


def _first_match(
    registry: RegistryView, key: str, levels: Sequence[str]
) -> tuple[str | None, list[AdminUnit]]:
    for level in levels:
        units = _lookup(registry, key, (level,))
        if units:
            return level, units
    return None, []


def _lookup(registry: RegistryView, key: str, levels: Sequence[str]) -> list[AdminUnit]:
    """Deduped by unit id: `ruian_name_index` carries several spellings per unit, so one
    name asked once can come back as the same unit twice."""
    if not key:
        return []
    found: dict[int, AdminUnit] = {}
    for unit in registry.admin_units_by_name(key, levels=levels):
        found.setdefault(unit.unit_id, unit)
    return [found[k] for k in sorted(found)]


def _tokens(value: str) -> list[str]:
    return [token for token in (part.strip() for part in _SEPARATORS.split(value)) if token]


# --------------------------------------------------------------------------- streets (W18)
#
# The SAME doctrine, one level down: a street claim is matched against the REGISTER inside the
# anchoring town, and fails closed when more than one row matches. (v5.6 deleted the split on
# separators — its one user was bazos' whole headline, which the text reading replaced.)
#
# Two rules the locality binder does not need, and each is a measured one:
#
#  1. BOTH KEYS. `ruian_streets.name_norm` comes from `name_index.normalize_street_name`,
#     which drops a leading `ulice`/`ul.` and nothing else — so the register says
#     `namesti miru` while S1's `split_street_type` hands the resolver `miru`. Matching both
#     forms is what makes the two sides symmetric without touching the loader, and it is
#     worth 215 titles that bind only with the generic word kept.
#  2. NOT A PLACE. A claim naming the anchoring obec, or a část obce / MOMC inside it, is
#     never a street candidate — 76 register streets across 20 obce are spelled exactly like a
#     část obce of their own town. Since v5.7 a line naming exactly one část of the town IS
#     that část (`named_part`): "Nový Svět" in Harrachov is the village whose č.p. 701 it is.
#     A line naming a MOMC names no part (W5) and is still no street: R3 never reads it.
#
# Two more since v5.7, each measured on the register (2026-10-14):
#
#  3. SPELLED. `name_norm` is accent-free, so 14 keys in 6 obce reach two streets each (9 in
#     Praha: Hanusova in Michle, Hanušova in Kbely); all 14 pairs are spelled differently. A
#     claim WITH diacritics spelled letter for letter like one of its own twins names that one
#     (`StreetBind.spelled`); an accent-free one may be either — four such keys have an
#     accent-free member (Hanusova, Muzikova, Radova ×2) — and BIND asks the part first.
#  4. ABBREVIATED. `_ABBREVIATIONS` folds a portal's short form onto the register's on BOTH
#     sides: 1,001 register streets change key and no two streets of one obec share a key
#     that did not share one before (per entry: csa 19, ceskoslovenske armady 45, nam 597, tr
#     60, nabr 27, gen 85, kpt 126, plk 25, sidl 30 streets — 0 new collisions each).
_ABBREVIATIONS = tuple((re.compile(rf"\b{short}\b"), full) for short, full in (
    ("ceskoslovenske armady", "cs armady"), ("csa", "cs armady"), ("nam", "namesti"),
    ("tr", "trida"), ("nabr", "nabrezi"), ("gen", "generala"), ("kpt", "kapitana"),
    ("plk", "plukovnika"), ("sidl", "sidliste")))


def fold_abbreviations(key: str) -> str:
    for pattern, full in _ABBREVIATIONS:
        key = pattern.sub(full, key)
    return key


@dataclass(frozen=True, slots=True)
class StreetBind:
    """What the street claims name inside the anchoring obec. `reason` is the diagnostic;
    `candidates` are the streets an `ambiguous_streets` claim could mean, each bound as if alone
    (BIND tie-breaks among them: the part, then `spelled`), and `named_part` the one part a line
    names."""

    street: Street | None = None
    cislo_domovni: int | None = None
    typ_so: str | None = None
    cislo_orientacni: int | None = None
    reason: str = "no_match"
    candidates: tuple[StreetBind, ...] = ()
    named_part: AdminUnit | None = None
    spelled: bool = False

    @property
    def bound(self) -> bool:
        return self.street is not None


def street_match_keys(value: str) -> tuple[frozenset[str], frozenset[str]]:
    """-> (the EXACT keys, every key this name may match by).

    The exact keys are the name as the portal wrote it and the same name minus the meaningless
    `ulice`/`ul.` wrapper. Both are "as written": neither drops a word that can be part of an
    official name, so either may claim an exact hit. The UNFOLDED form is in there for a
    measured reason — the register holds `Nová ulice` ×8, `V Ulici`, `Na Ulici`, `Horní
    Ulice`, `Husova ulice` and `I. ulice`…`IX. ulice`, and folding the generic word off those
    leaves a key that can never bind.

    The wider set adds the TYPE-word-stripped form, which is what lets a claim reach a
    register row spelled the other way: `nám. Míru` normalises to `nam miru`, matches the
    register's `namesti miru` at neither end, and both strip to `miru`.

    The tiers are kept apart because collapsing them is wrong in a measurable way: 45 register
    keys across 32 obce collide once the type word is dropped (Kladno holds `náměstí Svobody`
    AND `Svobody`, Děčín `5. května` AND `Nám. 5. května`). On one flat set a correct exact
    claim reads as two candidates; on two tiers it reads as one.
    """
    verbatim, _ = split_street_and_number((value or "").strip())
    unwrapped, _ = split_street_and_number(strip_street_generic(value))
    exact = {_key(verbatim), _key(unwrapped)}
    without_type, _ = split_street_type(unwrapped or verbatim)
    every = exact | {_key(without_type)}
    return (frozenset(k for k in exact if k), frozenset(k for k in every if k))


def _key(value: str) -> str:
    return fold_abbreviations(normalize_match_key(value))


def register_street_keys(street: Street) -> frozenset[str]:
    """The same pair, taken off the REGISTER row. `ruian_streets.name_norm` is built by
    `name_index.normalize_street_name`, which drops a leading `ulice`/`ul.` and keeps
    everything else — so the fold is made symmetric HERE, in the resolver, rather than by
    re-normalising 83,451 rows in the loader for a question only the matcher asks."""
    stripped, _ = split_street_type(street.name)
    return frozenset(k for k in {fold_abbreviations(street.name_norm), _key(stripped)} if k)


def build_street_index(streets: Sequence[Street]) -> dict[str, list[Street]]:
    """`key -> the streets it reaches`, folded ONCE per obec instead of once per street per
    claim. `register_street_keys` is two regex passes and Praha holds ~10,000 streets, so a
    four-segment title paid for 40,000 of them before this existed."""
    index: dict[str, list[Street]] = {}
    for street in streets:
        for key in register_street_keys(street):
            index.setdefault(key, []).append(street)
    return index


def street_index(registry: RegistryView, obec_kod: int) -> dict[str, list[Street]]:
    """The obec's index, from the view's own memo when it offers one.

    `street_index` is an ACCELERATOR, not an eleventh protocol question: the answer is
    identical either way (`build_street_index` over the same `streets_in_obec`), so a view
    that does not offer it simply pays the fold per listing. `CachedRegistryView` does offer
    it, which is what makes the fold once-per-obec-per-RUN in production.
    """
    memo = getattr(registry, "street_index", None)
    if memo is not None:
        return memo(obec_kod)
    return build_street_index(registry.streets_in_obec(obec_kod))


def match_streets(
    value: str, obec_kods: Sequence[int], registry: RegistryView
) -> tuple[list[Street], str]:
    """-> (the register rows this ONE name reaches, which tier reached them).

    TWO TIERS, and the order is the whole point. An EXACT full-name match — the claim's own
    spelling equalling `ruian_streets.name_norm` — wins outright and is never weighed against
    a type-word-tolerant one: without that, a Kladno claim of `náměstí Svobody` would reach
    both `náměstí Svobody` and `Svobody` and fail closed on a correct answer. Only when
    nothing matches exactly is the tolerant fold consulted, and only there can two rows be a
    genuine ambiguity.
    """
    exact_keys, keys = street_match_keys(value)
    exact: dict[int, Street] = {}
    tolerant: dict[int, Street] = {}
    for obec_kod in obec_kods:
        index = street_index(registry, obec_kod)
        for key in keys:
            for street in index.get(key, ()):
                if fold_abbreviations(street.name_norm) in exact_keys:
                    exact.setdefault(street.code, street)
                else:
                    tolerant.setdefault(street.code, street)
    if exact:
        return [exact[c] for c in sorted(exact)], "exact"
    return [tolerant[c] for c in sorted(tolerant)], "stripped"


def resolve_street(
    line: str | None, obec_kods: Sequence[int], registry: RegistryView,
    *, place_line: str | None = None,
) -> StreetBind:
    """-> the ONE street the street claim names inside the constraining obec, or nothing.

    A line that reaches two distinct streets names neither. Two SPELLINGS of one register row
    are one answer, which is why the set is keyed on the street's code. `place_line` is a
    street claim S1 refused as a place name: never a street, asked only which part of the town
    it names.
    """
    found: dict[int, tuple[Street, dict[str, object]]] = {}
    spelled: int | None = None
    parts: dict[int, AdminUnit] = {}
    keys = street_match_keys(line)[1] if line else ()
    if line and keys:
        streets, _ = match_streets(line, obec_kods, registry)
        # The place test is asked AFTER the match and only about a claim that matched: asked
        # first, prose keys would fill a `RunCache` that CLEARS ITSELF at 250,000 entries.
        places = places_named(keys, {s.obec_kod for s in streets}, registry) if streets else []
        if places:
            parts.update(_parts_of_town(places))
        else:
            _, numbers = split_street_and_number(strip_street_generic(line))
            found = {street.code: (street, numbers) for street in streets}
            spelled = _spelled_like(line, streets)
    if place_line:
        parts.update(_parts_of_town(
            places_named(street_match_keys(place_line)[1], set(obec_kods), registry)))
    named_part = _one_part(parts)
    if not found:
        return StreetBind(reason="street_names_part" if parts else "no_match",
                          named_part=named_part)
    if len(found) > 1:
        return StreetBind(reason="ambiguous_streets", named_part=named_part, candidates=tuple(
            replace(_bound(*found[code]), spelled=spelled == code) for code in sorted(found)))
    return _bound(*found[next(iter(found))], named_part=named_part)


def _spelled_like(line: str, streets: Sequence[Street]) -> int | None:
    if len(streets) < 2 or deaccent(line) == line:
        return None
    names = {case_fold(split_street_and_number(text)[0])
             for text in (line, strip_street_generic(line))}
    spelled = [s.code for s in streets if case_fold(s.name) in names]
    return spelled[0] if len(spelled) == 1 else None


def _bound(street: Street, numbers: dict[str, object], *,
           named_part: AdminUnit | None = None) -> StreetBind:
    cislo_domovni, typ_so = house_number(numbers)
    return StreetBind(
        street=street, cislo_domovni=cislo_domovni, typ_so=typ_so,
        cislo_orientacni=_as_int(numbers.get("cislo_orientacni")), reason="street_match",
        named_part=named_part,
    )


def places_named(
    keys: frozenset[str], obec_kods: set[int], registry: RegistryView
) -> list[AdminUnit]:
    """The town itself, or the parts of it, these keys name. `normalize.normalize_claim`'s
    `town_as_street` rule covers the town, when S1 is handed the place lookup; the collision is
    real at part level too (76 register streets across 20 obce are spelled exactly like a část
    obce of their own town)."""
    found: dict[int, AdminUnit] = {}
    for key in keys:
        for unit in registry.admin_units_by_name(key, levels=WHOLE_LEVELS):
            # Through the CHAIN, not through `AdminUnit.obec_kod`: that column is filled off
            # the ltree path by the SQL view and is empty on any other `RegistryView`, so
            # reading it here would make the rail fire in production and nowhere else.
            town = obec_of(unit, registry)
            if town is not None and town.code in obec_kods:
                found.setdefault(unit.unit_id, unit)
    return list(found.values())


def _parts_of_town(places: Sequence[AdminUnit]) -> dict[int, AdminUnit]:
    """The places below the town a line names (a MOMC included); naming the town names none."""
    if any(unit.level == "obec" for unit in places):
        return {}
    return {u.unit_id: u for u in places}


def _one_part(parts: dict[int, AdminUnit]) -> AdminUnit | None:
    """The one část the lines name: a MOMC spelled alike (Ostrava's Poruba) is a key, never the
    part, and the část is the level a streetless house number is unique in."""
    found = [u for u in parts.values() if u.level == "cast_obce"]
    return found[0] if len(found) == 1 else None


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
