"""Carry the single-valued deal-pipeline stage across a property merge and a detach.

These are the `Pipeline` carrier's implementation (`toolkit.property_carriers`), run inside
the merge/detach transactions. `property_pipeline` is single-valued (one card per property
per account), so it is not a `CurationTable` — a plain re-point would violate the PK when
both the survivor and the retired property hold a card:

  - on merge: snapshot BOTH sides' pre-merge cards to the append-only
    `property_pipeline_events` ledger, then keep the MOST-ADVANCED stage on the
    survivor (TERMINAL-AWARE: an active stage always beats a closed/terminal one,
    so a `lost`/`won` card can never bury a live deal; within the same terminality
    the higher position wins, tie → later updated_at) and drop the retired card.
  - on detach: when an advert's return reactivates the retired property, restore
    its card from that merge's snapshot (lossless); in the move-if-empty case
    (the survivor had no card of its own in that merge) drop the card the
    survivor absorbed so the restore isn't duplicated.

Every join/exists/update between the retired and survivor sides is partitioned by
account with an EXPLICIT, NULL-tolerant predicate. That is the third shape of the
tenancy doctrine and the only place it is legal: this runs as service-role
(BYPASSRLS), so the predicate is the sole gate rather than a second definition of a
caller RLS already scopes. Doctrine + the other three shapes:
`.claude/skills/database/references/tenancy.md`.

The survivor's own stage is NOT force-restored on a detach — that would clobber a
later merge's effect in a chained merge/detach. The retired side (the
reactivated property, the thing that mattered) is always lossless; a survivor
that absorbed the retired's stage in a both-cards merge keeps it until the
operator adjusts (documented best-effort).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

import psycopg

if TYPE_CHECKING:  # property_carriers imports this module at runtime
    from toolkit.property_carriers import Hop

# (0) snapshot BOTH sides' pre-merge cards so a detach can restore losslessly.
_SNAPSHOT_SQL = (
    "INSERT INTO property_pipeline_events "
    "  (account_id, property_id, to_stage_id, reason, merge_group_id) "
    "SELECT account_id, property_id, stage_id, 'merge_absorb', %(g)s "
    "FROM property_pipeline WHERE property_id IN (%(r)s, %(s)s)"
)

# (1) survivor has no card FOR THAT ACCOUNT -> move the retired card over as-is.
_MOVE_IF_EMPTY_SQL = (
    "UPDATE property_pipeline SET property_id = %(s)s "
    "WHERE property_id = %(r)s "
    "AND NOT EXISTS (SELECT 1 FROM property_pipeline s2 "
    "  WHERE s2.property_id = %(s)s "
    "  AND s2.account_id IS NOT DISTINCT FROM property_pipeline.account_id)"
)

# (2) an account held a card on BOTH sides -> keep the most-advanced stage on
#     the survivor. Terminal-aware: a non-terminal (live) stage beats a
#     terminal (closed) one, so merging never buries a live deal under
#     'lost'/'won'; within the same terminality the higher position wins
#     (tie -> later updated_at). Both stage joins re-check the card's account.
_KEEP_MOST_ADVANCED_SQL = (
    "UPDATE property_pipeline s "
    "SET stage_id = r.stage_id, board_position = r.board_position, "
    "    entered_stage_at = r.entered_stage_at, "
    "    updated_at = now() "
    "FROM property_pipeline r, pipeline_stages ss, pipeline_stages rs "
    "WHERE s.property_id = %(s)s AND r.property_id = %(r)s "
    "  AND r.account_id IS NOT DISTINCT FROM s.account_id "
    "  AND ss.id = s.stage_id AND ss.account_id IS NOT DISTINCT FROM s.account_id "
    "  AND rs.id = r.stage_id AND rs.account_id IS NOT DISTINCT FROM r.account_id "
    "  AND ((NOT rs.is_terminal AND ss.is_terminal) "
    "       OR (rs.is_terminal = ss.is_terminal "
    "           AND (rs.position > ss.position "
    "                OR (rs.position = ss.position AND r.updated_at > s.updated_at))))"
)

# (3) drop the remaining retired cards (their pre-merge state is in the ledger).
_DROP_RETIRED_SQL = "DELETE FROM property_pipeline WHERE property_id = %(r)s"

# Restore per (account_id, property_id). Bare ON CONFLICT: no inference target, so it is
# valid against both the (property_id) PK and 295's (account_id, property_id) PK.
_RESTORE_SQL = (
    "INSERT INTO property_pipeline (account_id, property_id, stage_id) "
    "SELECT e.account_id, e.property_id, e.to_stage_id "
    "FROM property_pipeline_events e "
    "WHERE e.merge_group_id = %(g)s AND e.reason = 'merge_absorb' "
    "  AND e.property_id = %(r)s AND e.to_stage_id IS NOT NULL "
    "ON CONFLICT DO NOTHING"
)

# Move-if-empty, per account: a survivor that held no card of its own in that merge got
# this property's card; drop it there so the restored card isn't duplicated.
_DROP_ABSORBED_SQL = (
    "DELETE FROM property_pipeline "
    "WHERE property_id = %(s)s "
    "  AND EXISTS (SELECT 1 FROM property_pipeline_events e "
    "    WHERE e.merge_group_id = %(g)s AND e.reason = 'merge_absorb' "
    "      AND e.property_id = %(r)s AND e.to_stage_id IS NOT NULL "
    "      AND e.account_id IS NOT DISTINCT FROM property_pipeline.account_id) "
    "  AND NOT EXISTS (SELECT 1 FROM property_pipeline_events e "
    "    WHERE e.merge_group_id = %(g)s AND e.reason = 'merge_absorb' "
    "      AND e.property_id = %(s)s "
    "      AND e.account_id IS NOT DISTINCT FROM property_pipeline.account_id)"
)

# Every statement the carrier can run (its `sql`: the strict-cursor test and the PREPARE corpus).
STATEMENTS: tuple[str, ...] = (
    _SNAPSHOT_SQL, _MOVE_IF_EMPTY_SQL, _KEEP_MOST_ADVANCED_SQL, _DROP_RETIRED_SQL,
    _RESTORE_SQL, _DROP_ABSORBED_SQL,
)


def reconcile_pipeline_on_merge(
    cur: psycopg.Cursor, *, retired_id: int, survivor_id: int, merge_group_id: str
) -> None:
    """Keep the most-advanced (terminal-aware) card on the survivor, per account; snapshot both."""
    params = {"r": retired_id, "s": survivor_id, "g": merge_group_id}
    for statement in (_SNAPSHOT_SQL, _MOVE_IF_EMPTY_SQL, _KEEP_MOST_ADVANCED_SQL,
                      _DROP_RETIRED_SQL):
        cur.execute(statement, params)


def reconcile_pipeline_on_detach(
    cur: psycopg.Cursor, *, restored_id: int, left_id: int, undo: Sequence[Hop]
) -> None:
    """Give a reactivated property its card back from the snapshot of the merge that retired it
    (`undo[0]`, the oldest hop the detach undoes); the absorbed card comes off the property the
    advert left only when that merge's survivor is that property."""
    g, s = undo[0].group, (left_id if left_id == undo[0].survivor else None)
    params = {"g": g, "r": restored_id, "s": s}
    cur.execute(_RESTORE_SQL, params)
    if s is None:
        return
    cur.execute(_DROP_ABSORBED_SQL, params)
