"""Hermetic tests for the property-grain operator-state reconciler.

A merge re-points property-anchored operator state (collections/tags/notes/
watchdog dispatches) onto the survivor in the same transaction, so it never
orphans onto the merged_away loser. These tests assert the SQL the reconciler
emits per shape (SET = collision-collapse + re-point; APPEND = re-point only);
the real union/no-orphan semantics are verified out-of-band via the Supabase MCP.
"""

from __future__ import annotations

from typing import Any

from toolkit.operator_state import (
    OPERATOR_STATE_TABLES,
    carry_operator_state_on_merge,
)


class _Cur:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((" ".join(sql.split()), params))


def test_registry_covers_exactly_the_property_anchored_tables():
    names = {t[0] for t in OPERATOR_STATE_TABLES}
    assert names == {
        "collection_properties",
        "property_tags",
        "property_notes",
        "notification_dispatches",
    }


def test_set_tables_collision_collapse_then_repoint():
    cur = _Cur()
    carry_operator_state_on_merge(cur, retired_id=20, survivor_id=10)
    sqls = [s for s, _ in cur.executed]

    expected = {
        "collection_properties": "s.collection_id = r.collection_id",
        "property_tags": "s.tag_id = r.tag_id",
        "notification_dispatches":
            "s.subscription_id = r.subscription_id AND s.change_kind = r.change_kind",
    }
    for tbl, join in expected.items():
        dels = [s for s in sqls if s.startswith(f"DELETE FROM {tbl} r")]
        assert len(dels) == 1, f"{tbl}: expected one collision delete"
        assert join in dels[0], f"{tbl}: wrong dedup join"
        ups = [s for s in sqls if f"UPDATE {tbl} SET property_id" in s]
        assert len(ups) == 1, f"{tbl}: expected one re-point update"


def test_append_table_repoints_without_a_collision_delete():
    cur = _Cur()
    carry_operator_state_on_merge(cur, retired_id=20, survivor_id=10)
    sqls = [s for s, _ in cur.executed]
    assert not any(s.startswith("DELETE FROM property_notes") for s in sqls)
    assert any("UPDATE property_notes SET property_id" in s for s in sqls)


def test_every_statement_repoints_retired_to_survivor():
    cur = _Cur()
    carry_operator_state_on_merge(cur, retired_id=20, survivor_id=10)
    for _sql, params in cur.executed:
        assert params == {"retired": 20, "survivor": 10}
