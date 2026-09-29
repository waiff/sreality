"""E111: the standing check that E110's acceptance has not been revoked.

E110 accepts the honest clock on one operator ruling about one development and names the event
that takes it back — an operator NEGATIVE inside a K-B family. These tests pin the event's
definition: the operator tier only, the family read off the CERTIFICATE, and both adverts of
the pair in the SAME component.
"""

from __future__ import annotations

from autodedup import revocation


def rows(*pairs: tuple[int, int], certificate: str = "K-B") -> list[dict[str, object]]:
    return [{"lo": lo, "hi": hi, "certificate": certificate} for lo, hi in pairs]


def ruled(pair: tuple[int, int], verdict: str) -> tuple[tuple[int, int], str]:
    return pair, verdict


def operator(*labelled: tuple[tuple[int, int], str]) -> dict[tuple[int, int], str]:
    return dict(labelled)


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
    assert "E110 IS REVOKED" in report.line() and "1 x 3" in report.line()
    assert report.to_json()["revoked"] is True


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
    """D31 (vii): gold contradicts the operator on 5 of 10 same-family positives, and E110 is
    the ruling that gold lost. A gold negative here measures the judge, not the engine."""
    report = revocation.check_rows(rows((1, 2)), {})
    assert not report.revoked and report.families == 1 and report.members == 2


def test_an_operator_POSITIVE_inside_a_family_is_counted_and_holds() -> None:
    report = revocation.check_rows(
        rows((1, 2), (2, 3)), operator(ruled((1, 2), "same"), ruled((1, 3), "same"))
    )
    assert not report.revoked and report.operator_labelled_inside == 2
    assert "E110 holds" in report.line()
    assert report.to_json() == {
        "families": 1, "members": 3, "operator_labelled_inside": 2,
        "negatives": [], "revoked": False,
    }
