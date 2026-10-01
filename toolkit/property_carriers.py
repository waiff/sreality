"""THE ordered list of everything that follows a property across a merge and a detach (rules
15, 18, 22): `PROPERTY_CARRIERS`, the seam `toolkit.property_identity` walks, and `NOT_CARRIED`,
the written reason for every other column that names a property. A census enforces the two
together, offline over `migrations/*.sql` (tests/test_property_carriers.py) and live over the
replayed schema (tests/test_property_carriers_live.py): a new column naming a property fails
until it is carried or named here.

Carrier invariants:
  - A carrier runs on the writer's service-role cursor, inside the writer's transaction; it
    opens no transaction, commits nothing and raises nothing of its own, so any psycopg error
    aborts the whole merge or detach.
  - Every cross-side predicate names its account explicitly (tenancy shape 3,
    `.claude/skills/database/references/tenancy.md`): the service role bypasses RLS.
  - It never deletes history. The one sanctioned delete is a SET table's collision collapse.
  - `on_merge` runs once per retired property, after the merge ledger row and the advert
    re-point and before the retire; `on_detach` only when a detach reactivated `restored`,
    over the list REVERSED (undo in the reverse of the carry).

The hard ordering: the ledger INSERT comes before the re-point (it selects the adverts the
re-point then moves; that lives in `property_identity._merge_pair`); Pipeline before Dismissals
(the dismissal lift reads the live card the pipeline carry just placed); every carrier before
the retire, which under migration 559 is the last write. The rest write disjoint tables.

Adding one: a SET or APPEND table keyed on `property_id` is one `CurationTable(...)` line before
`Pipeline()`; any other shape is one class meeting `Carrier`; a column that must NOT follow a
merge is one `NOT_CARRIED` line with its reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NamedTuple, Protocol, runtime_checkable

import psycopg

from toolkit import pipeline_identity

# "auto" = the removed legacy engine (historic rows only); "autodedup" = migration 558.
MergeSource = Literal["auto", "operator", "autodedup"]


class Hop(NamedTuple):
    """One live ledger row a detach undoes, oldest first."""

    event_id: int
    group: str
    survivor: int
    prev: int


@dataclass(frozen=True)
class MergeStep:
    survivor: int
    retired: int
    group: str
    source: MergeSource


@dataclass(frozen=True)
class DetachStep:
    restored: int  # the property the detach just reactivated
    left: int  # the property the advert left
    undo: tuple[Hop, ...]
    source: MergeSource


@runtime_checkable
class Carrier(Protocol):
    name: str
    columns: tuple[tuple[str, str], ...]  # (table, column) it keeps true across merge/detach
    sql: tuple[str, ...]  # every statement it can execute

    def on_merge(self, cur: psycopg.Cursor, step: MergeStep) -> None: ...

    def on_detach(self, cur: psycopg.Cursor, step: DetachStep) -> None: ...


def _ledger_source(source: MergeSource) -> str:
    # asset_membership_events.source's CHECK (migration 224) allows only these two.
    return "operator" if source == "operator" else "auto"


# Decision 17: the one asset link moves onto the survivor, never left on a merged_away row; each
# membership change is logged as `toolkit.asset_identity` logs a link or an unlink, the reason
# naming the merge group so a detach can give the link back (`_RESTORE_ASSET_LINK_SQL`).
_CARRY_ASSET_LINK_SQL = """
WITH moved AS (
    UPDATE properties
    SET asset_id = CASE WHEN id = %(survivor)s::bigint THEN %(asset)s::bigint END
    WHERE id IN (%(survivor)s::bigint, %(retired)s::bigint)
      AND asset_id IS DISTINCT FROM
          CASE WHEN id = %(survivor)s::bigint THEN %(asset)s::bigint END
    RETURNING id, asset_id
)
INSERT INTO asset_membership_events (asset_id, property_id, action, reason, source)
SELECT %(asset)s::bigint, m.id,
       CASE WHEN m.asset_id IS NULL THEN 'unlinked' ELSE 'linked' END,
       %(reason)s::text, %(source)s::text
