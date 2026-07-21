"""The delisting-sweep family must survive a NULL in its seen-set (rule #3).

Every sweep in the family narrows with `<id column> <> ALL(%s)`. SQL's
three-valued logic makes that predicate two-sided dangerous, and the listing
identity refactor (Gate 2: non-sreality rows carry sreality_id = NULL) arms the
first side:

  * ONE NULL element   -> the comparison is NULL for EVERY row, so the UPDATE
    matches nothing and the sweep is a permanent no-op for the whole portal.
  * an EMPTY array     -> the comparison is true for EVERY row, so the sweep
    delists the entire scope.

Both verified against the live database:
  select 5 <> all(array[1,2,NULL]);  -> NULL
  select 5 <> all('{}'::bigint[]);   -> true

So NULLs must be dropped from the bound array AND an all-NULL seen-set must
bail out rather than degrade into the empty-array case. Hermetic fake conn
records the executed SQL + bound params, same pattern as test_db_inactive_at.
"""

from __future__ import annotations

from typing import Any

import pytest

from scraper import db


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((" ".join(sql.split()), params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[tuple[str, Any]] = []

    def transaction(self) -> _Ctx:
        return _Ctx()

    def cursor(self) -> _Cur:
        return _Cur(self)


def _sweep(conn: _FakeConn) -> tuple[str, Any] | None:
    return next((e for e in conn.executed if "SET is_active = false" in e[0]), None)


def _bound_ids(conn: _FakeConn) -> list[Any]:
    sweep = _sweep(conn)
    assert sweep is not None, "expected a delisting sweep statement"
    return sweep[1][-1]          # the seen-set array is always the last param


# --- a NULL in the seen-set must never reach the bound array ----------------


def test_mark_inactive_drops_null_from_seen_set() -> None:
    conn = _FakeConn()
    db.mark_inactive(conn, "byt", "prodej", {1, 2, None})  # type: ignore[arg-type]
    ids = _bound_ids(conn)
    assert None not in ids, "a NULL element voids `<> ALL(...)` for EVERY row"
    assert sorted(ids) == [1, 2]


def test_mark_inactive_native_drops_null_from_seen_set() -> None:
    conn = _FakeConn()
    db.mark_inactive_native(conn, "idnes", "byt", "prodej", {"a", "b", None})  # type: ignore[arg-type]
    ids = _bound_ids(conn)
    assert None not in ids
    assert sorted(ids) == ["a", "b"]


def test_mark_inactive_agenda_drops_null_from_seen_set() -> None:
    conn = _FakeConn()
    db.mark_inactive_agenda(conn, "remax", "prodej", {"a", None})  # type: ignore[arg-type]
    ids = _bound_ids(conn)
    assert None not in ids
    assert ids == ["a"]


# --- an all-NULL seen-set must bail out, NOT degrade to the empty array -----


@pytest.mark.parametrize("call", [
    lambda c: db.mark_inactive(c, "byt", "prodej", {None}),
    lambda c: db.mark_inactive_native(c, "idnes", "byt", "prodej", {None}),
    lambda c: db.mark_inactive_agenda(c, "remax", "prodej", {None}),
])
def test_all_null_seen_set_never_sweeps(call: Any) -> None:
    # `x <> ALL('{}')` is TRUE for every row: sweeping with the emptied array
    # would delist the entire scope. Bail out instead.
    conn = _FakeConn()
    assert call(conn) == 0
    assert _sweep(conn) is None


# --- the safety rails the sweep rides on are unchanged ----------------------


def test_null_filter_preserves_the_staleness_rail() -> None:
    # min_unseen_hours (rule #3's second rail) is bound BEFORE the seen-set, so
    # dropping NULLs must not disturb the parameter order.
    conn = _FakeConn()
    db.mark_inactive_native(
        conn, "idnes", "byt", "prodej", {"a", None}, min_unseen_hours=12,  # type: ignore[arg-type]
    )
    sweep = _sweep(conn)
    assert sweep is not None
    assert "last_seen_at < now() - make_interval(hours => %s)" in sweep[0]
    assert sweep[1] == ("idnes", "byt", "prodej", 12, ["a"])
