"""Hermetic tests for the operator-editable tag-priority DB glue (toolkit.dedup_priorities)."""

from __future__ import annotations

from typing import Any

import pytest

from toolkit.dedup_priorities import (
    load_tag_priority_overrides,
    priorities_view,
    set_family_priority,
)
from toolkit.room_taxonomy import HOUSE_PRIORITY, INTERIOR_PRIORITY, LAND_PRIORITY


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._row: tuple[Any, ...] | None = None

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        if s.startswith("SELECT value FROM app_settings"):
            self._row = (self._conn.value,) if self._conn.value is not None else None
        elif s.startswith("INSERT INTO app_settings"):
            self._conn.written = params  # (key, Jsonb(blob), desc, updated_by)
            self._row = None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self, value: Any = None) -> None:
        self.value = value          # the stored app_settings JSON blob
        self.written: Any = None

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Ctx:
        return _Ctx()


def test_load_overrides_validates_normalizes_and_ignores_junk() -> None:
    conn = _FakeConn({
        "dum": ["kitchen", "exterior_facade"],   # valid reorder
        "byt": ["nonsense_tag"],                  # all-unknown → default
        "bogus_family": ["kitchen"],              # unknown family → ignored
    })
    out = load_tag_priority_overrides(conn)
    assert out["dum"][0] == "kitchen" and set(out["dum"]) == set(HOUSE_PRIORITY)
    assert out["byt"] == list(INTERIOR_PRIORITY)
    assert "bogus_family" not in out


def test_load_overrides_empty_when_absent_or_malformed() -> None:
    assert load_tag_priority_overrides(_FakeConn(None)) == {}
    assert load_tag_priority_overrides(_FakeConn(["not", "a", "dict"])) == {}


def test_priorities_view_reports_defaults_and_edits() -> None:
    view = {v["family"]: v for v in priorities_view(_FakeConn({"pozemek": ["garden", "site_plan"]}))}
    assert len(view) == 5
    assert view["byt"]["is_default"] is True
    assert view["byt"]["order"] == list(INTERIOR_PRIORITY)
    assert view["pozemek"]["is_default"] is False
    assert view["pozemek"]["order"][0] == "garden"
    assert view["pozemek"]["default_order"] == list(LAND_PRIORITY)


def test_set_family_priority_persists_normalized_and_merges_blob() -> None:
    import json

    conn = _FakeConn({"byt": ["bathroom", "kitchen"]})  # an existing edit on another family
    stored = set_family_priority(conn, "dum", ["garden", "kitchen", "zzz"])
    assert stored[0] == "garden" and set(stored) == set(HOUSE_PRIORITY)  # completed, junk dropped
    # the written blob (json.dumps'd) keeps byt AND adds dum (other families untouched).
    written_blob = json.loads(conn.written[1])  # params = (key, json_str, desc, updated_by)
    assert "byt" in written_blob and "dum" in written_blob
    assert written_blob["dum"] == stored


def test_set_family_priority_rejects_unknown_family() -> None:
    with pytest.raises(ValueError):
        set_family_priority(_FakeConn(None), "garaz", ["kitchen"])
