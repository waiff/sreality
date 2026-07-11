"""_select_pending: SET-LOCAL is a literal (not a bound param) + priority→global drain.

Regression guard for the SyntaxError that stalled every clip_tag shard: `SET` is a
PostgreSQL utility statement and cannot take a bound parameter ($1), so the timeout must
be interpolated, never passed as %s.
"""

from __future__ import annotations

from typing import Any

from scripts import clip_tag_backfill as ctb


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if s.startswith("SET LOCAL"):
            self._rows = []
        elif isinstance(params, dict) and "region" in params:
            self._rows = list(self._conn.region_rows.get(params["region"], []))
        elif "JOIN image_clip_tags" in s:  # the spare-capacity repair select
            self._rows = list(self._conn.repair_rows)
        elif "FROM images i" in s:  # the global fallback select
            self._rows = list(self._conn.global_rows)
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _Txn:
    def __enter__(self) -> "_Txn":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, region_rows: dict | None = None,
                 global_rows: list | None = None,
                 repair_rows: list | None = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.region_rows = region_rows or {}
        self.global_rows = global_rows or []
        self.repair_rows = repair_rows or []

    def transaction(self) -> "_Txn":
        return _Txn()

    def cursor(self) -> "_Cur":
        return _Cur(self)


def test_set_local_statement_timeout_is_literal_not_parameterized() -> None:
    conn = _Conn(global_rows=[(1, "k", True)])
    ctb._select_pending(conn, limit=10, shards=1, shard=0, priority_regions=[])
    set_stmts = [(s, p) for s, p in conn.executed if s.startswith("SET LOCAL")]
    # One per selection transaction: the fresh phases share one txn; the
    # spare-capacity repair phase runs in its OWN txn (a repair timeout must
    # skip repair, never abort the fresh work).
    assert len(set_stmts) == 2
    for sql, params in set_stmts:
        assert params is None  # SET can't bind a param
        assert "%s" not in sql and "$1" not in sql
        assert str(ctb.SELECT_TIMEOUT_MS) in sql


def test_repair_phase_fills_spare_capacity_only() -> None:
    # Fresh work first; the tagged-but-vectorless repair backlog fills what's left.
    conn = _Conn(global_rows=[(1, "a", True)], repair_rows=[(7, "r", False)])
    rows, phase = ctb._select_pending(
        conn, limit=10, shards=1, shard=0, priority_regions=[])
    assert [r[0] for r in rows] == [1, 7]
    assert "global:1" in phase and "repair:1" in phase


def test_repair_phase_skipped_when_budget_filled() -> None:
    conn = _Conn(global_rows=[(1, "a", True), (2, "b", True)],
                 repair_rows=[(7, "r", False)])
    rows, phase = ctb._select_pending(
        conn, limit=2, shards=1, shard=0, priority_regions=[])
    assert [r[0] for r in rows] == [1, 2]
    assert "repair" not in phase


def test_repair_phase_timeout_does_not_abort_fresh_work() -> None:
    # A repair-select failure (e.g. statement_timeout on the drained anti-join)
    # must be swallowed — the fresh rows still return.
    class _RepairBoom(_Conn):
        def cursor(self) -> "_Cur":
            cur = _Cur(self)
            orig = cur.execute

            def execute(sql: str, params: Any = None) -> None:
                if "JOIN image_clip_tags" in sql:
                    raise RuntimeError("canceling statement due to statement timeout")
                orig(sql, params)

            cur.execute = execute  # type: ignore[method-assign]
            return cur

    conn = _RepairBoom(global_rows=[(1, "a", True)])
    rows, phase = ctb._select_pending(
        conn, limit=10, shards=1, shard=0, priority_regions=[])
    assert [r[0] for r in rows] == [1]
    assert "repair" not in phase


def test_drains_priority_region_then_global() -> None:
    conn = _Conn(
        region_rows={19: [(1, "a", True), (2, "b", False)]},
        global_rows=[(3, "c", True)],
    )
    rows, phase = ctb._select_pending(
        conn, limit=10, shards=1, shard=0, priority_regions=[19])
    assert [r[0] for r in rows] == [1, 2, 3]  # region 19 first, then global
    assert "r19:2" in phase and "global:1" in phase


def test_priority_filling_budget_skips_global() -> None:
    conn = _Conn(
        region_rows={19: [(1, "a", True), (2, "b", True)]},
        global_rows=[(9, "x", True)],
    )
    rows, phase = ctb._select_pending(
        conn, limit=2, shards=1, shard=0, priority_regions=[19])
    assert [r[0] for r in rows] == [1, 2]
    assert "global" not in phase  # budget filled by the priority region


def test_region_and_global_overlap_is_deduped() -> None:
    # The global fallback has no region exclusion, so it can re-emit a priority-region
    # image — _select_pending must de-dup so it isn't processed twice in one run.
    conn = _Conn(
        region_rows={19: [(1, "a", True), (2, "b", True)]},
        global_rows=[(2, "b", True), (3, "c", True)],
    )
    rows, _ = ctb._select_pending(
        conn, limit=10, shards=1, shard=0, priority_regions=[19])
    assert [r[0] for r in rows] == [1, 2, 3]  # image 2 appears once


class _FakeR2:
    def __init__(self, data: dict) -> None:
        self._data = data

    def download_bytes(self, key: str) -> bytes:
        v = self._data[key]
        if isinstance(v, Exception):
            raise v
        return v


def test_download_decode_marks_terminal_keeps_transient_retryable() -> None:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="PNG")
    good = buf.getvalue()
    r2 = _FakeR2({"good": good, "bad": b"not-an-image",
                  "gone": RuntimeError("R2 blip")})
    decoded, terminal = ctb._download_decode(
        r2, [(1, "good"), (2, "bad"), (3, "gone")], workers=2)
    assert {d[0] for d in decoded} == {1}      # decoded successfully
    assert set(terminal) == {2}                # corrupt bytes -> terminal (gets marked)
    # the download exception (3) is transient -> neither decoded nor terminal -> retries
    assert 3 not in {d[0] for d in decoded} and 3 not in terminal
