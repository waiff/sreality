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

1. **Whole string first.** The normalised line is looked up at obec, část obce, MOMC and
   správní obvod. An exact match at any level wins and nothing is split — which is why
   "Frýdek-Místek" stays the town it is and "Praha-Řeporyje" binds the MOMC and resolves up
   to Praha, with no special-case list anywhere.
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
"Pardubice II" are what RÚIAN calls those units, so they match as themselves.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from location_data.resolver.normalize import (
    STREET_LINE_SEPARATOR,
    normalize_match_key,
    split_street_and_number,
    split_street_type,
    strip_street_generic,
)
from location_data.resolver.types import AdminUnit, RegistryView, Street

# The levels a locality line may name BELOW the town, finest first.
PART_LEVELS: tuple[str, ...] = ("cast_obce", "momc", "spravni_obvod")
# The whole-string ladder. The town is tried first: a name that IS a town is never read as
# somebody else's quarter.
WHOLE_LEVELS: tuple[str, ...] = ("obec", *PART_LEVELS)
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
        obec = _obec_of(unit, registry)
        if obec is None:
            continue
        part = _part_inside(tokens, index, obec, registry)
        if part is None and unit.level != "obec":
            part = unit
        if part is not None:
            return CompositeBind(obec=obec, part=part, reason="anchored_part")
        if town_only is None:
            town_only = obec
    if town_only is None:
        return CompositeBind(reason="no_anchor")
    return CompositeBind(obec=town_only, reason="anchor_only")


def _resolve_up(unit: AdminUnit, registry: RegistryView, reason: str) -> CompositeBind:
    obec = _obec_of(unit, registry)
    if obec is None:
        return CompositeBind(reason="no_obec_ancestor")
    return CompositeBind(obec=obec, part=None if unit.level == "obec" else unit, reason=reason)


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
        _, parts = _first_match(registry, key, PART_LEVELS)
        if len(parts) == 1:
            part_hits.append((index, parts[0]))
    return [*obec_hits, *part_hits]


def _part_inside(
    tokens: Sequence[str], anchor_index: int, obec: AdminUnit, registry: RegistryView
) -> AdminUnit | None:
    """The finest unit another token names INSIDE the anchoring town."""
    for index, token in enumerate(tokens):
        if index == anchor_index:
            continue
        key = normalize_match_key(token)
        if not key:
            continue
        for level in PART_LEVELS:
            inside = [
                unit
                for unit in _lookup(registry, key, (level,))
                if (parent := _obec_of(unit, registry)) is not None and parent.code == obec.code
            ]
            if len(inside) == 1:
                return inside[0]
            if inside:
                break  # ambiguous inside one town: this token settles nothing
    return None


def _obec_of(unit: AdminUnit, registry: RegistryView) -> AdminUnit | None:
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
# The SAME doctrine, one level down. A portal states a street inside a line as readily as it
# states a quarter inside one — bazos' headline is "Prodej bytu 3+1, ul. Jiráskova, Mladá
# Boleslav" and its parser's own reading is "Kladno - Dubí, Ke Křížku" — so the answer is the
# one W9 already gave for localities: split on the separators the portals write, match each
# segment against the REGISTER inside the anchoring town, and fail closed when more than one
# thing matches.
#
# Three rules the locality binder does not need, and each is a measured one:
#
#  1. BOTH KEYS. `ruian_streets.name_norm` comes from `name_index.normalize_street_name`,
#     which drops a leading `ulice`/`ul.` and nothing else — so the register says
#     `namesti miru` while S1's `split_street_type` hands the resolver `miru`. Matching both
#     forms is what makes the two sides symmetric without touching the loader, and it is
#     worth 215 titles that bind only with the generic word kept.
#  2. NO TRIGRAM. `bind`'s R3 exists for a single claimed name with a typo in it. Run over the
#     segments of a whole title it would fuzzy-match "Prodej bytu" against a street, so a line
#     binds EXACTLY or not at all. A title cut at bazos' 60-character cap ("ul. Vršo") is the
#     same rule seen from the other side: a truncated stem is ambiguous by nature and there is
#     no prefix matching here.
#  3. NOT A PLACE. A segment naming the anchoring obec, or a část obce / MOMC inside it, is
#     never a street candidate — 76 register streets across 20 obce are spelled exactly like a
#     část obce of their own town, and on a line there is nothing to tell them apart.
_STREET_SEPARATORS = STREET_LINE_SEPARATOR


