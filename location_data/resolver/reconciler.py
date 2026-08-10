"""S9 — the deterministic reconciler (03 §3.11). **The reconciler is code, not a model.**

The architecture-determining measurement: small-model major-contradiction recall was
**1 / 9 (11 %)** with a 1 : 7 signal-to-noise ratio; on the two realitymix rows it read the
location correctly, sat it next to a stored pin 180 km away in Slovakia with the whole admin
hierarchy NULL, and emitted `"contradictions": []` — twice. "The failure is not in reading
text — it is in the comparison step, which is pure string/geometry logic that does not need
a model at all."

Rule set v1 is the cheap structural half of §3.11.1: everything computable from the claims,
the resolution and the registry with no network and no model.

Ledger discipline (00 §8, 03 §3.11.2):

* detections are APPEND-ONLY; nothing here ever UPDATEs one;
* `dedupe_key` is VERSION-FREE — a stable hash over
  `(listing_id, rule, field, normalized_claimed_value, normalized_served_value)`, and
  `listing_id` is INSIDE it so the disposition table can key on it standalone. It
  deliberately excludes `reconciler_version`, `registry_version_id` and `snapshot_id`:
  bumping the reconciler version is routine (one per shipped rule) and every bump would
  otherwise orphan every operator judgement and re-flood the queue with decided cards;
* **auto-close is an APPENDED disposition, never an edit**, and fires only when the
  predicate stops firing AND the inputs changed. A re-run that merely happens again closes
  nothing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from location_data.resolver.normalize import normalize_match_key
from location_data.resolver.types import (
    Claim,
    NormalizedClaim,
    RegistryView,
    Resolution,
)

SEVERITIES = ("major", "minor", "info")


@dataclass(frozen=True, slots=True)
class Detection:
    listing_id: int
    rule: str
    field: str
    severity: str
    dedupe_key: str
    stored: Any = None
    claimed: Any = None
    distance_m: float | None = None
    evidence_claim_ids: tuple[int, ...] = ()
    served_claim_id: int | None = None
    claimed_claim_id: int | None = None
    auto_action: str = "none"
    evidence_quote: str | None = None


@dataclass(frozen=True, slots=True)
class AutoClose:
    dedupe_key: str
    reason: str
    status: str = "resolved_upstream"
    decided_by: str = "reconciler"


def dedupe_key(
    listing_id: int, rule: str, field_name: str, claimed: Any, served: Any
) -> str:
    """The version-free identity (00 §8.2). Values are normalized before hashing so a
    diacritic or a case change is the SAME finding, not a new card."""
    payload = "\x1f".join(
        [
            str(listing_id),
            rule,
            field_name,
            _norm_value(claimed),
            _norm_value(served),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _norm_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{float(value):.4f}"
    if isinstance(value, (list, tuple)):
        return "|".join(_norm_value(v) for v in value)
    if isinstance(value, dict):
        return "|".join(f"{k}={_norm_value(v)}" for k, v in sorted(value.items()))
    return normalize_match_key(str(value))


def _detect(
    listing_id: int,
    rule: str,
    field_name: str,
    severity: str,
    *,
    stored: Any = None,
    claimed: Any = None,
    distance_m: float | None = None,
    evidence_claim_ids: Sequence[int] = (),
    auto_action: str = "none",
    evidence_quote: str | None = None,
) -> Detection:
    return Detection(
        listing_id=listing_id,
        rule=rule,
        field=field_name,
        severity=severity,
        dedupe_key=dedupe_key(listing_id, rule, field_name, claimed, stored),
        stored=stored,
        claimed=claimed,
        distance_m=distance_m,
        evidence_claim_ids=tuple(sorted(evidence_claim_ids)),
        claimed_claim_id=(min(evidence_claim_ids) if evidence_claim_ids else None),
        auto_action=auto_action,
        evidence_quote=evidence_quote,
    )


def run(
    resolution: Resolution,
    claims: Sequence[Claim],
    normalized: dict[int, NormalizedClaim],
    *,
    registry: RegistryView,
    per_source_locality_trusted: bool = False,
) -> list[Detection]:
    """Rule set v1. Every rule is a pure predicate over (claims, resolution, registry)."""
    return run_with_coverage(
        resolution, claims, normalized, registry=registry,
        per_source_locality_trusted=per_source_locality_trusted,
    )[0]


def run_with_coverage(
    resolution: Resolution,
    claims: Sequence[Claim],
    normalized: dict[int, NormalizedClaim],
    *,
    registry: RegistryView,
    per_source_locality_trusted: bool = False,
) -> tuple[list[Detection], frozenset[str]]:
    """`run`, plus the set of rules this run actually EVALUATED.

    Auto-close reads it (00 §8.2): a rule whose guard was not satisfied this time — the
    street survivorship produced no winner, so `street_not_in_obec` never ran — did not
    "stop firing". It was not asked. Closing its open finding would record an upstream fix
    that never happened."""
    listing_id = resolution.listing_id
    out: list[Detection] = []
    evaluated: set[str] = set(_signal_rules_evaluated(resolution))
    served_street = _served(resolution, "street_name")
    served_obec = resolution.admin.obec_name
    obec_kod = resolution.admin.obec_kod

    # ---- the signals S4/S6/S2 already produced (country_dispute, pin_registry_distance,
    # declared_precision_vs_assigned). They are detections, not a second detector.
    for signal in resolution.contradiction_signals:
        out.append(
            _detect(
                listing_id,
                signal.rule,
                signal.field,
                signal.severity,
                stored=signal.stored,
                claimed=signal.claimed,
                distance_m=signal.distance_m,
                evidence_claim_ids=signal.evidence_claim_ids,
                auto_action=signal.auto_action,
                evidence_quote=signal.evidence_quote,
            )
        )

    # ---- street_not_in_obec: the winning street must exist in the resolved obec.
    if served_street and obec_kod is not None:
        evaluated.add("street_not_in_obec")
        key = normalize_match_key(served_street)
        if not any(s.name_norm == key for s in registry.streets_in_obec(obec_kod)):
            out.append(
                _detect(
                    listing_id, "street_not_in_obec", "street_name", "major",
                    stored=served_street, claimed=served_obec,
                )
            )

    # ---- street_from_excluded_block_vs_served: the remax carousel class. A
    # `subject_scoped=false` extraction is stored, never rankable, and DOES open this.
    if served_street:
        evaluated.add("street_from_excluded_block_vs_served")
    for claim in sorted(claims, key=lambda c: c.id):
        if claim.claim_type != "street_name" or claim.subject_scoped is not False:
            continue
        if served_street and normalize_match_key(claim.value_text or "") != normalize_match_key(
            served_street
        ):
            out.append(
                _detect(
                    listing_id, "street_from_excluded_block_vs_served", "street_name", "major",
                    stored=served_street, claimed=claim.value_text,
                    evidence_claim_ids=(claim.id,), evidence_quote=claim.value_text,
                )
            )

    # ---- street_claim_vs_derived: a validated text street claim ≠ the derived street.
    derived_street = _derived_street(resolution)
    if derived_street and served_street:
        evaluated.add("street_claim_vs_derived")
        for claim in sorted(claims, key=lambda c: c.id):
            if claim.claim_type != "street_name" or claim.subject_scoped is False:
                continue
            if claim.extraction_method in ("registry_derived", "legacy_column"):
                continue
            if normalize_match_key(claim.value_text or "") != normalize_match_key(derived_street):
                out.append(
                    _detect(
                        listing_id, "street_claim_vs_derived", "street_name", "major",
                        stored=derived_street, claimed=claim.value_text,
                        evidence_claim_ids=(claim.id,),
                    )
                )

    # ---- house_number_disagreement: two claims give different čp. Two portals disagreeing
    # on a house number is a DO-NOT-MERGE signal, not a survivorship tie.
    evaluated.add("house_number_disagreement")
    numbers: dict[str, list[int]] = {}
    for claim in sorted(claims, key=lambda c: c.id):
        if claim.claim_type != "house_number_cp" or claim.subject_scoped is False:
            continue
        norm = normalized.get(claim.id)
        value = str((norm.typed_slots.get("cislo_domovni") if norm else None) or claim.value_text or "")
        if value:
            numbers.setdefault(value, []).append(claim.id)
    if len(numbers) > 1:
        ordered = sorted(numbers.items())
        out.append(
            _detect(
                listing_id, "house_number_disagreement", "house_number_cp", "major",
                stored=ordered[0][0], claimed=[k for k, _ in ordered[1:]],
                evidence_claim_ids=[cid for _, ids in ordered for cid in ids],
            )
        )

    # ---- obec_claim_vs_resolution, AFTER the post-town exclusion (a Czech Post town is not
    # an obec and differs on 57.0 % of bazos rows).
    if served_obec and per_source_locality_trusted:
        evaluated.add("obec_claim_vs_resolution")
    if served_obec:
        for claim in sorted(claims, key=lambda c: c.id):
            if claim.claim_type != "obec_name" or claim.subject_scoped is False:
                continue
            if not per_source_locality_trusted:
                continue
            if normalize_match_key(claim.value_text or "") != normalize_match_key(served_obec):
                out.append(
                    _detect(
                        listing_id, "obec_claim_vs_resolution", "obec_name", "major",
                        stored=served_obec, claimed=claim.value_text,
                        evidence_claim_ids=(claim.id,),
                    )
                )

    # ---- cadastral_vs_obec: a k.ú. claim that maps to a different obec.
    if obec_kod is not None:
        evaluated.add("cadastral_vs_obec")
    for claim in sorted(claims, key=lambda c: c.id):
        if claim.claim_type != "cadastral_territory_name" or claim.subject_scoped is False:
            continue
        key = normalize_match_key(claim.value_text or "")
        obec_codes = {
            ancestor.code
            for unit in registry.admin_units_by_name(key, levels=("katastralni_uzemi",))
            for ancestor in registry.admin_chain(unit.unit_id)
            if ancestor.level == "obec"
        }
        if obec_codes and obec_kod is not None and obec_kod not in obec_codes:
            out.append(
                _detect(
                    listing_id, "cadastral_vs_obec", "cadastral_territory_name", "major",
                    stored=obec_kod, claimed=sorted(obec_codes),
                    evidence_claim_ids=(claim.id,),
                )
            )

    # ---- pin_collapse_with_heterogeneity: a cluster at/over threshold with ≥2 streets.
    collision = resolution.precision.collision
    if "threshold_n" in collision:
        evaluated.add("pin_collapse_with_heterogeneity")
    if (
        # `>=` — 03 §3.11.1 spells the rule "cluster >= threshold with >=2 distinct
        # streets", the same reading as the S6 cap and the epoch classifier.
        int(collision.get("n_exact", 1)) >= int(collision.get("threshold_n", 1))
        and int(collision.get("heterogeneity", 0)) >= 2
    ):
        out.append(
            _detect(
                listing_id, "pin_collapse_with_heterogeneity", "coordinate", "minor",
                stored=collision.get("classification"), claimed=collision.get("n_exact"),
                auto_action="downgraded_precision",
            )
        )

    # ---- postal_city_vs_obec: INFORMATIONAL, never an error.
    postal = _served(resolution, "postal_town")
    if postal and served_obec:
        evaluated.add("postal_city_vs_obec")
        if normalize_match_key(postal) != normalize_match_key(served_obec):
            out.append(
                _detect(
                    listing_id, "postal_city_vs_obec", "postal_town", "info",
                    stored=served_obec, claimed=postal,
                )
            )

    ordered_out = sorted(out, key=lambda d: (SEVERITIES.index(d.severity), d.rule, d.dedupe_key))
    return ordered_out, frozenset(evaluated | {d.rule for d in ordered_out})


def _signal_rules_evaluated(resolution: Resolution) -> set[str]:
    """Which of the resolver-side signal rules S2/S4/S6 actually got to test this run.

    `country_dispute` always: S2 runs on every resolution. The other two are conditional on
    inputs the resolution itself records — a registry point AND a pin for the cross-check,
    a mapped declared label for the precision comparison."""
    rules = {"country_dispute"}
    positions = {c.position_source for c in resolution.candidates}
    kinds = {c.target_kind for c in resolution.candidates}
    if "registry_point" in positions and "coordinate_only" in kinds:
        rules.add("pin_registry_distance")
    if any(cap.startswith("declared:") for cap in resolution.precision.declared_caps):
        rules.add("declared_precision_vs_assigned")
    return rules


def member_location_spread(
    property_row: dict[str, Any], *, member_listing_ids: Sequence[int]
) -> list[Detection]:
    """The one property-grain rule of §3.11.1: members disagree beyond their combined
    uncertainty. Read off the projection's own disagreement columns so there is exactly one
    computation of "the members disagree"."""
    flags = set(property_row.get("disagreement_flags") or ())
    if "member_spread_exceeds_uncertainty" not in flags:
        return []
    anchor = int(property_row["winner_listing_id"])
    return [
        _detect(
            anchor, "member_location_spread", "coordinate", "minor",
            stored=property_row.get("member_spread_m"),
            claimed=sorted(member_listing_ids),
        )
    ]


def auto_close(
    open_keys: Sequence[str], current: Sequence[Detection], *, inputs_changed: bool
) -> list[AutoClose]:
    """Append a `resolved_upstream` disposition for every finding whose predicate stopped
    firing — but ONLY when the inputs actually changed (new snapshot, new registry version,
    new collision epoch)."""
    if not inputs_changed:
        return []
    still_firing = {d.dedupe_key for d in current}
    return [
        AutoClose(dedupe_key=key, reason="predicate_no_longer_fires_after_input_change")
        for key in sorted(set(open_keys) - still_firing)
    ]


def _served(resolution: Resolution, field_name: str) -> str | None:
    winner = resolution.fields.get(field_name)
    if winner is None or winner.value is None:
        return None
    return str(winner.value)


def _derived_street(resolution: Resolution) -> str | None:
    winner = resolution.fields.get("street_name")
    if winner is None or winner.method not in ("registry_derived", "legacy_column"):
        return None
    return str(winner.value) if winner.value is not None else None
