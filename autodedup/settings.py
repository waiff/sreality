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

# C2 of the W8 verification: 21 of the 23 pairs an interior ratio alone would have carried
# rested on three photos or fewer, so an interior arm carries a floor no settings row may
# lower.
CONTEXT_RULE_MIN_IMAGES_FLOOR: float = 4.0

# E84: one minute, the floor under K-B's window separation. No scrape cadence in this program
# re-lists an advert within a minute of its last sighting, so the bar can only catch two adverts
# the same index walk saw for the first and only time.
MIN_CERTIFICATE_B_GAP_DAYS: float = 1.0 / 1440.0


@dataclass(slots=True)
class Settings:
    area_band_tol: float = 0.20
    area_reject_pct: float = 0.08
    area_band_pct: float = 0.03
    catalog_df: int = 8
    # E83 (W10): E9's population test cannot tell a marketing catalogue from a broker re-posting
    # ONE advert nine times, so a frame is stock only when its carriers are several PARTIES.
    # Each limb is a settings row because each costs differently; `combine` says whether one limb
    # clearing its bar is enough to call a frame stock (`any`, the conservative reading — more
    # frames stay subtracted) or every configured limb must (`all`).
    # `catalog_carrier_coverage_min` is the honest rail: carrier identity is observable only
    # inside the cohort while `pop` is corpus-wide, so a frame the cohort holds less than this
    # share of the population of stays subtracted. OFF by default — E9 is what g6 shipped with,
    # and every relaxation of it adds image evidence and therefore adds merges.
    catalog_carrier_aware: bool = False
    catalog_min_broker_carriers: int | None = None
    catalog_min_source_carriers: int | None = None
    catalog_min_block_carriers: int | None = None
    catalog_carrier_combine: str = "any"
    catalog_carrier_coverage_min: float = 1.0
    # E83's purity rail, E60's restated for a photograph: carriers that print two dispositions
    # or stated areas further apart than this hold the PROJECT's material, not one advert's.
    # 0.01 is K-B's own `CERT_B_AREA` — the certificate already demands the two sides agree
    # within 1 %, so the frame that carries it may not span more.
    catalog_carrier_area_tol: float = 0.01
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
    # E60 (W8): two adverts printing the same rare, pure agency order code are one order.
    # ON by default — 0 operator and 0 gold false merges on g5, and it is the only certificate
    # resting on a fact the broker states rather than on a resemblance.
    certificate_kr_enabled: bool = True
    # E65 (W9): K-B may not certify a pair whose photographs the engine never compared. The
    # floor is read on both SIDES (`n_images_min`, E9-subtracted, so an all-catalogue gallery
    # counts as zero) and on the MATCHED set (`phash_loose_matches`); an absent match count
    # fails it, because nothing to compare is not evidence of agreement. 0 disables a limb.
    # Measured on the g6 cohort: under the DETECTION clock a floor of (1, 1) withholds 982 of
    # 1,110 K-B merges carrying 169 reliable labelled duplicates and 0 labelled negatives, so
    # the default is OFF — the floor is the HONEST clock's price, not a free tightening, and
    # `validate` refuses to run the honest clock without it.
    # E110 (W13): `validate` no longer refuses the honest clock without this floor. The floor's
    # whole case was the two gold negatives of the ceskereality Rezidence K Botici families,
    # and on 2026-09-20 the operator ruled both pairs `same`; the price the honest clock pays
    # is now E84 alone. The rows stay live and `certificate_b` still enforces them, so
    # restoring the W9 arm is one number.
    certificate_b_min_images: float = 0.0
    certificate_b_min_matched_images: float = 0.0
    # E84 (W10): K-B's disjointness clause reads a PAIR of windows, so it has to be read at the
    # resolution the sightings have. Under the honest clock an advert seen exactly once has
    # `first_seen == last_seen` and its live window is a POINT, which makes `max(starts) >
    # min(ends)` true for ANY two once-seen adverts — two Regus products listed 2.8 seconds apart
    # in one index walk certified as a re-post (412540 x 412544, gold not-same, unanimous). The
    # separation must therefore exceed the sighting cadence, and the rail reads the pair, never
    # one side: "either side degenerate" costs 7 labelled duplicates, the pair gap costs 0. One
    # minute is measured: engine-wide over the candidate's 1,167 K-B merges it withholds exactly
    # that pair; an hour costs 2 labelled duplicates, a day 95 (M89). 0 by DEFAULT, and that is
    # not timidity: on the detection clock the gap runs from `inactive_at`, so a sub-minute one
    # is a delisting DETECTED and the successor listed in the same drain pass — a true re-post,
    # and the minute withholds exactly one of g6's K-B certificates, 18715382 x 18739520, the
    # same broker's 29,000 Kc 2+kk re-listed 44 seconds after it was detected gone (M89). Under
    # the honest clock the gap runs from a SIGHTING, where a sub-minute separation can only mean
    # one index walk saw both adverts for the first and only time. So the rail is required
    # exactly where the hazard is: `validate` refuses the honest clock without it.
    certificate_b_min_gap_days: float = 0.0
    # E85 (W11): K-B certifies a pair only when its FAMILY — the connected component of the
    # pairs K-B certified — is consistent with ONE unit. Every clause of K-B is pair-local, and
    # a serial developer's project reads the same way pair by pair as a broker's own re-post
    # chain; what separates them is the chain. `off` is the shipped default, `family` refuses
    # every K-B edge of an impure family, `cell` partitions the family and refuses only the
    # edges that cross a cell or sit in an inconsistent one. Each clause below is its own row
    # because each was priced separately against the 77 adjudicated families (M93).
    # W11's verification then RE-ADJUDICATED those families and inverted the reading (M99): the
    # two ceskereality families that carry 64 of the 77 refusals are one unit each, and on the
    # sealed split the guard changes nothing at all (M100). It stays as data, `off`, with its
    # structural case withdrawn; `pair` is the fourth mode E86 added, the only one whose verdict
    # does not move with the order its family arrived in (M101).
    family_guard_mode: str = "off"
    family_guard_area_tol: float = 0.01
    # How far a number the body prints may sit from the stored area and still be read as this
    # unit's headline size. `stated_areas` bounds mentions at 3x the stored value, which is wide
    # enough to pick a half-house's whole-house number (478609: 103 and 206 against a stored
    # 150) and refuse a true re-post on it.
    family_guard_area_window: float = 0.25
    family_guard_price_tol: float = 0.01
    family_guard_price_rise_max: float = 0.10
    family_guard_overlap_days: float = 0.0
    family_guard_concurrency_clause: bool = True
    family_guard_price_clause: bool = True
    # OFF, and the measurement says why: E60 already ruled that a DIFFERING agency order code is
    # evidence of nothing in either direction, and engine-wide this clause refuses E60's own
    # worked example (23201 x 486034 and 395722 x 496635 — one 43,3 m2 flat at 7 974 910 Kc
    # under codes N115815 and N118731) plus two more pairs of the identical shape in one
    # project. It buys two adjudicated MULTI families whose only separator is a code, and it
    # pays for them by contradicting a standing rule (M95).
    family_guard_ref_code_clause: bool = False
    family_guard_unit_designator_clause: bool = True
    family_guard_disposition_clause: bool = True
    # E88 (W12): the honest clock everywhere EXCEPT where the new-development hazard lives.
    # A K-B certificate inside a new-development serial-poster family is HELD in the band and
    # the pair is decided as it would be with no certificate — never rejected. `off` is the
    # shipped default; `narrow` is vocabulary AND a family of at least
    # `development_size_min_narrow`; `wide` is vocabulary OR a family of at least
    # `development_size_min_wide` OR a block density at or above
    # `development_block_density_min`. Both definitions and every constant below were written
    # down and committed BEFORE the first arm ran (E88) — they are a pre-registration, not a
    # fit, and `autodedup/development.py` carries the two term lists. REFUTED (E89, D32): the
    # rule stays here as DATA and OFF; both definitions cost more true duplicates than the
    # honest clock buys, and the sealed read makes the NARROW row worse than g6 outright.
    development_hold_mode: str = "off"
    # A marker is a property of the FAMILY, so a term has to be in at least this share of its
    # members: a serial poster re-posts one template, so a genuine project marker travels with
    # it. One member mentioning a project next door is not a development family.
    development_vocab_min_share: float = 0.50
    # A co-operative advert on its own is a flat sold as a share (`podil` + `anuita` is how a
    # družstevní byt is priced); it is a DEVELOPMENT marker only beside a family of adverts.
    development_coop_min_size: int = 3
    development_size_min_narrow: int = 3
    development_size_min_wide: int = 4
    # "Many OTHER listings at the same address block in the same category". 20 is not a new
    # number: it is E63's own fungible-catalogue block limb (C3, `context_rule_block_min`),
    # which is the only block-density bar this program has ever written down.
    development_block_density_min: int = 20
    # E61 (W8): two adverts naming a DIFFERENT unit inside one address block are two units,
    # whatever they look like. ON by default — it demotes nothing the operator or gold calls a
    # duplicate, and it is the only rule that can separate a developer's own near-identical
    # adverts. Its evidence is 3 facts in 1 development; the mechanism carries it, not the n.
    unit_designator_veto: bool = True
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
    # E110 (W13): the price `validate` asks for it is E84's pair-gap rail and nothing else,
    # because W11's seal measured exactly that arm (347 of 406 against g6's 300, gained 47,
    # lost 0) and the one reliable false merge it carried is a pair the OPERATOR has since
    # ruled a duplicate. Still False by default: promoting it is a generation decision.
    live_window_from_sighting: bool = False
    # E63 (W8): the band promotion the W8 verification endorsed, and the ONLY one it endorsed.
    # W8a proposed merging band pairs inside a "safe" hazard context; verification refuted that
    # — the context arm carried both of the rule's labelled errors while the context-free arm
    # carried none — and kept this: a near-identical BODY (`containment_max`), an identical
    # current PRICE, and a score at the very top of the isotonic range. On g5 it moves 347 band
    # pairs (6.47%) at 0 labelled negatives on 110 labelled pairs, 0 negative blocks in 66, and
    # 0 in 33 sealed; it selects none of the 18 operator, 63 gold or 43 structural band
    # negatives. OFF by default: the sealed split that read it had already been spent by W8a's
    # 1,476-candidate search, so this is a settings row a promotion turns on (as E57's bridge
    # pass is), not a new default.
    context_rule_enabled: bool = False
    context_rule_min_score: float = 0.9999
    context_rule_containment_min: float = 0.90
    context_rule_price_ratio_min: float = 0.995
    # C1 from the verification: the endorsed arm states no area clause and 2 of its 347 pairs
    # differ by more than 1% (max 5.3%). Adding it is a TIGHTENING — it withholds no labelled
    # pair — so the rule ships with the clause the verifier said any wider rule would need.
    context_rule_area_max: float = 0.01
    # C2: an interior-ratio arm may never rest on three photos. The arm itself is OFF (None);
    # `validate` refuses to enable it below four images, so the condition cannot be forgotten.
    context_rule_interior_min: float | None = None
    context_rule_min_images: float = 4.0
    # C3, the fungible-catalogue veto, one settings row per limb because each costs differently
    # and each cost was measured rather than assumed. On g5: the FROM price withholds 0 of the
    # 347 endorsed pairs, an ALL-stock image warrant 3 — while the block-size limb withholds
    # 112 pairs carrying 66 labelled DUPLICATES, so that limb is off and its cost is recorded
    # rather than paid. `None` disables a census limb. `image_population_min` reads the
    # LEAST-carried SHARED image (see `hazard_context.shared_image_population`).
    context_rule_from_price_veto: bool = True
    context_rule_block_min: int | None = None
    context_rule_image_population_min: int | None = 10
    # C4: a census only ever gets worse, so a promotion taken under one owes a re-read. The cap
    # is per address block, so a block that doubles overnight re-opens a few merges rather than
    # all of them; re-opening means back to the BAND, never an unmerge.
    context_rail_max_reopen_per_block: int = 8
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
        from autodedup.stock import COMBINE_MODES, MIN_CARRIER_BAR

        if self.catalog_carrier_combine not in COMBINE_MODES:
            raise ValueError(
                f"catalog_carrier_combine must be one of {COMBINE_MODES}: "
                f"{self.catalog_carrier_combine}"
            )
        if not 0.0 <= self.catalog_carrier_area_tol < 1.0:
            raise ValueError(
                f"catalog_carrier_area_tol must be in [0, 1): {self.catalog_carrier_area_tol}"
            )
        if not 0.0 < self.catalog_carrier_coverage_min <= 1.0:
            raise ValueError(
                "catalog_carrier_coverage_min must be in (0, 1]: "
                f"{self.catalog_carrier_coverage_min}"
            )
        limbs = ("catalog_min_broker_carriers", "catalog_min_source_carriers",
                 "catalog_min_block_carriers")
        for name in limbs:
            value = getattr(self, name)
            # A bar of one is vacuous: every frame has a carrier, so nothing would ever be stock.
            if value is not None and value < MIN_CARRIER_BAR:
                raise ValueError(f"{name} must be at least {MIN_CARRIER_BAR} or null: {value}")
        if self.catalog_carrier_aware and not any(getattr(self, name) is not None
                                                  for name in limbs):
            raise ValueError(
                "catalog_carrier_aware needs at least one carrier limb: with none configured "
                "the switch is inert and E9 runs exactly as it does with it off"
            )
        if self.certificate_b_min_gap_days < 0.0:
            raise ValueError(
                "certificate_b_min_gap_days must not be negative: "
                f"{self.certificate_b_min_gap_days}"
            )
        for name in ("certificate_b_min_images", "certificate_b_min_matched_images"):
            if getattr(self, name) < 0.0:
                raise ValueError(f"{name} must not be negative: {getattr(self, name)}")
        if (self.certificate_b_min_matched_images > 0.0
                and self.certificate_b_min_matched_images > self.certificate_b_min_images):
            raise ValueError(
                "certificate_b_min_matched_images cannot exceed certificate_b_min_images: a "
                "matched set is a subset of the smaller gallery "
                f"({self.certificate_b_min_matched_images} > {self.certificate_b_min_images})"
            )
        # E110 (W13): the honest clock's price is E84, and it is a price a SEALED read has
        # measured. E87 and E89 both refused a widening because the arm behind it had never
        # been read on a holdout; this one has. W11's seal read exactly this arm — the honest
        # clock, the E84 one-minute pair gap, NO image floor, no family guard, no hold — and
        # measured 347 of 406 sealed reliable labelled duplicates merged against g6's 300,
        # McNemar gained 47 lost 0 (p ~ 7.1e-15), band 4,524 against 4,907, with ONE reliable
        # false merge at pair, block and family grain: the sealed gold negative
        # 522698 x 13221982 (M98/D30 i). On 2026-09-20 the OPERATOR ruled that pair, and its
        # dev-side twin 555448 x 18626270, `same` in the validation UI — and operator labels
        # outrank gold everywhere in this program — so the one number that refused the arm is
        # gone and the sealed read stands as its price. The E65 image floor is no longer
        # required: it withheld 89% of the one-unit re-post chains' merges for a hazard the
        # operator has now ruled is not one (M96, D24 ii).
        #
        # What this acceptance RESTS ON, stated so a later session can revoke it without
        # re-deriving it: one operator ruling about ONE development, the two ceskereality
        # Rezidence K Botici serial-poster families F518656 and F521778. What REVOKES it: an
        # operator NEGATIVE inside a K-B family — a pair the operator rules is two units that
        # K-B certifies under the honest clock. That is a settings change, not a code change:
        # the floor rows are still read, `certificate_b` still enforces them, and putting a
        # number back in `certificate_b_min_images` restores the W9 arm exactly.
        if self.live_window_from_sighting and self.certificate_b_min_gap_days <= 0.0:
            raise ValueError(
                "live_window_from_sighting needs the E84 gap rail: under the honest clock a "
                "once-seen advert's live window is a point, so every pair of once-seen adverts "
                "is disjoint by construction and K-B certifies a 2.8-second separation as a "
                "re-post"
            )
        from autodedup.development import MODES as DEVELOPMENT_HOLD_MODES

        if self.development_hold_mode not in DEVELOPMENT_HOLD_MODES:
            raise ValueError(
                f"development_hold_mode must be one of {DEVELOPMENT_HOLD_MODES}: "
                f"{self.development_hold_mode}"
            )
        if not 0.0 < self.development_vocab_min_share <= 1.0:
            raise ValueError(
                "development_vocab_min_share must be in (0, 1]: "
                f"{self.development_vocab_min_share}"
            )
        # A family has at least two adverts by construction, so a size bar below two is
        # vacuous: it would hold every K-B certificate the engine ever issues.
        for name in ("development_coop_min_size", "development_size_min_narrow",
                     "development_size_min_wide"):
            if getattr(self, name) < 2:
                raise ValueError(f"{name} must be at least 2: {getattr(self, name)}")
        if self.development_block_density_min < 1:
            raise ValueError(
                "development_block_density_min must be at least 1: "
                f"{self.development_block_density_min}"
            )
        # The hold is not a second spelling of E84: it withholds a certificate the honest clock
        # manufactured, and E84 removes the degenerate window that manufactures it. A hold
        # without the gap rail would still certify two adverts one index walk saw once each.
        if self.development_hold_mode != "off" and self.certificate_b_min_gap_days <= 0.0:
            raise ValueError(
                "development_hold_mode needs the E84 gap rail (certificate_b_min_gap_days): "
                "the hold removes a certificate inside a development family, and E84 removes "
                "the one it should never have issued anywhere"
            )
        from autodedup.family import MODES as FAMILY_GUARD_MODES

        if self.family_guard_mode not in FAMILY_GUARD_MODES:
            raise ValueError(
                f"family_guard_mode must be one of {FAMILY_GUARD_MODES}: {self.family_guard_mode}"
            )
        for name in ("family_guard_area_tol", "family_guard_area_window",
                     "family_guard_price_tol", "family_guard_price_rise_max"):
            value = getattr(self, name)
            if not 0.0 <= value < 1.0:
                raise ValueError(f"{name} must be in [0, 1): {value}")
        if self.family_guard_overlap_days < 0.0:
            raise ValueError(
                f"family_guard_overlap_days must not be negative: {self.family_guard_overlap_days}"
            )
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
        if not 0.0 <= self.context_rule_min_score <= 1.0:
            raise ValueError(
                f"context_rule_min_score must be in [0, 1]: {self.context_rule_min_score}"
            )
        for name in ("context_rule_containment_min", "context_rule_price_ratio_min",
                     "context_rule_area_max"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]: {value}")
        if self.context_rule_interior_min is not None:
            if not 0.0 <= self.context_rule_interior_min <= 1.0:
                raise ValueError(
                    f"context_rule_interior_min must be in [0, 1] or null: "
                    f"{self.context_rule_interior_min}"
                )
            if self.context_rule_min_images < CONTEXT_RULE_MIN_IMAGES_FLOOR:
                raise ValueError(
                    "an interior-ratio arm needs at least "
                    f"{CONTEXT_RULE_MIN_IMAGES_FLOOR:g} images (C2 of the W8 verification): "
                    f"context_rule_min_images={self.context_rule_min_images}"
                )
        for name in ("context_rule_block_min", "context_rule_image_population_min"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be at least 1 or null: {value}")
        if self.context_rail_max_reopen_per_block < 1:
            raise ValueError(
                "context_rail_max_reopen_per_block must be at least 1: "
                f"{self.context_rail_max_reopen_per_block}"
            )
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
