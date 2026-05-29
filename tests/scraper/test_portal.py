"""Hermetic tests for scraper.portal: PortalConfig + the registry loader."""

from __future__ import annotations

from typing import Any

import pytest

from scraper.portal import PortalConfig, default_config, load_portal_config


class _Cur:
    def __init__(self, row: Any) -> None:
        self._row = row

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.sql = sql
        self.params = params

    def fetchone(self) -> Any:
        return self._row


class _Conn:
    def __init__(self, row: Any) -> None:
        self._row = row

    def cursor(self) -> _Cur:
        return _Cur(self._row)


def test_default_config_sreality():
    cfg = default_config("sreality")
    assert cfg.supports_complete_walk is True
    assert cfg.split_threshold == 10000
    assert cfg.splits is True
    assert len(cfg.categories) == 6
    assert {"category_main_cb": 1, "category_type_cb": 2} in cfg.categories


def test_default_config_bazos():
    cfg = default_config("bazos")
    assert cfg.supports_complete_walk is False
    assert cfg.split_threshold is None
    assert cfg.splits is False
    assert cfg.categories == [{"sale_type": "prodam", "category": "byt"}]


def test_default_config_idnes():
    cfg = default_config("idnes")
    assert cfg.supports_complete_walk is False
    assert cfg.split_threshold is None
    assert cfg.splits is False
    assert cfg.categories == [{"sale_type": "prodej", "category": "byty"}]


def test_default_config_unknown_raises():
    with pytest.raises(ValueError):
        default_config("nope")


def test_load_reads_db_row():
    row = (True, [{"category_main_cb": 9, "category_type_cb": 9}], 5000)
    cfg = load_portal_config(_Conn(row), "sreality")
    assert cfg.supports_complete_walk is True
    assert cfg.split_threshold == 5000
    assert cfg.categories == [{"category_main_cb": 9, "category_type_cb": 9}]


def test_load_missing_row_falls_back_to_default():
    cfg = load_portal_config(_Conn(None), "bazos")
    assert cfg == default_config("bazos")


def test_load_null_categories_falls_back_to_default_categories():
    row = (False, None, None)
    cfg = load_portal_config(_Conn(row), "sreality")
    assert cfg.categories == default_config("sreality").categories
    assert cfg.supports_complete_walk is False  # the row's value still wins


def test_portalconfig_splits_property():
    assert PortalConfig("x", True, [], split_threshold=1).splits is True
    assert PortalConfig("x", True, [], split_threshold=None).splits is False
