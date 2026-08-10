"""S7 — per-field survivorship (03 §3.9). Policy is DATA; this is the one evaluator.

`location_field_policy` (01 §6.2) is read by exactly one deterministic function,
`evaluate_field`, and the winning row's `policy_version` is cached on the resolution. The
two columns an earlier draft dropped — `may_overwrite_non_null` and
`requires_independent_agreement` — ARE D7's graded write-back guard: an `llm_text` claim
never overwrites a non-NULL value and never fills even a NULL alone; it opens a
contradiction instead.

`max_age_days` is evaluated against `as_of = max(observed_at)` over the consumed claims,
never against `now()` (§3.0 rule 2) — that is what keeps the resolver a pure function of
its inputs.

The §3.9.1 invariants are enforced HERE, in the evaluator, not in the data:

1. derived never overwrites claimed;
2. weaker never overwrites stronger (granularity, not recency), absent a demotion reason;
3. a wrong value is worse than NULL — text-derived values are gazetteer-validated before
   they can win;
4. low precision DEFERS, it does not exclude — a low-precision claim stays rankable here
   and is excluded only from co-location evidence and geometric blocking;
5. no portal-proprietary identifier is ever a field: `locality_district_id` and friends
   live as namespaced claims and never become a query dimension;
6. licence-encumbered positions (`ephemeral_display_only`) are inadmissible.

(03 §3.9.1 numbers six invariants. The W1 brief names "the five" — 4 is a negative
invariant, enforced by the ABSENCE of a precision filter in the ranking, and is asserted as
such in `tests/location_data/test_resolver_survivorship.py`.)
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from location_data.resolver.types import (
    Claim,
    ContradictionSignal,
    FieldPolicyRow,
    FieldWinner,
    GranularityRank,
    NormalizedClaim,
)

EPHEMERAL = "ephemeral_display_only"
TEXT_METHODS = frozenset({"llm_text", "regex_text"})
DERIVED_METHODS = frozenset({"registry_derived", "legacy_column"})
CONFIDENCE_ORDER = ("low", "medium", "high", "exact")

# Invariant 5: portal-proprietary identifiers are claims, never survivorship fields.
PORTAL_PROPRIETARY_FIELDS = frozenset({"portal_admin_id", "portal_street_id", "osm_relation_id"})


@dataclass(frozen=True, slots=True)
class FieldContext:
    """What the evaluator needs about the world beyond the claims themselves."""

    as_of: datetime | None
    incumbent: dict[str, object]
    rank: GranularityRank
    claim_granularity: dict[int, str]
    validate: Callable[[str, str], bool] | None = None  # (field, value_norm) -> exists
    derived_values: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class SurvivorshipResult:
    fields: dict[str, FieldWinner]
    signals: tuple[ContradictionSignal, ...]
    blocked: tuple[str, ...]


def matches(policy: FieldPolicyRow, claim: Claim) -> bool:
    """Does this policy row govern this claim? `source_pattern` names a CLASS of producer
    (`ruian`, `portal:*`, `portal:<name>`, `llm_text`, `operator`), `method_pattern` an
    extraction method (`*` allowed)."""
    if policy.method_pattern not in ("*", claim.extraction_method):
        return False
    pattern = policy.source_pattern
    if pattern == "*":
        return True
    if pattern == "ruian":
        return claim.extraction_method == "registry_derived" or claim.surface == "registry"
    if pattern == "llm_text":
        return claim.extraction_method == "llm_text"
    if pattern == "operator":
        return claim.extraction_method == "operator_manual"
    if pattern.startswith("portal:"):
        wanted = pattern.split(":", 1)[1]
        return wanted == "*" or wanted == claim.source
    return pattern == claim.source


def admissible(claim: Claim, norm: NormalizedClaim | None) -> str | None:
    """-> rejection reason, or None. §3.2 rule 4 + §3.9.1 invariants 5 and 6."""
    if claim.subject_scoped is False:
        return "not_subject_scoped"
    if claim.licence_class == EPHEMERAL:
        return "licence_ephemeral"
    if claim.claim_type in PORTAL_PROPRIETARY_FIELDS:
        return "portal_proprietary_identifier"
    if norm is not None and norm.rejected:
        return f"normalization:{norm.rejections[0]}"
    return None


def evaluate_field(
    field: str,
    claims: Sequence[Claim],
    normalized: dict[int, NormalizedClaim],
    policy: Sequence[FieldPolicyRow],
    ctx: FieldContext,
) -> tuple[FieldWinner | None, tuple[ContradictionSignal, ...]]:
    """THE deterministic survivorship function. One field, one winner (or none)."""
    signals: list[ContradictionSignal] = []
    scored: list[tuple[tuple, Claim, FieldPolicyRow, NormalizedClaim | None]] = []

    for claim in sorted(claims, key=lambda c: c.id):
        if claim.claim_type != field:
            continue
        norm = normalized.get(claim.id)
        if admissible(claim, norm) is not None:
            continue
        row = _best_policy(policy, claim, field)
        if row is None:
            continue
        if row.min_confidence and _below(claim.claim_confidence, row.min_confidence):
            continue
        if row.max_age_days is not None and ctx.as_of is not None:
            age_days = (ctx.as_of - claim.observed_at).days
            if age_days > row.max_age_days:
                continue
        granularity = ctx.claim_granularity.get(claim.id)
        if row.min_granularity and granularity is not None:
            if not ctx.rank.at_least(granularity, row.min_granularity):
                continue
        # Invariant 3: a text-derived value must gazetteer-validate before it can win.
        value_norm = (norm.value_ascii if norm else None) or (claim.value_text or "")
        if claim.extraction_method in TEXT_METHODS and ctx.validate is not None:
            if not ctx.validate(field, value_norm):
                signals.append(
                    ContradictionSignal(
                        rule="text_claim_failed_gazetteer",
                        field=field,
                        severity="minor",
                        claimed=claim.value_text,
                        evidence_claim_ids=(claim.id,),
                        auto_action="blocked_write",
                    )
                )
                continue
        # Primary order is the POLICY RANK (registry > portal > mined text); the
        # `tie_breaker` column then orders within one rank.
        granularity_rank = ctx.rank.rank(granularity) if granularity else -1
        key = (row.rank, -granularity_rank, -claim.observed_at.timestamp(), claim.id)
        scored.append((key, claim, row, norm))

    if not scored:
        return None, tuple(signals)

    scored.sort(key=lambda item: item[0])
    _, winner, row, norm = scored[0]
    value = _value_of(winner, norm)
    incumbent = ctx.incumbent.get(field)

    # D7's graded write-back guard.
    if row.requires_independent_agreement and not _independently_agreed(scored, value):
        signals.append(
            ContradictionSignal(
                rule="claim_lacks_independent_agreement",
                field=field,
                severity="minor",
                claimed=value,
                stored=incumbent,
                evidence_claim_ids=(winner.id,),
                auto_action="blocked_write",
            )
        )
        return None, tuple(signals)

    if incumbent is not None and _differs(incumbent, value):
        if not row.may_overwrite_non_null:
            signals.append(
                ContradictionSignal(
                    rule="write_back_blocked_non_null",
                    field=field,
                    severity="minor",
                    stored=incumbent,
                    claimed=value,
                    evidence_claim_ids=(winner.id,),
                    auto_action="blocked_write",
                )
            )
            return None, tuple(signals)
        # Invariant 2: weaker never overwrites stronger.
        incumbent_granularity = ctx.incumbent.get(f"{field}__granularity")
        new_granularity = ctx.claim_granularity.get(winner.id)
        if (
            isinstance(incumbent_granularity, str)
            and isinstance(new_granularity, str)
            and ctx.rank.rank(new_granularity) < ctx.rank.rank(incumbent_granularity)
        ):
            signals.append(
                ContradictionSignal(
                    rule="weaker_would_overwrite_stronger",
                    field=field,
                    severity="minor",
                    stored=incumbent,
                    claimed=value,
                    evidence_claim_ids=(winner.id,),
                    auto_action="blocked_write",
                )
            )
            return None, tuple(signals)
    elif incumbent is None and not row.may_fill_null:
        return None, tuple(signals)

    # Invariant 1: derived never overwrites claimed.
    derived = (ctx.derived_values or {}).get(field)
    if derived is not None and winner.extraction_method in DERIVED_METHODS:
        claimed_exists = any(
            c.claim_type == field and c.extraction_method not in DERIVED_METHODS for c in claims
        )
        if claimed_exists:
            return None, tuple(signals)

    return (
        FieldWinner(
            field=field,
            value=value,
            source_claim_ids=(winner.id,),
            rule=f"policy:{row.policy_version}:rank{row.rank}:{row.source_pattern}",
            method=winner.extraction_method,
            granularity=ctx.claim_granularity.get(winner.id),
            confidence=winner.claim_confidence,
        ),
        tuple(signals),
    )


def evaluate(
    fields: Sequence[str],
    claims: Sequence[Claim],
    normalized: dict[int, NormalizedClaim],
    policy: Sequence[FieldPolicyRow],
    ctx: FieldContext,
) -> SurvivorshipResult:
    winners: dict[str, FieldWinner] = {}
    signals: list[ContradictionSignal] = []
    blocked: list[str] = []
    for field in fields:
        winner, field_signals = evaluate_field(field, claims, normalized, policy, ctx)
        signals.extend(field_signals)
        if winner is not None:
            winners[field] = winner
        elif any(c.claim_type == field for c in claims):
            blocked.append(field)
    return SurvivorshipResult(winners, tuple(signals), tuple(sorted(blocked)))


def _best_policy(
    policy: Sequence[FieldPolicyRow], claim: Claim, field: str
) -> FieldPolicyRow | None:
    rows = [r for r in policy if r.field == field and matches(r, claim)]
    if not rows:
        return None
    return min(rows, key=lambda r: (r.rank, r.source_pattern, r.method_pattern))


def _value_of(claim: Claim, norm: NormalizedClaim | None) -> object:
    if claim.value_text is not None:
        if norm is not None and norm.typed_slots:
            for key in ("street", "psc"):
                if key in norm.typed_slots:
                    return norm.typed_slots[key]
        return claim.value_text
    if claim.value_num is not None:
        return claim.value_num
    if claim.has_position:
        return {"lat": claim.lat, "lon": claim.lon}
    return claim.value_jsonb


def _independently_agreed(scored, value: object) -> bool:
    """Two claims agree independently when they come from different producers — a different
    source or a different extraction method. Two runs of the same extractor are one voice."""
    voices = {
        (claim.source, claim.extraction_method)
        for _, claim, _, norm in scored
        if not _differs(_value_of(claim, norm), value)
    }
    return len(voices) >= 2


def _differs(a: object, b: object) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().casefold() != b.strip().casefold()
    return a != b


def _below(value: str | None, floor: str) -> bool:
    current = value if value in CONFIDENCE_ORDER else "low"
    return CONFIDENCE_ORDER.index(current) < CONFIDENCE_ORDER.index(floor)
