"""The operator's reasons for changing training-set marks (migration 473).

What can go wrong is small and specific: an empty note stored as a reason, a
note absorbed into another tag's revision (silent audit corruption), a note
read into two revisions, and the read path taking a page down before the
migration is applied.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Cur:
    def __init__(self, rows: list[tuple], log: list) -> None:
        self._rows, self._log = rows, log

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: Any) -> None: ...

    def execute(self, sql: str, params: Any = None) -> None:
        self._log.append((sql, params))

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, *batches: list[tuple]) -> None:
        self._batches = list(batches)
        self.log: list = []

    def cursor(self) -> _Cur:
        rows = self._batches.pop(0) if self._batches else []
        return _Cur(rows, self.log)


def test_a_note_is_recorded_with_the_change_it_explains() -> None:
    from toolkit import tag_label_notes as n

    conn = _Conn([(7, None)])
    out = n.record_note(conn, image_id=5, tag_id=3, from_state="positive",
                        to_state="negative", note="  entrance door,   facade is backdrop ")
    assert out["id"] == 7 and out["from_state"] == "positive" and out["to_state"] == "negative"
    # Whitespace collapsed; the reason is stored as written, not rewritten.
    assert out["note"] == "entrance door, facade is backdrop"
    assert conn.log[0][1]["note"] == "entrance door, facade is backdrop"


@pytest.mark.parametrize("note", ["", "   ", "x" * 601])
def test_an_empty_or_oversized_note_is_refused(note: str) -> None:
    from toolkit import tag_label_notes as n

    with pytest.raises(ValueError):
        n.record_note(_Conn([]), image_id=5, tag_id=3, to_state="negative", note=note)


def test_states_are_checked() -> None:
    from toolkit import tag_label_notes as n

    with pytest.raises(ValueError):
        n.record_note(_Conn([]), image_id=5, tag_id=3, to_state="maybe", note="x")
    with pytest.raises(ValueError):
        n.record_note(_Conn([]), image_id=5, tag_id=3, from_state="maybe",
                      to_state="negative", note="x")


def test_absorb_is_scoped_to_the_definitions_own_tag_in_sql() -> None:
    # Absorbing garáž's notes into a kuchyně revision would corrupt the audit
    # silently; the tag match lives in the WHERE, not in the caller's care.
    from toolkit import tag_label_notes as n

    assert "n.tag_id = d.tag_id" in n._ABSORB_SQL
    assert "n.absorbed_definition_id IS NULL" in n._ABSORB_SQL  # never twice
    conn = _Conn([(1,), (2,)])
    assert n.absorb(conn, definition_id=99, note_ids=[1, 2, 3]) == [1, 2]
    assert n.absorb(_Conn([]), definition_id=99, note_ids=[]) == []


def test_open_notes_are_the_default_read() -> None:
    from toolkit import tag_label_notes as n

    conn = _Conn([])
    n.list_notes(conn, tag_id=3)
    sql, params = conn.log[0]
    assert "absorbed_definition_id IS NULL" in sql
    assert params["include_absorbed"] is False


def test_the_read_paths_survive_the_migration_not_being_applied() -> None:
    # Merge is not apply: these reads ship with 473 and must not take the
    # taxonomy page down in the window between.
    import psycopg

    from toolkit import tag_label_notes as n

    class _Boom(_Conn):
        def cursor(self) -> _Cur:
            raise psycopg.errors.UndefinedTable("relation does not exist")

    assert n.list_notes(_Boom(), tag_id=3) == []
    assert n.open_counts(_Boom()) == {}


def test_the_absorption_rule_is_written_where_it_is_read() -> None:
    # The rule — one general line per batch, never one sentence per note — is
    # what keeps definitions readable by a person. It lives in the migration
    # and the module, not only in a chat.
    mig = (ROOT / "migrations" / "473_tag_label_notes.sql").read_text()
    src = (ROOT / "toolkit" / "tag_label_notes.py").read_text()
    for text in (mig, src):
        # Line-wrapped prose, so the anchors are phrases that fit on one line.
        assert "NOT copied into the definition" in text
        assert "ONCE" in text
    assert "revoke all on tag_label_notes from anon, authenticated" in mig
    assert "revoke all on sequence tag_label_notes_id_seq" in mig
