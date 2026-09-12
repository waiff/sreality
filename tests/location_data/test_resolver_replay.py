"""THE purity gate: deterministic replay.

"Re-running on unchanged claims and an unchanged registry reproduces a byte-identical row"
is only testable with every version input pinned. W2-a took that from five to THREE —
`claim_set_hash`, `resolver_version`, `registry_version` — because the two that went
(`policy_version`, `collision_epoch_id`) named tables this wave deletes. The two remaining
knobs are exactly the two the drain's sweep compares, so this file is also the gate on
"bumping the version re-resolves the corpus".

The comparison is on BYTES: the canonical serialization of the whole answer row.
"""

from __future__ import annotations

import dataclasses

from location_data.resolver import core, serialize
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm

REGISTRY = "ruian:2026-07-31"


def _claims():
    return [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
        mm.claim(3, "psc", value_text="160 00"),
        mm.claim(4, "coordinate", lat=50.10102, lon=14.34804,
                 declared_precision_label="gps"),
        mm.claim(5, "cast_obce_name", value_text="Vokovice"),
    ]


def _resolve(claims=None, ctx=None, **overrides):
    kwargs = {"resolver_version": RESOLVER_VERSION, "registry_version": REGISTRY}
    kwargs.update(overrides)
    return core.resolve(claims or _claims(), ctx or mm.context(), **kwargs)


def _bytes(resolution) -> str:
    return serialize.canonical(dataclasses.asdict(resolution))


def test_two_runs_are_byte_identical():
    first, second = _resolve(), _resolve()
    assert _bytes(first) == _bytes(second)
    assert first.claim_set_hash == second.claim_set_hash


def test_a_fresh_context_and_a_reordered_claim_list_replay_identically():
    """The claim list arrives in whatever order the drain read it; the row must not."""
    forward = _resolve(_claims(), mm.context())
    backward = _resolve(list(reversed(_claims())), mm.context())
    assert _bytes(forward) == _bytes(backward)


def test_each_of_the_three_version_inputs_changes_the_row():
    """The three the answer row stamps and the sweep compares. A hard-coded "next" version
    silently stopped varying its input the day RESOLVER_VERSION caught up with it, so it is
    derived."""
    base = _bytes(_resolve())
    assert _bytes(_resolve(resolver_version=f"{RESOLVER_VERSION}+next")) != base
    assert _bytes(_resolve(registry_version="ruian:2026-08-31")) != base
    changed = _claims()
    changed[0] = mm.claim(1, "obec_name", value_text="Bílovec")
    assert _resolve(changed).claim_set_hash != _resolve().claim_set_hash


def test_a_naive_and_an_aware_observed_at_hash_the_same():
    """`datetime.timestamp()` on a naive value silently applies the HOST's timezone, so the
    same claims would hash differently on two machines. `serialize._plain` reads a naive
    instant as UTC."""
    import datetime as dt

    aware = _claims()
    naive = [
        dataclasses.replace(c, observed_at=c.observed_at.replace(tzinfo=None)) for c in aware
    ]
    assert _resolve(naive).claim_set_hash == _resolve(aware).claim_set_hash
    assert isinstance(aware[0].observed_at, dt.datetime)


def test_the_r0_registry_key_resolves_to_the_address_point():
    claims = _claims() + [mm.claim(6, "address_point_id", value_text="21690278")]
    resolution = _resolve(claims)
    assert resolution.granularity == "address_point"
    assert resolution.ruian_adm_kod == 21690278
