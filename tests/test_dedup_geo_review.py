"""Tests for the geo-review backend enablers: the per-tier facet in summary() and
the scoped bulk-approve (bulk_merge_candidates). Hermetic — scripted fake conn / monkeypatch.
"""

from __future__ import annotations

from typing import Any

import api.property_dedup as dedup
from toolkit.property_identity import MergeError


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        if "GROUP BY 1, 2" in s:
            self._rows = list(self._conn.bucket_rows)
        elif "GROUP BY c.tier" in s:
            self._rows = list(self._conn.tier_rows)
        else:
            self._rows = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, *, bucket_rows: Any = None, tier_rows: Any = None) -> None:
        self.bucket_rows = bucket_rows or []
        self.tier_rows = tier_rows or []

    def cursor(self) -> _Cur:
        return _Cur(self)


def test_summary_includes_per_tier_facet() -> None:
    conn = _FakeConn(
        bucket_rows=[("geo_strong", None, 100), ("no_images", None, 50)],
        tier_rows=[("geo_dum", 80, 60), ("street_disposition", 70, 0)],
    )
    out = dedup.summary(conn)["data"]
    assert out["total"] == 150  # total still derives from the reason buckets
    assert {"tier": "geo_dum", "count": 80, "strong": 60} in out["tiers"]
    assert {"tier": "street_disposition", "count": 70, "strong": 0} in out["tiers"]


def test_bulk_merge_counts_merged_and_skipped(monkeypatch: Any) -> None:
    calls: list[int] = []

    def fake_merge(conn: Any, cid: int) -> dict[str, Any] | None:
        calls.append(cid)
        if cid == 2:
            raise MergeError("retired is not active")  # conflict → skip, not fatal
        if cid == 3:
            return None  # 404 / gone → skip
        return {"data": {"merge_group_id": f"g{cid}"}}

    monkeypatch.setattr(dedup, "merge_candidate", fake_merge)
    out = dedup.bulk_merge_candidates(object(), [1, 2, 3, 4])["data"]
    assert out["merged"] == 2          # 1 and 4
    assert out["skipped"] == 2         # 2 (conflict) and 3 (gone)
    assert out["merge_group_ids"] == ["g1", "g4"]
    assert calls == [1, 2, 3, 4]       # every id attempted; one failure never aborts


def test_bulk_merge_empty_is_noop(monkeypatch: Any) -> None:
    monkeypatch.setattr(dedup, "merge_candidate",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")))
    out = dedup.bulk_merge_candidates(object(), [])["data"]
    assert out == {"merged": 0, "skipped": 0, "merge_group_ids": []}
