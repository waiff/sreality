"""The SHIPPED v1 policy seeds, exercised as the resolver will actually meet them.

Every assertion here reads `location_uncertainty_policy` / `location_field_policy` out of
the MIGRATIONS, not out of `mini_mirror`'s hand-written fixture. That distinction is the
whole point of the file: the fixture has no per-source rows at all, so the shipped seed's
six `declared_shape` rows — sreality's three and maxima's three, every one of them with
`r95_m NULL` — were exercised by nothing, and with them in place every sreality
portal-pin listing that resolves at `obec`, `cast_obce_or_quarter` or `street` raised
`UncertaintyPolicyError` in S4 and never got a resolution at all. sreality is the steady
hourly ingest; that is most of the corpus.

The rule this pins: **a `declared_shape` row with no declared shape DEGRADES.** The portal
publishes a shape on SOME rows (sreality's `locality.geometry` bbox is an empty stub on
`entity_type='address'`), so "no shape on this row" is the normal case, not a policy error.
The lookup falls through to the `'*'` row and then to the admin geometric bound.
"""

from __future__ import annotations

import dataclasses

from location_data import contracts
from location_data.resolver import core, uncertainty
from location_data.resolver.core import SURVIVORSHIP_FIELDS
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm

SEED = mm.v1_uncertainty_policy()

# The three (position_source, granularity) pairs sreality's own seed rows own.
SREALITY_DECLARED_SHAPE_RUNGS = (
    ("portal_pin", "street"),
    ("portal_pin", "cast_obce_or_quarter"),
    ("portal_pin", "obec"),
)


def _ctx(**kwargs):
    """A resolver context carrying the SHIPPED uncertainty seed."""
    return dataclasses.replace(mm.context(**kwargs), uncertainty_policy=SEED)


def _resolve(claims, ctx=None):
    return core.resolve(
        claims,
        ctx or _ctx(),
        resolver_version=RESOLVER_VERSION,
        registry_version_id=7,
        policy_version="v1",
        collision_epoch_id=11,
    )


def test_the_sreality_rows_really_are_the_most_specific_match():
    """The precondition. If the per-source row stopped winning the lookup the degrade
    below would pass for the wrong reason."""
    for position_source, granularity in SREALITY_DECLARED_SHAPE_RUNGS:
        row = uncertainty.lookup(
            SEED, position_source=position_source, granularity=granularity, source="sreality"
        )
        assert row is not None
        assert row.source == "sreality"
        assert row.derivation == "declared_shape"
        assert row.r95_m is None, "a declared_shape row carries no invented constant"


def test_a_declared_shape_row_with_no_shape_never_raises():
    """The regression itself. Every one of these raised `UncertaintyPolicyError` before."""
    for position_source, granularity in SREALITY_DECLARED_SHAPE_RUNGS:
        radius, semantics = uncertainty.radius_for(
            SEED, position_source=position_source, granularity=granularity, source="sreality"
        )
        assert radius > 0 and semantics in ("geometric_bound", "declared")


def test_the_degrade_lands_on_the_star_row_when_the_seed_has_one():
    """`(portal_pin, street)` has a `'*'` row: 300 m, the street-centroid bound."""
    assert uncertainty.radius_for(
        SEED, position_source="portal_pin", granularity="street", source="sreality"
    ) == (300.0, "geometric_bound")


def test_and_on_the_admin_geometric_bound_when_it_does_not():
    """`(portal_pin, obec)` has NO `'*'` row — the `'*'` obec/quarter radii in the seed sit
    under `portal_pin_blurred`. The honest bound for "we only know the obec" is that obec's
    own containment radius; with no polygon measurement in hand it is the CZ-scale
    sentinel, never an invented constant. S6 supplies the measurement, which is why the
    SERVED radius is the obec bound."""
    for granularity in ("obec", "cast_obce_or_quarter"):
        assert uncertainty.radius_for(
            SEED, position_source="portal_pin", granularity=granularity, source="sreality"
        ) == (uncertainty.UNRESOLVED_FALLBACK_M, "geometric_bound")
    assert uncertainty.radius_for(
        SEED, position_source="portal_pin", granularity="obec", source="sreality",
        containment_radius_m=12_000.0,
    ) == (12_000.0, "geometric_bound")


def test_maximas_blurred_declared_shape_rows_degrade_to_their_star_rows():
    """maxima's three sit under `portal_pin_blurred`, where the seed DOES carry the
    `'*'` blur band, so every one of them has a `'*'` row to fall through to."""
    for granularity, expected in (
        ("street", 500.0), ("cast_obce_or_quarter", 750.0), ("obec", 1000.0)
    ):
        radius, semantics = uncertainty.radius_for(
            SEED, position_source="portal_pin_blurred", granularity=granularity, source="maxima"
        )
        assert (radius, semantics) == (expected, "geometric_bound")


