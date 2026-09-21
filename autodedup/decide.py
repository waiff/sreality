"""The two-layer decision (PROGRAM.md §6): the rule floor, then the calibrated score.

Layer 1 is absolute and evaluated first — guards (E2–E5), the auto-reject rules, and the three
certificates (E24) whose precision is structural rather than learned (K-A demoted to propose-only
by default, W4c). Three gates then stand between any candidate and the merge zone: E45 requires one
UNIT-specific corroboration, E46 refuses catalogue-only agreement between co-live adverts, E47
refuses one broker's two adverts that ran side by side for weeks. Layer 2 is the logistic
model's calibrated probability cut into three zones by `t_hi`/`t_lo` (E22). Between the two
sits the evidence-diversity gate (E11): a merge needs corroboration from ≥2 of the five
families, so images alone can never merge a pair and a mis-weighted model cannot either. Over
both sits E48: every merge is read against ITS STRATUM's cut, and a stratum the evidence could
not carry ships propose-only — the only switch that reaches a certificate, which is decided
before any threshold is read.

Every refusal carries the NAME of the rule that refused it, so a run summary can say what the
rule floor removed instead of only how many pairs survived it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from autodedup.dataset import Listing
from autodedup.features import Feats, evidence_families, parse_ts, window_end_stamp
from autodedup.fingerprint import Fingerprint
from autodedup.guards import UNIT_DESIGNATOR_VETO, pair_veto, unit_designator_conflict
from autodedup.hazard_context import ContextIndex, PairContext, fungible_catalogue
from autodedup.indistinguishable import GATE, distinguishing_facts, promotion_warrant
from autodedup.model import LogisticModel
from autodedup.settings import Settings

ZONES: tuple[str, ...] = ("merge", "band", "reject", "veto")
CERTIFICATES: tuple[str, ...] = ("K-A", "K-B", "K-C", "K-R")
SIDES: tuple[str, ...] = ("same", "cross")

MIN_EVIDENCE_FAMILIES: int = 2
MAX_ATTR_CONTRADICTIONS: float = 3.0

UNIT_N_IMAGES_MIN: float = 1.0

# E63 names its own reasons so a run summary can say which arm promoted a pair and which
# limb refused one, without re-deriving either from the features.
CONTEXT_RULE_REASON: str = "context_rule"
# The two guards E63 may never overrule: E46 and E47 are the developer shapes the standing
# ruling is about, and a pair they banded is exactly the pair a text-and-price warrant
# cannot speak for (a developer's adverts share both by construction).
CONTEXT_RULE_NEVER_OVERRIDES: tuple[str, ...] = ("developer_signature", "developer_colive")

# E130/E131 name their own reasons for the reason E63 does: the store has to say WHY every g8
# merge exists and WHICH fact demoted each g7 merge, without re-running the pass.
D43_GATE_REASON: str = "d43_gate"
D43_PROMOTE_REASON: str = "d43_promote"

CERT_A_AREA: float = 0.02
CERT_B_AREA: float = 0.01
CERT_B_CONTAINMENT: float = 0.90
CERT_C_TIGHT_MATCHES: float = 4.0
CERT_C_SEQ_MONOTONE: float = 0.80
CERT_C_AREA: float = 0.03
CERT_C_CATALOG_RATIO: float = 0.20


@dataclass(slots=True)
class Decision:
    lo: int
    hi: int
    zone: str
    score: float
    families: set[str] = field(default_factory=set)
    certificate: str | None = None
    veto: str | None = None
    reason: str = ""
    # The STRINGS a rule refused or certified on, so a refusal can be adjudicated by reading it
    # rather than by re-running the pass (E61 carries the two unit designators, E60 the code).
    evidence: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {
            "lo": self.lo,
            "hi": self.hi,
            "zone": self.zone,
            "score": self.score,
            "families": sorted(self.families),
            "certificate": self.certificate,
            "veto": self.veto,
            "reason": self.reason,
            **({"evidence": dict(self.evidence)} if self.evidence else {}),
        }


def present_value(feats: Feats, name: str) -> float | None:
    """The feature's value when it is PRESENT, else None — E12 read as "unknown", not zero."""
    value, present = feats.get(name, (0.0, False))
    return float(value) if present else None


