"""The R95 radius lookup (03 §3.8.3 / 01 §6.2) — one resolution of
`location_uncertainty_policy`, used by S3 candidates, S4 positions and S6 alike.

Two rules are enforced here rather than trusted:

* **`radius_semantics` is never `'r95_empirical'` in v1.** The seed radii are engineering
  judgement, not measurement (01 OQ4), and calling an uncalibrated number a 95 %
  containment radius would be a false probability statement. The calibration pass writes a
  NEW `policy_version`, which re-resolves through the campaign runner.
* **Semantics never mix in arithmetic** (01 §3.3.1): combining radii takes the MAX, never
  the mean, and the coarser semantics wins.

`admin_containment_radius` reads the polygon's `containment_radius_m` — the max
centre-to-boundary distance paired with `representative_point` — and NEVER
`inscribed_radius_m`, which for an elongated obec is far smaller than the true bound and
would let a town-centroid row pass a certain-containment test.

**A `declared_shape` row with no declared shape DEGRADES, it never raises.** The v1 seed
gives sreality its own `declared_shape` rows at `obec` / `cast_obce_or_quarter` / `street`
because sreality *sometimes* ships `locality.geometry` — but that bbox is an empty stub on
`entity_type='address'`, so on most rows there is no shape to read. Treating "the
per-source row cannot produce a number" as a hard error would make every sreality
portal-pin listing at those three rungs unresolvable; the honest answer is to fall through
to the `'*'` row and then to the admin geometric bound, which is what the lookup order
already expresses.
"""

from __future__ import annotations

from collections.abc import Sequence

from location_data.resolver.types import UncertaintyPolicyRow

FORBIDDEN_SEMANTICS_V1 = "r95_empirical"

# The coarsest defensible bound for a row with no usable position (01 §6.1's sentinel).
UNRESOLVED_FALLBACK_M = 250_000.0


class UncertaintyPolicyError(RuntimeError):
    """The policy set cannot produce a radius. Never silently defaulted."""


def candidate_rows(
    policy: Sequence[UncertaintyPolicyRow],
    *,
    position_source: str,
    granularity: str,
    source: str,
) -> tuple[UncertaintyPolicyRow, ...]:
    """The rows that may answer for this pair, most specific first: the per-source row, then
    the `'*'` row — the PK is (policy_version, position_source, granularity, source)."""
    exact = [
        r
        for r in policy
        if r.position_source == position_source and r.granularity == granularity
    ]
    return tuple(
        [r for r in exact if r.source == source] + [r for r in exact if r.source == "*"]
    )


def lookup(
    policy: Sequence[UncertaintyPolicyRow],
    *,
    position_source: str,
    granularity: str,
    source: str,
) -> UncertaintyPolicyRow | None:
    rows = candidate_rows(
        policy, position_source=position_source, granularity=granularity, source=source
    )
    return rows[0] if rows else None


def radius_for(
    policy: Sequence[UncertaintyPolicyRow],
    *,
    position_source: str,
    granularity: str,
    source: str,
    declared_radius_m: float | None = None,
    containment_radius_m: float | None = None,
    input_radii_m: Sequence[float] = (),
) -> tuple[float, str]:
    """-> (uncertainty_radius_m, radius_semantics). Both NOT NULL, always together."""
    rows = list(
        candidate_rows(
            policy, position_source=position_source, granularity=granularity, source=source
        )
    )
    if position_source not in ("none", "admin_centroid"):
        # A coordinate whose granularity was capped to an ADMIN rung (the collision cap of
        # 03 §3.8.4 does exactly this) has no (position_source, granularity) seed row —
        # 01 §6.2 seeds the pin ladder only down to `street`. The honest bound for "we now
        # only know the obec" is that unit's own area bound, not an invented constant. It
        # is also where a shapeless `declared_shape` row lands after it degrades.
        rows.extend(
            candidate_rows(
                policy,
                position_source="admin_centroid",
                granularity=granularity,
                source=source,
            )
        )

    for row in rows:
        if row.radius_semantics == FORBIDDEN_SEMANTICS_V1:
            raise UncertaintyPolicyError(
                "radius_semantics='r95_empirical' is not admissible in v1 — the seed radii "
                "are uncalibrated (01 OQ4); calibrate under a new policy_version first"
            )
        resolved = _radius_from_row(
            row,
            position_source=position_source,
            granularity=granularity,
            declared_radius_m=declared_radius_m,
            containment_radius_m=containment_radius_m,
            input_radii_m=input_radii_m,
        )
        if resolved is not None:
            return resolved

    # No row could answer: fall back to the coarsest defensible bound rather than inventing
    # a number, and keep the semantics honest.
    if position_source == "none" or granularity == "unknown":
        return UNRESOLVED_FALLBACK_M, "geometric_bound"
    raise UncertaintyPolicyError(
        f"location_uncertainty_policy has no usable row for "
        f"({position_source!r}, {granularity!r}, {source!r} / '*')"
    )


def _radius_from_row(
    row: UncertaintyPolicyRow,
    *,
    position_source: str,
    granularity: str,
    declared_radius_m: float | None,
    containment_radius_m: float | None,
    input_radii_m: Sequence[float],
) -> tuple[float, str] | None:
    """-> (radius, semantics), or None when this row cannot answer and the next one must."""
    semantics = row.radius_semantics

    if row.derivation == "declared_shape":
        if declared_radius_m is not None:
            return float(declared_radius_m), "declared"
        if row.r95_m is None:
            # DEGRADE, never raise: the portal declares a shape on SOME rows only, and a
            # row without one still has to resolve.
            return None
        return float(row.r95_m), semantics

    if row.derivation == "admin_containment_radius":
        if containment_radius_m is None:
            # An admin position with no polygon measurement is not a 'guess a constant'
            # case: the honest bound is the unresolved sentinel.
            return UNRESOLVED_FALLBACK_M, "geometric_bound"
        return float(containment_radius_m), semantics

    if row.derivation == "max_of_inputs":
        candidates = [float(r) for r in input_radii_m if r is not None]
        if declared_radius_m is not None:
            candidates.append(float(declared_radius_m))
        if row.r95_m is not None:
            candidates.append(float(row.r95_m))
        if not candidates:
            return UNRESOLVED_FALLBACK_M, "geometric_bound"
        return max(candidates), semantics

    if row.r95_m is None:
        raise UncertaintyPolicyError(
            f"constant row ({position_source}, {granularity}) carries no r95_m"
        )
    return float(row.r95_m), semantics


def combine(a: tuple[float, str], b: tuple[float, str]) -> tuple[float, str]:
    """Max, never mean; the coarser semantics label survives (01 §3.3.1)."""
    order = {"declared": 0, "geometric_bound": 1}
    radius = max(a[0], b[0])
    semantics = a[1] if order.get(a[1], 9) >= order.get(b[1], 9) else b[1]
    return radius, semantics
