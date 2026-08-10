"""S8 — the two serving projections (03 §3.10, 01 §7.1/§7.2, 00 §7).

These are CACHES, never truth: truncating either is always legal and the builder is the
only writer. Every derived value comes from `location_data.resolver.derived`, which is the
builder-side twin of migration 384's SQL functions.

Two things the builder owns that nothing else may write:

* `location_disputed` is a CACHE of `EXISTS(open contradiction with severity='major')`.
  The reconciler never writes the projection; a contradiction opening or closing enqueues a
  rebuild.
* `geo_cell_key` is written ONLY when `geo_blockable`, NULL otherwise — that is what keeps
  the granularity rung out of an IMMUTABLE expression, and cell equality must be expanded
  to the 3×3 neighbourhood at query time.

**Property grain is a RECONCILIATION over children, never a single-child lottery**
(00 §7.5). Today every property location column is
`CASE WHEN cnt = 1 THEN l.<col> ELSE p.<col> END`, so a group holding one precise pin and
one town centroid can publish the centroid and the disagreement is discarded. Here the
winner is chosen by precision with a stated `winner_rule`, and the disagreement is a
first-class output: `member_spread_m`, `members_with_geom`, `distinct_street_names`,
`distinct_obec_kods`, `disagreement_flags`.

**Grain honesty** (01 §7.2.1, normative): "how good is our location data?" is a LISTING-grain
question. "Highest-precision member wins" is a maximum order statistic, so property-grain
precision distributions are upward-biased and improve whenever GROUPING improves, with no
change in data quality.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from location_data.resolver import derived
from location_data.resolver.geo import distance_between
from location_data.resolver.types import ClusterEvidence, GranularityRank, Resolution

DISPLAY_FALLBACK = "Neznámá lokalita"


def build_listing_row(
    resolution: Resolution,
    *,
    property_id: int | None,
    resolution_id: int | None,
    registry_version_label: str,
    rank: GranularityRank,
    cluster: ClusterEvidence | None,
    threshold_n: int,
    location_disputed: bool,
    history_completeness: str | None = None,
) -> dict[str, Any]:
    precision = resolution.precision
    position = resolution.position
    admin = resolution.admin
    fields = resolution.fields

    pin_collision_class = cluster.classification if cluster else "normal"
    heterogeneity_ok = cluster.heterogeneity_ok if cluster else True
    n_exact = cluster.listing_count if cluster else 1
    collision_ok = derived.pin_collision_ok(
        pin_collision_class=pin_collision_class,
        cluster_heterogeneity_ok=heterogeneity_ok,
        pin_shared_by_n=n_exact,
        threshold_n=threshold_n,
    )
    blockable = derived.geo_blockable(
        granularity=precision.granularity,
        position_source=precision.position_source,
        collision_ok=collision_ok,
        rank=rank,
    )
    renderable = derived.renderable_as_point(
        granularity=precision.granularity,
        position_source=precision.position_source,
        collision_ok=collision_ok,
        location_disputed=location_disputed,
        rank=rank,
    )
    street = _text(fields, "street_name")
    cp = _text(fields, "house_number_cp")
    co = _text(fields, "house_number_co")

    return {
        "listing_id": resolution.listing_id,
        "property_id": property_id,
        "source": resolution.source,
        "resolution_id": resolution_id,
        "registry_version_id": resolution.registry_version_id,
        "registry_version": registry_version_label,
        "resolver_version": resolution.resolver_version,
        "policy_version": resolution.policy_version,
        # D1
        "country_code": resolution.country.country_code,
        "country_status": resolution.country.status,
        "country_method": resolution.country.method,
        "country_confidence": resolution.country.confidence,
        "country_driving_claim_ids": list(resolution.country.driving_claim_ids),
        "is_cz": resolution.country.status == "cz",
        # D3 — the four axes travel with the coordinate, all NOT NULL
        "lat": position.lat,
        "lon": position.lon,
        "granularity": precision.granularity,
        "position_source": precision.position_source,
        "blur_evidence": precision.blur_evidence,
        "match_confidence": precision.match_confidence,
        "match_components": precision.match_components,
        "uncertainty_radius_m": precision.uncertainty_radius_m,
        "radius_semantics": precision.radius_semantics,
        "position_licence_class": resolution.position_licence_class,
        # D4 registry identity — both currencies
        "ruian_adm_kod": _winner_key(resolution, "ruian_adm_kod"),
        "stavebni_objekt_kod": _winner_key(resolution, "stavebni_objekt_kod"),
        "parcela_id": _winner_key(resolution, "parcela_id"),
        "ulice_kod": _winner_key(resolution, "ulice_kod"),
        "obec_kod": admin.obec_kod,
        "cast_obce_kod": admin.cast_obce_kod,
        "momc_kod": admin.momc_kod,
        "ku_kod": admin.ku_kod,
        "pou_kod": admin.pou_kod,
        "orp_kod": admin.orp_kod,
        "okres_kod": admin.okres_kod,
        "kraj_kod": admin.kraj_kod,
        "obec_unit_id": admin.obec_unit_id,
        "cast_obce_unit_id": admin.cast_obce_unit_id,
        "okres_unit_id": admin.okres_unit_id,
        "kraj_unit_id": admin.kraj_unit_id,
        "admin_path": admin.admin_path,
        "admin_assignment_method": admin.method,
        "admin_position_source": admin.position_source,
        "admin_sliver_distance_m": admin.sliver_distance_m,
        # display / postal
        "display_label": display_label(street, cp, co, admin),
        "display_path": admin.display_path,
        "street_name": street,
        "house_number_cp": cp,
        "house_number_co": co,
        "evidencni": _text(fields, "evidencni"),
        "psc": _text(fields, "psc"),
        "postal_town": _text(fields, "postal_town"),
        "cast_obce_name": admin.cast_obce_name or _text(fields, "cast_obce_name"),
        "obec_name": admin.obec_name or _text(fields, "obec_name"),
        "okres_name": admin.okres_name,
        "kraj_name": admin.kraj_name,
        "development_name": _text(fields, "development_name"),
        "place_search_text": place_search_text(street, admin, _text(fields, "postal_town")),
        # honesty signals (D10)
        "pin_shared_by_n": n_exact,
        "pin_shared_by_n_25m": cluster.n_25m if cluster else 1,
        "pin_shared_by_n_100m": cluster.n_100m if cluster else 1,
        "pin_cluster_id": cluster.cluster_id if cluster else None,
        "pin_collision_class": pin_collision_class,
        "cluster_heterogeneity_ok": heterogeneity_ok,
        "render_as": derived.render_as(
            renderable=renderable,
            granularity=precision.granularity,
            position_source=precision.position_source,
            has_geom=position.lat is not None,
            rank=rank,
        ),
        "renderable_as_point": renderable,
        "is_low_precision": derived.is_low_precision(granularity=precision.granularity, rank=rank),
        "geo_blockable": blockable,
        "location_disputed": location_disputed,
        "distance_to_nearest_boundary_m": admin.distance_to_nearest_boundary_m,
        "history_completeness": history_completeness,
        # provenance
        "field_provenance": field_provenance(resolution),
        "geom_claim_id": (position.source_claim_ids[0] if position.source_claim_ids else None),
        "street_claim_id": (
            fields["street_name"].source_claim_ids[0]
            if "street_name" in fields and fields["street_name"].source_claim_ids
            else None
        ),
        # blocking keys
        "addr_block_key": derived.addr_block_key(_winner_key(resolution, "ruian_adm_kod")),
        "building_block_key": derived.building_block_key(
            _winner_key(resolution, "stavebni_objekt_kod")
        ),
        "street_block_key": derived.street_block_key(admin.obec_kod, street, cp),
        # written ONLY when geo_blockable (01 §7.1.1)
        "geo_cell_key": (
            derived.geo_cell_key(position.lat, position.lon) if blockable else None
        ),
        "h3_r10": None,  # h3-pg unavailable; the rounded cell above is the shipped fallback
        "position_quality_class": precision.position_quality_class,
        "collision_epoch_id": resolution.collision_epoch_id,
    }


def display_label(street: str | None, cp: str | None, co: str | None, admin) -> str:
    """Replaces nine incompatible portal `locality` semantics. `postal_town` is NEVER
    folded in — for bazos neither value is wrong, they answer different questions."""
    parts: list[str] = []
    if street:
        number = "/".join(p for p in (cp, co) if p)
        parts.append(f"{street} {number}".strip() if number else street)
    place = admin.cast_obce_name or admin.obec_name
    if place:
        parts.append(place)
    if admin.obec_name and admin.cast_obce_name and admin.obec_name != admin.cast_obce_name:
        parts.append(admin.obec_name)
    if not parts and admin.okres_name:
        parts.append(admin.okres_name)
    if not parts and admin.kraj_name:
        parts.append(admin.kraj_name)
    return ", ".join(dict.fromkeys(parts)) or DISPLAY_FALLBACK


def place_search_text(street: str | None, admin, postal_town: str | None) -> str:
    tokens = [
        street, admin.cast_obce_name, admin.obec_name, admin.okres_name, admin.kraj_name,
        postal_town,
    ]
    return " ".join(dict.fromkeys(t for t in tokens if t))


def field_provenance(resolution: Resolution) -> dict[str, Any]:
    return {
        name: {
            "claim_ids": list(winner.source_claim_ids),
            "method": winner.method,
            "rule": winner.rule,
        }
        for name, winner in sorted(resolution.fields.items())
    }


def _text(fields, name: str) -> str | None:
    winner = fields.get(name)
    if winner is None or winner.value is None:
        return None
    return str(winner.value)


def _winner_key(resolution: Resolution, attribute: str):
    if resolution.chosen_rank is None:
        return None
    for candidate in resolution.candidates:
        if candidate.rank == resolution.chosen_rank:
            return getattr(candidate, attribute)
    return None


# ------------------------------------------------------------------ property grain


def build_property_row(
    property_id: int, children: Sequence[dict[str, Any]], *, rank: GranularityRank
) -> dict[str, Any] | None:
    """Reconciliation over children (00 §7.5). Returns None for an empty group."""
    members = [c for c in children if c.get("listing_id") is not None]
    if not members:
        return None

    ordered = sorted(members, key=lambda row: _precision_key(row, rank))
    winner = ordered[0]
    with_geom = [m for m in members if m.get("lat") is not None]
    streets = {m.get("street_name") for m in members if m.get("street_name")}
    obec_kods = {m.get("obec_kod") for m in members if m.get("obec_kod") is not None}
    spread = _max_spread_m(with_geom)

    flags: list[str] = []
    if len(streets) > 1:
        flags.append("street_disagreement")
    if len(obec_kods) > 1:
        flags.append("obec_disagreement")
    if len({m.get("granularity") for m in members}) > 1:
        flags.append("precision_mix")
    if spread is not None:
        combined = sum(
            sorted((float(m.get("uncertainty_radius_m") or 0.0) for m in with_geom), reverse=True)[:2]
        )
        if spread > combined:
            flags.append("member_spread_exceeds_uncertainty")
    if len(with_geom) < len(members):
        flags.append("members_without_geom")

    return {
        "property_id": property_id,
        "member_count": len(members),
        "winner_listing_id": winner["listing_id"],
        "winner_rule": _winner_rule(winner, members, rank),
        "winner_source": winner.get("source"),
        "lat": winner.get("lat"),
        "lon": winner.get("lon"),
        "granularity": winner["granularity"],
        "position_source": winner["position_source"],
        "blur_evidence": winner["blur_evidence"],
        "match_confidence": winner["match_confidence"],
        "uncertainty_radius_m": winner["uncertainty_radius_m"],
        "radius_semantics": winner["radius_semantics"],
        "position_licence_class": winner["position_licence_class"],
        "ruian_adm_kod": winner.get("ruian_adm_kod"),
        "stavebni_objekt_kod": winner.get("stavebni_objekt_kod"),
        "obec_kod": winner.get("obec_kod"),
        "cast_obce_kod": winner.get("cast_obce_kod"),
        "okres_kod": winner.get("okres_kod"),
        "kraj_kod": winner.get("kraj_kod"),
        "admin_path": winner.get("admin_path"),
        "admin_assignment_method": winner["admin_assignment_method"],
        "street_name": winner.get("street_name"),
        "psc": winner.get("psc"),
        "display_label": winner.get("display_label") or DISPLAY_FALLBACK,
        "place_search_text": winner.get("place_search_text"),
        "country_code": winner.get("country_code"),
        "country_status": winner["country_status"],
        "member_spread_m": spread,
        "members_with_geom": len(with_geom),
        "distinct_street_names": len(streets),
        "distinct_obec_kods": len(obec_kods),
        "disagreement_flags": flags,
        "pin_shared_by_n": max(int(m.get("pin_shared_by_n") or 1) for m in members),
        "geo_blockable": bool(winner.get("geo_blockable")),
        "render_as": winner.get("render_as") or "area",
    }


def _precision_key(row: dict[str, Any], rank: GranularityRank) -> tuple:
    """Highest precision first: a centroid child can never out-rank a precise child, and
    the tie-break is deterministic (listing_id), never insertion order."""
    quality_order = {"precise": 0, "approximate": 1, "area": 2, "none": 3}
    return (
        0 if row.get("lat") is not None else 1,
        -rank.rank(row["granularity"]),
        quality_order.get(str(row.get("position_quality_class") or "none"), 3),
        float(row.get("uncertainty_radius_m") or 0.0),
        int(row["listing_id"]),
    )


def _winner_rule(winner: dict[str, Any], members: Sequence[dict[str, Any]], rank) -> str:
    if len(members) == 1:
        return "sole_member"
    return f"highest_precision:{winner['granularity']}:{winner['position_source']}"


def _max_spread_m(rows: Sequence[dict[str, Any]]) -> float | None:
    if len(rows) < 2:
        return None
    points = [(float(r["lat"]), float(r["lon"])) for r in rows]
    spread = 0.0
    for i, a in enumerate(points):
        for b in points[i + 1 :]:
            distance = distance_between(a, b)
            if distance is not None:
                spread = max(spread, distance)
    return spread