def window_gap_days(la: Listing, lb: Listing, settings: Settings | None = None) -> float | None:
    """The separation between the two live windows, in days — negative when they overlap.

    None when either window is unknown, which is never disjointness: a missing window can
    assert neither overlap nor its absence."""
    cfg = settings or Settings()
    starts = (parse_ts(la.first_seen_at), parse_ts(lb.first_seen_at))
    ends = (parse_ts(window_end_stamp(la, cfg)), parse_ts(window_end_stamp(lb, cfg)))
    if any(value is None for value in starts) or any(value is None for value in ends):
        return None
    later, earlier = max(starts[0], starts[1]), min(ends[0], ends[1])  # type: ignore[type-var]
    return float(later - earlier)  # `parse_ts` is already epoch DAYS


def disjoint_windows(la: Listing, lb: Listing, settings: Settings | None = None) -> bool:
    """K-B's load-bearing clause: the two adverts were never live at the same time.

    Unknown is not disjoint — a missing window can never assert the absence of overlap. Which
    end stamp counts is `features.window_end_stamp`'s question, not a second spelling here.

    E84: the separation has to clear `certificate_b_min_gap_days`, read on the PAIR. A window is
    only as sharp as the sightings behind it, and under the honest clock a once-seen advert's
    window is a point — so without a floor any two adverts one index walk saw for the first and
    only time are disjoint by construction, whatever else they are."""
    cfg = settings or Settings()
    gap = window_gap_days(la, lb, cfg)
    if gap is None:
        return False
    return gap > 0.0 and gap >= cfg.certificate_b_min_gap_days


def certificate_a(feats: Feats) -> bool:
    """Exact address-point identity plus agreeing unit attributes."""
    area = present_value(feats, "area_rel_diff")
    floor = present_value(feats, "floor_diff")
    return (
        present_value(feats, "same_ruian_adm_kod") == 1.0
        and present_value(feats, "dispo_equal") == 1.0
        and area is not None
        and area <= CERT_A_AREA
        and (floor is None or floor == 0.0)
    )


def certificate_b_image_floor(feats: Feats, settings: Settings) -> bool:
    """E65: K-B may not certify a pair whose photographs it never compared.

    Every clause of K-B — one portal, one broker, one template body, one stated size, windows
    that never touched — is satisfied BY CONSTRUCTION when a developer re-posts one template
    across several units of one project, so the certificate rests on no observation of the unit
    itself. It held only because the delisting-DETECTION clock inflated an inactive advert's
    window by up to 70.7 days and manufactured the overlap that K-B's disjointness clause reads
    as "not a re-post"; the honest clock (E62) removes that accident. The guard written for this
    exact shape — E46, catalogue-only agreement — cannot reach a K-B pair, because E46 requires
    `overlap_days > 0` and K-B requires 0: the two are disjoint by construction.

    So the floor is read here, on both sides and on the MATCHED set. `n_images_min` is the
    E9-subtracted count, so a gallery that is entirely catalogue stock counts as zero frames —
    which is the measured shape: of 1,600 K-B merges under the honest clock, 1,348 rest on
    galleries E9 emptied (`catalog_ratio_max` 1.0 on 1,318 of them) and NONE on adverts that
    carry no photograph at all. An ABSENT match count fails the floor: nothing to compare is
    not evidence of agreement, and `present_value` returning None is exactly that.

    The matched limb reads the LOOSE count, not K-C's tight one. K-C asks "the same shoot"; this
    asks only "was anything compared, and did anything agree" — a portal re-encode moves a hash
    by more than 6 bits, and on the g6 cohort the tight bar withholds 5 further dev-side
    labelled duplicates for no measured negative."""
    if settings.certificate_b_min_images > 0.0:
        images = present_value(feats, "n_images_min")
        if images is None or images < settings.certificate_b_min_images:
            return False
    if settings.certificate_b_min_matched_images > 0.0:
        matched = present_value(feats, "phash_loose_matches")
        if matched is None or matched < settings.certificate_b_min_matched_images:
            return False
    return True


