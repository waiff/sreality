"""Per-portal W1 extraction, against payload shapes taken from recon/db-raw-samples.md §3.

Every assertion here is a statement about what the CONTRACT plus the payload produce — the
extractor has no per-portal branches of its own beyond the readers the contract names.
"""

from __future__ import annotations

from location_data.claims_intake import extract_listing
from tests.location_data.claim_intake_fixtures import (
    BAZOS_LINK,
    BEZREALITKY,
    CESKEREALITY_PAGE,
    IDNES_PAGE,
    MAXIMA_PAGE,
    REALITYMIX_PAGE,
    REMAX,
    SREALITY_POST_CUTOVER,
    entries_for,
    listing,
)


def test_every_claim_writes_blur_evidence_and_history_completeness_explicitly():
    """The fleet mechanic, and the one thing this file still asserts for all nine: whatever
    a portal's contract reads, every claim it produces writes the two axes 06 §6.6 rules 6
    and 7 forbid this lane to default, and stamps the contract version that produced it.

    NO pinned version table any more. A version is the record of one portal's extraction
    changes, and pinning nine of them here made every contract bump a diff in a file about
    lane mechanics — the versions are asserted per portal, in the per-portal contract
    tests (W1-c R14). A portal whose contract reads only the stored page body yields no
    PAYLOAD claim at all, which is why the loop asserts over what it gets rather than
    demanding a claim from each one."""
    cases = (
        ("sreality", SREALITY_POST_CUTOVER, 50.0, 14.4),
        ("bezrealitky", BEZREALITKY, 50.1, 14.4),
        ("bazos", BAZOS_LINK, 48.8, 17.1),
        ("idnes", IDNES_PAGE, 50.4, 13.4),
        ("remax", REMAX, 50.0, 14.4),
        ("ceskereality", CESKEREALITY_PAGE, 50.0, 14.3),
        ("realitymix", REALITYMIX_PAGE, 50.3, 13.6),
        ("maxima", MAXIMA_PAGE, 50.7, 15.0),
    )
    expected_history = {
        "sreality": "full", "bezrealitky": "payload_only", "mmreality": "payload_only",
    }
    seen = 0
    for source, payload, lat, lon in cases:
        entries = entries_for(source)
        (expected_version,) = {e.contract_version for e in entries}
        result = extract_listing(listing(source, payload), entries)
        for claim in result.claims:
            seen += 1
            assert claim.blur_evidence in ("none", "declared"), (source, claim.extractor_id)
            assert claim.history_completeness == expected_history.get(
                source, "locality_text_only")
            assert claim.extractor_version == f"contract:{source}@{expected_version}"
            assert claim.first_observed_at == result.claims[0].first_observed_at
    assert seen, "no portal yielded a payload claim — the loop proves nothing"
