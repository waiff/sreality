"""The registry's canonical street form on registry-bound rows (operator decision
2026-09-10), and the one exception to it.

Before W2-a this was a survivorship RANKING — policy v1's ('ruian','registry_derived',100)
rung outranking the portal's 300 — and the preserve-if-null fill was the reason it never
produced a winner. FILL states it directly instead: on a bound row the registry's spelling
IS the street name. The only value it does not get to respell is an OPERATOR correction,
which is the single survivorship rule that outlived `location_field_policy`.
"""

from __future__ import annotations

from location_data.resolver import core
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm


def _resolve(claims):
    return core.resolve(
        claims, mm.context(), resolver_version=RESOLVER_VERSION,
        registry_version="ruian:2026-07-31",
    )


def test_case_and_diacritics_are_respelled_from_the_registry():
    """The bulk class (`Na Strži` vs `Na strži`): same match key, same street. The portal's
    spelling never reaches the answer row."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Borislavkou 487/40"),
    ])
    assert resolution.street_name == "Nad Bořislavkou"
    assert resolution.ruian_adm_kod == 21690278


def test_a_dropped_prefix_is_restored():
    """`Budovatelů` vs RÚIAN `nám. Budovatelů`. The normalizer parses the type word OFF the
    claim, so the two are indistinguishable downstream and both mean the same street."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Bílovec"),
        mm.claim(2, "street_name", value_text="Budovatelů"),
        mm.claim(3, "address_point_id", value_text="33000002"),
    ])
    assert resolution.street_name == "nám. Budovatelů"


def test_a_claim_that_carries_the_prefix_agrees():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Bílovec"),
        mm.claim(2, "street_name", value_text="nám. Budovatelů 5"),
        mm.claim(3, "address_point_id", value_text="33000002"),
    ])
    assert resolution.street_name == "nám. Budovatelů"


def test_a_different_street_loses_to_the_address_point_the_row_is_bound_to():
    """A real disagreement: the portal names a DIFFERENT Bílovec street than the address
    point the row is bound to. The registry still wins — and there is no ledger row any
    more, because the `disputed` column carries one reason and this is not one of them."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Bílovec"),
        mm.claim(2, "street_name", value_text="Slunečná"),
        mm.claim(3, "address_point_id", value_text="33000002"),
    ])
    assert resolution.street_name == "nám. Budovatelů"
    assert resolution.disputed is None


def test_an_operator_correction_is_never_respelled_by_the_registry():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Bílovec"),
        mm.claim(2, "street_name", value_text="Budovatelů"),
        mm.claim(3, "address_point_id", value_text="33000002"),
        mm.claim(4, "street_name", value_text="Budovatelská", source="operator",
                 extraction_method="operator_manual", surface="operator"),
    ])
    assert resolution.street_name == "Budovatelská"


def test_the_registry_still_fills_a_street_nobody_claimed():
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Bílovec"),
        mm.claim(2, "address_point_id", value_text="33000002"),
    ])
    assert resolution.street_name == "nám. Budovatelů"


def test_a_street_the_registry_cannot_bind_keeps_the_portals_spelling():
    """Preserve-if-null: with no address point and no matching RÚIAN street there is nothing
    to respell to, and dropping the value would be worse than keeping the portal's."""
    resolution = _resolve([
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Neexistující 4"),
    ])
    assert resolution.street_name == "Neexistující"
    assert resolution.ulice_kod is None