FROM moved m
"""

# The carry's inverse, when a detach reactivates the property a merge unlinked: the link it lost
# (unless its asset dissolved since) comes back while a survivor on the advert's path (`path`, the
# merges it undoes: `merges`) still holds it, and leaves each such survivor one of those merges
# linked it to — so detaches in any order give every link back to where it was.
_RESTORE_ASSET_LINK_SQL = """
WITH carried AS (
    SELECT e.asset_id FROM asset_membership_events e
    JOIN assets a ON a.id = e.asset_id AND a.status = 'active'
    WHERE e.property_id = %(restored)s::bigint AND e.action = 'unlinked'
      AND e.reason = %(merge)s::text
    ORDER BY e.id DESC LIMIT 1
), holders AS (
    SELECT p.id, EXISTS (
             SELECT 1 FROM asset_membership_events s
             WHERE s.property_id = p.id AND s.asset_id = p.asset_id AND s.action = 'linked'
               AND s.reason = ANY(%(merges)s::text[])) AS carried_here
    FROM properties p JOIN carried c ON p.asset_id = c.asset_id
    WHERE p.id = ANY(%(path)s::bigint[])
), moved AS (
    UPDATE properties p
    SET asset_id = CASE WHEN p.id = %(restored)s::bigint THEN c.asset_id END
    FROM carried c
    WHERE EXISTS (SELECT 1 FROM holders)
      AND ((p.id = %(restored)s::bigint AND p.asset_id IS NULL)
           OR p.id IN (SELECT h.id FROM holders h WHERE h.carried_here))
    RETURNING p.id, p.asset_id, c.asset_id AS carried
)
INSERT INTO asset_membership_events (asset_id, property_id, action, reason, source)
SELECT m.carried, m.id, CASE WHEN m.asset_id IS NULL THEN 'unlinked' ELSE 'linked' END,
       %(detach)s::text, %(source)s::text