def certificate_b(
    feats: Feats, la: Listing, lb: Listing, settings: Settings | None = None
) -> bool:
    """One broker's own re-post on one portal: same text, same size, never live together."""
    area = present_value(feats, "area_rel_diff")
    containment = present_value(feats, "containment_max")
    cfg = settings or Settings()
    return (
        present_value(feats, "same_source") == 1.0
        and present_value(feats, "same_broker_key") == 1.0
        and containment is not None
        and containment >= CERT_B_CONTAINMENT
        and area is not None
        and area <= CERT_B_AREA
        and certificate_b_image_floor(feats, cfg)
        and disjoint_windows(la, lb, settings)
    )


def certificate_c(feats: Feats) -> bool:
    """Four or more non-catalog photos in gallery order — the same shoot, not a catalogue."""
    tight = present_value(feats, "phash_tight_matches")
    monotone = present_value(feats, "seq_monotone_ratio")
    area = present_value(feats, "area_rel_diff")
    catalog = present_value(feats, "catalog_ratio_max")
    return (
        tight is not None
        and tight >= CERT_C_TIGHT_MATCHES
        and monotone is not None
        and monotone >= CERT_C_SEQ_MONOTONE
        and area is not None
        and area <= CERT_C_AREA
        and present_value(feats, "dispo_equal") == 1.0
        and catalog is not None
        and catalog <= CERT_C_CATALOG_RATIO
    )


def certificate_r(feats: Feats) -> bool:
    """E60: both bodies print the SAME rare agency order code.

    An order number is the agency's key for ONE order, and one order is one unit: it travels
    onto every portal the order is syndicated to and onto the re-post when the advert expires.
    `features.index_reference_codes` has already thrown out the codes a crowd carries and the
    codes whose carriers disagree about the unit, so what reaches here certifies.

    A DIFFERING code certifies nothing in either direction — W6b built the conflict rule and W7
    refuted it on 395722 x 486034 and 395722 x 496635, one flat carrying N115815 on
    ceskereality and N118731 on sreality with identical text and an identical 7 974 910 Kc.
    That is why the feature is ABSENT rather than 0.0 when the codes differ, and why this
    reads a presence, never a value."""
    return present_value(feats, "ref_code_shared") == 1.0


def certificate_of(
    feats: Feats,
    la: Listing,
    lb: Listing,
    settings: Settings | None = None,
    kb_refused: bool = False,
) -> str | None:
    """The first certificate the pair earns, in K-A, K-B, K-C order.

    K-R is read FIRST because it is the only certificate whose premise is a broker's own
    statement about which order this advert is, rather than an inference from what the two
    adverts look like. K-A is off unless `settings.certificate_ka_enabled` says otherwise — an OMITTED settings row
    leaves it off too, because the default must be the decision the evidence supports: K-A merged
    at 52.6% HT precision (n=98) on the gold holdout against K-B's 100% (n=94) and K-C's 97.6%
    (n=150), one RUIAN point plus disposition plus area certifying a BUILDING, which is the
    developer-unit false-merge shape itself. The code path stays so an evaluation can switch it
    back on.

    `kb_refused` is E85's one entry point: the family pass runs AFTER every pair has been
    decided (a family is a property of the pair set, not of a pair), and a pair whose family
    refuses it is re-decided with K-B withdrawn — so it falls to K-C or to the model exactly as
    an uncertified pair does. Nothing else about the pair changes: the guard removes evidence,
    it never manufactures a contradiction."""
    if (settings is None or settings.certificate_kr_enabled) and certificate_r(feats):
        return "K-R"
    if settings is not None and settings.certificate_ka_enabled and certificate_a(feats):
        return "K-A"
    if not kb_refused and certificate_b(feats, la, lb, settings):
        return "K-B"
    if certificate_c(feats):
        return "K-C"
    return None


