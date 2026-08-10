"""Admissibility, pin choice and typed-slot survivorship — the three places a claim that
S7 already refuses used to win anyway (03 §3.2 rule 4, §3.6, §3.9.1).

`survivorship.admissible()` is the gate: `subject_scoped=false` (the remax carousel class),
`licence_class='ephemeral_display_only'` (Mapy), a portal-proprietary identifier, a claim S1
rejected. S7 applied it. S3 and S4 did not — so the carousel street ranked a candidate,
that candidate carried the admin chain, and the preserve-if-null registry fill then wrote
the poisoned address back out as `registry_derived`. The gate is now evaluated ONCE in
`core.resolve` and handed to both.

Excluded claims are still STORED and still reach S9: a refused coordinate becomes a
candidate row with its own rejection reason, and a refused street still opens
`street_from_excluded_block_vs_served`. Refused means "may not win", never "discarded".
"""

from __future__ import annotations

from datetime import datetime, timezone

from location_data.resolver import core, reconciler, serialize
from location_data.resolver.types import Claim
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm

# The Prague address the mini-mirror carries end to end.
STREET = "Nad Bořislavkou 487/40"


def _resolve(claims, ctx=None):
    return core.resolve(
        claims,
        ctx or mm.context(),
        resolver_version=RESOLVER_VERSION,
        registry_version_id=7,
        policy_version="v1",
        collision_epoch_id=11,
    )


def _coordinate_candidates(resolution):
    return [c for c in resolution.candidates if c.target_kind == "coordinate_only"]


# --------------------------------------------------------------- S3: the carousel street


def test_a_carousel_street_never_wins_the_street_field():
    resolution = _resolve(
        [
            mm.claim(1, "obec_name", value_text="Praha"),
            mm.claim(2, "street_name", value_text=STREET),
            mm.claim(3, "street_name", value_text="Milady Horákové 12", subject_scoped=False),
        ]
    )
    assert str(resolution.fields["street_name"].value).startswith("Nad Bořislavkou")
    assert resolution.fields["street_name"].source_claim_ids == (2,)


def test_a_carousel_street_never_ranks_a_candidate_or_carries_the_admin_chain():
    """With NO admissible street claim the listing must resolve at the obec rung, not at
    whatever address the carousel happened to name."""
    resolution = _resolve(
        [
            mm.claim(1, "obec_name", value_text="Praha"),
            mm.claim(2, "street_name", value_text=STREET, subject_scoped=False),
            mm.claim(3, "house_number_cp", value_text="487", subject_scoped=False),
        ]
    )
    assert "street_name" not in resolution.fields
    assert resolution.precision.granularity in ("obec", "cast_obce_or_quarter", "unknown")
    for candidate in resolution.candidates:
        assert candidate.source_claim_ids != (2,)


def test_an_excluded_street_still_reaches_s9():
    """Stored, never rankable, and it DOES open the finding (§3.11.1)."""
    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text=STREET),
        mm.claim(3, "street_name", value_text="Milady Horákové 12", subject_scoped=False),
    ]
    resolution = _resolve(claims)
    detections = reconciler.run(
        resolution, claims, {}, registry=mm.default_mirror()
    )
    assert "street_from_excluded_block_vs_served" in {d.rule for d in detections}


# ------------------------------------------------------------------ S4: which pin wins


def test_the_pin_is_chosen_by_declared_quality_not_by_claim_id():
    """A blurred coordinate that merely arrived FIRST used to become the position."""
    resolution = _resolve(
        [
            mm.claim(1, "obec_name", value_text="Praha"),
            mm.claim(2, "coordinate", lat=50.0755, lon=14.4378,
                     declared_precision_label="municipality"),
            mm.claim(3, "coordinate", lat=50.10102, lon=14.34804,
                     declared_precision_label="gps"),
        ]
    )
    assert resolution.position.source_claim_ids == (3,)
    assert resolution.position.lat == 50.10102
    loser = next(c for c in _coordinate_candidates(resolution) if c.source_claim_ids == (2,))
    assert loser.rejected_reason == "lost_to_declared_quality"
    assert loser.distance_to_pin_m is not None and loser.distance_to_pin_m > 0


def test_a_carousel_coordinate_never_becomes_the_pin_but_is_still_stored():
    resolution = _resolve(
        [
            mm.claim(1, "obec_name", value_text="Praha"),
            mm.claim(2, "coordinate", lat=49.5936, lon=17.2987, subject_scoped=False),
            mm.claim(3, "coordinate", lat=50.0755, lon=14.4378),
        ]
    )
    assert resolution.position.source_claim_ids == (3,)
    loser = next(c for c in _coordinate_candidates(resolution) if c.source_claim_ids == (2,))
    assert loser.rejected_reason == "claim_inadmissible"


