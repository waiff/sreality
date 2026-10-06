"""Carry the single-valued deal-pipeline card across a property merge (rule 22).

These are the `Pipeline` carrier's statements (`toolkit.property_carriers`), run inside the
merge transaction. `property_pipeline` is single-valued (one card per property per account), so
it is not a `CurationTable`: a plain re-point would violate the key when both the survivor and
the retired property hold a card. Per account the winning card moves as itself, TERMINAL-AWARE:
an active stage always beats a closed one, so a `lost`/`won` card never buries a live deal;
within the same terminality the higher position wins, a tie the later update. The losing card
is deleted and goes into the carry record as folded (migration 589), its snapshot the row.

Every join/exists/update between the retired and survivor sides is partitioned by
account with an EXPLICIT, NULL-tolerant predicate. That is the third shape of the
tenancy doctrine and the only place it is legal: this runs as service-role
(BYPASSRLS), so the predicate is the sole gate rather than a second definition of a
caller RLS already scopes. Doctrine + the other three shapes:
`.claude/skills/database/references/tenancy.md`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

# A carry row stands while its own `undone_at` is empty and its merge step stands: a ledger row of
# that merge retiring its from-property is not undone. Until W4 a split stamps only the ledger, so
# the second half keeps a split-off origin out. Both came-from lookups (here and the dismissal
# carrier's) read this one definition.
STANDING_CARRY = (
    "c.undone_at IS NULL AND EXISTS (SELECT 1 FROM property_merge_events e "
    "  WHERE e.merge_group_id = c.merge_group_id "
    "    AND e.retired_property_id = c.from_property_id AND e.undone_at IS NULL)"
)

# (1) the survivor's card that loses to the retired's: folded. It came from the property its
#     newest standing carry row names (an earlier step or merge moved it here), else it is the
#     survivor's own.
_FOLD_SURVIVOR_SQL = (
    "DELETE FROM property_pipeline s "
    "USING property_pipeline r, pipeline_stages ss, pipeline_stages rs "
    "WHERE s.property_id = %(s)s AND r.property_id = %(r)s "
    "  AND r.account_id IS NOT DISTINCT FROM s.account_id "
    "  AND ss.id = s.stage_id AND ss.account_id IS NOT DISTINCT FROM s.account_id "
    "  AND rs.id = r.stage_id AND rs.account_id IS NOT DISTINCT FROM r.account_id "
    "  AND ((NOT rs.is_terminal AND ss.is_terminal) "
    "       OR (rs.is_terminal = ss.is_terminal "
    "           AND (rs.position > ss.position "
    "                OR (rs.position = ss.position AND r.updated_at > s.updated_at)))) "
    "RETURNING s.account_id, s.added_at, to_jsonb(s), "
    "  (SELECT c.from_property_id FROM property_merge_carries c "
    "   WHERE c.table_name = 'property_pipeline' AND c.to_property_id = s.property_id "
    "     AND c.account_id IS NOT DISTINCT FROM s.account_id AND c.row_at = s.added_at "
    "     AND c.kind = 'moved' AND " + STANDING_CARRY +
    "   ORDER BY c.id DESC LIMIT 1)"
)

# (2) the retired's card where the survivor kept the same account's: folded.
_FOLD_RETIRED_SQL = (
    "DELETE FROM property_pipeline r "
    "WHERE r.property_id = %(r)s "
    "AND EXISTS (SELECT 1 FROM property_pipeline s "
    "  WHERE s.property_id = %(s)s AND s.account_id IS NOT DISTINCT FROM r.account_id) "
    "RETURNING r.account_id, r.added_at, to_jsonb(r)"
)

# (3) every other retired card moves as itself.
_MOVE_SQL = (
    "UPDATE property_pipeline SET property_id = %(s)s WHERE property_id = %(r)s "
    "RETURNING account_id, added_at"
)

# Every statement the carrier runs (its `sql`: the strict-cursor test and the PREPARE corpus).
STATEMENTS: tuple[str, ...] = (_FOLD_SURVIVOR_SQL, _FOLD_RETIRED_SQL, _MOVE_SQL)


def reconcile_pipeline_on_merge(
    cur: psycopg.Cursor, *, retired_id: int, survivor_id: int
) -> list[tuple[Any, datetime, int, str, dict[str, Any] | None]]:
    """Each account's winning card on the survivor, the loser folded; one (account_id,
    added_at, from_property, kind, snapshot) per card carried."""
    params = {"r": retired_id, "s": survivor_id}
    cur.execute(_FOLD_SURVIVOR_SQL, params)
    out = [(acc, at, survivor_id if came is None else int(came), "folded", snap)
           for acc, at, snap, came in cur.fetchall()]
    cur.execute(_FOLD_RETIRED_SQL, params)
    out += [(acc, at, retired_id, "folded", snap) for acc, at, snap in cur.fetchall()]
    cur.execute(_MOVE_SQL, params)
    out += [(acc, at, retired_id, "moved", None) for acc, at in cur.fetchall()]
    return out
