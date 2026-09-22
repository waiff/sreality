"""The clear never blanks a cell it has not first copied into the backup table."""

from __future__ import annotations

from typing import Any

import pytest

from scripts import clear_unmeasured_enrichment_fills as clr


class _Cur:
    def __init__(self, log: list[tuple[str, Any]], pages: list[list[int]]) -> None:
        self._log, self._pages = log, pages

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._log.append((sql, params))

    def fetchall(self) -> list[tuple[int]]:
        page = self._pages.pop(0) if self._pages else []
        return [(i,) for i in page]


class _Conn:
    def __init__(self, pages: list[list[int]]) -> None:
        self.log: list[tuple[str, Any]] = []
        self._pages = pages

    def cursor(self) -> _Cur:
        return _Cur(self.log, self._pages)


def test_apply_without_a_backup_table_is_refused() -> None:
    with pytest.raises(ValueError):
        clr.run(_Conn([[1, 2]]), ["floor"], apply=True, page=2)


def test_every_page_is_backed_up_before_it_is_cleared() -> None:
    conn = _Conn([[1, 2], [3]])
    counts = clr.run(conn, ["floor"], apply=True, page=2, backup_table="bk")
    assert counts == {"floor": 3}
    writes = [(sql, p) for sql, p in conn.log if "INSERT INTO bk" in sql or "UPDATE listings" in sql]
    kinds = ["backup" if "INSERT INTO bk" in sql else "clear" for sql, _ in writes]
    assert kinds == ["backup", "clear", "backup", "clear"]
    assert writes[0][1] == {"column": "floor", "ids": [1, 2]}
    assert writes[1][1] == {"ids": [1, 2]}


def test_a_dry_run_writes_nothing() -> None:
    conn = _Conn([[1, 2]])
    assert clr.run(conn, ["has_lift"], apply=False, page=10) == {"has_lift": 2}
    assert not [s for s, _ in conn.log if "INSERT" in s or "UPDATE" in s]


def test_the_backup_table_name_must_be_a_plain_identifier() -> None:
    with pytest.raises(ValueError):
        clr.create_backup_table(_Conn([]), "listings; drop table listings")
