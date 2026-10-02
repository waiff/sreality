"""Every is_active write site must maintain listings.inactive_at (migration 175).

Flips to false stamp inactive_at = now(); reactivations clear it to NULL —
otherwise the delisting-latency health check (migration 176) reads garbage.
Hermetic: a scripted fake conn records every executed statement so the tests
assert the SQL text, same pattern as test_db_property.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from scraper import db


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        for predicate, rows in self._conn.script:
            if predicate(s):
                self._rows = list(rows)
                self.rowcount = len(rows)
                return
        self._rows = []
        self.rowcount = 0

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, script: list[tuple[Any, list[tuple[Any, ...]]]] | None = None) -> None:
        self.script = script or []
        self.executed: list[tuple[str, Any]] = []

    def transaction(self) -> _Ctx:
        return _Ctx()

    def cursor(self) -> _Cur:
        return _Cur(self)


def _find(executions, needle: str) -> tuple[str, Any] | None:
    return next((e for e in executions if needle in e[0]), None)


# --- flips to false stamp the delisting moment -----------------------------


def test_mark_listing_inactive_stamps_inactive_at_once():
    conn = _FakeConn()
    db.mark_listing_inactive(conn, "bazos", "12345")
    sql, params = _find(conn.executed, "SET is_active = false")
    assert "SET is_active = false, inactive_at = now()" in sql
    # Guarded: an already-inactive row keeps its first inactive_at.
    assert "WHERE source = %s AND source_id_native = %s AND is_active = true" in sql
    assert params == ("bazos", "12345")
    assert "last_seen_at" not in sql                    # rule #4: a flip never touches it


_FLIP_RE = re.compile(r"UPDATE\s+listings\b[^;]*?SET\s+is_active\s*=\s*false", re.S | re.I)


def test_mark_listing_inactive_is_the_only_flip_writer():
    """Rule #3: one writer flips a listing inactive, so the guard, the inactive_at
    stamp, the dirty mark and the failure-ledger clear cannot drift between paths."""
    root = Path(__file__).resolve().parent.parent
    hits = []
    for top in ("scraper", "api", "toolkit", "scripts", "autodedup", "location_data"):
        for path in sorted((root / top).rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for m in _FLIP_RE.finditer(text):
                hits.append((path.relative_to(root).as_posix(), text.count("\n", 0, m.start()) + 1))
    assert len(hits) == 1, hits
    path, line = hits[0]
    assert path == "scraper/db.py"
    src = (root / path).read_text(encoding="utf-8").splitlines()
    owner = next(l for l in reversed(src[:line]) if l.startswith("def "))
    assert owner.startswith("def mark_listing_inactive(")


# --- reactivations clear the stamp ------------------------------------------


def test_touch_listings_clears_inactive_at_in_both_statements():
    conn = _FakeConn()
    db.touch_listings(conn, [1, 2])
    react = _find(conn.executed, "WITH react AS")
    assert react is not None and "inactive_at = NULL" in react[0]
    bulk = _find(conn.executed, "SET last_seen_at = now(), is_active = true")
    assert bulk is not None and "inactive_at = NULL" in bulk[0]
