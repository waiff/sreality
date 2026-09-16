"""Every tunable of the offline engine in one frozen-by-default row (PROGRAM.md E17/E22/E34).

The W2 lane is pure Python over the exported cohort, but each default here is the local
twin of a column that `autodedup.settings` will carry in production, so a sweep is a JSON
file rather than an edit: `harness run --settings s.json`. `from_json` rejects unknown keys
on purpose — a typo in a sweep file must fail loudly, not silently run the defaults.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Settings:
    area_band_tol: float = 0.20
    area_reject_pct: float = 0.08
    area_band_pct: float = 0.03
    catalog_df: int = 8
    anchor_images: int = 3
    max_block_size: int = 200
    max_candidates_per_listing: int = 60
    phash_tight: int = 6
    phash_loose: int = 11
    band_bits: int = 16
    simhash_bands: int = 4
    text_min_chars: int = 200
    t_hi: float = 0.97
    t_lo: float = 0.30
    store_floor: float = 0.02
    cluster_area_spread: float = 0.08
    max_cluster_size: int = 8
    rare_token_df: int = 2
    # The two image-lane sample caps `features.py` reads: CLIP is the only non-popcount quadratic
    # in the pass, so both belong in the swept row rather than in a module constant.
    clip_sample: int = 8
    phash_sample: int = 30

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Every ordering a sweep can break, named — `--settings s.json` must fail at load."""
        if not 0.0 < self.area_band_tol < 1.0:
            raise ValueError(f"area_band_tol must be in (0, 1): {self.area_band_tol}")
        if not 0.0 <= self.area_band_pct <= self.area_reject_pct:
            raise ValueError(
                f"area_band_pct must be in [0, area_reject_pct]: {self.area_band_pct} "
                f"> {self.area_reject_pct}"
            )
        if not 0.0 <= self.t_lo <= self.t_hi <= 1.0:
            raise ValueError(f"thresholds must satisfy 0 <= t_lo <= t_hi <= 1: "
                             f"t_lo={self.t_lo} t_hi={self.t_hi}")
        if not 0.0 <= self.store_floor <= self.t_lo:
            raise ValueError(f"store_floor must be in [0, t_lo]: {self.store_floor} > {self.t_lo}")
        for name in ("catalog_df", "anchor_images", "max_block_size",
                     "max_candidates_per_listing", "band_bits", "simhash_bands",
                     "clip_sample", "phash_sample"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1: {getattr(self, name)}")
        if self.simhash_bands * self.band_bits > 64:
            raise ValueError(f"cannot split 64 bits into {self.simhash_bands} bands "
                             f"of {self.band_bits}")
        if self.phash_tight > self.phash_loose:
            raise ValueError(f"phash_tight must not exceed phash_loose: "
                             f"{self.phash_tight} > {self.phash_loose}")
        if self.max_cluster_size < 2:
            raise ValueError(f"max_cluster_size must be at least 2: {self.max_cluster_size}")
        if self.cluster_area_spread <= 0.0:
            raise ValueError(f"cluster_area_spread must be positive: {self.cluster_area_spread}")
        if self.rare_token_df < 0 or self.text_min_chars < 0:
            raise ValueError("rare_token_df and text_min_chars must not be negative")

    def band_width(self) -> float:
        """`w = -ln(1 - t)` — the log-band width lifted from `toolkit/dedup_candidates_sql.py`,
        which turns a ±t range tolerance into an equality lookup on `floor(ln(area)/w)`."""
        if not 0.0 < self.area_band_tol < 1.0:
            raise ValueError(f"area_band_tol must be in (0, 1): {self.area_band_tol}")
        return -math.log(1.0 - self.area_band_tol)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Settings":
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(raw) - known)
        if unknown:
            raise ValueError(f"unknown settings keys: {', '.join(unknown)}")
        return cls(**raw)

    @classmethod
    def from_json(cls, path: str | Path) -> "Settings":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
