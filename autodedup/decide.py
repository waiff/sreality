"""The two-layer decision (PROGRAM.md §6): the rule floor, then the calibrated score.

Layer 1 is absolute and evaluated first — guards (E2–E5), the auto-reject rules, and the three
certificates (E24) whose precision is structural rather than learned. Layer 2 is the logistic
model's calibrated probability cut into three zones by `t_hi`/`t_lo` (E22). Between the two
sits the evidence-diversity gate (E11): a merge needs corroboration from ≥2 of the five
families, so images alone can never merge a pair and a mis-weighted model cannot either.

Every refusal carries the NAME of the rule that refused it, so a run summary can say what the
rule floor removed instead of only how many pairs survived it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from autodedup.dataset import Listing
from autodedup.features import Feats, evidence_families, parse_ts
from autodedup.fingerprint import Fingerprint
from autodedup.guards import pair_veto
from autodedup.model import LogisticModel
from autodedup.settings import Settings

ZONES: tuple[str, ...] = ("merge", "band", "reject", "veto")
CERTIFICATES: tuple[str, ...] = ("K-A", "K-B", "K-C")

MIN_EVIDENCE_FAMILIES: int = 2
MAX_ATTR_CONTRADICTIONS: float = 3.0

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
        }


def present_value(feats: Feats, name: str) -> float | None:
    """The feature's value when it is PRESENT, else None — E12 read as "unknown", not zero."""
    value, present = feats.get(name, (0.0, False))
    return float(value) if present else None


def disjoint_windows(la: Listing, lb: Listing) -> bool:
    """K-B's load-bearing clause: the two adverts were never live at the same time.

    Unknown is not disjoint — a missing window can never assert the absence of overlap."""
    starts = (parse_ts(la.first_seen_at), parse_ts(lb.first_seen_at))
    ends = (
        parse_ts(la.inactive_at) or parse_ts(la.last_seen_at),
        parse_ts(lb.inactive_at) or parse_ts(lb.last_seen_at),
    )
    if any(value is None for value in starts) or any(value is None for value in ends):
        return False
    return max(starts[0], starts[1]) > min(ends[0], ends[1])  # type: ignore[operator]


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


def certificate_b(feats: Feats, la: Listing, lb: Listing) -> bool:
    """One broker's own re-post on one portal: same text, same size, never live together."""
    area = present_value(feats, "area_rel_diff")
    containment = present_value(feats, "containment_max")
    return (
        present_value(feats, "same_source") == 1.0
        and present_value(feats, "same_broker_key") == 1.0
        and containment is not None
        and containment >= CERT_B_CONTAINMENT
        and area is not None
        and area <= CERT_B_AREA
        and disjoint_windows(la, lb)
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


def certificate_of(feats: Feats, la: Listing, lb: Listing) -> str | None:
    """The first certificate the pair earns, in K-A, K-B, K-C order."""
    if certificate_a(feats):
        return "K-A"
    if certificate_b(feats, la, lb):
        return "K-B"
    if certificate_c(feats):
        return "K-C"
    return None


def auto_reject_reason(feats: Feats) -> str | None:
    contradictions = present_value(feats, "attr_contradictions")
    if contradictions is not None and contradictions >= MAX_ATTR_CONTRADICTIONS:
        return "attr_contradictions"
    if present_value(feats, "numeral_conflict") == 1.0:
        return "numeral_conflict"
    return None


def decide_pair(
    fa: Fingerprint,
    fb: Fingerprint,
    la: Listing,
    lb: Listing,
    feats: Feats,
    probes: Iterable[str],
    model: LogisticModel,
    settings: Settings,
) -> Decision:
    """Guards, then auto-rejects, then certificates, then the calibrated score — in that order."""
    lo, hi = (fa.listing_id, fb.listing_id) if fa.listing_id < fb.listing_id else (
        fb.listing_id, fa.listing_id
    )
    veto = pair_veto(fa, fb, settings)
    if veto is not None:
        return Decision(lo, hi, "veto", 0.0, set(), None, veto, f"guard:{veto}")

    families = evidence_families(feats)
    score = model.predict_proba(feats)

    rejected = auto_reject_reason(feats)
    if rejected is not None:
        return Decision(lo, hi, "reject", score, families, None, None, f"auto_reject:{rejected}")

    diverse = len(families) >= MIN_EVIDENCE_FAMILIES
    certificate = certificate_of(feats, la, lb)
    if certificate is not None:
        if diverse:
            return Decision(lo, hi, "merge", score, families, certificate, None,
                            f"certificate:{certificate}")
        return Decision(lo, hi, "band", score, families, certificate, None,
                        f"certificate:{certificate}:evidence_gate")

    if score >= settings.t_hi:
        if diverse:
            return Decision(lo, hi, "merge", score, families, None, None, "model")
        return Decision(lo, hi, "band", score, families, None, None, "evidence_gate")
    if score > settings.t_lo:
        return Decision(lo, hi, "band", score, families, None, None, "model")
    return Decision(lo, hi, "reject", score, families, None, None, "model")
