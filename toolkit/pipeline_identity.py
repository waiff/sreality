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
    its card from that merge's snapshot (lossless: its pre-merge stage; logged as
    `unmerge_restore`), then in the move-if-empty case drop the card that followed the
    advert, wherever it now sits on the advert's undo path, unless it may carry another
    deal (`_DROP_ABSORBED_SQL`).

Every join/exists/update between the retired and survivor sides is partitioned by
account with an EXPLICIT, NULL-tolerant predicate. That is the third shape of the
tenancy doctrine and the only place it is legal: this runs as service-role
(BYPASSRLS), so the predicate is the sole gate rather than a second definition of a
caller RLS already scopes. Doctrine + the other three shapes:
`.claude/skills/database/references/tenancy.md`.

Best-effort, by design (a card records no deal identity). The survivor's own stage is NOT
force-restored on a detach — that would clobber a later merge's effect. The ledger stands in
for deal identity: the merge snapshots, the logged restores and the API's logged adds. The
drop keeps a card whenever it may carry another deal, so a deal can stay on two cards: a
survivor that held its own card at its merge keeps it, a path property merged back in after a
detach reads as another deal, and a property reactivated after the deal it carried already
went home still gets its snapshot back. A survivor with no card of its own keeps none once
every merge that brought one in is undone, in either order. The restore is the pre-merge
stage, so progress made on the merged card does not come back with it. A card written
without the API's add event (`api.pipeline.add_card`) reads as the card that followed.
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

# Restore per (account_id, property_id), logged as `unmerge_restore` of that merge: the ledger then
# shows which merged-in deals went home (`_DROP_ABSORBED_SQL`). Bare ON CONFLICT: no inference
# target, so it is valid against both the (property_id) PK and 295's (account_id, property_id) PK.
_RESTORE_SQL = (
    "WITH back AS (INSERT INTO property_pipeline (account_id, property_id, stage_id, note) "
    "  SELECT e.account_id, e.property_id, e.to_stage_id, e.note_snapshot "
    "  FROM property_pipeline_events e "
    "  WHERE e.merge_group_id = %(g)s AND e.reason = 'merge_absorb' "
    "    AND e.property_id = %(r)s AND e.to_stage_id IS NOT NULL "
    "  ON CONFLICT DO NOTHING "
    "  RETURNING account_id, property_id, stage_id, note) "
    "INSERT INTO property_pipeline_events "
    "  (account_id, property_id, to_stage_id, reason, merge_group_id, note_snapshot) "
    "SELECT account_id, property_id, stage_id, 'unmerge_restore', %(g)s, note FROM back"
)

