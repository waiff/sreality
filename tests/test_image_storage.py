"""Tests for image_storage helpers (key generation, env-var validation)."""

from __future__ import annotations

import pytest

from scraper.image_storage import R2_ENV_VARS, image_key, is_configured


def test_image_key_pads_sequence():
    assert image_key(2836292428, 1) == "2836292428/0001.jpg"
    assert image_key(2836292428, 19) == "2836292428/0019.jpg"
    assert image_key(2836292428, 1234) == "2836292428/1234.jpg"


def test_image_key_handles_missing_sequence():
    assert image_key(123, None) == "123/0000.jpg"


def test_is_configured_requires_all_vars(monkeypatch):
    for name in R2_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert is_configured() is False

    for name in R2_ENV_VARS[:-1]:
        monkeypatch.setenv(name, "x")
    assert is_configured() is False

    monkeypatch.setenv(R2_ENV_VARS[-1], "x")
    assert is_configured() is True


def test_from_env_raises_when_missing(monkeypatch):
    from scraper.image_storage import R2Client

    for name in R2_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="R2_ACCOUNT_ID"):
        R2Client.from_env()
