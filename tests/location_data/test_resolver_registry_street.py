"""The registry's canonical street form on registry-bound rows (operator decision 2026-09-10).

Policy v1's ('ruian','registry_derived',100) rung always outranked the portal's 300; S7's
registry fill being preserve-if-null is the only reason it never produced a `street_name`
winner. Now it does — and the only value the registry does not get to respell is an
operator correction.
"""

from __future__ import annotations

import dataclasses

from location_data.resolver import core
from location_data.resolver.types import FieldPolicyRow
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm

# `('operator','operator_manual',50)` — migration 400's rung, absent from the hand-written
# mini fixture. Added per-test rather than to the shared fixture: only these tests need it.
_OPERATOR_RUNG = FieldPolicyRow(
    policy_version="v1", field="street_name", source_pattern="operator",
    method_pattern="operator_manual", rank=50, min_confidence=None,
    may_fill_null=True, may_overwrite_non_null=True, requires_independent_agreement=False,
)


def _resolve(claims, *, ctx=None):
    return core.resolve(
        claims,
        ctx or mm.context(),
        resolver_version=RESOLVER_VERSION,
        registry_version_id=7,
        policy_version="v1",
        collision_epoch_id=11,
    )


def _street(resolution):
    return resolution.fields["street_name"]


def _overrides(resolution):
    return [
        s for s in resolution.contradiction_signals if s.rule == "street_form_registry_override"
    ]


def test_case_and_diacritics_are_respelled_from_the_registry_without_a_signal():
    """The bulk class (`Na Strži` vs `Na strži`): same match key, same street — respell
    SILENTLY. That is the whole point of the decision."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Borislavkou 487/40"),
    ])
    assert _street(resolution).value == "Nad Bořislavkou"
    assert _street(resolution).rule == "registry:street"
    assert _street(resolution).method == "registry_derived"
    assert _street(resolution).source_claim_ids == ()
    assert _overrides(resolution) == []


def test_a_dropped_prefix_is_restored_silently():
    """`Budovatelů` vs RÚIAN `nám. Budovatelů`. S1 parses the type word OFF the claim, so
    the winner reads `Budovatelů` whether the portal wrote the prefix or not — the two are
    indistinguishable downstream and both mean the same street. Comparing the winner against
    the STRIPPED official name is what keeps every nám./ulice/třída street off the ledger."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Bílovec"),
        mm.claim(2, "street_name", value_text="Budovatelů"),
        mm.claim(3, "address_point_id", value_text="33000002"),
    ])
    assert _street(resolution).value == "nám. Budovatelů"
    assert _street(resolution).rule == "registry:street"
    assert _overrides(resolution) == []


def test_a_claim_that_carries_the_prefix_agrees_and_signals_nothing():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Bílovec"),
        mm.claim(2, "street_name", value_text="nám. Budovatelů 5"),
        mm.claim(3, "address_point_id", value_text="33000002"),
    ])
    assert _street(resolution).value == "nám. Budovatelů"
    assert _overrides(resolution) == []


def test_a_different_street_is_overridden_and_the_ledger_keeps_what_the_portal_said():
    """A real disagreement: the portal names a DIFFERENT Bílovec street than the address
    point the row is bound to. The registry still wins, and the info row keeps the claim."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Bílovec"),
        mm.claim(2, "street_name", value_text="Slunečná"),
        mm.claim(3, "address_point_id", value_text="33000002"),
    ])
    assert _street(resolution).value == "nám. Budovatelů"
    assert _street(resolution).rule == "registry:street"
    signal = _overrides(resolution)[0]
    assert signal.field == "street_name"
    assert signal.severity == "info"
    assert signal.claimed == ["Slunečná"]
    assert signal.stored == "nám. Budovatelů"
    assert signal.evidence_claim_ids == (2,)


def test_an_operator_correction_is_never_respelled_by_the_registry():
    ctx = dataclasses.replace(
        mm.context(), field_policy=mm.FIELD_POLICY + (_OPERATOR_RUNG,)
    )
    resolution = _resolve(
        [
            mm.claim(1, "obec_name", value_text="Bílovec"),
            mm.claim(2, "street_name", value_text="Budovatelů"),
            mm.claim(3, "address_point_id", value_text="33000002"),
            mm.claim(4, "street_name", value_text="Budovatelská", source="operator",
                     extraction_method="operator_manual", surface="operator"),
        ],
        ctx=ctx,
    )
    assert _street(resolution).value == "Budovatelská"
    assert _street(resolution).method == "operator_manual"
    assert _street(resolution).source_claim_ids == (4,)
    assert _overrides(resolution) == []


def test_the_registry_still_fills_a_street_nobody_claimed():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Bílovec"),
        mm.claim(2, "address_point_id", value_text="33000002"),
    ])
    assert _street(resolution).value == "nám. Budovatelů"
    assert _overrides(resolution) == []
