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
    its card from that merge's snapshot (lossless: its pre-merge stage), then in the
    move-if-empty case drop the card that followed the advert, wherever it now sits
    on the advert's undo path, unless it may carry another deal (`_DROP_ABSORBED_SQL`).

Every join/exists/update between the retired and survivor sides is partitioned by
account with an EXPLICIT, NULL-tolerant predicate. That is the third shape of the
tenancy doctrine and the only place it is legal: this runs as service-role
(BYPASSRLS), so the predicate is the sole gate rather than a second definition of a
caller RLS already scopes. Doctrine + the other three shapes:
`.claude/skills/database/references/tenancy.md`.

Best-effort, by design (a card records no deal identity). The survivor's own stage is NOT
force-restored on a detach — that would clobber a later merge's effect. The drop keeps a card
whenever it may carry another deal, so a deal can stay on two cards: a survivor that held a
card at its merge (its own, or one an earlier merge brought in) keeps it, a path property
merged back in after a detach reads as another deal, and a property reactivated after the
deal it carried already went home still gets its snapshot back. The restore is the pre-merge
stage, so progress made on the merged card does not come back with it; and a card the
operator adds where the one that followed used to sit reads as that card.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

import psycopg

if TYPE_CHECKING:  # property_carriers imports this module at runtime
    from toolkit.property_carriers import Hop

# (0) snapshot BOTH sides' pre-merge cards so a detach can restore losslessly.
_SNAPSHOT_SQL = (
    "INSERT INTO property_pipeline_events "
    "  (account_id, property_id, to_stage_id, reason, merge_group_id, note_snapshot) "
    "SELECT account_id, property_id, stage_id, 'merge_absorb', %(g)s, note "
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
    "    note = COALESCE(s.note, r.note), entered_stage_at = r.entered_stage_at, "
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
    "INSERT INTO property_pipeline (account_id, property_id, stage_id, note) "
    "SELECT e.account_id, e.property_id, e.to_stage_id, e.note_snapshot "
    "FROM property_pipeline_events e "
    "WHERE e.merge_group_id = %(g)s AND e.reason = 'merge_absorb' "
    "  AND e.property_id = %(r)s AND e.to_stage_id IS NOT NULL "
    "ON CONFLICT DO NOTHING"
)

# Move-if-empty, per account, along the advert's undo path (`hop`, oldest first: each undone
# merge's group `g`, survivor `s` and retired `p`). The card that followed the advert sits on the
# first path survivor that is active: one a detach has since reactivated got it back from its own
# snapshot, else it is the property the advert left. The restored card replaces it, so it is
# dropped there, unless it may carry another deal or none followed:
#   - the restored property held no card at the merge that retired it, or that reactivated
#     survivor carried none into its next merge;
#   - a survivor up to there held a card before its merge began: snapshotted at every pair step
#     of its group (a set merge snapshots its survivor again at each later step, once it holds
#     the card it took in), so its snapshots number the group's retired properties;
#   - another card-holding property still stands merged into one of them (still `merged_away`
#     into it, by a ledger row not undone; not one of the path's own hops), in that same merge
#     or since the card arrived (by event id: one transaction shares one now()).
_DROP_ABSORBED_SQL = (
    "WITH hop AS (SELECT h.k, h.g, h.s, h.p "
    "  FROM unnest(%(groups)s::uuid[], %(path)s::bigint[], %(prevs)s::bigint[]) "
    "    WITH ORDINALITY AS h(g, s, p, k)), "
    "here AS (SELECT hop.k, hop.s FROM hop JOIN properties pr ON pr.id = hop.s "
    "  WHERE pr.status = 'active' ORDER BY hop.k LIMIT 1) "
    "DELETE FROM property_pipeline pp USING here t "
    "WHERE pp.property_id = t.s AND t.s <> %(r)s "
    "  AND EXISTS (SELECT 1 FROM property_pipeline_events e "
    "    WHERE e.merge_group_id = %(g)s AND e.reason = 'merge_absorb' "
    "      AND e.property_id = %(r)s AND e.to_stage_id IS NOT NULL "
    "      AND e.account_id IS NOT DISTINCT FROM pp.account_id) "
    "  AND NOT EXISTS (SELECT 1 FROM hop n WHERE n.k = t.k + 1 "
    "    AND NOT EXISTS (SELECT 1 FROM property_pipeline_events e "
    "      WHERE e.merge_group_id = n.g AND e.reason = 'merge_absorb' "
    "        AND e.property_id = n.p AND e.to_stage_id IS NOT NULL "
    "        AND e.account_id IS NOT DISTINCT FROM pp.account_id)) "
    "  AND NOT EXISTS (SELECT 1 FROM hop h WHERE h.k <= t.k "
    "    AND (SELECT count(*) FROM property_pipeline_events e "
    "         WHERE e.merge_group_id = h.g AND e.reason = 'merge_absorb' "
    "           AND e.property_id = h.s "
    "           AND e.account_id IS NOT DISTINCT FROM pp.account_id) "
    "     >= (SELECT count(DISTINCT m.retired_property_id) FROM property_merge_events m "
    "         WHERE m.merge_group_id = h.g)) "
    "  AND NOT EXISTS (SELECT 1 FROM property_pipeline_events o "
    "    JOIN properties d ON d.id = o.property_id AND d.status = 'merged_away' "
    "    JOIN hop h ON h.s = d.merged_into AND h.k <= t.k "
    "    WHERE o.reason = 'merge_absorb' AND o.to_stage_id IS NOT NULL "
    "      AND o.account_id IS NOT DISTINCT FROM pp.account_id "
    "      AND NOT EXISTS (SELECT 1 FROM hop c "
    "        WHERE c.g = o.merge_group_id AND c.p = o.property_id) "
    "      AND (o.merge_group_id = h.g "
    "        OR o.id > (SELECT min(e0.id) FROM property_pipeline_events e0 "
    "          WHERE e0.merge_group_id = %(g)s AND e0.reason = 'merge_absorb' "
    "            AND e0.property_id = %(r)s)) "
    "      AND EXISTS (SELECT 1 FROM property_merge_events m "
    "        WHERE m.merge_group_id = o.merge_group_id "
    "          AND m.retired_property_id = o.property_id "
    "          AND m.survivor_property_id = h.s AND m.undone_at IS NULL))"
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
    cur: psycopg.Cursor, *, restored_id: int, undo: Sequence[Hop]
) -> None:
    """Give a reactivated property its card back from the snapshot of the merge that retired it
    (`undo[0]`, the oldest hop the detach undoes), then drop the card that followed its advert
    where it sits now unless it may carry another deal (`_DROP_ABSORBED_SQL`), per account."""
    params = {"g": undo[0].group, "r": restored_id,
              "groups": [h.group for h in undo], "path": [h.survivor for h in undo],
              "prevs": [h.prev for h in undo]}
    cur.execute(_RESTORE_SQL, params)
    cur.execute(_DROP_ABSORBED_SQL, params)