# Move-if-empty, per account, along the advert's undo path (`hop`, oldest first: each undone
# merge's group `g`, survivor `s`, retired `p`, and `at`, the survivor's first snapshot there).
# The card that followed the advert sits on the first path survivor that is active: one a
# detach has since reactivated got it back from its own snapshot, else it is the property the
# advert left. The restored card replaces it, so it is dropped there unless none followed or it
# may carry another deal. `inn` is every card merged into a path survivor: `fill` when the
# survivor held none before that merge began, `home` when the property it came from got a card
# back since (`unmerge_restore`) and nothing card-holding merged into that property after the
# snapshot that came back still stands there. Kept when:
#   - a path property up to the one that brought it there carried no card into its merge;
#   - a card was put there since it arrived: the operator added one (the API logs every add), or
#     a merge filled it with a card not gone home;
#   - a survivor up to there held a card before its merge began: snapshotted at every pair step
#     of its group (a set merge snapshots its survivor again at each later step, once it holds
#     the card it took in), so its snapshots number the group's retired properties. Not when a
#     merge had filled it, nothing was added since, and every card merged in from that fill on
#     went home;
#   - another card-holding property still stands merged into one of them (still `merged_away`
#     into it, by a ledger row not undone; not one of the path's own hops), in that same merge
#     or since the card arrived (by event id: one transaction shares one now()).
_DROP_ABSORBED_SQL = (
    "WITH hop AS (SELECT h.k, h.g, h.s, h.p, (SELECT min(e.id) FROM property_pipeline_events e "
    "    WHERE e.merge_group_id = h.g AND e.reason = 'merge_absorb' AND e.property_id = h.s) AS at "
    "  FROM unnest(%(groups)s::uuid[], %(path)s::bigint[], %(prevs)s::bigint[]) "
    "    WITH ORDINALITY AS h(g, s, p, k)), "
    "here AS (SELECT hop.k, hop.s FROM hop JOIN properties pr ON pr.id = hop.s "
    "  WHERE pr.status = 'active' ORDER BY hop.k LIMIT 1), "
    "side AS (SELECT h.k, h.g AS hg, o.id, o.account_id, o.merge_group_id AS og "
    "  FROM hop h JOIN properties d ON d.merged_into = h.s AND d.status = 'merged_away' "
    "  JOIN property_pipeline_events o ON o.property_id = d.id "
    "    AND o.reason = 'merge_absorb' AND o.to_stage_id IS NOT NULL "
    "  WHERE NOT EXISTS (SELECT 1 FROM hop c WHERE c.g = o.merge_group_id AND c.p = d.id) "
    "    AND EXISTS (SELECT 1 FROM property_merge_events m "
    "      WHERE m.merge_group_id = o.merge_group_id AND m.retired_property_id = d.id "
    "        AND m.survivor_property_id = h.s AND m.undone_at IS NULL)), "
    "inn AS (SELECT DISTINCT m.survivor_property_id AS s, c.id, c.account_id, "
    "    c.merge_group_id AS g, "
    "    (SELECT count(*) FROM property_pipeline_events e "
    "     WHERE e.merge_group_id = c.merge_group_id AND e.reason = 'merge_absorb' "
    "       AND e.property_id = m.survivor_property_id "
    "       AND e.account_id IS NOT DISTINCT FROM c.account_id) "
    "    < (SELECT count(DISTINCT m2.retired_property_id) FROM property_merge_events m2 "
    "       WHERE m2.merge_group_id = c.merge_group_id) AS fill, "
    "    EXISTS (SELECT 1 FROM property_pipeline_events b "
    "      WHERE b.property_id = c.property_id AND b.reason = 'unmerge_restore' "
    "        AND b.id > c.id AND b.account_id IS NOT DISTINCT FROM c.account_id "
    "        AND NOT EXISTS (SELECT 1 FROM properties d JOIN property_pipeline_events o "
    "          ON o.property_id = d.id AND o.reason = 'merge_absorb' "
    "          AND o.to_stage_id IS NOT NULL "
    "          AND o.account_id IS NOT DISTINCT FROM c.account_id "
    "          WHERE d.merged_into = c.property_id AND d.status = 'merged_away' "
    "            AND o.id > (SELECT min(e.id) FROM property_pipeline_events e "
    "              WHERE e.merge_group_id = b.merge_group_id AND e.reason = 'merge_absorb' "
    "                AND e.property_id = c.property_id) "
    "            AND EXISTS (SELECT 1 FROM property_merge_events m3 "
    "              WHERE m3.merge_group_id = o.merge_group_id AND m3.retired_property_id = d.id "
    "                AND m3.survivor_property_id = c.property_id "
    "                AND m3.undone_at IS NULL))) AS home "
    "  FROM property_merge_events m JOIN property_pipeline_events c "
    "    ON c.merge_group_id = m.merge_group_id AND c.property_id = m.retired_property_id "
    "    AND c.reason = 'merge_absorb' AND c.to_stage_id IS NOT NULL "
    "  WHERE m.survivor_property_id IN (SELECT hop.s FROM hop)), "
    "added AS (SELECT a.property_id AS s, a.id, a.account_id FROM property_pipeline_events a "
    "  WHERE a.property_id IN (SELECT hop.s FROM hop) AND a.reason = 'operator' "
    "    AND a.from_stage_id IS NULL AND a.to_stage_id IS NOT NULL) "
    "DELETE FROM property_pipeline pp USING here t "
    "WHERE pp.property_id = t.s AND t.s <> %(r)s "
    "  AND NOT EXISTS (SELECT 1 FROM hop n WHERE n.k <= t.k + 1 "
    "    AND NOT EXISTS (SELECT 1 FROM property_pipeline_events e "
    "      WHERE e.merge_group_id = n.g AND e.reason = 'merge_absorb' "
    "        AND e.property_id = n.p AND e.to_stage_id IS NOT NULL "
    "        AND e.account_id IS NOT DISTINCT FROM pp.account_id)) "
    "  AND NOT EXISTS (SELECT 1 FROM hop n JOIN property_pipeline_events e "
    "      ON e.merge_group_id = n.g AND e.reason = 'merge_absorb' AND e.property_id = n.p "
    "      AND e.account_id IS NOT DISTINCT FROM pp.account_id "
    "    WHERE n.k = least(t.k + 1, (SELECT max(x.k) FROM hop x)) "
    "      AND (EXISTS (SELECT 1 FROM added a WHERE a.s = t.s AND a.id > e.id "
    "          AND a.account_id IS NOT DISTINCT FROM pp.account_id) "
    "        OR EXISTS (SELECT 1 FROM inn i WHERE i.s = t.s AND i.fill AND NOT i.home "
    "          AND i.id > e.id AND i.g <> e.merge_group_id "
    "          AND i.account_id IS NOT DISTINCT FROM pp.account_id))) "
    "  AND NOT EXISTS (SELECT 1 FROM hop h WHERE h.k <= t.k "
    "    AND (SELECT count(*) FROM property_pipeline_events e "
    "         WHERE e.merge_group_id = h.g AND e.reason = 'merge_absorb' "
    "           AND e.property_id = h.s "
    "           AND e.account_id IS NOT DISTINCT FROM pp.account_id) "
    "     >= (SELECT count(DISTINCT m.retired_property_id) FROM property_merge_events m "
    "         WHERE m.merge_group_id = h.g) "
    "    AND NOT EXISTS (SELECT 1 FROM inn f WHERE f.s = h.s AND f.fill AND f.id < h.at "
    "      AND f.account_id IS NOT DISTINCT FROM pp.account_id "
    "      AND NOT EXISTS (SELECT 1 FROM added a WHERE a.s = h.s AND a.id > f.id "
    "        AND a.account_id IS NOT DISTINCT FROM pp.account_id) "
    "      AND NOT EXISTS (SELECT 1 FROM inn c WHERE c.s = h.s AND NOT c.home "
    "        AND (c.g = f.g OR c.id > f.id) AND c.id < h.at "
    "        AND c.account_id IS NOT DISTINCT FROM pp.account_id))) "
    "  AND NOT EXISTS (SELECT 1 FROM side w WHERE w.k <= t.k "
    "    AND w.account_id IS NOT DISTINCT FROM pp.account_id "
    "    AND (w.og = w.hg "
    "      OR w.id > (SELECT min(e0.id) FROM property_pipeline_events e0 "
    "        WHERE e0.merge_group_id = %(g)s AND e0.reason = 'merge_absorb' "
    "          AND e0.property_id = %(r)s)))"
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
