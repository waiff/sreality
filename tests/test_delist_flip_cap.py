"""The ceiling on how much of a category one sweep may delist (migration 451).

mark_inactive never had one: it flipped every unseen active row of a category in
a single statement, however many that was. That was survivable only because the
completeness gate kept the dangerous cases from ever running — a coincidence,
not a safety property, and the coincidence ends every time a portal's walk is
repaired. Fixing coverage is the SAME EVENT as authorising the mass flip it
unblocks: ceskereality's rebuilt walk went from 85.7% to 99.8% on byt/prodej in
one deploy, and ~29,400 rows became eligible the moment its flag allows it.
idnes has identical exposure the first time its walk ever completes.
"""

from __future__ import annotations

from typing import Any

from scraper import db


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if "FROM app_settings" in s:
            self._rows = [(self._conn.setting,)] if self._conn.setting is not None else []
        else:
            self._rows = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Conn:
    def __init__(self, setting: dict[str, Any] | None = None) -> None:
        self.setting = setting
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Ctx:
        return _Ctx()


def _allowed(conn, candidates: int, active_rows: int) -> bool:
    return db._delist_flip_allowed(
        conn, source="ceskereality", category_main="byt", category_type="prodej",
        subtype=None, candidates=candidates, active_rows=active_rows,
    )


def test_a_routine_sweep_is_allowed() -> None:
    conn = _Conn()
    assert _allowed(conn, candidates=100, active_rows=10_000) is True
    assert not any("delist_flip_refusals" in s for s, _ in conn.executed)


def test_the_mass_flip_is_refused() -> None:
    """The live shape: 78,718 active against 48,235 declared, so a newly-complete
    walk would flip ~29,400 in one pass."""
    conn = _Conn()
    assert _allowed(conn, candidates=29_400, active_rows=78_718) is False


def test_a_refusal_is_recorded_not_merely_logged() -> None:
    """An Actions log expires. The whole lesson of this sprint is that a signal
    nothing can query is a signal nobody receives."""
    conn = _Conn()
    _allowed(conn, candidates=29_400, active_rows=78_718)
    insert = next((p for s, p in conn.executed if "delist_flip_refusals" in s), None)
    assert insert is not None
    assert 29_400 in insert and 78_718 in insert


def test_small_categories_are_exempt() -> None:
    """2% of a 200-row category is 4, which ordinary churn would trip weekly. The
    cap polices catastrophes, not small categories."""
    conn = _Conn()
    assert _allowed(conn, candidates=50, active_rows=200) is True


def test_the_boundary_is_inclusive() -> None:
    conn = _Conn()
    assert _allowed(conn, candidates=200, active_rows=10_000) is True    # exactly 2%
    assert _allowed(conn, candidates=201, active_rows=10_000) is False


def test_the_cap_is_operator_tunable() -> None:
    conn = _Conn(setting={"fraction": 0.5, "min_rows": 100})
    assert _allowed(conn, candidates=4_000, active_rows=10_000) is True


def test_a_broken_setting_cannot_disarm_the_cap() -> None:
    """A knob that fails open is not a knob, it is a hole. Garbage in
    app_settings must fall back to the baked defaults, never to 'allow'."""
    conn = _Conn(setting={"fraction": "not-a-number"})
    assert _allowed(conn, candidates=29_400, active_rows=78_718) is False


def test_a_settings_read_failure_cannot_disarm_the_cap() -> None:
    class _Exploding(_Conn):
        def cursor(self):  # noqa: ANN201
            raise RuntimeError("pooler said no")

    assert _allowed(_Exploding(), candidates=29_400, active_rows=78_718) is False
