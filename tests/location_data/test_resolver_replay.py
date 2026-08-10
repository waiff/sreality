"""THE W1 GATE: deterministic replay (03 §3.0 rule 4, 06 §6.4).

"Re-running on unchanged claims + registry_version reproduces byte-identical
`location_resolutions`" is only testable with all FIVE version inputs pinned — including
`collision_epoch_id`, the corpus-wide one — or the gate passes only on the day it is run.

The comparison is on BYTES: the canonical serialization of the whole resolution payload,
plus the content hash stamped on the row.
"""

from __future__ import annotations

from location_data.resolver import core, serialize
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm


def _claims():
    return [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
        mm.claim(3, "psc", value_text="160 00"),
        mm.claim(4, "coordinate", lat=50.10102, lon=14.34804,
                 declared_precision_label="gps"),
        mm.claim(5, "cast_obce_name", value_text="Vokovice"),
    ]


def _resolve(claims=None, ctx=None):
    return core.resolve(
        claims or _claims(),
        ctx or mm.context(),
        resolver_version=RESOLVER_VERSION,
        registry_version_id=7,
        policy_version="v1",
        collision_epoch_id=11,
    )


def test_two_runs_are_byte_identical():
    first, second = _resolve(), _resolve()
    assert serialize.canonical(core.content_payload(first)) == serialize.canonical(
        core.content_payload(second)
    )
    assert first.content_hash == second.content_hash
    assert first.claim_set_hash == second.claim_set_hash


def test_a_fresh_context_and_a_reordered_claim_list_replay_identically():
    """The claim list arrives in whatever order the drain read it; the resolution must
    not."""
    forward = _resolve(_claims(), mm.context())
    backward = _resolve(list(reversed(_claims())), mm.context())
    assert forward.content_hash == backward.content_hash


def test_as_of_is_the_max_observed_at_not_a_wall_clock():
    claims = _claims()
    resolution = _resolve(claims)
    assert resolution.as_of == max(c.observed_at for c in claims)


def test_changing_any_one_version_input_changes_the_identity():
    base = _resolve()
    others = [
        core.resolve(_claims(), mm.context(), resolver_version="resolver:v2",
                     registry_version_id=7, policy_version="v1", collision_epoch_id=11),
        core.resolve(_claims(), mm.context(), resolver_version=RESOLVER_VERSION,
                     registry_version_id=8, policy_version="v1", collision_epoch_id=11),
        core.resolve(_claims(), mm.context(), resolver_version=RESOLVER_VERSION,
                     registry_version_id=7, policy_version="v2", collision_epoch_id=11),
        core.resolve(_claims(), mm.context(), resolver_version=RESOLVER_VERSION,
                     registry_version_id=7, policy_version="v1", collision_epoch_id=12),
    ]
    for other in others:
        assert other.content_hash != base.content_hash


def test_a_changed_claim_value_changes_the_claim_set_hash():
    changed = _claims()
    changed[0] = mm.claim(1, "obec_name", value_text="Bílovec")
    assert _resolve(changed).claim_set_hash != _resolve().claim_set_hash


def test_the_r0_registry_key_resolves_to_the_address_point():
    claims = _claims() + [mm.claim(6, "address_point_id", value_text="21690278")]
    resolution = _resolve(claims)
    assert resolution.precision.granularity == "address_point"
    assert resolution.position.position_source == "registry_point"
    assert resolution.candidates[0].ruian_adm_kod == 21690278
