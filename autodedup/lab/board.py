"""The plug board: the ladder as an ordered list of registered rungs, then one registered group
step, all run over a cohort's evidence cache from ONE experiment config.

A rung is `fn(cohort, decisions, params) -> decisions`: it reads cached signal columns and the
decisions so far, and settles or re-reads the pairs its rule speaks to, stamping (zone, rung,
name, carrier). A new mechanic is one function under `@rung("name")` and one line in a config —
no settings field, no flag. The reference rungs reproduce `decide.decide_pair` (w31) exactly,
reason strings included, which `verify` checks against a stored generation."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import itertools
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from autodedup import store_score
from autodedup.cluster import cluster_pairs
from autodedup.d43 import relation_for
from autodedup.decide import (
    CONTEXT_RULE_REASON,
    Decision,
    apply_merge_policy,
    certificate_of,
    context_rule_may_promote,
)
from autodedup.features import TAG_FEATURE_NAMES
from autodedup.guards import UNIT_DESIGNATOR_VETO
from autodedup.indistinguishable import FEATURE_SLOTS
from autodedup.lab.cache import FIDX, Cohort
from autodedup.lab.score import FAMILIES, family_split, scores
from toolkit.room_taxonomy import category_main_compatible

U, VETO, REJECT, BAND, MERGE = 0, 1, 2, 3, 4
ZONE_NAMES: tuple[str, ...] = ("undecided", "veto", "reject", "band", "merge")

# One vocabulary for carriers (LADDER_SPEC section 2; C6's FACT_FAMILY).
_FACTS = {
    "ATTR": """category_type category_main area stated_area printed_area headline_area offer_area
        two_unit accessory accessory_area cellar_area outdoor_accessory extent extent_package
        extent_variant part_whole part_addition disposition unit_count floor total_floors
        prose_floor subject_floor storey_word offered_storey plot_area plot_prose plot_prose_exact
        plot_attribute neighbour_plot product_class offered_use commercial_subtype rental_colive
        onesided_area onesided_floor attr_contradictions""",
    "PRICE": "price charge accessory_price priced_row",
    "LOC": """obec obec_prose body_obec street street_prose orientation stored_house_number
        printed_house_number parcel""",
    "TXT": """unit_designator printed_designator unit_code labelled_unit english_unit_code
        slug_unit space_number plan_space position_designator named_villa lot_label agency_code
        agency_code_plus agency_code_colive body_align onesided_code unit_designator_conflict
        numeral_conflict""",
    "IMG": "interior floorplan",
}
FACT_FAMILY: dict[str, str] = {n: fam for fam, names in _FACTS.items() for n in names.split()}
CERT_FAMILY: dict[str, str] = {"K-R": "TXT", "K-B": "TXT", "K-C": "IMG", "K-A": "LOC"}
CORRO_FAMILY: dict[str, str] = {"photo": "IMG", "body": "TXT", "code": "TXT", "twin": "TXT",
                                "pin": "LOC", "price_path": "PRICE"}
LIMB_FAMILY: dict[str, str] = {"area": "ATTR", "disposition": "ATTR", "price": "PRICE",
                               "obec": "LOC"}


@dataclass
class Decisions:
    """Every candidate pair's decision so far, as columns."""

    zone: np.ndarray
    final: np.ndarray
    gated: np.ndarray
    rung: np.ndarray
    name: np.ndarray
    carrier: np.ndarray
    reason: np.ndarray
    cert: np.ndarray
    score: np.ndarray
    columns: dict[str, np.ndarray] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def blank(cls, n: int, score: np.ndarray) -> "Decisions":
        def obj() -> np.ndarray:
            out = np.empty(n, dtype=object)
            out[:] = ""
            return out
        return cls(np.zeros(n, dtype=np.int8), np.zeros(n, dtype=bool), np.zeros(n, dtype=bool),
                   obj(), obj(), obj(), obj(), obj(), score.astype(np.float64).copy())

    def copy(self) -> "Decisions":
        return copy.deepcopy(self)

    def settle(self, mask: np.ndarray, zone: int, rung: str, name: Any, carrier: Any,
               reason: Any) -> None:
        self.zone[mask] = zone
        self.rung[mask] = rung
        self.name[mask] = name if np.isscalar(name) else np.asarray(name, dtype=object)[mask]
        self.carrier[mask] = (carrier if np.isscalar(carrier)
                              else np.asarray(carrier, dtype=object)[mask])
        self.reason[mask] = reason if np.isscalar(reason) else np.asarray(reason, dtype=object)[mask]


