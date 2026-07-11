"""Carry the single-valued deal-pipeline stage across a property merge/unmerge.

`property_pipeline` is single-valued (one card per property per account), so
the generic operator-state reconciler (toolkit.operator_state, which re-points
SET/APPEND rows) cannot carry it — a plain re-point would violate the PK when
both the survivor and the retired property hold a card. These dedicated
reconcilers run in the `merge_properties` / `unmerge_group` transactions:

  - on merge: snapshot BOTH sides' pre-merge cards to the append-only
    `property_pipeline_events` ledger, then keep the MOST-ADVANCED stage on the
    survivor (TERMINAL-AWARE: an active stage always beats a closed/terminal one,
    so a `lost`/`won` card can never bury a live deal; within the same terminality
    the higher position wins, tie → later updated_at) and drop the retired card.
  - on unmerge: restore the reactivated retired property's card from its
    snapshot (lossless); in the move-if-empty case (survivor had no pre-merge
    card) drop the card the survivor absorbed so the restore isn't duplicated.

Every join/exists/update between the retired and survivor sides is partitioned
by account: this runs as service-role (BYPASSRLS), so the account predicates
must be explicit, and they use IS NOT DISTINCT FROM because pre-backfill legacy
rows carry account_id NULL (migrations 294/295).

The survivor's own stage is NOT force-restored on unmerge — that would clobber a
later merge's effect in a chained merge/unmerge. The retired side (the
reactivated property, the thing that mattered) is always lossless; a survivor
that absorbed the retired's stage in a both-cards merge keeps it until the
operator adjusts (documented best-effort).
"""

from __future__ import annotations

import psycopg


def reconcile_pipeline_on_merge(
    cur: psycopg.Cursor, *, retired_id: int, survivor_id: int, merge_group_id: str
) -> None:
    """Keep the most-advanced (terminal-aware) card on the survivor, per account; snapshot both."""
    params = {"r": retired_id, "s": survivor_id, "g": merge_group_id}

    # (0) snapshot BOTH sides' pre-merge cards so unmerge can restore losslessly.
    cur.execute(
        "INSERT INTO property_pipeline_events "
        "  (account_id, property_id, to_stage_id, reason, merge_group_id, note_snapshot) "
        "SELECT account_id, property_id, stage_id, 'merge_absorb', %(g)s, note "
        "FROM property_pipeline WHERE property_id IN (%(r)s, %(s)s)",
        params,
    )

    # (1) survivor has no card FOR THAT ACCOUNT -> move the retired card over as-is.
    cur.execute(
        "UPDATE property_pipeline SET property_id = %(s)s "
        "WHERE property_id = %(r)s "
        "AND NOT EXISTS (SELECT 1 FROM property_pipeline s2 "
        "  WHERE s2.property_id = %(s)s "
        "  AND s2.account_id IS NOT DISTINCT FROM property_pipeline.account_id)",
        params,
    )

    # (2) an account held a card on BOTH sides -> keep the most-advanced stage on
    #     the survivor. Terminal-aware: a non-terminal (live) stage beats a
    #     terminal (closed) one, so merging never buries a live deal under
    #     'lost'/'won'; within the same terminality the higher position wins
    #     (tie -> later updated_at). Both stage joins re-check the card's account.
    cur.execute(
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
        "                OR (rs.position = ss.position AND r.updated_at > s.updated_at))))",
        params,
    )

    # (3) drop the remaining retired cards (their pre-merge state is in the ledger).
    cur.execute(
        "DELETE FROM property_pipeline WHERE property_id = %(r)s", params,
    )


def reconcile_pipeline_on_unmerge(
    cur: psycopg.Cursor, *, merge_group_id: str, survivor_id: int
) -> None:
    """Restore the reactivated retired property's pipeline card from the snapshot."""
    params = {"g": merge_group_id, "s": survivor_id}

    # restore each retired (non-survivor) snapshot onto its now-active property,
    # per (account_id, property_id). Bare ON CONFLICT: no inference target, so it
    # is valid against both the (property_id) PK and 295's (account_id,
    # property_id) PK; once 295 is the only schema this can become
    # `ON CONFLICT (account_id, property_id) DO UPDATE`.
    cur.execute(
        "INSERT INTO property_pipeline (account_id, property_id, stage_id, note) "
        "SELECT e.account_id, e.property_id, e.to_stage_id, e.note_snapshot "
        "FROM property_pipeline_events e "
        "WHERE e.merge_group_id = %(g)s AND e.reason = 'merge_absorb' "
        "  AND e.property_id <> %(s)s AND e.to_stage_id IS NOT NULL "
        "  AND EXISTS (SELECT 1 FROM properties p "
        "              WHERE p.id = e.property_id AND p.status = 'active') "
        "ON CONFLICT DO NOTHING",
        params,
    )

    # move-if-empty cleanup, per account: if an account's survivor card has no
    # pre-merge snapshot (the survivor had no card for that account), drop the
    # card it absorbed so the restored retired card isn't duplicated.
    cur.execute(
        "DELETE FROM property_pipeline "
        "WHERE property_id = %(s)s "
        "  AND NOT EXISTS (SELECT 1 FROM property_pipeline_events e "
        "    WHERE e.merge_group_id = %(g)s AND e.reason = 'merge_absorb' "
        "      AND e.property_id = %(s)s "
        "      AND e.account_id IS NOT DISTINCT FROM property_pipeline.account_id)",
        params,
    )
