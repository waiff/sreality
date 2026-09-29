"""E111: the standing check that E110's acceptance still holds.

E110 accepts the honest live-window clock on ONE operator ruling about ONE development, and it
names what takes the acceptance back: an operator NEGATIVE inside a K-B family — a pair the
operator rules is two units, whose two adverts sit in one component of the pairs K-B certified
under the honest clock. A rule that names its own revoking event and then leaves the event to
be remembered is a rule that expires silently, so the event is a COUNT something runs.

The family is `family.kb_families`' family and never a second spelling of it: the guard, the
incremental rail and this check have to disagree about nothing, and a component read off merge
edges or off zones moves under its own verdict (E85). What the check adds is only the join —
which operator-labelled pairs sit inside one of those components, and how many of them are
negatives.

Only the operator's rulings count (`evaluate.read_rulings`: the newest ruling per pair, a
must-not-link included). Gold saying `different` inside a K-B family is exactly the reading
E110 overturned (D31 vii), so no machine label is read here.

A revocation is a settings change, not a code change: put a number back in
`certificate_b_min_images` and the W9 arm returns exactly. So this reports; it never decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from autodedup.family import kb_families
from autodedup.labels import PairKey, pair_key

CERTIFICATE: str = "K-B"


@dataclass(frozen=True, slots=True)
class _Certified:
    """The three fields `kb_families` reads, so a stored pair row can feed the one definition."""

    lo: int
    hi: int
    certificate: str


@dataclass(frozen=True, slots=True)
class Revocation:
    """What the check found: the families, what the operator has said inside them, and the
    offending pairs. `revoked` is a property of the offenders and never a stored flag."""

    families: int
    members: int
    operator_labelled_inside: int
    negatives: tuple[PairKey, ...]

    @property
    def revoked(self) -> bool:
        return bool(self.negatives)

    def to_json(self) -> dict[str, Any]:
        return {
            "families": self.families,
            "members": self.members,
            "operator_labelled_inside": self.operator_labelled_inside,
            "negatives": [list(pair) for pair in self.negatives],
            "revoked": self.revoked,
        }

    def line(self) -> str:
        head = (
            f"E111 revocation check: {len(self.negatives)} operator negative(s) inside "
            f"{self.families} K-B families ({self.operator_labelled_inside} operator-labelled "
            f"pairs inside them)"
        )
        if not self.revoked:
            return head + " — E110 holds"
        named = ", ".join(f"{lo} x {hi}" for lo, hi in self.negatives[:5])
        return (
            head
            + f" — E110 IS REVOKED by {named}"
            + (" …" if len(self.negatives) > 5 else "")
            + ": put certificate_b_min_images back and the W9 arm returns"
        )


def families_from_rows(rows: Iterable[Mapping[str, Any]]) -> dict[int, list[int]]:
    """K-B families off stored pair rows, through `family.kb_families` rather than beside it."""
    return kb_families(
        _Certified(int(row["lo"]), int(row["hi"]), CERTIFICATE)
        for row in rows
        if (row.get("certificate") or "") == CERTIFICATE
    )


def family_index(families: Mapping[int, list[int]]) -> dict[int, int]:
    return {member: key for key, members in families.items() for member in members}


def check(
    families: Mapping[int, list[int]],
    rulings: Mapping[PairKey, str],
) -> Revocation:
    index = family_index(families)
    inside = 0
    negatives: list[PairKey] = []
    for key, verdict in rulings.items():
        lo, hi = pair_key(*key)
        home = index.get(lo)
        if home is None or index.get(hi) != home:
            continue
        inside += 1
        if verdict == "different":
            negatives.append((lo, hi))
    return Revocation(
        families=len(families),
        members=len(index),
        operator_labelled_inside=inside,
        negatives=tuple(sorted(negatives)),
    )


def check_rows(
    rows: Iterable[Mapping[str, Any]],
    rulings: Mapping[PairKey, str],
) -> Revocation:
    return check(families_from_rows(rows), rulings)