FROM moved m
"""


class AssetLink:
    """Decision 17's one link per property: carried onto the survivor, given back (chain-aware)
    when a detach reactivates the property the merge unlinked."""

    name = "asset_link"
    columns = (("properties", "asset_id"), ("asset_membership_events", "property_id"))
    # The row is already locked by the merge's set lock.
    RETIRED_ASSET_SQL = "SELECT asset_id FROM properties WHERE id = %(retired)s"
    sql = (RETIRED_ASSET_SQL, _CARRY_ASSET_LINK_SQL, _RESTORE_ASSET_LINK_SQL)

    def on_merge(self, cur: psycopg.Cursor, step: MergeStep) -> None:
        cur.execute(self.RETIRED_ASSET_SQL, {"retired": step.retired})
        row = cur.fetchone()
        if row is None or row[0] is None:
            return
        cur.execute(_CARRY_ASSET_LINK_SQL, {
            "survivor": step.survivor, "retired": step.retired, "asset": int(row[0]),
            "reason": f"merge {step.group}", "source": _ledger_source(step.source),
        })

    def on_detach(self, cur: psycopg.Cursor, step: DetachStep) -> None:
        first = step.undo[0]
        cur.execute(_RESTORE_ASSET_LINK_SQL, {
            "restored": step.restored, "merge": f"merge {first.group}",
            "merges": [f"merge {h.group}" for h in step.undo],
            "path": [h.survivor for h in step.undo], "detach": f"detach {first.group}",
            "source": _ledger_source(step.source),
        })


class CurationTable:
    """A property-anchored SET table (`keys` given: rows unique on keys + property_id; union
    onto the survivor, collapsing a retired row whose twin the survivor holds, NULL-safe) or
    APPEND table (no keys: every row moves). The table name is code-controlled, never input."""

    def __init__(self, table: str, keys: tuple[str, ...] = ()) -> None:
        self.name = table
        self.keys = keys
        self.columns: tuple[tuple[str, str], ...] = ((table, "property_id"),)
        move = (f"UPDATE {table} SET property_id = %(survivor)s "
                f"WHERE property_id = %(retired)s")
        if not keys:
            self.sql: tuple[str, ...] = (move,)
            return
        # NULL-safe: notification_dispatches' keys are nullable, and a NULL must collapse
        # against a NULL, which plain `=` never does.
        join = " AND ".join(f"s.{c} IS NOT DISTINCT FROM r.{c}" for c in keys)
        collapse = (f"DELETE FROM {table} r "
                    f"WHERE r.property_id = %(retired)s AND EXISTS ("
                    f" SELECT 1 FROM {table} s "
                    f" WHERE s.property_id = %(survivor)s AND {join})")
        self.sql = (collapse, move)

    def on_merge(self, cur: psycopg.Cursor, step: MergeStep) -> None:
        params = {"retired": step.retired, "survivor": step.survivor}
        for statement in self.sql:
            cur.execute(statement, params)

    def on_detach(self, cur: psycopg.Cursor, step: DetachStep) -> None:
        """Nothing: the rows stay on the property the advert left (rule 18, best-effort)."""


class Pipeline:
    """Rule 22's single-valued card, terminal-aware; implemented in `toolkit.pipeline_identity`."""

    name = "pipeline"
    columns = (("property_pipeline", "property_id"), ("property_pipeline_events", "property_id"))
    sql = pipeline_identity.STATEMENTS

    def on_merge(self, cur: psycopg.Cursor, step: MergeStep) -> None:
        pipeline_identity.reconcile_pipeline_on_merge(
            cur, retired_id=step.retired, survivor_id=step.survivor, merge_group_id=step.group)

    def on_detach(self, cur: psycopg.Cursor, step: DetachStep) -> None:
        pipeline_identity.reconcile_pipeline_on_detach(
            cur, restored_id=step.restored, left_id=step.left, undo=step.undo)


# One active row per (property, account): where both sides hold one, the survivor's stands and
# the retired's is lifted, not deleted.
_DISMISSAL_LIFT_DUPLICATE_SQL = (
    "UPDATE property_dismissals r SET lifted_at = now(), lift_reason = 'merge' "
    "WHERE r.property_id = %(r)s AND r.lifted_at IS NULL "
    "AND EXISTS (SELECT 1 FROM property_dismissals s "
    "  WHERE s.property_id = %(s)s AND s.account_id = r.account_id "
    "  AND s.lifted_at IS NULL)"
)
_DISMISSAL_REPOINT_SQL = (
    "UPDATE property_dismissals SET property_id = %(s)s WHERE property_id = %(r)s"
)
# A LIVE deal and a dismissal never coexist for one account, whichever side each fact came from;
# a card closed into a terminal stage keeps it (the rule `api.dismissals` states on the tenant
# connection, here per account).
_DISMISSAL_LIFT_LIVE_DEAL_SQL = (
    "UPDATE property_dismissals d SET lifted_at = now(), lift_reason = 'pipeline' "
    "WHERE d.property_id = %(s)s AND d.lifted_at IS NULL "
    "AND EXISTS (SELECT 1 FROM property_pipeline pp "
    "  JOIN pipeline_stages ps ON ps.id = pp.stage_id "
    "  WHERE pp.property_id = d.property_id AND pp.account_id = d.account_id "
    "  AND NOT ps.is_terminal)"
)


class Dismissals:
    """Migration 536's append-only dismissals: the survivor inherits them from either side, a
    colliding active row is lifted (`merge`), never deleted — so not a `CurationTable`."""

    name = "dismissals"
    columns = (("property_dismissals", "property_id"),)
    sql = (_DISMISSAL_LIFT_DUPLICATE_SQL, _DISMISSAL_REPOINT_SQL, _DISMISSAL_LIFT_LIVE_DEAL_SQL)

    def on_merge(self, cur: psycopg.Cursor, step: MergeStep) -> None:
        params = {"r": step.retired, "s": step.survivor}
        for statement in self.sql:
            cur.execute(statement, params)

    def on_detach(self, cur: psycopg.Cursor, step: DetachStep) -> None:
        """Nothing: the rows stay on the property the advert left, their history intact."""