def _interior_arm(feats: Feats, settings: Settings) -> bool:
    """Non-catalogue INTERIOR frames agreeing on both sides — the one arm K-A survived on
    (K-A + interior_match_ratio >= 0.5 ran 88.6%, n=35, against K-A's 52.6% overall)."""
    interior = present_value(feats, "interior_match_ratio")
    images = present_value(feats, "n_images_min")
    return (
        interior is not None
        and interior >= settings.unit_interior_min
        and images is not None
        and images >= UNIT_N_IMAGES_MIN
    )


def _repost_arm(feats: Feats, settings: Settings) -> bool:
    """K-B's shape: one broker, one portal, containment, and windows that never overlapped.
    100% (n=94) on the gold holdout — the strongest arm there is."""
    containment = present_value(feats, "containment_max")
    return (
        present_value(feats, "same_source") == 1.0
        and present_value(feats, "same_broker_key") == 1.0
        and containment is not None
        and containment >= settings.unit_containment_min
        and present_value(feats, "overlap_days") == 0.0
    )


def _unit_number_arm(feats: Feats) -> bool:
    """One address point, one floor, and the SAME unit number stated by both adverts."""
    return (
        present_value(feats, "same_ruian_adm_kod") == 1.0
        and present_value(feats, "floor_diff") == 0.0
        and present_value(feats, "unit_number_shared") == 1.0
    )


def _rare_token_arm(feats: Feats, settings: Settings) -> bool:
    """Rare shared tokens, which corroborate a UNIT only with a non-catalogue photo beside them.

    A rare token is as often the PROJECT name as the unit's own, so the arm was measured on its
    own: with E46 widened below, every merge resting on rare tokens alone is a judged positive
    (19/19, HT 100%), and requiring a non-catalogue photo beside them cost 1.9 points of weighted
    recall for none of precision. `unit_rare_requires_support` keeps that clause as a dial."""
    rare = present_value(feats, "rare_token_overlap")
    if rare is None or rare < settings.unit_rare_tokens_min:
        return False
    if not settings.unit_rare_requires_support:
        return True
    interior = present_value(feats, "interior_match_ratio")
    images = present_value(feats, "n_images_min")
    return (
        interior is not None
        and interior >= settings.unit_rare_support_interior_min
        and images is not None
        and images >= UNIT_N_IMAGES_MIN
    )


def _ref_code_arm(feats: Feats) -> bool:
    """E60 inside E45: an order key names one ORDER, so it is unit-grade by construction.

    The arm the gate was missing. Of the pairs a shared code certifies, 62.8% carry
    `rare_token_overlap` 0.0 — one token cannot be rare in a block of fewer than 20 documents —
    so without this arm the strongest positive in the cohort reaches the gate with no evidence
    family that speaks about a unit."""
    return certificate_r(feats)


def unit_grade_evidence(feats: Feats, settings: Settings) -> bool:
    """The arms that held up: interior frames, the K-B re-post, a unit number, an order code."""
    return (
        _interior_arm(feats, settings)
        or _repost_arm(feats, settings)
        or _unit_number_arm(feats)
        or _ref_code_arm(feats)
    )


def unit_evidence(feats: Feats, settings: Settings) -> bool:
    """E45: at least one UNIT-specific corroboration, or the pair may not enter the merge zone.

    Building-grade agreement (one address, one catalogue, one template, one project name) is not
    evidence about a unit — it is the developer-unit false-merge shape."""
    return unit_grade_evidence(feats, settings) or _rare_token_arm(feats, settings)


def developer_signature(feats: Feats, settings: Settings) -> bool:
    """E46: adverts that were live TOGETHER and agree only on catalogue material.

    Every hand-checked K-A false merge had this shape. The same/one-broker clauses are
    tighteners, not requirements (`developer_signature_same_broker_only`): two of the residual
    judged false merges were the identical catalogue-only shape across two PORTALS
    (83202 x 10515714, 83467 x 10515837; both gold `same_building_different_unit`)."""
    overlap = present_value(feats, "overlap_days")
    catalog = present_value(feats, "catalog_ratio_max")
    interior = present_value(feats, "interior_match_ratio")
    if settings.developer_signature_same_broker_only and not (
        present_value(feats, "same_source") == 1.0
        and present_value(feats, "same_broker_key") == 1.0
    ):
        return False
    return (
        overlap is not None
        and overlap > 0.0
        and catalog is not None
        and catalog >= settings.developer_catalog_ratio_min
        and (interior is None or interior < settings.unit_interior_min)
    )


