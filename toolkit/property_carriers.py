"""THE ordered list of everything that follows a property across a merge (rules 15, 18, 22):
`PROPERTY_CARRIERS`, the seam `toolkit.property_identity` walks, and `NOT_CARRIED`, the written
reason for every other column that names a property. A census enforces the two together,
offline over `migrations/*.sql` (tests/test_property_carriers.py) and live over the replayed
schema (tests/test_property_carriers_live.py): a new column naming a property fails until it is
carried or named here.

Carrier invariants:
  - A carrier runs on the writer's service-role cursor, inside the writer's transaction; it
    opens no transaction, commits nothing and raises nothing of its own, so any psycopg error
    aborts the whole merge.
  - Every cross-side predicate over an account's rows is partitioned by account (tenancy shape
    3, `.claude/skills/database/references/tenancy.md`): the service role bypasses RLS. Pipeline
    and Dismissals name `account_id`; a `CurationTable` is partitioned through its keys, ids one
    account owns (collection_id, tag_id, subscription_id), so one whose keys are not
    account-owned must add `account_id` to them.
  - It never deletes history. The sanctioned deletes are a SET table's collision collapse and
    the losing pipeline card, each folded into the carry record with its snapshot.
  - `on_merge` runs once per retired property, after the merge ledger row and the advert
    re-point, and returns a `Carried` per curation row it moved or folded (alert events none).
    A detach runs no carrier: it routes the curation its ads take with them (`curation_plan`,
    read before any ad moves, then `route_curation`; MS17, MS18).

The hard ordering: the ledger INSERT comes before the re-point (it selects the adverts the
re-point then moves; that lives in `property_identity._merge_pair`); Pipeline before Dismissals
(the dismissal lift reads the live card the pipeline carry just placed); every carrier before the
carry record's INSERT (migration 589), and that INSERT before the retire, which under migration
559 is the last write, so the next step's came-from lookups read this step's rows. The rest write
disjoint tables.

Adding one: a SET or APPEND table keyed on `property_id` is one `CurationTable(...)` line before
`Pipeline()`, `at=` its own timestamp column; any other shape is one class meeting `Carrier`; a
column that must NOT follow a merge is one `NOT_CARRIED` line with its reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import (
    Any, Iterable, Literal, Mapping, NamedTuple, Protocol, Sequence, runtime_checkable,
)
from uuid import UUID

import psycopg

from toolkit import pipeline_identity

# "auto" = the removed legacy engine (historic rows only); "autodedup" = migration 558.
MergeSource = Literal["auto", "operator", "autodedup"]


@dataclass(frozen=True)
class MergeStep:
    survivor: int
    retired: int
    group: str
    source: MergeSource


class Carried(NamedTuple):
    """One curation row a step moved or folded onto its survivor: a carry record row (MS14)."""

    table: str
    row_key: int | None  # the note/dismissal id, collection_id, tag_id; None = a pipeline card
    row_at: datetime  # the row's own timestamp
    account_id: UUID | None
    from_property: int
    kind: str  # 'moved' | 'folded'
    snapshot: dict[str, Any] | None  # a folded row as it stood before the fold


@runtime_checkable
class Carrier(Protocol):
    name: str
    columns: tuple[tuple[str, str], ...]  # (table, column) it keeps true across a merge
    sql: tuple[str, ...]  # every statement it can execute

    def on_merge(self, cur: psycopg.Cursor, step: MergeStep) -> list[Carried]: ...


class CurationTable:
    """A property-anchored SET table (`keys` given: rows unique on keys + property_id; union
    onto the survivor, collapsing a retired row whose twin the survivor holds, NULL-safe) or
    APPEND table (no keys: every row moves). The table name is code-controlled, never input.
    `keys` must include an account-owned id or `account_id`: the collapse joins on them alone.
    With `at` (the row's own timestamp column) its rows enter the carry record: the collapsed
    ones folded, with their snapshot, the rest moved; the row key is `keys[0]`, else `id`."""

    def __init__(self, table: str, keys: tuple[str, ...] = (), *, at: str | None = None) -> None:
        self.name = table
        self.keys = keys
        self.at = at
        self.columns: tuple[tuple[str, str], ...] = ((table, "property_id"),)
        key = keys[0] if keys else "id"
        move = (f"UPDATE {table} SET property_id = %(survivor)s "
                f"WHERE property_id = %(retired)s"
                + (f" RETURNING {key}, {at}, account_id" if at else ""))
        if not keys:
            self.sql: tuple[str, ...] = (move,)
            return
        # NULL-safe: notification_dispatches' keys are nullable, and a NULL must collapse
        # against a NULL, which plain `=` never does.
        join = " AND ".join(f"s.{c} IS NOT DISTINCT FROM r.{c}" for c in keys)
        collapse = (f"DELETE FROM {table} r "
                    f"WHERE r.property_id = %(retired)s AND EXISTS ("
                    f" SELECT 1 FROM {table} s "
                    f" WHERE s.property_id = %(survivor)s AND {join})"
                    + (f" RETURNING r.{key}, r.{at}, r.account_id, to_jsonb(r)" if at else ""))
        self.sql = (collapse, move)

    def on_merge(self, cur: psycopg.Cursor, step: MergeStep) -> list[Carried]:
        params = {"retired": step.retired, "survivor": step.survivor}
        out: list[Carried] = []
        for statement in self.sql:
            cur.execute(statement, params)
            if self.at:
                kind = "moved" if statement == self.sql[-1] else "folded"
                out += [Carried(self.name, row[0], row[1], row[2], step.retired, kind,
                                row[3] if kind == "folded" else None)
                        for row in cur.fetchall()]
        return out


class Dispatches(CurationTable):
    """The unified event table (migration 206) as a SET table, whose collapse would otherwise
    strand a delivery: `channel_sends.notification_id` is ON DELETE SET NULL (migration 207),
    and `channel_sends_check` (migration 274) refuses a NULL on a notification-backed send, so
    one delivered alert aborted the whole merge. Before the collapse, `LOCK_SQL` stops the
    outbox claiming new sends on the retired rows, then `RESEND_SQL` hands each such send to
    the survivor's twin, the row the collapse keeps; `channel_sends` is unique only on its own
    dedupe_key, so a move never collides."""

    # Its own statement, never a CTE of the resend: FOR UPDATE waits out a claim whose foreign
    # key check holds FOR KEY SHARE on a retired row, so the resend's fresh READ COMMITTED
    # snapshot sees that send; a later claim waits for the merge to commit.
    LOCK_SQL = ("SELECT id FROM notification_dispatches WHERE property_id = %(retired)s "
                "ORDER BY id FOR UPDATE")

    # The twin is the collapse's own join over the same keys; subscription_id and collection_id
    # are account-owned ids, so the pairing never crosses an account.
    RESEND_SQL = """
UPDATE channel_sends cs SET notification_id = s.id
FROM notification_dispatches r
JOIN notification_dispatches s
  ON s.property_id = %(survivor)s
 AND s.subscription_id IS NOT DISTINCT FROM r.subscription_id
 AND s.collection_id IS NOT DISTINCT FROM r.collection_id
 AND s.change_kind IS NOT DISTINCT FROM r.change_kind
 AND s.trigger_snapshot_id IS NOT DISTINCT FROM r.trigger_snapshot_id
WHERE r.property_id = %(retired)s AND cs.notification_id = r.id
"""

    def __init__(self) -> None:
        # Identity spans both property-grain producers (subscription_id XOR collection_id) and
        # the per-snapshot grain (trigger_snapshot_id, NULL for 'new').
        super().__init__("notification_dispatches",
                         ("subscription_id", "collection_id", "change_kind",
                          "trigger_snapshot_id"))
        self.sql = (self.LOCK_SQL, self.RESEND_SQL, *self.sql)


class Pipeline:
    """Rule 22's single-valued card, terminal-aware; implemented in `toolkit.pipeline_identity`."""

    name = "pipeline"
    columns = (("property_pipeline", "property_id"),)
    sql = pipeline_identity.STATEMENTS

    def on_merge(self, cur: psycopg.Cursor, step: MergeStep) -> list[Carried]:
        cards = pipeline_identity.reconcile_pipeline_on_merge(
            cur, retired_id=step.retired, survivor_id=step.survivor)
        return [Carried("property_pipeline", None, at, account, origin, kind, snapshot)
                for account, at, origin, kind, snapshot in cards]


# One active row per (property, account): where both sides hold one, the survivor's stands and
# the retired's is lifted, not deleted. PostgreSQL 17's RETURNING has no OLD, so a lifted row's
# snapshot is its new row with the lift taken off (both lifts touch only an unlifted row).
_DISMISSAL_LIFT_DUPLICATE_SQL = (
    "UPDATE property_dismissals r SET lifted_at = now(), lift_reason = 'merge' "
    "WHERE r.property_id = %(r)s AND r.lifted_at IS NULL "
    "AND EXISTS (SELECT 1 FROM property_dismissals s "
    "  WHERE s.property_id = %(s)s AND s.account_id = r.account_id "
    "  AND s.lifted_at IS NULL) "
    "RETURNING r.id, r.dismissed_at, r.account_id, "
    "  to_jsonb(r) || '{\"lifted_at\": null, \"lift_reason\": null}'::jsonb"
)
_DISMISSAL_REPOINT_SQL = (
    "UPDATE property_dismissals SET property_id = %(s)s WHERE property_id = %(r)s "
    "RETURNING id, dismissed_at, account_id"
)
# A LIVE deal and a dismissal never coexist for one account, whichever side each fact came from;
# a card closed into a terminal stage keeps it (the rule `api.dismissals` states on the tenant
# connection, here per account). The survivor's own dismissal came from the property its newest
# standing carry row names (`pipeline_identity.STANDING_CARRY`), if a merge brought it here.
_DISMISSAL_LIFT_LIVE_DEAL_SQL = (
    "UPDATE property_dismissals d SET lifted_at = now(), lift_reason = 'pipeline' "
    "WHERE d.property_id = %(s)s AND d.lifted_at IS NULL "
    "AND EXISTS (SELECT 1 FROM property_pipeline pp "
    "  JOIN pipeline_stages ps ON ps.id = pp.stage_id "
    "  WHERE pp.property_id = d.property_id AND pp.account_id = d.account_id "
    "  AND NOT ps.is_terminal) "
    "RETURNING d.id, d.dismissed_at, d.account_id, "
    "  to_jsonb(d) || '{\"lifted_at\": null, \"lift_reason\": null}'::jsonb, "
    "  (SELECT c.from_property_id FROM property_merge_carries c "
    "   WHERE c.table_name = 'property_dismissals' AND c.to_property_id = d.property_id "
    "     AND c.row_key = d.id AND " + pipeline_identity.STANDING_CARRY +
    "   ORDER BY c.id DESC LIMIT 1)"
)


class Dismissals:
    """Migration 536's append-only dismissals: the survivor inherits them from either side, a
    colliding active row is lifted (`merge`), never deleted — so not a `CurationTable`. One
    carry row per dismissal, its last kind: a lifted one is folded, from the retired property
    when this step moved it, else from where it came."""

    name = "dismissals"
    columns = (("property_dismissals", "property_id"),)
    sql = (_DISMISSAL_LIFT_DUPLICATE_SQL, _DISMISSAL_REPOINT_SQL, _DISMISSAL_LIFT_LIVE_DEAL_SQL)

    def on_merge(self, cur: psycopg.Cursor, step: MergeStep) -> list[Carried]:
        params = {"r": step.retired, "s": step.survivor}
        rows: dict[int, Carried] = {}
        cur.execute(_DISMISSAL_LIFT_DUPLICATE_SQL, params)
        table = "property_dismissals"
        for key, at, account, snapshot in cur.fetchall():
            rows[key] = Carried(table, key, at, account, step.retired, "folded", snapshot)
        cur.execute(_DISMISSAL_REPOINT_SQL, params)
        for key, at, account in cur.fetchall():
            rows.setdefault(key, Carried(table, key, at, account, step.retired, "moved", None))
        cur.execute(_DISMISSAL_LIFT_LIVE_DEAL_SQL, params)
        for key, at, account, snapshot, came in cur.fetchall():
            origin = step.retired if key in rows else (step.survivor if came is None else came)
            rows[key] = Carried(table, key, at, account, int(origin), "folded", snapshot)
        return list(rows.values())


PROPERTY_CARRIERS: tuple[Carrier, ...] = (
    CurationTable("collection_properties", ("collection_id",), at="added_at"),
    CurationTable("property_tags", ("tag_id",), at="attached_at"),
    CurationTable("property_notes", at="created_at"),
    Dispatches(),
    Pipeline(),
    Dismissals(),
)

_ENGINE_HISTORY = "history of the removed decision engine, never consulted (rule 15)"
_AUTODEDUP_HISTORY = "an engine or operator ledger: history (D7)"
_MERGE_LEDGER = "the chokepoint's own ledger: history, replayed by a detach"
_CARRY_RECORD = "the carry record itself (migration 589): history a split reads (MS14)"
_ASSET_LINKS_REMOVED = "dropped in W6; the asset-link feature was removed in W1b"

NOT_CARRIED: dict[tuple[str, str], str] = {
    ("listings", "property_id"):
        "the identity link itself: the writers move it (merge re-point, detach move)",
    ("properties", "merged_into"):
        "the merge's own pointer: set by the retire, cleared by the reactivation",
    ("properties", "asset_id"): _ASSET_LINKS_REMOVED,
    ("asset_membership_events", "property_id"): _ASSET_LINKS_REMOVED,
    ("property_merge_events", "survivor_property_id"): _MERGE_LEDGER,
    ("property_merge_events", "retired_property_id"): _MERGE_LEDGER,
    ("property_merge_events", "prev_property_id"): _MERGE_LEDGER,
    ("property_merge_carries", "from_property_id"): _CARRY_RECORD,
    ("property_merge_carries", "to_property_id"): _CARRY_RECORD,
    ("property_pipeline_events", "property_id"):
        "the pipeline's move log: history naming the property at the time; merges no longer "
        "write it (W3)",
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
    """Every (table, column) some carrier keeps true across a merge."""
    return frozenset(col for c in PROPERTY_CARRIERS for col in c.columns)




# --- routing: what a detach gives back (MS17, MS18) ------------------------------------------


class Route(NamedTuple):
    """One curation item a detach routes: moved to where its anchor ad lands, copied there, or a
    folded item re-created. `skipped` says why a re-creation or a copy is not made: 'held' (its
    account holds that item where it would land), 'card' (a dismissal where that account's live
    card lands) or 'gone' (a fold whose twin is no longer on `left`)."""

    table: str
    key: int | None  # the note/dismissal id, collection_id, tag_id; None = a pipeline card
    account_id: UUID | None
    action: str  # 'move' | 'copy' | 'recreate'
    anchor: int | None  # an ad: the item lands where that ad lands (None = stays on `left`)
    origin: int | None  # the property it came from, back there when this detach gives it ads
    stage_id: int | None  # a card's stage
    carry_ids: tuple[int, ...]  # standing carry rows it consumes (stamped undone)
    item: str  # 'note:<id>' | 'pipeline' | 'collection:<id>' | 'tag:<id>' | 'dismissal' | 'fold:<id>'
    label: str | None
    skipped: str | None = None


# The routing read, one statement for every account: each curation item on `left` (an active
# dismissal only) with the ad a note was written on while that ad is on `left`, and the carry rows
# it came by, oldest first; then each fold. A carry row counts only toward `left` through its
# merged-away chain, only `group`'s when one is given, and only while it stands (a fold of the
# survivor's own item, from = to, while its merge has a ledger row standing).
_ROUTES_SQL = (
    """
WITH RECURSIVE tree(id) AS (
    SELECT %(left)s::bigint
    UNION
    SELECT p.id FROM properties p JOIN tree t ON p.merged_into = t.id
    WHERE p.status = 'merged_away'
), item AS (
    SELECT 'property_notes'::text AS tbl, n.id AS row_key, n.account_id, n.created_at AS row_at,
           'note:' || n.id::text AS item, left(n.body, 60) AS label, NULL::bigint AS stage_id,
           NULL::boolean AS live,
           CASE WHEN EXISTS (SELECT 1 FROM listings l WHERE l.id = n.origin_listing_ref_id
                               AND l.property_id = %(left)s)
                THEN n.origin_listing_ref_id END AS ad
      FROM property_notes n WHERE n.property_id = %(left)s
    UNION ALL
    SELECT 'property_pipeline', NULL, p.account_id, p.added_at, 'pipeline', s.label, p.stage_id,
           NOT s.is_terminal, NULL
      FROM property_pipeline p JOIN pipeline_stages s ON s.id = p.stage_id
     WHERE p.property_id = %(left)s
    UNION ALL
    SELECT 'collection_properties', cp.collection_id, cp.account_id, cp.added_at,
           'collection:' || cp.collection_id::text, c.name, NULL, NULL, NULL
      FROM collection_properties cp JOIN collections c ON c.id = cp.collection_id
     WHERE cp.property_id = %(left)s
    UNION ALL
    SELECT 'property_tags', pt.tag_id, pt.account_id, pt.attached_at,
           'tag:' || pt.tag_id::text, t.name, NULL, NULL, NULL
      FROM property_tags pt JOIN tags t ON t.id = pt.tag_id
     WHERE pt.property_id = %(left)s
    UNION ALL
    SELECT 'property_dismissals', d.id, d.account_id, d.dismissed_at, 'dismissal', NULL, NULL,
           NULL, NULL
      FROM property_dismissals d WHERE d.property_id = %(left)s AND d.lifted_at IS NULL
), carry AS (
    SELECT c.* FROM property_merge_carries c
     WHERE c.to_property_id IN (SELECT id FROM tree)
       AND (%(group)s::uuid IS NULL OR c.merge_group_id = %(group)s::uuid)
       AND ("""
    + pipeline_identity.STANDING_CARRY
    + """
            OR (c.undone_at IS NULL AND c.from_property_id = c.to_property_id
                AND EXISTS (SELECT 1 FROM property_merge_events e
                             WHERE e.merge_group_id = c.merge_group_id AND e.undone_at IS NULL)))
)
SELECT 'move' AS kind, i.tbl, i.row_key, i.account_id, i.item, i.label, i.stage_id, i.live, i.ad,
       NULL::bigint AS to_property_id,
       (array_agg(c.from_property_id ORDER BY c.id) FILTER (WHERE c.id IS NOT NULL))[1],
       coalesce(array_agg(c.id ORDER BY c.id) FILTER (WHERE c.id IS NOT NULL), '{}'::bigint[]),
       NULL::boolean AS twin
  FROM item i
  LEFT JOIN carry c ON c.kind = 'moved' AND c.table_name = i.tbl
       AND c.row_key IS NOT DISTINCT FROM i.row_key
       AND c.account_id IS NOT DISTINCT FROM i.account_id AND c.row_at = i.row_at
 GROUP BY i.tbl, i.row_key, i.account_id, i.row_at, i.item, i.label, i.stage_id, i.live, i.ad
UNION ALL
SELECT 'fold', f.table_name, f.row_key, f.account_id, 'fold:' || f.id::text,
       coalesce(co.name, tg.name, st.label), st.id, NOT st.is_terminal, NULL,
       f.to_property_id, f.from_property_id, ARRAY[f.id],
       CASE f.table_name
         WHEN 'collection_properties' THEN EXISTS (SELECT 1 FROM collection_properties x
              WHERE x.property_id = %(left)s AND x.collection_id = f.row_key)
         WHEN 'property_tags' THEN EXISTS (SELECT 1 FROM property_tags x
              WHERE x.property_id = %(left)s AND x.tag_id = f.row_key)
         WHEN 'property_pipeline' THEN EXISTS (SELECT 1 FROM property_pipeline x
              WHERE x.property_id = %(left)s AND x.account_id IS NOT DISTINCT FROM f.account_id)
         WHEN 'property_dismissals' THEN EXISTS (SELECT 1 FROM property_dismissals x
              WHERE x.property_id = %(left)s AND x.account_id = f.account_id
                AND x.lifted_at IS NULL)
           OR EXISTS (SELECT 1 FROM property_pipeline x JOIN pipeline_stages s ON s.id = x.stage_id
              WHERE x.property_id = %(left)s AND x.account_id = f.account_id AND NOT s.is_terminal)
         ELSE false END
  FROM carry f
  LEFT JOIN collections co ON f.table_name = 'collection_properties' AND co.id = f.row_key
  LEFT JOIN tags tg ON f.table_name = 'property_tags' AND tg.id = f.row_key
  LEFT JOIN pipeline_stages st ON f.table_name = 'property_pipeline'
       AND st.id = (f.snapshot ->> 'stage_id')::bigint AND st.archived_at IS NULL
 WHERE f.kind = 'folded'
 ORDER BY 1, 5
"""
)

# A move lands only where its account does not hold that item already (an origin active again
# holds its own), so it never trips a key; the item then stays on `left`, uncounted.
_MOVE_SQL: dict[str, str] = {
    "property_notes": (
        "UPDATE property_notes SET property_id = %(to)s "
        "WHERE id = %(key)s AND property_id = %(left)s"),
    "property_pipeline": (
        "UPDATE property_pipeline SET property_id = %(to)s "
        "WHERE property_id = %(left)s AND account_id = %(account)s "
        "AND NOT EXISTS (SELECT 1 FROM property_pipeline x "
        "  WHERE x.property_id = %(to)s AND x.account_id = %(account)s)"),
    "collection_properties": (
        "UPDATE collection_properties SET property_id = %(to)s "
        "WHERE property_id = %(left)s AND collection_id = %(key)s "
        "AND NOT EXISTS (SELECT 1 FROM collection_properties x "
        "  WHERE x.property_id = %(to)s AND x.collection_id = %(key)s)"),
    "property_tags": (
        "UPDATE property_tags SET property_id = %(to)s "
        "WHERE property_id = %(left)s AND tag_id = %(key)s "
        "AND NOT EXISTS (SELECT 1 FROM property_tags x "
        "  WHERE x.property_id = %(to)s AND x.tag_id = %(key)s)"),
    "property_dismissals": (
        "UPDATE property_dismissals d SET property_id = %(to)s "
        "WHERE d.id = %(key)s AND d.property_id = %(left)s "
        "AND NOT EXISTS (SELECT 1 FROM property_dismissals x "
        "  WHERE x.property_id = %(to)s AND x.account_id = d.account_id AND x.lifted_at IS NULL)"),
}

# A copy or a re-created fold (MS18): a note keeps its text, dates and origin ad (read from its
# row, wherever it went); a card keeps its stage, last in its column, its dates now; the rest
# dated now. Never over a row already there.
_INSERT_SQL: dict[str, str] = {
    "property_notes": (
        "INSERT INTO property_notes (property_id, body, origin_listing_id, origin_listing_ref_id, "
        "  created_at, updated_at, account_id) "
        "SELECT %(to)s, body, origin_listing_id, origin_listing_ref_id, created_at, updated_at, "
        "  account_id FROM property_notes WHERE id = %(key)s RETURNING id"),
    "property_pipeline": (
        "INSERT INTO property_pipeline (property_id, stage_id, board_position, account_id) "
        "SELECT %(to)s::bigint, %(stage)s::bigint, coalesce(max(board_position), 0) + 1, "
        "  %(account)s::uuid "
        "FROM property_pipeline WHERE account_id = %(account)s::uuid AND stage_id = %(stage)s::bigint "
        "ON CONFLICT DO NOTHING"),
    "collection_properties": (
        "INSERT INTO collection_properties (collection_id, property_id, account_id) "
        "VALUES (%(key)s, %(to)s, %(account)s) ON CONFLICT DO NOTHING"),
    "property_tags": (
        "INSERT INTO property_tags (tag_id, property_id, account_id) "
        "VALUES (%(key)s, %(to)s, %(account)s) ON CONFLICT DO NOTHING"),
    "property_dismissals": (
        "INSERT INTO property_dismissals (account_id, property_id) VALUES (%(account)s, %(to)s) "
        "ON CONFLICT (property_id, account_id) WHERE lifted_at IS NULL DO NOTHING"),
}

# What a copy brings with it, keyed on the copy's new id: a note's attachments, pointing at the
# same stored bytes (never deleted); each takes its account from the new note (migration 592).
_AFTER_COPY_SQL: dict[str, str] = {
    "property_notes": (
        "INSERT INTO property_note_attachments (note_id, storage_key, filename, mime_type, "
        "  byte_size, sha256_hex, created_at) "
        "SELECT %(copy)s, storage_key, filename, mime_type, byte_size, sha256_hex, created_at "
        "FROM property_note_attachments WHERE note_id = %(key)s ORDER BY id"),
}

_STAMP_SQL = (
    "UPDATE property_merge_carries SET undone_at = now() "
    "WHERE id = ANY(%(ids)s::bigint[]) AND undone_at IS NULL"
)

# Every routing statement (the PREPARE corpus reads the tuple); none deletes anything.
ROUTE_SQL = (_ROUTES_SQL, *_MOVE_SQL.values(), *_INSERT_SQL.values(), *_AFTER_COPY_SQL.values(),
             _STAMP_SQL)

# Cards first, so a dismissal is settled against where each account's live card ends; within a
# table the user's copies before the folds re-created by default.
_SETTLE_ORDER = {"property_pipeline": 0, "property_dismissals": 2}


def _holding(r: Route) -> tuple[str, Any] | None:
    """What a route puts where it lands, as a conflict knows it; a note never conflicts."""
    if r.table == "property_notes":
        return None
    if r.table in ("collection_properties", "property_tags"):
        return (r.table, r.key)
    return (r.table, r.account_id)


def _settle(routes: list[Route], *, left: int, movers: Mapping[int, int | None],
            live: Mapping[tuple[UUID | None, str], bool]) -> list[Route]:
    """The conflict pass: a copy or a re-created fold never lands where its account holds that
    item after the moves (one card, collection entry, tag or active dismissal: 'held'), nor a
    dismissal where its account's live card lands ('card'); such an insert is kept, skipped."""
    def slot(anchor: int | None) -> tuple[str, int | None]:
        if anchor is None or anchor not in movers:
            return ("left", left)
        return ("back", movers[anchor]) if movers[anchor] is not None else ("new", anchor)

    held: dict[tuple[str, int | None], set[tuple[str, Any]]] = {}

    def put(r: Route) -> None:
        here = held.setdefault(slot(r.anchor), set())
        if (h := _holding(r)) is not None:
            here.add(h)
        if r.table == "property_pipeline" and live.get((r.account_id, r.item)):
            here.add(("live", r.account_id))

    out = [r for r in routes if r.action == "move"]
    for r in out:
        put(r)
    for r in sorted((r for r in routes if r.action != "move"),
                    key=lambda r: (_SETTLE_ORDER.get(r.table, 1), r.action != "copy")):
        here = held.get(slot(r.anchor), set())
        why = None if r.skipped else "held" if _holding(r) in here else (
            "card" if r.table == "property_dismissals" and ("live", r.account_id) in here
            else None)
        if why:
            r = r._replace(skipped=why, carry_ids=() if r.origin == left else r.carry_ids)
        if not r.skipped:
            put(r)
        out.append(r)
    return out


def curation_plan(
    cur: psycopg.Cursor,
    *,
    left: int,
    movers: Mapping[int, int | None],
    group: str | None = None,
    choices: Mapping[str, tuple[int | None, tuple[int, ...]]] | None = None,
    account: UUID | None = None,
) -> list[Route]:
    """Where every account's curation on `left` goes when `movers` leave it (each ad -> the
    origin it goes back to, None = a new property): READ before any ad moves, since the carry
    rows it reads are the ones the detach then stamps. Preselected (MS17, MS18): a note goes with
    the ad it was written on while that ad is on `left`; any other item goes back to the
    property its oldest standing carry row names (only `group`'s rows when given) when this
    detach gives that property ads back, else it stays; a fold is re-created where it came from
    while its twin is on `left`. `choices` override the acting `account`'s own items (an anchor
    ad, None = stays; copies to other anchors), consuming the item's carry rows; then
    `_settle`."""
    cur.execute(_ROUTES_SQL, {"left": left, "group": group})
    rows = cur.fetchall()
    back: dict[int, int] = {}
    for ad, origin in sorted(movers.items()):
        if origin is not None:
            back.setdefault(int(origin), int(ad))
    routes: list[Route] = []
    came: dict[tuple[UUID | None, str], tuple[int, ...]] = {}
    live: dict[tuple[UUID | None, str], bool] = {}
    for kind, table, key, acc, item, label, stage, is_live, ad, to, origin, ids, twin in rows:
        ids = tuple(int(i) for i in ids or ())
        live[(acc, item)] = bool(is_live)
        frm = None if origin is None else int(origin)
        if kind == "move":
            came[(acc, item)] = ids
            anchor, frm = ((int(ad), None) if ad is not None else
                           (back[frm], frm) if frm in back else (None, None))
            routes.append(Route(table, key, acc, "move", anchor, frm, stage,
                                ids if anchor in movers else (), item, label))
        elif table == "property_pipeline" and stage is None:
            continue  # its stage was archived since the fold: a card is never re-made there
        elif frm in back:
            routes.append(Route(table, key, acc, "recreate", back[frm], frm, stage, ids, item,
                                label, skipped=None if twin else "gone"))
        elif frm == left and to == left and twin:
            routes.append(Route(table, key, acc, "recreate", None, left, stage, ids, item, label))
    if choices:
        chosen: list[Route] = []
        for r in routes:
            pick = (choices.get(r.item) if r.action == "move" and account is not None
                    and r.account_id == account else None)
            if pick is None:
                chosen.append(r)
                continue
            anchor, copies = pick
            ids = came[(r.account_id, r.item)] if anchor != r.anchor or anchor in movers else ()
            chosen.append(r._replace(anchor=anchor, carry_ids=ids))
            chosen += [Route(r.table, r.key, r.account_id, "copy", c, None, r.stage_id, (),
                             r.item, r.label) for c in copies]
        routes = chosen
    return _settle(routes, left=left, movers=movers, live=live)


def _counts(routes: Iterable[Route]) -> dict[str, int]:
    """Of these routes, `note_moves`: the notes that go with their ad; `carry_rows`: every other
    item that moves, plus the folds re-created (a copy or a skipped route is neither)."""
    notes = rest = 0
    for r in routes:
        if r.skipped or r.action == "copy":
            continue
        if r.action == "move" and r.table == "property_notes" and r.origin is None:
            notes += 1
        else:
            rest += 1
    return {"carry_rows": rest, "note_moves": notes}


def route_curation(
    cur: psycopg.Cursor, routes: Sequence[Route], *, left: int, landed: Mapping[int, int],
) -> dict[str, int]:
    """Make `curation_plan`'s answer true once the ads moved (`landed`: each ad that moved -> where
    it is now): the moves, the copies (a note copy reads its row wherever it went and takes its
    attachments, and a copy to the property left lands once its source has gone), the re-created
    folds, the carry stamp, then the live-deal lift on `left` and on every property an ad landed
    on (rule 22, the dismissal carrier's own statement). A route whose anchor did not move routes
    nothing; one is counted where its write changed a row: a move onto an item its account already
    holds there (an origin active again) leaves it, and its carry rows."""
    wrote: list[Route] = []
    for action in ("move", "copy", "recreate"):
        for r in routes:
            if (r.action != action or r.skipped or (r.anchor is None and action == "move")
                    or (r.anchor is not None and r.anchor not in landed)):
                continue
            cur.execute((_MOVE_SQL if action == "move" else _INSERT_SQL)[r.table], {
                "to": left if r.anchor is None else landed[r.anchor], "left": left,
                "key": r.key, "account": r.account_id, "stage": r.stage_id})
            if cur.rowcount:
                wrote.append(r)
                if action == "copy" and (follow := _AFTER_COPY_SQL.get(r.table)):
                    cur.execute(follow, {"copy": cur.fetchone()[0], "key": r.key})

    def spent(r: Route) -> bool:
        # a move consumes its carry rows where it moved (or was chosen to stay); a fold is
        # settled once its origin got ads back, re-made or not
        if r.action == "move" and r.anchor is not None:
            return any(r is w for w in wrote)
        return r.anchor is None or r.anchor in landed

    if ids := sorted({i for r in routes if spent(r) for i in r.carry_ids}):
        cur.execute(_STAMP_SQL, {"ids": ids})
    for pid in sorted({left, *landed.values()}):
        cur.execute(_DISMISSAL_LIFT_LIVE_DEAL_SQL, {"s": pid})
    return _counts(wrote)


def curation_preview(
    conn: psycopg.Connection, merge_group_id: str, listing_ids: Sequence[int],
) -> dict[str, int]:
    """The brake's dry run (`autodedup.apply.unapply`): what undoing merge `merge_group_id` for
    these ads would route, by the live routing's own plan (`curation_plan`), counted by kind:
    `note_moves` and `carry_rows` (the undo counts what it changed: an item an origin active
    again already holds stays, uncounted)."""
    # imported here: toolkit.property_identity imports this module
    from toolkit.property_identity import _plan_detaches

    plans = _plan_detaches(conn, sorted({int(i) for i in listing_ids}), merge_group_id)
    lefts: dict[int, dict[int, int | None]] = {}
    for lid, (at, outcome, _undo, target) in plans.items():
        if outcome == "detached" and at is not None:
            lefts.setdefault(int(at), {})[lid] = int(target)
    counts = {"carry_rows": 0, "note_moves": 0}
    with conn.cursor() as cur:
        for left, movers in sorted(lefts.items()):
            plan = curation_plan(cur, left=left, movers=movers, group=str(merge_group_id))
            leaving = (r for r in plan if r.action != "move" or r.anchor in movers)
            for key, n in _counts(leaving).items():
                counts[key] += n
    return counts
