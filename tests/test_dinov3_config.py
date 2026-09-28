"""The DINOv3 six-fact identity rail — data/dinov3_config.json + scraper/dinov3_config.py.

The rail is the point: a vector written under a guessed resolution or an unpinned
revision is silently incomparable with every other row in the table, and nothing at
runtime can detect it afterwards (docs/design/new-dedup/ENCODER-DECISION.md §4.1, and
the lesson migration 456 already paid for once). So every one of the six facts is
tested for individually — a validator that checks five of them is a hole that looks
like a rule.

Offline: no DB, no HF hub, no torch.
"""

from __future__ import annotations

import json
import re

import pytest

from scraper.dinov3_config import (
    IDENTITY_FIELDS,
    encoder_identity,
    load_dinov3_config,
    validate_config,
)

COMPLETE = {
    "model": "facebook/dinov3-vitb16-pretrain-lvd1689m",
    "revision": "0" * 40,
    "library": "transformers",
    "pooling": "cls",
    "resolution": 224,
    "preprocessing": "letterbox_pad",
    "dtype": "fp32",
}


def _write(tmp_path, config: dict):
    path = tmp_path / "dinov3_config.json"
    path.write_text(json.dumps(config))
    return path


# --- the shipped file --------------------------------------------------------


def test_shipped_config_is_the_active_tag_models_population():
    # The operator's 2026-09-05 ruling locked the model; the revision was pinned 2026-09-08.
    # Resolution, preprocessing and dtype are v1's own arm (bake-off run 1,
    # dinov3-b16@768/bf16, letterbox_pad): the corpus is embedded in the population the
    # active heads were trained on, never a plausible-looking guess (G4, 2026-09-27).
    config = load_dinov3_config()
    assert config["model"] == "facebook/dinov3-vitb16-pretrain-lvd1689m"
    assert config["library"] == "transformers"
    assert config["pooling"] == "cls"
    assert re.fullmatch(r"[0-9a-f]{40}", config["revision"]), (
        "revision must be a full HF commit sha, never a branch name like 'main'"
    )
    assert (config["resolution"], config["preprocessing"], config["dtype"]) == (
        768, "letterbox_pad", "bf16")


def test_shipped_config_loads_all_seven_facts():
    identity = encoder_identity()
    assert set(identity) == set(IDENTITY_FIELDS)


def test_shipped_config_documents_every_identity_field():
    config = load_dinov3_config()
    for field in IDENTITY_FIELDS:
        assert field in config, f"the config file has no {field} key at all"


# --- the validator -----------------------------------------------------------


def test_complete_config_validates_and_returns_only_the_identity():
    identity = validate_config({**COMPLETE, "_status": "noise", "extra": 1})
    assert identity == COMPLETE
    assert set(identity) == set(IDENTITY_FIELDS)


@pytest.mark.parametrize("field", IDENTITY_FIELDS)
def test_every_single_missing_field_is_refused(field):
    config = {k: v for k, v in COMPLETE.items() if k != field}
    with pytest.raises(RuntimeError) as exc:
        validate_config(config)
    assert field in str(exc.value)


@pytest.mark.parametrize("field", IDENTITY_FIELDS)
def test_every_single_null_field_is_refused(field):
    with pytest.raises(RuntimeError) as exc:
        validate_config({**COMPLETE, field: None})
    assert field in str(exc.value)


@pytest.mark.parametrize("field", IDENTITY_FIELDS)
def test_every_single_empty_field_is_refused(field):
    with pytest.raises(RuntimeError) as exc:
        validate_config({**COMPLETE, field: ""})
    assert field in str(exc.value)


def test_error_names_all_the_missing_fields_at_once():
    with pytest.raises(RuntimeError) as exc:
        validate_config({"model": COMPLETE["model"]})
    message = str(exc.value)
    for field in IDENTITY_FIELDS:
        if field != "model":
            assert field in message


def test_encoder_identity_reads_the_given_path(tmp_path):
    assert encoder_identity(_write(tmp_path, COMPLETE)) == COMPLETE
