"""Every tunable of the offline engine in one frozen-by-default row (PROGRAM.md E17/E22/E34).

The W2 lane is pure Python over the exported cohort, but each default here is the local
twin of a column that `autodedup.settings` will carry in production, so a sweep is a JSON
file rather than an edit: `harness run --settings s.json`. `from_json` rejects unknown keys
on purpose — a typo in a sweep file must fail loudly, not silently run the defaults.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from autodedup.features import DEFAULT_VOCABULARY_ATTR_KEYS


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
    # E45/E46/W4c (gold eval 2026-09-16). `price_unit` contradicts on 55% of CROSS-portal TRUE
    # duplicates (221/401) and 0% of same-portal ones (1/238) because the portals spell one fact
    # two ways (`celkem|mesic` on sreality/bezrealitky, `za nemovitost|za mesic` elsewhere);
    # `area_basis` contradicts on 31% of cross-portal positives against 14% of negatives. Both are
    # portal VOCABULARY, not property facts, so neither may contradict nor agree.
    vocabulary_attr_keys: tuple[str, ...] = DEFAULT_VOCABULARY_ATTR_KEYS
    # Only identity slots may raise `numeral_conflict` — a price cut (kc) or a rounded area (m2)
    # is what an E6 re-listing looks like, not a different unit.
    numeral_conflict_units: tuple[str, ...] = ("floor", "rooms", "unit")
    max_attr_contradictions: float = 3.0
    # K-A merged at 52.6% HT precision (n=98) on the gold holdout — structurally it certifies a
    # BUILDING (one RUIAN point + disposition + area), which is exactly the developer-unit
    # false-merge shape. Kept as code so an evaluation can switch it back on.
    certificate_ka_enabled: bool = False
    # E45: the merge zone needs one UNIT-specific corroboration. K-A + interior_match_ratio >= 0.5
    # scored 88.6% (n=35) against K-A's 52.6% overall; K-B (the disjoint-window re-post shape)
    # scored 100% (n=94).
    unit_evidence_required: bool = True
    unit_interior_min: float = 0.50
    unit_rare_tokens_min: float = 2.0
    unit_containment_min: float = 0.90
    # OFF by measurement: once E46 is widened below, every merge resting on rare tokens ALONE is
    # a judged positive (19/19, HT 100%) — the 82% that argued for a support clause was the two
    # catalogue-only CROSS-portal pairs E46 now bands. Requiring a photo beside the tokens cost
    # 1.9 points of weighted recall for -0.06 of precision, so the clause stays as a dial.
    unit_rare_requires_support: bool = False
    unit_rare_support_interior_min: float = 0.01
    # E46: every hand-checked K-A false merge was a co-live advert pair whose only image
    # agreement was catalogue material (n_images_min 0, catalog_ratio_max ~1.0). The
    # same-portal/one-broker clauses are a TIGHTENER, not a requirement — the same shape appears
    # across two portals (83202 x 10515714, 83467 x 10515837, both gold not-same).
    developer_signature_guard: bool = True
    developer_signature_same_broker_only: bool = False
    developer_catalog_ratio_min: float = 0.80
    # E47: two adverts of one broker on one portal that ran side by side for WEEKS are two units
    # of one project until a shared unit number says otherwise — no image or text rule can
    # separate units photographed together (21000 x 27781 merged as K-C at interior 1.0).
    # Swept 0/3/7/14/30/60/90 days: precision is flat at 98.06% across the range and recall rises
    # with the bar, so the default sits at the wide end that still clears the 61.6-day case.
    developer_colive_guard: bool = True
    colive_overlap_days: float = 30.0
    # W8: read an advert's live window as ending at its last SIGHTING rather than at the
    # delisting-DETECTION stamp (`dataset.live_end_stamp`). True is the honest clock and the one
    # the benchmark always uses; the engine defaults to False because the truer clock is the
    # looser one for every co-live test it feeds — see `features.window_end_stamp` for the
    # measurement and the refit debt.
    live_window_from_sighting: bool = False
    # E48: the per-stratum merge switch D3 asks for. A key is `<layer>|<side>` spelled exactly
    # as `evaluate.decide_stratum` spells it (K-A/K-B/K-C/model x same/cross); the value is that
    # stratum's own `t_hi`, and NULL is not "missing" but PROPOSE-ONLY — the stratum could not
    # prove the bar, so nothing in it auto-merges however high it scores or whichever certificate
    # it earned. A stratum with no entry runs on the global `t_hi`. This is the only way to ship
    # K-C|same propose-only (94.7% dev / 85.7% sealed, under the 0.97 floor) while K-B and
    # K-C|cross keep merging: certificates are read BEFORE `t_hi`, so a global cut cannot reach
    # them.
    t_hi_by_stratum: dict[str, float | None] = field(default_factory=dict)
    # E57: a refused BRIDGE (E37) is re-offered once, and applied only when the MERGED member
    # set satisfies every invariant — must-not-link included — and the edge is a certificate or
    # scores at least `bridge_min_score`. Default OFF, so E37 stands wherever a settings row does
    # not ask for it.
    bridge_apply: bool = False
    bridge_min_score: float = 0.999
    # The two image-lane sample caps `features.py` reads: CLIP is the only non-popcount quadratic
    # in the pass, so both belong in the swept row rather than in a module constant.
    clip_sample: int = 8
    phash_sample: int = 30

    def __post_init__(self) -> None:
        # A sweep file is JSON, so a tuple field arrives as a list: normalise before validating.
        self.vocabulary_attr_keys = tuple(str(key) for key in self.vocabulary_attr_keys)
        self.numeral_conflict_units = tuple(str(unit) for unit in self.numeral_conflict_units)
        self.t_hi_by_stratum = {
            str(key): (None if value is None else float(value))
            for key, value in dict(self.t_hi_by_stratum).items()
        }
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
        from autodedup.features import ATTR_KEYS, CONFLATED_ATTR_KEYS, NUMERAL_TOLERANCE

        unknown_attrs = sorted(
            set(self.vocabulary_attr_keys) - set(ATTR_KEYS) - set(CONFLATED_ATTR_KEYS)
        )
        if unknown_attrs:
            raise ValueError(f"vocabulary_attr_keys names no attribute: {', '.join(unknown_attrs)}")
        unknown_units = sorted(set(self.numeral_conflict_units) - set(NUMERAL_TOLERANCE))
        if unknown_units:
            raise ValueError(f"numeral_conflict_units names no slot: {', '.join(unknown_units)}")
        if not 0.0 <= self.unit_interior_min <= 1.0:
            raise ValueError(f"unit_interior_min must be in [0, 1]: {self.unit_interior_min}")
        if not 0.0 <= self.unit_containment_min <= 1.0:
            raise ValueError(f"unit_containment_min must be in [0, 1]: {self.unit_containment_min}")
        if not 0.0 <= self.developer_catalog_ratio_min <= 1.0:
            raise ValueError(
                f"developer_catalog_ratio_min must be in [0, 1]: {self.developer_catalog_ratio_min}"
            )
        if not 0.0 <= self.unit_rare_support_interior_min <= 1.0:
            raise ValueError(
                f"unit_rare_support_interior_min must be in [0, 1]: "
                f"{self.unit_rare_support_interior_min}"
            )
        if self.colive_overlap_days < 0.0:
            raise ValueError(
                f"colive_overlap_days must not be negative: {self.colive_overlap_days}"
            )
        if self.unit_rare_tokens_min < 0.0:
            raise ValueError(
                f"unit_rare_tokens_min must not be negative: {self.unit_rare_tokens_min}"
            )
        from autodedup.decide import CERTIFICATES, SIDES

        layers = (*CERTIFICATES, "model")
        for key, value in self.t_hi_by_stratum.items():
            layer, _, side = key.partition("|")
            if layer not in layers or side not in SIDES:
                raise ValueError(
                    f"t_hi_by_stratum key must be <layer>|<side> with layer in "
                    f"{layers} and side in {SIDES}: {key}"
                )
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"t_hi_by_stratum[{key}] must be in [0, 1] or null: {value}")
        if not 0.0 <= self.bridge_min_score <= 1.0:
            raise ValueError(f"bridge_min_score must be in [0, 1]: {self.bridge_min_score}")
        if self.max_attr_contradictions <= 0.0:
            raise ValueError(
                f"max_attr_contradictions must be positive: {self.max_attr_contradictions}"
            )

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
