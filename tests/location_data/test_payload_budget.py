"""The storage arithmetic behind the retention cap — the number the operator signs.

The cap shipped at 20 because the design document said 20, and nobody had multiplied it
by a corpus. This module is the multiplication, pinned so it cannot quietly stop being
true: if `payloads.DEFAULT_VERSION_CAP` is raised, or the frozen per-portal evidence is
edited, the ceiling moves and these tests say by how much.

TWO CORRECTIONS ARE PINNED HERE, because both were wrong in ways that made the gate pass
for the wrong reason:

  * The gate asserted the archive's WHOLE ceiling against the WHOLE subsystem budget,
    while ~16 GB of that budget was already spent. The real allowance is the remainder.
  * It gated on the `active` cohort while the module's own docstring says the archive
    converges on `ever` — rule 3 delists but never deletes, and a pinned first/latest
    body outlives the listing's activity. `ever` is 73 % larger.

Corrected, no cap fits a database-resident archive: even cap 1 on `ever` is 19.1 GB
against a 4 GB allowance. That is what moved the bodies to R2, and it is why the gate
now asserts the POSTGRES footprint — metadata rows — while the R2 bytes are reported.

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
    active / 9.6 GB ever before any retention policy is applied at all. Since W2a-7
    those are R2 bytes, which is what took the number off the critical path."""
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
    number 42 % smaller than the one they wanted."""
    with pytest.raises(ValueError):
        payload_budget.one_body_bytes("everything")


def test_the_allowance_is_what_is_left_of_the_budget_not_the_whole_of_it() -> None:
    """THE ACCOUNTING CORRECTION. Gating the archive against the subsystem's whole
    20 GB envelope compared it to a budget it does not have: the RÚIAN mirror, the
    claim spine and the projections already occupy ~16 GB, so the archive is competing
    for the remainder. Derived rather than hand-set, so re-measuring the subsystem
    moves the gate instead of needing two edits that can disagree."""
    assert payload_budget.ARCHIVE_ALLOWANCE_GB == pytest.approx(
        payload_budget.SUBSYSTEM_BUDGET_GB - payload_budget.SUBSYSTEM_SPENT_GB)
    assert 0 < payload_budget.ARCHIVE_ALLOWANCE_GB < payload_budget.SUBSYSTEM_BUDGET_GB


def test_no_cap_would_have_fitted_a_database_resident_archive() -> None:
    """The finding that moved the bodies, kept as the reason. Against the HONEST
    allowance and the cohort the archive converges on, there is no retention setting
    at which bodies-in-Postgres fits — not cap 20, not cap 2, not cap 1. A gate that
    cannot be satisfied by tuning its own knob is telling you the design is wrong,
    which is the only reason this PR touches storage placement at all."""
    for cap in (1, 2, 3, 20):
        bodies_in_postgres_gb = (payload_budget.bodies_per_group(cap)
                                 * payload_budget.one_body_bytes("ever")
                                 / payload_budget.BYTES_PER_GB)
        assert bodies_in_postgres_gb > payload_budget.ARCHIVE_ALLOWANCE_GB


def test_the_shipped_default_fits_the_allowance_on_the_cohort_it_converges_on() -> None:
    """THE GATE, on the two axes the old one got wrong: the archive's actual ALLOWANCE
    (not the subsystem's whole envelope) and the `ever` cohort (not `active`). Raising
    the default cap without re-deriving the arithmetic fails here rather than on the
    operator's storage bill."""
    ceiling = payload_budget.postgres_ceiling_gb(payloads.DEFAULT_VERSION_CAP, "ever")

    assert ceiling <= payload_budget.ARCHIVE_ALLOWANCE_GB, (
        f"cap {payloads.DEFAULT_VERSION_CAP} projects {ceiling:.2f} GB of Postgres over "
        f"{payload_budget.MEASURED_AT}'s ever-corpus, past the "
        f"{payload_budget.ARCHIVE_ALLOWANCE_GB:.1f} GB left of the location subsystem's "
        f"{payload_budget.SUBSYSTEM_BUDGET_GB:.0f} GB envelope — re-derive with "
        f"scripts/location_payload_storage_ceiling.py")


def test_the_gate_can_still_fail_for_the_right_reason() -> None:
    """A gate that nothing can trip is decoration. Postgres rows are no longer free
    just because the bodies left: at the cap this shipped with, 21 metadata rows per
    listing over the ever-corpus is 10.5 GB, well past the allowance."""
    assert payload_budget.postgres_ceiling_gb(20, "ever") > payload_budget.ARCHIVE_ALLOWANCE_GB
    assert not payload_budget.ceiling_table((20,))[0]["fits_allowance"]


def test_the_headroom_above_the_default_is_published_not_implied() -> None:
    """What the R2 move actually bought. The cap stopped being a budget instrument —
    the archive fits several caps deeper than the shipped default — so the number is
    now chosen on evidentiary grounds and the operator can see how much room a
    deeper one has. That headroom is the deliverable; the default staying at 2 is
    a judgement about readers, not a constraint."""
    headroom = payload_budget.largest_affordable_cap("ever")

    assert headroom > payloads.DEFAULT_VERSION_CAP
    assert payload_budget.postgres_ceiling_gb(headroom, "ever") <= (
        payload_budget.ARCHIVE_ALLOWANCE_GB)
    assert payload_budget.postgres_ceiling_gb(headroom + 1, "ever") > (
        payload_budget.ARCHIVE_ALLOWANCE_GB)


def test_the_postgres_footprint_is_row_overhead_rather_than_body_size() -> None:
    """The property that makes the archive affordable, asserted rather than asserted
    ABOUT: with bodies in the bucket the database cost moves with the corpus and the
    cap, not with how heavy a portal's HTML is. A metadata row is ~713 B against
    ~20 KB for the same row carrying its body."""
    row = payload_budget.postgres_row_bytes()

    assert 600 <= row <= 850
    mean_body = (payload_budget.one_body_bytes("ever")
                 / payload_budget.group_count("ever"))
    assert mean_body > 15 * row


def test_the_r2_bill_is_cents_and_therefore_not_the_constraint() -> None:
    """The other half of the sign-off. Object storage at ~$0.015/GB/month means even
    the cap this shipped with — a 200 GB archive — is ~$3/month, so the retention
    question stopped being "what can we afford" and became "what has a reader"."""
    assert payload_budget.r2_cost_usd_per_month(payloads.DEFAULT_VERSION_CAP, "ever") < 1.0
    assert payload_budget.r2_cost_usd_per_month(20, "ever") < 5.0


def test_the_two_footprints_together_are_the_whole_archive() -> None:
    """No bytes are unaccounted for: everything either spilled or stayed inline, and
    the inline residue is counted on the Postgres side, not dropped."""
    cap = payloads.DEFAULT_VERSION_CAP
    bodies = payload_budget.bodies_per_group(cap)
    inline_gb = bodies * payload_budget.inline_body_bytes("ever") / payload_budget.BYTES_PER_GB
    total_gb = bodies * payload_budget.one_body_bytes("ever") / payload_budget.BYTES_PER_GB

    assert payload_budget.r2_ceiling_gb(cap, "ever") == pytest.approx(
        total_gb - inline_gb, abs=0.01)
    rows_gb = (bodies * payload_budget.group_count("ever")
               * payload_budget.postgres_row_bytes() / payload_budget.BYTES_PER_GB)
    assert payload_budget.postgres_ceiling_gb(cap, "ever") == pytest.approx(
        rows_gb + inline_gb, abs=0.01)


def test_the_ceiling_is_linear_in_the_cap() -> None:
    """Not a tautology worth asserting for its own sake — it is the claim the whole
    recommendation rests on: there is no cap at which the archive gets cheap, so the
    lever is the cap itself and not a better volatile profile."""
    # Off the unrounded functions, not off `ceiling_table`'s display rounding: ten
    # steps of a value rounded to 0.1 GB accumulate half a gigabyte of slop and would
    # need a tolerance wide enough to hide a real non-linearity.
    step = payload_budget.r2_ceiling_gb(3, "ever") - payload_budget.r2_ceiling_gb(2, "ever")

    assert step == pytest.approx(
        (payload_budget.one_body_bytes("ever") - payload_budget.inline_body_bytes("ever"))
        / payload_budget.BYTES_PER_GB, abs=0.01)
    assert payload_budget.r2_ceiling_gb(20, "ever") == pytest.approx(
        payload_budget.r2_ceiling_gb(10, "ever") + 10 * step, abs=0.01)


def test_every_portal_carries_its_provenance() -> None:
    """A frozen measurement without a method is folklore in a year. Each row has to say
    which live read and which compression measurement produced it, because
    `scripts/location_payload_storage_ceiling.py` re-derives exactly these and the
    operator has to be able to tell a real drift from a change of method."""
    for portal in payload_budget.PORTAL_STORAGE:
        assert portal.provenance
        assert portal.stored_bytes_per_body > 0
        assert portal.groups_ever >= portal.groups_active
    for component in payload_budget.POSTGRES_ROW_LAYOUT:
        assert component.provenance
        assert component.bytes_per_row > 0


def test_the_frozen_corpus_covers_every_portal_the_archive_can_receive() -> None:
    """A source missing here is a source missing from the ceiling — the failure mode
    that makes a storage projection read low and get signed. Checked against the portal
    CONTRACTS, which is the same list `payload_dual_write` is gated per-portal from, so
    onboarding a tenth portal fails here until its page weight is measured."""
    from location_data.contracts import CONTRACT_DIR

    contracted = {path.stem for path in CONTRACT_DIR.glob("*.yaml")}

    assert {p.source for p in payload_budget.PORTAL_STORAGE} == contracted


def test_the_corpus_is_detail_only_and_says_so_rather_than_implying_it() -> None:
    """The surface half of the same failure. `payload_index_archive` can point the
    writer at index, map or gazetteer surfaces, whose groups are week-stamped
    positions rather than listings and whose page weight nobody has measured — so a
    ceiling computed from this table would silently omit them. Naming the measured
    surfaces is what lets the write chokepoint refuse the rest."""
    assert payload_budget.measured_surfaces() == {
        (p.source, "detail") for p in payload_budget.PORTAL_STORAGE}
    assert payload_budget.is_measured("idnes", "detail")
    assert not payload_budget.is_measured("idnes", "index")
    assert not payload_budget.is_measured("nosuchportal", "detail")
