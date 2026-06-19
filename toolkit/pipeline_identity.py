"""Carry the single-valued deal-pipeline stage across a property merge.

`property_pipeline` is single-valued (PK on property_id), so the generic
operator-state reconciler (toolkit.operator_state, which re-points SET/APPEND
rows) cannot carry it — a plain re-point would violate the PK when both the
survivor and the retired property hold a card. This dedicated reconciler runs in
the same `merge_properties` transaction and resolves the conflict by keeping the
MOST-ADVANCED stage on the survivor (max stage position; tie → later updated_at),
logging the retired card to the append-only ledger first so a later phase's
unmerge can restore it. Like the curation reconciler, unmerge/split are
best-effort today (the card stays on the surviving/anchor property); the lossless
unmerge replay + terminal-aware conflict policy are a later phase.
"""

from __future__ import annotations

import psycopg


def reconcile_pipeline_on_merge(
    cur: psycopg.Cursor, *, retired_id: int, survivor_id: int, merge_group_id: str
) -> None:
    """Keep the most-advanced pipeline card on the survivor; drop the retired one."""
    params = {"r": retired_id, "s": survivor_id, "g": merge_group_id}

    # (1) survivor has no card -> simply move the retired card over.
    cur.execute(
        "UPDATE property_pipeline SET property_id = %(s)s "
        "WHERE property_id = %(r)s "
        "AND NOT EXISTS (SELECT 1 FROM property_pipeline WHERE property_id = %(s)s)",
        params,
    )

    # (2) both held a card -> log the retired card (for a future lossless unmerge),
    #     keep whichever stage is most-advanced on the survivor, then drop the loser.
    cur.execute(
        "INSERT INTO property_pipeline_events "
        "  (property_id, from_stage_id, to_stage_id, reason, merge_group_id, note_snapshot) "
        "SELECT %(r)s, r.stage_id, r.stage_id, 'merge_absorb', %(g)s, r.note "
        "FROM property_pipeline r "
        "WHERE r.property_id = %(r)s "
        "  AND EXISTS (SELECT 1 FROM property_pipeline WHERE property_id = %(s)s)",
        params,
    )
    cur.execute(
        "UPDATE property_pipeline s "
        "SET stage_id = r.stage_id, board_position = r.board_position, "
        "    note = COALESCE(s.note, r.note), entered_stage_at = r.entered_stage_at, "
        "    updated_at = now() "
        "FROM property_pipeline r, pipeline_stages ss, pipeline_stages rs "
        "WHERE s.property_id = %(s)s AND r.property_id = %(r)s "
        "  AND ss.id = s.stage_id AND rs.id = r.stage_id "
        "  AND (rs.position > ss.position "
        "       OR (rs.position = ss.position AND r.updated_at > s.updated_at))",
        params,
    )
    cur.execute(
        "DELETE FROM property_pipeline WHERE property_id = %(r)s", params,
    )
