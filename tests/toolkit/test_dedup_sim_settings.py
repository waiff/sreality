"""Registry invariants + override CRUD for the NEW DEDUP settings registry.
Hermetic fake conn — no DB (migration 372 is verified separately, live)."""

from __future__ import annotations

from typing import Any

import pytest

from toolkit import dedup_sim_settings as dss


# --- registry shape invariants ---------------------------------------------


def test_registry_is_nonempty() -> None:
    assert len(dss.REGISTRY) > 0


def test_registry_keys_match_setting_keys() -> None:
    for key, d in dss.REGISTRY.items():
        assert key == d.key, f"registry key {key!r} != SettingDef.key {d.key!r}"


def test_every_setting_has_a_nonempty_explanation() -> None:
    for d in dss.REGISTRY.values():
        assert d.explanation and len(d.explanation) > 20, (
            f"{d.key} has no meaningful plain-language blurb"
        )


def test_every_default_passes_its_own_validation() -> None:
    for d in dss.REGISTRY.values():
        dss._validate(d, d.default)  # raises on failure


def test_enum_defaults_are_in_their_own_choices() -> None:
    for d in dss.REGISTRY.values():
        if d.enum_choices is not None:
            assert d.default in d.enum_choices


# --- effective value / settings, conn=None (no DB) --------------------------


def test_effective_value_returns_default_with_no_conn() -> None:
    for key, d in dss.REGISTRY.items():
        assert dss.effective_value(key, conn=None) == d.default


def test_effective_value_unknown_key_raises() -> None:
    with pytest.raises(KeyError):
        dss.effective_value("not_a_real_setting", conn=None)


def test_effective_settings_covers_every_registry_key() -> None:
    effective = dss.effective_settings(conn=None)
    assert set(effective) == set(dss.REGISTRY)
    for key, d in dss.REGISTRY.items():
        assert effective[key] == d.default


def test_list_with_metadata_shape() -> None:
    rows = dss.list_with_metadata(conn=None)
    assert len(rows) == len(dss.REGISTRY)
    for row in rows:
        assert row["is_override"] is False
        assert row["value"] == row["default"]
        assert row["explanation"] == dss.REGISTRY[row["key"]].explanation


# --- validation -------------------------------------------------------------


def test_validate_rejects_wrong_type() -> None:
    with pytest.raises(dss.SettingValidationError):
        dss._validate(dss.REGISTRY["l0_geo_radius_m"], "not a number")


def test_validate_rejects_bool_for_numeric() -> None:
    with pytest.raises(dss.SettingValidationError):
        dss._validate(dss.REGISTRY["l0_geo_radius_m"], True)


def test_validate_rejects_out_of_range() -> None:
    with pytest.raises(dss.SettingValidationError):
        dss._validate(dss.REGISTRY["l2_phash_hamming_threshold"], 999)


def test_validate_rejects_non_integer_for_integer_type() -> None:
    with pytest.raises(dss.SettingValidationError):
        dss._validate(dss.REGISTRY["l0_floor_tolerance"], 2.5)


def test_validate_rejects_bad_enum_choice() -> None:
    with pytest.raises(dss.SettingValidationError):
        dss._validate(dss.REGISTRY["l2_phash_family_semantics"], "bogus")


def test_validate_accepts_good_boolean() -> None:
    dss._validate(dss.REGISTRY["l1_exact_attrs_enabled"], True)


# --- override CRUD, fake conn ------------------------------------------------


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if s.startswith("SELECT key, value FROM dedup_sim.settings"):
            self._rows = list(self._conn.table.items())
        elif s.startswith("INSERT INTO dedup_sim.settings"):
            key, value_json, updated_by = params
            self._conn.table[key] = _loads(value_json)
        elif s.startswith("DELETE FROM dedup_sim.settings"):
            (key,) = params
            self._conn.table.pop(key, None)

    def fetchall(self) -> list[tuple[Any, Any]]:
        return getattr(self, "_rows", [])


class _Txn:
    def __enter__(self) -> "_Txn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self) -> None:
        self.table: dict[str, Any] = {}
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Txn:
        return _Txn()


def _loads(value_json: str) -> Any:
    import json
    return json.loads(value_json)


def test_update_setting_then_effective_value_reflects_override() -> None:
    conn = _FakeConn()
    dss.update_setting(conn, "l2_phash_hamming_threshold", 8, updated_by="test")
    assert dss.effective_value("l2_phash_hamming_threshold", conn=conn) == 8
    # Untouched settings still fall back to their registry default.
    assert dss.effective_value("l0_geo_radius_m", conn=conn) == 75


def test_update_setting_validates_before_writing() -> None:
    conn = _FakeConn()
    with pytest.raises(dss.SettingValidationError):
        dss.update_setting(conn, "l2_phash_hamming_threshold", -1, updated_by="test")
    assert conn.table == {}


def test_update_setting_unknown_key_raises() -> None:
    conn = _FakeConn()
    with pytest.raises(KeyError):
        dss.update_setting(conn, "not_a_real_setting", 1, updated_by="test")


def test_reset_setting_reverts_to_default() -> None:
    conn = _FakeConn()
    dss.update_setting(conn, "l0_floor_tolerance", 3, updated_by="test")
    assert dss.effective_value("l0_floor_tolerance", conn=conn) == 3
    dss.reset_setting(conn, "l0_floor_tolerance")
    assert dss.effective_value("l0_floor_tolerance", conn=conn) == 2


def test_reset_setting_unknown_key_raises() -> None:
    conn = _FakeConn()
    with pytest.raises(KeyError):
        dss.reset_setting(conn, "not_a_real_setting")