def developer_colive(feats: Feats, settings: Settings) -> bool:
    """E47: one broker, one portal, both adverts live for WEEKS, and no shared unit number.

    K-A's demotion re-routed the shape rather than removing it — 319 of its 838 merges came back
    as K-B, K-C or model at 90.4% HT (n=54), including the worst historical false merge
    (21000 x 27781, gold `same_building_different_unit`, 61 co-live days, one broker, one portal,
    interior_match_ratio 1.0 because the two units shared a photo shoot). No image or text rule
    can separate two units of one project that were photographed together, so the co-live window
    itself is the signal: one advert replacing another (E6) does not run beside it for weeks."""
    overlap = present_value(feats, "overlap_days")
    return (
        present_value(feats, "same_source") == 1.0
        and present_value(feats, "same_broker_key") == 1.0
        and overlap is not None
        and overlap > settings.colive_overlap_days
        and present_value(feats, "unit_number_shared") != 1.0
    )


def stratum_key(feats: Feats, certificate: str | None) -> str:
    """The stratum D3's per-stratum floor is read on: deciding layer x source side (E48).

    Spelled exactly as `evaluate.decide_stratum` spells it, so the table an evaluation writes is
    the table the engine reads — `K-C|same`, `model|cross`. Side is read off `same_source`, and an
    ABSENT `same_source` reads as `cross`: two listings the row cannot prove came from one portal
    are the harder regime, and a gate must fail towards the stricter cell, not the laxer one."""
    return f"{certificate or 'model'}|" + (
        "same" if present_value(feats, "same_source") == 1.0 else "cross"
    )


def stratum_t_hi(feats: Feats, certificate: str | None, settings: Settings) -> float | None:
    """The cut THIS pair's stratum merges at, or None for propose-only (E48).

    None is the whole point of the table: a stratum that could not prove the bar merges nothing,
    and because a certificate is read before `t_hi`, this is the only switch that can hold one
    back. A stratum with no entry runs on the global `t_hi`."""
    table = settings.t_hi_by_stratum
    if table:
        key = stratum_key(feats, certificate)
        if key in table:
            value = table[key]
            return None if value is None else float(value)
    return settings.t_hi


def auto_reject_reason(feats: Feats, settings: Settings | None = None) -> str | None:
    """A contradiction on a portal-VOCABULARY slot never reaches here: `features.pair_features`
    drops those slots from `attr_contradictions` (and from the agreements) before the count."""
    limit = MAX_ATTR_CONTRADICTIONS if settings is None else settings.max_attr_contradictions
    contradictions = present_value(feats, "attr_contradictions")
    if contradictions is not None and contradictions >= limit:
        return "attr_contradictions"
    if present_value(feats, "numeral_conflict") == 1.0:
        return "numeral_conflict"
    return None


def merge_zone_block(feats: Feats, settings: Settings) -> str | None:
    """The name of the gate that keeps this pair out of the merge zone, or None (E45/E46/E47)."""
    if settings.developer_signature_guard and developer_signature(feats, settings):
        return "developer_signature"
    if settings.developer_colive_guard and developer_colive(feats, settings):
        return "developer_colive"
    if settings.unit_evidence_required and not unit_evidence(feats, settings):
        return "unit_evidence_gate"
    return None


