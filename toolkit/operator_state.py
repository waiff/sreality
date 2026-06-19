"""Carry property-anchored operator state across a property merge (rule #15).

Operator-curated state — collection memberships, tags, notes, watchdog
dispatches — is keyed on `property_id` so it describes the real-world property,
not one portal's advert, and is therefore dedup-stable. `merge_properties`
re-points the retired property's child listings onto the survivor; this module
re-points that operator state the SAME way inside the SAME transaction, so the
state follows the property and can never orphan onto the merged_away loser.

Unmerge and split are deliberately best-effort: operator state stays on the
surviving / anchor property (the reactivated or detached side starts clean), so
there is no lossy ledger to replay. Because every merge re-points here, no
operator-state row can reference a merged_away property — the invariant holds by
construction (asserted in tests).

`OPERATOR_STATE_TABLES` is the ONE place a property-anchored operator-state
table is declared; a new one becomes merge-safe by adding a single line. Shapes:
  - "set":    rows unique on (dedup_cols, property_id); union onto the survivor,
              dropping retired rows that would collide with an existing survivor row.
  - "append": journal rows with no dedup key; every row moves to the survivor.
"""

from __future__ import annotations

import psycopg

# (table, dedup_cols, shape). dedup_cols + property_id is the natural key for a
# "set" table; "append" tables have none. The names here are code-controlled
# (never user input), so f-string interpolation into the SQL is safe.
OPERATOR_STATE_TABLES: list[tuple[str, list[str], str]] = [
    ("collection_properties", ["collection_id"], "set"),
    ("property_tags", ["tag_id"], "set"),
    ("property_notes", [], "append"),
    ("notification_dispatches", ["subscription_id", "change_kind"], "set"),
]


def carry_operator_state_on_merge(
    cur: psycopg.Cursor, *, retired_id: int, survivor_id: int
) -> None:
    """Re-point every property-anchored operator-state row retired -> survivor."""
    for table, dedup_cols, shape in OPERATOR_STATE_TABLES:
        if shape == "set" and dedup_cols:
            join = " AND ".join(f"s.{c} = r.{c}" for c in dedup_cols)
            cur.execute(
                f"DELETE FROM {table} r "
                f"WHERE r.property_id = %(retired)s AND EXISTS ("
                f" SELECT 1 FROM {table} s "
                f" WHERE s.property_id = %(survivor)s AND {join})",
                {"retired": retired_id, "survivor": survivor_id},
            )
        cur.execute(
            f"UPDATE {table} SET property_id = %(survivor)s "
            f"WHERE property_id = %(retired)s",
            {"retired": retired_id, "survivor": survivor_id},
        )
