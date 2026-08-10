"""S7 — the survivorship invariants of 03 §3.9.1, enforced in the evaluator.

Policy is data (`location_field_policy`), but the invariants are code: a policy row can
never be written that lets a derived value overwrite a claimed one, that lets a weaker
value overwrite a stronger one, or that lets an `llm_text` claim fill a field on its own.
"""

from __future__ import annotations

from datetime import timedelta

from location_data.resolver import normalize, survivorship
from location_data.resolver.types import GranularityRank
from tests.location_data import mini_mirror as mm


def _ctx(**overrides):
    base = {
        "as_of": None,
        "incumbent": {},
        "rank": GranularityRank(),
        "claim_granularity": {},
        "validate": None,
        "derived_values": {},
    }
    base.update(overrides)
    return survivorship.FieldContext(**base)


def _evaluate(field, claims, ctx=None):
    return survivorship.evaluate_field(
        field, claims, normalize.normalize_all(claims), mm.FIELD_POLICY, ctx or _ctx()
    )


def test_a_structured_portal_field_beats_a_mined_one():
    claims = [
        mm.claim(1, "street_name", value_text="Slunečná", extraction_method="llm_text",
                 claim_confidence="high"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou",
                 extraction_method="portal_structured_field"),
    ]
    winner, _ = _evaluate("street_name", claims)
    assert winner is not None
    assert winner.value == "Nad Bořislavkou"


def test_invariant_6_an_ephemeral_licence_claim_is_inadmissible():
    """The Mapy.cz class can neither win a field nor reach S8 — a constraint, not a
    convention (00 §6.1's three artifacts)."""
    claims = [
        mm.claim(1, "street_name", value_text="Krymská",
                 licence_class="ephemeral_display_only"),
    ]
    winner, _ = _evaluate("street_name", claims)
    assert winner is None


def test_invariant_5_a_portal_proprietary_identifier_is_never_a_field():
    """`locality_district_id` is sreality-only (0 rows on the other eight sources) yet four
    consumers filter on it with bare equality."""
    claims = [mm.claim(1, "portal_admin_id", value_text="sreality.locality_district_id=5")]
    winner, _ = _evaluate("portal_admin_id", claims)
    assert winner is None
    assert survivorship.admissible(claims[0], None) == "portal_proprietary_identifier"


def test_a_subject_scoped_false_claim_is_stored_but_never_rankable():
    """remax `raw_json.address` is mis-sourced from the 'Podobné nemovitosti' carousel on 5
    of the 8 pages that render it and reached `listings.street` on 2 rows."""
    claims = [mm.claim(1, "street_name", value_text="Krymská", subject_scoped=False)]
    winner, _ = _evaluate("street_name", claims)
    assert winner is None


def test_llm_text_requires_independent_agreement_even_to_fill_a_null():
    lone = [
        mm.claim(1, "street_name", value_text="Slunečná", extraction_method="llm_text",
                 claim_confidence="high"),
    ]
    winner, signals = _evaluate("street_name", lone)
    assert winner is None
    assert [s.rule for s in signals] == ["claim_lacks_independent_agreement"]

    corroborated = lone + [
        mm.claim(2, "street_name", value_text="Slunečná", source="remax",
                 extraction_method="html_selector_parse"),
    ]
    winner, _ = _evaluate("street_name", corroborated)
    assert winner is not None and winner.value == "Slunečná"


def test_llm_text_never_overwrites_a_non_null_value_it_opens_a_contradiction():
    claims = [
        mm.claim(1, "street_name", value_text="Slunečná", extraction_method="llm_text",
                 claim_confidence="high"),
        mm.claim(2, "street_name", value_text="Slunečná", source="remax",
                 extraction_method="llm_text", claim_confidence="high"),
    ]
    ctx = _ctx(incumbent={"street_name": "Nad Bořislavkou"})
    winner, signals = _evaluate("street_name", claims, ctx)
    assert winner is None
    assert [s.rule for s in signals] == ["write_back_blocked_non_null"]
    assert signals[0].auto_action == "blocked_write"


def test_invariant_2_weaker_never_overwrites_stronger():
    claims = [
        mm.claim(1, "obec_name", value_text="Bílovec", extraction_method="html_selector_parse"),
    ]
    ctx = _ctx(
        incumbent={"obec_name": "Praha", "obec_name__granularity": "address_point"},
        claim_granularity={1: "obec"},
    )
    winner, signals = _evaluate("obec_name", claims, ctx)
    assert winner is None
    assert [s.rule for s in signals] == ["weaker_would_overwrite_stronger"]


def test_invariant_3_a_text_claim_must_gazetteer_validate_before_it_can_win():
    claims = [
        mm.claim(1, "street_name", value_text="Vymyšlená", extraction_method="llm_text",
                 claim_confidence="high"),
        mm.claim(2, "street_name", value_text="Vymyšlená", source="remax",
                 extraction_method="llm_text", claim_confidence="high"),
    ]
    ctx = _ctx(validate=lambda field, value: False)
    winner, signals = _evaluate("street_name", claims, ctx)
    assert winner is None
    assert {s.rule for s in signals} == {"text_claim_failed_gazetteer"}
    assert all(s.severity == "minor" for s in signals)  # downgrade + route, not a hard drop


def test_invariant_4_low_precision_defers_it_does_not_exclude():
    """A low-precision claim stays RANKABLE here — it is excluded only from co-location
    evidence and geometric blocking. Blocking's contract is recall."""
    claims = [
        mm.claim(1, "obec_name", value_text="Praha", claim_confidence="low",
                 blur_evidence="declared", declared_precision_label="municipality"),
    ]
    winner, _ = _evaluate("obec_name", claims, _ctx(claim_granularity={1: "obec"}))
    assert winner is not None and winner.value == "Praha"


def test_max_age_days_is_measured_against_as_of_never_against_now():
    old = mm.claim(1, "street_name", value_text="Slunečná",
                   extraction_method="portal_structured_field")
    policy = tuple(
        survivorship.FieldPolicyRow(
            policy_version="v1", field="street_name", source_pattern="portal:*",
            method_pattern="portal_structured_field", rank=300, max_age_days=1,
        )
        for _ in (0,)
    )
    fresh_ctx = _ctx(as_of=old.observed_at)
    stale_ctx = _ctx(as_of=old.observed_at + timedelta(days=5))
    assert survivorship.evaluate_field(
        "street_name", [old], normalize.normalize_all([old]), policy, fresh_ctx
    )[0] is not None
    assert survivorship.evaluate_field(
        "street_name", [old], normalize.normalize_all([old]), policy, stale_ctx
    )[0] is None


def test_the_ranking_has_no_precision_filter_at_all():
    """Invariant 4 is a NEGATIVE invariant: it holds because the evaluator never inspects
    a precision axis. Asserted structurally so a future 'helpful' filter fails here."""
    source = (__import__("inspect").getsource(survivorship.evaluate_field))
    for forbidden in ("uncertainty_radius", "position_quality", "pin_shared_by_n", "granularity <"):
        assert forbidden not in source