def context_rule_warrant(feats: Feats, score: float, settings: Settings) -> str | None:
    """E63: the arm this band pair carries, or None.

    ONE arm is live — a near-identical body, an identical current price and a score at the top
    of the isotonic range — because it is the only one the W8 verification read clean at both
    grains on both splits (347 g5 band pairs, 0 negatives in 110 labelled, 0 negative blocks in
    66, 0 in 33 sealed). The interior arm beside it is OFF and carries C2's image floor, so a
    settings row that reaches for it cannot reach for three photos."""
    if score < settings.context_rule_min_score:
        return None
    area = present_value(feats, "area_rel_diff")
    if area is None or area > settings.context_rule_area_max:
        return None
    price = present_value(feats, "price_last_ratio")
    if price is None or price < settings.context_rule_price_ratio_min:
        return None
    containment = present_value(feats, "containment_max")
    if containment is not None and containment >= settings.context_rule_containment_min:
        return "text"
    if settings.context_rule_interior_min is not None:
        interior = present_value(feats, "interior_match_ratio")
        images = present_value(feats, "n_images_min")
        if (interior is not None
                and interior >= settings.context_rule_interior_min
                and images is not None
                and images >= settings.context_rule_min_images):
            return "interior"
    return None


def context_rule_may_promote(reason: str) -> bool:
    """E63 re-reads E11, E45 and E48's propose-only cells; it never re-reads E46 or E47."""
    return not any(name in reason for name in CONTEXT_RULE_NEVER_OVERRIDES)


def _census_limbs_configured(settings: Settings) -> bool:
    return (settings.context_rule_block_min is not None
            or settings.context_rule_image_population_min is not None)


def apply_context_rule(
    decision: Decision,
    feats: Feats,
    la: Listing,
    lb: Listing,
    settings: Settings,
    context: ContextIndex | PairContext | None,
) -> Decision:
    """Promote a band pair the E63 warrant carries — unless a fungible-catalogue limb refuses.

    A census limb with no index to read is a limb that cannot answer, and an unanswered guard
    fails towards the stricter side (E48's direction): the promotion is simply not made."""
    if not settings.context_rule_enabled or decision.zone != "band":
        return decision
    if not context_rule_may_promote(decision.reason):
        return decision
    arm = context_rule_warrant(feats, decision.score, settings)
    if arm is None:
        return decision
    pair_context = (
        context.pair_context(la, lb) if isinstance(context, ContextIndex) else context
    )
    if pair_context is None:
        if _census_limbs_configured(settings):
            return decision
        evidence = dict(decision.evidence)
    else:
        refused = fungible_catalogue(
            pair_context.stamp,
            pair_context.from_price,
            block_min=settings.context_rule_block_min,
            image_population_min=settings.context_rule_image_population_min,
            from_price_veto=settings.context_rule_from_price_veto,
        )
        if refused is not None:
            return Decision(
                decision.lo, decision.hi, "band", decision.score, decision.families,
                decision.certificate, None,
                f"{CONTEXT_RULE_REASON}:fungible:{refused}", dict(decision.evidence),
            )
        evidence = {**decision.evidence, **pair_context.stamp.to_evidence()}
    return Decision(
        decision.lo, decision.hi, "merge", decision.score, decision.families,
        decision.certificate, None, f"{CONTEXT_RULE_REASON}:{arm}", evidence,
    )


def apply_d43_rule(
    decision: Decision,
    la: Listing,
    lb: Listing,
    feats: Feats,
    settings: Settings,
) -> Decision:
    """D43 at the decide layer: the gate demotes, the promotion promotes, both named.

    The GATE reads the permissive area bar (E136): it is about to overrule evidence the engine
    already certified, and a parse defect in the 3-8 % band must not split a certified merge.
    The PROMOTION reads the strict one, because merging on the ABSENCE of a fact is the one
    place the engine has no positive evidence to fall back on.

    A veto or an auto-reject is never reached — the ruling is about which adverts are one unit,
    not about the rule floor that says they cannot be compared at all.
    """
    if decision.zone == "merge" and settings.d43_gate:
        facts = distinguishing_facts(la, lb, feats, settings, GATE)
        if facts:
            return Decision(
                decision.lo, decision.hi, "band", decision.score, decision.families,
                decision.certificate, None,
                f"{decision.reason}:{D43_GATE_REASON}:{facts[0].name}",
                {**decision.evidence,
                 f"{D43_GATE_REASON}_facts": ",".join(fact.name for fact in facts)},
            )
        return decision
    if decision.zone == "band" and settings.d43_promote:
        warrant = promotion_warrant(la, lb, feats, settings)
        if warrant is not None:
            return Decision(
                decision.lo, decision.hi, "merge", decision.score, decision.families,
                decision.certificate, None,
                f"{D43_PROMOTE_REASON}:{warrant}",
                {**decision.evidence, "d43_banded_as": decision.reason},
            )
    return decision