def test_a_row_that_DOES_publish_a_shape_still_wins_with_declared_semantics():
    """Degrading must not cost the portals that actually ship a radius what they ship."""
    radius, semantics = uncertainty.radius_for(
        SEED, position_source="portal_pin_blurred", granularity="obec", source="maxima",
        declared_radius_m=1360.0,
    )
    assert (radius, semantics) == (1360.0, "declared")


def test_a_sreality_portal_pin_listing_resolves_under_the_shipped_seed():
    """End to end: obec text + an undeclared pin. Before the degrade this raised in S4 and
    the listing had no resolution, no projection row and no map pin."""
    resolution = _resolve(
        [
            mm.claim(1, "obec_name", value_text="Praha"),
            mm.claim(2, "coordinate", lat=50.0755, lon=14.4378),
        ]
    )
    assert resolution.status == "resolved"
    assert resolution.position.position_source == "portal_pin"
    assert resolution.position.uncertainty_radius_m > 0
    assert resolution.precision.radius_semantics in ("geometric_bound", "declared")


def test_every_survivorship_field_has_a_v1_policy_row():
    """S7 arbitrates thirteen fields; the 383 seed covered ten. A field with no policy row
    is not "unranked" — `_best_policy` returns None, so the claim is skipped and the column
    is structurally always NULL. Migration 388 adds the five."""
    seeded = mm.v1_field_policy_fields()
    missing = sorted(set(SURVIVORSHIP_FIELDS) - seeded)
    assert not missing, (
        "location_field_policy has no v1 row for survivorship field(s), so no claim for "
        f"them can ever win: {missing}"
    )


def test_every_producer_a_shipped_contract_can_emit_has_a_v1_policy_row():
    """The same failure as the test above, one axis over — and the one the seven-portal
    W2-6…W2-12 activation actually walked into.

    A field with no policy row is structurally NULL. A field/METHOD pair with no row is
    exactly as dead and much harder to see: the field has rows, other producers win it,
    and one producer's claims are simply never counted. Before migration 470, SEVEN of the
    ten `location_extraction_method` labels had no v1 row at all, which cost nothing while
    no contract emitted them and would have cost the whole activation wave the moment
    seven contracts did — correct claims, correct evidence spans, declined at S7 forever.

    So the gate is stated where it can fire before a merge rather than after a corpus:
    every EXECUTABLE contract entry that emits a survivorship field must have a policy row
    for its (method, field) pair. Activating an eighth portal on a method nobody seeded
    reds here."""
    seeded = mm.v1_field_policy_pairs()
    # Sanity: the parse found the shipped seed, so an empty answer cannot read as "all
    # covered" if a glob or a statement shape ever moves.
    assert ("registry_derived", "street_name") in seeded
    assert ("llm_text", "psc") in seeded

    required = {
        (entry.extraction_method, entry.claim_type): f"{contract.source}/{entry.entry_id}"
        for contract in contracts.load_all()
        for entry in contract.entries
        # `reader is None` is the contract's own "declared ahead, executes nowhere" state;
        # such an entry emits nothing, so it needs no rung until it is activated.
        if entry.reader and entry.claim_type in SURVIVORSHIP_FIELDS
    }
    missing = sorted(f"{m}/{f} ({required[(m, f)]})"
                     for (m, f) in required if (m, f) not in seeded)
    assert not missing, (
        "a shipped contract entry emits a survivorship field through a producer with no "
        f"location_field_policy v1 row, so its claims can never win: {missing}")


def test_a_field_that_produces_no_winner_is_surfaced_as_blocked():
    """S7's own `blocked` signal used to be computed and thrown away, which is how five
    fields could be silently unwinnable. It now travels on the resolution."""
    resolution = _resolve(
        [
            mm.claim(1, "obec_name", value_text="Praha"),
            mm.claim(
                2, "postal_town", value_text="Praha 6",
                extraction_method="llm_text", surface="description",
            ),
        ]
    )
    # A lone llm_text claim never fills even a NULL (D7's graded guard) ...
    assert "postal_town" not in resolution.fields
    # ... and that refusal is now visible instead of silent.
    assert "postal_town" in resolution.survivorship_blocked
    assert "claim_lacks_independent_agreement" in {
        s.rule for s in resolution.contradiction_signals
    }
