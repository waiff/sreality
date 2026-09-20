"""E96: the standing check that E95's acceptance has not been revoked.

E95 accepts the honest clock on one operator ruling about one development and names the event
that takes it back — an operator NEGATIVE inside a K-B family. These tests pin the event's
definition: the operator tier only, the family read off the CERTIFICATE, and both adverts of
the pair in the SAME component.
"""

from __future__ import annotations

from autodedup import revocation
from autodedup.labels import (
    OPERATOR_TIER,
    OperatorLabelRow,
    SOURCE_EXPLICIT,
    operator_label_pairs,
)


def rows(*pairs: tuple[int, int], certificate: str = "K-B") -> list[dict[str, object]]:
    return [{"lo": lo, "hi": hi, "certificate": certificate} for lo, hi in pairs]


def ruled(pair: tuple[int, int], verdict: str) -> OperatorLabelRow:
    return OperatorLabelRow(
        lo=pair[0], hi=pair[1], verdict=verdict, source=SOURCE_EXPLICIT,
        relation="same_property" if verdict == "same" else None,
    )


def operator(*labelled: OperatorLabelRow) -> dict[tuple[int, int], object]:
    return operator_label_pairs(list(labelled))


def test_a_family_is_a_component_of_the_certified_pairs() -> None:
    families = revocation.families_from_rows(rows((1, 2), (2, 3), (7, 8)))
    assert sorted(len(members) for members in families.values()) == [2, 3]
    assert revocation.family_index(families)[3] == revocation.family_index(families)[1]


def test_an_uncertified_pair_makes_no_family() -> None:
    """The definition is `family.kb_families`': the certificate, never the zone or the model."""
    assert revocation.families_from_rows(rows((1, 2), certificate="K-C")) == {}
    assert revocation.families_from_rows(rows((1, 2), certificate="")) == {}


def test_an_operator_negative_inside_a_family_revokes_e95() -> None:
    report = revocation.check_rows(
        rows((1, 2), (2, 3)), operator(ruled((1, 3), "different"))
    )
    assert report.revoked and report.negatives == ((1, 3),)
    assert report.operator_labelled_inside == 1
    assert "E95 IS REVOKED" in report.line() and "1 x 3" in report.line()
    assert report.to_json()["revoked"] is True


def test_a_must_not_link_is_a_negative_however_it_is_spelled() -> None:
    report = revocation.check_rows(
        rows((1, 2)), operator(ruled((1, 2), "same_building_different_unit"))
    )
    assert report.revoked and report.negatives == ((1, 2),)


def test_an_operator_negative_ACROSS_two_families_does_not_revoke_it() -> None:
    """The rule is about a pair K-B certifies as one unit, not about any two listings the
    operator has separated — a negative between two chains is what the engine already says."""
    report = revocation.check_rows(
        rows((1, 2), (7, 8)), operator(ruled((1, 7), "different"))
    )
    assert not report.revoked and report.operator_labelled_inside == 0


def test_a_negative_touching_a_family_from_outside_does_not_revoke_it() -> None:
    report = revocation.check_rows(rows((1, 2)), operator(ruled((1, 99), "different")))
    assert not report.revoked and report.operator_labelled_inside == 0


def test_only_the_OPERATOR_tier_counts() -> None:
    """D31 (vii): gold contradicts the operator on 5 of 10 same-family positives, and E95 is
    the ruling that gold lost. A gold negative here measures the judge, not the engine."""
    report = revocation.check_rows(rows((1, 2)), {})
    assert not report.revoked and report.families == 1 and report.members == 2


def test_an_operator_POSITIVE_inside_a_family_is_counted_and_holds() -> None:
    report = revocation.check_rows(
        rows((1, 2), (2, 3)), operator(ruled((1, 2), "same"), ruled((1, 3), "same"))
    )
    assert not report.revoked and report.operator_labelled_inside == 2
    assert "E95 holds" in report.line()
    assert report.to_json() == {
        "families": 1, "members": 3, "operator_labelled_inside": 2,
        "negatives": [], "revoked": False,
    }


def test_an_unsure_ruling_is_no_ruling() -> None:
    report = revocation.check_rows(rows((1, 2)), operator(ruled((1, 2), "unsure")))
    assert not report.revoked and report.operator_labelled_inside == 0


def test_the_implied_tier_counts_too_because_a_cluster_ruling_is_a_ruling() -> None:
    implied = OperatorLabelRow(
        lo=1, hi=2, verdict="different", source="implied", cluster_key=5
    )
    report = revocation.check_rows(rows((1, 2)), operator_label_pairs([implied]))
    assert report.revoked
    assert next(iter(operator_label_pairs([implied]).values())).tier == OPERATOR_TIER