def decide_pair(
    fa: Fingerprint,
    fb: Fingerprint,
    la: Listing,
    lb: Listing,
    feats: Feats,
    probes: Iterable[str],
    model: LogisticModel,
    settings: Settings,
    context: ContextIndex | PairContext | None = None,
    kb_refused: bool = False,
) -> Decision:
    """Guards, then auto-rejects, then certificates, then the calibrated score — in that order,
    and then E63 re-reads what landed in the band.

    Every merge, certificate or score, is read against ITS STRATUM's cut (E48): a stratum the
    evidence could not clear ships propose-only and lands in the band whatever it earned. E63
    is the one path back out of that band, and it can only ever read a pair the layers above it
    have already decided — it never reaches a veto, an auto-reject or a developer guard."""
    decision = _decide_layers(fa, fb, la, lb, feats, probes, model, settings, kb_refused)
    decision = apply_context_rule(decision, feats, la, lb, settings, context)
    return apply_d43_rule(decision, la, lb, feats, settings)


def _decide_layers(
    fa: Fingerprint,
    fb: Fingerprint,
    la: Listing,
    lb: Listing,
    feats: Feats,
    probes: Iterable[str],
    model: LogisticModel,
    settings: Settings,
    kb_refused: bool = False,
) -> Decision:
    """The rule floor and the calibrated score — every zone E63 is then allowed to re-read."""
    lo, hi = (fa.listing_id, fb.listing_id) if fa.listing_id < fb.listing_id else (
        fb.listing_id, fa.listing_id
    )
    veto = pair_veto(fa, fb, settings)
    if veto is not None:
        return Decision(lo, hi, "veto", 0.0, set(), None, veto, f"guard:{veto}")

    # E61 is a guard, not a score: it reads the two BODIES, which `pair_veto`'s fingerprint-grain
    # sides cannot see, so it stands here rather than inside it.
    designators = unit_designator_conflict(la, lb, settings)
    if designators is not None:
        return Decision(
            lo, hi, "veto", 0.0, set(), None, UNIT_DESIGNATOR_VETO,
            f"guard:{UNIT_DESIGNATOR_VETO}",
            {"unit_lo": designators[0], "unit_hi": designators[1]},
        )

    families = evidence_families(feats)
    score = model.predict_proba(feats)

    rejected = auto_reject_reason(feats, settings)
    if rejected is not None:
        return Decision(lo, hi, "reject", score, families, None, None, f"auto_reject:{rejected}")

    diverse = len(families) >= settings.min_evidence_families
    certificate = certificate_of(feats, la, lb, settings, kb_refused)
    if certificate is not None:
        if stratum_t_hi(feats, certificate, settings) is None:
            return Decision(lo, hi, "band", score, families, certificate, None,
                            f"certificate:{certificate}:stratum_propose_only")
        blocked = merge_zone_block(feats, settings)
        if blocked is not None:
            return Decision(lo, hi, "band", score, families, certificate, None,
                            f"certificate:{certificate}:{blocked}")
        if diverse:
            return Decision(lo, hi, "merge", score, families, certificate, None,
                            f"certificate:{certificate}")
        return Decision(lo, hi, "band", score, families, certificate, None,
                        f"certificate:{certificate}:evidence_gate")

    cut = stratum_t_hi(feats, None, settings)
    if cut is not None and score >= cut:
        blocked = merge_zone_block(feats, settings)
        if blocked is not None:
            return Decision(lo, hi, "band", score, families, None, None, blocked)
        if diverse:
            return Decision(lo, hi, "merge", score, families, None, None, "model")
        return Decision(lo, hi, "band", score, families, None, None, "evidence_gate")
    if score > settings.t_lo:
        return Decision(lo, hi, "band", score, families, None, None, "model")
    return Decision(lo, hi, "reject", score, families, None, None, "model")