@dataclass(frozen=True, slots=True)
class StreetBind:
    """What a street LINE names inside the anchoring obec. `reason` is the diagnostic."""

    street: Street | None = None
    cislo_domovni: int | None = None
    cislo_orientacni: int | None = None
    znak_orientacniho: str | None = None
    reason: str = "no_match"

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
    exact = {normalize_match_key(verbatim), normalize_match_key(unwrapped)}
    without_type, _ = split_street_type(unwrapped or verbatim)
    every = exact | {normalize_match_key(without_type)}
    return (frozenset(k for k in exact if k), frozenset(k for k in every if k))


def street_keys(value: str) -> frozenset[str]:
    """Every key one name may match by — `street_match_keys` without the tier."""
    return street_match_keys(value)[1]


def register_street_keys(street: Street) -> frozenset[str]:
    """The same pair, taken off the REGISTER row. `ruian_streets.name_norm` is built by
    `name_index.normalize_street_name`, which drops a leading `ulice`/`ul.` and keeps
    everything else — so the fold is made symmetric HERE, in the resolver, rather than by
    re-normalising 83,451 rows in the loader for a question only the matcher asks."""
    stripped, _ = split_street_type(street.name)
    return frozenset(k for k in {street.name_norm, normalize_match_key(stripped)} if k)


def build_street_index(streets: Sequence[Street]) -> dict[str, list[Street]]:
    """`key -> the streets it reaches`, folded ONCE per obec instead of once per street per
    segment. `register_street_keys` is two regex passes and Praha holds ~10,000 streets, so a
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
                if street.name_norm in exact_keys:
                    exact.setdefault(street.code, street)
                else:
                    tolerant.setdefault(street.code, street)
    if exact:
        return [exact[c] for c in sorted(exact)], "exact"
    return [tolerant[c] for c in sorted(tolerant)], "stripped"


def resolve_street(
    lines: Sequence[str], obec_kods: Sequence[int], registry: RegistryView
) -> StreetBind:
    """-> the ONE street the lines name inside the constraining obec, or nothing.

    Distinct across every line and every segment, because a line that names two streets is a
    line nobody can resolve: "Sokolovská, roh Křižíkovy" is two answers and neither is the
    listing's. Two SPELLINGS of one register row are one answer, which is why the set is keyed
    on the street's code.
    """
    tiers: dict[str, dict[int, tuple[Street, dict[str, object]]]] = {"exact": {}, "stripped": {}}
    for line in lines:
        for segment in _street_segments(line):
            _, keys = street_match_keys(segment)
            if not keys:
                continue
            streets, tier = match_streets(segment, obec_kods, registry)
            # The place test is asked AFTER the match and only about a segment that matched.
            # Asked first it ran `admin_units_by_name` on every word of every title — prose
            # keys, unique per listing, straight into a `RunCache` that CLEARS ITSELF at
            # 250,000 entries and would have evicted the streets and chains the run lives on.
            if not streets or _names_a_place(keys, {s.obec_kod for s in streets}, registry):
                continue
            _, numbers = split_street_and_number(strip_street_generic(segment))
            for street in streets:
                tiers[tier].setdefault(street.code, (street, numbers))
    # The EXACT tier is consulted alone when it has anything at all, across every segment of
    # every line: a title that names one street exactly and grazes another through the
    # tolerant fold has named one street.
    found = tiers["exact"] or tiers["stripped"]
    if not found:
        return StreetBind(reason="no_match")
    if len(found) > 1:
        return StreetBind(reason="ambiguous_streets")
    street, numbers = found[next(iter(sorted(found)))]
    return StreetBind(
        street=street,
        cislo_domovni=_as_int(numbers.get("cislo_domovni")),
        cislo_orientacni=_as_int(numbers.get("cislo_orientacni")),
        znak_orientacniho=(str(numbers["znak_orientacniho"])
                           if numbers.get("znak_orientacniho") else None),
        reason="segment_match",
    )


def _street_segments(line: str) -> list[str]:
    return [seg for seg in (part.strip() for part in _STREET_SEPARATORS.split(line or "")) if seg]


def _names_a_place(
    keys: frozenset[str], obec_kods: set[int], registry: RegistryView
) -> bool:
    """The town itself, or a part of it. `normalize.normalize_claim`'s `town_as_street` rule
    covers a claim that IS one name; a segment of a line never reaches it, and the collision
    is real at part level too (76 register streets across 20 obce are spelled exactly like a
    část obce of their own town)."""
    for key in keys:
        for unit in registry.admin_units_by_name(key, levels=("obec", *PART_LEVELS)):
            # Through the CHAIN, not through `AdminUnit.obec_kod`: that column is filled off
            # the ltree path by the SQL view and is empty on any other `RegistryView`, so
            # reading it here would make the rail fire in production and nowhere else.
            town = _obec_of(unit, registry)
            if town is not None and town.code in obec_kods:
                return True
    return False


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None
