"""Street + disposition keyed dedup engine — pure decision logic.

Replaces the old geo-proximity matcher (20m/price/area spatial probe). The new
engine keys on the two identifiers the operator trusts — STREET and DISPOSITION
— and confirms ambiguous pairs with room-aware vision. The rules (A-E):

  A. Eligibility. A listing matches only with BOTH a street and a disposition;
     otherwise it's flagged (location_unclear / disposition_unclear) and never
     compared. Computed inline (a partial index backs the scan, migration 127).
  B. Exact-address auto-merge. Same street + house number + disposition + floor
     => same property. Guarded: if area is known on both and differs by >5%,
     don't blind-merge — route to visual (a building can have같은-floor units).
  C. Candidate gate. Two listings sharing street + disposition are visual
     candidates UNLESS a hard contradiction rules them out: floors differ (both
     known), areas differ by >20% (both known), or house numbers differ (both
     known). Never compare anything that doesn't share street + disposition.
  D. Visual confirmation (layered): pHash fast-path on INTERIOR photos (>=2
     near-identical pairs => merge), else room-aware forensic comparison in
     priority order, stop at the first High verdict => merge.
  E. Everything left (same street+disposition, no contradiction, but no images
     or a non-High verdict) is queued for the operator.

This module is the PURE half: dataclasses + functions with no DB / LLM / I/O, so
the rules are unit-tested in isolation. scripts/dedup_engine.py is the I/O
orchestrator that feeds these and calls merge_properties / the vision tools.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from scraper.street import street_name_key as _street_name_key
from toolkit.comparables import _DISPOSITION_LOOSE
# Single-source image-tag taxonomy + family grouping (interior / exterior / plan) and the
# comparison priority orders. See toolkit/room_taxonomy.py — one place defines which tag
# is interior vs exterior and in what order rooms are compared.
from toolkit.room_taxonomy import (
    DISTINCTIVE_ROOMS,
    FULL_PRIORITY as ROOM_PRIORITY,
    HOUSE_PRIORITY,
    INTERIOR_PRIORITY as BYT_ROOM_PRIORITY,
    LAND_PRIORITY,
    NON_INTERIOR_TAGS,
    category_main_compatible,
)

# Rule B: exact-address merge is blocked when areas disagree by more than this.
ADDRESS_AREA_GUARD_PCT = 0.05
# Rule C disqualifier: an area gap this large means "not the same property" — a HARD
# reject on BOTH the exact-address and candidate paths, for EVERY category (unified at
# 10% per operator). One development stacks near-identical units differing mainly in
# area; the old 20% gate let 73/87/99 m2 units of "Rezidence Na Bradle" chain-merge via
# transitivity (73->87 = 16%, 87->99 = 12% each slipped under 20%, then pHash on shared
# renders auto-merged the bands), and a NULL-floor bridge similarly chained 59/62/74 m2
# units on "Budovatelů". 10% rejects both before the image layers ever run.
CANDIDATE_AREA_MAX_PCT = 0.10
# pHash fast-path: an image pair this close (Hamming) counts as identical.
PHASH_IDENTICAL_MAX = 6
# pHash fast-path: this many near-identical pairs over generic images => auto-merge. One
# shared photo can be a reused stock/marketing shot (a development sharing a facade across
# units); two distinct matches is a real same-property signal (only 0.34% of dismissed
# pairs reach 2). DISTINCTIVE rooms override this: a SINGLE near-identical kitchen/bathroom
# match is enough (those rooms are unit-specific, not shared marketing) — operator policy.
PHASH_MIN_IDENTICAL_PAIRS = 2


@dataclass(frozen=True)
class MatchProfile:
    """Per-category matching policy — the seam that makes the engine category-aware.

    The street+disposition rules this module grew up on are the `byt` (apartment)
    profile. Single-dwelling families (dum/pozemek/komercni/ostatni) have NO usable
    disposition — it is an apartment-shaped token (`2+kk`), ~0% present for them — so
    they must key on the coordinate + area instead. The flags are DATA, one profile per
    family, so a category's policy is a profile row, never an `if category ==` branch.

    `classify_pair` (pure, street-path) consumes `disposition_required` + the two area
    guards. The orchestrator (P1) consumes `geo_blocked` (block by geo cell vs street),
    `geo_auto_merge_allowed` (may a coord+area(+price) match auto-merge this family), and
    `requires_development_guard` (such an auto-merge needs the same-development guard).
    """
    family: str
    disposition_required: bool
    address_area_guard_pct: float
    candidate_area_max_pct: float
    geo_blocked: bool
    geo_auto_merge_allowed: bool
    requires_development_guard: bool


# Apartments: disposition is the mandatory disambiguator (one building stacks many
# units on one coordinate — coord alone would false-merge them), so byt keeps the
# street block and NEVER geo-auto-merges. This is exactly today's behavior.
_BYT_PROFILE = MatchProfile(
    family="byt", disposition_required=True,
    address_area_guard_pct=ADDRESS_AREA_GUARD_PCT,
    candidate_area_max_pct=CANDIDATE_AREA_MAX_PCT,
    geo_blocked=False, geo_auto_merge_allowed=False, requires_development_guard=False,
)
# Houses: one dwelling per address, so coord + area (+ house number / price) identifies
# the property; disposition is dropped. May geo-auto-merge — but only behind the
# same-development guard (a new estate of near-identical houses must not collapse).
_DUM_PROFILE = MatchProfile(
    family="dum", disposition_required=False,
    address_area_guard_pct=ADDRESS_AREA_GUARD_PCT,
    candidate_area_max_pct=CANDIDATE_AREA_MAX_PCT,
    geo_blocked=True, geo_auto_merge_allowed=True, requires_development_guard=True,
)
# Land / commercial / other: coord+area is a weaker same-property signal (land shares
# an exact price only ~58% of the time), so these are QUEUE-ONLY — geo blocking finds
# the candidate, but a human confirms the merge. Never geo-auto-merge.
_POZEMEK_PROFILE = MatchProfile(
    family="pozemek", disposition_required=False,
    address_area_guard_pct=ADDRESS_AREA_GUARD_PCT,
    candidate_area_max_pct=CANDIDATE_AREA_MAX_PCT,
    geo_blocked=True, geo_auto_merge_allowed=False, requires_development_guard=True,
)
_KOMERCNI_PROFILE = MatchProfile(
    family="komercni", disposition_required=False,
    address_area_guard_pct=ADDRESS_AREA_GUARD_PCT,
    candidate_area_max_pct=CANDIDATE_AREA_MAX_PCT,
    geo_blocked=True, geo_auto_merge_allowed=False, requires_development_guard=True,
)
_OSTATNI_PROFILE = MatchProfile(
    family="ostatni", disposition_required=False,
    address_area_guard_pct=ADDRESS_AREA_GUARD_PCT,
    candidate_area_max_pct=CANDIDATE_AREA_MAX_PCT,
    geo_blocked=True, geo_auto_merge_allowed=False, requires_development_guard=True,
)

_PROFILES: dict[str, MatchProfile] = {
    "byt": _BYT_PROFILE, "dum": _DUM_PROFILE, "pozemek": _POZEMEK_PROFILE,
    "komercni": _KOMERCNI_PROFILE, "ostatni": _OSTATNI_PROFILE,
}


def profile_for(category_main: str | None) -> MatchProfile:
    """The MatchProfile for a category. Unknown / NULL → the byt profile, so a row
    with no category behaves exactly as the street+disposition engine always has."""
    return _PROFILES.get(category_main or "", _BYT_PROFILE)


# The families that carry their own comparison-priority order. dum/komercni/ostatni share
# the HOUSE default but are listed separately so the operator can tune each independently.
TAG_PRIORITY_FAMILIES: tuple[str, ...] = ("byt", "dum", "komercni", "ostatni", "pozemek")


def default_priority_for_family(family: str | None) -> tuple[str, ...]:
    """The coded default comparison-tag order for a family — also the full set of VALID tags
    for it (the operator can only reorder these). byt → interior rooms; pozemek → site plan
    first; dum/komercni/ostatni → facade first."""
    if family == "byt":
        return BYT_ROOM_PRIORITY
    if family == "pozemek":
        return LAND_PRIORITY
    return HOUSE_PRIORITY


def normalize_priority(order: list[str] | tuple[str, ...], default: tuple[str, ...]) -> tuple[str, ...]:
    """Coerce an operator-supplied order into a COMPLETE, VALID priority for a family: keep
    only known tags (the default's set) in the operator's order, drop duplicates, then append
    any default tags the operator omitted (so no room is silently dropped from comparison)."""
    valid = set(default)
    seen: set[str] = set()
    out: list[str] = []
    for tag in order:
        if tag in valid and tag not in seen:
            out.append(tag)
            seen.add(tag)
    out.extend(t for t in default if t not in seen)
    return tuple(out)


def room_priority_for(
    category_main: str | None, overrides: dict[str, list[str]] | None = None
) -> tuple[str, ...]:
    """Comparison tag order for a category's perceptual + forensic image layers — the
    PRIORITY that leads (stop-at-first-High). byt → interior rooms (wet rooms first);
    pozemek → SITE PLAN first (the plot's identity); dum/komercni/ostatni → FACADE first
    (the building's identity), then interiors. `overrides` (operator-editable, keyed by
    family) reorders within the family's valid tag set; an absent / partial entry falls
    back to (or is completed from) the coded default via `normalize_priority`."""
    fam = profile_for(category_main).family
    default = default_priority_for_family(fam)
    if overrides and fam in overrides and overrides[fam]:
        return normalize_priority(overrides[fam], default)
    return default


def distinctive_rooms_for(category_main: str | None) -> frozenset[str]:
    """The tags where a SINGLE near-identical pHash match auto-merges (the count-of-1
    override). byt → kitchen/bathroom (wet rooms are unit-specific). Non-apartments →
    EMPTY: a facade / site plan is shared across a development's units (like a render), so
    one match there is NOT conclusive — they require the >=2-match count instead."""
    return DISTINCTIVE_ROOMS if profile_for(category_main).family == "byt" else frozenset()


def phash_excluded_tags_for(category_main: str | None) -> tuple[str, ...]:
    """Image tags that disqualify a pHash pair for this category. Apartments exclude
    KNOWN-exterior / shared-marketing images (NON_INTERIOR_TAGS); other categories
    exclude nothing (any image can carry a house/plot's identity)."""
    return NON_INTERIOR_TAGS if profile_for(category_main).family == "byt" else ()


# A byt image scoring >= this on the CLIP render axis (image_clip_tags.render_score,
# migration 239) is a shared development RENDER, not a real photo of THE unit, so it
# never feeds the byt pHash/cosine merge signal. The LIVE value is the operator-tunable
# app_setting `dedup_render_exclude_min` (registry default 0.95); this constant is only
# the fallback when no threshold is passed. 0.95 keeps only the most certain renders
# excluded — at 0.65 the [0.85,0.95) band over-excluded real photos and suppressed
# legitimate pHash matches (a false EXCLUSION costs recall, the harm we now minimise).
RENDER_SCORE_EXCLUDE_MIN = 0.95


def phash_render_exclude_for(
    category_main: str | None, threshold: float = RENDER_SCORE_EXCLUDE_MIN
) -> float | None:
    """The render_score threshold above which an image is excluded from this category's
    pHash / cosine merge signal. Apartments only (the validated case); None = no exclusion
    (untagged / not-yet-scored images are never excluded — recall holds as coverage ramps).
    `threshold` is the live `dedup_render_exclude_min` setting (the caller reads it once)."""
    return threshold if profile_for(category_main).family == "byt" else None


def render_exclusion_clause(
    params: dict[str, Any], alias: str,
    excluded_tags: tuple[str, ...], render_exclude_min: float | None,
) -> str:
    """A `AND NOT EXISTS (... image_clip_tags ...)` SQL fragment excluding an image (by
    `alias`) that is a KNOWN-exterior/shared tag OR scores >= render_exclude_min on the
    render axis — the ONE source of the pHash exclusion predicate, shared by the engine's
    pHash count (`_phash_identical_pairs`) and the /dedup evidence reader
    (`_phash_pair_evidence`), so the two never drift. Empty when neither filter applies;
    mutates `params` with the bound values (`%(excl)s` / `%(rmin)s`)."""
    conds: list[str] = []
    if excluded_tags:
        conds.append("t.logical_tag = ANY(%(excl)s)")
        params["excl"] = list(excluded_tags)
    if render_exclude_min is not None:
        conds.append("t.render_score >= %(rmin)s")
        params["rmin"] = render_exclude_min
    if not conds:
        return ""
    return (f" AND NOT EXISTS (SELECT 1 FROM image_clip_tags t "
            f"WHERE t.image_id = {alias}.id AND ({' OR '.join(conds)}))")


@dataclass(frozen=True)
class ListingKey:
    """The matchable identity of one eligible listing."""
    sreality_id: int
    property_id: int | None
    source: str
    street_key: str          # one grouping key: 'id:<street_id>' or 'name:<normalized>'
    disposition: str
    house_number: str | None
    floor: int | None
    area_m2: float | None
    description: str | None = None
    # Offering category: category_type is prodej/pronajem/drazba/podil,
    # category_main is byt/dum/… A sale and a rental (or a flat and a house) at
    # one address are categorically different offerings — never the same
    # property — so a mismatch is a hard reject in classify_pair.
    category_type: str | None = None
    category_main: str | None = None
    # Canonical portal street id when known (sreality), regardless of which
    # grouping key this instance carries. Two known-but-different ids inside a
    # name group mean two distinct streets sharing a name — a hard reject.
    street_id: int | None = None
    # Coordinate + price — the geo-path signals (the street path ignores these).
    lat: float | None = None
    lng: float | None = None
    price_czk: int | None = None


# Unit markers in the description that identify a SPECIFIC unit within one
# development (developer projects list near-identical plots/houses/flats with
# only the unit number differing — "pozemek č. 3" vs "č. 4", "dům 3A" vs "5C",
# "byt 42" vs "45", "budova A" vs "budova B"). When two listings name DIFFERENT
# units under the same keyword they are distinct properties, even though
# street/disposition/photos (shared marketing renders) all match.
#
# Two token shapes:
#  - NUMERIC: "<keyword> [č./č/no/number] <token>" where the token is a number
#    optionally followed by a letter or /number (3, 3A, 12/4). Case-insensitive
#    (run on diacritics-stripped lowercase text).
#  - LETTER: "<keyword> [A-H]" — a single UPPERCASE building/section letter
#    ("Budova A" vs "Budova B"). Matched CASE-SENSITIVELY against the original
#    text so the Czech conjunction "a"/"i" and stray lowercase letters don't
#    masquerade as unit labels. Restricted to A–H (real projects don't label
#    blocks past a handful) to further cut false positives.
#
# Keyword sets are kept apart: numeric keywords (a flat/plot number can follow)
# vs. container keywords that take a letter label (building/block/entrance/…).
_NUMERIC_KEYWORDS = "pozemek|dum|byt|jednotka|parcela|chata|rd|objekt|sekce|vchod"
_CONTAINER_KEYWORDS = "budova|blok|sekce|vchod|etapa|objekt|dum|dom"

_UNIT_NUM_RE = re.compile(
    r"\b(" + _NUMERIC_KEYWORDS + r")\b"
    r"(?:\s*(?:c|cislo|no|number)\.?)?\s*"
    r"(\d+[a-z]?(?:/\d+)?)\b"
)
# Letter labels: keyword must keep its diacritics-stripped form, but the LETTER
# is read from the original text so case is preserved. Built case-insensitively
# on the keyword, case-sensitively on the [A-H] group via an inline flag.
_UNIT_LETTER_RE = re.compile(
    r"(?i:\b(" + _CONTAINER_KEYWORDS + r")\b)\s*"
    r"(?-i:([A-H]))(?![\w])"
)


def _strip_diacritics(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _unit_markers(description: str | None) -> dict[str, set[str]]:
    """Map each unit keyword → the set of unit tokens it names in the text.

    e.g. "...dům, pozemek č.4..." → {'pozemek': {'4'}}; "Budova A" → {'budova':
    {'A'}}. Numeric tokens come from diacritics-stripped lowercase text; letter
    labels from the original text (case preserved) so 'A'/'B' are unit labels but
    the conjunction 'a' is not. Empty when nothing matches.
    """
    if not description:
        return {}
    out: dict[str, set[str]] = {}
    ascii_lower = _strip_diacritics(description).lower()
    for kw, token in _UNIT_NUM_RE.findall(ascii_lower):
        out.setdefault(kw, set()).add(token)
    # Letter labels: run on the diacritics-stripped (but case-preserved) text so
    # 'dům'→'dum' for the keyword while 'A' stays uppercase.
    ascii_cased = _strip_diacritics(description)
    for kw, letter in _UNIT_LETTER_RE.findall(ascii_cased):
        out.setdefault(kw.lower(), set()).add(letter)
    return out


def _unit_markers_contradict(a: str | None, b: str | None) -> bool:
    """True when both descriptions name the SAME unit keyword but DIFFERENT
    tokens for it (e.g. pozemek 3 vs pozemek 4) — distinct units, never merge.

    Conservative: only fires on a shared keyword whose token sets are disjoint.
    A keyword present in only one description, or with overlapping tokens, does
    NOT contradict (avoids false rejections from prose like "byt 2+kk")."""
    ma, mb = _unit_markers(a), _unit_markers(b)
    for kw in ma.keys() & mb.keys():
        if ma[kw].isdisjoint(mb[kw]):
            return True
    return False


@dataclass(frozen=True)
class PairDecision:
    action: str               # 'auto_merge' | 'candidate' | 'reject'
    reason: str | None        # 'address_exact' | 'area_guard' | None ...
    # for 'reject': which disqualifier fired
    detail: str | None = None


# The street-NAME grouping key (`_street_name_key`) lives in `scraper.street` — the
# single home for all street string logic — and is imported above. It is stored on
# `listings.street_name_key` at write time AND recomputed here for grouping, so the
# stored column never drifts from the engine (parity-tested). See street_group_keys.


def normalize_street(street: str | None, street_id: int | None) -> str | None:
    """The primary street grouping key: prefer the portal's canonical
    street_id, else the normalized street name. None when there is no street."""
    if street_id is not None and street_id > 0:
        return f"id:{street_id}"
    name = _street_name_key(street)
    return f"name:{name}" if name is not None else None


def street_group_keys(
    street: str | None, street_id: int | None, obec_id: int | None = None,
) -> tuple[str, ...]:
    """ALL grouping keys for one listing, canonical id first. A row carrying
    both a street_id and a street name is dual-keyed — it joins its 'id:' group
    AND the 'name:' group — so an id-keyed sreality row can meet a name-only
    bazos row in one candidate group (id-only grouping made that impossible).

    The NAME key is scoped by `obec_id` (the geom-derived municipality). A common
    street name like "Žižkova" has 100+ active listings across dozens of towns;
    one nationwide "name:zizkova" group blows MAX_GROUP_SIZE and gets the WHOLE
    group skipped — so cross-portal pairs there (HTML portals carry no street_id,
    so the name group is the only place they can meet a sreality row) were never
    compared. Scoping by obec keeps each town's street its own small group AND
    blocks cross-town false merges (classify_pair has no geo check). The 'id:' key
    stays global — a street_id is one physical street, already town-specific."""
    keys: list[str] = []
    if street_id is not None and street_id > 0:
        keys.append(f"id:{street_id}")
    name = _street_name_key(street)
    if name is not None:
        keys.append(f"name:{obec_id}:{name}")
    return tuple(keys)


def disposition_compatible(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return a == b or b in _DISPOSITION_LOOSE.get(a, ())


def disposition_class(disposition: str | None) -> str:
    """Canonical representative of a disposition's loose-equivalence class.

    _DISPOSITION_LOOSE links only same-room-count labels (N+kk <-> N+1), so a street
    group sharded on this class keeps every classify_pair-compatible pair together —
    the shard is loss-free by construction. Unmapped values (atypicky, 6+kk, ...)
    are their own class (their compatibility is exact equality)."""
    if disposition is None:
        return ""
    group = _DISPOSITION_LOOSE.get(disposition)
    return min(group) if group else disposition


def _area_pct_diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or max(a, b) == 0:
        return None
    return abs(a - b) / max(a, b)


def _house_numbers_contradict(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a.strip().lower() != b.strip().lower()


def classify_pair(a: ListingKey, b: ListingKey) -> PairDecision:
    """Decide what to do with two eligible listings that share a street key.

    Pure: no images yet. Returns 'auto_merge' (rule B), 'reject' (rule C
    disqualifier), or 'candidate' (needs the visual layer). The street-key match
    is a precondition the caller guarantees by only pairing within a street
    group; we re-assert disposition compatibility here.
    """
    if a.sreality_id == b.sreality_id:
        return PairDecision("reject", None, "same_listing")
    if a.property_id is not None and a.property_id == b.property_id:
        return PairDecision("reject", None, "already_merged")
    if a.street_key != b.street_key:
        return PairDecision("reject", None, "street_mismatch")
    # Dual-keying lets same-NAME streets from different towns share one name
    # group; when both portals supplied a canonical street_id and they differ,
    # these are two distinct streets — hard reject (NULL ids don't contradict).
    if (
        a.street_id is not None and b.street_id is not None
        and a.street_id != b.street_id
    ):
        return PairDecision("reject", None, "street_id_contradiction")
    # Offering category is fundamental: a sale and a rental (prodej vs pronajem),
    # or a flat and a house (byt vs dum), at one address are different offerings,
    # never the same property — reject before anything else. NULLs don't
    # contradict (a missing category is unknown, not a conflict).
    if (
        a.category_type is not None and b.category_type is not None
        and a.category_type != b.category_type
    ):
        return PairDecision("reject", None, "category_type_contradiction")
    if not category_main_compatible(a.category_main, b.category_main):
        return PairDecision("reject", None, "category_main_contradiction")
    # Category drives the matching policy. The pair's categories are COMPATIBLE here
    # (equal, or the one sanctioned dum<->komercni cross-type), so the FIRST listing's
    # family is the profile (operator policy: no special cross-type logic — the side that
    # reached the engine first sets the priorities); a NULL/unknown category falls back to
    # the byt profile (unchanged behavior).
    profile = profile_for(a.category_main if a.category_main is not None else b.category_main)
    # Disposition is mandatory for apartments; for single-dwelling families it's absent,
    # so only enforce compatibility when the profile requires it OR both rows carry one.
    if profile.disposition_required or (
        a.disposition is not None and b.disposition is not None
    ):
        if not disposition_compatible(a.disposition, b.disposition):
            return PairDecision("reject", None, "disposition_mismatch")

    # Rule C hard disqualifiers (apply to BOTH the exact-address and candidate
    # paths — a contradiction means "not the same property", full stop).
    # Floor is a SOFT cross-portal signal: idnes counts the ground floor as 0
    # (patro) while sreality counts it as 1 (NP), so the SAME flat reads one
    # floor apart on the two portals — and sreality is itself lister-inconsistent.
    # A gap of exactly 1 is therefore convention noise, not a contradiction: let
    # it fall through to the visual layer (rule B's exact auto-merge still
    # requires floor equality, so an off-by-one never auto-merges without photo
    # confirmation). Only a gap of 2+ is a real "different unit" signal worth a
    # hard reject.
    if (
        a.floor is not None and b.floor is not None
        and abs(a.floor - b.floor) >= 2
    ):
        return PairDecision("reject", None, "floor_contradiction")
    if _house_numbers_contradict(a.house_number, b.house_number):
        return PairDecision("reject", None, "house_number_contradiction")
    area_diff = _area_pct_diff(a.area_m2, b.area_m2)
    if area_diff is not None and area_diff > profile.candidate_area_max_pct:
        return PairDecision("reject", None, "area_contradiction")
    # Distinct units of one development (pozemek č.3 vs č.4, dům 3A vs 5C, …):
    # the descriptions name the same keyword with different unit tokens. The
    # photos can't disambiguate (shared renders), so this MUST gate before the
    # visual layer — a hard reject, like the other rule-C contradictions.
    if _unit_markers_contradict(a.description, b.description):
        return PairDecision("reject", None, "unit_marker_contradiction")

    # Rule B (exact address) is RETIRED (2026-06): exact street+house_number+disposition+floor
    # was the ONLY auto-merge path that produced false merges (6.7% later unmerged — two
    # different units at the same address+floor — vs 0% for pHash + visual). Exact address is
    # not unit-conclusive, so it is now just a (strong) rule-C CANDIDATE: the pair flows
    # through the pHash fast-path → forensic visual → floor-plan gate (the 0%-reversal paths)
    # like any street+disposition pair. The `address_exact` reason is kept for provenance.
    exact_address = (
        a.house_number is not None and b.house_number is not None
        and a.house_number.strip().lower() == b.house_number.strip().lower()
        and a.floor is not None and b.floor is not None and a.floor == b.floor
        and a.disposition == b.disposition
    )
    if exact_address:
        if area_diff is not None and area_diff > profile.address_area_guard_pct:
            # Same address+floor+disposition but materially different area: likely two distinct
            # units — a candidate the visual flow settles (was already demoted pre-retirement).
            return PairDecision("candidate", "area_guard")
        return PairDecision("candidate", "address_exact")

    # Rule C: shares street + disposition, no contradiction — a visual candidate.
    return PairDecision("candidate", None)


def prioritized_group_pairs(
    members: list[ListingKey], *, cap: int,
    classify: Any = None,
    priority_property_ids: set[int] | None = None,
) -> list[tuple[ListingKey, ListingKey]]:
    """Bounded, value-ordered pair list for an OVERSIZED group.

    Replaces the historical whole-group skip, which silently dropped EVERY pair on
    busy streets — 342 groups holding 18.7% of the eligible market in the 2026-07
    audit. Deterministic rejects are dropped up front (classify is pure and cheap),
    then the highest-value pairs come first so a bounded scan spends its budget
    where merges live:
      1. pairs touching a claimed dirty property (the real-time SLO pairs),
      2. cross-source pairs (dedup's payoff),
      3. smaller price gap, then smaller area gap (unknown gaps sort last).
    Capped at `cap`, so an oversized group costs at most a fixed pair budget."""
    decide = classify or classify_pair
    pri = priority_property_ids or set()
    scored: list[tuple[tuple[int, int, float, float], tuple[ListingKey, ListingKey]]] = []
    for i in range(len(members)):
        a = members[i]
        for j in range(i + 1, len(members)):
            b = members[j]
            if decide(a, b).action == "reject":
                continue
            if a.price_czk and b.price_czk:
                price_gap = abs(a.price_czk - b.price_czk) / max(a.price_czk, b.price_czk)
            else:
                price_gap = math.inf
            area_gap = _area_pct_diff(a.area_m2, b.area_m2)
            scored.append((
                (
                    0 if (a.property_id in pri or b.property_id in pri) else 1,
                    0 if a.source != b.source else 1,
                    price_gap,
                    area_gap if area_gap is not None else math.inf,
                ),
                (a, b),
            ))
    scored.sort(key=lambda t: t[0])
    return [pair for _, pair in scored[:cap]]


# --- geo path: single-dwelling families (dum/pozemek/komercni/ostatni) -------
# These have no usable disposition, so the matcher keys on the COORDINATE instead.
# The orchestrator blocks pairs into a small geo cell (one obec + a rounded coord),
# and classify_geo_pair is the per-pair decision INSIDE that cell. Deterministic, no
# LLM. A geo cell at 4-dp precision spans ~11 m, so a same-cell pair is co-located by
# construction; the haversine guard below is the precise backstop for cell-edge cases.
GEO_MAX_COORD_M = 35.0
GEO_STRONG_AREA_PCT = 0.03      # "same area" for the strong same-property signal
GEO_PRICE_MATCH_PCT = 0.02      # asking prices this close count as the same price


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres (in-memory pair check, not a DB query)."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def geo_category_bucket(category_main: str | None) -> str | None:
    """The geo-cell category token. dum and komercni collapse to ONE bucket so the
    sanctioned cross-type co-locates in the same cell (and reaches classify_geo_pair);
    every other category buckets to itself (a flat never shares a house's cell)."""
    return "dum|komercni" if category_main in ("dum", "komercni") else category_main


def geo_cell_key(
    obec_id: int | None, lat: float | None, lng: float | None,
    category_main: str | None, category_type: str | None, *, precision: int = 4,
) -> str | None:
    """Blocking key for the geo path: one municipality + a rounded coordinate + the
    offering. Scoping by obec_id keeps a coordinate collision across towns apart, and
    by (category bucket, category_type) keeps a sale flat and a rental house out of one
    cell — while dum and komercni share a bucket (geo_category_bucket) so the one
    sanctioned cross-type can pair. None when the coordinate / municipality is missing
    (the row can't geo-block).

    AUTHORITATIVE DEFINITION MOVED TO SQL: the stored, trigger-maintained
    listings.geo_cell_key (public.listing_geo_cell_key, migration 276) is what the
    engine's geo loader groups on now. This function is retained as the format's
    executable documentation (and for tests); its rendering may differ from SQL's in
    trailing zeros / exact-tie rounding, which is fine — nothing compares the two."""
    if obec_id is None or lat is None or lng is None:
        return None
    bucket = geo_category_bucket(category_main)
    return f"geo:{obec_id}:{round(lat, precision)}:{round(lng, precision)}:{bucket}:{category_type}"


def _price_match(a: int | None, b: int | None, pct: float = GEO_PRICE_MATCH_PCT) -> bool:
    if a is None or b is None or max(a, b) == 0:
        return False
    return abs(a - b) / max(a, b) <= pct


def classify_geo_pair(
    a: ListingKey, b: ListingKey, profile: MatchProfile, *, max_coord_m: float = GEO_MAX_COORD_M,
    max_area_pct: float | None = None,
) -> PairDecision:
    """Decide two co-located single-dwelling listings (same geo cell). Pure, no images.

    'reject' on a hard contradiction (coordinate too far, different house number, area
    gap > `max_area_pct` (defaults to the profile's), or a same-development unit-marker
    clash); 'auto_merge' only for a STRONG house signal (near-identical area AND matching
    price-or-house-number) when the profile permits it; otherwise 'candidate'. The
    orchestrator decides whether to honour an auto_merge — in the unified geo path it maps
    auto_merge → candidate, so the free-first visual flow is the sole merge gate."""
    area_max_pct = profile.candidate_area_max_pct if max_area_pct is None else max_area_pct
    if a.sreality_id == b.sreality_id:
        return PairDecision("reject", None, "same_listing")
    if a.property_id is not None and a.property_id == b.property_id:
        return PairDecision("reject", None, "already_merged")
    if (
        a.category_type is not None and b.category_type is not None
        and a.category_type != b.category_type
    ):
        return PairDecision("reject", None, "category_type_contradiction")
    if not category_main_compatible(a.category_main, b.category_main):
        return PairDecision("reject", None, "category_main_contradiction")
    if (
        a.lat is not None and a.lng is not None and b.lat is not None and b.lng is not None
        and _haversine_m(a.lat, a.lng, b.lat, b.lng) > max_coord_m
    ):
        return PairDecision("reject", None, "coord_too_far")
    if _house_numbers_contradict(a.house_number, b.house_number):
        return PairDecision("reject", None, "house_number_contradiction")
    area_diff = _area_pct_diff(a.area_m2, b.area_m2)
    if area_diff is not None and area_diff > area_max_pct:
        return PairDecision("reject", None, "area_contradiction")
    # Same-development text guard (a new estate names "dům 3A" vs "5C", "pozemek č.3"
    # vs "č.4"): distinct units, never the same property — reuse the street path's check.
    if _unit_markers_contradict(a.description, b.description):
        return PairDecision("reject", None, "unit_marker_contradiction")

    house_no_match = (
        a.house_number is not None and b.house_number is not None
        and a.house_number.strip().lower() == b.house_number.strip().lower()
    )
    strong = (
        area_diff is not None and area_diff <= GEO_STRONG_AREA_PCT
        and (_price_match(a.price_czk, b.price_czk) or house_no_match)
    )
    # Geo-auto-merge gates on BOTH families, not just `a`'s. For a same-type pair this is
    # exactly `profile.geo_auto_merge_allowed` (one family). For the dum<->komercni cross-type
    # it is the SYMMETRIC, conservative choice: komercni isn't geo-auto-merge-validated, so the
    # pair QUEUES regardless of which side arrived first — it never auto-merges on a geo signal
    # alone (it still merges via the exact-address / pHash / visual paths, or operator review).
    # Without this, (dum, komercni) and (komercni, dum) would decide differently by order.
    both_allow_auto_merge = (
        profile.geo_auto_merge_allowed
        and profile_for(b.category_main).geo_auto_merge_allowed
    )
    if strong and both_allow_auto_merge:
        return PairDecision("auto_merge", "geo_exact")
    return PairDecision("candidate", "geo_strong" if strong else "geo_weak")


@dataclass
class VisualOutcome:
    """The result of the visual layer for one candidate pair."""
    action: str               # 'auto_merge' | 'queue'
    reason: str | None        # 'image_phash' | 'visual_match' | None
    room_type: str | None = None
    verdict: str | None = None      # High|Medium|Low (forensic path)
    rationale: str | None = None
    phash_pairs: int = 0
    rooms_tried: list[str] = field(default_factory=list)


def decide_phash_fastpath(
    identical_image_pairs: int, distinctive_match: bool = False,
    min_identical_pairs: int = PHASH_MIN_IDENTICAL_PAIRS,
) -> bool:
    """pHash fast-path: >=`min_identical_pairs` near-identical generic image pairs OR a
    single near-identical DISTINCTIVE-room (kitchen/bathroom) pair => same property. The
    distinctive override (operator policy) reflects that wet rooms are unit-specific, not
    shared marketing, so one identical match there is conclusive. For byt the generic count
    excludes known-exterior images upstream (phash_excluded_tags_for); other categories
    count any image. `min_identical_pairs` defaults to the classic 2; the non-byt
    phash-single arm (cost plan §2.2a, dedup_nonbyt_phash_single_enabled) lowers it to 1
    for houses/land/commercial, where photo sets are property-unique (99%+ replay
    precision) rather than development-shared like byt marketing renders."""
    return identical_image_pairs >= min_identical_pairs or distinctive_match


def rooms_in_priority(
    common_rooms: set[str], category_main: str | None = None,
    overrides: dict[str, list[str]] | None = None,
) -> list[str]:
    """The room types present in BOTH listings, in comparison priority order for the
    category (operator `overrides` honoured per family). Apartments (default / NULL) compare
    INTERIOR rooms only; other categories may compare exterior rooms too."""
    return [r for r in room_priority_for(category_main, overrides) if r in common_rooms]


def verdict_is_merge(verdict: str | None) -> bool:
    """Rule D / auto gate: only a High forensic verdict auto-merges."""
    return verdict == "High"


# The most identifying interior rooms — a confident "different" on one of these
# is the auto-DISMISS signal (operator policy: "kitchen/bathroom clearly differ").
# Calibrated: 0 of 273 operator-merged pairs carried a Low verdict, and a same
# property whose kitchen merely changed is rescued by the High OR-gate on another
# room (so reaching auto-dismiss means NO room matched). Bedroom/exterior/etc. are
# too generic to dismiss on alone.
DISTINCTIVE_DISMISS_ROOMS: frozenset[str] = frozenset({"kitchen", "bathroom"})


def decide_visual_dismiss(
    room_verdicts: dict[str, str], category_main: str | None = None,
    facade_dismiss: bool = False,
) -> bool:
    """True iff the forensic room verdicts confidently say "different property".

    Auto-dismiss (don't queue for the operator) only when:
      * no room reached High (the merge OR-gate already fired otherwise), AND
      * a dismissal-qualifying room was compared and returned Low, AND
      * no dismissal-qualifying room is non-Low (no Medium/ambiguous hedge).
    Dismissal-qualifying rooms are the DISTINCTIVE wet rooms (kitchen/bathroom — the
    #506 byt-era calibration, unchanged), plus `exterior_facade` for NON-byt families
    when `facade_dismiss` is on (cost plan §5.2, fid5 operator-requested option,
    dedup_facade_dismiss_enabled, default OFF): for houses/land/commercial the facade
    IS the identity-bearing surface (#619 made the merge side family-aware; this is
    the dismiss-side counterpart), while byt facades stay non-qualifying — a
    development's shared building shell says nothing about which unit is listed.
    Everything else (a qualifying Medium, or only generic rooms compared) stays
    queued for a human. room_verdicts maps room_type -> 'High'|'Medium'|'Low'.
    """
    if not room_verdicts:
        return False
    if any(v == "High" for v in room_verdicts.values()):
        return False
    qualifying = set(DISTINCTIVE_DISMISS_ROOMS)
    if facade_dismiss and category_main and category_main != "byt":
        qualifying.add("exterior_facade")
    relevant = [v for r, v in room_verdicts.items() if r in qualifying]
    if not relevant:
        return False
    return all(v == "Low" for v in relevant)


# Stage 4b: the CLIP cosine recall tier picks WHICH forensic model judges a room
# by the upstream same-room cosine. Calibrated from the trial: same-property
# same-tag median ~0.90, different-property p95 ~0.84.
@dataclass(frozen=True)
class CosineBands:
    haiku_min: float = 0.90    # cosine >= this -> Haiku confirms (near-certain, cheap)
    sonnet_min: float = 0.70   # [sonnet_min, haiku_min) -> Sonnet (the uncertain band)
    # cosine < sonnet_min -> 'manual': too dissimilar to spend the LLM on this room
    # (NOT a dismiss — the pair still queues if no room merges; protects reshoots).


def route_by_cosine(cosine: float | None, bands: CosineBands) -> str:
    """Which forensic model judges this room, from its upstream CLIP cosine:
    'haiku' | 'sonnet' | 'manual'. NEVER auto-merges or auto-dismisses — it only
    picks who decides, or 'manual' to skip the LLM for a too-dissimilar room. A
    missing cosine (no stored embedding) routes to 'sonnet' — the precise model —
    so the absence of CLIP never silently weakens a decision."""
    if cosine is None:
        return "sonnet"
    if cosine >= bands.haiku_min:
        return "haiku"
    if cosine >= bands.sonnet_min:
        return "sonnet"
    return "manual"
