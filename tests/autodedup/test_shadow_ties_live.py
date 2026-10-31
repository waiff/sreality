"""The wave shadow's `ids` block, executed: `shadow.tied_ads` reads every ruling the judge reads
(`evaluate.read_rulings`) and the property's siblings, in one read-only statement. Runs in CI's
DB job (`TEST_DATABASE_URL`); every test rolls back."""

from __future__ import annotations

import uuid
from typing import Any

from autodedup import shadow
from tests._live_property import REQUIRED_DB, _advert, _property

pytestmark = REQUIRED_DB


def test_a_must_not_link_partner_and_an_operator_merge_ride_with_the_mover(cur: Any) -> None:
    """A mover whose only ruling is an operator must-not-link still brings its partner, so the
    judge sees the pair in both arms; a guard's must-not-link is no ruling and stays out."""
    pid = _property(cur)
    mover, sibling = _advert(cur, pid, source="idnes"), _advert(cur, pid, source="bazos")
    partner, guarded, merged = (_advert(cur, _property(cur), source="idnes") for _ in range(3))
    for other, source in ((partner, "operator"), (guarded, "guard")):
        lo, hi = sorted((mover, other))
        cur.execute("insert into autodedup.must_not_link (listing_lo, listing_hi, source) "
                    "values (%s, %s, %s)", (lo, hi, source))
    cur.execute("insert into autodedup.operator_merges (merge_group_id, merged_at, member_ids, "
                "member_sides, member_property_ids) values (%s, now(), %s, %s, %s)",
                (uuid.uuid4(), [mover, merged], [0, 1], [pid, pid]))
    assert shadow.tied_ads(cur.connection, [mover]) == sorted({mover, sibling, partner, merged})