PROPERTY_CARRIERS: tuple[Carrier, ...] = (
    AssetLink(),
    CurationTable("collection_properties", ("collection_id",)),
    CurationTable("property_tags", ("tag_id",)),
    CurationTable("property_notes"),
    # The unified event table (migration 206): identity spans both producers (subscription_id
    # XOR collection_id) and the per-snapshot grain (trigger_snapshot_id, NULL for 'new').
    CurationTable("notification_dispatches",
                  ("subscription_id", "collection_id", "change_kind", "trigger_snapshot_id")),
    Pipeline(),
    Dismissals(),
)

_ENGINE_HISTORY = "history of the removed decision engine, never consulted (rule 15)"
_AUTODEDUP_HISTORY = "an engine or operator ledger: history (D7)"
_MERGE_LEDGER = "the chokepoint's own ledger: history, replayed by a detach"

NOT_CARRIED: dict[tuple[str, str], str] = {
    ("listings", "property_id"):
        "the identity link itself: the writers move it (merge re-point, detach move)",
    ("properties", "merged_into"):
        "the merge's own pointer: set by the retire, cleared by the reactivation",
    ("property_merge_events", "survivor_property_id"): _MERGE_LEDGER,
    ("property_merge_events", "retired_property_id"): _MERGE_LEDGER,
    ("property_merge_events", "prev_property_id"): _MERGE_LEDGER,
    ("property_status_events", "property_id"):
        "each property's own activity log (migration 559): a survivor holding two logs charts "
        "false gaps",
    ("dirty_properties", "property_id"):
        "a work queue: a retired id left queued recomputes to nothing",
    ("tag_candidates", "property_id"): "a draw-time snapshot (migration 450)",
    ("backup_464_tag_candidates", "property_id"):
        "migration 464's frozen copy of tag_candidates: a backup, never read",
    ("browse_list", "property_id"):
        "a read model rebuilt wholesale (migration 276); the writers patch it per property",
    ("autodedup.clusters", "property_id"): _AUTODEDUP_HISTORY,
    ("autodedup.merges", "survivor_property_id"): _AUTODEDUP_HISTORY,
    ("autodedup.merges", "retired_property_id"): _AUTODEDUP_HISTORY,
    ("autodedup.applied_merges", "survivor_property_id"): _AUTODEDUP_HISTORY,
    ("autodedup.applied_merges", "retired_property_id"): _AUTODEDUP_HISTORY,
    ("autodedup.operator_merges", "survivor_property_id"): _AUTODEDUP_HISTORY,
    ("autodedup.operator_merges", "retired_property_ids"): _AUTODEDUP_HISTORY,
    ("autodedup.operator_merges", "member_property_ids"): _AUTODEDUP_HISTORY,
    **{(table, f"{side}_property_id"): _ENGINE_HISTORY
       for table in ("dedup_pair_audit", "dedup_decision_feedback", "dedup_golden_sets",
                     "dedup_model_compare_sets")
       for side in ("left", "right")},
}

# Every carrier's statements, deduped: the PREPARE sweep's corpus reads it (tests/sql_corpus.py;
# a plain assignment, which is how its AST pass knows to import this module and resolve it).
CARRIER_SQL = tuple(dict.fromkeys(s for c in PROPERTY_CARRIERS for s in c.sql))


def carried_columns() -> frozenset[tuple[str, str]]:
    """Every (table, column) some carrier keeps true across a merge and a detach."""
    return frozenset(col for c in PROPERTY_CARRIERS for col in c.columns)
