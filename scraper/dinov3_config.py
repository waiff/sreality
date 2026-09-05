"""The DINOv3 encoder identity — one JSON file, one loader, one refuse-to-run rail.

A vector's identity is SIX facts, not one: model, revision (the HF commit sha),
library, pooling, resolution, preprocessing and dtype. Any of them changing means a
NEW POPULATION, not a new value (docs/design/new-dedup/ENCODER-DECISION.md §4.1),
which is why all seven columns sit in `image_dinov3_embeddings`' primary key.

`data/dinov3_config.json` is the single source of truth for those facts — the same
role `data/clip_taxonomy.json` plays for CLIP — and this module is the only reader.
It ships with `revision`/`resolution`/`preprocessing`/`dtype` deliberately NULL,
because they are gated on the bake-off (§5) and the operator's licence acceptance;
`validate_config` therefore refuses every load until they are filled in. That refusal
is the rail, not a bug: `scraper/clip_tagger.py` already enforces the same thing for
the revision alone, and this is the lesson migration 456 paid for once.

No torch, no transformers, no DB — importable anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "dinov3_config.json"

# The six facts, in the order ENCODER-DECISION §4.1 states them. Every one is part of
# the vector's identity and part of the target table's primary key.
IDENTITY_FIELDS: tuple[str, ...] = (
    "model",
    "revision",
    "library",
    "pooling",
    "resolution",
    "preprocessing",
    "dtype",
)

_DOC = "docs/design/new-dedup/ENCODER-DECISION.md"


def load_dinov3_config(path: Path | None = None) -> dict[str, Any]:
    """The raw config document. Unvalidated on purpose — a --help or a lint may want
    to read it while it is still provisional."""
    return json.loads((path or _CONFIG_PATH).read_text())


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Raise unless all six identity facts are set. Returns the identity subset.

    A missing fact is not a default to be guessed: a vector written under a guessed
    resolution or an unpinned revision is silently incomparable with every other
    vector in the table, and nothing at runtime can detect it afterwards.
    """
    missing = [
        field
        for field in IDENTITY_FIELDS
        if config.get(field) is None or config.get(field) == ""
    ]
    if missing:
        raise RuntimeError(
            "data/dinov3_config.json is incomplete — unset: "
            + ", ".join(missing)
            + ". A vector's identity is all six facts (model, revision, "
            "library, pooling, resolution, preprocessing, dtype) — refusing to embed "
            f"against an under-specified encoder. See {_DOC} §3.2/§5: these are set by "
            "the bake-off and the operator's licence acceptance, not by a default."
        )
    return {field: config[field] for field in IDENTITY_FIELDS}


def encoder_identity(path: Path | None = None) -> dict[str, Any]:
    """The validated six-fact identity — the params every read and write of
    `image_dinov3_embeddings` binds. The one chokepoint; never a second hardcoded copy."""
    return validate_config(load_dinov3_config(path))