RungFn = Callable[[Cohort, Decisions, dict[str, Any]], Decisions]
GroupFn = Callable[[Cohort, Decisions, dict[str, Any]], "Groups"]
RUNGS: dict[str, RungFn] = {}
GROUPS: dict[str, GroupFn] = {}


def rung(name: str) -> Callable[[RungFn], RungFn]:
    def register(fn: RungFn) -> RungFn:
        RUNGS[name] = fn
        return fn
    return register


def group_step(name: str) -> Callable[[GroupFn], GroupFn]:
    def register(fn: GroupFn) -> GroupFn:
        GROUPS[name] = fn
        return fn
    return register


def _cat(parts: list[Any]) -> np.ndarray:
    """Element-wise string concatenation of scalars and object columns."""
    n = max(len(p) for p in parts if isinstance(p, np.ndarray))
    out = np.empty(n, dtype=object)
    out[:] = ""
    for part in parts:
        out = out + (part if isinstance(part, np.ndarray) else np.full(n, part, dtype=object))
    return out


def _side(c: Cohort) -> np.ndarray:
    v, p = c.column("same_source")
    out = np.empty(c.n, dtype=object)
    out[:] = "cross"
    out[p & (v == 1.0)] = "same"
    return out


def _cuts(c: Cohort, layer: np.ndarray, p: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """E48: each pair's stratum cut and whether its stratum is propose-only (None)."""
    table = p.get("t_hi_by_stratum", c.settings.t_hi_by_stratum) or {}
    default = float(p.get("t_hi", c.settings.t_hi))
    keys = _cat([layer, "|", _side(c)])
    cut = np.full(c.n, default)
    propose_only = np.zeros(c.n, dtype=bool)
    for key, value in table.items():
        hit = keys == key
        if value is None:
            propose_only |= hit
        else:
            cut[hit] = float(value)
    return cut, propose_only


def _no_tag_fact(column: str) -> None:
    """Tags are never facts, in any mode (standing ruling): no rung may veto on a tag column."""
    if column in TAG_FEATURE_NAMES:
        raise ValueError(f"{column} is tag-derived: a tag is never a fact that separates a pair")


# --- the reference rungs: decide_pair, one rule each --------------------------------------------

@rung("veto")
def veto_rung(c: Cohort, d: Decisions, p: dict[str, Any]) -> Decisions:
    """The walls re-checked at decide time and E61's unit-designator conflict, over every zone:
    `decide` runs it first whatever the config's order, and again after the last rung."""
    d = d.copy()
    names = c.sig["veto"]
    m = names != ""
    families = np.array([FACT_FAMILY.get(x, "ATTR") for x in names], dtype=object)
    d.settle(m, VETO, "fact", names, families, _cat(["guard:", names]))
    d.score[m] = 0.0
    d.final |= m
    return d


@rung("auto_reject")
def auto_reject_rung(c: Cohort, d: Decisions, p: dict[str, Any]) -> Decisions:
    """The pair-grain facts `attributes` and `numerals` (LA6)."""
    d = d.copy()
    names = c.sig["auto"]
    m = (d.zone == U) & (names != "")
    families = np.array([FACT_FAMILY.get(x, "ATTR") for x in names], dtype=object)
    d.settle(m, REJECT, "fact", names, families, _cat(["auto_reject:", names]))
    d.final |= m
    return d


def _allowed_certificates(c: Cohort, allow: list[str]) -> np.ndarray:
    """The certificate each pair earns when only `allow` may certify, read by the engine's own
    `certificate_of`: K-R and K-A off are its settings switches, K-B off is E85's `kb_refused`
    (the pair falls to K-C exactly as the engine's family pass re-decides it). K-C is the last
    certificate, so K-C off leaves the pair uncertified."""
    cert = c.sig["cert"].copy()
    redo = np.flatnonzero((cert != "") & ~np.isin(cert, allow))
    if len(redo) == 0:
        return cert
    settings = dataclasses.replace(
        c.settings, certificate_kr_enabled=c.settings.certificate_kr_enabled and "K-R" in allow,
        certificate_ka_enabled=c.settings.certificate_ka_enabled and "K-A" in allow)
    for i in redo:
        lo, hi = c.keys[i]
        again = certificate_of(c.feats(int(i)), c.ds.listings[lo], c.ds.listings[hi], settings,
                               "K-B" not in allow) or ""
        cert[i] = again if again in allow else ""
    return cert


@rung("proof")
def proof_rung(c: Cohort, d: Decisions, p: dict[str, Any]) -> Decisions:
    """K-R, K-B, K-C (whichever `allow` lists): certified pairs merge, subject to E48's
    propose-only strata, the E46/E47 block and E11's family count. A pair whose certificate is
    not allowed is re-read by `certificate_of` without it (`_allowed_certificates`)."""
    d = d.copy()
    allow = list(p.get("allow", ("K-R", "K-A", "K-B", "K-C")))
    cert = _allowed_certificates(c, allow) if "allow" in p else c.sig["cert"]
    m = (d.zone == U) & (cert != "")
    _, propose_only = _cuts(c, cert, p)
    block = c.sig["block"]
    diverse = c.sig["nfam"] >= int(p.get("min_families", c.settings.min_evidence_families))
    carrier = np.array([CERT_FAMILY.get(x, "NONE") for x in cert], dtype=object)
    d.cert[m] = cert[m]
    held = m & propose_only
    d.settle(held, BAND, "proof", cert, carrier, _cat(["certificate:", cert, ":stratum_propose_only"]))
    blocked = m & ~propose_only & (block != "")
    d.settle(blocked, BAND, "proof", cert, carrier, _cat(["certificate:", cert, ":", block]))
    ok = m & ~propose_only & (block == "")
    d.settle(ok & diverse, MERGE, "proof", cert, carrier, _cat(["certificate:", cert]))
    d.settle(ok & ~diverse, BAND, "proof", cert, carrier,
             _cat(["certificate:", cert, ":evidence_gate"]))
    return d


@rung("score")
def score_rung(c: Cohort, d: Decisions, p: dict[str, Any]) -> Decisions:
    """The learned score's cut: at or above the stratum's t_hi merge, above t_lo band."""
    d = d.copy()
    layer = np.full(c.n, "model", dtype=object)
    cut, propose_only = _cuts(c, layer, p)
    t_lo = float(p.get("t_lo", c.settings.t_lo))
    block = c.sig["block"]
    diverse = c.sig["nfam"] >= int(p.get("min_families", c.settings.min_evidence_families))
    m = d.zone == U
    over = m & ~propose_only & (d.score >= cut)
    d.settle(over & (block != ""), BAND, "score", "cut", "NONE", block)
    d.settle(over & (block == "") & diverse, MERGE, "score", "cut", "NONE", "model")
    d.settle(over & (block == "") & ~diverse, BAND, "score", "cut", "NONE", "evidence_gate")
    rest = m & ~over
    d.settle(rest & (d.score > t_lo), BAND, "score", "band", "NONE", "model")
    d.settle(rest & (d.score <= t_lo), REJECT, "score", "cut", "NONE", "model")
    return d


@rung("context")
def context_rung(c: Cohort, d: Decisions, p: dict[str, Any]) -> Decisions:
    """E63: a band pair with a near-identical body, one price and a top score merges unless a
    fungible-catalogue limb refuses; `context_rule_may_promote` reads the band reason, as
    `apply_context_rule` does."""
    d = d.copy()
    if not p.get("enabled", c.settings.context_rule_enabled):
        return d
    arm, refused = c.sig["ctx_arm"], c.sig["ctx_refused"]
    m = ((d.zone == BAND) & ~d.final & (arm != "")
         & (d.score >= float(p.get("min_score", c.settings.context_rule_min_score))))
    for i in np.flatnonzero(m):
        m[i] = context_rule_may_promote(d.reason[i])
    fungible = m & (refused != "") & (refused != "unanswered")
    d.reason[fungible] = _cat([CONTEXT_RULE_REASON + ":fungible:", refused])[fungible]
    ok = m & (refused == "")
    carrier = np.where(arm == "text", "TXT", "IMG").astype(object)
    d.settle(ok, MERGE, "context", arm, carrier, _cat([CONTEXT_RULE_REASON + ":", arm]))
    return d


@rung("gate")
def gate_rung(c: Cohort, d: Decisions, p: dict[str, Any]) -> Decisions:
    """Stated facts cap the zone: a merge the comparator separates is never a merge edge.
    `settings` re-reads the gate under the row plus those overrides (the engine's own
    `distinguishing_facts`, filled lazily and kept in the overlay under the overrides)."""
    d = d.copy()
    settings = c.settings_with(p.get("settings"))
    if not p.get("enabled", settings.d43_gate):
        return d
    m = d.zone == MERGE
    sig = c.lazy("gate", np.flatnonzero(m), p.get("settings"))
    first = np.array([facts[0] if facts else "" for facts in sig["gate"]], dtype=object)
    hit = m & (first != "")
    families = np.array([FACT_FAMILY.get(x, "ATTR") for x in first], dtype=object)
    d.settle(hit, BAND, "fact", first, families, _cat([d.reason, ":d43_gate:", first]))
    d.gated |= hit
    return d


@rung("demonstrate")
def demonstrate_rung(c: Cohort, d: Decisions, p: dict[str, Any]) -> Decisions:
    """D43 promotion under D50: a band pair no fact separates merges when it positively agrees
    and one unit-grade corroboration holds. `settings` re-reads the PROMOTE-mode facts under the
    row plus those overrides (e.g. an image-facts switch: tags are never facts, in any mode)."""
    d = d.copy()
    settings = c.settings_with(p.get("settings"))
    if not p.get("enabled", settings.d43_promote):
        return d
    m = (d.zone == BAND) & ~d.final & ~d.gated
    sig = c.lazy("promote", np.flatnonzero(m), p.get("settings"))
    warrant, refusal, corro = sig["warrant"], sig["refusal"], sig["corro"]
    refused = m & (warrant != "") & (refusal != "")
    limb = np.array([LIMB_FAMILY.get(r[2:], "ATTR") if r.startswith("A:") else "NONE"
                     for r in refusal], dtype=object)
    d.settle(refused, BAND, "demonstration", refusal, limb,
             _cat([d.reason, ":d43_demonstrate:", refusal]))
    ok = m & (warrant != "") & (refusal == "")
    carrier = np.array([CORRO_FAMILY.get(x, "NONE") for x in corro], dtype=object)
    d.settle(ok, MERGE, "demonstration", corro, carrier, _cat(["d43_promote:", warrant]))
    return d


@rung("policy")
def policy_rung(c: Cohort, d: Decisions, p: dict[str, Any]) -> Decisions:
    """D65: a category cell the operator holds propose-only never reaches the merge zone; each
    merge is read by the engine's own `apply_merge_policy` (`table` overrides the row's)."""
    d = d.copy()
    settings = (dataclasses.replace(c.settings, merge_policy=dict(p["table"] or {}))
                if "table" in p else c.settings)
    if not settings.merge_policy:
        return d
    hold = np.zeros(c.n, dtype=bool)
    reason = d.reason.copy()
    for i in np.flatnonzero(d.zone == MERGE):
        lo, hi = c.keys[i]
        read = apply_merge_policy(Decision(lo, hi, "merge", float(d.score[i]), set(),
                                           d.cert[i] or None, None, d.reason[i]),
                                  c.ds.listings[lo], c.ds.listings[hi], settings)
        if read.zone != "merge":
            hold[i], reason[i] = True, read.reason
    d.settle(hold, BAND, "policy", "hold", "OPERATOR", reason)
    return d


# --- example plugs: one function each, no flag --------------------------------------------------

_OPS: dict[str, Callable[[np.ndarray, float], np.ndarray]] = {
    ">=": np.greater_equal, ">": np.greater, "<=": np.less_equal, "<": np.less,
    "==": np.equal, "!=": np.not_equal}


def _conditions(c: Cohort, conditions: list[list[Any]], absent: bool) -> np.ndarray:
    """AND of `[column, op, value]`; a pair missing a column reads `absent`."""
    out = np.ones(c.n, dtype=bool)
    for column, op, value in conditions:
        values, present = c.column(column)
        out &= np.where(present, _OPS[op](values, float(value)), absent)
    return out


@rung("column_proof")
def column_proof_rung(c: Cohort, d: Decisions, p: dict[str, Any]) -> Decisions:
    """A proof written as data: pairs in `from` zones whose columns meet every `all` condition
    merge, unless a stated fact separates them (`fact_veto`) or any `veto` condition holds.
    The operator's kitchen-to-kitchen proof (tag columns as EVIDENCE) is one config of this rung;
    a GPU job's image scores (`extra_features`) are another. A `veto` condition may not read a tag
    column: tags are never facts."""
    d = d.copy()
    zones = [dict(band=BAND, reject=REJECT)[z] for z in p.get("from", ("band",))]
    m = np.isin(d.zone, zones) & ~d.final & ~d.gated & _conditions(c, p["all"], False)
    for veto in p.get("veto", ()):
        _no_tag_fact(veto[0])
        m &= ~_conditions(c, [veto], False)
    if p.get("fact_veto", True):
        gate = c.lazy("gate", np.flatnonzero(m), p.get("settings"))["gate"]
        m &= np.array([not facts for facts in gate])
    d.settle(m, MERGE, "proof", p.get("name", "column"), p.get("carrier", "IMG"),
             "column_proof:" + p.get("name", "column"))
    return d


@rung("feature_veto")
def feature_veto_rung(c: Cohort, d: Decisions, p: dict[str, Any]) -> Decisions:
    """Demote merges on one feature bound, e.g. `{"feature": "floor_stated_conflict", "ge": 1}`;
    never on a tag column (tags are never facts)."""
    _no_tag_fact(p["feature"])
    d = d.copy()
    v, present = c.column(p["feature"])
    hit = present.copy()
    if "ge" in p:
        hit &= v >= float(p["ge"])
    if "le" in p:
        hit &= v <= float(p["le"])
    m = (d.zone == MERGE) & hit & ~np.isin(d.rung, list(p.get("spare", ())))
    d.settle(m, BAND, "fact", p["feature"], "IMG", _cat([d.reason, ":veto:" + p["feature"]]))
    d.gated |= m
    return d


# --- the group step -----------------------------------------------------------------------------

@dataclass
class Groups:
    clusters: dict[int, tuple[int, ...]]
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def member_of(self) -> dict[int, int]:
        return {m: k for k, members in self.clusters.items() for m in members}

    def co_pairs(self) -> set[tuple[int, int]]:
        return {(a, b) for members in self.clusters.values()
                for i, a in enumerate(members) for b in members[i + 1:]}


def storable(c: Cohort, d: Decisions) -> np.ndarray:
    """`store_score.storable` itself, pair by pair: a row carries evidence only when it is E61's
    veto (the two unit designators), the one lab decision that stores evidence below the band."""
    evidence = (d.zone == VETO) & (c.sig["veto"] == UNIT_DESIGNATOR_VETO)
    floor = c.settings.store_floor
    return np.fromiter((store_score.storable({"zone": ZONE_NAMES[z], "score": s, "evidence": e},
                                             floor)
                        for z, s, e in zip(d.zone.tolist(), d.score.tolist(), evidence.tolist())),
                       dtype=bool, count=c.n)


def merge_edges(c: Cohort, d: Decisions) -> list[Decision]:
    return [Decision(c.keys[i][0], c.keys[i][1], "merge", float(d.score[i]), set(),
                     d.cert[i] or None, None, d.reason[i], {})
            for i in np.flatnonzero(d.zone == MERGE)]


class _RelationMemo:
    """The D43 relation's per-pair answers, kept across arms: an answer is reused only when the
    pair's stored feature slots, its K-C certificate and the group step's settings overrides are
    what they were when it was read."""

    def __init__(self, c: Cohort, slots: dict[tuple[int, int], Any],
                 kc: dict[tuple[int, int], str] | None, tag: str = "") -> None:
        self.c, self.slots, self.kc, self.tag = c, slots, kc or {}, tag

    def _key(self, key: tuple[int, int]) -> tuple[Any, ...]:
        base = (key, key in self.slots, key in self.kc)
        return base + (self.tag,) if self.tag else base

    def get(self, key: tuple[int, int]) -> bool | None:
        return self.c.relation.get(self._key(key))

    def __setitem__(self, key: tuple[int, int], value: bool) -> None:
        self.c.relation[self._key(key)] = value
        self.c.dirty = True


@dataclass
class RelationParts:
    settings: Any
    slots: dict[tuple[int, int], Any]
    vetoed: frozenset[tuple[int, int]]
    relation: Any


def relation_parts(c: Cohort, d: Decisions, p: dict[str, Any]) -> RelationParts:
    """The group step's inputs: its settings (the row plus the arm's `settings` overrides), the
    stored feature slots, E61's machine vetoes and the memoised D43 relation."""
    overrides = p.get("settings") or {}
    settings = dataclasses.replace(c.settings, **overrides) if overrides else c.settings
    tag = json.dumps(overrides, sort_keys=True) if overrides else ""
    slot_idx = [FIDX[s] for s in FEATURE_SLOTS]
    slots = {c.keys[i]: {s: ((float(c.V[i, j]), True) if c.P[i, j] else (0.0, False))
                         for s, j in zip(FEATURE_SLOTS, slot_idx)}
             for i in np.flatnonzero(storable(c, d))}
    kc = ({c.keys[i]: "K-C" for i in np.flatnonzero(d.cert == "K-C")}
          if settings.d43_cluster_price_kc_house_number else None)
    vetoed = frozenset(c.keys[i] for i in np.flatnonzero(c.sig["veto"] == UNIT_DESIGNATOR_VETO))
    relation = relation_for(settings, c.ds.listings, slots, kc)
    if relation is not None and p.get("memo", True):
        relation._memo = _RelationMemo(c, slots, kc, tag)  # type: ignore[assignment]
    return RelationParts(settings, slots, vetoed, relation)


@group_step("relation")
def relation_group(c: Cohort, d: Decisions, p: dict[str, Any]) -> Groups:
    """The engine's own `cluster_pairs` with the D43 relation over the stored feature slots.
    `settings` overrides the group step's own dials for this arm only, e.g.
    `{"d43_cluster_image_facts": false}`: the relation then stops reading tag-derived facts."""
    parts = relation_parts(c, d, p)
    result = cluster_pairs(merge_edges(c, d), c.ds.listings, c.fps, parts.settings,
                           frozenset(p.get("must_not_link", ())), parts.relation,
                           must_link=frozenset(p.get("must_link", ())),
                           machine_vetoes=parts.vetoed)
    return Groups({int(k): tuple(sorted(int(m) for m in v)) for k, v in result.clusters.items()},
                  dict(result.stats))


def _walls(c: Cohort, x: int) -> tuple[frozenset[str], frozenset[str]]:
    listing = c.ds.listings.get(x) if c.ds is not None else None
    kind = getattr(listing, "category_type", None)
    main = getattr(listing, "category_main", None)
    return frozenset({kind} - {None}), frozenset({main} - {None})


@group_step("components")
def components_group(c: Cohort, d: Decisions, p: dict[str, Any]) -> Groups:
    """Connected components of the merge edges, strongest first, with no repair but the walls
    read on the whole set: an advert with no category is compatible with every other, so without
    the set read it would chain a flat to a commercial unit, or a sale to a rental."""
    parent: dict[int, int] = {}
    kinds: dict[int, frozenset[str]] = {}
    mains: dict[int, frozenset[str]] = {}

    def find(x: int) -> int:
        if x not in parent:
            parent[x] = x
            kinds[x], mains[x] = _walls(c, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    edges = np.flatnonzero(d.zone == MERGE)
    for i in edges[np.argsort(-d.score[edges], kind="stable")]:
        a, b = find(c.keys[i][0]), find(c.keys[i][1])
        if a == b:
            continue
        kind, main = kinds[a] | kinds[b], mains[a] | mains[b]
        if len(kind) > 1 or not all(category_main_compatible(x, y)
                                    for x, y in itertools.combinations(sorted(main), 2)):
            continue
        root = min(a, b)
        parent[max(a, b)] = root
        kinds[root], mains[root] = kind, main
    members: dict[int, list[int]] = {}
    for x in list(parent):
        members.setdefault(find(x), []).append(x)
    return Groups({k: tuple(sorted(v)) for k, v in members.items() if len(v) > 1})


# --- one experiment -----------------------------------------------------------------------------

REFERENCE_LADDER: tuple[str, ...] = ("veto", "auto_reject", "proof", "score", "context", "gate",
                                     "demonstrate", "policy")


@dataclass
class Outcome:
    config: dict[str, Any]
    decisions: Decisions
    groups: Groups
    timings: dict[str, float]
    walls_forced: bool = False
    scorer: dict[str, Any] = field(default_factory=dict)


def config_id(config: dict[str, Any]) -> str:
    body = {k: v for k, v in config.items() if k not in ("name", "note")}
    return hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:10]


def decide(c: Cohort, config: dict[str, Any], finish: bool = True,
           scorer: dict[str, Any] | None = None) -> tuple[Decisions, bool]:
    """The arm's ladder over every pair; `finish` settles what no rung reached as an unscored
    reject and names the score decisions' carriers. Returns the decisions and whether the config
    did not put the walls first (they are put first either way). `scorer` receives the score
    column's provenance."""
    c.extra = {}
    for path in config.get("extra_features", ()):
        c.load_extra(os.path.expandvars(str(path).format(cohort=c.name)))
    score, model, provenance = scores(config.get("model", "ref"), c)
    if scorer is not None:
        scorer.update(provenance)
    d = Decisions.blank(c.n, score)
    configured = [step for step in config.get("ladder", [{"rung": name} for name in REFERENCE_LADDER])
                  if step.get("on", True)]
    walls_forced = not configured or configured[0]["rung"] != "veto"
    # The standing rulings (never a rental with a sale, never a flat with a commercial unit) are
    # not a rung an arm may remove or reorder: the walls settle first, over every pair, and are
    # read again after the last rung, so no rung can carry a walled pair into any other zone.
    ladder = [{"rung": "veto"}] + [step for step in configured if step["rung"] != "veto"]
    for step in ladder:
        d = RUNGS[step["rung"]](c, d, step)
    d = RUNGS["veto"](c, d, {})
    if scorer is not None and d.provenance:
        scorer.clear()
        scorer.update(d.provenance)
    if not finish:
        return d, walls_forced
    rest = d.zone == U
    d.settle(rest, REJECT, "score", "unscored", "NONE", "unscored")
    if model is not None:
        split = family_split(model, c)
        stack = np.vstack([split[f] for f in FAMILIES])
        best = np.array(FAMILIES, dtype=object)[np.argmax(stack, axis=0)]
        worst = np.array(FAMILIES, dtype=object)[np.argmin(stack, axis=0)]
        scored = d.rung == "score"
        d.carrier[scored & (d.zone == MERGE)] = best[scored & (d.zone == MERGE)]
        d.carrier[scored & (d.zone != MERGE)] = worst[scored & (d.zone != MERGE)]
    return d, walls_forced


def run(c: Cohort, config: dict[str, Any]) -> Outcome:
    clock = time.perf_counter()
    scorer: dict[str, Any] = {}
    d, walls_forced = decide(c, config, scorer=scorer)
    decide_s = time.perf_counter() - clock
    group = config.get("group", {"step": "relation"})
    groups = GROUPS[group["step"]](c, d, group)
    c.save()
    timings = {"decide_s": decide_s, "group_s": time.perf_counter() - clock - decide_s}
    return Outcome(config, d, groups, timings, walls_forced, scorer)


def expand(config: dict[str, Any]) -> list[dict[str, Any]]:
    """A config with `"sweep": {"ladder.3.t_lo": [0.1, 0.3], ...}` is one arm per combination."""
    sweep = config.get("sweep") or {}
    if not sweep:
        return [config]
    arms = []
    paths = sorted(sweep)
    for values in itertools.product(*(sweep[path] for path in paths)):
        arm = copy.deepcopy({k: v for k, v in config.items() if k != "sweep"})
        for path, value in zip(paths, values):
            node: Any = arm
            parts = path.split(".")
            for part in parts[:-1]:
                node = node[int(part)] if isinstance(node, list) else node[part]
            last = parts[-1]
            if isinstance(node, list):
                node[int(last)] = value
            else:
                node[last] = value
        arm["name"] = f"{config.get('name', 'arm')}[" + ",".join(
            f"{path.split('.')[-1]}={value}" for path, value in zip(paths, values)) + "]"
        arms.append(arm)
    return arms


def load_config(path: str | Path) -> dict[str, Any]:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    config.setdefault("name", Path(path).stem)
    return config


# The challenger's rungs (`mf_score`, `facts`) and group step (`mf_union`) register on import.
from autodedup.lab import challenger as _challenger  # noqa: E402,F401
