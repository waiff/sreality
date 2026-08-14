"""The storage arithmetic behind the retention cap — the number the operator signs.

The cap shipped at 20 because the design document said 20, and nobody had multiplied it
by a corpus. This module is the multiplication, pinned so it cannot quietly stop being
true: if `payloads.DEFAULT_VERSION_CAP` is raised, or the frozen per-portal evidence is
edited, the ceiling moves and these tests say by how much.

No database and no network — `payload_budget` is frozen measurements plus arithmetic,
and that is exactly what makes it assertable in the ordinary test lane rather than only
in the DB job.
"""

from __future__ import annotations

import pytest

from location_data import payload_budget, payloads


def test_one_body_per_listing_is_already_six_gigabytes() -> None:
    """The floor nobody had computed. A cap of 1 does not buy a small archive: the
    archive's unit of cost is one body per listing, and on this corpus that is 6.1 GB
    before any retention policy is applied at all."""
    active = payload_budget.one_body_bytes("active") / payload_budget.BYTES_PER_GB
    ever = payload_budget.one_body_bytes("ever") / payload_budget.BYTES_PER_GB

    assert 6.0 <= active <= 6.2
    # Rule 3 delists but never deletes, and a pinned first/latest body outlives the
    # listing's activity — so the archive converges on this cohort, not the active one.
    assert 9.4 <= ever <= 9.7
    assert ever > active


def test_the_worst_case_is_cap_plus_one_body_per_group() -> None:
    """`_PRUNE_SQL` deletes unpinned rows ranked beyond the cap, and the FIRST version
    is pinned — so once a group is deeper than the cap the first body survives OUTSIDE
    it. A ceiling quoted as `cap x bytes` understates by a whole body per listing."""
    assert payload_budget.bodies_per_group(1) == 2
    assert payload_budget.bodies_per_group(20) == 21


def test_a_cap_below_one_is_not_a_retention_policy() -> None:
    with pytest.raises(ValueError):
        payload_budget.bodies_per_group(0)


def test_an_unknown_cohort_is_refused_rather_than_defaulted() -> None:
    """Silently falling back to "active" would answer a question nobody asked with a
    number 36 % smaller than the one they wanted."""
    with pytest.raises(ValueError):
        payload_budget.one_body_bytes("everything")


def test_the_inherited_default_of_twenty_permits_seven_budgets_of_archive() -> None:
    """The finding this PR exists for, kept as a regression: at the cap the store
    shipped with, the archive alone is ~128 GB against a subsystem budgeted at 20 GB
    in total — and that is the CEILING, reached by churn rather than caused by it."""
    assert payload_budget.ceiling_gb(20, "active") > 6 * payload_budget.SUBSYSTEM_BUDGET_GB


def test_the_shipped_default_ceiling_fits_the_subsystem_budget() -> None:
    """THE GATE. Raising the default cap without re-deriving the arithmetic fails here
    rather than on the operator's storage bill. If a future measurement justifies a
    higher cap, the evidence in PORTAL_STORAGE moves first and this assertion follows."""
    ceiling = payload_budget.ceiling_gb(payloads.DEFAULT_VERSION_CAP, "active")

    assert ceiling <= payload_budget.SUBSYSTEM_BUDGET_GB, (
        f"cap {payloads.DEFAULT_VERSION_CAP} projects a {ceiling:.1f} GB archive over "
        f"{payload_budget.MEASURED_AT}'s active corpus, past the "
        f"{payload_budget.SUBSYSTEM_BUDGET_GB:.0f} GB the whole location subsystem is "
        f"budgeted — re-derive with scripts/location_payload_storage_ceiling.py")
    # And the next cap up does not, which is what makes 2 a chosen number rather than
    # a merely-safe one: each unit of cap costs another ~6.1 GB.
    assert payload_budget.ceiling_gb(
        payloads.DEFAULT_VERSION_CAP + 1, "active") > payload_budget.SUBSYSTEM_BUDGET_GB


def test_the_ceiling_is_linear_in_the_cap() -> None:
    """Not a tautology worth asserting for its own sake — it is the claim the whole
    recommendation rests on: there is no cap at which the archive gets cheap, so the
    lever is the cap itself and not a better volatile profile."""
    table = {row["cap"]: row["active_gb"] for row in payload_budget.ceiling_table()}
    step = table[3] - table[2]

    assert step == pytest.approx(payload_budget.one_body_bytes("active")
                                 / payload_budget.BYTES_PER_GB, abs=0.1)
    assert table[20] == pytest.approx(table[10] + 10 * step, abs=0.2)


def test_every_portal_carries_its_provenance() -> None:
    """A frozen measurement without a method is folklore in a year. Each row has to say
    which live read and which compression measurement produced it, because
    `scripts/location_payload_storage_ceiling.py` re-derives exactly these and the
    operator has to be able to tell a real drift from a change of method."""
    for portal in payload_budget.PORTAL_STORAGE:
        assert portal.provenance
        assert portal.stored_bytes_per_body > 0
        assert portal.listings_ever >= portal.active_listings


def test_the_frozen_corpus_covers_every_portal_the_archive_can_receive() -> None:
    """A source missing here is a source missing from the ceiling — the failure mode
    that makes a storage projection read low and get signed. Checked against the portal
    CONTRACTS, which is the same list `payload_dual_write` is gated per-portal from, so
    onboarding a tenth portal fails here until its page weight is measured."""
    from location_data.contracts import CONTRACT_DIR

    contracted = {path.stem for path in CONTRACT_DIR.glob("*.yaml")}

    assert {p.source for p in payload_budget.PORTAL_STORAGE} == contracted