def test_an_ephemeral_coordinate_never_becomes_the_pin_but_is_still_stored():
    """00 §6.1 artifacts 2/3: the licence class is a structural bar, and the candidate row
    is the forensic record the purge ledger needs."""
    resolution = _resolve(
        [
            mm.claim(1, "obec_name", value_text="Praha"),
            mm.claim(2, "coordinate", lat=49.5936, lon=17.2987,
                     licence_class="ephemeral_display_only"),
            mm.claim(3, "coordinate", lat=50.0755, lon=14.4378),
        ]
    )
    assert resolution.position.source_claim_ids == (3,)
    assert resolution.position_licence_class != "ephemeral_display_only"
    loser = next(c for c in _coordinate_candidates(resolution) if c.source_claim_ids == (2,))
    assert loser.rejected_reason == "licence_ephemeral_inadmissible"


def test_a_blurred_sibling_does_not_blur_the_pin_it_lost_to():
    """The declaration hanging off a LOSING coordinate is that coordinate's, not the
    listing's."""
    resolution = _resolve(
        [
            mm.claim(1, "obec_name", value_text="Praha"),
            mm.claim(2, "coordinate", lat=50.0755, lon=14.4378,
                     declared_precision_label="municipality"),
            mm.claim(3, "coordinate", lat=50.10102, lon=14.34804,
                     declared_precision_label="gps"),
        ]
    )
    assert resolution.position.position_source == "portal_pin"
    assert resolution.position.blur_evidence == "none"


# ------------------------------------------------- S6: the declared-vs-assigned conflict


def test_declared_precision_vs_assigned_can_actually_fire():
    """It is tested against the rung S6 was HANDED. Comparing the value S6 RETURNED could
    never fire — S6 applies the declared cap itself, so the post-cap rung is at most the
    cap by construction."""
    resolution = _resolve(
        [
            mm.claim(1, "obec_name", value_text="Praha"),
            mm.claim(2, "street_name", value_text=STREET),
            mm.claim(3, "psc", value_text="160 00"),
            mm.claim(4, "coordinate", lat=50.10102, lon=14.34804,
                     declared_precision_label="municipality"),
        ]
    )
    assert "declared_precision_vs_assigned" in {
        s.rule for s in resolution.contradiction_signals
    }
    # ... and the cap is still applied: the signal reports, it does not certify.
    rank = mm.context().granularity_rank
    assert rank.rank(resolution.precision.granularity) <= rank.rank("obec")


def test_no_conflict_when_the_declaration_matches_the_rung():
    resolution = _resolve(
        [
            mm.claim(1, "obec_name", value_text="Praha"),
            mm.claim(2, "coordinate", lat=50.0755, lon=14.4378,
                     declared_precision_label="municipality"),
        ]
    )
    assert "declared_precision_vs_assigned" not in {
        s.rule for s in resolution.contradiction_signals
    }


# --------------------------------------------------------- S7: typed slots, not verbatim


def test_a_combined_house_number_claim_is_unwrapped_into_its_own_slot():
    """03 §3.3.2: three typed slots, never collapsed. Keying the unwrap on which slot
    happens to be PRESENT wrote "487/40" into house_number_cp verbatim, because a
    house-number claim carries no `street`/`psc` slot to trip the old branch."""
    resolution = _resolve(
        [
            mm.claim(1, "obec_name", value_text="Praha"),
            mm.claim(2, "house_number_cp", value_text="487/40"),
        ]
    )
    assert resolution.fields["house_number_cp"].value == "487"


def test_the_orientation_number_keeps_its_letter():
    resolution = _resolve(
        [
            mm.claim(1, "obec_name", value_text="Praha"),
            mm.claim(2, "house_number_co", value_text="487/40a"),
        ]
    )
    assert resolution.fields["house_number_co"].value == "40a"


def test_an_evidencni_claim_is_unwrapped_too():
    resolution = _resolve(
        [
            mm.claim(1, "obec_name", value_text="Praha"),
            mm.claim(2, "evidencni", value_text="ev.č. 12"),
        ]
    )
    assert resolution.fields["evidencni"].value == "12"


# ------------------------------------------------------------ purity: no local timezone


def test_a_naive_observed_at_is_read_as_utc_not_as_process_local_time():
    naive = datetime(2026, 8, 1, 12, 0)
    aware = naive.replace(tzinfo=timezone.utc)
    assert serialize.epoch_seconds(naive) == aware.timestamp()
    assert serialize.epoch_seconds(aware) == aware.timestamp()


def test_a_naive_and_an_aware_claim_set_resolve_identically():
    """The survivorship sort key reads `observed_at`; `datetime.timestamp()` on a naive
    value silently applies the HOST's timezone, so the same claims would rank differently
    on two machines."""

    def _claims(tzinfo):
        moment = datetime(2026, 8, 1, 12, 0, tzinfo=tzinfo)
        return [
            Claim(
                id=i, listing_id=900001, source="sreality", claim_type=claim_type,
                surface="api_json", extraction_method="portal_structured_field",
                extractor_id="fx", licence_class="portal", observed_at=moment,
                value_text=value, claim_confidence="high", subject_scoped=True,
            )
            for i, (claim_type, value) in enumerate(
                (("obec_name", "Praha"), ("street_name", STREET)), start=1
            )
        ]

    naive = _resolve(_claims(None))
    aware = _resolve(_claims(timezone.utc))
    assert naive.fields["street_name"].value == aware.fields["street_name"].value
    assert naive.precision.granularity == aware.precision.granularity
